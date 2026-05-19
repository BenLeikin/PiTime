#!/usr/bin/env python3
"""
Chrony GPS Time Server Dashboard - Space-Age Edition

Direct TCP query to gpsd (works regardless of service user).
NASA/JPL-inspired mission control aesthetic.

Usage:
    python3 chrony_dashboard.py [--port 8080] [--bind 0.0.0.0]
"""

import subprocess
import json
import re
import argparse
import socket
import os
import time
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone


# ============================================================================
# Visitor tracking: lightweight fingerprinting of dashboard webpage viewers.
# (Distinct from NTP clients - this counts browsers loading the HTML page.)
# Fingerprint = SHA256(IP|UserAgent) truncated to 16 chars.
# Current = anyone seen within VISITOR_ACTIVE_S seconds.
# Total unique = every distinct fingerprint ever recorded, persisted to disk.
# ============================================================================
VISITOR_DATA_DIR = "/var/lib/chrony-dashboard"
VISITOR_FILE = os.path.join(VISITOR_DATA_DIR, "visitors.json")
VISITOR_ACTIVE_S = 120
_visitor_lock = threading.Lock()
_visitor_recent = {}  # fingerprint -> last_seen_epoch
_visitor_unique = set()  # all-time set of fingerprints
_visitor_loaded = False


def _load_visitors():
    global _visitor_unique, _visitor_loaded
    try:
        with open(VISITOR_FILE) as f:
            data = json.load(f)
            _visitor_unique = set(data.get("unique", []))
    except Exception:
        _visitor_unique = set()
    _visitor_loaded = True


def _save_visitors():
    try:
        os.makedirs(VISITOR_DATA_DIR, exist_ok=True)
        tmp = VISITOR_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"unique": sorted(_visitor_unique)}, f)
        os.replace(tmp, VISITOR_FILE)
    except Exception:
        pass


def record_visitor(ip, ua):
    fp = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:16]
    now = time.time()
    with _visitor_lock:
        if not _visitor_loaded:
            _load_visitors()
        _visitor_recent[fp] = now
        # Prune anything inactive from the recent map
        cutoff = now - VISITOR_ACTIVE_S
        stale = [k for k, t in _visitor_recent.items() if t < cutoff]
        for k in stale:
            _visitor_recent.pop(k, None)
        is_new = fp not in _visitor_unique
        if is_new:
            _visitor_unique.add(fp)
            _save_visitors()


def get_visitor_stats():
    now = time.time()
    cutoff = now - VISITOR_ACTIVE_S
    with _visitor_lock:
        if not _visitor_loaded:
            _load_visitors()
        current = sum(1 for t in _visitor_recent.values() if t >= cutoff)
        total = len(_visitor_unique)
    return {"current": current, "total_unique": total}


def run(cmd, timeout=5):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


def safe_float(s, default=None):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


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


def parse_sources():
    raw = run(["chronyc", "-n", "sources", "-v"])
    sources = []
    for line in raw.splitlines():
        m = re.match(
            r'^([\^=#])([\*\+\-\?xX~])\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)',
            line.strip()
        )
        if m:
            mode_char = m.group(1)
            state_char = m.group(2)
            state_map = {
                "*": "synced", "+": "combined", "-": "not combined",
                "?": "unreachable", "x": "false ticker", "~": "too variable",
            }
            mode_map = {"^": "server", "=": "peer", "#": "refclock"}
            sources.append({
                "mode_char": mode_char,
                "state_char": state_char,
                "state":      state_map.get(state_char, state_char),
                "mode":       mode_map.get(mode_char, "unknown"),
                "name":       m.group(3),
                "stratum":    m.group(4),
                "poll":       m.group(5),
                "reach":      m.group(6),
                "last_rx":    m.group(7),
                "last_sample_offset": m.group(9),
                "margin":     m.group(10),
            })
    return sources


def parse_sourcestats():
    raw = run(["chronyc", "-n", "sourcestats"])
    stats = []
    for line in raw.splitlines():
        m = re.match(
            r'^(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)',
            line.strip()
        )
        if m and not m.group(1).startswith("=") and m.group(1) != "Name/IP":
            stats.append({
                "name":      m.group(1),
                "np":        m.group(2),
                "nr":        m.group(3),
                "span":      m.group(4),
                "freq":      m.group(5),
                "freq_skew": m.group(6),
                "offset":    m.group(7),
                "std_dev":   m.group(8),
            })
    return stats


