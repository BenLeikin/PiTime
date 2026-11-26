"""Tests for the dashboard's parsers, using recorded chronyc/gpsd output."""

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
^,?,10.0.0.16,0,6,0,4294967295,0.000000000,0.000000000,0.000000000
^,+,162.159.200.1,3,9,377,352,-0.010559363,-0.010559363,0.023447527
^,*,162.159.200.123,3,10,377,940,-0.009622829,-0.010018874,0.026427295
#,*,PPS,0,4,377,15,0.000000012,0.000000020,0.000000101
#,?,NMEA,0,4,377,14,0.081234567,0.081234567,0.100000000
"""

SOURCESTATS_CSV = """\
10.0.0.16,0,0,0,0.000,2000.000,0.000000000,4.000000000
162.159.200.1,6,3,7222,-1.387,3.784,-0.007031661,0.002260464
162.159.200.123,32,13,28964,0.163,0.181,0.001009292,0.002714957
"""

CLIENTS_CSV = """\
192.168.1.20,1234,5,6,-,17,0,0,-,4294967295
2001:db8::1,42,0,10,-,3600,0,0,-,4294967295
"""

REFCLOCKS_LOG = """\
===============================================================================
   Date (UTC) Time         Refid  DP L P  Raw offset   Cooked offset      Disp.
===============================================================================
2026-11-25 10:00:00.000000 NMEA    0 N 0  8.12345e-02   8.12345e-02  1.000e-03
2026-11-25 10:00:00.000000 PPS     0 N 0  1.23456e-07  -4.20000e-08  1.000e-09
2026-11-25 10:00:01.000000 PPS     0 N 0  1.33456e-07   3.10000e-08  1.000e-09
2026-11-25 10:00:02.000000 PPS     0 N 0  1.13456e-07  -1.00000e-09  1.000e-09
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
        self.assertEqual(nmea["state"], "unreachable")

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

    def test_reads_cooked_offsets_for_refid_only(self):
        self.assertEqual(cd.parse_pps_history("PPS", self.path),
                         [-4.2e-08, 3.1e-08, -1e-09])

    def test_other_refid(self):
        self.assertEqual(cd.parse_pps_history("NMEA", self.path), [0.0812345])

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
            try:
                conn.recv(4096)
            except OSError:
                pass

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


if __name__ == "__main__":
    unittest.main()
