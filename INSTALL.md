# Installation Guide

End-to-end setup for a GPS-disciplined Stratum 1 NTP server on a Raspberry Pi 4. Tested on Raspberry Pi OS Bookworm.

Most of the work is done by `install.sh`, which copies the dashboard, its service, and (with `--configs`) the system configs from this repository. The sections below explain what each piece is for, what to check, and what to change for your own network.

## Prerequisites

- Raspberry Pi 4 (any RAM variant)
- Fresh install of Raspberry Pi OS (64-bit recommended)
- Static IP or DHCP reservation for the Pi
- SSH access
- GT-U7 or similar GPS module wired per [WIRING.md](WIRING.md)

## 1. System preparation

Update and install required packages, then get this repository:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y chrony gpsd gpsd-clients pps-tools python3 git
git clone https://github.com/BenLeikin/PiTime.git
cd PiTime
```

### Make the configs yours

The files under `etc/` are the ones running on my server. Before installing, change:

| File | What to change |
|---|---|
| `etc/chrony/chrony.conf` | `ntsntpserver time.pilg0re.net` to your host name, or remove the whole NTS block if you are not using NTS (without the certificate files chrony logs errors and NTS does not work). `allow all` serves anyone who can reach the Pi; narrow it to your LAN (for example `allow 192.168.0.0/16`) if you want. |
| `systemd/chrony-webpage.service` | `--trusted-proxy 10.0.0.69` to your reverse proxy's address, or remove the option if there is none. |
| `boot/firmware/config.txt` | This is a complete Pi 4 file and replaces yours. If you have your own settings, skip it and add the two lines from section 2 by hand. |

## 2. Free up the UART

The Pi 4 connects its internal Bluetooth chip to the primary (high-quality) UART by default. We need that UART for the GPS module.

`boot/firmware/config.txt` in this repository already has the lines below under `[all]`; `install.sh --configs` installs it (keeping a backup of yours). To do it by hand instead, edit `/boot/firmware/config.txt` and add:

```
enable_uart=1
dtoverlay=disable-bt
dtoverlay=pps-gpio,gpiopin=18
```

`disable-bt` turns off the onboard Bluetooth so the UART is freed. `pps-gpio` registers GPIO 18 as the PPS input pin.

Edit `/boot/firmware/cmdline.txt` (all on one line, do not add line breaks):

Find any `console=serial0,115200` and remove it. Optionally append CPU isolation parameters:

```
isolcpus=3 nohz_full=3 rcu_nocbs=3 irqaffinity=0-2
```

This keeps ordinary tasks off CPU core 3. Note that the PPS interrupt itself is always serviced on CPU 0 (see section 10), so the isolation is not what keeps PPS quiet; the tuning script is.

Disable conflicting services:

```bash
sudo systemctl disable --now hciuart
sudo systemctl disable --now serial-getty@ttyAMA0
sudo systemctl disable --now bluetooth
```

## 3. Install

```bash
sudo ./install.sh --configs
sudo reboot
```

`install.sh --configs` installs chrony's and gpsd's configs, `config.txt`, the dashboard and its service, the failover helpers and the tuning service, and restarts what changed. Every file it replaces is first copied to `<file>.bak-<timestamp>`. The reboot is needed once for the UART and PPS overlays.

After the reboot, verify:

```bash
ls -la /dev/serial0      # Should symlink to ttyAMA0
ls -la /dev/pps0          # Should exist
cat /proc/interrupts | grep pps   # Should show pps@12.-1 with rising counter
```

## 4. gpsd

`etc/default/gpsd` sets:

```
START_DAEMON="true"
USBAUTO="true"
DEVICES="/dev/serial0 /dev/pps0"
GPSD_OPTIONS="-n -G"
```

The `-n` flag makes gpsd read continuously without waiting for a client, which chrony needs. The `-G` flag makes gpsd listen on all network interfaces instead of only localhost; remove it unless other machines need to read your GPS (it also exposes your position to them).

Add a udev rule so PPS devices are readable without root (handy for `ppstest`):

```bash
sudo cp etc/udev/rules.d/99-pps.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

