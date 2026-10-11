#!/usr/bin/env python3
"""OpenHD ground video viewer with the FC OSD burned INTO the picture (like DJI goggles).

Video: OpenHD ground always forwards the primary video stream (RTP/H.264) to udp://127.0.0.1:5800
       in addition to QOpenHD's 5600, so this viewer runs next to QOpenHD without disturbing it.
OSD:   read from OpenHD's TCP MAVLink server (localhost:5760); fc_osd_bridge.py on the air unit sends
       the FC's whole OSD screen (incl. its menu) as ENCAPSULATED_DATA "OSD1" frames.
The OSD is blended into every video frame (overlaycomposition), so it scales with the picture and is part
of the optional recording.

Keys:  F = fullscreen on/off   R = start/stop recording (~/Videos, OSD included)
       O = OSD on/off          Q / Esc = quit
"""
import argparse
import os
import socket
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fc_osd_overlay import MavStream, OsdFrames, glyph_text  # noqa: E402

try:
    from PyQt5.QtCore import Qt, QTimer, QRect, QPointF
    from PyQt5.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen
    from PyQt5.QtWidgets import QApplication, QWidget
except ImportError:
    from PyQt6.QtCore import Qt, QTimer, QRect, QPointF
    from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen
    from PyQt6.QtWidgets import QApplication, QWidget

ACTIVE_FILE = "/tmp/fc_osd_viewer.active"  # fc_osd_overlay.py hides itself while this viewer runs


# ---------------------------------------------------------------- OSD data (background thread)

class OsdSource(threading.Thread):
    def __init__(self, host, ports, timeout):
        super().__init__(daemon=True)
        self.host, self.ports, self.timeout = host, ports, timeout
        self.frames = OsdFrames()
        self.lock = threading.Lock()
        self.status = "OSD: waiting for OpenHD"

    def cells(self):
        with self.lock:
            if time.monotonic() - self.frames.last_frame > self.timeout:
                return []
            return list(self.frames.cells)

    def run(self):
        i = 0
        while True:
            port = self.ports[i % len(self.ports)]
            i += 1
            try:
                s = socket.create_connection((self.host, port), timeout=1)
            except OSError:
                time.sleep(1)
                continue
            s.settimeout(1)
            self.status = "OSD: connected tcp %d" % port
            stream = MavStream()
            while True:
                try:
                    data = s.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    data = b""
                if not data:
                    break
                for payload in stream.feed(data):
                    with self.lock:
                        self.frames.handle(payload)
            s.close()
            self.status = "OSD: connection lost, retrying"
            time.sleep(1)


# ---------------------------------------------------------------- OSD drawing (Qt, streaming thread)

