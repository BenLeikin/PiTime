"""Tests for the dashboard's parsers, using recorded chronyc/gpsd output."""

import contextlib
import importlib.util
import json
import math
import os
import socket
import tempfile
import threading
import time
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


class PpsSamplerTests(unittest.TestCase):
    ROW = "#,*,PPS,0,4,377,{last_rx},{offset},0.000000020,0.000000101\n"

    def poll(self, sampler, last_rx, offset, now):
        out = self.ROW.format(last_rx=last_rx, offset=offset)
        with mock.patch.object(cd, "run", fake_run({"sources": out})), \
             mock.patch.object(cd.time, "time", return_value=now):
            return sampler.poll("PPS")

    def test_each_chrony_sample_is_taken_once(self):
        sampler = cd.PpsSampler()
        self.assertTrue(self.poll(sampler, 2, "0.000000012", 1000.0))   # taken at 998
        self.assertFalse(self.poll(sampler, 6, "0.000000012", 1004.0))  # same sample
        self.assertFalse(self.poll(sampler, 10, "0.000000012", 1008.3)) # still the same
        self.assertTrue(self.poll(sampler, 2, "-0.000000040", 1016.0))  # next poll
        self.assertEqual(sampler.history("PPS"), [1.2e-08, -4e-08])

    def test_silent_source_adds_nothing(self):
        sampler = cd.PpsSampler()
        self.assertFalse(self.poll(sampler, 4294967295, "0.0", 1000.0))
        with mock.patch.object(cd, "parse_pps_history", return_value=[1e-9]) as fallback:
            self.assertEqual(sampler.history("PPS"), [1e-9])   # falls back to the log
        fallback.assert_called_once_with("PPS")

    def test_keeps_the_latest_sixty(self):
        sampler = cd.PpsSampler()
        for i in range(70):
            self.poll(sampler, 1, f"{i}e-9", 1000.0 + 16 * i)
        history = sampler.history("PPS")
        self.assertEqual(len(history), 60)
        self.assertAlmostEqual(history[-1], 69e-9)


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


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "history.sqlite3")
        self.addCleanup(lambda: [os.unlink(os.path.join(self.dir, n)) for n in os.listdir(self.dir)]
                        and None or os.rmdir(self.dir))

    def row(self, **values):
        base = dict.fromkeys(name for name, _ in cd.HISTORY_FIELDS)
        base.update(values)
        return base

    # Timestamps are recent: anything older than keep_days is pruned on write.
    NOW = int(time.time())

    def test_samples_are_written_in_batches(self):
        store = cd.HistoryStore(path=self.path, flush_s=600)
        store.add(self.row(sats_used=5), ts=self.NOW - 120)
        store.add(self.row(sats_used=6), ts=self.NOW - 60)
        self.assertFalse(os.path.exists(self.path))         # nothing written yet
        # ...but queries already see them.
        result = store.query(600, points=1, now=self.NOW)
        self.assertEqual(result["fields"]["sats_used"], [5.5])
        store.flush()
        self.assertTrue(os.path.exists(self.path))
        reopened = cd.HistoryStore(path=self.path)
        self.assertEqual(reopened.query(600, points=1, now=self.NOW)["fields"]["sats_used"], [5.5])

    def test_buckets_average_and_health_keeps_the_worst(self):
        store = cd.HistoryStore(path=self.path, flush_s=0)
        start = self.NOW - 240
        for i, (health, temp) in enumerate([(0, 40.0), (2, 42.0), (0, 44.0), (1, 50.0)]):
            store.add(self.row(health=health, temperature_c=temp), ts=start + i * 60)
        result = store.query(240, points=2, now=self.NOW)
        self.assertEqual(result["step"], 120)
        self.assertEqual(result["fields"]["temperature_c"], [41.0, 47.0])
        self.assertEqual(result["fields"]["health"], [2, 1])

    def test_empty_buckets_are_none(self):
        store = cd.HistoryStore(path=self.path, flush_s=0)
        store.add(self.row(sats_used=4), ts=self.NOW - 400)
        result = store.query(400, points=4, now=self.NOW)
        self.assertEqual(result["fields"]["sats_used"], [4.0, None, None, None])

    def test_old_rows_are_pruned(self):
        store = cd.HistoryStore(path=self.path, keep_days=1, flush_s=0)
        now = time.time()
        store.add(self.row(sats_used=1), ts=now - 3 * 86400)
        store._last_prune = 0.0
        store.add(self.row(sats_used=2), ts=now)
        result = store.query(5 * 86400, points=5, now=now)
        self.assertEqual([v for v in result["fields"]["sats_used"] if v is not None], [2.0])

    def test_unwritable_file_is_logged_and_samples_are_kept(self):
        store = cd.HistoryStore(path="/nonexistent/dir/history.sqlite3", flush_s=0)
        cd._warned.pop("history", None)
        with self.assertLogs("chrony-dashboard", level="WARNING"):
            store.add(self.row(sats_used=3), ts=self.NOW)
        self.assertEqual(len(store._pending), 1)

    def test_sample_from_a_snapshot(self):
        snapshot = {
            "tracking": {"stratum": "1", "last_offset_s": 1e-7, "rms_offset_s": 2e-7,
                         "frequency_ppm": 8.7, "skew_ppm": 0.01},
            "sources": [{"name": "PPS", "reach": "377", "offset_s": -4e-8},
                        {"name": "NMEA", "reach": "377", "offset_s": 0.002}],
            "system": {"temperature_c": 41.8},
            "gps": {"available": True, "sats_used": 5, "sats_visible": 16,
                    "best_snr": 27, "fix_mode": "3D"},
            "health": {"status": "DEGRADED"},
        }
        row = cd.history_sample(snapshot)
        self.assertEqual(row["stratum"], 1)
        self.assertEqual(row["pps_offset_s"], -4e-8)
        self.assertEqual(row["pps_reach"], 255)
        self.assertEqual((row["fix"], row["health"]), (3, 1))
        # A silent PPS has no offset worth plotting.
        snapshot["sources"][0]["reach"] = "0"
        self.assertIsNone(cd.history_sample(snapshot)["pps_offset_s"])
        # Without gpsd, GPS fields are unknown rather than zero.
        snapshot["gps"] = {"available": False}
        row = cd.history_sample(snapshot)
        self.assertEqual((row["sats_used"], row["best_snr"], row["fix"]), (None, None, None))


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path


