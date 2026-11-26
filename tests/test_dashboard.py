"""Tests for the dashboard's parsers, using recorded chronyc/gpsd output."""

import contextlib
import importlib.util
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "chrony_dashboard", ROOT / "dashboard" / "chrony_dashboard.py")
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


# Recorded from chrony 4.9 (`chronyc -n -c ...`).
SOURCES_CSV = """\
^,?,192.0.2.16,0,6,0,4294967295,0.000000000,0.000000000,0.000000000
^,+,162.159.200.1,3,9,377,352,-0.010559363,-0.010559363,0.023447527
^,*,162.159.200.123,3,10,377,940,-0.009622829,-0.010018874,0.026427295
#,*,PPS,0,4,377,15,0.000000012,0.000000020,0.000000101
#,?,NMEA,0,4,377,14,0.081234567,0.081234567,0.100000000
"""

SOURCESTATS_CSV = """\
192.0.2.16,0,0,0,0.000,2000.000,0.000000000,4.000000000
162.159.200.1,6,3,7222,-1.387,3.784,-0.007031661,0.002260464
162.159.200.123,32,13,28964,0.163,0.181,0.001009292,0.002714957
"""

# `chronyc -c selectdata` from the Pi: NMEA has the noselect option.
SELECTDATA_CSV = """\
N,NMEA,N,N,-,-,-,-,N,-,-,-,-,0,1.0,0.000000000,0.000000000,Normal
*,PPS,N,-,P,-,-,-,-,P,-,-,-,10,1.0,-0.000001855,-0.000000891,Normal
"""

CLIENTS_CSV = """\
192.168.1.20,1234,5,6,-,17,0,0,-,4294967295
2001:db8::1,42,0,10,-,3600,0,0,-,4294967295
"""

# Format recorded from the Pi (chrony 4.3, PPS refclock with "lock NMEA").
# Per-pulse lines carry pulse phase; "-" lines are filtered offsets.
REFCLOCKS_LOG = """\
===============================================================================
   Date (UTC) Time         Refid  DP L P  Raw offset   Cooked offset      Disp.
===============================================================================
2026-10-24 20:08:24.930279 PPS    12 N 1  6.972097e-02  6.972099e-02  3.700e-08
2026-10-24 20:08:26.333333 NMEA    - N -       -       8.12345e-02  1.000e-03
2026-10-24 20:08:26.333333 PPS     - N -       -       -4.200000e-08  2.105e-07
2026-10-24 20:08:30.930279 PPS    13 N 1  2.618798e-02  2.618803e-02  3.700e-08
2026-10-24 20:08:42.333333 PPS     - N -       -       3.100000e-08  2.105e-07
2026-10-24 20:08:58.333333 PPS     - N -       -       -1.000000e-09  2.105e-07
2026-10-24 20:08:59.973811 PPS     0 N 1  2.618798e-02  2.618803e-02  3.700e-08
"""

PER_PULSE_ONLY_LOG = """\
2026-11-25 10:00:00.000000 PPS     0 N 1  1.23456e-07  -4.20000e-08  1.000e-09
2026-11-25 10:00:01.000000 PPS     1 N 1  1.33456e-07   3.10000e-08  1.000e-09
"""


def fake_run(outputs):
    """Return a run() replacement that answers by chronyc subcommand."""
    def _run(cmd, timeout=5):
        return outputs.get(cmd[-1], "").strip()
    return _run


class FormatTests(unittest.TestCase):
    def test_fmt_seconds_matches_chronyc(self):
        # Expected strings are what `chronyc sources/sourcestats` print.
        self.assertEqual(cd.fmt_seconds(-0.009622829), "-9623us")
        self.assertEqual(cd.fmt_seconds(-0.010018874), "-10ms")
        self.assertEqual(cd.fmt_seconds(-0.007031661), "-7032us")
        self.assertEqual(cd.fmt_seconds(0.001009292), "+1009us")
        self.assertEqual(cd.fmt_seconds(0.0), "+0ns")
        self.assertEqual(cd.fmt_seconds(4.0, signed=False), "4000ms")
        self.assertEqual(cd.fmt_seconds(0.023447527, signed=False), "23ms")
        self.assertEqual(cd.fmt_seconds(12.5), "+12s")
        self.assertEqual(cd.fmt_seconds(None), "—")

    def test_fmt_interval_matches_chronyc(self):
        self.assertEqual(cd.fmt_interval("352"), "352")
        self.assertEqual(cd.fmt_interval("7222"), "120m")
        self.assertEqual(cd.fmt_interval("28964"), "482m")
        self.assertEqual(cd.fmt_interval("100000"), "27h")
        self.assertEqual(cd.fmt_interval("864000"), "10d")
        self.assertEqual(cd.fmt_interval("4294967295"), "-")
        self.assertEqual(cd.fmt_interval("-"), "-")


