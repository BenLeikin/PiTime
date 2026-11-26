# openTimeServer

A Raspberry Pi stratum-1 NTP/NTS time server. It is disciplined by a GPS
receiver's PPS pulse, and a web dashboard shows its live state.

- **chrony** serves NTP and NTS. Its time comes from the GPS PPS pulse on
  GPIO 18, which is numbered by the GPS NMEA time from gpsd. Internet pools
  act as sanity checks.
- **gpsd** reads the GPS receiver on the Pi's UART (`/dev/serial0`) and passes
  NMEA time to chrony through shared memory (SHM unit 2).
- **chrony_dashboard.py** is a single-file Python web dashboard (standard
  library only) on port 8080. It shows sync state, sources, GPS sky view,
  PPS jitter, clients and system health.

## Repository layout

| Path | Installed to | Purpose |
|---|---|---|
| `dashboard/chrony_dashboard.py` | `/opt/chrony-dashboard/` | Dashboard server and page |
| `systemd/chrony-webpage.service` | `/etc/systemd/system/` | Dashboard service (runs as `_chrony`, sandboxed) |
| `etc/chrony/chrony.conf` | `/etc/chrony/` | chrony: PPS + NMEA refclocks, pools, NTS |
| `etc/default/gpsd` | `/etc/default/` | gpsd devices and options |
| `boot/firmware/config.txt` | `/boot/firmware/` | Enables UART, disables Bluetooth, PPS on GPIO 18 |
| `install.sh` | – | Installs on the Pi (run as root) |
| `deploy.sh` | – | Uploads to the Pi over SSH and runs `install.sh` |
| `tests/` | – | Parser tests using recorded chronyc/gpsd output |

## Hardware

- Raspberry Pi 4/5 (the configs assume Raspberry Pi OS)
- GPS module with a PPS output, wired to the UART (GPIO 14/15) with PPS on
  **GPIO 18**
- An antenna with a clear view of the sky

## First-time setup on the Pi

```bash
sudo apt install chrony gpsd gpsd-clients pps-tools python3
git clone https://gitlab.pilg0re.net/Ben/opentimeserver.git
cd opentimeserver
sudo ./install.sh --configs
sudo reboot   # needed once for the UART/PPS overlays in config.txt
```

NTS needs a certificate and key at `/etc/chrony/nts/fullchain.crt` and
`/etc/chrony/nts/privkey.key`, readable by `_chrony`.

Check it is working:

```bash
chronyc sources        # PPS should be marked '*'
ppstest /dev/pps0      # one pulse per second
curl -s localhost:8080/api/health
```

## Deploying updates from a workstation

Add an SSH alias for the Pi to `~/.ssh/config`:

```
Host timeserver
    HostName time.pilg0re.net
    User <your-pi-user>
```

Then, from this repository:

```bash
./deploy.sh            # dashboard + service unit
./deploy.sh --diff     # just show what differs from the Pi
./deploy.sh --configs  # also chrony/gpsd/boot configs (shows a diff, asks first)
```

Set `PI_HOST=user@host` to target another machine. With passwordless sudo
for the deploy user, the deploy runs unattended. Otherwise it prompts for the
sudo password. Config files are backed up on the Pi as
`<file>.bak-<timestamp>` before being replaced.

## Dashboard

```
python3 chrony_dashboard.py [--port 8080] [--bind 0.0.0.0] [--pps-refid PPS] [--cache-ttl 2]
```

- `GET /` serves the dashboard page.
- `GET /api/status` returns the full JSON snapshot. One snapshot is shared by
  all viewers for `--cache-ttl` seconds.
- `GET /api/health` returns `{"status": "OK"|"DEGRADED"|"FAULT", ...}`, for
  monitoring.

The PPS jitter chart reads `/var/log/chrony/refclocks.log`, so
`log refclocks` must be enabled in `chrony.conf`. Visitor counts are kept in
`/var/lib/chrony-dashboard/visitors.json`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
