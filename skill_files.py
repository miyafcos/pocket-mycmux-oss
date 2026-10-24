"""Read-only skill file plans and bounded ZIP exports.

Filesystem policy is supplied by the server; this module never accepts a root
from an HTTP client. The response shape follows the dispatch contract.
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import quote

LARGE_BYTES = 5 * 1024 * 1024
COLLAPSE_FILES = 200
ENTRY_BUDGET = 2000
LIMIT_BYTES = 50 * 1024 * 1024
LIMIT_FILES = 5000
SPOOL_BYTES = 16 * 1024 * 1024
NEVER_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
              "node_modules", ".venv", "venv", ".tox", ".idea", ".vscode")
NEVER_FILES = ("*.pyc", "*.pyo", ".ds_store", "thumbs.db", "desktop.ini")
RULES = (
    ("backup", ("_backup*", "backup*", "_bak*", "bak", "*_bak", "*.bak*",
                "_archive*", "archive", "*_old", "old"),
               ("*.bak", "*.bak-*", "*.bak_*", "*.orig", "*.old", "*~", "*.swp")),
    ("record", ("state", "states", "log", "logs", "history", "runs", "cache",
                "caches", "tmp", "temp", "self-improve", "store", "mirror",
                "mirror_*", "*quarantine*", "inbox", "outbox", "trash", ".trash", "learned"),
               ("*.log", "*.err", "*.jsonl", "*.db", "*.sqlite", "*.sqlite3",
                "*.lock", "state.md", "*-log.md", "*_log.md", "*.pid")),
    ("output", ("output", "outputs", "out", "reports", "dist", "build", "exports",
                "export", "generated", "results", "artifacts"), ()),
    ("config", ("config", "configs", "settings", ".claude", ".codex", ".cursor"),
               ("config.json", "*-config.json", "*_config.json", "settings.json", "settings.local.json")),
    ("data", ("data", "dataset", "datasets", "corpus"), ()),
)
REASONS = ("backup", "record", "output", "config", "data", "large")
_PRIORITY = {reason: i for i, reason in enumerate(("cache",) + REASONS)}


class SkillFilesError(RuntimeError):
    def __init__(self, status: int, code: str, **details: Any):
        super().__init__(code)
        self.status, self.code, self.details = status, code, details


@dataclass(frozen=True)
class FilePolicy:
    resolve: Callable[[str], Path]
    denied: Callable[[str], bool]
    kind: Callable[[Path], str]
    opened: Callable[[BinaryIO], Path]
    denied_patterns: tuple[str, ...]


@dataclass(frozen=True)
class SkillFile:
    parts: tuple[str, ...]
    size: int
    mtime: float
    reason: str | None

    @property
    def path(self) -> str:
        return "/".join(self.parts)


@dataclass
class SkillPlan:
    body: dict[str, Any]
    files: list[SkillFile]
    root: Path
    single: bool
    policy: FilePolicy


@dataclass
class SkillZip:
    stream: BinaryIO
    headers: dict[str, str]


@lru_cache(maxsize=64)
def _patterns(patterns: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(fnmatch.translate(p) for p in patterns) or r"(?!)")


def _stronger(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if _PRIORITY[left] <= _PRIORITY[right] else right


@lru_cache(maxsize=4096)
def _directory_reason(name: str) -> str | None:
    if name in NEVER_DIRS:
        return "cache"
    return next((reason for reason, dirs, _ in RULES if _patterns(dirs).match(name)), None)


def _file_reason(name: str, inherited: str | None, size: int) -> str | None:
    if inherited == "cache" or _patterns(NEVER_FILES).match(name):
        return "cache"
    reason = next((reason for reason, _, files in RULES if _patterns(files).match(name)), None)
    reason = _stronger(inherited, reason)
    return reason or ("large" if size > LARGE_BYTES else None)


def reason_for(parts: list[str] | tuple[str, ...], is_file: bool, size: int) -> str | None:
    lower = [part.lower() for part in parts]
    reason = None
    for name in lower[:-1] if is_file else lower:
        reason = _stronger(reason, _directory_reason(name))
    return _file_reason(lower[-1], reason, size) if is_file else reason


def _reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _no_links(path: Path) -> None:
    # Inspect the original spelling, before resolve() can hide a junction.
    for parent in reversed((path,) + tuple(path.parents)):
        if _reparse(os.lstat(parent)):
            raise SkillFilesError(403, "fileDenied")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def _entry_is_denied(entry: os.DirEntry, parent: Path, policy: FilePolicy,
                     denied_names: re.Pattern, is_dir: bool) -> bool:
    name = entry.name.lower()
    if denied_names.match(name):
        return True
    # Match the two contextual rules in server._path_is_denied as well.
    if parent.name.lower() == ".codex" and fnmatch.fnmatchcase(name, "auth*"):
        return True
    if parent.name.lower() == ".config" and name == "gws":
        return True
    # Checking each folder covers denied absolute roots without resolving the
    # same server roots tens of thousands of times for individual filenames.
    return is_dir and policy.denied(entry.path)


def _walk(root: Path, policy: FilePolicy) -> list[SkillFile]:
    files = []
    stack = [(root, (), None)]
    denied_names = _patterns(policy.denied_patterns)
    while stack:
        folder, parts, inherited = stack.pop()
        try:
            # A queued directory may have been replaced since scandir saw it.
            if _reparse(os.lstat(folder)):
                continue
            resolved = policy.resolve(os.fspath(folder))
            if not _same_path(resolved, folder):
                continue
            with os.scandir(folder) as entries:
                for entry in entries:
                    # Never stat or open a private filename.
                    if denied_names.match(entry.name.lower()):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if _reparse(info):
                            continue
                        is_dir = stat.S_ISDIR(info.st_mode)
                        if _entry_is_denied(entry, folder, policy, denied_names, is_dir):
                            continue
                        child = parts + (entry.name,)
                        if is_dir:
                            reason = _stronger(inherited, _directory_reason(entry.name.lower()))
                            stack.append((Path(entry.path), child, reason))
                        elif stat.S_ISREG(info.st_mode):
                            files.append(SkillFile(child, info.st_size, info.st_mtime,
                                _file_reason(entry.name.lower(), inherited, info.st_size)))
                    except OSError:
                        continue
        except PermissionError as exc:
            raise SkillFilesError(403, "fileDenied") from exc
        except FileNotFoundError as exc:
            if folder == root:
                raise SkillFilesError(404, "fileNotFound") from exc
        except OSError as exc:
            if folder == root:
                raise SkillFilesError(404, "fileNotFound") from exc
    return files


def _order(parts: tuple[str, ...], is_dir: bool) -> tuple:
    name = parts[-1]
    return (0 if len(parts) == 1 and not is_dir and name.lower() == "skill.md" else 1,
            int(is_dir), name.casefold(), name)


def build_plan(snapshot: dict, skill_id: str, policy: FilePolicy) -> SkillPlan:
    skill = next((row for row in snapshot["skills"] if row["id"] == skill_id), None)
    if skill is None:
        raise SkillFilesError(404, "skillNotFound")
    doc = skill.get("docPath")
    if not doc:
        raise SkillFilesError(404, "skillHasNoFiles")
    document = Path(doc)
    single = document.name.lower() != "skill.md"
    raw_root = document if single else document.parent
    root = policy.resolve(os.fspath(raw_root))
    policy.resolve(doc)
    try:
        _no_links(raw_root)
        if single:
            info = os.lstat(root)
            if not stat.S_ISREG(info.st_mode):
                raise SkillFilesError(404, "fileNotFound")
            files = [SkillFile((document.name,), info.st_size, info.st_mtime,
                               reason_for([document.name], True, info.st_size))]
        else:
            if not stat.S_ISDIR(os.lstat(root).st_mode):
                raise SkillFilesError(404, "fileNotFound")
            files = _walk(root, policy)
    except PermissionError as exc:
        raise SkillFilesError(403, "fileDenied") from exc
    except OSError as exc:
        raise SkillFilesError(404, "fileNotFound") from exc

    folders, children, folder_reasons = {}, {}, {(): None}
    for file in files:
        children.setdefault(file.parts[:-1], []).append((False, file))
        for depth in range(1, len(file.parts)):
            key = file.parts[:depth]
            if key not in folders:
                folders[key] = dict(files=0, size=0, mtime=0.0, visibleFiles=0, visibleSize=0)
                children.setdefault(key[:-1], []).append((True, key))
                folder_reasons[key] = _stronger(folder_reasons[key[:-1]], _directory_reason(key[-1].lower()))
            aggregate = folders[key]
            aggregate["files"] += 1
            aggregate["size"] += file.size
            aggregate["mtime"] = max(aggregate["mtime"], file.mtime)
            if file.reason not in ("cache", "backup"):
                aggregate["visibleFiles"] += 1
                aggregate["visibleSize"] += file.size
    for items in children.values():
        items.sort(key=lambda item: _order(item[1] if item[0] else item[1].parts, item[0]))

    entries, hidden_entries, hidden_groups = [], [], {}
    pending = list(reversed(children.get((), [])))
    while pending:
        is_dir, item = pending.pop()
        parts = item if is_dir else item.parts
        depth, path = len(parts) - 1, "/".join(parts)
        if not is_dir:
            row = dict(path=path, name=parts[-1], depth=depth, dir=False,
                       size=item.size, mtime=round(item.mtime, 3), kind=policy.kind(Path(parts[-1])))
            if depth == 0 and parts[-1].lower() == "skill.md":
                row["role"] = "document"
            if item.reason in ("cache", "backup"):
                hidden_entries.append({**row, "hidden": item.reason})
            else:
                if item.reason:
                    row["share"] = item.reason
                entries.append(row)
        else:
            aggregate, reason = folders[parts], folder_reasons[parts]
            row = dict(path=path, name=parts[-1], depth=depth, dir=True,
                       files=aggregate["files"], size=aggregate["size"], mtime=round(aggregate["mtime"], 3))
            if reason in ("cache", "backup"):
                hidden_groups[path] = dict(path=path, reason=reason, files=aggregate["files"], size=aggregate["size"])
                hidden_entries.append({**row, "hidden": reason, "collapsed": True})
                continue
            if reason:
                row["share"] = reason
            if aggregate["visibleFiles"] != aggregate["files"]:
                row["files"], row["size"] = aggregate["visibleFiles"], aggregate["visibleSize"]
            if aggregate["files"] > COLLAPSE_FILES:
                row["collapsed"] = True
            entries.append(row)
            if not row.get("collapsed"):
                pending.extend(reversed(children.get(parts, [])))

    include, exclude, never = {}, {}, {}
    for file in files:
        if file.reason is None:
            top = file.parts[0]
            group = include.setdefault(top, dict(key=top, dir=len(file.parts) > 1, files=0, size=0))
            group["files"] += 1
            group["size"] += file.size
            if len(file.parts) == 1 and top.lower() == "skill.md":
                group["locked"] = True
        else:
            bucket = never if file.reason == "cache" else exclude
            group = bucket.setdefault(file.reason, dict(reason=file.reason, files=0, size=0, paths=set()))
            group["files"] += 1
            group["size"] += file.size
            spot = next(("/".join(file.parts[:i]) for i in range(1, len(file.parts))
                         if folder_reasons[file.parts[:i]] == file.reason), file.path)
            group["paths"].add(spot)
        if file.reason in ("cache", "backup") and not any(
                folder_reasons[file.parts[:i]] in ("cache", "backup") for i in range(1, len(file.parts))):
            hidden_groups[file.path] = dict(path=file.path, reason=file.reason, files=1, size=file.size)

    def shape(group):
        paths = sorted(group["paths"], key=lambda path: (path.count("/"), path.casefold()))
        return {**{key: value for key, value in group.items() if key != "paths"},
                "paths": paths[:3], "more": max(0, len(paths) - 3)}

    inc = sorted(include.values(), key=lambda group: (not group.get("locked", False), group["dir"], group["key"].casefold()))
    visible = [file for file in files if file.reason not in ("cache", "backup")]
    hidden = [file for file in files if file.reason in ("cache", "backup")]
    body = {
        "id": skill["id"], "label": skill.get("label", skill["id"]), "root": os.fspath(root),
        "rootName": root.name if not single else document.stem, "document": doc, "single": single,
        "summary": {"files": len(visible), "size": sum(file.size for file in visible),
                    "newest": round(max((file.mtime for file in visible), default=0.0), 3)},
        "entries": entries[:ENTRY_BUDGET], "truncated": len(entries) > ENTRY_BUDGET,
        "hidden": {"files": len(hidden), "size": sum(file.size for file in hidden),
                   "groups": sorted(hidden_groups.values(), key=lambda group: group["path"].casefold())},
        "hiddenEntries": hidden_entries[:500],
        "share": {"zipName": skill["id"].replace(":", "-").replace("/", "-") + ".zip",
                  "limits": {"bytes": LIMIT_BYTES, "files": LIMIT_FILES}, "include": inc,
                  "exclude": [shape(exclude[reason]) for reason in REASONS if reason in exclude],
                  "never": [shape(never["cache"])] if "cache" in never else [],
                  "totals": {"files": sum(group["files"] for group in inc), "size": sum(group["size"] for group in inc)}}
    }
    return SkillPlan(body, files, root, single, policy)


def _switches(query: dict[str, list[str]], key: str) -> set[str]:
    values = query.get(key, [])
    if not values:
        return set()
    if len(values) != 1:
        raise SkillFilesError(400, "shareBadRequest")
    return set(values[0].split(",")) if values[0] else set()


def select_files(plan: SkillPlan, query: dict[str, list[str]]) -> list[SkillFile]:
    on, off = _switches(query, "on"), _switches(query, "off")
    include = {group["key"]: group for group in plan.body["share"]["include"]}
    if on - set(REASONS) or off - include.keys() or any(include[key].get("locked") for key in off):
        raise SkillFilesError(400, "shareBadRequest")
    return sorted((file for file in plan.files if file.parts[0] not in off and
                   (file.reason is None or file.reason in on)), key=lambda file: (file.path.casefold(), file.path))


def _check_limits(files: int, size: int) -> None:
    if files > LIMIT_FILES or size > LIMIT_BYTES:
        raise SkillFilesError(413, "shareTooLarge", files=files, size=size,
                              maxFiles=LIMIT_FILES, maxSize=LIMIT_BYTES)


def zip_headers(zip_name: str, length: int, files: int, size: int) -> dict[str, str]:
    # BaseHTTPRequestHandler writes Latin-1 headers; filename* carries Unicode.
    fallback = zip_name.encode("ascii", "replace").decode("ascii")
    fallback = re.sub(r'[\x00-\x1f\x7f"\\]', "_", fallback)
    return {"Content-Type": "application/zip", "Content-Length": str(length),
            "Content-Disposition": f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(zip_name, safe="")}',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Pocket-Zip-Files": str(files), "X-Pocket-Zip-Bytes": str(size)}


def make_zip(plan: SkillPlan, query: dict[str, list[str]]) -> SkillZip:
    selected = select_files(plan, query)
    planned_size = sum(file.size for file in selected)
    _check_limits(len(selected), planned_size)
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES, mode="w+b")
    actual_size = 0
    try:
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, file in enumerate(selected):
                target = plan.root if plan.single else plan.root.joinpath(*file.parts)
                _no_links(target)
                if not _same_path(plan.policy.resolve(os.fspath(target)), target):
                    raise SkillFilesError(403, "fileDenied")
                with target.open("rb") as source:
                    if not _same_path(plan.policy.opened(source), target):
                        raise SkillFilesError(403, "fileDenied")
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise SkillFilesError(403, "fileDenied")
                    _check_limits(len(selected), actual_size + info.st_size + sum(f.size for f in selected[index + 1:]))
                    name = plan.body["rootName"] + "/" + file.path
                    with archive.open(name, "w") as destination:
                        while chunk := source.read(min(1024 * 1024, LIMIT_BYTES - actual_size + 1)):
                            actual_size += len(chunk)
                            _check_limits(len(selected), actual_size)
                            destination.write(chunk)
        length = spool.tell()
        spool.seek(0)
        return SkillZip(spool, zip_headers(plan.body["share"]["zipName"], length, len(selected), actual_size))
    except PermissionError as exc:
        spool.close()
        raise SkillFilesError(403, "fileDenied") from exc
    except OSError as exc:
        spool.close()
        raise SkillFilesError(404, "fileNotFound") from exc
    except BaseException:
        spool.close()
        raise