class NetworkLinkTests(TempDirTest):
    ROUTE = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
             "wlan0\t00000000\t0100000A\t0003\t0\t0\t600\t00000000\n"
             "end0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\n"
             "end0\t0000000A\t00000000\t0001\t0\t0\t100\t00FCFFFF\n")
    WIRELESS = ("Inter-| sta-|   Quality        |   Discarded packets\n"
                " face | tus | link level noise |  nwid  crypt   frag\n"
                " wlan0: 0000   70.  -38.  -256        0      0      0\n")

    def link(self, route):
        os.makedirs(os.path.join(self.dir, "net", "wlan0", "wireless"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "net", "end0"), exist_ok=True)
        return cd.get_network_link(self.write("route", route), self.write("wireless", self.WIRELESS),
                                   os.path.join(self.dir, "net"))

    def test_cable_preferred_by_metric(self):
        self.assertEqual(self.link(self.ROUTE), {"iface": "end0", "kind": "cable", "signal_dbm": None})

    def test_wifi_with_signal(self):
        wifi_only = "\n".join(line for line in self.ROUTE.splitlines() if not line.startswith("end0"))
        self.assertEqual(self.link(wifi_only + "\n"),
                         {"iface": "wlan0", "kind": "Wi-Fi", "signal_dbm": -38.0})

    def test_no_default_route(self):
        self.assertEqual(self.link(self.ROUTE.splitlines()[0] + "\n")["kind"], "none")


