#!/usr/bin/env python3
"""
Chrony GPS Time Server Dashboard - Space-Age Edition

Direct TCP query to gpsd (works regardless of service user).
NASA/JPL-inspired mission control aesthetic.

The page itself lives next to this file in static/ (index.html,
dashboard.css, dashboard.js); this file is the server and the data collectors.

Usage:
    python3 chrony_dashboard.py [--port 8080] [--bind 0.0.0.0]
"""

import csv
import gzip
import subprocess
import json
import re
import argparse
import collections
import contextlib
import signal
import socket
import sqlite3
import os
import time
import hashlib
import ipaddress
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from html import escape as html_escape
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, UTC

# Keep in step with the newest entry in CHANGELOG.md (a test checks it).
__version__ = "2.3.0"

log = logging.getLogger("chrony-dashboard")
log.addHandler(logging.NullHandler())  # main() sets up real logging
_warned = {}


def warn(key, message, every=300.0):
    """Log a problem that may recur on every snapshot: once, then at most
    every `every` seconds per key, so a lasting fault does not flood the log."""
    now = time.monotonic()
    if now - _warned.get(key, float("-inf")) >= every:
        _warned[key] = now
        log.warning(message)


# ============================================================================
# Visitor tracking: lightweight fingerprinting of dashboard webpage viewers.
# (Distinct from NTP clients - this counts browsers loading the HTML page.)
# Fingerprint = SHA256(IP|UserAgent) truncated to 16 chars.
# Current = anyone seen within VISITOR_ACTIVE_S seconds.
# Total unique = every distinct fingerprint ever recorded, persisted to disk.
# ============================================================================
VISITOR_DATA_DIR = "/var/lib/chrony-dashboard"
VISITOR_FILE = os.path.join(VISITOR_DATA_DIR, "visitors.json")


class VisitorLog:
    """Who is looking at the page: how many now, and how many ever.

    The User-Agent is whatever the client sends, so everything it can make
    grow is bounded.
    """

    def __init__(self, path=VISITOR_FILE, active_s=120, max_unique=50000,
                 max_recent=5000, save_interval_s=30):
        self.path = path
        self.active_s = active_s              # "viewing now" means seen this recently
        self.max_unique = max_unique          # stored fingerprints (about 1 MB on disk)
        self.max_recent = max_recent          # fingerprints tracked as viewing now
        self.save_interval_s = save_interval_s  # write the file at most this often
        self.recent = {}                      # fingerprint -> last seen (epoch)
        self.unique = set()                   # every fingerprint ever recorded
        self._lock = threading.Lock()
        self._loaded = False
        self._dirty = False
        self._last_save = 0.0
        self._last_prune = 0.0

    def _load(self):
        try:
            with open(self.path) as f:
                self.unique = set(json.load(f).get("unique", []))
        except FileNotFoundError:
            self.unique = set()  # first run
        except (OSError, ValueError, AttributeError) as e:
            log.warning("visitor file %s unreadable, starting empty: %s", self.path, e)
            self.unique = set()
        self._loaded = True

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"unique": sorted(self.unique)}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            warn("visitors-save", f"cannot save {self.path}: {e}")

    def record(self, ip, ua):
        """Note one page view or API poll from this address and browser."""
        fp = hashlib.sha256(f"{ip}|{ua[:256]}".encode()).hexdigest()[:16]
        now = time.time()
        with self._lock:
            if not self._loaded:
                self._load()
            if now - self._last_prune >= 1.0:
                cutoff = now - self.active_s
                for k in [k for k, t in self.recent.items() if t < cutoff]:
                    del self.recent[k]
                self._last_prune = now
            if fp in self.recent or len(self.recent) < self.max_recent:
                self.recent[fp] = now
            if fp not in self.unique and len(self.unique) < self.max_unique:
                self.unique.add(fp)
                self._dirty = True
            if self._dirty and now - self._last_save >= self.save_interval_s:
                self._save()
                self._dirty = False
                self._last_save = now

    def stats(self):
        """Counts for the page footer: viewing now, and unique visitors ever."""
        cutoff = time.time() - self.active_s
        with self._lock:
            if not self._loaded:
                self._load()
            current = sum(1 for t in self.recent.values() if t >= cutoff)
            return {"current": current, "total_unique": len(self.unique)}


VISITORS = VisitorLog()


def record_visitor(ip, ua):
    """Record a viewer in the process-wide visitor log."""
    VISITORS.record(ip, ua)


def get_visitor_stats():
    """Current and all-time visitor counts."""
    return VISITORS.stats()


# Reverse proxies whose X-Real-IP header is believed (--trusted-proxy).
TRUSTED_PROXIES = set()


def client_ip(peer_ip, headers):
    """The viewer's address: X-Real-IP if a trusted proxy sent it, else the peer."""
    if peer_ip in TRUSTED_PROXIES:
        real = (headers.get("X-Real-IP") or "").strip()
        if real:
            return real[:64]
    return peer_ip


FORWARD_HEADERS = ("X-Real-IP", "X-Forwarded-For", "Forwarded")
# Addresses that can only be on the server's own network. Listed explicitly:
# ipaddress's is_private also covers documentation and other reserved ranges.
LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # private IPv4
    "127.0.0.0/8", "169.254.0.0/16",                    # loopback, link-local
    "::1/128", "fc00::/7", "fe80::/10",                 # IPv6 equivalents
)]


def viewer_is_local(peer_ip, headers):
    """Is this viewer on a private network, and so shown full details?

    A request passed on by a trusted proxy counts by the address that proxy
    reports. Any other request carrying forwarding headers came through a
    proxy we know nothing about, so it is treated as public rather than
    judged by the proxy's own (usually private) address.
    """
    if peer_ip in TRUSTED_PROXIES:
        address = (headers.get("X-Real-IP") or "").strip()
    elif any(headers.get(h) for h in FORWARD_HEADERS):
        return False
    else:
        address = peer_ip
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return any(ip.version == net.version and ip in net for net in LOCAL_NETWORKS)


def mask_address(host):
    """Shorten an address to its network: 203.0.113.x, 2001:db8:1::x."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "hidden"
    if address.version == 4:
        return ".".join(host.split(".")[:3]) + ".x"
    network = ipaddress.ip_network(f"{host}/48", strict=False).network_address
    return str(network).rstrip(":") + "::x"


def redact(data):
    """A copy of a snapshot without what identifies the site or its clients:
    client addresses are shortened, the server's own address and the antenna
    position are left out. Sent to viewers who are not on a private network."""
    out = dict(data, redacted=True)
    out["clients"] = [dict(c, host=mask_address(c.get("host", "")))
                      for c in data.get("clients", [])]
    out["system"] = dict(data.get("system", {}), ip="—")
    out["gps"] = dict(data.get("gps", {}), lat=None, lon=None)
    return out


def run(cmd, timeout=5):
    """Output of a command, or "" if it could not be run.

    A non-zero exit is not treated as a failure: chronyc exits non-zero with
    "501 Not authorised" for reports the current user may not see, and the
    parsers already ignore that text.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        warn("run:" + " ".join(cmd), f"{' '.join(cmd)} failed: {e}")
        return ""


def run_csv(cmd, timeout=5):
    """Run a chronyc command in CSV mode (-c) and return its rows."""
    raw = run(cmd, timeout=timeout)
    return [row for row in csv.reader(raw.splitlines()) if row]


def safe_float(s, default=None):
    """float(s), or `default` if s is not a number."""
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def safe_int(s, default=None):
    """int(s), or `default` if s is not a whole number."""
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


# chronyc reports "never" as an all-ones 32-bit value.
CHRONY_NEVER = 4294967295