class OsdPainter:
    """Renders the OSD with Qt into a transparent BGRA image and hands it to GStreamer's
    overlaycomposition element, which blends it into every video frame (so recordings include it).
    Needs no python3-gi-cairo (not installed on the OpenHD ground image)."""

    def __init__(self, source, args):
        self.source, self.args = source, args
        self.enabled = True
        self.width = self.height = 0
        self.max_col = self.max_row = 0
        self.cache_key = None
        self.composition = None

    def on_caps(self, _overlay, caps, *_window_size):
        s = caps.get_structure(0)
        self.width, self.height = s.get_value("width"), s.get_value("height")
        self.cache_key = None

    def on_draw(self, _overlay, _sample):
        if not self.enabled or not self.width:
            return None
        cells = self.source.cells()
        key = (tuple(cells), self.width, self.height)
        if key != self.cache_key:
            self.cache_key = key
            self.composition = self.build(cells) if cells else None
        return self.composition

    def build(self, cells):
        for r, c, raw in cells:
            self.max_col = max(self.max_col, c + len(raw))
            self.max_row = max(self.max_row, r + 1)
        hd = self.args.hd or self.max_col > 30 or self.max_row > 16
        cols, rows = (53, 20) if hd else (30, 16)
        w, h = self.width, self.height
        cw, ch = w / cols, h / rows
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        font = QFont(self.args.font)
        font.setBold(True)
        font.setPixelSize(max(6, int(ch * 0.8)))
        fm = QFontMetricsF(font)
        adv = fm.horizontalAdvance("M")
        if adv > cw * 0.98:
            font.setPixelSize(max(6, int(font.pixelSize() * cw * 0.98 / adv)))
            fm = QFontMetricsF(font)
        path = QPainterPath()
        for r, c, raw in cells:
            for k, chr_ in enumerate(glyph_text(raw)):
                if chr_ == " ":
                    continue
                x = (c + k) * cw + (cw - fm.horizontalAdvance(chr_)) / 2
                y = r * ch + (ch + fm.ascent() - fm.descent()) / 2
                path.addText(QPointF(x, y), font, chr_)
        if path.isEmpty():
            return None
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.strokePath(path, QPen(QColor(0, 0, 0, 230), max(2.0, ch / 9), Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.fillPath(path, QColor(255, 255, 255))
        p.end()
        # only blend the area that actually contains OSD text
        br = path.boundingRect().adjusted(-ch / 4, -ch / 4, ch / 4, ch / 4).toAlignedRect()
        br = br.intersected(img.rect())
        crop = img.copy(br)
        stride = crop.bytesPerLine()
        ptr = crop.constBits()
        ptr.setsize(crop.sizeInBytes() if hasattr(crop, "sizeInBytes") else crop.byteCount())
        buf = Gst.Buffer.new_wrapped(bytes(ptr))
        GstVideo.buffer_add_video_meta_full(buf, GstVideo.VideoFrameFlags.NONE, GstVideo.VideoFormat.BGRA,
                                            br.width(), br.height(), 1, [0, 0, 0, 0], [stride, 0, 0, 0])
        rect = GstVideo.VideoOverlayRectangle.new_raw(
            buf, br.x(), br.y(), br.width(), br.height(),
            GstVideo.VideoOverlayFormatFlags.PREMULTIPLIED_ALPHA)
        return GstVideo.VideoOverlayComposition.new(rect)


# ---------------------------------------------------------------- video pipeline

def pick_decoder(prefer):
    order = [prefer] if prefer else []
    order += ["vah264dec", "vaapih264dec", "avdec_h264", "openh264dec"]
    for name in order:
        if name and Gst.ElementFactory.find(name):
            return name
    raise SystemExit("no H.264 decoder found (install gstreamer1.0-libav or a VA driver)")


def pick_sink(prefer=""):
    """xvimagesink (hardware scaling) if the display has an Xv port, else 'qt' (appsink + QPainter)."""
    if prefer == "qt":
        return "qt"
    for name in ([prefer] if prefer else []) + ["xvimagesink"]:
        el = Gst.ElementFactory.make(name, None) if Gst.ElementFactory.find(name) else None
        if el is None:
            continue
        ok = el.set_state(Gst.State.READY) != Gst.StateChangeReturn.FAILURE
        el.set_state(Gst.State.NULL)
        if ok:
            return name
        print("video sink %s unavailable, using Qt rendering" % name, flush=True)
    return "qt"


class Recorder:
    """Dynamic tee branch: queue ! videoconvert ! x264enc ! h264parse ! matroskamux ! filesink."""

    def __init__(self, pipeline, tee, directory):
        self.pipeline, self.tee, self.dir = pipeline, tee, directory
        self.bin = None
        self.tee_pad = None
        self.path = None

    @property
    def active(self):
        return self.bin is not None

    def start(self):
        if self.bin:
            return
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, time.strftime("osd_%Y%m%d_%H%M%S.mkv"))
        self.bin = Gst.parse_bin_from_description(
            "queue max-size-buffers=0 max-size-time=2000000000 ! videoconvert ! "
            "x264enc tune=zerolatency speed-preset=veryfast bitrate=6000 key-int-max=60 ! h264parse ! "
            "matroskamux ! filesink location=\"%s\" async=false" % self.path, True)
        self.pipeline.add(self.bin)
        self.tee_pad = self.tee.request_pad_simple("src_%u") if hasattr(self.tee, "request_pad_simple") \
            else self.tee.get_request_pad("src_%u")
        self.tee_pad.link(self.bin.get_static_pad("sink"))
        self.bin.sync_state_with_parent()
        print("recording ->", self.path, flush=True)

    def stop(self):
        if not self.bin:
            return
        rec_bin, pad = self.bin, self.tee_pad
        self.bin = self.tee_pad = None

        def on_blocked(blocked_pad, _info):
            blocked_pad.unlink(rec_bin.get_static_pad("sink"))
            rec_bin.get_static_pad("sink").send_event(Gst.Event.new_eos())  # finalize the mkv
            def finish():
                time.sleep(1.0)
                rec_bin.set_state(Gst.State.NULL)
                self.pipeline.remove(rec_bin)
                self.tee.release_request_pad(blocked_pad)
                print("recording saved:", self.path, flush=True)
            threading.Thread(target=finish, daemon=True).start()
            return Gst.PadProbeReturn.REMOVE

        pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, on_blocked)


