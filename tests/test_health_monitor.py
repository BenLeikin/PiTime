"""Tests for scripts/health_monitor.py: when it speaks up and when it stays quiet."""

import importlib.util
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "health_monitor", ROOT / "scripts" / "health_monitor.py")
hm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hm)

MIN = 60


class DecideTests(unittest.TestCase):
    def run_sequence(self, observations, threshold=3, remind_s=24 * 3600, step=5 * MIN):
        """Feed (state, issues) pairs, one per run; return the messages sent."""
        stored, messages, now = {}, [], 1_000_000
        for state, issues in observations:
            stored, message = hm.decide(stored, state, issues, now, "time server",
                                        threshold, remind_s)
            messages.append(message)
            now += step
        return messages

    def test_healthy_server_is_silent(self):
        self.assertEqual(self.run_sequence([("OK", [])] * 6), [None] * 6)

    def test_first_run_reports_an_existing_problem(self):
        messages = self.run_sequence([("DEGRADED", ["GPS fix: no fix"])])
        self.assertEqual(messages, ["[ALERT] time server: DEGRADED - GPS fix: no fix"])

    def test_a_restart_blip_is_not_reported(self):
        blip = [("OK", []), ("FAULT", ["No synced source"]), ("FAULT", []), ("OK", [])]
        self.assertEqual(self.run_sequence(blip), [None] * 4)

    def test_a_lasting_problem_is_reported_once_then_recovery(self):
        bad = ("DEGRADED", ["Not synced to GPS"])
        messages = self.run_sequence([("OK", [])] + [bad] * 5 + [("OK", [])] * 4)
        self.assertEqual(messages[:3], [None, None, None])
        self.assertEqual(messages[3], "[ALERT] time server: DEGRADED (was OK) - Not synced to GPS")
        self.assertEqual(messages[4:8], [None] * 4)
        # Recovery is reported after it too has lasted three runs.
        self.assertEqual(messages[8], "[OK] time server: healthy again (was DEGRADED for 25 min)")
        self.assertEqual(messages[9], None)

    def test_reminder_while_still_unhealthy(self):
        bad = ("UNREACHABLE", ["dashboard did not answer: timed out"])
        messages = self.run_sequence([bad] * 5, remind_s=10 * MIN)
        self.assertTrue(messages[0].startswith("[ALERT] time server: UNREACHABLE - "))
        self.assertEqual(messages[1], None)
        self.assertTrue(messages[2].startswith("[ALERT] time server: still UNREACHABLE after 10 min"))
        self.assertEqual(messages[3], None)
        self.assertIsNotNone(messages[4])

    def test_change_between_two_bad_states_is_reported(self):
        messages = self.run_sequence(
            [("DEGRADED", ["a"])] + [("FAULT", ["No synced source"])] * 3)
        self.assertEqual(messages[3], "[ALERT] time server: FAULT (was DEGRADED) - No synced source")


def answers(**by_host):
    """A fake ntp_query: host -> offset in seconds, or an exception to raise."""
    def query(host):
        result = by_host[host]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, dict):
            return result
        return {"offset": result, "delay": 0.01, "stratum": 2, "leap": 0}
    return query


class NtpCheckTests(unittest.TestCase):
    REFS = ("ref-a", "ref-b", "ref-c")

    def test_agreeing_server_is_fine(self):
        query = answers(pi=0.0021, **{"ref-a": 0.0019, "ref-b": 0.0030, "ref-c": 0.0012})
        self.assertEqual(hm.check_ntp("pi", self.REFS, 0.010, query), [])

    def test_wrong_second_is_caught(self):
        # Healthy by its own account, but a whole second out.
        query = answers(pi=1.0003, **{"ref-a": 0.0, "ref-b": 0.001, "ref-c": -0.001})
        self.assertEqual(hm.check_ntp("pi", self.REFS, 0.010, query),
                         ["NTP time is +1000.3 ms from 3 outside servers"])

    def test_one_bad_reference_does_not_cause_an_alert(self):
        query = answers(pi=0.0, **{"ref-a": 0.0, "ref-b": 0.5, "ref-c": 0.001})
        self.assertEqual(hm.check_ntp("pi", self.REFS, 0.010, query), [])

    def test_unreachable_references_are_skipped(self):
        query = answers(pi=0.0, **{r: OSError("timed out") for r in self.REFS})
        self.assertEqual(hm.check_ntp("pi", self.REFS, 0.010, query), [])

    def test_server_problems(self):
        self.assertEqual(hm.check_ntp("pi", (), 0.01, answers(pi=OSError("timed out"))),
                         ["NTP did not answer: timed out"])
        unsynced = {"offset": 0.0, "delay": 0.01, "stratum": 16, "leap": 3}
        self.assertEqual(hm.check_ntp("pi", self.REFS, 0.01, answers(pi=unsynced)),
                         ["NTP says it is not synchronised"])

    def test_offset_from_the_four_timestamps(self):
        # Server 2 s ahead, 100 ms each way: offset 2 s, round trip 200 ms.
        def ts(t):
            whole = int(t) + hm.NTP_EPOCH_OFFSET
            return hm.struct.pack("!II", whole, int((t - int(t)) * 2**32))
        reply = bytearray(48)
        reply[0], reply[1] = 0x24, 1
        reply[32:40] = ts(1002.1)       # server receive
        reply[40:48] = ts(1002.1)       # server transmit
        clock = iter([1000.0, 1000.2])  # client send, client receive

        class FakeSocket:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def settimeout(self, t): pass
            def sendto(self, data, addr): pass
            def recvfrom(self, n): return bytes(reply), None

        with mock.patch.object(hm.socket, "socket", FakeSocket), \
             mock.patch.object(hm.time, "time", lambda: next(clock)):
            result = hm.ntp_query("pi")
        self.assertAlmostEqual(result["offset"], 2.0, places=6)
        self.assertAlmostEqual(result["delay"], 0.2, places=6)
        self.assertEqual(result["stratum"], 1)


class ConfigTests(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        config = hm.load_config("/nonexistent/timeserver-monitor")
        self.assertEqual(config["THRESHOLD"], "3")
        self.assertEqual(config["WEBHOOK_URL"], "")

    def test_example_config_parses(self):
        config = hm.load_config(ROOT / "scripts" / "timeserver-monitor.conf.example")
        self.assertEqual(config["NAME"], "time server")
        self.assertEqual(config["WEBHOOK_URL"], "")
        self.assertEqual(int(config["THRESHOLD"]), 3)

    def test_span(self):
        self.assertEqual(hm.span(25 * MIN), "25 min")
        self.assertEqual(hm.span(130 * MIN), "2 h 10 min")
        self.assertEqual(hm.span(26 * 3600), "1 d 2 h")


if __name__ == "__main__":
    unittest.main()
