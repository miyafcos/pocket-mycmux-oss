"""Bounded Mac Codex observations. No process arguments or log bodies are published."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

BASE = Path.home() / "Developer" / "_win-checks" / "cmd-261008"
CONFIG_PATH = Path.home() / ".pocket-relay" / "mac-seats-config.json"
SNAPSHOT_PATH = Path.home() / ".pocket-relay" / "mac-seats.json"
MAX_SEATS = 60
RECENT_SECONDS = 24 * 3600
ACTIVE_SECONDS = 300.0
ACTIVE_INTERVAL = 30.0
IDLE_INTERVAL = 300.0
HEAD_BYTES = 2048
TAIL_BYTES = 4096
ROLLOUT_BYTES = 65536
CONTROL_BYTES = 65536
PROCESS_TIMEOUT = 3.0
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}\Z")
MODEL = re.compile(r"(?:gpt-[A-Za-z0-9._-]{1,60}|o[1-9][A-Za-z0-9._-]{0,60})\Z")
EFFORTS = frozenset(("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))
MARK = re.compile(r"(?:DONE|REVIEW-DONE|PROGRESS|BLOCKER|TEST-REQUEST-\d+|[A-Za-z0-9._-]+-(?:READY|APPROVED))\.md\Z")
PUBLIC_TEXT_CHARACTERS = re.compile(
    r"[\x20-\x7e\u3000\u3041-\u3096\u309d-\u309f"
    r"\u30a1-\u30fa\u30fd-\u30ff\u31f0-\u31ff\uff61-\uff9f"
    r"\U0001aff0-\U0001afff\U0001b000-\U0001b16f"
    r"\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ee5f\U00030000-\U0003347f"
    r"\uff01-\uff5e、。・ー〜「」『』（）［］【】…—–→←]*\Z", re.ASCII)
OPTION_FLAG = r"(?<![A-Za-z0-9])-{1,2}[A-Za-z]"
URL_SHAPE = (r"[A-Za-z][A-Za-z0-9+.-]*://|"
             r"(?<![A-Za-z0-9+.-])(?:(?i:https?|ftp|file|javascript|mailto|ssh|data|plugin):|(?i:www)\.)")
PUBLIC_ID_SHAPE = (r"[0-9A-Fa-f]{16,}|[A-Za-z0-9_]{24,}|"
                   r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
PRIVATE_SHAPE = re.compile(r"[/\\\"'`@‘’‚‛“”„‟‹›«»〝〞〟′″]|(?<![A-Za-z0-9_])[A-Za-z]:|" + OPTION_FLAG + "|" +
                           URL_SHAPE + "|" + PUBLIC_ID_SHAPE, re.ASCII)
RUN_ATTEMPTS = 2


def iso_at(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def short_text(value: str) -> Optional[str]:
    # Validate original characters before any normalization or whitespace removal.
    if (not PUBLIC_TEXT_CHARACTERS.fullmatch(value)
            or any(unicodedata.category(char)[0] in "CM" for char in value)):
        return None
    # NFKC is detection-only; accepted text retains its original characters.
    if PRIVATE_SHAPE.search(unicodedata.normalize("NFKC", value)):
        return None
    return " ".join(value.split())[:60]


class SafeFS:
    """POSIX descriptor traversal; there is no pathname-open fallback."""
    def __init__(self) -> None:
        if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
                or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
                or os.listdir not in os.supports_fd):
            raise OSError("descriptorAccessUnavailable")

    @contextmanager
    def directory(self, path: Path):
        path = Path(os.path.abspath(path))
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[1:]:
                if part in (".", ".."):
                    raise OSError("unsafeComponent")
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("notDirectory")
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def file(self, directory_fd: int, name: str):
        if name in (".", "..") or "/" in name or "\x00" in name:
            raise OSError("unsafeComponent")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("notRegularFile")
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def child_directory(self, directory_fd: int, name: str):
        if name in (".", "..") or "/" in name or "\x00" in name:
            raise OSError("unsafeComponent")
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("notDirectory")
            yield fd
        finally:
            os.close(fd)

    def info(self, directory_fd: int, name: str) -> Optional[os.stat_result]:
        try:
            with self.file(directory_fd, name) as fd:
                return os.fstat(fd)
        except OSError:
            return None

    def read(self, fd: int, amount: int, offset: int = 0) -> bytes:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, amount)

    def tail(self, fd: int, amount: int) -> str:
        size = os.fstat(fd).st_size
        offset = max(0, size - amount)
        before = self.read(fd, 1, offset - 1) if offset else b"\n"
        data = self.read(fd, amount, offset)
        if offset and before != b"\n":
            data = data.partition(b"\n")[2]
        return data.decode("utf-8", errors="replace")


@dataclass
class SeatSettings:
    allowed_patterns: List[str] = field(default_factory=lambda: [str(BASE / "wt-*" / ".dispatch"), str(BASE / "_review-*")])
    watch_file: Path = BASE / "commander_watch_seats.txt"
    sessions_root: Path = Path.home() / ".codex" / "sessions"
    output_path: Path = SNAPSHOT_PATH


def load_settings(path: Path = CONFIG_PATH) -> SeatSettings:
    """A separate, optional settings file; never read the Hermes config here."""
    defaults = SeatSettings()
    if path.is_symlink():
        raise ValueError("seatConfigSymlink")
    try:
        info = path.stat()
    except FileNotFoundError:
        return defaults
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("invalidSeatConfigFile")
    fs = SafeFS()
    with fs.directory(path.parent) as parent, fs.file(parent, path.name) as fd:
        data = fs.read(fd, CONTROL_BYTES + 1)
    if len(data) > CONTROL_BYTES:
        raise ValueError("seatConfigTooLarge")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalidSeatConfig")
    patterns = value.get("allowedPatterns", defaults.allowed_patterns)
    if (not isinstance(patterns, list) or len(patterns) > 32
            or any(not isinstance(item, str) or not item or len(item) > 4096
                   or not Path(item).expanduser().is_absolute() for item in patterns)):
        raise ValueError("invalidSeatPatterns")
    paths = {}
    for key, fallback in (("watchFile", defaults.watch_file), ("sessionsRoot", defaults.sessions_root),
                          ("outputPath", defaults.output_path)):
        item = value.get(key, str(fallback))
        if not isinstance(item, str) or not item or len(item) > 4096:
            raise ValueError("invalidSeatPath")
        expanded = Path(item).expanduser()
        if not expanded.is_absolute():
            raise ValueError("seatPathMustBeAbsolute")
        paths[key] = expanded
    return SeatSettings(patterns, paths["watchFile"], paths["sessionsRoot"], paths["outputPath"])


class ProcessSeats(set):
    """Candidate paths are private; unresolved process lines carry only a count."""
    def __init__(self) -> None:
        super().__init__()
        self.unattributed = 0
        self.counts = {}


def process_seats(output: str) -> Set[Path]:
    """Accept one exact output value. Never reconstruct it from later tokens."""
    found = ProcessSeats()
    lines = output.splitlines()
    for index, line in enumerate(lines):
        columns = line.strip().split(None, 2)
        if len(columns) != 3 or not columns[0].isdigit():
            continue
        lexer = shlex.shlex(columns[2], posix=True)
        lexer.whitespace_split, lexer.commenters = True, ""
        recognized = False
        try:
            executable = next(lexer)
            if PurePosixPath(executable).name in ("node", "node.exe"):
                executable = next(lexer)
                if PurePosixPath(executable).name not in ("codex", "codex.js"):
                    continue
            elif PurePosixPath(executable).name not in ("codex", "codex.exe"):
                continue
            if next(lexer) != "exec":
                continue
            recognized = True
            if index + 1 < len(lines):
                following = lines[index + 1].strip().split(None, 2)
                if len(following) != 3 or not following[0].isdigit():
                    found.unattributed += 1
                    continue
            targets, options = [], True
            for token in lexer:
                if token == "--":
                    options = False
                    continue
                if not options:
                    continue
                if token in ("-o", "--output-last-message"):
                    target = next(lexer)
                elif token.startswith("--output-last-message="):
                    target = token.partition("=")[2]
                elif token.startswith("-o="):
                    target = token[3:]
                else:
                    continue
                targets.append(target)
            if len(targets) == 1:
                target = targets[0]
                path = PurePosixPath(target)
                if (len(target) <= 4096 and not any(char in target for char in "\r\n\x00")
                        and path.is_absolute() and path.name == "last_message.md"
                        and ".." not in path.parts and str(path) == target):
                    found.add(Path(path.parent))
                    parent = Path(path.parent)
                    found.counts[parent] = found.counts.get(parent, 0) + 1
                    continue
            found.unattributed += 1
        except (ValueError, StopIteration):
            if recognized:
                found.unattributed += 1
    return found


def _stdout(result: Any) -> str:
    output = result.stdout
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output or ""


def read_process_seats(runner: Callable[..., Any] = subprocess.run) -> Set[Path]:
    result = runner(["ps", "-axww", "-o", "pid=,etime=,command="],
                    capture_output=True, timeout=PROCESS_TIMEOUT)
    if result.returncode != 0:
        raise OSError("processReadFailed")
    return process_seats(_stdout(result))


def mac_pressure(runner: Callable[..., Any] = subprocess.run,
                 load_reader: Optional[Callable[[], Tuple[float, float, float]]] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"memFreePct": None, "swapUsedGiB": None, "loadAvg1": None}
    for command, key, pattern in (
        (["/usr/bin/memory_pressure"], "memFreePct", r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%"),
        (["/usr/sbin/sysctl", "vm.swapusage"], "swapUsedGiB", r"\bused\s*=\s*(\d+(?:\.\d+)?)\s*([KMGT]?)")):
        try:
            response = runner(command, capture_output=True, timeout=PROCESS_TIMEOUT,
                              env=dict(os.environ, LC_ALL="C"))
            match = re.search(pattern, _stdout(response)) if response.returncode == 0 else None
            if match:
                value = float(match.group(1))
                if key == "swapUsedGiB":
                    value *= {"": 2**-30, "K": 2**-20, "M": 2**-10, "G": 1, "T": 2**10}[match.group(2)]
                result[key] = round(value, 2)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        reader = load_reader if load_reader is not None else getattr(os, "getloadavg", None)
        if reader is not None:
            load = reader()[0]
            result["loadAvg1"] = round(load, 2) if math.isfinite(load) else None
    except (OSError, ValueError):
        pass
    return result


def _pattern_parts(pattern: str) -> Tuple[str, ...]:
    parts = Path(os.path.abspath(os.path.expanduser(pattern))).parts
    split = next((index for index, part in enumerate(parts) if any(char in part for char in "*?[")), len(parts))
    anchor = Path(*parts[:split])
    if anchor.resolve() != anchor:
        raise ValueError("seatPatternSymlink")
    return anchor.parts + parts[split:]


class AllowedSeats:
    def __init__(self, patterns: List[str]) -> None:
        self.patterns = [_pattern_parts(pattern) for pattern in patterns]

    def allows(self, path: Path) -> bool:
        # Pure lexical admission is followed by a no-follow descriptor walk.
        # Do not resolve/stat an external process argument.
        parts = path.parts
        if not path.is_absolute() or ".." in parts:
            return False
        return any(len(parts) == len(pattern) and all(fnmatch.fnmatchcase(part, rule)
                   for part, rule in zip(parts, pattern)) for pattern in self.patterns)

    def directories(self, fs: Optional[SafeFS] = None) -> Set[Path]:
        """List and descend using anchored descriptors, including ancestors."""
        fs = fs or SafeFS()
        found: Set[Path] = set()
        for pattern in self.patterns:
            split = next((i for i, part in enumerate(pattern) if any(c in part for c in "*?[")), len(pattern))
            anchor = Path(*pattern[:split])
            def expand(fd, directory, index):
                if index == len(pattern):
                    found.add(directory)
                    return
                component = pattern[index]
                choices = os.listdir(fd) if any(c in component for c in "*?[") else [component]
                for name in choices:
                    if fnmatch.fnmatchcase(name, component):
                        try:
                            with fs.child_directory(fd, name) as child:
                                expand(child, directory / name, index + 1)
                        except OSError:
                            continue
            try:
                with fs.directory(anchor) as fd:
                    expand(fd, anchor, split)
            except OSError:
                continue
        return found


def _tail(path: Path, amount: int) -> str:
    fs = SafeFS()
    with fs.directory(path.parent) as parent, fs.file(parent, path.name) as fd:
        return fs.tail(fd, amount)


@dataclass
class RunFacts:
    run: Optional[str] = None
    started: Optional[float] = None
    label: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    exit: Optional[int] = None
    session: Optional[str] = field(default=None, repr=False)


def run_facts(head: str, tail: str) -> RunFacts:
    result = RunFacts()
    lines = head.splitlines()
    first = lines[0] if lines else ""
    start = re.match(r"^(START|RESUME)\s+(\S+)(?:\s|$)", first)
    if start:
        result.run, result.started = start.group(1).lower(), timestamp(start.group(2))
    if result.started is None:
        return result
    for key in ("label", "model", "effort"):
        match = re.search(r"(?:^|\s)" + key + r"=([^\s]+)(?:\s|$)", first)
        if match:
            setattr(result, key, match.group(1))
    # Codex's header finishes before the prompt begins. Do not take metadata
    # from later terminal output that happens to look like a header.
    separators = 0
    for line in lines[1:]:
        if not line.strip() or line.startswith("OpenAI Codex "):
            continue
        if line.strip() == "--------":
            separators += 1
            if separators == 2:
                break
            continue
        if line.strip() in ("user", "assistant", "thinking", "exec"):
            break
        handled = False
        for prefix, key in (("model:", "model"), ("reasoning effort:", "effort"), ("session id:", "session")):
            if line.startswith(prefix):
                setattr(result, key, line[len(prefix):].strip())
                handled = True
        if not handled and not line.startswith(("workdir:", "provider:", "approval:", "approval policy:",
                                               "sandbox:", "reasoning summaries:")):
            break
    result.label = result.label if result.label and TOKEN.fullmatch(result.label) and not UUID.search(result.label) else None
    result.model = result.model if result.model and MODEL.fullmatch(result.model) else None
    result.effort = result.effort if result.effort in EFFORTS else None
    result.session = result.session if result.session and UUID.fullmatch(result.session) else None
    last = tail.rstrip().splitlines()
    exit_line = re.fullmatch(r"EXIT\s+(-?\d+)\s+(\S+)(?:\s+.*)?", last[-1]) if last else None
    if exit_line and result.started is not None:
        ended = timestamp(exit_line.group(2))
        if ended is not None and ended >= result.started:
            result.exit = int(exit_line.group(1))
    return result


def error_kind(tail: str, started: Optional[float]) -> Optional[str]:
    """Only the last current-run task_complete matters; its text never escapes."""
    if started is None:
        return None
    for line in reversed(tail.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if event.get("type") == "event_msg" else event
        if not isinstance(payload, dict) or payload.get("type") != "task_complete":
            continue
        at = timestamp(event.get("timestamp"))
        if at is None or at < started:
            return None
        error = payload.get("error")
        if error is None or error is False or error == "":
            return None
        if isinstance(error, dict):
            known = {"usage_limit_reached": "usageLimit", "rate_limit_exceeded": "usageLimit",
                     "usage_limit": "usageLimit", "server_overloaded": "congestion",
                     "server_busy": "congestion", "congestion": "congestion"}
            identifiers = [error[key] for key in ("type", "code") if key in error]
            categories = [known.get(value) if isinstance(value, str) else None for value in identifiers]
            return categories[0] if categories and None not in categories and len(set(categories)) == 1 else "other"
        if not isinstance(error, str):
            return "other"
        text = error.lower()
        if re.search(r"usage[_ -]?limit|rate[_ -]?limit|quota|使用量.*上限|利用.*上限", text):
            return "usageLimit"
        if re.search(r"overload|capacity|congest|server[_ -]?busy|混雑", text):
            return "congestion"
        return "other"
    return None


class RunChanged(OSError):
    pass


def _stamp(info: os.stat_result) -> Tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class SeatCollector:
    def __init__(self, settings: SeatSettings, *, process_reader: Callable[[], Set[Path]] = read_process_seats,
                 mac_reader: Callable[[], Dict[str, Any]] = mac_pressure,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings, self.process_reader, self.mac_reader, self.clock = settings, process_reader, mac_reader, clock
        self.allowed = AllowedSeats(settings.allowed_patterns)
        self.fs = SafeFS()
        self._previous = {}

    def _watched(self) -> Set[str]:
        path = self.settings.watch_file
        try:
            with self.fs.directory(path.parent) as parent, self.fs.file(parent, path.name) as fd:
                data = self.fs.read(fd, CONTROL_BYTES)
        except OSError:
            return set()
        return {line.strip() for line in data.decode("utf-8", errors="replace").splitlines()
                if TOKEN.fullmatch(line.strip())}

    def _limit(self, facts: RunFacts) -> Optional[str]:
        if facts.session is None or facts.started is None:
            return None
        root = Path(os.path.abspath(self.settings.sessions_root))
        latest, held = None, None
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date()
        # Retain only the newest descriptor, not one descriptor per matching file.
        try:
            for offset in range(3):
                date = day - timedelta(days=offset)
                directory = root / str(date.year) / f"{date.month:02d}" / f"{date.day:02d}"
                try:
                    with self.fs.directory(directory) as parent:
                        for name in os.listdir(parent):
                            if fnmatch.fnmatchcase(name, "rollout-*-" + facts.session + ".jsonl"):
                                context = self.fs.file(parent, name)
                                try:
                                    fd = context.__enter__()
                                except OSError:
                                    continue
                                retained = False
                                try:
                                    candidate = (os.fstat(fd).st_mtime_ns, fd)
                                    if latest is None or candidate[0] > latest[0]:
                                        if held is not None: held.__exit__(None, None, None)
                                        latest, held = candidate, context
                                        retained = True
                                finally:
                                    if not retained: context.__exit__(None, None, None)
                except OSError:
                    continue
            if latest is None:
                return None
            _, fd = latest
            return error_kind(self.fs.tail(fd, ROLLOUT_BYTES), facts.started)
        finally:
            if held is not None: held.__exit__(None, None, None)

    def _base_row(self, directory: Optional[Path], running: bool, watched: bool,
                  restricted: bool, count: int = 1) -> Dict[str, Any]:
        # Named rows are constructed only after their allowed folder was opened.
        row = {"id": None, "name": None, "group": None, "kind": "external",
               "label": None, "run": None, "model": None, "effort": None, "startedAt": None,
               "running": running, "exit": None, "lastOutputAt": None, "progress": None, "blocker": None,
               "marks": {}, "flags": {"done": False, "ready": [], "testRequest": False, "blocker": False, "limit": None},
               "state": "running" if running else "unknown", "watched": watched, "restricted": restricted,
               "count": count, "withheld": [], "runUnstable": False}
        if directory is not None and not restricted:
            name = directory.parent.name if directory.name == ".dispatch" else directory.name
            group = directory.parent.parent.name if directory.name == ".dispatch" else directory.parent.name
            row.update(id=hashlib.sha256(os.fsencode(directory)).hexdigest()[:24],
                       kind="review" if name.startswith("_review-") else "seat")
            for field, value in (("name", name), ("group", group)):
                self._text(row, row, field, field, value)
        return row

    def _text(self, row, container, field, mark, value):
        sanitized = short_text(value)
        container[field] = sanitized
        if sanitized is None:
            row["withheld"].append(mark)

    def _read_row(self, parent: int, directory: Path, running: bool, watched: bool) -> Dict[str, Any]:
        row = self._base_row(directory, running, watched, False)
        try:
            context = self.fs.file(parent, "run.log")
            fd = context.__enter__()
        except FileNotFoundError:
            info = os.fstat(parent)
            if (info.st_dev, info.st_ino) in self._previous:
                raise RunChanged("runChanged")
            return row
        try:
            initial = os.fstat(fd)
            head_bytes = self.fs.read(fd, HEAD_BYTES)
            if b"\n" not in head_bytes and initial.st_size > len(head_bytes):
                raise RunChanged("runChanged")
            parse_head = head_bytes
            if initial.st_size > len(head_bytes) and not head_bytes.endswith(b"\n"):
                parse_head = head_bytes.rpartition(b"\n")[0]
            head = parse_head.decode("utf-8", errors="replace")
            facts = run_facts(head, self.fs.tail(fd, TAIL_BYTES))
            row["lastOutputAt"] = iso_at(initial.st_mtime)
            row.update(label=facts.label, run=facts.run, model=facts.model, effort=facts.effort,
                       startedAt=iso_at(facts.started) if facts.started is not None else None, exit=facts.exit)
            for field in ("label", "model"):
                if row[field] is not None and short_text(row[field]) != row[field]:
                    row[field] = None
                    row["withheld"].append(field)
            current = {}
            for name in os.listdir(parent):
                if MARK.fullmatch(name):
                    info = self.fs.info(parent, name)
                    if info is not None and facts.started is not None and info.st_mtime > facts.started:
                        current[name] = info.st_mtime
            for name, at in current.items():
                safe_name = short_text(name)
                if safe_name == name:
                    row["marks"][name] = iso_at(at)
                elif "marks" not in row["withheld"]:
                    row["withheld"].append("marks")
            flags = row["flags"]
            flags["done"] = "DONE.md" in current or "REVIEW-DONE.md" in current
            flags["testRequest"] = any(name.startswith("TEST-REQUEST-") for name in current)
            flags["blocker"] = "BLOCKER.md" in current
            for name, at in current.items():
                if name.endswith("-READY.md") and current.get(name[:-9] + "-APPROVED.md", 0) <= at:
                    display = name[:-3]
                    if short_text(display) != display:
                        display = "[withheld]"
                        if "flags.ready" not in row["withheld"]: row["withheld"].append("flags.ready")
                    flags["ready"].append(display)
            flags["ready"].sort()
            for name, field in (("PROGRESS.md", "progress"), ("BLOCKER.md", "blocker")):
                if name in current:
                    try:
                        with self.fs.file(parent, name) as text_fd:
                            info = os.fstat(text_fd)
                            if info.st_mtime <= facts.started:
                                continue
                            if field == "progress":
                                lines = self.fs.tail(text_fd, TAIL_BYTES).split("\n")
                                headings = [(line[:-1] if index < len(lines) - 1 and line.endswith("\r") else line)[3:]
                                            for index, line in enumerate(lines) if line.startswith("## ")]
                                heading = headings[-1] if headings else None
                            else:
                                first, newline, _ = self.fs.read(text_fd, 512).partition(b"\n")
                                if newline and first.endswith(b"\r"):
                                    first = first[:-1]
                                heading = first.decode("utf-8", errors="replace")
                            row[field] = {"at": iso_at(info.st_mtime), "heading": None}
                            if heading is not None:
                                self._text(row, row[field], "heading", field + ".heading", heading)
                    except OSError:
                        row["withheld"].append(field + ".heading")
            flags["limit"] = self._limit(facts)
            if running:
                row["state"] = "running"
            elif flags["ready"]:
                row["state"] = "approval"
            elif flags["limit"] in ("usageLimit", "congestion"):
                row["state"] = flags["limit"]
            elif facts.exit == 0:
                row["state"] = "completed" if flags["done"] else "completedNoReport"
            elif facts.exit is not None:
                row["state"] = "failed"
            elif facts.started is not None:
                row["state"] = "disappeared"
            final_head = self.fs.read(fd, HEAD_BYTES).split(b"\n", 1)[0]
            final = os.fstat(fd)
            entry = self.fs.info(parent, "run.log")
            if (entry is None or _stamp(initial) != _stamp(final) or _stamp(final) != _stamp(entry)
                    or head_bytes.split(b"\n", 1)[0] != final_head):
                raise RunChanged("runChanged")
            return row
        finally:
            context.__exit__(None, None, None)

    def _row(self, directory: Path, running: bool, watched: bool) -> Dict[str, Any]:
        if not self.allowed.allows(directory):
            return self._base_row(None, running, False, True)
        with self.fs.directory(directory) as parent:
            info = os.fstat(parent)
            key = (info.st_dev, info.st_ino)
            for attempt in range(RUN_ATTEMPTS):
                try:
                    row = self._read_row(parent, directory, running, watched)
                    self._previous[key] = copy.deepcopy(row)
                    return row
                except OSError:
                    continue
            row = copy.deepcopy(self._previous.get(key)) or self._base_row(directory, running, watched, False)
            row["runUnstable"] = True
            return row

    def collect(self) -> Dict[str, Any]:
        supplied = self.process_reader()
        counts = getattr(supplied, "counts", {})
        anonymous = getattr(supplied, "unattributed", 0)
        running = set()
        for directory in supplied:
            if self.allowed.allows(directory):
                running.add(directory)
            else:
                anonymous += counts.get(directory, 1)
        watched = self._watched()
        candidates = list(sorted(running, key=str))
        recent = []
        for directory in self.allowed.directories(self.fs):
            name = directory.parent.name if directory.name == ".dispatch" else directory.name
            if name in watched:
                candidates.append(directory)
            else:
                try:
                    with self.fs.directory(directory) as parent:
                        info = self.fs.info(parent, "run.log")
                    if info is not None and info.st_mtime >= self.clock() - RECENT_SECONDS:
                        recent.append((info.st_mtime, directory))
                except OSError:
                    continue
        candidates.extend(directory for _, directory in sorted(recent, key=lambda item: (-item[0], str(item[1]))))
        unique = list(dict.fromkeys(candidates))
        errors, rows = [], []
        for directory in unique:
            if len(rows) >= MAX_SEATS:
                if "seatsTruncated" not in errors: errors.append("seatsTruncated")
                break
            name = directory.parent.name if directory.name == ".dispatch" else directory.name
            try:
                row = self._row(directory, directory in running, name in watched)
                rows.append(row)
                if row["runUnstable"] and "runChanged" not in errors: errors.append("runChanged")
            except OSError:
                if directory in running: anonymous += counts.get(directory, 1)
                if "seatReadFailed" not in errors: errors.append("seatReadFailed")
        if anonymous:
            if len(rows) == MAX_SEATS:
                rows.pop()
                if "seatsTruncated" not in errors: errors.append("seatsTruncated")
            rows.append(self._base_row(None, True, False, True, anonymous))
        mac = self.mac_reader()
        for key, error in (("memFreePct", "memoryPressureUnavailable"), ("swapUsedGiB", "swapUnavailable"),
                           ("loadAvg1", "loadUnavailable")):
            if mac.get(key) is None: errors.append(error)
        # Cache is bounded too; no process paths or raw source text enter it.
        if len(self._previous) > MAX_SEATS:
            self._previous = dict(list(self._previous.items())[-MAX_SEATS:])
        return {"generatedAt": iso_at(self.clock()), "ageSeconds": 0, "mac": mac, "seats": rows, "errors": errors}


def write_snapshot(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".mac-seats-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class MacSeatsWatcher:
    def __init__(self, collector: Callable[[], Dict[str, Any]], *, output_path: Path = SNAPSHOT_PATH,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.collector, self.output_path, self.clock, self.monotonic = collector, output_path, clock, monotonic
        self._lock, self._collect_lock = threading.Lock(), threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._last_request: Optional[float] = None
        self.thread: Optional[threading.Thread] = None
        self._value: Dict[str, Any] = {"generatedAt": None, "ageSeconds": None, "mac": {}, "seats": [], "errors": ["notCollected"]}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            value = copy.deepcopy(self._value)
        at = timestamp(value["generatedAt"])
        value["ageSeconds"] = max(0, int(self.clock() - at)) if at is not None else None
        return value

    def collect_once(self) -> Dict[str, Any]:
        with self._collect_lock:
            try:
                value = self.collector()
                with self._lock:
                    self._value = value
            except Exception:
                with self._lock:
                    self._value = dict(self._value, errors=["collectionFailed"])
            value = self.status()
            try:
                write_snapshot(self.output_path, value)
            except Exception:
                with self._lock:
                    self._value = dict(self._value, errors=list(self._value["errors"]) + ["snapshotWriteFailed"])
            return self.status()

    def refresh_interval(self) -> float:
        with self._lock:
            recent = self._last_request is not None and self.monotonic() - self._last_request < ACTIVE_SECONDS
        return ACTIVE_INTERVAL if recent else IDLE_INTERVAL

    def note_request(self) -> None:
        with self._lock:
            was_idle = self._last_request is None or self.monotonic() - self._last_request >= ACTIVE_SECONDS
            self._last_request = self.monotonic()
        if was_idle:
            self._wake.set()

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="mac-seats-watcher", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self.collect_once()
            self._wake.wait(self.refresh_interval())