# Recorded together on the Pi (chrony 4.3): the CSV row and, below, what
# `chronyc tracking` printed for it.
TRACKING_CSV = ("50505300,PPS,1,1795720974.987433748,0.000000199,-0.000000203,0.000023913,"
                "8.713,-0.004,0.030,0.000000001,0.000007910,32.0,Normal\n")
TRACKING_TEXT = {
    "reference_id": "50505300 (PPS)",
    "stratum": "1",
    "ref_time": "Thu Nov 26 19:22:54 2026",
    "system_time": "0.000000199 seconds slow of NTP time",
    "last_offset": "-0.000000203 seconds",
    "rms_offset": "0.000023913 seconds",
    "freq_error": "8.713 ppm fast",
    "residual_freq": "-0.004 ppm",
    "skew": "0.030 ppm",
    "root_delay": "0.000000001 seconds",
    "root_dispersion": "0.000007910 seconds",
    "update_interval": "32.0 seconds",
    "leap_status": "Normal",
}
SERVERSTATS_CSV = "50,0,21419,0,0,0,0,11,0,0,0\n"
ACTIVITY_CSV = "5,0,0,0,6\n"


class TrackingTests(unittest.TestCase):
    def test_csv_is_worded_as_chronyc_words_it(self):
        with mock.patch.object(cd, "run", fake_run({"tracking": TRACKING_CSV})):
            tracking = cd.parse_tracking()
        for key, text in TRACKING_TEXT.items():
            self.assertEqual(tracking[key], text, key)

    def test_numbers_are_exposed_for_calculation(self):
        with mock.patch.object(cd, "run", fake_run({"tracking": TRACKING_CSV})):
            tracking = cd.parse_tracking()
        self.assertEqual(tracking["last_offset_s"], -2.03e-7)
        self.assertEqual(tracking["frequency_ppm"], 8.713)
        self.assertEqual(tracking["skew_ppm"], 0.030)
        self.assertEqual(tracking["update_interval_s"], 32.0)

    def test_signs(self):
        row = TRACKING_CSV.replace("0.000000199", "-0.000000199").replace("8.713", "-8.713")
        with mock.patch.object(cd, "run", fake_run({"tracking": row})):
            tracking = cd.parse_tracking()
        self.assertEqual(tracking["system_time"], "0.000000199 seconds fast of NTP time")
        self.assertEqual(tracking["freq_error"], "8.713 ppm slow")

    def test_chrony_not_running(self):
        for output in ("", "506 Cannot talk to daemon", "a,b,c"):
            with mock.patch.object(cd, "run", fake_run({"tracking": output})):
                tracking = cd.parse_tracking()
            self.assertEqual(tracking["stratum"], "\u2014", output)
            self.assertIsNone(tracking["last_offset_s"], output)
        # Health still reports it rather than failing.
        self.assertEqual(cd.compute_health(tracking, [], {"available": False})["status"], "FAULT")