class Viewer(QWidget):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.setWindowTitle("FC OSD Viewer")
        self.setStyleSheet("background-color: black;")
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.resize(1280, 720)
        self.osd_source = OsdSource(args.host, args.ports, args.timeout)
        self.osd_source.start()
        self.painter = OsdPainter(self.osd_source, args)

        Gst.init(None)
        dec = pick_decoder(args.decoder)
        sink = pick_sink(args.sink)
        src = args.src or ("udpsrc port=%d caps=\"application/x-rtp, media=video, "
                           "clock-rate=90000, encoding-name=H264, payload=96\" ! rtph264depay" % args.port)
        if sink == "qt":
            display = ("videoconvert ! video/x-raw,format=BGRx ! appsink name=vsink max-buffers=1 drop=true "
                       "sync=false")
        else:
            display = "videoconvert ! %s name=vsink sync=false force-aspect-ratio=true" % sink
        desc = ("%s ! h264parse ! %s ! videoconvert ! overlaycomposition name=osd ! tee name=t "
                "t. ! queue leaky=downstream max-size-buffers=2 ! %s" % (src, dec, display))
        self.frame = None
        print("pipeline:", desc, flush=True)
        self.pipeline = Gst.parse_launch(desc)
        osd = self.pipeline.get_by_name("osd")
        osd.connect("caps-changed", self.painter.on_caps)
        osd.connect("draw", self.painter.on_draw)
        self.recorder = Recorder(self.pipeline, self.pipeline.get_by_name("t"), args.record_dir)
        self.vsink = self.pipeline.get_by_name("vsink")
        self.qt_render = sink == "qt"
        if self.qt_render:
            self.frame_timer = QTimer(self)
            self.frame_timer.timeout.connect(self.pull_frame)
            self.frame_timer.start(8)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            GstVideo.VideoOverlay.set_window_handle(self.vsink, int(self.winId()))
        self.bus = self.pipeline.get_bus()
        self.pipeline.set_state(Gst.State.PLAYING)
        if args.record:
            self.recorder.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_bus)
        self.timer.start(100)
        try:
            with open(ACTIVE_FILE, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

    def pull_frame(self):
        sample = self.vsink.emit("try-pull-sample", 0)
        if sample is None:
            return
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        stride = len(data) // h
        self.frame = QImage(data, w, h, stride, QImage.Format.Format_RGB32).copy()
        self.update()

    def paintEvent(self, _e):
        if not self.qt_render:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.frame is not None:
            img = self.frame.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x, y = (self.width() - img.width()) // 2, (self.height() - img.height()) // 2
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(QRect(x, y, img.width(), img.height()), self.frame)
        p.end()

    def poll_bus(self):
        while True:
            msg = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS)
            if not msg:
                break
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                print("GStreamer error:", err.message, dbg, flush=True)
            elif msg.type == Gst.MessageType.WARNING:
                w, _ = msg.parse_warning()
                print("GStreamer warning:", w.message, flush=True)
        rec = "  [REC]" if self.recorder.active else ""
        self.setWindowTitle("FC OSD Viewer  -  %s%s" % (self.osd_source.status, rec))

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Q, Qt.Key.Key_Escape):
            self.close()
        elif k == Qt.Key.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif k == Qt.Key.Key_R:
            self.recorder.stop() if self.recorder.active else self.recorder.start()
        elif k == Qt.Key.Key_O:
            self.painter.enabled = not self.painter.enabled

    def closeEvent(self, e):
        self.recorder.stop()
        time.sleep(1.2 if self.args.record or self.recorder.path else 0)
        self.pipeline.set_state(Gst.State.NULL)
        try:
            os.unlink(ACTIVE_FILE)
        except OSError:
            pass
        e.accept()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5800, help="UDP port of the RTP/H.264 video (OpenHD ground: 5800)")
    ap.add_argument("--src", default="", help="custom GStreamer source ending in H.264 (testing)")
    ap.add_argument("--decoder", default="", help="force a decoder element, e.g. openh264dec")
    ap.add_argument("--sink", default="", help="force a video sink, e.g. ximagesink")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ports", type=int, nargs="+", default=[5760, 1445], help="OpenHD TCP MAVLink port(s)")
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--font", default="DejaVu Sans Mono")
    ap.add_argument("--hd", action="store_true", help="force the 53x20 HD OSD canvas")
    ap.add_argument("--record", action="store_true", help="start recording immediately")
    ap.add_argument("--record-dir", default=os.path.expanduser("~/Videos"))
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--duration", type=float, default=0, help="close after N seconds (testing)")
    args = ap.parse_args()
    app = QApplication(sys.argv[:1])
    v = Viewer(args)
    v.show() if args.windowed else v.showFullScreen()
    if args.duration:
        QTimer.singleShot(int(args.duration * 1000), v.close)
    sys.exit(app.exec() if hasattr(app, "exec") else app.exec_())


if __name__ == "__main__":
    main()
