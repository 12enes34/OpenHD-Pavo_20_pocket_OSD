# openhd-fc-osd

**Show your flight controller's OSD, menus and telemetry in OpenHD, the way DJI goggles do.**

[Türkçe rehber → README_TR.md](README_TR.md)

This folder (`tools/fc-osd/` on the `fc-osd-bridge` branch) is self-contained: it doesn't change or
rebuild OpenHD itself, it only adds files you copy to the air and ground SD cards / USB sticks.

Many small FPV drones only speak **MSP DisplayPort** (the "DJI / HD OSD" protocol) on their VTX
UART, or use a proprietary protocol. OpenHD only understands standard MAVLink on its FC port, so with
these FCs QOpenHD stays empty: no battery, no flight mode, no arming state, and no FC menu.

This project adds a small bridge on the OpenHD **air unit** plus an OSD overlay on the **ground
station**:

```
 FC  ──UART (old DJI/HD VTX wire)──▶  fc_osd_bridge.py  ──/run/fc_osd_bridge (virtual FC port)──▶  OpenHD air
 (MSP DisplayPort OSD,                 decodes the OSD, converts to                                    │
  or MAVLink, or Pavo USB)             standard MAVLink, forwards the                                  │ wifibroadcast
                                       whole OSD screen                                                ▼
                                                                                                 OpenHD ground
                                                                          QOpenHD  ◀── standard MAVLink (HUD, ARMED, battery, mode)
                                                              fc_osd_overlay.py  ◀── OSD screen (FC menu, warnings) drawn over QOpenHD
                                                               fc_osd_viewer.py  ◀── or: OSD burned into the video (+ recording)
```

* **No firmware changes.** Nothing that changes FC settings is ever sent to the FC.
* **Python 3 standard library only** on the air unit. The ground side uses PyQt5 and GStreamer,
  which the OpenHD x86 image already has.
* **Fully reversible:** each install script has an uninstall script that restores the old settings.

## What you get

| FC on the UART | Detected as | Shown in QOpenHD | FC OSD screen / menu on the ground |
|---|---|---|---|
| FC that only outputs MSP DisplayPort OSD (e.g. **BETAFPV Pavo20 Pocket** stock firmware) | MSP OSD | battery voltage, flight mode, ARMED/DISARMED (read from the OSD), warnings | yes |
| **Betaflight** with a DJI/HD (MSP DisplayPort) port | MSP OSD + MSP queries | the above + artificial horizon, altitude, GPS, RC, RSSI, real arming state | yes |
| **ArduPilot / INAV / PX4** (MAVLink on that UART) | MAVLink | normal OpenHD behaviour (transparent two-way passthrough) | – |
| Extra: BETAFPV Pavo USB connected to the Pi | Pavo USB | attitude/horizon, altitude (ToF/baro), speed | – |

The FC type and baud rate are detected automatically, so the same SD card works in another drone.

### Tested
* OpenHD **2.6.4**: air = Raspberry Pi Zero 2W + OV5647 camera + BL-M8812EU2; ground = x86 PC (Ubuntu 24.04 based OpenHD image).
* Real hardware: **BETAFPV Pavo20 Pocket** (stock firmware): OSD, FC menu, arming, flight modes.
* Betaflight and ArduPilot were tested with simulated FCs (scripted MSP / MAVLink streams), not on a real
  aircraft yet. Reports are welcome.

## Wiring (air unit)

Use the FC UART that used to go to the DJI / HD VTX:

| FC | Raspberry Pi |
|---|---|
| TX | pin 10 (GPIO15, RXD) |
| RX | pin 8 (GPIO14, TXD) |
| GND | GND (e.g. pin 6) |

3.3 V logic only. Don't connect the VTX 5V/9V/12V power wire to the Pi. On the OpenHD Pi image, this
UART is `/dev/serial0`, which is enabled by default because OpenHD uses it for the FC itself.

## Installation

### 1. Air unit (Raspberry Pi with the OpenHD air image)
1. Put the air SD card in a PC. Copy the **`air`** folder to the SD card's boot partition (the one
   Windows can open) and rename it to **`fc-osd`**. On the Pi it is then `/boot/fc-osd/`.
2. Put the SD card back, power the drone (props off!), and connect to the OpenHD air hotspot
   (default SSID `openhd_air…`, password `openhdopenhd`).
3. `ssh openhd@192.168.3.1` (default password `openhd`), then run:
   ```
   sudo bash /boot/fc-osd/install.sh
   ```
   The installer runs in the background. OpenHD restarts, so the hotspot and the SSH session drop
   for a moment; that is normal. After about a minute the result is in
   `/boot/fc-osd/install_log.txt`, which you can also read with the SD card in a PC.

You can also do this with a keyboard and monitor on the Pi (Alt+F2 opens a login shell).

### 2. Ground station (OpenHD x86 image)
1. Copy the **`ground`** folder to `/boot/openhd/fc-osd/`. That is the FAT partition of the ground
   USB stick/SSD, which you can also reach from Windows.
2. In a terminal on the ground desktop, run as the normal user (**without sudo**):
   ```
   bash /boot/openhd/fc-osd/install_ground.sh
   ```
   Optional: `--hide-throttle` hides QOpenHD's THROTTLE widget. Use it for FCs that never report
   throttle, such as the Pavo. Close QOpenHD first, because QOpenHD rewrites its settings when it
   exits.

The overlay then starts automatically at every login.

## Usage

