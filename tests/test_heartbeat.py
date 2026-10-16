import os
import tempfile
import unittest
from pathlib import Path

from amici_fb_health import HEARTBEAT_SECONDS, STAMP_NAME, heartbeat_due, mark_telegram_sent


class TelegramHeartbeatTests(unittest.TestCase):
    def test_missing_stamp_is_due(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(heartbeat_due(Path(directory), now=1000))

    def test_stamp_avoids_noise_until_seven_days_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mark_telegram_sent(root)
            stamp = root / STAMP_NAME
            os.utime(stamp, (1000, 1000))
            self.assertFalse(heartbeat_due(root, now=1000 + HEARTBEAT_SECONDS - 1))
            self.assertTrue(heartbeat_due(root, now=1000 + HEARTBEAT_SECONDS))

    def test_marking_success_refreshes_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mark_telegram_sent(root)
            self.assertFalse(heartbeat_due(root))


if __name__ == "__main__":
    unittest.main()
