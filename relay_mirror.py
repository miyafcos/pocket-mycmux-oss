"""A bounded, read-only copy of Windows situation and talk on the Mac mini.

Only this module writes under the mirror root.  The saved payload is kept
separate from its timestamp so the Windows JSON is never silently rewritten.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote, urlsplit


def configured_https_url(value: str, *, origin_only: bool = False) -> str:
    if not value.strip():
        return ""
    parsed = urlsplit(value.strip())
    parsed.port
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(char.isspace() for char in value) or "\\" in value
            or (origin_only and parsed.path.strip("/"))):
        raise ValueError("Configure an HTTPS URL without credentials, query or fragment")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = "[" + ipaddress.IPv6Address(host).compressed + "]"
    port = ":" + str(parsed.port) if parsed.port not in (None, 443) else ""
    origin = "https://" + host + port
    return origin if origin_only else origin + parsed.path.rstrip("/")


WIN_BASE_URL = configured_https_url(os.environ.get("POCKET_WIN_API_BASE", ""))
MIRROR_ROOT = Path.home() / ".pocket-relay" / "mirror"
TALK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
INTERVAL_SECONDS = 60.0
TIMEOUT_SECONDS = 10.0
MAX_TALKS = 8
RETENTION_SECONDS = 14 * 24 * 3600
CLEANUP_SECONDS = 3600


def valid_talk_id(value: Any) -> bool:
    return isinstance(value, str) and TALK_ID.fullmatch(value) is not None


def iso_at(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec="seconds")


def timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def talk_seats(overview: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Use the same tab/pane guards that Routing.swift uses for .talk(id)."""
    found = {}

    def include(row: Any, tab: bool) -> None:
        if not isinstance(row, dict):
            return
        if row.get("lifecycle") == "declared" or row.get("duplicateId") is True or row.get("linkable") is False:
            return
        kind = row.get("kind") or row.get("type")
        if tab and kind in ("web", "browser"):
            return
        talk_id = row.get("claudeSessionId")
        if not valid_talk_id(talk_id):
            return
        value = {"talkId": talk_id, "title": row.get("title") or row.get("label") or "",
                 "lastOutputAt": row.get("lastOutputAt"), "status": row.get("status")}
        old = found.get(talk_id)
        if old is None or timestamp(value["lastOutputAt"]) > timestamp(old["lastOutputAt"]):
            found[talk_id] = value

    for workspace in overview.get("workspaces", []):
        if not isinstance(workspace, dict):
            continue
        for pane in workspace.get("panes", []):
            if not isinstance(pane, dict):
                continue
            tabs = pane.get("tabs")
            if isinstance(tabs, list):
                for row in tabs:
                    include(row, True)
            include(pane, False)
    return found


