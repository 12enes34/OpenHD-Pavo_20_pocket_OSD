#!/usr/bin/env python3
"""FC OSD overlay for the OpenHD ground station (draws the drone's OSD like DJI goggles do).

fc_osd_bridge.py on the air unit sends the flight controller's whole OSD screen (MSP DisplayPort:
voltage, mode, warnings and the FC's own menu, e.g. the Pavo CONFIG / CALI menu) inside standard
MAVLink ENCAPSULATED_DATA messages. OpenHD forwards them to the ground, where this program reads
them from OpenHD's TCP MAVLink server (localhost:5760) and draws them in a transparent, click-through
window on top of QOpenHD.

Usage:  python3 fc_osd_overlay.py                 (auto: tcp 127.0.0.1:5760, fullscreen on screen 0)
        python3 fc_osd_overlay.py --demo          (shows a sample screen, to check placement/size)
"""
import argparse
import os
import socket
import struct
import sys
import time

try:
    from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF
    from PyQt5.QtGui import (QBitmap, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath,
                             QPen, QRegion)
    from PyQt5.QtWidgets import QApplication, QWidget
except ImportError:  # PyQt6 fallback
    from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
    from PyQt6.QtGui import (QBitmap, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath,
                             QPen, QRegion)
    from PyQt6.QtWidgets import QApplication, QWidget

# Betaflight OSD font glyphs -> one printable character each (keeps the grid aligned)
GLYPHS = {0x06: "V", 0x9A: "A", 0x07: "h", 0x0C: "m", 0x0F: "f", 0x7E: "~", 0x2D: "-"}
GLYPHS.update({c: "▮" for c in range(0x90, 0x98)})  # battery icons -> ▮
GLYPHS.update({0x9B: "◷", 0x9C: "◷"})  # flight-time icons -> ◷
GLYPHS.update({0x01: "↑", 0x03: "↓"})  # RSSI/arrow-ish


_qopenhd_cache = [0.0, set()]


def qopenhd_pids():
    """PIDs of running QOpenHD processes (checked at most once a second)."""
    now = time.monotonic()
    if now - _qopenhd_cache[0] < 1.0:
        return _qopenhd_cache[1]
    found = set()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if open("/proc/%s/comm" % pid).read().strip() == "QOpenHD":
                    found.add(int(pid))
            except OSError:
                pass
    _qopenhd_cache[:] = [now, found]
    return found


def qopenhd_running():
    """True while a QOpenHD process exists (always True off Linux)."""
    return not sys.platform.startswith("linux") or bool(qopenhd_pids())


