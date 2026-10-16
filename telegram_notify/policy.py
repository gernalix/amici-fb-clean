"""Content-free, process-shared policy for optional noisy-bot alerts."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable


SEVERITIES = {"info", "warning", "critical", "reminder"}


def policy_db_path() -> Path:
    configured = os.environ.get("TELEGRAM_NOTIFY_POLICY_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_home / "telegram_notify" / "policy.sqlite"


def _counter(db: sqlite3.Connection, name: str) -> None:
    db.execute(
        "INSERT INTO counters(name, value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value=value+1",
        (name,),
    )


def dispatch(
    *,
    event_key: str,
    severity: str,
    title: str,
    message: str,
    sender: Callable[[str, str], None],
    no_op: bool = False,
    cooldown_seconds: int = 3600,
    min_interval_seconds: int = 0,
    state_path: Path | None = None,
    now: float | None = None,
) -> str:
    """Return sent, grouped or suppressed; retain only key digests and counts.

    Critical alerts and reminders always pass. A repeated ordinary event is
    folded into the next delivery after cooldown, including its suppressed
    count. Transport failures roll back state so producers can retry.
    """
    if severity not in SEVERITIES:
        raise ValueError("unsupported alert severity")
    if not event_key.strip():
        raise ValueError("event_key is required")
    if cooldown_seconds < 0 or min_interval_seconds < 0:
        raise ValueError("intervals must be non-negative")
    path = state_path or policy_db_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        db = sqlite3.connect(path, timeout=30)
    finally:
        os.umask(old_umask)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, last_sent REAL NOT NULL, suppressed INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL NOT NULL)")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        if no_op:
            _counter(db, "normal_suppressed")
            db.commit()
            return "suppressed"
        stamp = time.time() if now is None else now
        key = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
        row = db.execute("SELECT last_sent, suppressed FROM events WHERE key=?", (key,)).fetchone()
        previous_sent, repeats = row if row else (float("-inf"), 0)
        last_global = db.execute("SELECT value FROM meta WHERE key='last_ordinary_sent'").fetchone()
        ordinary = severity in {"info", "warning"}
        if ordinary and (stamp - previous_sent < cooldown_seconds or
                         last_global is not None and stamp - last_global[0] < min_interval_seconds):
            db.execute(
                "INSERT INTO events(key,last_sent,suppressed) VALUES (?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET suppressed=suppressed+1",
                (key, previous_sent if row else 0),
            )
            _counter(db, "suppressed")
            db.commit()
            return "suppressed"
        if ordinary and repeats:
            message = f"{message}\n\nRipetuto altre {repeats} volte dall'ultimo avviso."
        sender(title, message)
        db.execute(
            "INSERT INTO events(key,last_sent,suppressed) VALUES (?,?,0) "
            "ON CONFLICT(key) DO UPDATE SET last_sent=excluded.last_sent,suppressed=0",
            (key, stamp),
        )
        if ordinary:
            db.execute(
                "INSERT INTO meta(key,value) VALUES ('last_ordinary_sent',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (stamp,)
            )
        _counter(db, "sent")
        if repeats and ordinary:
            _counter(db, "grouped")
        if severity == "critical":
            _counter(db, "critical_sent")
        db.commit()
        return "grouped" if repeats and ordinary else "sent"
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def counters(state_path: Path | None = None) -> dict[str, int]:
    path = state_path or policy_db_path()
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return dict(db.execute("SELECT name,value FROM counters ORDER BY name"))
