# GPS-Disciplined Stratum 1 NTP Server on Raspberry Pi

As mentioned here: https://www.hackster.io/news/got-the-time-ben-leikin-does-thanks-to-a-stratum-1-time-server-built-from-a-raspberry-pi-755764618216

A self-hosted, GPS-disciplined Stratum 1 time server running on a Raspberry Pi 4, with a live mission-control style web dashboard, PTP master support, and NTS (Network Time Security) authentication.

Built for accuracy, transparency, and learning. Achieves sub-microsecond offset and ~200ns PPS jitter using a $15 GPS module and the Pi's built-in GPIO PPS support.

https://ntp.pilg0re.net

## What it does

- **Stratum 1 NTP server** synchronized directly from GPS satellites
- **Hardware PPS discipline** via GPIO for nanosecond-grade precision
- **Mission-control web dashboard** with live satellite sky view, telemetry, and historical PPS jitter
- **PTP (Precision Time Protocol) master** for PTP-aware clients on the LAN (software timestamping, so microsecond-level)
- **NTS (Network Time Security)** server for cryptographically authenticated time over the internet
- **Auto-renewing TLS certificate** via acme.sh wildcard, synced from a separate reverse proxy host
- **Low-jitter tuning** applied at boot: fixed CPU speed, a CPU kept free for the PPS interrupt, GPS in stationary mode
- **Temperature compensation** tooling that fits chrony's `tempcomp` from the Pi's own logs
- **Wired + Wi-Fi failover** on one address, so clients never need reconfiguring
- **Internet NTP fallback** when GPS signal is lost
- **Alerting**: a monitor on another machine posts to a webhook when the server turns unhealthy or stops answering
- **Rate limiting** per client, so a public server cannot be abused or swamped
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
| CPU usage | under 1% of the Pi (chrony, gpsd and the dashboard together) |
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

