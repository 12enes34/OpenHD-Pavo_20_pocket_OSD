#!/bin/bash
# Removes the FC OSD overlay from the ground user:   bash /boot/openhd/fc-osd/uninstall_ground.sh
pkill -f "python[0-9.]* .*fc_osd_overlay\.py" 2>/dev/null
pkill -f "python[0-9.]* .*fc_osd_viewer\.py" 2>/dev/null
rm -f "$HOME/.local/bin/fc_osd_viewer.py" "$HOME/Desktop/FC-OSD-Viewer.desktop" "$HOME"/.local/bin/__pycache__/fc_osd_viewer.*.pyc
rm -f "$HOME/.local/bin/fc_osd_overlay.py" "$HOME/.config/autostart/fc-osd-overlay.desktop" \
      "$HOME/Desktop/FC-OSD-Demo.desktop" "$HOME"/.local/bin/__pycache__/fc_osd_overlay.*.pyc
# QOpenHD throttle widget back on, only where --hide-throttle changed it (it left a .fc_osd_backup)
QCONF="$HOME/.config/OpenHD/QOpenHD.conf"
[ -f "$QCONF.fc_osd_backup" ] && sed -i 's/^show_throttle=false$/show_throttle=true/' "$QCONF"
sudo test -f /root/.config/OpenHD/QOpenHD.conf.fc_osd_backup &&   sudo sed -i 's/^show_throttle=false$/show_throttle=true/' /root/.config/OpenHD/QOpenHD.conf
# QOpenHD desktop icon back to the original (with debug output)
[ -f "$HOME/.local/share/QOpenHD.desktop.fc_osd_backup" ] && cp "$HOME/.local/share/QOpenHD.desktop.fc_osd_backup" "$HOME/Desktop/QOpenHD.desktop"
echo "FC OSD overlay removed."