class ServerStatsAndActivityTests(unittest.TestCase):
    def test_serverstats(self):
        with mock.patch.object(cd, "run", fake_run({"serverstats": SERVERSTATS_CSV})):
            stats = cd.parse_serverstats()
        self.assertEqual(stats["ntp_packets_received"], "50")
        self.assertEqual(stats["command_packets_received"], "21419")
        self.assertEqual(stats["client_log_records"], "0")
        self.assertEqual(stats["authenticated_ntp_packets"], "11")

    def test_serverstats_not_authorised(self):
        with mock.patch.object(cd, "run", fake_run({"serverstats": "501 Not authorised"})):
            stats = cd.parse_serverstats()
        self.assertEqual(stats["ntp_packets_received"], "0")

    def test_activity_has_no_stray_status_line(self):
        # The text form starts with "200 OK", which used to be shown as a count.
        with mock.patch.object(cd, "run", fake_run({"activity": ACTIVITY_CSV})):
            activity = cd.parse_activity()
        self.assertEqual(activity, {
            "sources online": "5", "sources offline": "0",
            "sources doing burst (return to online)": "0",
            "sources doing burst (return to offline)": "0",
            "sources with unknown address": "6"})

    def test_activity_unavailable(self):
        with mock.patch.object(cd, "run", fake_run({"activity": "506 Cannot talk to daemon"})):
            self.assertEqual(cd.parse_activity(), {})


class ChronycParserTests(unittest.TestCase):
    def test_sources(self):
        with mock.patch.object(cd, "run", fake_run({"sources": SOURCES_CSV})):
            sources = cd.parse_sources()
        self.assertEqual(len(sources), 5)
        never, combined, synced, pps, nmea = sources
        self.assertEqual(never["state"], "unreachable")
        self.assertEqual(never["last_rx"], "-")
        self.assertEqual(combined["state"], "combined")
        self.assertEqual(synced["name"], "162.159.200.123")
        self.assertEqual(synced["state"], "synced")
        self.assertEqual(synced["last_sample_offset"], "-9623us")
        self.assertEqual(synced["measured_offset"], "-10ms")
        self.assertEqual(synced["margin"], "±26ms")
        self.assertEqual(synced["reach"], "377")
        self.assertEqual(pps["mode"], "refclock")
        self.assertEqual(pps["last_sample_offset"], "+12ns")
        self.assertEqual(pps["margin"], "±101ns")
        # Answers (reach 377) but '?', and selectdata wasn't authorised.
        self.assertEqual(nmea["state"], "unusable")

    def test_noselect_from_selectdata(self):
        with mock.patch.object(cd, "run", fake_run({"sources": SOURCES_CSV,
                                                     "selectdata": SELECTDATA_CSV})):
            states = {s["name"]: s["state"] for s in cd.parse_sources()}
        self.assertEqual(states["NMEA"], "noselect")
        self.assertEqual(states["PPS"], "synced")
        self.assertEqual(states["192.0.2.16"], "unreachable")

    def test_selectdata_not_authorised(self):
        with mock.patch.object(cd, "run", fake_run({"sources": SOURCES_CSV,
                                                     "selectdata": "501 Not authorised"})):
            states = {s["name"]: s["state"] for s in cd.parse_sources()}
        self.assertEqual(states["NMEA"], "unusable")

    def test_sourcestats(self):
        with mock.patch.object(cd, "run", fake_run({"sourcestats": SOURCESTATS_CSV})):
            stats = cd.parse_sourcestats()
        self.assertEqual([s["span"] for s in stats], ["0", "120m", "482m"])
        self.assertEqual(stats[1]["offset"], "-7032us")
        self.assertEqual(stats[1]["std_dev"], "2260us")
        self.assertEqual(stats[1]["freq_skew"], "3.784")
        self.assertEqual(stats[2]["freq"], "+0.163")
        self.assertEqual(stats[0]["std_dev"], "4000ms")

    def test_clients_uses_ntp_last_column(self):
        with mock.patch.object(cd, "run", fake_run({"clients": CLIENTS_CSV})):
            clients = cd.parse_clients()
        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[0]["host"], "192.168.1.20")
        self.assertEqual(clients[0]["ntp"], "1234")
        self.assertEqual(clients[0]["drops"], "5")
        self.assertEqual(clients[0]["last_query"], "17")  # not the cmd column
        self.assertEqual(clients[1]["last_query"], "60m")

    def test_clients_not_authorised(self):
        with mock.patch.object(cd, "run", fake_run({"clients": "501 Not authorised"})):
            self.assertEqual(cd.parse_clients(), [])

    def test_empty_output(self):
        with mock.patch.object(cd, "run", fake_run({})):
            self.assertEqual(cd.parse_sources(), [])
            self.assertEqual(cd.parse_sourcestats(), [])


class PpsHistoryTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write(REFCLOCKS_LOG)

    def tearDown(self):
        os.unlink(self.path)

    def test_uses_filtered_offsets_not_pulse_phase(self):
        self.assertEqual(cd.parse_pps_history("PPS", self.path),
                         [-4.2e-08, 3.1e-08, -1e-09])

    def test_other_refid(self):
        self.assertEqual(cd.parse_pps_history("NMEA", self.path), [0.0812345])

    def test_falls_back_to_per_pulse_cooked(self):
        with open(self.path, "w") as f:
            f.write(PER_PULSE_ONLY_LOG)
        self.assertEqual(cd.parse_pps_history("PPS", self.path), [-4.2e-08, 3.1e-08])

    def test_missing_file(self):
        self.assertEqual(cd.parse_pps_history("PPS", self.path + ".nope"), [])

    def test_limit_and_partial_first_line(self):
        line = "2026-11-25 10:00:00.000000 PPS     0 N 0  1.0e-07  5.00000e-08  1.0e-09\n"
        with open(self.path, "w") as f:
            f.write(line * 5000)  # larger than the 64 KiB tail window
        samples = cd.parse_pps_history("PPS", self.path)
        self.assertEqual(len(samples), 60)
        self.assertTrue(all(s == 5e-08 for s in samples))


class FakeGpsd:
    """Minimal gpsd stand-in that answers ?WATCH/?POLL with canned JSON."""

    def __init__(self, messages):
        self.messages = messages
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            conn.recv(4096)
            for m in self.messages:
                conn.sendall((json.dumps(m) + "\n").encode())
            with contextlib.suppress(OSError):
                conn.recv(4096)

    def close(self):
        self.sock.close()


SATS = [
    {"PRN": 5, "el": 45, "az": 120, "ss": 38, "used": True},
    {"PRN": 12, "el": 70, "az": 300, "ss": 42, "used": True},
    {"PRN": 20, "el": 10, "az": 30, "ss": 18, "used": False},
]


class GpsdTests(unittest.TestCase):
    def _query(self, messages):
        fake = FakeGpsd(messages)
        real = cd._read_gpsd
        try:
            with mock.patch.object(cd, "_read_gpsd",
                                   lambda: real(port=fake.port, timeout=2.0)):
                return cd.get_gpsd_data()
        finally:
            fake.close()

    def test_poll_response(self):
        gps = self._query([
            {"class": "VERSION", "release": "3.25"},
            {"class": "POLL",
             "tpv": [{"mode": 3, "time": "2026-11-25T10:00:00.000Z",
                      "lat": 37.1, "lon": -122.2, "altMSL": 12.5}],
             "sky": [{"hdop": 0.9, "pdop": 1.6, "satellites": SATS}]},
        ])
        self.assertTrue(gps["available"])
        self.assertEqual(gps["fix_mode"], "3D")
        self.assertEqual((gps["sats_used"], gps["sats_visible"]), (2, 3))
        self.assertEqual(gps["best_snr"], 42)
        self.assertEqual(gps["alt"], 12.5)
        self.assertEqual(gps["hdop"], 0.9)

    def test_sky_without_satellites_is_not_used_for_sat_list(self):
        gps = self._query([
            {"class": "TPV", "mode": 3, "time": "2026-11-25T10:00:00.000Z"},
            {"class": "SKY", "hdop": 1.1, "pdop": 2.0},           # DOPs only
            {"class": "SKY", "satellites": SATS},                  # full list
        ])
        self.assertEqual(gps["sats_visible"], 3)
        self.assertEqual(gps["hdop"], 1.1)

    def test_gpsd_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        real = cd._read_gpsd
        with mock.patch.object(cd, "_read_gpsd", lambda: real(port=port, timeout=0.5)):
            gps = cd.get_gpsd_data()
        self.assertFalse(gps["available"])
        self.assertEqual(gps["satellites"], [])


