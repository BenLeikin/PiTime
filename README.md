# GPS-Disciplined Stratum 1 NTP Server on Raspberry Pi

As mentioned here: https://www.hackster.io/news/got-the-time-ben-leikin-does-thanks-to-a-stratum-1-time-server-built-from-a-raspberry-pi-755764618216

A self-hosted, GPS-disciplined Stratum 1 time server running on a Raspberry Pi 4, with a live mission-control style web dashboard, PTP master support, and NTS (Network Time Security) authentication.

Built for accuracy, transparency, and learning. Achieves sub-microsecond offset and ~200ns PPS jitter using a $15 GPS module and the Pi's built-in GPIO PPS support.

https://ntp.pilg0re.net

## What it does

- **Stratum 1 NTP server** synchronized directly from GPS satellites
- **Hardware PPS discipline** via GPIO for nanosecond-grade precision
- **Mission-control web dashboard** with live satellite sky view, telemetry, and historical PPS jitter
- **PTP (Precision Time Protocol) master** for sub-microsecond LAN-local time distribution
- **NTS (Network Time Security)** server for cryptographically authenticated time over the internet
- **Auto-renewing TLS certificate** via acme.sh wildcard, synced from a separate reverse proxy host
- **Low-jitter tuning** applied at boot: fixed CPU speed, a CPU kept free for the PPS interrupt, GPS in stationary mode
- **Temperature compensation** tooling that fits chrony's `tempcomp` from the Pi's own logs
- **Wired + Wi-Fi failover** on one address, so clients never need reconfiguring
- **Internet NTP fallback** when GPS signal is lost
- **One-command install and deploy**, with config backups and a health check

## Performance

Real numbers from a running instance:

| Metric | Value |
|---|---|
| Stratum | 1 |
| Reference | GPS via PPS |
| Typical last offset | 100-1000 nanoseconds |
| PPS sample standard deviation | ~200 ns |
| RMS offset | <1 microsecond |
| Root delay | 1 nanosecond (minimum reported) |
| Frequency stability | <0.05 ppm skew |
| CPU usage | <5% on isolated core |
| Cost | ~$90 total hardware |

These are the Pi's own clock against GPS. What a client sees also depends on the network between it and the Pi: tens of microseconds over wired Ethernet, milliseconds over Wi-Fi.

## Hardware

