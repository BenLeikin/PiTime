"""Tests for scripts/health_monitor.py: when it speaks up and when it stays quiet."""

import importlib.util
import unittest
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
