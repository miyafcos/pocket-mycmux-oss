"""Serve PC-produced agent-design JSON; never inspect agent configuration files."""
from __future__ import annotations

import copy
import ctypes
import json
import ntpath
import os
import posixpath
import re
import socket
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mycmux_socket import MycmuxError, MycmuxSocket


COMPARE_ROWS = (
    "globalInstructions", "folderInstructions", "rules", "settings", "permissions",
    "memory", "ownSkills", "skillListing", "plugins", "agents", "mcp", "hooks",
    "scheduled", "references",
)
SERVICES = {"claude", "codex", "hermes"}
MAX_REVISION = (1 << 64) - 1
UNKNOWN_REFRESH = "Unknown socket command: agent_design.refresh"


class CatalogError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def folder_key(folder: str) -> str:
    """The PC path's syntax determines Windows/Mac comparison, not the phone."""
    if re.match(r"^[a-zA-Z]:", folder) or folder.startswith("\\\\"):
        return ntpath.normpath(folder).replace("\\", "/").casefold()
    return posixpath.normpath(folder)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if match is None:
        raise ValueError("timestamp")
    # Python 3.10 accepts only 3/6 fractional digits; Rust writes up to 9.
    fraction = (match[2] or "").ljust(6, "0")[:6]
    return datetime.fromisoformat(match[1] + "." + fraction + match[3].replace("Z", "+00:00"))


def _instant(value: str) -> int:
    parsed = _timestamp(value)
    delta = parsed.replace(microsecond=0) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    fraction = re.search(r"\.(\d+)", value)
    nanos = int(fraction[1].ljust(9, "0")) if fraction else 0
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + nanos


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.UnlockFileEx.restype = wintypes.BOOL


def _sharing_retry(operation: Callable[[], Any], *, replacement: bool = False) -> Any:
    for attempt in range(8):
        try:
            return operation()
        except OSError as exc:
            # Match the PC writer: MoveFileEx replacement can report error 5
            # for a short-lived reader/scanner even with FILE_SHARE_DELETE.
            retryable = {5, 32, 33} if replacement else {32, 33}
            if os.name != "nt" or getattr(exc, "winerror", None) not in retryable or attempt == 7:
                raise
            time.sleep(min(0.02 * (attempt + 1), 0.1))


def _open_shared(path: Path, *, lock: bool = False):
    if os.name != "nt":
        return path.open("a+b" if lock else "rb")
    # Share delete as well as reads/writes, so PC atomic replacements can proceed.
    handle = _kernel.CreateFileW(str(path), 0xC0000000 if lock else 0x80000000,
                                 0x7, None, 4 if lock else 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if lock else os.O_RDONLY) | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, "r+b" if lock else "rb")


def _read_bytes(path: Path) -> bytes:
    def read():
        with _open_shared(path) as stream:
            return stream.read()
    return _sharing_retry(read)


