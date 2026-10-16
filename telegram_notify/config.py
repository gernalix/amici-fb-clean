"""Configuration loading for the reusable Telegram notification package."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


TOKEN_ENV_NAMES = (
    "TELEGRAM_INSERT_BOT_NOISY_BOT_TOKEN",
    "NOISY_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
)
CHAT_ENV_NAMES = (
    "TELEGRAM_INSERT_BOT_NOISY_CHAT_ID",
    "NOISY_TELEGRAM_CHAT_ID",
    "TELEGRAM_CHAT_ID",
)
CONFIG_PATH_ENV = "TELEGRAM_NOTIFY_CONFIG"
DEFAULT_USER_CONFIG_PATH = Path.home() / ".config" / "codex" / "secrets" / "telegram.env"


def config_paths() -> list[Path]:
    configured = os.getenv(CONFIG_PATH_ENV, "").strip()
    if configured:
        return [Path(configured).expanduser()]
    return [DEFAULT_USER_CONFIG_PATH]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config_files() -> None:
    for path in config_paths():
        _load_env_file(path)


def first_env(names: Iterable[str]) -> tuple[str, str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return name, value
    return "", ""


def get_token() -> str:
    _, token = first_env(TOKEN_ENV_NAMES)
    if not token:
        raise RuntimeError(
            "Missing Telegram configuration: TELEGRAM_INSERT_BOT_NOISY_BOT_TOKEN, "
            "NOISY_TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN must be set."
        )
    return token


def get_default_chat_id() -> str:
    _, chat_id = first_env(CHAT_ENV_NAMES)
    if not chat_id:
        raise RuntimeError(
            "Missing Telegram recipient: pass chat_id or set "
            "TELEGRAM_INSERT_BOT_NOISY_CHAT_ID, NOISY_TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID."
        )
    return chat_id


def get_config() -> tuple[str, str]:
    return get_token(), get_default_chat_id()


def validate_config() -> None:
    get_config()


def _status(name: str, value: str) -> str:
    if not value:
        return f"{name or 'missing'}:missing"
    return f"{name}:configured"


def config_summary() -> str:
    token_name, token = first_env(TOKEN_ENV_NAMES)
    chat_name, chat_id = first_env(CHAT_ENV_NAMES)
    paths = ",".join(str(path) for path in config_paths())
    return (
        f"token={_status(token_name, token)} "
        f"chat_id={_status(chat_name, chat_id)} "
        f"config_paths={paths}"
    )


load_config_files()
