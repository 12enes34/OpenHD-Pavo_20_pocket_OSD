#!/bin/bash
# Undo install.sh:   sudo bash /boot/fc-osd/uninstall.sh   (log: uninstall_log.txt next to this script)
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" != 0 ]; then
  echo "Please run with sudo:  sudo bash $0"
  exit 1
fi
if [ "${1:-}" != "--detached" ]; then
  setsid nohup bash "$0" --detached > /dev/null 2>&1 < /dev/null &
  echo "Uninstall started in the background (SSH may drop). Result: $DIR/uninstall_log.txt"
  exit 0
fi
exec > "$DIR/uninstall_log.txt" 2>&1
set -x
systemctl disable --now fc-osd-bridge
rm -f /etc/systemd/system/fc-osd-bridge.service /usr/local/bin/fc_osd_bridge.py
systemctl daemon-reload
systemctl stop openhd
SETTINGS=$(find /usr/local/share/openhd /config /boot/openhd -name air_settings.json 2>/dev/null | head -n 1)
if [ -n "$SETTINGS" ] && [ -f "$DIR/air_settings.backup.json" ]; then
  cp "$DIR/air_settings.backup.json" "$SETTINGS"
fi
if [ -n "$SETTINGS" ]; then
  # never leave OpenHD pointing at a bridge port that no longer exists
  python3 - "$SETTINGS" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
if str(d.get("fc_uart_connection_type", "")).startswith("/run/"):
    d["fc_uart_connection_type"] = "DEFAULT"
    json.dump(d, open(p, "w"), indent=2)
print("fc_uart_connection_type:", d.get("fc_uart_connection_type"))
EOF
fi
systemctl start openhd
sync
echo "=== done"