class CacheTests(unittest.TestCase):
    def test_snapshot_is_shared_within_ttl(self):
        calls = []
        with mock.patch.object(cd, "get_all_data",
                               lambda: calls.append(1) or {"health": {}}), \
             mock.patch.object(cd, "get_visitor_stats",
                               lambda: {"current": 1, "total_unique": 1}):
            cache = cd.StatusCache(ttl=60)
            a = cache.get()
            b = cache.get()
        self.assertEqual(len(calls), 1)
        self.assertEqual(a["visitors"]["current"], 1)
        self.assertIsNot(a, b)


class VersionTests(unittest.TestCase):
    def test_version_matches_the_changelog(self):
        changelog = (ROOT / "CHANGELOG.md").read_text()
        newest = next(line for line in changelog.splitlines() if line.startswith("## ["))
        self.assertTrue(newest.startswith(f"## [{cd.__version__}]"), newest)


class HealthTests(unittest.TestCase):
    TRACKING = {"stratum": "1", "reference_id": "50505300 (PPS)"}
    GPS = {"available": True, "fix_mode": "3D", "sats_used": 6}

    def test_ok(self):
        sources = [{"name": "PPS", "state": "synced", "reach": "377"}]
        self.assertEqual(cd.compute_health(self.TRACKING, sources, self.GPS),
                         {"status": "OK", "issues": []})

    def test_selected_source_that_went_silent_is_reported(self):
        # chrony still says "PPS, stratum 1" but no pulse has arrived for 8 polls.
        sources = [{"name": "PPS", "state": "synced", "reach": "0"}]
        health = cd.compute_health(self.TRACKING, sources, self.GPS)
        self.assertEqual(health["status"], "DEGRADED")
        self.assertEqual(health["issues"], ["PPS is selected but has stopped responding"])

    def test_no_fix_and_internet_time(self):
        health = cd.compute_health(
            {"stratum": "4", "reference_id": "A29FC87B (162.159.200.123)"},
            [{"name": "162.159.200.123", "state": "synced", "reach": "377"}],
            {"available": True, "fix_mode": "no fix", "sats_used": 0})
        self.assertEqual(health["status"], "DEGRADED")
        self.assertEqual(health["issues"],
                         ["Stratum 4", "GPS fix: no fix", "Not synced to GPS"])

    def test_nothing_synced_is_a_fault(self):
        health = cd.compute_health({"stratum": "10", "reference_id": "7F7F0101 ()"}, [],
                                   {"available": False})
        self.assertEqual(health["status"], "FAULT")
        self.assertIn("No synced source", health["issues"])


class SourceStateTests(unittest.TestCase):
    def test_silent_noselect_source_is_unreachable(self):
        # selectdata keeps saying N for a noselect refclock that has gone quiet.
        self.assertEqual(cd.source_state("?", "0", "N"), "unreachable")
        self.assertEqual(cd.source_state("?", "377", "N"), "noselect")
        self.assertEqual(cd.source_state("?", "377", None), "unusable")
        self.assertEqual(cd.source_state("*", "377", "*"), "synced")


class PpsWindowTests(unittest.TestCase):
    PULSE = "2026-11-25 10:00:00.000000 PPS    %2d N 1  6.97e-02  6.97e-02  3.7e-08\n"
    NMEA = "2026-11-25 10:00:00.100000 NMEA   %2d N 0  5.4e-06  5.1e-06  1.0e-03\n"
    FILTERED = "2026-11-25 10:00:00.300000 PPS     - N -       -       %.6e  2.1e-07\n"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "refclocks.log")
        cd._pps_window.clear()

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _write(self, path, first, count):
        """One filtered sample per 16 seconds of per-pulse lines, like the Pi."""
        with open(path, "w") as f:
            for i in range(first, first + count):
                for dp in range(16):
                    f.write(self.PULSE % dp)
                    f.write(self.NMEA % dp)
                f.write(self.FILTERED % (i * 1e-9))

    def test_reads_far_enough_back_for_a_full_chart(self):
        self._write(self.path, 0, 200)   # ~500 KiB; 64 KiB holds only ~25 samples
        samples = cd.parse_pps_history("PPS", self.path)
        self.assertEqual(len(samples), 60)
        self.assertAlmostEqual(samples[-1], 199e-9)
        self.assertAlmostEqual(samples[0], 140e-9)

    def test_tops_up_from_rotated_log(self):
        self._write(self.path + ".1", 0, 100)
        self._write(self.path, 100, 5)   # just rotated: only 5 samples so far
        samples = cd.parse_pps_history("PPS", self.path)
        self.assertEqual(len(samples), 60)
        self.assertAlmostEqual(samples[-1], 104e-9)
        self.assertAlmostEqual(samples[0], 45e-9)


