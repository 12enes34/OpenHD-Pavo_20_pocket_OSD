# openhd-fc-osd (Türkçe rehber)

**Uçuş kontrolcüsünün (FC) OSD'sini, menülerini ve telemetrisini, DJI gözlükteki gibi OpenHD'de gösterir.**

[English README](README.md)

Bu klasör (`fc-osd-bridge` dalında `tools/fc-osd/`) bağımsızdır: OpenHD'nin kendisini değiştirmez veya
yeniden derlemez, sadece hava ve yer SD kartlarına / USB'lerine kopyalanan dosyaları ekler.

Birçok küçük FPV drone, VTX kablosundan yalnızca **MSP DisplayPort** ("DJI / HD OSD") gönderir ya da
kendine özel bir protokol kullanır. OpenHD ise FC portunda sadece standart MAVLink anlar. Bu yüzden
böyle bir FC ile QOpenHD boş kalır: pil, uçuş modu, ARM durumu ve FC menüsü görünmez.

Bu proje iki parça ekler:
* **Hava biriminde** küçük bir çevirici (`fc_osd_bridge.py`): FC'nin OSD'sini çözer, standart
  MAVLink'e çevirir ve OSD ekranını olduğu gibi yere yollar.
* **Yer istasyonunda** bir OSD katmanı (`fc_osd_overlay.py`): FC menüsünü, uyarıları, voltajı ve
  modu QOpenHD penceresinin ortasına çizer. İsteğe bağlı olarak `fc_osd_viewer.py`, OSD'yi
  görüntünün içine işler ve kayıt yapar.

* FC yazılımına **dokunulmaz**, FC'ye ayar değiştiren hiçbir komut gönderilmez.
* Her kurulumun bir kaldırma betiği vardır ve eski ayarlar geri yüklenir.

## Neler görünür

