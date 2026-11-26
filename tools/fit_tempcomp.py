#!/usr/bin/env python3
"""Fit chrony's tempcomp coefficients from the Pi's own logs.

chrony.conf logs the CPU temperature (tempcomp.log, every 30 s, millidegrees C)
next to the clock frequency it measures against PPS (tracking.log, ppm). This
script lines the two up, fits frequency as a function of temperature, and
prints the `tempcomp` line that cancels it.

    tools/fit_tempcomp.py --host timeserver          # fetch the logs over SSH
    tools/fit_tempcomp.py --tempcomp tempcomp.log* --tracking tracking.log*

Sign convention. tracking.log's frequency is how fast the uncorrected clock
runs (positive = fast), with any tempcomp already taken out. tempcomp's value
is added to the clock's rate (positive = run faster). So if the crystal speeds
up by b ppm per unit of temperature, the compensation must be -b:

    f(T)    = a + b*(T - T0) + c*(T - T0)^2        (fitted here)
    comp(T) =     -b*(T - T0) - c*(T - T0)^2       (k0 = 0, k1 = -b, k2 = -c)

k0 stays 0 so the average frequency remains in chrony's drift file.
"""

import argparse
import bisect
import math
import subprocess
import sys
from datetime import datetime, UTC

LOG_DIR = "/var/log/chrony"
BIN_S = 300              # average both logs over 5-minute bins
RESTART_GAP_S = 120      # a gap this long in tracking.log means chrony restarted
SETTLE_S = 900           # ignore this long after a restart
MAX_TEMP_AGE_S = 90      # a frequency row needs a temperature this recent
# Below these, a fit says more about noise than about the crystal.
MIN_HOURS = 48
MIN_TEMP_RANGE = 3000    # millidegrees C
MIN_R2 = 0.5


def parse_time(date, clock):
    return datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=UTC).timestamp()


def parse_tempcomp(lines):
    """[(epoch, temperature)] from tempcomp.log lines."""
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4 or parts[0][:2] != "20":
            continue
        try:
            out.append((parse_time(parts[0], parts[1]), float(parts[2])))
        except ValueError:
            continue
    return sorted(set(out))


def parse_tracking(lines, refid="PPS"):
    """[(epoch, frequency ppm)] for rows synchronised to `refid` at stratum 1."""
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6 or parts[0][:2] != "20":
            continue
        if parts[2] != refid or parts[3] != "1":
            continue
        try:
            out.append((parse_time(parts[0], parts[1]), float(parts[4])))
        except ValueError:
            continue
    return sorted(set(out))


def drop_after_restarts(freqs):
    """Remove rows in the settling time after each start of chrony."""
    kept, settle_until, prev = [], None, None
    for t, f in freqs:
        if prev is None or t - prev > RESTART_GAP_S:
            settle_until = t + SETTLE_S
        prev = t
        if t >= settle_until:
            kept.append((t, f))
    return kept


def pair_up(temps, freqs):
    """Give each frequency row the latest temperature reading before it."""
    times = [t for t, _ in temps]
    pairs = []
    for t, f in freqs:
        i = bisect.bisect_right(times, t) - 1
        if i >= 0 and t - times[i] <= MAX_TEMP_AGE_S:
            pairs.append((t, temps[i][1], f))
    return pairs


def bin_means(pairs, width=BIN_S):
    """Average (temperature, frequency) over fixed time bins."""
    bins = {}
    for t, temp, f in pairs:
        b = bins.setdefault(int(t // width), [0, 0.0, 0.0])
        b[0] += 1
        b[1] += temp
        b[2] += f
    return [(k * width, s_t / n, s_f / n) for k, (n, s_t, s_f) in sorted(bins.items())]


def solve(matrix, rhs):
    """Gaussian elimination with partial pivoting."""
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-300:
            raise ValueError("singular fit (temperature did not vary)")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            k = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= k * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))) / a[r][r]
    return x


def polyfit(xs, ys, degree):
    """Least-squares polynomial; returns coefficients lowest power first."""
    size = degree + 1
    matrix = [[sum(x ** (i + j) for x in xs) for j in range(size)] for i in range(size)]
    rhs = [sum(y * x ** i for x, y in zip(xs, ys, strict=True)) for i in range(size)]
    return solve(matrix, rhs)


def r_squared(xs, ys, coeffs):
    mean = sum(ys) / len(ys)
    total = sum((y - mean) ** 2 for y in ys)
    resid = sum((y - sum(c * x ** i for i, c in enumerate(coeffs))) ** 2
                for x, y in zip(xs, ys, strict=True))
    return (1.0 - resid / total if total > 0 else 0.0), math.sqrt(resid / len(ys))