class GpsdPollTests(unittest.TestCase):
    def setUp(self):
        cd._fix_state.update(seen=False, since=None, exact=False, last_fix=None)

    def test_poll_without_satellites_returns_at_once(self):
        # During an outage gpsd has a TPV but no satellites; do not wait for any.
        fake = FakeGpsd([
            {"class": "VERSION", "release": "3.22"},
            {"class": "POLL", "tpv": [{"mode": 1, "time": "2026-11-25T10:00:00.000Z"}],
             "sky": [{"satellites": []}]},
        ])
        try:
            start = cd.time.monotonic()
            tpv, sky, _ = cd._read_gpsd(port=fake.port, timeout=3.0)
            elapsed = cd.time.monotonic() - start
        finally:
            fake.close()
        self.assertLess(elapsed, 1.0)
        self.assertEqual(tpv["mode"], 1)
        self.assertIsNone(sky)

    def test_fix_tracking(self):
        with mock.patch.object(cd.time, "time", return_value=1000.0):
            first = cd._note_fix({"mode": 3})
        self.assertEqual((first["has_fix"], first["fix_since"]), (True, 1000.0))
        self.assertFalse(first["fix_since_exact"])  # already locked at first look
        with mock.patch.object(cd.time, "time", return_value=1010.0):
            lost = cd._note_fix({"mode": 1})
        self.assertEqual((lost["has_fix"], lost["fix_since"], lost["last_fix"]),
                         (False, None, 1000.0))
        with mock.patch.object(cd.time, "time", return_value=1020.0):
            again = cd._note_fix({"mode": 2})
        self.assertEqual(again["fix_since"], 1020.0)
        self.assertTrue(again["fix_since_exact"])   # we saw this lock begin
        with mock.patch.object(cd.time, "time", return_value=1030.0):
            self.assertEqual(cd._note_fix({"mode": 3})["fix_since"], 1020.0)
            self.assertIsNone(cd._note_fix(None)["fix_since"])  # gpsd down