Everything specific to one site (who may use the server, the NTS host name, the reverse proxy's address) is in `site/`. Those files hold my server's values; INSTALL.md lists what to change for yours.

## Repository layout

| Path | Installed to | Purpose |
|---|---|---|
| `dashboard/chrony_dashboard.py` | `/opt/chrony-dashboard/` | Dashboard server and data collectors |
| `dashboard/static/` | `/opt/chrony-dashboard/static/` | The page: `index.html`, `dashboard.css`, `dashboard.js` |
| `systemd/chrony-webpage.service` | `/etc/systemd/system/` | Dashboard service (runs as `_chrony`, sandboxed) |
| `site/etc/default/chrony-dashboard` | `/etc/default/` | **Site setting:** dashboard options (reverse proxy address, time zone) |
| `site/etc/chrony/conf.d/site.conf` | `/etc/chrony/conf.d/` | **Site setting:** who may use the server, NTS certificate and host name |
| `site/etc/ssh/sshd_config.d/01-timeserver.conf` | `/etc/ssh/sshd_config.d/` | **Site setting:** SSH with keys from anywhere, passwords only from the LAN; no X11 forwarding |
| `usr/local/sbin/nts-cert-receiver` | `/usr/local/sbin/` | The only thing the certificate-sync key may run: installs a pushed NTS certificate |
| `site/etc/systemd/system/gitlab-runner.service.d/timeserver.conf` | same path under `/` | **Site setting:** low priority for a GitLab CI runner on the Pi (installed only if a runner is) |
| `etc/chrony/chrony.conf` | `/etc/chrony/` | chrony: PPS + NMEA refclocks, pools, rate limits, temperature logging |
| `etc/default/gpsd` | `/etc/default/` | gpsd devices and options |
| `boot/firmware/config.txt.required` | – | Lines `install.sh` adds to `/boot/firmware/config.txt` if missing (UART, Bluetooth off, PPS on GPIO 18) |
| `etc/NetworkManager/dispatcher.d/50-link-failover-garp` | `/etc/NetworkManager/dispatcher.d/` | Announces the address when the active link changes |
| `etc/sysctl.d/90-timeserver-failover.conf` | `/etc/sysctl.d/` | Only the preferred link answers ARP |
| `usr/local/sbin/timeserver-tune` | `/usr/local/sbin/` | Boot-time low-jitter settings: fixed CPU speed, other interrupts off the PPS CPU, GPS stationary mode |
| `systemd/timeserver-tune.service` | `/etc/systemd/system/` | Runs `timeserver-tune` at boot |
| `etc/systemd/system.conf.d/90-timeserver-watchdog.conf` | `/etc/systemd/system.conf.d/` | Hardware watchdog: reboots the Pi if it hangs |
| `etc/apt/apt.conf.d/20auto-upgrades`, `52timeserver-unattended-upgrades` | `/etc/apt/apt.conf.d/` | Automatic security updates; reboot only when one needs it |
| `etc/udev/rules.d/99-pps.rules` | `/etc/udev/rules.d/` | Makes `/dev/pps*` readable without root (copy by hand) |
| `etc/linuxptp/ptp4l.conf`, `systemd/ptp4l.service` | `/etc/linuxptp/`, `/etc/systemd/system/` | Optional PTP master (copy by hand) |
| `install.sh` | – | Installs on the Pi (run as root) |
| `deploy.sh` | – | Uploads to the Pi over SSH and runs `install.sh` |
| `scripts/health_monitor.py`, `scripts/timeserver-monitor.conf.example` | another machine | Alerts through a webhook when the server is unhealthy or unreachable |
| `scripts/convert-to-chrony.sh` | – | Points another Linux host at this server |
| `scripts/sync-nts-cert.sh.example` | – | Pushes a renewed TLS certificate to the Pi for NTS |
| `tools/release_notes.py` | – | Prints a version's notes from CHANGELOG.md, for GitHub or GitLab releases |
| `tools/fit_tempcomp.py` | – | Fits chrony `tempcomp` coefficients from the Pi's temperature and frequency logs |
| `tools/check.sh`, `ruff.toml`, `.gitlab-ci.yml` | – | Tests and linters, locally and in CI |
| `tests/` | – | Python and JavaScript tests using recorded chronyc/gpsd output and synthetic logs |

## Deploying updates from a workstation

Add an SSH alias for the Pi to `~/.ssh/config`:

```
Host timeserver
    HostName <your-pi-address>
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
                           [--cache-ttl 2] [--trusted-proxy IP]... [--timezone NAME]
                           [--version]
```

The service takes its site options from `DASHBOARD_OPTS` in
`/etc/default/chrony-dashboard`.

- `GET /` serves the page; its stylesheet and script are under `/static/`.
- `GET /api/status` returns the full JSON snapshot. One snapshot is shared by
  all viewers for `--cache-ttl` seconds.
- `GET /api/health` returns `{"status": "OK"|"DEGRADED"|"FAULT", ...}` with
  the reasons and the dashboard's version. Add
  `?strict=1` to get HTTP 503 whenever the status is not `OK`, for monitors
  that only look at the status code.

**Who sees what.** Viewers on the server's own network see everything.
Everyone else gets a snapshot with client addresses shortened
(`203.0.113.x`) and without the server's address or the antenna position.
Behind a reverse proxy, pass its address with `--trusted-proxy` and have it
send `X-Real-IP`: the dashboard then judges viewers, and counts visitors, by
the address the proxy reports. A request forwarded by any other proxy is
treated as public.

Problems reading chrony, gpsd or `/proc` are written to the journal
(`journalctl -u chrony-webpage`), once and then at most every five minutes
each. The PPS chart collects chrony's PPS samples itself (one per poll), so it needs
no refclocks log; it fills in over its first 16 minutes. Visitor counts and
the history behind the charts (one sample a minute, kept 30 days, written to
disk every 10 minutes) are in `/var/lib/chrony-dashboard/`.

## Monitoring

The dashboard cannot tell you it is down, and nobody watches a dashboard
around the clock: my GPS antenna once failed and the server ran on internet
time for a month before I noticed. `scripts/health_monitor.py` closes that
gap. Run it from cron on **another** machine:

```bash
sudo install -m 755 scripts/health_monitor.py /usr/local/bin/timeserver-monitor
sudo install -m 600 scripts/timeserver-monitor.conf.example /etc/default/timeserver-monitor
sudoedit /etc/default/timeserver-monitor       # addresses and the webhook URL
sudo timeserver-monitor --test                 # sends a test message
echo '*/5 * * * * root /usr/local/bin/timeserver-monitor' | sudo tee /etc/cron.d/timeserver-monitor
```

Every run reads `/api/health`, asks the NTP service itself for the time, and
compares that time with two outside servers. The comparison does not depend on
the Pi's own health report or the monitoring machine's clock, so it catches a
server that is confidently wrong, such as one locked to the wrong second.
A change is reported once it has lasted three runs, so restarts stay quiet;
recovery is reported too, and a lasting problem is repeated daily. The
webhook can be Discord, Slack, Mattermost or Google Chat.

## Wired + Wi-Fi failover

The wired and Wi-Fi NetworkManager profiles both carry the same static address, so DNS, port forwards and clients never change. Route
metrics make the cable preferred whenever it is plugged in:

```bash
S="ipv4.method manual ipv4.addresses <address>/<prefix> ipv4.gateway <gateway> ipv4.dns <dns-server>"
sudo nmcli con mod "<wired-profile>" $S ipv4.route-metric 100
sudo nmcli con mod "<wifi-profile>" $S ipv4.route-metric 600 802-11-wireless.powersave 2
```

`etc/NetworkManager/dispatcher.d/50-link-failover-garp` (installed with
`--configs`) sends gratuitous ARPs from whichever interface takes over, so
other machines switch to its MAC address within seconds.
`etc/sysctl.d/90-timeserver-failover.conf` sets `arp_filter`, so while the
cable is in, only the wired interface answers ARP for the address; otherwise
clients could send requests over Wi-Fi and get replies by cable, which skews
their measured offset. Expect NTP clients to see millisecond-level jitter
while the Pi is on Wi-Fi. The Pi's own clock stays PPS-disciplined either way.

## History

The dashboard records one sample a minute (offsets, frequency, temperature,
satellites, signal, client traffic, health), keeps 30 days, and draws them in
the History section for the last 6 hours to 30 days. Samples are written to
disk every 10 minutes to spare the SD card.

- **On GPS** is the share of the period the server was at stratum 1, with the
  number of outages and the longest. Stretches with nothing recorded (the
  dashboard or the Pi was down) count as unknown, not as outages.
- **Client traffic** is NTP requests per hour, NTS-authenticated ones, and
  ones dropped by the rate limits.
- **Crystal ageing** takes the last 30 days on GPS, removes the frequency's
  dependence on temperature, and shows what is left day by day with its trend
  in ppm per month: how fast the crystal is drifting as it ages.

`GET /api/history?range=6h|24h|7d|30d` returns the same data as JSON.

## On the page

- The header shows the active network link: cable, or Wi-Fi and its signal.
  Over a cable clients get time good to tens of microseconds; over Wi-Fi, to
  milliseconds.
- When the clock is not on GPS, a banner says since when, what it uses
  instead, and chrony's bound on its error.
- The NTS certificate's and the leap-second table's expiry dates are listed;
  the status turns DEGRADED 21 and 30 days before them.

## Keeping the Pi healthy

- **SD card wear.** chrony logs only `tracking` and `tempcomp`, which the
  temperature fit needs. The busier logs (`measurements`, `statistics`,
  `refclocks`) were most of the card's writes, and nothing reads them; turn
  them back on in `chrony.conf` when debugging.
- **Updates.** `unattended-upgrades` installs security updates from Debian
  and Raspberry Pi OS's own packages, and reboots at 03:30 only when an update
  needs it. Install it with `sudo apt install unattended-upgrades`; the
  settings are in `etc/apt/apt.conf.d/`. Upgrades never replace a changed
  config file.
- **Watchdog.** systemd keeps the Pi's hardware watchdog fed. If the system
  hangs, the watchdog reboots it within 15 seconds.

## Tuning and temperature compensation

`timeserver-tune` runs at boot (installed with `--configs`) and:

- sets the CPU governor to `performance`, so the delay before a pulse is
  timestamped does not change with load;
- moves every interrupt that can be moved off CPU 0 (and off any isolated
  core). The PPS pin's interrupt is always serviced on CPU 0 and cannot be
  moved, so this leaves CPU 0 to it;
- puts the u-blox receiver in its stationary model and its serial port at
  115200 baud, so NMEA arrives 0.07 s after the second instead of 0.13 s.

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
tools/check.sh
```

runs the Python tests, the JavaScript tests (`node`), `ruff` and
`shellcheck`, skipping any tool that is not installed. GitLab CI runs the
same script on every push (`.gitlab-ci.yml`). It is written for a runner with
the shell executor and installs the two linters into a virtualenv on its
first run.

## Versions

[CHANGELOG.md](CHANGELOG.md) lists what changed in each version.
`tools/release_notes.py 2.2.0` prints one version's notes, ready to paste into
a release on GitHub or GitLab. 1.0.0 is the
project as first published; 2.0.0 is the merged and reworked one. Upgrading
an existing 1.0.0 install means following INSTALL.md again.

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