def fmt_seconds(x, signed=True):
    """Format seconds the way chronyc does: +12ns, -9623us, -10ms, 4000ms."""
    if x is None:
        return "—"
    value, unit = x, "s"
    for scale, name in ((1e9, "ns"), (1e6, "us"), (1e3, "ms")):
        if abs(x) * scale < 9999.5:
            value, unit = x * scale, name
            break
    return f"{value:+.0f}{unit}" if signed else f"{abs(value):.0f}{unit}"


def fmt_interval(s):
    """Format a whole-second interval the way chronyc does: 352, 120m, 13h, 4d."""
    s = safe_int(s)
    if s is None or s < 0 or s >= CHRONY_NEVER:
        return "-"
    if s < 1200:
        return str(s)
    if s < 36000:
        return f"{s // 60}m"
    if s < 345600:
        return f"{s // 3600}h"
    d = s // 86400
    return f"{d // 365}y" if d > 999 else f"{d}d"


TRACKING_UNKNOWN = {
    "reference_id": "—", "stratum": "—", "ref_time": "—", "system_time": "—",
    "last_offset": "—", "rms_offset": "—", "freq_error": "—", "residual_freq": "—",
    "skew": "—", "root_delay": "—", "root_dispersion": "—", "update_interval": "—",
    "leap_status": "—",
    "ref_time_epoch": None, "system_time_s": None, "last_offset_s": None,
    "rms_offset_s": None, "frequency_ppm": None, "residual_freq_ppm": None,
    "skew_ppm": None, "root_delay_s": None, "root_dispersion_s": None,
    "update_interval_s": None,
}


def parse_tracking():
    """chronyd's synchronisation state, from `chronyc tracking`.

    Each value comes back twice: as a number (seconds or ppm) for anything
    that calculates with it, and worded the way chronyc prints it, which is
    what the page shows.
    """
    # CSV columns: reference id (hex), reference name, stratum, reference time
    # (epoch), system time offset (s, positive = slow), last offset (s), RMS
    # offset (s), frequency (ppm, positive = fast), residual frequency (ppm),
    # skew (ppm), root delay (s), root dispersion (s), update interval (s),
    # leap status.
    rows = run_csv(["chronyc", "-n", "-c", "tracking"])
    if not rows or len(rows[0]) < 14:
        return dict(TRACKING_UNKNOWN)
    row = rows[0]
    numbers = [safe_float(v) for v in row[3:13]]
    if None in numbers:
        return dict(TRACKING_UNKNOWN)
    (ref_time, system_time, last_offset, rms_offset, frequency, residual,
     skew, root_delay, root_dispersion, interval) = numbers
    return {
        "reference_id":    f"{row[0]} ({row[1]})",
        "stratum":         row[2],
        "ref_time":        datetime.fromtimestamp(ref_time, tz=UTC).strftime(
                               "%a %b %d %H:%M:%S %Y"),
        "system_time":     f"{abs(system_time):.9f} seconds "
                           f"{'slow' if system_time > 0 else 'fast'} of NTP time",
        "last_offset":     f"{last_offset:+.9f} seconds",
        "rms_offset":      f"{rms_offset:.9f} seconds",
        "freq_error":      f"{abs(frequency):.3f} ppm {'fast' if frequency > 0 else 'slow'}",
        "residual_freq":   f"{residual:+.3f} ppm",
        "skew":            f"{skew:.3f} ppm",
        "root_delay":      f"{root_delay:.9f} seconds",
        "root_dispersion": f"{root_dispersion:.9f} seconds",
        "update_interval": f"{interval:.1f} seconds",
        "leap_status":     row[13],
        "ref_time_epoch":     ref_time,
        "system_time_s":      system_time,
        "last_offset_s":      last_offset,
        "rms_offset_s":       rms_offset,
        "frequency_ppm":      frequency,
        "residual_freq_ppm":  residual,
        "skew_ppm":           skew,
        "root_delay_s":       root_delay,
        "root_dispersion_s":  root_dispersion,
        "update_interval_s":  interval,
    }


SOURCE_STATES = {
    "*": "synced", "+": "combined", "-": "not combined",
    "?": "unreachable", "x": "false ticker", "~": "too variable",
}
SOURCE_MODES = {"^": "server", "=": "peer", "#": "refclock"}


def parse_selectdata():
    """Map source name -> selection state letter from `chronyc selectdata`.

    selectdata needs an authorised (Unix socket) connection, so this is empty
    unless the dashboard runs as root or _chrony. 'N' means the source has the
    noselect option, as the NMEA refclock does when it only numbers PPS pulses.
    """
    return {row[1]: row[0]
            for row in run_csv(["chronyc", "-n", "-c", "selectdata"])
            if len(row) > 2}


def source_state(state_char, reach, select_state=None):
    """The state name the page shows for one source.

    state_char is chronyc's state symbol, reach its octal reach register,
    and select_state the letter from `chronyc selectdata` when that is
    available.
    """
    if state_char == "?" and reach in ("0", ""):
        # Nothing received in the last 8 polls. Checked first: selectdata still
        # says 'N' for a noselect source that has gone silent.
        return "unreachable"
    if select_state == "N":
        return "noselect"
    if state_char == "?":
        # chrony also shows '?' for sources that answer but fail its tests.
        return "unusable"
    return SOURCE_STATES.get(state_char, state_char)


def parse_sources():
    """chronyd's time sources, one dict each, from `chronyc sources`."""
    # CSV columns: mode, state, name, stratum, poll, reach (octal), last rx (s),
    # adjusted offset (s), measured offset (s), error bound (s).
    sources = []
    selectdata = None
    for row in run_csv(["chronyc", "-n", "-c", "sources"]):
        if len(row) < 10:
            continue
        if selectdata is None:
            selectdata = parse_selectdata()
        mode_char, state_char = row[0], row[1]
        offset = safe_float(row[7])
        measured = safe_float(row[8])
        error = safe_float(row[9])
        sources.append({
            "mode_char":  mode_char,
            "state_char": state_char,
            "state":      source_state(state_char, row[5], selectdata.get(row[2])),
            "mode":       SOURCE_MODES.get(mode_char, "unknown"),
            "name":       row[2],
            "stratum":    row[3],
            "poll":       row[4],
            "reach":      row[5],
            "last_rx":    fmt_interval(row[6]),
            "last_sample_offset": fmt_seconds(offset),
            "measured_offset":    fmt_seconds(measured),
            "margin":     "±" + fmt_seconds(error, signed=False) if error is not None else "—",
            "offset_s":   offset,
            "error_s":    error,
        })
    return sources


def parse_sourcestats():
    """Drift and offset statistics per source, from `chronyc sourcestats`."""
    # CSV columns: name, np, nr, span (s), frequency (ppm), freq skew (ppm),
    # offset (s), std dev (s).
    stats = []
    for row in run_csv(["chronyc", "-n", "-c", "sourcestats"]):
        if len(row) < 8:
            continue
        freq = safe_float(row[4])
        skew = safe_float(row[5])
        stats.append({
            "name":      row[0],
            "np":        row[1],
            "nr":        row[2],
            "span":      fmt_interval(row[3]),
            "freq":      f"{freq:+.3f}" if freq is not None else "—",
            "freq_skew": f"{skew:.3f}" if skew is not None else "—",
            "offset":    fmt_seconds(safe_float(row[6])),
            "std_dev":   fmt_seconds(safe_float(row[7]), signed=False),
        })
    return stats


