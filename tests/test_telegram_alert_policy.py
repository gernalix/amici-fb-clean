import sqlite3
import tempfile
import unittest
from pathlib import Path

from telegram_notify.policy import counters, dispatch


class AlertPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "policy.sqlite"
        self.sent = []

    def call(self, key="event", severity="info", now=100.0, no_op=False, sender=None):
        return dispatch(
            event_key=key,
            severity=severity,
            title="Titolo",
            message="Messaggio",
            sender=sender or (lambda title, message: self.sent.append((title, message))),
            no_op=no_op,
            state_path=self.path,
            now=now,
            cooldown_seconds=60,
            min_interval_seconds=10,
        )

    def test_repeat_and_burst_group_after_cooldown(self):
        self.assertEqual(self.call(), "sent")
        self.assertEqual(self.call(now=101), "suppressed")
        self.assertEqual(self.call(now=102), "suppressed")
        self.assertEqual(self.call(now=161), "grouped")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Ripetuto altre 2 volte", self.sent[-1][1])
        self.assertEqual(counters(self.path), {"grouped": 1, "sent": 2, "suppressed": 2})

    def test_global_spacing_and_noop(self):
        self.assertEqual(self.call(), "sent")
        self.assertEqual(self.call(key="different", now=101), "suppressed")
        self.assertEqual(self.call(key="different", now=111), "grouped")
        self.assertEqual(self.call(key="normal", now=112, no_op=True), "suppressed")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(counters(self.path)["normal_suppressed"], 1)

    def test_critical_and_reminder_bypass_limits(self):
        self.assertEqual(self.call(severity="critical"), "sent")
        self.assertEqual(self.call(severity="critical", now=100), "sent")
        self.assertEqual(self.call(severity="reminder", now=100), "sent")
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(counters(self.path)["critical_sent"], 2)

    def test_failed_transport_retains_retry_and_no_message_content(self):
        def fail(_title, _message):
            raise RuntimeError("network unavailable")

        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            self.call(sender=fail)
        self.assertEqual(self.call(), "sent")
        with sqlite3.connect(self.path) as db:
            stored = " ".join(str(row) for table in ("events", "counters", "meta") for row in db.execute(f"SELECT * FROM {table}"))
        self.assertNotIn("Titolo", stored)
        self.assertNotIn("Messaggio", stored)
        self.assertNotIn("event", stored)


if __name__ == "__main__":
    unittest.main()