class ExpiryTests(TempDirTest):
    def test_cert_path_from_chrony_config_and_conf_d(self):
        main = self.write("chrony.conf", "server x\nconfdir /etc/chrony/conf.d\n")
        self.write("conf.d/site.conf", "ntsserverkey /k.key\nntsservercert /etc/chrony/nts/full.crt\n")
        self.write("conf.d/ignored.txt", "ntsservercert /wrong\n")
        self.assertEqual(cd.nts_cert_path((main, os.path.join(self.dir, "conf.d"))),
                         "/etc/chrony/nts/full.crt")
        self.assertIsNone(cd.nts_cert_path((self.write("other.conf", "server x\n"),)))

    def test_cert_expiry_parses_openssl(self):
        with mock.patch.object(cd, "run", return_value="notAfter=Jan 28 23:59:59 2027 GMT"):
            when = cd.cert_expiry("/x.crt")
        self.assertEqual(cd.datetime.fromtimestamp(when, tz=cd.UTC).strftime("%Y-%m-%d %H:%M"),
                         "2027-01-28 23:59")
        cd._warned.pop("nts-cert", None)
        with mock.patch.object(cd, "run", return_value=""), \
             self.assertLogs("chrony-dashboard", level="WARNING"):
            self.assertIsNone(cd.cert_expiry("/missing.crt"))

    def test_health_warns_before_expiry(self):
        tracking = {"stratum": "1", "reference_id": "50505300 (PPS)"}
        sources = [{"name": "PPS", "state": "synced", "reach": "377"}]
        gps = {"available": True, "fix_mode": "3D", "sats_used": 6}
        far = {"nts_cert": {"expires": "2027-01-28", "days": 63},
               "leap_table": {"expires": "2027-06-28", "days": 213}}
        self.assertEqual(cd.compute_health(tracking, sources, gps, far)["status"], "OK")
        near = {"nts_cert": {"expires": "2026-12-10", "days": 14}, "leap_table": None}
        self.assertEqual(cd.compute_health(tracking, sources, gps, near),
                         {"status": "DEGRADED", "issues": ["NTS certificate expires in 14 days"]})
        gone = {"nts_cert": None, "leap_table": {"expires": "2026-11-01", "days": -25}}
        self.assertEqual(cd.compute_health(tracking, sources, gps, gone)["issues"],
                         ["Leap-second table expired 25 days ago"])


class SyncTrackerTests(unittest.TestCase):
    ON = {"reference_id": "50505300 (PPS)", "root_delay_s": 1e-9, "root_dispersion_s": 8e-6}
    OFF = {"reference_id": "A29FC87B (162.159.200.123)", "root_delay_s": 0.02,
           "root_dispersion_s": 0.003}

    def test_tracks_last_time_on_gps(self):
        sync = cd.SyncTracker()
        first = sync.note(self.ON, "PPS", now=1000)
        self.assertEqual((first["on_gps"], first["since_exact"]), (True, False))
        sync.note(self.ON, "PPS", now=1060)
        lost = sync.note(self.OFF, "PPS", now=1120)
        self.assertEqual((lost["on_gps"], lost["since"], lost["since_exact"], lost["last_on_gps"]),
                         (False, 1120, True, 1060))
        self.assertAlmostEqual(lost["error_bound_s"], 0.013)   # 3 ms + 20 ms / 2
        self.assertEqual(sync.note(self.ON, "PPS", now=1200)["since"], 1200)

    def test_selected_but_silent_pps_is_not_on_gps(self):
        sync = cd.SyncTracker()
        sync.note(self.ON, "PPS", [{"name": "PPS", "reach": "377"}], now=1000)
        silent = sync.note(self.ON, "PPS", [{"name": "PPS", "reach": "0"}], now=1060)
        self.assertEqual((silent["on_gps"], silent["last_on_gps"]), (False, 1000))

    def test_other_refclock_names_are_not_gps(self):
        self.assertFalse(cd.SyncTracker().note({"reference_id": "50505300 (PPSX)"}, "PPS")["on_gps"])