def parse_clients():
    """The NTP clients chronyd has served, from `chronyc clients`."""
    # CSV columns: host, NTP hits, NTP drops, NTP interval, NTP timeout interval,
    # NTP last (s ago), cmd hits, cmd drops, cmd interval, cmd last (s ago).
    # Needs access to chronyd's Unix socket (root or the chrony user).
    clients = []
    for row in run_csv(["chronyc", "-n", "-c", "clients"]):
        if len(row) < 6 or not re.match(r'^[\d.:a-fA-F]+$', row[0]):
            continue
        clients.append({
            "host":       row[0],
            "ntp":        row[1],
            "drops":      row[2],
            "interval":   row[3],
            "last_query": fmt_interval(row[5]),
        })
    return clients


SERVERSTATS_FIELDS = (
    # Order of `chronyc -c serverstats`. Later chrony versions append more.
    "ntp_packets_received", "ntp_packets_dropped",
    "command_packets_received", "command_packets_dropped",
    "client_log_records",          # client log records dropped
    "nts_ke_connections_accepted", "nts_ke_connections_dropped",
    "authenticated_ntp_packets",
)


def parse_serverstats():
    """Counters of what chronyd has served, from `chronyc serverstats`.

    Needs access to chronyd's Unix socket; without it every counter is "0".
    """
    stats = dict.fromkeys(SERVERSTATS_FIELDS, "0")
    rows = run_csv(["chronyc", "-n", "-c", "serverstats"])
    if rows and len(rows[0]) >= 5 and all(v.isdigit() for v in rows[0]):
        stats.update(zip(SERVERSTATS_FIELDS, rows[0], strict=False))
    return stats


ACTIVITY_LABELS = (
    # Order of `chronyc -c activity`.
    "sources online", "sources offline",
    "sources doing burst (return to online)",
    "sources doing burst (return to offline)",
    "sources with unknown address",
)


def parse_activity():
    """How many NTP sources are in each state, from `chronyc activity`."""
    rows = run_csv(["chronyc", "-n", "-c", "activity"])
    if not rows or len(rows[0]) < len(ACTIVITY_LABELS) or not all(
            v.isdigit() for v in rows[0]):
        return {}
    return dict(zip(ACTIVITY_LABELS, rows[0], strict=False))


REFCLOCKS_LOG = "/var/log/chrony/refclocks.log"


class PpsSampler:
    """The PPS refclock's recent filtered samples, collected from chronyc.

    chrony makes one filtered PPS sample per poll (16 s here), and
    `chronyc sources` reports the latest. Polling that more often than chrony
    polls catches every sample, so the chart needs no refclocks.log, which is
    the biggest source of SD card writes. Only if this has nothing yet does the
    dashboard fall back to reading the log, for setups that keep it.
    """

    def __init__(self, limit=60):
        self.samples = collections.deque(maxlen=limit)
        self._last_time = None

    def poll(self, refid):
        """Add the latest sample if it is new. Returns True if one was added."""
        for row in run_csv(["chronyc", "-n", "-c", "sources"]):
            if len(row) < 10 or row[2] != refid:
                continue
            last_rx, offset = safe_int(row[6]), safe_float(row[7])
            if last_rx is None or last_rx >= CHRONY_NEVER or offset is None:
                return False
            # When the sample was taken, to the second; the same sample seen
            # again on the next poll gives the same time.
            taken = round(time.time() - last_rx)
            if self._last_time is not None and abs(taken - self._last_time) <= 1:
                return False
            self._last_time = taken
            self.samples.append(offset)
            return True
        return False

    def history(self, refid):
        """Recent samples, oldest first; from refclocks.log if none collected yet."""
        return list(self.samples) or parse_pps_history(refid)


PPS_SAMPLER = PpsSampler()


def _pps_poller(interval=4):
    """Collect PPS samples as chrony makes them. Runs as a daemon thread."""
    while True:
        try:
            PPS_SAMPLER.poll(CONFIG["pps_refid"])
        except Exception:  # noqa: BLE001 - the poller must outlive any bug
            log.exception("pps sampler: unexpected error")
        time.sleep(interval)


def _tail_lines(path, max_bytes=64 * 1024):
    """Return the last lines of a file without reading all of it."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is probably cut in half
    return lines


# Tail size that last held enough samples, per log file, so the usual case is
# one read of the right size.
_pps_window = {}


def _pps_samples(lines, refid):
    """Split one refclock's log lines into (filtered, per-pulse) cooked offsets."""
    filtered, per_pulse = [], []
    for line in lines:
        if refid not in line:
            continue
        parts = line.split()
        if len(parts) < 9 or parts[2] != refid:
            continue
        cooked = safe_float(parts[7])
        if cooked is None:
            continue
        (filtered if parts[3] == "-" else per_pulse).append(cooked)
    return filtered, per_pulse


def _recent_samples(path, refid, need, max_bytes):
    """Read the end of one log, growing the tail until it holds `need` samples.

    Returns (filtered, per_pulse, whole_file_was_read).
    """
    window = _pps_window.get(path, 64 * 1024)
    size = os.path.getsize(path)
    while True:
        filtered, per_pulse = _pps_samples(_tail_lines(path, window), refid)
        enough = (len(filtered) >= need
                  or (not filtered and len(per_pulse) >= need))
        if enough or window >= size or window >= max_bytes:
            break
        window *= 4
    _pps_window[path] = window
    return filtered, per_pulse, window >= size


def parse_pps_history(refid="PPS", path=REFCLOCKS_LOG, limit=60,
                      max_bytes=1024 * 1024):
    """Return the most recent filtered offsets (seconds) for one refclock.

    refclocks.log columns (chrony "log refclocks"):
      date time refid DP L P raw-offset cooked-offset dispersion

    chrony writes two kinds of lines. Per-pulse lines have a driver poll
    number in DP; for a PPS refclock locked to NMEA their offsets are only
    the pulse phase (e.g. 6.97e-02), not the clock error. Filtered samples,
    one per poll, have "-" in DP/P/raw and carry the real offset. Use those,
    and fall back to per-pulse cooked offsets only if none are present.

    Filtered samples are sparse (one per poll among ~2 per-pulse lines a
    second), so the tail that is read grows until it holds `limit` of them.
    Just after log rotation the previous log tops the list up.
    """
    try:
        filtered, per_pulse, whole_file = _recent_samples(path, refid, limit, max_bytes)
    except OSError:
        return []
    if len(filtered) < limit and whole_file:
        try:
            older, older_pulse, _ = _recent_samples(
                path + ".1", refid, limit - len(filtered), max_bytes)
            filtered = older + filtered
            per_pulse = older_pulse + per_pulse
        except OSError:
            pass
    return (filtered or per_pulse)[-limit:]


def _read_gpsd(host="127.0.0.1", port=2947, timeout=3.0):
    """Return the latest (TPV, SKY) reports from gpsd.

    Sends ?POLL right after ?WATCH so gpsd answers with its cached fix at once
    instead of us waiting for the next streamed report. The POLL reply is
    gpsd's whole current state, so reading stops there: if it lists no
    satellites, none are in view, and waiting longer would only stall every
    snapshot for the full timeout during a GPS outage. Newer gpsd versions also
    stream SKY reports that carry only DOPs; those are not used for the
    satellite list, but their DOPs are kept.
    """
    tpv = sky = dops = None
    polled = False
    deadline = time.monotonic() + timeout

    def consider(obj):
        nonlocal tpv, sky, dops, polled
        cls = obj.get("class")
        if cls == "TPV" and obj.get("mode") is not None:
            tpv = obj
        elif cls == "SKY":
            if obj.get("satellites"):
                sky = obj
            if any(k in obj for k in ("hdop", "pdop", "vdop")):
                dops = obj
        elif cls == "POLL":
            polled = True
            for t in obj.get("tpv") or []:
                consider(dict(t, **{"class": "TPV"}))
            for s in obj.get("sky") or []:
                consider(dict(s, **{"class": "SKY"}))

    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(b'?WATCH={"enable":true,"json":true};\n?POLL;\n')
        buf = b""
        while time.monotonic() < deadline and not (polled or (tpv and sky)):
            s.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                data = s.recv(8192)
            except TimeoutError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                try:
                    consider(json.loads(line))
                except (json.JSONDecodeError, AttributeError):
                    continue
        with contextlib.suppress(OSError):
            s.sendall(b'?WATCH={"enable":false};\n')
    return tpv, sky, dops


