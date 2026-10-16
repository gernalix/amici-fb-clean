# amici_fb_task_runner.py v2
"""Thin scheduled runner for amici_fb.py with early diagnostics and Kuma push."""

from __future__ import annotations

import contextlib
import tempfile
import traceback
import faulthandler
import os
import runpy
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


KUMA_PUSH_URL_ENV = "UPTIME_KUMA_PUSH_URL"
KUMA_CONNECT_TIMEOUT_ENV = "UPTIME_KUMA_CONNECT_TIMEOUT_SECONDS"
KUMA_MAX_TIME_ENV = "UPTIME_KUMA_MAX_TIME_SECONDS"
KUMA_PUSH_STARTED_ENV = "AMICI_FB_KUMA_PUSH_STARTED"


def _sanitize_log_text(text: object) -> str:
    raw = str(text)
    raw = re.sub(r"https?://\S+", "<redacted-url>", raw)
    return raw.replace("\n", " ")[:220]


def _failure_reason(exc: BaseException | str) -> str:
    if isinstance(exc, SystemExit):
        code = exc.code if isinstance(exc.code, int) else 1
        return f"exit {code}"
    return _sanitize_log_text(f"{type(exc).__name__}: {exc}")


def _load_project_env(project_dir: Path) -> None:
    env_path = project_dir / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _push_url_base(push_url: str) -> str:
    parsed = urlsplit(push_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"status", "msg", "ping"}
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def push_kuma(status: str, msg: str, ping: int | None = None) -> bool:
    push_url = os.getenv(KUMA_PUSH_URL_ENV, "").strip()
    if not push_url:
        print("kuma_push.skipped not_configured", flush=True)
        return False

    params = {"status": status, "msg": msg}
    if ping is not None:
        params["ping"] = str(ping)
    timeout = (
        _env_float(KUMA_CONNECT_TIMEOUT_ENV, 2.0),
        _env_float(KUMA_MAX_TIME_ENV, 5.0),
    )

    try:
        response = requests.get(_push_url_base(push_url), params=params, timeout=timeout)
        response.raise_for_status()
    except Exception as exc:
        print(f"kuma_push.warning status={status} error={_failure_reason(exc)}", flush=True)
        return False

    print(f"kuma_push.sent status={status} msg={_sanitize_log_text(msg)}", flush=True)
    return True


def _run() -> tuple[int, str | None]:
    project_dir = Path(__file__).resolve().parent
    _load_project_env(project_dir)
    script_path = project_dir / "amici_fb.py"
    dump_seconds = int(os.getenv("AMICI_FB_STACK_DUMP_SECONDS", "0"))
    started_at = time.monotonic()

    faulthandler.enable(all_threads=True)
    if dump_seconds > 0:
        faulthandler.dump_traceback_later(dump_seconds, repeat=True)

    print(f"task_runner.start script={script_path} argv={sys.argv[1:]}", flush=True)
    if os.getenv(KUMA_PUSH_STARTED_ENV, "").strip() == "1":
        push_kuma("up", "STARTED")
    sys.argv = [str(script_path), *sys.argv[1:]]
    try:
        namespace = runpy.run_path(str(script_path), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code == 0:
            push_kuma("up", "OK", int((time.monotonic() - started_at) * 1000))
            print("task_runner.end", flush=True)
            return 0, None
        push_kuma("down", f"FAILED:{_failure_reason(exc)}")
        raise
    except BaseException as exc:
        push_kuma("down", f"FAILED:{_failure_reason(exc)}")
        raise
    push_kuma("up", "OK", int((time.monotonic() - started_at) * 1000))
    print("task_runner.end", flush=True)
    return 0, namespace.get("CLI_RESULT")


def main() -> int:
    # Keep manual login prompts and argparse help visible. Snapshot diagnostics
    # go to a private per-run log; publish only this run's successful result.
    if "--headless" not in sys.argv[1:] or any(
        arg in sys.argv[1:] for arg in ("--login", "--help", "-h")
    ):
        code, _ = _run()
        return code
    output_dir = Path(os.getenv("AMICI_FB_OUTPUT_DIR") or
                      str(Path(os.getenv("AMICI_FB_DATA_HOME") or
                               str(Path.home() / ".local/share/amici_fb")) / "data"))
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                     prefix="run-", suffix=".log",
                                     dir=log_dir, delete=False) as log:
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                code, report = _run()
        except BaseException as exc:
            traceback.print_exc(file=log)
            print(f"Errore: {_failure_reason(exc)}. Log: {log.name}",
                  file=sys.stderr, flush=True)
            return exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) and exc.code else 1
    if report:
        print(report, flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
