import importlib
from pathlib import Path

reports = importlib.import_module("telegram_notify.reports")


def test_send_task_reports_sends_plain_message_and_two_files(monkeypatch) -> None:
    messages: list[tuple[str, str | None, object]] = []
    files: list[tuple[str, str, object]] = []

    def fake_send_long_text(text: str, *, title=None, **kwargs) -> int:
        messages.append((text, title, kwargs.get("project_id")))
        return 2

    def fake_send_file(filepath, *, caption=None, **kwargs) -> None:
        path = Path(filepath)
        files.append((path.name, path.read_text(encoding="utf-8"), kwargs.get("project_id")))

    monkeypatch.setattr(reports, "send_long_text", fake_send_long_text)
    monkeypatch.setattr(reports, "send_file", fake_send_file)

    result = reports.send_task_reports(
        "Spiegazione semplice",
        "Dettagli tecnici",
        prompt_id=42,
        project_id=8,
    )

    assert messages == [("Spiegazione semplice", "Codex Report — PROMPT_ID=42", 8)]
    assert files == [
        ("FINAL_REPORT_PLAIN.txt", "Spiegazione semplice\n", 8),
        ("FINAL_REPORT_TECHNICAL.txt", "Dettagli tecnici\n", 8),
    ]
    assert result == {
        "plain_message_parts": 2,
        "plain_attachment_sent": True,
        "technical_attachment_sent": True,
    }


def test_send_task_reports_rejects_empty_reports() -> None:
    for plain, technical in [("", "technical"), ("plain", "")]:
        try:
            reports.send_task_reports(plain, technical)
        except ValueError:
            pass
        else:
            raise AssertionError("empty reports must be rejected")