# How long the receiver has held a fix. gpsd does not report this, so it is
# tracked here: get_gpsd_data() updates it on every snapshot, and a monitor
# thread (started in main) polls every FIX_MONITOR_INTERVAL_S so the record is
# continuous even when nobody has the page open.
FIX_MONITOR_INTERVAL_S = 10.0
_fix_lock = threading.Lock()
_fix_state = {
    "seen": False,     # has gpsd been observed at least once?
    "since": None,     # epoch when the current unbroken fix was first observed
    "exact": False,    # False if the fix was already there at the first look
    "last_fix": None,  # epoch of the most recent observation with a fix
}


def _note_fix(tpv):
    """Record one observation of gpsd (tpv=None: gpsd gave nothing)."""
    has_fix = bool(tpv) and tpv.get("mode") in (2, 3)
    now = time.time()
    with _fix_lock:
        first = not _fix_state["seen"]
        _fix_state["seen"] = True
        if has_fix:
            if _fix_state["since"] is None:
                _fix_state["since"] = now
                _fix_state["exact"] = not first
            _fix_state["last_fix"] = now
        else:
            _fix_state["since"] = None
        return {"has_fix": has_fix,
                "fix_since": _fix_state["since"],
                "fix_since_exact": _fix_state["exact"],
                "last_fix": _fix_state["last_fix"]}


def _gps_monitor(interval=FIX_MONITOR_INTERVAL_S):
    while True:
        try:
            _note_fix(_read_gpsd()[0])
        except (OSError, ValueError) as e:
            _note_fix(None)
            warn("gpsd", f"gpsd query failed: {e}")
        except Exception:  # noqa: BLE001 - this thread must outlive any bug
            _note_fix(None)
            log.exception("gps monitor: unexpected error")
        time.sleep(interval)


def get_gpsd_data():
    """Query gpsd directly over TCP. Works regardless of service user."""
    try:
        tpv, sky, dops = _read_gpsd()

        sats_used = 0
        sats_visible = 0
        best_snr = 0
        sat_list = []

        if sky:
            satellites = sky.get("satellites", [])
            sats_visible = len(satellites)
            for sat in satellites:
                used = bool(sat.get("used", False))
                ss = sat.get("ss") or 0
                if used:
                    sats_used += 1
                    best_snr = max(best_snr, ss)
                sat_list.append({
                    "prn": sat.get("PRN", 0),
                    "el": sat.get("el"),
                    "az": sat.get("az"),
                    "ss": ss,
                    "used": used,
                })
        dop_src = dops or sky or {}
        hdop = dop_src.get("hdop")
        pdop = dop_src.get("pdop")
        vdop = dop_src.get("vdop")

        fix_mode_map = {0: "unknown", 1: "no fix", 2: "2D", 3: "3D"}
        fix_mode = "—"
        fix_time = None
        lat = lon = alt = None
        if tpv:
            fix_mode = fix_mode_map.get(tpv.get("mode", 0), "—")
            fix_time = tpv.get("time")
            lat = tpv.get("lat")
            lon = tpv.get("lon")
            alt = next((tpv[k] for k in ("altMSL", "alt", "altHAE")
                        if tpv.get(k) is not None), None)

        return {
            "available": True,
            **_note_fix(tpv),
            "fix_mode": fix_mode,
            "fix_time": fix_time,
            "sats_used": sats_used,
            "sats_visible": sats_visible,
            "best_snr": best_snr,
            "hdop": hdop, "pdop": pdop, "vdop": vdop,
            "lat": lat, "lon": lon, "alt": alt,
            "satellites": sat_list,
        }
    except (OSError, ValueError, TypeError, AttributeError) as e:
        # Unreachable gpsd, or a report shaped differently than expected.
        warn("gpsd", f"gpsd query failed: {e}")
        return {
            "available": False,
            **_note_fix(None),
            "error": str(e),
            "satellites": [],
            "sats_used": 0, "sats_visible": 0, "best_snr": 0,
            "fix_mode": "—",
        }


def _cpu_snapshot():
    cores = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and not line.startswith("cpu "):
                parts = line.split()
                vals = [int(x) for x in parts[1:8]]
                cores[parts[0]] = (vals[3] + vals[4], sum(vals))
    return cores


class CpuSampler:
    """Per-core usage since the previous sample.

    Measuring across the gap between snapshots needs no sleep and averages
    over a couple of seconds instead of a 0.2 s blip. Only the first sample,
    or one after a long idle gap, takes a short measurement of its own.
    """

    def __init__(self, max_window_s=30):
        self.max_window_s = max_window_s
        self._previous = None  # (monotonic time, {cpu: (idle, total)})

    def sample(self):
        """A list of {"core", "usage"}: the percent each core was busy."""
        try:
            now = time.monotonic()
            previous = self._previous
            if previous is None or now - previous[0] > self.max_window_s:
                previous = (now, _cpu_snapshot())
                time.sleep(0.1)
                now = time.monotonic()
            current = _cpu_snapshot()
            self._previous = (now, current)
            result = []
            for cpu_id in sorted(current):
                if cpu_id not in previous[1]:
                    continue
                idle1, total1 = previous[1][cpu_id]
                idle2, total2 = current[cpu_id]
                d_idle = idle2 - idle1
                d_total = total2 - total1
                usage = 100.0 * (1.0 - d_idle / d_total) if d_total > 0 else 0.0
                result.append({"core": cpu_id, "usage": round(usage, 1)})
            return result
        except (OSError, ValueError, IndexError) as e:
            warn("cpu", f"cannot read /proc/stat: {e}")
            return []


CPU_SAMPLER = CpuSampler()


def get_cpu_per_core():
    """Per-core CPU usage since the previous snapshot."""
    return CPU_SAMPLER.sample()


def get_pps_irq_count():
    """How many PPS interrupts the kernel has counted since boot."""
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                if "pps" in line.lower():
                    parts = line.split()
                    counts = [int(p) for p in parts[1:5] if p.isdigit()]
                    return {"total": sum(counts), "per_core": counts,
                            "name": parts[-1] if parts else "pps"}
    except (OSError, ValueError) as e:
        warn("pps-irq", f"cannot read /proc/interrupts: {e}")
    return {"total": 0, "per_core": [], "name": "—"}


def get_isolated_cpus():
    """The CPUs reserved with isolcpus=, as the kernel lists them, or "none"."""
    try:
        with open("/sys/devices/system/cpu/isolated") as f:
            return f.read().strip() or "none"
    except OSError:
        return "—"  # not every kernel exposes this


CHRONY_CONFIGS = ("/etc/chrony/chrony.conf", "/etc/chrony/conf.d")
EXPIRY_WARN_DAYS = {"nts_cert": 21, "leap_table": 30}
_expiry_cache = {"at": float("-inf"), "value": None}


