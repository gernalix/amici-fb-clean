import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import telegram_notify.config as config
from telegram_notify import notify, send_alert, send_file, send_message, send_text_raw


TOKEN = "123456:TEST_TOKEN_VALUE"
NOISY_TOKEN = "123456:NOISY_TOKEN_VALUE"
DEFAULT_CHAT_ID = "-100DEFAULT"
NOISY_CHAT_ID = "-100NOISY"
EXPLICIT_CHAT_ID = -1001234567890


class TelegramNotifyTests(unittest.TestCase):
    def test_default_secret_path_is_canonical(self) -> None:
        self.assertEqual(
            config.DEFAULT_USER_CONFIG_PATH,
            Path.home() / ".config" / "codex" / "secrets" / "telegram.env",
        )

    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {
                "HOME": os.environ["HOME"],
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "TELEGRAM_CHAT_ID": DEFAULT_CHAT_ID,
                "TELEGRAM_NOTIFY_CONFIG": "/tmp/telegram-notify-test-does-not-exist.env",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def mock_response(self, ok: bool = True, status_code: int = 200, text: str = "OK") -> Mock:
        return Mock(ok=ok, status_code=status_code, text=text)

    def test_explicit_chat_id_overrides_config(self) -> None:
        with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
            notify(chat_id=EXPLICIT_CHAT_ID, message="Test", project_id=1)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], str(EXPLICIT_CHAT_ID))

    def test_falls_back_to_config_chat_id(self) -> None:
        with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
            notify(message="Test", title="Titolo", project_id=1)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], DEFAULT_CHAT_ID)

    def test_existing_noisy_config_overrides_shared_defaults(self) -> None:
        os.environ["TELEGRAM_INSERT_BOT_NOISY_BOT_TOKEN"] = NOISY_TOKEN
        os.environ["TELEGRAM_INSERT_BOT_NOISY_CHAT_ID"] = NOISY_CHAT_ID

        with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
            notify(message="Test", title="Titolo", project_id=1)

        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertIn(NOISY_TOKEN, url)
        self.assertNotIn(TOKEN, url)
        self.assertEqual(payload["chat_id"], NOISY_CHAT_ID)

    def test_missing_explicit_and_config_chat_id_is_clear_error(self) -> None:
        os.environ.pop("TELEGRAM_CHAT_ID")

        with self.assertRaisesRegex(RuntimeError, "TELEGRAM_INSERT_BOT_NOISY_CHAT_ID, NOISY_TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID"):
            notify(message="Test", project_id=1)

    def test_empty_explicit_chat_id_is_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "chat_id must not be empty"):
            notify(chat_id=" ", message="Test", project_id=1)

    def test_token_not_in_http_error_message(self) -> None:
        response = self.mock_response(ok=False, status_code=400, text=f"bad token {TOKEN}")

        with patch("telegram_notify.notify.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                notify(chat_id=EXPLICIT_CHAT_ID, message="Test", project_id=1)

        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("<redacted>", str(ctx.exception))

    def test_token_not_in_request_exception_message(self) -> None:
        error = requests.RequestException(f"https://api.telegram.org/bot{TOKEN}/sendMessage")

        with patch("telegram_notify.notify.requests.post", side_effect=error):
            with self.assertRaises(RuntimeError) as ctx:
                notify(chat_id=EXPLICIT_CHAT_ID, message="Test", project_id=1)

        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("Telegram sendMessage failed", str(ctx.exception))

    def test_old_signatures_remain_compatible(self) -> None:
        with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
            notify(message="Test", title="Titolo", project_id=1)
            send_message("Titolo", "Test", project_id=1)

        self.assertEqual(post.call_count, 2)

    def test_send_alert_uses_shared_policy_without_changing_legacy_transport(self) -> None:
        with patch("telegram_notify.notify.dispatch", return_value="suppressed") as policy:
            self.assertEqual(send_alert("Amici Facebook", "Nessuna variazione", event_key="amici:unchanged", no_op=True), "suppressed")
        self.assertEqual(policy.call_args.kwargs["severity"], "info")
        self.assertTrue(policy.call_args.kwargs["no_op"])

    def test_send_text_raw_uses_explicit_chat_id(self) -> None:
        with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
            send_text_raw("Test", chat_id=EXPLICIT_CHAT_ID, project_id=1)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], str(EXPLICIT_CHAT_ID))
        self.assertEqual(payload["text"], "project_id=1 | Test")

    def test_send_file_uses_explicit_chat_id(self) -> None:
        with tempfile.NamedTemporaryFile() as fh:
            Path(fh.name).write_text("payload")
            with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
                send_file(fh.name, "Titolo", "Test", chat_id=EXPLICIT_CHAT_ID, project_id=1)

        data = post.call_args.kwargs["data"]
        self.assertEqual(data["chat_id"], str(EXPLICIT_CHAT_ID))
        self.assertTrue(data["caption"].startswith("project_id=1 | "))

    def test_project_id_is_optional_and_explicit_id_is_first_visible_content(self) -> None:
        with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
            send_text_raw("Test")
        self.assertEqual(post.call_args.kwargs["json"]["text"], "Test")
        with patch("telegram_notify.notify.requests.post", return_value=self.mock_response()) as post:
            send_message("Titolo", "Test", project_id="1")
        payload = post.call_args.kwargs["json"]
        self.assertTrue(payload["text"].startswith("project_id=1 | "))


if __name__ == "__main__":
    unittest.main()
