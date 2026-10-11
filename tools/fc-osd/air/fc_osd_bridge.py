#!/usr/bin/env python3
"""Flight-controller -> standard MAVLink bridge for OpenHD (air unit).

OpenHD only understands standard MAVLink on its FC port. This bridge sits between the FC and
OpenHD and presents a virtual FC port (pty, default /run/fc_osd_bridge) to OpenHD.

Sources (all optional, auto-detected):

  UART (default /dev/serial0, the wire that used to go to the DJI unit):
    * standard MAVLink (ArduPilot / INAV / PX4)  -> passed through unchanged, both directions
    * MSP DisplayPort OSD (BETAFPV Pavo, Betaflight with a DJI/HD VTX port)
        - OSD text is decoded: battery voltage, flight mode, armed/disarmed, warnings,
          and the FC's on-screen menu (e.g. the Pavo CONFIG / CALI menu) -> STATUSTEXT
        - read-only MSP queries; a real Betaflight answers them, giving attitude,
          altitude, GPS, RC and arming state
    The baud rate is searched automatically if nothing is recognised.

  USB (BETAFPV Pavo proprietary MAVLink, VID 0483/0493 PID 5740):
    attitude, altitude (ToF / baro), optical-flow speed, RC, voltage.
    Only the 1 Hz heartbeat the official BETAFPV Configator sends is ever written to it.

Nothing that changes FC settings is ever sent. Only the Python 3 standard library is used.
"""
import argparse
import glob
import math
import os
import re
import select
import signal
import struct
import sys
import time

# ============================================================== MAVLink helpers

def x25(data, crc=0xFFFF):
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def v1_frame(msgid, crc_extra, payload, seq, sysid, compid):
    body = bytes([len(payload), seq & 0xFF, sysid, compid, msgid]) + payload
    return b"\xFE" + body + struct.pack("<H", x25(bytes([crc_extra]), x25(body)))


def find_mavlink_heartbeat(buf):
    """True if buf holds a CRC-valid standard MAVLink v1/v2 HEARTBEAT (msgid 0, crc_extra 50)."""
    i = 0
    while True:
        a, b = buf.find(b"\xFE", i), buf.find(b"\xFD", i)
        cands = [x for x in (a, b) if x >= 0]
        if not cands:
            return False
        i = min(cands)
        if buf[i] == 0xFE and i + 8 + 9 <= len(buf) and buf[i + 1] == 9 and buf[i + 5] == 0:
            fr = buf[i:i + 17]
            if x25(bytes([50]), x25(fr[1:15])) == struct.unpack("<H", fr[15:17])[0]:
                return True
        if buf[i] == 0xFD and i + 12 <= len(buf):
            plen = buf[i + 1]
            msgid = buf[i + 7] | (buf[i + 8] << 8) | (buf[i + 9] << 16)
            end = i + 10 + plen + 2
            if msgid == 0 and 1 <= plen <= 9 and end <= len(buf):
                fr = buf[i:end]
                if x25(bytes([50]), x25(fr[1:10 + plen])) == struct.unpack("<H", fr[-2:])[0]:
                    return True
        i += 1


# BETAFPV USB dialect (BETAFPV Configator libraries/mavlink.js); field order = wire order.
PAVO = {
    3: ("SYS_STATUS", "<ffBBB", ("voltage", "current", "present", "enabled", "health"), 83),
    4: ("IMU", "<fffffffff", ("ax", "ay", "az", "gx", "gy", "gz", "mx", "my", "mz"), 53),
    5: ("ATTITUDE", "<hhh", ("roll", "pitch", "yaw"), 209),
    6: ("LOCAL_POSITION", "<ffffff", ("x", "y", "z", "vx", "vy", "vz"), 245),
    7: ("RC_CHANNELS", "<12hBB", tuple("ch%d" % i for i in range(1, 13)) + ("count", "rssi"), 45),
    12: ("ALTITUDE", "<hh", ("tof", "baro"), 79),
}
PAVO_HEARTBEAT = v1_frame(1, 196, bytes([0, 1]), 0, 0, 0)  # byte-identical to the official app