def _days_until(epoch, now=None):
    return int((epoch - (time.time() if now is None else now)) // 86400)


def nts_cert_path(configs=CHRONY_CONFIGS):
    """The certificate chrony serves NTS with (ntsservercert), or None."""
    files = []
    for path in configs:
        if os.path.isdir(path):
            files += sorted(os.path.join(path, n) for n in os.listdir(path)
                            if n.endswith(".conf"))
        else:
            files.append(path)
    cert = None
    for path in files:
        try:
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == "ntsservercert":
                        cert = parts[1]
        except OSError:
            continue
    return cert


def cert_expiry(path):
    """When a PEM certificate expires (epoch seconds), or None if unreadable."""
    out = run(["openssl", "x509", "-noout", "-enddate", "-in", path])
    if not out.startswith("notAfter="):
        warn("nts-cert", f"cannot read the NTS certificate {path}")
        return None
    try:
        when = datetime.strptime(out.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return when.replace(tzinfo=UTC).timestamp()


def get_expiries(max_age_s=3600):
    """Expiry dates that need renewing in time: the NTS certificate and the
    leap-second table. Each is {"expires": "YYYY-MM-DD", "days": n} or None.
    Read at most once an hour."""
    if time.monotonic() - _expiry_cache["at"] < max_age_s:
        return _expiry_cache["value"]
    result = {"nts_cert": None, "leap_table": None}
    cert = nts_cert_path()
    when = cert_expiry(cert) if cert else None
    if when is not None:
        result["nts_cert"] = {"expires": datetime.fromtimestamp(when, tz=UTC).strftime("%Y-%m-%d"),
                              "days": _days_until(when)}
    leap = get_leap_table_expiry()
    if leap:
        when = datetime.strptime(leap, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        result["leap_table"] = {"expires": leap, "days": _days_until(when)}
    _expiry_cache.update(at=time.monotonic(), value=result)
    return result


def get_leap_table_expiry():
    """The date the system's leap-second table expires (YYYY-MM-DD), or None."""
    leap_file = "/usr/share/zoneinfo/leap-seconds.list"
    try:
        if not os.path.exists(leap_file):
            return None
        with open(leap_file) as f:
            content = f.read()
        m = re.search(r'^#@\s+(\d+)', content, re.MULTILINE)
        if m:
            ntp_ts = int(m.group(1))
            unix_ts = ntp_ts - 2208988800
            expiry = datetime.fromtimestamp(unix_ts, tz=UTC)
            return expiry.strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError) as e:
        warn("leap", f"cannot read {leap_file}: {e}")
    return None


def get_uptime():
    """System uptime worded like `uptime -p`, read from /proc (no subprocess)."""
    try:
        with open("/proc/uptime") as f:
            minutes = int(float(f.read().split()[0])) // 60
    except (OSError, ValueError, IndexError) as e:
        warn("uptime", f"cannot read /proc/uptime: {e}")
        return "—"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    parts = [f"{n} {unit}{'' if n == 1 else 's'}"
             for n, unit in ((days, "day"), (hours, "hour"), (mins, "minute")) if n]
    return "up " + (", ".join(parts) or "0 minutes")


def get_network_link(route="/proc/net/route", wireless="/proc/net/wireless",
                     sys_net="/sys/class/net"):
    """The interface carrying the default route: {"iface", "kind", "signal_dbm"}.

    kind is "cable" or "Wi-Fi"; signal_dbm is the Wi-Fi signal, or None. It
    decides what clients get: tens of microseconds over a cable, milliseconds
    over Wi-Fi.
    """
    best = None
    try:
        with open(route) as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) >= 7 and parts[1] == "00000000":   # default route
                    metric = safe_int(parts[6], 0)
                    if best is None or metric < best[1]:
                        best = (parts[0], metric)
    except (OSError, StopIteration) as e:
        warn("route", f"cannot read /proc/net/route: {e}")
    if best is None:
        return {"iface": None, "kind": "none", "signal_dbm": None}
    iface = best[0]
    wifi = os.path.isdir(os.path.join(sys_net, iface, "wireless"))
    signal = None
    if wifi:
        try:
            with open(wireless) as f:
                for line in f:
                    if line.strip().startswith(iface + ":"):
                        signal = safe_float(line.split()[3].rstrip("."))
        except (OSError, IndexError):
            pass
    return {"iface": iface, "kind": "Wi-Fi" if wifi else "cable", "signal_dbm": signal}


def get_system_info():
    """Host facts for the header and the Hardware Vitals panel."""
    hostname = socket.gethostname()
    try:
        # Connecting a UDP socket sends nothing; it only makes the kernel pick
        # the source address it would use for the default route. 192.0.2.1 is
        # a documentation address, used so no real host is named.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            ip = s.getsockname()[0]
    except OSError:
        ip = "—"  # no default route
    uptime_raw = get_uptime()
    temp_str = "—"
    temp_c = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp_c = round(int(f.read().strip()) / 1000, 1)
            temp_str = f"{temp_c}°C"
    except (OSError, ValueError) as e:
        warn("temperature", f"cannot read the CPU temperature: {e}")
    mem_str = "—"
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        total_m = re.search(r'MemTotal:\s+(\d+)', meminfo)
        avail_m = re.search(r'MemAvailable:\s+(\d+)', meminfo)
        if total_m and avail_m:
            total = int(total_m.group(1))
            avail = int(avail_m.group(1))
            used_pct = round(100 * (total - avail) / total, 1)
            mem_str = f"{used_pct}% of {round(total/1024)}MB"
    except (OSError, ValueError, ZeroDivisionError) as e:
        warn("memory", f"cannot read /proc/meminfo: {e}")
    load_str = "—"
    try:
        with open("/proc/loadavg") as f:
            load_str = " ".join(f.read().strip().split()[:3])
    except OSError as e:
        warn("loadavg", f"cannot read /proc/loadavg: {e}")
    return {
        "hostname": hostname,
        "ip": ip,
        "uptime": uptime_raw,
        "temperature": temp_str,
        "temperature_c": temp_c,
        "memory": mem_str,
        "loadavg": load_str,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "epoch": time.time(),
        "isolated_cpus": get_isolated_cpus(),
        "link": get_network_link(),
    }


def compute_health(tracking, sources, gps, expiries=None):
    """Overall status and the reasons for it.

    OK: disciplined by GPS as designed. DEGRADED: still serving time, but
    something is off (no GPS fix, internet time only, a silent source, a
    certificate or leap-second table close to expiring). FAULT: chrony has no
    synchronised source at all.
    """
    issues = []
    status = "OK"
    stratum = tracking.get("stratum", "—")
    try:
        s = int(stratum)
        if s == 0 or s > 3:
            issues.append(f"Stratum {s}")
            status = "DEGRADED"
    except (ValueError, TypeError):
        issues.append("Unknown stratum")
        status = "DEGRADED"
    if gps.get("available"):
        if gps.get("fix_mode") not in ("3D", "2D"):
            issues.append(f"GPS fix: {gps.get('fix_mode')}")
            status = "DEGRADED"
        elif gps.get("sats_used", 0) < 4:
            issues.append(f"Only {gps.get('sats_used')} sats used")
            status = "DEGRADED"
    else:
        issues.append("gpsd unreachable")
        status = "DEGRADED"
    ref = tracking.get("reference_id", "")
    if "GPS" not in ref and "PPS" not in ref:
        issues.append("Not synced to GPS")
        if status == "OK":
            status = "DEGRADED"
    synced = [s for s in sources if s.get("state") == "synced"]
    if not synced:
        issues.append("No synced source")
        status = "FAULT"
    for source in synced:
        # chrony goes on naming a source as its reference after that source
        # falls silent (seen: an hour on a dead PPS), so check that the
        # selected source is still delivering samples.
        if source.get("reach") in ("0", ""):
            issues.append(f"{source.get('name')} is selected but has stopped responding")
            if status == "OK":
                status = "DEGRADED"
    names = {"nts_cert": "NTS certificate", "leap_table": "Leap-second table"}
    for key, item in (expiries or {}).items():
        if item is None or item["days"] >= EXPIRY_WARN_DAYS.get(key, 30):
            continue
        days = item["days"]
        issues.append(f"{names.get(key, key)} expired {-days} days ago" if days < 0
                      else f"{names.get(key, key)} expires in {days} days")
        if status == "OK":
            status = "DEGRADED"
    return {"status": status, "issues": issues}


# The collectors mostly wait on chronyc, gpsd or files, so running them side
# by side makes a snapshot take as long as the slowest one, not the sum.
_COLLECT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="collect")


