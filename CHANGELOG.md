# Changelog

Notable changes to this project. Versions follow [semantic versioning](https://semver.org/): the major number changes when an upgrade needs action on an existing install.

## [2.4.1] - 2026-11-26

### Added

- Explanations (the "?" tips) for everything new in 2.4.0: the five status tiles, the live satellite view, the signal bars, the sky visibility map, the Agreement column, Skew and Data Collected, and the server clock in the header. Explanations for labels that no longer exist are removed.
- A test that every label on the page has an explanation and every explanation has a label.

## [2.4.0] - 2026-11-26

### Added

- **Status at the top**: the status with its reasons, then five tiles - the server's offset, its error bound, stratum, time on GPS over 24 hours, and how far your own device's clock is off.
- **Signal bars**: one bar per satellite under the sky view, its height the signal strength, coloured by whether it is used.
- **Sky visibility map**: over days, where satellites are heard and where they never are, with the blocked directions named. Counts fade with a 7-day half-life, so moving the antenna shows within days. `/api/skymap` serves the grid.
- **Agreement column** in the time sources table: each source's offset and error margin on a scale even in powers of ten.
- **Readable history charts**: value labels on each axis, a time axis in the server's zone, a crosshair readout of the values under the pointer, and charts that start at the first sample while the history is shorter than the chosen range.
- **The browser tab** shows the status: a green, amber or red dot as the icon, and "OK", "DEGRADED", "FAULT" or "NO LINK" in the title.
- **Wall display**: `?display` (linked in the footer) shows only the clock, the status and the tiles, large.
- **Light theme** when the viewer's device asks for one.

### Changed

- Sections in order of importance: status, GPS, history, network, then system details folded away (remembered per browser). Hostname, IP, uptime and temperature are no longer repeated in the header.
- Green, amber and red are used only for good, warning and bad; plain values are plain.
- Clients: a summary line (how many, how many in the last hour, requests an hour), then the list sorted by most recent, folded away; localhost is left out.
- The fonts (Orbitron, Space Mono) are served by the dashboard itself, so viewers' browsers no longer contact Google; the Content-Security-Policy allows fonts from this server only.
- The page appears at once with placeholders instead of a loading screen; keyboard focus is visible, the status is announced to screen readers, and charts have text descriptions.

## [2.3.0] - 2026-11-26

### Fixed

- **Every clock on the page showed the viewer's own device time.** The header clock and the world clocks now show the server's time, measured from the browser the way an NTP client measures it (several quick requests to the new `/api/time`, keeping the shortest round trip), and the header says how far the viewer's device clock is off.
- The GPS-lost banner did not appear while chrony still named a silent PPS as its reference, and the source table showed it as a green "synced". Both now treat a selected source with no recent samples as lost ("silent").
- The frequency card was labelled "Clock Offset"; it is the crystal's rate error.
- The offset card lost its large-number style whenever it was coloured.
- Tooltips that described the wrong thing (uptime, altitude, last sample, the world clock) and "did you know" facts that were wrong or dated are corrected; the facts that use live numbers are only shown when those numbers mean something.

### Changed

- Readability: secondary text has at least 5.5:1 contrast (it was 2.9:1), nothing is smaller than 11 px, banner reasons are plain readable text, and times are shown in suitable units (216.33 µs rather than 0.000216328 seconds).
- Motion: with "reduce motion" set on the viewer's system there is no radar sweep, easing or blinking, and the fault banner no longer flashes on and off for anyone; it glows slowly.

## [2.2.1] - 2026-11-26

### Security

- The key that pushes the NTS certificate to the Pi as root is locked to one job: `nts-cert-receiver` carries out only "fingerprint", "put fullchain.crt", "put privkey.key" and "restart", checks that each file parses before installing it, and refuses anything else. Previously the key gave a full root shell to whoever held it.
- SSH: passwords are accepted only from the LAN (keys from anywhere), and X11 forwarding is off. The deploy account has passwordless sudo, so a guessed password used to be root.
- The certificate sync checks the Pi's host key strictly instead of trusting it on first use.

## [2.2.0] - 2026-11-26

### Added

- **On GPS percentage**: the History section says how much of the chosen period the server was disciplined by GPS, how many outages there were and the longest, from the recorded history.
- **GPS-lost banner**: while the clock is not on GPS, the page says since when, what it uses instead, and chrony's bound on the error.
- **Expiry warnings**: the health check turns DEGRADED three weeks before the NTS certificate expires and 30 days before the leap-second table does, so the monitor alerts in time. Both dates are on the page.
- **Active network link** in the page header: cable, or Wi-Fi with its signal strength.
- **Client traffic chart**: NTP requests per hour, how many were NTS-authenticated, and how many were dropped.
- **Crystal ageing**: the crystal's daily frequency at a fixed temperature over the last 30 days, with its trend in ppm per month.
- `tools/release_notes.py` prints a version's notes from this changelog for a GitHub or GitLab release.

### Changed

- The page is laid out for phones: banners, the sky view header and the "did you know" strip stack instead of squeezing side by side, and the history charts fit the screen.
- The history file gains three columns for traffic; older files are upgraded when the dashboard starts.

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