- **Raspberry Pi 4 Model B** (any model will work, this project really doesn't need much proc power or memory at all)
- **GT-U7 GPS module** (u-blox 7 based, NEO-6M compatible, ~$10-15 on Amazon)
- **MicroSD card** 32GB+
- **Case with passive cooling**

The GT-U7 is a budget module but produces clean PPS pulses. For better holdover or sub-100ns precision, consider a u-blox NEO-M8T or NEO-F9T timing-grade module.

## Wiring

GPS module to Pi 4 GPIO header:

| GPS Module | Pi Pin | Pi Function |
|---|---|---|
| VCC (3.3V or 5V) | 4 | 5V |
| GND | 6 | Ground |
| RX | 8 | GPIO 14 / UART TX |
| TX | 10 | GPIO 15 / UART RX |
| PPS | 12 | GPIO 18 |

See [WIRING.md](WIRING.md) for full details, antenna placement, and notes on the bluetooth/UART conflict workaround.

## Architecture

```
+-------------+      +----------+      +---------+      +----------+
| GPS Module  |----->| /dev/    |----->| gpsd    |----->| chrony   |
| (NMEA)      |      | serial0  |      | SHM 0   |      | refclock |
+-------------+      +----------+      +---------+      | NMEA     |
                                                        +----------+
                                                             | numbers
                                                             v the pulses
+-------------+      +----------+                       +----------+
| GPS Module  |----->| /dev/    |---------------------->| chrony   |
| (PPS pulse) |      | pps0     |                       | refclock |
+-------------+      +----------+                       | PPS      |
                                                        +----------+
                                                             |
                                                             v
                                                        +---------+
                                                        | System  |
                                                        | Clock   |
                                                        +---------+
                                                             |
                                             +---------------+---------------+
                                             |               |               |
                                             v               v               v
                                        +--------+      +--------+      +---------+
                                        | NTP    |      | NTS    |      | PTP     |
                                        | Server | <--- | KE     |      | Master  |
                                        | UDP    |      | TCP    |      | UDP     |
                                        | 123    |      | 4460   |      | 319/320 |
                                        +--------+      +--------+      +---------+
```

- **chrony** serves NTP and NTS. Its time comes from the PPS pulse on GPIO 18; the NMEA time from gpsd only says which second each pulse belongs to. Internet pools act as sanity checks.
- **gpsd** reads the receiver on `/dev/serial0` and passes NMEA time to chrony through shared memory **unit 0**. Unit 2 is the `/dev/pps0` pulse with the second taken from the system clock. It looks precise but is not GPS time of day, so chrony must not lock PPS to it.
- **chrony_dashboard.py** is a single-file Python web dashboard (standard library only) on port 8080.

## Quick start

See [INSTALL.md](INSTALL.md) for full step-by-step setup.

The short version:

1. Wire the GPS module to the Pi
2. On the Pi:
   ```bash
   sudo apt install chrony gpsd gpsd-clients pps-tools python3
   git clone https://github.com/BenLeikin/PiTime.git
   cd PiTime
   sudo ./install.sh --configs
   sudo reboot   # once, for the UART/PPS overlays in config.txt
   ```
3. Check it: `chronyc sources` should mark PPS with `*`, and `curl -s localhost:8080/api/health` should say `OK`
4. (Optional) Configure NTS with a wildcard cert
5. (Optional) Enable the PTP master
6. (Optional) Convert other LAN clients with `scripts/convert-to-chrony.sh`

The configs in `etc/` are the ones running on my server, so they carry my addresses and host names. INSTALL.md lists what to change for yours.

## Repository layout

| Path | Installed to | Purpose |
|---|---|---|
| `dashboard/chrony_dashboard.py` | `/opt/chrony-dashboard/` | Dashboard server and page |
| `systemd/chrony-webpage.service` | `/etc/systemd/system/` | Dashboard service (runs as `_chrony`, sandboxed) |
| `etc/chrony/chrony.conf` | `/etc/chrony/` | chrony: PPS + NMEA refclocks, pools, NTS, temperature logging |
| `etc/default/gpsd` | `/etc/default/` | gpsd devices and options |
| `boot/firmware/config.txt` | `/boot/firmware/` | Enables UART, disables Bluetooth, PPS on GPIO 18 |
| `etc/NetworkManager/dispatcher.d/50-link-failover-garp` | `/etc/NetworkManager/dispatcher.d/` | Announces the address when the active link changes |
| `etc/sysctl.d/90-timeserver-failover.conf` | `/etc/sysctl.d/` | Only the preferred link answers ARP |
| `usr/local/sbin/timeserver-tune` | `/usr/local/sbin/` | Boot-time low-jitter settings: fixed CPU speed, other interrupts off the PPS CPU, GPS stationary mode |
| `systemd/timeserver-tune.service` | `/etc/systemd/system/` | Runs `timeserver-tune` at boot |
| `etc/udev/rules.d/99-pps.rules` | `/etc/udev/rules.d/` | Makes `/dev/pps*` readable without root (copy by hand) |
| `etc/linuxptp/ptp4l.conf`, `systemd/ptp4l.service` | `/etc/linuxptp/`, `/etc/systemd/system/` | Optional PTP master (copy by hand) |
| `install.sh` | – | Installs on the Pi (run as root) |
| `deploy.sh` | – | Uploads to the Pi over SSH and runs `install.sh` |
| `scripts/convert-to-chrony.sh` | – | Points another Linux host at this server |
| `scripts/sync-nts-cert.sh.example` | – | Pushes a renewed TLS certificate to the Pi for NTS |
| `tools/fit_tempcomp.py` | – | Fits chrony `tempcomp` coefficients from the Pi's temperature and frequency logs |
| `tests/` | – | Tests using recorded chronyc/gpsd output and synthetic logs |

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
python3 chrony_dashboard.py [--port 8080] [--bind 0.0.0.0] [--pps-refid PPS]
                           [--cache-ttl 2] [--trusted-proxy IP]...
```

Behind a reverse proxy, pass its address with `--trusted-proxy` (set in the
systemd unit) and have it send `X-Real-IP`. Visitor counts then use the real
visitor address; the header is ignored from anyone else.

- `GET /` serves the dashboard page.
- `GET /api/status` returns the full JSON snapshot. One snapshot is shared by
  all viewers for `--cache-ttl` seconds.
- `GET /api/health` returns `{"status": "OK"|"DEGRADED"|"FAULT", ...}`, for
  monitoring.

The PPS jitter chart reads `/var/log/chrony/refclocks.log`, so
`log refclocks` must be enabled in `chrony.conf`. Visitor counts are kept in
`/var/lib/chrony-dashboard/visitors.json`.

## Wired + Wi-Fi failover

The wired (`end0`) and Wi-Fi (`wlan0`) NetworkManager profiles both carry the
same static address, so DNS, port forwards and clients never change. Route
metrics make the cable preferred whenever it is plugged in:

```bash
S="ipv4.method manual ipv4.addresses 10.0.0.16/22 ipv4.gateway 10.0.0.1 ipv4.dns 10.0.0.13 ipv4.dns-search pilg0re.net"
sudo nmcli con mod "Wired connection 1" $S ipv4.route-metric 100
sudo nmcli con mod <wifi-profile> $S ipv4.route-metric 600 802-11-wireless.powersave 2
```

`etc/NetworkManager/dispatcher.d/50-link-failover-garp` (installed with
`--configs`) sends gratuitous ARPs from whichever interface takes over, so
other machines switch to its MAC address within seconds.
`etc/sysctl.d/90-timeserver-failover.conf` sets `arp_filter`, so while the
cable is in, only the wired interface answers ARP for the address; otherwise
clients could send requests over Wi-Fi and get replies by cable, which skews
their measured offset. Expect NTP clients to see millisecond-level jitter
while the Pi is on Wi-Fi. The Pi's own clock stays PPS-disciplined either way.

## Tuning and temperature compensation

`timeserver-tune` runs at boot (installed with `--configs`) and:

- sets the CPU governor to `performance`, so the delay before a pulse is
  timestamped does not change with load;
- moves every interrupt that can be moved to CPUs 1-2. The PPS pin's
  interrupt is always serviced on CPU 0 and cannot be moved, so this leaves
  CPU 0 to it;
- puts the u-blox receiver in its stationary model.

On my Pi this roughly halved the spread of PPS offsets.

The crystal's frequency follows temperature (about -0.2 ppm per degree C
here). `chrony.conf` logs the CPU temperature next to the frequency, and
after a few days:

```bash
tools/fit_tempcomp.py --host timeserver
```

prints the `tempcomp` line that cancels it, or says why the data is not ready
yet.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Why I built this

I wanted a real Stratum 1 time reference on my home network, both for the technical satisfaction and to learn how GPS-based timing actually works at the systems level. Commercial Stratum 1 references start around $500 and don't teach you anything. A Pi with a $15 GPS module produces sub-microsecond accuracy and forces you to understand every layer of the stack.

The dashboard exists because every existing chrony monitoring tool is either ugly, requires Prometheus, or doesn't visualize the satellite constellation. Mine does all of the above in a single Python file with zero dependencies beyond the standard library.

## License

[MIT](LICENSE). The dashboard page loads the Orbitron and Space Mono fonts from
Google Fonts at view time; they are not part of this repository and are
licensed separately (SIL Open Font License).

## Acknowledgments

- The [chrony](https://chrony.tuxfamily.org/) project for being the cleanest NTP daemon to work with
- The [linuxptp](https://linuxptp.nwtime.org/) project for PTP support
- The [acme.sh](https://github.com/acmesh-official/acme.sh) project for sane TLS automation
