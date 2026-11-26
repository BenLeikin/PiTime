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
import contextlib
import socket
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
        cutoff = time.time() - self.active_s
        with self._lock:
            if not self._loaded:
                self._load()
            current = sum(1 for t in self.recent.values() if t >= cutoff)
            return {"current": current, "total_unique": len(self.unique)}


VISITORS = VisitorLog()


def record_visitor(ip, ua):
    VISITORS.record(ip, ua)


def get_visitor_stats():
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
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def safe_int(s, default=None):
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


def parse_tracking():
    raw = run(["chronyc", "-n", "tracking"])
    data = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            data[key.strip()] = val.strip()
    return {
        "reference_id":    data.get("Reference ID", "—"),
        "stratum":         data.get("Stratum", "—"),
        "ref_time":        data.get("Ref time (UTC)", "—"),
        "system_time":     data.get("System time", "—"),
        "last_offset":     data.get("Last offset", "—"),
        "rms_offset":      data.get("RMS offset", "—"),
        "freq_error":      data.get("Frequency", "—"),
        "residual_freq":   data.get("Residual freq", "—"),
        "skew":            data.get("Skew", "—"),
        "root_delay":      data.get("Root delay", "—"),
        "root_dispersion": data.get("Root dispersion", "—"),
        "update_interval": data.get("Update interval", "—"),
        "leap_status":     data.get("Leap status", "—"),
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


def parse_serverstats():
    raw = run(["chronyc", "-n", "serverstats"])
    data = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            data[key.strip()] = val.strip()
    return {
        "ntp_packets_received": data.get("NTP packets received", "0"),
        "ntp_packets_dropped":  data.get("NTP packets dropped", "0"),
        "command_packets_received": data.get("Command packets received", "0"),
        "command_packets_dropped":  data.get("Command packets dropped", "0"),
        "client_log_records": data.get("Client log records dropped", "0"),
    }


def parse_activity():
    raw = run(["chronyc", "-n", "activity"])
    data = {}
    for line in raw.splitlines():
        m = re.match(r'^(\d+)\s+(.+)', line.strip())
        if m:
            count = m.group(1)
            label = m.group(2).strip().rstrip('.')
            data[label] = count
    return data


REFCLOCKS_LOG = "/var/log/chrony/refclocks.log"


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
    return CPU_SAMPLER.sample()


def get_pps_irq_count():
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
    try:
        with open("/sys/devices/system/cpu/isolated") as f:
            return f.read().strip() or "none"
    except OSError:
        return "—"  # not every kernel exposes this


def get_next_leap_second():
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


def get_system_info():
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
        "memory": mem_str,
        "loadavg": load_str,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "epoch": time.time(),
        "isolated_cpus": get_isolated_cpus(),
    }


def compute_health(tracking, sources, gps):
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
    return {"status": status, "issues": issues}


# The collectors mostly wait on chronyc, gpsd or files, so running them side
# by side makes a snapshot take as long as the slowest one, not the sum.
_COLLECT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="collect")


def get_all_data():
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
        "next_leap":   get_next_leap_second,
        "pps_history": lambda: parse_pps_history(CONFIG["pps_refid"]),
        "system":      get_system_info,
    }
    futures = {name: _COLLECT_POOL.submit(fn) for name, fn in jobs.items()}
    data = {name: future.result() for name, future in futures.items()}
    data["health"] = compute_health(data["tracking"], data["sources"], data["gps"])
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
        with self._lock:
            if self._data is None or time.monotonic() - self._stamp >= self.ttl:
                self._data = get_all_data()
                self._stamp = time.monotonic()
            data = self._data
        # Visitor counts are cheap and change per request, so keep them live.
        return dict(data, visitors=get_visitor_stats())


CONFIG = {"pps_refid": "PPS"}
STATUS_CACHE = StatusCache(ttl=2.0)

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
            .replace("{css_version}", versions["dashboard.css"])
            .replace("{js_version}", versions["dashboard.js"]))
    body = html.encode()
    PAGE.update(body=body, gzip=gzip.compress(body, 9))


class Handler(BaseHTTPRequestHandler):
    # Drop idle or slow connections instead of tying up a thread forever.
    timeout = 15
    server_version = "chrony-dashboard"
    sys_version = ""

    def log_message(self, fmt, *args):
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
                                "reference_id": d["tracking"]["reference_id"]},
                               status=503 if strict and not healthy else 200)
            except Exception:  # noqa: BLE001 - see above
                log.exception("could not build the health report")
                self.send_json({"status": "ERROR"}, status=500)
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
        self.do_GET()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Chrony GPS Dashboard - Space-Age")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--pps-refid", default="PPS",
                        help="refid of the PPS refclock in chrony.conf (default: PPS)")
    parser.add_argument("--cache-ttl", type=float, default=2.0,
                        help="seconds to reuse one data snapshot across viewers (default: 2)")
    parser.add_argument("--trusted-proxy", action="append", default=[], metavar="IP",
                        help="reverse proxy whose X-Real-IP header gives the real "
                             "visitor address (repeatable)")
    parser.add_argument("--timezone", metavar="NAME",
                        help="IANA time zone for the header clock "
                             "(default: this server's, e.g. America/Los_Angeles)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    CONFIG["pps_refid"] = args.pps_refid
    STATUS_CACHE.ttl = args.cache_ttl
    TRUSTED_PROXIES.update(args.trusted_proxy)
    load_page(tz=args.timezone)
    threading.Thread(target=_gps_monitor, name="gps-monitor", daemon=True).start()

    server = DashboardServer((args.bind, args.port), Handler)
    log.info("listening on http://%s:%s", args.bind, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
