#!/bin/bash
# Read-only diagnostics on the OpenHD air unit (Raspberry Pi) (OpenHD keeps running, SSH stays up):
#     sudo bash /boot/fc-osd/diag.sh
# Log: diag_log.txt next to this script
DIR="$(cd "$(dirname "$0")" && pwd)"
exec > >(tee "$DIR/diag_log.txt") 2>&1
echo "=== diag $(date)  uptime: $(uptime)"
cat /proc/device-tree/model; echo
echo "--- power / temperature (0x0 = OK; bit 0 = under-voltage now, bit 16 = under-voltage happened)"
vcgencmd get_throttled; vcgencmd measure_temp; vcgencmd measure_volts core
echo "--- camera detection"
vcgencmd get_camera 2>/dev/null
ls -l /dev/video* /dev/media* 2>&1 | head -n 20
dmesg | grep -i -E "ov5647|imx|unicam|csi|camera|i2c|under-voltage|voltage" | tail -n 40
for t in libcamera-hello rpicam-hello; do
  command -v $t > /dev/null && { echo "--- $t --list-cameras"; timeout 10 $t --list-cameras 2>&1 | tail -n 15; }
done
echo "--- OpenHD screen output (tty1, last lines)"
COLS=$(stty -F /dev/tty1 size 2>/dev/null | awk '{print $2}')
fold -w "${COLS:-80}" /dev/vcs1 | grep -v "^ *$" | tail -n 60
echo "--- processes (cpu)"
top -b -n 1 | head -n 20
echo "--- fc-osd-bridge"; systemctl is-active fc-osd-bridge openhd; ls -l /run/fc_osd_bridge 2>&1
journalctl -u fc-osd-bridge -n 30 --no-pager
echo "--- openhd video settings"
for f in /usr/local/share/openhd/video/*.json; do echo "## $f"; cat "$f"; echo; done 2>/dev/null
echo "--- recordings partition"
df -h | grep -i -E "record|mmcblk|Filesystem"
sync
echo "=== done"