class SyncTracker:
    """When the server was last disciplined by GPS, for the GPS-lost banner.

    Updated on every snapshot (at least once a minute, by the history
    recorder).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.on_gps = None       # None until the first snapshot
        self.since = None        # when the current state was first seen
        self.since_exact = False
        self.last_on_gps = None

    def note(self, tracking, refid, sources=(), now=None):
        """Record one snapshot's reference; returns the state for the page.

        On GPS means chrony's reference is the PPS refclock and PPS is still
        delivering samples: chrony goes on naming a refclock as its reference
        for a while after its pulses stop.
        """
        now = time.time() if now is None else now
        pps = next((src for src in sources if src.get("name") == refid), None)
        silent = pps is not None and pps.get("reach") in ("0", "")
        on_gps = tracking.get("reference_id", "").endswith(f"({refid})") and not silent
        delay, dispersion = tracking.get("root_delay_s"), tracking.get("root_dispersion_s")
        with self._lock:
            if on_gps != self.on_gps:
                self.since_exact = self.on_gps is not None
                self.on_gps, self.since = on_gps, now
            if on_gps:
                self.last_on_gps = now
            return {
                "on_gps": on_gps,
                "since": self.since,
                "since_exact": self.since_exact,
                "last_on_gps": self.last_on_gps,
                "reference": tracking.get("reference_id"),
                # chrony's bound on the clock's error: everything accumulated
                # from its reference, plus half the round trip to it.
                "error_bound_s": (dispersion + delay / 2
                                  if delay is not None and dispersion is not None else None),
            }


SYNC = SyncTracker()


def get_all_data():
    """Collect one complete snapshot for /api/status."""
    jobs = {
        "tracking":    parse_tracking,
        "sources":     parse_sources,
        "sourcestats": parse_sourcestats,
        "clients":     parse_clients,
        "serverstats": parse_serverstats,
        "activity":    parse_activity,
        "gps":         get_gpsd_data,
        "cpu_cores":   get_cpu_per_core,
        "pps_irq":     get_pps_irq_count,
        "next_leap":   get_leap_table_expiry,
        "pps_history": lambda: PPS_SAMPLER.history(CONFIG["pps_refid"]),
        "system":      get_system_info,
        "expiries":    get_expiries,
    }
    futures = {name: _COLLECT_POOL.submit(fn) for name, fn in jobs.items()}
    data = {name: future.result() for name, future in futures.items()}
    data["sync"] = SYNC.note(data["tracking"], CONFIG["pps_refid"], data["sources"])
    data["health"] = compute_health(data["tracking"], data["sources"], data["gps"],
                                    data["expiries"])
    return data


class StatusCache:
    """Share one data snapshot between all viewers.

    Collecting a snapshot runs several chronyc commands and talks to gpsd.
    Every viewer polls every few seconds; without this, each poll would
    repeat that work. Concurrent requests that arrive while a refresh is
    running wait for it and reuse it.
    """

    def __init__(self, ttl):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data = None
        self._stamp = 0.0

    def get(self):
        """The current snapshot, refreshed if older than ttl, with live visitor counts."""
        with self._lock:
            if self._data is None or time.monotonic() - self._stamp >= self.ttl:
                self._data = get_all_data()
                self._stamp = time.monotonic()
            data = self._data
        # Visitor counts are cheap and change per request, so keep them live.
        return dict(data, visitors=get_visitor_stats())


CONFIG = {"pps_refid": "PPS"}
STATUS_CACHE = StatusCache(ttl=2.0)


# ============================================================================
# History: one sample a minute, kept for HISTORY_DAYS, for the charts.
# ============================================================================
HISTORY_FILE = os.path.join(VISITOR_DATA_DIR, "history.sqlite3")
HISTORY_DAYS = 30
HISTORY_FIELDS = (
    # name, what it is
    ("stratum", "chrony's stratum"),
    ("last_offset_s", "chrony's last clock correction, seconds"),
    ("rms_offset_s", "chrony's RMS offset, seconds"),
    ("frequency_ppm", "clock frequency error chrony corrects, ppm (positive = fast)"),
    ("skew_ppm", "uncertainty of that frequency, ppm"),
    ("pps_offset_s", "last PPS sample's offset, seconds"),
    ("pps_reach", "PPS reach register as a number, 0-255"),
    ("temperature_c", "CPU temperature, degrees C"),
    ("sats_used", "satellites in the GPS fix"),
    ("sats_visible", "satellites the GPS lists"),
    ("best_snr", "strongest signal among satellites in use, dB-Hz"),
    ("fix", "GPS fix: 0 none, 2 2D, 3 3D"),
    ("health", "dashboard health: 0 OK, 1 DEGRADED, 2 FAULT"),
    ("ntp_requests", "NTP requests answered in the minute"),
    ("ntp_dropped", "NTP requests dropped (rate limit, access) in the minute"),
    ("nts_requests", "NTS-authenticated requests in the minute"),
)


class CounterDeltas:
    """Per-sample increases of chrony's cumulative counters.

    The counters restart from zero when chronyd restarts; a counter lower
    than last time is taken as counting from zero again.
    """

    def __init__(self):
        self._last = {}

    def delta(self, name, value):
        """How much `name` grew since the previous call, or None the first time."""
        value = safe_int(value)
        previous, self._last[name] = self._last.get(name), value
        if value is None or previous is None:
            return None
        return value - previous if value >= previous else value


COUNTERS = CounterDeltas()
HEALTH_CODES = {"OK": 0, "DEGRADED": 1, "FAULT": 2}
FIX_CODES = {"2D": 2, "3D": 3}
HISTORY_RANGES = {"6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
HISTORY_POINTS = 360   # buckets per chart: enough detail, small responses


def history_sample(data):
    """One history row (a dict of HISTORY_FIELDS) from a status snapshot."""
    tracking, gps = data.get("tracking", {}), data.get("gps", {})
    stats = data.get("serverstats", {})
    pps = next((src for src in data.get("sources", [])
                if src.get("name") == CONFIG["pps_refid"]), {})
    try:
        reach = int(pps["reach"], 8)  # chronyc prints reach in octal
    except (KeyError, TypeError, ValueError):
        reach = None
    return {
        "stratum": safe_int(tracking.get("stratum")),
        "last_offset_s": tracking.get("last_offset_s"),
        "rms_offset_s": tracking.get("rms_offset_s"),
        "frequency_ppm": tracking.get("frequency_ppm"),
        "skew_ppm": tracking.get("skew_ppm"),
        "pps_offset_s": pps.get("offset_s") if reach else None,
        "pps_reach": reach,
        "temperature_c": data.get("system", {}).get("temperature_c"),
        "sats_used": gps.get("sats_used") if gps.get("available") else None,
        "sats_visible": gps.get("sats_visible") if gps.get("available") else None,
        "best_snr": (gps.get("best_snr") or None) if gps.get("available") else None,
        "fix": FIX_CODES.get(gps.get("fix_mode"), 0) if gps.get("available") else None,
        "health": HEALTH_CODES.get(data.get("health", {}).get("status")),
        "ntp_requests": COUNTERS.delta("ntp", stats.get("ntp_packets_received")),
        "ntp_dropped": COUNTERS.delta("dropped", stats.get("ntp_packets_dropped")),
        "nts_requests": COUNTERS.delta("nts", stats.get("authenticated_ntp_packets")),
    }


class HistoryStore:
    """A minute-by-minute record of the server's state, for the history charts.

    Samples are kept in memory and written to SQLite every `flush_s` seconds,
    so the SD card sees one small write every few minutes instead of one a
    minute. A crash loses at most that much. Rows older than `keep_days` are
    removed once a day.
    """

    def __init__(self, path=HISTORY_FILE, keep_days=HISTORY_DAYS, flush_s=600):
        self.path = path
        self.keep_s = keep_days * 86400
        self.flush_s = flush_s
        self._lock = threading.Lock()
        self._pending = []           # (ts, row) not yet written
        self._last_flush = time.monotonic()
        self._last_prune = 0.0
        self._ready = False

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        if not self._ready:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            columns = ", ".join(f"{name} REAL" for name, _ in HISTORY_FIELDS)
            conn.execute(f"CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, {columns})")
            # A file from an older version lacks the newer fields: add them.
            have = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
            for name, _ in HISTORY_FIELDS:
                if name not in have:
                    conn.execute(f"ALTER TABLE samples ADD COLUMN {name} REAL")
            self._ready = True
        return conn

    def add(self, row, ts=None):
        """Queue one sample; writes to disk when the flush interval has passed."""
        ts = int(ts if ts is not None else time.time())
        with self._lock:
            self._pending.append((ts, row))
            if time.monotonic() - self._last_flush >= self.flush_s:
                self._flush_locked()

    def flush(self):
        """Write queued samples to disk now."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        self._last_flush = time.monotonic()
        if not self._pending:
            return
        names = [name for name, _ in HISTORY_FIELDS]
        try:
            conn = self._connect()
            try:
                with conn:
                    conn.executemany(
                        f"INSERT OR REPLACE INTO samples (ts, {', '.join(names)}) "
                        f"VALUES ({', '.join('?' * (len(names) + 1))})",
                        [(ts, *(row.get(n) for n in names)) for ts, row in self._pending])
                    now = time.time()
                    if now - self._last_prune >= 86400:
                        conn.execute("DELETE FROM samples WHERE ts < ?", (now - self.keep_s,))
                        self._last_prune = now
            finally:
                conn.close()
            self._pending.clear()
        except (sqlite3.Error, OSError) as e:
            # Keep the samples and try again next time; cap memory meanwhile.
            del self._pending[:-1440]
            warn("history", f"cannot write {self.path}: {e}")

    def _rows(self, start, names):
        rows = []
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT ts, {', '.join(names)} FROM samples WHERE ts >= ? ORDER BY ts",
                    (start,)).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as e:
            warn("history-read", f"cannot read {self.path}: {e}")
        with self._lock:
            rows += [(ts, *(row.get(n) for n in names))
                     for ts, row in self._pending if ts >= start]
        return rows

    def summary(self, seconds, now=None):
        """How much of the period the server was on GPS (stratum 1), and its outages.

        Samples are a minute apart; a stretch with no samples (the dashboard
        or the Pi was down) is unknown, so it neither counts as on GPS nor
        joins two outages together.
        """
        now = int(now if now is not None else time.time())
        rows = self._rows(now - seconds, ["stratum"])
        known = [(ts, st) for ts, st in rows if st is not None]
        on = sum(1 for _, st in known if st == 1)
        outages, longest, run_start, last_ts = 0, 0, None, None
        for ts, st in known:
            if run_start is not None and (st == 1 or ts - last_ts > 150):
                outages += 1
                longest = max(longest, last_ts - run_start + 60)
                run_start = None
            if st != 1 and run_start is None:
                run_start = ts
            last_ts = ts
        ongoing = None
        if run_start is not None:
            outages += 1
            longest = max(longest, last_ts - run_start + 60)
            ongoing = run_start
        return {"on_gps_pct": round(100 * on / len(known), 2) if known else None,
                "minutes_recorded": len(known), "outages": outages,
                "longest_outage_s": longest, "ongoing_outage_since": ongoing}

    def ageing(self, days=30, reference_c=40.0, now=None):
        """The crystal's frequency at a fixed temperature, day by day.

        Frequency follows temperature, so a straight line is fitted to
        frequency against temperature over the whole period (only minutes on
        GPS, when chrony's estimate is good) and each day's mean frequency is
        shifted to `reference_c` with it. What remains changes only slowly as
        the crystal ages; the trend is a straight line through the days.
        """
        now = int(now if now is not None else time.time())
        rows = [(ts, f, t) for ts, st, f, t in
                self._rows(now - days * 86400, ["stratum", "frequency_ppm", "temperature_c"])
                if st == 1 and f is not None and t is not None]
        result = {"reference_c": reference_c, "temp_coeff_ppm_per_c": None,
                  "daily": [], "trend_ppm_per_month": None}
        if len(rows) < 60:
            return result
        n = len(rows)
        mean_t = sum(t for _, _, t in rows) / n
        mean_f = sum(f for _, f, _ in rows) / n
        var_t = sum((t - mean_t) ** 2 for _, _, t in rows)
        slope = (sum((t - mean_t) * (f - mean_f) for _, f, t in rows) / var_t
                 if var_t > 0 else 0.0)
        result["temp_coeff_ppm_per_c"] = round(slope, 5)
        by_day = {}
        for ts, f, t in rows:
            by_day.setdefault(ts // 86400, []).append(f - slope * (t - reference_c))
        daily = [(day * 86400, sum(v) / len(v)) for day, v in sorted(by_day.items())
                 if len(v) >= 60]   # at least an hour of data that day
        result["daily"] = [[day, round(v, 4)] for day, v in daily]
        if len(daily) >= 3:
            xs = [d / 86400 for d, _ in daily]
            ys = [v for _, v in daily]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            vx = sum((x - mx) ** 2 for x in xs)
            if vx > 0:
                per_day = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / vx
                result["trend_ppm_per_month"] = round(per_day * 30, 4)
        return result

    def query(self, seconds, points=HISTORY_POINTS, now=None):
        """Averages over `points` equal buckets covering the last `seconds`.

        Returns {"start", "step", "fields": {name: [value or None, ...]}}.
        Health is the worst value in each bucket rather than the average, so a
        short fault still shows.
        """
        now = int(now if now is not None else time.time())
        start = now - seconds
        step = seconds / points
        names = [name for name, _ in HISTORY_FIELDS]
        rows = self._rows(start, names)
        sums = [[0.0] * points for _ in names]
        counts = [[0] * points for _ in names]
        worst = [None] * points
        health_col = names.index("health")
        for ts, *values in rows:
            b = min(points - 1, int((ts - start) / step))
            for i, v in enumerate(values):
                if v is None:
                    continue
                sums[i][b] += v
                counts[i][b] += 1
                if i == health_col:
                    worst[b] = v if worst[b] is None else max(worst[b], v)
        fields = {}
        for i, name in enumerate(names):
            if i == health_col:
                fields[name] = worst
            else:
                fields[name] = [sums[i][b] / counts[i][b] if counts[i][b] else None
                                for b in range(points)]
        return {"start": start, "step": step, "fields": fields}


HISTORY = HistoryStore()
_history_cache = {}   # range name -> (monotonic time, response)


def _history_recorder(interval=60):
    """Add one sample a minute to HISTORY. Runs as a daemon thread."""
    while True:
        try:
            HISTORY.add(history_sample(STATUS_CACHE.get()))
        except Exception:  # noqa: BLE001 - the recorder must outlive any bug
            log.exception("history: could not record a sample")
        time.sleep(interval - time.time() % interval)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; "
        # Scripts only from this server: the page has no inline script.
        "script-src 'self'; "
        # 'unsafe-inline' is for style="" attributes (bar widths and the like).
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


# ============================================================================
# The page: static/index.html plus the stylesheet and script it links to.
# All three are read once at startup and kept in memory, plain and gzipped.
# ============================================================================
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
ASSETS = {}   # URL path -> {"body", "gzip", "type", "version"}
PAGE = {}     # {"body", "gzip"} for index.html


def detect_timezone():
    """The server's IANA time zone name (e.g. America/Los_Angeles), or UTC."""
    try:
        with open("/etc/timezone") as f:
            name = f.read().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def load_page(hostname=None, tz=None, static_dir=STATIC_DIR):
    """Read the page and its assets from disk into ASSETS and PAGE."""
    versions = {}
    for url, (filename, content_type) in STATIC_FILES.items():
        with open(os.path.join(static_dir, filename), "rb") as f:
            body = f.read()
        # The version goes in the page's links, so a changed file gets a new
        # URL and browsers can cache each version indefinitely.
        version = hashlib.sha256(body).hexdigest()[:12]
        ASSETS[url] = {"body": body, "gzip": gzip.compress(body, 9),
                       "type": content_type, "version": version}
        versions[filename] = version
    with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = (html
            .replace("{hostname}", html_escape(hostname or socket.gethostname()))
            .replace("{timezone}", html_escape(tz or detect_timezone(), quote=True))
            .replace("{version}", __version__)
            .replace("{css_version}", versions["dashboard.css"])
            .replace("{js_version}", versions["dashboard.js"]))
    body = html.encode()
    PAGE.update(body=body, gzip=gzip.compress(body, 9))


class Handler(BaseHTTPRequestHandler):
    """Serves the page, its two assets and the JSON API, and nothing else."""
    # Drop idle or slow connections instead of tying up a thread forever.
    timeout = 15
    server_version = "chrony-dashboard"
    sys_version = ""

    def log_message(self, fmt, *args):
        """Drop the default per-request access log."""
        pass

    def _send(self, status, body, content_type, cache="no-store", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data, status=200):
        """Send `data` as a JSON response."""
        body = json.dumps(data, default=str).encode()
        self._send(status, body, "application/json")

    def _send_compressible(self, item, content_type, cache):
        extra = {"Vary": "Accept-Encoding"}
        body = item["body"]
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = item["gzip"]
            extra["Content-Encoding"] = "gzip"
        self._send(200, body, content_type, cache=cache, extra=extra)

    def _record_visitor(self):
        try:
            record_visitor(client_ip(self.client_address[0], self.headers),
                           self.headers.get("User-Agent", "unknown"))
        except Exception:  # noqa: BLE001 - counting visitors must never fail a request
            log.exception("could not record visitor")

    def do_GET(self):
        """Route a request: API, page, asset, or 404."""
        url = urlsplit(self.path)
        path = url.path

        if path == "/api/status":
            # Record viewer activity on API polls (the page polls while open).
            self._record_visitor()
            try:
                data = STATUS_CACHE.get()
                if not viewer_is_local(self.client_address[0], self.headers):
                    data = redact(data)
                self.send_json(data)
            except Exception:  # noqa: BLE001 - answer 500 rather than drop the connection
                log.exception("could not build the status snapshot")
                self.send_json({"error": "internal error"}, status=500)
        elif path == "/api/health":
            # Small endpoint for monitoring and deploy checks. Always 200 with
            # the status in the body; with ?strict=1 the HTTP status itself
            # says it (503 unless OK), for monitors that only look at that.
            try:
                d = STATUS_CACHE.get()
                healthy = d["health"]["status"] == "OK"
                strict = parse_qs(url.query).get("strict", ["0"])[0] not in ("", "0")
                self.send_json({"status": d["health"]["status"],
                                "issues": d["health"]["issues"],
                                "reference_id": d["tracking"]["reference_id"],
                                "version": __version__},
                               status=503 if strict and not healthy else 200)
            except Exception:  # noqa: BLE001 - see above
                log.exception("could not build the health report")
                self.send_json({"status": "ERROR"}, status=500)
        elif path == "/api/time":
            # The server's clock, read as late as possible before answering,
            # for the page to work out how far the viewer's clock is off.
            self.send_json({"now": time.time()})
        elif path == "/api/history":
            name = parse_qs(url.query).get("range", ["24h"])[0]
            if name not in HISTORY_RANGES:
                self.send_json({"error": f"range must be one of {', '.join(HISTORY_RANGES)}"},
                               status=400)
                return
            cached = _history_cache.get(name)
            if cached is None or time.monotonic() - cached[0] >= 60:
                seconds = HISTORY_RANGES[name]
                cached = (time.monotonic(), dict(HISTORY.query(seconds),
                                                 summary=HISTORY.summary(seconds),
                                                 ageing=HISTORY.ageing()))
                _history_cache[name] = cached
            self.send_json(dict(cached[1], range=name))
        elif path in ("/", "/index.html"):
            self._record_visitor()
            self._send_compressible(PAGE, "text/html; charset=utf-8", "no-store")
        elif path in ASSETS:
            asset = ASSETS[path]
            # Only the URL the page links to (with its version) is cacheable
            # for good; anything else must be revalidated.
            current = parse_qs(url.query).get("v", [""])[0] == asset["version"]
            self._send_compressible(
                asset, asset["type"],
                "public, max-age=31536000, immutable" if current else "no-cache")
        else:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self):
        """Answer HEAD like GET; _send leaves the body out."""
        self.do_GET()


