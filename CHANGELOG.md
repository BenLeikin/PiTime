# Changelog

Notable changes to this project. Versions follow [semantic versioning](https://semver.org/): the major number changes when an upgrade needs action on an existing install.

## [2.1.0] - 2026-11-26

### Added

- **History charts**: one sample a minute (offsets, frequency, temperature, satellites, signal, health) kept for 30 days, shown for the last 6 hours to 30 days, and available as `/api/history`.
- The health monitor compares the time server's time with outside NTP servers, using the four timestamps of each exchange, and alerts if it is further from them than network delay can explain.
- Automatic security updates with `unattended-upgrades`, rebooting at 03:30 only when an update needs it, in place of a nightly upgrade-and-reboot.
- The Pi's hardware watchdog, kept fed by systemd.
- `--state-dir` for where visitor counts and history are kept.

### Changed

- The GPS runs at 115200 baud instead of 9600, and its settings are saved in the receiver. NMEA time now reaches chrony 0.070 s after the second instead of 0.13 s; the NMEA offset in `chrony.conf` changes to match.
- chrony logs only `tracking` and `tempcomp`. The `measurements`, `statistics` and `refclocks` logs were most of the SD card's writes and nothing read them; the PPS chart now collects its samples from chronyc.

## [2.0.0] - 2026-11-26

PiTime and the openTimeServer repository are merged into one project.

**Upgrading from 1.0.0 needs action:** the chrony configuration is split into a generic file and `site/` settings, the dashboard is now several files installed by `install.sh`, and the NMEA refclock moves from SHM unit 2 to unit 0. Follow INSTALL.md rather than copying files over an old install.

### Fixed

- **chrony never took the time of day from GPS.** The refclock called NMEA read gpsd's SHM unit 2, which is the PPS pulse numbered by the system clock. It now reads unit 0 (the NMEA sentences) with a measured offset, and `minsources 1` lets GPS alone discipline the clock when the internet is down.
- Dashboard source table: offsets and error margins were parsed wrongly from chronyc's text output. Every chronyc report is now read in CSV mode.
- PPS chart: it mixed raw pulse phase with clock offsets, and showed about 22 samples instead of 60.
- "Time Since Lock" always showed 0-5 seconds. It is now tracked on the server.
- Every refresh waited 3 seconds whenever the GPS saw no satellites.
- A failed poll left old data on screen with no sign it was stale.
- The Activity row showed chronyc's "200 OK" status line as a count.
- Visitor counts behind a reverse proxy counted everyone as the proxy.
- The NTS certificate sync could fail silently and leave the server on an expiring certificate.

### Added

- `install.sh` and `deploy.sh`: one-command install on the Pi and deploy from a workstation, with config backups, a diff preview, validation of the chrony config and a health check.
- Health monitor (`scripts/health_monitor.py`) that alerts through a webhook when the server is unhealthy or unreachable, and `/api/health?strict=1` for monitors that read only the HTTP status.
- Health reports a source chrony still names as its reference but that has stopped responding.
- Wired + Wi-Fi failover on one address, with gratuitous ARP on link change.
- Boot-time low-jitter tuning: fixed CPU speed, other interrupts kept off the PPS CPU, GPS stationary mode.
- Temperature logging and `tools/fit_tempcomp.py` to fit chrony's `tempcomp`.
- Per-client rate limits for NTP and NTS key exchange.
- Tests (Python and JavaScript), `ruff` and `shellcheck` configuration, `tools/check.sh` and GitLab CI.
- Header clock in the server's own time zone (`--timezone`), `--trusted-proxy`, `--cache-ttl`, `--version`.

### Changed

- The dashboard runs as `_chrony` in a systemd sandbox instead of as root.
- The page moved out of the Python file into `dashboard/static/`; the Content-Security-Policy no longer allows inline scripts.
- Viewers outside the server's own network get client addresses shortened and no server address or antenna position.
- One data snapshot is shared by all viewers; a refresh takes about 0.03 s instead of 0.27 s.
- Problems reading chrony, gpsd or `/proc` are logged instead of silently showing a dash.
- Site-specific values live in `site/`; `install.sh` adds the lines it needs to `/boot/firmware/config.txt` instead of replacing the file.

## [1.0.0] - 2026-06-12

The project as first published as PiTime: a GPS-disciplined stratum 1 NTP server on a Raspberry Pi 4 with a single-file web dashboard, example configs for chrony, gpsd and PTP, an installation guide and a wiring guide.