class QOpenHDWindow:
    """Finds the QOpenHD window with plain libX11 (ctypes, no extra packages) so the OSD can follow
    its position/size and hide while it is minimized or covered by another active window."""

    def __init__(self):
        self.ok = False
        if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
            return
        try:
            import ctypes
            import ctypes.util
            c = ctypes
            x = c.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        except (OSError, ImportError):
            return
        self.c, self.x = c, x
        x.XOpenDisplay.restype = c.c_void_p
        x.XOpenDisplay.argtypes = [c.c_char_p]
        x.XDefaultRootWindow.restype = c.c_ulong
        x.XDefaultRootWindow.argtypes = [c.c_void_p]
        x.XInternAtom.restype = c.c_ulong
        x.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
        x.XGetWindowProperty.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_long, c.c_long, c.c_int,
                                         c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_int),
                                         c.POINTER(c.c_ulong), c.POINTER(c.c_ulong),
                                         c.POINTER(c.POINTER(c.c_ubyte))]
        x.XFree.argtypes = [c.c_void_p]
        x.XQueryTree.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_ulong),
                                 c.POINTER(c.POINTER(c.c_ulong)), c.POINTER(c.c_uint)]
        x.XTranslateCoordinates.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_int, c.c_int,
                                            c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(c.c_ulong)]
        x.XGetGeometry.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_int),
                                   c.POINTER(c.c_int), c.POINTER(c.c_uint), c.POINTER(c.c_uint),
                                   c.POINTER(c.c_uint), c.POINTER(c.c_uint)]
        # a window can vanish between two calls; the default X error handler would exit the program
        self._handler = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_void_p)(lambda d, e: 0)
        x.XSetErrorHandler(self._handler)
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            return
        self.root = x.XDefaultRootWindow(self.dpy)
        atom = lambda n: x.XInternAtom(self.dpy, n.encode(), 0)
        self.a_clients, self.a_pid = atom("_NET_CLIENT_LIST"), atom("_NET_WM_PID")
        self.a_class, self.a_state = atom("WM_CLASS"), atom("_NET_WM_STATE")
        self.a_hidden, self.a_active = atom("_NET_WM_STATE_HIDDEN"), atom("_NET_ACTIVE_WINDOW")
        self.a_wtype, self.a_wmstate = atom("_NET_WM_WINDOW_TYPE"), atom("WM_STATE")
        self.a_desktop, self.a_dock = atom("_NET_WM_WINDOW_TYPE_DESKTOP"), atom("_NET_WM_WINDOW_TYPE_DOCK")
        self.ok = True

    def prop(self, win, atom, length=4096):
        c = self.c
        typ, fmt, n, after = c.c_ulong(), c.c_int(), c.c_ulong(), c.c_ulong()
        data = c.POINTER(c.c_ubyte)()
        if self.x.XGetWindowProperty(self.dpy, win, atom, 0, length, 0, 0, c.byref(typ), c.byref(fmt),
                                     c.byref(n), c.byref(after), c.byref(data)) != 0 or not data:
            return []
        try:
            if fmt.value == 32:  # 32-bit properties are C longs in Xlib
                arr = c.cast(data, c.POINTER(c.c_ulong))
                return [arr[i] for i in range(n.value)]
            if fmt.value == 8:
                return bytes(data[:n.value])
            return []
        finally:
            self.x.XFree(data)

    def rect(self, win):
        """(x, y, w, h) of the window's inside, in root coordinates, or None."""
        c = self.c
        root, i, ui = c.c_ulong(), c.c_int(), c.c_uint()
        w, h = c.c_uint(), c.c_uint()
        if not self.x.XGetGeometry(self.dpy, win, c.byref(root), c.byref(i), c.byref(i), c.byref(w),
                                   c.byref(h), c.byref(ui), c.byref(ui)):
            return None
        rx, ry, child = c.c_int(), c.c_int(), c.c_ulong()
        if not self.x.XTranslateCoordinates(self.dpy, win, self.root, 0, 0, c.byref(rx), c.byref(ry),
                                            c.byref(child)):
            return None
        return rx.value, ry.value, w.value, h.value

    def children(self, win):
        c = self.c
        r, p, kids, n = c.c_ulong(), c.c_ulong(), c.POINTER(c.c_ulong)(), c.c_uint()
        if not self.x.XQueryTree(self.dpy, win, c.byref(r), c.byref(p), c.byref(kids), c.byref(n)) or not kids:
            return []
        try:
            return [kids[i] for i in range(n.value)]
        finally:
            self.x.XFree(kids)

    def clients(self):
        """Top-level application windows (EWMH list, or a walk of the window tree if the WM has none)."""
        wins = self.prop(self.root, self.a_clients)
        if wins:
            return wins
        out = []
        for top in self.children(self.root):  # top-level windows and, under reparenting WMs, their frames
            out.append(top)
            out.extend(self.children(top))
        return out

    def locate(self, pids):
        """Returns (x, y, w, h) where the OSD should go, 'hidden', or None if QOpenHD's window is unknown."""
        best = None
        for win in self.clients():
            pid = self.prop(win, self.a_pid)
            cls = self.prop(win, self.a_class)
            if not ((pid and pid[0] in pids) or (isinstance(cls, bytes) and b"qopenhd" in cls.lower())):
                continue
            wm_state = self.prop(win, self.a_wmstate)
            if wm_state[:1] == [0]:
                continue  # withdrawn (never shown / closed)
            r = self.rect(win)
            if r and r[2] > 50 and r[3] > 50 and (best is None or r[2] * r[3] > best[1][2] * best[1][3]):
                best = (win, r, wm_state)
        if best is None:
            return None
        win, r, wm_state = best
        if self.a_hidden in self.prop(win, self.a_state) or wm_state[:1] == [3]:
            return "hidden"  # minimized (EWMH hidden or ICCCM IconicState)
        active = self.prop(self.root, self.a_active)
        if active and active[0] not in (0, win):
            wtype = self.prop(active[0], self.a_wtype)
            apid = self.prop(active[0], self.a_pid)
            ar = self.rect(active[0])
            if (ar and not (self.a_desktop in wtype or self.a_dock in wtype)
                    and not (apid and (apid[0] in pids or apid[0] == os.getpid()))
                    and ar[0] < r[0] + r[2] and r[0] < ar[0] + ar[2] and ar[1] < r[1] + r[3] and r[1] < ar[1] + ar[3]):
                return "hidden"  # another program is in front of QOpenHD
        return r