class DashboardServer(ThreadingHTTPServer):
    """One thread per request; daemon threads, so shutdown never waits on a client."""
    daemon_threads = True
    allow_reuse_address = True


def _stop_on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main():
    """Parse the command line and serve until interrupted."""
    parser = argparse.ArgumentParser(description="Chrony GPS Dashboard - Space-Age")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--pps-refid", default="PPS",
                        help="refid of the PPS refclock in chrony.conf (default: PPS)")
    parser.add_argument("--cache-ttl", type=float, default=2.0,
                        help="seconds to reuse one data snapshot across viewers (default: 2)")
    parser.add_argument("--trusted-proxy", action="append", default=[], metavar="IP",
                        help="reverse proxy whose X-Real-IP header gives the real "
                             "visitor address (repeatable)")
    parser.add_argument("--state-dir", default=VISITOR_DATA_DIR, metavar="DIR",
                        help="where visitor counts and history are kept "
                             f"(default: {VISITOR_DATA_DIR})")
    parser.add_argument("--timezone", metavar="NAME",
                        help="IANA time zone for the header clock "
                             "(default: this server's, e.g. America/Los_Angeles)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    CONFIG["pps_refid"] = args.pps_refid
    STATUS_CACHE.ttl = args.cache_ttl
    TRUSTED_PROXIES.update(args.trusted_proxy)
    VISITORS.path = os.path.join(args.state_dir, os.path.basename(VISITOR_FILE))
    HISTORY.path = os.path.join(args.state_dir, os.path.basename(HISTORY_FILE))
    load_page(tz=args.timezone)
    threading.Thread(target=_gps_monitor, name="gps-monitor", daemon=True).start()
    threading.Thread(target=_history_recorder, name="history", daemon=True).start()
    threading.Thread(target=_pps_poller, name="pps", daemon=True).start()
    # systemd stops the service with SIGTERM: leave serve_forever the same
    # way as Ctrl-C, so pending history is written below.
    signal.signal(signal.SIGTERM, _stop_on_sigterm)

    server = DashboardServer((args.bind, args.port), Handler)
    log.info("listening on http://%s:%s", args.bind, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        HISTORY.flush()


if __name__ == "__main__":
    main()
