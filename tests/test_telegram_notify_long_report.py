import importlib
from pathlib import Path

notify_module = importlib.import_module("telegram_notify.notify")


def test_split_long_text_prefers_boundaries_and_preserves_content() -> None:
    chunks = notify_module._split_long_text("alpha beta\ngamma delta", 11)
    assert all(len(chunk) <= 11 for chunk in chunks)
    assert " ".join(" ".join(chunks).split()) == "alpha beta gamma delta"


def test_send_long_text_numbers_all_parts(monkeypatch) -> None:
    sent: list[str] = []

    def fake_send_text_raw(message: str, **kwargs) -> None:
        sent.append(message)

    monkeypatch.setattr(notify_module, "send_text_raw", fake_send_text_raw)
    parts = notify_module.send_long_text("x" * 50, title="Report", max_length=40)

    assert parts == len(sent)
    assert sent[0].startswith(f"Report (1/{parts})")
    assert sent[-1].startswith(f"Report ({parts}/{parts})")
    assert all(len(message) <= 40 for message in sent)


def test_send_final_report_sends_attachment_and_cleans_tempfile(monkeypatch) -> None:
    attached_paths: list[Path] = []

    monkeypatch.setattr(notify_module, "send_long_text", lambda *args, **kwargs: 2)

    def fake_send_file(filepath, **kwargs) -> None:
        path = Path(filepath)
        assert path.read_text(encoding="utf-8") == "complete report"
        attached_paths.append(path)

    monkeypatch.setattr(notify_module, "send_file", fake_send_file)
    result = notify_module.send_final_report("complete report")

    assert result == {"parts": 2, "attachment_sent": True}
    assert attached_paths
    assert not attached_paths[0].exists()
