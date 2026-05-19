# Installation Guide

End-to-end setup for a GPS-disciplined Stratum 1 NTP server on a Raspberry Pi 4. Tested on Raspberry Pi OS Bookworm.

## Prerequisites

- Raspberry Pi 4 (any RAM variant)
- Fresh install of Raspberry Pi OS (64-bit recommended)
- Static IP or DHCP reservation for the Pi
- SSH access
- GT-U7 or similar GPS module wired per [WIRING.md](WIRING.md)

## 1. System preparation

Update and install required packages:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y chrony gpsd gpsd-clients pps-tools linuxptp python3
```

## 2. Free up the UART

The Pi 4 connects its internal Bluetooth chip to the primary (high-quality) UART by default. We need that UART for the GPS module.

Edit `/boot/firmware/config.txt`:

```bash
sudo nano /boot/firmware/config.txt
```

Under `[all]` add:

```
dtoverlay=disable-bt
dtoverlay=pps-gpio,gpiopin=18
```

The first line disables the onboard Bluetooth so the UART is freed. The second registers GPIO 18 as the PPS input pin.

Edit `/boot/firmware/cmdline.txt` (all on one line, do not add line breaks):

Find any `console=serial0,115200` and remove it. Append CPU isolation parameters:

```
isolcpus=3 nohz_full=3 rcu_nocbs=3
```

This dedicates CPU core 3 to time-critical work and reduces scheduler jitter.

Disable conflicting services:

```bash
sudo systemctl disable --now hciuart
sudo systemctl disable --now serial-getty@ttyAMA0
sudo systemctl disable --now bluetooth
```

Reboot:

```bash
sudo reboot
```

After reboot, verify:

```bash
ls -la /dev/serial0      # Should symlink to ttyAMA0
ls -la /dev/pps0          # Should exist
cat /proc/interrupts | grep pps   # Should show pps@12.-1 with rising counter
```

## 3. Configure gpsd

Edit `/etc/default/gpsd`:

```
START_DAEMON="true"
USBAUTO="false"
DEVICES="/dev/serial0 /dev/pps0"
GPSD_OPTIONS="-n -G"
```

The `-n` flag makes gpsd read continuously without waiting for a client. The `-G` flag makes shared memory segments world-readable, which is required for chrony to access them.

Add a udev rule so PPS devices stay world-readable across reboots:

```bash
sudo tee /etc/udev/rules.d/99-pps.rules <<'EOF'
KERNEL=="pps[0-9]*", MODE="0644"
EOF

sudo udevadm control --reload-rules
```

Restart gpsd:

```bash
sudo systemctl restart gpsd
```

Verify GPS data:

```bash
gpspipe -w -n 10
```

You should see JSON with TPV (position) and SKY (satellites) data. If no TPV appears with `mode: 3`, your antenna needs better positioning. South-facing window with clear sky view works best.

## 4. Configure chrony

Replace `/etc/chrony/chrony.conf` with the contents of `config/chrony.conf.server.example`. Key sections:

```
# GPS time via gpsd SHM (NMEA, low precision)
refclock SHM 2 refid NMEA precision 1e-3 offset 0.0 delay 0.2 noselect

# PPS pulse from GPIO (high precision, used for sync)
refclock PPS /dev/pps0 refid PPS lock NMEA precision 1e-9 prefer

# Internet fallback sources
pool time.cloudflare.com iburst maxsources 4
pool 2.pool.ntp.org iburst maxsources 3

# Serve the LAN (Your LAN here)
allow 192.168.0.0/16
allow 10.0.0.0/8
```

Disable chrony's seccomp filter, which silently blocks the PPS device:

```bash
sudo sed -i 's|DAEMON_OPTS="-F 1"|DAEMON_OPTS=""|' /etc/default/chrony
```

Restart chrony:

```bash
sudo systemctl restart chrony
```

Wait 60 seconds, then verify:

```bash
chronyc sources
```

Expected output:

```
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
#? NMEA                          0   4   177    15  ...
#* PPS                           0   4   177    15  +480ns[+1009ns] +/-  199ns
^- time.cloudflare.com           3   6   ...
```

The `#*` next to PPS means it's selected as the synced source. You're now Stratum 1.

