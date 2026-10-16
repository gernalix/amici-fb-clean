"""Persist the last successful Telegram activity for low-noise heartbeats."""

from __future__ import annotations

import time
from pathlib import Path

HEARTBEAT_SECONDS = 7 * 24 * 60 * 60
STAMP_NAME = ".last_telegram_notification"


def heartbeat_due(output_dir: Path, now: float | None = None) -> bool:
    stamp = output_dir / STAMP_NAME
    try:
        last_sent = stamp.stat().st_mtime
    except FileNotFoundError:
        return True
    return (time.time() if now is None else now) - last_sent >= HEARTBEAT_SECONDS


def mark_telegram_sent(output_dir: Path) -> None:
    (output_dir / STAMP_NAME).touch()
