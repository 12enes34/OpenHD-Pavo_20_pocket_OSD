#!/bin/bash
# Update an existing installation:   sudo bash /boot/fc-osd/update.sh
# Installs the new fc_osd_bridge.py / service and restarts only the bridge
# (OpenHD keeps running, so the SSH session stays up). Log: update_log.txt next to this script.
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" != 0 ]; then
  echo "Please run with sudo:  sudo bash $0"
  exit 1
fi
exec > >(tee "$DIR/update_log.txt") 2>&1
echo "=== update $(date)"
if [ ! -f /etc/systemd/system/fc-osd-bridge.service ]; then
  echo "fc-osd-bridge is not installed yet -> run install.sh instead."
  exit 1
fi
install -m 755 "$DIR/fc_osd_bridge.py" /usr/local/bin/fc_osd_bridge.py
install -m 644 "$DIR/fc-osd-bridge.service" /etc/systemd/system/fc-osd-bridge.service
systemctl daemon-reload
systemctl restart fc-osd-bridge
sleep 4
systemctl is-active fc-osd-bridge openhd
journalctl -u fc-osd-bridge -n 15 --no-pager
sync
echo "=== done"