class VisitorTests(unittest.TestCase):
    def setUp(self):
        self._proxies = set(cd.TRUSTED_PROXIES)
        cd.TRUSTED_PROXIES.clear()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "visitors.json")

    def tearDown(self):
        cd.TRUSTED_PROXIES.clear()
        cd.TRUSTED_PROXIES.update(self._proxies)
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def test_real_ip_only_from_trusted_proxy(self):
        headers = {"X-Real-IP": "203.0.113.7"}
        self.assertEqual(cd.client_ip("192.0.2.69", headers), "192.0.2.69")
        cd.TRUSTED_PROXIES.add("192.0.2.69")
        self.assertEqual(cd.client_ip("192.0.2.69", headers), "203.0.113.7")
        self.assertEqual(cd.client_ip("198.51.100.1", headers), "198.51.100.1")
        self.assertEqual(cd.client_ip("192.0.2.69", {}), "192.0.2.69")

    def test_counts_and_persistence(self):
        log = cd.VisitorLog(path=self.path, save_interval_s=0)
        log.record("203.0.113.7", "browser-a")
        log.record("203.0.113.7", "browser-a")     # same visitor again
        log.record("203.0.113.8", "browser-a")
        self.assertEqual(log.stats(), {"current": 2, "total_unique": 2})
        # A new process picks the total up from the file; nobody is viewing yet.
        self.assertEqual(cd.VisitorLog(path=self.path).stats(),
                         {"current": 0, "total_unique": 2})

    def test_unique_set_is_capped_and_saves_are_throttled(self):
        log = cd.VisitorLog(path=self.path, max_unique=5, save_interval_s=30)
        saves = []
        with mock.patch.object(log, "_save", lambda: saves.append(1)):
            for i in range(50):
                log.record("203.0.113.7", f"agent-{i}")
        self.assertEqual(len(log.unique), 5)
        self.assertEqual(len(saves), 1)   # not one write per new visitor

    def test_unreadable_file_is_logged_and_ignored(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        with self.assertLogs("chrony-dashboard", level="WARNING"):
            self.assertEqual(cd.VisitorLog(path=self.path).stats()["total_unique"], 0)


class SystemTests(unittest.TestCase):
    def test_cpu_usage_needs_no_sleep_after_first_call(self):
        sampler = cd.CpuSampler()
        sampler.sample()
        with mock.patch.object(cd.time, "sleep",
                               side_effect=AssertionError("slept")):
            cores = sampler.sample()
        self.assertTrue(cores)
        self.assertTrue(all(0.0 <= c["usage"] <= 100.0 for c in cores))

    def test_uptime_wording(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="93784.21 1.0\n")):
            self.assertEqual(cd.get_uptime(), "up 1 day, 2 hours, 3 minutes")


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self._proxies = set(cd.TRUSTED_PROXIES)
        cd.TRUSTED_PROXIES.clear()

    def tearDown(self):
        cd.TRUSTED_PROXIES.clear()
        cd.TRUSTED_PROXIES.update(self._proxies)

    def test_direct_viewers_are_judged_by_their_own_address(self):
        self.assertTrue(cd.viewer_is_local("192.168.1.20", {}))
        self.assertTrue(cd.viewer_is_local("127.0.0.1", {}))
        self.assertFalse(cd.viewer_is_local("203.0.113.7", {}))

    def test_unknown_proxy_is_treated_as_public(self):
        # A proxy we were not told about: its own address is private, but the
        # person behind it could be anyone.
        self.assertFalse(cd.viewer_is_local("192.168.1.5", {"X-Real-IP": "192.168.1.20"}))
        self.assertFalse(cd.viewer_is_local("192.168.1.5", {"X-Forwarded-For": "10.0.0.9"}))

    def test_trusted_proxy_passes_on_the_real_address(self):
        cd.TRUSTED_PROXIES.add("192.168.1.5")
        self.assertTrue(cd.viewer_is_local("192.168.1.5", {"X-Real-IP": "192.168.1.20"}))
        self.assertFalse(cd.viewer_is_local("192.168.1.5", {"X-Real-IP": "203.0.113.7"}))
        self.assertFalse(cd.viewer_is_local("192.168.1.5", {}))            # no header
        self.assertFalse(cd.viewer_is_local("192.168.1.5", {"X-Real-IP": "junk"}))

    def test_mask_address(self):
        self.assertEqual(cd.mask_address("203.0.113.57"), "203.0.113.x")
        self.assertEqual(cd.mask_address("2001:db8:1:2::5"), "2001:db8:1::x")
        self.assertEqual(cd.mask_address("not-an-address"), "hidden")

    def test_redact_hides_clients_address_and_position(self):
        data = {"clients": [{"host": "203.0.113.57", "ntp": "9"}],
                "system": {"ip": "192.168.1.2", "hostname": "time"},
                "gps": {"lat": 37.1, "lon": -122.2, "alt": 12.5, "fix_mode": "3D"}}
        out = cd.redact(data)
        self.assertTrue(out["redacted"])
        self.assertEqual(out["clients"], [{"host": "203.0.113.x", "ntp": "9"}])
        self.assertEqual(out["system"], {"ip": "\u2014", "hostname": "time"})
        self.assertEqual((out["gps"]["lat"], out["gps"]["lon"], out["gps"]["alt"]),
                         (None, None, 12.5))
        # The shared snapshot itself is untouched.
        self.assertEqual(data["clients"][0]["host"], "203.0.113.57")
        self.assertEqual(data["gps"]["lat"], 37.1)


