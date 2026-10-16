# amici_fb.py v19
import os
import csv
import datetime
import re
import argparse
import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, urlencode

IMPORT_TRACE = os.getenv("AMICI_FB_IMPORT_TRACE") == "1"
if IMPORT_TRACE:
    print("amici_fb.import.before_playwright", flush=True)
from playwright.sync_api import sync_playwright
if IMPORT_TRACE:
    print("amici_fb.import.after_playwright", flush=True)
import telegram_notify  # notifiche + invio file
from amici_fb_health import heartbeat_due, mark_telegram_sent
if IMPORT_TRACE:
    print("amici_fb.import.after_telegram", flush=True)

# ==========================
# CONFIG
# ==========================
BASE_URL = "https://www.facebook.com"
FRIENDS_URL = f"{BASE_URL}/me/friends"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR_NAME = "data"
APP_ID = "amici_fb"
STORAGE_STATE_FILENAME = "fb_storage_state.json"  # per riusare il login
ECCEZIONI_FILENAME = "eccezioni.ini"
FB_HOSTS = {"www.facebook.com", "facebook.com", "m.facebook.com"}
FB_REDIRECT_HOSTS = {"l.facebook.com", "lm.facebook.com"}

SCROLL_PAUSE_MS = 1500
MAX_SCROLL_IDLE = 5

DEFAULT_HEADLESS = False         # Il runner Fedora pianificato passa --headless esplicitamente.
CHECK_REMOVED_VISIBILITY = True  # False se vuoi evitare richieste extra

LOGIN_MARKERS = [
    "Log into Facebook",
    "log in to Facebook",
    "Accedi a Facebook",
    "Accedi",
    "Password dimenticata",
]

# SQLite DB (creato se non esiste)
DB_NAME = "amici_fb.sqlite3"
DEFAULT_NAVIGATION_TIMEOUT_MS = 120000
POST_FRIENDS_GOTO_TIMEOUT_MS = 10000
PROCESS_WATCH_POLL_MS = 250

CHECKPOINT_MARKERS = [
    "checkpoint",
    "security check",
    "controllo di sicurezza",
]

CONSENT_MARKERS = [
    "allow all cookies",
    "accetta tutti",
    "cookie",
    "consent",
]

INTERSTITIAL_MARKERS = [
    "temporarily blocked",
    "sospeso temporaneamente",
    "review recent activity",
]


