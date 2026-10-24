"""Incremental, rate-limited Codex usage accounting, off the shelf worker."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES_PER_SECOND = 60 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MAX_AGE_SECONDS = 90 * 86400
RECORD_VERSION = 2
USER_TOKEN = re.compile(r"\$([\w:.-]+)")
TIMESTAMP = re.compile(rb'"timestamp"\s*:\s*"([^"\\]+)"')
SKILL_PATH = re.compile(r'skills[/\\](?:[^/\\\s"\']+[/\\])*?([^/\\\s"\']+)[/\\]SKILL\.md', re.I)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def atomic_json(path, value, *, backup=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.is_file():
        stamp = datetime.now().strftime("%y%m%d-%H%M%S-%f")
        shutil.copy2(path, path.with_name(path.name + ".bak-" + stamp))
    text = json.dumps(value, ensure_ascii=True, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(text, encoding="utf-8", newline="\n")
    assert temporary.read_text(encoding="utf-8") == text and "\ufffd" not in text
    os.replace(temporary, path)


def epoch_ms(value, fallback):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() * 1000
    except (ValueError, TypeError, AttributeError, OverflowError):
        return fallback


class RateLimiter:
    def __init__(self, rate=MAX_BYTES_PER_SECOND, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.clock, self.sleep = rate, clock, sleep
        self.started, self.bytes, self.waited = clock(), 0, 0.0

    def consume(self, count):
        self.bytes += count
        delay = self.bytes / self.rate - (self.clock() - self.started)
        if delay > 0:
            self.sleep(delay)
            self.waited += delay


def aggregate(files, allowed=None):
    totals = {}
    for entry in files.values():
        if not isinstance(entry, dict):
            continue
        for name in set(entry.get("skills") or []):
            if not isinstance(name, str) or (allowed is not None and name not in allowed):
                continue
            row = totals.setdefault(name, {"count": 0, "last": None})
            row["count"] += 1
            last = entry.get("last")
            if isinstance(last, (int, float)):
                row["last"] = max(row["last"] or 0, last)
    return totals


_cached = {}
_cache_guard = threading.Lock()


SHARED_NAMES = {
    "skill_shelf.json": "shelf.json",
    "skill_shelf_auto.json": "shelf_auto.json",
    "skill_usage_codex.json": "usage_codex.json",
}


def shared_path(state_dir, name, *, home=None, for_write=False):
    """One shared owner; state_dir remains the migration fallback only."""
    directory = Path.home() if home is None else Path(home)
    path = directory / ".mycmux" / "skills" / SHARED_NAMES[name]
    return path if for_write or path.is_file() else Path(state_dir) / name


def migrate_shared(state_dir, *, home=None, defaults_path=None):
    """Copy existing bytes once; exclusive creation never replaces a file."""
    copied = []
    for old, new in SHARED_NAMES.items():
        source = Path(state_dir) / old
        destination = shared_path(state_dir, old, home=home, for_write=True)
        if not source.is_file() or destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Read before reserving the destination; a read failure creates nothing.
        data = source.read_bytes()
        if old == "skill_shelf.json" and defaults_path is not None:
            # Personal shipped definitions must leave the public PC bundle.
            # Copy the effective legacy layers together into the new owner.
            def merge_layers(base, overlay):
                if not isinstance(base, dict) or not isinstance(overlay, dict):
                    return overlay
                result = dict(base)
                for key, value in overlay.items():
                    result[key] = merge_layers(result.get(key), value)
                return result
            previous = json.loads(data.decode("utf-8-sig"))
            combined = merge_layers(read_json(defaults_path), previous)
            # Retain provenance for the PC/pocket parity report. Both readers
            # intentionally treat the copied layer as the personal owner.
            combined["migration"] = {"source": "legacy-effective-layers", "version": 1}
            data = (json.dumps(combined, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
        try:
            with destination.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            continue
        copied.append(str(destination))
    return copied


def read_usage(state_dir, *, home=None):
    """Direct collectors reuse aggregates rather than reparsing a large ledger."""
    path = shared_path(state_dir, "skill_usage_codex.json", home=home)
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size, int(time.time() // 86400))
    except OSError:
        return {}
    with _cache_guard:
        previous = _cached.get(path)
        if previous is None or previous[0] != stamp:
            cutoff = time.time() * 1000 - MAX_AGE_SECONDS * 1000
            files = {key: row for key, row in read_json(path).items()
                     if isinstance(row, dict) and row.get("mtimeNs", 0) / 1_000_000 >= cutoff}
            previous = (stamp, aggregate(files))
            _cached[path] = previous
        return previous[1]


class CodexUsage:
    def __init__(self, home, state_dir, *, on_update=lambda: None,
                 clock=time.monotonic, now=time.time, rate=MAX_BYTES_PER_SECOND,
                 sleep=None, checkpoint_seconds=5.0):
        self.home, self.state_dir = Path(home), Path(state_dir)
        self.path = shared_path(self.state_dir, "skill_usage_codex.json", home=self.home, for_write=True)
        previous_path = shared_path(self.state_dir, "skill_usage_codex.json", home=self.home)
        cutoff = now() - MAX_AGE_SECONDS
        self._dirty = True
        self.files = {key: row for key, row in read_json(previous_path).items()
                      if isinstance(row, dict) and row.get("mtimeNs", 0) / 1_000_000_000 >= cutoff}
        self.clock, self.now, self.rate = clock, now, rate
        self._stop, self._wake = threading.Event(), threading.Event()
        self.sleep = sleep or self._stop.wait
        self.checkpoint_seconds = checkpoint_seconds
        self.on_update = on_update
        self._guard = threading.Lock()
        self._allowed, self._folders, self._matcher, self._names = set(), {}, None, {}
        self._totals = aggregate(self.files)
        self.thread = None
        self.initial_done = threading.Event()
        self.last_error = None
        self.last_scan = {}

    def set_skills(self, rows):
        allowed, folders, names = set(), {}, {}
        for row in rows:
            if "codex" not in row.get("agents", []):
                continue
            name = row["id"].casefold()
            allowed.add(name)
            names[name] = name
            for alias in row.get("aliases", []):
                names[alias.casefold()] = name
            folders.setdefault(row["id"].split(":")[-1].casefold(), set()).add(name)
            if row.get("docPath"):
                folders.setdefault(Path(row["docPath"]).parent.name.casefold(), set()).add(name)
        with self._guard:
            changed = allowed != self._allowed or folders != self._folders or names != self._names
            before = {key: value for key, value in self._totals.items() if key in self._allowed}
            after = {key: value for key, value in self._totals.items() if key in allowed}
            self._allowed, self._folders, self._names = allowed, folders, names
            self._matcher = re.compile(r"\$(" + "|".join(re.escape(n) for n in sorted(allowed, key=len, reverse=True))
                                       + r")(?![\w:.+-])", re.I) if allowed else None
        if changed:
            self._wake.set()
            if before != after:
                self.on_update()

    def snapshot(self):
        with self._guard:
            return {key: dict(value) for key, value in self._totals.items() if key in self._allowed}

    def _publish(self):
        with self._guard:
            names, folders = dict(self._names), dict(self._folders)
        for entry in self.files.values():
            if "mentions" in entry and "paths" in entry:
                resolved = {names[value] for value in entry["mentions"] if value in names}
                for folder in entry["paths"]:
                    resolved.update(folders.get(folder, ()))
                if entry["skills"] != sorted(resolved):
                    entry["skills"] = sorted(resolved)
                    self._dirty = True
        totals = aggregate(self.files)
        with self._guard:
            changed = totals != self._totals
            self._totals = totals
        if self._dirty:
            atomic_json(self.path, self.files)
            self._dirty = False
        if changed:
            self.on_update()

    def _matches(self, line, skills, matcher, folders, mentions, paths):
        if b'SKILL.md' not in line and b'$' not in line:
            return
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(row, dict) or row.get("type") != "response_item":
            return
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content")
            if isinstance(content, list):
                content = "\n".join(item.get("text", "") for item in content
                                    if isinstance(item, dict) and isinstance(item.get("text"), str))
            if isinstance(content, str):
                mentions.update(match.group(1).casefold().rstrip(".") for match in USER_TOKEN.finditer(content))
                if matcher:
                    skills.update(match.group(1).casefold() for match in matcher.finditer(content))
        elif payload.get("type") in {"function_call", "custom_tool_call", "tool_call"}:
            arguments = payload.get("arguments", payload.get("input", ""))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            def strings(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, dict):
                    for child in value.values():
                        yield from strings(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from strings(child)
            for text in strings(arguments):
                normalized = re.sub(r"[/\\]+", "/", text)
                for match in SKILL_PATH.finditer(normalized):
                    folder = match.group(1).casefold()
                    paths.add(folder)
                    skills.update(folders.get(folder, ()))

    def _read_file(self, path, stat, limiter, matcher, folders):
        key = str(path)
        old = self.files.get(key) or {}
        identity = [stat.st_dev, stat.st_ino]
        offset = old.get("offset", 0)
        reset = (not old or old.get("recordVersion") != RECORD_VERSION or old.get("identity") != identity or stat.st_size < old.get("size", 0)
                 or (stat.st_size == old.get("size") and stat.st_mtime_ns != old.get("mtimeNs")))
        if not reset and not old.get("interrupted") and stat.st_size == old.get("size") and stat.st_mtime_ns == old.get("mtimeNs"):
            return 0
        with path.open("rb") as stream:
            if old and not reset:
                # Detect replacement-in-place even when the new file is longer.
                stream.seek(max(0, offset - 256))
                anchor = stream.read(min(256, offset))
                limiter.consume(len(anchor))
                reset = hashlib.sha256(anchor).hexdigest() != old.get("anchor")
            if reset:
                offset, skills, last_text, mentions, paths = 0, set(), None, set(), set()
            else:
                skills, last_text = set(old.get("skills", [])), old.get("lastTimestamp")
                mentions, paths = set(old.get("mentions", [])), set(old.get("paths", []))
            stream.seek(offset)
            pending, consumed = b"", 0
            while offset + len(pending) < stat.st_size and not self._stop.is_set():
                chunk = stream.read(min(CHUNK_BYTES, stat.st_size - offset - len(pending)))
                if not chunk:
                    break
                consumed += len(chunk)
                limiter.consume(len(chunk))
                pending += chunk
                end = pending.rfind(b"\n")
                if end < 0:
                    continue
                complete, pending = pending[:end + 1], pending[end + 1:]
                # Timestamp extraction is cheap; deserialize only possible usages.
                for line in complete.splitlines():
                    stamp = TIMESTAMP.search(line)
                    if stamp:
                        last_text = stamp.group(1).decode("ascii", errors="ignore")
                    self._matches(line, skills, matcher, folders, mentions, paths)
                offset += len(complete)
            stream.seek(max(0, offset - 256))
            anchor = stream.read(min(256, offset))
            limiter.consume(len(anchor))
        self._dirty = True
        self.files[key] = {"size": stat.st_size, "mtimeNs": stat.st_mtime_ns,
                           "offset": offset, "skills": sorted(skills),
                           "last": epoch_ms(last_text, stat.st_mtime * 1000),
                           "lastTimestamp": last_text, "identity": identity,
                           "anchor": hashlib.sha256(anchor).hexdigest(),
                           "interrupted": self._stop.is_set() and offset < stat.st_size,
                           "recordVersion": RECORD_VERSION, "mentions": sorted(mentions), "paths": sorted(paths)}
        return consumed

    def scan_once(self):
        started = self.clock()
        limiter = RateLimiter(self.rate, self.clock, self.sleep)
        cutoff = self.now() - MAX_AGE_SECONDS
        with self._guard:
            matcher, folders = self._matcher, dict(self._folders)
            allowed = set(self._allowed)
        if not allowed:
            return {"files": 0, "bytesRead": 0, "seconds": 0}
        selected, seen = [], set()
        for path in (self.home / ".codex/sessions").rglob("rollout-*.jsonl"):
            if self._stop.is_set():
                break
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                selected.append((path, stat))
                seen.add(str(path))
        pruned = {key: row for key, row in self.files.items() if key in seen}
        if len(pruned) != len(self.files):
            self._dirty = True
        self.files = pruned
        count, read_bytes, checkpoint, errors = 0, 0, self.clock(), []
        for path, stat in sorted(selected, key=lambda item: item[1].st_mtime, reverse=True):
            if self._stop.is_set():
                break
            try:
                read_bytes += self._read_file(path, stat, limiter, matcher, folders)
                count += 1
            except OSError as exc:
                errors.append({"path": str(path), "error": type(exc).__name__})
                continue
            if self.clock() - checkpoint >= self.checkpoint_seconds:
                self._publish()
                checkpoint = self.clock()
        self._publish()
        self.last_scan = {"files": count, "eligibleFiles": len(selected), "bytesRead": read_bytes,
                          "seconds": self.clock() - started, "throttledSeconds": limiter.waited,
                          "rateLimit": self.rate, "finished": not self._stop.is_set(), "errors": errors}
        return self.last_scan

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="pocket-skill-usage", daemon=True)
            self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                with self._guard:
                    ready = bool(self._allowed)
                if ready:
                    self.scan_once()
                    self.initial_done.set()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
            self._wake.wait(30)

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
