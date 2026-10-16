"""Delivery helpers for paired human-readable and technical task reports."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from .notify import fix_mojibake, send_file, send_long_text


def send_task_reports(
    plain_report: str,
    technical_report: str,
    *,
    title: str = "Codex Report",
    prompt_id: str | int | None = None,
    chat_id: str | int | None = None,
    project_id: str | int | None = None,
) -> dict[str, object]:
    """Send the readable report in chat and attach both complete reports.

    The technical report is never expanded into Telegram chat: it is attached as
    a text file for later machine-assisted analysis.
    """
    plain_report = fix_mojibake(plain_report).strip()
    technical_report = fix_mojibake(technical_report).strip()
    if not plain_report:
        raise ValueError("plain_report must not be empty.")
    if not technical_report:
        raise ValueError("technical_report must not be empty.")

    suffix = f" — PROMPT_ID={prompt_id}" if prompt_id is not None else ""
    resolved_title = f"{fix_mojibake(title)}{suffix}"
    parts = send_long_text(
        plain_report,
        title=resolved_title,
        chat_id=chat_id,
        project_id=project_id,
    )

    with TemporaryDirectory(prefix="codex_task_reports_") as temp_dir:
        directory = Path(temp_dir)
        plain_path = directory / "FINAL_REPORT_PLAIN.txt"
        technical_path = directory / "FINAL_REPORT_TECHNICAL.txt"
        plain_path.write_text(plain_report + "\n", encoding="utf-8")
        technical_path.write_text(technical_report + "\n", encoding="utf-8")
        send_file(
            plain_path,
            caption=f"{resolved_title} — versione semplice completa",
            chat_id=chat_id,
            project_id=project_id,
        )
        send_file(
            technical_path,
            caption=f"{resolved_title} — report tecnico per analisi",
            chat_id=chat_id,
            project_id=project_id,
        )

    return {
        "plain_message_parts": parts,
        "plain_attachment_sent": True,
        "technical_attachment_sent": True,
    }
