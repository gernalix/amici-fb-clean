#!/usr/bin/env python3
"""Report a final amici-fb systemd failure to the configured Telegram chat."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telegram_notify


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    if args.test:
        body = "TEST: il percorso di allarme per amici_fb funziona. Nessun errore reale."
    else:
        body = (
            "Il controllo amici Facebook e' fallito dopo i tentativi automatici. "
            "Verificare: systemctl status amici-fb.service; "
            "journalctl -u amici-fb.service -n 100."
        )
    telegram_notify.notify(body, title="Allarme monitor", prefix="amici_fb", project_id=1)
    print("amici_fb_failure_alert.sent", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