def stop_other_instances():
    """Only one overlay may run: an old copy (e.g. started at login before an update) would draw a second OSD."""
    if not sys.platform.startswith("linux"):
        return
    import signal
    keep = {os.getpid(), os.getppid()}
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in keep:
            continue
        try:
            args = open("/proc/%s/cmdline" % pid, "rb").read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"fc_osd_overlay.py") for a in args) and b"--demo" not in args \
                and os.path.basename(args[0]).startswith(b"python"):
            try:
                os.kill(int(pid), signal.SIGTERM)
                print("stopped old overlay (pid %s)" % pid, flush=True)
            except OSError as e:
                print("cannot stop old overlay pid %s: %s" % (pid, e), flush=True)


def viewer_running():
    """fc_osd_viewer.py draws the OSD into the video itself; don't draw it twice."""
    try:
        pid = int(open("/tmp/fc_osd_viewer.active").read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def glyph_text(raw):
    return "".join(GLYPHS.get(b, chr(b) if 32 <= b < 127 else " ") for b in raw)


# ---------------------------------------------------------------- MAVLink (only what we need)

def x25(data, crc=0xFFFF):
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


ENCAP_ID, ENCAP_CRC = 131, 223


class MavStream:
    """Extracts ENCAPSULATED_DATA payloads (MAVLink v1 or v2) from a byte stream."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i1, i2 = self.buf.find(b"\xFE"), self.buf.find(b"\xFD")
            cands = [i for i in (i1, i2) if i >= 0]
            if not cands:
                self.buf.clear()
                return out
            i = min(cands)
            del self.buf[:i]
            if self.buf[0] == 0xFE:
                if len(self.buf) < 8:
                    return out
                plen = self.buf[1]
                flen = 6 + plen + 2
                if len(self.buf) < flen:
                    return out
                msgid, hdr_end = self.buf[5], 6
                crc_span = bytes(self.buf[1:6 + plen])
            else:
                if len(self.buf) < 12:
                    return out
                plen, incompat = self.buf[1], self.buf[2]
                flen = 10 + plen + 2 + (13 if incompat & 1 else 0)
                if len(self.buf) < flen:
                    return out
                msgid = self.buf[7] | (self.buf[8] << 8) | (self.buf[9] << 16)
                hdr_end = 10
                crc_span = bytes(self.buf[1:10 + plen])
            if msgid != ENCAP_ID:
                # skip the whole frame if its length is plausible, else resync by one byte
                del self.buf[:flen if plen <= 255 else 1]
                continue
            crc = struct.unpack_from("<H", self.buf, hdr_end + plen)[0]
            if x25(bytes([ENCAP_CRC]), x25(crc_span)) != crc:
                del self.buf[:1]
                continue
            payload = bytes(self.buf[hdr_end:hdr_end + plen]).ljust(255, b"\0")
            del self.buf[:flen]
            out.append(payload[2:])  # drop seqnr -> 253 data bytes


class OsdFrames:
    def __init__(self):
        self.pending = None
        self.pending_id = None
        self.cells = []
        self.last_frame = 0.0

    def handle(self, data):
        if data[:4] != b"OSD1":
            return False
        flags, fid = data[4], data[5]
        if flags & 1:
            self.pending, self.pending_id = [], fid
        if self.pending is None or fid != self.pending_id:
            return False
        i = 6
        while i + 3 <= len(data):
            r, c, n = data[i], data[i + 1], data[i + 2]
            if n == 0 and r == 0 and c == 0:
                break  # zero padding
            self.pending.append((r, c, bytes(data[i + 3:i + 3 + n])))
            i += 3 + n
        if flags & 2:
            self.cells, self.pending = self.pending, None
            self.last_frame = time.monotonic()
            return True
        return False


# ---------------------------------------------------------------- window

class Overlay(QWidget):
    def __init__(self, args):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        if sys.platform.startswith("linux"):
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        super().__init__(None, flags)
        self.args = args
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        screens = QApplication.screens()
        self.screen_rect = screens[min(args.screen, len(screens) - 1)].geometry()
        self.setGeometry(self.screen_rect)
        self.qwin = QOpenHDWindow() if not args.fullscreen else None
        self.place, self.next_place = None, 0.0
        self.stream, self.frames = MavStream(), OsdFrames()
        self.sock = None
        self.port_i = 0
        self.next_connect = 0.0
        self.max_col = self.max_row = 0
        self.drawn_key = None
        if args.demo:
            self.frames.cells = demo_cells()
            self.frames.last_frame = float("inf")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)
        self.raise_timer = QTimer(self)
        self.raise_timer.timeout.connect(self.raise_)
        self.raise_timer.start(2000)

    # ---- network
    def connect_source(self):
        if self.args.demo or self.sock or time.monotonic() < self.next_connect:
            return
        self.next_connect = time.monotonic() + 2
        if self.args.udp:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.args.udp))
        else:
            port = self.args.ports[self.port_i % len(self.args.ports)]
            self.port_i += 1
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect((self.args.host, port))
            except OSError:
                s.close()
                return
            print("connected to OpenHD MAVLink tcp %s:%d" % (self.args.host, port), flush=True)
        s.setblocking(False)
        self.sock = s

    def read_source(self):
        if not self.sock:
            return False
        got = False
        while True:
            try:
                data = self.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                data = b""
            if not data:
                print("connection closed, retrying", flush=True)
                self.sock.close()
                self.sock = None
                break
            for payload in self.stream.feed(data):
                got |= self.frames.handle(payload)
        return got

    def placement(self):
        """Where to draw: the QOpenHD window if it can be found, else the whole screen; None = hidden."""
        now = time.monotonic()
        if now >= self.next_place:
            self.next_place = now + 0.2
            r = None
            if self.qwin and self.qwin.ok and qopenhd_pids():
                try:
                    # QOpenHD runs but has no window (yet): draw nothing rather than over the whole screen
                    r = self.qwin.locate(qopenhd_pids()) or "hidden"
                except Exception as e:  # never let window tracking kill the OSD
                    print("window tracking off: %r" % e, flush=True)
                    self.qwin = None
            if r == "hidden":
                if self.place is not None:
                    print("QOpenHD window not visible (minimized, covered or not open yet) -> OSD hidden", flush=True)
                self.place = None
            elif r:
                dpr = self.devicePixelRatioF() or 1.0  # X11 pixels -> Qt logical pixels
                self.place = tuple(int(round(v / dpr)) for v in r)
            else:
                g = self.screen_rect
                self.place = (g.x(), g.y(), g.width(), g.height())
        return self.place

    def tick(self):
        self.connect_source()
        self.read_source()
        place = self.placement()
        alive = (time.monotonic() - self.frames.last_frame < self.args.timeout and not viewer_running()
                 and (self.args.always or qopenhd_running()) and place is not None)
        if place and (self.x(), self.y(), self.width(), self.height()) != place:
            print("OSD area %d,%d %dx%d" % place, flush=True)
            self.setGeometry(*place)
        cells = self.frames.cells if alive else []
        for r, c, raw in cells:
            self.max_col = max(self.max_col, c + len(raw))
            self.max_row = max(self.max_row, r + 1)
        key = (tuple(cells), self.width(), self.height())
        if key == self.drawn_key:
            return
        self.drawn_key = key
        self.image = self.render_image(cells)
        if cells:
            if sys.platform.startswith("linux"):
                # works without a compositor: only the text pixels belong to the window
                self.setMask(QRegion(QBitmap.fromImage(self.image.createAlphaMask())))
            self.show()
            self.update()
        else:
            self.hide()

    # ---- drawing
    def grid(self):
        hd = self.max_col > 30 or self.max_row > 16 or self.args.hd
        return (53, 20) if hd else (30, 16)

    def render_image(self, cells):
        w, h = self.width(), self.height()
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        if not cells:
            return img
        cols, rows = self.grid()
        # 16:9 canvas centred on the screen, scaled by --scale
        cw = min(w, h * 16 / 9) * self.args.scale
        ch = cw * 9 / 16
        x0 = (w - cw) / 2 + self.args.offset_x
        y0 = (h - ch) / 2 + self.args.offset_y
        cell_w, cell_h = cw / cols, ch / rows
        font = QFont(self.args.font)
        font.setBold(True)
        font.setPixelSize(max(6, int(cell_h * 0.8)))
        fm = QFontMetricsF(font)
        # shrink if the font is wider than a cell
        adv = fm.horizontalAdvance("M")
        if adv > cell_w * 0.98:
            font.setPixelSize(max(6, int(font.pixelSize() * cell_w * 0.98 / adv)))
            fm = QFontMetricsF(font)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        for r, c, raw in cells:
            text = glyph_text(raw)
            for k, ch_ in enumerate(text):
                if ch_ == " ":
                    continue
                x = x0 + (c + k) * cell_w + (cell_w - fm.horizontalAdvance(ch_)) / 2
                y = y0 + r * cell_h + (cell_h + fm.ascent() - fm.descent()) / 2
                path.addText(QPointF(x, y), font, ch_)
        outline = max(2.0, font.pixelSize() / 7)
        p.strokePath(path, QPen(QColor(0, 0, 0, 230), outline, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.fillPath(path, QColor(255, 255, 255))
        p.end()
        return img

    def paintEvent(self, _event):
        if getattr(self, "image", None) is not None:
            p = QPainter(self)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.drawImage(0, 0, self.image)
            p.end()


def demo_cells():
    rows = [b"- CONFIG -", b" TOF   OFF", b" OPF   ON", b" LED   OFF", b">CALI  OK", b" BACK"]
    cells = [(6 + i, 22, t) for i, t in enumerate(rows)]
    cells += [(15, 2, b"\x977.73\x06"), (16, 2, b"\x9c00:12"), (15, 43, b"SPORT"), (16, 43, b" MID")]
    return cells


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ports", type=int, nargs="+", default=[5760, 1445], help="OpenHD ground TCP MAVLink port(s)")
    ap.add_argument("--udp", type=int, default=0, help="listen on this UDP port instead of TCP (testing)")
    ap.add_argument("--screen", type=int, default=0)
    ap.add_argument("--scale", type=float, default=1.0, help="OSD canvas size relative to the screen")
    ap.add_argument("--offset-x", type=int, default=0)
    ap.add_argument("--offset-y", type=int, default=0)
    ap.add_argument("--font", default="DejaVu Sans Mono")
    ap.add_argument("--hd", action="store_true", help="force the 53x20 HD canvas")
    ap.add_argument("--timeout", type=float, default=3.0, help="hide the OSD after N s without data")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--always", action="store_true", help="show the OSD even when QOpenHD is not running")
    ap.add_argument("--fullscreen", action="store_true",
                    help="always cover the whole screen instead of following the QOpenHD window")
    args = ap.parse_args()
    if args.demo:
        args.always = True
    else:
        stop_other_instances()
    app = QApplication(sys.argv[:1])
    w = Overlay(args)
    w.tick()
    sys.exit(app.exec() if hasattr(app, "exec") else app.exec_())


if __name__ == "__main__":
    main()