# ==========================
# TIME HELPERS
# ==========================
def utc_now_naive() -> datetime.datetime:
    """UTC senza timezone nel formato storico usato da filename e CSV."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def iso_utc_z(dt: datetime.datetime | None = None) -> str:
    """Ritorna timestamp UTC nel formato 2026-01-16T23:35:54Z (senza microsecondi)."""
    if dt is None:
        dt = utc_now_naive()
    dt = dt.replace(microsecond=0)
    return dt.isoformat() + "Z"


def filename_stamp_utc(dt: datetime.datetime | None = None) -> str:
    """Timestamp sicuro per filename: YYYY-MM-DDTHHMMSSZ."""
    if dt is None:
        dt = utc_now_naive()
    dt = dt.replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H%M%SZ")


# ==========================
# PATH / FILE
# ==========================
def ensure_output_dir():
    output_dir_path().mkdir(parents=True, exist_ok=True)


def project_path(*parts: str) -> Path:
    return SCRIPT_DIR.joinpath(*parts)


def output_dir_path() -> Path:
    raw = os.environ.get("AMICI_FB_OUTPUT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return app_data_dir_path() / OUTPUT_DIR_NAME


def app_data_dir_path() -> Path:
    raw = os.environ.get("AMICI_FB_DATA_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share" / APP_ID


def legacy_storage_state_path() -> Path:
    return project_path(STORAGE_STATE_FILENAME)


def storage_state_path() -> Path:
    raw = os.environ.get("AMICI_FB_STORAGE_STATE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return app_data_dir_path() / STORAGE_STATE_FILENAME


def chromium_profile_dir_path() -> Path:
    raw = os.environ.get("AMICI_FB_CHROMIUM_PROFILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return app_data_dir_path() / "chromium-profile"


def ensure_storage_state_parent() -> None:
    storage_state_path().parent.mkdir(parents=True, exist_ok=True)


def ensure_chromium_profile_dir() -> Path:
    profile_dir = chromium_profile_dir_path()
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        pass
    return profile_dir


def chromium_profile_initialized(profile_dir: Path | None = None) -> bool:
    profile = profile_dir or chromium_profile_dir_path()
    return (profile / "Default").exists() or (profile / "Local State").exists()


def chmod_user_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def candidate_chromium_executables() -> list[Path]:
    explicit = os.environ.get("AMICI_FB_CHROMIUM_EXECUTABLE", "").strip()
    candidates = [Path(explicit).expanduser()] if explicit else []
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    candidates.extend([Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")])

    unique: list[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def chromium_launch_kwargs(headless: bool) -> dict:
    kwargs: dict = {"headless": headless}
    for executable in candidate_chromium_executables():
        if executable.is_file() and os.access(executable, os.X_OK):
            kwargs["executable_path"] = str(executable)
            break
    return kwargs


def restore_storage_state_to_context(context, storage_path: Path, logger=None) -> None:
    if not storage_path.exists():
        return
    try:
        state = json.loads(storage_path.read_text(encoding="utf-8"))
    except Exception as e:
        if logger:
            logger.log(f"persistent_profile.storage_state_read_failed path={storage_path} error={type(e).__name__}: {e}")
        return

    cookies = state.get("cookies") or []
    if cookies:
        context.add_cookies(cookies)

    restored_origins = 0
    for origin in state.get("origins") or []:
        origin_url = origin.get("origin")
        local_storage = origin.get("localStorage") or []
        if not origin_url or not local_storage:
            continue
        page = context.new_page()
        try:
            page.goto(origin_url, wait_until="domcontentloaded", timeout=15000)
            for item in local_storage:
                name = item.get("name")
                value = item.get("value")
                if name is not None and value is not None:
                    page.evaluate("([k, v]) => localStorage.setItem(k, v)", [name, value])
            restored_origins += 1
        except Exception as e:
            if logger:
                logger.log(f"persistent_profile.local_storage_restore_failed origin={origin_url} error={type(e).__name__}: {e}")
        finally:
            safe_close(page)

    if logger:
        logger.log(
            f"persistent_profile.storage_state_restored path={storage_path} "
            f"cookies={len(cookies)} origins={restored_origins}"
        )


def exceptions_file_path() -> Path:
    return project_path(ECCEZIONI_FILENAME)


def default_db_path() -> Path:
    legacy_db = project_path(DB_NAME)
    if legacy_db.exists():
        return legacy_db
    return output_dir_path() / DB_NAME


def resolve_input_path(raw_path: str | os.PathLike | None, default_path: Path) -> Path:
    if raw_path in (None, ""):
        return default_path

    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path

    return project_path(str(path))


def db_path():
    return str(default_db_path())


def snapshot_csv_path(run_stamp: str):
    return str(output_dir_path() / f"amici_{run_stamp}.csv")


def find_previous_csv(today_path):
    output_dir = output_dir_path()
    if not output_dir.is_dir():
        return None
    files = [p.name for p in output_dir.iterdir() if p.is_file() and p.name.startswith("amici_") and p.suffix == ".csv"]
    if not files:
        return None
    files.sort()
    today_name = os.path.basename(today_path)
    previous = [f for f in files if f < today_name]
    if not previous:
        return None
    return str(output_dir / previous[-1])


def list_previous_csv_paths(today_path) -> list[str]:
    output_dir = output_dir_path()
    if not output_dir.is_dir():
        return []
    files = [p.name for p in output_dir.iterdir() if p.is_file() and p.name.startswith("amici_") and p.suffix == ".csv"]
    files.sort()
    today_name = os.path.basename(today_path)
    return [str(output_dir / name) for name in files if name < today_name]


def snapshot_date_from_csv_path(path: str) -> str | None:
    name = Path(path).name
    m = re.fullmatch(r"amici_(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})Z\.csv", name)
    if not m:
        return None
    return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}Z"


def diff_csv_path(prev_path, today_path):
    prev_stamp = os.path.basename(prev_path)[6:-4]
    today_stamp = os.path.basename(today_path)[6:-4]
    return str(output_dir_path() / f"diff_{prev_stamp}_vs_{today_stamp}.csv")


def debug_rejected_csv_path(run_stamp: str):
    return str(output_dir_path() / f"debug_rejected_{run_stamp}.csv")


def debug_cards_csv_path(run_stamp: str):
    return str(output_dir_path() / f"debug_cards_{run_stamp}.csv")


def browser_diag_dir_path(run_stamp: str) -> Path:
    return output_dir_path() / f"browser_diag_{run_stamp}"


class DiagLogger:
    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path
        self._lock = threading.Lock()
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        stamp = utc_now_naive().replace(microsecond=0).isoformat() + "Z"
        line = f"[diag {stamp}] {message}"
        print(line, flush=True)
        if not self.log_path:
            return
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class BrowserProcessWatcher:
    def __init__(self, browser, logger: DiagLogger | None = None):
        self.browser = browser
        self.logger = logger
        self._stop = threading.Event()
        self._thread = None
        self._seen_exit = False

    def start(self) -> None:
        proc = browser_transport_process(self.browser)
        pid = getattr(proc, "pid", None)
        if self.logger:
            self.logger.log(f"browser_process_watcher.start pid={pid}")
        if proc is None:
            return
        self._thread = threading.Thread(target=self._run, name="browser-process-watcher", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        proc = browser_transport_process(self.browser)
        while not self._stop.is_set():
            rc = getattr(proc, "returncode", None) if proc is not None else None
            if rc is not None and not self._seen_exit:
                self._seen_exit = True
                if self.logger:
                    self.logger.log(f"browser_process_watcher.exit pid={getattr(proc, 'pid', None)} returncode={rc}")
                return
            time.sleep(PROCESS_WATCH_POLL_MS / 1000.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


def browser_transport_process(browser):
    try:
        return browser._impl_obj._connection._transport._proc
    except Exception:
        return None


def browser_process_pid(browser):
    proc = browser_transport_process(browser)
    return getattr(proc, "pid", None)


def browser_process_returncode(browser):
    proc = browser_transport_process(browser)
    return getattr(proc, "returncode", None)


def browser_is_connected(browser) -> str:
    if browser is None:
        return "n/a"
    try:
        return str(browser.is_connected())
    except Exception as e:
        return f"error:{type(e).__name__}:{e}"


def context_page_count(context) -> str:
    if context is None:
        return "n/a"
    try:
        return str(len(context.pages))
    except Exception as e:
        return f"error:{type(e).__name__}:{e}"


def safe_page_url(page) -> str:
    if page is None:
        return ""
    try:
        return page.url or ""
    except Exception as e:
        return f"<url-error {type(e).__name__}: {e}>"


def safe_page_title(page) -> str:
    if page is None:
        return ""
    try:
        if page.is_closed():
            return "<closed>"
        return page.title()
    except Exception as e:
        return f"<title-error {type(e).__name__}: {e}>"


def safe_page_closed(page) -> str:
    if page is None:
        return "n/a"
    try:
        return str(page.is_closed())
    except Exception as e:
        return f"error:{type(e).__name__}:{e}"


def save_page_artifacts(page, diag_dir: Path | None, label: str, logger: DiagLogger | None = None) -> list[str]:
    saved = []
    if diag_dir is None or page is None:
        return saved

    base_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "page"
    state_path = diag_dir / f"{base_name}.state.txt"
    try:
        state_path.write_text(
            "\n".join(
                [
                    f"url={safe_page_url(page)}",
                    f"title={safe_page_title(page)}",
                    f"is_closed={safe_page_closed(page)}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        saved.append(str(state_path))
    except Exception as e:
        if logger:
            logger.log(f"save_artifacts.state_failed label={label} error={type(e).__name__}: {e}")

    try:
        if not page.is_closed():
            screenshot_path = diag_dir / f"{base_name}.png"
            page.screenshot(path=str(screenshot_path), full_page=True, timeout=5000)
            saved.append(str(screenshot_path))
    except Exception as e:
        if logger:
            logger.log(f"save_artifacts.screenshot_failed label={label} error={type(e).__name__}: {e}")

    try:
        if not page.is_closed():
            html_path = diag_dir / f"{base_name}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            saved.append(str(html_path))
    except Exception as e:
        if logger:
            logger.log(f"save_artifacts.html_failed label={label} error={type(e).__name__}: {e}")

    return saved


def log_browser_state(logger: DiagLogger | None, label: str, browser=None, context=None, page=None) -> None:
    if logger is None:
        return
    logger.log(
        f"{label} browser_connected={browser_is_connected(browser)} "
        f"browser_pid={browser_process_pid(browser)} browser_returncode={browser_process_returncode(browser)} "
        f"context_pages={context_page_count(context)} page_closed={safe_page_closed(page)} "
        f'page_url="{safe_page_url(page)}" page_title="{safe_page_title(page)}"'
    )


def attach_page_debug(page, label: str, logger: DiagLogger | None = None, diag_dir: Path | None = None) -> None:
    if page is None or getattr(page, "_amici_fb_debug_attached", False):
        return

    setattr(page, "_amici_fb_debug_attached", True)
    setattr(page, "_amici_fb_debug_label", label)

    if logger:
        logger.log(f'page.attach label={label} page_id={id(page)} url="{safe_page_url(page)}"')

    def _log(event_message: str) -> None:
        if logger:
            logger.log(f"page.{event_message} label={label} page_id={id(page)}")

    page.on("close", lambda: _log(f'close url="{safe_page_url(page)}" title="{safe_page_title(page)}"'))
    page.on("crash", lambda: (_log(f'crash url="{safe_page_url(page)}"'), save_page_artifacts(page, diag_dir, f"{label}_crash", logger)))
    page.on("dialog", lambda dialog: _log(f'dialog type={dialog.type} message="{dialog.message}"'))
    page.on("pageerror", lambda exc: _log(f'pageerror error={type(exc).__name__}: {exc}'))
    page.on("popup", lambda popup: (attach_page_debug(popup, f"{label}_popup", logger, diag_dir), _log(f"popup popup_id={id(popup)}")))

    def on_request_failed(request) -> None:
        failure = ""
        try:
            failure = request.failure or ""
        except Exception:
            pass
        _log(f'requestfailed method={request.method} url="{request.url}" failure="{failure}"')

    def on_response(response) -> None:
        try:
            request = response.request
            is_document = request.resource_type == "document"
        except Exception:
            request = None
            is_document = False
        if not is_document and response.status < 400:
            return
        resource_type = getattr(request, "resource_type", "?")
        _log(f'response status={response.status} resource={resource_type} url="{response.url}"')

    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)


def attach_context_debug(context, logger: DiagLogger | None = None, diag_dir: Path | None = None) -> None:
    if context is None or getattr(context, "_amici_fb_debug_attached", False):
        return

    setattr(context, "_amici_fb_debug_attached", True)
    if logger:
        logger.log(f"context.attach existing_pages={len(context.pages)}")

    for idx, page in enumerate(list(context.pages), start=1):
        attach_page_debug(page, f"context_existing_{idx}", logger, diag_dir)

    def on_page(page) -> None:
        attach_page_debug(page, f"context_page_{len(context.pages)}", logger, diag_dir)
        if logger:
            logger.log(f"context.page created page_id={id(page)} total_pages={len(context.pages)}")

    context.on("page", on_page)


def new_debug_page(context, label: str, logger: DiagLogger | None = None, diag_dir: Path | None = None):
    if logger:
        logger.log(f"new_page.start label={label} context_pages_before={context_page_count(context)}")
    page = context.new_page()
    attach_page_debug(page, label, logger, diag_dir)
    log_browser_state(logger, f"new_page.done label={label}", context=context, page=page)
    return page


def goto_with_debug(page, url: str, label: str, timeout_ms: int, wait_until: str, logger: DiagLogger | None = None, diag_dir: Path | None = None, browser=None, context=None):
    if logger:
        logger.log(f'goto.start label={label} url="{url}" wait_until={wait_until} timeout_ms={timeout_ms}')
    log_browser_state(logger, f"goto.pre label={label}", browser=browser, context=context, page=page)
    try:
        response = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        status = None
        if response is not None:
            try:
                status = response.status
            except Exception:
                status = "unknown"
        log_browser_state(logger, f"goto.ok label={label} status={status}", browser=browser, context=context, page=page)
        return response
    except Exception as e:
        saved = save_page_artifacts(page, diag_dir, f"{label}_goto_failure", logger)
        log_browser_state(logger, f"goto.fail label={label} error={type(e).__name__}: {e} artifacts={saved}", browser=browser, context=context, page=page)
        raise


def detect_facebook_special_state(page) -> tuple[str | None, str]:
    url = safe_page_url(page)
    title = safe_page_title(page)
    body = ""
    if page is not None:
        try:
            if not page.is_closed():
                body = page.inner_text("body") or ""
        except Exception:
            body = ""

    joined = "\n".join([url, title, body]).lower()
    if page_looks_like_login(page):
        return "login", f'url="{url}" title="{title}"'
    if any(marker in joined for marker in CHECKPOINT_MARKERS):
        return "checkpoint", f'url="{url}" title="{title}"'
    if any(marker in joined for marker in CONSENT_MARKERS):
        return "consent", f'url="{url}" title="{title}"'
    if any(marker in joined for marker in INTERSTITIAL_MARKERS):
        return "interstitial", f'url="{url}" title="{title}"'
    return None, f'url="{url}" title="{title}"'


def ensure_browser_page_alive(page, label: str, browser=None, context=None, logger: DiagLogger | None = None, diag_dir: Path | None = None) -> None:
    closed = False
    try:
        closed = page.is_closed()
    except Exception:
        closed = True

    connected = None
    if browser is not None:
        try:
            connected = browser.is_connected()
        except Exception:
            connected = False

    if closed or connected is False:
        saved = save_page_artifacts(page, diag_dir, f"{label}_dead_page", logger)
        log_browser_state(logger, f"page.dead label={label} artifacts={saved}", browser=browser, context=context, page=page)
        raise RuntimeError(f"Browser/page non piu' vivi durante {label}: page_closed={closed} browser_connected={connected}")


def ensure_known_facebook_state(page, label: str, logger: DiagLogger | None = None, diag_dir: Path | None = None) -> None:
    state, details = detect_facebook_special_state(page)
    if state is None:
        if logger:
            logger.log(f"facebook_state.ok label={label} {details}")
        return
    saved = save_page_artifacts(page, diag_dir, f"{label}_{state}", logger)
    if logger:
        logger.log(f"facebook_state.blocked label={label} state={state} {details} artifacts={saved}")
    raise RuntimeError(f"Facebook ha mostrato una pagina speciale ({state}) durante {label}: {details}")


# ==========================
# SQLITE
# ==========================
def get_db_conn(db_file: str | os.PathLike | None = None):
    path = resolve_input_path(db_file, default_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            total_friends INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            profile_url TEXT,
            name TEXT,
            first_seen_snapshot_id INTEGER,
            last_seen_snapshot_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(profile_id),
            FOREIGN KEY(first_seen_snapshot_id) REFERENCES snapshots(id) ON DELETE SET NULL,
            FOREIGN KEY(last_seen_snapshot_id) REFERENCES snapshots(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS snapshot_friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            name_at_time TEXT,
            url_at_time TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(snapshot_id, friend_id),
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
            FOREIGN KEY(friend_id) REFERENCES friends(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS diff_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            prev_snapshot_id INTEGER,
            change_type TEXT NOT NULL CHECK (change_type IN ('added','removed')),
            friend_id INTEGER NOT NULL,
            visibility TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(snapshot_id, change_type, friend_id),
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
            FOREIGN KEY(prev_snapshot_id) REFERENCES snapshots(id) ON DELETE SET NULL,
            FOREIGN KEY(friend_id) REFERENCES friends(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS eccezioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(profile_id)
        );

        CREATE TRIGGER IF NOT EXISTS eccezioni_updated_at
        AFTER UPDATE ON eccezioni
        FOR EACH ROW
        BEGIN
            UPDATE eccezioni
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            WHERE id = OLD.id;
        END;

CREATE TABLE IF NOT EXISTS friend_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT,                 -- numeric id or vanity username when available
    profile_url TEXT,                -- canonical URL
    name TEXT,
    requested_at TEXT,               -- when YOU sent the request (manual input)
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected','unknown')),
    accepted_at TEXT,
    last_checked_at TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(profile_id, profile_url)
);

CREATE TRIGGER IF NOT EXISTS friend_requests_updated_at
AFTER UPDATE ON friend_requests
FOR EACH ROW
BEGIN
    UPDATE friend_requests
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
    WHERE id = OLD.id;
END;

CREATE INDEX IF NOT EXISTS idx_friend_requests_status ON friend_requests(status);
CREATE INDEX IF NOT EXISTS idx_friend_requests_profile_id ON friend_requests(profile_id);

        CREATE INDEX IF NOT EXISTS idx_snapshots_snapshot_date ON snapshots(snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_friends_profile_id ON friends(profile_id);
        CREATE INDEX IF NOT EXISTS idx_snapshot_friends_snapshot_id ON snapshot_friends(snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_snapshot_friends_friend_id ON snapshot_friends(friend_id);
        CREATE INDEX IF NOT EXISTS idx_diff_events_snapshot_id ON diff_events(snapshot_id);
        """
    )
    conn.commit()