class LoggingTests(unittest.TestCase):
    def test_recurring_warning_is_logged_once_per_interval(self):
        cd._warned.pop("test-key", None)
        with self.assertLogs("chrony-dashboard", level="WARNING") as logs:
            cd.warn("test-key", "first")
            cd.warn("test-key", "second")          # suppressed
            cd.warn("test-key", "third", every=0)  # interval over
        self.assertEqual([r.getMessage() for r in logs.records], ["first", "third"])

    def test_missing_command_is_logged_not_hidden(self):
        cd._warned.clear()
        with self.assertLogs("chrony-dashboard", level="WARNING") as logs:
            self.assertEqual(cd.run(["/nonexistent/chronyc", "tracking"]), "")
        self.assertIn("/nonexistent/chronyc tracking failed", logs.output[0])


class HttpTests(unittest.TestCase):
    """The real server on a local port, with a canned snapshot."""

    SNAPSHOT = {
        "health": {"status": "DEGRADED", "issues": ["GPS fix: no fix"]},
        "tracking": {"reference_id": "A29FC801 (162.159.200.1)"},
        "clients": [{"host": "203.0.113.57", "ntp": "9", "drops": "0", "last_query": "5"}],
        "system": {"ip": "192.168.1.2", "hostname": "time"},
        "gps": {"lat": 37.1, "lon": -122.2},
    }

    @classmethod
    def setUpClass(cls):
        cd.load_page(hostname="test<host>", tz="Europe/Berlin")
        cls.server = cd.DashboardServer(("127.0.0.1", 0), cd.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.snapshot = json.loads(json.dumps(self.SNAPSHOT))
        for target, replacement in (
                ("get", lambda: dict(self.snapshot, visitors={})),):
            patcher = mock.patch.object(cd.STATUS_CACHE, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cd, "record_visitor", lambda ip, ua: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, path, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_page_is_filled_in_and_links_versioned_assets(self):
        status, headers, body = self.get("/")
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("test&lt;host&gt;", page)            # hostname is escaped
        self.assertIn('data-timezone="Europe/Berlin"', page)
        self.assertIn(f"dashboard v{cd.__version__}", page)
        for placeholder in ("{hostname}", "{timezone}", "{css_version}", "{js_version}",
                            "{version}"):
            self.assertNotIn(placeholder, page)
        self.assertNotIn("<script>", page)                  # no inline script
        csp = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self';", csp)
        version = cd.ASSETS["/static/dashboard.js"]["version"]
        self.assertIn(f"/static/dashboard.js?v={version}", page)

    def test_assets(self):
        version = cd.ASSETS["/static/dashboard.css"]["version"]
        status, headers, body = self.get(f"/static/dashboard.css?v={version}")
        self.assertEqual(status, 200)
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(body, cd.ASSETS["/static/dashboard.css"]["body"])
        # An old or missing version must not be cached for good.
        self.assertEqual(self.get("/static/dashboard.css?v=old")[1]["Cache-Control"], "no-cache")
        gz = self.get("/static/dashboard.js", {"Accept-Encoding": "gzip"})
        self.assertEqual(gz[1]["Content-Encoding"], "gzip")

    def test_only_listed_files_are_served(self):
        for path in ("/static/index.html", "/static/../chrony_dashboard.py",
                     "/static/", "/chrony_dashboard.py", "/nope"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_health_strict_mode_uses_the_http_status(self):
        status, _, body = self.get("/api/health")
        self.assertEqual((status, json.loads(body)["status"]), (200, "DEGRADED"))
        status, _, body = self.get("/api/health?strict=1")
        self.assertEqual((status, json.loads(body)["status"]), (503, "DEGRADED"))
        self.snapshot["health"] = {"status": "OK", "issues": []}
        self.assertEqual(self.get("/api/health?strict=1")[0], 200)

    def test_status_is_full_for_local_viewers_and_redacted_for_others(self):
        local = json.loads(self.get("/api/status")[2])
        self.assertNotIn("redacted", local)
        self.assertEqual(local["clients"][0]["host"], "203.0.113.57")
        public = json.loads(self.get("/api/status", {"X-Real-IP": "198.51.100.9"})[2])
        self.assertTrue(public["redacted"])
        self.assertEqual(public["clients"][0]["host"], "203.0.113.x")
        self.assertIsNone(public["gps"]["lat"])


if __name__ == "__main__":
    unittest.main()