def parse_clients():
    raw = run(["chronyc", "-n", "clients"])
    clients = []
    for line in raw.splitlines():
        if "Hostname" in line or "===" in line or not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 6 and re.match(r'^[\d.:a-fA-F]+$', parts[0]):
            clients.append({
                "host": parts[0],
                "ntp": parts[1] if len(parts) > 1 else "0",
                "drops": parts[2] if len(parts) > 2 else "0",
                "interval": parts[3] if len(parts) > 3 else "—",
                "last_query": parts[-1] if len(parts) > 5 else "—",
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


def parse_pps_history():
    """Read refclock samples from chrony refclocks.log, fall back to measurements.log."""
    candidates = [
        "/var/log/chrony/refclocks.log",
        "/var/log/chrony/measurements.log",
    ]
    for log_path in candidates:
        if not os.path.exists(log_path):
            continue
        try:
            result = subprocess.run(
                ["tail", "-n", "200", log_path],
                capture_output=True, text=True, timeout=3
            )
            samples = []
            for line in result.stdout.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                # refclocks.log format: date time refid ... offset ...
                # We want the offset column (typically column 6 or 7)
                parts = line.split()
                if len(parts) < 6:
                    continue
                # Try to find a small floating-point offset value
                for p in parts[3:]:
                    f = safe_float(p)
                    if f is not None and abs(f) < 1.0 and abs(f) > 1e-12:
                        samples.append(f)
                        break
            if samples:
                return samples[-60:]
        except Exception:
            continue
    return []


def get_gpsd_data():
    """Query gpsd directly over TCP. Works regardless of service user."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(("127.0.0.1", 2947))
        s.sendall(b'?WATCH={"enable":true,"json":true};\n')

        tpv = None
        sky = None
        deadline = time.time() + 3.0
        buf = b''
        while time.time() < deadline:
            try:
                data = s.recv(4096)
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    line, _, buf = buf.partition(b'\n')
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        cls = obj.get("class")
                        if cls == "TPV":
                            tpv = obj
                        elif cls == "SKY":
                            sky = obj
                        if tpv and sky:
                            break
                    except json.JSONDecodeError:
                        continue
                if tpv and sky:
                    break
            except socket.timeout:
                break

        try:
            s.sendall(b'?WATCH={"enable":false};\n')
        except Exception:
            pass
        s.close()

        sats_used = 0
        sats_visible = 0
        best_snr = 0
        sat_list = []
        hdop = pdop = vdop = None

        if sky:
            satellites = sky.get("satellites", [])
            sats_visible = len(satellites)
            for sat in satellites:
                used = sat.get("used", False)
                if used:
                    sats_used += 1
                ss = sat.get("ss", 0)
                if used and ss > best_snr:
                    best_snr = ss
                sat_list.append({
                    "prn": sat.get("PRN", 0),
                    "el": sat.get("el", 0),
                    "az": sat.get("az", 0),
                    "ss": ss,
                    "used": used,
                })
            hdop = sky.get("hdop")
            pdop = sky.get("pdop")
            vdop = sky.get("vdop")

        fix_mode_map = {0: "unknown", 1: "no fix", 2: "2D", 3: "3D"}
        fix_mode = "—"
        fix_time = None
        lat = lon = alt = None
        if tpv:
            fix_mode = fix_mode_map.get(tpv.get("mode", 0), "—")
            fix_time = tpv.get("time")
            lat = tpv.get("lat")
            lon = tpv.get("lon")
            alt = tpv.get("alt") or tpv.get("altMSL") or tpv.get("altHAE")

        return {
            "available": True,
            "fix_mode": fix_mode,
            "fix_time": fix_time,
            "sats_used": sats_used,
            "sats_visible": sats_visible,
            "best_snr": best_snr,
            "hdop": hdop, "pdop": pdop, "vdop": vdop,
            "lat": lat, "lon": lon, "alt": alt,
            "satellites": sat_list,
        }
    except Exception as e:
        return {
            "available": False,
            "error": str(e),
            "satellites": [],
            "sats_used": 0, "sats_visible": 0, "best_snr": 0,
            "fix_mode": "—",
        }


def get_cpu_per_core():
    try:
        def snapshot():
            cores = {}
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("cpu") and not line.startswith("cpu "):
                        parts = line.split()
                        cpu_id = parts[0]
                        vals = [int(x) for x in parts[1:8]]
                        idle = vals[3] + vals[4]
                        total = sum(vals)
                        cores[cpu_id] = (idle, total)
            return cores
        s1 = snapshot()
        time.sleep(0.2)
        s2 = snapshot()
        result = []
        for cpu_id in sorted(s1.keys()):
            idle1, total1 = s1[cpu_id]
            idle2, total2 = s2[cpu_id]
            d_idle = idle2 - idle1
            d_total = total2 - total1
            usage = 100.0 * (1.0 - d_idle / d_total) if d_total > 0 else 0.0
            result.append({"core": cpu_id, "usage": round(usage, 1)})
        return result
    except Exception:
        return []


def get_pps_irq_count():
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                if "pps" in line.lower():
                    parts = line.split()
                    counts = [int(p) for p in parts[1:5] if p.isdigit()]
                    return {"total": sum(counts), "per_core": counts,
                            "name": parts[-1] if parts else "pps"}
    except Exception:
        pass
    return {"total": 0, "per_core": [], "name": "—"}


def get_isolated_cpus():
    try:
        with open("/sys/devices/system/cpu/isolated") as f:
            return f.read().strip() or "none"
    except Exception:
        return "—"


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
            expiry = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
            return expiry.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


def get_system_info():
    hostname = socket.gethostname()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "—"
    uptime_raw = run(["uptime", "-p"])
    temp_str = "—"
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp_c = round(int(f.read().strip()) / 1000, 1)
            temp_str = f"{temp_c}°C"
    except Exception:
        pass
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
    except Exception:
        pass
    load_str = "—"
    try:
        with open("/proc/loadavg") as f:
            load_str = " ".join(f.read().strip().split()[:3])
    except Exception:
        pass
    return {
        "hostname": hostname,
        "ip": ip,
        "uptime": uptime_raw,
        "temperature": temp_str,
        "memory": mem_str,
        "loadavg": load_str,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
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
    has_synced = any(s.get("state") == "synced" for s in sources)
    if not has_synced:
        issues.append("No synced source")
        status = "FAULT"
    return {"status": status, "issues": issues}


def get_all_data():
    tracking = parse_tracking()
    sources = parse_sources()
    sourcestats = parse_sourcestats()
    clients = parse_clients()
    serverstats = parse_serverstats()
    activity = parse_activity()
    gps = get_gpsd_data()
    cpu_cores = get_cpu_per_core()
    pps_irq = get_pps_irq_count()
    next_leap = get_next_leap_second()
    pps_history = parse_pps_history()
    system = get_system_info()
    health = compute_health(tracking, sources, gps)
    return {
        "tracking":    tracking,
        "sources":     sources,
        "sourcestats": sourcestats,
        "clients":     clients,
        "serverstats": serverstats,
        "activity":    activity,
        "gps":         gps,
        "cpu_cores":   cpu_cores,
        "pps_irq":     pps_irq,
        "next_leap":   next_leap,
        "pps_history": pps_history,
        "system":      system,
        "health":      health,
        "visitors":    get_visitor_stats(),
    }


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Time Server</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-0: #03060a;
    --bg-1: #050a12;
    --bg-2: #0a121e;
    --surface: #0a1422;
    --surface-2: #0f1c30;
    --border: #1a2c47;
    --border-glow: #2d4a78;
    --cyan: #00d4ff;
    --cyan-dim: #007090;
    --amber: #ffaa00;
    --amber-dim: #8a5a00;
    --green: #00ff88;
    --green-dim: #00aa55;
    --red: #ff3355;
    --yellow: #ffd700;
    --magenta: #ff00aa;
    --blue: #4488ff;
    --muted: #4a6080;
    --text: #c0d0e0;
    --heading: #e8f0ff;
    --display: 'Orbitron', 'Arial', sans-serif;
    --mono: 'Space Mono', 'Courier New', monospace;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html, body { background: var(--bg-0); }

  body {
    color: var(--text);
    font-family: var(--mono);
    min-height: 100vh;
    overflow-x: hidden;
    position: relative;
  }

  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
      radial-gradient(ellipse at 20% 30%, rgba(0,212,255,0.08) 0%, transparent 50%),
      radial-gradient(ellipse at 80% 70%, rgba(255,170,0,0.06) 0%, transparent 50%),
      radial-gradient(ellipse at 50% 100%, rgba(255,0,170,0.04) 0%, transparent 50%),
      radial-gradient(1px 1px at 12% 18%, rgba(255,255,255,0.6) 0%, transparent 100%),
      radial-gradient(1px 1px at 78% 12%, rgba(0,212,255,0.5) 0%, transparent 100%),
      radial-gradient(2px 2px at 35% 65%, rgba(255,170,0,0.4) 0%, transparent 100%),
      radial-gradient(1px 1px at 88% 55%, rgba(255,255,255,0.5) 0%, transparent 100%),
      radial-gradient(1px 1px at 5% 80%, rgba(0,255,136,0.3) 0%, transparent 100%),
      radial-gradient(1px 1px at 62% 38%, rgba(200,210,255,0.4) 0%, transparent 100%),
      radial-gradient(1px 1px at 25% 92%, rgba(255,255,255,0.4) 0%, transparent 100%),
      radial-gradient(1px 1px at 95% 88%, rgba(0,212,255,0.4) 0%, transparent 100%),
      var(--bg-0);
    pointer-events: none;
    z-index: 0;
  }

  body::after {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg, transparent 0, transparent 2px,
      rgba(0,212,255,0.02) 2px, rgba(0,212,255,0.02) 3px
    );
    pointer-events: none;
    z-index: 1;
    mix-blend-mode: overlay;
  }

  .wrap {
    position: relative;
    z-index: 2;
    max-width: 1400px;
    margin: 0 auto;
    padding: 1rem 1.25rem 4rem;
  }

  header {
    border: 1px solid var(--border);
    background: linear-gradient(135deg, var(--surface) 0%, var(--bg-1) 100%);
    padding: 1rem 1.5rem;
    margin-bottom: 1rem;
    position: relative;
    overflow: hidden;
    display: grid;
    grid-template-columns: auto auto 1fr auto;
    gap: 1.5rem;
    align-items: center;
    clip-path: polygon(12px 0, 100% 0, 100% calc(100% - 12px), calc(100% - 12px) 100%, 0 100%, 0 12px);
  }
  header::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--cyan), transparent);
  }
  header::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--amber), transparent);
  }

  .mission-patch {
    width: 70px; height: 70px;
    border: 2px solid var(--cyan);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    background: radial-gradient(circle, var(--bg-2) 0%, var(--bg-0) 100%);
    position: relative;
    flex-shrink: 0;
    box-shadow: 0 0 20px rgba(0,212,255,0.3), inset 0 0 12px rgba(0,212,255,0.15);
  }
  .mission-patch::before {
    content: '';
    position: absolute;
    inset: 4px;
    border: 1px dashed var(--cyan-dim);
    border-radius: 50%;
    animation: rotate 30s linear infinite;
  }
  .mission-patch svg { width: 38px; height: 38px; }

  @keyframes rotate {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  .header-text {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
  }
  .header-text .designation {
    font-family: var(--display);
    font-size: 0.7rem;
    font-weight: 500;
    letter-spacing: 0.4em;
    color: var(--cyan);
    text-transform: uppercase;
  }
  .header-text h1 {
    font-family: var(--display);
    font-size: clamp(1.5rem, 3vw, 2.2rem);
    font-weight: 900;
    color: var(--heading);
    line-height: 1;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    text-shadow: 0 0 30px rgba(0,212,255,0.4);
  }
  .header-text h1 .accent { color: var(--amber); }
  .header-text .subtitle {
    font-size: 0.7rem;
    color: var(--muted);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-top: 0.2rem;
  }

  .local-time-block {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-self: center;
    padding: 0.4rem 1.4rem;
    border-left: 1px solid var(--border);
    border-right: 1px solid var(--border);
    min-width: 0;
  }
  .local-time-block .lt-label {
    font-family: var(--display);
    font-size: 0.6rem;
    letter-spacing: 0.3em;
    color: var(--cyan);
    text-transform: uppercase;
    margin-bottom: 0.25rem;
  }
  .local-time-block .lt-value {
    font-family: var(--mono);
    font-size: clamp(1.1rem, 2.2vw, 1.6rem);
    font-weight: 700;
    color: var(--heading);
    line-height: 1;
    letter-spacing: 0.04em;
    text-shadow: 0 0 16px rgba(0,212,255,0.35);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .local-time-block .lt-ms {
    color: var(--amber);
    font-size: 0.75em;
    font-weight: 500;
  }
  .local-time-block .lt-date {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    color: var(--muted);
    text-transform: uppercase;
    margin-top: 0.3rem;
  }
  @media (max-width: 900px) {
    /* On mobile, stack the header vertically: patch+title on top, then
       the Pacific clock, then the meta block. Trying to fit 4 columns
       side-by-side at phone widths makes everything overlap. */
    header {
      grid-template-columns: auto 1fr;
      grid-template-areas:
        "patch  title"
        "clock  clock"
        "meta   meta";
      gap: 1rem;
      padding: 0.9rem 1rem;
    }
    .mission-patch { grid-area: patch; width: 56px; height: 56px; }
    .mission-patch svg { width: 30px; height: 30px; }
    .header-text { grid-area: title; }
    .header-text h1 { font-size: 1.4rem; }
    .header-text .designation { font-size: 0.6rem; letter-spacing: 0.25em; }
    .header-text .subtitle { font-size: 0.6rem; letter-spacing: 0.12em; }
    .local-time-block {
      grid-area: clock;
      border-left: none;
      border-right: none;
      border-top: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
      padding: 0.6rem 0;
      justify-self: stretch;
      align-items: center;
    }
    .header-meta {
      grid-area: meta;
      grid-template-columns: max-content 1fr;
      justify-content: start;
      gap: 0.3rem 1rem;
      width: 100%;
    }
    .header-meta dt { text-align: left; }
  }
  @media (max-width: 480px) {
    .header-text h1 { font-size: 1.2rem; }
    .local-time-block .lt-value { font-size: 1.3rem; }
  }

  /* Tooltip system - hover any element with [data-tip] to see definition */
  [data-tip] {
    position: relative;
    cursor: help;
  }
  [data-tip]::after {
    content: '?';
    display: inline-block;
    margin-left: 0.35em;
    width: 0.95em;
    height: 0.95em;
    line-height: 0.85em;
    text-align: center;
    font-size: 0.65em;
    font-weight: 700;
    font-family: var(--display);
    color: var(--cyan);
    background: transparent;
    border: 1px solid var(--cyan-dim);
    border-radius: 50%;
    vertical-align: 0.05em;
    opacity: 0.55;
    transition: opacity 0.15s, color 0.15s, border-color 0.15s;
  }
  [data-tip]:hover::after {
    opacity: 1;
    color: var(--amber);
    border-color: var(--amber);
  }
  .tip-pop {
    position: fixed;
    z-index: 9999;
    max-width: 340px;
    padding: 0.7rem 0.9rem;
    background: var(--bg-0);
    border: 1px solid var(--cyan);
    border-radius: 2px;
    box-shadow: 0 4px 24px rgba(0,0,0,0.6), 0 0 16px rgba(0,212,255,0.25);
    font-family: var(--body);
    font-size: 0.78rem;
    line-height: 1.5;
    color: var(--text);
    letter-spacing: 0.02em;
    text-transform: none;
    pointer-events: none;
    opacity: 0;
    transform: translateY(4px);
    transition: opacity 0.12s ease-out, transform 0.12s ease-out;
    clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px);
  }
  .tip-pop.visible {
    opacity: 1;
    transform: translateY(0);
  }
  .tip-pop .tip-title {
    display: block;
    font-family: var(--display);
    font-size: 0.65rem;
    letter-spacing: 0.25em;
    color: var(--amber);
    text-transform: uppercase;
    margin-bottom: 0.35rem;
  }

  .header-meta {
    display: grid;
    grid-template-columns: auto auto;
    gap: 0.25rem 1rem;
    font-size: 0.7rem;
    align-items: center;
  }
  .header-meta dt {
    color: var(--muted);
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-size: 0.6rem;
    text-align: right;
  }
  .header-meta dd {
    color: var(--text);
    font-family: var(--mono);
    font-weight: 700;
  }

  .tx-indicator {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0.5rem;
    border: 1px solid var(--green-dim);
    border-radius: 2px;
    font-size: 0.6rem;
    letter-spacing: 0.2em;
    color: var(--green);
    text-transform: uppercase;
    font-family: var(--display);
    font-weight: 700;
  }
  .tx-indicator::before {
    content: '';
    width: 6px; height: 6px;
    background: var(--green);
    border-radius: 50%;
    box-shadow: 0 0 8px var(--green);
    animation: blink 1.2s ease-in-out infinite;
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1rem 1.5rem;
    background: var(--surface);
    border: 1px solid var(--border);
    margin-bottom: 1rem;
    font-family: var(--display);
    font-weight: 700;
    letter-spacing: 0.25em;
    text-transform: uppercase;
    font-size: 0.95rem;
    position: relative;
    overflow: hidden;
    clip-path: polygon(8px 0, 100% 0, 100% calc(100% - 8px), calc(100% - 8px) 100%, 0 100%, 0 8px);
  }
  .banner::before {
    content: '';
    position: absolute;
    top: 0; bottom: 0; left: 0;
    width: 6px;
  }
  .banner.ok { color: var(--green); }
  .banner.ok::before { background: var(--green); box-shadow: 0 0 16px var(--green); }
  .banner.degraded { color: var(--yellow); }
  .banner.degraded::before { background: var(--yellow); box-shadow: 0 0 16px var(--yellow); }
  .banner.fault { color: var(--red); animation: fault-flash 1s steps(2) infinite; }
  .banner.fault::before { background: var(--red); box-shadow: 0 0 16px var(--red); }
  @keyframes fault-flash {
    0%   { background: var(--surface); }
    50%  { background: rgba(255,51,85,0.1); }
  }
  .banner .issues {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: none;
    letter-spacing: 0.05em;
    font-weight: normal;
  }

  .skyview-card {
    background: radial-gradient(ellipse at 50% 50%, var(--bg-2) 0%, var(--bg-0) 100%);
    border: 1px solid var(--border);
    margin-bottom: 1rem;
    position: relative;
    overflow: hidden;
    clip-path: polygon(12px 0, 100% 0, 100% calc(100% - 12px), calc(100% - 12px) 100%, 0 100%, 0 12px);
  }
  .skyview-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--cyan) 30%, var(--cyan) 70%, transparent);
  }
  .skyview-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 0.7rem 1.2rem 0.5rem;
    border-bottom: 1px solid var(--border);
    background: rgba(10,20,34,0.6);
  }
  .skyview-header .title {
    font-family: var(--display);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: var(--cyan);
  }
  .skyview-header .legend {
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.05em;
  }
  .skyview-header .legend .dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    margin: 0 4px 0 12px;
    vertical-align: middle;
  }
  .skyview-header .legend .dot.used { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .skyview-header .legend .dot.visible { background: var(--muted); }

  #skyview-canvas {
    width: 100%;
    height: 320px;
    display: block;
  }

  .section-heading {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin: 1.5rem 0 0.7rem;
    font-family: var(--display);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.35em;
    text-transform: uppercase;
    color: var(--cyan);
  }
  .section-heading::before, .section-heading::after {
    content: '';
    flex: 1;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--border-glow), transparent);
  }
  .section-heading .marker { color: var(--amber); }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem; }
  .grid-3 { display: grid; grid-template-columns: repeat(3,1fr); gap: 1rem; margin-bottom: 1rem; }
  .grid-4 { display: grid; grid-template-columns: repeat(4,1fr); gap: 1rem; margin-bottom: 1rem; }
  .full   { margin-bottom: 1rem; }

  @media (max-width: 900px) {
    .grid-3, .grid-4 { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 600px) {
    .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 1rem 1.2rem 1.1rem;
    position: relative;
    overflow: hidden;
    clip-path: polygon(8px 0, 100% 0, 100% calc(100% - 8px), calc(100% - 8px) 100%, 0 100%, 0 8px);
    transition: border-color 0.2s;
  }
  .card:hover { border-color: var(--border-glow); }
  .card::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 30%;
    height: 1px;
    background: var(--cyan);
    box-shadow: 0 0 8px var(--cyan);
  }
  .card::after {
    content: '';
    position: absolute;
    bottom: 0; right: 0;
    width: 30%;
    height: 1px;
    background: var(--amber);
    box-shadow: 0 0 8px var(--amber);
  }

  .card-title {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-family: var(--display);
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: var(--cyan);
    margin-bottom: 0.9rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px dashed var(--border);
  }
  .card-title::before {
    content: '\25C6';
    color: var(--amber);
    font-size: 0.6rem;
  }

  .stat-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 0.3rem 0;
    border-bottom: 1px solid rgba(45,74,120,0.15);
    gap: 1rem;
  }
  .stat-row:last-child { border-bottom: none; }
  .stat-key { color: var(--muted); font-size: 0.72rem; white-space: nowrap; letter-spacing: 0.05em; }
  .stat-val { color: var(--text); font-size: 0.8rem; text-align: right; font-weight: 700; }
  .stat-val.accent { color: var(--cyan); text-shadow: 0 0 10px rgba(0,212,255,0.3); }
  .stat-val.amber  { color: var(--amber); text-shadow: 0 0 10px rgba(255,170,0,0.3); }
  .stat-val.good   { color: var(--green); text-shadow: 0 0 10px rgba(0,255,136,0.3); }
  .stat-val.bad    { color: var(--red);   text-shadow: 0 0 10px rgba(255,51,85,0.3); }
  .stat-val.warn   { color: var(--yellow);}

  .hero-stat { text-align: center; padding: 0.5rem 0; }
  .hero-stat .val {
    font-family: var(--display);
    font-size: clamp(1.6rem, 3.8vw, 2.4rem);
    font-weight: 900;
    color: var(--amber);
    line-height: 1;
    letter-spacing: 0.02em;
    text-shadow: 0 0 20px rgba(255,170,0,0.4);
  }
  .hero-stat .lbl {
    font-family: var(--display);
    font-size: 0.6rem;
    color: var(--muted);
    letter-spacing: 0.25em;
    text-transform: uppercase;
    margin-top: 0.4rem;
  }
  .hero-stat .sub {
    font-size: 0.7rem;
    color: var(--cyan-dim);
    margin-top: 0.15rem;
  }

  .sparkline { width: 100%; height: 80px; margin-top: 0.5rem; }

  .core-bar {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    margin: 0.4rem 0;
    font-size: 0.72rem;
    font-family: var(--mono);
  }
  .core-bar .label {
    width: 60px;
    color: var(--muted);
    letter-spacing: 0.05em;
  }
  .core-bar .label.isolated {
    color: var(--magenta);
    font-weight: 700;
  }
  .core-bar .track {
    flex: 1;
    height: 10px;
    background: var(--bg-2);
    border: 1px solid var(--border);
    overflow: hidden;
    position: relative;
  }
  .core-bar .track::before {
    content: '';
    position: absolute;
    inset: 0;
    background: repeating-linear-gradient(
      90deg, transparent 0, transparent 9px,
      rgba(45,74,120,0.3) 9px, rgba(45,74,120,0.3) 10px
    );
  }
  .core-bar .fill {
    height: 100%;
    background: linear-gradient(90deg, var(--green), var(--amber));
    transition: width 0.4s;
    position: relative;
    z-index: 1;
  }
  .core-bar .fill.isolated {
    background: linear-gradient(90deg, var(--magenta), var(--cyan));
  }
  .core-bar .pct {
    width: 48px;
    text-align: right;
    color: var(--text);
    font-variant-numeric: tabular-nums;
    font-weight: 700;
  }

  .world-clock {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 0.7rem;
    margin-top: 0.3rem;
  }
  .tz-cell {
    background: var(--surface-2);
    border: 1px solid var(--border);
    padding: 0.6rem 0.7rem;
    text-align: center;
    position: relative;
    clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px);
  }
  .tz-cell::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 1px;
    background: var(--cyan-dim);
  }
  .tz-cell .city {
    font-family: var(--display);
    font-size: 0.6rem;
    color: var(--muted);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    font-weight: 500;
  }
  .tz-cell .time {
    font-family: var(--display);
    font-size: 1.25rem;
    color: var(--cyan);
    font-weight: 700;
    margin-top: 0.25rem;
    font-variant-numeric: tabular-nums;
    text-shadow: 0 0 12px rgba(0,212,255,0.4);
  }
  .tz-cell .date {
    font-size: 0.62rem;
    color: var(--cyan-dim);
    margin-top: 0.15rem;
    font-family: var(--mono);
  }

  .tbl-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78rem; font-family: var(--mono); }
  thead th {
    text-align: left;
    padding: 0.5rem 0.7rem;
    font-family: var(--display);
    font-size: 0.6rem;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--cyan);
    border-bottom: 1px solid var(--border-glow);
    background: var(--surface-2);
  }
  tbody tr {
    border-bottom: 1px solid rgba(45,74,120,0.15);
    transition: background 0.15s;
  }
  tbody tr:hover { background: rgba(0,212,255,0.04); }
  tbody td { padding: 0.5rem 0.7rem; color: var(--text); }

  .state-badge {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    font-family: var(--display);
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: 1px solid;
  }
  .state-synced       { color: var(--green);  border-color: var(--green-dim);  background: rgba(0,255,136,0.08); }
  .state-combined     { color: var(--amber);  border-color: var(--amber-dim);  background: rgba(255,170,0,0.08); }
  .state-not-combined { color: var(--blue);   border-color: var(--cyan-dim);   background: rgba(68,136,255,0.08); }
  .state-unreachable, .state-false-ticker, .state-too-variable {
    color: var(--red); border-color: var(--red); background: rgba(255,51,85,0.08);
  }

  .reach-bits { display: inline-flex; gap: 2px; vertical-align: middle; }
  .reach-bit { width: 6px; height: 14px; }
  .reach-bit.on  { background: var(--green); box-shadow: 0 0 4px var(--green); }
  .reach-bit.off { background: var(--bg-2); border: 1px solid var(--border); }

  .fun-fact {
    background: linear-gradient(90deg, transparent, rgba(0,212,255,0.04) 20%, rgba(0,212,255,0.04) 80%, transparent);
    border-left: 2px solid var(--cyan-dim);
    border-right: 2px solid var(--amber-dim);
    padding: 0.55rem 1.2rem;
    margin-bottom: 1rem;
    font-family: var(--mono);
    font-size: 0.78rem;
    color: var(--text);
    letter-spacing: 0.02em;
    display: flex;
    align-items: center;
    gap: 0.8rem;
    min-height: 2.4rem;
  }
  .fun-fact .label {
    font-family: var(--display);
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.25em;
    color: var(--amber);
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .fun-fact .text {
    color: var(--text);
  }
  .fun-fact .text .hl {
    color: var(--cyan);
    font-weight: 700;
    text-shadow: 0 0 8px rgba(0,212,255,0.4);
  }
  .fun-fact .text .hl-amber {
    color: var(--amber);
    font-weight: 700;
    text-shadow: 0 0 8px rgba(255,170,0,0.4);
  }

  #refresh-bar {
    height: 2px;
    background: var(--bg-2);
    border: 1px solid var(--border);
    margin-bottom: 1rem;
    overflow: hidden;
    position: relative;
  }
  #refresh-progress {
    height: 100%;
    background: linear-gradient(90deg, var(--cyan-dim), var(--cyan), var(--amber));
    width: 0%;
    transition: width linear;
    box-shadow: 0 0 10px var(--cyan);
  }

  footer {
    text-align: center;
    margin-top: 3rem;
    font-family: var(--display);
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.3em;
    text-transform: uppercase;
  }

  #loading {
    text-align: center;
    padding: 4rem;
    color: var(--cyan);
    font-family: var(--display);
    font-size: 1.1rem;
    letter-spacing: 0.4em;
    text-transform: uppercase;
    animation: blink 1.5s ease-in-out infinite;
  }
  #dashboard { display: none; }

  .mission-patch svg path,
  .mission-patch svg circle,
  .mission-patch svg line { stroke: var(--cyan); fill: none; stroke-width: 1.5; }
  .mission-patch svg .filled { fill: var(--amber); stroke: none; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="mission-patch">
      <svg viewBox="0 0 40 40">
        <circle cx="20" cy="20" r="16"/>
        <circle cx="20" cy="20" r="3" class="filled"/>
        <line x1="20" y1="4" x2="20" y2="36"/>
        <line x1="4" y1="20" x2="36" y2="20"/>
        <path d="M 20 8 Q 32 20 20 32"/>
        <path d="M 20 8 Q 8 20 20 32"/>
      </svg>
    </div>
    <div class="header-text">
      <div class="designation">Stratum 1 Time Authority</div>
      <h1 id="h-hostname">{hostname}<span class="accent">.</span></h1>
      <div class="subtitle">GPS-Disciplined NTP Reference</div>
    </div>
    <div class="local-time-block">
      <div class="lt-label">Pacific <span id="lt-zone">PT</span></div>
      <div class="lt-value">
        <span id="lt-hms">--:--:--</span><span class="lt-ms" id="lt-ms">.000</span>
      </div>
      <div class="lt-date" id="lt-date">&mdash;</div>
    </div>
    <dl class="header-meta">
      <dt>Node</dt><dd id="h-ip">—</dd>
      <dt>Uptime</dt><dd id="h-uptime">—</dd>
      <dt>Core Temp</dt><dd id="h-temp">—</dd>
      <dt>Mission Time</dt><dd id="h-ts" style="font-size: 0.65rem">—</dd>
      <dt>Status</dt><dd><span class="tx-indicator">TRANSMITTING</span></dd>
    </dl>
  </header>

  <div id="refresh-bar"><div id="refresh-progress"></div></div>

  <div id="loading">&#9658; ESTABLISHING TELEMETRY LINK</div>

  <div id="dashboard">

    <div class="banner ok" id="status-banner">
      <span id="status-text">CHECKING...</span>
      <span class="issues" id="status-issues"></span>
    </div>

    <div class="fun-fact" id="fun-fact">
      <span class="label">DID YOU KNOW</span>
      <span class="text" id="fun-fact-text">Computing telemetry...</span>
    </div>

    <div class="section-heading"><span class="marker">&#9658;</span><span>Orbital Tracking</span></div>

    <div class="grid-2">
      <div class="skyview-card" style="margin-bottom:0">
        <div class="skyview-header">
          <span class="title">&#9658; GPS Constellation View</span>
          <span class="legend">
            <span class="dot used"></span>Locked
            <span class="dot visible"></span>Visible
            <span id="skyview-meta" style="margin-left:1.2rem; color: var(--amber);">—</span>
          </span>
        </div>
        <canvas id="skyview-canvas"></canvas>
      </div>
      <div class="card">
        <div class="card-title">GPS Subsystem</div>
        <div class="stat-row"><span class="stat-key">Fix Mode</span><span class="stat-val accent" id="g-fix">—</span></div>
        <div class="stat-row"><span class="stat-key">Satellites Locked / Visible</span><span class="stat-val" id="g-sats">—</span></div>
        <div class="stat-row"><span class="stat-key">Best SNR (dB-Hz)</span><span class="stat-val" id="g-snr">—</span></div>
        <div class="stat-row"><span class="stat-key">HDOP</span><span class="stat-val" id="g-hdop">—</span></div>
        <div class="stat-row"><span class="stat-key">PDOP</span><span class="stat-val" id="g-pdop">—</span></div>
        <div class="stat-row"><span class="stat-key">Position</span><span class="stat-val" id="g-pos">—</span></div>
        <div class="stat-row"><span class="stat-key">Altitude</span><span class="stat-val" id="g-alt">—</span></div>
        <div class="stat-row"><span class="stat-key">Time Since Lock</span><span class="stat-val good" id="g-fix-age">—</span></div>
      </div>
    </div>

    <div class="section-heading"><span class="marker">&#9658;</span><span>Primary Telemetry</span></div>

    <div class="grid-3">
      <div class="card">
        <div class="card-title">System Offset</div>
        <div class="hero-stat">
          <div class="val" id="h-sys-offset">—</div>
          <div class="lbl">Last Offset</div>
          <div class="sub" id="h-rms-offset">RMS —</div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Stratum Level</div>
        <div class="hero-stat">
          <div class="val" id="h-stratum">—</div>
          <div class="lbl">NTP Stratum</div>
          <div class="sub" id="h-leap">Leap —</div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Frequency Drift</div>
        <div class="hero-stat">
          <div class="val" id="h-freq">—</div>
          <div class="lbl">Clock Offset</div>
          <div class="sub" id="h-skew">Skew —</div>
        </div>
      </div>
    </div>

    <div class="card full">
        <div class="card-title">PPS Discipline (recent samples)</div>
        <svg class="sparkline" id="pps-spark" preserveAspectRatio="none"></svg>
        <div class="stat-row" style="margin-top:0.7rem"><span class="stat-key">Min</span><span class="stat-val good" id="pps-min">—</span></div>
        <div class="stat-row"><span class="stat-key">Max</span><span class="stat-val warn" id="pps-max">—</span></div>
        <div class="stat-row"><span class="stat-key">Range</span><span class="stat-val accent" id="pps-range">—</span></div>
        <div class="stat-row"><span class="stat-key">Sample count</span><span class="stat-val" id="pps-count">—</span></div>
        <div class="stat-row"><span class="stat-key">PPS IRQ Total</span><span class="stat-val amber" id="pps-irq">—</span></div>
      </div>

    <div class="section-heading"><span class="marker">&#9658;</span><span>System Telemetry</span></div>

    <div class="grid-2">
      <div class="card">
        <div class="card-title">Tracking Detail</div>
        <div class="stat-row"><span class="stat-key">Reference ID</span><span class="stat-val accent" id="t-refid">—</span></div>
        <div class="stat-row"><span class="stat-key">Ref Time (UTC)</span><span class="stat-val" id="t-reftime">—</span></div>
        <div class="stat-row"><span class="stat-key">Update Interval</span><span class="stat-val" id="t-interval">—</span></div>
        <div class="stat-row"><span class="stat-key">Root Delay</span><span class="stat-val good" id="t-rootdelay">—</span></div>
        <div class="stat-row"><span class="stat-key">Root Dispersion</span><span class="stat-val" id="t-rootdisp">—</span></div>
        <div class="stat-row"><span class="stat-key">Residual Freq</span><span class="stat-val" id="t-residfreq">—</span></div>
        <div class="stat-row"><span class="stat-key">Activity</span><span class="stat-val" id="t-activity">—</span></div>
      </div>

      <div class="card">
        <div class="card-title">Hardware Vitals</div>
        <div class="stat-row"><span class="stat-key">Hostname</span><span class="stat-val accent" id="s-host">—</span></div>
        <div class="stat-row"><span class="stat-key">IP Address</span><span class="stat-val" id="s-ip">—</span></div>
        <div class="stat-row"><span class="stat-key">Core Temperature</span><span class="stat-val" id="s-temp">—</span></div>
        <div class="stat-row"><span class="stat-key">Memory</span><span class="stat-val" id="s-mem">—</span></div>
        <div class="stat-row"><span class="stat-key">Load Average</span><span class="stat-val" id="s-load">—</span></div>
        <div class="stat-row"><span class="stat-key">System Uptime</span><span class="stat-val" id="s-uptime">—</span></div>
        <div class="stat-row"><span class="stat-key">Isolated Cores</span><span class="stat-val amber" id="s-isolated">—</span></div>
        <div class="stat-row"><span class="stat-key">Leap Data Expiry</span><span class="stat-val" id="s-leap">—</span></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-title">CPU Core Utilization</div>
        <div id="cpu-cores">—</div>
      </div>
      <div class="card">
        <div class="card-title">NTP Service Statistics</div>
        <div class="stat-row"><span class="stat-key">NTP Packets Received</span><span class="stat-val good" id="ss-rx">—</span></div>
        <div class="stat-row"><span class="stat-key">NTP Packets Dropped</span><span class="stat-val" id="ss-drop">—</span></div>
        <div class="stat-row"><span class="stat-key">Command Packets Received</span><span class="stat-val" id="ss-cmd-rx">—</span></div>
        <div class="stat-row"><span class="stat-key">Command Packets Dropped</span><span class="stat-val" id="ss-cmd-drop">—</span></div>
        <div class="stat-row"><span class="stat-key">Client Log Records Dropped</span><span class="stat-val" id="ss-clog">—</span></div>
      </div>
    </div>

    <div class="section-heading"><span class="marker">&#9658;</span><span>Global Reference</span></div>

    <div class="card full">
      <div class="card-title">Authoritative Time (Disseminated From This Node)</div>
      <div class="world-clock" id="world-clock">—</div>
    </div>

    <div class="section-heading"><span class="marker">&#9658;</span><span>Source Network</span></div>

    <div class="card full">
      <div class="card-title">Time Sources</div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th>State</th><th>Source</th><th>Stratum</th><th>Poll</th>
            <th>Reach</th><th>Last Rx</th><th>Offset</th><th>Margin</th>
          </tr></thead>
          <tbody id="sources-body"><tr><td colspan="8" style="color:var(--muted);padding:1rem">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="card full">
      <div class="card-title">Source Statistics</div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th>Source</th><th>Samples (NP/NR)</th><th>Span</th>
            <th>Freq Skew</th><th>Std Dev</th><th>Offset</th>
          </tr></thead>
          <tbody id="stats-body"><tr><td colspan="6" style="color:var(--muted);padding:1rem">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="card full">
      <div class="card-title">Connected Clients</div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Client</th><th>NTP Requests</th><th>Drops</th><th>Last Query</th></tr></thead>
          <tbody id="clients-body"><tr><td colspan="4" style="color:var(--muted);padding:1rem">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

  </div>

  <footer>
    &#9670; GPS-Disciplined Stratum 1 &#9670; NTPv4 Compliant &#9670; Refreshing every 5 seconds &#9670;
    <div id="visitor-stats" style="margin-top:0.6rem;font-size:0.55rem;color:var(--muted);letter-spacing:0.2em;opacity:0.6">
      <span id="visitor-current">&mdash;</span> viewing now &#9670; <span id="visitor-total">&mdash;</span> unique all-time
    </div>
  </footer>

</div>

<script>
const REFRESH_S = 5;
let lastFixTime = null;
let lastFixServerTimeMs = null;

function reachBits(octal) {
  let dec = parseInt(octal, 8);
  if (isNaN(dec)) return octal;
  let html = '<span class="reach-bits">';
  for (let i = 7; i >= 0; i--) {
    html += `<span class="reach-bit ${(dec >> i) & 1 ? 'on' : 'off'}"></span>`;
  }
  return html + '</span>';
}

function stateClass(state) { return 'state-' + (state || '').replace(/ /g, '-'); }

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = (val === undefined || val === null || val === '') ? '\u2014' : val;
}

function colorizeOffset(val, id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  const m = val && val.match(/([-\d.]+)\s*(ns|us|ms|s|seconds)?/);
  if (m) {
    const n = Math.abs(parseFloat(m[1]));
    const unit = m[2];
    let ns = n;
    if (unit === 'us') ns = n * 1000;
    else if (unit === 'ms') ns = n * 1e6;
    else if (unit === 's' || unit === 'seconds') ns = n * 1e9;
    el.className = 'stat-val ' + (ns < 10000 ? 'good' : ns < 1e6 ? 'accent' : 'bad');
  }
}

function formatDuration(seconds) {
  if (seconds == null || seconds < 0) return '\u2014';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatSecondsAsHuman(s) {
  if (s == null) return '\u2014';
  const abs = Math.abs(s);
  if (abs < 1e-6) return (s * 1e9).toFixed(1) + ' ns';
  if (abs < 1e-3) return (s * 1e6).toFixed(2) + ' \u00b5s';
  if (abs < 1)    return (s * 1e3).toFixed(3) + ' ms';
  return s.toFixed(3) + ' s';
}

const canvas = document.getElementById('skyview-canvas');
const ctx = canvas.getContext('2d');
let satsCurrent = [];
let satsAnimated = [];
let radarAngle = 0;

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', resizeCanvas);

function projectSat(el, az, w, h) {
  const cx = w / 2;
  const cy = h / 2;
  const r = Math.min(w, h) / 2 - 28;
  const elRad = (el || 0) * Math.PI / 180;
  const azRad = (az || 0) * Math.PI / 180;
  const dist = r * (1 - Math.sin(elRad));
  const x = cx + dist * Math.sin(azRad);
  const y = cy - dist * Math.cos(azRad);
  return { x, y };
}

function drawSky() {
  resizeCanvas();
  const rect = canvas.getBoundingClientRect();
  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  const cx = w / 2;
  const cy = h / 2;
  const r = Math.min(w, h) / 2 - 28;

  if (r <= 0) return;

  const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
  grad.addColorStop(0, 'rgba(0,212,255,0.04)');
  grad.addColorStop(1, 'rgba(0,212,255,0)');
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, 2 * Math.PI);
  ctx.fill();

  if (typeof ctx.createConicGradient === 'function') {
    const sweepGrad = ctx.createConicGradient(radarAngle, cx, cy);
    sweepGrad.addColorStop(0, 'rgba(0,212,255,0.18)');
    sweepGrad.addColorStop(0.1, 'rgba(0,212,255,0.0)');
    sweepGrad.addColorStop(1, 'rgba(0,212,255,0.0)');
    ctx.fillStyle = sweepGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    ctx.fill();
  }
  radarAngle += 0.012;

  ctx.strokeStyle = 'rgba(45,74,120,0.4)';
  ctx.lineWidth = 1;
  for (const elev of [0, 30, 60]) {
    const ringR = r * (1 - elev / 90);
    ctx.beginPath();
    ctx.arc(cx, cy, ringR, 0, 2 * Math.PI);
    ctx.stroke();
  }

  ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.moveTo(cx - r, cy); ctx.lineTo(cx + r, cy);
  ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = '#00d4ff';
  ctx.font = "bold 11px 'Orbitron', sans-serif";
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('N', cx, cy - r - 12);
  ctx.fillText('S', cx, cy + r + 12);
  ctx.fillText('E', cx + r + 12, cy);
  ctx.fillText('W', cx - r - 12, cy);

  ctx.fillStyle = '#4a6080';
  ctx.font = "9px 'Space Mono', monospace";
  ctx.fillText('30\u00b0', cx + 4, cy - r * 0.667 + 2);
  ctx.fillText('60\u00b0', cx + 4, cy - r * 0.333 + 2);

  for (const sat of satsAnimated) {
    if (sat.el == null || sat.az == null) continue;
    const { x, y } = projectSat(sat.el, sat.az, w, h);
    const used = sat.used;
    const ss = sat.ss || 0;

    if (used) {
      ctx.strokeStyle = 'rgba(0,255,136,0.4)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(x, y, 11, 0, 2 * Math.PI);
      ctx.stroke();

      ctx.shadowBlur = 14;
      ctx.shadowColor = '#00ff88';
      ctx.fillStyle = '#00ff88';
      ctx.beginPath();
      ctx.arc(x, y, 4 + Math.min(ss / 12, 4), 0, 2 * Math.PI);
      ctx.fill();
      ctx.shadowBlur = 0;

      ctx.strokeStyle = 'rgba(0,255,136,0.8)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x - 14, y); ctx.lineTo(x - 9, y);
      ctx.moveTo(x + 9, y);  ctx.lineTo(x + 14, y);
      ctx.moveTo(x, y - 14); ctx.lineTo(x, y - 9);
      ctx.moveTo(x, y + 9);  ctx.lineTo(x, y + 14);
      ctx.stroke();
    } else {
      ctx.fillStyle = ss > 25 ? 'rgba(192,208,224,0.7)' : 'rgba(74,96,128,0.7)';
      ctx.beginPath();
      ctx.arc(x, y, 2.5, 0, 2 * Math.PI);
      ctx.fill();
    }

    ctx.fillStyle = used ? '#00ff88' : 'rgba(74,96,128,0.8)';
    ctx.font = "bold 10px 'Space Mono', monospace";
    ctx.fillText(sat.prn, x, y + (used ? 22 : 12));
  }

  ctx.strokeStyle = '#ffaa00';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, 6, 0, 2 * Math.PI);
  ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy);
  ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10);
  ctx.stroke();
  ctx.fillStyle = '#ffaa00';
  ctx.beginPath();
  ctx.arc(cx, cy, 1.5, 0, 2 * Math.PI);
  ctx.fill();
}

function animateSats() {
  for (let i = 0; i < satsAnimated.length; i++) {
    const sat = satsAnimated[i];
    const target = satsCurrent.find(s => s.prn === sat.prn);
    if (target) {
      const dEl = target.el - sat.el;
      const dAz = target.az - sat.az;
      sat.el += dEl * 0.06;
      sat.az += dAz * 0.06;
      sat.ss = target.ss;
      sat.used = target.used;
    }
  }
  drawSky();
  requestAnimationFrame(animateSats);
}

function updateSats(newSats) {
  for (const ns of newSats) {
    const existing = satsAnimated.find(s => s.prn === ns.prn);
    if (!existing) {
      satsAnimated.push({...ns});
    }
  }
  satsAnimated = satsAnimated.filter(s => newSats.find(n => n.prn === s.prn));
  satsCurrent = newSats;
}

requestAnimationFrame(animateSats);

function drawSparkline(values) {
  const svg = document.getElementById('pps-spark');
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (!values || values.length === 0) {
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', '50%');
    text.setAttribute('y', '50%');
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('fill', '#4a6080');
    text.setAttribute('font-family', 'Space Mono');
    text.setAttribute('font-size', '10');
    text.textContent = 'NO DATA - enable: log measurements in chrony.conf';
    svg.appendChild(text);
    return;
  }
  const rect = svg.getBoundingClientRect();
  const w = rect.width;
  const h = rect.height;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const padding = 6;
  const usableH = h - 2 * padding;

  for (let i = 1; i < 4; i++) {
    const y = padding + (usableH * i / 4);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', 0);
    line.setAttribute('x2', w);
    line.setAttribute('y1', y);
    line.setAttribute('y2', y);
    line.setAttribute('stroke', 'rgba(45,74,120,0.3)');
    line.setAttribute('stroke-dasharray', '2,3');
    svg.appendChild(line);
  }

  if (min < 0 && max > 0) {
    const zeroY = padding + usableH * (max / range);
    const zeroLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    zeroLine.setAttribute('x1', 0);
    zeroLine.setAttribute('x2', w);
    zeroLine.setAttribute('y1', zeroY);
    zeroLine.setAttribute('y2', zeroY);
    zeroLine.setAttribute('stroke', '#ffaa00');
    zeroLine.setAttribute('stroke-dasharray', '4,3');
    zeroLine.setAttribute('opacity', '0.4');
    svg.appendChild(zeroLine);
  }

  const stepX = w / (values.length - 1 || 1);
  let pathStr = `M 0 ${h}`;
  values.forEach((v, i) => {
    const x = i * stepX;
    const y = padding + usableH * (1 - (v - min) / range);
    pathStr += ` L ${x} ${y}`;
  });
  pathStr += ` L ${w} ${h} Z`;

  const fillGrad = `<defs><linearGradient id="ppsGrad" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0%" stop-color="#00d4ff" stop-opacity="0.3"/>
    <stop offset="100%" stop-color="#00d4ff" stop-opacity="0"/>
  </linearGradient></defs>`;
  svg.insertAdjacentHTML('afterbegin', fillGrad);

  const fill = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  fill.setAttribute('d', pathStr);
  fill.setAttribute('fill', 'url(#ppsGrad)');
  svg.appendChild(fill);

  let d = '';
  values.forEach((v, i) => {
    const x = i * stepX;
    const y = padding + usableH * (1 - (v - min) / range);
    d += (i === 0 ? 'M' : 'L') + x + ',' + y + ' ';
  });
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', d);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', '#00d4ff');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('filter', 'drop-shadow(0 0 4px rgba(0,212,255,0.6))');
  svg.appendChild(path);

  const lastX = (values.length - 1) * stepX;
  const lastY = padding + usableH * (1 - (values[values.length - 1] - min) / range);
  const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot.setAttribute('cx', lastX);
  dot.setAttribute('cy', lastY);
  dot.setAttribute('r', '3.5');
  dot.setAttribute('fill', '#00d4ff');
  dot.setAttribute('filter', 'drop-shadow(0 0 6px #00d4ff)');
  svg.appendChild(dot);
}

const TIMEZONES = [
  { city: 'Los Angeles', tz: 'America/Los_Angeles' },
  { city: 'New York',    tz: 'America/New_York' },
  { city: 'London',      tz: 'Europe/London' },
  { city: 'Berlin',      tz: 'Europe/Berlin' },
  { city: 'Tokyo',       tz: 'Asia/Tokyo' },
  { city: 'Sydney',      tz: 'Australia/Sydney' },
  { city: 'UTC',         tz: 'UTC' },
];

function updateWorldClock() {
  const wc = document.getElementById('world-clock');
  const now = new Date();
  wc.innerHTML = TIMEZONES.map(t => {
    const time = now.toLocaleTimeString('en-US', { timeZone: t.tz, hour12: false });
    const date = now.toLocaleDateString('en-US', { timeZone: t.tz, month: 'short', day: 'numeric' });
    return `<div class="tz-cell">
      <div class="city">${t.city}</div>
      <div class="time">${time}</div>
      <div class="date">${date}</div>
    </div>`;
  }).join('');
}
setInterval(updateWorldClock, 1000);

// Pacific time header clock - sub-second precision, smooth update via rAF.
// Browser Date.now() is millisecond-resolution at best (timer precision is
// intentionally reduced by browsers), so 3 decimal places is the honest ceiling.
const _ltHms = document.getElementById('lt-hms');
const _ltMs = document.getElementById('lt-ms');
const _ltDate = document.getElementById('lt-date');
const _ltZone = document.getElementById('lt-zone');
const _ltHmsFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles',
  hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
});
const _ltDateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles',
  weekday: 'short', month: 'short', day: '2-digit', year: 'numeric'
});
const _ltZoneFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles', timeZoneName: 'short'
});
function _ltGetAbbrev(d) {
  const parts = _ltZoneFmt.formatToParts(d);
  const p = parts.find(x => x.type === 'timeZoneName');
  return p ? p.value : 'PT';
}
function updatePacificClock() {
  const now = new Date();
  // toLocaleTimeString with fractionalSecondDigits respects the timezone correctly,
  // and we render hh:mm:ss separately from ms to keep the ms in its own color.
  const hms = _ltHmsFmt.format(now).replace(/^24/, '00'); // normalize midnight
  const ms = String(now.getMilliseconds()).padStart(3, '0');
  if (_ltHms.textContent !== hms) _ltHms.textContent = hms;
  _ltMs.textContent = '.' + ms;
  // Date and zone change infrequently. Update them once a second is plenty.
  if (!window._ltLastSec || window._ltLastSec !== hms) {
    window._ltLastSec = hms;
    _ltDate.textContent = _ltDateFmt.format(now);
    _ltZone.textContent = _ltGetAbbrev(now);
  }
  requestAnimationFrame(updatePacificClock);
}
requestAnimationFrame(updatePacificClock);

function updateFixAge() {
  if (lastFixTime && lastFixServerTimeMs) {
    const elapsedMs = Date.now() - lastFixServerTimeMs;
    const fixAge = Math.floor(elapsedMs / 1000);
    setText('g-fix-age', formatDuration(fixAge));
  }
}
setInterval(updateFixAge, 1000);

function render(d) {
  const { tracking: t, sources, sourcestats, clients, serverstats: ss, activity,
          gps, cpu_cores, pps_irq, pps_history, system: s, health } = d;

  setText('h-hostname', s.hostname);
  setText('h-ip', s.ip);
  setText('h-uptime', s.uptime);
  setText('h-temp', s.temperature);
  setText('h-ts', s.timestamp);

  // Fun fact rotation - throttled independent of data refresh
  if (!window._lastFactTime || Date.now() - window._lastFactTime > 15000) {
    document.getElementById('fun-fact-text').innerHTML = generateFunFact(d);
    window._lastFactTime = Date.now();
  }

  const banner = document.getElementById('status-banner');
  banner.classList.remove('ok', 'degraded', 'fault');
  if (health.status === 'OK') {
    banner.classList.add('ok');
    setText('status-text', '\u25C6 NOMINAL \u25B8 LOCKED & SERVING');
  } else if (health.status === 'DEGRADED') {
    banner.classList.add('degraded');
    setText('status-text', '\u26A0 DEGRADED');
  } else {
    banner.classList.add('fault');
    setText('status-text', '\u2715 FAULT');
  }
  setText('status-issues', health.issues.length ? health.issues.join('  \u25B8  ') : '');

  if (gps.satellites) {
    updateSats(gps.satellites);
    setText('skyview-meta', `${gps.sats_used || 0} LOCKED \u25C6 ${gps.sats_visible || 0} VISIBLE`);
  }

  setText('g-fix', gps.fix_mode);
  setText('g-sats', `${gps.sats_used || 0} / ${gps.sats_visible || 0}`);
  setText('g-snr', gps.best_snr || '\u2014');
  setText('g-hdop', gps.hdop != null ? gps.hdop.toFixed(2) : '\u2014');
  setText('g-pdop', gps.pdop != null ? gps.pdop.toFixed(2) : '\u2014');
  if (gps.lat != null && gps.lon != null) {
    setText('g-pos', `${gps.lat.toFixed(5)}\u00b0, ${gps.lon.toFixed(5)}\u00b0`);
  } else {
    setText('g-pos', '\u2014');
  }
  setText('g-alt', gps.alt != null ? gps.alt.toFixed(1) + ' m' : '\u2014');

  if (gps.fix_time && gps.fix_time !== lastFixTime) {
    lastFixTime = gps.fix_time;
    lastFixServerTimeMs = Date.now();
  }

  colorizeOffset(t.last_offset, 'h-sys-offset');
  setText('h-rms-offset', 'RMS ' + t.rms_offset);
  setText('h-stratum', t.stratum);
  setText('h-leap', 'Leap ' + t.leap_status);
  setText('h-freq', t.freq_error);
  setText('h-skew', 'Skew ' + t.skew);

  setText('t-refid', t.reference_id);
  setText('t-reftime', t.ref_time);
  setText('t-interval', t.update_interval);
  setText('t-rootdelay', t.root_delay);
  setText('t-rootdisp', t.root_dispersion);
  setText('t-residfreq', t.residual_freq);
  const actStr = Object.entries(activity).map(([k, v]) => `${v} ${k}`).join(' \u25B8 ');
  setText('t-activity', actStr || '\u2014');

  setText('s-host', s.hostname);
  setText('s-ip', s.ip);
  setText('s-temp', s.temperature);
  setText('s-mem', s.memory);
  setText('s-load', s.loadavg);
  setText('s-uptime', s.uptime);
  setText('s-isolated', s.isolated_cpus);
  setText('s-leap', d.next_leap || 'no scheduled leap');

  const ccDiv = document.getElementById('cpu-cores');
  const isolatedSet = new Set();
  if (s.isolated_cpus && s.isolated_cpus !== 'none' && s.isolated_cpus !== '\u2014') {
    s.isolated_cpus.split(',').forEach(part => {
      if (part.includes('-')) {
        const [a, b] = part.split('-').map(Number);
        for (let i = a; i <= b; i++) isolatedSet.add('cpu' + i);
      } else {
        isolatedSet.add('cpu' + part.trim());
      }
    });
  }
  ccDiv.innerHTML = cpu_cores.map(c => {
    const isIso = isolatedSet.has(c.core);
    return `<div class="core-bar">
      <span class="label ${isIso ? 'isolated' : ''}">${c.core.toUpperCase()}${isIso ? ' \u25C6' : ''}</span>
      <div class="track"><div class="fill ${isIso ? 'isolated' : ''}" style="width:${c.usage}%"></div></div>
      <span class="pct">${c.usage}%</span>
    </div>`;
  }).join('') + (isolatedSet.size > 0 ? '<div style="font-size:0.62rem;color:var(--magenta);margin-top:0.7rem;letter-spacing:0.1em;">\u25C6 ISOLATED CORE \u25C6</div>' : '');

  setText('ss-rx', Number(ss.ntp_packets_received).toLocaleString());
  setText('ss-drop', ss.ntp_packets_dropped);
  setText('ss-cmd-rx', Number(ss.command_packets_received).toLocaleString());
  setText('ss-cmd-drop', ss.command_packets_dropped);
  setText('ss-clog', ss.client_log_records);

  if (pps_history && pps_history.length > 0) {
    drawSparkline(pps_history);
    const min = Math.min(...pps_history);
    const max = Math.max(...pps_history);
    setText('pps-min', formatSecondsAsHuman(min));
    setText('pps-max', formatSecondsAsHuman(max));
    setText('pps-range', formatSecondsAsHuman(max - min));
    setText('pps-count', pps_history.length);
  } else {
    drawSparkline([]);
    setText('pps-min', '\u2014');
    setText('pps-max', '\u2014');
    setText('pps-range', '\u2014');
    setText('pps-count', '0');
  }
  setText('pps-irq', pps_irq.total ? pps_irq.total.toLocaleString() : '\u2014');

  const sb = document.getElementById('sources-body');
  if (sources.length === 0) {
    sb.innerHTML = '<tr><td colspan="8" style="color:var(--muted);padding:1rem">No sources</td></tr>';
  } else {
    sb.innerHTML = sources.map(src => `
      <tr>
        <td><span class="state-badge ${stateClass(src.state)}">${src.state}</span></td>
        <td>${src.name}</td>
        <td>${src.stratum}</td>
        <td>${src.poll}</td>
        <td>${reachBits(src.reach)}</td>
        <td>${src.last_rx}</td>
        <td>${src.last_sample_offset}</td>
        <td>${src.margin}</td>
      </tr>`).join('');
  }

  const stb = document.getElementById('stats-body');
  if (sourcestats.length === 0) {
    stb.innerHTML = '<tr><td colspan="6" style="color:var(--muted);padding:1rem">No stats</td></tr>';
  } else {
    stb.innerHTML = sourcestats.map(r => `
      <tr>
        <td>${r.name}</td>
        <td>${r.np} / ${r.nr}</td>
        <td>${r.span}</td>
        <td>${r.freq_skew}</td>
        <td>${r.std_dev}</td>
        <td>${r.offset}</td>
      </tr>`).join('');
  }

  const cb = document.getElementById('clients-body');
  if (clients.length === 0) {
    cb.innerHTML = '<tr><td colspan="4" style="color:var(--muted);padding:1rem">No clients yet</td></tr>';
  } else {
    cb.innerHTML = clients.map(c => `
      <tr>
        <td>${c.host}</td>
        <td>${c.ntp}</td>
        <td>${c.drops}</td>
        <td>${c.last_query}</td>
      </tr>`).join('');
  }

  if (d.visitors) {
    setText('visitor-current', d.visitors.current);
    setText('visitor-total', d.visitors.total_unique.toLocaleString());
  }
}


function generateFunFact(d) {
  const offsetStr = d.tracking.last_offset || '';
  const m = offsetStr.match(/([-\d.]+)\s*(ns|us|ms|s|seconds)?/);
  let ns = 1000;
  if (m) {
    const n = Math.abs(parseFloat(m[1]));
    const unit = m[2];
    ns = n;
    if (unit === 'us') ns = n * 1000;
    else if (unit === 'ms') ns = n * 1e6;
    else if (unit === 's' || unit === 'seconds') ns = n * 1e9;
  }
  const seconds = ns / 1e9;
  const speedOfLight = 299792458;
  const lightDistance = seconds * speedOfLight;
  const soundSpeed = 343;
  const soundDistance = seconds * soundSpeed;

  const fmtMeters = (m) => {
    if (m < 0.001) return (m * 1000).toFixed(2) + ' mm';
    if (m < 1)     return (m * 100).toFixed(1) + ' cm';
    if (m < 1000)  return m.toFixed(2) + ' m';
    return (m / 1000).toFixed(2) + ' km';
  };

  const sats = d.gps.sats_used || 0;
  const visible = d.gps.sats_visible || 0;
  const alt = d.gps.alt;
  const ntpRx = parseInt(d.serverstats.ntp_packets_received) || 0;
  const uptime = d.system.uptime || '';
  const stratum = d.tracking.stratum;
  const freq = parseFloat((d.tracking.freq_error || '0').replace(/[^-\d.]/g, '')) || 0;
  const skew = parseFloat((d.tracking.skew || '0').replace(/[^-\d.]/g, '')) || 0;
  const ppsTotal = (d.pps_irq && d.pps_irq.total) || 0;
  const temp = d.system.temperature || '';
  const rootDelay = d.tracking.root_delay || '';

  const facts = [
    // Light & sound at your offset
    `Your last offset of <span class="hl">${offsetStr}</span> is the time it takes light to travel <span class="hl-amber">${fmtMeters(lightDistance)}</span> in a vacuum.`,
    `Sound travels just <span class="hl-amber">${fmtMeters(soundDistance)}</span> in the time of your current offset of <span class="hl">${offsetStr}</span>.`,
    `In the time of your offset, an electron travels less than <span class="hl-amber">${fmtMeters(lightDistance * 0.05)}</span> through copper wire (signals propagate at ~5% of c).`,
    `Light travels <span class="hl">~30 cm in 1 nanosecond</span>. Your last offset corresponds to about <span class="hl-amber">${fmtMeters(lightDistance)}</span> of light travel.`,
    `Your offset of <span class="hl">${offsetStr}</span> is roughly the time it takes a photon to travel <span class="hl-amber">${fmtMeters(lightDistance)}</span>. A human hair is 80 microns thick.`,
    `An optical fiber transmits light at about <span class="hl">2/3 the vacuum speed of light</span>. Your offset equates to ${fmtMeters(lightDistance * 0.67)} of fiber travel.`,
    `In your current offset, a radio wave at 2.4 GHz Wi-Fi completes only <span class="hl-amber">${Math.max(1, Math.floor(2.4e9 * ns / 1e9)).toLocaleString()}</span> oscillations.`,
    `A bullet travels at roughly <span class="hl">760 m/s</span>. In the duration of your offset it would move just <span class="hl-amber">${fmtMeters(seconds * 760)}</span>.`,
    `Earth orbits the Sun at <span class="hl">~30 km/s</span>. In your offset duration, Earth moved <span class="hl-amber">${fmtMeters(seconds * 30000)}</span> along its orbit.`,
    `The Moon recedes from Earth at <span class="hl">38 mm/year</span>. Your offset is so brief the Moon moved <span class="hl-amber">${(38 * seconds / 31557600 * 1e9).toExponential(2)} nm</span> in that time.`,

    // NTP precision comparisons
    `A standard NTP query exchange takes around <span class="hl">10 ms</span>. Your server is roughly <span class="hl-amber">${(10000000 / Math.max(ns, 1)).toFixed(0)}x</span> more precise than the round trip.`,
    `A typical home internet connection has <span class="hl">5-30 ms latency</span> to its NTP source. Your local server delivers time orders of magnitude tighter.`,
    `Public NTP pool servers offer accuracy around <span class="hl">1-50 ms</span>. Yours is better by a factor of <span class="hl-amber">${(1000000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span>.`,
    `Windows' built-in w32time targets <span class="hl">~1 second accuracy</span> by default. Your stratum 1 box is roughly <span class="hl-amber">${(1e9 / Math.max(ns, 1)).toExponential(1)}x</span> tighter.`,
    `Cloudflare's NTP service publishes about <span class="hl">200 microsecond</span> accuracy. Yours beats that by <span class="hl-amber">${(200000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span>.`,
    `Apple's time.apple.com is stratum 2. You're <span class="hl">one level closer to the source</span>, with no upstream server between you and atomic time.`,

    // GPS / orbital
    `Right now <span class="hl">${sats} of ${visible}</span> visible satellites are being used to discipline this clock. Each one is roughly <span class="hl-amber">20,200 km</span> overhead, moving at <span class="hl-amber">14,000 km/h</span>.`,
    `GPS satellite atomic clocks tick about <span class="hl">38 microseconds per day</span> faster than ours due to relativity. Your offset is <span class="hl-amber">${(38000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span> better than that drift.`,
    `Each GPS satellite carries <span class="hl">two rubidium and two cesium atomic clocks</span>, kept in agreement and continuously monitored by ground control.`,
    `GPS satellites orbit Earth twice per sidereal day, at an altitude of <span class="hl">20,200 km</span>. Their signals reach you about <span class="hl-amber">67 ms</span> after transmission.`,
    `The GPS constellation has <span class="hl">31 operational satellites</span> currently. You can typically see 8-12 above the horizon at any time.`,
    `A GPS satellite must broadcast its position to within <span class="hl">a few meters</span> for your fix to work. That requires its onboard clocks to agree to within <span class="hl-amber">~10 ns</span>.`,
    `GPS time started at <span class="hl">midnight UTC, January 6, 1980</span>. It has not had a leap second adjustment since, which is why GPS time is now 18 seconds ahead of UTC.`,
    `Your altitude reading of <span class="hl">${alt != null ? alt.toFixed(0) + ' m' : 'unknown'}</span> comes from triangulating signals that left GPS satellites about <span class="hl-amber">67 milliseconds ago</span>.`,
    `Your position fix is computed from at least <span class="hl">4 satellites</span>: three for X/Y/Z, and one extra to solve for the receiver's clock offset.`,
    `Russia's GLONASS, China's BeiDou, and Europe's Galileo each operate <span class="hl">independent satellite constellations</span> for global positioning. Many modern receivers combine all four.`,
    `A GPS satellite weighs about <span class="hl">2,000 kg</span> and is powered by solar panels generating roughly <span class="hl-amber">1.5 kW</span>.`,
    `The signal you receive from each GPS satellite is below the noise floor, weaker than <span class="hl">-130 dBm</span>. Your receiver de-spreads it using known pseudo-random codes.`,
    `If GPS satellites didn't correct for relativity, position fixes would drift by <span class="hl-amber">~11 km per day</span>.`,
    `GPS satellites broadcast on <span class="hl">L1 (1575.42 MHz)</span> and <span class="hl">L2 (1227.60 MHz)</span>. Civilian receivers primarily use L1 C/A code.`,
    `A single GPS L1 carrier wave cycle takes <span class="hl">0.65 nanoseconds</span>. Your timing precision is in that same neighborhood.`,
    `Sky-view satellites visible to your antenna right now: <span class="hl">${visible}</span>. The maximum theoretically observable from any point on Earth is around <span class="hl-amber">12</span> at once.`,

    // Human comparisons
    `A human eye blinks in about <span class="hl">100 milliseconds</span>. Your last offset is <span class="hl-amber">${(1e8 / Math.max(ns, 1)).toFixed(0)}x</span> faster than a blink.`,
    `A neuron in your brain fires in about <span class="hl">1 millisecond</span>. Your last offset is <span class="hl-amber">${(1e6 / Math.max(ns, 1)).toFixed(0)}x</span> shorter than one neural firing.`,
    `The fastest human reaction time on record is about <span class="hl">100 ms</span>. Your offset is <span class="hl-amber">${(1e8 / Math.max(ns, 1)).toFixed(0)}x</span> tighter than human reflexes.`,
    `Audio engineers can perceive timing differences down to about <span class="hl">10 microseconds</span> between ears. Your offset is <span class="hl-amber">${(10000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span> smaller.`,
    `A high-speed camera shooting at <span class="hl">10,000 frames per second</span> captures one frame every 100 microseconds. Your offset is far below that.`,
    `Movies project at <span class="hl">24 frames per second</span>, so one frame is 41.7 ms. Your offset is <span class="hl-amber">${(41.7e6 / Math.max(ns, 1)).toFixed(0)}x</span> shorter than a single film frame.`,

    // Frequency, drift, skew
    `Your clock's frequency error is <span class="hl">${freq.toFixed(2)} ppm</span>. A typical wristwatch drifts at around <span class="hl-amber">10-50 ppm</span>.`,
    `Your skew of <span class="hl">${skew.toFixed(3)} ppm</span> means your local oscillator can drift by less than <span class="hl-amber">${(skew * 86.4).toFixed(0)} microseconds per day</span> if left undisciplined.`,
    `The Pi 4's crystal oscillator runs at <span class="hl">19.2 MHz</span>. Without GPS discipline, it would drift by roughly <span class="hl-amber">${Math.abs(freq * 86.4).toFixed(0)} ms per day</span>.`,
    `A quartz crystal in a typical computer drifts by <span class="hl">several seconds per day</span>. Yours is corrected continuously, kept to nanoseconds.`,
    `Temperature changes of <span class="hl">1°C</span> shift a typical quartz oscillator's frequency by about <span class="hl-amber">0.04 ppm</span>. Your CPU at ${temp || '37°C'} is being compensated in real time.`,
    `Your residual frequency tells chrony how much its current correction is missing the mark. Values near zero mean the loop has fully converged.`,
    `Allan deviation measures how a clock's frequency varies over different averaging intervals. A typical TCXO sits around <span class="hl">10^-7</span>; your GPS-disciplined clock reaches <span class="hl-amber">10^-9</span> or better.`,

    // Stratum and NTP fundamentals
    `Stratum <span class="hl">${stratum}</span> means you're directly disciplined by GPS. Most internet NTP clients you'll meet are stratum 3 or worse.`,
    `NTP stratum increases by one for each layer down the hierarchy. Reference clocks (atomic, GPS) are stratum 0. You read from one directly, making you stratum 1.`,
    `The first NTP RFC was published in <span class="hl">1985</span>. The protocol you're running has been refined for over <span class="hl-amber">40 years</span>.`,
    `NTP version 4, the current spec, was standardized in <span class="hl">RFC 5905 (2010)</span>. Chrony implements it with optional NTS extensions for authentication.`,
    `The NTP packet you send is exactly <span class="hl">48 bytes</span> on the wire. The protocol has changed very little since the 1980s.`,
    `Chrony was created by <span class="hl">Richard Curnow</span> in 1997, designed for systems with intermittent network connections, like dial-up. It now powers Stratum 1 servers worldwide.`,
    `Many Linux distributions replaced ntpd with chrony as the default time daemon between <span class="hl">2014 and 2020</span>, citing faster convergence and lower resource use.`,

    // Server activity
    `Since boot, this server has answered <span class="hl">${ntpRx.toLocaleString()}</span> NTP packets. ${uptime}.`,
    `An NTP exchange uses <span class="hl">UDP port 123</span>. The 48-byte packet has been the same shape since 1985.`,
    `Your server is operating <span class="hl">stateless</span>. It doesn't track clients; each query is independent. Yet you still see <span class="hl-amber">${ntpRx.toLocaleString()}</span> requests served.`,

    // PPS and discipline
    `Your PPS pulse has fired <span class="hl">${ppsTotal.toLocaleString()}</span> times since boot. That's one per second from a satellite signal originating in space.`,
    `PPS stands for <span class="hl">Pulse Per Second</span>. A precise voltage rising edge from your GPS module marks the start of each second to within nanoseconds.`,
    `Your PPS signal arrives as a hardware interrupt, bypassing the OS scheduler entirely. That's why GPS-disciplined Linux can hit nanosecond precision.`,
    `Without PPS, GPS time discipline is limited to about <span class="hl">100 ms</span> due to serial data jitter. With PPS, you can reach <span class="hl-amber">tens of nanoseconds</span>.`,

    // Physics / relativity / metrology
    `Cesium-133 atoms vibrate <span class="hl">9,192,631,770 times per second</span>. That's the official definition of one second since 1967.`,
    `The kilogram was redefined in 2019 using Planck's constant. The second, however, has been atomically defined since <span class="hl">1967</span>. Time is the most precisely measured quantity in physics.`,
    `One nanosecond per year is the accuracy of the best lab atomic clocks. Your server is about <span class="hl">${(ns / 1).toFixed(0)}x</span> less accurate, but still excellent for civilian use.`,
    `NIST's <span class="hl">NIST-F2 fountain clock</span> would neither gain nor lose a second in 300 million years. Your clock is about a million times less precise, but a billion times cheaper.`,
    `The Earth's rotation slows by about <span class="hl">1.7 ms per century</span>. Your clock is more stable than the planet itself.`,
    `A second on a GPS satellite is measurably <span class="hl">~38 microseconds longer per day</span> than one on Earth, due to weaker gravity (general relativity).`,
    `Time runs slower in stronger gravity. The clock at your feet ticks <span class="hl">10^-16</span> slower than one near your head, a difference measurable with optical lattice clocks.`,
    `The world's most precise clock, an optical lattice strontium clock at JILA, has uncertainty around <span class="hl">10^-18</span>, equivalent to losing one second in the age of the universe.`,
    `Coordinated Universal Time (UTC) is maintained by averaging <span class="hl">~400 atomic clocks</span> at <span class="hl-amber">~80 labs</span> worldwide. The result is published monthly by the BIPM in Paris.`,
    `International Atomic Time (TAI) doesn't include leap seconds. UTC stays close to solar time by inserting them periodically. <span class="hl">UTC = TAI - 37 seconds</span> right now.`,
    `Stock exchanges require time accuracy of <span class="hl">100 microseconds</span> for trade timestamps. You're <span class="hl-amber">${(100000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span> better than that requirement.`,
    `MiFID II regulations require financial firms to timestamp orders to <span class="hl">100 microseconds</span> traceable to UTC. Your stratum 1 box would satisfy that requirement out of the box.`,
    `Power grids synchronize their AC waveforms to <span class="hl">±10 microseconds</span> across continents. Time disciplines power as much as it disciplines computers.`,
    `Cellular base stations require time sync to <span class="hl">a few microseconds</span> for handoffs to work. Your server is precise enough to run a small cell network.`,

    // Leap seconds and edge cases
    `GPS time is currently <span class="hl">18 seconds ahead of UTC</span> due to leap seconds. Your chrony daemon handles that conversion automatically.`,
    `A leap second has not been added since <span class="hl">December 31, 2016</span>. In 2022, the world's metrologists voted to abolish leap seconds by 2035.`,
    `The clock has experienced <span class="hl">27 leap seconds</span> since UTC was synchronized to atomic time in 1972. All but one were positive (extra seconds added).`,
    `Negative leap seconds, where the clock skips a second, have never been needed but remain part of the spec. Earth's rotation has slightly sped up in recent years.`,

    // Performance / math
    `A modern CPU executes about <span class="hl">3 billion instructions per second</span>. In your current offset of <span class="hl-amber">${offsetStr}</span>, it could run roughly <span class="hl">${Math.max(1, Math.floor(3 * ns)).toLocaleString()}</span> instructions.`,
    `If your offset stays at <span class="hl">${offsetStr}</span>, your clock would gain or lose only <span class="hl-amber">${(ns * 86400 / 1e9).toFixed(6)} seconds per day</span>.`,
    `Root delay of <span class="hl">${rootDelay}</span> is the round-trip latency to your time reference. For GPS-disciplined clocks this should be near zero.`,
    `Your CPU is running at <span class="hl">${temp}</span>. Atomic clocks in GPS satellites are kept at constant temperature in deep space, varying by less than <span class="hl-amber">1°C</span>.`,
    `The Allan deviation of a typical TCXO is <span class="hl">10^-7</span>. Your GPS-disciplined clock achieves better than <span class="hl-amber">10^-9</span> after stabilization.`,

    // Hardware tidbits
    `The Raspberry Pi's broadcom SoC has a hardware <span class="hl">system timer</span> running at 1 MHz, accessible via memory-mapped registers, giving microsecond precision before any GPS help.`,
    `Your GPS module's antenna is sensitive to signals weaker than a millionth of a billionth of a watt. Yet from <span class="hl">20,200 km away</span>, it pulls out timing to nanoseconds.`,
    `A typical GPS receiver chipset draws under <span class="hl">100 mW</span>. The satellites broadcasting to it draw <span class="hl-amber">15,000x</span> more power.`,
    `The PPS signal often arrives via a single GPIO pin. From a hardware perspective, a Stratum 1 time server can be built around <span class="hl">three wires and a $30 module</span>.`,

    // History
    `Before atomic clocks, the second was defined as <span class="hl">1/86,400 of a mean solar day</span>. We discovered Earth's rotation was too erratic for precision science.`,
    `John Harrison built the first marine chronometer in 1761 to solve the longitude problem. His H4 lost only <span class="hl">5 seconds in 81 days</span> at sea, a feat unrivaled for a century.`,
    `The transatlantic telegraph cable of 1858 was the first time signal sent globally. It marked the beginning of synchronized world time.`,
    `Railway time forced the first time zones in the 1840s. Before that, every town kept its own local solar time. Your clock now synchronizes with all of them via a single second standard.`,
    `Greenwich Mean Time (GMT) was adopted as the world reference in <span class="hl">1884</span> at the International Meridian Conference. UTC replaced it in 1972 as the atomic-based successor.`,

    // Bandwidth and packet trivia
    `NTP traffic is among the lightest on the internet, around <span class="hl">90 bytes per packet</span> including UDP/IP headers. Your server handles thousands of these for negligible bandwidth.`,
    `An NTP packet's timestamp field uses <span class="hl">64 bits</span>: 32 for seconds since 1900, 32 for fractional seconds (~233 picosecond resolution).`,
    `NTP's timestamp will roll over in <span class="hl">2036</span>. NTPv4's 128-bit Era field already solves this, but most implementations still rely on the 32-bit format.`,
    `Public NTP server "ntp.org" handles <span class="hl">over a billion packets per day</span> globally. Your private server avoids that load entirely.`,
  ];

  return facts[Math.floor(Math.random() * facts.length)];
}

let prog = document.getElementById('refresh-progress');
function startProgress() {
  prog.style.transition = 'none';
  prog.style.width = '0%';
  requestAnimationFrame(() => {
    prog.style.transition = `width ${REFRESH_S}s linear`;
    prog.style.width = '100%';
  });
}

// ============================================================================
// Tooltip system: definitions for every labeled metric on the dashboard.
// Add or edit entries here, then attachTooltips() picks them up automatically.
// ============================================================================
const GLOSSARY = {
  // Header meta
  'Node': 'The IP address this server is reachable at on the local network. Clients use this address to query for time.',
  'Uptime': 'How long the chrony daemon has been continuously running since the last boot or restart.',
  'Core Temp': 'Temperature of the CPU. Affects the local oscillator frequency. Stable temperatures yield more stable clocks.',
  'Mission Time': 'The current system time as reported by the server, in UTC. Refreshed each time telemetry is fetched.',
  'Status': 'Overall transmit indicator. TRANSMITTING means the NTP daemon is actively serving time to clients on the network.',

  // GPS Subsystem
  'Fix Mode': '3D means latitude, longitude, and altitude are all being computed. 2D means altitude is unavailable. No Fix means the receiver cannot solve a position.',
  'Satellites Locked / Visible': 'Locked: satellites actively being used in the time/position solution. Visible: total satellites the receiver can see in the sky.',
  'Best SNR (dB-Hz)': 'Signal-to-noise ratio of the strongest satellite signal. Higher is better. Values above 40 dB-Hz indicate excellent reception.',
  'HDOP': 'Horizontal Dilution of Precision. A geometric quality factor for the horizontal position fix. Lower is better: <1 is ideal, 1-2 is excellent, 2-5 is good, >5 is poor.',
  'PDOP': 'Position Dilution of Precision. Combines horizontal and vertical geometry. Lower means the satellites are well-distributed for an accurate 3D fix.',
  'Position': 'Latitude and longitude of the antenna, computed from satellite signals. Accuracy is typically a few meters with a good fix.',
  'Altitude': 'Height above the WGS84 ellipsoid (approximately mean sea level). Typically less accurate than horizontal position due to satellite geometry.',
  'Time Since Lock': 'How long the receiver has held a continuous fix without losing lock on enough satellites.',

  // Offset / Stratum / Frequency
  'System Offset': 'The estimated difference between your system clock and the true reference time. Smaller is better; near-zero values indicate excellent discipline.',
  'Last Offset': 'The most recent measured offset between your clock and the GPS reference at the last update.',
  'Stratum Level': 'Your position in the NTP hierarchy. Stratum 0 is the reference clock itself (GPS, atomic). Stratum 1 reads directly from it. Each downstream layer adds 1.',
  'NTP Stratum': 'Stratum number assigned to packets your server sends out. Lower means closer to the time source. 1 is the lowest a server can advertise.',
  'Frequency Drift': 'How much the local oscillator drifts per second relative to true time, expressed in parts per million (ppm). Chrony compensates for this in real time.',
  'Clock Offset': 'The current frequency correction chrony is applying to keep your clock locked. Reflects how far off the raw oscillator runs.',

  // PPS card
  'PPS Discipline (recent samples)': 'A sparkline of recent Pulse-Per-Second samples from the GPS receiver. Tighter clustering means more stable hardware timing.',
  'Min': 'Smallest (most negative or zero) sample value in the recent PPS history.',
  'Max': 'Largest sample value in the recent PPS history.',
  'Range': 'Difference between Min and Max. A small range indicates a stable PPS source.',
  'Sample count': 'Number of recent PPS samples included in this view.',
  'PPS IRQ Total': 'Total count of PPS hardware interrupts received since boot. One pulse per second when GPS is locked.',

  // Tracking detail
  'Tracking Detail': 'Detailed metrics from "chronyc tracking" describing your synchronization state.',
  'Reference ID': 'Identifier of the time source chrony is currently using. For GPS this is typically a refclock name like "GPS" or "PPS".',
  'Ref Time (UTC)': 'The UTC timestamp of the most recent measurement from the reference clock.',
  'Update Interval': 'How often chrony updates its estimate of the system clock, in seconds.',
  'Root Delay': 'Total round-trip network delay to the stratum 0 reference. For GPS-disciplined clocks this is effectively zero.',
  'Root Dispersion': 'Accumulated maximum error estimate up the stratum chain. Bounds how wrong your time could plausibly be.',
  'Residual Freq': 'The portion of frequency error chrony has not yet corrected. Approaches zero as the loop converges.',
  'Activity': 'Counts of NTP sources by state: online, offline, burst-in-progress, etc.',

  // Hardware vitals
  'Hardware Vitals': 'System-level metrics about the machine running chrony.',
  'Hostname': 'The machine name as reported by the kernel.',
  'IP Address': 'Primary network interface IP address.',
  'Core Temperature': 'Current CPU package temperature. Higher temperatures can affect oscillator stability.',
  'Memory': 'RAM usage on the server.',
  'Load Average': 'System load over 1, 5, and 15 minute windows. Values below your core count are healthy.',
  'System Uptime': 'How long the OS has been running since the last boot.',
  'Isolated Cores': 'CPU cores reserved via isolcpus for time-critical tasks, kept off the general scheduler.',
  'Leap Data Expiry': 'Date by which the current leap-second table will need updating. Stale leap data can introduce errors.',

  // CPU
  'CPU Core Utilization': 'Per-core CPU usage. Spikes on isolated cores can affect timestamping precision.',

  // NTP service
  'NTP Service Statistics': 'Aggregate counts of NTP and command packets handled by chrony since startup.',
  'NTP Packets Received': 'Total NTP queries answered since chrony started.',
  'NTP Packets Dropped': 'NTP queries refused due to rate limiting, access control, or malformed packets.',
  'Command Packets Received': 'chronyc administrative commands received over the local socket.',
  'Command Packets Dropped': 'Administrative commands rejected, usually due to permission rules.',
  'Client Log Records Dropped': 'Per-client tracking entries that overflowed the log buffer. High values may indicate many distinct clients.',

  // Cards / section titles
  'GPS Subsystem': 'Live status of the GPS receiver and the satellites it can see.',
  'Authoritative Time (Disseminated From This Node)': 'The current time this server is broadcasting to NTP clients. This is what your network sees as "the truth".',
  'Time Sources': 'List of upstream time references chrony is comparing against. For a stratum 1 server, refclocks (GPS, PPS) appear here.',
  'Source Statistics': 'Long-term statistics for each source: sample counts, frequency, offset, and standard deviation.',
  'Connected Clients': 'Devices currently being served NTP time by this node.',

  // Time Sources table headers
  'State': 'Source state symbol: * (synced), + (combined), - (not combined), ? (unreachable), x (false ticker), ~ (too variable).',
  'Source': 'Hostname, IP, or refclock identifier of the time source.',
  'Stratum': 'Stratum level reported by this source.',
  'Poll': 'log2 of the polling interval in seconds. A poll of 6 means querying every 64 seconds.',
  'Reach': 'Octal value tracking the last 8 polls. 377 (octal) means all 8 recent polls succeeded.',
  'Last Rx': 'Time since the last packet was received from this source, in seconds.',
  'Offset': 'Measured time difference between your clock and this source.',
  'Margin': 'Estimated error margin on the last measurement.',

  // Source stats table headers
  'Samples (NP/NR)': 'Number of points used (NP) and runs (NR) in the regression analysis for this source.',
  'Span': 'Time span covered by the samples used in the regression, in seconds.',
  'Freq Skew': 'Estimated uncertainty in the frequency estimate, in ppm.',
  'Std Dev': 'Standard deviation of the measurements, indicating consistency.',

  // Clients table headers
  'Client': 'Hostname or IP of the connecting NTP client.',
  'NTP Requests': 'Total queries received from this client.',
  'Drops': 'Queries from this client that were dropped (e.g., rate-limited).',
  'Last Query': 'Time since the last query from this client.',
};

let _tipPop = null;
function _ensureTipPop() {
  if (!_tipPop) {
    _tipPop = document.createElement('div');
    _tipPop.className = 'tip-pop';
    document.body.appendChild(_tipPop);
  }
  return _tipPop;
}

function _showTip(target, label, text) {
  const pop = _ensureTipPop();
  pop.innerHTML = `<span class="tip-title">${label}</span>${text}`;
  pop.classList.add('visible');
  // Position after render so we know its size
  requestAnimationFrame(() => {
    const r = target.getBoundingClientRect();
    const popR = pop.getBoundingClientRect();
    const margin = 8;
    let left = r.left;
    let top = r.bottom + margin;
    // Flip below->above if it would overflow the viewport bottom
    if (top + popR.height > window.innerHeight - 8) {
      top = r.top - popR.height - margin;
    }
    // Clamp horizontally
    if (left + popR.width > window.innerWidth - 8) {
      left = window.innerWidth - popR.width - 8;
    }
    if (left < 8) left = 8;
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  });
}

function _hideTip() {
  if (_tipPop) _tipPop.classList.remove('visible');
}

function attachTooltips(root) {
  root = root || document;
  // Selectors covering every kind of label that should be hoverable
  const selectors = ['.stat-key', '.card-title', '.header-meta dt', 'th', '.hero-stat .lbl'];
  const nodes = root.querySelectorAll(selectors.join(','));
  nodes.forEach(el => {
    if (el.dataset.tipBound) return;
    const text = (el.textContent || '').trim();
    if (!text) return;
    const def = GLOSSARY[text];
    if (!def) return;
    el.setAttribute('data-tip', '1');
    el.dataset.tipBound = '1';
    el.addEventListener('mouseenter', () => _showTip(el, text, def));
    el.addEventListener('mouseleave', _hideTip);
    el.addEventListener('focus', () => _showTip(el, text, def));
    el.addEventListener('blur', _hideTip);
    // Tap support for touch devices: tap the label to toggle the tooltip
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const pop = _ensureTipPop();
      const isVisibleForThis = pop.classList.contains('visible') && pop.dataset.owner === el.dataset.tipBound + el.textContent;
      if (isVisibleForThis) {
        _hideTip();
      } else {
        pop.dataset.owner = el.dataset.tipBound + el.textContent;
        _showTip(el, text, def);
      }
    });
    el.tabIndex = 0;
  });
}

// Dismiss any visible tooltip when tapping/clicking elsewhere
document.addEventListener('click', _hideTip);

async function fetchData() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    const wasHidden = document.getElementById('dashboard').style.display !== 'block';
    document.getElementById('loading').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    if (wasHidden) { requestAnimationFrame(() => { resizeCanvas(); drawSky(); }); }
    render(data);
    attachTooltips();
  } catch (e) {
    document.getElementById('loading').textContent = '\u2715 TELEMETRY LINK FAILED';
    console.error(e);
  }
  startProgress();
}

updateWorldClock();
attachTooltips();
fetchData();
setInterval(fetchData, REFRESH_S * 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Record viewer for any real request (HTML or API poll counts as activity)
        try:
            ip = self.client_address[0]
            ua = self.headers.get("User-Agent", "unknown")
            record_visitor(ip, ua)
        except Exception:
            pass

        if self.path == "/api/status":
            try:
                self.send_json(get_all_data())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path in ("/", "/index.html"):
            hostname = socket.gethostname()
            self.send_html(HTML.replace("{hostname}", hostname))
        else:
            self.send_response(404)
            self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Chrony GPS Dashboard - Space-Age")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()
    server = HTTPServer((args.bind, args.port), Handler)
    print(f"[chrony-dashboard] Listening on http://{args.bind}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[chrony-dashboard] Stopped.")


if __name__ == "__main__":
    main()