def load_exceptions_from_db(conn) -> set:
    cur = conn.execute("SELECT profile_id FROM eccezioni")
    res = {row["profile_id"] for row in cur.fetchall() if row["profile_id"]}
    print(f"Eccezioni caricate dal DB: {len(res)}")
    return res


def load_exceptions_from_file(path: Path | None = None) -> set:
    source = path or exceptions_file_path()
    if not source.is_file():
        return set()

    res = set()
    with source.open("r", encoding="utf-8-sig", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            res.add(line)

    print(f"Eccezioni caricate da file: {len(res)}")
    return res


def load_exceptions(conn) -> set:
    db_exceptions = load_exceptions_from_db(conn)
    file_exceptions = load_exceptions_from_file()
    merged = db_exceptions | file_exceptions
    if file_exceptions:
        print(f"Eccezioni totali attive: {len(merged)}")
    return merged


def db_insert_snapshot(conn, snapshot_date_isoz: str, total_friends: int) -> int:
    now = iso_utc_z()
    cur = conn.execute(
        "INSERT INTO snapshots (snapshot_date, created_at, total_friends) VALUES (?, ?, ?)",
        (snapshot_date_isoz, now, int(total_friends)),
    )
    conn.commit()
    return int(cur.lastrowid)


def db_upsert_friend(conn, profile_id: str, profile_url: str, name: str, snapshot_id: int) -> int:
    now = iso_utc_z()
    row = conn.execute("SELECT id, first_seen_snapshot_id FROM friends WHERE profile_id = ?", (profile_id,)).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO friends
              (profile_id, profile_url, name, first_seen_snapshot_id, last_seen_snapshot_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (profile_id, profile_url, name, snapshot_id, snapshot_id, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)

    friend_id = int(row["id"])
    first_seen = row["first_seen_snapshot_id"]
    if first_seen is None:
        conn.execute(
            """
            UPDATE friends
            SET profile_url=?, name=?, first_seen_snapshot_id=?, last_seen_snapshot_id=?, updated_at=?
            WHERE id=?
            """,
            (profile_url, name, snapshot_id, snapshot_id, now, friend_id),
        )
    else:
        conn.execute(
            """
            UPDATE friends
            SET profile_url=?, name=?, last_seen_snapshot_id=?, updated_at=?
            WHERE id=?
            """,
            (profile_url, name, snapshot_id, now, friend_id),
        )
    conn.commit()
    return friend_id


def db_insert_snapshot_friend(conn, snapshot_id: int, friend_id: int, name_at_time: str, url_at_time: str):
    now = iso_utc_z()
    conn.execute(
        """
        INSERT OR IGNORE INTO snapshot_friends
          (snapshot_id, friend_id, name_at_time, url_at_time, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (snapshot_id, friend_id, name_at_time, url_at_time, now),
    )


def db_get_prev_snapshot_id(conn, snapshot_id: int) -> int | None:
    row = conn.execute("SELECT id FROM snapshots WHERE id < ? ORDER BY id DESC LIMIT 1", (snapshot_id,)).fetchone()
    return int(row["id"]) if row else None


def db_get_snapshot_id_by_snapshot_date(conn, snapshot_date_isoz: str | None) -> int | None:
    if not snapshot_date_isoz:
        return None
    row = conn.execute("SELECT id FROM snapshots WHERE snapshot_date = ? ORDER BY id DESC LIMIT 1", (snapshot_date_isoz,)).fetchone()
    return int(row["id"]) if row else None


def db_get_friend_ids_for_snapshot(conn, snapshot_id: int) -> set:
    cur = conn.execute("SELECT friend_id FROM snapshot_friends WHERE snapshot_id = ?", (snapshot_id,))
    return {int(r["friend_id"]) for r in cur.fetchall()}


def db_insert_diff_event(conn, snapshot_id: int, prev_snapshot_id: int | None, change_type: str, friend_id: int, visibility: str | None):
    now = iso_utc_z()
    conn.execute(
        """
        INSERT OR IGNORE INTO diff_events
          (snapshot_id, prev_snapshot_id, change_type, friend_id, visibility, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (snapshot_id, prev_snapshot_id, change_type, friend_id, visibility, now),
    )


def db_friend_url_by_id(conn, friend_id: int) -> str | None:
    row = conn.execute("SELECT profile_url FROM friends WHERE id = ?", (friend_id,)).fetchone()
    return (row["profile_url"] if row else None)


# ==========================
# URL / NORMALIZE
# ==========================
def normalize_fb_href(href: str) -> str:
    if not href:
        return ""
    href = href.strip()

    if href.startswith("//"):
        href = "https:" + href

    if href.startswith("/"):
        return BASE_URL + href

    try:
        parsed = urlparse(href)
        netloc = parsed.netloc.lower()
        if netloc in FB_REDIRECT_HOSTS and parsed.path.startswith("/l.php"):
            qs = parse_qs(parsed.query)
            u = qs.get("u", [""])[0]
            if u:
                return normalize_fb_href(unquote(u))

        if netloc in FB_HOSTS:
            path = parsed.path or ""
            if path != "/" and path.endswith("/"):
                path = path.rstrip("/")

            query = ""
            if path.startswith("/profile.php"):
                qs = parse_qs(parsed.query)
                if "id" in qs and qs["id"]:
                    query = urlencode({"id": qs["id"][0]})

            return f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    except Exception:
        pass

    return href


def is_profile_url(href: str) -> bool:
    href = normalize_fb_href(href)
    if not href:
        return False

    parsed = urlparse(href)
    if parsed.netloc.lower() not in FB_HOSTS:
        return False

    path = parsed.path.strip("/")

    bad_exact = {
        "", "friends", "friends_all", "groups", "pages", "events",
        "marketplace", "watch", "settings", "notifications",
        "permalink.php", "photo.php", "story.php", "reel", "share", "login",
        "checkpoint", "recover", "privacy", "help", "messages"
    }
    if path in bad_exact:
        return False

    bad_prefixes = (
        "ajax/", "api/", "plugins/", "sharer", "share/", "photo/", "watch/",
        "events/", "groups/", "pages/", "marketplace/", "settings/",
        "notifications/", "stories/", "reel/", "reels/"
    )
    if path.startswith(bad_prefixes):
        return False

    if path.startswith("profile.php"):
        qs = parse_qs(parsed.query)
        return "id" in qs and bool(qs.get("id", [""])[0])

    if path.startswith("people/"):
        parts = [part for part in path.split("/") if part]
        return len(parts) >= 3 and parts[-1].isdigit()

    if "/" in path:
        return False

    if path.endswith(".php"):
        return False

    return True


def extract_profile_id(url: str):
    url = normalize_fb_href(url)
    if not url:
        return None

    parsed = urlparse(url)

    if parsed.path.startswith("/profile.php"):
        qs = parse_qs(parsed.query)
        return qs.get("id", [None])[0]

    path = parsed.path.strip("/")
    if not path:
        return None
    if path.startswith("people/"):
        parts = [part for part in path.split("/") if part]
        if parts and parts[-1].isdigit():
            return parts[-1]
        return None
    if "/" in path:
        return None
    return path.split("/")[0]


def canonical_profile_url(url: str) -> str:
    normalized = normalize_fb_href(url)
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    profile_id = extract_profile_id(normalized)
    if parsed.path.startswith("/profile.php") and profile_id:
        return f"{BASE_URL}/profile.php?id={profile_id}"

    if parsed.path.startswith("/people/") and profile_id and profile_id.isdigit():
        return f"{BASE_URL}/profile.php?id={profile_id}"

    path = parsed.path.rstrip("/")
    return f"{BASE_URL}{path}" if path else normalized


def friend_dict_key(profile_url: str, profile_id: str | None = None) -> str:
    pid = (profile_id or "").strip()
    if pid:
        return f"PID::{pid}"
    return profile_url


# ==========================
# LOGIN HELPERS
# ==========================
def page_looks_like_login(page) -> bool:
    try:
        url = (page.url or "").lower()
        if "login" in url:
            return True
    except Exception:
        pass

    try:
        body = page.inner_text("body") or ""
    except Exception:
        return False

    body_l = body.lower()
    for m in LOGIN_MARKERS:
        if m.lower() in body_l:
            return True
    return False


def interactive_login(context, page=None):
    if page is None:
        page = context.new_page()

    print("Apro Facebook per login manuale...")
    page.goto(BASE_URL, wait_until="domcontentloaded")
    print("\n1) Nel browser appena aperto, effettua il login su Facebook (email/password, 2FA, ecc.).")
    print("2) Quando vedi la tua home, torna qui in console.")
    input("3) Premi INVIO qui quando hai finito il login... ")

    storage_path = storage_state_path()
    ensure_storage_state_parent()
    context.storage_state(path=str(storage_path))
    chmod_user_private(storage_path)
    print(f"Sessione salvata in {storage_path}")

    try:
        page.close()
    except Exception:
        pass


# ==========================
# SCROLL & SCRAPE AMICI
# ==========================
def scroll_friends_page(page, browser=None, context=None, logger: DiagLogger | None = None, diag_dir: Path | None = None):
    idle_rounds = 0
    last_height = None

    while idle_rounds < MAX_SCROLL_IDLE:
        ensure_browser_page_alive(page, "scroll_friends_page.loop_start", browser=browser, context=context, logger=logger, diag_dir=diag_dir)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(SCROLL_PAUSE_MS)

        height = page.evaluate("document.body.scrollHeight")
        if logger:
            logger.log(f"scroll.height height={height} last_height={last_height} idle_rounds={idle_rounds}")
        if last_height is not None and height == last_height:
            idle_rounds += 1
        else:
            idle_rounds = 0

        last_height = height

    print("Scroll completato.")


def _best_name_from_link(link):
    try:
        name = (link.inner_text() or "").strip()
        if name:
            return name
    except Exception:
        pass

    try:
        aria = (link.get_attribute("aria-label") or "").strip()
        if aria:
            return aria
    except Exception:
        pass

    try:
        title = (link.get_attribute("title") or "").strip()
        if title:
            return title
    except Exception:
        pass

    return ""


def _best_name_from_link_data(link_data: dict):
    for key in ("text", "aria", "title"):
        value = (link_data.get(key) or "").strip()
        if value:
            return value
    return ""


def _best_name_from_card(card):
    """Tenta di estrarre un nome da una 'card' (anche se non c'è <a> con testo)."""
    candidates = []

    # 1) testo visibile
    try:
        spans = card.query_selector_all("span[dir='auto']")
        for sp in spans:
            t = (sp.inner_text() or "").strip()
            if t:
                candidates.append(t)
    except Exception:
        pass

    # 2) aria-label su elementi cliccabili
    try:
        els = card.query_selector_all("[aria-label]")
        for el in els:
            t = (el.get_attribute("aria-label") or "").strip()
            if t:
                candidates.append(t)
    except Exception:
        pass

    # Heuristica: prendi il candidato più lungo (di solito è il nome completo)
    candidates = [c for c in candidates if len(c) <= 120]  # taglia roba lunghissima tipo frasi UI
    if not candidates:
        return ""
    return max(candidates, key=len)


def debug_cards(page, out_path: str):
    """
    Debug per trovare 'falsi negativi':
    scansiona le card della lista amici e salva:
      - nome stimato
      - eventuale href profilo trovato dentro la card
      - motivo se manca href / nome
    """
    cards = page.query_selector_all("div[role='listitem']")
    print(f"Debug cards: trovate {len(cards)} card (div[role=listitem]).")

    rows = []
    ok = 0
    no_profile_href = 0
    name_missing = 0

    for i, card in enumerate(cards):
        name_guess = _best_name_from_card(card)

        href_any = ""
        href_profile = ""
        pid = ""

        # cerca link dentro la card (a[href] e div[role=link] con data-href)
        try:
            candidates = card.query_selector_all("a[href], div[role='link']")
        except Exception:
            candidates = []

        for el in candidates:
            h = ""
            try:
                h = (el.get_attribute("href") or "").strip()
            except Exception:
                h = ""
            if not h:
                try:
                    h = (el.get_attribute("data-href") or "").strip()
                except Exception:
                    h = ""
            if not h:
                continue

            h = normalize_fb_href(h)
            if not href_any:
                href_any = h

            if is_profile_url(h):
                href_profile = h
                pid = extract_profile_id(h) or ""
                break

        if not name_guess:
            reason = "name_missing"
            name_missing += 1
        elif not href_profile:
            reason = "no_profile_href_in_card"
            no_profile_href += 1
        else:
            reason = "ok"
            ok += 1

        rows.append([i, reason, name_guess, href_profile, pid, href_any])

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["card_index", "reason", "name_guess", "profile_href", "profile_id", "any_href"])
        w.writerows(rows)

    print(f"Debug cards salvato in {out_path}. ok={ok} no_profile_href_in_card={no_profile_href} name_missing={name_missing}")


def extract_friends(page, exceptions=None, debug_rejected_path=None):
    if exceptions is None:
        exceptions = set()

    links = page.eval_on_selector_all(
        "a[href]",
        """
        els => els.map((a) => ({
            href: a.getAttribute("href") || "",
            text: (a.innerText || "").trim(),
            aria: (a.getAttribute("aria-label") || "").trim(),
            title: (a.getAttribute("title") || "").trim()
        }))
        """,
    )
    print(f"Link totali trovati: {len(links)}")

    friends = {}
    skipped_exc = 0
    rejected_rows = []

    for link in links:
        href_raw = link.get("href") or ""
        href = normalize_fb_href(href_raw)
        aria_label = link.get("aria") or ""
        title = link.get("title") or ""

        if not href:
            if debug_rejected_path:
                rejected_rows.append(("empty_href", "", "", "", "", ""))
            continue

        if not is_profile_url(href):
            if debug_rejected_path:
                rejected_rows.append(("not_profile_url", href, "", "", aria_label, title))
            continue

        pid = extract_profile_id(href) or ""
        if pid and pid in exceptions:
            skipped_exc += 1
            if debug_rejected_path:
                rejected_rows.append(("exception_filtered", href, pid, "", aria_label, title))
            continue

        name = _best_name_from_link_data(link)
        if not name:
            if debug_rejected_path:
                rejected_rows.append(("missing_name", href, pid, "", aria_label, title))
            continue

        clean_url = canonical_profile_url(href)
        friend_key = friend_dict_key(clean_url, pid)
        if friend_key:
            friends.setdefault(friend_key, (clean_url, name))

    print(f"Amici estratti (chiavi uniche): {len(friends)}")
    if exceptions:
        print(f"Profili ignorati (eccezioni): {skipped_exc}")

    if debug_rejected_path:
        try:
            with open(debug_rejected_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["reason", "href", "profile_id", "name", "aria_label", "title"])
                w.writerows(rejected_rows)
            print(f"Debug rejected salvato in {debug_rejected_path} ({len(rejected_rows)} righe).")
        except Exception as e:
            print(f"Impossibile salvare debug rejected: {e}")

    return friends


# ==========================
# CSV
# ==========================
def save_snapshot_csv(path, snapshot_date_isoz, friends_dict):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["snapshot_date", "profile_url", "profile_id", "name"])
        for key, (url, name) in sorted(friends_dict.items(), key=lambda x: x[1][1].lower()):
            pid = extract_profile_id(url) if url else None
            w.writerow([snapshot_date_isoz, url or "", pid or "", name])
    print(f"Snapshot salvato in {path}")


def load_friends_csv(path, verbose: bool = True):
    res = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = canonical_profile_url(row.get("profile_url", "") or "")
            pid = (row.get("profile_id", "") or "").strip()
            if not pid and url:
                pid = extract_profile_id(url) or ""
            name = row.get("name", "") or ""
            if not name:
                continue
            key = friend_dict_key(url, pid) if (url or pid) else f"NOURL::{name}"
            res[key] = (url, name)
    if verbose:
        print(f"Caricato snapshot precedente da {path} ({len(res)} righe).")
    return res


def select_comparison_csv(today_path, current_total_friends: int, logger: DiagLogger | None = None) -> str | None:
    previous_paths = list_previous_csv_paths(today_path)
    if not previous_paths:
        return None

    immediate_prev = previous_paths[-1]
    if len(previous_paths) < 2 or current_total_friends <= 0:
        return immediate_prev

    recent_paths = previous_paths[-5:]
    recent_counts = [(path, len(load_friends_csv(path, verbose=False))) for path in recent_paths]
    latest_count = recent_counts[-1][1]
    earlier_counts = [count for _path, count in recent_counts[:-1]]
    if not earlier_counts:
        return immediate_prev

    healthy_reference = max(earlier_counts)
    healthy_floor = max(1, int(healthy_reference * 0.75))
    latest_is_suspicious = latest_count < healthy_floor
    current_looks_healthy = current_total_friends >= healthy_floor

    if latest_is_suspicious and current_looks_healthy:
        for path, count in reversed(recent_counts[:-1]):
            if count >= healthy_floor:
                if logger:
                    logger.log(
                        f"comparison_csv.fallback immediate_prev={immediate_prev} latest_count={latest_count} "
                        f"fallback={path} fallback_count={count} current_total={current_total_friends} healthy_floor={healthy_floor}"
                    )
                return path

    if logger:
        logger.log(
            f"comparison_csv.immediate selected={immediate_prev} latest_count={latest_count} "
            f"current_total={current_total_friends} healthy_floor={healthy_floor}"
        )
    return immediate_prev


def save_diff_csv(path, snapshot_date_isoz, added, removed, visibility_map):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["snapshot_date", "change_type", "profile_url", "profile_id", "visibility", "name"])

        for key, (url, name) in sorted(added.items(), key=lambda x: x[1][1].lower()):
            pid = extract_profile_id(url) if url else None
            vis = visibility_map.get(key, "unknown")
            w.writerow([snapshot_date_isoz, "added", url or "", pid or "", vis, name])

        for key, (url, name) in sorted(removed.items(), key=lambda x: x[1][1].lower()):
            pid = extract_profile_id(url) if url else None
            vis = visibility_map.get(key, "unknown")
            w.writerow([snapshot_date_isoz, "removed", url or "", pid or "", vis, name])

    print(f"Diff salvato in {path}")


