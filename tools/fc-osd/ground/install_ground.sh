#!/bin/bash
# Run on the OpenHD GROUND (x86 desktop image), in a terminal, as the normal user (no sudo):
#     bash /boot/openhd/fc-osd/install_ground.sh                  (normal)
#     bash /boot/openhd/fc-osd/install_ground.sh --hide-throttle  (also hide QOpenHD's THROTTLE widget, for FCs
#                                                                  that never report throttle, e.g. BETAFPV Pavo)
# Installs for the current user only: ~/.local/bin/fc_osd_overlay.py + fc_osd_viewer.py, an autostart entry
# and desktop shortcuts. The QOpenHD desktop icon is switched to "Qt debug output off" (backup kept).
# Log: /boot/openhd/fc-osd/install_ground_log.txt (or ~/install_ground_log.txt if that is read-only)
set -u
HIDE_THROTTLE=0
[ "${1:-}" = "--hide-throttle" ] && HIDE_THROTTLE=1
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR"
[ -w "$OUT" ] || OUT="$HOME"   # /boot/openhd (FAT) may be root-only
LOG="$OUT/install_ground_log.txt"
exec > >(tee "$LOG") 2>&1
echo "=== install_ground $(date) user=$(id -un) DISPLAY=${DISPLAY:-none}"

mkdir -p "$HOME/.local/bin" "$HOME/.config/autostart" "$HOME/Desktop"
install -m 755 "$DIR/fc_osd_overlay.py" "$HOME/.local/bin/fc_osd_overlay.py"
install -m 755 "$DIR/fc_osd_viewer.py" "$HOME/.local/bin/fc_osd_viewer.py"

# video viewer with the OSD burned into the picture (F fullscreen, R record, O OSD, Q quit)
cat > "$HOME/Desktop/FC-OSD-Viewer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FC OSD Viewer
Comment=OpenHD video with the drone OSD inside the picture (F: fullscreen, R: record, O: OSD, Q: quit)
Exec=bash -c 'python3 $HOME/.local/bin/fc_osd_viewer.py > $HOME/fc_osd_viewer.log 2>&1'
Terminal=false
EOF
chmod +x "$HOME/Desktop/FC-OSD-Viewer.desktop"

cat > "$HOME/.config/autostart/fc-osd-overlay.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FC OSD Overlay
Comment=Draws the drone OSD (from fc_osd_bridge) on top of QOpenHD
Exec=python3 $HOME/.local/bin/fc_osd_overlay.py
X-GNOME-Autostart-enabled=true
EOF

cat > "$HOME/Desktop/FC-OSD-Demo.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FC OSD Demo (10 s)
Comment=Shows a sample OSD for 10 seconds to check size and position
Exec=bash -c 'timeout 10 python3 $HOME/.local/bin/fc_osd_overlay.py --demo'
Terminal=false
EOF
chmod +x "$HOME/Desktop/FC-OSD-Demo.desktop"

# --hide-throttle: hide QOpenHD's THROTTLE widget (Settings > Widgets > Show Throttle) for FCs without throttle data.
# The desktop icon starts QOpenHD with sudo, so its settings live in /root/.config (edit both, with backups).
CONF_PY="$(mktemp)"
cat > "$CONF_PY" <<'PYEOF'
import os, shutil, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
if not os.path.exists(path + ".fc_osd_backup"):  # also marks the file for uninstall_ground.sh
    if os.path.exists(path):
        shutil.copy2(path, path + ".fc_osd_backup")
    else:
        open(path + ".fc_osd_backup", "w").close()
try:
    lines = open(path).read().splitlines()
except FileNotFoundError:
    lines = []
sec, done, out = None, False, []
for ln in lines:
    s = ln.strip()
    if s.startswith("[") and s.endswith("]"):
        if sec == "[General]" and not done:
            out.append("show_throttle=false"); done = True
        sec = s
    elif sec == "[General]" and s.split("=", 1)[0].strip() == "show_throttle":
        ln, done = "show_throttle=false", True
    out.append(ln)
if not done:
    if sec != "[General]":
        out = ["[General]", "show_throttle=false", ""] + out if "[General]" not in lines else out
    if "show_throttle=false" not in out:
        out.append("show_throttle=false")
open(path, "w").write("\n".join(out) + "\n")
print("QOpenHD throttle widget hidden (%s)" % path)
PYEOF
if [ "$HIDE_THROTTLE" = 0 ]; then
  :
elif pgrep -x QOpenHD >/dev/null; then
  echo "QOpenHD is running -> throttle widget NOT changed. Close QOpenHD and run this script again."
else
  python3 "$CONF_PY" "$HOME/.config/OpenHD/QOpenHD.conf"
  echo "(sudo may ask for the password of user openhd, normally: openhd)"
  sudo python3 "$CONF_PY" /root/.config/OpenHD/QOpenHD.conf || echo "could not change /root/.config/OpenHD/QOpenHD.conf"
fi
rm -f "$CONF_PY"

# The QOpenHD desktop icon runs it in a terminal that fills up with Qt debug lines such as
# "FCMavlinkSystem received unmatched message with ID 131" (our OSD data, which QOpenHD does not use).
# Start it with Qt debug output off; warnings and errors still show.
QDESK="$HOME/Desktop/QOpenHD.desktop"
if [ -f "$QDESK" ] && grep -q "sudo QOpenHD" "$QDESK"; then
  mkdir -p "$HOME/.local/share"
  [ -f "$HOME/.local/share/QOpenHD.desktop.fc_osd_backup" ] || cp "$QDESK" "$HOME/.local/share/QOpenHD.desktop.fc_osd_backup"
  sed -i 's/sudo QOpenHD/sudo env QT_LOGGING_RULES=default.debug=false QOpenHD/' "$QDESK"
  echo "QOpenHD desktop icon: debug output off"
fi

echo "--- checks"
python3 -c "import PyQt5.QtWidgets as w; print('PyQt5 OK')" || python3 -c "import PyQt6.QtWidgets; print('PyQt6 OK')"
python3 -m py_compile "$HOME/.local/bin/fc_osd_overlay.py" && echo "overlay compiles OK"
python3 -m py_compile "$HOME/.local/bin/fc_osd_viewer.py" && echo "viewer compiles OK"
python3 - <<'PYEOF'
import gi
gi.require_version("Gst", "1.0"); gi.require_version("GstVideo", "1.0")
from gi.repository import Gst
Gst.init(None)
for n in ("udpsrc", "rtph264depay", "h264parse", "vah264dec", "openh264dec", "avdec_h264", "overlaycomposition",
          "xvimagesink", "appsink", "x264enc", "matroskamux"):
    print("gst %-18s %s" % (n, "yes" if Gst.ElementFactory.find(n) else "NO"))
PYEOF
for port in 5760 1445; do
  if timeout 1 bash -c "echo > /dev/tcp/127.0.0.1/$port" 2>/dev/null; then echo "OpenHD TCP $port: open"; else echo "OpenHD TCP $port: closed (normal if OpenHD ground is not running)"; fi
done

# (re)start it now in this desktop session
pkill -f "python[0-9.]* .*fc_osd_overlay\.py" 2>/dev/null
if [ -n "${DISPLAY:-}" ]; then
  nohup python3 "$HOME/.local/bin/fc_osd_overlay.py" > "$OUT/overlay_log.txt" 2>&1 &
  echo "overlay started (pid $!), log: $OUT/overlay_log.txt"
fi
echo "=== done"