class PavoParser:
    def __init__(self):
        self.buf = bytearray()
        self.bad_crc = 0

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(0xFE)
            if i < 0:
                self.buf.clear()
                return out
            del self.buf[:i]
            if len(self.buf) < 8:
                return out
            flen = 8 + self.buf[1]
            if len(self.buf) < flen:
                return out
            frame = bytes(self.buf[:flen])
            msgid = frame[5]
            if not 1 <= msgid <= 16:
                del self.buf[:1]
                continue
            info = PAVO.get(msgid)
            if info is None:
                del self.buf[:flen]
                continue
            if x25(bytes([info[3]]), x25(frame[1:-2])) != struct.unpack("<H", frame[-2:])[0]:
                self.bad_crc += 1
                del self.buf[:1]
                continue
            del self.buf[:flen]
            need = struct.calcsize(info[1])
            payload = frame[6:-2].ljust(need, b"\0")[:need]
            out.append((msgid, dict(zip(info[2], struct.unpack(info[1], payload)))))


# ============================================================== MSP helpers

MSP_STATUS, MSP_RAW_GPS, MSP_RC, MSP_ATTITUDE, MSP_ALTITUDE, MSP_ANALOG = 101, 106, 105, 108, 109, 110
MSP_DISPLAYPORT = 182
MSP_POLL = (MSP_ATTITUDE, MSP_ALTITUDE, MSP_ANALOG, MSP_RC, MSP_STATUS, MSP_ATTITUDE, MSP_RAW_GPS)


def msp_request(cmd):
    return b"$M<" + bytes([0, cmd, cmd])