# ==========================
# DIFF
# ==========================
def compare_friend_lists(old_dict, new_dict):
    old_keys = set(old_dict.keys())
    new_keys = set(new_dict.keys())

    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys

    added = {k: new_dict[k] for k in added_keys}
    removed = {k: old_dict[k] for k in removed_keys}

    return added, removed


# ==========================
# ID NUMERICO PERSONALE
# ==========================
def get_own_numeric_id(context, browser=None, logger: DiagLogger | None = None, diag_dir: Path | None = None):
    page = new_debug_page(context, "get_own_numeric_id", logger, diag_dir)
    try:
        try:
            goto_with_debug(
                page,
                f"{BASE_URL}/me",
                "facebook_me",
                timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS,
                wait_until="domcontentloaded",
                logger=logger,
                diag_dir=diag_dir,
                browser=browser,
                context=context,
            )
            ensure_browser_page_alive(page, "facebook_me", browser=browser, context=context, logger=logger, diag_dir=diag_dir)
            ensure_known_facebook_state(page, "facebook_me", logger=logger, diag_dir=diag_dir)
            html = page.content()
        except Exception as e:
            print(f"Attenzione: lookup numeric Facebook ID saltato: {type(e).__name__}: {e}")
            if logger:
                logger.log(f"facebook_me.numeric_id skipped error={type(e).__name__}: {e}")
            return None

        m = re.search(r'"entity_id":"(\d+)"', html) or re.search(r'"userID":"(\d+)"', html)
        if m:
            own_id = m.group(1)
            out_path = output_dir_path() / "my_facebook_id.txt"
            with out_path.open("w", encoding="utf-8") as f:
                f.write(own_id + "\n")
            print(f"Il tuo numeric Facebook ID e' {own_id} (salvato in {out_path})")
            if logger:
                logger.log(f"facebook_me.numeric_id found={own_id}")
            return own_id

        print("Non sono riuscito a trovare un numeric ID nel sorgente (non e' grave).")
        if logger:
            logger.log("facebook_me.numeric_id missing_in_html")
        return None
    finally:
        safe_close(page)