Verify GPS data:

```bash
gpspipe -w -n 10
```

You should see JSON with TPV (position) and SKY (satellites) data. If no TPV appears with `mode: 3`, your antenna needs better positioning. South-facing window with clear sky view works best.

## 5. chrony

`etc/chrony/chrony.conf` is installed by `install.sh --configs`. Key sections:

```
# GPS time of day from gpsd (NMEA sentences), only used to number the pulses
refclock SHM 0 refid NMEA precision 1e-3 offset 0.121 delay 0.2 noselect

# PPS pulse from GPIO (high precision, used for sync)
refclock PPS /dev/pps0 refid PPS lock NMEA precision 1e-9 prefer

# Internet sanity-check sources
pool time.cloudflare.com iburst maxsources 4
pool 2.pool.ntp.org iburst maxsources 3

# One good source is enough, so GPS alone keeps the clock when the internet is down
minsources 1
```

**Use SHM unit 0, not 2.** With `DEVICES="/dev/serial0 /dev/pps0"`, gpsd puts the NMEA time in unit 0. Unit 2 is the `/dev/pps0` pulse with the second taken from the system clock. It shows nanosecond offsets and looks like the better source, but it cannot tell chrony which second it is, so locking PPS to it means the time of day never comes from GPS.

**Calibrate the NMEA offset.** The sentences arrive a little after the second they describe, and `lock NMEA` only works if the corrected NMEA time is within 0.2 s of the truth. Measure the delay on your module:

```bash
sudo ntpshmmon -n 20
```

On the `NTP0` lines, the `Clock` column minus the `Real` column is the delay (about 0.12 s for my module at 9600 baud). Put that value in `offset`.

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
#? NMEA                          0   4   377    15   +481us[ +480us] +/-  101ms
#* PPS                           0   4   377    15  +480ns[+1009ns] +/-  199ns
^- time.cloudflare.com           3   6   ...
```

The `#*` next to PPS means it's selected as the synced source. You're now Stratum 1. NMEA shows `?` because it is marked `noselect`: it only numbers the pulses. Its offset should be within a few milliseconds of zero; if it is near 100 ms or more, redo the offset calibration above.

## 6. The dashboard

Installed by `install.sh` (with or without `--configs`) to `/opt/chrony-dashboard/`, as the service `chrony-webpage`. It runs as chrony's own user, `_chrony`, inside a systemd sandbox; that user can use chronyd's command socket, which the Clients and Server Stats panels need.

Access at `http://<pi-ip>:8080`. `curl -s localhost:8080/api/health` gives a one-line status for monitoring.

To put it behind a reverse proxy (nginx, Caddy, etc.) for HTTPS, point your proxy at `http://<pi-ip>:8080`, have it send `X-Real-IP`, and pass the proxy's address to the dashboard with `--trusted-proxy` in the service unit so visitor counts use the real addresses. The dashboard sets its own security headers (`Content-Security-Policy`, `X-Frame-Options`, ...); if the proxy adds the same ones, hide the dashboard's copies (`proxy_hide_header` in nginx) so each is sent once.

The PPS jitter chart reads `/var/log/chrony/refclocks.log`, which the repository's `chrony.conf` enables with `log ... refclocks`. Within a few minutes of chrony starting, the chart fills in.

## 7. (Optional) PTP master

Install:

```bash
sudo apt install -y linuxptp
sudo cp etc/linuxptp/ptp4l.conf /etc/linuxptp/ptp4l.conf
sudo cp systemd/ptp4l.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ptp4l
```

The last line of `ptp4l.conf` names the network interface (`[end0]`); change it if yours differs.

Verify with `systemctl status ptp4l` and look for "assuming the grand master role".