class TrafficTests(unittest.TestCase):
    def test_deltas_and_restart(self):
        counters = cd.CounterDeltas()
        self.assertIsNone(counters.delta("ntp", "100"))      # first reading
        self.assertEqual(counters.delta("ntp", "130"), 30)
        self.assertEqual(counters.delta("ntp", "5"), 5)       # chronyd restarted
        self.assertIsNone(counters.delta("ntp", "n/a"))


class HistorySummaryTests(TempDirTest):
    NOW = int(time.time()) // 60 * 60

    def store(self, strata, start=None, temps=None, freqs=None):
        store = cd.HistoryStore(path=os.path.join(self.dir, "h.sqlite3"), flush_s=3600)
        start = start if start is not None else self.NOW - 60 * len(strata)
        for i, st in enumerate(strata):
            row = dict.fromkeys(name for name, _ in cd.HISTORY_FIELDS)
            row["stratum"] = st
            if temps:
                row["temperature_c"], row["frequency_ppm"] = temps[i], freqs[i]
            store.add(row, ts=start + 60 * i)
        return store

    def test_on_gps_share_and_outages(self):
        strata = [1] * 50 + [3] * 10 + [1] * 30 + [None] * 5 + [2] * 5
        summary = self.store(strata).summary(3600 * 2, now=self.NOW)
        self.assertEqual(summary["minutes_recorded"], 95)
        self.assertEqual(summary["on_gps_pct"], round(100 * 80 / 95, 2))
        self.assertEqual(summary["outages"], 2)
        self.assertEqual(summary["longest_outage_s"], 600)
        self.assertIsNotNone(summary["ongoing_outage_since"])

    def test_a_gap_in_recording_is_not_an_outage(self):
        store = self.store([1] * 10)
        row = dict.fromkeys(name for name, _ in cd.HISTORY_FIELDS)
        row["stratum"] = 1
        store.add(row, ts=self.NOW + 3600)          # an hour later, still on GPS
        summary = store.summary(7200, now=self.NOW + 3600)
        self.assertEqual((summary["outages"], summary["on_gps_pct"]), (0, 100.0))

    def test_no_data(self):
        summary = self.store([]).summary(3600, now=self.NOW)
        self.assertIsNone(summary["on_gps_pct"])

    def test_ageing_removes_temperature_and_finds_the_trend(self):
        # Four days at -0.2 ppm/C, and the crystal ageing +0.03 ppm per day.
        temps, freqs, strata = [], [], []
        for minute in range(4 * 1440):
            t = 40 + 3 * math.sin(2 * math.pi * minute / 1440)
            temps.append(t)
            freqs.append(9.0 - 0.2 * (t - 40) + 0.03 * minute / 1440)
            strata.append(1)
        start = (self.NOW // 86400 - 4) * 86400
        ageing = self.store(strata, start=start, temps=temps, freqs=freqs).ageing(
            days=30, now=start + 4 * 86400)
        self.assertAlmostEqual(ageing["temp_coeff_ppm_per_c"], -0.2, places=2)
        self.assertEqual(len(ageing["daily"]), 4)
        self.assertAlmostEqual(ageing["trend_ppm_per_month"], 0.9, places=1)

    def test_ageing_needs_data(self):
        self.assertIsNone(self.store([1] * 30).ageing(now=self.NOW)["temp_coeff_ppm_per_c"])


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

    def test_history_endpoint(self):
        with mock.patch.object(cd.HISTORY, "query",
                               lambda seconds: {"start": 0, "step": seconds / 360,
                                                "fields": {"health": [0] * 360}}):
            cd._history_cache.clear()
            status, _, body = self.get("/api/history?range=7d")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["range"], "7d")
            self.assertEqual(json.loads(body)["step"], 7 * 86400 / 360)
            self.assertEqual(self.get("/api/history?range=1y")[0], 400)

    def test_time_endpoint(self):
        before = time.time()
        status, headers, body = self.get("/api/time")
        now = json.loads(body)["now"]
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(before <= now <= time.time())

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