# ==========================
# CHECK VISIBILITÀ PROFILI
# ==========================
def check_profile_visibility(context, url: str) -> str:
    target_url = canonical_profile_url(url) or normalize_fb_href(url)
    if not target_url:
        return "unknown"

    page = context.new_page()
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
        body_text = page.inner_text("body")
    except Exception:
        page.close()
        return "error"

    unavailable_markers = [
        "This content isn't available right now",
        "The link you followed may be broken",
    ]
    for marker in unavailable_markers:
        if marker in body_text:
            page.close()
            return "unavailable"

    if any(marker.lower() in (body_text or "").lower() for marker in LOGIN_MARKERS):
        page.close()
        return "unknown"

    page.close()
    return "visible"


def update_friend_requests_status(conn, friends_dict: dict) -> None:
    """Aggiorna lo stato delle richieste amicizia (tabella friend_requests) confrontandole con la lista amici attuale.

    - pending/unknown + ora tra amici -> accepted (+accepted_at)
    - non possiamo inferire rejected automaticamente
    - se profile_id manca, prova a derivarlo dal profile_url e salvarlo
    """
    now = iso_utc_z()

    friend_pids = set()
    friend_urls = set()
    for _k, (url, _name) in friends_dict.items():
        canonical_url = canonical_profile_url(url)
        if canonical_url:
            friend_urls.add(canonical_url)
            pid = extract_profile_id(canonical_url)
            if pid:
                friend_pids.add(pid)

    rows = conn.execute(
        """
        SELECT id, profile_id, profile_url, status
        FROM friend_requests
        WHERE status IN ('pending','unknown')
        """
    ).fetchall()

    if not rows:
        return

    checked = 0
    accepted_now = 0
    derived_pid = 0

    for r in rows:
        rid = int(r["id"])
        pid = (r["profile_id"] or "").strip()
        url = canonical_profile_url((r["profile_url"] or "").strip())
        status = (r["status"] or "pending").strip()

        if (not pid) and url:
            dp = extract_profile_id(url)
            if dp:
                pid = dp
                conn.execute("UPDATE friend_requests SET profile_id=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (pid, rid))
                derived_pid += 1

        checked += 1

        is_friend = False
        if pid and pid in friend_pids:
            is_friend = True
        elif url and url in friend_urls:
            is_friend = True

        if is_friend and status != "accepted":
            conn.execute(
                """
                UPDATE friend_requests
                SET status='accepted',
                    accepted_at=COALESCE(accepted_at, ?),
                    last_checked_at=?
                WHERE id=?
                """,
                (now, now, rid),
            )
            accepted_now += 1
        else:
            conn.execute("UPDATE friend_requests SET last_checked_at=? WHERE id=?", (now, rid))

    conn.commit()
    print(f"Richieste amicizia: checked={checked} accepted_now={accepted_now} derived_profile_id={derived_pid}")


# ==========================
# MAIN
# ==========================
def parse_args():
    p = argparse.ArgumentParser(description="Snapshot amici Facebook + diff giornaliero + SQLite.")
    p.add_argument("--login", action="store_true", help="Forza login manuale e riscrive fb_storage_state.json.")
    p.add_argument("--headless", action="store_true", help="Avvia Chromium in modalita' headless.")
    p.add_argument("--debug-rejected", action="store_true", help="Salva CSV con i link scartati e il motivo.")
    p.add_argument("--debug-cards", action="store_true", help="Salva CSV con le 'card' amici e motivi (utile per falsi negativi).")
    p.add_argument("--db", default=os.environ.get("AMICI_FB_DB"), help="Path DB sqlite (se relativo, rispetto alla cartella dello script).")
    return p.parse_args()


def safe_close(resource) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception:
        pass


def format_run_diff(old_friends, friends, added, removed, csv_path):
    previous = str(len(old_friends)) if old_friends is not None else "non disponibile"
    delta = f"{len(friends) - len(old_friends):+d}" if old_friends is not None else "non disponibile"
    lines = [f"Amici: {previous} → {len(friends)} (variazione netta: {delta})"]
    if old_friends is None:
        lines.append("Primo snapshot: nessun confronto precedente disponibile.")
    elif not added and not removed:
        lines.append("Nessuna variazione.")
    else:
        for label, changes in (("Rimossi", removed), ("Aggiunti", added)):
            lines.append(f"{label}: {len(changes)}")
            for url, name in sorted(changes.values(), key=lambda item: item[1].lower()):
                lines.append(f"  {name} — {url or '(nessun URL)'}")
    lines.append(f"CSV: {csv_path}")
    return "\n".join(lines)


def main():
    args = parse_args()
    ensure_output_dir()
    telegram_enabled = True
    try:
        telegram_notify.validate_config()
    except Exception as e:
        telegram_enabled = False
        print(f"Telegram non configurato: notifiche disabilitate ({e})", file=sys.stderr, flush=True)
    else:
        print(f"Telegram config OK: {telegram_notify.config_summary()}", flush=True)

    now_dt = utc_now_naive()
    snapshot_date_isoz = iso_utc_z(now_dt)
    run_stamp = filename_stamp_utc(now_dt)
    today_path = snapshot_csv_path(run_stamp)
    prev_path = find_previous_csv(today_path)

    added = {}
    removed = {}
    total_friends = 0
    diff_path = None
    debug_rejected_path = debug_rejected_csv_path(run_stamp) if args.debug_rejected else None
    debug_cards_path = debug_cards_csv_path(run_stamp) if args.debug_cards else None
    diag_dir = browser_diag_dir_path(run_stamp)
    diag_logger = DiagLogger(diag_dir / "trace.log")
    headless_mode = args.headless or DEFAULT_HEADLESS

    conn = None
    browser = None
    context = None
    page = None
    old_friends = None
    browser_watcher = None
    comparison_prev_path = None

    diag_logger.log(f"run.start headless={headless_mode} login={args.login} cwd={SCRIPT_DIR}")
    diag_logger.log(f"run.paths snapshot_csv={today_path} prev_csv={prev_path} diag_dir={diag_dir}")

    try:
        conn = get_db_conn(getattr(args, "db", None))
        init_db(conn)
        exceptions = load_exceptions(conn)

        if not prev_path:
            print("Nessun snapshot precedente trovato (prima esecuzione o cambio formato file).")

        with sync_playwright() as p:
            diag_logger.log("playwright.start")
            storage_path = storage_state_path()
            profile_dir = ensure_chromium_profile_dir()
            profile_was_initialized = chromium_profile_initialized(profile_dir)

            if not args.login and headless_mode and (not profile_was_initialized) and (not storage_path.exists()):
                legacy_path = legacy_storage_state_path()
                if legacy_path.exists():
                    print(
                        f"Sessione legacy trovata in {legacy_path}, ma Fedora usa {storage_path}. "
                        "Esegui il primo login locale se la sessione Fedora non esiste.",
                        flush=True,
                    )
                raise RuntimeError("Nessuna sessione Facebook salvata: riesegui senza --headless usando --login.")

            launch_kwargs = chromium_launch_kwargs(headless_mode)
            diag_logger.log(
                f"launch.start browser_type=chromium persistent_profile={profile_dir} "
                f"profile_initialized={profile_was_initialized} kwargs={launch_kwargs}"
            )
            context = p.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
            browser = context.browser
            if browser is not None:
                browser_watcher = BrowserProcessWatcher(browser, diag_logger)
                browser_watcher.start()
            attach_context_debug(context, diag_logger, diag_dir)
            log_browser_state(diag_logger, "launch.done", browser=browser, context=context)

            if args.login:
                if headless_mode:
                    raise RuntimeError("Login manuale richiesto: riesegui senza --headless usando --login.")
                diag_logger.log(f"persistent_context.ready mode=manual_login profile={profile_dir}")
                interactive_login(context)
            else:
                if profile_was_initialized:
                    diag_logger.log(f"persistent_context.ready mode=profile profile={profile_dir}")
                    print(f"Uso profilo Chromium persistente da {profile_dir}")
                elif storage_path.exists():
                    chmod_user_private(storage_path)
                    diag_logger.log(f"persistent_context.restore_from_storage_state path={storage_path}")
                    restore_storage_state_to_context(context, storage_path, diag_logger)
                    print(f"Uso sessione salvata da {storage_path} e profilo Chromium persistente {profile_dir}")
                else:
                    diag_logger.log(f"persistent_context.ready mode=no_storage_state profile={profile_dir}")
                    interactive_login(context)
                if getattr(context, "_amici_fb_debug_attached", False) is False:
                    attach_context_debug(context, diag_logger, diag_dir)
                log_browser_state(diag_logger, "persistent_context.done", browser=browser, context=context)

            get_own_numeric_id(context, browser=browser, logger=diag_logger, diag_dir=diag_dir)

            page = new_debug_page(context, "friends_main", diag_logger, diag_dir)
            print(f"Vado alla pagina degli amici: {FRIENDS_URL}")
            goto_with_debug(
                page,
                FRIENDS_URL,
                "facebook_friends",
                timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS,
                wait_until="commit",
                logger=diag_logger,
                diag_dir=diag_dir,
                browser=browser,
                context=context,
            )
            ensure_browser_page_alive(page, "facebook_friends.after_goto", browser=browser, context=context, logger=diag_logger, diag_dir=diag_dir)
            try:
                page.locator("body").wait_for(timeout=POST_FRIENDS_GOTO_TIMEOUT_MS)
                diag_logger.log("facebook_friends.body_ready")
            except Exception as e:
                saved = save_page_artifacts(page, diag_dir, "facebook_friends_body_wait_failure", diag_logger)
                log_browser_state(diag_logger, f"facebook_friends.body_wait_failed error={type(e).__name__}: {e} artifacts={saved}", browser=browser, context=context, page=page)
                raise

            special_state, _details = detect_facebook_special_state(page)
            if special_state == "login":
                diag_logger.log("facebook_friends.login_detected requesting_manual_login")
                if headless_mode:
                    raise RuntimeError("Sessione Facebook scaduta: riesegui senza --headless usando --login.")
                print("\nAttenzione: Facebook sta mostrando la schermata di login (sessione scaduta).")
                print("Ti lascio loggare manualmente; poi riprovo a caricare la pagina amici.")
                interactive_login(context, page=page)
                page = new_debug_page(context, "friends_main_after_login", diag_logger, diag_dir)
                goto_with_debug(
                    page,
                    FRIENDS_URL,
                    "facebook_friends_after_login",
                    timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS,
                    wait_until="commit",
                    logger=diag_logger,
                    diag_dir=diag_dir,
                    browser=browser,
                    context=context,
                )
                ensure_browser_page_alive(page, "facebook_friends_after_login", browser=browser, context=context, logger=diag_logger, diag_dir=diag_dir)
                page.locator("body").wait_for(timeout=POST_FRIENDS_GOTO_TIMEOUT_MS)
                ensure_known_facebook_state(page, "facebook_friends_after_login", logger=diag_logger, diag_dir=diag_dir)
            else:
                ensure_known_facebook_state(page, "facebook_friends", logger=diag_logger, diag_dir=diag_dir)

            scroll_friends_page(page, browser=browser, context=context, logger=diag_logger, diag_dir=diag_dir)

            if debug_cards_path:
                debug_cards(page, debug_cards_path)

            friends = extract_friends(page, exceptions, debug_rejected_path=debug_rejected_path)
            total_friends = len(friends)

            if prev_path:
                comparison_prev_path = select_comparison_csv(today_path, total_friends, logger=diag_logger)
                if comparison_prev_path:
                    if comparison_prev_path != prev_path:
                        print(f"Snapshot precedente immediato {prev_path} ignorato per diff: uso {comparison_prev_path}")
                    old_friends = load_friends_csv(comparison_prev_path)

            save_snapshot_csv(today_path, snapshot_date_isoz, friends)

            snapshot_id = db_insert_snapshot(conn, snapshot_date_isoz, total_friends)

            for _key, (url, name) in friends.items():
                pid = extract_profile_id(url) if url else None
                if not pid:
                    continue
                fid = db_upsert_friend(conn, pid, url, name, snapshot_id)
                db_insert_snapshot_friend(conn, snapshot_id, fid, name, url)
            conn.commit()

            try:
                update_friend_requests_status(conn, friends)
            except Exception as e:
                print(f"Attenzione: update_friend_requests_status fallita: {e}")

            visibility_map = {}

            if old_friends is not None:
                added, removed = compare_friend_lists(old_friends, friends)

                if CHECK_REMOVED_VISIBILITY and removed:
                    print("\nControllo visibilita' profili per gli amici 'rimossi'...")
                    for key, (url, name) in removed.items():
                        status = check_profile_visibility(context, url)
                        visibility_map[key] = status
                        print(f"  {name} -> {url or '(nessun URL)'} -> {status}")

                for key in added.keys():
                    visibility_map.setdefault(key, "unknown")
                for key in removed.keys():
                    visibility_map.setdefault(key, visibility_map.get(key, "unknown"))

                diff_path = diff_csv_path(comparison_prev_path or prev_path, today_path)
                save_diff_csv(diff_path, snapshot_date_isoz, added, removed, visibility_map)

            comparison_prev_snapshot_id = None
            if comparison_prev_path:
                comparison_prev_snapshot_id = db_get_snapshot_id_by_snapshot_date(conn, snapshot_date_from_csv_path(comparison_prev_path))

            prev_snapshot_id = comparison_prev_snapshot_id or db_get_prev_snapshot_id(conn, snapshot_id)
            if prev_snapshot_id is not None:
                curr_set = db_get_friend_ids_for_snapshot(conn, snapshot_id)
                prev_set = db_get_friend_ids_for_snapshot(conn, prev_snapshot_id)

                added_ids = curr_set - prev_set
                removed_ids = prev_set - curr_set

                for fid in added_ids:
                    db_insert_diff_event(conn, snapshot_id, prev_snapshot_id, "added", fid, "unknown")

                for fid in removed_ids:
                    # The CSV diff path already performs optional visibility checks.
                    # Avoid a second Facebook navigation here; in scheduled runs it can
                    # outlive the useful work and block Telegram notifications.
                    db_insert_diff_event(conn, snapshot_id, prev_snapshot_id, "removed", fid, "unknown")

                conn.commit()
    finally:
        diag_logger.log("run.cleanup.start")
        if browser_watcher is not None:
            browser_watcher.stop()
            diag_logger.log("browser_process_watcher.stop")
        safe_close(page)
        safe_close(context)
        safe_close(browser)
        safe_close(conn)
        diag_logger.log("run.cleanup.done")

    report = format_run_diff(old_friends, friends, added, removed, today_path)

    if not telegram_enabled:
        diag_logger.log("telegram_notify.skipped not_configured")
        return report

    try:
        added_count = len(added)
        removed_count = len(removed)
        if old_friends is not None and added_count == 0 and removed_count == 0:
            # Preserve the weekly heartbeat; suppress ordinary no-op alerts.
            telegram_notify.send_alert(
                "Amici Facebook", "Nessuna variazione", event_key="amici_fb:unchanged", no_op=True
            )
            if heartbeat_due(output_dir_path()):
                telegram_notify.notify(
                    f"Controllo periodico OK. Amici estratti: {total_friends}. "
                    "Nessuna variazione nell'ultimo controllo.",
                    prefix="FB-Scraper",
                    project_id=1,
                )
                mark_telegram_sent(output_dir_path())
                diag_logger.log("telegram_notify.heartbeat.sent")
            else:
                diag_logger.log("telegram_notify.skipped no_friend_changes")
            return report

        msg_lines = [
            "La lista amici è stata aggiornata.",
            f"Aggiunti: {added_count}",
            f"Rimossi: {removed_count}",
            f"Totale attuale: {total_friends}",
            "Controlla il CSV allegato per i dettagli." if diff_path and (added_count or removed_count) else "Nessuna azione necessaria.",
        ]
        delivery = telegram_notify.send_alert(
            "Amici Facebook",
            "\n".join(msg_lines),
            event_key=f"amici_fb:snapshot:{snapshot_date_isoz}",
            severity="warning" if removed_count else "info",
        )
        if delivery != "suppressed":
            if diff_path and (added_count or removed_count):
                telegram_notify.send_file(diff_path, caption="Dettaglio delle variazioni degli amici", project_id=1)
            mark_telegram_sent(output_dir_path())
        diag_logger.log("telegram_notify.sent")
    except Exception as e:
        err = f"telegram_notify.failed {type(e).__name__}: {e}"
        print(err, file=sys.stderr)
        diag_logger.log(err)
        raise

    return report

if __name__ == "__main__":
    CLI_RESULT = main()
    if CLI_RESULT:
        print(CLI_RESULT, flush=True)