* **Overlay (default):** start QOpenHD as usual. The FC's OSD (menus, warnings, voltage, mode) is
  drawn centred over the QOpenHD window. It follows the window when it is moved, resized or made
  fullscreen, and hides when QOpenHD is minimized, covered by another active window, or closed.
* **FC OSD Viewer** (desktop icon): plays the OpenHD video in its own window with the OSD **burned
  into the picture**, like DJI goggles. Keys: `F` fullscreen, `R` start/stop recording
  (`~/Videos/*.mkv`, OSD included), `O` OSD on/off, `Q`/`Esc` quit. While it runs, the overlay hides
  itself.
* **FC OSD Demo** (desktop icon): shows a sample OSD for 10 s to check size and position.
* **FC menu:** open it with the radio sticks as the FC manual says (Pavo: disarmed, throttle
  left + pitch up). It appears on the ground like in DJI goggles.

## Options

Bridge (air): edit the `ExecStart=` line in `/etc/systemd/system/fc-osd-bridge.service`, then run
`sudo systemctl daemon-reload && sudo systemctl restart fc-osd-bridge`.

| Option | Meaning |
|---|---|
| `--uart /dev/ttyAMA0` | another FC UART (default `/dev/serial0`; `none` = off) |
| `--uart-baud 115200` | fixed baud instead of auto search |
| `--usb none` | don't look for a Pavo USB FC |
| `--no-msp-poll` | never send MSP queries on the UART |
| `--no-osd-arming` | don't derive ARMED/DISARMED from the OSD text |
| `--osd-ignore NAME` | OSD words that are not warnings (e.g. your craft name) |
| `--cells 2` | battery cell count (default: guessed from the voltage) |
| `--roll-sign -1` / `--pitch-sign -1` | flip the horizon if it moves the wrong way |
| `--no-osd-forward` | don't send the OSD screen to the ground |
| (remove) `--no-menu-text` | also send the FC menu as QOpenHD HUD messages |

Overlay (ground): edit `Exec=` in `~/.config/autostart/fc-osd-overlay.desktop`. Options are
`--scale 0.8` (OSD size), `--offset-x/--offset-y` (pixels), `--hd` (force the 53×20 HD grid),
`--fullscreen` (cover the whole screen instead of following QOpenHD), and `--always` (also show
when QOpenHD is not running).

## Updating / removing

| | Air (SSH) | Ground (terminal, no sudo) |
|---|---|---|
| update | copy the new files to `/boot/fc-osd/`, then `sudo bash /boot/fc-osd/update.sh` (SSH stays up) | `bash /boot/openhd/fc-osd/install_ground.sh` |
| remove | `sudo bash /boot/fc-osd/uninstall.sh` (restores OpenHD's FC setting) | `bash /boot/openhd/fc-osd/uninstall_ground.sh` |

## Troubleshooting

* **Logs:** on the air unit, run `journalctl -u fc-osd-bridge -n 50`, or run
  `sudo bash /boot/fc-osd/diag.sh`, which is read-only and writes `diag_log.txt` next to itself. On
  the ground, check `~/overlay_log.txt` and `~/fc_osd_viewer.log`.
* **The bridge log only repeats `UART /dev/serial0 open @ …` with changing baud rates** (no "FC
  detected" line): TX/RX are swapped or there is no common GND. Also check that the FC port is
  configured for the DJI/HD OSD (MSP DisplayPort).
* **QOpenHD terminal prints `FCMavlinkSystem received unmatched message with ID 131`:** this is
  harmless. It is the OSD data, which QOpenHD itself doesn't use. `install_ground.sh` switches the
  QOpenHD desktop icon to "Qt debug output off" to stop it.
* **Two OSDs on the screen:** an old overlay copy is still running. The current overlay stops older
  copies when it starts; logging out and back in also clears it.
* **Camera restarts every ~10 s:** OpenHD restarts the camera when it gets no frames. `diag.sh`
  shows the cause. For example, `ov5647 … probe failed with error -5` means the CSI ribbon cable is
  loose or damaged, not this project.
* **Horizon moves the wrong way:** use `--roll-sign -1` / `--pitch-sign -1`.

## How it works (details)

* The bridge reads MSP DisplayPort (MSP 182) screen updates, rebuilds the FC's character screen, and
  turns it into standard MAVLink: `HEARTBEAT` (mode + armed), `SYS_STATUS` (battery) and
  `STATUSTEXT` (warnings). It sends these from system 1 / component 1, which is how QOpenHD detects
  an FC.
* The whole OSD screen is also sent to the ground inside standard `ENCAPSULATED_DATA` messages
  (frames tagged `OSD1`, max 8 Hz, plus a 1 s keepalive). OpenHD forwards all FC MAVLink, so no
  OpenHD change is needed. The ground overlay reads them from OpenHD's TCP MAVLink server
  (`127.0.0.1:5760`).
* Betaflight FCs answer read-only MSP queries (`MSP_ATTITUDE`, `MSP_ALTITUDE`, `MSP_RAW_GPS`,
  `MSP_RC`, `MSP_STATUS`…). FCs that don't answer 3 probes are never queried again.
* For a MAVLink FC, the bridge is a transparent pipe in both directions.
* The viewer takes the RTP/H.264 stream that OpenHD ground always forwards to `127.0.0.1:5800`. It
  decodes it with VA-API (or openh264), adds the OSD with GStreamer `overlaycomposition`, and shows
  it with `xvimagesink`.

## License

GPL-3.0, like the rest of the OpenHD repository (see `LICENSE` in the repository root).