class Mirror:
    def __init__(self, watcher: Any, root: Path = MIRROR_ROOT,
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], float] = time.time,
                 base_url: str = WIN_BASE_URL,
                 interval: float = INTERVAL_SECONDS) -> None:
        self.watcher = watcher
        self.root = Path(root)
        self.opener = opener
        self.clock = clock
        self.base_url = configured_https_url(base_url)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_cleanup = 0.0

    def _directory(self, path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    def _write(self, path: Path, value: Any) -> None:
        self._directory(path.parent)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".mirror-",
                                             suffix=".tmp", delete=False) as handle:
                name = handle.name
                os.chmod(name, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
            os.chmod(path, 0o600)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _fetch(self, path: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
        if not self.base_url:
            return "notConfigured", None
        request = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status != 200:
                    return status, None
                payload = json.loads(response.read())
                return status, payload if isinstance(payload, dict) else None
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return "timeout", None

    def _save_payload(self, path: Path, payload: Dict[str, Any]) -> str:
        mirrored_at = iso_at(self.clock)
        self._write(path, {"payload": payload, "mirroredAt": mirrored_at})
        return mirrored_at

    def read(self, kind: str, talk_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if kind == "talk":
            if not valid_talk_id(talk_id):
                return None
            path = self.root / "talk" / (talk_id + ".json")
        elif kind in ("overview", "lines", "pc"):
            path = self.root / (kind + ".json")
        else:
            return None
        stored = self._read(path)
        if stored is None or not isinstance(stored.get("payload"), dict):
            return None
        mirrored_at = stored.get("mirroredAt")
        if not isinstance(mirrored_at, str):
            return None
        answer = dict(stored["payload"])
        answer["mirror"] = {"mirroredAt": mirrored_at,
                            "ageSeconds": max(0, int(self.clock() - timestamp(mirrored_at))),
                            "source": "win"}
        return answer

    def sync_once(self) -> None:
        if self.watcher.status().get("winApiOk") is not True:
            return
        self._directory(self.root)
        overview_status, overview = self._fetch("/overview")
        if overview_status == 200 and overview is not None:
            self._save_payload(self.root / "overview.json", overview)
        lines_status, lines = self._fetch("/lines")
        if lines_status == 200 and lines is not None:
            self._save_payload(self.root / "lines.json", lines)
        pc_status, pc = self._fetch("/pc")
        if pc_status == 200 and pc is not None:
            self._save_payload(self.root / "pc.json", pc)
        if overview_status != 200 or overview is None:
            return

        seats = talk_seats(overview)
        index = self._read(self.root / "index.json") or {}
        entries = index.get("talks")
        if not isinstance(entries, dict):
            entries = {}
        changed = []
        for talk_id, seat in seats.items():
            old = entries.get(talk_id)
            path = self.root / "talk" / (talk_id + ".json")
            if (not isinstance(old, dict)
                    or old.get("lastOutputAt") != seat["lastOutputAt"]
                    or old.get("status") != seat["status"]
                    or old.get("lastHttpStatus") not in (200, 404, 410)
                    or (not path.is_file() and old.get("lastHttpStatus") not in (404, 410))):
                changed.append(seat)
            elif old.get("title") != seat["title"]:
                old["title"] = seat["title"]

        def read_order(seat: Dict[str, Any]) -> Tuple[bool, float]:
            old = entries.get(seat["talkId"])
            timed_out = isinstance(old, dict) and old.get("lastHttpStatus") == "timeout"
            return timed_out, -timestamp(seat["lastOutputAt"])

        changed.sort(key=read_order)
        for seat in changed[:MAX_TALKS]:
            talk_id = seat["talkId"]
            old = entries.get(talk_id)
            old = old if isinstance(old, dict) else {}
            status, payload = self._fetch("/talk/" + quote(talk_id, safe="") + "?limit=100")
            entry = dict(old)
            entry["lastHttpStatus"] = status
            if status == 200 and payload is not None:
                entry.update(seat)
                entry["mirroredAt"] = self._save_payload(self.root / "talk" / (talk_id + ".json"), payload)
            elif status in (404, 410) or not old:
                entry.update(seat)
            entries[talk_id] = entry
            if status == "timeout":
                break
        self._write(self.root / "index.json", {"talks": entries})
        if self.clock() - self._last_cleanup >= CLEANUP_SECONDS:
            self.cleanup(entries, set(seats))
            self._last_cleanup = self.clock()

    def cleanup(self, entries: Dict[str, Any], current_ids: set) -> None:
        cutoff = self.clock() - RETENTION_SECONDS
        dirty = False
        for talk_id, entry in list(entries.items()):
            if (not valid_talk_id(talk_id) or talk_id in current_ids
                    or not isinstance(entry, dict)
                    or timestamp(entry.get("mirroredAt")) >= cutoff):
                continue
            path = self.root / "talk" / (talk_id + ".json")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            del entries[talk_id]
            dirty = True
        if dirty:
            self._write(self.root / "index.json", {"talks": entries})

    def start(self) -> None:
        if self._thread is not None or not self.base_url:
            return
        self._thread = threading.Thread(target=self._run, name="win-mirror", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as exc:  # one bad read must not end the mirror
                print("win mirror failed: " + type(exc).__name__, flush=True)
            self._stop.wait(self.interval)
