from __future__ import annotations

import argparse
import base64
import binascii
import errno
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import OrderedDict
import ctypes
from email.message import EmailMessage
from email.policy import SMTP
import fnmatch
import gzip
import hashlib
import html
from functools import lru_cache
import importlib.util
import json
import math
import mimetypes
import ntpath
import os
import posixpath
import queue
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pad_service
import quota_service
import hermes_bridge
import hermes_images
import upload_service
import transcript_changes
import transcript_snapshot
import ws_frame
import seat_activity
import pc_status
import picker_screen
import skill_shelf
import skill_suggest
import skill_files
import agent_design_catalog
from input_injector import InjectorUnavailable
from mycmux_socket import MycmuxError, MycmuxSocket, StatusFeedThread, Unauthorized


ROOT = Path(__file__).resolve().parent
WEB_ROOT = (ROOT / "web").resolve()


def resolve_state_dir(argv: list[str] | None = None, environ: Any = None) -> Path:
    """Where this server keeps ``_state``: --state-dir, then POCKET_STATE_DIR.

    mycmux writes ``allowed_logins.json`` next to the ``server.py`` it was
    configured to launch, and the running server reads it next to its own
    ``__file__``. The two only agree while the command names a real .py file,
    which the sidecar ruling (D1=B, single executable) takes away, so the
    folder has to be nameable from outside instead of derived from the code's
    own location. Read from argv rather than from the parsed arguments because
    every path below is bound while this module is imported, long before
    ``main()`` runs.
    """
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    value = None
    for index, item in enumerate(argv):
        if item.startswith("--state-dir="):
            value = item.split("=", 1)[1]
        elif item == "--state-dir" and index + 1 < len(argv):
            value = argv[index + 1]
    value = value or environ.get("POCKET_STATE_DIR")
    if not value:
        return ROOT / "_state"
    return Path(value).expanduser().resolve()


#: Every ``_state`` path below hangs off this one value, so that naming the
#: folder once moves the whole set (allowed_logins.json and devices.json
#: included).
STATE_DIR = resolve_state_dir()

# The files that make the app. A build is what these bytes are, not a number
# anyone remembers to raise: the cache version was bumped by hand five times in
# one night and there was still no way to say which of them a phone was
# running. A hash of the bytes cannot disagree with what was served.
BUILD_ASSETS = (
    "index.html", "app.js", "pad.js", "style.css", "strings.ja.js",
    "sw.js", "manifest.json",
)
_BUILD_CACHE: dict[str, Any] = {"stamp": None, "id": ""}


def build_id() -> str:
    """Eight hex characters naming exactly the bytes now on disk."""
    stamp = []
    for name in BUILD_ASSETS:
        target = WEB_ROOT / name
        try:
            info = target.stat()
        except OSError:
            stamp.append((name, None, None))
            continue
        stamp.append((name, info.st_mtime_ns, info.st_size))
    key = tuple(stamp)
    if _BUILD_CACHE["stamp"] == key and _BUILD_CACHE["id"]:
        return str(_BUILD_CACHE["id"])
    digest = hashlib.blake2b(digest_size=8)
    for name in BUILD_ASSETS:
        target = WEB_ROOT / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(target.read_bytes())
        except OSError:
            digest.update(b"\0")
    value = digest.hexdigest()[:8]
    _BUILD_CACHE["stamp"] = key
    _BUILD_CACHE["id"] = value
    return value


BUILD_ID_RE = re.compile(r"\A[0-9a-f]{1,16}\Z")


def record_client_build(reported: str, current: str, agent: str) -> str:
    """One line saying which build a phone had loaded, and when.

    Only the shapes the app itself sends are written down, so nothing a caller
    invents ends up in the file. Kept small on purpose: this is a log to read
    after someone says it does not work, not a dataset.
    """
    seen = (reported or "").strip().lower()
    if not BUILD_ID_RE.match(seen):
        seen = "unknown"
    line = "\t".join([
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        seen,
        current,
        "stale" if seen not in {current, "unknown"} else "current",
        re.sub(r"[\t\r\n]", " ", agent or "")[:120],
    ])
    try:
        CLIENTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if CLIENTS_LOG_PATH.exists() and CLIENTS_LOG_PATH.stat().st_size > CLIENTS_LOG_MAX_BYTES:
            kept = CLIENTS_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            CLIENTS_LOG_PATH.write_text("\n".join(kept[-500:]) + "\n", encoding="utf-8")
        with CLIENTS_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    return seen


SENDS_LOG_PATH = STATE_DIR / "sends.log"
SENDS_LOG_MAX_BYTES = 256 * 1024
_SENDS_LOG_LOCK = threading.Lock()


def record_send_attempt(record: dict[str, Any]) -> None:
    """Only counts and controlled result codes; never the message or image."""
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    try:
        with _SENDS_LOG_LOCK:
            SENDS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if SENDS_LOG_PATH.exists() and SENDS_LOG_PATH.stat().st_size + len(line) > SENDS_LOG_MAX_BYTES:
                # Bound the read as well as the retained file, even after an old oversized log.
                with SENDS_LOG_PATH.open("rb") as handle:
                    handle.seek(max(0, SENDS_LOG_PATH.stat().st_size - SENDS_LOG_MAX_BYTES // 2))
                    kept = handle.read().splitlines()[1:][-250:]
                SENDS_LOG_PATH.write_bytes(b"\n".join(kept) + (b"\n" if kept else b""))
            with SENDS_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass


def send_normalization_count(value: str) -> int:
    return sum(len(match.group()) for match in re.finditer(
        r"\r\n|[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029]", value))


def send_image_bytes(text: str) -> tuple[bool, int]:
    root = UPLOAD_ROOT.resolve()
    total = 0
    present = False
    for line in text.splitlines():
        candidate = line.strip().strip('"')
        if not candidate.lower().replace("\\", "/").startswith(str(root).lower().replace("\\", "/") + "/"):
            continue
        present = True
        try:
            path = Path(candidate).resolve()
            if path.is_relative_to(root) and path.is_file():
                total += path.stat().st_size
        except (OSError, ValueError):
            pass
    return present, total


FAULTS_LOG_PATH = STATE_DIR / "faults.log"
FAULTS_LOG_MAX_BYTES = 512 * 1024
FAULT_KINDS = frozenset({"error", "rejection", "request", "boot", "attachment"})


def record_fault(payload: dict[str, Any], current: str, agent: str) -> None:
    """One line per thing that went wrong on a phone.

    Only the four fields the app sends, each cut short. The detail can carry a
    fragment of whatever the app was holding when it broke, so this file stays
    on this machine and nothing here is read back into a page.
    """
    def field(name: str, limit: int) -> str:
        value = payload.get(name)
        if not isinstance(value, str):
            return ""
        return re.sub(r"[\t\r\n]+", " ", value).strip()[:limit]

    kind = field("kind", 16)
    if kind not in FAULT_KINDS:
        kind = "error"
    seen = field("build", 16)
    if not BUILD_ID_RE.match(seen.lower()):
        seen = "unknown"
    line = "\t".join([
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        kind,
        seen,
        current,
        field("route", 80) or "-",
        field("detail", 300) or "-",
        re.sub(r"[\t\r\n]", " ", agent or "")[:80],
    ])
    try:
        FAULTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if FAULTS_LOG_PATH.exists() and FAULTS_LOG_PATH.stat().st_size > FAULTS_LOG_MAX_BYTES:
            kept = FAULTS_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            FAULTS_LOG_PATH.write_text("\n".join(kept[-800:]) + "\n", encoding="utf-8")
        with FAULTS_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


BUILD_META_ANCHOR = b'<meta name="viewport"'
WORKER_CACHE_RE = re.compile(rb'(const CACHE_NAME = "pocket-static-)[^"]*(")')


def stamp_worker(payload: bytes, build: str) -> bytes:
    """Name the worker's cache after the build.

    A browser installs a new worker only when sw.js changes, and the cache is
    only replaced when its name changes, so both were a number raised by hand -
    which means both were a number that could be forgotten. Deriving them from
    the bytes makes the forgetting impossible.
    """
    return WORKER_CACHE_RE.sub(rb"\g<1>" + build.encode("ascii") + rb"\g<2>", payload, count=1)


def stamp_build(payload: bytes, build: str) -> bytes:
    """Put the build into the shell that carries it.

    The page has to know which build it *is*, not which build is current, and
    the only moment those are the same is when the bytes leave the server. The
    service worker caches whatever it got, so the stamp travels with the cached
    shell and stays true however old it becomes.
    """
    if BUILD_META_ANCHOR not in payload:
        return payload
    tag = ('<meta name="pocket-build" content="%s">\n  ' % build).encode("ascii")
    return payload.replace(BUILD_META_ANCHOR, tag + BUILD_META_ANCHOR, 1)


def _machine_defaults(os_name: str = os.name) -> dict[str, Any]:
    """Per-machine settings that cannot be the same on Windows and macOS.

    Everything is derived from ``Path.home()`` rather than a literal user
    directory, so the same file works for either host. ``_state/machine.json``
    overrides any key; see ``_load_machine_config``. ``os_name`` is a seam for
    tests: patching ``os.name`` itself breaks ``pathlib`` on the other platform.
    """
    home = Path.home()
    if os_name == "nt":
        return {
            "bridgeCli": ROOT / "scripts" / "mycmux_agent_cli_v088.py",
            "allowedFileRoots": [home],
            "launchCwdRoot": home,
            "launchPresets": [os.fspath(home)],
            "publicOrigin": "",
            "launchShell": ["powershell", "-NoLogo", "-NoExit"],
            "features": {"mail": False, "research": False},
            "hubSend": STATE_DIR / "optional" / "hub_send.py",
            "researchArchives": {"downloads": home / "Downloads"},
            "gwsCli": home / "bin" / "gws.exe",
            "hermes": {},
        }
    return {
        "bridgeCli": home / "mycmux" / "scripts" / "mycmux_agent_cli.py",
        "allowedFileRoots": [home],
        "launchCwdRoot": home,
        "launchPresets": [os.fspath(home)],
        "publicOrigin": "",
        # A login shell, so the PATH that starts claude/codex is the one the
        # operator gets in Terminal.
        "launchShell": ["/bin/zsh", "-l"],
        "features": {"mail": False, "research": False},
        "hubSend": STATE_DIR / "optional" / "hub_send.py",
        "researchArchives": {"downloads": home / "Downloads"},
        "gwsCli": home / "bin" / "gws",
        "hermes": {},
    }


MACHINE_CONFIG_PATH = Path(
    os.environ.get("POCKET_MACHINE_CONFIG") or STATE_DIR / "machine.json"
)


def _tailscale_cli(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    candidates = {
        "win32": (
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ),
        "darwin": (
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            "/Applications/Tailscale.app/Contents/MacOS/tailscale",
            "/usr/local/bin/tailscale",
            "/opt/homebrew/bin/tailscale",
        ),
    }.get(platform, ())
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("tailscale")
    if found:
        return found
    raise FileNotFoundError("Tailscale CLI not found")


def read_tailscale_status(timeout: float = 2) -> dict[str, Any]:
    """Read only; the caller can replace this function without invoking Serve."""
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    completed = subprocess.run(
        [_tailscale_cli(), "status", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        check=True, **options,
    )
    status = json.loads(completed.stdout)
    if not isinstance(status, dict) or not isinstance(status.get("Self"), dict):
        raise ValueError("Tailscale status has no Self object")
    return status


def tailscale_login(status: dict[str, Any]) -> str | None:
    own = status.get("Self") or {}
    users = status.get("User") or {}
    if not isinstance(own, dict) or not isinstance(users, dict):
        return None
    user = users.get(str(own.get("UserID")))
    login = user.get("LoginName") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None


def tailscale_machine(status: dict[str, Any]) -> dict[str, Any]:
    own = status.get("Self")
    own = own if isinstance(own, dict) else {}
    name, dns = own.get("HostName"), own.get("DNSName")
    return {
        "name": name if isinstance(name, str) and name else socket.gethostname(),
        "os": {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux"),
        "dnsName": dns.rstrip(".") or None if isinstance(dns, str) else None,
    }


def public_origin_for(machine: dict[str, Any]) -> str | None:
    """この要求の端末の公開URLを返す。未設定の既定値は配らない。"""
    dns = machine.get("dnsName")
    if dns:
        return "https://" + dns
    if MACHINE["_publicOriginSource"] == "legacy default":
        return None
    return PUBLIC_ORIGIN


class TailscaleStatusCache:
    def __init__(self, reader=None, monotonic=None, retry_wait=None) -> None:
        self.reader = reader
        self.monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._checked: float | None = None
        self._status: dict[str, Any] = {}
        self._refreshing = False
        self._worker_running = False
        self._refresh_done = threading.Event()
        self._stop = threading.Event()
        self._retry_wait = retry_wait or self._stop.wait
        self._last_logged_error: str | None = None
        self._last_logged_at: float | None = None
        self.error: str | None = None

    def _read(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            status = self.reader() if self.reader is not None else read_tailscale_status(timeout=10)
            if not isinstance(status, dict) or not isinstance(status.get("Self"), dict):
                raise ValueError("Tailscale status has no Self object")
            if not tailscale_login(status):
                return status, "Self.UserID has no User.LoginName"
            return status, None
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
            # Never put raw CLI output (which may include credentials) in logs.
            return None, type(exc).__name__

    def _apply(self, status: dict[str, Any] | None, error: str | None) -> None:
        if status is not None and (error is None or not self._status):
            self._status = status
        self.error = error
        self._checked = self.monotonic()

    def _log_failure(self) -> None:
        # Import-time machine configuration can finish before the logger is defined.
        logger = globals().get("_log_server_error")
        if logger is None:
            return
        with self._lock:
            reason = self.error
            if reason is None:
                return
            now = self.monotonic()
            if (reason == self._last_logged_error and self._last_logged_at is not None
                    and now - self._last_logged_at < 60):
                return
            self._last_logged_error = reason
            self._last_logged_at = now
        logger("Tailscale identity unavailable: " + reason)

    def _refresh_background(self) -> None:
        while True:
            status, error = self._read()
            with self._lock:
                self._apply(status, error)
                good = bool(tailscale_login(self._status))
                self._refreshing = False
                self._refresh_done.set()
                if good:
                    self._worker_running = False
            if error is not None:
                self._log_failure()
            if good or self._retry_wait(5):
                with self._lock:
                    self._worker_running = False
                return
            with self._lock:
                self._refreshing = True
                self._refresh_done = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._worker_running:
                return
            if (tailscale_login(self._status) and self._checked is not None
                    and self.monotonic() - self._checked < 60):
                return
            self._worker_running = True
            self._refreshing = True
            self._refresh_done = threading.Event()
        try:
            threading.Thread(target=self._refresh_background, daemon=True,
                             name="pocket-tailscale-status").start()
        except RuntimeError:
            with self._lock:
                self._worker_running = False
                self._refreshing = False
                self._refresh_done.set()

    def stop(self) -> None:
        self._stop.set()

    def get(self) -> dict[str, Any]:
        self.start()
        with self._lock:
            if tailscale_login(self._status):
                return self._status
            pending = self._refresh_done if self._refreshing else None
        if pending is not None:
            pending.wait(3)
        with self._lock:
            return self._status


TAILSCALE_STATUS = TailscaleStatusCache()
DEFAULT_HUB_ORIGIN = ""


def parse_origin(value: str) -> str | None:
    """Scheme, host and optional port only. A path makes it not an origin.

    The phone throws the path away (Machine.origin() flattens it to "/") while
    this server would keep it as a prefix, so a hub moved to a sub-path would
    be read from two different places with a 404 as the only symptom.
    """
    try:
        parts = urlsplit(value.strip())
        parts.port  # Raises on a port that is not a number; that is the check.
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.path.strip("/"):
        return None
    # netloc rather than hostname, so that an [::1] style host keeps its brackets.
    return parts.scheme + "://" + parts.netloc


def _load_machine_config(tailscale_status: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = _machine_defaults()
    status = TAILSCALE_STATUS.get() if tailscale_status is None else tailscale_status
    dns = tailscale_machine(status)["dnsName"]
    settings["_publicOriginSource"] = "legacy default"
    settings["hubOrigin"] = DEFAULT_HUB_ORIGIN
    if dns:
        settings["publicOrigin"] = "https://" + dns
        settings["_publicOriginSource"] = "tailscale Self.DNSName"
    try:
        stored = json.loads(MACHINE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return settings
    if not isinstance(stored, dict):
        return settings
    if isinstance(stored.get("publicOrigin"), str) and stored["publicOrigin"]:
        settings["publicOrigin"] = stored["publicOrigin"].rstrip("/")
        settings["_publicOriginSource"] = "machine.json"
    if isinstance(stored.get("hubOrigin"), str) and stored["hubOrigin"]:
        origin = parse_origin(stored["hubOrigin"])
        if origin is None:
            settings["_hubOriginError"] = (
                "machine.json hubOrigin is not an origin (scheme, host and optional"
                " port only); keeping " + settings["hubOrigin"]
            )
        else:
            settings["hubOrigin"] = origin
    for key in ("bridgeCli", "launchCwdRoot"):
        if isinstance(stored.get(key), str) and stored[key]:
            settings[key] = Path(stored[key])
    if isinstance(stored.get("allowedFileRoots"), list):
        roots = [Path(item) for item in stored["allowedFileRoots"] if isinstance(item, str)]
        if roots:
            settings["allowedFileRoots"] = roots
    for key in ("launchPresets", "launchShell"):
        if isinstance(stored.get(key), list):
            values = [item for item in stored[key] if isinstance(item, str)]
            if values:
                settings[key] = values
    if isinstance(stored.get("features"), dict):
        for name, value in stored["features"].items():
            if name in settings["features"] and isinstance(value, bool):
                settings["features"][name] = value
    # Machine-local only: the key reaches an endpoint that runs terminal-capable
    # work as the Mac's own user, so it lives in _state (gitignored) and never
    # in the defaults below.
    if isinstance(stored.get("hermes"), dict):
        settings["hermes"] = {
            name: value for name, value in stored["hermes"].items() if isinstance(value, str)
        }
    return settings


MACHINE = _load_machine_config()
#: Per-user helper paths all hang off the home directory, so the same source
#: file resolves correctly on either machine.
HOME = Path.home()
BRIDGE_CLI = MACHINE["bridgeCli"]
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
HYBRID_TRANSCRIPT_ROOT = Path.home() / ".claude-codex" / "config" / "projects"
CODEX_TRANSCRIPT_ROOT = Path.home() / ".codex" / "sessions"
MYCMUX_RUNTIME_ROOT = Path(
    os.environ.get("MYCMUX_RUNTIME_DIR") or Path.home() / ".mycmux"
).resolve()
PANE_SESSION_MAPPING_ROOT = MYCMUX_RUNTIME_ROOT / "pane-sessions"
MAIL_SNAPSHOT = STATE_DIR / "optional" / "mail-snapshot.json"
MAIL_STANDARDS = STATE_DIR / "optional" / "mail-standards.md"
MAIL_CHECKER = STATE_DIR / "optional" / "check_mail.py"
MAIL_PROMPT = ROOT / "prompts" / "mail_draft_prompt.md"
MAIL_REVISE_PROMPT = ROOT / "prompts" / "mail_revise_prompt.md"
MAIL_JOBS_PATH = STATE_DIR / "mail_jobs.json"
MAIL_HIDDEN_PATH = STATE_DIR / "mail_hidden.json"
#: The pictures a phone sent to the harness room, kept so the room can show
#: them again (hermes_images). Machine-local like everything else in _state.
HERMES_IMAGES_DIR = STATE_DIR / "hermes_images"
MAIL_SENDS_PATH = STATE_DIR / "mail_sends.json"
LAUNCHES_PATH = STATE_DIR / "launches.json"
RESEARCH_JOBS_PATH = STATE_DIR / "research_jobs.json"
UPLOAD_ROOT = STATE_DIR / "uploads"
# Which build each phone is actually running. Three rounds of "it does not
# work" were answered with measurements taken against whatever was on disk at
# the time, and there was never any way to say what the phone had loaded.
CLIENTS_LOG_PATH = STATE_DIR / "clients.log"
CLIENTS_LOG_MAX_BYTES = 256 * 1024
RESEARCH_ROOT = HOME / "reports" / "_research"
RESEARCH_QUICK_PROMPT = ROOT / "prompts" / "research_quick.md"
RESEARCH_RECENT_PROMPT = ROOT / "prompts" / "research_recent.md"
RESEARCH_DEEP_PROMPT = ROOT / "prompts" / "research_deep.md"
RESEARCH_SALVAGE_PROMPT = ROOT / "prompts" / "research_salvage.md"
RESEARCH_QUESTION_PROMPT = ROOT / "prompts" / "research_question.md"
RESEARCH_SOURCE_PREAMBLE = ROOT / "prompts" / "research_source_preamble.md"
RESEARCH_QUICK_HTML = STATE_DIR / "optional" / "quick_html.py"
RESEARCH_HUB_SEND = MACHINE["hubSend"]
RESEARCH_ARCHIVES = MACHINE["researchArchives"]
RESEARCH_UDR_ROOT = STATE_DIR / "optional" / "research"
RESEARCH_UDR_SCRIPTS = RESEARCH_UDR_ROOT / "scripts"
RESEARCH_QUESTION_TEMPLATE = RESEARCH_UDR_ROOT / "references" / "question-template.md"
RESEARCH_TIMEOUTS = {"quick": 600, "recent": 900, "deep": 2400}
RESEARCH_BUDGET_MINUTES = {"quick": 7, "deep": 28}
RESEARCH_SALVAGE_TIMEOUT = 300
#: Filing the report has to reach the hub itself, and on the Mac that means
#: Google Drive, where the first touch of anything not already cached costs
#: seconds per file. A cold run went past ten minutes on 2026-09-01 and was
#: killed with the document half-filed; a warm one finishes in under a second.
#: Windows reads the same hub off a local mirror, so its budget stays as it was.
RESEARCH_SEND_TIMEOUT = 600 if os.name == "nt" else 2400
RESEARCH_MIN_REPORT_CHARS = 3000
RESEARCH_FILE_PREFERENCE_RATIO = 4
GWS_CLI = MACHINE["gwsCli"]
MAIL_DIRECTION_MAX = 500
MAIL_INSTRUCTION_MAX = 500
MAIL_PROMPT_MAX_CHARS = 32000
MAIL_ACTIVE_STATUSES = {"queued", "drafting", "checking"}
MAIL_STATUSES = MAIL_ACTIVE_STATUSES | {"done", "failed"}
CACHE_TTL_SECONDS = 3.0
TRANSCRIPT_TAIL_BYTES = 512 * 1024
TRANSCRIPT_BLOCK_BYTES = 256 * 1024
TRANSCRIPT_SCAN_BYTES = 8 * 1024 * 1024
TRANSCRIPT_PAGE_LIMIT = 50
TALK_WAIT_INTERVAL = 0.2
TALK_WAIT_DEFAULT_TIMEOUT = 25
TALK_WAIT_MAX_TIMEOUT = 30
# A wait is a sleeping thread that stats one file five times a second; it is
# the cheapest request the server has. Its budget is sized so that every
# open talk screen (phone and web, several per seat while a phone re-opens
# one) is watched, and the ordinary-request budget below is what is left
# over once the whole watcher budget is in use (260916-pocket-feel-server).
TALK_WAIT_PER_SESSION = 4
TALK_WAIT_GLOBAL = 48
#: Told to a caller the watcher pool cannot take, as a jittered "come back in"
#: so refused phones do not return in lockstep.
TALK_WAIT_RETRY_AFTER_MS = (1000, 3000)
TRANSCRIPT_CONTINUATION_CURSOR_BASE = 1 << 63
MAX_EVENT_TEXT = 2000
MAX_SEND_TEXT = 4000
MAX_TERMINAL_SEND_TEXT = MAX_SEND_TEXT
TERMINAL_SEND_INTERVAL_SECONDS = 1.0
CODEX_READY_STABILITY_SECONDS = 2.0
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_BODY_BYTES = 6 * 1024 * 1024
#: A folder listing is drawn as one list on a phone, and the shelf has
#: directories holding thousands of names. The listing stops here and says it
#: was cut, rather than making the reader wait for a list nobody scrolls.
MAX_DIRECTORY_ENTRIES = 1500
FILE_READ_TIMEOUT_SECONDS = 10.0
FILE_READ_WORKERS = 4
FILE_READ_QUEUE = 8
RESERVATION_TTL_SECONDS = 14 * 24 * 60 * 60
RESERVATION_PENDING_TTL_SECONDS = 24 * 60 * 60
RESERVATION_INTERVAL_SECONDS = 10.0
RESERVATION_PER_SESSION_COUNT = 20
RESERVATION_GLOBAL_COUNT = 200
RESERVATION_PER_SESSION_BYTES = 64 * 1024
RESERVATION_GLOBAL_BYTES = 512 * 1024
RESERVATION_PATH = STATE_DIR / "reservations.json"
HTTP_HEADER_TIMEOUT_SECONDS = 8.0
HTTP_BODY_TIMEOUT_SECONDS = 10.0
#: Watchers plus ordinary requests. The watcher pool (TALK_WAIT_GLOBAL) is
#: refused before this limit is, so a saturated pool still leaves this many
#: slots for the home screen and sends.
HTTP_ORDINARY_CONCURRENT = 48
HTTP_MAX_CONCURRENT = TALK_WAIT_GLOBAL + HTTP_ORDINARY_CONCURRENT
#: One line per request: when, what, how long, how big, which kind of client.
#: Never the query string or the body (260916-pocket-feel-server).
ACCESS_LOG_PATH = ROOT / "_state" / "access.jsonl"
ACCESS_LOG_MAX_BYTES = 5 * 1024 * 1024
#: The phone's own measurements, one event per line, same rotation.
METRICS_LOG_PATH = ROOT / "_state" / "metrics.jsonl"
METRICS_LOG_MAX_BYTES = 5 * 1024 * 1024
METRICS_MAX_BODY_BYTES = 64 * 1024
METRICS_MAX_EVENTS = 500
METRICS_MAX_STRING = 200
METRICS_MAX_SESSION = 16
METRICS_NETWORKS = frozenset({"wifi", "cellular", "unknown"})
PAD_MAX_SOCKETS = 2
PAD_SOCKET_PATH = "/pocket/api/pad/socket"
PAD_SOCKET_READ_TIMEOUT = 20.0
PAD_PONG_DEADLINE = 45.0
PAD_SOCKET_MAX_SECONDS = 1800.0
PAD_STATUS_INTERVAL_SECONDS = 1.0
PAD_TOKEN_HEADER = "X-Pocket-Pad"


# --- Per-request timing and the phone's own measurements ---------------------
# (260916-pocket-feel-server) Until now the only record of how the server
# behaved was the send log, so a slow home screen or a saturated watcher pool
# could only be guessed at from the phone's side. One line per request, and a
# place for the phone to put its own timings, make the next "it feels slow"
# answerable from the files.

class AsyncRotatingLogs:
    """One bounded writer for access and metrics; producers never do file I/O."""

    def __init__(self, capacity: int = 10000) -> None:
        self._queue: queue.Queue[tuple[Path, str, int]] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._closing = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pocket-rotating-logs")
        self._thread.start()

    def enqueue(self, path: Path, text: str, max_bytes: int) -> None:
        if not text:
            return
        with self._lock:
            if self._closing:
                self.dropped += 1
                return
            try:
                self._queue.put_nowait((path, text, max_bytes))
            except queue.Full:
                self.dropped += 1

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait for all entries already queued (used by tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def close(self, timeout: float = 2.0) -> bool:
        with self._lock:
            self._closing = True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        handles: dict[Path, tuple[Any, int]] = {}
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    with self._lock:
                        if self._closing:
                            break
                    continue
                try:
                    path, text, max_bytes = item
                    try:
                        self._write(handles, path, text, max_bytes)
                    except OSError:
                        current = handles.pop(path, None)
                        if current is not None:
                            current[0].close()
                finally:
                    self._queue.task_done()
        finally:
            for handle, _ in handles.values():
                try:
                    handle.close()
                except OSError:
                    pass

    def _write(self, handles: dict[Path, tuple[Any, int]], path: Path,
               text: str, max_bytes: int) -> None:
        current = handles.get(path)
        if current is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            size = path.stat().st_size if path.exists() else 0
            if size >= max_bytes:
                os.replace(path, path.with_name(path.name + ".1"))
                size = 0
            handle = path.open("a", encoding="utf-8", newline="\n")
            handles[path] = (handle, size)
        else:
            handle, size = current
        chunk = []
        for line in text.splitlines(keepends=True):
            if size >= max_bytes:
                if chunk:
                    handle.write("".join(chunk))
                    handle.flush()
                    chunk.clear()
                handle.close()
                os.replace(path, path.with_name(path.name + ".1"))
                handle = path.open("a", encoding="utf-8", newline="\n")
                size = 0
                handles[path] = (handle, size)
            chunk.append(line)
            size += len(line.encode("utf-8"))
        if chunk:
            handle.write("".join(chunk))
            handle.flush()
        handles[path] = (handle, size)


_STANDALONE_LOG_WRITER: AsyncRotatingLogs | None = None
_STANDALONE_LOG_WRITER_LOCK = threading.Lock()


def _log_writer(writer: AsyncRotatingLogs | None) -> AsyncRotatingLogs:
    if writer is not None:
        return writer
    global _STANDALONE_LOG_WRITER
    with _STANDALONE_LOG_WRITER_LOCK:
        if _STANDALONE_LOG_WRITER is None:
            _STANDALONE_LOG_WRITER = AsyncRotatingLogs()
        return _STANDALONE_LOG_WRITER


#: Every word that appears as a fixed segment of an API path. A segment that
#: is not one of these is an identifier and is written as ``{id}``.
_ACCESS_ROUTE_WORDS = frozenset({
    "pocket", "api", "health", "quota", "login", "switch", "file", "meta",
    "lines", "overview", "session", "build", "launch", "launches", "agents",
    "research", "resume", "mail", "hidden", "hide", "draft", "jobs", "revise",
    "send", "reservations", "term", "ws", "talk", "ask", "wait", "key",
    "close", "pad", "input", "socket", "status", "pair", "upload", "stream", "fault",
    "hermes", "threads", "thread", "hub", "docs", "prefs", "metrics",
})


def access_route(path: str) -> str:
    """The path with every identifier replaced by ``{id}``; static files are
    one bucket, since their names carry nothing worth timing apart."""
    if not path.startswith("/pocket/api/"):
        return "static"
    parts = path.split("/")
    return "/".join(
        part if part in _ACCESS_ROUTE_WORDS or part == "" else "{id}"
        for part in parts
    )


def access_client(user_agent: str) -> str:
    if user_agent.startswith("PocketIOS/"):
        return "native"
    if "Mozilla/" in user_agent:
        return "pwa"
    return "other"


def record_access(method: str, route: str, status: int | None, ms: float,
                  size: int, client: str, seat: str | None = None,
                  writer: AsyncRotatingLogs | None = None,
                  reject: str | None = None) -> None:
    entry = {
        "t": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "method": method, "route": route, "status": status,
        "ms": round(ms, 1), "bytes": size, "client": client,
    }
    if seat is not None:
        entry["seat"] = seat
    if reject is not None:
        entry["reject"] = reject
    line = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
    _log_writer(writer).enqueue(ACCESS_LOG_PATH, line + "\n", ACCESS_LOG_MAX_BYTES)


def _metrics_string(value: Any, limit: int = METRICS_MAX_STRING) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    return value


def _metrics_extra_ok(value: Any, depth: int = 0) -> bool:
    """Only scalars, short strings, and small nested shapes; never a body."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, str):
        return len(value) <= METRICS_MAX_STRING
    if depth >= 3:
        return False
    if isinstance(value, list):
        return len(value) <= 50 and all(_metrics_extra_ok(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return (len(value) <= 50 and all(
            isinstance(key, str) and len(key) <= METRICS_MAX_STRING
            and _metrics_extra_ok(item, depth + 1) for key, item in value.items()))
    return False


def validate_metrics_payload(payload: Any) -> list[dict[str, Any]]:
    """The events to write, each already carrying the batch's build, device and
    network. Raises ValueError with a controlled reason for anything else."""
    if not isinstance(payload, dict):
        raise ValueError("metricsInvalid")
    build = _metrics_string(payload.get("build"))
    device = _metrics_string(payload.get("device"))
    network = payload.get("network")
    events = payload.get("events")
    if (build is None or device is None or network not in METRICS_NETWORKS
            or not isinstance(events, list)):
        raise ValueError("metricsInvalid")
    if len(events) > METRICS_MAX_EVENTS:
        raise ValueError("metricsTooMany")
    rows = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("metricsInvalid")
        stamp = _metrics_string(event.get("t"))
        kind = _metrics_string(event.get("kind"))
        duration = event.get("ms")
        if (stamp is None or kind is None or isinstance(duration, bool)
                or not isinstance(duration, (int, float)) or not math.isfinite(duration)):
            raise ValueError("metricsInvalid")
        try:
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("metricsInvalid") from None
        row = {"t": stamp, "kind": kind, "ms": duration}
        if "session" in event and event["session"] is not None:
            session = _metrics_string(event["session"], METRICS_MAX_SESSION)
            if session is None:
                raise ValueError("metricsInvalid")
            row["session"] = session
        if "extra" in event and event["extra"] is not None:
            extra = event["extra"]
            if not isinstance(extra, dict) or not _metrics_extra_ok(extra):
                raise ValueError("metricsInvalid")
            row["extra"] = extra
        rows.append({"build": build, "device": device, "network": network, **row})
    return rows


def record_metrics(rows: list[dict[str, Any]], client: str,
                   writer: AsyncRotatingLogs | None = None) -> int:
    received = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    text = "".join(json.dumps({"received": received, "client": client, **row},
                              ensure_ascii=True, separators=(",", ":")) + "\n"
                   for row in rows)
    _log_writer(writer).enqueue(METRICS_LOG_PATH, text, METRICS_LOG_MAX_BYTES)
    return len(rows)


class SingleFlight:
    """Run one computation per key at a time; concurrent callers of the same
    key wait for the running one and receive its result (or its exception).

    On 2026-09-15 twenty-six overlapping home-screen refreshes each re-read
    the tail of every live transcript and the server answered nothing for 25
    minutes. Under this, the twenty-six become one read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[Any, Future] = {}

    def run(self, key: Any, operation: Callable[[], Any]) -> Any:
        with self._lock:
            future = self._inflight.get(key)
            leader = future is None
            if leader:
                future = Future()
                self._inflight[key] = future
        if not leader:
            return future.result()
        try:
            value = operation()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(value)
            return value
        finally:
            with self._lock:
                if self._inflight.get(key) is future:
                    del self._inflight[key]


SINGLE_FLIGHT = SingleFlight()


class _RecvReader:
    """Socket reader for WebSocket frames. makefile() + timeout is unsupported."""

    def __init__(self, sock: socket.socket, leftover: bytes = b"") -> None:
        self._sock = sock
        self._buf = bytearray(leftover)

    def read(self, n: int) -> bytes:
        if n <= 0:
            return b""
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(n - len(self._buf))
            except TimeoutError:
                raise
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                if exc.errno in (errno.ETIMEDOUT, errno.EWOULDBLOCK, errno.EAGAIN) or winerror in (
                    10035,
                    10060,
                ):
                    raise TimeoutError from exc
                raise
            if not chunk:
                break
            self._buf.extend(chunk)
        payload = bytes(self._buf[:n])
        del self._buf[:n]
        return payload


BRIDGE_MAX_CONCURRENT = 4
LAUNCH_DUPLICATE_SECONDS = 5.0
LAUNCH_LEDGER_LIMIT = 100
LAUNCH_CWD_ROOT = MACHINE["launchCwdRoot"]
LAUNCH_KINDS = frozenset(("claude", "codex", "shell"))

# Where a launch lands inside its pane. "front" asks mycmux to make the new tab
# that pane's active one and says the request is the operator's own tap, so it is
# honoured in the workspace on screen too; a workspace the operator is not
# looking at still only gets its tab marked. mycmux answers with
# activationApplied either way, so the phone can say what actually happened
# instead of guessing.
LAUNCH_PLACEMENTS = ("front", "behind")
DEFAULT_LAUNCH_PLACEMENT = "front"

# Mirrors sanitizeLaunchSpecValue in src/lib/agentCatalog.ts and sanitized_model
# in src-tauri/src/ai/mod.rs: the first character must be alphanumeric so the
# value can never be read as a second flag by the CLI it is handed to.
LAUNCH_SPEC_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# The launcher menu, mirrored from src/lib/agentCatalog.ts so the phone offers
# exactly what the desktop launcher offers. tests/test_launch_catalog.py reads
# that file and fails when the two drift; it is the same guard the mycmux repo
# puts between its catalog and its two launcher scripts.
#
# "cli" is the executable the launcher ends up running, and its absence is what
# says a row takes no model or effort flags at all (hermes picks its own with
# `hermes model`). "agentKind" is the session identity mycmux tracks, absent for
# CLIs it does not track.
_CLAUDE_MODELS = (
    ("fable", "Fable (flagship)"),
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
)
_CODEX_MODELS = (
    ("gpt-6-astra", "Astra (flagship)"),
    ("gpt-5.6-sol", "Sol (5.6 fallback)"),
    ("gpt-5.6-terra", "Terra (standard)"),
    ("gpt-5.6-luna", "Luna (light)"),
)
_AGY_MODELS = (
    ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
    ("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
)
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CODEX_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max", "ultra")
_SHORT_EFFORTS = ("low", "medium", "high")

LAUNCH_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "target": "claude", "label": "Claude Code", "cli": "claude",
        "agentKind": "claude", "models": _CLAUDE_MODELS, "efforts": _CLAUDE_EFFORTS,
        # 2026-09-16: Opus at max effort is the standing default for a seat
        # started from the phone. Fable is selectable, never preselected.
        "defaultModel": "opus", "defaultEffort": "max",
    },
    {
        "target": "codex", "label": "Codex", "cli": "codex",
        "agentKind": "codex", "models": _CODEX_MODELS, "efforts": _CODEX_EFFORTS,
    },
    {
        "target": "claude-codex", "label": "claude-codex",
        "cli": "claude-codex", "agentKind": "claude-codex",
        "models": _CODEX_MODELS, "efforts": _CLAUDE_EFFORTS,
    },
    {
        "target": "grok", "label": "Grok Build", "cli": "grok", "agentKind": "grok",
        # grok takes `-m <MODEL>` but publishes no id list, so the field is free
        # text rather than a menu.
        "models": (), "efforts": _SHORT_EFFORTS,
    },
    {
        "target": "agy", "label": "Antigravity (agy)", "cli": "agy",
        "models": _AGY_MODELS, "efforts": _SHORT_EFFORTS,
    },
    {
        "target": "hermes", "label": "Hermes",
        "models": (), "efforts": (),
    },
    {"target": "omp", "label": "Oh My Pi", "models": (), "efforts": ()},
)

# The bare shell is not a catalog row: it skips the launcher entirely and is
# started by argv, which is the one place argv is the honest answer.
SHELL_TARGET = "shell"
LAUNCH_TARGETS = frozenset(
    [SHELL_TARGET] + [entry["target"] for entry in LAUNCH_AGENTS]
)


def launch_agent_entry(target: str) -> dict[str, Any] | None:
    return next((entry for entry in LAUNCH_AGENTS if entry["target"] == target), None)


def launch_agent_rows() -> list[dict[str, Any]]:
    """The menu as the phone reads it, models and efforts spelled out."""
    rows: list[dict[str, Any]] = [
        {
            "target": SHELL_TARGET,
            "label": "Shell",
            "models": [],
            "efforts": [],
            # Spelled out rather than left off: every row answers the same
            # questions, so a client never has to know which keys a shell row
            # happens to omit.
            "acceptsSpec": False,
            "tracksSession": False,
        }
    ]
    for entry in LAUNCH_AGENTS:
        rows.append(
            {
                "target": entry["target"],
                "label": entry["label"],
                "models": [
                    {"value": value, "label": label} for value, label in entry["models"]
                ],
                "efforts": list(entry["efforts"]),
                # A row with no cli takes no model / effort flags anywhere in the
                # chain, so the phone must not offer the free-text field either.
                "acceptsSpec": "cli" in entry,
                # Only these end up with a Claude session id a talk screen can
                # open; the rest launch and are read as terminals.
                "tracksSession": entry.get("agentKind") == "claude",
                **(
                    {"defaultModel": entry["defaultModel"]}
                    if "defaultModel" in entry
                    else {}
                ),
                **(
                    {"defaultEffort": entry["defaultEffort"]}
                    if "defaultEffort" in entry
                    else {}
                ),
            }
        )
    return rows
RESEARCH_DEPTHS = frozenset(("quick", "recent", "deep", "ultra"))
RESEARCH_ARCHIVE_CHOICES = frozenset(("none", "drive", "downloads"))
RESEARCH_ACTIVE_STATUSES = frozenset(("queued", "running", "awaiting"))
RESEARCH_STATUSES = RESEARCH_ACTIVE_STATUSES | frozenset(
    ("done", "failed", "canceled")
)
RESEARCH_JOB_ID_RE = re.compile(r"^r\d{6}-[0-9a-f]{6}$")
LAUNCH_PRESETS = tuple(MACHINE["launchPresets"])
PUBLIC_ORIGIN = MACHINE["publicOrigin"]
CSRF_HEADER = "X-Pocket-CSRF"
LOCAL_LOG_PATH = STATE_DIR / "pocket-security.log"
SERVER_ERRORS_LOG_PATH = STATE_DIR / "server_errors.log"
SERVER_ERRORS_LOG_MAX_BYTES = 256 * 1024
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE
)
COMMAND_PREFIX_RE = re.compile(
    r"^\s*<(?:command-name|command-message|local-command[^>]*)>", re.IGNORECASE
)
PROMPT_LINE_RE = re.compile(
    r"^\s*(?:\u276f|PS\s+[^>\r\n]*>|[A-Za-z]:\\[^>\r\n]*>|[$#>])"
)
CLAUDE_PROMPT_LINE_RE = re.compile(r"^\s*\u276f")
SHELL_PROMPT_LINE_RE = re.compile(
    r"^\s*(?:PS\s+[^>\r\n]*>|[A-Za-z]:\\[^>\r\n]*>|[$#>])\s*$"
)
CODEX_PROMPT_LINE_RE = re.compile(
    r"^\s*\u203a(?:\s|$)"
)
# DAN2F 1c: exact empty-prompt text captured by the mother lane on Codex 0.154.0.
CODEX_EMPTY_PROMPT_TEXT = "Ask Codex to do anything"
CODEX_STATUS_BAR_LINE_RE = re.compile(
    r"^\s*Context\s",
    re.IGNORECASE,
)
CODEX_STATUS_STATES = frozenset(
    {"Ready", "Starting", "Working", "Waiting", "Thinking"}
)
CODEX_SPINNER_LINE_RE = re.compile(
    r"^\s*\u2022\s+Working\s+\(",
    re.IGNORECASE,
)
CLAUDE_BUSY_SCREEN_RE = re.compile(
    r"\besc to interrupt\b"
    r"|(?:^|\s)Working\s*\(\d+[hms]"
    r"|^[\u2736\u273b\u273d\u2722]\s+.+(?:\u2026|\.\.\.).*\(\d+[hms]"
    r"|\bRunning\s+\d+\s+(?:shell\s+)?commands?\b",
    re.IGNORECASE | re.MULTILINE,
)
TUI_SEPARATOR_LINE_RE = re.compile(
    r"^\s*(?P<rule>[\u2500\u2501\u2504\u2505\u2508\u2509\u254c\u254d\u2550])"
    r"(?P=rule){2,}"
)
TUI_MODE_LINE_RE = re.compile(
    r"^\s*--\s+(?:INSERT|NORMAL)\s+--",
    re.IGNORECASE,
)
CLAUDE_PROMPT_FOLLOWING_LINE_RES = (
    TUI_SEPARATOR_LINE_RE,
)
CODEX_PROMPT_FOLLOWING_LINE_RES = (
    CODEX_STATUS_BAR_LINE_RE,
    CODEX_SPINNER_LINE_RE,
    TUI_MODE_LINE_RE,
    TUI_SEPARATOR_LINE_RE,
)
TUI_SCREEN_SCAN_LIMIT = 8
ALLOWED_FILE_ROOTS = tuple(MACHINE["allowedFileRoots"])
HUB_PUBLIC_ORIGIN = MACHINE["hubOrigin"]
HUB_MIRROR_ROOT = Path(
    os.environ.get("POCKET_HUB_MIRROR")
    or (
        str(Path.home() / "AppData" / "Local" / "AIHtmlHubMirror")
        if os.name == "nt"
        else str(Path.home() / ".hermes" / "html-hub-mirror")
    )
)
_HUB_INDEX_CACHE: dict[str, Any] = {"mtime": None, "by_name": None}
DENIED_FILE_ROOTS = tuple(
    # Both, because --state-dir moves the live state without making the folder
    # beside server.py (which may still hold an older copy) readable.
    dict.fromkeys((STATE_DIR, ROOT / "_state", ROOT / "_dispatch"))
)
#: Content types for the PWA's own assets. The stdlib mimetypes database is
#: seeded from the host (macOS answers "text/javascript" for .js where Windows
#: answers "application/javascript"), and the page must not be served
#: differently depending on which machine is hosting it.
STATIC_MIME_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}
TEXT_FILE_SUFFIXES = {
    ".md",
    ".txt",
    ".log",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".csv",
    ".py",
    ".ps1",
    ".psm1",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".sh",
    ".bat",
    ".cmd",
    ".toml",
    ".ini",
    ".cfg",
    ".xml",
    ".xsd",
    ".tsv",
    ".css",
    ".swift",
    ".sql",
    ".j2",
    ".tmpl",
    ".err",
}
FILE_MIME_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/plain",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "text/plain",
    ".jsonl": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".csv": "text/plain",
    ".py": "text/plain",
    ".ps1": "text/plain",
    ".psm1": "text/plain",
    ".js": "text/plain",
    ".mjs": "text/plain",
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".sh": "text/plain",
    ".bat": "text/plain",
    ".cmd": "text/plain",
    ".toml": "text/plain",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".xml": "text/plain",
    ".xsd": "text/plain",
    ".tsv": "text/plain",
    ".css": "text/plain",
    ".swift": "text/plain",
    ".sql": "text/plain",
    ".j2": "text/plain",
    ".tmpl": "text/plain",
    ".err": "text/plain",
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
ACTIVE_CONTENT_MIME_TYPES = {
    "application/javascript",
    "application/xhtml+xml",
    "application/xml",
    "image/svg+xml",
    "text/html",
    "text/javascript",
    "text/xml",
}
DENIED_FILE_SEGMENT_PATTERNS = (
    ".env*",
    "*credentials*",
    "*token*",
    "*secret*",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    "*.pem",
    "*.key",
    "*.ppk",
    "*.p12",
    "*.pfx",
    ".ssh",
    ".gnupg",
    "_state",
    ".git",
    "auth.json",
    "cookies*",
)

_PROCESS_LOCKS: set[str] = set()
_PROCESS_LOCKS_GUARD = threading.Lock()
BRIDGE_SEMAPHORE = threading.BoundedSemaphore(BRIDGE_MAX_CONCURRENT)
_SERVER_ERROR_LOG_LOCK = threading.Lock()


def _redact_diagnostic(value: str) -> str:
    cleaned = value.replace("\r", " ").replace("\n", " ")[:1000]
    return re.sub(
        r"(?i)(token|secret|password|authorization)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]",
        cleaned,
    )


def _log_local(category: str, detail: str, request_id: str) -> None:
    try:
        LOCAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOCAL_LOG_PATH.exists() and LOCAL_LOG_PATH.stat().st_size >= 256 * 1024:
            return
        entry = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "category": category,
            "requestId": request_id,
            "detail": _redact_diagnostic(detail),
        }
        with LOCAL_LOG_PATH.open("a", encoding="ascii", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except OSError:
        return


def _log_server_error(summary: str) -> None:
    try:
        SERVER_ERRORS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _SERVER_ERROR_LOG_LOCK:
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            detail = json.dumps(_redact_diagnostic(summary), ensure_ascii=True)
            with SERVER_ERRORS_LOG_PATH.open("a", encoding="ascii", newline="\n") as handle:
                handle.write(f"{timestamp} {detail}\n")
            if SERVER_ERRORS_LOG_PATH.stat().st_size <= SERVER_ERRORS_LOG_MAX_BYTES:
                return
            payload = SERVER_ERRORS_LOG_PATH.read_bytes()
            retained = payload[len(payload) // 2 :]
            newline_index = retained.find(b"\n")
            if newline_index >= 0:
                retained = retained[newline_index + 1 :]
            temp_path = SERVER_ERRORS_LOG_PATH.with_name(
                f"{SERVER_ERRORS_LOG_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temp_path.write_bytes(retained)
                os.replace(temp_path, SERVER_ERRORS_LOG_PATH)
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except (OSError, UnicodeError):
        return


class MachineFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        self._key = os.fspath(path.resolve()).casefold()
        with _PROCESS_LOCKS_GUARD:
            if self._key in _PROCESS_LOCKS:
                raise RuntimeError("state directory is already in use")
            _PROCESS_LOCKS.add(self._key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a+b")
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            self.close()
            raise RuntimeError("state directory is already in use") from exc

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass
            handle.close()
        with _PROCESS_LOCKS_GUARD:
            _PROCESS_LOCKS.discard(self._key)


class AllowedLogins:
    """A missing, removed or malformed allowlist grants no additional access.

    Values are casefolded on the way in, and every comparison against them is
    casefolded too: mycmux only trims what the operator typed, so a login that
    differs by case lined up in the list and never matched a request.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stamp = None
        self._logins: frozenset[str] = frozenset()

    def get(self) -> frozenset[str]:
        with self._lock:
            try:
                info = self.path.stat()
                stamp = (info.st_mtime_ns, info.st_size)
                if stamp == self._stamp:
                    return self._logins
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                values = stored.get("logins") if isinstance(stored, dict) else None
                self._logins = frozenset(
                    value.casefold()
                    for value in values if isinstance(value, str) and value
                ) if isinstance(values, list) else frozenset()
                self._stamp = stamp
            except (OSError, UnicodeError, ValueError):
                self._logins = frozenset()
                self._stamp = None
            return self._logins


class DeviceStore:
    """Bounded observations, owned by one server and flushed at most every 30s."""

    def __init__(self, path: Path, monotonic=None) -> None:
        self.path = path
        self.monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._machine_lock = MachineFileLock(path.parent / f".{path.name}.lock")
        self._timer: threading.Timer | None = None
        self._closed = False
        self._dirty = False
        self._written: float | None = None
        self._disabled = False
        self._rows: dict[str, dict[str, Any]] = {}
        try:
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                stored = {}
            if not isinstance(stored, dict) or any(
                self.device_id(key) is None or not isinstance(row, dict)
                or not isinstance(row.get("lastSeen"), str)
                or _parse_timestamp(row["lastSeen"]) is None
                for key, row in stored.items()
            ):
                raise ValueError("invalid devices state")
            self._rows = dict(sorted(
                stored.items(), key=lambda item: _parse_timestamp(item[1]["lastSeen"])
            )[-100:])
        except (ValueError, UnicodeError) as exc:
            # Device observations are optional. Keep the corrupt bytes and the
            # machine lock, but never replace them with an empty/new device file.
            self._disabled = True
            quarantine = path.with_name(
                path.name + ".corrupt-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                + "-" + uuid.uuid4().hex
            )
            try:
                path.rename(quarantine)
                preservation = "preserved as " + quarantine.name
            except OSError as preserve_error:
                # A read-only directory must still leave the original untouched.
                preservation = "original retained; rename failed: " + type(preserve_error).__name__
            _log_server_error(
                "device recording disabled: " + type(exc).__name__ + "; " + preservation
            )
        except BaseException:
            self._machine_lock.close()
            raise

    @staticmethod
    def device_id(value: str | None) -> str | None:
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
        ):
            return None
        return str(uuid.UUID(value))

    def record(self, device: str, login: str | None, agent: str, forwarded: str) -> None:
        key = self.device_id(device)
        if key is None:
            return
        with self._lock:
            if self._closed or self._disabled:
                return
            self._rows.pop(key, None)
            self._rows[key] = {
                "login": login, "lastSeen": _utc_now(),
                "userAgent": agent, "forwardedFor": forwarded,
            }
            while len(self._rows) > 100:
                del self._rows[next(iter(self._rows))]
            self._dirty = True
            self._flush_locked()

    def list_devices(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(row) for key, row in self._rows.items()}

    def _flush_locked(self) -> None:
        if self._closed or self._disabled or not self._dirty:
            return
        now = self.monotonic()
        remaining = 0 if self._written is None else 30 - (now - self._written)
        if remaining > 0:
            self._schedule_locked(remaining)
            return
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self._rows, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
            os.replace(temporary, self.path)
            self._dirty = False
        except OSError as exc:
            _log_server_error("device record write failed: " + type(exc).__name__)
            self._schedule_locked(30)
        finally:
            self._written = self.monotonic()
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _schedule_locked(self, delay: float) -> None:
        if self._timer is None:
            self._timer = threading.Timer(delay, self._flush_due)
            self._timer.daemon = True
            self._timer.start()

    def _flush_due(self) -> None:
        with self._lock:
            self._timer = None
            self._flush_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._machine_lock.close()


class BoundedExecutor:
    def __init__(self, max_workers: int, max_queue: int) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="pocket-file",
        )
        self._slots = threading.BoundedSemaphore(max_workers + max_queue)

    def submit(self, operation: Callable[[], Any]) -> Future[Any]:
        if not self._slots.acquire(blocking=False):
            raise FileAccessError(503, "fileBusy")
        try:
            future = self._executor.submit(operation)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        return future

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)


FILE_EXECUTOR = BoundedExecutor(FILE_READ_WORKERS, FILE_READ_QUEUE)


class BridgeError(RuntimeError):
    def __init__(self, code: str = "bridgeUnavailable", diagnostic: str = "") -> None:
        self.detail = code
        self.diagnostic = diagnostic
        public_codes = {
            "bridgeBusy",
            "bridgeUnavailable",
            "lastTabRefused",
            "tabNeedsConfirmation",
            "paneChanged",
            "paneCloseUnverified",
            "paneNotClosed",
            "sessionNotAllowed",
            "sessionStateUnavailable",
            "codexRolloutUnavailable",
            "workspaceNotFound",
        }
        self.code = code if code in public_codes else "bridgeUnavailable"
        super().__init__(self.code)


class FileAccessError(RuntimeError):
    def __init__(self, status: int, code: str, **details: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.details = details


class MailError(RuntimeError):
    def __init__(
        self,
        status: int,
        reason: str,
        check_findings: list[str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.check_findings = check_findings or []


class ReservationError(RuntimeError):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class LaunchError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class ResearchError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


# Path handling is a trust boundary: everything below decides which files the
# API will hand out. Windows and macOS disagree on separators, drives and long
# names, so the platform-specific parts are isolated here and every caller stays
# the same. On Windows the behaviour is unchanged.
IS_WINDOWS = os.name == "nt"
PATH_FLAVOUR = ntpath if IS_WINDOWS else posixpath


def _long_windows_path(value: str) -> str:
    if not IS_WINDOWS:
        return value
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = ctypes.windll.kernel32.GetLongPathNameW(value, buffer, size)
    if 0 < length < size:
        return buffer.value
    return value


def _normalized_path(value: str) -> str:
    """Normalize a path the way the host platform reads it.

    On Windows a forward slash is a separator and 8.3 short names have to be
    expanded before anything is compared. On POSIX a backslash is an ordinary
    filename character, so rewriting separators there would silently point at a
    different file.
    """
    if IS_WINDOWS:
        return ntpath.normpath(_long_windows_path(value.replace("/", "\\")))
    return posixpath.normpath(value)


def _path_casefold(value: str) -> str:
    """Fold case for comparisons.

    Windows and the default macOS volume are both case-insensitive. Folding also
    widens the deny list, which is the safe direction. An allow decision never
    rests on this alone: the final check re-reads the path from the opened
    descriptor.
    """
    return ntpath.normcase(value) if IS_WINDOWS else value.casefold()


def _path_is_within(candidate: str, root: str) -> bool:
    candidate_key = _path_casefold(_normalized_path(candidate))
    root_key = _path_casefold(_normalized_path(root))
    try:
        return PATH_FLAVOUR.commonpath((candidate_key, root_key)) == root_key
    except ValueError:
        return False


def _path_segments(value: str) -> list[str]:
    _, tail = PATH_FLAVOUR.splitdrive(value)
    # Both separators are split on regardless of platform. A POSIX filename may
    # legitimately contain a backslash, and splitting on it only ever yields
    # more segments to test against the deny list.
    return [
        segment.lower()
        for segment in re.split(r"[\\/]+", tail)
        if segment not in {"", ".", ".."}
    ]


def _is_uploaded_image_path(value: str) -> bool:
    expected_root = _normalized_path(os.path.join(os.path.realpath(STATE_DIR), "uploads"))
    resolved_root = _normalized_path(os.path.realpath(UPLOAD_ROOT))
    if _path_casefold(resolved_root) != _path_casefold(expected_root):
        return False
    roots = (_normalized_path(os.path.abspath(UPLOAD_ROOT)), resolved_root)
    for root in roots:
        if not _path_is_within(value, root):
            continue
        relative = PATH_FLAVOUR.relpath(_normalized_path(value), root).replace("\\", "/")
        if re.fullmatch(
            r"([0-9]{8})/\1T[0-9]{6}Z-[0-9a-f]{8}\.(?:jpg|png|gif|webp)",
            relative, re.IGNORECASE,
        ):
            return True
    return False


def _path_is_denied(value: str) -> bool:
    uploaded_image = _is_uploaded_image_path(value)
    state_roots = {_path_casefold(_normalized_path(os.path.realpath(root)))
                   for root in (STATE_DIR, ROOT / "_state")}
    if any(
        _path_is_within(value, os.fspath(root.resolve()))
        and not (uploaded_image and _path_casefold(_normalized_path(os.fspath(root.resolve()))) in state_roots)
        for root in DENIED_FILE_ROOTS
    ):
        return True
    segments = _path_segments(value)
    for index, segment in enumerate(segments):
        if uploaded_image and segment == "_state" and index == len(segments) - 4:
            continue
        if any(
            fnmatch.fnmatchcase(segment, pattern)
            for pattern in DENIED_FILE_SEGMENT_PATTERNS
        ):
            return True
    for index in range(len(segments) - 1):
        parent = segments[index]
        child = segments[index + 1]
        if parent == ".codex" and fnmatch.fnmatchcase(child, "auth*"):
            return True
        if parent == ".config" and child == "gws":
            return True
    return False


def _is_unsafe_absolute(normalized: str) -> bool:
    """Reject anything that is not a plain absolute path on this platform.

    Windows additionally rejects UNC shares, the ``\\\\?\\`` prefix and a colon in
    the tail, which would name an alternate data stream. POSIX has no drives,
    no UNC and no streams, so being absolute is the whole requirement there.
    """
    if not PATH_FLAVOUR.isabs(normalized):
        return True
    if not IS_WINDOWS:
        return False
    _, tail = ntpath.splitdrive(normalized)
    return (
        normalized.startswith("\\\\")
        or normalized.startswith("\\\\?\\")
        or ":" in tail
    )


def _roots_on_same_volume(
    normalized: str, roots: Any, *, resolve: bool = True
) -> list[str]:
    """Allowed roots that could possibly contain ``normalized``, normalized.

    Windows compares drive letters so a C: path is never tested against a G:
    root. POSIX has a single tree, so every root stays in play.

    ``resolve`` picks the real path of each root. Both forms are needed because
    macOS reaches the temp tree through a symlink (``/var`` -> ``/private/var``):
    a caller may name either side, and only the resolved form is trustworthy.
    """
    if IS_WINDOWS:
        candidate_drive = ntpath.normcase(ntpath.splitdrive(normalized)[0])
        roots = [
            root
            for root in roots
            if ntpath.normcase(ntpath.splitdrive(os.fspath(root))[0])
            == candidate_drive
        ]
    if resolve:
        return [
            _normalized_path(os.path.realpath(os.fspath(root))) for root in roots
        ]
    return [_normalized_path(os.fspath(root)) for root in roots]


def _within_any_declared_root(normalized: str, roots: Any) -> bool:
    """First-pass root check, before the filesystem is consulted.

    Accepts either spelling of a root, because on macOS a root under ``/var`` is
    the same directory as one under ``/private/var``. This pass only decides
    whether the request is worth resolving; the authoritative check is the one
    that runs afterwards against the resolved roots, and the file is checked
    once more from its open descriptor.
    """
    candidates = _roots_on_same_volume(normalized, roots, resolve=False)
    candidates += _roots_on_same_volume(normalized, roots, resolve=True)
    return any(_path_is_within(normalized, root) for root in candidates)


def resolve_file_path(raw_path: str) -> Path:
    if not raw_path or "\x00" in raw_path:
        raise FileAccessError(400, "fileNotFound")
    normalized = _normalized_path(raw_path)
    if _is_unsafe_absolute(normalized):
        raise FileAccessError(403, "fileDenied")
    if _path_is_denied(normalized):
        raise FileAccessError(403, "fileDenied")
    if not _within_any_declared_root(normalized, ALLOWED_FILE_ROOTS):
        raise FileAccessError(403, "fileDenied")
    resolved = _normalized_path(os.path.realpath(normalized))
    if _path_is_denied(resolved) or (_is_uploaded_image_path(normalized) and not _is_uploaded_image_path(resolved)):
        raise FileAccessError(403, "fileDenied")
    # Authoritative: the resolved path must sit inside a resolved root, so a
    # symlink pointing out of the tree is rejected even though the name looked
    # fine a moment ago.
    if not any(
        _path_is_within(resolved, root)
        for root in _roots_on_same_volume(resolved, ALLOWED_FILE_ROOTS)
    ):
        raise FileAccessError(403, "fileDenied")
    return Path(resolved)


def resolve_launch_cwd(raw_cwd: Any) -> Path:
    if not isinstance(raw_cwd, str) or not raw_cwd or "\x00" in raw_cwd:
        raise LaunchError(400, "launchCwdInvalid")
    normalized = _normalized_path(raw_cwd)
    if _is_unsafe_absolute(normalized):
        raise LaunchError(400, "launchCwdInvalid")
    if not _within_any_declared_root(normalized, (LAUNCH_CWD_ROOT,)):
        raise LaunchError(400, "launchCwdInvalid")
    root = _normalized_path(os.path.realpath(os.fspath(LAUNCH_CWD_ROOT)))
    resolved = _normalized_path(os.path.realpath(normalized))
    if not _path_is_within(resolved, root):
        raise LaunchError(400, "launchCwdInvalid")
    target = Path(resolved)
    try:
        if not target.is_dir():
            raise LaunchError(400, "launchCwdInvalid")
    except OSError as exc:
        raise LaunchError(400, "launchCwdInvalid") from exc
    return target


#: ``F_GETPATH`` from ``<sys/fcntl.h>``. Darwin only; Linux has no equivalent
#: and uses procfs instead.
DARWIN_F_GETPATH = 50
#: ``PATH_MAX`` on Darwin.
DARWIN_PATH_MAX = 1024


def _posix_handle_path(handle: Any) -> str:
    """Real path of an already-open descriptor, without re-opening by name.

    This is the check that survives a symlink or junction being swapped after
    the path was validated, so it must read the descriptor itself. Linux does
    that through procfs; **macOS has no /proc**, and the equivalent is the
    ``F_GETPATH`` fcntl.
    """
    if sys.platform == "darwin":
        import fcntl

        try:
            # fcntl returns the filled buffer; it does not write through the
            # argument, so the result is what has to be read.
            filled = fcntl.fcntl(
                handle.fileno(), DARWIN_F_GETPATH, b"\0" * DARWIN_PATH_MAX
            )
        except OSError as exc:
            raise FileAccessError(403, "fileDenied") from exc
        value = filled.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        if not value:
            raise FileAccessError(403, "fileDenied")
        return value
    return os.path.realpath(f"/proc/self/fd/{handle.fileno()}")


def _opened_handle_path(handle: Any) -> Path:
    if IS_WINDOWS:
        import msvcrt

        raw_handle = msvcrt.get_osfhandle(handle.fileno())
        flags = 0
        size = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(raw_handle), None, 0, flags
        )
        if size <= 0:
            raise FileAccessError(403, "fileDenied")
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(raw_handle), buffer, size + 1, flags
        )
        if written <= 0 or written > size:
            raise FileAccessError(403, "fileDenied")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    else:
        value = _posix_handle_path(handle)
    resolved = _normalized_path(value)
    requested = _normalized_path(os.fspath(handle.name))
    if _path_is_denied(resolved) or (_is_uploaded_image_path(requested) and not _is_uploaded_image_path(resolved)):
        raise FileAccessError(403, "fileDenied")
    matching_roots = _roots_on_same_volume(resolved, ALLOWED_FILE_ROOTS)
    if not any(_path_is_within(resolved, root) for root in matching_roots):
        raise FileAccessError(403, "fileDenied")
    return Path(resolved)


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "markdown"
    if suffix in TEXT_FILE_SUFFIXES:
        return "text"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}:
        return "image"
    if suffix in {".pdf", ".docx", ".xlsx", ".pptx"}:
        return suffix[1:]
    return "binary"


def file_content_type(path: Path) -> str:
    return FILE_MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _reject_directory(target: Path) -> None:
    # Opening a directory raises PermissionError on Windows, which was being
    # reported to the phone as "denied" - so a folder path came back looking
    # like the reader lacked permission to a file they can in fact reach.
    try:
        if target.is_dir():
            raise FileAccessError(400, "fileIsDirectory")
    except OSError:
        return


def _hub_index_by_name() -> dict[str, str]:
    index_path = Path(HUB_MIRROR_ROOT) / "mobile" / "search.json"
    try:
        mtime = index_path.stat().st_mtime
    except OSError:
        return {}
    cached = _HUB_INDEX_CACHE
    if cached["by_name"] is not None and cached["mtime"] == mtime:
        return cached["by_name"]
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        relative = (entry.get("p") or "").replace("\\", "/")
        if not relative:
            continue
        grouped.setdefault(Path(relative).name, []).append(relative)
    unique = {name: paths[0] for name, paths in grouped.items() if len(paths) == 1}
    cached["mtime"] = mtime
    cached["by_name"] = unique
    return unique


#: encodeURIComponent's unreserved set, as the phone spells it in
#: HubRules.makeHref (pocket-ios Sources/HubRules.swift), minus ``'`` and
#: ``()``. The phone's own inline link scanner (TalkLinks.decorate) ends a
#: link at ``'`` or ``)``, so leaving those two literal - which makeHref does -
#: still cut 1,474 of the 6,299 paths on the real shelf short. ``(`` goes with
#: its partner so that a pair never half-survives.
HUB_PATH_SAFE = "!~*"


def hub_href_path(relative: str) -> str:
    """Percent-encode a shelf path one segment at a time; "/" stays a separator."""
    return "/".join(
        quote(segment, safe=HUB_PATH_SAFE) for segment in relative.split("/")
    )


def hub_public_url(target: Path) -> str | None:
    """Phone-facing hub URL for an HTML file that is already on the shelf."""
    if not HUB_PUBLIC_ORIGIN:
        return None
    try:
        resolved = target.resolve()
    except OSError:
        return None
    try:
        relative = resolved.relative_to(Path(HUB_MIRROR_ROOT).resolve())
    except (ValueError, OSError):
        relative = None
    if relative is not None:
        return f"{HUB_PUBLIC_ORIGIN}/{hub_href_path(relative.as_posix())}"
    if resolved.suffix.lower() not in {".html", ".htm"}:
        return None
    mapped = _hub_index_by_name().get(resolved.name)
    if not mapped:
        return None
    return f"{HUB_PUBLIC_ORIGIN}/{hub_href_path(mapped)}"


def _validate_uploaded_image(handle: Any, target: Path, opened_target: Path) -> None:
    requested_upload = _is_uploaded_image_path(os.fspath(target))
    opened_upload = _is_uploaded_image_path(os.fspath(opened_target))
    if not requested_upload and not opened_upload:
        return
    if not requested_upload or not opened_upload:
        raise FileAccessError(403, "fileDenied")
    file_stat = os.fstat(handle.fileno())
    if file_stat.st_nlink != 1 or file_stat.st_size > upload_service.MAX_UPLOAD_BYTES:
        raise FileAccessError(403, "fileDenied")
    position = handle.tell()
    prefix = handle.read(12)
    handle.seek(position)
    kind = upload_service.detect_image_kind(prefix)
    expected = {".jpg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
    if kind is None or kind != expected.get(opened_target.suffix.lower()):
        raise FileAccessError(403, "fileDenied")


def get_file_metadata(raw_path: str) -> dict[str, Any]:
    target = resolve_file_path(raw_path)
    _reject_directory(target)
    try:
        with target.open("rb") as handle:
            opened_target = _opened_handle_path(handle)
            _validate_uploaded_image(handle, target, opened_target)
            file_stat = os.fstat(handle.fileno())
    except FileNotFoundError:
        return {
            "exists": False,
            "size": None,
            "mtime": None,
            "kind": file_kind(target),
        }
    except PermissionError as exc:
        raise FileAccessError(403, "fileDenied") from exc
    except OSError as exc:
        raise FileAccessError(404, "fileNotFound") from exc
    kind = file_kind(opened_target)
    if not stat.S_ISREG(file_stat.st_mode):
        return {"exists": False, "size": None, "mtime": None, "kind": kind}
    result = {
        "exists": True,
        "size": file_stat.st_size,
        "mtime": file_stat.st_mtime,
        "kind": kind,
    }
    hub_url = hub_public_url(opened_target)
    if hub_url:
        result["hubUrl"] = hub_url
    return result


def _directory_row(entry: os.DirEntry[str]) -> dict[str, Any] | None:
    """One line of a folder listing, or None for a name nobody may open.

    A denied name is left out rather than shown and then refused: the reader
    would tap it once for every visit.
    """
    normalized = _normalized_path(entry.path)
    if _path_is_denied(normalized):
        return None
    try:
        is_dir = entry.is_dir()
    except OSError:
        return None
    row: dict[str, Any] = {"name": entry.name, "path": normalized, "dir": is_dir}
    try:
        entry_stat = entry.stat()
    except OSError:
        entry_stat = None
    if entry_stat is not None:
        row["mtime"] = entry_stat.st_mtime
        if not is_dir:
            row["size"] = entry_stat.st_size
    if not is_dir:
        row["kind"] = file_kind(Path(entry.path))
    return row


def list_file_roots() -> dict[str, Any]:
    """Start a file walk at this machine's configured roots, using the same gate.

    No drive discovery or root expansion: a disconnected configured drive is
    visible as unavailable, and denied or duplicate roots are omitted.
    """
    rows = []
    seen = set()
    for root in ALLOWED_FILE_ROOTS:
        try:
            target = resolve_file_path(os.fspath(root))
            normalized = _normalized_path(os.fspath(target))
            key = normalized.casefold() if os.name == "nt" else normalized
            if key in seen:
                continue
            available = target.is_dir()
        except (FileAccessError, OSError):
            continue
        seen.add(key)
        rows.append({"name": target.name or normalized, "path": normalized,
                     "available": available})
    return {"roots": rows}


def list_directory(raw_path: str) -> dict[str, Any]:
    """What is inside a folder the reader is allowed to reach.

    Same gate as reading a file - ``resolve_file_path`` decides whether the
    path is reachable at all - so a listing can never name anything that could
    not have been opened by its own path.
    """
    target = resolve_file_path(raw_path)
    try:
        is_dir = target.is_dir()
        # "not a folder" and "not there" read the same to the caller unless
        # they are told apart here, and the phone answers them differently.
        exists = is_dir or target.exists()
    except OSError as exc:
        raise FileAccessError(404, "fileNotFound") from exc
    if not exists:
        raise FileAccessError(404, "fileNotFound")
    if not is_dir:
        raise FileAccessError(400, "notADirectory")
    rows: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                row = _directory_row(entry)
                if row is None:
                    continue
                if len(rows) >= MAX_DIRECTORY_ENTRIES:
                    truncated = True
                    break
                rows.append(row)
    except PermissionError as exc:
        raise FileAccessError(403, "fileDenied") from exc
    except OSError as exc:
        raise FileAccessError(404, "fileNotFound") from exc
    # Folders first, then names, the way a file manager orders them; the exact
    # spelling breaks the tie so that two names differing only in case keep a
    # stable order between visits.
    rows.sort(key=lambda row: (not row["dir"], row["name"].casefold(), row["name"]))
    normalized = _normalized_path(os.fspath(target))
    parent = _normalized_path(os.fspath(target.parent))
    if parent == normalized:
        parent = None
    else:
        # Offer the way up only while the parent is itself reachable, so the
        # listing never hands out a step that is about to be refused.
        try:
            resolve_file_path(parent)
        except FileAccessError:
            parent = None
    return {
        "path": normalized,
        "name": target.name or normalized,
        "parent": parent,
        "entries": rows,
        "count": len(rows),
        "truncated": truncated,
    }


def read_file_payload(raw_path: str) -> tuple[Path, bytes]:
    target = resolve_file_path(raw_path)
    _reject_directory(target)
    try:
        handle = target.open("rb")
    except FileNotFoundError as exc:
        raise FileAccessError(404, "fileNotFound") from exc
    except PermissionError as exc:
        raise FileAccessError(403, "fileDenied") from exc
    except OSError as exc:
        raise FileAccessError(404, "fileNotFound") from exc
    try:
        with handle:
            opened_target = _opened_handle_path(handle)
            _validate_uploaded_image(handle, target, opened_target)
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise FileAccessError(404, "fileNotFound")
            if file_stat.st_size > MAX_FILE_BYTES:
                raise FileAccessError(
                    413,
                    "fileTooLarge",
                    size=file_stat.st_size,
                    maxSize=MAX_FILE_BYTES,
                )
            payload = handle.read(MAX_FILE_BYTES + 1)
    except PermissionError as exc:
        raise FileAccessError(403, "fileDenied") from exc
    except OSError as exc:
        raise FileAccessError(404, "fileNotFound") from exc
    if len(payload) > MAX_FILE_BYTES:
        raise FileAccessError(
            413,
            "fileTooLarge",
            size=len(payload),
            maxSize=MAX_FILE_BYTES,
        )
    return opened_target, payload


def run_file_operation(operation: Callable[[], Any], timeout: float | None = None) -> Any:
    future = FILE_EXECUTOR.submit(operation)
    try:
        return future.result(timeout=FILE_READ_TIMEOUT_SECONDS if timeout is None else timeout)
    except FutureTimeout as exc:
        future.cancel()
        raise FileAccessError(503, "fileSlow") from exc


class TTLCache:
    def __init__(self, ttl_seconds: float = CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, ...], tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[str, ...], loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]
            value = loader()
            self._entries[key] = (time.monotonic() + self.ttl_seconds, value)
            return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


BRIDGE_CACHE = TTLCache()


def _run_bridge_cli(*args: str, include_exit: bool = False) -> Any:
    command = [os.fspath(Path(sys.executable)), os.fspath(BRIDGE_CLI), *args]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    request_id = uuid.uuid4().hex
    if not BRIDGE_SEMAPHORE.acquire(timeout=0.5):
        _log_local("bridge_busy", "concurrency limit reached", request_id)
        _log_server_error("bridge_busy concurrency limit reached")
        raise BridgeError("bridgeBusy")
    try:
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                check=False,
                env={key: value for key, value in os.environ.items()
                     if key not in {"MYCMUX_PANE_SESSION_ID", "MYCMUX_TAB_ID"}},
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log_local("bridge_invocation", str(exc), request_id)
            _log_server_error(f"bridge_invocation {exc}")
            raise BridgeError("bridgeTimeout" if isinstance(exc, subprocess.TimeoutExpired)
                              else "bridgeUnavailable", str(exc)) from exc
    finally:
        BRIDGE_SEMAPHORE.release()
    if include_exit:
        # Selection writes can exit 1 after bytes were written. Retain both
        # fields for their screen-based check; ordinary bridge calls stay strict.
        try:
            return completed.returncode, json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            _log_server_error("bridge_json invalid JSON")
            raise BridgeError() from exc
    if completed.returncode != 0:
        detail = ("read failed" if args and args[0] in {"workspaces", "panes", "read", "status"}
                  else completed.stderr.strip() or "no stderr")
        _log_server_error(f"bridge_exit exit={completed.returncode} {detail}")
        if args and args[0] == "read":
            diagnostic = _redact_diagnostic((completed.stderr or "").splitlines()[0][:160]
                                     if completed.stderr else "")
            code = ("terminalBufferMissing" if "no terminal buffer" in diagnostic.lower()
                    else "bridgeTimeout" if "timed out" in diagnostic.lower()
                    else "terminalReadFailed")
            raise BridgeError(code, diagnostic)
        raise BridgeError()
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        _log_server_error("bridge_json invalid JSON")
        raise BridgeError() from exc


class SocketBridge:
    """Direct reads with the existing CLI retained for writes and fallback."""

    def __init__(self, client: MycmuxSocket, cli: Callable[..., Any]):
        self.client = client
        self.cli = cli
        self.feed = StatusFeedThread(client, lambda message: _log_server_error("status_feed " + message))
        self.fallback = False
        self.last_error: str | None = None
        self.workspace_errors: list[dict[str, Any]] = []
        self._layout_api = None
        self._layout_api_at = 0.0

    def __call__(self, *args: str) -> Any:
        command = args[0] if args else ""
        options = dict(zip(args[1::2], args[2::2]))
        write = False
        if command == "workspaces":
            cmd, payload = "workspace.list", {}
        elif command == "panes":
            cmd = "pane.list_all" if "--all" in args else "pane.list"
            payload = {"workspaceId": options["--workspace"]} if "--workspace" in options else {}
        elif command == "read":
            cmd, payload = "pane.read", {"sessionId": options.get("--session")}
            if "--lines" in options:
                payload["lines"] = int(options["--lines"])
        elif command == "status":
            cmd = "session.state_view"
            payload = {"session_id": options["--session"]} if "--session" in options else {}
        elif command == "spawn" and args[1:2] == ("--split",) and "--" in args:
            # The v0.88 socket accepts commandArgv, but its spawn CLI only accepts
            # --target. Keep the configured shell and workspace without anchors.
            flags = {}
            activate = False
            remaining = iter(args[2:])
            for flag in remaining:
                if flag == "--":
                    command_argv = list(remaining)
                    break
                if flag in {"--activate", "--no-activate"}:
                    activate = flag == "--activate"
                else:
                    # Consume an option value even when the label itself is --.
                    flags[flag] = next(remaining)
            else:
                # An agent label can be -- too; it is not a shell argv delimiter.
                return self.cli(*args)
            cmd, payload = "pane.spawn", {
                "workspaceId": flags["--workspace"],
                "cwd": flags["--cwd"],
                "label": flags["--label"],
                "activate": activate,
                "commandArgv": command_argv,
            }
            write = True  # An uncertain spawn must never retry through the CLI.
        elif command == "close-tab":
            cmd, payload = "pane.close_tab", {"sessionId": options.get("--session"), "force": False}
            write = True
        elif command == "close-tabs":
            # A write with no CLI twin: the bridge CLI cannot close tabs, and a
            # close must never be retried down a second path once it may have
            # applied. The tab ids are the remaining positional arguments.
            cmd = "pane.close_tabs"
            payload = {"tabIds": [value for value in args[1:] if isinstance(value, str) and value]}
            write = True
        else:
            return self.cli(*args)
        try:
            result = self.client.request(cmd, payload)
        except Unauthorized:
            self.fallback = False
            self.last_error = "unauthorized"
            raise BridgeError() from None
        except MycmuxError as exc:
            if write:
                code = str(exc)
                if code.startswith("socket"):
                    self.last_error = "socketUnavailable"
                raise BridgeError("lastTabRefused" if code == "lastTabRefused" else "bridgeUnavailable") from None
            if command == "read" and "terminal buffer" in str(exc).lower() and (
                "no " in str(exc).lower() or "missing" in str(exc).lower()
            ):
                self.fallback = False
                self.last_error = "terminalBufferMissing"
                raise BridgeError("terminalBufferMissing", str(exc)) from None
            self.fallback = True
            self.last_error = "socketUnavailable"
            return self.cli(*args)
        self.fallback = False
        self.last_error = None
        if command == "status":
            from mycmux_socket import validate_status_result
            try:
                return validate_status_result(result, options.get("--session"))
            except RuntimeError:
                raise BridgeError("sessionStateUnavailable") from None
        return result

    def list_all(self) -> Any:
        tree = self("panes", "--all")
        if isinstance(tree, dict):
            # system.version's API response owns layout semantics (ADR 0017).
            now = time.monotonic()
            if not tree.get("api") and (self._layout_api is None or now - self._layout_api_at >= 45):
                try:
                    version = self.client.request("system.version", {})
                except MycmuxError:
                    version = {}
                self._layout_api = (version.get("api") if isinstance(version, dict) else None) or {"major": 1, "minor": 0}
                self._layout_api_at = now
            api = tree.get("api") or self._layout_api
            if isinstance(api, dict):
                tree = {**tree, "api": api, "workspaces": [
                    {**ws, "api": ws.get("api") or api} for ws in _workspace_rows(tree)]}
        return tree

    def health(self) -> dict[str, Any]:
        connected, _, meta = self.feed.snapshot()
        return {"connected": connected, "seq": meta["seq"],
                "lastError": meta["error"] or self.last_error,
                "fallback": self.fallback, "workspaceErrors": list(self.workspace_errors)}


SOCKET_BRIDGE = SocketBridge(MycmuxSocket(), _run_bridge_cli)


def run_bridge(*args: str) -> Any:
    return SOCKET_BRIDGE(*args)


def cached_bridge(*args: str) -> Any:
    # A direct read has no process startup cost; feed updates must not wait for TTL.
    return run_bridge(*args)


def _socket_source(bridge: Callable[..., Any]) -> SocketBridge | None:
    if isinstance(bridge, SocketBridge):
        return bridge
    if bridge in (cached_bridge, run_bridge):
        return SOCKET_BRIDGE
    return getattr(getattr(bridge, "__self__", None), "socket_bridge", None)


def _account_request(bridge):
    source = _socket_source(bridge)
    if source is not None:
        return source.client.request
    # Fixture bridges must explicitly opt into account operations.
    request = getattr(bridge, "account_request", None)
    if callable(request):
        return request
    def unavailable(*args):
        raise MycmuxError("accountUnavailable")
    return unavailable


def _feed_tab(tab: Any, sessions: dict[str, Any] | None) -> Any:
    if (not isinstance(tab, dict) or sessions is None or
            _status_value(tab.get("type") or "terminal") != "terminal" or
            tab.get("lifecycle") == "declared"):
        return tab
    session_id = tab.get("sessionId")
    status = sessions.get(session_id, {}) if isinstance(session_id, str) else {}
    state = status.get("ui_state")
    observed = isinstance(state, str) and state in {"working", "waiting", "done", "idle"}
    return {**tab, "uiState": state if observed else None,
            "agentStatus": state if observed else None, "processStatus": None,
            "agentStatusStale": not observed, "screenObserved": observed}


def _listing_bridge(bridge: Callable[..., Any]) -> Callable[..., Any]:
    """Port only build_view's whole-tree grouping; Phase 16 still builds rows."""
    source = _socket_source(bridge)
    if source is None:
        # Existing callers can still inject the legacy bridge contract.
        return bridge
    tree = source.list_all()
    if not isinstance(tree, dict) or not isinstance(tree.get("panes"), list):
        raise BridgeError()
    connected, sessions, _ = source.feed.snapshot()
    panes_by_workspace: dict[str, list[Any]] = {}
    for pane in tree["panes"]:
        if not isinstance(pane, dict) or not isinstance(pane.get("workspaceId"), str):
            continue
        pane = dict(pane)
        if isinstance(pane.get("tabs"), list):
            pane["tabs"] = [_feed_tab(tab, sessions if connected else None) for tab in pane["tabs"]]
        panes_by_workspace.setdefault(pane["workspaceId"], []).append(pane)
    ordered_workspaces = [dict(workspace) for workspace in _workspace_rows(tree)]
    for workspace in ordered_workspaces:
        workspace["id"] = workspace.get("workspaceId", workspace.get("id"))
        workspace["name"] = workspace.get("workspaceName", workspace.get("name"))
    seen_ids = {workspace["id"] for workspace in ordered_workspaces if isinstance(workspace["id"], str)}
    for workspace_id, panes in panes_by_workspace.items():
        if workspace_id not in seen_ids:
            ordered_workspaces.append({"id": workspace_id, "name": panes[0].get("workspaceName")})
    workspace_payload = {**tree, "workspaces": ordered_workspaces}
    # Keep raw totals, including malformed entries that row builders cannot show.
    workspace_payload.setdefault("paneCount", len(tree["panes"]))
    workspace_payload.setdefault("tabCount", sum(
        _source_count(pane.get("tabCount"), len(pane["tabs"]) if isinstance(pane.get("tabs"), list) else 0)
        for pane in tree["panes"] if isinstance(pane, dict)
    ))

    def snapshot(*args: str) -> Any:
        if args == ("workspaces",):
            return workspace_payload
        if len(args) == 3 and args[:2] == ("panes", "--workspace"):
            workspace = next((row for row in ordered_workspaces if row["id"] == args[2]), None)
            if workspace is None or workspace.get("error"):
                raise BridgeError("workspaceUnavailable")
            return {**workspace, "panes": panes_by_workspace.get(args[2], [])}
        raise BridgeError()

    return snapshot


#: Must stay under the client's own budget: mycmux gives health 2 seconds
#: (remote/iphone.rs health(..., 2)) and reads a timeout as "not healthy", so a
#: server that waits longer than that reports itself dead while it is working.
HEALTH_WAIT_SECONDS = 1.5
_HEALTH_INIT_LOCK = threading.Lock()


class _HealthProbe:
    """Bound each response and share one in-flight read across health requests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.future: Future | None = None
        self.last_ok_at: str | None = None

    def start(self, operation: Callable[[], Any]) -> Future:
        with self.lock:
            if self.future is not None and not self.future.done():
                return self.future
            future: Future = Future()
            self.future = future

            def run() -> None:
                try:
                    value = operation()
                    with self.lock:
                        self.last_ok_at = _health_timestamp()
                    future.set_result(value)
                except Exception as exc:
                    future.set_exception(exc)

            threading.Thread(target=run, daemon=True, name="pocket-health").start()
            return future


def state_dir_path(httpd: Any) -> str | None:
    """Absolute path of the folder this server actually reads its state from."""
    state_dir = getattr(httpd, "state_dir", None)
    return None if state_dir is None else os.fspath(state_dir)


def _health_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _health_bridge(httpd: Any) -> bool:
    # Observe the existing concurrency limit without adding a second bridge path.
    if not BRIDGE_SEMAPHORE.acquire(blocking=False):
        raise BridgeError("bridgeBusy")
    BRIDGE_SEMAPHORE.release()
    payload = httpd.cached_bridge("workspaces")
    if isinstance(payload, dict) and isinstance(payload.get("workspaces"), list):
        return True
    if isinstance(payload, list):
        return True
    raise BridgeError()


def _health_mail(httpd: Any) -> bool:
    if not httpd.features.get("mail", False) or httpd.mail is None:
        return False
    # Reuse snapshot validation, without filtering threads or counting replies.
    payload = httpd.mail.snapshot._read()
    return isinstance(payload.get("pending", payload.get("balls", [])), list)


def get_health(httpd: Any) -> dict[str, Any]:
    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    with _HEALTH_INIT_LOCK:
        if not hasattr(httpd, "_health_probes"):
            httpd._health_probes = {name: _HealthProbe() for name in ("bridge", "mail")}
        probes = httpd._health_probes
    bridge_ok = False
    mail_ok = False
    last_error = None
    try:
        bridge_future = probes["bridge"].start(lambda: _health_bridge(httpd))
        mail_future = probes["mail"].start(lambda: _health_mail(httpd))
        try:
            bridge_ok = bridge_future.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            last_error = "bridgeTimeout"
        except BridgeError as exc:
            last_error = "bridgeBusy" if exc.code == "bridgeBusy" else "bridgeUnavailable"
        except Exception:
            last_error = "bridgeUnavailable"
        try:
            mail_ok = mail_future.result(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            mail_ok = False
    except Exception:
        last_error = "bridgeUnavailable"
    # The PC route consumes this observation without starting a UI probe.
    httpd._pc_ui_ok = bridge_ok
    return {
        "server": "ok",
        # Behind the identity check with every other route, so this absolute
        # local path is only ever handed to a caller the server already trusts.
        "stateDir": state_dir_path(httpd),
        "bridge": {
            "ok": bridge_ok,
            "lastOkAt": probes["bridge"].last_ok_at,
            "lastError": last_error,
            "busy": last_error == "bridgeBusy",
        },
        "mycmux": {"connected": bridge_ok},
        "mail": {"ok": mail_ok},
        "generatedAt": _health_timestamp(),
        "feed": (httpd.socket_bridge.health() if getattr(httpd, "socket_bridge", None) is not None
                 else {"connected": False, "seq": 0, "lastError": None, "fallback": True,
                       "workspaceErrors": []}),
    }


def get_mac_status(httpd: Any) -> dict[str, Any]:
    """The existing tailnet cache's view of the Mac, without probing its relay."""
    result = {"macPeerOnline": None, "lastHandshake": None,
              "reason": "tailscaleUnavailable", "generatedAt": _health_timestamp()}
    peer_host = os.environ.get("POCKET_MAC_PEER_HOST", "").strip().rstrip(".").casefold()
    if not peer_host:
        result["reason"] = "notConfigured"
        return result
    try:
        status = httpd.tailscale_status.get()
    except Exception:
        return result
    peers = status.get("Peer") if isinstance(status, dict) else None
    if not isinstance(peers, dict):
        return result
    result["reason"] = "peerNotListed"
    for peer in peers.values():
        if not isinstance(peer, dict):
            continue
        dns = peer.get("DNSName")
        if not isinstance(dns, str):
            continue
        normalized_dns = dns.rstrip(".").casefold()
        if peer_host not in {normalized_dns, normalized_dns.split(".", 1)[0]}:
            continue
        online = peer.get("Online")
        result["macPeerOnline"] = online if isinstance(online, bool) else None
        result["reason"] = None if isinstance(online, bool) else "peerStatusUnknown"
        handshake = peer.get("LastHandshake")
        if isinstance(handshake, str):
            try:
                at = datetime.fromisoformat(handshake.replace("Z", "+00:00"))
                if at.year > 1:
                    result["lastHandshake"] = handshake
            except ValueError:
                pass
        break
    return result


def create_pc_status(httpd: Any) -> pc_status.PCStatus:
    """Project existing observations and a dedicated, short Rust-only socket."""
    source = getattr(httpd, "socket_bridge", None)
    core_client = (MycmuxSocket(runtime=source.client.runtime, timeout=pc_status.CORE_TIMEOUT_SECONDS)
                   if source is not None else None)
    return pc_status.PCStatus(
        version_reader=lambda: core_client.request("system.version") if core_client is not None else None,
        ui_reader=lambda: getattr(httpd, "_pc_ui_ok", None),
        feed_reader=lambda: source is not None and source.health()["connected"],
        overview_reader=lambda: (httpd.seat_activity.snapshot("overview")
                                 if httpd.seat_activity.ready else None),
        build_reader=build_id,
    )


def _status_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def normalize_status(agent_status: Any, process_status: Any) -> str:
    values = {_status_value(agent_status), _status_value(process_status)} - {""}
    attention = {
        "attention",
        "approval",
        "blocked",
        "error",
        "input",
        "needs_attention",
        "rate_limited",
        "waiting",
    }
    stopped = {
        "closed",
        "dead",
        "exited",
        "failed",
        "orphaned",
        "stopped",
        "terminated",
    }
    working = {"busy", "running", "running_silent", "starting", "streaming", "working"}
    # mycmux "done" marks a finished turn; the seat process is still alive.
    idle = {"done", "idle", "ready", "sleeping"}
    if values & attention:
        return "attention"
    if values & stopped:
        return "stopped"
    # The agent's own word comes first when it has one. mycmux reports the pane
    # process as "working" for as long as it is alive, so merging the two and
    # letting "working" win said every live seat was thinking -- the phone kept
    # its "thinking" line on screen for hours beside a seat that had finished
    # (2026-09-20). The process still decides for a pane with no agent.
    agent = _status_value(agent_status)
    if agent in working:
        return "working"
    if agent in idle:
        return "idle"
    if values & working:
        return "working"
    if values & idle:
        return "idle"
    return "unknown"


# A transcript never moves once written, but the search for it walked every
# project directory - 93 of them here - and a single talk request asks twice.
# On a machine where a backup agent or the virus scanner is reading that same
# tree, those walks turned a 50 ms request into 23 s. The answer is immutable,
# so remember it and confirm with one stat instead of ninety-three.
_TRANSCRIPT_PATHS: "OrderedDict[tuple[str, ...], Path]" = OrderedDict()
# Only overview topics use misses; talk and lines must discover new files now.
_TRANSCRIPT_MISSES: "OrderedDict[tuple[str, ...], float]" = OrderedDict()
_TRANSCRIPT_PATHS_GUARD = threading.Lock()
_TRANSCRIPT_CLOCK = time.monotonic
TRANSCRIPT_PATH_CACHE_SIZE = 512


def _transcript_directories() -> tuple[str, ...]:
    signature = []
    for root in dict.fromkeys((TRANSCRIPT_ROOT, HYBRID_TRANSCRIPT_ROOT)):
        signature.append(os.fspath(root))
        try:
            with os.scandir(root) as entries:
                signature.extend(sorted(f"{entry.name}:{Path(entry.path).stat(follow_symlinks=False).st_mtime_ns}"
                                        for entry in entries if entry.is_dir(follow_symlinks=False)))
        except FileNotFoundError:
            signature.append("missing")
    return tuple(signature)


def _scan_for_transcript(session_id: str) -> Path | None:
    matches = set()
    for root in dict.fromkeys((TRANSCRIPT_ROOT, HYBRID_TRANSCRIPT_ROOT)):
        try:
            for path in root.glob(f"*/{session_id}.jsonl"):
                if (path.is_file() and not path.is_symlink() and not path.parent.is_symlink()
                        and path.resolve().is_relative_to(root.resolve())):
                    matches.add(path.resolve())
        except OSError:
            return None
    return next(iter(matches)) if len(matches) == 1 else None


def find_transcript(session_id: str, *, cache_absence: bool = False) -> Path | None:
    if not SESSION_ID_RE.fullmatch(session_id):
        return None
    # The root is part of the key so that pointing TRANSCRIPT_ROOT somewhere
    # else - which the tests do for every fixture - cannot be answered from the
    # previous tree.
    miss_key = (os.fspath(TRANSCRIPT_ROOT), os.fspath(HYBRID_TRANSCRIPT_ROOT), session_id)
    with _TRANSCRIPT_PATHS_GUARD:
        missed_at = _TRANSCRIPT_MISSES.get(miss_key) if cache_absence else None
        if missed_at is not None:
            if _TRANSCRIPT_CLOCK() - missed_at < 30:
                _TRANSCRIPT_MISSES.move_to_end(miss_key)
                return None
            _TRANSCRIPT_MISSES.pop(miss_key, None)
    try:
        key = (*_transcript_directories(), session_id)
    except OSError:
        return None
    with _TRANSCRIPT_PATHS_GUARD:
        cached = _TRANSCRIPT_PATHS.get(key)
    if cached is not None:
        try:
            if cached.is_file():
                return cached
        except OSError:
            return None
        # The file was removed or the tree was reorganised; look again rather
        # than reporting a session that is still on disk as missing.
        with _TRANSCRIPT_PATHS_GUARD:
            _TRANSCRIPT_PATHS.pop(key, None)
    found = _scan_for_transcript(session_id)
    if found is None:
        if cache_absence:
            with _TRANSCRIPT_PATHS_GUARD:
                _TRANSCRIPT_MISSES[miss_key] = _TRANSCRIPT_CLOCK()
                _TRANSCRIPT_MISSES.move_to_end(miss_key)
                while len(_TRANSCRIPT_MISSES) > TRANSCRIPT_PATH_CACHE_SIZE:
                    _TRANSCRIPT_MISSES.popitem(last=False)
        return None
    try:
        after_key = (*_transcript_directories(), session_id)
        if after_key != key:
            found = _scan_for_transcript(session_id)
            if found is None or after_key != (*_transcript_directories(), session_id):
                return None
            key = after_key
    except OSError:
        return None
    with _TRANSCRIPT_PATHS_GUARD:
        _TRANSCRIPT_MISSES.pop(miss_key, None)
        _TRANSCRIPT_PATHS[key] = found
        _TRANSCRIPT_PATHS.move_to_end(key)
        while len(_TRANSCRIPT_PATHS) > TRANSCRIPT_PATH_CACHE_SIZE:
            _TRANSCRIPT_PATHS.popitem(last=False)
    return found


def _session_status_from_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if record.get("type") == "user":
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        parts = [content] if isinstance(content, str) else [
            block.get("text", "") for block in content or [] if isinstance(block, dict)
        ] if isinstance(content, list) else []
        for part in parts:
            if not isinstance(part, str):
                continue
            output = re.fullmatch(r"\s*<local-command-stdout>(.*?)</local-command-stdout>\s*", part, re.DOTALL)
            if output is None:
                continue
            text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|[*_]", "", output[1])
            model_change = re.search(r"\bSet model to\s+([A-Za-z0-9.-]+)", text)
            effort_change = re.search(r"(?:Set effort(?: level)? to|with)\s+(low|medium|high|xhigh|max|ultra)\b", text)
            if model_change or effort_change:
                exact_model = re.search(r"\bclaude-[A-Za-z0-9.-]+", text)
                return {"model": (exact_model[0] if exact_model else model_change[1]) if model_change else None,
                        "effort": effort_change[1] if effort_change else None,
                        "contextTokens": None, "at": record.get("timestamp")}
        return None
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    model = message.get("model")
    if model == "<synthetic>":
        return None
    effort = record.get("perTurnEffort") or record.get("effort")
    usage = message.get("usage")
    token_counts = []
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                token_counts.append(value)
    timestamp = record.get("timestamp")
    return {
        "model": model if isinstance(model, str) and model else None,
        "effort": effort if isinstance(effort, str) and effort else None,
        "contextTokens": sum(token_counts) if token_counts else None,
        "at": timestamp if isinstance(timestamp, str) and timestamp else None,
    }


def transcript_session_status(
    session_id: str,
    block_bytes: int = TRANSCRIPT_BLOCK_BYTES,
) -> dict[str, Any] | None:
    return _read_session_status(find_transcript(session_id), "claude", block_bytes)


def _read_session_status(path: Path | None, agent: str, block_bytes: int) -> dict[str, Any] | None:
    if path is None:
        return None
    if block_bytes < 1:
        raise ValueError("invalid transcript status scan configuration")
    try:
        size, mtime = _growth_stamp(path, agent)
        return dict(_cached_session_status(os.fspath(path), size, mtime, agent, block_bytes))
    except OSError:
        return None


@lru_cache(maxsize=256)
def _cached_session_status(path: str, size: int, mtime: int, agent: str, block_bytes: int) -> dict[str, Any]:
    status = {"model": None, "effort": None, "contextTokens": None, "at": None}
    parser = _codex_session_status_from_record if agent == "codex" else _session_status_from_record
    with Path(path).open("rb") as handle:
        position = size
        carry = b""
        while position > 0:
            next_start = max(0, position - block_bytes)
            handle.seek(next_start)
            payload = handle.read(position - next_start) + carry
            position = next_start
            if next_start > 0:
                newline = payload.find(b"\n")
                if newline < 0:
                    carry = payload
                    continue
                carry = payload[:newline]
                payload = payload[newline + 1 :]
            for raw_record in reversed(payload.splitlines()):
                if not raw_record:
                    continue
                try:
                    record = json.loads(raw_record.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                observed = parser(record)
                if observed is None:
                    continue
                # An older model's effort cannot describe a newer model.
                if status["model"] and observed["model"] and status["model"] != observed["model"]:
                    return status
                for key in status:
                    if status[key] is None and observed[key] is not None:
                        status[key] = observed[key]
                if status["model"] and status["effort"]:
                    return status
    return status


def read_tail_lines(path: Path, max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        payload = handle.read(max_bytes)
    if start > 0:
        newline = payload.find(b"\n")
        payload = payload[newline + 1 :] if newline >= 0 else b""
    return payload.decode("utf-8", errors="ignore").splitlines()


def _clean_user_text(text: str) -> str:
    cleaned = SYSTEM_REMINDER_RE.sub("", text).strip()
    if not cleaned or COMMAND_PREFIX_RE.match(cleaned):
        return ""
    return cleaned


def _user_text(record: dict[str, Any], content: Any) -> str:
    if record.get("isMeta") is True:
        return ""
    if record.get("sourceToolAssistantUUID") or record.get("toolUseResult") is not None:
        return ""
    if isinstance(content, str):
        return _clean_user_text(content)
    if not isinstance(content, list):
        return ""
    text_parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                text_parts.append(value)
    return _clean_user_text("\n".join(text_parts))


def _assistant_parts(content: Any) -> tuple[str, int]:
    if isinstance(content, str):
        return content.strip(), 0
    if not isinstance(content, list):
        return "", 0
    text_parts = []
    tool_count = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_count += 1
    return "\n".join(text_parts).strip(), tool_count


def _conversation_event(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    record_type = record.get("type")
    message = record.get("message")
    if record_type not in {"user", "assistant"} or not isinstance(message, dict):
        return None
    content = message.get("content")
    if record_type == "user":
        text = _user_text(record, content)
        tool_count = 0
        if not text:
            return None
    else:
        text, tool_count = _assistant_parts(content)
        if not text and tool_count == 0:
            return None
    return {
        "role": record_type,
        "ts": record.get("timestamp"),
        "text": text[:MAX_EVENT_TEXT],
        "toolCount": tool_count,
    }


def parse_transcript_lines(lines: list[str], limit: int = 50) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        event = _conversation_event(record)
        if event is not None:
            events.append(event)
    return events[-limit:]


def _events_from_transcript_bytes(
    payload: bytes, base_offset: int, event_parser: Callable = _conversation_event,
) -> list[dict[str, Any]]:
    events = []
    offset = base_offset
    for raw_line in payload.splitlines(keepends=True):
        line_offset = offset
        offset += len(raw_line)
        raw_record = raw_line.rstrip(b"\r\n")
        if not raw_record:
            continue
        try:
            record = json.loads(raw_record.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        event = event_parser(record)
        if event is not None:
            event["cursor"] = str(line_offset)
            events.append(event)
    return events



def _activity_record(record: Any) -> dict[str, Any] | None:
    # Keep tool results and input details that the visible conversation omits.
    # The conversation's byte parser still owns JSON decoding and cursors.
    return dict(record) if isinstance(record, dict) else None


def _unknown_activity() -> dict[str, Any]:
    return {
        "state": "unknown", "turnStartedAt": None, "lastEventAt": None,
        "silentSeconds": 0.0, "toolCount": 0, "step": None,
        "steps": [], "source": "transcript",
    }


def _activity_tool_step(block: dict[str, Any]) -> dict[str, Any]:
    name = block.get("name")
    inputs = block.get("input")
    detail = None
    if isinstance(inputs, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "description", "url"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                detail = " ".join(value.split())[:240]
                break
    return {
        "kind": "tool",
        "name": name[:160] if isinstance(name, str) else None,
        "detail": detail,
    }


def _activity_records(path: Path, *, handle: Any = None, end: int | None = None):
    # Keep one handle for the scan, file identity and append boundary checks.
    if handle is None:
        with path.open("rb") as opened:
            yield from _activity_records(path, handle=opened, end=end)
        return
    position = handle.seek(0, os.SEEK_END) if end is None else end
    carry = b""
    while position > 0:
        next_start = max(0, position - TRANSCRIPT_BLOCK_BYTES)
        handle.seek(next_start)
        payload = handle.read(position - next_start) + carry
        position = next_start
        offset = next_start
        if next_start > 0:
            newline = payload.find(b"\n")
            if newline < 0:
                carry = payload
                continue
            carry = payload[:newline]
            offset += newline + 1
            payload = payload[newline + 1:]
        else:
            carry = b""
        yield from reversed(_events_from_transcript_bytes(payload, offset, _activity_record))


class _ActivityTurn:
    """The current turn, including IDs that have fallen out of the visible tail."""

    def __init__(self) -> None:
        self.value = _unknown_activity()
        self.has_human = False
        self.tool_ids: set[Any] = set()
        self.steps: OrderedDict[Any, dict[str, Any]] = OrderedDict()

    def add_tool(self, identity: Any, block: dict[str, Any]) -> None:
        self.tool_ids.add(identity)
        # The backward scan keeps the latest occurrence of each ID.
        self.steps.pop(identity, None)
        self.steps[identity] = _activity_tool_step(block)
        if len(self.steps) > 20:
            self.steps.popitem(last=False)

    def append(self, record: dict[str, Any]) -> None:
        stamp = record.get("timestamp")
        timestamp = stamp if _parse_timestamp(stamp) is not None else None
        event = _conversation_event(record)
        if event is not None and event["role"] == "user":
            self.__init__()
            self.has_human = True
            self.value["turnStartedAt"] = timestamp
        if not self.has_human:
            return
        if timestamp is not None:
            self.value["lastEventAt"] = timestamp
        message = record.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        tools = [block for block in blocks
                 if isinstance(block, dict) and block.get("type") == "tool_use"
                 ] if record.get("type") == "assistant" else []
        if tools:
            self.value["state"] = "working"
            for index, block in enumerate(tools):
                raw_id = block.get("id")
                identity = raw_id if isinstance(raw_id, str) else (record["cursor"], index)
                self.add_tool(identity, block)
        elif record.get("type") == "user" and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks
        ):
            self.value["state"] = "working"
        elif event is not None and event["role"] == "assistant" and event["text"]:
            self.value["state"] = "idle"

    def result(self) -> dict[str, Any]:
        steps = list(self.steps.values())
        return {**self.value, "toolCount": len(self.tool_ids), "steps": steps,
                "step": steps[-1] if steps else None}


def _scan_activity_turn(path: Path, handle: Any, size: int) -> _ActivityTurn:
    turn = _ActivityTurn()
    state = None
    for record in _activity_records(path, handle=handle, end=size):
        stamp = record.get("timestamp")
        if turn.value["lastEventAt"] is None and _parse_timestamp(stamp) is not None:
            turn.value["lastEventAt"] = stamp
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        event = _conversation_event(record)
        if event is not None and event["role"] == "user":
            turn.has_human = True
            turn.value["turnStartedAt"] = stamp if _parse_timestamp(stamp) is not None else None
            turn.value["state"] = state or "unknown"
            turn.steps = OrderedDict(reversed(list(turn.steps.items())))
            return turn
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        tools = [block for block in blocks
                 if isinstance(block, dict) and block.get("type") == "tool_use"
                 ] if record.get("type") == "assistant" else []
        if tools:
            state = state or "working"
            for index, block in reversed(list(enumerate(tools))):
                raw_id = block.get("id")
                identity = raw_id if isinstance(raw_id, str) else (record["cursor"], index)
                if identity in turn.tool_ids:
                    continue
                turn.tool_ids.add(identity)
                if len(turn.steps) < 20:
                    turn.steps[identity] = _activity_tool_step(block)
        elif record.get("type") == "user" and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks
        ):
            state = state or "working"
        elif event is not None and event["role"] == "assistant" and event["text"]:
            state = state or "idle"
    return _ActivityTurn()


ACTIVITY_ANCHOR_BYTES = 4096


def _activity_signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _activity_anchors(handle: Any, size: int) -> tuple[bytes, bytes]:
    handle.seek(0)
    head = handle.read(min(size, ACTIVITY_ANCHOR_BYTES))
    handle.seek(max(0, size - ACTIVITY_ANCHOR_BYTES))
    return head, handle.read(min(size, ACTIVITY_ANCHOR_BYTES))


class _ActivityFile:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.signature: tuple[int, ...] | None = None
        self.content: transcript_snapshot.Snapshot | None = None
        self.turn = _ActivityTurn()

    def read(self, path: Path) -> dict[str, Any]:
        with self.lock:
            previous = self.signature
            self.signature = None
            with path.open("rb", buffering=0) as handle:
                signature = _activity_signature(os.fstat(handle.fileno()))
                size = signature[2]
                # No metadata-only fast path: even an unchanged size, timestamp
                # and journal can conceal an unflushed writable-map edit.
                content = (transcript_snapshot.Snapshot.read(handle, size, self.content)
                           if size <= transcript_snapshot.MAX_CACHE_BYTES else None)
                same_file = previous is not None and signature[:2] == previous[:2] and signature[1] != 0
                unchanged = same_file and content is not None and content == self.content
                append_only = (
                    same_file and content is not None and self.content is not None
                    and (self.content.size == 0 or self.content.blocks[-1].endswith(b"\n"))
                    and content.extends(self.content)
                )
                reader = content.reader() if content is not None else handle
                if append_only:
                    offset = previous[2]
                    reader.seek(offset)
                    carry = b""
                    remaining = size - offset
                    while remaining:
                        payload = reader.read(min(TRANSCRIPT_BLOCK_BYTES, remaining))
                        if not payload:
                            raise OSError("transcript shrank while reading")
                        remaining -= len(payload)
                        carry += payload
                        end = carry.rfind(b"\n") + 1 if remaining else len(carry)
                        for record in _events_from_transcript_bytes(carry[:end], offset, _activity_record):
                            self.turn.append(record)
                        offset += end
                        carry = carry[end:]
                elif not unchanged:
                    self.turn = _scan_activity_turn(path, reader, size)
                # Parsing sees immutable captured bytes. Check them once more
                # after a changed turn so a concurrent hidden edit invalidates
                # the aggregate rather than being certified by unchanged stat.
                if (content is not None and not unchanged and not content.matches(handle)
                        or _activity_signature(os.fstat(handle.fileno())) != signature
                        or _activity_signature(path.stat()) != signature):
                    raise OSError("transcript changed while reading")
                self.content = content
                self.signature = signature
                return self.turn.result()


class _ActivityCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.files: OrderedDict[str, _ActivityFile] = OrderedDict()

    def __call__(self, path_value: str, file_size: int, modified_ns: int) -> dict[str, Any]:
        # Serialize cache admission as well as observation: many parallel waits
        # must not each retain their own 128 MiB snapshot. Other request work is
        # outside this lock. Oversized transcripts are freshly scanned, uncached.
        with self.lock:
            entry = self.files.pop(path_value, None) or _ActivityFile()
            try:
                return entry.read(Path(path_value))
            finally:
                self.files[path_value] = entry
                retained = sum(item.content.size for item in self.files.values() if item.content is not None)
                while len(self.files) > 256 or retained > transcript_snapshot.MAX_CACHE_BYTES:
                    _, expired = self.files.popitem(last=False)
                    if expired.content is not None:
                        retained -= expired.content.size

    def cache_clear(self) -> None:
        with self.lock:
            self.files.clear()


_cached_transcript_activity = _ActivityCache()


def transcript_activity(session_id: str, *, now: datetime | float) -> dict[str, Any]:
    """Observe the current Claude turn even when its terminal is in the back.

    Keep the current turn per file and consume only appended JSONL records.
    Rebuild on replacement, truncation or an unverified append boundary.
    Silence still advances on every call; unavailable transcripts stay unknown.
    """
    try:
        path = find_transcript(session_id)
        if path is None:
            return _unknown_activity()
        info = path.stat()
        key = (os.fspath(path), info.st_size, info.st_mtime_ns)
        cached = SINGLE_FLIGHT.run(
            ("activity", key), lambda: _cached_transcript_activity(*key),
        )
    except (OSError, ValueError):
        return _unknown_activity()
    result = {**cached, "steps": [dict(step) for step in cached["steps"]]}
    result["step"] = dict(cached["step"]) if cached["step"] else None
    last_event = _parse_timestamp(result["lastEventAt"])
    current = now.timestamp() if isinstance(now, datetime) else now
    if last_event is not None:
        result["silentSeconds"] = max(0.0, current - last_event)
        if result["state"] == "working" and result["silentSeconds"] >= 90:
            result["state"] = "stalled"
    return result


def transcript_page(
    session_id: str,
    limit: int = TRANSCRIPT_PAGE_LIMIT,
    before: int | None = None,
    block_bytes: int = TRANSCRIPT_BLOCK_BYTES,
    max_scan_bytes: int = TRANSCRIPT_SCAN_BYTES,
) -> dict[str, Any] | None:
    path = find_transcript(session_id)
    if path is None:
        return None
    return _transcript_page_from_path(path, limit, before, block_bytes, max_scan_bytes)


def _transcript_page_from_path(
    path: Path, limit: int, before: int | None, block_bytes: int, max_scan_bytes: int,
    event_parser: Callable = _conversation_event,
) -> dict[str, Any] | None:
    if not 1 <= limit <= TRANSCRIPT_PAGE_LIMIT:
        raise ValueError("invalid limit")
    if block_bytes < 1 or max_scan_bytes < 1:
        raise ValueError("invalid transcript scan configuration")
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            if not isinstance(before, (int, type(None))) or isinstance(before, bool):
                raise ValueError("invalid before cursor")
            continuation = before is not None and before >= TRANSCRIPT_CONTINUATION_CURSOR_BASE
            end = (
                before - TRANSCRIPT_CONTINUATION_CURSOR_BASE
                if continuation
                else file_size if before is None else before
            )
            if not 0 <= end <= file_size:
                raise ValueError("invalid before cursor")
            if before is not None and not continuation and 0 < end < file_size:
                handle.seek(end - 1)
                if handle.read(1) != b"\n":
                    raise ValueError("invalid before cursor")

            lower_bound = max(0, end - max_scan_bytes)
            position = end
            carry = b""
            newest_first: list[dict[str, Any]] = []
            oldest_complete_offset: int | None = None
            while position > lower_bound:
                next_start = max(lower_bound, position - block_bytes)
                handle.seek(next_start)
                payload = handle.read(position - next_start) + carry
                position = next_start
                complete_payload = payload
                complete_offset = next_start
                if next_start > 0:
                    newline = payload.find(b"\n")
                    if newline < 0:
                        carry = payload
                        continue
                    carry = payload[:newline]
                    complete_offset = next_start + newline + 1
                    complete_payload = payload[newline + 1 :]
                else:
                    carry = b""
                if complete_payload:
                    oldest_complete_offset = complete_offset
                block_events = _events_from_transcript_bytes(
                    complete_payload,
                    complete_offset,
                    event_parser,
                )
                target_count = limit + 1 if next_start == 0 else limit
                for event in reversed(block_events):
                    newest_first.append(event)
                    if len(newest_first) >= target_count:
                        break
                if len(newest_first) >= target_count:
                    break

            has_older_event = len(newest_first) > limit
            returned = list(reversed(newest_first[:limit]))
            at_start = position == 0 and not has_older_event
            next_before = None
            if not at_start:
                if returned:
                    next_before = returned[0]["cursor"]
                elif oldest_complete_offset is not None and oldest_complete_offset < end:
                    next_before = str(oldest_complete_offset)
                elif position > 0:
                    next_before = str(TRANSCRIPT_CONTINUATION_CURSOR_BASE + position)
            scanned_bytes = end - position
            return {
                "events": returned,
                "nextBefore": next_before,
                "atStart": at_start,
                "scanLimited": position > 0 and scanned_bytes >= max_scan_bytes,
                "scannedBytes": scanned_bytes,
            }
    except OSError:
        return None


def _transcript_page_after_from_path(
    path: Path, after: int, limit: int,
    event_parser: Callable = _conversation_event,
) -> dict[str, Any] | None:
    """Events whose record starts after ``after``, oldest first, at most ``limit``.

    The cursor itself is excluded: a phone that holds the event at ``after``
    asks for what came later. Only complete records count; a line still being
    written is left for the next call. ``hasMore`` says a further call with
    the last returned cursor has something to fetch.
    """
    if not 1 <= limit <= TRANSCRIPT_PAGE_LIMIT:
        raise ValueError("invalid limit")
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise ValueError("invalid after cursor")
    empty = {"events": [], "hasMore": False, "nextBefore": None, "atStart": False}
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            if after >= file_size:
                return empty
            handle.seek(after)
            # The record at the cursor (or the rest of the one it points
            # into) is the caller's already; start at the next line.
            handle.readline()
            events: list[dict[str, Any]] = []
            offset = handle.tell()
            # ponytail: a forward read with no byte cap. A cursor from long
            # ago costs one sequential pass over the file; a cap would need a
            # second kind of cursor for the phone to continue from.
            while len(events) <= limit:
                raw_line = handle.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    break
                line_offset = offset
                offset += len(raw_line)
                raw_record = raw_line.rstrip(b"\r\n")
                if not raw_record:
                    continue
                try:
                    record = json.loads(raw_record.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                event = event_parser(record)
                if event is not None:
                    event["cursor"] = str(line_offset)
                    events.append(event)
            return {**empty, "events": events[:limit], "hasMore": len(events) > limit}
    except OSError:
        return None


def transcript_page_after(
    session_id: str, after: int, limit: int = TRANSCRIPT_PAGE_LIMIT,
) -> dict[str, Any] | None:
    path = find_transcript(session_id)
    if path is None:
        return None
    return _transcript_page_after_from_path(path, after, limit)



_CODEX_INDEX_ROOT: str | None = None
_CODEX_INDEX: dict[str, Path | None] = {}
_CODEX_INDEX_REBUILT_AT: float | None = None
_CODEX_INDEX_GUARD = threading.Lock()
_CODEX_INDEX_CLOCK = time.monotonic
_CODEX_ROLLOUT_NAME = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl$"
)


def _rebuild_codex_transcript_index() -> None:
    """Called under the index lock; never open or stat every rollout."""
    global _CODEX_INDEX_ROOT, _CODEX_INDEX, _CODEX_INDEX_REBUILT_AT
    found: dict[str, Path | None] = {}
    try:
        for path in CODEX_TRANSCRIPT_ROOT.glob("*/*/*/rollout-*.jsonl"):
            match = _CODEX_ROLLOUT_NAME.fullmatch(path.name)
            if match and SESSION_ID_RE.fullmatch(match[1]):
                session_id = match[1]
                found[session_id] = None if session_id in found else path
    except OSError:
        # A partial directory walk must not claim an unambiguous identity.
        found = {}
    _CODEX_INDEX_ROOT = os.fspath(CODEX_TRANSCRIPT_ROOT)
    _CODEX_INDEX = found
    _CODEX_INDEX_REBUILT_AT = _CODEX_INDEX_CLOCK()


def prime_codex_transcript_index() -> None:
    # Pay the one directory walk before accepting browser requests. Lazy
    # initialization remains available to embedded callers and temporary roots.
    with _CODEX_INDEX_GUARD:
        _rebuild_codex_transcript_index()


def find_codex_transcript(session_id: str, *, cache_absence: bool = False) -> Path | None:
    """Resolve the exact agent identity, without using modification times."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return None
    with _CODEX_INDEX_GUARD:
        rebuilt = _CODEX_INDEX_ROOT != os.fspath(CODEX_TRANSCRIPT_ROOT)
        if rebuilt:
            _rebuild_codex_transcript_index()
        path = _CODEX_INDEX.get(session_id)
        if path is not None and path.is_file():
            return path
        if not rebuilt and (not cache_absence or _CODEX_INDEX_REBUILT_AT is None
                            or _CODEX_INDEX_CLOCK() - _CODEX_INDEX_REBUILT_AT >= 10):
            # A running seat may create its file later. Only overview topics
            # allow a ten-second delay to avoid a walk for each absent seat.
            _rebuild_codex_transcript_index()
            path = _CODEX_INDEX.get(session_id)
        return path if path is not None and path.is_file() else None


_CODEX_TEXT_CONTROLS = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b[P^_]|\x90|\x98|\x9e|\x9f).*?(?:\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|$)"
    r"|\x1b[ -/]*[@-~]|[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]",
    re.DOTALL,
)


def _codex_session_status_from_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict) or record.get("type") != "turn_context":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    collaboration = payload.get("collaboration_mode")
    settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
    settings = settings if isinstance(settings, dict) else {}
    model = payload.get("model") or settings.get("model")
    effort = (payload.get("effort") or payload.get("reasoning_effort")
              or settings.get("effort") or settings.get("reasoning_effort"))
    timestamp = record.get("timestamp")
    return {"model": model if isinstance(model, str) and model else None,
            "effort": effort if isinstance(effort, str) and effort else None,
            "contextTokens": None,
            "at": timestamp if isinstance(timestamp, str) and timestamp else None}


def codex_session_status(session_id: str, block_bytes: int = TRANSCRIPT_BLOCK_BYTES) -> dict[str, Any] | None:
    status = _read_session_status(find_codex_transcript(session_id), "codex", block_bytes)
    return status if status and (status["model"] or status["effort"]) else None


def _codex_conversation_event(record: Any, text_limit: int | None = MAX_EVENT_TEXT) -> dict[str, Any] | None:
    if not isinstance(record, dict) or record.get("type") != "response_item":
        return None
    message = record.get("payload")
    if isinstance(message, dict) and message.get("type") in {"function_call", "custom_tool_call"}:
        return {"role": "assistant", "ts": record.get("timestamp"), "text": "", "toolCount": 1}
    if (not isinstance(message, dict) or message.get("type") != "message"
            or message.get("role") not in {"user", "assistant"}
            or message.get("channel") == "analysis"):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    text = "\n".join(block["text"] for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
        and block.get("type") in {None, "text", "input_text", "output_text"})
    text = _CODEX_TEXT_CONTROLS.sub("", text).strip()
    if message["role"] == "user":
        text = _clean_user_text(text)
    if not text:
        return None
    return {"role": message["role"], "ts": record.get("timestamp"),
            "text": text[:text_limit] if text_limit is not None else text, "toolCount": 0}


def codex_transcript_page(
    session_id: str,
    limit: int = TRANSCRIPT_PAGE_LIMIT,
    before: int | None = None,
    block_bytes: int = TRANSCRIPT_BLOCK_BYTES,
    max_scan_bytes: int = TRANSCRIPT_SCAN_BYTES,
) -> dict[str, Any] | None:
    path = find_codex_transcript(session_id)
    if path is None:
        return None
    # Open and seek to the current size on every request. Windows may leave the
    # modification timestamp unchanged while this still-open file grows.
    return _transcript_page_from_path(
        path, limit, before, block_bytes, max_scan_bytes, _codex_conversation_event,
    )


def codex_transcript_page_after(
    session_id: str, after: int, limit: int = TRANSCRIPT_PAGE_LIMIT,
) -> dict[str, Any] | None:
    path = find_codex_transcript(session_id)
    if path is None:
        return None
    return _transcript_page_after_from_path(path, after, limit, _codex_conversation_event)


def resolve_codex_talk_tab(
    session_id: str, listing: Any, launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """The Codex tab a talk id names, by its terminal session id.

    A Codex seat has no Claude session id, so the id the phone and the web
    use for it is the one ``/pocket/api/term/{id}`` uses: the tab's terminal
    session. The same rule serves ``/talk/{id}`` and its ``wait``.
    """
    for tab in _all_tabs(listing):
        # The phone names a Codex seat by its terminal session id; a wait may
        # also arrive keyed by the Codex session id (find_codex_transcript
        # accepts it). Match either, so the pane is found both ways.
        if (_is_readable_terminal_tab(tab)
                and session_id in {tab.get("sessionId"), tab.get("agentSessionId")}
                and tab_agent(tab, launch_kinds_by_tab) == "codex"):
            return tab
    return None


def resolve_wait_source(
    session_id: str,
    bridge: Callable[..., Any] | None = None,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Where a talk's new lines come from, decided once before a wait sleeps.

    A Claude transcript is found by its session id without the bridge. A
    Codex rollout is found by the Codex session id, or - the id the phone
    actually holds - by the terminal session id of the Codex tab, which
    costs one listing.
    """
    path = find_transcript(session_id)
    if path is not None:
        return {
            "agent": "claude", "path": path,
            "page": lambda **kwargs: transcript_page(session_id, **kwargs),
            "status": lambda: transcript_session_status(session_id),
        }
    codex_id: str | None = session_id if find_codex_transcript(session_id) else None
    if codex_id is None and bridge is not None:
        try:
            tab = resolve_codex_talk_tab(session_id, bridge("panes", "--all"), launch_kinds_by_tab)
        except BridgeError:
            return None
        codex_id = tab.get("agentSessionId") if tab is not None else None
    path = find_codex_transcript(codex_id) if isinstance(codex_id, str) else None
    if path is None:
        return None
    return {
        "agent": "codex", "path": path,
        "page": lambda **kwargs: codex_transcript_page(codex_id, **kwargs),
        "status": lambda: codex_session_status(codex_id),
        "agentSessionId": codex_id,
    }


def _growth_stamp(path: Path, agent: str) -> tuple[int, int]:
    """Size and mtime, with the size read from the open file for a rollout.

    Codex keeps its rollout open while it writes, and Windows then reports
    the directory entry's stale size from stat() until the writer flushes
    the metadata. Seeking to the end of a fresh handle sees every byte.
    """
    info = path.stat()
    size = info.st_size
    if agent == "codex":
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
    return size, info.st_mtime_ns

_CODEX_TURN_INDEX: dict[str, dict[str, Any]] = {}
_CODEX_TURN_LOCK = threading.Lock()


def codex_rollout_state(session_id: str) -> dict[str, Any] | None:
    """Index the whole first read, then only complete records appended since it.

    Keep offsets and user-message hashes, not copied conversation bodies. A turn
    can start megabytes before the current transcript page. Partial final JSONL
    records are reread after the writer finishes them.
    """
    path = find_codex_transcript(session_id)
    if path is None:
        return None
    key = str(path)
    try:
        with _CODEX_TURN_LOCK, path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            identity = (info.st_dev, info.st_ino)
            state = _CODEX_TURN_INDEX.get(key)
            if (state is None or state["identity"] != identity or info.st_size < state["size"]
                    or (info.st_size == state["size"] and info.st_mtime_ns != state["mtime"])):
                state = dict(identity=identity, offset=0, size=0, mtime=0,
                             started=None, completed=None, users=[], valid=True)
            handle.seek(state["offset"])
            while handle.tell() < info.st_size:
                offset = handle.tell()
                raw = handle.readline(info.st_size - offset)
                if not raw.endswith(b"\n"):
                    break
                state["offset"] = handle.tell()
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeError):
                    state["valid"] = False
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if record.get("type") == "event_msg" and isinstance(payload, dict):
                    event_type, turn_id = payload.get("type"), payload.get("turn_id")
                    if event_type in {"task_started", "task_complete"} and isinstance(turn_id, str):
                        state["started" if event_type == "task_started" else "completed"] = (turn_id, offset)
                if (record.get("type") == "response_item" and isinstance(payload, dict)
                        and payload.get("role") == "user"):
                    event = _codex_conversation_event(record, text_limit=None)
                    if event is not None:
                        digest = hashlib.sha256(normalize_send_text(event["text"]).strip().encode("utf-8")).hexdigest()
                        state["users"] = [*state["users"][-127:], (offset, digest)]
            state.update(size=info.st_size, mtime=info.st_mtime_ns)
            _CODEX_TURN_INDEX[key] = state
            # Bound closed/inactive seats without losing the active file's offsets.
            if len(_CODEX_TURN_INDEX) > 256:
                oldest = next(iter(_CODEX_TURN_INDEX))
                if oldest != key:
                    _CODEX_TURN_INDEX.pop(oldest)
            started, completed = state["started"], state["completed"]
            idle = bool(state["valid"] and started and completed and started[0] == completed[0]
                        and completed[1] > started[1] and state["offset"] == info.st_size
                        and not any(offset > completed[1] for offset, _ in state["users"]))
            return {**state, "users": list(state["users"]), "idle": idle, "path": key}
    except OSError:
        return None


_BRAILLE_RE = re.compile("[\u2800-\u28ff]")


def codex_composer_holds(payload: Any, session_id: str, text: str) -> bool:
    """True only when the bottom input line still shows this text unsent.

    Background screens interleave braille animation dots with the text, so they
    are removed before comparing. An unreadable screen never counts as a match.
    """
    if not isinstance(payload, dict) or payload.get("sessionId") != session_id:
        return False
    lines = payload.get("lines")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return False
    first = next((line.strip() for line in normalize_send_text(text).splitlines() if line.strip()), "")
    if not first:
        return False
    probe = first[:24]
    for line in reversed([line for line in lines if line.strip()]):
        cleaned = _BRAILLE_RE.sub("", line)
        match = CODEX_PROMPT_LINE_RE.match(cleaned)
        if match:
            rest = cleaned[match.end():].strip()
            return bool(rest) and (rest.startswith(probe) or probe.startswith(rest)
                                   or rest.startswith("[Pasted"))
    return False


def codex_screen_blocks_send(payload: Any, session_id: str) -> bool:
    """The screen can veto a send, but an unreadable screen cannot declare busy."""
    if not isinstance(payload, dict) or payload.get("sessionId") != session_id:
        return True
    lines = payload.get("lines")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return True
    tail = [line for line in lines if line.strip()]
    for index in range(len(tail) - 1, -1, -1):
        match = CODEX_PROMPT_LINE_RE.match(tail[index])
        if match:
            if tail[index][match.end():].strip() not in {"", CODEX_EMPTY_PROMPT_TEXT}:
                return True
            # An empty first line can still belong to a multiline draft. Only
            # known TUI footer rows may follow an empty composer prompt.
            return any(not any(pattern.match(line) for pattern in CODEX_PROMPT_FOLLOWING_LINE_RES)
                       for line in tail[index + 1:])
    return False


def empty_transcript_wait() -> dict[str, Any]:
    return {
        "events": [], "nextBefore": None, "atStart": True,
        "sessionStatus": None, "changed": False,
    }


def wait_for_transcript(
    session_id: str,
    cursor: int | None = None,
    timeout: float = TALK_WAIT_DEFAULT_TIMEOUT,
    source: dict[str, Any] | None = None,
    activity_reader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read once, then reopen only when the log's size or mtime changes.

    Stat before reading so an append racing the read is checked again on the
    next tick. No bridge or file handle is held while this request sleeps.
    ``source`` is what resolve_wait_source found; without one, a Claude
    transcript or a Codex rollout is looked up by the id alone.
    """
    deadline = time.monotonic() + min(timeout, TALK_WAIT_MAX_TIMEOUT)
    if source is None:
        source = resolve_wait_source(session_id)
    if source is None:
        return empty_transcript_wait()
    path, agent, page_of = source["path"], source["agent"], source["page"]
    previous_stamp = None
    previous_activity = None
    activity_changed = False
    response = empty_transcript_wait()
    while True:
        try:
            stamp = _growth_stamp(path, agent)
        except OSError:
            return empty_transcript_wait()
        if stamp != previous_stamp:
            previous_stamp = stamp
            page = page_of()
            if page is None:
                return empty_transcript_wait()
            batches = []
            while True:
                batches.append([
                    event for event in page["events"]
                    if cursor is None or int(event["cursor"]) > cursor
                ])
                # A burst can exceed the usual 50-event page. Walk back to the
                # supplied cursor so advancing it cannot silently skip replies.
                if (
                    cursor is None or page["nextBefore"] is None
                    or int(page["nextBefore"]) <= cursor
                    or (page["events"] and int(page["events"][0]["cursor"]) <= cursor)
                ):
                    break
                page = page_of(before=int(page["nextBefore"]))
                if page is None:
                    return empty_transcript_wait()
            events = [event for batch in reversed(batches) for event in batch]
            response.update(
                events=events, nextBefore=page["nextBefore"],
                atStart=page["atStart"], changed=bool(events),
            )
            if activity_reader is not None:
                latest = activity_reader()
                # A tool result can advance work without a visible conversation
                # row. Wake for that change while keeping "changed" about events.
                current_activity = {key: value for key, value in latest.items() if key != "silentSeconds"}
                activity_changed = previous_activity is not None and current_activity != previous_activity
                previous_activity = current_activity
        remaining = deadline - time.monotonic()
        if response["changed"] or activity_changed or remaining <= 0:
            # The status is a second read of the same log, and it is the last
            # thing a finished wait does. A failure there used to throw away a
            # wait that had already succeeded and answer 500 instead
            # (2026-09-19: two of them on the phone, both at the full timeout).
            try:
                response["sessionStatus"] = source["status"]()
            except (OSError, ValueError, KeyError, BridgeError):
                response["sessionStatus"] = None
            if agent == "codex":
                response["agentSessionId"] = source.get("agentSessionId")
            return response
        time.sleep(min(TALK_WAIT_INTERVAL, remaining))


def _talk_observation(tab: dict[str, Any], bridge: Callable[..., Any] | None) -> dict[str, Any]:
    source = _socket_source(bridge) if bridge is not None else None
    if source is None:
        return tab
    connected, sessions, _ = source.feed.snapshot()
    return _feed_tab(tab, sessions if connected else None)


def talk_pane_state(tab: dict[str, Any]) -> dict[str, Any]:
    """The seat state, including canonical uiState, by the snapshot rules."""
    return {
        "status": _tab_status(tab),
        "uiState": tab.get("uiState"),
        "canSend": True,
        "screenObserved": tab.get("screenObserved") if "screenObserved" in tab else None,
        "agentStatusStale": tab.get("agentStatusStale") if "agentStatusStale" in tab else None,
    }


def wait_pane_view(
    session_id: str, listing: Any, launch_kinds_by_tab: dict[str, str] | None = None,
    bridge: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """One listing, read after the wait returns: the Claude tab of the talk,
    or the Codex tab its terminal id names. None when the seat is gone."""
    tab = _matching_tab(listing, session_id)
    if tab is None:
        tab = resolve_codex_talk_tab(session_id, listing, launch_kinds_by_tab)
    return talk_pane_state(_talk_observation(tab, bridge)) if tab is not None else None


def transcript_events(session_id: str, limit: int = 50) -> list[dict[str, Any]] | None:
    page = transcript_page(session_id, limit=min(limit, TRANSCRIPT_PAGE_LIMIT))
    return None if page is None else page["events"]


def _ai_title(record: Any) -> str:
    if not isinstance(record, dict) or record.get("type") not in {"ai-title", "custom-title"}:
        return ""
    for key in ("customTitle", "aiTitle", "title", "text", "value", "content"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            joined = "\n".join(parts).strip()
            if joined:
                return joined
    return ""


# A custom title outranks a later AI title. Remember the custom-title result
# separately so concurrent title consumers need not rescan its absence. Like
# the title parts, this observation is valid only for an unchanged size/mtime.
_CUSTOM_TITLE_OBSERVATIONS: OrderedDict[tuple[str, int, int], str] = OrderedDict()
_CUSTOM_TITLE_GUARD = threading.Lock()


def _remember_custom_title(key: tuple[str, int, int], title: str) -> None:
    with _CUSTOM_TITLE_GUARD:
        _CUSTOM_TITLE_OBSERVATIONS.pop(key, None)
        _CUSTOM_TITLE_OBSERVATIONS[key] = title
        while len(_CUSTOM_TITLE_OBSERVATIONS) > 256:
            _CUSTOM_TITLE_OBSERVATIONS.popitem(last=False)


@lru_cache(maxsize=256)
def _cached_transcript_title_parts(
    path_value: str,
    file_size: int,
    modified_ns: int,
) -> tuple[str, str]:
    observation_key = (path_value, file_size, modified_ns)
    with _CUSTOM_TITLE_GUARD:
        known_custom = _CUSTOM_TITLE_OBSERVATIONS.get(observation_key)
    path = Path(path_value)
    latest_ai_title = ""
    first_user_text = ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            handle.seek(0)
            head = handle.read(TRANSCRIPT_BLOCK_BYTES)
            if len(head) < position:
                # Only complete records within the first block belong to the head.
                head = head.rpartition(b"\n")[0]
            for raw_line in head.splitlines():
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not first_user_text and isinstance(record, dict) and record.get("type") == "user":
                    message = record.get("message")
                    if isinstance(message, dict):
                        first_user_text = _user_text(record, message.get("content"))
                        if first_user_text:
                            break

            if known_custom:
                return known_custom, first_user_text
            lower_bound = max(0, position - 4 * 1024 * 1024)
            carry = b""
            while position > lower_bound:
                next_start = max(lower_bound, position - TRANSCRIPT_BLOCK_BYTES)
                handle.seek(next_start)
                payload = handle.read(position - next_start) + carry
                position = next_start
                if next_start > 0:
                    newline = payload.find(b"\n")
                    if newline < 0:
                        carry = payload
                        continue
                    carry = payload[:newline]
                    payload = payload[newline + 1:]
                marker = b'"custom-title"' if latest_ai_title else b'-title"'
                if marker not in payload:
                    continue
                for raw_line in reversed(payload.splitlines()):
                    if b'-title"' not in raw_line:
                        continue
                    try:
                        record = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    title = _ai_title(record)
                    if title and record.get("type") == "custom-title":
                        _remember_custom_title(observation_key, title)
                        return title, first_user_text
                    if title and not latest_ai_title:
                        latest_ai_title = title
                        if known_custom is not None:
                            return title, first_user_text
    except OSError:
        return "", ""
    _remember_custom_title(observation_key, "")
    return latest_ai_title, first_user_text


def transcript_title_parts(session_id: str) -> tuple[str, str]:
    path = find_transcript(session_id)
    if path is None:
        return "", ""
    try:
        metadata = path.stat()
    except OSError:
        return "", ""
    return _cached_transcript_title_parts(
        os.fspath(path),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def shortened_session_id(session_id: str) -> str:
    if len(session_id) <= 15:
        return session_id
    return f"{session_id[:8]}...{session_id[-4:]}"


def resolve_talk_title(tab: dict[str, Any], session_id: str) -> str:
    for key in ("display_name", "displayName", "label", "lastProcess", "agentKind"):
        value = tab.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return shortened_session_id(str(tab.get("sessionId") or session_id or tab.get("id") or ""))


def _talk_subtitle(session_id: str) -> str:
    ai_title, first_user = transcript_title_parts(session_id)
    return plain_snippet(ai_title, limit=80) if ai_title else plain_snippet(first_user, limit=24)


def plain_snippet(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*#>|]", " ", text)
    text = re.sub(r"\[|\]", " ", text)
    text = re.sub(r"(?:^|\s)-{3,}(?:\s|$)", " ", text)
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    # A snippet that stopped in the middle of a path became a file link on the
    # phone, and the phone then asked the server for a file nobody had named -
    # "C:\\Users\\exam", 403, on every visit to that workspace. A half path is
    # not worth its last characters, so the whole token goes.
    start = cut.rfind(" ") + 1
    if re.match(r"[A-Za-z]:(?:[\\/]|$)", cut[start:]):
        cut = cut[:start]
    return cut.rstrip()



# A seat's first instruction is immutable. Scan forward in chunks until it is
# found, including past injected startup context. Cache nonempty topics; empty
# transcripts are retried only after growth. Never load the whole log at once.
_TRANSCRIPT_TOPICS: dict[tuple[str, str], tuple[int, str]] = {}
_TRANSCRIPT_TOPICS_GUARD = threading.Lock()


def _session_topic_instruction(record: Any, kind: str, *, raw: bool = False) -> str | None:
    """None skips a machine record; an empty string keeps an empty first topic."""
    if kind == "claude":
        if not isinstance(record, dict) or record.get("type") != "user":
            return None
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        raw_text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            raw_text = "\n".join(
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
        # Check the original prefix before _user_text removes system reminders.
        if raw_text.lstrip().startswith((
            "<command-name>", "<local-command", "<system-reminder>",
            "Caveat: The messages below", "[Request interrupted",
        )):
            return None
        text = _user_text(record, content)
    else:
        event = _codex_conversation_event(record)
        if not event or event["role"] != "user":
            return None
        text = event["text"]
        if re.match(
            r"(?:#+\s*)?AGENTS\.md instructions|<INSTRUCTIONS|<environment_context>|# Codex",
            text,
        ):
            return None
    if not text:
        return None
    text = re.sub(r"</?pasted_content\b[^>]*>", "", text).strip()
    text = re.sub(r"^(?:(?:\[Image\s+#?\d+\]|Image\s+#?\d+\b)\s*)+", "", text).strip()
    if raw:
        return text
    handoff = re.match(r'^Handoff from previous session\.\s+Read\s+"([^"]+)"', text)
    request = re.match(r"^Read\s+((?:[A-Za-z]:[\\/]|/|\\\\)[^\s]+)", text)
    if handoff or request:
        path = (handoff or request).group(1)
        parts = [part for part in re.split(r"[\\/]+", path) if part]
        tail = "/".join(parts[-2:])
        prefix = "\u5f15\u304d\u7d99\u304e" if handoff else "\u4f9d\u983c\u66f8"
        text = prefix + ": " + tail
    return plain_snippet(text, limit=80)


def _topic_lines(handle: Any, size: int):
    """Complete lines in a bounded file prefix, retaining only one record."""
    pending = []
    remaining = size
    while remaining:
        block = handle.read(min(TRANSCRIPT_BLOCK_BYTES, remaining))
        if not block:
            break
        remaining -= len(block)
        parts = block.split(b"\n")
        if len(parts) == 1:
            pending.append(block)
            continue
        yield b"".join((*pending, parts[0]))
        pending = []
        yield from parts[1:-1]
        if parts[-1]:
            pending.append(parts[-1])
    if pending:
        yield b"".join(pending)


def session_topic(tab: dict[str, Any], agent: str, *, cache_absence: bool = False) -> str:
    session_id = tab.get("claudeSessionId")
    if session_id:
        path = (find_transcript(session_id, cache_absence=True) if cache_absence
                else find_transcript(session_id))
        kind = "claude"
    elif agent == "codex":
        session_id = _pane_session_fields(tab).get("agentSessionId") or tab.get("agentSessionId")
        if session_id:
            path = (find_codex_transcript(session_id, cache_absence=True) if cache_absence
                    else find_codex_transcript(session_id))
        else:
            path = None
        kind = "codex"
    else:
        return ""
    if path is None:
        return ""
    key = (os.fspath(path), kind)
    with _TRANSCRIPT_TOPICS_GUARD:
        cached = _TRANSCRIPT_TOPICS.get(key)
        if cached is not None and cached[1]:
            return cached[1]
        try:
            size = path.stat().st_size
            if cached is not None and cached[0] == size:
                return ""
            topic = ""
            with path.open("rb") as handle:
                for raw_line in _topic_lines(handle, size):
                    try:
                        record = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    instruction = _session_topic_instruction(record, kind)
                    if instruction is not None:
                        topic = instruction
                        break
        except OSError:
            return ""
        _TRANSCRIPT_TOPICS[key] = (size, topic)
        return topic


_FIRST_REQUEST_PREFIXES: dict[tuple[str, str], tuple[int, str, str]] = {}
_FIRST_REQUEST_PREFIXES_GUARD = threading.Lock()


def _prefix_digest(handle: Any, size: int) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = handle.read(min(TRANSCRIPT_BLOCK_BYTES, remaining))
        if not block:
            return ""
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()


@lru_cache(maxsize=256)
def _cached_session_first_request(path_value: str, agent: str, size: int, mtime: int) -> str:
    del mtime
    key = (path_value, agent)
    with _FIRST_REQUEST_PREFIXES_GUARD:
        cached = _FIRST_REQUEST_PREFIXES.get(key)
    try:
        with Path(path_value).open("rb") as handle:
            if cached and size >= cached[0] and _prefix_digest(handle, cached[0]) == cached[1]:
                return cached[2]
            handle.seek(0)
            for line in _topic_lines(handle, min(size, seat_activity.SCAN_BYTES)):
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                text = _session_topic_instruction(record, agent, raw=True)
                if text is not None:
                    result = text[:len(seat_activity.JOB_PREFIX)]
                    prefix_size = handle.tell()
                    # An incomplete final line can be invalidated by an append.
                    complete = prefix_size < size
                    if not complete and prefix_size:
                        handle.seek(prefix_size - 1)
                        complete = handle.read(1) == b"\n"
                    if complete:
                        digest = _prefix_digest(handle, prefix_size)
                        with _FIRST_REQUEST_PREFIXES_GUARD:
                            if len(_FIRST_REQUEST_PREFIXES) >= 256:
                                _FIRST_REQUEST_PREFIXES.pop(next(iter(_FIRST_REQUEST_PREFIXES)))
                            _FIRST_REQUEST_PREFIXES[key] = (prefix_size, digest, result)
                    return result
    except OSError:
        pass
    with _FIRST_REQUEST_PREFIXES_GUARD:
        _FIRST_REQUEST_PREFIXES.pop(key, None)
    return ""


def session_first_request(tab: dict[str, Any]) -> str:
    target = seat_activity.line_target(tab)
    if target is None:
        return ""
    _, session, agent = target
    path = find_codex_transcript(session) if agent == "codex" else find_transcript(session)
    if path is None:
        return ""
    try:
        info = path.stat()
        return _cached_session_first_request(str(path), agent, info.st_size, info.st_mtime_ns)
    except OSError:
        return ""


def mail_source_suspicious(row: dict[str, Any]) -> bool:
    convo = row.get("convo", [])
    if not isinstance(convo, list):
        return True
    material = "\n".join(
        str(item.get("body") or "")
        for item in convo
        if isinstance(item, dict) and not bool(item.get("own"))
    )
    return bool(
        re.search(
            r"(?i)(ignore (?:all |any )?(?:previous|prior) (?:rules|instructions)|"
            r"system prompt|developer message|internal standards|reveal .{0,24}(?:rules|prompt))",
            material,
        )
    )


def mail_unreplied_count() -> int:
    try:
        payload = json.loads(MAIL_SNAPSHOT.read_text(encoding="utf-8"))
        count = payload["counts"]["pending"]
        if isinstance(count, bool):
            return 0
        return max(0, int(count))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def validate_mail_direction(value: Any) -> str | None:
    if not isinstance(value, str):
        return "direction must be a string"
    if len(value) > MAIL_DIRECTION_MAX:
        return f"direction must be at most {MAIL_DIRECTION_MAX} characters"
    if "\r" in value or "\n" in value:
        return "direction must be one line"
    return None


def validate_mail_instruction(value: Any) -> str | None:
    if not isinstance(value, str):
        return "instruction must be a string"
    if not value.strip():
        return "instruction is required"
    if len(value) > MAIL_INSTRUCTION_MAX:
        return f"instruction must be at most {MAIL_INSTRUCTION_MAX} characters"
    return None


class MailSnapshotStore:
    def __init__(self, path: Path = MAIL_SNAPSHOT) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime_ns: int | None = None
        self._payload: dict[str, Any] | None = None

    def _read(self) -> dict[str, Any]:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError as exc:
            raise MailError(503, "mail snapshot unavailable") from exc
        with self._lock:
            if self._payload is not None and self._mtime_ns == mtime_ns:
                return self._payload
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MailError(503, "mail snapshot invalid") from exc
            if not isinstance(payload, dict):
                raise MailError(503, "mail snapshot invalid")
            self._payload = payload
            self._mtime_ns = mtime_ns
            return payload

    def pending(self) -> list[dict[str, Any]]:
        payload = self._read()
        rows = payload.get("pending")
        if not isinstance(rows, list):
            rows = payload.get("balls", [])
        if not isinstance(rows, list):
            raise MailError(503, "mail snapshot invalid")
        return [
            row
            for row in rows
            if isinstance(row, dict) and row.get("state", "pending") == "pending"
        ]

    def generation(self) -> int:
        self._read()
        if self._mtime_ns is None:
            raise MailError(503, "mail snapshot unavailable")
        return self._mtime_ns

    def thread(self, thread_id: str) -> dict[str, Any]:
        row = next(
            (item for item in self.pending() if item.get("thread_id") == thread_id),
            None,
        )
        if row is None:
            raise MailError(404, "mail thread not found")
        return row

    @staticmethod
    def _latest_incoming(row: dict[str, Any]) -> dict[str, Any]:
        convo = row.get("convo", [])
        if not isinstance(convo, list):
            return {}
        return next(
            (
                item
                for item in reversed(convo)
                if isinstance(item, dict) and not bool(item.get("own"))
            ),
            {},
        )

    def list_payload(self) -> list[dict[str, Any]]:
        output = []
        for row in self.pending():
            latest = self._latest_incoming(row)
            body = plain_snippet(latest.get("body", ""), limit=180)
            output.append(
                {
                    "threadId": str(row.get("thread_id", "")),
                    "from": str(row.get("who") or row.get("from") or ""),
                    "subject": str(row.get("subject") or ""),
                    "ageDays": float(row.get("age_days") or 0),
                    "urgent": bool(
                        row.get("urgent")
                        or row.get("vip")
                        or row.get("priority") == "high"
                    ),
                    "hasDraft": bool(row.get("has_draft")),
                    "snippet": body,
                    "sourceWarning": mail_source_suspicious(row),
                    "threadingDegraded": not bool(_mail_message_id(row, latest)),
                    "lastMsgId": _mail_last_message_id(row, latest),
                }
            )
        output.sort(key=lambda item: (not item["urgent"], -item["ageDays"]))
        return output

    def thread_payload(self, thread_id: str) -> list[dict[str, Any]]:
        row = self.thread(thread_id)
        convo = row.get("convo", [])
        if not isinstance(convo, list):
            raise MailError(503, "mail snapshot invalid")
        return [
            {
                "who": str(item.get("who") or ""),
                "ts": str(item.get("ts") or item.get("date_jst") or ""),
                "direction": "out" if bool(item.get("own")) else "in",
                "body": str(item.get("body") or ""),
            }
            for item in convo
            if isinstance(item, dict)
        ]


def _mail_header_value(value: Any, name: str) -> str:
    if isinstance(value, dict):
        result = value.get(name) or value.get(name.lower())
        return str(result or "")
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            if str(item.get("name", "")).lower() == name.lower():
                return str(item.get("value") or "")
    return ""


def _mail_message_id(row: dict[str, Any], latest: dict[str, Any]) -> str:
    candidates = (
        latest.get("rfc_message_id"),
        _mail_header_value(latest.get("headers"), "Message-ID"),
        row.get("rfc_message_id"),
        latest.get("message_id"),
        latest.get("messageId"),
        row.get("message_id"),
    )
    value = next((str(item).strip() for item in candidates if item), "")
    if value and not (value.startswith("<") and value.endswith(">")):
        value = f"<{value}>"
    return value


def _mail_last_message_id(row: dict[str, Any], latest: dict[str, Any]) -> str:
    candidates = (
        row.get("last_msg_id"),
        row.get("lastMsgId"),
        latest.get("id"),
        latest.get("msg_id"),
        latest.get("gmail_message_id"),
        latest.get("message_id"),
    )
    return next((str(item).strip() for item in candidates if item), "")


def _mail_references(row: dict[str, Any], latest: dict[str, Any], message_id: str) -> str:
    value = (
        latest.get("references")
        or _mail_header_value(latest.get("headers"), "References")
        or row.get("references")
        or ""
    )
    references = " ".join(str(value).split())
    if message_id and message_id not in references:
        references = f"{references} {message_id}".strip()
    return references


def build_reply_mime(row: dict[str, Any], body: str) -> bytes:
    latest = MailSnapshotStore._latest_incoming(row)
    recipient = str(
        latest.get("from")
        or latest.get("email")
        or row.get("from")
        or ""
    ).strip()
    source_subject = str(row.get("subject") or "").strip()
    subject = source_subject
    if not re.match(r"^re\s*:", subject, flags=re.IGNORECASE):
        subject = f"Re: {subject}"
    message_id = _mail_message_id(row, latest)
    references = _mail_references(row, latest, message_id)
    if not recipient or not source_subject:
        raise MailError(422, "reply headers unavailable")
    message = EmailMessage(policy=SMTP)
    message["To"] = recipient
    message["Subject"] = subject
    if message_id:
        message["In-Reply-To"] = message_id
        message["References"] = references
    message.set_content(body, subtype="plain", charset="utf-8")
    return message.as_bytes(policy=SMTP)


def render_mail_prompt(
    row: dict[str, Any], direction: str
) -> tuple[str, dict[str, Any]]:
    template = MAIL_PROMPT.read_text(encoding="utf-8")
    standards = MAIL_STANDARDS.read_text(encoding="utf-8")
    convo = row.get("convo", [])
    if not isinstance(convo, list):
        raise MailError(500, "mail conversation invalid")
    messages = [
        {
            "who": str(item.get("who") or ""),
            "at": str(item.get("ts") or item.get("date_jst") or ""),
            "own": bool(item.get("own")),
            "body": str(item.get("body") or ""),
        }
        for item in convo
        if isinstance(item, dict)
    ]
    for name in ("STANDARDS", "DIRECTION", "THREAD"):
        if template.count("{{" + name + "}}") != 1:
            raise MailError(500, "mail prompt template invalid")

    def render(selected: list[dict[str, Any]]) -> str:
        thread_text = json.dumps(
            selected,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        values = {
            "STANDARDS": standards,
            "DIRECTION": direction,
            "THREAD": thread_text,
        }
        return re.sub(
            r"\{\{(STANDARDS|DIRECTION|THREAD)\}\}",
            lambda match: values[match.group(1)],
            template,
        )

    if not messages:
        prompt = render([])
        if len(prompt) > MAIL_PROMPT_MAX_CHARS:
            raise MailError(413, "mail prompt exceeds budget")
        return prompt, {"omittedMessages": 0, "bodyTruncated": False}

    for selected_count in range(len(messages), 0, -1):
        prompt = render(messages[-selected_count:])
        if len(prompt) <= MAIL_PROMPT_MAX_CHARS:
            return prompt, {
                "omittedMessages": len(messages) - selected_count,
                "bodyTruncated": False,
            }

    latest = dict(messages[-1])
    body = latest["body"]
    marker = "[...truncated...]"
    retained_chars = max(len(body) - 500, 0)
    while True:
        latest["body"] = body[:retained_chars] + marker
        prompt = render([latest])
        if len(prompt) <= MAIL_PROMPT_MAX_CHARS:
            return prompt, {
                "omittedMessages": len(messages) - 1,
                "bodyTruncated": True,
            }
        if retained_chars == 0:
            break
        retained_chars = max(retained_chars - 500, 0)
    raise MailError(413, "mail prompt exceeds budget")


def render_mail_revise_prompt(
    current_draft: str,
    instruction: str,
    suffix: str = "",
) -> tuple[str, dict[str, Any]]:
    template = MAIL_REVISE_PROMPT.read_text(encoding="utf-8")
    standards = MAIL_STANDARDS.read_text(encoding="utf-8")
    error = validate_mail_instruction(instruction)
    if error:
        raise MailError(400, error)
    for name in ("CURRENT_DRAFT", "INSTRUCTION", "STANDARDS"):
        if template.count("{{" + name + "}}") != 1:
            raise MailError(500, "mail revise prompt template invalid")

    def render(draft_text: str) -> str:
        values = {
            "CURRENT_DRAFT": draft_text,
            "INSTRUCTION": instruction,
            "STANDARDS": standards,
        }
        return re.sub(
            r"\{\{(CURRENT_DRAFT|INSTRUCTION|STANDARDS)\}\}",
            lambda match: values[match.group(1)],
            template,
        ) + suffix

    prompt = render(current_draft)
    if len(prompt) <= MAIL_PROMPT_MAX_CHARS:
        return prompt, {"omittedMessages": 0, "bodyTruncated": False}

    marker = "[...truncated...]"
    retained_chars = max(len(current_draft) - 500, 0)
    while True:
        prompt = render(current_draft[:retained_chars] + marker)
        if len(prompt) <= MAIL_PROMPT_MAX_CHARS:
            return prompt, {"omittedMessages": 0, "bodyTruncated": True}
        if retained_chars == 0:
            break
        retained_chars = max(retained_chars - 500, 0)
    raise MailError(413, "mail revise prompt exceeds budget")


def _run_hidden(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        **kwargs,
    )


def run_mail_claude(prompt: str) -> str:
    try:
        completed = _run_hidden(
            ["claude", "-p", "--output-format", "text", "--tools", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            cwd=ROOT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MailError(500, "Claude draft failed") from exc
    draft = completed.stdout.strip()
    if completed.returncode != 0 or not draft:
        raise MailError(500, "Claude draft failed")
    return draft


def check_mail_draft(draft: str) -> tuple[bool, list[str]]:
    with tempfile.TemporaryDirectory(prefix="pocket-mail-") as temp_dir:
        path = Path(temp_dir) / "draft.txt"
        path.write_text(draft, encoding="utf-8")
        try:
            completed = _run_hidden(
                [sys.executable, os.fspath(MAIL_CHECKER), os.fspath(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MailError(500, "mail checker failed") from exc
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    lines = [line for line in output.splitlines() if line.strip()]
    if completed.returncode == 0:
        return True, []
    if completed.returncode == 1:
        return False, lines
    raise MailError(500, "mail checker failed", lines)


def create_gmail_draft(row: dict[str, Any], draft: str) -> str:
    raw = base64.urlsafe_b64encode(build_reply_mime(row, draft)).decode("ascii").rstrip("=")
    request_body = json.dumps(
        {"message": {"raw": raw, "threadId": str(row.get("thread_id") or "")}},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    request_params = json.dumps(
        {"userId": "me"},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        completed = _run_hidden(
            [
                os.fspath(GWS_CLI),
                "gmail",
                "users",
                "drafts",
                "create",
                "--params",
                request_params,
                "--json",
                request_body,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MailError(500, "Gmail draft creation failed") from exc
    if completed.returncode != 0:
        raise MailError(500, "Gmail draft creation failed")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MailError(500, "Gmail draft response invalid") from exc
    draft_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(draft_id, str) or not draft_id:
        raise MailError(500, "Gmail draft response invalid")
    return draft_id


def update_gmail_draft(row: dict[str, Any], draft_id: str, draft: str) -> str:
    raw = base64.urlsafe_b64encode(build_reply_mime(row, draft)).decode("ascii").rstrip("=")
    request_body = json.dumps(
        {"message": {"raw": raw, "threadId": str(row.get("thread_id") or "")}},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    request_params = json.dumps(
        {"userId": "me", "id": draft_id},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        completed = _run_hidden(
            [
                os.fspath(GWS_CLI),
                "gmail",
                "users",
                "drafts",
                "update",
                "--params",
                request_params,
                "--json",
                request_body,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MailError(500, "Gmail draft update failed") from exc
    if completed.returncode != 0:
        raise MailError(500, "Gmail draft update failed")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MailError(500, "Gmail draft update response invalid") from exc
    response_id = response.get("id") if isinstance(response, dict) else None
    if response_id != draft_id:
        raise MailError(500, "Gmail draft update response invalid")
    return draft_id


def _gmail_error_text(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        str(value or "").strip()
        for value in (completed.stdout, completed.stderr)
        if str(value or "").strip()
    ).lower()


def _gmail_plain_body(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("mimeType") or "").lower() == "text/plain":
        body = payload.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, str):
            try:
                padded = (data + "=" * (-len(data) % 4)).encode("ascii")
                decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
                return decoded.decode("utf-8")
            except (
                ValueError,
                UnicodeDecodeError,
                UnicodeEncodeError,
                binascii.Error,
            ):
                raise MailError(502, "mailDraftResponseInvalid")
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            result = _gmail_plain_body(part)
            if result is not None:
                return result
    return None


def get_gmail_draft(draft_id: str) -> dict[str, str]:
    request_params = json.dumps(
        {"userId": "me", "id": draft_id, "format": "full"},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        completed = _run_hidden(
            [
                os.fspath(GWS_CLI),
                "gmail",
                "users",
                "drafts",
                "get",
                "--params",
                request_params,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MailError(502, "mailDraftFetchFailed") from exc
    if completed.returncode != 0:
        detail = _gmail_error_text(completed)
        if "404" in detail or "not found" in detail:
            raise MailError(404, "mailDraftNotFound")
        raise MailError(502, "mailDraftFetchFailed")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MailError(502, "mailDraftResponseInvalid") from exc
    message = response.get("message") if isinstance(response, dict) else None
    payload = message.get("payload") if isinstance(message, dict) else None
    if not isinstance(payload, dict):
        raise MailError(502, "mailDraftResponseInvalid")
    body = _gmail_plain_body(payload)
    if body is None:
        raise MailError(502, "mailDraftResponseInvalid")
    headers = payload.get("headers")
    return {
        "body": body,
        "to": _mail_header_value(headers, "To"),
        "subject": _mail_header_value(headers, "Subject"),
    }


def send_gmail_draft(draft_id: str) -> None:
    request_params = json.dumps(
        {"userId": "me"},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    request_body = json.dumps(
        {"id": draft_id},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        completed = _run_hidden(
            [
                os.fspath(GWS_CLI),
                "gmail",
                "users",
                "drafts",
                "send",
                "--params",
                request_params,
                "--json",
                request_body,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MailError(502, "mailSendFailed") from exc
    if completed.returncode != 0:
        raise MailError(502, "mailSendFailed")


def process_mail_draft(
    row: dict[str, Any],
    direction: str,
    update: Callable[[str], None],
) -> tuple[str, dict[str, Any]]:
    prompt, meta = render_mail_prompt(row, direction)
    draft = run_mail_claude(prompt)
    update("checking")
    passed, findings = check_mail_draft(draft)
    if not passed:
        update("drafting")
        retry_prompt = (
            prompt
            + "\n\n## Mechanical check findings for the previous draft\n"
            + "\n".join(findings)
            + "\n\nRevise once and output only the complete replacement body."
        )
        draft = run_mail_claude(retry_prompt)
        update("checking")
        passed, findings = check_mail_draft(draft)
        if not passed:
            raise MailError(500, "mail check failed after retry", findings)
    return create_gmail_draft(row, draft), meta


def process_mail_revise(
    row: dict[str, Any],
    draft_id: str,
    instruction: str,
    update: Callable[[str], None],
) -> tuple[str, dict[str, Any]]:
    current = get_gmail_draft(draft_id)
    current_body = current["body"]
    prompt, meta = render_mail_revise_prompt(current_body, instruction)
    draft = run_mail_claude(prompt)
    update("checking")
    passed, findings = check_mail_draft(draft)
    if not passed:
        update("drafting")
        retry_suffix = (
            "\n\n## Mechanical check findings for the previous draft\n"
            + "\n".join(findings)
            + "\n\nRevise once and output only the complete replacement body."
        )
        retry_prompt, retry_meta = render_mail_revise_prompt(
            current_body,
            instruction,
            retry_suffix,
        )
        if retry_meta["bodyTruncated"]:
            meta["bodyTruncated"] = True
        draft = run_mail_claude(retry_prompt)
        update("checking")
        passed, findings = check_mail_draft(draft)
        if not passed:
            raise MailError(500, "mail check failed after retry", findings)
    return update_gmail_draft(row, draft_id, draft), meta


class MailJobService:
    def __init__(
        self,
        snapshot: MailSnapshotStore | None = None,
        state_path: Path = MAIL_JOBS_PATH,
        hidden_path: Path | None = None,
        sends_path: Path | None = None,
        processor: Callable[
            [dict[str, Any], str, Callable[[str], None]], Any
        ] = process_mail_draft,
        start_worker: bool = True,
        revise_processor: Callable[
            [dict[str, Any], str, str, Callable[[str], None]], Any
        ] | None = None,
    ) -> None:
        self.snapshot = snapshot or MailSnapshotStore()
        self.state_path = state_path
        self.hidden_path = hidden_path or state_path.parent / "mail_hidden.json"
        self.sends_path = sends_path or state_path.parent / "mail_sends.json"
        self.processor = processor
        self.revise_processor = revise_processor or process_mail_revise
        self._machine_locks: list[MachineFileLock] = []
        self._lock = threading.Lock()
        self._sending_drafts: set[str] = set()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        try:
            for path in (self.state_path, self.hidden_path, self.sends_path):
                self._machine_locks.append(
                    MachineFileLock(path.parent / f".{path.name}.lock")
                )
            self._jobs = self._load()
            self._hidden = self._load_records(
                self.hidden_path,
                "hidden",
                required=("threadId", "hiddenAt", "last_msg_id"),
            )
            if any(
                "permanent" in record
                and not isinstance(record.get("permanent"), bool)
                for record in self._hidden
            ):
                raise RuntimeError("hidden state is invalid")
            self._sends = self._load_records(
                self.sends_path,
                "sends",
                required=("threadId", "draftId", "sentAt"),
            )
            self._recover_sent_jobs()
            queued_ids = self._recover_loaded()
        except BaseException:
            for machine_lock in reversed(self._machine_locks):
                machine_lock.close()
            raise
        self._thread: threading.Thread | None = None
        for job_id in queued_ids:
            self._queue.put(job_id)
        if start_worker:
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="pocket-mail-drafts",
                daemon=True,
            )
            self._thread.start()

    def _load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("mail jobs state is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise RuntimeError("mail jobs state is invalid")
        jobs = payload["jobs"]
        if not all(
            isinstance(job, dict)
            and isinstance(job.get("jobId"), str)
            and isinstance(job.get("threadId"), str)
            and job.get("status") in MAIL_STATUSES
            for job in jobs
        ):
            raise RuntimeError("mail jobs state is invalid")
        return jobs

    @staticmethod
    def _load_records(
        path: Path,
        key: str,
        required: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{key} state is invalid") from exc
        rows = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not all(
            isinstance(row, dict)
            and all(isinstance(row.get(name), str) for name in required)
            for row in rows
        ):
            raise RuntimeError(f"{key} state is invalid")
        return rows

    def _recover_loaded(self) -> list[str]:
        queued_ids = []
        changed = False
        for job in self._jobs:
            status = job.get("status")
            if status == "queued":
                queued_ids.append(str(job["jobId"]))
            elif status in {"drafting", "checking"}:
                job["status"] = "failed"
                job["reason"] = "mail draft interrupted; external result uncertain"
                job.setdefault("history", []).append(
                    {"status": "failed", "at": _utc_now(), "recovery": True}
                )
                changed = True
        if changed:
            self._write_locked()
        return queued_ids

    def _recover_sent_jobs(self) -> None:
        sent_at_by_draft = {
            record["draftId"]: record["sentAt"] for record in self._sends
        }
        changed = False
        for job in self._jobs:
            sent_at = sent_at_by_draft.get(str(job.get("draftId") or ""))
            if sent_at and job.get("sentAt") != sent_at:
                job["sentAt"] = sent_at
                changed = True
        if changed:
            self._write_locked()

    def _write_locked(self) -> None:
        self._write_records_locked(self.state_path, "jobs", self._jobs)

    @staticmethod
    def _write_records_locked(
        path: Path,
        key: str,
        rows: list[dict[str, Any]],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(
                json.dumps({key: rows}, ensure_ascii=True, indent=2) + "\n",
                encoding="ascii",
                newline="\n",
            )
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "jobId",
            "threadId",
            "type",
            "status",
            "reason",
            "checkFindings",
            "draftId",
            "history",
            "truncated",
            "omittedMessages",
            "threadingDegraded",
            "sentAt",
        )
        return {key: job[key] for key in keys if key in job}

    def enqueue(self, thread_id: str, direction: str) -> dict[str, Any]:
        self.snapshot.thread(thread_id)
        error = validate_mail_direction(direction)
        if error:
            raise MailError(400, error)
        with self._lock:
            if any(
                job.get("threadId") == thread_id
                and job.get("status") in MAIL_ACTIVE_STATUSES
                for job in self._jobs
            ):
                raise MailError(409, "mail draft already running")
            now = _utc_now()
            job = {
                "jobId": uuid.uuid4().hex,
                "threadId": thread_id,
                "type": "draft",
                "direction": direction,
                "status": "queued",
                "snapshotGeneration": self.snapshot.generation(),
                "history": [{"status": "queued", "at": now}],
            }
            self._jobs.append(job)
            self._write_locked()
            job_id = job["jobId"]
        self._queue.put(job_id)
        return {"jobId": job_id}

    def enqueue_revise(
        self,
        thread_id: str,
        draft_id: str,
        instruction: str,
    ) -> dict[str, Any]:
        self.snapshot.thread(thread_id)
        error = validate_mail_instruction(instruction)
        if error:
            raise MailError(400, error)
        with self._lock:
            latest = self._latest_done_job_locked(thread_id)
            if latest is None or latest.get("draftId") != draft_id:
                raise MailError(404, "mailDraftNotFound")
            if latest.get("sentAt"):
                raise MailError(409, "mailDraftAlreadySent")
            if draft_id in self._sending_drafts:
                raise MailError(409, "mailDraftAlreadySending")
            if any(
                job.get("threadId") == thread_id
                and job.get("status") in MAIL_ACTIVE_STATUSES
                for job in self._jobs
            ):
                raise MailError(409, "mail draft already running")
            now = _utc_now()
            job = {
                "jobId": uuid.uuid4().hex,
                "threadId": thread_id,
                "type": "revise",
                "draftId": draft_id,
                "instruction": instruction,
                "status": "queued",
                "snapshotGeneration": self.snapshot.generation(),
                "history": [{"status": "queued", "at": now}],
            }
            self._jobs.append(job)
            self._write_locked()
            job_id = job["jobId"]
        self._queue.put(job_id)
        return {"jobId": job_id}

    def list_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._public(dict(job))
                for job in reversed(self._jobs)
                if job.get("threadId") == thread_id
            ]

    def _reconcile_hidden_locked(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        by_thread = {row["threadId"]: row for row in rows}
        active_hidden: list[dict[str, Any]] = []
        changed = False
        for record in self._hidden:
            row = by_thread.get(record["threadId"])
            if row is None:
                active_hidden.append(record)
                continue
            if (
                record.get("permanent") is not True
                and row.get("lastMsgId", "") != record.get("last_msg_id", "")
            ):
                changed = True
                continue
            active_hidden.append(record)
        if changed:
            self._hidden = active_hidden
            self._write_records_locked(self.hidden_path, "hidden", self._hidden)
        hidden_ids = {record["threadId"] for record in active_hidden}
        recoverable_ids = {
            record["threadId"]
            for record in active_hidden
            if record.get("permanent") is not True
        }
        visible = [row for row in rows if row["threadId"] not in hidden_ids]
        hidden_rows = [row for row in rows if row["threadId"] in recoverable_ids]
        return visible, hidden_rows

    def _snapshot_rows_with_drafts(self) -> list[dict[str, Any]]:
        rows = self.snapshot.list_payload()
        generation = self.snapshot.generation()
        drafted = {
            str(job.get("threadId"))
            for job in self._jobs
            if job.get("status") == "done"
            and job.get("completedSnapshotGeneration") == generation
        }
        active = {
            str(job.get("threadId")): str(job.get("status"))
            for job in self._jobs
            if job.get("status") in MAIL_ACTIVE_STATUSES
        }
        for row in rows:
            if row["threadId"] in drafted:
                row["hasDraft"] = True
            active_status = active.get(row["threadId"])
            if active_status:
                row["activeJobStatus"] = active_status
        return rows

    def list_threads(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._snapshot_rows_with_drafts()
            visible, _ = self._reconcile_hidden_locked(rows)
            return visible

    def list_hidden_threads(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._snapshot_rows_with_drafts()
            _, hidden = self._reconcile_hidden_locked(rows)
            return hidden

    def unreplied_count(self) -> int:
        return len(self.list_threads())

    def hide(self, thread_id: str, permanent: bool = False) -> None:
        row = self.snapshot.thread(thread_id)
        latest = self.snapshot._latest_incoming(row)
        record = {
            "threadId": thread_id,
            "hiddenAt": _utc_now(),
            "last_msg_id": _mail_last_message_id(row, latest),
        }
        if permanent:
            record["permanent"] = True
        with self._lock:
            self._hidden = [
                item for item in self._hidden if item.get("threadId") != thread_id
            ]
            self._hidden.append(record)
            self._write_records_locked(self.hidden_path, "hidden", self._hidden)

    def unhide(self, thread_id: str) -> None:
        with self._lock:
            remaining = [
                item for item in self._hidden if item.get("threadId") != thread_id
            ]
            if len(remaining) == len(self._hidden):
                raise MailError(404, "mailHiddenNotFound")
            self._hidden = remaining
            self._write_records_locked(self.hidden_path, "hidden", self._hidden)

    def _latest_done_job_locked(self, thread_id: str) -> dict[str, Any] | None:
        job = next(
            (
                dict(item)
                for item in reversed(self._jobs)
                if item.get("threadId") == thread_id
                and item.get("status") == "done"
                and isinstance(item.get("draftId"), str)
                and item.get("draftId")
            ),
            None,
        )
        return job

    def latest_done_job(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._latest_done_job_locked(thread_id)
        if job is None:
            raise MailError(404, "mailDraftNotFound")
        return job

    def draft_payload(self, thread_id: str) -> dict[str, Any]:
        job = self.latest_done_job(thread_id)
        draft_id = str(job["draftId"])
        draft = get_gmail_draft(draft_id)
        return {
            "threadId": thread_id,
            "draftId": draft_id,
            "threadingDegraded": bool(job.get("threadingDegraded")),
            **draft,
            **({"sentAt": job["sentAt"]} if "sentAt" in job else {}),
        }

    def send_draft(self, thread_id: str, draft_id: str) -> str:
        with self._lock:
            job = self._latest_done_job_locked(thread_id)
            if job is None or job.get("draftId") != draft_id:
                raise MailError(404, "mailDraftNotFound")
            if any(item.get("draftId") == draft_id for item in self._sends):
                raise MailError(409, "mailDraftAlreadySent")
            if draft_id in self._sending_drafts:
                raise MailError(409, "mailDraftAlreadySending")
            if any(
                item.get("threadId") == thread_id
                and item.get("status") in MAIL_ACTIVE_STATUSES
                for item in self._jobs
            ):
                raise MailError(409, "mail draft already running")
            self._sending_drafts.add(draft_id)
        try:
            send_gmail_draft(draft_id)
            sent_at = _utc_now()
            with self._lock:
                if any(item.get("draftId") == draft_id for item in self._sends):
                    raise MailError(409, "mailDraftAlreadySent")
                self._sends.append(
                    {"threadId": thread_id, "draftId": draft_id, "sentAt": sent_at}
                )
                target = next(
                    item
                    for item in self._jobs
                    if item.get("jobId") == job.get("jobId")
                )
                target["sentAt"] = sent_at
                self._write_records_locked(self.sends_path, "sends", self._sends)
                self._write_locked()
            return sent_at
        finally:
            with self._lock:
                self._sending_drafts.discard(draft_id)

    def _update(self, job_id: str, status: str, **values: Any) -> None:
        if status not in MAIL_STATUSES:
            raise ValueError("invalid mail job status")
        with self._lock:
            job = next(item for item in self._jobs if item.get("jobId") == job_id)
            job["status"] = status
            job.update(values)
            job.setdefault("history", []).append({"status": status, "at": _utc_now()})
            self._write_locked()

    def worker_once(self, job_id: str) -> None:
        with self._lock:
            job = next(
                (dict(item) for item in self._jobs if item.get("jobId") == job_id),
                None,
            )
        if job is None or job.get("status") != "queued":
            return
        try:
            row = self.snapshot.thread(str(job["threadId"]))
            latest = self.snapshot._latest_incoming(row)
            claim_id = uuid.uuid4().hex
            self._update(
                job_id,
                "drafting",
                claimId=claim_id,
                claimedAt=_utc_now(),
                threadingDegraded=not bool(_mail_message_id(row, latest)),
            )
            if job.get("type") == "revise":
                processor_result = self.revise_processor(
                    row,
                    str(job.get("draftId") or ""),
                    str(job.get("instruction") or ""),
                    lambda status: self._update(job_id, status),
                )
            else:
                processor_result = self.processor(
                    row,
                    str(job.get("direction") or ""),
                    lambda status: self._update(job_id, status),
                )
            meta: dict[str, Any] = {}
            if (
                isinstance(processor_result, tuple)
                and len(processor_result) == 2
                and isinstance(processor_result[1], dict)
            ):
                draft_id = processor_result[0]
                meta = processor_result[1]
            else:
                draft_id = processor_result
            done_values: dict[str, Any] = {
                "draftId": draft_id,
                "completedAt": _utc_now(),
                "completedSnapshotGeneration": self.snapshot.generation(),
            }
            omitted_messages = int(meta.get("omittedMessages") or 0)
            if omitted_messages > 0 or bool(meta.get("bodyTruncated")):
                done_values["truncated"] = True
                done_values["omittedMessages"] = omitted_messages
            self._update(job_id, "done", **done_values)
        except MailError as exc:
            values: dict[str, Any] = {"reason": exc.reason}
            if exc.check_findings:
                values["checkFindings"] = exc.check_findings
            self._update(job_id, "failed", **values)
        except Exception:
            self._update(job_id, "failed", reason="mail draft worker failed")

    def _worker_loop(self) -> None:
        try:
            while not self._stop.is_set():
                job_id = self._queue.get()
                if job_id is None:
                    return
                try:
                    self.worker_once(job_id)
                except BaseException as exc:
                    _log_local("mail_worker", str(exc), uuid.uuid4().hex)
                    continue
        finally:
            if self._stop.is_set():
                self._close_machine_locks()

    def _close_machine_locks(self) -> None:
        for machine_lock in reversed(self._machine_locks):
            machine_lock.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                return
            self._thread = None
        self._close_machine_locks()


def _research_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


RESEARCH_PREAMBLE_MAX_CHARS = 400
_RESEARCH_HEADING_RE = re.compile(r"(?m)^#{1,6} \S")
_RESEARCH_ARCHIVE_BANNED = frozenset("\\/:*?\"<>|+\uFF0B'`")


def _research_strip_preamble(markdown: str) -> str:
    match = _RESEARCH_HEADING_RE.search(markdown)
    if (
        match is None
        or match.start() == 0
        or match.start() > RESEARCH_PREAMBLE_MAX_CHARS
    ):
        return markdown
    return markdown[match.start():]


def _research_report_title(markdown: str, query: str) -> str:
    title = str(query)[:40]
    match = _RESEARCH_HEADING_RE.search(markdown)
    if match is not None:
        heading = markdown[match.start():].splitlines()[0].lstrip("# ").strip()
        if heading:
            title = heading
    return title[:60]


def _research_archive_name(title: str) -> str:
    safe = "".join(
        "_"
        if character in _RESEARCH_ARCHIVE_BANNED
        or ord(character) < 32
        or 127 <= ord(character) <= 159
        else character
        for character in title
    )
    safe = re.sub(r"_+", "_", safe).strip("_ ")
    return (safe or "research")[:60]


def _clean_research_query(value: Any) -> str:
    if not isinstance(value, str):
        raise ResearchError(400, "invalidQuery")
    cleaned = "".join(
        character
        for character in value
        if not (ord(character) < 32 or 127 <= ord(character) <= 159)
    )
    if not cleaned.strip() or len(cleaned) > 1000:
        raise ResearchError(400, "invalidQuery")
    return cleaned


class ResearchJobService:
    def __init__(
        self,
        state_path: Path = RESEARCH_JOBS_PATH,
        run_root: Path = RESEARCH_ROOT,
        runner: Callable[..., subprocess.CompletedProcess[str]] = _run_hidden,
        start_worker: bool = True,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_path = state_path
        self.run_root = run_root
        self.runner = runner
        self.start_worker = start_worker
        self.sleeper = sleeper
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._machine_lock = MachineFileLock(
            state_path.parent / f".{state_path.name}.lock"
        )
        try:
            self._jobs = self._load()
            queued_ids = self._recover_loaded()
        except BaseException:
            self._machine_lock.close()
            raise
        if start_worker:
            for job_id in queued_ids:
                self._start_thread(job_id, self.worker_once)

    def _load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("research jobs state is invalid") from exc
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list) or not all(
            isinstance(job, dict)
            and isinstance(job.get("id"), str)
            and RESEARCH_JOB_ID_RE.fullmatch(job["id"]) is not None
            and isinstance(job.get("query"), str)
            and job.get("depth") in RESEARCH_DEPTHS
            and job.get("status") in RESEARCH_STATUSES
            for job in jobs
        ):
            raise RuntimeError("research jobs state is invalid")
        return jobs

    def _recover_loaded(self) -> list[str]:
        queued_ids: list[str] = []
        changed = False
        for job in self._jobs:
            if job.get("status") == "queued":
                queued_ids.append(str(job["id"]))
            elif job.get("status") == "running":
                job["status"] = "failed"
                job["finishedAt"] = _research_now()
                job["error"] = "interrupted"
                changed = True
        if changed:
            self._write_locked()
        return queued_ids

    def _write_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_name(
            f"{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(
                json.dumps({"jobs": self._jobs}, ensure_ascii=True, indent=2) + "\n",
                encoding="ascii",
                newline="\n",
            )
            os.replace(temp_path, self.state_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job["id"],
            "query": job["query"],
            "depth": job["depth"],
            "status": job["status"],
            "phase": job.get("phase"),
            "progress": job.get("progress"),
            "createdAt": job["createdAt"],
            "startedAt": job.get("startedAt"),
            "finishedAt": job.get("finishedAt"),
            "docId": job.get("docId"),
            "reportPath": job.get("reportPath"),
            "error": job.get("error"),
            "awaiting": job.get("awaiting"),
            "sourceJobId": job.get("sourceJobId"),
            "sourceDegraded": job.get("sourceDegraded"),
            "partial": job.get("partial"),
            "archive": job.get("archive"),
            "archivePath": job.get("archivePath"),
            "archiveError": job.get("archiveError"),
        }

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if RESEARCH_JOB_ID_RE.fullmatch(job_id) is None:
            raise ResearchError(404, "jobNotFound")

    def _find_locked(self, job_id: str) -> dict[str, Any]:
        job = next((item for item in self._jobs if item.get("id") == job_id), None)
        if job is None:
            raise ResearchError(404, "jobNotFound")
        return job

    def enqueue(
        self,
        query: Any,
        depth: Any,
        source_job_id: Any = None,
        archive: Any = None,
    ) -> dict[str, Any]:
        cleaned = _clean_research_query(query)
        if depth not in RESEARCH_DEPTHS:
            raise ResearchError(400, "invalidDepth")
        archive_choice = "none" if archive is None else archive
        if (
            not isinstance(archive_choice, str)
            or archive_choice not in RESEARCH_ARCHIVE_CHOICES
        ):
            raise ResearchError(400, "invalidArchive")
        if source_job_id is not None and (
            not isinstance(source_job_id, str)
            or RESEARCH_JOB_ID_RE.fullmatch(source_job_id) is None
        ):
            raise ResearchError(400, "invalidSource")
        with self._lock:
            if source_job_id is not None:
                source_job = next(
                    (
                        item
                        for item in self._jobs
                        if item.get("id") == source_job_id
                    ),
                    None,
                )
                if source_job is None or source_job.get("status") != "done":
                    raise ResearchError(400, "invalidSource")
            active = [
                job
                for job in self._jobs
                if job.get("status") in RESEARCH_ACTIVE_STATUSES
            ]
            if depth == "ultra":
                if any(job.get("depth") == "ultra" for job in active):
                    raise ResearchError(409, "tooManyActive")
            elif sum(job.get("depth") != "ultra" for job in active) >= 3:
                raise ResearchError(409, "tooManyActive")
            job_id = "r" + datetime.now().astimezone().strftime("%y%m%d") + "-" + secrets.token_hex(3)
            run_dir = (self.run_root / job_id).resolve()
            job = {
                "id": job_id,
                "query": cleaned,
                "depth": depth,
                "status": "queued",
                "phase": "preflight" if depth == "ultra" else "search",
                "progress": None,
                "createdAt": _research_now(),
                "startedAt": None,
                "finishedAt": None,
                "docId": None,
                "reportPath": (run_dir / "index.html").as_posix(),
                "error": None,
                "awaiting": None,
                "sourceJobId": source_job_id,
                "sourceDegraded": None,
                "partial": None,
                "archive": archive_choice,
                "archivePath": None,
                "archiveError": None,
            }
            self._jobs.append(job)
            self._write_locked()
            result = self._public(dict(job))
        if self.start_worker:
            try:
                self._start_thread(job_id, self.worker_once)
            except RuntimeError as exc:
                self._update(job_id, "failed", finishedAt=_research_now(), error="launchFailed")
                raise ResearchError(503, "launchFailed") from exc
        return result

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(dict(job)) for job in reversed(self._jobs[-50:])]

    def get(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        with self._lock:
            return self._public(dict(self._find_locked(job_id)))

    def _update(self, job_id: str, status: str | None = None, **values: Any) -> bool:
        with self._lock:
            job = self._find_locked(job_id)
            if job.get("status") == "canceled" and status != "canceled":
                return False
            if status is not None:
                if status not in RESEARCH_STATUSES:
                    raise ValueError("invalid research status")
                job["status"] = status
            job.update(values)
            self._write_locked()
            return True

    def _start_thread(
        self,
        job_id: str,
        target: Callable[[str], None],
    ) -> None:
        if self._stop.is_set():
            raise RuntimeError("research service stopped")

        def run() -> None:
            try:
                target(job_id)
            finally:
                with self._lock:
                    self._threads.pop(job_id, None)

        thread = threading.Thread(
            target=run,
            name=f"pocket-research-{job_id}",
            daemon=True,
        )
        with self._lock:
            if job_id in self._threads:
                raise RuntimeError("research job already started")
            self._threads[job_id] = thread
        thread.start()

    def _run_process(
        self,
        command: list[str],
        timeout: float,
        cwd: Path,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "cwd": cwd,
            "check": False,
        }
        if input_text is not None:
            kwargs["input"] = input_text
        return self.runner(command, **kwargs)

    @staticmethod
    def _render_prompt(
        path: Path,
        query: str,
        budget_minutes: int | None = None,
    ) -> str:
        template = path.read_text(encoding="utf-8")
        if template.count("{{QUERY}}") != 1:
            raise RuntimeError("research prompt template invalid")
        if "{{BUDGET_MINUTES}}" in template:
            if budget_minutes is None:
                raise RuntimeError("research prompt template invalid")
            template = template.replace("{{BUDGET_MINUTES}}", str(budget_minutes))
        return template.replace("{{QUERY}}", query)

    def _source_prompt_prefix(
        self,
        job_id: str,
        source_job_id: Any,
    ) -> str | None:
        if (
            not isinstance(source_job_id, str)
            or RESEARCH_JOB_ID_RE.fullmatch(source_job_id) is None
        ):
            self._update(job_id, sourceDegraded=True)
            return None
        with self._lock:
            source_job = next(
                (
                    dict(item)
                    for item in self._jobs
                    if item.get("id") == source_job_id
                ),
                None,
            )
        if source_job is None or source_job.get("status") != "done":
            self._update(job_id, sourceDegraded=True)
            return None

        source_body = None
        source_dir = self.run_root / source_job_id
        for filename in ("99_\u7d71\u5408.md", "report.md"):
            try:
                candidate = (source_dir / filename).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if candidate.strip():
                source_body = candidate
                break
        if source_body is None:
            self._update(job_id, sourceDegraded=True)
            return None

        truncated = len(source_body) > 8000
        if truncated:
            source_body = source_body[:8000]
        truncated_note = (
            "\u203b \u524d\u56de\u306e\u672c\u6587\u306f8,000\u5b57\u3092\u8d85\u3048\u305f\u305f\u3081\u3001"
            "\u5148\u982d8,000\u5b57\u306b\u5207\u308a\u8a70\u3081\u305f\u3002"
            if truncated
            else ""
        )
        try:
            preamble = RESEARCH_SOURCE_PREAMBLE.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            self._update(job_id, sourceDegraded=True)
            return None
        lines = preamble.splitlines(keepends=True)
        try:
            content_start = next(
                index for index, line in enumerate(lines) if line.strip() == "---"
            )
        except StopIteration:
            self._update(job_id, sourceDegraded=True)
            return None
        preamble = "".join(lines[content_start:])
        values = {
            "SOURCE_QUERY": str(source_job.get("query") or ""),
            "SOURCE_DEPTH": str(source_job.get("depth") or ""),
            "SOURCE_DATE": str(source_job.get("createdAt") or ""),
            "SOURCE_TRUNCATED_NOTE": truncated_note,
            "SOURCE_BODY": source_body,
        }
        if any(
            preamble.count("{{" + name + "}}") != 1
            for name in values
        ):
            self._update(job_id, sourceDegraded=True)
            return None
        rendered = re.sub(
            r"\{\{(SOURCE_QUERY|SOURCE_DEPTH|SOURCE_DATE|SOURCE_TRUNCATED_NOTE|SOURCE_BODY)\}\}",
            lambda match: values[match.group(1)],
            preamble,
        )
        return rendered.rstrip() + "\n\n"

    def _collect_standard_report(
        self,
        query: str,
        run_dir: Path,
        stdout: str | bytes | None,
    ) -> str:
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        markdown = (stdout or "").strip()
        file_reports: list[tuple[int, str]] = []
        for path in run_dir.glob("*.md"):
            if path.name == "report.md":
                continue
            try:
                if not path.is_file() or path.stat().st_size == 0:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    file_reports.append((path.stat().st_size, content))
            except OSError:
                continue
        file_markdown = max(file_reports, default=(0, ""), key=lambda item: item[0])[1]
        if not markdown:
            markdown = file_markdown
        elif (
            len(markdown) < RESEARCH_MIN_REPORT_CHARS
            and len(file_markdown) >= len(markdown) * RESEARCH_FILE_PREFERENCE_RATIO
        ):
            markdown = file_markdown
        if markdown:
            return _research_strip_preamble(markdown)
        has_material = False
        for path in run_dir.rglob("*"):
            if path.name == "report.md":
                continue
            try:
                if path.is_file() and path.stat().st_size > 0:
                    has_material = True
                    break
            except OSError:
                continue
        if not has_material:
            return ""
        salvage_prompt = self._render_prompt(RESEARCH_SALVAGE_PROMPT, query)
        try:
            salvaged = self._run_process(
                [
                    "claude",
                    "-p",
                    salvage_prompt,
                    "--output-format",
                    "text",
                    "--allowedTools",
                    "Read Glob",
                ],
                RESEARCH_SALVAGE_TIMEOUT,
                run_dir,
            )
            salvage_stdout = salvaged.stdout
        except subprocess.TimeoutExpired as exc:
            salvage_stdout = exc.stdout
        if isinstance(salvage_stdout, bytes):
            salvage_stdout = salvage_stdout.decode("utf-8", errors="replace")
        return _research_strip_preamble((salvage_stdout or "").strip())

    def _active(self, job_id: str) -> bool:
        with self._lock:
            return self._find_locked(job_id).get("status") in {"queued", "running"}

    def worker_once(self, job_id: str) -> None:
        try:
            with self._lock:
                job = dict(self._find_locked(job_id))
            if job.get("status") != "queued":
                return
            if not self._update(
                job_id,
                "running",
                startedAt=_research_now(),
                error=None,
            ):
                return
            run_dir = self.run_root / job_id
            run_dir.mkdir(parents=True, exist_ok=True)
            if job["depth"] == "ultra":
                self._run_ultra_preflight(job_id, job, run_dir)
            else:
                self._run_standard(job_id, job, run_dir)
        except ResearchError as exc:
            self._fail(job_id, exc.code)
        except (OSError, subprocess.TimeoutExpired):
            self._fail(job_id, "launchFailed")
        except Exception as exc:
            _log_local("research_worker", str(exc), uuid.uuid4().hex)
            self._fail(job_id, "researchFailed")

    def _fail(self, job_id: str, code: str) -> None:
        try:
            self._update(
                job_id,
                "failed",
                finishedAt=_research_now(),
                error=code,
                awaiting=None,
            )
        except ResearchError:
            pass

    def _run_standard(
        self,
        job_id: str,
        job: dict[str, Any],
        run_dir: Path,
    ) -> None:
        depth = str(job["depth"])
        query = str(job["query"])
        prompt_path = {
            "quick": RESEARCH_QUICK_PROMPT,
            "recent": RESEARCH_RECENT_PROMPT,
            "deep": RESEARCH_DEEP_PROMPT,
        }[depth]
        prompt = self._render_prompt(
            prompt_path,
            query,
            RESEARCH_BUDGET_MINUTES.get(depth),
        )
        source_job_id = job.get("sourceJobId")
        if source_job_id is not None:
            if depth == "recent":
                self._update(job_id, sourceDegraded=True)
            else:
                source_prefix = self._source_prompt_prefix(job_id, source_job_id)
                if source_prefix is not None:
                    prompt = source_prefix + prompt
        if depth == "recent":
            command = [
                "claude",
                "-p",
                "/last30days " + query,
                "--output-format",
                "text",
                "--allowedTools",
                "Bash Read Write AskUserQuestion WebSearch",
            ]
        else:
            command = [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "text",
                "--allowedTools",
                "WebSearch WebFetch",
            ]
        timed_out = False
        try:
            completed = self._run_process(
                command,
                RESEARCH_TIMEOUTS[depth],
                run_dir,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout
            timed_out = True
        else:
            if completed.returncode != 0:
                raise ResearchError(503, "launchFailed")
            stdout = completed.stdout
        markdown = self._collect_standard_report(query, run_dir, stdout)
        if not markdown:
            raise ResearchError(503, "timedOut" if timed_out else "launchFailed")
        if timed_out:
            markdown = (
                "> \u26a0 \u5236\u9650\u6642\u9593\u3067\u6253\u3061\u5207\u3089\u308c\u305f"
                "\u305f\u3081\u3001\u3053\u3053\u307e\u3067\u306e\u5185\u5bb9\u3067\u3059\u3002\n\n"
                + markdown
            )
            self._update(job_id, partial=True)
            job["partial"] = True
        title = _research_report_title(markdown, query)
        (run_dir / "report.md").write_text(markdown + "\n", encoding="utf-8")
        if not self._active(job_id):
            return
        self._update(job_id, phase="render")
        try:
            rendered = self._run_process(
                [
                    sys.executable,
                    os.fspath(RESEARCH_QUICK_HTML),
                    "--title",
                    title,
                    "--md",
                    os.fspath(run_dir / "report.md"),
                    "--out",
                    os.fspath(run_dir / "index.html"),
                ],
                120,
                run_dir,
            )
        except subprocess.TimeoutExpired as exc:
            raise ResearchError(503, "timedOut") from exc
        if rendered.returncode != 0 or not (run_dir / "index.html").is_file():
            raise ResearchError(503, "launchFailed")
        try:
            self._send_result(job_id, job, run_dir)
        except subprocess.TimeoutExpired as exc:
            raise ResearchError(503, "timedOut") from exc

    def _send_result(
        self,
        job_id: str,
        job: dict[str, Any],
        run_dir: Path,
    ) -> None:
        if not self._active(job_id):
            return
        self._update(job_id, phase="send", progress=None)
        started = job.get("startedAt") or job.get("createdAt")
        try:
            elapsed = max(
                0,
                int((datetime.now().astimezone() - datetime.fromisoformat(str(started))).total_seconds() // 60),
            )
        except (TypeError, ValueError):
            elapsed = 0
        note = f"{job['depth']} / {elapsed} min"
        if job.get("partial") is True:
            note += " / \u9014\u4e2d\u307e\u3067"
        query = str(job["query"])
        title = query[:40]
        try:
            markdown = (run_dir / "report.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            pass
        else:
            title = _research_report_title(markdown, query)
        completed = self._run_process(
            [
                sys.executable,
                os.fspath(RESEARCH_HUB_SEND),
                os.fspath(run_dir / "index.html"),
                "--title",
                title,
                "--note",
                note,
            ],
            RESEARCH_SEND_TIMEOUT,
            run_dir,
        )
        matches = re.findall(r"(?m)^DOC_ID=([0-9a-f]{12})$", completed.stdout)
        if completed.returncode != 0 or len(matches) != 1:
            # The phone is only told the send failed. What it failed on is
            # worth keeping: the answer has been a missing permission, a
            # missing package and an unreachable hub, and none of those are
            # guessable from the word launchFailed alone.
            _log_local("research_send", (completed.stderr or "")[-600:], job_id)
            raise ResearchError(503, "launchFailed")
        doc_id = matches[0]
        finished_at = _research_now()
        if not self._update(
            job_id,
            "done",
            phase="send",
            finishedAt=finished_at,
            docId=doc_id,
            error=None,
            awaiting=None,
        ):
            return
        archive = job.get("archive") or "none"
        if archive == "none":
            return
        try:
            destination_dir = RESEARCH_ARCHIVES[archive]
            if archive == "drive":
                finished_month = datetime.fromisoformat(finished_at).astimezone().strftime(
                    "%Y-%m"
                )
                destination_dir = destination_dir / finished_month
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / f"{_research_archive_name(title)}__{doc_id}.html"
            shutil.copy2(run_dir / "index.html", destination)
            self._update(
                job_id,
                archivePath=destination.as_posix(),
                archiveError=None,
            )
        except OSError as exc:
            try:
                self._update(
                    job_id,
                    archivePath=None,
                    archiveError=str(exc),
                )
            except OSError:
                pass
            _log_local("research_archive", str(exc), job_id)

    def _run_ultra_preflight(
        self,
        job_id: str,
        job: dict[str, Any],
        run_dir: Path,
    ) -> None:
        question_template = RESEARCH_QUESTION_TEMPLATE.read_text(encoding="utf-8")
        prompt_template = RESEARCH_QUESTION_PROMPT.read_text(encoding="utf-8")
        if prompt_template.count("{{QUERY}}") != 1 or prompt_template.count("{{QUESTION_TEMPLATE}}") != 1:
            raise RuntimeError("research question prompt template invalid")
        question_prompt = prompt_template.replace("{{QUERY}}", str(job["query"]))
        question_prompt = question_prompt.replace("{{QUESTION_TEMPLATE}}", question_template)
        source_job_id = job.get("sourceJobId")
        if source_job_id is not None:
            source_prefix = self._source_prompt_prefix(job_id, source_job_id)
            if source_prefix is not None:
                question_prompt = source_prefix + question_prompt
        drafted = self._run_process(
            ["claude", "-p", "--output-format", "text", "--tools", ""],
            300,
            run_dir,
            question_prompt,
        )
        if drafted.returncode != 0 or not drafted.stdout.strip():
            raise ResearchError(503, "launchFailed")
        (run_dir / "00_\u554f\u3044.md").write_text(
            drafted.stdout.strip() + "\n",
            encoding="utf-8",
        )
        if not self._active(job_id):
            return
        self._update(job_id, phase="preflight")
        preflight = self._run_process(
            [
                sys.executable,
                os.fspath(RESEARCH_UDR_SCRIPTS / "udr_preflight.py"),
                "--json",
            ],
            180,
            run_dir,
        )
        try:
            results = json.loads(preflight.stdout.strip().splitlines()[-1])
            landable = sum(
                1
                for item in results.values()
                if isinstance(item, dict) and item.get("ok") is True
            )
        except (IndexError, AttributeError, json.JSONDecodeError) as exc:
            raise ResearchError(503, "launchFailed") from exc
        if landable < 4:
            self._update(
                job_id,
                "awaiting",
                phase="preflight",
                awaiting={
                    "kind": "preflightDegraded",
                    "detail": f"landable={landable}",
                    "options": ["cli", "cancel"],
                },
            )
            return
        self._run_ultra_after_preflight(job_id, "all")

    def _run_ultra_after_preflight(self, job_id: str, mode: str) -> None:
        try:
            with self._lock:
                job = dict(self._find_locked(job_id))
            run_dir = self.run_root / job_id
            if not self._update(
                job_id,
                "running",
                phase="launch",
                awaiting=None,
                error=None,
            ):
                return
            launch_command = [
                sys.executable,
                os.fspath(RESEARCH_UDR_SCRIPTS / "udr_launch.py"),
                "--run-dir",
                os.fspath(run_dir),
                "--engines",
                mode,
            ]
            if mode == "cli":
                launch_command.extend(["--min-engines", "1"])
            launched = self._run_process(launch_command, 300, run_dir)
            if launched.returncode != 0:
                raise ResearchError(503, "launchFailed")
            self._update(
                job_id,
                phase="collect",
                progress={"current": 0, "total": 7},
            )
            collect_started = self.monotonic()
            while not self._stop.is_set() and self._active(job_id):
                collected = self._run_process(
                    [
                        sys.executable,
                        os.fspath(RESEARCH_UDR_SCRIPTS / "udr_collect.py"),
                        "--run-dir",
                        os.fspath(run_dir),
                    ],
                    120,
                    run_dir,
                )
                if collected.returncode != 0:
                    raise ResearchError(503, "launchFailed")
                landed = sum(
                    1 for number in range(1, 8) if (run_dir / f"DONE_{number:02d}.md").is_file()
                )
                self._update(
                    job_id,
                    phase="collect",
                    progress={"current": landed, "total": 7},
                )
                elapsed = self.monotonic() - collect_started
                if landed == 7 or (elapsed >= 45 * 60 and landed >= 4):
                    break
                if elapsed >= 45 * 60:
                    raise ResearchError(503, "launchFailed")
                self.sleeper(60)
            if not self._active(job_id):
                return
            self._update(job_id, phase="synthesis", progress=None)
            synthesized = self._run_process(
                [
                    sys.executable,
                    os.fspath(RESEARCH_UDR_SCRIPTS / "udr_collect.py"),
                    "--run-dir",
                    os.fspath(run_dir),
                    "--synthesize",
                ],
                300,
                run_dir,
            )
            if synthesized.returncode != 0:
                raise ResearchError(503, "launchFailed")
            synthesis_started = self.monotonic()
            source = run_dir / "99_\u7d71\u5408.html"
            while not source.is_file() and self.monotonic() - synthesis_started < 20 * 60:
                if self._stop.is_set() or not self._active(job_id):
                    return
                self.sleeper(10)
            if not source.is_file():
                raise ResearchError(503, "launchFailed")
            shutil.copyfile(source, run_dir / "index.html")
            self._send_result(job_id, job, run_dir)
        except ResearchError as exc:
            self._fail(job_id, exc.code)
        except (OSError, subprocess.TimeoutExpired):
            self._fail(job_id, "launchFailed")
        except Exception as exc:
            _log_local("research_worker", str(exc), uuid.uuid4().hex)
            self._fail(job_id, "researchFailed")

    def resume(self, job_id: str, mode: Any) -> dict[str, Any]:
        self._validate_job_id(job_id)
        if mode not in {"cli", "cancel"}:
            raise ResearchError(400, "invalidDepth")
        with self._lock:
            job = self._find_locked(job_id)
            if job.get("status") != "awaiting":
                raise ResearchError(409, "notAwaiting")
            if mode == "cancel":
                job["status"] = "canceled"
                job["finishedAt"] = _research_now()
                job["awaiting"] = None
                self._write_locked()
                return self._public(dict(job))
        try:
            self._start_thread(
                job_id,
                lambda selected: self._run_ultra_after_preflight(selected, "cli"),
            )
        except RuntimeError as exc:
            raise ResearchError(503, "launchFailed") from exc
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        with self._lock:
            job = self._find_locked(job_id)
            if job.get("status") != "done":
                job["status"] = "canceled"
                job["finishedAt"] = job.get("finishedAt") or _research_now()
                job["awaiting"] = None
                self._write_locked()
            return self._public(dict(job))

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            return
        self._machine_lock.close()


def _workspace_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("workspaces"), list):
        raise BridgeError("bridge workspace schema is invalid")
    return [row for row in payload["workspaces"] if isinstance(row, dict)]


def _pane_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("panes"), list):
        raise BridgeError("bridge pane schema is invalid")
    return [row for row in payload["panes"] if isinstance(row, dict)]


def _numeric_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    return [
        float(item)
        for item in value
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    ]


def _layout_rows(payload: Any) -> dict[str, Any]:
    """Extract the workspace layout from a ``panes`` bridge response.

    ``splitColumns`` is the authoritative placement: ``splitColumns[column][row]``
    holds a pane id, column-first. ``gridTemplateId`` does NOT describe the real
    placement (a "1x1" workspace routinely carries six columns), so it is carried
    through for reference only.

    A missing or malformed field yields an empty value rather than an error, so an
    older mycmux that predates the layout fields keeps the flat list working.
    """
    if not isinstance(payload, dict):
        return {
            "api": {"major": 1, "minor": 0},
            "splitColumns": [],
            "columnWidths": [],
            "rowHeightsPerCol": [],
            "gridTemplateId": None,
        }
    from layout_compat import api_version
    raw_columns = payload.get("splitColumns")
    split_columns: list[list[str]] = []
    from layout_compat import container_layout
    if isinstance(raw_columns, list):
        for column in raw_columns:
            if isinstance(column, str) and container_layout(payload):
                column = [column]
            if not isinstance(column, list):
                continue
            split_columns.append(
                [pane_id for pane_id in column if _bridge_identifier(pane_id)]
            )
    raw_heights = payload.get("rowHeightsPerCol")
    row_heights: list[list[float]] = []
    if isinstance(raw_heights, list):
        row_heights = [_numeric_list(column) for column in raw_heights]
    grid_template = payload.get("gridTemplateId")
    return {
        "api": api_version(payload),
        "splitColumns": split_columns,
        "columnWidths": _numeric_list(payload.get("columnWidths")),
        "rowHeightsPerCol": row_heights,
        "gridTemplateId": grid_template if isinstance(grid_template, str) else None,
    }


def _layout_pane_title(pane: dict[str, Any], tab: dict[str, Any] | None) -> str:
    return resolve_talk_title(tab or pane, "")


def _layout_pane_row(
    pane: dict[str, Any],
    column: int,
    row: int,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    tab = _active_tab(pane)
    claude_session_id = _claude_session_id(tab) if tab is not None else None
    return {
        "paneId": pane.get("id"),
        "column": column,
        "row": row,
        "title": _layout_pane_title(pane, tab),
        "status": _tab_status(tab) if tab is not None else "unknown",
        "agent": tab_agent(tab, launch_kinds_by_tab) if tab is not None else "none",
        **_pane_session_fields(tab or {}),
        "appKind": tab_app_kind(tab or {}, launch_kinds_by_tab),
        "lifecycle": (tab or {}).get("lifecycle"),
        "type": (tab or {}).get("type") or "terminal",
        "claudeSessionId": claude_session_id,
        "tabCount": int(pane.get("tabCount") or len(_tab_rows(pane))),
        "stale": _tab_status(tab) == "unobserved" if tab is not None else True,
        **_observation_fields(tab or {}),
    }


def _layout_pane_rows(
    panes: list[dict[str, Any]],
    layout: dict[str, Any],
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Rows for the layout view, ordered column-first by ``splitColumns``.

    Every pane is emitted exactly once, whether or not it owns a Claude session.
    Dropping a pane would shift the column numbers of everything below it and
    break the one promise the layout view makes: a session keeps its position.
    Panes the layout does not mention are appended in bridge order so that a
    stale ``splitColumns`` can never make a pane disappear.
    """
    pane_by_id = {
        pane.get("id"): pane for pane in panes if isinstance(pane.get("id"), str)
    }
    rows: list[dict[str, Any]] = []
    placed: set[str] = set()
    from layout_compat import placement_columns
    columns = placement_columns(layout)
    for column_index, column in enumerate(columns, start=1):
        for row_index, pane_id in enumerate(column, start=1):
            pane = pane_by_id.get(pane_id)
            if pane is None or pane_id in placed:
                continue
            placed.add(pane_id)
            rows.append(
                _layout_pane_row(pane, column_index, row_index, launch_kinds_by_tab)
            )
    trailing_column = len(columns)
    for pane in panes:
        pane_id = pane.get("id")
        if not isinstance(pane_id, str) or pane_id in placed:
            continue
        placed.add(pane_id)
        trailing_column += 1
        rows.append(_layout_pane_row(pane, trailing_column, 1, launch_kinds_by_tab))
    return rows


def _bridge_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_RE.fullmatch(value))


def _tab_rows(pane: dict[str, Any]) -> list[dict[str, Any]]:
    pane_tabs = pane.get("tabs")
    return [tab for tab in pane_tabs if isinstance(tab, dict)] if isinstance(pane_tabs, list) else []


def _unambiguous_tabs(tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for tab in tabs:
        for key in ("id", "sessionId", "claudeSessionId"):
            value = tab.get(key)
            if _bridge_identifier(value):
                pair = (key, value)
                counts[pair] = counts.get(pair, 0) + 1
    return [
        {**tab, "duplicateId": any(
            counts.get((key, tab.get(key)), 0) > 1
            for key in ("id", "sessionId", "claudeSessionId")
            if _bridge_identifier(tab.get(key))
        )}
        for tab in tabs
    ]


def _addressable_tab(tab: dict[str, Any]) -> bool:
    return (
        _bridge_identifier(tab.get("id"))
        and _bridge_identifier(tab.get("sessionId"))
        and (tab.get("type") is None or _status_value(tab.get("type")) == "terminal")
        and ("claudeSessionId" not in tab or _bridge_identifier(tab.get("claudeSessionId")))
    )


def _observation_fields(tab: dict[str, Any]) -> dict[str, Any]:
    return {key: tab[key] for key in ("agentStatusStale", "screenObserved", "uiState", "display_name") if key in tab}


def _worst_status(tabs: list[dict[str, Any]]) -> str:
    rank = {"attention": 5, "working": 4, "stopped": 3, "idle": 2, "unknown": 1, "unobserved": 0}
    return max((_tab_status(tab) for tab in tabs), key=rank.get, default="unobserved")


def _source_count(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback


def _workspace_counts(
    workspace: dict[str, Any], panes: list[dict[str, Any]], payload: dict[str, Any] | None = None,
) -> dict[str, int]:
    source = payload or {}
    raw_panes = source.get("panes", panes)
    raw_tabs = sum(
        _source_count(pane.get("tabCount"), len(pane["tabs"]) if isinstance(pane.get("tabs"), list) else 0)
        for pane in panes
    )
    return {
        "w": 1,
        "p": _source_count(workspace.get("paneCount"), _source_count(source.get("paneCount"), len(raw_panes))),
        "t": _source_count(workspace.get("tabCount"), _source_count(source.get("tabCount"), raw_tabs)),
    }


def _workspace_tab_rows(
    panes: list[dict[str, Any]], launch_kinds_by_tab=None, launch_times_by_tab=None,
    *, include_list_metadata: bool = False,
) -> list[dict[str, Any]]:
    source = []
    for pane_index, pane in enumerate(panes):
        for tab in _tab_rows(pane):
            source.append({**tab, "paneId": pane.get("id"), "paneIndex": pane_index,
                           "addressable": _addressable_tab(tab)})
    # Resolve mapped Claude ids before checking whether a display route is unique.
    source = [{**tab, "claudeSessionId": _claude_session_id(tab)} for tab in source]
    rows = []
    for index, tab in enumerate(_unambiguous_tabs(source)):
        tab_type = tab.get("type") or "terminal"
        agent = tab_agent(tab, launch_kinds_by_tab)
        terminal = _status_value(tab_type) == "terminal"
        linkable = (
            _bridge_identifier(tab.get("id")) and _bridge_identifier(tab.get("sessionId"))
            and tab["addressable"] and terminal and not tab["duplicateId"] and tab.get("lifecycle") != "declared"
        )
        rows.append({
            "tabId": tab.get("id"), "paneId": tab["paneId"], "paneIndex": tab["paneIndex"],
            "desktopIndex": index, "type": tab_type,
            "kind": "web" if not terminal else "shell" if agent == "none" else "terminal",
            "title": resolve_talk_title(tab, ""),
            # Keep the original label for dispatch owner lookup; UI names use
            # display_name / displayName / title in that order.
            "label": (tab["label"].strip() if isinstance(tab.get("label"), str) and tab["label"].strip()
                      else resolve_talk_title(tab, "")),
            **({"topic": session_topic(tab, agent, cache_absence=True),
                "startedAt": (launch_times_by_tab or {}).get(tab.get("id"))}
               if include_list_metadata else {}),
            # A tab whose PTY is gone is stopped in the list, whatever its last
            # agent or feed state said (a finished turn now reads as idle).
            "status": ("stopped" if tab.get("processStatusReason") == "no_live_pty_session"
                       else _tab_status(tab)), "agent": agent,
            **({"agentSessionId": tab["agentSessionId"]}
               if agent == "codex" and _bridge_identifier(tab.get("agentSessionId")) else {}),
            **_pane_session_fields(tab), "lifecycle": tab.get("lifecycle"),
            "appKind": tab_app_kind(tab, launch_kinds_by_tab),
            **({"sessionStatus": _display_session_status(tab, agent)} if include_list_metadata and linkable else {}),
            "duplicateId": tab["duplicateId"], "linkable": bool(linkable),
            "canSend": bool(linkable and agent in {"claude", "codex"}),
            "claudeSessionId": tab.get("claudeSessionId"), "terminalSessionId": tab.get("sessionId"),
            "processStatusReason": tab.get("processStatusReason"),
            "lastOutputAt": tab.get("lastOutputAt"), **_observation_fields(tab),
        })
    return rows


def _find_workspace(
    workspace_id: Any,
    bridge: Callable[..., Any],
) -> dict[str, Any]:
    if not isinstance(workspace_id, str) or not workspace_id:
        raise LaunchError(400, "launchWorkspaceInvalid")
    workspace = next(
        (
            row
            for row in _workspace_rows(bridge("workspaces"))
            if isinstance(row.get("id"), str) and row.get("id") == workspace_id
        ),
        None,
    )
    if workspace is None:
        raise LaunchError(400, "launchWorkspaceInvalid")
    return workspace


def _launch_label(value: Any, kind: str) -> str:
    if value is None:
        return f"{kind}-{datetime.now().strftime('%H%M%S')}"
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise LaunchError(400, "launchLabelInvalid")
    if any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value):
        raise LaunchError(400, "launchLabelInvalid")
    return value


def _launch_target(payload: dict[str, Any]) -> str:
    """The catalog row to start, from either body key.

    ``kind`` is what the PWA has always sent and names claude / codex / shell.
    ``target`` is the full launcher menu. Sending both is refused rather than
    resolved: a body that says two different things has no right answer.
    """
    has_kind = "kind" in payload
    has_target = "target" in payload
    if has_kind and has_target:
        raise LaunchError(400, "launchBodyInvalid")
    if has_target:
        target = payload.get("target")
        if not isinstance(target, str) or target not in LAUNCH_TARGETS:
            raise LaunchError(400, "launchTargetInvalid")
        return target
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in LAUNCH_KINDS:
        raise LaunchError(400, "launchKindInvalid")
    return kind


def _launch_placement(value: Any) -> str:
    if value is None:
        return DEFAULT_LAUNCH_PLACEMENT
    if not isinstance(value, str) or value not in LAUNCH_PLACEMENTS:
        raise LaunchError(400, "launchPlacementInvalid")
    return value


def _launch_spec_value(value: Any, target: str, field: str) -> str | None:
    """One half of the launch spec, refused rather than quietly dropped.

    A caller that asked for Opus and silently got the default would have no way
    to notice, so a value that cannot be passed through is an error: it could be
    read as a flag, or the target takes no such flag anywhere in the chain.
    """
    code = "launchModelInvalid" if field == "model" else "launchEffortInvalid"
    if value is None:
        return None
    if not isinstance(value, str) or not LAUNCH_SPEC_VALUE.match(value.strip()):
        raise LaunchError(400, code)
    entry = launch_agent_entry(target)
    if entry is None or "cli" not in entry:
        raise LaunchError(400, code)
    return value.strip()


def _launch_anchor_session(payload: Any) -> str:
    panes = _pane_rows(payload)
    for pane in panes:
        tabs = _tab_rows(pane)
        active_tab_id = pane.get("activeTabId")
        active_tab = next((tab for tab in tabs if tab.get("id") == active_tab_id), None)
        candidates = [active_tab, *tabs, pane]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            session_id = candidate.get("sessionId")
            if isinstance(session_id, str) and session_id:
                return session_id
    raise LaunchError(409, "launchWorkspaceUnavailable")


def _launch_shell_argv(cwd: Path) -> list[str]:
    """Argv that opens this machine's bare shell.

    The shell comes from the machine config so a Mac gets zsh instead of
    PowerShell. Agents never come through here: they are started by
    ``spawn --split --target``, which is what makes mycmux track the session and
    what lets the launcher apply a model and effort. An agent name spelled into
    this argv produced a tab mycmux read as a plain shell, with no Claude
    session id, so the phone could never open its talk screen (P11, undone
    2026-09-16).
    """
    del cwd  # The tab's cwd is set by --cwd; the shell starts there already.
    return list(MACHINE["launchShell"])


class LaunchService:
    def __init__(
        self,
        bridge: Callable[..., Any] = run_bridge,
        state_path: Path = LAUNCHES_PATH,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bridge = bridge
        self.state_path = state_path
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._recent: dict[tuple[Any, ...], float] = {}

    def _reserve(self, fingerprint: tuple[Any, ...]) -> float:
        now = self.monotonic()
        with self._lock:
            self._recent = {
                key: timestamp
                for key, timestamp in self._recent.items()
                if now - timestamp < LAUNCH_DUPLICATE_SECONDS
            }
            if fingerprint in self._recent:
                raise LaunchError(429, "launchRateLimited")
            self._recent[fingerprint] = now
        return now

    def _release(self, fingerprint: tuple[Any, ...], stamp: float) -> None:
        with self._lock:
            if self._recent.get(fingerprint) == stamp:
                self._recent.pop(fingerprint, None)

    def _read_ledger_locked(self) -> list[dict[str, Any]]:
        try:
            stored = json.loads(self.state_path.read_text(encoding="ascii"))
            if isinstance(stored, dict) and isinstance(stored.get("launches"), list):
                return [row for row in stored["launches"] if isinstance(row, dict)]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        return []

    def _append_ledger(self, record: dict[str, Any]) -> None:
        with self._lock:
            records = self._read_ledger_locked()
            records.append(record)
            records = records[-LAUNCH_LEDGER_LIMIT:]
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"launches": records}, ensure_ascii=True, indent=2) + "\n",
                encoding="ascii",
            )
            os.replace(temporary, self.state_path)

    def launch_times_by_tab(self) -> dict[str, str]:
        with self._lock:
            records = self._read_ledger_locked()
        result: dict[str, str] = {}
        for record in records:
            tab_id = record.get("tabId")
            timestamp = record.get("timestamp")
            if isinstance(tab_id, str) and isinstance(timestamp, str):
                result[tab_id] = timestamp
        return result

    def launch_kinds_by_tab(self) -> dict[str, str]:
        with self._lock:
            records = self._read_ledger_locked()
        result: dict[str, str] = {}
        for record in records:
            tab_id = record.get("tabId")
            kind = record.get("kind")
            if isinstance(tab_id, str) and isinstance(kind, str) and kind in LAUNCH_KINDS:
                result[tab_id] = kind
        return result

    def launch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise LaunchError(400, "launchBodyInvalid")
        if set(payload) - {
            "kind", "target", "workspaceId", "cwd", "label", "model", "effort",
            "placement",
        }:
            raise LaunchError(400, "launchBodyInvalid")
        target = _launch_target(payload)
        workspace_id = payload.get("workspaceId")
        cwd = resolve_launch_cwd(payload.get("cwd"))
        label_value = payload.get("label") if "label" in payload else None
        label = _launch_label(label_value, target)
        placement = _launch_placement(payload.get("placement"))
        model = _launch_spec_value(payload.get("model"), target, "model")
        effort = _launch_spec_value(payload.get("effort"), target, "effort")

        _find_workspace(workspace_id, self.bridge)

        fingerprint = (
            target,
            workspace_id,
            payload.get("cwd"),
            label_value,
            model,
            effort,
            placement,
        )
        stamp = self._reserve(fingerprint)
        try:
            placement_listing = self.bridge("panes", "--workspace", workspace_id)
            if not _pane_rows(placement_listing):
                raise LaunchError(409, "launchWorkspaceUnavailable")
            # v0.88 has no delegation opt-out for anchored spawn_tab.
            # pane.spawn without a caller creates a new display place.
            args = [
                "spawn", "--split", "--workspace", workspace_id,
                "--cwd",
                os.fspath(cwd),
                "--label",
                label,
                *(["--activate"] if placement == "front" else ["--no-activate"]),
            ]
            if target == "shell":
                args += ["--", *_launch_shell_argv(cwd)]
            else:
                args += ["--target", target]
            if model is not None:
                args += ["--model", model]
            if effort is not None:
                args += ["--effort", effort]
            result = self.bridge(*args)
        except Exception:
            self._release(fingerprint, stamp)
            raise

        if not isinstance(result, dict):
            raise BridgeError("launch response schema is invalid")
        tab_id = result.get("tabId")
        session_id = result.get("sessionId")
        if not tab_id and isinstance(session_id, str):
            # pane.spawn may omit tabId. Identify the new tab once; never spawn again.
            listing = self.bridge("panes", "--workspace", workspace_id)
            matches = [tab for pane in _pane_rows(listing) for tab in _tab_rows(pane)
                       if tab.get("sessionId") == session_id]
            if len(matches) == 1:
                tab_id = matches[0].get("id")
        if (
            not isinstance(tab_id, str)
            or not tab_id
            or not isinstance(session_id, str)
            or not session_id
        ):
            raise BridgeError("launch response schema is invalid")
        try:
            self._append_ledger(
                {
                    "timestamp": (
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    ),
                    "kind": target,
                    "cwd": os.fspath(cwd),
                    "workspaceId": workspace_id,
                    "tabId": tab_id,
                }
            )
        except OSError as exc:
            _log_server_error(f"launch_ledger {exc}")
        return {
            "tabId": tab_id,
            "sessionId": session_id,
            "target": target,
            "placement": placement,
            # What the pane did with the request, not what was asked for: the
            # phone tells the operator where to look, so it must not claim a
            # tab came forward when mycmux left the screen alone.
            "activated": result.get("activationApplied") is True,
            "workspaceId": (
                result.get("workspaceId")
                if isinstance(result.get("workspaceId"), str)
                else workspace_id
            ),
            "paneId": result.get("paneId") if isinstance(result.get("paneId"), str) else None,
            # The id the talk screen opens on, never the tab id. spawn-tab
            # returns as soon as the PTY is up, which is before any agent has
            # written its session file, so this is None at launch and the phone
            # waits for /pocket/api/launch/status to carry it. Present here so
            # both routes hand the client the same shape.
            "claudeSessionId": (
                result.get("claudeSessionId")
                if isinstance(result.get("claudeSessionId"), str)
                and result.get("claudeSessionId")
                else None
            ),
        }


def _active_tab(pane: dict[str, Any]) -> dict[str, Any] | None:
    candidates = _tab_rows(pane)
    active_id = pane.get("activeTabId")
    return next((tab for tab in candidates if tab.get("id") == active_id), None) or (
        candidates[0] if candidates else None
    )


def _tab_status(tab: dict[str, Any]) -> str:
    if tab.get("agentStatusStale") is True or tab.get("screenObserved") is False:
        return "unobserved"
    if tab.get("uiState") in {"working", "waiting", "done", "idle"}:
        return {"working": "working", "waiting": "attention", "done": "attention", "idle": "idle"}[tab["uiState"]]
    return normalize_status(tab.get("agentStatus"), tab.get("processStatus"))


def get_launch_status(
    workspace_id: Any,
    tab_id: Any,
    bridge: Callable[..., Any] = cached_bridge,
) -> dict[str, Any]:
    _find_workspace(workspace_id, bridge)
    if not _bridge_identifier(tab_id):
        raise LaunchError(400, "launchTabInvalid")
    tab = next(
        (
            row
            for row in _all_tabs(bridge("panes", "--workspace", workspace_id))
            if row.get("id") == tab_id
        ),
        None,
    )
    if tab is None:
        return {"exists": False, "claudeSessionId": None, "status": None}
    claude_session_id = _claude_session_id(tab)
    return {
        "exists": True,
        "claudeSessionId": claude_session_id,
        "status": _tab_status(tab),
    }


def normalize_send_text(value: Any) -> Any:
    """Normalize pasted office text before both delivery and transcript matching."""
    if not isinstance(value, str):
        return value
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\v\f\x85\u2028\u2029]", "\n", value).replace("\t", "    ")
    return re.sub(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", "", value)


def validate_send_text(value: Any) -> str | None:
    value = normalize_send_text(value)
    if not isinstance(value, str):
        return "text must be a string"
    if not 1 <= len(value) <= MAX_SEND_TEXT:
        return f"text length must be between 1 and {MAX_SEND_TEXT}"
    return None


def validate_send_key(value: Any) -> str | None:
    if not isinstance(value, str) or value not in {"esc", "up", "down", "enter", "tab"}:
        return "keyNotAllowed"
    return None


def validate_terminal_send_text(value: Any) -> str | None:
    return validate_send_text(value)


def _all_tabs(payload: Any) -> list[dict[str, Any]]:
    tabs = []
    for pane in _pane_rows(payload):
        tabs.extend(_tab_rows(pane))
    # Action callers retain their original validated, unique terminal contract.
    candidates = [tab for tab in tabs if _addressable_tab(tab)]
    identities = _unambiguous_tabs([
        {**tab, "claudeSessionId": _claude_session_id(tab)} for tab in candidates
    ])
    return [tab for tab, identity in zip(candidates, identities) if not identity["duplicateId"]]


def _pane_session_fields(tab: dict[str, Any]) -> dict[str, Any]:
    tab_id = tab.get("id")
    if not _bridge_identifier(tab_id):
        return {}
    path = PANE_SESSION_MAPPING_ROOT / f"{tab_id}.txt"
    unknown = {"agentKind": "unknown", "agentSessionId": None}
    try:
        if path.is_symlink():
            return unknown
        if not path.is_file():
            return {}
        if path.stat().st_size > 512:
            return unknown
        contents = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return unknown
    match = re.fullmatch(r"([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)", contents)
    if match is None:
        return unknown
    return {"agentKind": match.group(1), "agentSessionId": match.group(2)}


def _claude_session_id(tab: dict[str, Any]) -> str | None:
    value = tab.get("claudeSessionId")
    if _bridge_identifier(value):
        return value
    mapped = _pane_session_fields(tab)
    if mapped.get("agentKind") in {"claude", "claude-codex"}:
        return mapped["agentSessionId"]
    return None


def tab_agent(
    tab: dict[str, Any],
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> str:
    if _claude_session_id(tab) is not None:
        return "claude"
    mapped = _pane_session_fields(tab)
    if mapped:
        return "codex" if mapped["agentKind"] == "codex" else "unknown"
    containers = [tab]
    containers.extend(
        container
        for container in (tab.get("metadata"), tab.get("agentMetadata"))
        if isinstance(container, dict)
    )
    explicit_non_agent = False
    for container in containers:
        for key in ("agentKind", "agent_kind", "agent", "agentId", "agent_id"):
            if key not in container:
                continue
            normalized = _status_value(container.get(key))
            if normalized in {"claude", "claude_code"}:
                return "claude"
            if normalized in {"codex", "openai_codex"}:
                return "codex"
            if normalized in {"none", "powershell", "shell", "terminal"}:
                explicit_non_agent = True
    if explicit_non_agent:
        return "none"
    tab_id = tab.get("id")
    launch_kind = (
        launch_kinds_by_tab.get(tab_id)
        if isinstance(tab_id, str) and launch_kinds_by_tab is not None
        else None
    )
    return launch_kind if launch_kind in {"claude", "codex"} else "none"


def tab_app_kind(tab: dict[str, Any], launch_kinds_by_tab=None) -> str:
    aliases = {"claude": "claude", "claude_code": "claude", "claude-code": "claude",
               "claude-handoff": "claude", "codex": "codex", "codex-handoff": "codex",
               "openai_codex": "codex", "claude-codex": "claude-codex",
               "claude-codex-open": "claude-codex", "claude_codex": "claude-codex",
               "grok": "grok", "agy": "antigravity", "antigravity": "antigravity",
               "hermes": "hermes", "omp": "omp", "shell": "shell", "none": "shell"}
    aliases = {_status_value(key): value for key, value in aliases.items()}
    mapped = _pane_session_fields(tab)
    containers = [mapped] if mapped else [tab]
    if not mapped:
        containers.extend(value for value in (tab.get("metadata"), tab.get("agentMetadata"))
                          if isinstance(value, dict))
    for key in ("appKind", "agentKind", "agent_kind", "agent", "agentId", "agent_id"):
        for container in containers:
            value = _status_value(container.get(key))
            if value and value not in {"none", "shell"}:
                return aliases.get(value, "unknown")
    launched = _status_value((launch_kinds_by_tab or {}).get(tab.get("id")))
    return aliases.get(launched, aliases.get(tab_agent(tab, launch_kinds_by_tab), "unknown"))


def _display_session_status(tab: dict[str, Any], agent: str) -> dict[str, Any] | None:
    identity = tab.get("agentSessionId") or _pane_session_fields(tab).get("agentSessionId")
    if agent == "codex":
        path = find_codex_transcript(identity, cache_absence=True) if isinstance(identity, str) else None
    else:
        session = _claude_session_id(tab)
        path = find_transcript(session, cache_absence=True) if session else None
    if path is None:
        return None
    try:
        size, modified = _growth_stamp(path, agent)
        return dict(_cached_display_session_status(os.fspath(path), size, modified, agent))
    except OSError:
        return None


@lru_cache(maxsize=256)
def _cached_display_session_status(path: str, size: int, modified: int, agent: str) -> dict[str, Any]:
    status = {"model": None, "effort": None, "contextTokens": None, "at": None}
    parser = _codex_session_status_from_record if agent == "codex" else _session_status_from_record
    for line in reversed(read_tail_lines(Path(path), max_bytes=256 * 1024)):
        try:
            observed = parser(json.loads(line))
        except (ValueError, TypeError):
            continue
        if observed is None:
            continue
        if status["model"] and observed["model"] and status["model"] != observed["model"]:
            break
        for key in status:
            if status[key] is None and observed[key] is not None:
                status[key] = observed[key]
        if status["model"] and status["effort"]:
            break
    return status


def _is_readable_terminal_tab(tab: dict[str, Any]) -> bool:
    if not _bridge_identifier(tab.get("id")):
        return False
    if _claude_session_id(tab) is not None:
        return False
    tab_type = tab.get("type")
    if tab_type is not None and _status_value(tab_type) != "terminal":
        return False
    lifecycle = tab.get("lifecycle")
    if isinstance(lifecycle, str) and lifecycle.lower() == "declared":
        return False
    return _bridge_identifier(tab.get("sessionId"))


def _matching_tab(payload: Any, session_id: str) -> dict[str, Any] | None:
    """The one addressable tab whose Claude session id is ``session_id``."""
    for tab in _all_tabs(payload):
        if session_id == _claude_session_id(tab):
            terminal_session_id = tab.get("sessionId")
            if isinstance(terminal_session_id, str) and SESSION_ID_RE.fullmatch(
                terminal_session_id
            ):
                return tab
    return None


def _owning_pane(payload: Any, tab: dict[str, Any]) -> dict[str, Any] | None:
    """The one pane whose tab list holds ``tab``, by the tab's own id."""
    owners = [
        pane for pane in _pane_rows(payload)
        if _bridge_identifier(pane.get("id"))
        and any(row.get("id") == tab["id"] for row in _tab_rows(pane))
    ]
    return owners[0] if len(owners) == 1 else None


def resolve_talk(
    session_id: str, bridge: Callable[..., Any] = run_bridge,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The talk's tab, and the pane it sits in when that pane is unambiguous.

    One listing serves both. Reading a talk must not start failing because
    its pane is oddly shaped, so the pane may be None; closing is the strict
    caller (resolve_pane) and refuses in that case.
    """
    if not SESSION_ID_RE.fullmatch(session_id):
        raise BridgeError("sessionNotAllowed")
    payload = bridge("panes", "--all")
    tab = _matching_tab(payload, session_id)
    if tab is None:
        raise BridgeError("sessionNotAllowed")
    return tab, _owning_pane(payload, tab)


def resolve_tab(session_id: str, bridge: Callable[..., Any] = run_bridge) -> dict[str, Any]:
    return resolve_talk(session_id, bridge)[0]


def resolve_pane(
    session_id: str, bridge: Callable[..., Any] = run_bridge,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The pane a talk lives in, keyed by the talk's Claude session id.

    A talk URL names a Claude session. What the phone closes is the pane that
    session's tab sits in. Closing now targets its confirmed front tab. The tab is
    found by the identity contract every action uses (resolve_tab) and the
    pane by that tab's id; a tab id or terminal id from the request never
    stands in for either.
    """
    tab, pane = resolve_talk(session_id, bridge)
    if pane is None:
        raise BridgeError("sessionNotAllowed")
    return pane, tab


def talk_pane_view(
    pane: dict[str, Any], launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The exact front tab the confirmation can close; background tabs stay."""
    front = _active_tab(pane)
    rows = _workspace_tab_rows([{**pane, "tabs": [front] if front else []}], launch_kinds_by_tab)
    return {
        "paneId": pane.get("id"),
        "workspaceId": pane.get("workspaceId"),
        "title": resolve_talk_title(front or pane, ""),
        "tabCount": len(rows),
        "activeTabId": front.get("id") if front else None,
        "closeScope": "activeTab",
        "tabs": rows,
    }


def close_talk_pane(
    session_id: str, pane_id: Any, bridge: Callable[..., Any] = run_bridge,
    *, tab_id: Any = None,
) -> dict[str, Any]:
    """Close the confirmed front tab behind a talk, then prove that tab is gone.

    ``pane_id`` is the pane the phone showed in its confirmation. If the talk
    has moved to another pane since, nothing is closed: the reader confirmed
    one pane, and that is the only pane this may take. mycmux refuses the last
    tab of a workspace's last pane; that refusal comes back as lastTabRefused
    so the screen can say it instead of failing quietly.
    """
    pane, _ = resolve_pane(session_id, bridge)
    if not _bridge_identifier(pane_id) or pane.get("id") != pane_id:
        raise BridgeError("paneChanged")
    front = _active_tab(pane)
    if tab_id is not None and (front is None or front.get("id") != tab_id):
        raise BridgeError("paneChanged")
    tabs = [front] if front else []
    tab_ids = [row["id"] for row in tabs if _bridge_identifier(row.get("id"))]
    if not tab_ids or len(tab_ids) != len(tabs):
        raise BridgeError("paneNotClosed")
    source = _socket_source(bridge)
    if source is not None:
        if not _bridge_identifier(front.get("sessionId")):
            raise BridgeError("paneNotClosed")
        result = source("close-tab", "--session", front["sessionId"])
        if isinstance(result, dict) and result.get("effect") == "needs_confirmation":
            raise BridgeError("tabNeedsConfirmation")
        if not isinstance(result, dict) or result.get("closed") is not True or result.get("tabId") != tab_ids[0]:
            raise BridgeError("paneCloseUnverified")
        closed = tab_ids
    else:
        # Existing injected fixture bridges expose the batch seam.
        result = bridge("close-tabs", *tab_ids)
        closed = result.get("closed") if isinstance(result, dict) else None
    try:
        after = _pane_rows(bridge("panes", "--all"))
    except BridgeError:
        # The close was sent and may well have applied; what is missing is
        # the proof. Saying "failed" here would be false, and trying again
        # would only find the pane gone.
        raise BridgeError("paneCloseUnverified") from None
    remaining = next((row for row in after if row.get("id") == pane_id), None)
    leftover = _tab_rows(remaining) if remaining is not None else []
    if any(row.get("id") in tab_ids for row in leftover) or (isinstance(closed, list) and set(tab_ids) - set(closed)):
        raise BridgeError("paneNotClosed")
    return {
        "closed": True,
        "claudeSessionId": session_id,
        "paneId": pane_id,
        "workspaceId": pane.get("workspaceId"),
        "tabIds": tab_ids,
        "paneRemoved": remaining is None,
    }


def resolve_terminal_tab(
    terminal_session_id: str,
    bridge: Callable[..., Any] = run_bridge,
) -> dict[str, Any]:
    if not SESSION_ID_RE.fullmatch(terminal_session_id):
        raise BridgeError("sessionNotAllowed")
    for tab in _all_tabs(bridge("panes", "--all")):
        if _is_readable_terminal_tab(tab) and terminal_session_id == tab.get("sessionId"):
            return tab
    raise BridgeError("sessionNotAllowed")


# --- AskUserQuestion, answered from the phone (DAN5A) -----------------------
# Claude's choice screen never reaches the transcript until it is answered, so
# it is read off the screen, and it is answered with digit keys only
# (cmux-for-linux-dev-master docs/adr/0003). The bridge skill already does both
# halves; this server borrows its parser and calls its answer-ask command, and
# decides nothing about screens or keys itself.
ASK_BRIDGE_DIR = (
    Path(os.environ["POCKET_ASK_BRIDGE_DIR"])
    if os.environ.get("POCKET_ASK_BRIDGE_DIR") else None
)
ASK_SCREEN_LINES = 80
ASK_ANSWER_TIMEOUT_SECONDS = 30
ASK_ROUTE = "/pocket/api/talk/*/ask"
ASK_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
# What answer-ask says, in its own words, when it pressed one digit and then
# stopped at a question it was not given (tabbed questions are answered one
# screen at a time) or at the review screen that follows the last one.
ASK_NO_ANSWER_PREFIX = "no answer supplied for on-screen question"
ASK_REVIEW_MISMATCH = "review answers do not match the requested answers"
ASK_LOG_CODES = {
    "askChanged", "askNotDelivered", "askReviewUnsupported", "askUnavailable",
    "askBusy", "askInvalid", "notAllowed", "originDenied", "csrfDenied", "jsonRequired",
    "sessionNotAllowed", "bridgeUnavailable",
}
_ASK_SUPPORT_CACHE: dict[tuple[str, int | None], Any] = {}
_ASK_SUPPORT_LOCK = threading.Lock()
# ponytail: one lock per PTY session ever answered, never pruned; a handful a
# day. Prune on tab close if the process ever lives long enough for it to show.
_ASK_SESSION_LOCKS: dict[str, threading.Lock] = {}
_ASK_SESSION_LOCKS_GUARD = threading.Lock()


def ask_support(directory: Path | None = None) -> tuple[Callable[..., Any], Callable[..., Any]] | None:
    """The bridge's ``scan_ask_question`` and ``ask_fingerprint``, or None.

    None switches the feature off without a word: no card, no answers. A file
    that appears or changes later is picked up by its modification time.
    """
    selected = directory if directory is not None else ASK_BRIDGE_DIR
    if selected is None:
        return None
    base = Path(selected)
    support_path = base / "mycmux_bridge_support.py"
    try:
        mtime: int | None = support_path.stat().st_mtime_ns
    except OSError:
        mtime = None
    key = (os.fspath(base), mtime)
    with _ASK_SUPPORT_LOCK:
        if key in _ASK_SUPPORT_CACHE:
            return _ASK_SUPPORT_CACHE[key]
        loaded = None
        try:
            if mtime is not None and (base / "mycmux_bridge.py").is_file():
                spec = importlib.util.spec_from_file_location(
                    "pocket_ask_bridge_support", support_path)
                if spec is not None and spec.loader is not None:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    scan = getattr(module, "scan_ask_question", None)
                    fingerprint = getattr(module, "ask_fingerprint", None)
                    if callable(scan) and callable(fingerprint):
                        loaded = (scan, fingerprint)
        except Exception:
            loaded = None
        _ASK_SUPPORT_CACHE[key] = loaded
        return loaded


def read_ask_screen(
    tab: dict[str, Any],
    bridge: Callable[..., Any],
    support: tuple[Callable[..., Any], Callable[..., Any]],
    *, screen: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    terminal_session_id = tab["sessionId"]
    payload = screen if screen is not None else read_claude_input_screen(tab, bridge)
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("sessionId") != terminal_session_id
            or not isinstance(lines, list) or not all(isinstance(line, str) for line in lines)):
        raise BridgeError("terminalReadUnavailable")
    try:
        ask = support[0](lines)
    except Exception:
        return None
    return ask if isinstance(ask, dict) else None


def ask_bridge_answers(ask: dict[str, Any], answers: Any, submit: Any) -> dict[str, Any] | None:
    """The answers JSON for answer-ask, or None when the request cannot be one.

    Only rows whose role is ``option`` can be chosen, the same rule as the
    bridge's ``_validate_choices``; typing or chatting is left to the PC.
    """
    if ask.get("kind") == "review":
        review = ask.get("review_answers")
        if submit is not True or answers is not None or not isinstance(review, dict) or not review:
            return None
        # A review screen is answered with what it shows. answer-ask today only
        # submits a review inside the same call that chose every answer, so it
        # refuses this (askReviewUnsupported); the shape is the one a
        # fresh-review submit would compare against review_answers.
        return {str(question): str(value) for question, value in review.items()}
    question = ask.get("question")
    if submit is not None or not isinstance(answers, dict) or not isinstance(question, str):
        return None
    if list(answers) != [question]:
        return None
    available = {
        option.get("index") for option in ask.get("options") or []
        if isinstance(option, dict) and option.get("role") == "option"
        and type(option.get("index")) is int and 1 <= option["index"] <= 9
    }
    value = answers[question]
    if ask.get("multi_select"):
        if (not isinstance(value, list) or not value
                or any(type(item) is not int or item not in available for item in value)
                or len(set(value)) != len(value)):
            return None
        return {question: sorted(value)}
    if type(value) is not int or value not in available:
        return None
    return {question: value}


def ask_advanced(before: dict[str, Any], after: dict[str, Any] | None,
                 fingerprint: Callable[..., Any]) -> bool:
    """One tabbed question was answered and the screen moved on to the next."""
    if after is None or fingerprint(after) == fingerprint(before):
        return False
    if after.get("kind") == "review":
        return True
    active = [tab.get("label") for tab in before.get("tabs") or []
              if isinstance(tab, dict) and tab.get("active")]
    return len(active) == 1 and any(
        isinstance(tab, dict) and tab.get("label") == active[0] and tab.get("answered") is True
        for tab in after.get("tabs") or [])


def _ask_session_lock(terminal_session_id: str) -> threading.Lock:
    with _ASK_SESSION_LOCKS_GUARD:
        return _ASK_SESSION_LOCKS.setdefault(terminal_session_id, threading.Lock())


class SelectionError(ReservationError):
    def __init__(self, reason: str, kind: str | None = None) -> None:
        super().__init__(409, reason)
        self.kind = kind


def _screen_selection(tab, bridge, screen, support=None):
    support = ask_support() if support is None else support
    ask = read_ask_screen(tab, bridge, support, screen=screen) if support else None
    return ask, None if ask is not None else picker_screen.scan_picker(screen["lines"])


def _guard_selection(tab, bridge, screen):
    ask, picker = _screen_selection(tab, bridge, screen)
    if ask is not None or picker is not None:
        raise SelectionError("selectionOpen", "ask" if ask is not None else "picker")


def _selection_expectations(tab, bridge):
    snapshot = bridge("status", "--session", tab["sessionId"])
    # Reuse the identity/schema checks without changing seat_state's text gate.
    delivery_state(
        {**tab, "agentStatus": "working", "agentStatusStale": False},
        lambda *args: snapshot,
    )
    entry = snapshot["sessions"][0]
    view = entry["view"]
    if view.get("lifecycle") != "alive":
        raise SelectionError("sessionNotAlive")
    attention = view.get("attention")
    attention_id = attention.get("attention_id") if isinstance(attention, dict) else None
    expectations = []
    for flag, value in (
        ("--expect-epoch", view.get("session_epoch")),
        ("--expect-attention-id", attention_id),
        ("--expect-revision", view.get("session_revision")),
        ("--expect-input-revision", entry.get("input_revision")),
    ):
        valid = (isinstance(value, str) if flag == "--expect-attention-id" else
                 type(value) is int and value >= 0)
        if valid:
            expectations.extend((flag, str(value)))
    return snapshot, expectations


def _send_selection_key(tab, bridge, key, expectations):
    args = ("--text", key) if re.fullmatch(r"[a-z]", key) else ("--key", key)
    command = ("send", "--session", tab["sessionId"], *args, *expectations)
    source = _socket_source(bridge)
    reply = (_run_bridge_cli(*command, include_exit=True)
             if source is not None and source.cli is _run_bridge_cli else bridge(*command))
    exit_code, result = reply if isinstance(reply, tuple) and len(reply) == 2 else (0, reply)
    if type(exit_code) is not int or exit_code not in {0, 1} or not isinstance(result, dict):
        raise SelectionError("keyNotConfirmed")
    if (result.get("ok") is True and result.get("confirmed") is True
            and result.get("sent") is not False):
        return True
    written = result.get("bytes")
    queued = result.get("queuedBytes")
    if ((result.get("reason") == "submit_unconfirmed" and type(written) is int and written >= 1)
            or (result.get("unverified") is True and type(queued) is int and queued >= 1)):
        return False
    raise SelectionError("keyNotConfirmed")


def _read_selection_after(tab, bridge, before_screen, before_picker, *, require_change=False):
    deadline = time.monotonic() + 1.0
    while True:
        screen = read_claude_input_screen(tab, bridge)
        _, picker = _screen_selection(tab, bridge, screen)
        changed = (picker is None or picker["fingerprint"] != before_picker["fingerprint"]
                   if before_picker is not None else screen["lines"] != before_screen["lines"])
        if changed:
            return screen, picker
        if time.monotonic() >= deadline:
            if require_change:
                raise SelectionError("keyNoEffect")
            return screen, picker
        time.sleep(min(0.025, max(0, deadline - time.monotonic())))


_CLAUDE_EDITOR_SETTINGS = Path.home() / ".claude" / "settings.json"
_CLAUDE_EDITOR_CACHE: dict[str, Any] = {}
_CLAUDE_EDITOR_LOCK = threading.Lock()


def _claude_editor_is_vim():
    path = _CLAUDE_EDITOR_SETTINGS
    try:
        stamp = (os.fspath(path), path.stat().st_mtime_ns)
    except OSError:
        stamp = (os.fspath(path), None)
    with _CLAUDE_EDITOR_LOCK:
        if _CLAUDE_EDITOR_CACHE.get("stamp") != stamp:
            mode = None
            if stamp[1] is not None:
                try:
                    mode = json.loads(path.read_text(encoding="utf-8")).get("editorMode")
                except (OSError, ValueError, AttributeError):
                    pass
            _CLAUDE_EDITOR_CACHE.update(stamp=stamp, mode=mode)
        return _CLAUDE_EDITOR_CACHE.get("mode") == "vim"


def _composer_is_insert(screen):
    return bool(screen["lines"] and screen["lines"][-1].lstrip().startswith("-- INSERT --"))


def _ensure_composer_insert(tab, bridge, screen, expectations):
    before = picker_screen.composer_text(screen["lines"])
    if not _claude_editor_is_vim() or before is None or _composer_is_insert(screen):
        return screen, expectations
    try:
        _, expectations = _selection_expectations(tab, bridge)
        screen = read_claude_input_screen(tab, bridge)
        _guard_selection(tab, bridge, screen)
        before = picker_screen.composer_text(screen["lines"])
        if before is None:
            raise SelectionError("composerNotInsert")
        if _composer_is_insert(screen):
            return screen, expectations
        confirmed = _send_selection_key(tab, bridge, "i", expectations)
        after = (read_claude_input_screen(tab, bridge) if confirmed else
                 _read_selection_after(tab, bridge, screen, None)[0])
    except (BridgeError, SelectionError) as exc:
        raise SelectionError("composerNotInsert") from exc
    after_text = picker_screen.composer_text(after["lines"])
    if (after_text is not None and len(after_text) == len(before) + 1
            and any(after_text[index] == "i"
                    and after_text[:index] + after_text[index + 1:] == before
                    for index in range(len(after_text)))):
        try:
            _, fresh = _selection_expectations(tab, bridge)
            _send_selection_key(tab, bridge, "backspace", fresh)
        except (BridgeError, SelectionError) as exc:
            raise SelectionError("composerNotInsert") from exc
        raise SelectionError("composerNotInsert")
    if after_text is None or not _composer_is_insert(after):
        raise SelectionError("composerNotInsert")
    _guard_selection(tab, bridge, after)
    try:
        _, fresh = _selection_expectations(tab, bridge)
    except (BridgeError, SelectionError) as exc:
        raise SelectionError("composerNotInsert") from exc
    return after, fresh


def _last_json_object(text: str) -> dict[str, Any] | None:
    for line in reversed((text or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else None
    return None


def run_answer_ask(terminal_session_id: str, answers: dict[str, Any]) -> tuple[str, str]:
    """Run the bridge's answer-ask once: (result kind, bridge detail).

    The detail is for classifying the outcome here and is never logged; it can
    name the question on screen.
    """
    if ASK_BRIDGE_DIR is None:
        return "notStarted", ""
    command = [
        os.fspath(Path(sys.executable)),
        os.fspath(ASK_BRIDGE_DIR / "mycmux_bridge.py"),
        "answer-ask",
        "--session", terminal_session_id,
        "--answers-json", json.dumps(answers, ensure_ascii=True, separators=(",", ":")),
    ]
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ASK_ANSWER_TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "timeout", ""
    except OSError:
        return "notStarted", ""
    for stream in (completed.stdout, completed.stderr):
        value = _last_json_object(stream)
        if value is not None and isinstance(value.get("result"), str):
            kind = value["result"]
            if kind == "observed_delivered" and completed.returncode != 0:
                return "unparsed", ""
            detail = value.get("error", value.get("detail", ""))
            return kind, detail if isinstance(detail, str) else ""
    return "unparsed", ""


def read_terminal_payload(
    terminal_session_id: str,
    bridge: Callable[..., Any] = run_bridge,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    tab = _talk_observation(resolve_terminal_tab(terminal_session_id, bridge), bridge)
    payload = bridge(
        "read",
        "--session",
        terminal_session_id,
        "--lines",
        "200",
    )
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("sessionId") != terminal_session_id
        or not isinstance(lines, list)
        or not all(isinstance(line, str) for line in lines)
    ):
        raise BridgeError("terminalReadUnavailable")
    label = ""
    for key in ("display_name", "label"):
        value = tab.get(key)
        if isinstance(value, str) and value.strip():
            label = value.strip()
            break
    tab_id = tab.get("id")
    return {
        "lines": lines[-200:],
        "terminalSessionId": terminal_session_id,
        "title": (
            label
            if isinstance(label, str) and label.strip()
            else tab_id if isinstance(tab_id, str) and tab_id else terminal_session_id
        ),
        "status": _tab_status(tab),
        "agent": tab_agent(tab, launch_kinds_by_tab),
        "appKind": tab_app_kind(tab, launch_kinds_by_tab),
        **_observation_fields(tab),
    }


def read_tab_terminal_payload(
    tab: dict[str, Any],
    bridge: Callable[..., Any] = run_bridge,
) -> dict[str, Any]:
    terminal_session_id = tab.get("sessionId")
    if not isinstance(terminal_session_id, str) or not SESSION_ID_RE.fullmatch(
        terminal_session_id
    ):
        raise BridgeError("sessionNotAllowed")
    payload = bridge("read", "--session", terminal_session_id, "--lines", "200")
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("sessionId") != terminal_session_id
        or not isinstance(lines, list)
        or not all(isinstance(line, str) for line in lines)
    ):
        raise BridgeError("terminalReadUnavailable")
    return {"terminalSessionId": terminal_session_id, "lines": lines[-200:]}


def claude_screen_is_idle(payload: Any, expected_session_id: str) -> bool:
    if not isinstance(payload, dict) or payload.get("sessionId") != expected_session_id:
        return False
    lines = payload.get("lines")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return False
    if CLAUDE_BUSY_SCREEN_RE.search("\n".join(lines)):
        return False
    return any(CLAUDE_PROMPT_LINE_RE.match(line) for line in lines)


#: Attention kinds under which the seat is asking a person for something: a
#: choice, a permission, a limit or an error to acknowledge. Text typed then
#: is read as the answer, so nothing is sent (ruling of 2026-09-16, item 0).
BLOCKING_ATTENTION_KINDS = frozenset({"input", "approval", "rate_limited", "error"})
#: What a Claude screen shows while it waits for a person, for the fallback
#: that reads the screen when the status feed is stale.
CLAUDE_QUESTION_SCREEN_RE = re.compile(
    r"Do you want to\b|Esc to cancel|Enter to select|Enter to confirm"
    r"|Yes, and don't ask again|\(Y/n\)|\by/n\b",
    re.IGNORECASE,
)


def _claude_screen_prompt_is_empty(lines: list[str]) -> bool:
    for line in lines:
        match = CLAUDE_PROMPT_LINE_RE.match(line)
        if match and not line[match.end():].strip():
            return True
    return False


def claude_screen_seat_state(payload: Any, expected_session_id: str) -> str:
    """idle, working or blocked, from the screen alone.

    Working is claimed only when the busy marker is on screen together with
    an empty input line: what is typed then lands in that line and is read
    when the turn ends, as it is on the PC. Anything else - a question, a
    draft left in the line, an unreadable screen - is blocked.
    """
    if not isinstance(payload, dict) or payload.get("sessionId") != expected_session_id:
        return "blocked"
    lines = payload.get("lines")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return "blocked"
    joined = "\n".join(lines)
    if CLAUDE_QUESTION_SCREEN_RE.search(joined):
        return "blocked"
    if claude_screen_is_idle(payload, expected_session_id):
        return "idle"
    if CLAUDE_BUSY_SCREEN_RE.search(joined) and _claude_screen_prompt_is_empty(lines):
        return "working"
    return "blocked"


def seat_state(
    tab: dict[str, Any],
    bridge: Callable[..., Any] = run_bridge,
    *,
    with_reason: bool = False,
) -> tuple[str, list[str]] | tuple[str, list[str], str | None]:
    """Whether a message can go to the seat now, and the guard arguments.

    ``idle``: the seat is waiting for input. ``working``: it is on a turn
    and not asking anything, so a message typed now queues behind the turn
    exactly as one typed on the PC does. ``blocked``: it is asking a person
    something, is not alive, or cannot be read - the message is kept back.
    """
    terminal_session_id = str(tab.get("sessionId") or "")
    def answer(state: str, expectations: list[str], reason: str | None = None):
        return (state, expectations, reason) if with_reason else (state, expectations)

    if with_reason and tab.get("processStatusReason") == "no_live_pty_session":
        return answer("blocked", [], "seatNotRunning")
    screen_fallback = (
        tab.get("agentStatusStale") is True
        or not _status_value(tab.get("agentStatus"))
    )
    try:
        payload = bridge("status", "--session", terminal_session_id)
    except BridgeError:
        if screen_fallback:
            return answer("blocked", [], "statusUnreadable")
        raise
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list) or len(sessions) != 1:
        raise BridgeError("sessionStateUnavailable")
    entry = sessions[0]
    view = entry.get("view") if isinstance(entry, dict) else None
    if (
        not isinstance(entry, dict)
        or entry.get("session_id") != terminal_session_id
        or not isinstance(view, dict)
    ):
        raise BridgeError("sessionStateUnavailable")
    attention = view.get("attention")
    attention_kind = attention.get("kind") if isinstance(attention, dict) else None
    attention_open = (
        attention_kind in BLOCKING_ATTENTION_KINDS
        or (attention_kind not in {None, "none", "done"})
        or entry.get("ui_state") == "waiting"
    )
    activity = normalize_status(view.get("activity"), None)
    if view.get("lifecycle") != "alive" or attention_open:
        state = "blocked"
    elif activity in {"idle", "working"}:
        state = activity
    else:
        state = "blocked"
    expectation_args: list[str] = []
    epoch = view.get("session_epoch")
    revision = view.get("session_revision")
    input_revision = entry.get("input_revision")
    attention_id = attention.get("attention_id") if isinstance(attention, dict) else None
    if all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (epoch, revision, input_revision)
    ) and isinstance(attention_id, str):
        expectation_args = [
            "--expect-epoch",
            str(epoch),
            "--expect-attention-id",
            attention_id,
            "--expect-revision",
            str(revision),
            "--expect-input-revision",
            str(input_revision),
        ]
    if input_revision is None and view.get("lifecycle") == "alive":
        # A live seat whose input line mycmux cannot vouch for: never send
        # without an input expectation; hold the message instead.
        return answer("blocked", [], "statusUnreadable")
    reason = ("seatNotRunning" if view.get("lifecycle") != "alive" else
              "questionOpen" if attention_open else
              "seatBusy" if state == "blocked" else None)
    if screen_fallback:
        try:
            screen = bridge(
                "read", "--session", terminal_session_id, "--lines", "80"
            )
        except BridgeError:
            return answer("blocked", expectation_args, "statusUnreadable")
        state = claude_screen_seat_state(screen, terminal_session_id)
        if state == "blocked":
            lines = screen.get("lines") if isinstance(screen, dict) else None
            if isinstance(lines, list) and all(isinstance(line, str) for line in lines):
                reason = "questionOpen" if CLAUDE_QUESTION_SCREEN_RE.search("\n".join(lines)) else "statusUnreadable"
            else:
                reason = "statusUnreadable"
    return answer(state, expectation_args, reason if state == "blocked" else None)


def delivery_state(
    tab: dict[str, Any],
    bridge: Callable[..., Any] = run_bridge,
) -> tuple[bool, list[str]]:
    """Idle or not, for the callers that only deliver to an idle seat: the
    reservation worker and the key route. Same reading as seat_state."""
    state, expectation_args = seat_state(tab, bridge)
    return state == "idle", expectation_args


def read_claude_input_screen(
    tab: dict[str, Any], bridge: Callable[..., Any]
) -> dict[str, Any]:
    terminal_session_id = tab["sessionId"]
    payload = bridge("read", "--session", terminal_session_id, "--lines", "80")
    if (
        not isinstance(payload, dict)
        or payload.get("sessionId") != terminal_session_id
        or not isinstance(payload.get("lines"), list)
        or not all(isinstance(line, str) for line in payload["lines"])
    ):
        raise BridgeError("terminalReadUnavailable")
    return payload


def shell_prompt_visible(payload: dict[str, Any]) -> bool:
    non_empty_lines = [line for line in payload["lines"] if line.strip()]
    return bool(non_empty_lines and SHELL_PROMPT_LINE_RE.fullmatch(non_empty_lines[-1]))


CLAUDE_BODY_OR_CHOICE_LINE_RE = re.compile(
    r"^\s*(?:\u276f|\u25cf|\u23bf|\d+[.)](?:\s|$))"
)


def _claude_input_line(payload: dict[str, Any]) -> str | None:
    if shell_prompt_visible(payload):
        return None
    screen_tail = [line for line in payload["lines"] if line.strip()][-TUI_SCREEN_SCAN_LIMIT:]
    for index in range(len(screen_tail) - 1, -1, -1):
        line = screen_tail[index]
        prompt = CLAUDE_PROMPT_LINE_RE.match(line)
        if prompt is None:
            continue
        following = screen_tail[index + 1:]
        if following and TUI_SEPARATOR_LINE_RE.match(following[0]):
            return line
        # A bare prompt can precede the status bar without a separator.
        # Bullets mark assistant/tool output; numbered rows mark choices.
        # A later separator closes a multiline composer: the intervening
        # lines are draft continuations, not a footer.
        if (not line[prompt.end():].strip()
                and not any(TUI_SEPARATOR_LINE_RE.match(row) for row in following)
                and not any(CLAUDE_BODY_OR_CHOICE_LINE_RE.match(row) for row in following)
                and not CLAUDE_QUESTION_SCREEN_RE.search("\n".join(payload["lines"]))):
            return line
        return None  # Never fall back to an older prompt.
    return None


def claude_input_line_visible(payload: dict[str, Any]) -> bool:
    return _claude_input_line(payload) is not None


def _reservation_tabs(listing: Any) -> list[dict[str, Any]]:
    # The worker must see duplicate Claude ids to recover a stale tab.
    # Retain the action routes' uniqueness checks for tab and PTY ids.
    tabs = [tab for pane in _pane_rows(listing) for tab in _tab_rows(pane)
            if _addressable_tab(tab)]
    identities = _unambiguous_tabs([{**tab, "claudeSessionId": None} for tab in tabs])
    return [tab for tab, identity in zip(tabs, identities) if not identity["duplicateId"]]


def reservation_delivery_gate(
    tab: dict[str, Any], bridge: Callable[..., Any],
) -> tuple[bool, list[str], dict[str, Any] | None, str | None]:
    # Fetch before seat_state so its stale-feed fallback cannot hide a
    # sessionStateUnavailable error from the worker's stale-tab recovery.
    status = bridge("status", "--session", tab["sessionId"])
    state, expectations, reason = seat_state(
        tab, lambda *args: status if args[0] == "status" else bridge(*args),
        with_reason=True,
    )
    entry = status["sessions"][0]  # Identity/schema were checked by seat_state.
    view = entry["view"]
    attention = view.get("attention")
    kind = attention.get("kind") if isinstance(attention, dict) else None
    if view.get("lifecycle") != "alive":
        return False, expectations, None, "seatNotRunning"
    if kind not in {None, "none", "done"} or entry.get("ui_state") == "waiting":
        return False, expectations, None, "questionOpen"
    if state not in {"idle", "working"}:
        return False, expectations, None, reason or "statusUnreadable"
    screen = read_claude_input_screen(tab, bridge)
    try:
        _guard_selection(tab, bridge, screen)
    except SelectionError:
        return False, expectations, screen, "questionOpen"
    if CLAUDE_QUESTION_SCREEN_RE.search("\n".join(screen["lines"])):
        return False, expectations, screen, "questionOpen"
    line = _claude_input_line(screen)
    if line is None:
        reason = "seatNotRunning" if shell_prompt_visible(screen) else "statusUnreadable"
        return False, expectations, screen, reason
    if line[CLAUDE_PROMPT_LINE_RE.match(line).end():].strip():
        return False, expectations, screen, "seatBusy"
    return True, expectations, screen, None


def _delivery_screen_check(
    payload: Any,
    text: str,
    prompt_re: re.Pattern[str],
    following_line_res: tuple[re.Pattern[str], ...],
    expected_session_id: str | None = None,
) -> tuple[bool, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        return False, "read returned invalid screen data"
    if expected_session_id is not None and payload.get("sessionId") != expected_session_id:
        return False, "read returned a different terminal session"
    lines = payload["lines"]
    if not all(isinstance(line, str) for line in lines):
        return False, "read returned invalid screen data"
    non_empty_lines = [line for line in lines if line.strip()]
    screen_tail = non_empty_lines[-TUI_SCREEN_SCAN_LIMIT:]
    current_line = ""
    if prompt_re is CLAUDE_PROMPT_LINE_RE:
        current_line = _claude_input_line(payload) or ""
        if not current_line and not shell_prompt_visible(payload):
            for line in reversed(screen_tail):
                prompt = prompt_re.match(line)
                if prompt is not None:
                    if text and (text in line or ("\n" in text and line[prompt.end():].strip())):
                        return False, "sent text remains in the input prompt"
                    break
    else:
        for index in range(len(screen_tail) - 1, -1, -1):
            line = screen_tail[index]
            if not prompt_re.match(line):
                continue
            if index < len(screen_tail) - 1 and not any(
                pattern.match(screen_tail[index + 1]) for pattern in following_line_res
            ):
                break
            current_line = line
            break
    if not current_line or not prompt_re.match(current_line):
        return False, "current input line was not identified"
    # DAN2F 1c: Codex renders this captured placeholder inside its empty prompt.
    # It is not text left over from a multiline delivery (Claude is unchanged).
    prompt = prompt_re.match(current_line)
    if prompt_re is CODEX_PROMPT_LINE_RE and current_line[prompt.end():].strip() == CODEX_EMPTY_PROMPT_TEXT:
        current_line = current_line[:prompt.end()]
    # A multiline draft can be wrapped or only partly visible. Require the
    # identified prompt to be empty instead of searching for the full message
    # (which can never fit in one screen line). Continuations below a prompt
    # already fail the footer check above.
    if "\n" in text and current_line[prompt_re.match(current_line).end():].strip():
        return False, "sent text remains in the input prompt"
    if text and text in current_line:
        return False, "sent text remains in the input prompt"
    return True, None


def delivery_screen_check(payload: Any, text: str) -> tuple[bool, str | None]:
    return _delivery_screen_check(
        payload,
        text,
        CLAUDE_PROMPT_LINE_RE,
        CLAUDE_PROMPT_FOLLOWING_LINE_RES,
    )


def codex_delivery_screen_check(
    payload: Any,
    text: str,
    expected_session_id: str | None = None,
) -> tuple[bool, str | None]:
    return _delivery_screen_check(
        payload,
        text,
        CODEX_PROMPT_LINE_RE,
        CODEX_PROMPT_FOLLOWING_LINE_RES,
        expected_session_id,
    )


def codex_ready_model(
    payload: Any,
    expected_session_id: str,
) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        return None
    if payload.get("sessionId") != expected_session_id:
        return None
    lines = payload["lines"]
    if not all(isinstance(line, str) for line in lines):
        return None
    non_empty_lines = [line for line in lines if line.strip()]
    for line in reversed(non_empty_lines[-TUI_SCREEN_SCAN_LIMIT:]):
        if not CODEX_STATUS_BAR_LINE_RE.match(line):
            continue
        fields = [field.strip() for field in line.split("\u00b7")]
        if len(fields) < 2 or not fields[1]:
            return None
        states = [field for field in fields if field in CODEX_STATUS_STATES]
        if states == ["Ready"]:
            return fields[1]
        return None
    return None


def _delivered_picker(tab, bridge, screen):
    if (not isinstance(screen, dict) or screen.get("sessionId") != tab["sessionId"]
            or not isinstance(screen.get("lines"), list)
            or not all(isinstance(line, str) for line in screen["lines"])):
        return False
    _, picker = _screen_selection(tab, bridge, screen)
    return picker is not None


def deliver_to_tab(
    tab: dict[str, Any],
    text: str,
    bridge: Callable[..., Any] = run_bridge,
    expectation_args: list[str] | None = None,
    screen_check: Callable[[Any, str], tuple[bool, str | None]] = delivery_screen_check,
    bracketed_paste: bool = True,
    input_screen: dict[str, Any] | None = None,
) -> tuple[bool, str | None, Any]:
    terminal_session_id = tab["sessionId"]
    try:
        if expectation_args is None:
            is_idle, expectation_args = delivery_state(tab, bridge)
            if not is_idle:
                return False, "target is not idle", None
        if input_screen is not None:
            _guard_selection(tab, bridge, input_screen)
            input_screen, expectation_args = _ensure_composer_insert(
                tab, bridge, input_screen, expectation_args)
        # The CLI writes the body, waits for its echo, then sends the key with
        # the guarded input revision advanced. --key enter has one attempt;
        # --enter may retry. Keep both steps in its existing session transaction.
        body = f"\x1b[200~{text}\x1b[201~" if bracketed_paste else text
        submit_args = ("--key", "enter") if bracketed_paste else ("--enter",)
        confirmation = bridge(
            "send",
            "--session",
            terminal_session_id,
            f"--text={body}",
            *submit_args,
            *expectation_args,
        )
    except BridgeError as exc:
        _log_server_error(f"bridge_delivery {exc.detail}")
        return False, "bridgeUnavailable", None
    if not (
        isinstance(confirmation, dict)
        and confirmation.get("ok") is True
        and confirmation.get("confirmed") is True
        and confirmation.get("sent") is not False
    ):
        return False, "enter delivery was not confirmed", None
    last_read = None
    reason = "screen delivery check did not complete"
    for attempt in range(3):
        if attempt:
            time.sleep(0.15)
        try:
            last_read = bridge("read", "--session", terminal_session_id, "--lines", "80")
        except BridgeError as exc:
            _log_server_error(f"bridge_delivery_check {exc.detail}")
            reason = "bridgeUnavailable"
            continue
        delivered, reason = screen_check(last_read, text)
        if (input_screen is not None and bracketed_paste
                and _delivered_picker(tab, bridge, last_read)):
            return True, None, last_read
        if delivered:
            return True, None, last_read
    return False, reason, last_read


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ReservationService:
    def __init__(
        self,
        state_path: Path = RESERVATION_PATH,
        bridge: Callable[..., Any] = run_bridge,
        interval_seconds: float = RESERVATION_INTERVAL_SECONDS,
        start_worker: bool = True,
        ready_sleeper: Callable[[float], None] = time.sleep,
        receipt_sleeper: Callable[[float], None] = time.sleep,
        wait_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_path = state_path
        self.bridge = bridge
        self.interval_seconds = interval_seconds
        self.ready_sleeper = ready_sleeper
        self.receipt_sleeper = receipt_sleeper
        self.wait_clock = wait_clock
        self._wait_check_times: dict[str, float] = {}
        self._stale_observations: dict[str, int] = {}
        self._machine_lock = MachineFileLock(
            state_path.parent / f".{state_path.name}.lock"
        )
        self._lock = threading.Lock()
        self._session_locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._terminal_send_times: dict[str, float] = {}
        self._stop = threading.Event()
        self._inflight: set[str] = set()
        try:
            self._records = self._load()
            self._recover_loaded()
        except BaseException:
            self._machine_lock.close()
            raise
        self._thread: threading.Thread | None = None
        self.cleanup()
        if start_worker:
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="pocket-reservations",
                daemon=True,
            )
            self._thread.start()

    def _load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load reservations: {exc}") from exc
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise RuntimeError("reservations file schema is invalid")
        return payload

    def _recover_loaded(self) -> None:
        changed = False
        for record in self._records:
            if record.get("status") == "delivering":
                record["status"] = "failed"
                record["reason"] = "delivery interrupted; terminal result uncertain"
                record["updatedAt"] = _utc_now()
                changed = True
        if changed:
            self._write_locked()

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def _write_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f"{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(self._records, ensure_ascii=True, indent=2) + "\n"
        try:
            temporary.write_text(payload, encoding="ascii", newline="\n")
            os.replace(temporary, self.state_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup(self) -> None:
        cutoff = time.time() - RESERVATION_TTL_SECONDS
        pending_cutoff = time.time() - RESERVATION_PENDING_TTL_SECONDS
        with self._lock:
            retained = []
            changed = False
            for record in self._records:
                if record.get("status") == "pending":
                    timestamp = _parse_timestamp(record.get("createdAt"))
                    if timestamp is not None and timestamp < pending_cutoff:
                        record["status"] = "expired"
                        record["reason"] = "pending reservation expired"
                        record["updatedAt"] = _utc_now()
                        changed = True
                    else:
                        retained.append(record)
                        continue
                timestamp = _parse_timestamp(record.get("updatedAt"))
                if timestamp is None or timestamp >= cutoff:
                    retained.append(record)
            if changed or retained != self._records:
                self._records = retained
                self._write_locked()

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._records if row.get("sessionId") == session_id]

    def reserve(self, session_id: str, text: str, *, terminal_tab: dict[str, Any] | None = None,
                reserve_reason: str | None = None) -> dict[str, Any]:
        now = _utc_now()
        record = {
            "id": uuid.uuid4().hex,
            "sessionId": session_id,
            "text": text,
            "status": "pending",
            "createdAt": now,
            "updatedAt": now,
        }
        if reserve_reason is not None:
            record["reserveReason"] = reserve_reason
        if terminal_tab is not None:
            record.update(targetKind="term", tabId=terminal_tab.get("id"),
                          agentSessionId=terminal_tab.get("agentSessionId"))
        with self._lock:
            pending = [row for row in self._records if row.get("status") == "pending"]
            per_session = [
                row for row in pending if row.get("sessionId") == session_id
            ]
            text_bytes = len(text.encode("utf-8"))
            per_session_bytes = sum(
                len(str(row.get("text") or "").encode("utf-8"))
                for row in per_session
            )
            global_bytes = sum(
                len(str(row.get("text") or "").encode("utf-8")) for row in pending
            )
            if len(per_session) >= RESERVATION_PER_SESSION_COUNT:
                raise ReservationError(429, "per-session reservation quota exceeded")
            if per_session_bytes + text_bytes > RESERVATION_PER_SESSION_BYTES:
                raise ReservationError(429, "per-session reservation bytes exceeded")
            if len(pending) >= RESERVATION_GLOBAL_COUNT:
                raise ReservationError(507, "global reservation quota exceeded")
            if global_bytes + text_bytes > RESERVATION_GLOBAL_BYTES:
                raise ReservationError(507, "global reservation bytes exceeded")
            self._records.append(record)
            self._write_locked()
        return dict(record)

    def session_for_reservation(self, reservation_id: str) -> str | None:
        with self._lock:
            record = next(
                (row for row in self._records if row.get("id") == reservation_id),
                None,
            )
            value = record.get("sessionId") if record is not None else None
            return value if isinstance(value, str) else None

    def resolve_reservation_target(self, session_id, bridge):
        records = self.list_for_session(session_id)
        if any(row.get("targetKind") == "term" for row in records):
            return resolve_terminal_tab(session_id, bridge)
        return resolve_tab(session_id, bridge)

    def cancel(self, reservation_id: str) -> str:
        with self._lock:
            for record in self._records:
                if record.get("id") != reservation_id:
                    continue
                if record.get("status") != "pending" or reservation_id in self._inflight:
                    return "not_pending"
                record["status"] = "canceled"
                record["updatedAt"] = _utc_now()
                self._write_locked()
                return "canceled"
        return "not_found"

    def submit(self, session_id: str, text: str) -> dict[str, Any]:
        """Send now unless the seat is asking a person something.

        A seat on a turn takes the message the way the PC does: it lands in
        the input line and is read when the turn ends, so it is sent and
        confirmed exactly as an idle delivery is (ruling of 2026-09-16, item 0; before
        this, any non-idle seat became a reservation, and one phone send
        with an image was reserved and then cancelled 19 seconds later).
        Only a seat with a question open, a seat that is gone, or a bridge
        that cannot say is kept as a reservation.
        """
        with self._session_lock(session_id):
            try:
                tab = resolve_tab(session_id, self.bridge)
                state, expectations, reserve_reason = seat_state(tab, self.bridge, with_reason=True)
            except BridgeError as exc:
                _log_server_error(f"bridge_submit {exc.detail}")
                state, expectations, tab = "blocked", [], None
                reserve_reason = ("seatNotRunning" if exc.code == "sessionNotAllowed" else
                                  "statusUnreadable" if exc.code == "sessionStateUnavailable" else
                                  "bridgeUnavailable")
            screen = None
            if tab is not None and reserve_reason != "seatNotRunning":
                try:
                    screen = read_claude_input_screen(tab, self.bridge)
                except BridgeError:
                    if state in {"idle", "working"} or reserve_reason == "statusUnreadable":
                        state, reserve_reason = "blocked", "statusUnreadable"
                else:
                    _guard_selection(tab, self.bridge, screen)
                    if ((state in {"idle", "working"} or reserve_reason == "statusUnreadable")
                            and not claude_input_line_visible(screen)):
                        # Only a shell prompt at the bottom says Claude has exited; any other
                        # screen without the input line (a dialog drawn before the status
                        # caught up, a start-up screen) is a seat that could not be read.
                        state = "blocked"
                        reserve_reason = ("seatNotRunning" if shell_prompt_visible(screen)
                                          else "statusUnreadable")
            if state == "blocked" or tab is None:
                reserve_reason = reserve_reason or "statusUnreadable"
                record = self.reserve(session_id, text, reserve_reason=reserve_reason)
                return {"result": "reserved", "id": record["id"], "seat": "busy",
                        "reason": reserve_reason}
            delivered, reason, _screen = deliver_to_tab(
                tab,
                text,
                self.bridge,
                expectation_args=expectations,
                input_screen=screen,
            )
            if delivered:
                result = {"result": "delivered", "seat": state}
                if _delivered_picker(tab, self.bridge, _screen):
                    result["opened"] = "picker"
                return result
            reason = reason or "delivery failed"
            code = {
                "bridgeUnavailable": "bridgeUnavailable",
                "enter delivery was not confirmed": "enterUnconfirmed",
                "current input line was not identified": "promptUnidentified",
                "sent text remains in the input prompt": "textRemains",
            }.get(reason, "deliveryFailed")
            return {"result": "failed", "reason": reason, "code": code, "seat": state}

    def submit_terminal(
        self,
        terminal_session_id: str,
        text: str,
        launch_kinds_by_tab: dict[str, str],
    ) -> dict[str, Any]:
        with self._session_lock(terminal_session_id):
            try:
                tab = resolve_terminal_tab(terminal_session_id, self.bridge)
            except BridgeError as exc:
                if exc.code == "sessionNotAllowed":
                    raise ReservationError(403, "sendUnavailableShell") from exc
                return {"result": "failed", "reason": "bridgeUnavailable"}
            if tab_agent(tab, launch_kinds_by_tab) != "codex":
                raise ReservationError(403, "sendUnavailableShell")
            now = time.monotonic()
            with self._lock:
                last_send = self._terminal_send_times.get(terminal_session_id)
                if (
                    last_send is not None
                    and now - last_send < TERMINAL_SEND_INTERVAL_SECONDS
                ):
                    raise ReservationError(429, "termRateLimited")
                self._terminal_send_times = {
                    key: timestamp
                    for key, timestamp in self._terminal_send_times.items()
                    if now - timestamp < TERMINAL_SEND_INTERVAL_SECONDS
                }
                self._terminal_send_times[terminal_session_id] = now
            try:
                current_tab, ready = self._terminal_ready(tab, launch_kinds_by_tab)
            except BridgeError as exc:
                if exc.code == "sessionNotAllowed":
                    raise ReservationError(403, "sendUnavailableShell") from exc
                if exc.code == "codexRolloutUnavailable":
                    raise ReservationError(403, exc.code) from exc
                return {"result": "failed", "reason": "bridgeUnavailable"}
            if not ready:
                record = self.reserve(terminal_session_id, text, terminal_tab=current_tab)
                return {"result": "reserved", "id": record["id"], "seat": "busy"}
            delivered, reason, _screen = self._deliver_terminal(current_tab, text)
            if delivered:
                return {"result": "delivered"}
            return {"result": "failed", "reason": reason or "delivery failed"}

    def _terminal_ready(self, tab, launch_kinds_by_tab):
        session_id = tab["sessionId"]
        current = resolve_terminal_tab(session_id, self.bridge)
        if (current.get("id") != tab.get("id")
                or tab_agent(current, launch_kinds_by_tab) != "codex"
                or (tab.get("agentSessionId") and
                    current.get("agentSessionId") != tab["agentSessionId"])):
            raise BridgeError("sessionNotAllowed")
        state = codex_rollout_state(current.get("agentSessionId"))
        if state is None:
            raise BridgeError("codexRolloutUnavailable")
        current["_codexBaseline"] = state
        if not state["idle"]:
            return current, False
        screen = self.bridge("read", "--session", session_id, "--lines", "20")
        if codex_screen_blocks_send(screen, session_id):
            return current, False
        # Reading the optional screen is a race window. Recheck both identity
        # and the append-only rollout before the one body/Enter transaction.
        latest = resolve_terminal_tab(session_id, self.bridge)
        if latest.get("id") != current.get("id") or latest.get("agentSessionId") != current.get("agentSessionId") or tab_agent(latest, launch_kinds_by_tab) != "codex":
            raise BridgeError("sessionNotAllowed")
        state = codex_rollout_state(current.get("agentSessionId"))
        current["_codexBaseline"] = state
        return current, bool(state and state["idle"])

    def _deliver_terminal(self, tab, text):
        before = tab["_codexBaseline"]
        digest = hashlib.sha256(normalize_send_text(text).strip().encode("utf-8")).hexdigest()
        try:
            # The body is never replayed, including target_unmounted and an
            # ambiguous CLI error. A later matching rollout record is the proof.
            self.bridge("send", "--session", tab["sessionId"],
                        f"--text=\x1b[200~{text}\x1b[201~", "--key", "enter")
        except BridgeError:
            pass
        if self._codex_receipt(tab, before, digest):
            return True, None, None
        # Under load the body can land while its Enter is lost, leaving the text
        # unsent in the composer (seen 2026-09-13). Press Enter once more - never
        # the body - and only while the screen shows this text still waiting.
        try:
            screen = self.bridge("read", "--session", tab["sessionId"], "--lines", "40")
        except BridgeError:
            screen = None
        if codex_composer_holds(screen, tab["sessionId"], text):
            try:
                self.bridge("send", "--session", tab["sessionId"], "--key", "enter")
            except BridgeError:
                pass
            if self._codex_receipt(tab, before, digest):
                return True, None, None
        return False, "codexDeliveryUnconfirmed", None

    def _codex_receipt(self, tab, before, digest) -> bool:
        for attempt in range(80):
            if attempt:
                self.receipt_sleeper(0.25)
            state = codex_rollout_state(tab.get("agentSessionId"))
            if state and state["identity"] == before["identity"] and state["path"] == before["path"]:
                if any(offset >= before["size"] and value == digest for offset, value in state["users"]):
                    return True
        return False

    def _record_wait(self, reservation_id: str, reason: str) -> None:
        now = self.wait_clock()
        with self._lock:
            current = next((row for row in self._records if row.get("id") == reservation_id), None)
            if current is None or current.get("status") != "pending":
                return
            last = self._wait_check_times.get(reservation_id)
            if current.get("reserveReason") == reason and last is not None and now - last < 60:
                return
            stamp = _utc_now()
            current.update(reserveReason=reason, checkedAt=stamp, updatedAt=stamp)
            self._write_locked()
            self._wait_check_times[reservation_id] = now

    def _fail_pending(self, reservation_id: str, reason: str) -> dict[str, Any] | None:
        with self._lock:
            current = next((row for row in self._records if row.get("id") == reservation_id), None)
            if current is None or current.get("status") != "pending":
                return None
            current.update(status="failed", reason=reason, updatedAt=_utc_now())
            self._write_locked()
            self._stale_observations.pop(reservation_id, None)
            self._wait_check_times.pop(reservation_id, None)
            return dict(current)

    def _rebind_stale_reservation(self, tab, tabs, *, include_current: bool = False):
        live = {}
        for candidate in tabs:
            if ((not include_current and candidate.get("id") == tab.get("id"))
                    or _claude_session_id(candidate) != _claude_session_id(tab)
                    or not _bridge_identifier(candidate.get("id"))
                    or not _bridge_identifier(candidate.get("sessionId"))
                    or candidate.get("processStatusReason") == "no_live_pty_session"):
                continue
            try:
                status = self.bridge("status", "--session", candidate["sessionId"])
            except BridgeError as exc:
                if exc.code == "sessionStateUnavailable":
                    continue
                raise
            sessions = status.get("sessions") if isinstance(status, dict) else None
            if not isinstance(sessions, list) or len(sessions) != 1:
                continue
            entry = sessions[0]
            view = entry.get("view") if isinstance(entry, dict) else None
            if (isinstance(entry, dict) and entry.get("session_id") == candidate["sessionId"]
                    and isinstance(view, dict) and view.get("lifecycle") == "alive"):
                live[candidate["id"]] = candidate
        if len(live) == 1:
            return next(iter(live.values())), None
        return None, ("seat ambiguous (stale tab)" if live else "seat not running (stale tab)")

    def worker_once(self) -> list[dict[str, Any]]:
        with self._lock:
            pending = [
                dict(row)
                for row in self._records
                if row.get("status") == "pending"
                and isinstance(row.get("id"), str)
                and row["id"] not in self._inflight
            ]
        with self._lock:
            pending_ids = {row["id"] for row in pending}
            self._stale_observations = {
                key: value for key, value in self._stale_observations.items() if key in pending_ids
            }
            self._wait_check_times = {
                key: value for key, value in self._wait_check_times.items() if key in pending_ids
            }
        updates = []
        for snapshot in pending:
            reservation_id = snapshot["id"]
            session_id = str(snapshot.get("sessionId", ""))
            is_terminal = snapshot.get("targetKind") == "term"
            with self._session_lock(session_id):
                try:
                    if is_terminal:
                        target = {"sessionId": session_id, "id": snapshot.get("tabId"),
                                  "agentSessionId": snapshot.get("agentSessionId")}
                        tab, is_ready = self._terminal_ready(target, {})
                        expectations = []
                    else:
                        if not SESSION_ID_RE.fullmatch(session_id):
                            raise BridgeError("sessionNotAllowed")
                        tabs = _reservation_tabs(self.bridge("panes", "--all"))
                        candidates = [row for row in tabs if _claude_session_id(row) == session_id]
                        if not candidates:
                            raise BridgeError("sessionNotAllowed")
                        tab = candidates[0]
                        # Duplicate Claude ids always require a unique live target.
                        stale = (len(candidates) > 1
                                 or tab.get("processStatusReason") == "no_live_pty_session")
                        if not stale:
                            try:
                                is_ready, expectations, screen, wait_reason = reservation_delivery_gate(
                                    tab, self.bridge)
                            except BridgeError as exc:
                                if exc.code != "sessionStateUnavailable":
                                    raise
                                stale = True
                        if stale:
                            tab, stale_reason = self._rebind_stale_reservation(
                                tab, tabs, include_current=len(candidates) > 1)
                            if tab is None:
                                count = self._stale_observations.get(reservation_id, 0) + 1
                                self._stale_observations[reservation_id] = count
                                if count >= 3:
                                    failed = self._fail_pending(reservation_id, stale_reason)
                                    if failed is not None:
                                        updates.append(failed)
                                else:
                                    self._record_wait(reservation_id, "seatNotRunning")
                                continue
                            is_ready, expectations, screen, wait_reason = reservation_delivery_gate(
                                tab, self.bridge)
                        self._stale_observations.pop(reservation_id, None)
                except BridgeError as exc:
                    if not is_terminal:
                        self._stale_observations.pop(reservation_id, None)
                    if exc.code != "sessionNotAllowed":
                        if not is_terminal:
                            self._record_wait(reservation_id, "bridgeUnavailable")
                        continue
                    failed = self._fail_pending(reservation_id, "target gone")
                    if failed is not None:
                        updates.append(failed)
                    continue
                if not is_ready:
                    if not is_terminal:
                        self._record_wait(reservation_id, wait_reason or "statusUnreadable")
                    continue
                if not is_terminal:
                    try:
                        screen, expectations = _ensure_composer_insert(
                            tab, self.bridge, screen, expectations)
                    except SelectionError as exc:
                        self._record_wait(
                            reservation_id,
                            "questionOpen" if exc.reason == "selectionOpen" else "statusUnreadable",
                        )
                        continue
                    except BridgeError:
                        self._record_wait(reservation_id, "bridgeUnavailable")
                        continue
                with self._lock:
                    current = next(
                        (row for row in self._records if row.get("id") == reservation_id),
                        None,
                    )
                    if current is None or current.get("status") != "pending":
                        continue
                    current["status"] = "delivering"
                    current["claimId"] = uuid.uuid4().hex
                    current["updatedAt"] = _utc_now()
                    self._inflight.add(reservation_id)
                    self._write_locked()
                if snapshot.get("targetKind") == "term":
                    delivered, reason, _screen = self._deliver_terminal(tab, str(snapshot.get("text", "")))
                else:
                    delivered, reason, _screen = deliver_to_tab(
                        tab, str(snapshot.get("text", "")), self.bridge,
                        expectation_args=expectations, input_screen=screen,
                    )
                with self._lock:
                    self._inflight.discard(reservation_id)
                    current = next(
                        (row for row in self._records if row.get("id") == reservation_id),
                        None,
                    )
                    if current is None or current.get("status") != "delivering":
                        continue
                    current["status"] = "delivered" if delivered else "failed"
                    current["updatedAt"] = _utc_now()
                    if delivered:
                        current.pop("reason", None)
                    else:
                        current["reason"] = reason or "delivery failed"
                    self._write_locked()
                    updates.append(dict(current))
        self.cleanup()
        return updates

    def _worker_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.worker_once()
            except Exception:
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._machine_lock.close()


def _transcript_last_line(
    session_id: str, path: Path, size: int, mtime: int, agent: str = "claude",
) -> dict[str, Any] | None:
    # A live seat's file changes between two home-screen refreshes, so its
    # cached line is stale for both, and both would read the tail. The second
    # waits for the first instead (2026-09-15: 26 such reads at once).
    key = (session_id, os.fspath(path), size, mtime, agent)
    return SINGLE_FLIGHT.run(
        ("tail", key), lambda: _transcript_last_line_cached(session_id, path, size, mtime, agent),
    )


@lru_cache(maxsize=256)
def _transcript_last_line_cached(
    session_id: str, path: Path, size: int, mtime: int, agent: str = "claude",
) -> dict[str, Any] | None:
    # Preserve the last fifty visible events, decoding only as far back as
    # the latest display line needs. Cache absence until the file changes.
    if agent == "claude":
        return _claude_line_and_turn(path)
    visible = 0
    for record in _last_line_records(path):
        event = _codex_conversation_event(record)
        if event is None:
            continue
        visible += 1
        if visible > 50:
            break
        if event.get("role") != "assistant":
            continue
        text = plain_snippet(_clean_user_text(event.get("text") or ""), limit=200)
        if not text:
            continue
        line = {"text": text}
        timestamp = _parse_timestamp(event.get("ts"))
        if timestamp is not None:
            line["at"] = int(timestamp * 1000)
        return line
    return None


def _last_line_records(path: Path):
    """Decode newest records lazily within the legacy bounded tail window."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        lower_bound = max(0, position - TRANSCRIPT_SCAN_BYTES)
        carry = b""
        while position > lower_bound:
            start = max(lower_bound, position - TRANSCRIPT_BLOCK_BYTES)
            handle.seek(start)
            payload = handle.read(position - start) + carry
            position = start
            if start > 0:
                newline = payload.find(b"\n")
                if newline < 0:
                    carry = payload
                    continue
                carry, payload = payload[:newline], payload[newline + 1:]
            else:
                carry = b""
            for raw_line in reversed(payload.splitlines()):
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    yield record


def _claude_line_and_turn(path: Path) -> dict[str, Any] | None:
    """Stop once the latest display line and raw turn are known."""
    line: dict[str, Any] = {}
    turn_seen = False
    visible = 0
    for record in _last_line_records(path):
        kind = record.get("type")
        message = record.get("message")
        if not turn_seen and kind in ("user", "assistant") and isinstance(message, dict):
            turn_seen = True
            reason = message.get("stop_reason")
            if kind == "user":
                turn = "in_turn"
            elif reason in ("end_turn", "stop_sequence"):
                turn = "ended"
            elif reason == "tool_use":
                blocks = message.get("content")
                turn = ("asking" if isinstance(blocks, list) and any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == "AskUserQuestion" for block in blocks
                ) else "in_turn")
            else:
                turn = None
            if turn is not None:
                line["turn"] = turn
                timestamp = _parse_timestamp(record.get("timestamp"))
                if timestamp is not None:
                    line["turnAt"] = int(timestamp * 1000)
        event = _conversation_event(record)
        if event is None:
            continue
        visible += 1
        if "text" not in line and event.get("role") == "assistant":
            text = plain_snippet(_clean_user_text(event.get("text") or ""), limit=200)
            if text:
                line["text"] = text
                timestamp = _parse_timestamp(event.get("ts"))
                if timestamp is not None:
                    line["at"] = int(timestamp * 1000)
        if "text" in line and turn_seen:
            break
        if visible >= 50:
            break
    return line or None


_transcript_last_line.cache_clear = _transcript_last_line_cached.cache_clear  # type: ignore[attr-defined]


def get_lines(bridge: Callable[..., Any] = cached_bridge,
              prepared_overview: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"lines": {}, "generatedAt": _health_timestamp()}
    try:
        overview = prepared_overview if prepared_overview is not None else overview_payload(bridge)
    except (BridgeError, ValueError, TypeError, KeyError):
        return result
    targets = {}
    for workspace in overview.get("workspaces", []):
        for pane in workspace.get("panes", []):
            for tab in pane.get("tabs", []):
                claude_id = tab.get("claudeSessionId")
                codex_id = tab.get("agentSessionId")
                terminal_id = tab.get("terminalSessionId")
                if _bridge_identifier(claude_id):
                    targets[claude_id] = (claude_id, "claude")
                elif (tab.get("agent") == "codex" and tab.get("linkable") is not False
                      and _bridge_identifier(codex_id) and _bridge_identifier(terminal_id)):
                    targets[f"term:{terminal_id}"] = (codex_id, "codex")
    for key, (session_id, agent) in targets.items():
        path = find_codex_transcript(session_id) if agent == "codex" else find_transcript(session_id)
        if path is None:
            continue
        try:
            info = path.stat()
            line = _transcript_last_line(session_id, path, info.st_size, info.st_mtime_ns, agent)
        except OSError:
            continue
        if line:
            result["lines"][key] = line
    return result


def get_overview(
    bridge: Callable[..., Any] = cached_bridge,
    launch_times_by_tab: dict[str, str] | None = None,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = _socket_source(bridge)
    bridge = _listing_bridge(bridge)
    workspace_payload = bridge("workspaces")
    workspaces = []
    for workspace in _workspace_rows(workspace_payload):
        workspace_id = workspace.get("id")
        if not isinstance(workspace_id, str):
            continue
        try:
            pane_payload = bridge("panes", "--workspace", workspace_id)
            panes = _pane_rows(pane_payload)
            layout = _layout_rows(pane_payload)
            tabs = [tab for pane in panes for tab in _tab_rows(pane)]
            working_count = sum(_tab_status(tab) == "working" for tab in tabs)
            counts = _workspace_counts(workspace, panes, pane_payload)
            display_tabs = _workspace_tab_rows(
                panes, launch_kinds_by_tab, launch_times_by_tab, include_list_metadata=True,
            )
            pane_rows = _layout_pane_rows(panes, layout, launch_kinds_by_tab)
            for row in pane_rows:
                children = [tab for tab in display_tabs if tab["paneId"] == row["paneId"]]
                row["tabs"] = children
                row["tabCount"] = len(children)
                active = next((tab for tab in children if tab["claudeSessionId"] == row["claudeSessionId"]), None)
                if active is not None and not active["linkable"]:
                    row["claudeSessionId"] = None
                    row["duplicateId"] = active["duplicateId"]
            workspaces.append(
                {
                    "id": workspace_id,
                    "name": workspace.get("name") or workspace_id,
                    "paneCount": len(panes),
                    "tabCount": len(tabs),
                    "status": _worst_status(tabs),
                    "attentionCount": sum(_tab_status(tab) == "attention" for tab in tabs),
                    "bridgeCounts": counts,
                    "workingCount": working_count,
                    "layout": layout,
                    "panes": pane_rows,
                }
            )
        except (BridgeError, ValueError, TypeError, KeyError):
            workspaces.append({
                "id": workspace_id, "name": workspace.get("name") or workspace_id,
                "paneCount": 0, "tabCount": 0, "status": "unobserved",
                "attentionCount": 0, "workingCount": 0, "panes": [],
                "layout": _layout_rows({}), "bridgeCounts": _workspace_counts(workspace, []),
                "error": "workspaceUnavailable",
            })
    if source is not None:
        source.workspace_errors = [{"id": row["id"], "name": row["name"]}
                                   for row in workspaces if row.get("error")]
    workspace_ids = {workspace["id"] for workspace in workspaces}
    active_workspace_id = (
        workspace_payload.get("activeWorkspaceId")
        if isinstance(workspace_payload, dict)
        else None
    )
    if active_workspace_id not in workspace_ids:
        active_workspace_id = None
    return {
        "workspaces": workspaces,
        "bridgeCounts": {
            "w": _source_count(workspace_payload.get("workspaceCount"), len(workspace_payload["workspaces"])),
            "p": _source_count(workspace_payload.get("paneCount"), sum(row["bridgeCounts"]["p"] for row in workspaces)),
            "t": _source_count(workspace_payload.get("tabCount"), sum(row["bridgeCounts"]["t"] for row in workspaces)),
        },
        "activeWorkspaceId": active_workspace_id,
        "mailUnreplied": mail_unreplied_count(),
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _bridge_owner_id(bridge: Callable[..., Any]) -> int:
    # A bound method (server.cached_bridge) is a fresh object each access, so
    # its own id is useless as a key; the instance it is bound to is stable.
    return id(getattr(bridge, "__self__", bridge))


def overview_payload(
    bridge: Callable[..., Any] = cached_bridge,
    launch_times_by_tab: dict[str, str] | None = None,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Generate every row before sharing it; ledger snapshots are part of the key."""
    if launch_times_by_tab is None and launch_kinds_by_tab is None:
        return SINGLE_FLIGHT.run(("overview", _bridge_owner_id(bridge)), lambda: get_overview(bridge))
    times = dict(launch_times_by_tab or {})
    kinds = dict(launch_kinds_by_tab or {})
    key = ("overview", _bridge_owner_id(bridge), tuple(sorted(times.items())), tuple(sorted(kinds.items())))
    return SINGLE_FLIGHT.run(key, lambda: get_overview(bridge, times, kinds))


def lines_payload(bridge: Callable[..., Any] = cached_bridge) -> dict[str, Any]:
    """get_lines, computed once for every caller that arrives while it runs."""
    return SINGLE_FLIGHT.run(("lines", _bridge_owner_id(bridge)), lambda: get_lines(bridge))


#: Keys that change on every call without the content changing. Left out of
#: the tag so a screen that has what the server has is told so with a 304.
ETAG_VOLATILE_KEYS = frozenset({"generatedAt"})


def payload_etag(payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key not in ETAG_VOLATILE_KEYS}
    digest = hashlib.sha256(
        json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f'"{digest[:32]}"'


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag or candidate == "*":
            return True
    return False


def get_workspace_talks(
    workspace_id: str,
    bridge: Callable[..., Any] = cached_bridge,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    bridge = _listing_bridge(bridge)
    workspace = next(
        (
            row
            for row in _workspace_rows(bridge("workspaces"))
            if row.get("id") == workspace_id
        ),
        None,
    )
    if workspace is None:
        raise BridgeError("workspaceNotFound")
    panes = _pane_rows(bridge("panes", "--workspace", workspace_id))
    rows = _workspace_tab_rows(panes, launch_kinds_by_tab)
    talks = []
    for row in rows:
        session_id = row["claudeSessionId"]
        if session_id is None or row["kind"] == "web":
            continue
        events = transcript_events(session_id, limit=20) or []
        latest_text = next((event["text"] for event in reversed(events) if event.get("text")), "")
        talks.append({
            **row, "subtitle": _talk_subtitle(session_id),
            "snippet": plain_snippet(latest_text, limit=240),
            "tabCount": len(_tab_rows(panes[row["paneIndex"]])),
            "tabs": [tab for tab in rows if tab["paneIndex"] == row["paneIndex"]],
        })
    return talks


def get_workspace_terminals(
    workspace_id: str,
    bridge: Callable[..., Any] = cached_bridge,
    launch_times_by_tab: dict[str, str] | None = None,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    bridge = _listing_bridge(bridge)
    workspace = next(
        (
            row
            for row in _workspace_rows(bridge("workspaces"))
            if row.get("id") == workspace_id
        ),
        None,
    )
    if workspace is None:
        raise BridgeError("workspaceNotFound")
    launch_times = launch_times_by_tab or {}
    terminals = []
    panes = _pane_rows(bridge("panes", "--workspace", workspace_id))
    for tab in _unambiguous_tabs([tab for pane in panes for tab in _tab_rows(pane)]):
        declared = (tab.get("lifecycle") == "declared"
                    and _status_value(tab.get("type") or "terminal") == "terminal"
                    and _claude_session_id(tab) is None)
        if not declared and not _is_readable_terminal_tab(tab):
            continue
        terminal_session_id = tab.get("sessionId")
        tab_id = tab.get("id")
        terminals.append(
            {
                "terminalSessionId": terminal_session_id,
                "label": resolve_talk_title(tab, terminal_session_id),
                **_pane_session_fields(tab), "lifecycle": tab.get("lifecycle"),
                **({"linkable": False, "canSend": False} if declared else {}),
                **_observation_fields(tab),
                **({"duplicateId": True, "linkable": False} if tab["duplicateId"] else {}),
                "status": _tab_status(tab),
                "agent": tab_agent(tab, launch_kinds_by_tab),
                "startedAt": launch_times.get(tab_id)
                if isinstance(tab_id, str)
                else None,
            }
        )
    return terminals


def get_workspace_payload(
    workspace_id: str,
    bridge: Callable[..., Any] = cached_bridge,
    launch_times_by_tab: dict[str, str] | None = None,
    launch_kinds_by_tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    bridge = _listing_bridge(bridge)
    workspaces = _workspace_rows(bridge("workspaces"))
    workspace = next((row for row in workspaces if row.get("id") == workspace_id), None)
    if workspace is None:
        raise BridgeError("workspaceNotFound")
    snapshots = []
    def listing_bridge(*args):
        payload = bridge(*args)
        if args == ("panes", "--workspace", workspace_id):
            snapshots.append(payload)
        return payload
    talks = get_workspace_talks(workspace_id, listing_bridge, launch_kinds_by_tab)
    terminals = get_workspace_terminals(
        workspace_id, listing_bridge, launch_times_by_tab, launch_kinds_by_tab,
    )
    panes = _pane_rows(snapshots[0])
    rows = _workspace_tab_rows(panes, launch_kinds_by_tab)
    talks_by_index = {talk["desktopIndex"]: talk for talk in talks}
    for row in rows:
        talk = talks_by_index.get(row["desktopIndex"], {})
        row.update({key: talk[key] for key in ("subtitle", "snippet") if key in talk})
    return {
        "tabs": rows,
        "paneCount": len(panes),
        "panes": [{"paneIndex": index, "title": _layout_pane_title(pane, _active_tab(pane)),
                   "tabCount": len(_tab_rows(pane))} for index, pane in enumerate(panes)],
        "bridgeCounts": _workspace_counts(workspace, panes, snapshots[0]),
        "workspace": {
            "id": workspace_id,
            "name": str(workspace.get("name") or workspace_id),
        },
        "talks": talks,
        "terminals": terminals,
    }


def public_features(features: dict[str, bool] | None = None) -> dict[str, bool]:
    selected = dict(MACHINE["features"])
    if features is not None:
        selected.update(features)
    if any(selected.get(name, False) for name in ("mail", "research")):
        raise ValueError("Mail and research integrations are not included in this source preview")
    return selected


class PocketHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        reservation_service: ReservationService | None = None,
        mail_service: MailJobService | None = None,
        launch_service: LaunchService | None = None,
        hermes_client: "hermes_bridge.HermesClient | None" = None,
        research_service: ResearchJobService | None = None,
        max_concurrent_requests: int = HTTP_MAX_CONCURRENT,
        allowed_origins: set[str] | None = None,
        pad_enabled: bool = False,
        pad_state_dir: Path | None = None,
        pad: Any = None,
        features: dict[str, bool] | None = None,
        socket_client: MycmuxSocket | None = None,
        pair_state_dir: Path | None = None,
        tailscale_status: TailscaleStatusCache | None = None,
        activity_enabled: bool | None = None,
        dispatch_root: Path | None = None,
    ) -> None:
        self.features = public_features(features)
        owns_reservations = reservation_service is None
        owns_mail = mail_service is None and self.features.get("mail", False)
        self.reservations = reservation_service or ReservationService()
        self.research = research_service if self.features.get("research", False) else None
        try:
            self.mail = (mail_service or MailJobService()) if self.features.get("mail", False) else None
            super().__init__(server_address, handler_class)
        except BaseException:
            if owns_reservations:
                self.reservations.stop()
            if owns_mail and getattr(self, "mail", None) is not None:
                self.mail.stop()
            raise
        self.bridge = self.reservations.bridge
        self.agent_design = agent_design_catalog.AgentDesignCatalog(
            refresh_client=agent_design_catalog.RefreshClient(socket_client))
        self.socket_bridge = (SocketBridge(socket_client, self.bridge) if socket_client is not None
                              else SOCKET_BRIDGE if self.bridge is run_bridge else None)
        self.pc_status = create_pc_status(self)
        self.launcher = launch_service or LaunchService(bridge=self.bridge)
        # The harness room. An unconfigured machine gets a client that
        # answers hermesNotConfigured, so the route exists and says why
        # rather than 404ing as if the feature were unknown.
        self.hermes = hermes_client or hermes_bridge.HermesClient(
            hermes_bridge.HermesConfig.from_mapping(MACHINE.get("hermes")),
            images=hermes_images.HermesImageStore(HERMES_IMAGES_DIR))
        self._bridge_cache = TTLCache()
        self.csrf_token = secrets.token_urlsafe(32)
        local_port = self.server_port
        configured_origins = allowed_origins if allowed_origins is not None else {
            PUBLIC_ORIGIN,
            f"http://127.0.0.1:{local_port}",
            f"http://localhost:{local_port}",
        }
        self.allowed_origins = {origin for origin in configured_origins if origin}
        self.tailscale_status = tailscale_status or TAILSCALE_STATUS
        status = self.tailscale_status.get()
        if not tailscale_login(status):
            self.tailscale_status._log_failure()
        _log_server_error(
            "publicOrigin source=" + MACHINE["_publicOriginSource"]
            + " value=" + PUBLIC_ORIGIN
        )
        if MACHINE.get("_hubOriginError"):
            _log_server_error(MACHINE["_hubOriginError"])
        state_dir = pair_state_dir or getattr(
            self.reservations, "state_path", RESERVATION_PATH
        ).parent
        #: Published by hello and health so that mycmux can compare the folder
        #: it writes allowed_logins.json into with the folder the server it
        #: adopted actually reads, instead of trusting a health probe on 8994.
        self.state_dir = Path(state_dir).resolve()
        self.skill_shelf = skill_shelf.SkillShelf(
            self.state_dir, resolve_document=resolve_file_path, log=_log_server_error)
        self.skill_suggest = skill_suggest.SkillSuggest(
            self.state_dir, self.skill_shelf.snapshot, refresh=self.skill_shelf.refresh)
        _log_server_error("stateDir=" + os.fspath(self.state_dir))
        self.allowed_logins = AllowedLogins(state_dir / "allowed_logins.json")
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._talk_wait_guard = threading.Lock()
        self._talk_wait_sessions: dict[str, int] = {}
        self._listing_guard = threading.Lock()
        self._listing_cache: tuple[float, Any] | None = None
        self._pad_slots = threading.BoundedSemaphore(PAD_MAX_SOCKETS)
        self.pad = pad
        if self.pad is None and pad_enabled:
            created = None
            try:
                created = pad_service.PadService(
                    state_dir=pad_state_dir or STATE_DIR,
                    state_path=(pad_state_dir or STATE_DIR)
                    / pad_service.PAD_GENERATION_STATE_NAME,
                )
                created.start()
                self.pad = created
            except BaseException:
                if created is not None:
                    try:
                        created.stop()
                    except Exception:
                        pass
                self.server_close()
                raise

        try:
            self.devices = DeviceStore(state_dir / "devices.json")
        except BaseException:
            self.server_close()
            raise

        if self.socket_bridge is not None:
            if self.socket_bridge.feed.ident is not None:
                self.socket_bridge.feed = StatusFeedThread(
                    self.socket_bridge.client, self.socket_bridge.feed.log,
                )
            self.socket_bridge.feed.start()
        self.log_writer = AsyncRotatingLogs()
        self.activity_enabled = (self.bridge is run_bridge if activity_enabled is None
                                 else activity_enabled)
        self.seat_activity = seat_activity.SeatActivity(
            self.state_dir, dispatch_root or Path.home() / ".claude" / "dispatch",
            lambda: overview_payload(self.cached_bridge, self.launcher.launch_times_by_tab(),
                                     self.launcher.launch_kinds_by_tab()),
            lambda overview: get_lines(self.cached_bridge, overview),
            lambda session, agent: (find_codex_transcript(session) if agent == "codex"
                                    else find_transcript(session)),
            transcript_title_parts, first_request=session_first_request,
            log=_log_server_error,
        )
        if self.activity_enabled:
            self.seat_activity.start()

    def cached_bridge(self, *args: str) -> Any:
        if self.socket_bridge is not None:
            return self.socket_bridge(*args)
        key = tuple(args)
        return self._bridge_cache.get(key, lambda: self.bridge(*args))

    #: How long one pane listing serves every wait that ends around the same
    #: moment. Forty watchers timing out together must not be forty listings.
    LISTING_REUSE_SECONDS = 0.25

    def recent_listing(self) -> Any:
        """The pane listing, shared by waits that finish at the same time."""
        now = time.monotonic()
        with self._listing_guard:
            cached = self._listing_cache
        if cached is not None and cached[0] > now:
            return cached[1]

        def load() -> Any:
            value = self.bridge("panes", "--all")
            with self._listing_guard:
                self._listing_cache = (time.monotonic() + self.LISTING_REUSE_SECONDS, value)
            return value

        return SINGLE_FLIGHT.run(("listing", id(self)), load)

    def acquire_talk_wait(self, session_id: str) -> bool:
        # Held only for accounting, never while reading or waiting. The pool
        # is refused before the HTTP limit is, so HTTP_ORDINARY_CONCURRENT
        # slots stay free for the home screen and sends at saturation.
        with self._talk_wait_guard:
            count = self._talk_wait_sessions.get(session_id, 0)
            if count >= TALK_WAIT_PER_SESSION or sum(self._talk_wait_sessions.values()) >= TALK_WAIT_GLOBAL:
                return False
            self._talk_wait_sessions[session_id] = count + 1
            return True

    def release_talk_wait(self, session_id: str) -> None:
        with self._talk_wait_guard:
            count = self._talk_wait_sessions[session_id] - 1
            if count:
                self._talk_wait_sessions[session_id] = count
            else:
                del self._talk_wait_sessions[session_id]

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                received = b""
                while b"\r\n\r\n" not in received and len(received) < 8192:
                    chunk = request.recv(4096)
                    if not chunk:
                        break
                    received += chunk
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
                request.shutdown(1)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def server_close(self) -> None:
        pc = getattr(self, "pc_status", None)
        if pc is not None:
            pc.stop()
        suggest = getattr(self, "skill_suggest", None)
        if suggest is not None:
            suggest.stop()
        shelf = getattr(self, "skill_shelf", None)
        if shelf is not None:
            shelf.stop()
        activity = getattr(self, "seat_activity", None)
        if activity is not None:
            activity.stop()
        writer = getattr(self, "log_writer", None)
        if writer is not None:
            writer.close(timeout=2.0)
        devices = getattr(self, "devices", None)
        if devices is not None:
            devices.close()
        source = getattr(self, "socket_bridge", None)
        if source is not None:
            source.feed.stop()
            if source.feed.ident is not None:
                source.feed.join(timeout=1)
        self.reservations.stop()
        if self.mail is not None:
            self.mail.stop()
        if self.research is not None:
            self.research.stop()
        pad = getattr(self, "pad", None)
        pad_error: BaseException | None = None
        if pad is not None:
            try:
                pad.stop()
            except Exception as exc:
                pad_error = exc
        try:
            super().server_close()
        finally:
            if pad_error is not None:
                raise pad_error


def _client_gone(exc: BaseException) -> bool:
    """A peer closing a socket is not an API/server failure."""
    return isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)) or (
        isinstance(exc, OSError)
        and (getattr(exc, "winerror", None) in {10053, 10054} or exc.errno in {10053, 10054})
    )


def _access_logged(method: Callable[..., None]) -> Callable[..., None]:
    """Time one request and write its access line, whatever the method did."""

    def wrapper(self: "PocketHandler") -> None:
        started = time.monotonic()
        self._access_status = None
        self._access_bytes = 0
        try:
            method(self)
        except Exception as exc:
            if _client_gone(exc):
                self._access_status = 499
                self.close_connection = True
            else:
                self._access_status = 500
                if self.path.startswith("/pocket/api/"):
                    _log_server_error("api_unhandled %s %s" % (
                        access_route(unquote(urlsplit(self.path).path)),
                        " | ".join(traceback.format_exc().strip().splitlines()[-3:])))
                raise
        finally:
            try:
                path = unquote(urlsplit(self.path).path)
                seat_match = (re.fullmatch(r"/pocket/api/talk/([^/]+)(?:/wait)?", path)
                              if self.command == "GET" else None)
                record_access(
                    self.command, access_route(path),
                    self._access_status, (time.monotonic() - started) * 1000,
                    self._access_bytes, access_client(self.headers.get("User-Agent", "")),
                    seat=seat_match.group(1) if seat_match else None,
                    writer=getattr(self.server, "log_writer", None),
                )
            except Exception:
                pass

    wrapper.__name__ = method.__name__
    return wrapper


class PocketHandler(BaseHTTPRequestHandler):
    server_version = "PocketMycmux"
    sys_version = ""
    _access_status: int | None = None
    _access_bytes: int = 0

    def version_string(self) -> str:
        return self.server_version

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HTTP_HEADER_TIMEOUT_SECONDS)

    def send_response(self, code: int, message: str | None = None) -> None:
        self._access_status = code
        super().send_response(code, message)

    def send_header(self, keyword: str, value: str) -> None:
        if keyword.lower() == "content-length":
            try:
                self._access_bytes = int(value)
            except (TypeError, ValueError):
                pass
        super().send_header(keyword, value)

    #: API prefixes owned by a machine feature. Hiding the row in the UI is not
    #: enough: a direct call would otherwise run a Windows-only helper and fail
    #: somewhere deep instead of saying the feature is not available here.
    FEATURE_ROUTES = (
        ("mail", "/pocket/api/mail"),
        ("research", "/pocket/api/research"),
    )

    def _reject_disabled_feature(self, path: str) -> bool:
        features = getattr(self.server, "features", None) or {}
        for feature, prefix in self.FEATURE_ROUTES:
            if path != prefix and not path.startswith(prefix + "/"):
                continue
            if features.get(feature, True):
                return False
            self._send_json(
                503, {"error": "featureUnavailable", "feature": feature}
            )
            return True
        return False

    def parse_request(self) -> bool:
        started = time.monotonic()
        if not super().parse_request():
            return False
        try:
            path = unquote(urlsplit(self.path).path)
        except ValueError:
            self.send_error(400)
            return False
        if path != "/pocket" and not path.startswith("/pocket/"):
            return True
        # health used to skip this. mycmux's two callers pass either way (the
        # local probe is loopback with Host 127.0.0.1:8994, the certificate
        # warm-up goes through Serve with the owner's header), so the exception
        # only let a tailnet stranger read workspace names and bridge errors,
        # and made the Funnel-returns-403 guarantee untrue for one route.
        allowed = self._authorize_identity()
        if not allowed:
            try:
                record_access(
                    self.command, access_route(path), 403,
                    (time.monotonic() - started) * 1000,
                    self._access_bytes, access_client(self.headers.get("User-Agent", "")),
                    writer=getattr(self.server, "log_writer", None),
                    reject=self._identity_reject,
                )
            except Exception:
                pass
        return allowed

    def _origin_allowed(self, origin: str | None) -> bool:
        """Page origins this server accepts writes from.

        The stored set is fixed when the server starts. The pair page hands out
        the origin derived from this request's own Tailscale name, which can be
        a different (and more correct) host than the one that was readable at
        import, so that one is accepted as well - otherwise a PWA opened from
        the QR could read everything and write nothing.
        """
        if not origin or origin == "null":
            return False
        if origin in self.server.allowed_origins:
            return True
        live = public_origin_for(
            tailscale_machine(getattr(self, "_identity_status", None) or {})
        )
        return live is not None and origin == live

    def _authorize_identity(self) -> bool:
        logins = self.headers.get_all("Tailscale-User-Login", [])
        login = logins[0] if logins else None
        status = self.server.tailscale_status.get()
        own_login = tailscale_login(status)
        allowed_logins = self.server.allowed_logins.get()
        if logins:
            key = login.casefold() if login else login
            allowed = len(logins) == 1 and bool(key) and (
                (own_login is not None and key == own_login.casefold())
                or key in allowed_logins
            )
        else:
            # Do not use forwarding headers as proof of a local TCP peer.
            hosts = self.headers.get_all("Host", [])
            local_host = len(hosts) == 1 and re.fullmatch(
                r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?",
                hosts[0], re.IGNORECASE,
            ) is not None
            allowed = self.client_address[0] in {"127.0.0.1", "::1"} and local_host
        if not allowed:
            self._identity_reject = "identityUnavailable" if own_login is None else "notAllowed"
            self._send_json(403, {"error": "notAllowed", "you": {"login": login}})
            return False
        self._identity_status = status
        self._identity_login = login
        self._identity_via = "serve" if "Tailscale-Headers-Info" in self.headers else "loopback"
        device = self.headers.get("X-Pocket-Device")
        if device:
            self.server.devices.record(
                device, login, self.headers.get("User-Agent", ""),
                self.headers.get("X-Forwarded-For", ""),
            )
        return True

    @_access_logged
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/pocket/pair":
            self._serve_pair(parse_qs(parsed.query, keep_blank_values=True))
            return
        if path == PAD_SOCKET_PATH and ws_frame.is_websocket_upgrade(self.headers):
            self._serve_pad_socket()
            return
        if path.startswith("/pocket/api/"):
            if self._reject_disabled_feature(path):
                return
            self._serve_api(path, parse_qs(parsed.query))
            return
        self._serve_static(path)

    @_access_logged
    def do_POST(self) -> None:
        path = unquote(urlsplit(self.path).path)
        match = re.fullmatch(r"/pocket/api/(talk|term)/.*/send", path)
        route = f"/pocket/api/{match[1]}/*/send" if match else path
        observed = bool(match) or path in {"/pocket/api/upload", "/pocket/api/launch"}
        started = time.monotonic()
        self._send_started = started
        build = self.headers.get("X-Pocket-Build", "")
        self._send_observation = dict(
            time=_utc_now(), route=route, seatKind=match[1] if match else path.rsplit("/", 1)[-1],
            characters=None, lines=None, normalizedCharacters=None,
            hasImage=path == "/pocket/api/upload", imageBytes=None,
            result="requestInterrupted", status=None,
            clientBuild=build if BUILD_ID_RE.fullmatch(build) else "unknown",
            ua=re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", self.headers.get("User-Agent", ""))[:120],
        ) if observed else None
        if re.fullmatch(r"/pocket/api/talk/[^/]+/ask", path):
            # An answer is logged as route, result and time taken; never what
            # was chosen, nor the question it was chosen for.
            self._send_observation = dict(time=_utc_now(), route=ASK_ROUTE,
                                          result="requestInterrupted", status=None)
        try:
            self._post_request()
        finally:
            if self._send_observation is not None:
                self._send_observation["durationMs"] = round((time.monotonic() - started) * 1000, 2)
                record_send_attempt(self._send_observation)
            self._send_observation = None

    def _observe_send_payload(self, payload):
        record = getattr(self, "_send_observation", None)
        if record is None or record.get("route") == ASK_ROUTE:
            return
        text = payload.get("text", "")
        if isinstance(text, str):
            normalized = normalize_send_text(text)
            client_count = payload.get("normalizedCharacters", 0)
            if type(client_count) is not int or not 0 <= client_count <= 65536:
                client_count = 0
            record.update(characters=len(normalized), lines=normalized.count("\n") + bool(normalized),
                          normalizedCharacters=send_normalization_count(text) + client_count)
            record["hasImage"], record["imageBytes"] = send_image_bytes(normalized)
        if record["route"] == "/pocket/api/upload":
            data = payload.get("dataBase64")
            record["hasImage"] = True
            record["imageBytes"] = max(0, len(data) // 4 * 3 - (len(data) - len(data.rstrip("=")))) if isinstance(data, str) and len(data) % 4 == 0 else None
            kind = payload.get("seatKind")
            record["seatKind"] = kind if isinstance(kind, str) and kind in {"talk", "term", "launch"} else "unknown"

    def _post_request(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/pocket/api/fault":
            # No token on this one. The request that fetches the token is
            # itself something that can fail, and a phone that cannot get a
            # token is exactly the phone whose trouble needs writing down.
            # Same origin is still required, so only this app can write here.
            self._serve_fault_report()
            return
        video_stream = path == "/pocket/api/upload/stream"
        if not self._authorize_mutation(video_stream=video_stream):
            return
        if self._reject_disabled_feature(path):
            return
        if path in {"/pocket/api/agent-design/close", "/pocket/api/agent-design/refresh"}:
            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            status, result = self.server.agent_design.handle("POST", path.rsplit("/", 1)[-1], payload)
            self._send_json(status, result)
            return
        if path == "/pocket/api/skills/suggest":
            try:
                payload = self._read_json_body() if self.headers.get("Content-Length", "0") != "0" else {}
                status, result = self.server.skill_suggest.start(payload.get("ids"), payload.get("redo", False))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except skill_suggest.SuggestError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            self._send_json(status, result)
            return
        if path == "/pocket/api/seat/role":
            try:
                payload = self._read_json_body()
                if "role" not in payload:
                    raise ValueError("seatRoleInvalid")
                self.server.seat_activity.set_role(payload.get("tabId"), payload["role"])
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(500, {"error": "seatRoleSaveFailed"})
                return
            self._send_json(200, {"tabId": payload["tabId"], "role": payload["role"]})
            return
        if path.startswith("/pocket/api/pad/"):
            self._serve_pad_post(path)
            return
        if path == "/pocket/api/upload/stream":
            # A rejected body may still be arriving. Never interpret its bytes
            # as a second request on this connection.
            self.close_connection = True
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if not lengths:
                    raise upload_service.UploadError(411, "lengthRequired")
                if len(lengths) != 1 or len(lengths[0]) > 20 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise upload_service.UploadError(400, "invalidLength")
                length = int(lengths[0])
                if length > upload_service.MAX_VIDEO_BYTES:
                    raise upload_service.UploadError(413, "tooLarge")
                if self.headers.get("Transfer-Encoding") is not None:
                    raise upload_service.UploadError(400, "invalidLength")
                self.connection.settimeout(max(30, HTTP_BODY_TIMEOUT_SECONDS))
                result = upload_service.handle_video_upload(
                    self.rfile, length, content_type=self.headers.get_content_type().lower(),
                    root=UPLOAD_ROOT, now=datetime.now(timezone.utc),
                )
            except upload_service.UploadError as exc:
                self._send_json(exc.status, {"error": exc.reason})
                return
            self._send_json(200, result)
            return
        if path == "/pocket/api/upload":
            try:
                payload = self._read_upload_json_body()
                result = upload_service.handle_upload(
                    payload,
                    root=UPLOAD_ROOT,
                    now=datetime.now(timezone.utc),
                )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except upload_service.UploadError as exc:
                self._send_json(exc.status, {"error": exc.reason})
                return
            self._send_json(200, result)
            return
        if path == "/pocket/api/research":
            try:
                payload = self._read_json_body()
                if self.server.research is None:
                    raise ResearchError(503, "launchFailed")
                job = self.server.research.enqueue(
                    payload.get("query"),
                    payload.get("depth"),
                    payload.get("sourceJobId"),
                    payload.get("archive"),
                )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ResearchError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            self._send_json(200, {"job": job})
            return
        research_prefix = "/pocket/api/research/"
        resume_suffix = "/resume"
        if path.startswith(research_prefix) and path.endswith(resume_suffix):
            job_id = path[len(research_prefix) : -len(resume_suffix)]
            try:
                payload = self._read_json_body()
                if self.server.research is None:
                    raise ResearchError(503, "launchFailed")
                job = self.server.research.resume(job_id, payload.get("mode"))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ResearchError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            self._send_json(200, {"job": job})
            return
        if path == "/pocket/api/quota/login":
            try:
                import quota_login
                payload = self._read_json_body()
                product = payload.get("product")
                if not isinstance(product, str):
                    raise ValueError("quotaSwitchFailed")
                quota_service.quota_payload(force=True)
                result = quota_login.LOGIN.start(product)
            except ValueError as exc:
                self._send_json(409 if str(exc) == "quotaLoginBusy" else 400, {"error": str(exc)})
                return
            except Exception:
                self._send_json(500, {"error": "quotaSwitchFailed"})
                return
            self._send_json(200, result)
            return
        if path == "/pocket/api/quota/switch":
            try:
                payload = self._read_json_body()
                product = payload.get("product")
                identity = payload.get("identity")
                if not isinstance(product, str) or not isinstance(identity, str):
                    raise ValueError("quotaSwitchFailed")
                from mycmux_account_api import AccountAPI, AccountAPIError
                try:
                    result = AccountAPI(_account_request(self.server.bridge)).mutate(payload)
                    quota_service.clear_cache()
                except AccountAPIError as exc:
                    self._send_json(exc.status, {"error": exc.code})
                    return
            except ValueError:
                self._send_json(400, {"error": "quotaSwitchFailed"})
                return
            except Exception:
                self._send_json(500, {"error": "quotaSwitchFailed"})
                return
            self._send_json(200, result)
            return
        if path == "/pocket/api/quota/reset-ticket":
            from mycmux_account_api import AccountAPI, AccountAPIError
            try:
                payload = self._read_json_body()
                result = AccountAPI(_account_request(self.server.bridge)).mutate(payload, reset=True)
                quota_service.clear_cache()
            except AccountAPIError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            except ValueError:
                self._send_json(400, {"error": "accountInvalid"})
                return
            self._send_json(200, result)
            return
        if path == "/pocket/api/hermes/threads":
            # A new conversation: what typing into the harness list means, the way
            # typing into a Slack DM rather than into a thread starts something.
            # Same body and the same guards as a reply; only the room is new.
            try:
                payload = self._read_upload_json_body()
            except upload_service.UploadError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                message = hermes_bridge.chat_message(payload)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._serve_hermes(lambda: self.server.hermes.start_room(message))
            return
        if path.startswith("/pocket/api/hermes/thread/") and path.endswith("/send"):
            session_id = path[len("/pocket/api/hermes/thread/"):-len("/send")]
            try:
                payload = self._read_upload_json_body()
            except upload_service.UploadError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                message = hermes_bridge.chat_message(payload)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._serve_hermes(lambda: self.server.hermes.start_turn(session_id, message))
            return
        if path.startswith("/pocket/api/hermes/turn/") and path.endswith("/approval"):
            # The phone's answer to "this command needs approval", as Slack's buttons
            # give it. The bridge checks the answer and that the turn is one of ours.
            turn_id = path[len("/pocket/api/hermes/turn/"):-len("/approval")]
            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if set(payload) - {"requestId", "choice"}:
                self._send_json(400, {"error": "invalid JSON body"})
                return
            self._serve_hermes(lambda: self.server.hermes.approve(
                turn_id, payload.get("requestId"), payload.get("choice")))
            return
        if path == "/pocket/api/metrics":
            # The phone's own timings. Numbers and short labels only; there
            # is no field for a message, and none is read if sent.
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length > METRICS_MAX_BODY_BYTES:
                self._send_json(413, {"error": "metricsTooLarge"})
                return
            try:
                rows = validate_metrics_payload(self._read_json_body())
            except ValueError as exc:
                reason = str(exc)
                self._send_json(400, {"error": reason if reason.startswith("metrics") else "metricsInvalid"})
                return
            accepted = record_metrics(rows, access_client(self.headers.get("User-Agent", "")),
                                      writer=self.server.log_writer)
            self._send_json(200, {"accepted": accepted})
            return
        if path == "/pocket/api/launch":
            try:
                payload = self._read_json_body()
                result = self.server.launcher.launch(payload)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except LaunchError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            except BridgeError:
                self._send_json(503, {"error": "launchFailed"})
                return
            except Exception:
                self._send_json(500, {"error": "launchFailed"})
                return
            self.server._bridge_cache.clear()
            self._send_json(200, result)
            return
        ask_prefix = "/pocket/api/talk/"
        if path.startswith(ask_prefix) and path.endswith("/picker/select"):
            self._serve_picker_select(path[len(ask_prefix):-len("/picker/select")])
            return
        if path.startswith(ask_prefix) and path.endswith("/ask"):
            self._serve_talk_ask_post(path[len(ask_prefix):-len("/ask")])
            return
        key_prefix = next(
            (prefix for prefix in ("/pocket/api/talk/", "/pocket/api/term/")
             if path.startswith(prefix) and path.endswith("/key")),
            None,
        )
        if key_prefix is not None:
            session_id = path[len(key_prefix) : -len("/key")]
            if not session_id or "/" in session_id:
                self._send_json(404, {"error": "not found"})
                return
            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            terminal = key_prefix == "/pocket/api/term/"
            key = payload.get("key")
            error = (validate_send_key(key) if terminal else
                     None if isinstance(key, str) and key else "keyNotAllowed")
            if error or "text" in payload:
                self._send_json(400, {"error": error or "keyNotAllowed"})
                return
            try:
                resolver = resolve_terminal_tab if terminal else resolve_tab
                tab = resolver(session_id, self.server.bridge)
            except BridgeError as exc:
                if exc.code == "sessionNotAllowed":
                    self._send_json(
                        403 if terminal else 404,
                        {"error": "sendUnavailableShell" if terminal else "sessionNotAllowed"},
                    )
                else:
                    self._send_json(503, {"error": "bridgeUnavailable"})
                return
            if not terminal:
                if tab_agent(tab, self.server.launcher.launch_kinds_by_tab()) != "claude":
                    self._send_json(404, {"error": "sessionNotAllowed"})
                    return
                self._serve_talk_key(tab, payload)
                return
            if tab_agent(tab, self.server.launcher.launch_kinds_by_tab()) != "codex":
                self._send_json(403, {"error": "sendUnavailableShell"})
                return
            try:
                snapshot = self.server.bridge("status", "--session", tab["sessionId"])
                # Reuse the expectation parser on this single status snapshot.
                # Disable its legacy idle-only screen fallback on a local copy.
                _, expectations = delivery_state(
                    {**tab, "agentStatus": "working", "agentStatusStale": False},
                    lambda *args: snapshot,
                )
                if snapshot["sessions"][0]["view"].get("lifecycle") != "alive":
                    self._send_json(409, {"error": "sessionNotAlive"})
                    return
                if not expectations:
                    self._send_json(409, {"error": "keyNotConfirmed"})
                    return
                if not terminal:
                    screen = read_claude_input_screen(tab, self.server.bridge)
                    if shell_prompt_visible(screen) and not claude_input_line_visible(screen):
                        self._send_json(409, {"error": "seatNotRunning"})
                        return
                result = self.server.bridge(
                    "send", "--session", tab["sessionId"], "--key", payload["key"],
                    *expectations,
                )
            except BridgeError:
                self._send_json(503, {"error": "bridgeUnavailable"})
                return
            if not isinstance(result, dict) or result.get("ok") is not True or result.get("confirmed") is not True:
                self._send_json(409, {"error": "keyNotConfirmed"})
                return
            self._send_json(200, {"sent": True, "key": payload["key"]})
            return
        term_prefix = "/pocket/api/term/"
        term_suffix = "/send"
        if path.startswith(term_prefix) and path.endswith(term_suffix):
            terminal_session_id = path[len(term_prefix) : -len(term_suffix)]
            if not terminal_session_id or "/" in terminal_session_id:
                self._send_json(404, {"error": "not found"})
                return
            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            text = normalize_send_text(payload.get("text"))
            error = validate_terminal_send_text(text)
            if error:
                self._send_json(400, {"error": error})
                return
            launch_kinds = self.server.launcher.launch_kinds_by_tab()
            try:
                tab = resolve_terminal_tab(terminal_session_id, self.server.bridge)
            except BridgeError as exc:
                if exc.code == "sessionNotAllowed":
                    self._send_json(403, {"error": "sendUnavailableShell"})
                else:
                    self._send_json(503, {"error": "bridgeUnavailable"})
                return
            if tab_agent(tab, launch_kinds) != "codex":
                self._send_json(403, {"error": "sendUnavailableShell"})
                return
            try:
                result = self.server.reservations.submit_terminal(
                    terminal_session_id,
                    text,
                    launch_kinds,
                )
            except ReservationError as exc:
                self._send_json(exc.status, {"error": exc.reason})
                return
            self._send_json(200, result)
            return
        if path == "/pocket/api/mail/hide":
            try:
                payload = self._read_json_body()
                thread_id = payload.get("threadId")
                if not isinstance(thread_id, str) or not thread_id:
                    raise MailError(400, "threadId is required")
                permanent = payload.get("permanent", False)
                if not isinstance(permanent, bool):
                    raise MailError(400, "permanent must be a boolean")
                self.server.mail.hide(thread_id, permanent=permanent)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except MailError as exc:
                self._send_json(exc.status, {"error": exc.reason})
                return
            self._send_json(200, {"hidden": True})
            return
        if path == "/pocket/api/mail/revise":
            try:
                payload = self._read_json_body()
                thread_id = payload.get("threadId")
                draft_id = payload.get("draftId")
                instruction = payload.get("instruction")
                if not isinstance(thread_id, str) or not thread_id:
                    raise MailError(400, "threadId is required")
                if not isinstance(draft_id, str) or not draft_id:
                    raise MailError(400, "draftId is required")
                error = validate_mail_instruction(instruction)
                if error:
                    raise MailError(400, error)
                result = self.server.mail.enqueue_revise(
                    thread_id,
                    draft_id,
                    instruction,
                )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except MailError as exc:
                self._send_json(
                    exc.status,
                    {"error": exc.reason, "checkFindings": exc.check_findings},
                )
                return
            self._send_json(200, result)
            return
        if path == "/pocket/api/mail/send":
            try:
                payload = self._read_json_body()
                thread_id = payload.get("threadId")
                draft_id = payload.get("draftId")
                if not isinstance(thread_id, str) or not thread_id:
                    raise MailError(400, "threadId is required")
                if not isinstance(draft_id, str) or not draft_id:
                    raise MailError(400, "draftId is required")
                sent_at = self.server.mail.send_draft(thread_id, draft_id)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except MailError as exc:
                self._send_json(exc.status, {"error": exc.reason})
                return
            self._send_json(200, {"sentAt": sent_at})
            return
        mail_prefix = "/pocket/api/mail/"
        mail_suffix = "/draft"
        if path.startswith(mail_prefix) and path.endswith(mail_suffix):
            thread_id = path[len(mail_prefix) : -len(mail_suffix)]
            if not thread_id or "/" in thread_id:
                self._send_json(404, {"error": "not found"})
                return
            try:
                payload = self._read_json_body()
                direction = payload.get("direction")
                error = validate_mail_direction(direction)
                if error:
                    raise MailError(400, error)
                result = self.server.mail.enqueue(thread_id, direction)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except MailError as exc:
                self._send_json(
                    exc.status,
                    {"error": exc.reason, "checkFindings": exc.check_findings},
                )
                return
            except ReservationError as exc:
                self._send_json(exc.status, {"error": exc.reason})
                return
            self._send_json(200, result)
            return
        close_prefix = "/pocket/api/talk/"
        close_suffix = "/close"
        if path.startswith(close_prefix) and path.endswith(close_suffix):
            session_id = path[len(close_prefix) : -len(close_suffix)]
            if not session_id or "/" in session_id:
                self._send_json(404, {"error": "not found"})
                return
            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            pane_id = payload.get("paneId")
            if not _bridge_identifier(pane_id):
                self._send_json(400, {"error": "paneIdRequired"})
                return
            request_id = uuid.uuid4().hex
            try:
                tab_id = payload.get("tabId")
                if not _bridge_identifier(tab_id):
                    self._send_json(400, {"error": "tabIdRequired"})
                    return
                result = close_talk_pane(session_id, pane_id, self.server.bridge, tab_id=tab_id)
            except BridgeError as exc:
                status = {
                    "sessionNotAllowed": 404,
                    "paneChanged": 409,
                    "tabNeedsConfirmation": 409,
                    "lastTabRefused": 409,
                    "paneCloseUnverified": 502,
                    "paneNotClosed": 502,
                }.get(exc.code, 503)
                if exc.code in {"paneCloseUnverified", "paneNotClosed"}:
                    # Tabs may have gone; nothing cached about them is true now.
                    self.server._bridge_cache.clear()
                _log_local(
                    "pane_close_refused",
                    f"session={session_id} pane={pane_id} code={exc.code}",
                    request_id,
                )
                self._send_json(status, {"error": exc.code})
                return
            self.server._bridge_cache.clear()
            _log_local(
                "pane_close",
                f"session={session_id} pane={pane_id} tabs={len(result['tabIds'])}",
                request_id,
            )
            self._send_json(200, result)
            return
        prefix = "/pocket/api/talk/"
        suffix = "/send"
        if not (path.startswith(prefix) and path.endswith(suffix)):
            self._send_json(404, {"error": "not found"})
            return
        session_id = path[len(prefix) : -len(suffix)]
        if not session_id or "/" in session_id:
            self._send_json(404, {"error": "not found"})
            return
        try:
            resolve_tab(session_id, self.server.bridge)
        except BridgeError as exc:
            if exc.code == "sessionNotAllowed":
                self._send_json(404, {"error": "sessionNotAllowed"})
            else:
                reason = exc.detail if exc.detail in {"bridgeBusy", "bridgeTimeout", "listingUnavailable"} else "bridgeUnavailable"
                self._send_json(503, {"error": "bridgeUnavailable", "reason": reason},
                                {"Retry-After": "5"})
            return
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        text = normalize_send_text(payload.get("text"))
        error = validate_send_text(text)
        if error:
            self._send_json(400, {"error": error})
            return
        try:
            result = self.server.reservations.submit(session_id, text)
        except ReservationError as exc:
            response = {"error": exc.reason}
            if isinstance(exc, SelectionError) and exc.kind is not None:
                response["kind"] = exc.kind
            if exc.status in {429, 507}:
                response.update(code="reservationQuota",
                                scope="session" if exc.status == 429 else "global")
            self._send_json(exc.status, response)
            return
        self._send_json(200, result)

    @_access_logged
    def do_DELETE(self) -> None:
        if not self._authorize_mutation():
            return
        path = unquote(urlsplit(self.path).path)
        if self._reject_disabled_feature(path):
            return
        if path.startswith("/pocket/api/pad/"):
            try:
                self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._serve_pad_delete(path)
            return
        research_prefix = "/pocket/api/research/"
        if path.startswith(research_prefix):
            job_id = path[len(research_prefix) :]
            try:
                if self.server.research is None:
                    raise ResearchError(503, "launchFailed")
                job = self.server.research.cancel(job_id)
            except ResearchError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            self._send_json(200, {"job": job})
            return
        try:
            self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        mail_hide_prefix = "/pocket/api/mail/hide/"
        if path.startswith(mail_hide_prefix) and len(path) > len(mail_hide_prefix):
            thread_id = path[len(mail_hide_prefix) :]
            if "/" in thread_id:
                self._send_json(404, {"error": "not found"})
                return
            try:
                self.server.mail.unhide(thread_id)
            except MailError as exc:
                self._send_json(exc.status, {"error": exc.reason})
                return
            self._send_json(200, {"hidden": False})
            return
        prefix = "/pocket/api/reservations/"
        if not path.startswith(prefix) or len(path) == len(prefix):
            self._send_json(404, {"error": "not found"})
            return
        reservation_id = path[len(prefix) :]
        if "/" in reservation_id:
            self._send_json(404, {"error": "not found"})
            return
        session_id = self.server.reservations.session_for_reservation(reservation_id)
        if session_id is not None:
            try:
                self.server.reservations.resolve_reservation_target(session_id, self.server.bridge)
            except BridgeError:
                self._send_json(404, {"error": "sessionNotAllowed"})
                return
        result = self.server.reservations.cancel(reservation_id)
        if result == "not_found":
            self._send_json(404, {"error": "reservation not found"})
        elif result == "not_pending":
            self._send_json(409, {"error": "reservation is not pending"})
        else:
            self._send_json(200, {"result": "canceled"})

    def _serve_talk_key(self, tab, payload):
        lock = _ask_session_lock(tab["sessionId"])
        if not lock.acquire(blocking=False):
            self._send_json(409, {"error": "askBusy"})
            return
        try:
            _, expectations = _selection_expectations(tab, self.server.bridge)
            screen = read_claude_input_screen(tab, self.server.bridge)
            if shell_prompt_visible(screen) and not claude_input_line_visible(screen):
                raise SelectionError("seatNotRunning")
            ask, picker = _screen_selection(tab, self.server.bridge, screen)
            key = payload["key"]
            if ask is not None:
                raise SelectionError("askOpen")
            if picker is not None:
                if "fingerprint" in payload and payload["fingerprint"] != picker["fingerprint"]:
                    raise SelectionError("pickerChanged")
                if key not in picker["keys"]:
                    raise SelectionError("keyNotOffered")
            elif key != "esc":
                raise SelectionError("keyNotOffered")
            else:
                if not picker_screen.is_working(screen["lines"]):
                    raise SelectionError("notWorking")
            confirmed = _send_selection_key(tab, self.server.bridge, key, expectations)
            _, after_picker = _read_selection_after(
                tab, self.server.bridge, screen, picker, require_change=not confirmed)
            self._send_json(200, {"sent": True, "key": key, "picker": after_picker})
        except SelectionError as exc:
            self._send_json(409, {"error": exc.reason})
        except BridgeError:
            self._send_json(503, {"error": "bridgeUnavailable"})
        finally:
            lock.release()

    def _serve_picker_select(self, session_id):
        if not session_id or "/" in session_id or not SESSION_ID_RE.fullmatch(session_id):
            self._send_json(404, {"error": "not found"})
            return
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        number, fingerprint = payload.get("number"), payload.get("fingerprint")
        if (type(number) is not int or not 1 <= number <= 999
                or not isinstance(fingerprint, str)
                or not ASK_FINGERPRINT_RE.fullmatch(fingerprint)):
            self._send_json(400, {"error": "pickerInvalid"})
            return
        try:
            tab = resolve_tab(session_id, self.server.bridge)
        except BridgeError as exc:
            self._send_json(404 if exc.code == "sessionNotAllowed" else 503,
                            {"error": exc.code if exc.code == "sessionNotAllowed" else "bridgeUnavailable"})
            return
        if tab_agent(tab, self.server.launcher.launch_kinds_by_tab()) != "claude":
            self._send_json(404, {"error": "sessionNotAllowed"})
            return
        lock = _ask_session_lock(tab["sessionId"])
        if not lock.acquire(blocking=False):
            self._send_json(409, {"error": "askBusy"})
            return
        try:
            screen = read_claude_input_screen(tab, self.server.bridge)
            _, picker = _screen_selection(tab, self.server.bridge, screen)
            if picker is None or picker["fingerprint"] != fingerprint:
                raise SelectionError("pickerChanged")
            title = picker["title"]
            for steps in range(41):
                cursors = [row["number"] for row in picker["rows"] if row["cursor"]]
                if len(cursors) != 1:
                    raise SelectionError("rowNotReached")
                current = cursors[0]
                if current == number:
                    self._send_json(200, {"moved": True, "picker": picker})
                    return
                if steps == 40:
                    raise SelectionError("rowNotReached")
                key = "down" if number > current else "up"
                if key not in picker["keys"]:
                    raise SelectionError("rowNotReached")
                _, expectations = _selection_expectations(tab, self.server.bridge)
                fresh_screen = read_claude_input_screen(tab, self.server.bridge)
                _, fresh_picker = _screen_selection(tab, self.server.bridge, fresh_screen)
                if fresh_picker is None or fresh_picker["fingerprint"] != picker["fingerprint"]:
                    raise SelectionError("pickerChanged")
                confirmed = _send_selection_key(tab, self.server.bridge, key, expectations)
                after_screen, after = _read_selection_after(
                    tab, self.server.bridge, fresh_screen, fresh_picker, require_change=not confirmed)
                if after is None or after["title"] != title:
                    raise SelectionError("rowNotReached")
                after_cursors = [row["number"] for row in after["rows"] if row["cursor"]]
                if (len(after_cursors) != 1 or after_cursors[0] == current
                        or (after_cursors[0] > current) != (key == "down")):
                    raise SelectionError("rowNotReached")
                screen, picker = after_screen, after
        except SelectionError as exc:
            self._send_json(409, {"error": exc.reason})
        except BridgeError:
            self._send_json(503, {"error": "bridgeUnavailable"})
        finally:
            lock.release()

    def _serve_talk_ask(self, session_id: str) -> None:
        """The AskUserQuestion on a Claude seat's screen, if one is showing."""
        if not SESSION_ID_RE.fullmatch(session_id):
            self._send_json(404, {"error": "sessionNotAllowed"})
            return
        unavailable = {"available": False, "ask": None, "fingerprint": None, "picker": None}
        support = ask_support()
        if support is None:
            self._send_json(200, unavailable)
            return
        try:
            tab = resolve_tab(session_id, self.server.bridge)
        except BridgeError as exc:
            if exc.code != "sessionNotAllowed":
                raise
            self._send_json(404, {"error": "sessionNotAllowed"})
            return
        if tab_agent(tab, self.server.launcher.launch_kinds_by_tab()) != "claude":
            self._send_json(200, unavailable)
            return
        screen = read_claude_input_screen(tab, self.server.bridge)
        ask, picker = _screen_selection(tab, self.server.bridge, screen, support)
        self._send_json(200, {"available": True, "ask": ask,
                              "fingerprint": support[1](ask) if ask is not None else None,
                              "picker": picker})

    def _serve_talk_ask_post(self, session_id: str) -> None:
        """Answer the question on screen with the bridge's answer-ask, once.

        Nothing is sent unless the screen still shows exactly the question the
        phone was shown (the fingerprint of the bridge's own parse).
        """
        if not session_id or "/" in session_id or not SESSION_ID_RE.fullmatch(session_id):
            self._send_json(404, {"error": "not found"})
            return
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        fingerprint = payload.get("fingerprint")
        if not isinstance(fingerprint, str) or not ASK_FINGERPRINT_RE.fullmatch(fingerprint):
            self._send_json(400, {"error": "askInvalid"})
            return
        support = ask_support()
        if support is None:
            self._send_json(503, {"error": "askUnavailable"})
            return
        try:
            tab = resolve_tab(session_id, self.server.bridge)
        except BridgeError as exc:
            if exc.code == "sessionNotAllowed":
                self._send_json(404, {"error": "sessionNotAllowed"})
            else:
                self._send_json(503, {"error": "bridgeUnavailable"})
            return
        if tab_agent(tab, self.server.launcher.launch_kinds_by_tab()) != "claude":
            self._send_json(404, {"error": "sessionNotAllowed"})
            return
        terminal_session_id = tab["sessionId"]
        lock = _ask_session_lock(terminal_session_id)
        if not lock.acquire(blocking=False):
            self._send_json(409, {"error": "askBusy"})
            return
        try:
            try:
                before = read_ask_screen(tab, self.server.bridge, support)
            except BridgeError:
                self._send_json(503, {"error": "bridgeUnavailable"})
                return
            if before is None or support[1](before) != fingerprint:
                self._send_json(409, {"error": "askChanged"})
                return
            answers = ask_bridge_answers(before, payload.get("answers"), payload.get("submit"))
            if answers is None:
                self._send_json(400, {"error": "askInvalid"})
                return
            kind, detail = run_answer_ask(terminal_session_id, answers)
            if kind == "observed_delivered":
                self._send_json(200, {"result": "delivered"})
                return
            if kind == "prompt_changed" and before.get("kind") == "review" and detail == ASK_REVIEW_MISMATCH:
                # Nothing was pressed: answer-ask checks the review before its
                # one key, and it cannot yet submit a review it did not reach.
                self._send_json(409, {"error": "askReviewUnsupported"})
                return
            if (kind == "prompt_changed" and before.get("kind") == "tabbed"
                    and (detail.startswith(ASK_NO_ANSWER_PREFIX) or detail == ASK_REVIEW_MISMATCH)):
                try:
                    after = read_ask_screen(tab, self.server.bridge, support)
                except BridgeError:
                    after = None
                if ask_advanced(before, after, support[1]):
                    self._send_json(200, {"result": "advanced"})
                    return
            if kind in {"prompt_changed", "stale_target"}:
                self._send_json(409, {"error": "askChanged"})
                return
            if kind == "notStarted":
                self._send_json(503, {"error": "askUnavailable"})
                return
            self._send_json(502, {"error": "askNotDelivered"})
        finally:
            lock.release()

    def _serve_talk_wait(self, session_id: str, query: dict[str, list[str]]) -> None:
        if not SESSION_ID_RE.fullmatch(session_id):
            self._send_json(404, {"error": "sessionNotAllowed"})
            return
        cursor_value = query.get("cursor", [None])[0]
        try:
            if cursor_value is not None and (
                not cursor_value.isascii() or not cursor_value.isdecimal()
            ):
                raise ValueError
            cursor = None if cursor_value is None else int(cursor_value)
        except ValueError:
            self._send_json(400, {"error": "invalid cursor"})
            return
        try:
            timeout = float(query.get("timeout", [str(TALK_WAIT_DEFAULT_TIMEOUT)])[0])
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError
        except ValueError:
            self._send_json(400, {"error": "invalid timeout"})
            return
        if not self.server.acquire_talk_wait(session_id):
            # The same empty shape the web reads, plus when to come back.
            self._send_json(200, {
                **empty_transcript_wait(),
                "activity": transcript_activity(session_id, now=datetime.now(timezone.utc)),
                "retryAfterMs": TALK_WAIT_RETRY_AFTER_MS[0] + secrets.randbelow(
                    TALK_WAIT_RETRY_AFTER_MS[1] - TALK_WAIT_RETRY_AFTER_MS[0] + 1),
            })
            return
        try:
            launch_kinds = self.server.launcher.launch_kinds_by_tab()
            # Decided once, before the sleep: a Claude transcript needs no
            # bridge; a Codex seat named by its terminal id costs one listing.
            source = resolve_wait_source(session_id, self.server.bridge, launch_kinds)
            activity_reader = lambda: (
                _unknown_activity() if source is not None and source["agent"] == "codex"
                else transcript_activity(session_id, now=datetime.now(timezone.utc))
            )
            response = wait_for_transcript(
                session_id, cursor, timeout, source=source, activity_reader=activity_reader,
            )
            response["activity"] = activity_reader()
            # The seat's state rides on the answer so the phone need not ask
            # for the whole snapshot again. One listing, after the wait.
            try:
                response["pane"] = wait_pane_view(session_id, self.server.recent_listing(), launch_kinds, self.server.bridge)
            except BridgeError:
                response["pane"] = None
        finally:
            self.server.release_talk_wait(session_id)
        self._send_json(200, response)

    def _serve_api(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path in {"/pocket/api/agent-design/catalog", "/pocket/api/agent-design/document"}:
                status, result = self.server.agent_design.handle("GET", path.rsplit("/", 1)[-1], query)
                self._send_json(status, result)
                return
            if path == "/pocket/api/health":
                self._send_json(200, get_health(self.server))
                return
            if path == "/pocket/api/pc":
                self._send_json_tagged(self.server.pc_status.snapshot())
                return
            if path == "/pocket/api/mac/status":
                self._send_json(200, get_mac_status(self.server))
                return
            if path == "/pocket/api/hello":
                machine = tailscale_machine(self._identity_status)
                # Same derivation as the pair page, so that one answer can
                # never name two machines (publicOrigin vs dnsName).
                machine["publicOrigin"] = public_origin_for(machine)
                features = dict(getattr(self.server, "features", None) or {})
                features.update(hub=True, files=True, pad=getattr(self.server, "pad", None) is not None)
                self._send_json(200, {
                    "app": "pocket-mycmux", "apiVersion": 1, "build": build_id(),
                    "machine": machine,
                    "you": {"login": self._identity_login, "allowed": True,
                            "via": self._identity_via},
                    "features": features, "hubOrigin": HUB_PUBLIC_ORIGIN,
                    "stateDir": state_dir_path(self.server),
                })
                return
            if path == "/pocket/api/devices":
                self._send_json(200, self.server.devices.list_devices())
                return
            if path == "/pocket/api/quota":
                from mycmux_account_api import AccountAPI, AccountAPIError
                try:
                    value = AccountAPI(_account_request(self.server.bridge)).usage()
                except AccountAPIError:
                    value = quota_service.quota_payload()
                self._send_json(200, value)
                return
            if path == "/pocket/api/quota/login":
                import quota_login
                self._send_json(200, quota_login.LOGIN.status())
                return
            if path == "/pocket/api/file/meta":
                raw_path = query.get("path", [""])[0]
                if not raw_path:
                    self._send_json(400, {"error": "fileNotFound"})
                    return
                metadata = run_file_operation(lambda: get_file_metadata(raw_path))
                self._send_json(200, metadata)
                return
            if path == "/pocket/api/file/roots":
                roots = run_file_operation(list_file_roots)
                self._send_json(200, roots)
                return
            if path == "/pocket/api/file/list":
                raw_path = query.get("path", [""])[0]
                if not raw_path:
                    self._send_json(400, {"error": "fileNotFound"})
                    return
                listing = run_file_operation(lambda: list_directory(raw_path))
                self._send_json(200, listing)
                return
            if path == "/pocket/api/file":
                raw_path = query.get("path", [""])[0]
                if not raw_path:
                    self._send_json(400, {"error": "fileNotFound"})
                    return
                target, payload = run_file_operation(lambda: read_file_payload(raw_path))
                self._send_file(target, payload)
                return
            if path in {"/pocket/api/overview", "/pocket/api/lines", "/pocket/api/activity"}:
                if getattr(self.server, "activity_enabled", False):
                    self.server.seat_activity.note_request()
            if path == "/pocket/api/skills/suggest":
                self._send_json(200, self.server.skill_suggest.status())
                return
            if path in {"/pocket/api/skills/files", "/pocket/api/skills/zip"}:
                snapshot = self.server.skill_shelf.snapshot()
                if snapshot is None:
                    self._send_json(503, {"error": "skillsPreparing"}, {"Retry-After": "1"})
                    return
                policy = skill_files.FilePolicy(resolve_file_path, _path_is_denied,
                    file_kind, _opened_handle_path, DENIED_FILE_SEGMENT_PATTERNS)
                build = lambda: skill_files.build_plan(snapshot, query.get("id", [""])[0], policy)
                if path.endswith("/files"):
                    plan = run_file_operation(build, timeout=20)
                    self._send_json(200, plan.body)
                else:
                    result = run_file_operation(lambda: skill_files.make_zip(build(), query), timeout=90)
                    with result.stream:
                        self.send_response(200)
                        for name, value in result.headers.items():
                            self.send_header(name, value)
                        self.end_headers()
                        while chunk := result.stream.read(64 * 1024):
                            self.wfile.write(chunk)
                return
            if path == "/pocket/api/skills":
                payload = self.server.skill_shelf.snapshot()
                if payload is None:
                    self._send_json(503, {"error": "skillsPreparing"}, {"Retry-After": "1"})
                else:
                    skill_shelf.send_prepared(self, payload)
                return
            if path == "/pocket/api/activity":
                try:
                    limit = int(query.get("limit", ["60"])[0])
                    before = int(query["before"][0]) if "before" in query else None
                    if not 1 <= limit <= 200 or (before is not None and before < 0):
                        raise ValueError()
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "activityQueryInvalid"})
                    return
                self._send_json_tagged(self.server.seat_activity.activity(limit, before))
                return
            if path == "/pocket/api/lines":
                payload = (self.server.seat_activity.snapshot("lines") if getattr(self.server, "activity_enabled", False) and self.server.seat_activity.ready
                           else lines_payload(self.server.cached_bridge))
                self._send_json_tagged(payload)
                return
            if path == "/pocket/api/overview":
                launcher = getattr(self.server, "launcher", None)
                overview = (self.server.seat_activity.snapshot("overview") if getattr(self.server, "activity_enabled", False) and self.server.seat_activity.ready
                            else dict(overview_payload(
                    self.server.cached_bridge,
                    launcher.launch_times_by_tab() if launcher is not None else None,
                    launcher.launch_kinds_by_tab() if launcher is not None else None,
                )))
                features = dict(getattr(self.server, "features", None) or {})
                # The home screen must not depend on a feature this machine does
                # not have. Asking for the unreplied count where there is no mail
                # snapshot would fail the whole screen, not just the mail row.
                overview["mailUnreplied"] = (
                    self.server.mail.unreplied_count()
                    if features.get("mail", True)
                    else 0
                )
                overview["padEnabled"] = getattr(self.server, "pad", None) is not None
                # Mail and research depend on Windows-only helpers. The client
                # hides them rather than offering a row that always fails.
                overview["features"] = features
                self._send_json_tagged(overview)
                return
            if path == "/pocket/api/launch/status":
                self._send_json(
                    200,
                    get_launch_status(
                        query.get("workspaceId", [""])[0],
                        query.get("tabId", [""])[0],
                        self.server.cached_bridge,
                    ),
                )
                return
            if path == "/pocket/api/launch/presets":
                self._send_json(200, {"presets": list(LAUNCH_PRESETS)})
                return
            if path == "/pocket/api/hermes/threads":
                self._serve_hermes(lambda: self.server.hermes.threads(
                    hermes_bridge.bounded_int(query.get("limit", [""])[0], default=60, maximum=200)))
                return
            if path.startswith("/pocket/api/hermes/thread/") and path.endswith("/messages"):
                session_id = path[len("/pocket/api/hermes/thread/"):-len("/messages")]
                self._serve_hermes(lambda: self.server.hermes.messages(
                    session_id,
                    hermes_bridge.bounded_int(query.get("limit", [""])[0], default=60, maximum=200)))
                return
            if path.startswith("/pocket/api/hermes/turn/"):
                turn_id = path[len("/pocket/api/hermes/turn/"):]
                self._serve_hermes(lambda: self.server.hermes.turn(
                    turn_id, hermes_bridge.bounded_int(query.get("after", [""])[0], default=0, maximum=10**7)))
                return
            if path.startswith("/pocket/api/hermes/image/"):
                # A picture the phone sent earlier, named on a transcript row. The
                # bytes go out as a file; a name this server never gave out is a
                # refusal in the harness's own code rather than a file lookup.
                session_id, _, name = path[len("/pocket/api/hermes/image/"):].partition("/")
                try:
                    target, payload = self.server.hermes.image(session_id, name)
                except hermes_bridge.HermesError as exc:
                    self._send_json(exc.status, {"error": exc.code})
                    return
                self._send_file(target, payload)
                return
            if path == "/pocket/api/launch/agents":
                self._send_json(
                    200,
                    {
                        "agents": launch_agent_rows(),
                        "placements": list(LAUNCH_PLACEMENTS),
                        "defaultPlacement": DEFAULT_LAUNCH_PLACEMENT,
                    },
                )
                return
            if path == "/pocket/api/build":
                self._send_json(200, {"build": build_id()})
                return
            if path == "/pocket/api/session":
                current = build_id()
                # The app asks for its token once at startup and carries the
                # build it was served, so this is the moment a phone can say
                # what it is running without a separate round trip.
                seen = record_client_build(
                    query.get("build", [""])[0], current,
                    self.headers.get("User-Agent", ""))
                self._send_json(200, {
                    "csrfToken": self.server.csrf_token,
                    "build": current,
                    "stale": seen not in {current, "unknown"},
                })
                return
            if path == "/pocket/api/research":
                if self.server.research is None:
                    raise ResearchError(503, "launchFailed")
                self._send_json(200, {"jobs": self.server.research.list_jobs()})
                return
            research_prefix = "/pocket/api/research/"
            if path.startswith(research_prefix):
                job_id = path[len(research_prefix) :]
                if self.server.research is None:
                    raise ResearchError(503, "launchFailed")
                self._send_json(200, {"job": self.server.research.get(job_id)})
                return
            if path == "/pocket/api/mail":
                self._send_json(200, self.server.mail.list_threads())
                return
            if path == "/pocket/api/mail/hidden":
                self._send_json(200, self.server.mail.list_hidden_threads())
                return
            if path == "/pocket/api/mail/draft":
                thread_id = query.get("threadId", [""])[0]
                if not thread_id:
                    self._send_json(400, {"error": "threadId is required"})
                    return
                self._send_json(200, self.server.mail.draft_payload(thread_id))
                return
            if path == "/pocket/api/mail/jobs":
                thread_id = query.get("thread", [""])[0]
                if not thread_id:
                    self._send_json(400, {"error": "thread is required"})
                    return
                self._send_json(
                    200,
                    {"jobs": self.server.mail.list_for_thread(thread_id)},
                )
                return
            mail_prefix = "/pocket/api/mail/"
            if path.startswith(mail_prefix) and len(path) > len(mail_prefix):
                thread_id = path[len(mail_prefix) :]
                if "/" in thread_id:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(
                    200,
                    self.server.mail.snapshot.thread_payload(thread_id),
                )
                return
            if path == "/pocket/api/reservations":
                session_id = query.get("session", [""])[0]
                if not session_id:
                    self._send_json(400, {"error": "session is required"})
                    return
                try:
                    self.server.reservations.resolve_reservation_target(session_id, self.server.bridge)
                except BridgeError:
                    self._send_json(404, {"error": "sessionNotAllowed"})
                    return
                reservations = self.server.reservations.list_for_session(session_id)
                self._send_json(200, {"reservations": reservations})
                return
            term_prefix = "/pocket/api/term/"
            if path.startswith(term_prefix) and len(path) > len(term_prefix):
                terminal_session_id = path[len(term_prefix) :]
                if "/" in terminal_session_id:
                    self._send_json(404, {"error": "sessionNotAllowed"})
                    return
                try:
                    payload = read_terminal_payload(
                        terminal_session_id,
                        self.server.bridge,
                        self.server.launcher.launch_kinds_by_tab(),
                    )
                except BridgeError as exc:
                    if exc.code != "sessionNotAllowed":
                        raise
                    self._send_json(404, {"error": "sessionNotAllowed"})
                    return
                records = self.server.reservations.list_for_session(terminal_session_id)
                if records:
                    payload["reservations"] = records
                self._send_json(200, payload)
                return
            ws_prefix = "/pocket/api/ws/"
            if path.startswith(ws_prefix) and len(path) > len(ws_prefix):
                workspace_id = path[len(ws_prefix) :]
                if "/" in workspace_id:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(
                    200,
                    get_workspace_payload(
                        workspace_id,
                        self.server.cached_bridge,
                        self.server.launcher.launch_times_by_tab(),
                        self.server.launcher.launch_kinds_by_tab(),
                    ),
                )
                return
            talk_prefix = "/pocket/api/talk/"
            if path.startswith(talk_prefix) and path.endswith("/ask"):
                self._serve_talk_ask(path[len(talk_prefix):-len("/ask")])
                return
            if path.startswith(talk_prefix) and path.endswith("/wait"):
                self._serve_talk_wait(path[len(talk_prefix):-len("/wait")], query)
                return
            if path.startswith(talk_prefix) and len(path) > len(talk_prefix):
                session_id = path[len(talk_prefix) :]
                tab: dict[str, Any] | None = None
                talk_stage = "match"

                def talk_error(status: int, body: dict[str, Any], stage: str,
                               detail: str = "", retry: bool = False) -> None:
                    tab_id = tab.get("id") if tab is not None else None
                    terminal_id = tab.get("sessionId") if tab is not None else None
                    agent = tab_agent(tab) if tab is not None else None
                    pty_reason = tab.get("processStatusReason") if tab is not None else None
                    diagnostic = _redact_diagnostic(detail.splitlines()[0][:160]) if detail else "-"
                    _log_server_error(
                        f"talk_get status={status} seat={session_id} "
                        f"tab={tab_id or '-'} terminal={terminal_id or '-'} "
                        f"agent={agent if agent in {'claude', 'codex'} else '-'} "
                        f"stage={stage} reason={body.get('reason', body['error'])} "
                        f"pty={pty_reason or '-'} detail={diagnostic}"
                    )
                    self._send_json(status, body, {"Retry-After": "5"} if retry else None)

                try:
                    limit = max(
                        1,
                        min(
                            TRANSCRIPT_PAGE_LIMIT,
                            int(query.get("limit", [str(TRANSCRIPT_PAGE_LIMIT)])[0]),
                        ),
                    )
                except ValueError:
                    self._send_json(400, {"error": "invalid limit"})
                    return
                before_value = query.get("before", [None])[0]
                before = None
                if before_value is not None:
                    if not before_value.isascii() or not before_value.isdecimal():
                        self._send_json(400, {"error": "invalid before cursor"})
                        return
                    before = int(before_value)
                # ?after=<cursor>: only what came after the line the phone
                # already holds, oldest first, with hasMore. Without it the
                # answer is the newest page exactly as before.
                after_value = query.get("after", [None])[0]
                after = None
                if after_value is not None:
                    if not after_value.isascii() or not after_value.isdecimal():
                        self._send_json(400, {"error": "invalid after cursor"})
                        return
                    after = int(after_value)
                launch_kinds = self.server.launcher.launch_kinds_by_tab()
                if not SESSION_ID_RE.fullmatch(session_id):
                    talk_error(404, {"error": "sessionNotAllowed"}, "match")
                    return
                talk_stage = "listing"
                try:
                    listing = self.server.bridge("panes", "--all")
                except BridgeError as exc:
                    talk_error(503, {"error": "bridgeUnavailable", "reason": "listingUnavailable"},
                               "listing", exc.diagnostic or exc.detail, retry=True)
                    return
                talk_stage = "match"
                tab = _matching_tab(listing, session_id)
                codex_id = None
                if tab is None:
                    # A Codex seat has no Claude session id; the talk id for
                    # it is its terminal session id, as /term/{id} names it.
                    tab = resolve_codex_talk_tab(session_id, listing, launch_kinds)
                    if tab is None:
                        talk_error(404, {"error": "sessionNotAllowed"}, "match")
                        return
                    codex_id = tab.get("agentSessionId")
                pane = _owning_pane(listing, tab)
                talk_stage = "transcript"
                try:
                    if tab_agent(tab, launch_kinds) == "codex":
                        page = (codex_transcript_page_after(codex_id, after, limit) if after is not None
                                else codex_transcript_page(codex_id, limit=limit, before=before))
                    else:
                        page = (transcript_page_after(session_id, after, limit) if after is not None
                                else transcript_page(session_id, limit=limit, before=before))
                except ValueError:
                    self._send_json(400, {"error": "invalid before cursor" if after is None else "invalid after cursor"})
                    return
                observed_tab = _talk_observation(tab, self.server.bridge)
                response = {
                    "title": resolve_talk_title(tab, session_id),
                    "activity": (_unknown_activity() if tab_agent(tab, launch_kinds) == "codex"
                                 else transcript_activity(session_id, now=datetime.now(timezone.utc))),
                    "agent": tab_agent(tab, launch_kinds),
                    "appKind": tab_app_kind(tab, launch_kinds),
                    "status": _tab_status(observed_tab),
                    **_observation_fields(observed_tab),
                    "terminalSessionId": tab.get("sessionId"),
                    "canSend": True,
                }
                # Use the recorded seat launch, never the time a phone opened it.
                tab_id = tab.get("id")
                launched_at = (self.server.launcher.launch_times_by_tab().get(tab_id)
                               if isinstance(tab_id, str) else None)
                if _parse_timestamp(launched_at) is not None:
                    response["startedAt"] = launched_at
                if codex_id is not None:
                    response["agentSessionId"] = codex_id
                if pane is not None:
                    # The close control on the talk screen lives on this. A
                    # server without it hands the phone no pane, and the
                    # phone then offers nothing it could not carry out.
                    response["pane"] = talk_pane_view(pane, launch_kinds)
                if page is None:
                    talk_stage = "terminal_read"
                    ended_body = {
                        "error": "seatEnded", "reason": "noTranscriptNoScreen",
                        "message": "\u3053\u306e\u5e2d\u306f\u7d42\u4e86\u3057\u3066\u3044\u3066\u3001\u4f1a\u8a71\u306e\u8a18\u9332\u3082\u753b\u9762\u3082\u6b8b\u3063\u3066\u3044\u307e\u305b\u3093\u3002",
                        "title": response["title"], "agent": response["agent"],
                    }
                    if tab.get("processStatusReason") == "no_live_pty_session":
                        talk_error(410, ended_body, "terminal_read")
                        return
                    try:
                        terminal = read_tab_terminal_payload(tab, self.server.bridge)
                    except BridgeError as exc:
                        _log_server_error(f"bridge_api {exc.detail}")
                        if exc.detail == "terminalBufferMissing":
                            talk_error(410, ended_body, "terminal_read", exc.diagnostic or exc.detail)
                        else:
                            reason = exc.detail if exc.detail in {
                                "bridgeBusy", "bridgeTimeout", "terminalReadUnavailable",
                            } else "terminalReadFailed"
                            talk_error(503, {"error": "bridgeUnavailable", "reason": reason},
                                       "terminal_read", exc.diagnostic or exc.detail, retry=True)
                        return
                    response.update(
                        {
                            "events": [],
                            "source": "terminal",
                            "nextBefore": None,
                            "atStart": True,
                            **terminal,
                        }
                    )
                else:
                    response.update(
                        {
                            "events": page["events"],
                            "source": "transcript",
                            "nextBefore": page["nextBefore"],
                            "atStart": page["atStart"],
                            "sessionStatus": (transcript_session_status(session_id)
                                              if response["agent"] != "codex" else codex_session_status(codex_id)),
                        }
                    )
                    if after is not None:
                        response["hasMore"] = bool(page.get("hasMore"))
                self._send_json(200, response)
                return
            if path.startswith("/pocket/api/pad/"):
                self._serve_pad_get(path, query)
                return
            self._send_json(404, {"error": "not found"})
        except (FileAccessError, skill_files.SkillFilesError) as exc:
            self._send_json(exc.status, {"error": exc.code, **exc.details})
        except LaunchError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except BridgeError as exc:
            _log_server_error(f"bridge_api {exc.detail}")
            if "talk_error" in locals():
                reason = exc.detail if exc.detail in {
                    "bridgeBusy", "bridgeTimeout", "terminalReadUnavailable",
                } else "terminalReadFailed"
                talk_error(503, {"error": "bridgeUnavailable", "reason": reason},
                           talk_stage, exc.diagnostic or exc.detail, retry=True)
            else:
                self._send_json(503, {"error": "bridgeUnavailable"})
        except MailError as exc:
            self._send_json(
                exc.status,
                {"error": exc.reason, "checkFindings": exc.check_findings},
            )
        except ResearchError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except Exception as exc:
            if _client_gone(exc):
                self._access_status = 499
                self.close_connection = True
                return
            # A 500 used to leave nothing behind: the phone saw "internal error"
            # and the log said no more than the access line. The route and the
            # last frames go to the error log so the next one can be read
            # (2026-09-19: talk waits were answering 500 with no trace at all).
            _log_server_error("api_unhandled %s %s" % (
                access_route(path), " | ".join(traceback.format_exc().strip().splitlines()[-3:])))
            if "talk_error" in locals():
                talk_error(500, {"error": "internal error"}, talk_stage, type(exc).__name__)
            else:
                self._send_json(500, {"error": "internal error"})

    def _send_raw_http(self, status: int, reason: str) -> None:
        self.wfile.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Connection: close\r\n"
                "Content-Length: 0\r\n"
                "\r\n"
            ).encode("ascii")
        )
        self.wfile.flush()
        self.close_connection = True

    def _send_pad_exception(self, exc: BaseException) -> None:
        if isinstance(exc, pad_service.PadProtocolUpgradeRequired):
            self._send_json(400, {"error": "padProtocolUpgradeRequired"})
            return
        if isinstance(exc, pad_service.PadSequenceExceeded):
            self._send_json(400, {"error": "padSequenceExceeded"})
            return
        if isinstance(exc, pad_service.PairingLocked):
            self._send_json(429, {"error": "padPairLocked"})
            return
        if isinstance(exc, pad_service.PairingBusy):
            self._send_json(429, {"error": "padPairBusy"})
            return
        if isinstance(exc, pad_service.PadDisarmed):
            self._send_json(409, {"error": "padDisarmed"})
            return
        if isinstance(exc, pad_service.PadRateLimited):
            self._send_json(429, {"error": "padRateLimited"})
            return
        if isinstance(exc, InjectorUnavailable):
            self._send_json(503, {"error": "padUnavailable"})
            return
        if isinstance(exc, pad_service.PadGenerationExhausted):
            self._send_json(503, {"error": "padGenerationExhausted"})
            return
        if isinstance(exc, ValueError):
            self._send_json(400, {"error": "padCommandInvalid"})
            return
        raise exc

    FAULT_MAX_BODY = 4096

    def _serve_fault_report(self) -> None:
        """What went wrong on the phone, in one line someone can read later."""
        if not self._origin_allowed(self.headers.get("Origin")):
            self._send_json(403, {"error": "originDenied"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > self.FAULT_MAX_BODY:
            self._send_json(400, {"error": "faultTooLarge"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "faultInvalid"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "faultInvalid"})
            return
        record_fault(payload, build_id(), self.headers.get("User-Agent", ""))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorize_pad_csrf(self) -> bool:
        if not self._origin_allowed(self.headers.get("Origin")):
            self._send_json(403, {"error": "originDenied"})
            return False
        token = self.headers.get(CSRF_HEADER)
        if not isinstance(token, str) or not secrets.compare_digest(
            token,
            self.server.csrf_token,
        ):
            self._send_json(403, {"error": "csrfDenied"})
            return False
        return True

    def _pad_ws_send(self, opcode: int, payload: bytes) -> None:
        self.wfile.write(ws_frame.encode_frame(opcode, payload))
        self.wfile.flush()

    def _pad_ws_send_json(self, value: dict[str, Any]) -> None:
        self._pad_ws_send(
            0x1,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _pad_ws_close(self, code: int) -> None:
        try:
            self._pad_ws_send(0x8, code.to_bytes(2, "big"))
        except OSError:
            return

    @staticmethod
    def _is_socket_timeout(exc: BaseException) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, OSError):
            winerror = getattr(exc, "winerror", None)
            return exc.errno in (
                errno.ETIMEDOUT,
                errno.EWOULDBLOCK,
                errno.EAGAIN,
            ) or winerror in (10035, 10060)
        return False

    def _serve_pad_get(self, path: str, query: dict[str, list[str]]) -> None:
        pad = getattr(self.server, "pad", None)
        if pad is None:
            self._send_json(404, {"error": "not found"})
            return
        try:
            if path == "/pocket/api/pad/state":
                device_id = pad.verify_token(self.headers.get(PAD_TOKEN_HEADER) or "")
                if device_id is None:
                    self._send_json(403, {"error": "padTokenDenied"})
                    return
                payload = pad.state(device_id)
                snapshot = pad.foreground_snapshot(device_id)
                payload["fg"] = snapshot.get("fg")
                payload["mon"] = snapshot.get("mon")
                self._send_json(200, payload)
                return
            if path == "/pocket/api/pad/monitors":
                self._send_json(200, {"monitors": pad.state().get("monitors", [])})
                return
            if path == "/pocket/api/pad/pair/status":
                nonce = query.get("nonce", [""])[0]
                self._send_json(200, pad.pairing_status(nonce))
                return
            if path == "/pocket/api/pad/devices":
                if not self._authorize_pad_csrf():
                    return
                self._send_json(200, {"devices": pad.list_devices()})
                return
            self._send_json(404, {"error": "not found"})
        except (
            pad_service.PadSequenceExceeded,
            pad_service.PairingLocked,
            pad_service.PairingBusy,
            pad_service.PadDisarmed,
            pad_service.PadRateLimited,
            pad_service.PadGenerationExhausted,
            InjectorUnavailable,
            ValueError,
        ) as exc:
            self._send_pad_exception(exc)

    def _serve_pad_post(self, path: str) -> None:
        pad = getattr(self.server, "pad", None)
        if pad is None:
            self._send_json(404, {"error": "not found"})
            return
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        try:
            if path == "/pocket/api/pad/pair/start":
                result = pad.request_pairing(payload.get("label", ""))
                self._send_json(202, result)
                return
            device_id = pad.verify_token(self.headers.get(PAD_TOKEN_HEADER) or "")
            if device_id is None:
                self._send_json(403, {"error": "padTokenDenied"})
                return
            if path == "/pocket/api/pad/arm":
                if set(payload) - {"takeover"} or (
                    "takeover" in payload and not isinstance(payload["takeover"], bool)
                ):
                    raise ValueError("pad arm fields are invalid")
                self._send_json(
                    200, pad.arm(device_id, takeover=payload.get("takeover", False))
                )
                return
            if path == "/pocket/api/pad/disarm":
                self._send_json(200, pad.disarm(device_id))
                return
            if path == "/pocket/api/pad/input":
                self._send_json(200, pad.dispatch(device_id, payload))
                return
            self._send_json(404, {"error": "not found"})
        except (
            pad_service.PadSequenceExceeded,
            pad_service.PairingLocked,
            pad_service.PairingBusy,
            pad_service.PadDisarmed,
            pad_service.PadRateLimited,
            pad_service.PadGenerationExhausted,
            InjectorUnavailable,
            ValueError,
        ) as exc:
            self._send_pad_exception(exc)

    def _serve_pad_delete(self, path: str) -> None:
        pad = getattr(self.server, "pad", None)
        if pad is None:
            self._send_json(404, {"error": "not found"})
            return
        prefix = "/pocket/api/pad/devices/"
        if not path.startswith(prefix) or path == prefix or "/" in path[len(prefix) :]:
            self._send_json(404, {"error": "not found"})
            return
        device_id = path[len(prefix) :]
        try:
            revoked = pad.revoke_device(device_id)
        except (
            pad_service.PadSequenceExceeded,
            pad_service.PairingLocked,
            pad_service.PairingBusy,
            pad_service.PadDisarmed,
            pad_service.PadRateLimited,
            InjectorUnavailable,
            ValueError,
        ) as exc:
            self._send_pad_exception(exc)
            return
        if not revoked:
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, {"revoked": True})

    def _serve_pad_socket(self) -> None:
        pad = getattr(self.server, "pad", None)
        if pad is None:
            self._send_json(404, {"error": "not found"})
            return
        if not self._origin_allowed(self.headers.get("Origin")):
            self._send_raw_http(403, "Forbidden")
            return
        protocols = ws_frame.parse_subprotocols(
            self.headers.get("Sec-WebSocket-Protocol") or ""
        )
        if len(protocols) < 2 or protocols[0] != "pocket-pad":
            self._send_raw_http(403, "Forbidden")
            return
        device_id = pad.verify_token(protocols[1])
        if device_id is None:
            self._send_raw_http(403, "Forbidden")
            return
        if not self.server._pad_slots.acquire(blocking=False):
            self._send_raw_http(503, "Service Unavailable")
            return
        acquired = True
        key = self.headers.get("Sec-WebSocket-Key")
        try:
            accept = ws_frame.accept_key(key or "")
        except (TypeError, ValueError):
            acquired = False
            self.server._pad_slots.release()
            self._send_raw_http(403, "Forbidden")
            return
        try:
            self.wfile.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "Sec-WebSocket-Protocol: pocket-pad\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            self.wfile.flush()
        except OSError:
            acquired = False
            self.server._pad_slots.release()
            self.close_connection = True
            return
        self.close_connection = True
        wait = min(PAD_STATUS_INTERVAL_SECONDS, PAD_SOCKET_READ_TIMEOUT)
        opened = False
        try:
            self.connection.settimeout(wait)
            leftover = b""
            peek = getattr(self.rfile, "peek", None)
            if callable(peek):
                try:
                    pending = peek()
                    if pending:
                        leftover = self.rfile.read(len(pending))
                except Exception:
                    leftover = b""
            pad.connection_opened(device_id)
            opened = True
            hello = pad.state(device_id)
            hello["t"] = "hello"
            self._pad_ws_send_json(hello)
            reader = ws_frame.FrameReader(_RecvReader(self.connection, leftover))
            started = time.monotonic()
            last_rx = started
            ping_since: float | None = None
            last_status = 0.0
            while True:
                now = time.monotonic()
                if now - started > PAD_SOCKET_MAX_SECONDS:
                    break
                if ping_since is not None and now - ping_since > PAD_PONG_DEADLINE:
                    break
                if now - last_status >= PAD_STATUS_INTERVAL_SECONDS:
                    snapshot = pad.foreground_snapshot(device_id)
                    self._pad_ws_send_json(
                        {
                            "t": "s",
                            "mon": snapshot.get("mon"),
                            "fg": snapshot.get("fg"),
                            "armed": snapshot.get("armed"),
                            "generation": snapshot.get("generation"),
                        }
                    )
                    last_status = now
                try:
                    opcode, payload = reader.read_message()
                except Exception as exc:
                    if self._is_socket_timeout(exc):
                        now = time.monotonic()
                        if now - last_rx >= PAD_SOCKET_READ_TIMEOUT:
                            self._pad_ws_send(0x9, b"")
                            if ping_since is None:
                                ping_since = now
                        continue
                    if isinstance(exc, ws_frame.WsProtocolError):
                        self._pad_ws_close(exc.close_code)
                        break
                    if isinstance(exc, (ConnectionError, OSError, ValueError)):
                        break
                    raise
                last_rx = time.monotonic()
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._pad_ws_send(0xA, payload)
                    continue
                if opcode == 0xA:
                    ping_since = None
                    continue
                if opcode != 0x1:
                    continue
                try:
                    command = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    self._pad_ws_close(1007)
                    break
                if not isinstance(command, dict):
                    self._pad_ws_close(1007)
                    break
                try:
                    result = pad.dispatch(device_id, command)
                except pad_service.PadProtocolUpgradeRequired:
                    self._pad_ws_send_json(
                        {"t": "err", "code": "padProtocolUpgradeRequired"}
                    )
                    continue
                except pad_service.PadSequenceExceeded:
                    self._pad_ws_send_json({"t": "err", "code": "padSequenceExceeded"})
                    continue
                except pad_service.PadRateLimited:
                    self._pad_ws_close(1008)
                    break
                except pad_service.PadDisarmed:
                    self._pad_ws_close(1008)
                    break
                except ValueError:
                    self._pad_ws_send_json({"t": "err", "code": "padCommandInvalid"})
                    continue
                except InjectorUnavailable:
                    self._pad_ws_close(1011)
                    break
                if isinstance(result, dict) and result.get("blocked"):
                    self._pad_ws_send_json({"t": "blocked"})
                elif isinstance(result, dict) and result.get("t"):
                    self._pad_ws_send_json(result)
                elif isinstance(result, dict) and result.get("dropped"):
                    self._pad_ws_send_json(
                        {"t": "stale", "reason": result.get("reason")}
                    )
        except (OSError, Exception):
            pass
        finally:
            try:
                if opened:
                    try:
                        pad.connection_closed(device_id)
                    except Exception:
                        pass
                if acquired:
                    acquired = False
                    self.server._pad_slots.release()
            finally:
                try:
                    self.connection.close()
                except OSError:
                    pass

    def _read_json_body(self) -> dict[str, Any]:
        self.connection.settimeout(HTTP_BODY_TIMEOUT_SECONDS)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if not 1 <= length <= 65536:
            raise ValueError("invalid request body length")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        self._observe_send_payload(payload)
        return payload

    def _read_upload_json_body(self) -> dict[str, Any]:
        self.connection.settimeout(HTTP_BODY_TIMEOUT_SECONDS)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length > MAX_UPLOAD_BODY_BYTES:
            raise upload_service.UploadError(413, "tooLarge")
        if length < 1:
            raise ValueError("invalid request body length")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        self._observe_send_payload(payload)
        return payload

    def _authorize_mutation(self, *, video_stream: bool = False) -> bool:
        if not self._origin_allowed(self.headers.get("Origin")):
            self._send_json(403, {"error": "originDenied"})
            return False
        content_type = self.headers.get_content_type().lower()
        accepted = content_type in upload_service.VIDEO_CONTENT_TYPES if video_stream else content_type == "application/json"
        if not accepted:
            self._send_json(415, {"error": "videoRequired" if video_stream else "jsonRequired"})
            return False
        token = self.headers.get(CSRF_HEADER)
        if not isinstance(token, str) or not secrets.compare_digest(
            token,
            self.server.csrf_token,
        ):
            self._send_json(403, {"error": "csrfDenied"})
            return False
        return True

    def _serve_pair(self, query: dict[str, list[str]]) -> None:
        # ``m`` is read and ignored: mycmux always fills it with this machine's
        # own host name, the phone never displays it, and honouring it let one
        # page show one PC's name above another PC's links.
        del query
        machine = tailscale_machine(self._identity_status)
        origin = public_origin_for(machine)
        if origin is None:
            # No Tailscale name yet, and the fallback would name a different
            # machine. The QR path still works; this one waits.
            self._send_json(503, {"error": "publicOriginUnknown"})
            return
        name = machine["name"]
        base = origin.rstrip("/") + "/pocket/"
        app_link = "pocketios://pair?u=" + quote(base, safe="") + "&m=" + quote(name, safe="")
        values = {"MACHINE": name, "BASE": base, "APP_LINK": app_link}
        try:
            template = (WEB_ROOT / "pair.html").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            self.send_error(404)
            return
        # One pass: a name containing a template marker stays literal data.
        page = re.sub(
            r"\{\{(MACHINE|BASE|APP_LINK)\}\}",
            lambda match: html.escape(values[match[1]], quote=True), template,
        )
        self._send_static_payload(
            page.encode("utf-8"), "text/html; charset=utf-8", "no-store"
        )

    def _serve_static(self, path: str) -> None:
        if path == "/pocket":
            self.send_response(308)
            self.send_header("Location", "/pocket/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not path.startswith("/pocket/"):
            self.send_error(404)
            return
        relative = path[len("/pocket/") :] or "index.html"
        if "\\" in relative or "\x00" in relative:
            self.send_error(404)
            return
        try:
            target = (WEB_ROOT / relative).resolve()
            target.relative_to(WEB_ROOT)
        except (OSError, ValueError):
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return
        try:
            payload = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        content_type = STATIC_MIME_TYPES.get(target.suffix.lower()) or (
            mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        if target.suffix in {".html", ".js", ".css", ".json"}:
            content_type += "; charset=utf-8"
        current = build_id()
        if relative in {"", "index.html"}:
            payload = stamp_build(payload, current)
        elif relative == "sw.js":
            payload = stamp_worker(payload, current)
        cache_control = (
            "no-cache"
            if relative == "sw.js"
            else "max-age=300, stale-while-revalidate=86400"
        )
        self._send_static_payload(payload, content_type, cache_control)

    def _send_static_payload(self, payload: bytes, content_type: str, cache_control: str) -> None:
        self.send_response(200)
        self.send_header("X-Pocket-Build", build_id())
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        body, encoding = self._encode_body(payload, content_type)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    # Nothing was compressed. A first load was 828 KB over the wire - 268 KB of
    # app.js, 101 KB of style.css, and JSON printed with an indent - which is
    # 4.4 seconds of download on poor mobile data before the app can do
    # anything. Over Tailscale from a phone that is the whole first impression.
    GZIP_MIN_BYTES = 900
    GZIP_TYPES = ("text/", "application/json", "application/javascript",
                  "image/svg+xml")

    def _encode_body(self, payload: bytes, content_type: str) -> tuple[bytes, str | None]:
        if len(payload) < self.GZIP_MIN_BYTES:
            return payload, None
        if not content_type.startswith(self.GZIP_TYPES):
            return payload, None
        accepted = self.headers.get("Accept-Encoding", "")
        if not any(part.strip().split(";")[0] == "gzip" for part in accepted.split(",")):
            return payload, None
        squeezed = gzip.compress(payload, 6)
        # A body that does not shrink is sent as it is, so nothing pays for the
        # header and the decompression to save nothing.
        if len(squeezed) >= len(payload):
            return payload, None
        return squeezed, "gzip"

    def _serve_hermes(self, operation: Callable[[], Any]) -> None:
        """Answer a harness-room call, with the upstream's own failure code.

        The phone renders the code, so a refusal has to stay a refusal: a
        session outside the home chat must not read as an empty room, and an
        unconfigured machine must not read as a missing feature.
        """
        try:
            self._send_json(200, operation())
        except hermes_bridge.HermesError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except Exception as exc:
            _log_server_error(f"hermes {type(exc).__name__}")
            self._send_json(500, {"error": "hermesFailed"})

    def _send_json_tagged(self, payload: dict[str, Any]) -> None:
        """A list with an ETag; a caller that already holds it gets a 304."""
        etag = payload_etag(payload)
        headers = getattr(self, "headers", None)
        conditional = headers.get("If-None-Match") if headers is not None else None
        if etag_matches(conditional, etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, payload, {"ETag": etag})

    def _send_json(self, status: int, value: Any, extra_headers: dict[str, str] | None = None) -> None:
        record = getattr(self, "_send_observation", None)
        if record is not None:
            record["status"] = status
            if record.get("route") == "/pocket/api/talk/*/send" and isinstance(value, dict):
                seat = value.get("seat")
                if seat in {"idle", "working", "busy"}:
                    record["seat"] = seat
                reserve_reason = value.get("reason") if value.get("result") == "reserved" else None
                if reserve_reason in {"seatNotRunning", "questionOpen", "seatBusy",
                                      "statusUnreadable", "bridgeUnavailable"}:
                    record["reserveReason"] = reserve_reason
            outcome = value.get("result") if isinstance(value, dict) else None
            reason = value.get("error", value.get("reason", "")) if isinstance(value, dict) else ""
            outcome = outcome if isinstance(outcome, str) else None
            reason = reason if isinstance(reason, str) else ""
            if record.get("route") == ASK_ROUTE:
                # Controlled codes only: the answer and the question stay out.
                if status < 400:
                    record["result"] = outcome if outcome in {"delivered", "advanced"} else "rejected"
                else:
                    record["result"] = reason if reason in ASK_LOG_CODES else (
                        "notFound" if reason == "not found" else "rejected")
            elif status < 400 and outcome != "failed":
                record["result"] = outcome if outcome in {"delivered", "reserved"} else "delivered"
            else:
                # Human error details may contain data. Write only known static shapes.
                reason_codes = {
                    "text must be a string": "invalidText", "invalid JSON body": "invalidJson",
                    "invalid request body length": "invalidLength", "invalid content length": "invalidLength",
                    "JSON body must be an object": "invalidJson", "not found": "notFound",
                    "enter delivery was not confirmed": "enterUnconfirmed",
                    "current input line was not identified": "promptUnidentified",
                    "sent text remains in the input prompt": "textRemains",
                    "delivery failed": "deliveryFailed", "target is not idle": "targetBusy",
                }
                allowed = {"notAllowed", "originDenied", "csrfDenied", "jsonRequired", "sessionNotAllowed", "sendUnavailableShell",
                           "bridgeUnavailable", "termRateLimited", "termNotReady", "codexRolloutUnavailable",
                           "codexDeliveryUnconfirmed", "tooLarge", "unsupportedType",
                           "invalidBase64", "launchFailed", "featureUnavailable"}
                record["result"] = reason if reason in allowed else reason_codes.get(reason, "rejected")
                if isinstance(reason, str) and reason.startswith("text length must be between"):
                    record["result"] = "textLength"
                if isinstance(reason, str) and "reservation" in reason and "exceeded" in reason:
                    record["result"] = "reservationQuota"
            record["durationMs"] = round((time.monotonic() - self._send_started) * 1000, 2)
            record_send_attempt(record)
            self._send_observation = None
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, header_value in (extra_headers or {}).items():
            self.send_header(name, header_value)
        body, encoding = self._encode_body(payload, "application/json")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        try:
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:
            if _client_gone(exc):
                self._access_status = 499
                self.close_connection = True
            raise

    def _send_file(self, target: Path, payload: bytes) -> None:
        content_type = file_content_type(target)
        base_content_type = content_type
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        disposition = (
            "attachment" if base_content_type in ACTIVE_CONTENT_MIME_TYPES else "inline"
        )
        self.send_header("Content-Disposition", disposition)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if base_content_type in ACTIVE_CONTENT_MIME_TYPES:
            self.send_header(
                "Content-Security-Policy",
                "sandbox; default-src 'none'; connect-src 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            )
        body, encoding = self._encode_body(payload, content_type)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def make_server(
    port: int = 8994,
    bridge_runner: Callable[..., Any] = run_bridge,
    state_path: Path = RESERVATION_PATH,
    mail_snapshot_path: Path = MAIL_SNAPSHOT,
    mail_jobs_path: Path = MAIL_JOBS_PATH,
    mail_hidden_path: Path | None = None,
    mail_sends_path: Path | None = None,
    launches_path: Path = LAUNCHES_PATH,
    research_jobs_path: Path | None = None,
    research_runner: Callable[..., subprocess.CompletedProcess[str]] = _run_hidden,
    mail_processor: Callable[
        [dict[str, Any], str, Callable[[str], None]], Any
    ] = process_mail_draft,
    mail_revise_processor: Callable[
        [dict[str, Any], str, str, Callable[[str], None]], Any
    ] | None = None,
    pad_enabled: bool = False,
    pad_state_dir: Path | None = None,
    pad: Any = None,
    features: dict[str, bool] | None = None,
    pair_state_dir: Path | None = None,
    tailscale_status: TailscaleStatusCache | None = None,
) -> PocketHTTPServer:
    effective_features = public_features(features)
    reservations = ReservationService(state_path=state_path, bridge=bridge_runner)
    if mail_jobs_path != MAIL_JOBS_PATH:
        if mail_hidden_path is None:
            mail_hidden_path = mail_jobs_path.with_name(
                mail_jobs_path.stem + "-hidden.json"
            )
        if mail_sends_path is None:
            mail_sends_path = mail_jobs_path.with_name(
                mail_jobs_path.stem + "-sends.json"
            )
    try:
        mail = MailJobService(
            snapshot=MailSnapshotStore(mail_snapshot_path),
            state_path=mail_jobs_path,
            hidden_path=mail_hidden_path,
            sends_path=mail_sends_path,
            processor=mail_processor,
            revise_processor=mail_revise_processor,
        ) if effective_features.get("mail", False) else None
    except BaseException:
        reservations.stop()
        raise
    if research_jobs_path is None:
        research_stem = state_path.stem
        research_stem = (
            research_stem.replace("reservations", "research-jobs")
            if "reservations" in research_stem
            else research_stem + "-research-jobs"
        )
        research_jobs_path = (
            RESEARCH_JOBS_PATH
            if state_path == RESERVATION_PATH
            else state_path.with_name(research_stem + ".json")
        )
    try:
        research = ResearchJobService(
            state_path=research_jobs_path,
            runner=research_runner,
        ) if effective_features.get("research", False) else None
    except BaseException:
        reservations.stop()
        if mail is not None:
            mail.stop()
        raise
    try:
        return PocketHTTPServer(
            ("127.0.0.1", port),
            PocketHandler,
            reservation_service=reservations,
            mail_service=mail,
            research_service=research,
            launch_service=LaunchService(
                bridge=bridge_runner,
                state_path=launches_path,
            ),
            pad_enabled=pad_enabled,
            pad_state_dir=pad_state_dir,
            pad=pad,
            features=effective_features,
            pair_state_dir=pair_state_dir,
            tailscale_status=tailscale_status,
        )
    except BaseException:
        reservations.stop()
        if mail is not None:
            mail.stop()
        if research is not None:
            research.stop()
        raise


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Pocket mycmux PWA")
    parser.add_argument("--port", type=int, default=8994)
    parser.add_argument("--pad", action="store_true")
    # Declared so that --help and argument validation know it; the value itself
    # was read by resolve_state_dir() before this module finished importing.
    parser.add_argument("--state-dir", default=None,
                        help="folder for _state (default: beside this file)")
    return parser


def main() -> None:
    args = argument_parser().parse_args()
    prime_codex_transcript_index()
    TAILSCALE_STATUS.start()
    # The running server is the one that must respect what this machine can
    # actually do; a feature whose helpers are missing is closed here.
    httpd = make_server(
        args.port, pad_enabled=args.pad, features=MACHINE["features"]
    )
    shelf = getattr(httpd, "skill_shelf", None)
    if shelf is not None:
        shelf.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