@contextmanager
def _exclusive_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    # Persistent file: never unlink, truncate or replace it, including on error.
    with _sharing_retry(lambda: _open_shared(directory / "closed.lock", lock=True)) as stream:
        if os.name == "nt":
            handle = msvcrt.get_osfhandle(stream.fileno())
            overlapped = _Overlapped()
            if not _kernel.LockFileEx(handle, 2, 0, 0xFFFFFFFF, 0xFFFFFFFF,
                                      ctypes.byref(overlapped)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                yield
            finally:
                if not _kernel.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF,
                                            ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _validate_catalog(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("catalog")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise CatalogError(409, "unsupportedVersion")
    _timestamp(value.get("generatedAt"))
    if any(not isinstance(value.get(key), str) or not value[key]
           for key in ("generator", "home", "workFolder", "cwd")):
        raise ValueError("header")
    refresh_ms = value.get("refreshMs")
    if value["cwd"] != value["workFolder"] or type(refresh_ms) not in (int, float) or refresh_ms < 0 or (isinstance(refresh_ms, float) and not refresh_ms.is_integer()):
        raise ValueError("header")
    for key in ("services", "layers", "compareRows", "items", "links", "readingFlows", "findings", "warnings"):
        if not isinstance(value.get(key), list):
            raise ValueError("catalog")
    services = value["services"]
    if len(services) != 3 or any(not isinstance(s, dict) for s in services):
        raise ValueError("services")
    if {s.get("id") for s in services} != SERVICES:
        raise ValueError("services")
    for service in services:
        if not isinstance(service.get("stats"), dict) or not isinstance(service.get("context"), dict):
            raise ValueError("service")
        if any(v is not None and not _integer(v) for v in service["stats"].values()):
            raise ValueError("stats")
    layers = value["layers"]
    if len(layers) != 7 or any(not isinstance(layer, dict) for layer in layers):
        raise ValueError("layers")
    if [layer.get("id") for layer in layers] != list(range(1, 8)):
        raise ValueError("layers")
    if any(not isinstance(layer.get("role"), str) or not isinstance(layer.get("itemIds"), list) for layer in layers):
        raise ValueError("layers")
    rows = value["compareRows"]
    if len(rows) != 14 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("rows")
    if [row.get("id") for row in rows] != list(COMPARE_ROWS):
        raise ValueError("rows")
    for row in rows:
        cells = row.get("cells")
        if not isinstance(cells, list) or len(cells) != 3 or any(not isinstance(c, dict) for c in cells):
            raise ValueError("cells")
        if {c.get("service") for c in cells} != SERVICES:
            raise ValueError("cells")
    for key in ("items", "findings"):
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in value[key]):
            raise ValueError(key)
        if len({item["id"] for item in value[key]}) != len(value[key]):
            raise ValueError(key)
    if not _integer(value.get("closedCount")) or not _integer(value.get("closedRevision")):
        raise ValueError("closed")
    documents = value.get("documents")
    if not isinstance(documents, dict):
        raise ValueError("documents")
    for identifier, document in documents.items():
        if not isinstance(document, dict) or document.get("id") != identifier:
            raise ValueError("document")
        body = document.get("body")
        if body is not None and (not isinstance(body, str) or len(body.encode("utf-8")) > 2 * 1024 * 1024):
            raise ValueError("document")
        if not isinstance(document.get("fields"), list) or not isinstance(document.get("size"), dict) or not isinstance(document.get("status"), str):
            raise ValueError("document")
    return value


def _validate_cache(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("cache")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise CatalogError(409, "unsupportedVersion")
    contexts = value.get("contexts")
    if not isinstance(contexts, list) or len(contexts) > 8:
        raise ValueError("contexts")
    for catalog in contexts:
        _validate_catalog(catalog)
    return value


def _validate_closed(value: Any) -> dict:
    if not isinstance(value, dict):
        raise CatalogError(409, "closedBroken")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise CatalogError(409, "closedUnsupported")
    if not _integer(value.get("revision")) or value["revision"] > MAX_REVISION or not isinstance(value.get("closed"), list):
        raise CatalogError(409, "closedBroken")
    entries = []
    for entry in value["closed"]:
        if not isinstance(entry, dict):
            raise CatalogError(409, "closedBroken")
        normalized = {"id": entry.get("id"), "workFolder": entry.get("workFolder", entry.get("cwd")),
                      "reason": entry.get("reason"), "closedAt": entry.get("closedAt", entry.get("date")),
                      "closedFrom": entry.get("closedFrom")}
        if any(not isinstance(normalized[k], str) or not normalized[k] for k in ("id", "workFolder", "reason")):
            raise CatalogError(409, "closedBroken")
        if not normalized["reason"].strip() or len(normalized["reason"]) > 1000 or not isinstance(normalized["closedFrom"], str) or normalized["closedFrom"] not in {"pc", "iphone"}:
            raise CatalogError(409, "closedBroken")
        if not (ntpath.isabs(normalized["workFolder"]) or posixpath.isabs(normalized["workFolder"])):
            raise CatalogError(409, "closedBroken")
        try:
            _timestamp(normalized["closedAt"])
            for text in normalized.values():
                text.encode("utf-8")
        except (ValueError, TypeError):
            raise CatalogError(409, "closedBroken") from None
        entries.append(normalized)
    return {"schemaVersion": 1, "revision": value["revision"], "closed": entries}


class RefreshClient:
    """Keep only the fixed unknown-command error; never expose peer diagnostics."""
    def __init__(self, client: MycmuxSocket | None = None):
        self.client = client or MycmuxSocket(timeout=3)

    def request(self, command: str, args: dict) -> dict:
        try:
            payload = {"cmd": command, "args": args}
            token = self.client.token()
            if token is not None:
                payload["token"] = token
            with socket.create_connection(("127.0.0.1", self.client.port()), timeout=self.client.timeout) as conn:
                conn.settimeout(self.client.timeout)
                conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                with conn.makefile("rb") as stream:
                    line = stream.readline(65537)
            if len(line) > 65536 or not line.endswith(b"\n"):
                raise ValueError("response")
            response = json.loads(line.decode("utf-8"))
            if not isinstance(response, dict):
                raise ValueError("response")
            return response
        except (OSError, MycmuxError, ValueError, UnicodeError):
            raise CatalogError(503, "pcUnavailable") from None


class AgentDesignCatalog:
    def __init__(self, home: Path | None = None, *, refresh_client: Any = None,
                 now: Callable[[], datetime] | None = None):
        self.home = home if home is not None else Path.home()
        self.directory = self.home / ".mycmux" / "agent_design"
        self.refresh_client = refresh_client or RefreshClient()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._guard = threading.RLock()
        self._files: dict[str, tuple[Any, str, Any, Any]] = {}

    def _load(self, name: str, validate: Callable) -> tuple[str, Any]:
        path = self.directory / name
        with self._guard:
            old = self._files.get(name)
            previous = old[3] if old else None
            try:
                stat = path.stat()
                signature = (str(path), stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                signature = None
            except OSError:
                return "broken", previous
            if old is not None and old[0] == signature:
                return old[1], old[2]
            if signature is None:
                state, value = "missing", None
            else:
                try:
                    value = validate(json.loads(_read_bytes(path).decode("utf-8")))
                    state, previous = "ok", value
                except CatalogError:
                    state, value = "unsupportedVersion", None
                except (ValueError, TypeError, KeyError, OSError, UnicodeError):
                    state, value = "broken", previous
            self._files[name] = (signature, state, value, previous)
            return state, value

    def _select(self, folder: Any = None) -> tuple[str, Any, list[dict], str]:
        if folder is not None and not isinstance(folder, str):
            raise CatalogError(400, "folderInvalid")
        main_state, main = self._load("catalog.json", _validate_catalog)
        cache_state, cache = self._load("cache.json", _validate_cache)
        home = main["home"] if main else (cache["contexts"][0]["home"] if cache and cache["contexts"] else str(self.home))
        wanted = home if folder in (None, "", "home") else folder
        if "unsupportedVersion" in (main_state, cache_state):
            return "unsupportedVersion", None, [], wanted
        contexts = {}
        for catalog in [*(cache["contexts"] if cache else []), *([main] if main else [])]:
            key = folder_key(catalog["workFolder"])
            current = contexts.get(key)
            if current is None or _instant(catalog["generatedAt"]) > _instant(current["generatedAt"]):
                contexts[key] = catalog
        ordered = sorted(contexts.values(), key=lambda c: (folder_key(c["workFolder"]) != folder_key(home), -_instant(c["generatedAt"])))[:8]
        contexts = {folder_key(c["workFolder"]): c for c in ordered}
        selected = contexts.get(folder_key(wanted))
        if selected is None and folder in (None, "", "home"):
            selected = main
        folders = [{"workFolder": c["workFolder"],
                    "label": "Home" if folder_key(c["workFolder"]) == folder_key(home) else (ntpath.basename(c["workFolder"].rstrip("/\\")) or c["workFolder"]),
                    "generatedAt": c["generatedAt"], "isHome": folder_key(c["workFolder"]) == folder_key(home)} for c in ordered]
        state = "broken" if "broken" in (main_state, cache_state) else "ok"
        if selected is None and state != "broken":
            state = "missing"
        return state, selected, folders, selected["workFolder"] if selected else wanted

    def _read_closed(self, *, cached: bool = False) -> dict:
        path = self.directory / "closed.json"
        with self._guard if cached else nullcontext():
            signature = None
            try:
                if cached:
                    stat = path.stat()
                    signature = (str(path), stat.st_mtime_ns, stat.st_size)
                    old = self._files.get("closed.json")
                    if old is not None and old[0] == signature:
                        if old[1] != "ok":
                            raise CatalogError(409, old[1])
                        return old[2]
                value = _validate_closed(json.loads(_read_bytes(path).decode("utf-8")))
            except FileNotFoundError:
                if cached:
                    self._files.pop("closed.json", None)
                return {"schemaVersion": 1, "revision": 0, "closed": []}
            except CatalogError as exc:
                if cached:
                    self._files["closed.json"] = (signature, exc.code, None, None)
                raise
            except (ValueError, TypeError, UnicodeError):
                if cached:
                    self._files["closed.json"] = (signature, "closedBroken", None, None)
                raise CatalogError(409, "closedBroken") from None
            except OSError:
                raise CatalogError(503, "closedUnavailable") from None
            if cached:
                self._files["closed.json"] = (signature, "ok", value, value)
            return value

    @staticmethod
    def _closed_keys(closed: dict, folder: str) -> set[str]:
        return {entry["id"] for entry in closed["closed"] if folder_key(entry["workFolder"]) == folder_key(folder)}

    def catalog(self, folder: Any = None) -> dict:
        state, source, folders, selected = self._select(folder)
        value, age, stale = None, 0, False
        if source is not None:
            # Do not copy or serialize document bodies when preparing the catalogue.
            value = copy.deepcopy({key: data for key, data in source.items() if key != "documents"})
            for item in value["items"]:
                item["hasDocument"] = isinstance(source["documents"].get(item["id"], {}).get("body"), str)
            closed = self._read_closed(cached=True)
            ids = self._closed_keys(closed, source["workFolder"])
            value["findings"] = [finding for finding in value["findings"] if finding["id"] not in ids]
            value["closedCount"], value["closedRevision"] = len(ids), closed["revision"]
            nanos = max(0, _instant(self.now().isoformat()) - _instant(source["generatedAt"]))
            age, stale = nanos // 1_000_000_000, nanos > 300_000_000_000
        return {"state": state, "catalog": value, "folders": folders, "selected": selected,
                "ageSeconds": age, "stale": stale}

    def document(self, folder: Any, identifier: Any) -> dict:
        _, source, _, _ = self._select(folder)
        if source is None or not isinstance(identifier, str) or identifier not in source["documents"]:
            raise CatalogError(404, "documentMissing")
        return copy.deepcopy(source["documents"][identifier])

    def close(self, folder: Any, finding_id: Any, work_folder: Any, reason: Any) -> dict:
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise CatalogError(400, "reasonInvalid")
        try:
            reason.encode("utf-8")
        except UnicodeError:
            raise CatalogError(400, "reasonInvalid") from None
        response = self.catalog(folder)
        if response["state"] == "unsupportedVersion":
            raise CatalogError(409, "unsupportedVersion")
        catalog = response["catalog"]
        if catalog is None or not isinstance(finding_id, str) or not any(f["id"] == finding_id for f in catalog["findings"]):
            raise CatalogError(400, "findingUnknown")
        if not isinstance(work_folder, str) or folder_key(work_folder) != folder_key(catalog["workFolder"]):
            raise CatalogError(400, "workFolderMismatch")
        entry = {"id": finding_id, "workFolder": catalog["workFolder"], "reason": reason.strip(),
                 "closedAt": self.now().isoformat(), "closedFrom": "iphone"}
        try:
            observed_revision = self._read_closed()["revision"]
            with _exclusive_lock(self.directory):
                latest = self._read_closed()
                # Always use the lock-protected read, even if its revision happens
                # to equal observed_revision; PC writes can occur before locking.
                key = (finding_id, folder_key(catalog["workFolder"]))
                entries = [c for c in latest["closed"] if (c["id"], folder_key(c["workFolder"])) != key]
                entries.append(entry)
                if latest["revision"] == MAX_REVISION:
                    raise CatalogError(409, "closedRevisionOverflow")
                updated = {"schemaVersion": 1, "revision": latest["revision"] + 1, "closed": entries}
                self._write_closed(updated)
            return {"closedCount": len(self._closed_keys(updated, catalog["workFolder"])),
                    "closedRevision": updated["revision"]}
        except OSError:
            raise CatalogError(503, "closedUnavailable") from None

    def _write_closed(self, value: dict) -> None:
        fd, filename = tempfile.mkstemp(prefix=".closed-", suffix=".tmp", dir=self.directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _sharing_retry(lambda: os.replace(filename, self.directory / "closed.json"), replacement=True)

    def refresh(self, folder: Any = None) -> dict:
        _, catalog, _, selected = self._select(folder)
        if catalog is None and folder not in (None, "", "home"):
            raise CatalogError(400, "folderUnknown")
        try:
            response = self.refresh_client.request("agent_design.refresh", {"workFolder": selected})
        except (OSError, MycmuxError):
            raise CatalogError(503, "pcUnavailable") from None
        if not isinstance(response, dict):
            raise CatalogError(503, "pcUnavailable")
        if response.get("error") == UNKNOWN_REFRESH:
            raise CatalogError(501, "pcTooOld")
        if response.get("error") is not None or "result" not in response:
            raise CatalogError(503, "pcUnavailable")
        return {"requestedAt": self.now().isoformat()}

    def handle(self, method: str, endpoint: str, payload: dict) -> tuple[int, dict]:
        """Thin HTTP adapter; identity/Origin/CSRF remain owned by server.py."""
        try:
            if method == "GET":
                folder = payload.get("folder", [None])[0]
                if endpoint == "catalog":
                    return 200, self.catalog(folder)
                if endpoint == "document":
                    return 200, self.document(folder, payload.get("id", [None])[0])
            else:
                if endpoint == "close":
                    return 200, self.close(payload.get("folder"), payload.get("findingId"),
                                           payload.get("workFolder"), payload.get("reason"))
                if endpoint == "refresh":
                    return 202, self.refresh(payload.get("folder"))
            return 404, {"error": "not found"}
        except CatalogError as exc:
            return exc.status, {"error": exc.code}