Note: the Pi 4's onboard NIC does not have hardware timestamping support, so PTP accuracy is limited to software timestamping (microseconds, not nanoseconds). Still useful for PTP-aware clients.

## 8. (Optional) NTS server

Requires a TLS certificate covering your time server's hostname.

If you already have a wildcard cert managed elsewhere, see `scripts/sync-nts-cert.sh.example` for a renewal hook that pushes the cert to this server via SSH. Run it from the ACME client after each renewal and also daily from cron: it skips the push when the certificate is already in place, and a push that failed is retried the next day instead of leaving the time server on an expiring certificate.

If issuing fresh on this server, install acme.sh or certbot and point chrony at the cert files.

The repository's `chrony.conf` has:

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

Use `scripts/convert-to-chrony.sh` to switch other Linux hosts on the network to use this time server. It detects the existing NTP client, swaps it for chrony, and writes a clean config. Edit `TIME_SERVER` at the top first.

```bash
scp scripts/convert-to-chrony.sh root@host:/tmp/
ssh root@host bash /tmp/convert-to-chrony.sh
```

## 10. Tuning

`install.sh --configs` also installs `timeserver-tune`, run at every boot by `timeserver-tune.service`. It:

- sets the CPU governor to `performance`;
- moves every movable interrupt (network, SD card, Wi-Fi, serial) to CPUs 1-2, leaving CPU 0 to the PPS interrupt, which Linux will not move;
- sets the u-blox receiver's dynamic model to stationary.

Check it with `journalctl -u timeserver-tune`. To undo it, `sudo systemctl disable timeserver-tune` and reboot.

For temperature compensation, the repository's `chrony.conf` logs the CPU temperature (`tempcomp ... 0 0 0 0`, which applies no correction). After a few days of logging, run `tools/fit_tempcomp.py --host <pi>` from a workstation; it prints the `tempcomp` line to put in `chrony.conf`, or says why there is not enough data yet.

## 11. Updating from a workstation

Once the Pi is set up, `deploy.sh` pushes changes from a checkout on another machine and runs `install.sh` there:

```bash
./deploy.sh --diff     # what differs between this checkout and the Pi
./deploy.sh            # dashboard and its service
./deploy.sh --configs  # system configs too (shows the diff and asks first)
```

See the README for the SSH alias it expects.

## Troubleshooting

**Reach 0 on PPS source**: Either PPS pulses aren't reaching the kernel (check `cat /proc/interrupts | grep pps`), or chrony can't read /dev/pps0. If the IRQ counter isn't incrementing, the PPS wire is loose, the GPIO pin is wrong, or the GPS has no fix (most modules only pulse once they have one). Run `sudo ppstest /dev/pps0` to see raw pulses.

**Reach 0 on NMEA source**: gpsd is not feeding SHM unit 0. Run `sudo ntpshmmon -n 5`: you should see `NTP0` lines once a second. If there are none, check `gpspipe -w -n 5` shows TPV reports with a `time`, and that gpsd was started with `-n`.

**NMEA is fine but PPS is never selected**: The lock needs the NMEA time within 0.2 s of the truth. Check the NMEA line in `chronyc sources`; if its offset is far from zero, recalibrate `offset` (section 5).

**PPS pulses firing too fast (>1 per second)**: Almost always a loose wire or the wrong GPIO pin. A floating PPS line picks up noise. Re-seat the connection at both ends.

**Dashboard shows "DEGRADED"**: Means chrony is synced to internet NTP instead of GPS, or the GPS has no fix. The banner lists the reasons. Check GPS antenna position and `chronyc sources` to confirm.

**Dashboard shows "TELEMETRY LINK LOST"**: The page cannot reach the dashboard service; what is on screen is old. Check `systemctl status chrony-webpage`.

**No satellites visible**: GPS module needs a clear view of the sky. Indoor reception works in windows but not in basements or rooms without exterior walls. Signal strengths of 35 dB-Hz and up are good; a fix that hangs on with 20-25 dB-Hz is fragile.
