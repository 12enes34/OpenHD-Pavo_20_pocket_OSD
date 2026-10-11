#!/bin/bash
# Run on the OpenHD AIR unit over SSH:   sudo bash /boot/fc-osd/install.sh
# - installs fc_osd_bridge.py + fc-osd-bridge.service
# - points OpenHD's FC connection to the bridge's virtual port /run/fc_osd_bridge (old settings are backed up)
# The real work runs detached, so it finishes even when the SSH session drops
# (restarting OpenHD briefly turns its WiFi hotspot off). Log: install_log.txt next to this script.
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/install_log.txt"

if [ "$(id -u)" != 0 ]; then
  echo "Please run with sudo:  sudo bash $0"
  exit 1
fi
if [ "${1:-}" != "--detached" ]; then
  setsid nohup bash "$0" --detached > /dev/null 2>&1 < /dev/null &
  echo "Installation started in the background. The SSH connection may drop; that is normal."
  echo "Result in about 1 minute: $LOG"
  exit 0
fi

exec > "$LOG" 2>&1
echo "=== install $(date)"
set -x
# an earlier private version of this project used the name pavo-bridge; it must not run in parallel
if [ -f /etc/systemd/system/pavo-bridge.service ]; then
  systemctl disable --now pavo-bridge
  rm -f /etc/systemd/system/pavo-bridge.service /usr/local/bin/pavo_bridge.py
fi
install -m 755 "$DIR/fc_osd_bridge.py" /usr/local/bin/fc_osd_bridge.py
install -m 644 "$DIR/fc-osd-bridge.service" /etc/systemd/system/fc-osd-bridge.service
systemctl daemon-reload
systemctl stop openhd
sleep 2

SETTINGS=$(find /usr/local/share/openhd /config /boot/openhd -name air_settings.json 2>/dev/null | head -n 1)
echo "air_settings.json: ${SETTINGS:-NOT FOUND}"
if [ -n "$SETTINGS" ]; then
  [ -f "$DIR/air_settings.backup.json" ] || cp "$SETTINGS" "$DIR/air_settings.backup.json"
  python3 - "$SETTINGS" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
print("before:", {k: v for k, v in d.items() if "uart" in k.lower()})
d["fc_uart_connection_type"] = "/run/fc_osd_bridge"
d["fc_uart_baudrate"] = 115200
json.dump(d, open(p, "w"), indent=2)
print("after:", {k: v for k, v in d.items() if "uart" in k.lower()})
EOF
else
  echo "WARNING: OpenHD air_settings.json not found. Set the FC UART connection to /run/fc_osd_bridge in QOpenHD"
  echo "         (OpenHD settings > air > FC_UART_CONN) by hand."
fi

systemctl enable fc-osd-bridge
systemctl restart fc-osd-bridge
sleep 3
systemctl start openhd
set +x
sleep 25
echo "=== status"
systemctl is-active fc-osd-bridge openhd
ls -l /run/fc_osd_bridge
echo "=== fc-osd-bridge log"
journalctl -u fc-osd-bridge -n 30 --no-pager
sync
echo "=== done"
