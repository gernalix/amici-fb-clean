# telegram_notify package v16
"""
Helper comune per notifiche Telegram.

Usa variabili d'ambiente o un file di configurazione utente, senza segreti
hardcoded. Il percorso predefinito e':

    ~/.config/codex/secrets/telegram.env
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import tempfile
from pathlib import Path

import requests

from .config import (
    DEFAULT_USER_CONFIG_PATH,
    config_summary,
    get_default_chat_id,
    get_token,
    validate_config,
)
from .policy import dispatch

PROJECT_PREFIX_RE = "project_id="


def fix_mojibake(text: str) -> str:
    if not isinstance(text, str):
        return text
    if "Ã" not in text and "Â" not in text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except Exception:
        return text


def _resolve_chat_id(chat_id: str | int | None = None) -> str:
    if chat_id is None:
        return get_default_chat_id()
    resolved = str(chat_id).strip()
    if not resolved:
        raise ValueError("Telegram chat_id must not be empty.")
    return resolved


def _telegram_url(method: str) -> str:
    token = get_token()
    return f"https://api.telegram.org/bot{token}/{method}"


def _redact_secrets(text: str) -> str:
    try:
        token = get_token()
    except Exception:
        token = ""
    if token:
        text = text.replace(token, "<redacted>")
    return text


def _raise_for_response(action: str, response: requests.Response) -> None:
    if response.ok:
        return
    body = _redact_secrets((response.text or "").replace("\r", " ").replace("\n", " ")[:500])
    raise RuntimeError(f"{action} failed: HTTP {response.status_code}: {body}")


def _post_telegram(action: str, method: str, **kwargs) -> requests.Response:
    try:
        response = requests.post(_telegram_url(method), **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"{action} failed: {type(exc).__name__}") from None
    _raise_for_response(action, response)
    return response


def _project_prefix(project_id: int | str | None) -> str:
    if project_id is None:
        return ""
    text = str(project_id).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("Telegram project_id must be a positive integer.")
    return f"project_id={int(text)} | "


def with_project_id(text: str, project_id: int | str | None) -> str:
    text = fix_mojibake(text)
    if text.startswith(PROJECT_PREFIX_RE):
        return text
    return _project_prefix(project_id) + text


def send_text_raw(
    message: str,
    *,
    disable_preview: bool = False,
    chat_id: str | int | None = None,
    project_id: int | str | None = None,
) -> None:
    resolved_chat_id = _resolve_chat_id(chat_id)
    payload = {"chat_id": resolved_chat_id, "text": with_project_id(message, project_id)}
    if disable_preview:
        payload["disable_web_page_preview"] = True

    _post_telegram("Telegram sendMessage", "sendMessage", json=payload, timeout=20)


def send_message(title: str, message: str, *, chat_id: str | int | None = None, project_id: int | str | None = None) -> None:
    resolved_chat_id = _resolve_chat_id(chat_id)
    text = (
        _project_prefix(project_id)
        + f"<b>{html.escape(fix_mojibake(title))}</b>\n\n{html.escape(fix_mojibake(message))}"
    )
    _post_telegram(
        "Telegram sendMessage",
        "sendMessage",
        json={"chat_id": resolved_chat_id, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )


def send_alert(
    title: str,
    message: str,
    *,
    event_key: str,
    severity: str = "info",
    no_op: bool = False,
    chat_id: str | int | None = None,
    cooldown_seconds: int = 3600,
    min_interval_seconds: int = 0,
    state_path: Path | None = None,
) -> str:
    """Apply the shared alert policy; legacy send_message stays unchanged."""
    resolved_chat_id = _resolve_chat_id(chat_id)
    return dispatch(
        event_key=f"{resolved_chat_id}:{event_key}",
        severity=severity,
        title=title,
        message=message,
        no_op=no_op,
        cooldown_seconds=cooldown_seconds,
        min_interval_seconds=min_interval_seconds,
        state_path=state_path,
        sender=lambda heading, body: send_message(heading, body, chat_id=resolved_chat_id),
    )
def send_file(
    filepath: str | os.PathLike[str],
    title: str | None = None,
    message: str | None = None,
    *,
    caption: str | None = None,
    chat_id: str | int | None = None,
    project_id: int | str | None = None,
) -> None:
    resolved_chat_id = _resolve_chat_id(chat_id)
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(str(path))

    if caption is not None:
        text = html.escape(with_project_id(caption, project_id))
    else:
        title = fix_mojibake(title or "")
        message = fix_mojibake(message or "")
        text = (
            _project_prefix(project_id)
            + (f"<b>{html.escape(title)}</b>\n\n{html.escape(message)}" if (title or message) else "")
        )

    with path.open("rb") as fh:
        _post_telegram(
            "Telegram sendDocument",
            "sendDocument",
            data={
                "chat_id": resolved_chat_id,
                "caption": text,
                "parse_mode": "HTML",
            },
            files={"document": (path.name, fh)},
            timeout=600,
        )


def _split_long_text(text: str, max_length: int) -> list[str]:
    if max_length < 1:
        raise ValueError("max_length must be positive.")
    text = fix_mojibake(text)
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, max_length + 1)
        if split_at <= 0:
            split_at = max_length
        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk or remaining[:max_length])
        remaining = remaining[split_at:].lstrip("\n ")
    chunks.append(remaining)
    return chunks


def send_long_text(
    text: str,
    *,
    title: str | None = None,
    max_length: int = 4096,
    disable_preview: bool = True,
    chat_id: str | int | None = None,
    project_id: int | str | None = None,
) -> int:
    title = fix_mojibake(title or "")
    reserve = len(title) + 32 if title else 24
    if max_length <= reserve:
        raise ValueError("max_length is too small for the message header.")
    chunks = _split_long_text(text, max_length - reserve)
    total = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        header = f"{title} ({index}/{total})" if title else f"({index}/{total})"
        send_text_raw(
            f"{header}\n\n{chunk}",
            disable_preview=disable_preview,
            chat_id=chat_id,
            project_id=project_id,
        )
    return total


def send_final_report(
    report: str,
    *,
    title: str = "Codex Final Report",
    attach_txt: bool = True,
    chat_id: str | int | None = None,
    project_id: int | str | None = None,
) -> dict[str, object]:
    report = fix_mojibake(report)
    parts = send_long_text(report, title=title, chat_id=chat_id, project_id=project_id)
    attached = False
    if attach_txt:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="codex_final_report_",
                delete=False,
            ) as handle:
                handle.write(report)
                temp_path = Path(handle.name)
            send_file(temp_path, caption=f"{title} — report integrale", chat_id=chat_id, project_id=project_id)
            attached = True
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return {"parts": parts, "attachment_sent": attached}


def notify(
    message: str,
    prefix: str | None = None,
    title: str | None = None,
    *,
    chat_id: str | int | None = None,
    project_id: int | str | None = None,
) -> None:
    if title is None:
        title = prefix if prefix else "Notifica"
    elif prefix:
        title = f"[{prefix}] {title}"
    send_message(title, message, chat_id=chat_id, project_id=project_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Invia notifiche Telegram (testo o file).")
    parser.add_argument("--file", "-f", help="Percorso file da inviare", default=None)
    parser.add_argument("--final-report-file", help="Invia il contenuto del file come report finale.", default=None)
    parser.add_argument("--no-attach", action="store_true", help="Non allegare il report finale come file TXT.")
    parser.add_argument("--check", action="store_true", help="Verifica configurazione senza inviare.")
    parser.add_argument("--config-path", action="store_true", help="Stampa il percorso config utente predefinito.")
    parser.add_argument("title", nargs="?", help="Titolo del messaggio")
    parser.add_argument("message", nargs="?", help="Corpo del messaggio")
    args = parser.parse_args()

    try:
        if args.config_path:
            print(DEFAULT_USER_CONFIG_PATH)
            return 0
        if args.check:
            validate_config()
            print(f"OK {config_summary()}")
            return 0
        if args.final_report_file:
            report_path = Path(args.final_report_file)
            report = report_path.read_text(encoding="utf-8")
            result = send_final_report(
                report,
                title=args.title or "Codex Final Report",
                attach_txt=not args.no_attach,
            )
            print(f"OK parts={result['parts']} attachment_sent={result['attachment_sent']}")
            return 0
        if args.file:
            path = Path(args.file)
            if not path.exists():
                raise FileNotFoundError(str(path))
            send_file(path, args.title, args.message,
                      caption="" if args.title is None and args.message is None else None,
                      project_id=os.environ.get("TELEGRAM_PROJECT_ID"))
        else:
            if not args.title or args.message is None:
                parser.error("title and message are required unless --check, --final-report-file or --file is used")
            send_message(args.title, args.message, project_id=os.environ.get("TELEGRAM_PROJECT_ID"))
        print("OK")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