def fit(temps, freqs):
    """Return a dict describing the fit, or raise ValueError if there is no data."""
    pairs = pair_up(temps, drop_after_restarts(freqs))
    rows = bin_means(pairs)
    if len(rows) < 6:
        raise ValueError(f"only {len(rows)} five-minute bins of paired data")
    t0 = round(sum(r[1] for r in rows) / len(rows))
    xs = [r[1] - t0 for r in rows]
    ys = [r[2] for r in rows]
    mean_f = sum(ys) / len(ys)
    spread = math.sqrt(sum((y - mean_f) ** 2 for y in ys) / len(ys))

    linear = polyfit(xs, ys, 1)
    r2_lin, sd_lin = r_squared(xs, ys, linear)
    quad = polyfit(xs, ys, 2)
    r2_quad, sd_quad = r_squared(xs, ys, quad)
    # Keep the curve only if it explains clearly more than the straight line.
    use_quad = r2_quad - r2_lin > 0.02
    coeffs = quad if use_quad else linear + [0.0]
    r2, resid = (r2_quad, sd_quad) if use_quad else (r2_lin, sd_lin)

    temp_lo, temp_hi = min(r[1] for r in rows), max(r[1] for r in rows)
    k1, k2 = -coeffs[1], -coeffs[2]
    extremes = [k1 * (t - t0) + k2 * (t - t0) ** 2 for t in (temp_lo, temp_hi)]
    hours = (rows[-1][0] - rows[0][0] + BIN_S) / 3600.0

    problems = []
    if hours < MIN_HOURS:
        problems.append(f"only {hours:.1f} h of data (want {MIN_HOURS} h)")
    if temp_hi - temp_lo < MIN_TEMP_RANGE:
        problems.append(f"temperature only varied {(temp_hi - temp_lo) / 1000:.1f} C "
                        f"(want {MIN_TEMP_RANGE / 1000:.0f} C)")
    if r2 < MIN_R2:
        problems.append(f"temperature explains only {r2:.0%} of the frequency changes")
    if max(abs(e) for e in extremes) >= 10:
        problems.append("compensation would exceed chrony's 10 ppm limit")

    return {
        "bins": len(rows), "hours": hours,
        "temp_lo": temp_lo, "temp_hi": temp_hi,
        "freq_mean": mean_f, "freq_sd": spread, "resid_sd": resid, "r2": r2,
        "model": "quadratic" if use_quad else "linear",
        "t0": t0, "k0": 0.0, "k1": k1, "k2": k2,
        "comp_at_extremes": extremes,
        "problems": problems,
        "line": (f"tempcomp /sys/class/thermal/thermal_zone0/temp 30 "
                 f"{t0} 0 {k1:.6e} {k2:.6e}"),
    }


def report(result):
    r = result
    out = [
        f"Data:        {r['hours']:.1f} h in {r['bins']} five-minute bins",
        f"Temperature: {r['temp_lo'] / 1000:.1f} to {r['temp_hi'] / 1000:.1f} C",
        f"Frequency:   mean {r['freq_mean']:.3f} ppm, spread (sd) {r['freq_sd']:.4f} ppm",
        f"Fit:         {r['model']}, explains {r['r2']:.0%} of the frequency changes",
        (f"             spread left after compensation: {r['resid_sd']:.4f} ppm "
         f"({r['freq_sd'] / r['resid_sd']:.1f}x smaller)") if r["resid_sd"] > 0 else "",
        f"Slope:       {-r['k1'] * 1000:+.4f} ppm per C at {r['t0'] / 1000:.1f} C",
        ("Compensation at the coldest/hottest seen: "
         f"{r['comp_at_extremes'][0]:+.3f} / {r['comp_at_extremes'][1]:+.3f} ppm"),
        "",
    ]
    if r["problems"]:
        out.append("NOT READY TO APPLY:")
        out += [f"  - {p}" for p in r["problems"]]
    else:
        out.append("chrony.conf line:")
        out.append("  " + r["line"])
    return "\n".join(out)


def fetch(host, name):
    cmd = ["ssh", "-o", "BatchMode=yes", host,
           f"sudo -n sh -c 'cat {LOG_DIR}/{name}.log*'"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True,
                          timeout=120).stdout.splitlines()


def read_files(paths):
    lines = []
    for path in paths:
        with open(path, errors="replace") as f:
            lines += f.read().splitlines()
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", help="fetch both logs from this SSH host")
    parser.add_argument("--tempcomp", nargs="+", default=[], help="tempcomp.log files")
    parser.add_argument("--tracking", nargs="+", default=[], help="tracking.log files")
    parser.add_argument("--refid", default="PPS")
    args = parser.parse_args(argv)
    if args.host:
        temp_lines, track_lines = fetch(args.host, "tempcomp"), fetch(args.host, "tracking")
    elif args.tempcomp and args.tracking:
        temp_lines, track_lines = read_files(args.tempcomp), read_files(args.tracking)
    else:
        parser.error("give --host, or both --tempcomp and --tracking")
    try:
        result = fit(parse_tempcomp(temp_lines), parse_tracking(track_lines, args.refid))
    except ValueError as e:
        print(f"Cannot fit: {e}")
        return 2
    print(report(result))
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