## 5. Install the dashboard

```bash
sudo mkdir -p /opt/chrony-dashboard
sudo cp dashboard/chrony_dashboard.py /opt/chrony-dashboard/
sudo cp dashboard/chrony-webpage.service /etc/systemd/system/
sudo mkdir -p /var/lib/chrony-dashboard

sudo systemctl daemon-reload
sudo systemctl enable --now chrony-webpage
```

Access at `http://<pi-ip>:8080`.

To put it behind a reverse proxy (nginx, Caddy, etc.) for HTTPS, point your proxy at `http://localhost:8080`.

## 6. Enable measurement logging

For the PPS jitter sparkline in the dashboard to populate, add `log refclocks` to chrony.conf:

```bash
sudo sed -i '/^log /d' /etc/chrony/chrony.conf
echo "log tracking measurements statistics refclocks" | sudo tee -a /etc/chrony/chrony.conf
sudo systemctl restart chrony
```

Within a few minutes `/var/log/chrony/refclocks.log` will start collecting data.

## 7. (Optional) PTP master

Install:

```bash
sudo apt install -y linuxptp
sudo cp config/ptp4l.conf.example /etc/linuxptp/ptp4l.conf
sudo cp systemd/ptp4l.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ptp4l
```

Verify with `systemctl status ptp4l` and look for "assuming the grand master role".

Note: the Pi 4's onboard NIC does not have hardware timestamping support, so PTP accuracy is limited to software timestamping (microseconds, not nanoseconds). Still useful for PTP-aware clients.

## 8. (Optional) NTS server

Requires a TLS certificate covering your time server's hostname.

If you already have a wildcard cert managed elsewhere, see `scripts/sync-nts-cert.sh.example` for a renewal hook that syncs the cert to this server via SSH.

If issuing fresh on this server, install acme.sh or certbot and point chrony at the cert files.

Add to chrony.conf:

```
ntsserverkey /etc/chrony/nts/privkey.key
ntsservercert /etc/chrony/nts/fullchain.crt
ntsport 4460
ntsdumpdir /var/lib/chrony
ntsntpserver your.hostname.here
```

The cert files must be owned by `_chrony:_chrony` with mode 644 (cert) and 600 (key).

Open TCP port 4460 in any firewall.

## 9. (Optional) Convert LAN clients

Use `scripts/convert-to-chrony.sh` to switch other Linux hosts on the network to use this time server. It detects the existing NTP client, swaps it for chrony, and writes a clean config.

```bash
scp scripts/convert-to-chrony.sh root@host:/tmp/
ssh root@host bash /tmp/convert-to-chrony.sh
```

## Troubleshooting

**Reach 0 on PPS source**: Either PPS pulses aren't reaching the kernel (check `cat /proc/interrupts | grep pps`), or chrony can't read /dev/pps0 (check `ls -la /dev/pps0`, should be mode 644 or 666). If the IRQ counter isn't incrementing, the PPS wire is loose, or the GPIO pin is wrong. Run `sudo ppstest /dev/pps0` to see raw pulses.

**Reach 0 on NMEA source**: The SHM segment from gpsd isn't readable. Verify `ipcs -m` shows the segment with key ending in `32` (NTP unit 2) at mode 666. If not, gpsd isn't running with `-G`, or you have stale segments. Delete them with `ipcrm` and restart gpsd.

**PPS pulses firing too fast (>1 per second)**: Almost always a loose wire or the wrong GPIO pin. A floating PPS line picks up noise. Re-seat the connection at both ends.

**Dashboard shows "DEGRADED"**: Means chrony is synced to internet NTP instead of GPS. Check GPS antenna position and `chronyc sources` to confirm.

**No satellites visible**: GPS module needs a clear view of the sky. Indoor reception works in windows but not in basements or rooms without exterior walls.