class MspParser:
    """MSP v1 replies/pushes from the FC ('$M>')."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(b"$M")
            if i < 0:
                del self.buf[:max(0, len(self.buf) - 1)]
                return out
            del self.buf[:i]
            if len(self.buf) < 6:
                return out
            direction, size, cmd = self.buf[2], self.buf[3], self.buf[4]
            if direction not in b"><!":
                del self.buf[:2]
                continue
            end = 5 + size + 1
            if len(self.buf) < end:
                return out
            ck = size ^ cmd
            for b in self.buf[5:5 + size]:
                ck ^= b
            if ck == self.buf[end - 1] and direction == ord(">"):
                out.append((cmd, bytes(self.buf[5:5 + size])))
            del self.buf[:end if ck == self.buf[end - 1] else 2]


# ============================================================== OSD (MSP DisplayPort) decoding

# Betaflight-font glyphs that appear inside OSD elements
GLYPH_TEXT = {0x06: "V", 0x9A: "A", 0x07: "mAh", 0x0C: "m", 0x0F: "ft"}
MODE_WORDS = {
    # BETAFPV Pavo
    "NORMAL": ("NORMAL", 16), "SPORT": ("SPORT", 13), "MANUAL": ("MANUAL", 1),
    "TURTLE": ("TURTLE", 14), "LAND": ("LAND", 9), "AUTOLAND": ("AUTOLAND", 9),
    # Betaflight (OSD flight mode element)
    "ANGL": ("ANGLE", 0), "HOR": ("HORIZON", 0), "ACRO": ("ACRO", 1), "AIR": ("AIR", 1),
    "!FS!": ("FAILSAFE", 9), "RESC": ("RESCUE", 6), "HEAD": ("HEADFREE", 0), "PASS": ("PASSTHRU", 1),
}
SPEED_WORDS = {"SLOW", "MID", "FAST"}
MENU_MARKERS = ("EXIT", "BACK", "SAVE", "- MAIN -", "MAIN", "--")


def osd_text(raw):
    return "".join(GLYPH_TEXT.get(c, chr(c) if 32 <= c < 127 else "") for c in raw).strip()


class OsdDecoder:
    STALE_S = 1.0

    def __init__(self, ignore=()):
        self.cells = {}  # (row, col) -> (raw bytes, time)
        self.ignore = {s.upper() for s in ignore}
        self.frames = 0

    def handle(self, payload, now):
        """Returns a snapshot dict on 'draw', else None."""
        if not payload:
            return None
        sub = payload[0]
        if sub == 2:  # clear screen
            self.cells.clear()
        elif sub == 3 and len(payload) >= 4:  # write string: row, col, attribute, text
            row, col = payload[1], payload[2]
            self.cells[(row, col)] = (bytes(payload[4:]), now)
        elif sub == 4:  # draw
            self.cells = {k: v for k, v in self.cells.items() if now - v[1] < self.STALE_S}
            self.frames += 1
            return self.snapshot()
        return None

    def snapshot(self):
        items = sorted(self.cells.items())
        texts = [(r, c, osd_text(raw), raw) for (r, c), (raw, _) in items]
        snap = {"voltage": None, "mode": None, "speed": None, "disarm": False,
                "menu": None, "warnings": [], "cells": [(r, c, raw) for (r, c), (raw, _) in items]}
        for r, c, t, raw in texts:
            m = re.search(rb"(\d{1,2}\.\d{1,2})\x06", raw)
            if m and snap["voltage"] is None:
                snap["voltage"] = float(m.group(1))
        words = [t.upper() for _, _, t, _ in texts if t]
        is_menu = any(w.startswith(">") for w in words) and any(
            any(mk in w for mk in MENU_MARKERS) for w in words)
        if is_menu:
            rows = {}
            for r, c, t, _ in texts:
                if t:
                    rows.setdefault(r, []).append((c, t))
            snap["menu"] = ["  ".join(t for _, t in sorted(v)) for _, v in sorted(rows.items())]
            return snap
        for w in words:
            key = w.strip()
            if key in MODE_WORDS:
                snap["mode"] = MODE_WORDS[key]
            elif key in SPEED_WORDS:
                snap["speed"] = key
            elif "DISARM" in key:
                snap["disarm"] = True
            elif (re.fullmatch(r"[A-Z!<>\- ]{3,}", key) and key not in self.ignore
                  and not re.search(r"\d", key)):
                snap["warnings"].append(key)
        return snap


class OsdForwarder:
    """Sends the FC's whole OSD screen to the ground (fc_osd_overlay.py draws it like a DJI goggle OSD).

    Carried in standard MAVLink ENCAPSULATED_DATA (131), which OpenHD forwards untouched:
      data = b"OSD1" | flags (1=start, 2=end) | frame id | entries: row, col, len, raw OSD bytes
    A frame is sent when the screen changes (max MAX_HZ) and repeated every second.
    """
    MAX_HZ = 8.0
    KEEPALIVE_S = 1.0

    def __init__(self):
        self.last_cells = None
        self.last_sent = -1e9
        self.frame_id = 0
        self.seqnr = 0

    def on_frame(self, cells, mav, now):
        changed = cells != self.last_cells
        due = now - self.last_sent >= (1 / self.MAX_HZ if changed else self.KEEPALIVE_S)
        if not due:
            return
        entries = [bytes([r, c, min(len(raw), 60)]) + raw[:60] for r, c, raw in cells if r < 64 and c < 128]
        packets, cur = [], b""
        for e in entries:
            if len(cur) + len(e) > 253 - 6:
                packets.append(cur)
                cur = b""
            cur += e
        packets.append(cur)
        self.frame_id = (self.frame_id + 1) & 0xFF
        for i, body in enumerate(packets):
            flags = (1 if i == 0 else 0) | (2 if i == len(packets) - 1 else 0)
            mav.encapsulated(self.seqnr, b"OSD1" + bytes([flags, self.frame_id]) + body)
            self.seqnr += 1
        self.last_cells = cells
        self.last_sent = now


# ============================================================== I/O

BAUD_CANDIDATES = (115200, 57600, 230400, 460800, 921600)


class LinuxSerial:
    def __init__(self, path, baud=115200):
        import tty
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self.fd)
        self.set_baud(baud)

    def set_baud(self, baud):
        import termios
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = getattr(termios, "B%d" % baud)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.baud = baud

    def read(self, n=4096):
        try:
            data = os.read(self.fd, n)
        except BlockingIOError:
            return b""
        if not data:
            raise OSError("device closed")
        return data

    def write(self, data):
        try:
            os.write(self.fd, data)
        except BlockingIOError:
            pass

    def close(self):
        os.close(self.fd)


class PySerial:
    """Windows/macOS fallback for testing (needs pyserial)."""

    def __init__(self, path, baud=115200):
        import serial
        self.s = serial.Serial(path, baud, timeout=0)
        self.fd = None
        self.baud = baud

    def set_baud(self, baud):
        self.s.baudrate = baud
        self.baud = baud

    def read(self, n=4096):
        return self.s.read(n)

    def write(self, data):
        self.s.write(data)

    def close(self):
        self.s.close()


def open_serial(path, baud):
    return LinuxSerial(path, baud) if sys.platform.startswith("linux") else PySerial(path, baud)


def find_pavo_usb():
    if sys.platform.startswith("linux"):
        for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
            dev = os.path.realpath(os.path.join(tty, "device"))
            for up in (dev, os.path.dirname(dev)):
                try:
                    vid = open(os.path.join(up, "idVendor")).read().strip()
                    pid = open(os.path.join(up, "idProduct")).read().strip()
                except OSError:
                    continue
                if vid in ("0483", "0493") and pid == "5740":
                    return "/dev/" + os.path.basename(tty)
        return None
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        if p.vid in (0x0483, 0x0493) and p.pid == 0x5740:
            return p.device
    return None


class PtyOutput:
    """Virtual serial port for OpenHD (FC_UART_CONN = the symlink)."""
    MAX_PENDING = 16 * 1024

    def __init__(self, link):
        import tty
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.slave_name = os.ttyname(slave)
        self._slave = slave  # keep open: writes never fail before OpenHD connects
        self.link = link
        try:
            os.unlink(link)
        except FileNotFoundError:
            pass
        os.symlink(self.slave_name, link)
        os.set_blocking(self.master, False)
        self.pending = bytearray()
        self.fd = self.master

    def write(self, data):
        # whole frames only: keep the unwritten tail of a partial write queued
        if len(self.pending) + len(data) <= self.MAX_PENDING:
            self.pending += data
        self.flush()

    def flush(self):
        while self.pending:
            try:
                n = os.write(self.master, self.pending)
            except BlockingIOError:
                return
            del self.pending[:n]

    def read(self):
        try:
            return os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return b""

    def close(self):
        try:
            os.unlink(self.link)
        except OSError:
            pass


class UdpOutput:
    def __init__(self, host, port):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.addr = (host, port)
        self.fd = None

    def write(self, data):
        self.sock.sendto(data, self.addr)

    def flush(self):
        pass

    def read(self):
        return b""

    def close(self):
        self.sock.close()


class FileOutput:
    def __init__(self, path):
        self.f = open(path, "wb")
        self.fd = None

    def write(self, data):
        self.f.write(data)
        self.f.flush()

    def flush(self):
        pass

    def read(self):
        return b""

    def close(self):
        self.f.close()


def open_output(spec):
    kind, _, rest = spec.partition(":")
    if kind == "pty":
        return PtyOutput(rest or "/run/fc_osd_bridge")
    if kind == "udp":
        host, _, port = rest.rpartition(":")
        return UdpOutput(host or "127.0.0.1", int(port))
    if kind == "file":
        return FileOutput(rest)
    raise SystemExit("unknown --out %r (pty:/path | udp:host:port | file:/path)" % spec)


# ============================================================== shared telemetry state

class State:
    def __init__(self):
        self.t = {}  # field -> last update (monotonic)
        self.roll = self.pitch = self.yaw = 0.0  # rad
        self.rates = (0.0, 0.0, 0.0)
        self.alt_m = 0.0
        self.climb = 0.0
        self.ground_speed = 0.0
        self.lat = self.lon = 0
        self.voltage = 0.0
        self.current = -1
        self.rssi = 255
        self.channels = None
        self.armed = False
        self.custom_mode = 0
        self.mode_name = ""

    def set(self, field, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.t[field] = time.monotonic()

    def fresh(self, field, max_age=1.5):
        return time.monotonic() - self.t.get(field, -1e9) < max_age


class MavOut:
    SYSID, COMPID = 1, 1  # MAV_COMP_ID_AUTOPILOT1: QOpenHD treats this system as the FC

    def __init__(self, out):
        self.out = out
        self.seq = 0
        self.t0 = time.monotonic()

    def send(self, msgid, crc_extra, payload):
        self.out.write(v1_frame(msgid, crc_extra, payload, self.seq, self.SYSID, self.COMPID))
        self.seq = (self.seq + 1) & 0xFF

    def ms(self):
        return int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF

    def heartbeat(self, st):
        base_mode = 1 | (128 if st.armed else 0)  # CUSTOM_MODE_ENABLED | SAFETY_ARMED
        self.send(0, 50, struct.pack("<IBBBBB", st.custom_mode, 2, 0, base_mode, 4, 3))

    def encapsulated(self, seqnr, data):
        self.send(131, 223, struct.pack("<H", seqnr & 0xFFFF) + data.ljust(253, b"\0")[:253])

    def statustext(self, text, severity=6):
        self.send(253, 83, struct.pack("<B50s", severity, text.encode("ascii", "replace")[:50]))

    def attitude(self, st):
        self.send(30, 39, struct.pack("<Iffffff", self.ms(), st.roll, st.pitch, st.yaw, *st.rates))

    def position(self, st):
        hdg = int(math.degrees(st.yaw) * 100) % 36000
        self.send(33, 104, struct.pack("<IiiiihhhH", self.ms(), st.lat, st.lon, int(st.alt_m * 1000),
                                       int(st.alt_m * 1000), 0, 0, int(-st.climb * 100), hdg))

    def vfr_hud(self, st):
        thr = 0
        if st.channels:
            thr = int(max(0, min(100, (st.channels[2] - 1000) / 10)))
        self.send(74, 20, struct.pack("<ffffhH", 0.0, st.ground_speed, st.alt_m, st.climb,
                                      int(math.degrees(st.yaw)) % 360, thr))

    def sys_status(self, st, cells_hint):
        pct = -1
        if st.voltage > 1.0:
            cells = cells_hint or max(1, math.ceil(st.voltage / 4.35))
            pct = int(max(0, min(100, (st.voltage / cells - 3.3) / 0.9 * 100)))
        self.send(1, 124, struct.pack("<IIIHHhHHHHHHb", 0, 0, 0, 0, int(st.voltage * 1000) & 0xFFFF,
                                      st.current, 0, 0, 0, 0, 0, 0, pct))

    def rc(self, st):
        ch = list(st.channels[:18]) + [65535] * (18 - len(st.channels[:18]))
        self.send(65, 118, struct.pack("<I18HBB", self.ms(), *ch, len(st.channels), st.rssi))


# ============================================================== sources

class UartSource:
    """Auto-detects MAVLink / MSP on the UART and feeds State (or passes MAVLink through)."""

    DETECT_WINDOW_S = 4.0

    def __init__(self, path, baud, args, log):
        self.path, self.args, self.log = path, args, log
        self.port = None
        self.fixed_baud = baud
        self.baud_idx = 0
        self.kind = None  # None | "mavlink" | "msp"
        self.detect_buf = bytearray()
        self.window_start = 0.0
        self.last_valid = 0.0
        self.msp = MspParser()
        self.osd = OsdDecoder(args.osd_ignore)
        self.forwarder = None if args.no_osd_forward else OsdForwarder()
        self.msp_answers = 0
        self.msp_probes = 0
        self.poll_i = 0
        self.next_poll = 0.0
        self.last_snapshot = None
        self.disarm_seen_at = 0.0
        self.no_disarm_since = None
        self.osd_armed = False

    def open(self):
        if self.port or not self.path or self.path == "none":
            return
        if not os.path.exists(self.path) and sys.platform.startswith("linux"):
            return
        baud = self.fixed_baud or BAUD_CANDIDATES[self.baud_idx]
        try:
            self.port = open_serial(self.path, baud)
        except OSError as e:
            self.log("UART %s: %s" % (self.path, e))
            return
        self.window_start = time.monotonic()
        self.log("UART %s open @ %d" % (self.path, baud))

    def next_baud(self):
        if self.fixed_baud:
            return
        self.baud_idx = (self.baud_idx + 1) % len(BAUD_CANDIDATES)
        self.port.set_baud(BAUD_CANDIDATES[self.baud_idx])
        self.window_start = time.monotonic()
        self.detect_buf.clear()

    def poll(self, st, mav, now):
        """Read UART; returns raw bytes to forward to OpenHD when in MAVLink passthrough."""
        if not self.port:
            return b""
        try:
            data = self.port.read()
        except OSError as e:
            self.log("UART error: %s" % e)
            self.port.close()
            self.port = None
            self.kind = None
            return b""

        if self.kind == "mavlink":
            if data:
                self.last_valid = now
            elif now - self.last_valid > 10:
                self.log("UART: MAVLink stream lost, re-detecting")
                self.kind = None
            return data

        if self.kind is None:
            self.detect_buf += data
            if find_mavlink_heartbeat(self.detect_buf):
                self.kind = "mavlink"
                self.last_valid = now
                self.log("UART: standard MAVLink FC detected @ %d -> transparent passthrough" % self.port.baud)
                out = bytes(self.detect_buf)
                self.detect_buf.clear()
                return out
            if b"$M>" in self.detect_buf:
                self.kind = "msp"
                self.last_valid = now
                self.log("UART: MSP FC detected @ %d -> OSD decoding + MSP polling" % self.port.baud)
                data = bytes(self.detect_buf)
                self.detect_buf.clear()
            else:
                if len(self.detect_buf) > 8192:
                    del self.detect_buf[:4096]
                if now - self.window_start > self.DETECT_WINDOW_S:
                    # nothing recognised: probe for MSP once, then try the next baud rate
                    self.next_baud()
                elif now - self.window_start > self.DETECT_WINDOW_S / 2 and now >= self.next_poll:
                    self.port.write(msp_request(1))  # MSP_API_VERSION (read-only)
                    self.next_poll = now + 1.0
                return b""

        # ---- MSP mode
        if data:
            for cmd, p in self.msp.feed(data):
                self.last_valid = now
                if cmd == MSP_DISPLAYPORT:
                    snap = self.osd.handle(p, now)
                    if snap is not None:
                        if self.forwarder:
                            self.forwarder.on_frame(snap["cells"], mav, now)
                        self.on_osd(snap, st, mav, now)
                else:
                    self.on_msp(cmd, p, st)
        if now - self.last_valid > 10:
            self.log("UART: MSP stream lost, re-detecting")
            self.kind = None
            self.window_start = now
            return b""
        if now >= self.next_poll and not self.args.no_msp_poll:
            # read-only queries; fast if the FC answers, otherwise only an occasional probe
            if self.msp_answers > 0:
                self.port.write(msp_request(MSP_POLL[self.poll_i % len(MSP_POLL)]))
                self.poll_i += 1
                self.next_poll = now + 0.02
            elif self.msp_probes < 3:
                # an FC that never answers (BETAFPV Pavo) gets no further queries after 3 probes
                self.port.write(msp_request(MSP_ATTITUDE))
                self.msp_probes += 1
                self.next_poll = now + 5.0
                if self.msp_probes == 3:
                    self.log("UART: FC does not answer MSP queries -> OSD only, no more queries")
            else:
                self.next_poll = now + 3600
        return b""

    def on_msp(self, cmd, p, st):
        if self.msp_answers == 0:
            self.next_poll = 0.0  # FC answers MSP: switch to fast polling right away
            self.log("UART: FC answers MSP queries -> attitude/altitude/GPS available")
        self.msp_answers += 1
        if cmd == MSP_ATTITUDE and len(p) >= 6:
            r, pi, y = struct.unpack_from("<hhh", p)
            st.set("attitude", roll=math.radians(r / 10), pitch=math.radians(-pi / 10), yaw=math.radians(y))
        elif cmd == MSP_ALTITUDE and len(p) >= 6:
            alt_cm, vario = struct.unpack_from("<ih", p)
            st.set("altitude", alt_m=alt_cm / 100, climb=vario / 100)
        elif cmd == MSP_ANALOG and len(p) >= 7:
            vbat = p[0] / 10
            rssi = struct.unpack_from("<H", p, 3)[0]
            cur = struct.unpack_from("<h", p, 5)[0]
            if len(p) >= 9:
                vbat = struct.unpack_from("<H", p, 7)[0] / 100 or vbat
            st.set("voltage", voltage=vbat, current=cur)
            st.set("voltage_msp")
            st.set("rssi", rssi=min(254, rssi * 254 // 1023))
        elif cmd == MSP_RC and len(p) >= 8:
            n = len(p) // 2
            st.set("rc", channels=list(struct.unpack_from("<%dH" % n, p)))
        elif cmd == MSP_STATUS and len(p) >= 10:
            flags = struct.unpack_from("<I", p, 6)[0]
            st.set("armed_msp", armed=bool(flags & 1))  # box 0 = ARM in Betaflight
        elif cmd == MSP_RAW_GPS and len(p) >= 16:
            fix, sats, lat, lon, alt, speed = struct.unpack_from("<BBiihH", p)
            if fix:
                st.set("gps", lat=lat, lon=lon, ground_speed=speed / 100)

    def on_osd(self, snap, st, mav, now):
        prev = self.last_snapshot or {}
        # OSD text is the least precise voltage source: only use it when MSP/USB give none
        if snap["voltage"] is not None and not st.fresh("voltage_usb", 3) and not st.fresh("voltage_msp", 3):
            st.set("voltage", voltage=snap["voltage"])
        if snap["menu"] is not None:
            # FC menu open (only possible while disarmed). QOpenHD's message HUD shows 5 lines that
            # fade after 5 s, so send just two: the menu title and the line under the cursor.
            lines = snap["menu"]
            title = lines[0].strip(" -") if lines else ""
            cursor = next((l for l in lines if l.lstrip().startswith(">")), "")
            key = (title, cursor)
            if self.args.no_menu_text:
                snap["menu_sent"] = now
            elif key != prev.get("menu_key") or now - prev.get("menu_sent", 0) > 4:
                mav.statustext("MENU " + title, 5)
                if cursor:
                    mav.statustext(re.sub(r"\s{2,}", "  ", cursor.strip()), 5)
                snap["menu_sent"] = now
            else:
                snap["menu_sent"] = prev.get("menu_sent", now)
            snap["menu_key"] = key
            for k in ("mode", "speed", "warned"):  # remember pre-menu state, so closing it isn't a "change"
                snap[k] = prev.get(k)
            if not st.fresh("armed_msp", 3):
                st.armed = False
            self.last_snapshot = snap
            return
        if prev.get("menu") is not None and not self.args.no_menu_text:
            mav.statustext("MENU CLOSED", 6)
        if snap["mode"] and snap["mode"] != prev.get("mode"):
            name, custom = snap["mode"]
            st.mode_name, st.custom_mode = name, custom
            mav.statustext("MODE " + name, 6)
        if snap["speed"] and snap["speed"] != prev.get("speed"):
            mav.statustext("SPEED " + snap["speed"], 6)
        # Arming guessed from the OSD "DISARM" text, with hysteresis against blinking text.
        # The hysteresis matters: OpenHD reacts to every arm change (TX power, hotspot, and with air
        # recording "auto" a camera pipeline restart). --no-osd-arming keeps the guess out of OpenHD.
        if not st.fresh("armed_msp", 3):
            if snap["disarm"]:
                self.no_disarm_since = None
                self.disarm_seen_at = self.disarm_seen_at or now
                if self.osd_armed and now - self.disarm_seen_at > 1.0:
                    self.osd_armed = False
                    self.log("OSD: DISARMED")
                    mav.statustext("DISARMED", 5)
            else:
                self.disarm_seen_at = 0.0
                self.no_disarm_since = self.no_disarm_since or now
                if not self.osd_armed and self.osd.frames > 10 and now - self.no_disarm_since > 2.0:
                    self.osd_armed = True
                    self.log("OSD: ARMED")
                    mav.statustext("ARMED", 5)
            st.armed = False if self.args.no_osd_arming else self.osd_armed
        warned = prev.get("warned") or {}
        snap["warned"] = {}
        for w in snap["warnings"]:
            last = warned.get(w)
            if last is None or now - last > 30:  # on first appearance, then a reminder every 30 s
                mav.statustext(w, 4)
                last = now
            snap["warned"][w] = last
        self.last_snapshot = snap

    def write(self, data):
        if self.port and self.kind == "mavlink":
            self.port.write(data)

    def close(self):
        if self.port:
            self.port.close()
            self.port = None


class UsbPavoSource:
    def __init__(self, args, log):
        self.args, self.log = args, log
        self.port = None
        self.parser = None
        self.next_try = 0.0
        self.next_hb = 0.0
        self.last_rx = 0.0
        self.warned = False

    def open(self, now):
        if self.port or self.args.usb == "none" or now < self.next_try:
            return
        self.next_try = now + 5
        path = self.args.usb if self.args.usb != "auto" else find_pavo_usb()
        if not path:
            return
        try:
            self.port = open_serial(path, 115200)
        except OSError as e:
            self.log("USB %s: %s" % (path, e))
            return
        self.parser = PavoParser()
        self.last_rx = now
        self.log("USB: BETAFPV FC on %s" % path)

    def poll(self, st, now):
        if not self.port:
            return
        try:
            if now >= self.next_hb:
                self.port.write(PAVO_HEARTBEAT)
                self.next_hb = now + 1.0
            data = self.port.read()
        except OSError as e:
            self.log("USB error: %s" % e)
            self.port.close()
            self.port = None
            return
        if data:
            self.last_rx = now
            self.warned = False
        elif now - self.last_rx > 3 and not self.warned:
            self.log("USB: no data for 3 s (replug the FC USB if this persists)")
            self.warned = True
        a = self.args
        for msgid, m in self.parser.feed(data):
            if msgid == 5:
                st.set("attitude", roll=math.radians(m["roll"] * a.roll_sign),
                       pitch=math.radians(m["pitch"] * a.pitch_sign), yaw=math.radians(m["yaw"]))
            elif msgid == 4:
                st.rates = tuple(math.radians(v) for v in (m["gx"], m["gy"], m["gz"]))
            elif msgid == 3 and m["voltage"] > 1.0:
                st.set("voltage_usb", voltage=m["voltage"])
                st.set("voltage")
            elif msgid == 6:
                vx, vy, vz = (m[k] * a.speed_scale for k in ("vx", "vy", "vz"))
                st.set("speed", ground_speed=math.hypot(vx, vy), climb=vz)
            elif msgid == 12:
                tof_m, baro_m = m["tof"] * a.tof_scale, m["baro"] * a.baro_scale
                use_tof = a.alt_source == "tof" or (a.alt_source == "auto" and 0 < tof_m < a.tof_max)
                st.set("altitude", alt_m=tof_m if use_tof else baro_m)
            elif msgid == 7:
                st.set("rc", channels=[max(0, m["ch%d" % i]) for i in range(1, 13)])
                st.set("rssi", rssi=m["rssi"])

    def close(self):
        """Stop the heartbeat and let the FC finish streaming before closing (else its USB stalls)."""
        if not self.port:
            return
        t0 = quiet = time.monotonic()
        while time.monotonic() - t0 < 4 and time.monotonic() - quiet < 0.5:
            try:
                if self.port.read():
                    quiet = time.monotonic()
            except OSError:
                break
            time.sleep(0.02)
        self.port.close()
        self.port = None


# ============================================================== main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uart", default="/dev/serial0", help="FC UART (former DJI/VTX wire); 'none' to disable")
    ap.add_argument("--uart-baud", type=int, default=0, help="fixed baud; 0 = auto-search")
    ap.add_argument("--usb", default="auto", help="Pavo USB device, 'auto' or 'none'")
    ap.add_argument("--out", default="pty:/run/fc_osd_bridge", help="pty:/path | udp:host:port | file:/path")
    ap.add_argument("--cells", type=int, default=0, help="battery cells (0 = guess from voltage)")
    ap.add_argument("--roll-sign", type=int, default=1, choices=(1, -1))
    ap.add_argument("--pitch-sign", type=int, default=1, choices=(1, -1))
    ap.add_argument("--speed-scale", type=float, default=0.01, help="Pavo USB speed unit -> m/s")
    ap.add_argument("--tof-scale", type=float, default=0.01, help="Pavo ToF unit -> m")
    ap.add_argument("--baro-scale", type=float, default=0.01, help="Pavo baro unit -> m")
    ap.add_argument("--tof-max", type=float, default=3.0)
    ap.add_argument("--alt-source", choices=("auto", "tof", "baro"), default="auto")
    ap.add_argument("--osd-ignore", nargs="*", default=[], help="OSD words never reported as warnings (e.g. craft name)")
    ap.add_argument("--no-msp-poll", action="store_true", help="never send MSP queries on the UART")
    ap.add_argument("--no-osd-forward", action="store_true", help="do not send the OSD screen to the ground overlay")
    ap.add_argument("--no-menu-text", action="store_true", help="do not mirror the FC menu as STATUSTEXT")
    ap.add_argument("--no-osd-arming", action="store_true",
                    help="do not report the arm state read from the OSD 'DISARM' text to OpenHD")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (testing)")
    args = ap.parse_args()

    def log(msg):
        print(time.strftime("%H:%M:%S ") + msg, flush=True)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    out = open_output(args.out)
    if isinstance(out, PtyOutput):
        log("virtual FC port %s -> %s (OpenHD FC_UART_CONN = %s)" % (out.link, out.slave_name, out.link))
    st = State()
    mav = MavOut(out)
    uart = UartSource(args.uart, args.uart_baud, args, log)
    usb = UsbPavoSource(args, log)
    t_start = time.monotonic()
    next_hb = next_fast = next_slow = 0.0

    while not stop["flag"]:
        now = time.monotonic()
        if args.duration and now - t_start > args.duration:
            break
        uart.open()
        usb.open(now)

        fds = [f for f in (getattr(uart.port, "fd", None), getattr(usb.port, "fd", None), out.fd) if f is not None]
        if fds:
            select.select(fds, [], [], 0.01)
        else:
            time.sleep(0.01)

        raw = uart.poll(st, mav, now)
        to_fc = out.read()
        if uart.kind == "mavlink":
            # another aircraft with a real MAVLink FC: be a transparent wire, generate nothing
            if raw:
                out.write(raw)
            if to_fc:
                uart.write(to_fc)
            out.flush()
            continue
        usb.poll(st, now)

        if now >= next_hb:
            mav.heartbeat(st)
            next_hb = now + 1.0
        if now >= next_fast:
            next_fast = now + 1 / 15
            if st.fresh("attitude"):
                mav.attitude(st)
            if st.fresh("altitude") or st.fresh("gps"):
                mav.position(st)
            if st.fresh("speed") or st.fresh("altitude") or st.fresh("gps"):
                mav.vfr_hud(st)
        if now >= next_slow:
            next_slow = now + 0.5
            if st.fresh("voltage", 3):
                mav.sys_status(st, args.cells)
            if st.fresh("rc") and st.channels:
                mav.rc(st)
        out.flush()

    usb.close()
    uart.close()
    out.close()
    log("bridge stopped")


if __name__ == "__main__":
    main()
