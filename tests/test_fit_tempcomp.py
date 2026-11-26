"""Tests for tools/fit_tempcomp.py using synthetic logs with a known answer."""

import importlib.util
import math
import random
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "fit_tempcomp", ROOT / "tools" / "fit_tempcomp.py")
ft = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ft)

START = datetime(2026, 11, 26, 5, 0, 0, tzinfo=timezone.utc).timestamp()


def stamp(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def make_logs(hours, slope_per_mc, curve=0.0, swing_mc=4000, noise=0.004, seed=1):
    """Daily temperature cycle; frequency = 9.8 + slope*(T-40000) + curve*(T-40000)^2."""
    rng = random.Random(seed)
    temp_lines, track_lines = [], []

    def temp_at(t):
        return 40000 + swing_mc * math.sin(2 * math.pi * (t - START) / 86400)

    for i in range(int(hours * 120)):          # every 30 s
        t = START + i * 30
        temp_lines.append(f"{stamp(t)}  {temp_at(t):.4e}  0.0000e+00")
    for i in range(int(hours * 225)):          # every 16 s
        t = START + i * 16
        d = temp_at(t) - 40000
        f = 9.8 + slope_per_mc * d + curve * d * d + rng.gauss(0, noise)
        track_lines.append(
            f"{stamp(t)} PPS              1 {f:10.3f}      0.020  1.0e-07 N  1  "
            "1.0e-07 -1.0e-11  1.0e-09  9.0e-06  2.6e-05")
    return temp_lines, track_lines


class FitTests(unittest.TestCase):
    def test_recovers_linear_slope_with_opposite_sign(self):
        temps, tracks = make_logs(72, slope_per_mc=-8e-5)   # -0.08 ppm per C
        result = ft.fit(ft.parse_tempcomp(temps), ft.parse_tracking(tracks))
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["model"], "linear")
        # The crystal slows as it warms, so compensation must speed it up.
        self.assertAlmostEqual(result["k1"], 8e-5, delta=4e-6)
        self.assertEqual(result["k2"], 0.0)
        self.assertGreater(result["r2"], 0.95)
        self.assertLess(result["resid_sd"], result["freq_sd"] / 5)
        self.assertIn("tempcomp /sys/class/thermal/thermal_zone0/temp 30 ", result["line"])

    def test_uses_a_curve_when_the_data_has_one(self):
        temps, tracks = make_logs(72, slope_per_mc=-5e-5, curve=1.5e-8)
        result = ft.fit(ft.parse_tempcomp(temps), ft.parse_tracking(tracks))
        self.assertEqual(result["model"], "quadratic")
        self.assertAlmostEqual(result["k2"], -1.5e-8, delta=3e-9)

    def test_refuses_too_little_data(self):
        temps, tracks = make_logs(6, slope_per_mc=-8e-5)
        result = ft.fit(ft.parse_tempcomp(temps), ft.parse_tracking(tracks))
        self.assertTrue(any("h of data" in p for p in result["problems"]))

    def test_refuses_when_temperature_does_not_explain_frequency(self):
        temps, tracks = make_logs(72, slope_per_mc=0.0, noise=0.05)
        result = ft.fit(ft.parse_tempcomp(temps), ft.parse_tracking(tracks))
        self.assertTrue(any("explains only" in p for p in result["problems"]))

    def test_refuses_flat_temperature(self):
        temps, tracks = make_logs(72, slope_per_mc=-8e-5, swing_mc=300)
        result = ft.fit(ft.parse_tempcomp(temps), ft.parse_tracking(tracks))
        self.assertTrue(any("temperature only varied" in p for p in result["problems"]))

    def test_few_minutes_cannot_be_fitted(self):
        temps, tracks = make_logs(0.1, slope_per_mc=-8e-5)
        with self.assertRaises(ValueError):
            ft.fit(ft.parse_tempcomp(temps), ft.parse_tracking(tracks))

    def test_ignores_other_sources_and_settling_after_restart(self):
        rows = ft.parse_tracking([
            "2026-11-26 05:00:00 162.159.200.1    3      9.000      0.020  1.0e-07 N  1",
            "2026-11-26 05:00:16 PPS              1      9.800      0.020  1.0e-07 N  1",
            "===========================================",
        ])
        self.assertEqual(len(rows), 1)
        freqs = [(START + i * 16, 9.8) for i in range(100)]          # 26 minutes
        freqs += [(START + 5000 + i * 16, 9.8) for i in range(100)]  # after a restart
        kept = ft.drop_after_restarts(freqs)
        self.assertTrue(all(t - START >= ft.SETTLE_S for t, _ in kept))
        self.assertFalse(any(5000 <= t - START < 5000 + ft.SETTLE_S for t, _ in kept))


if __name__ == "__main__":
    unittest.main()
