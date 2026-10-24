"""Prepared home metadata and bounded, incremental JSONL activity.

Only the single background observer touches dispatch/transcript files. HTTP
readers take immutable published snapshots; disk work never holds their lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

QUESTION_PHRASES = (
    "\u9078\u3093\u3067\u304f\u3060\u3055\u3044", "\u6c7a\u3081\u3066\u304f\u3060\u3055\u3044",
    "\u3054\u78ba\u8a8d\u304f\u3060\u3055\u3044", "\u78ba\u8a8d\u3057\u3066\u304f\u3060\u3055\u3044",
    "\u6559\u3048\u3066\u304f\u3060\u3055\u3044", "\u3069\u3046\u3057\u307e\u3059\u304b",
    "\u3069\u3061\u3089\u306b", "\u3088\u3044\u3067\u3059\u304b", "\u3044\u3044\u3067\u3059\u304b",
    "\u8fd4\u4e8b\u5f85\u3061", "\u3054\u5224\u65ad", "\u5224\u65ad\u3057\u3066\u304f\u3060\u3055\u3044",
    "\u7b54\u3048\u3066\u304f\u3060\u3055\u3044",
)
JOB_PREFIX = "# \u5171\u901a\u5883\u754c \u2014 \u5168\u81ea\u52d5\u5b9f\u884c\u30b8\u30e7\u30d6\u306b\u9069\u7528"
AUTOMATIC_NAME = re.compile(r"^(claude|codex|claude-codex|claude-codex-open|grok|agy|hermes|shell)(-\d{6})?$", re.I)
LIMIT_NOTICE = re.compile(r"you['\u2019]ve hit your (?:weekly|5[- ]hour|usage) limit|5[- ]hour limit|usage limit|weekly limit|you(?: have|'ve) (?:hit|reached) (?:your |the )?(?:usage |rate )?limit", re.I)
DAY_MS = 24 * 60 * 60 * 1000
BOOTSTRAP_MS = 2 * 60 * 60 * 1000
ACTIVE_WINDOW_SECONDS = 120
ACTIVE_REFRESH_SECONDS = 1
IDLE_REFRESH_SECONDS = 15
STATE_WRITE_SECONDS = 10
SCAN_BYTES = 32 * 1024 * 1024
BLOCK_BYTES = 256 * 1024
APPEND_BYTES = 4 * 1024 * 1024


def milliseconds(value: Any) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def plain(text: Any, limit: int = 160) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", str(text or ""))
    text = re.sub(r"[`*_#>|]", "", text)
    return " ".join(text.split())[:limit]


def question_from(text: str) -> str:
    found = ""
    fenced = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.startswith("    ") or stripped.startswith(("`", "C:\\", "C:/", "-", "|")):
            continue
        for sentence in re.split(r"(?<=[\u3002\uff1f?])\s*", stripped):
            if any(phrase in sentence for phrase in QUESTION_PHRASES):
                found = plain(sentence, 120)
    return found


def spec_heading(text: str) -> str:
    for line in text.splitlines():
        if not line.startswith("# "):
            continue
        heading = re.sub(r"^\u4f5c\u696d\u4f9d\u983c[:\uff1a]\s*", "", line[2:].strip())
        if " \u2014 " in heading:
            heading = heading.split(" \u2014 ", 1)[1].strip()
        while True:
            shorter = re.sub(r"\s*[\uff08(][^()\uff08\uff09]*[)\uff09]\s*$", "", heading).strip()
            if shorter == heading:
                return heading
            heading = shorter
    return ""


class DispatchIndex:
    def __init__(self, root: Path):
        self.root = root
        self.signature = None
        self.by_tab: dict[str, dict] = {}
        self.by_slug: dict[str, dict] = {}
        self.specs: dict[str, tuple[Any, str]] = {}

    @staticmethod
    def stamp(path: Path):
        try:
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def refresh(self):
        path = self.root / "ledger.jsonl"
        signature = self.stamp(path)
        if signature == self.signature:
            return
        tabs, slugs = {}, {}
        if signature is not None:
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(row, dict):
                            continue
                        if row.get("tab_id"):
                            tabs[str(row["tab_id"])] = row
                        if row.get("slug"):
                            slugs[str(row["slug"])] = row
            except OSError:
                return  # Preserve the last complete index on a transient failure.
        self.by_tab, self.by_slug, self.signature = tabs, slugs, signature

    def directory(self, tab: dict) -> Path | None:
        row = self.by_tab.get(tab["tabId"], {})
        if row.get("dir"):
            return Path(row["dir"])
        label = str(tab.get("label") or tab.get("title") or "")
        if label and Path(label).name == label and label not in {".", ".."}:
            return self.root / label
        return None

    def heading(self, directory: Path | None) -> str:
        if directory is None:
            return ""
        for name in ("SPEC.md", "spec.md", "BRIEF.md", "brief.md"):
            path = directory / name
            signature = self.stamp(path)
            if signature is None:
                continue
            cached = self.specs.get(str(path))
            if cached is not None and cached[0] == signature:
                title = cached[1]
            else:
                try:
                    title = spec_heading(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    title = ""
                self.specs[str(path)] = (signature, title)
            if title:
                return title
        return ""

    def metadata(self, tabs: list[dict], titles: dict[str, str], roles: dict[str, str],
                 first_requests: dict[str, str] | None = None) -> dict[str, dict]:
        live = {tab["tabId"]: tab for tab in tabs if tab.get("tabId")}
        # File/dead PTY tabs cannot be live parents.
        parents = {key: tab for key, tab in live.items() if not is_file(tab)
                   and tab.get("processStatusReason") != "no_live_pty_session"
                   and tab.get("status") != "stopped" and tab.get("lifecycle") != "declared"}
        labels = {str(tab.get("label") or tab.get("title")): key for key, tab in parents.items()}
        result, directories = {}, {}
        for key, tab in live.items():
            row = self.by_tab.get(key, {})
            parent = row.get("parent_tab_id")
            if not parent:
                owner = row.get("owner")
                parent = labels.get(owner) or self.by_slug.get(owner, {}).get("tab_id")
            if parent not in parents or parent == key:
                parent = None
            name = str(tab.get("display_name") or tab.get("displayName") or "").strip()
            canonical_name = bool(name)
            if not name and not is_file(tab):
                for field in ("label", "title"):
                    candidate = str(tab.get(field) or "").strip()
                    if (candidate and not AUTOMATIC_NAME.fullmatch(candidate)
                            and not re.match(r"^\d{6}-|^https?://", candidate, re.I)
                            and not candidate.endswith((" resumed", " handoff"))):
                        name = candidate
                        break
            source = "mycmux" if canonical_name else "label"
            directory = self.directory(tab)
            directories[key] = directory
            if not name:
                name, source = self.heading(directory), "spec"
            if not name:
                name, source = titles.get(key, ""), "claudeTitle"
            if not name:
                name, source = str(tab.get("topic") or ""), "firstRequest"
            request = (first_requests or {}).get(key, str(tab.get("topic") or ""))
            role = ("file" if is_file(tab) else "job" if request.startswith(JOB_PREFIX)
                    else "work" if parent else "talk")
            result[key] = {"displayName": name, "nameSource": source,
                           "role": roles.get(key, role), "parentTabId": parent, "rootTabId": key}
        # Invalid cyclic ledger links are cut, so every published root is stable.
        for key in result:
            current, seen = key, set()
            while result[current]["parentTabId"] in result:
                if current in seen:
                    for member in seen:
                        result[member]["parentTabId"] = None
                        if result[member]["role"] == "work" and member not in roles:
                            result[member]["role"] = "talk"
                    break
                seen.add(current)
                current = result[current]["parentTabId"]
        for key in result:
            current = key
            while result[current]["parentTabId"] in result:
                current = result[current]["parentTabId"]
            result[key]["rootTabId"] = current
        for key, value in result.items():
            if value["nameSource"] != "firstRequest":
                continue
            families = []
            for child, child_value in result.items():
                if child != key and child in parents and child_value["rootTabId"] == key:
                    directory = directories[child]
                    try:
                        relative = directory.relative_to(self.root) if directory else None
                    except ValueError:
                        relative = None
                    family_folder = relative.parts[0] if relative and relative.parts else ""
                    match = re.match(r"^\d{6}-([^-]+)-", family_folder)
                    if match:
                        families.append(match[1])
            if families:
                family = Counter(families).most_common(1)[0][0]
                value["displayName"], value["nameSource"] = family + " \u306e\u6bcd\u8266", "family"
        return result


def is_file(tab: dict) -> bool:
    return tab.get("type") == "browser" or tab.get("kind") == "web"


def compact_record(record: Any, agent: str) -> dict | None:
    if not isinstance(record, dict):
        return None
    stamp = milliseconds(record.get("timestamp"))
    typ = record.get("type")
    if agent == "codex":
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        if typ == "event_msg" and payload.get("type") in {"task_started", "task_complete", "turn_aborted"}:
            return {"at": stamp, "control": payload["type"]}
        if (typ != "response_item" or payload.get("type") != "message"
                or payload.get("role") != "assistant" or payload.get("channel") == "analysis"):
            return None
        content = payload.get("content")
        text = "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and isinstance(block.get("text"), str)
                         and block.get("type") in {None, "text", "output_text"}) if isinstance(content, list) else ""
        return reply_record(text, stamp)
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    if typ == "user" and not record.get("isMeta") and not record.get("sourceToolAssistantUUID") and record.get("toolUseResult") is None:
        if not any(isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks):
            text = content if isinstance(content, str) else "\n".join(
                block["text"] for block in blocks if isinstance(block, dict)
                and block.get("type") == "text" and isinstance(block.get("text"), str))
            if text.strip():
                return {"at": stamp, "control": "human"}
    if typ == "assistant":
        text = content if isinstance(content, str) else "\n".join(
            block["text"] for block in blocks if isinstance(block, dict)
            and block.get("type") == "text" and isinstance(block.get("text"), str))
        value = reply_record(text, stamp)
        if message.get("stop_reason") in {"end_turn", "stop_sequence"}:
            value["control"] = "claude_done"
        return value if text or "control" in value else None
    return None


def reply_record(text: str, stamp: int | None) -> dict:
    return {"at": stamp, "text": plain(text), "question": question_from(text),
            "limit": bool(LIMIT_NOTICE.search(text))}


class TranscriptObserver:
    """One file cursor, no retained transcript bytes, bounded bootstrap/append."""
    def __init__(self, path: Path, agent: str, saved: dict | None = None):
        self.path, self.agent = path, agent
        self.state = dict(saved or {})
        self.bytes_read = 0

    def _read(self, handle, size: int) -> bytes:
        raw = handle.read(size)
        self.bytes_read += len(raw)
        return raw

    def _parse(self, raw: bytes, offset: int):
        for line in raw.splitlines(keepends=True):
            position = offset
            offset += len(line)
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            stamp = milliseconds(record.get("timestamp")) if isinstance(record, dict) else None
            value = compact_record(record, self.agent)
            yield position, stamp, value

    def _bootstrap(self, handle, size: int, cutoff: int):
        position, floor, carry = size, max(0, size - SCAN_BYTES), b""
        records, anchor = [], False
        last_at = None
        while position > floor:
            start = max(floor, position - BLOCK_BYTES)
            handle.seek(start)
            payload = self._read(handle, position - start) + carry
            position, offset = start, start
            if start > 0:
                newline = payload.find(b"\n")
                if newline < 0:
                    carry = payload
                    continue
                carry, payload = payload[:newline], payload[newline + 1:]
                offset += newline + 1
            else:
                carry = b""
            for cursor, stamp, value in reversed(list(self._parse(payload, offset))):
                if last_at is None and stamp is not None:
                    last_at = stamp
                if value:
                    records.append((cursor, value))
                    anchor |= value.get("control") in {"human", "task_started"}
                if stamp is not None and stamp < cutoff and anchor:
                    return list(reversed(records)), last_at
        return list(reversed(records)), last_at

    def poll(self, now: int) -> list[dict]:
        stat = self.path.stat()
        signature = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        previous = self.state.get("signature")
        if signature == previous:
            return []
        events = []
        with self.path.open("rb") as handle:
            cursor = self.state.get("cursor", 0)
            append = (previous is not None and signature[:2] == previous[:2]
                      and signature[2] > previous[2] and cursor <= signature[2])
            if not append:
                records, last_at = self._bootstrap(handle, signature[2], now - BOOTSTRAP_MS)
                self.state = {}
                # Re-read an unterminated line when it completes on the next poll.
                handle.seek(max(0, signature[2] - BLOCK_BYTES))
                tail = self._read(handle, min(signature[2], BLOCK_BYTES))
                cursor = signature[2] if tail.endswith(b"\n") else max(0, signature[2] - len(tail) + tail.rfind(b"\n") + 1)
            else:
                handle.seek(cursor)
                remaining = signature[2] - cursor
                payload = self._read(handle, min(APPEND_BYTES, remaining))
                end = payload.rfind(b"\n") + 1
                # A record larger than the append bound triggers bounded recovery.
                if end == 0 and len(payload) == APPEND_BYTES:
                    self.state.pop("signature", None)
                    return []
                records = [(offset, value) for offset, _, value in self._parse(payload[:end], cursor) if value]
                stamps = [stamp for _, stamp, _ in self._parse(payload[:end], cursor) if stamp is not None]
                last_at = stamps[-1] if stamps else self.state.get("turnAt")
                cursor += end
            for offset, record in records:
                event = self._consume(record)
                if event and event["at"] is not None and event["at"] >= now - (DAY_MS if append else BOOTSTRAP_MS):
                    identity = f"{self.path}|{self.agent}|{offset}|{event['kind']}"
                    event["id"] = hashlib.sha256(identity.encode()).hexdigest()[:32]
                    events.append(event)
            if last_at is not None:
                self.state["turnAt"] = last_at
            self.state["cursor"] = cursor
            # If the append budget was exhausted, leave the observed size at the
            # cursor so the next tick consumes the next batch rather than skips it.
            if append and remaining > APPEND_BYTES:
                signature[2] = cursor
            self.state["signature"] = signature
        return events

    def _consume(self, record: dict) -> dict | None:
        control, stamp = record.get("control"), record.get("at")
        if "text" in record and record["text"]:
            self.state["reply"] = {key: record[key] for key in ("text", "question", "limit", "at")}
        if control in {"task_started", "human"}:
            if stamp is not None:
                self.state["turnStartedAt"] = stamp
            else:
                self.state.pop("turnStartedAt", None)
            self.state.pop("turnEndedAt", None)
            self.state["turn"] = "in_turn"
            if control == "task_started":
                return {"at": stamp, "kind": "start", "text": ""}
        if control in {"task_complete", "turn_aborted", "claude_done"}:
            self.state["turn"] = "ended"
            if stamp is not None:
                self.state["turnEndedAt"] = stamp
            reply = self.state.get("reply", {})
            kind = "report" if control == "claude_done" else "done"
            if control == "claude_done" and reply.get("question"):
                kind = "question"
            if reply.get("limit") and control != "turn_aborted":
                kind = "limit"
            text = "\u4e2d\u65ad" if control == "turn_aborted" else reply.get("question") if kind == "question" else reply.get("text", "")
            return {"at": stamp, "kind": kind, "text": text}
        return None

    def additions(self, legacy: dict) -> dict:
        result = {}
        if self.agent == "codex" and self.state.get("turn"):
            result["turn"] = self.state["turn"]
            if self.state.get("turnAt") is not None:
                result["turnAt"] = self.state["turnAt"]
        turn = result.get("turn", legacy.get("turn"))
        if self.state.get("turnStartedAt") is not None:
            result["turnStartedAt"] = self.state["turnStartedAt"]
        if turn == "ended":
            if self.state.get("turnEndedAt") is not None:
                result["turnEndedAt"] = self.state["turnEndedAt"]
            question = self.state.get("reply", {}).get("question")
            if question:
                result["question"] = question
        return result


def atomic_json(path: Path, payload: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class SeatActivity:
    def __init__(self, state_dir: Path, dispatch_root: Path, overview: Callable,
                 lines: Callable, transcript: Callable, titles: Callable,
                 *, clock: Callable = time.time, log: Callable = lambda message: None,
                 first_request: Callable | None = None,
                 monotonic: Callable = time.monotonic):
        self.state_dir, self.clock, self.log = state_dir, clock, log
        self.monotonic = monotonic
        self.dispatch = DispatchIndex(dispatch_root)
        self.overview_loader, self.lines_loader = overview, lines
        self.transcript, self.titles = transcript, titles
        self.first_request = first_request
        self._guard = threading.Lock()
        self._role_write = threading.Lock()
        self._refresh_guard = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_request = None
        self.thread: threading.Thread | None = None
        self.ready = False
        self.refreshes = 0
        self.last_error = None
        self._overview = {"workspaces": [], "bridgeCounts": {"w": 0, "p": 0, "t": 0},
                          "activeWorkspaceId": None, "mailUnreplied": 0, "generatedAt": self.generated_at()}
        self._lines = {"lines": {}, "generatedAt": self.generated_at()}
        self._grouped: list[dict] = []
        self._events: dict[str, dict] = {}
        self._observers: dict[str, TranscriptObserver] = {}
        self._saved = {}
        self._loaded = False
        self._persisted = None
        self._pending_state = None
        self._last_write = None
        self._activity_generated_at = self.generated_at()
        self._automatic_roles = {}
        self.roles = {}
        try:
            roles = json.loads((state_dir / "seat_roles.json").read_text(encoding="utf-8"))
            if isinstance(roles, dict):
                self.roles = {key: value for key, value in roles.items() if value in {"talk", "background"}}
        except (OSError, ValueError, TypeError):
            pass

    def generated_at(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    def start(self):
        self.thread = threading.Thread(target=self._run, name="pocket-seat-activity", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        else:
            with self._refresh_guard:
                self._persist(force=True)

    def note_request(self):
        now = self.monotonic()
        with self._guard:
            idle = self._last_request is None or now - self._last_request > ACTIVE_WINDOW_SECONDS
            self._last_request = now
            if idle:
                self._wake.set()

    def refresh_interval(self):
        with self._guard:
            recent = self._last_request is not None and self.monotonic() - self._last_request <= ACTIVE_WINDOW_SECONDS
        return ACTIVE_REFRESH_SECONDS if recent else IDLE_REFRESH_SECONDS

    def _run(self):
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    self.refresh_once()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    self.log("seat_activity " + self.last_error)
                self._wake.wait(self.refresh_interval())
        finally:
            try:
                with self._refresh_guard:
                    self._persist(force=True)
            except OSError as exc:
                self.log("seat_activity " + type(exc).__name__)

    def snapshot(self, name: str) -> dict:
        with self._guard:
            value = self._overview if name == "overview" else self._lines
        return copy.deepcopy(value)

    def activity(self, limit: int = 60, before: int | None = None) -> dict:
        now = int(self.clock() * 1000)
        with self._guard:
            rows = self._grouped
            generated_at = self._activity_generated_at
        rows = [row for row in rows if row["at"] >= now - DAY_MS and (before is None or row["at"] < before)]
        return {"events": copy.deepcopy(rows[:limit]), "hasMore": len(rows) > limit,
                "generatedAt": generated_at}

    def set_role(self, tab_id: Any, role: Any):
        if (not isinstance(tab_id, str) or not tab_id.strip() or len(tab_id) > 256
                or role not in ("talk", "background", None)):
            raise ValueError("seatRoleInvalid")
        with self._role_write:
            roles = dict(self.roles)
            if role is None:
                roles.pop(tab_id, None)
            else:
                roles[tab_id] = role
            atomic_json(self.state_dir / "seat_roles.json", roles)
            with self._guard:
                self.roles = roles
                # The next tick recomputes the automatic role; explicit choices
                # are visible immediately without reading any dispatch files.
                applied = role or self._automatic_roles.get(tab_id)
                if applied is not None:
                    overview = copy.deepcopy(self._overview)
                    for tab in all_tabs(overview):
                        if tab.get("tabId") == tab_id:
                            tab["role"] = applied
                    grouped = copy.deepcopy(self._grouped)
                    for event in grouped:
                        if tab_id in event["tabIds"]:
                            event["roles"][tab_id] = applied
                            if event["tabIds"][0] == tab_id:
                                event["role"] = applied
                    self._overview, self._grouped = overview, grouped

    def refresh_once(self):
        with self._refresh_guard:
            self._refresh()

    def _refresh(self):
        now = int(self.clock() * 1000)
        if not self._loaded:
            try:
                saved = json.loads((self.state_dir / "seat_activity.json").read_text(encoding="utf-8"))
                self._events = {row["id"]: row for row in saved.get("events", []) if row["at"] >= now - DAY_MS}
                self._saved = saved.get("observers", {})
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self._loaded = True
        overview = copy.deepcopy(self.overview_loader())
        lines = self.lines_loader(overview)
        self.dispatch.refresh()
        tabs, titles = all_tabs(overview), {}
        for tab in tabs:
            if not is_file(tab) and tab.get("claudeSessionId"):
                titles[tab["tabId"]] = self.titles(tab["claudeSessionId"])[0]
        with self._guard:
            roles = dict(self.roles)
        requests = {tab["tabId"]: self.first_request(tab) for tab in tabs if not is_file(tab)} if self.first_request else {}
        metadata = self.dispatch.metadata(tabs, titles, {}, requests)
        automatic_roles = {key: value["role"] for key, value in metadata.items()}
        for key, value in metadata.items():
            value["role"] = roles.get(key, value["role"])
        live_observers = {}
        for tab in tabs:
            identity = tab.get("tabId")
            if identity in metadata:
                tab.update(metadata[identity])
            if is_file(tab):
                continue
            target = line_target(tab)
            if target is None:
                continue
            key, session_id, agent = target
            path = self.transcript(session_id, agent)
            if path is None:
                continue
            observer_key = agent + ":" + str(path)
            observer = self._observers.get(observer_key)
            if observer is None:
                observer = TranscriptObserver(path, agent, self._saved.get(observer_key))
            live_observers[observer_key] = observer
            try:
                events = observer.poll(now)
            except OSError:
                continue
            legacy = lines["lines"].get(key, {})
            additions = observer.additions(legacy)
            if additions:
                lines["lines"][key] = {**legacy, **additions}
            for event in events:
                event = {**event, "tabIds": [identity], "rootTabId": tab["rootTabId"], "role": tab["role"]}
                self._events[event["id"]] = event
        self._observers = live_observers
        self._events = {key: row for key, row in self._events.items() if row["at"] >= now - DAY_MS}
        # Update roles/roots for retained events when the operator moves a seat.
        for event in self._events.values():
            current = metadata.get(event["tabIds"][0])
            if current:
                event["role"], event["rootTabId"] = current["role"], current["rootTabId"]
        grouped = {}
        for event in self._events.values():
            group_key = (event["at"] // 60000, event["kind"], event["text"])
            group_id = hashlib.sha256(json.dumps(group_key, ensure_ascii=True).encode()).hexdigest()[:32]
            group = grouped.setdefault(group_key, {**event, "id": group_id, "tabIds": [], "roles": {}})
            group["at"] = max(group["at"], event["at"])
            for tab_id in event["tabIds"]:
                if tab_id not in group["tabIds"]:
                    group["tabIds"].append(tab_id)
                group["roles"][tab_id] = event["role"]
        grouped_rows = sorted(grouped.values(), key=lambda row: (-row["at"], row["id"]))
        with self._guard:
            # A POST may have completed while transcripts were being observed.
            # Apply its current role to the new snapshot before publishing it.
            for tab in tabs:
                key = tab.get("tabId")
                if key in automatic_roles:
                    tab["role"] = self.roles.get(key, automatic_roles[key])
            for event in grouped_rows:
                for key in event["tabIds"]:
                    if key in automatic_roles:
                        event["roles"][key] = self.roles.get(key, automatic_roles[key])
                event["role"] = event["roles"][event["tabIds"][0]]
            self._automatic_roles = automatic_roles
            self._overview, self._lines, self._grouped = overview, lines, grouped_rows
            self._activity_generated_at = self.generated_at()
            self.ready = True
            self.refreshes += 1
        self._pending_state = {"events": list(self._events.values()),
                               "observers": {key: observer.state for key, observer in self._observers.items()}}
        self._persist()
        self._saved = {}

    def _persist(self, *, force=False):
        if self._pending_state is None:
            return
        now = self.monotonic()
        if not force and self._last_write is not None and now - self._last_write < STATE_WRITE_SECONDS:
            return
        encoded = json.dumps(self._pending_state, ensure_ascii=True, sort_keys=True)
        if force or encoded != self._persisted:
            atomic_json(self.state_dir / "seat_activity.json", self._pending_state)
            self._persisted = encoded
            self._last_write = now


def all_tabs(overview: dict) -> list[dict]:
    return [tab for workspace in overview.get("workspaces", []) for pane in workspace.get("panes", [])
            for tab in pane.get("tabs", [])]


def line_target(tab: dict) -> tuple[str, str, str] | None:
    if tab.get("claudeSessionId"):
        value = tab["claudeSessionId"]
        return value, value, "claude"
    if (tab.get("agent") == "codex" and tab.get("linkable") is not False
            and tab.get("agentSessionId") and tab.get("terminalSessionId")):
        return "term:" + tab["terminalSessionId"], tab["agentSessionId"], "codex"
    return None