| UART'taki FC | QOpenHD'de | Yerde FC OSD ekranı/menüsü |
|---|---|---|
| Sadece MSP OSD veren FC (ör. **BETAFPV Pavo20 Pocket** orijinal yazılım) | voltaj, uçuş modu, ARMED/DISARMED (OSD'den okunur), uyarılar | var |
| **Betaflight** (DJI/HD portu) | yukarıdakiler + yapay ufuk, irtifa, GPS, RC, RSSI, gerçek ARM durumu | var |
| **ArduPilot / INAV / PX4** (MAVLink) | OpenHD'nin normal davranışı (aynen aktarılır) | – |
| Ek: Pavo'nun USB'si Pi'ye bağlı | ufuk, irtifa (ToF/baro), hız | – |

FC tipi ve baud hızı otomatik bulunur. Aynı SD kart başka bir drone'a takıldığında da çalışır.

**Test edildi:** OpenHD 2.6.4. Hava birimi Pi Zero 2W + OV5647 + BL-M8812EU2, yer istasyonu x86 PC.
Gerçek donanımda BETAFPV Pavo20 Pocket ile denendi. Betaflight ve ArduPilot şimdilik sadece
benzetilmiş (simüle) FC ile test edildi.

## Bağlantı (hava birimi)

DJI / HD VTX'e giden FC UART'ını kullanın:

| FC | Raspberry Pi |
|---|---|
| TX | pin 10 (RXD) |
| RX | pin 8 (TXD) |
| GND | GND (ör. pin 6) |

Bu hat yalnızca 3.3 V seviyesindedir. VTX'in 5V/9V/12V besleme kablosunu Pi'ye bağlamayın.

## Kurulum

### 1. Hava birimi (OpenHD air imajlı Raspberry Pi)
1. Hava SD kartını bilgisayara takın. **`air`** klasörünü SD kartın boot bölümüne (Windows'un
   açabildiği bölüm) kopyalayın ve adını **`fc-osd`** yapın. Pi'de yolu `/boot/fc-osd/` olur.
2. Kartı geri takın ve drone'a pil takın (**pervaneler sökülü!**). Ardından OpenHD hava hotspot'una
   bağlanın (varsayılan ad `openhd_air…`, şifre `openhdopenhd`).
3. `ssh openhd@192.168.3.1` ile bağlanın (varsayılan şifre `openhd`) ve şunu çalıştırın:
   ```
   sudo bash /boot/fc-osd/install.sh
   ```
   Kurulum arka planda sürer. OpenHD yeniden başlarken hotspot ve SSH bir an kopar; bu normal.
   Yaklaşık 1 dakika sonra sonuç `/boot/fc-osd/install_log.txt` dosyasında olur. Bu dosya SD kart
   bilgisayara takılınca da okunabilir.

### 2. Yer istasyonu (OpenHD x86 imajı)
1. **`ground`** klasörünü `/boot/openhd/fc-osd/` yoluna kopyalayın. Bu, yer USB'sinin FAT
   bölümüdür; Windows'tan da görünür.
2. Yer masaüstünde bir terminal açın ve normal kullanıcıyla (**sudo olmadan**) şunu çalıştırın:
   ```
   bash /boot/openhd/fc-osd/install_ground.sh
   ```
   Gaz bilgisi göndermeyen FC'lerde (ör. Pavo) QOpenHD'nin THROTTLE göstergesini gizlemek için
   sonuna `--hide-throttle` ekleyin. Bunu yaparken QOpenHD kapalı olmalı.

OSD katmanı her oturum açılışında kendiliğinden başlar.

## Kullanım
* **Katman:** QOpenHD'yi her zamanki gibi açın. FC'nin OSD'si QOpenHD penceresinin ortasında
  görünür. Pencere taşınınca, boyutu değişince veya tam ekran olunca onu takip eder. QOpenHD
  küçültülünce, önüne başka bir pencere gelince veya kapanınca gizlenir.
* **FC OSD Viewer** (masaüstü simgesi): OSD görüntünün içine işlenmiş olarak oynatılır.
  * `F` tam ekran
  * `R` kayıt (`~/Videos`, OSD dahil)
  * `O` OSD aç/kapa
  * `Q` çıkış
* **FC menüsü:** FC kılavuzundaki çubuk hareketiyle açılır (Pavo: disarm iken gaz sol + pitch
  yukarı).

## Güncelleme / kaldırma
* **Hava:**
  * Güncelleme: yeni dosyaları `/boot/fc-osd/` klasörüne koyup `sudo bash /boot/fc-osd/update.sh`
    çalıştırın. Bu sırada SSH kopmaz.
  * Kaldırma: `sudo bash /boot/fc-osd/uninstall.sh`
* **Yer:**
  * Güncelleme: `install_ground.sh` betiğini yeniden çalıştırın.
  * Kaldırma: `bash /boot/openhd/fc-osd/uninstall_ground.sh`

## Sorun giderme
* **Kayıtlar (loglar):**
  * Hava: `journalctl -u fc-osd-bridge -n 50` veya `sudo bash /boot/fc-osd/diag.sh` (sadece okur).
  * Yer: `~/overlay_log.txt` dosyası.
* **Kayıtta sadece `UART … open @ …` satırları dönüyor:** TX/RX ters bağlanmış ya da GND ortak
  değil.
* **QOpenHD terminalinde `unmatched message with ID 131` yazıyor:** zararsızdır, OSD verisidir.
  Kurulum, QOpenHD simgesini bu satırları göstermeyecek şekilde ayarlar.
* **Ekranda iki OSD var:** eski bir kopya hâlâ çalışıyor. Yeni sürüm açılırken eski kopyaları
  kendisi kapatır; oturumu kapatıp açmak da düzeltir.
* **Kamera 10 saniyede bir yeniden başlıyor:** kamera görüntü vermiyordur. `diag.sh` çıktısında
  `ov5647 … error -5` görüyorsanız kamera kablosu (CSI) gevşek ya da hasarlıdır.
* **Ufuk ters dönüyor:** servis dosyasında komutun sonuna `--roll-sign -1` veya `--pitch-sign -1`
  ekleyin.

Lisans: GPL-3.0 (OpenHD reposunun geri kalanı gibi, ana dizindeki `LICENSE`).

Tüm ayarlar ve teknik ayrıntılar için [README.md](README.md) dosyasına bakın.
