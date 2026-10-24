"""Read-only skill discovery; HTTP callers only use the prepared snapshot."""
from __future__ import annotations

import glob
import gzip
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import skill_usage_codex


DEFAULTS_PATH = Path(__file__).with_name("skill_shelf_defaults.json")
GUESS_PATH = Path(__file__).with_name("skill_shelf_guess.json")
SEEN_AGE_SECONDS = 30 * 86400
_MATTER_CACHE = {}
LISTING_BYTES = 4 * 1024 * 1024
LISTING_FILES = 10
LISTING_AGE_SECONDS = 7 * 24 * 60 * 60
CHECK_SECONDS = 30.0
PREPARING_SECONDS = 1.5
STARTUP_YIELD_SECONDS = 0.5
AGENTS = ("claude", "codex", "hermes")
KINDS = ("own", "command", "builtin", "plugin")
UNSORTED = {"id": "unsorted", "name": "\u672a\u5206\u985e", "color": "#8E8E93",
            "symbol": "questionmark.folder"}


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _json(path: Path) -> dict:
    try:
        value = json.loads(_text(path))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def frontmatter(path: Path) -> dict:
    stamp = _stamp(path)
    cached = _MATTER_CACHE.get(path)
    if cached is None or cached[0] != stamp:
        cached = (stamp, _frontmatter(path))
        if len(_MATTER_CACHE) >= 4096:
            _MATTER_CACHE.clear()
        _MATTER_CACHE[path] = cached
    return cached[1]


def _frontmatter(path: Path) -> dict:
    match = re.match(r"^---\s*\n(.*?)\n---", _text(path), re.S)
    if not match:
        return {}
    block = match.group(1)
    # Match the catalogue's YAML handling when available, without requiring it.
    try:
        import yaml
        value = yaml.safe_load(block)
        if isinstance(value, dict):
            return value
    except (ImportError, ValueError):
        pass
    except Exception:
        # Malformed frontmatter must not discard the rest of the shelf.
        pass
    result = {}
    for key in ("name", "description"):
        found = re.search(r"^" + key + r":\s*(.*?)(?=^\w[\w-]*:|\Z)", block, re.M | re.S)
        if found:
            value = found.group(1).strip()
            if value.startswith(("|", ">")):
                value = value[1:].lstrip("+-0123456789")
            elif len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key] = " ".join(value.split())
    # The optional YAML dependency is not required for Pocket metadata.
    metadata = re.search(r"^metadata:\s*\n((?:[ \t]+[^\n]*\n?)*)", block, re.M)
    if metadata:
        data = {}
        short = re.search(r"^  short-description:\s*(.+)$", metadata.group(1), re.M)
        if short:
            data["short-description"] = short.group(1).strip().strip("\"'")
        pocket = re.search(r"^  pocket:\s*\n((?:    [^\n]*\n?)*)", metadata.group(1), re.M)
        if pocket:
            fields = {}
            for field in ("label", "category", "line", "symbol"):
                match = re.search(r"^    " + field + r":\s*(.+)$", pocket.group(1), re.M)
                if match:
                    fields[field] = match.group(1).strip().strip("\"'")
            data["pocket"] = fields
        if data:
            result["metadata"] = data
    return result


def _children(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []


def _stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), None, None


def _newest(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: (_stamp(p)[1] or 0, str(p)), reverse=True)


def _plugins(home: Path) -> tuple[list[str], Path | None]:
    text = _text(home / ".codex" / "config.toml")
    enabled = []
    for section in re.finditer(r'^\[plugins\."([^"]+)"\][^\n]*\n([^\[]*)', text, re.M):
        if re.search(r"^\s*enabled\s*=\s*true\s*(?:#.*)?$", section.group(2), re.M):
            enabled.append(section.group(1))
    personal = re.search(r"^\[marketplaces\.personal\][^\n]*\n([^\[]*)", text, re.M)
    source = re.search(r"^\s*source\s*=\s*('([^']*)'|\"([^\"]*)\")", personal.group(1), re.M) if personal else None
    root = None
    if source:
        value = source.group(2)
        if value is None:
            try:
                value = json.loads(source.group(1))
            except ValueError:
                value = source.group(3)
        root = Path(value.removeprefix("\\\\?\\")).expanduser()
    return enabled, root


def _sources(home: Path, *, include_claude=True) -> tuple[list[tuple], list[Path], set[Path]]:
    documents, command_folders, watched = [], [], set()
    for relative, agent in ((".claude", "claude"), (".agents", "codex"),
                            (".codex", "codex"), (".hermes", "hermes")):
        root = home / relative / "skills"
        watched.update((root.parent, root))
        for folder in _children(root):
            if (relative == ".claude" and folder.name == "synced") or folder.name.startswith(("_", ".")) or "backup" in folder.name.casefold():
                continue
            watched.add(folder)
            path = folder / "SKILL.md"
            watched.add(path)
            if path.is_file():
                documents.append((path, agent, "own", None))
    root = home / ".claude" / "commands"
    watched.add(root)
    for path in _children(root):
        watched.add(path)
        if path.is_file() and path.suffix == ".md":
            documents.append((path, "claude", "command", None))
        elif path.is_dir():
            command_folders.append(path)
    root = home / ".codex" / "skills" / ".system"
    watched.add(root)
    for folder in _children(root):
        watched.update((folder, folder / "SKILL.md"))
        if (folder / "SKILL.md").is_file():
            documents.append((folder / "SKILL.md", "codex", "builtin", None))
    enabled, personal = _plugins(home)
    watched.add(home / ".codex" / "config.toml")
    for key in enabled:
        plugin, _, market = key.partition("@")
        roots = []
        if market == "openai-primary-runtime":
            root = (home / ".cache" / "codex-runtimes" / "codex-primary-runtime"
                    / "plugins" / "openai-primary-runtime" / "plugins" / plugin / "skills")
            roots.append(root)
            watched.add(root.parent)
        elif market == "personal" and personal is not None:
            watched.add(personal)
            roots.extend(Path(p) for p in glob.glob(str(personal / "**" / plugin / "skills"), recursive=True))
        else:
            root = home / ".codex" / "plugins" / "cache" / market / plugin
            versions = [p for p in _children(root) if p.is_dir()]
            watched.update([root, *versions])
            if versions:
                roots.append(_newest(versions)[0] / "skills")
        for root in roots:
            watched.add(root)
            files = sorted(set(root.glob("*/SKILL.md")) | set(root.glob("*/*/SKILL.md")))
            # Directory stamps also reveal additions to an existing nested group.
            watched.update(_children(root))
            for child in _children(root):
                if child.is_dir():
                    watched.update(_children(child))
            for path in files:
                watched.update((path.parent, path))
                documents.append((path, "codex", "plugin", plugin))
    if include_claude:
        _, plugin_watch = _claude_documents(home)
        watched.update(plugin_watch)
    return documents, command_folders, watched


def _claude_documents(home: Path) -> tuple[dict, set]:
    documents, watched = {}, set()
    root = home / ".claude/skills/synced"
    watched.add(root)
    for group in _children(root):
        watched.add(group)
        for folder in _children(group):
            path = folder / "SKILL.md"
            watched.update((folder, path))
            if path.is_file():
                name = "anthropic-skills:" + folder.name.casefold()
                documents.setdefault(name, []).append(path)
    synced_names = set(documents)
    installed = home / ".claude/plugins/installed_plugins.json"
    watched.add(installed)
    plugins = _json(installed).get("plugins", {})
    if isinstance(plugins, dict):
        for key, entries in plugins.items():
            if not isinstance(entries, list):
                continue
            plugin = key.split("@", 1)[0]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                scope = entry.get("scope")
                project = entry.get("projectPath")
                if scope != "user" and not (scope == "project" and isinstance(project, str)
                                           and Path(project).resolve() == home.resolve()):
                    continue
                install = entry.get("installPath")
                if not isinstance(install, str) or not install:
                    continue
                root = Path(install) / "skills"
                watched.add(root)
                for folder in _children(root):
                    path = folder / "SKILL.md"
                    watched.update((folder, path))
                    if path.is_file():
                        name = (plugin + ":" + folder.name).casefold()
                        if name not in synced_names:
                            documents.setdefault(name, []).append(path)
    return {name: _newest(paths)[0] for name, paths in documents.items()}, watched


def _transcripts(home: Path, *, now: float | None = None) -> tuple[list[Path], set[Path]]:
    root = home / ".claude" / "projects"
    # Claude Code spells C:\\Users\\example as C--Users-example, and /Users/x as -Users-x.
    project = root / re.sub(r"[^A-Za-z0-9]", "-", str(home))
    projects = [project] if project.is_dir() else [p for p in _children(root) if p.is_dir()]
    watched = {root, project, *projects}
    cutoff = (time.time() if now is None else now) - LISTING_AGE_SECONDS
    recent = []
    for folder in projects:
        for path in folder.glob("*.jsonl"):
            stamp = _stamp(path)
            if stamp[1] is not None and stamp[1] / 1_000_000_000 >= cutoff:
                recent.append(path)
    candidates = _newest(recent)[:LISTING_FILES]
    watched.update(candidates)
    return candidates, watched


def _parse_listing(prefix: bytes) -> tuple[dict[str, str], int]:
    result, notices = {}, 0
    def add(name, description=""):
        name = name.strip()
        if name:
            first, previous = result.get(name.casefold(), (name, ""))
            result[name.casefold()] = (first, description or previous)
    for line in prefix.splitlines():
        if b'"skill_listing"' not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        attachment = row.get("attachment") if isinstance(row, dict) and row.get("type") == "attachment" else None
        if not isinstance(attachment, dict) or attachment.get("type") != "skill_listing":
            continue
        notices += 1
        names = attachment.get("names") or []
        names = [name for name in names if isinstance(name, str)] if isinstance(names, list) else []
        content = attachment.get("content") or ""
        if isinstance(content, str):
            starts = [m.start() for m in re.finditer(r"^- ", content, re.M)] + [len(content)]
            for start, end in zip(starts, starts[1:]):
                name, _, description = content[start + 2:end].partition(": ")
                name = name.strip()
                if name and (name in names or re.fullmatch(r"[A-Za-z0-9_:\-]+", name)):
                    add(name, " ".join(description.split()))
        for name in names:
            add(name)
    return dict(result.values()), notices


class ListingCache:
    """A worker-owned cache of append-only transcript prefixes, per file."""
    def __init__(self):
        self._files = {}
        self.last_scan = []

    def read(self, path: Path) -> dict[str, str]:
        try:
            stat = path.stat()
            key = (path, stat.st_size >= LISTING_BYTES)
            cached = self._files.get(key)
            reused = cached is not None and (key[1] or cached[0] == stat.st_size)
            read_bytes = 0
            if not reused:
                with path.open("rb") as stream:
                    prefix = stream.read(LISTING_BYTES)
                read_bytes = len(prefix)
                rows, notices = _parse_listing(prefix)
                # A file may cross 4 MiB between stat and read. Its full prefix
                # is already immutable, so the next check must reuse it too.
                key = (path, key[1] or read_bytes == LISTING_BYTES)
                cached = (stat.st_size, rows, notices)
                self._files[key] = cached
                if key[1]:
                    self._files.pop((path, False), None)
            self.last_scan.append({"path": str(path), "mtime_ns": stat.st_mtime_ns,
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "size": stat.st_size, "bytes_read": read_bytes, "notice_count": cached[2],
                "cached": reused, "saturated": key[1]})
            return cached[1]
        except OSError as exc:
            self.last_scan.append({"path": str(path), "bytes_read": 0,
                                  "notice_count": 0, "error": type(exc).__name__})
            return {}


def _listing(home: Path, cache: ListingCache | None = None,
             state_dir: Path | None = None, persist: bool = True) -> dict[str, str]:
    cache = ListingCache() if cache is None else cache
    cache.last_scan = []
    candidates, _ = _transcripts(home)
    result, observed = {}, {}
    for path in candidates:
        observed_at = (_stamp(path)[1] or 0) / 1_000_000_000
        for name, description in cache.read(path).items():
            observed[name.casefold()] = max(observed.get(name.casefold(), 0), observed_at)
            first, previous = result.get(name.casefold(), (name, ""))
            result[name.casefold()] = (first, previous or description)
    if state_dir is not None:
        path = Path(state_dir) / "skill_listing_seen.json"
        previous = _json(path)
        cutoff = time.time() - SEEN_AGE_SECONDS
        seen = {name: value for name, value in previous.items() if isinstance(value, dict)
                and isinstance(value.get("lastSeen"), (int, float)) and value["lastSeen"] >= cutoff}
        for key, (name, description) in result.items():
            old_key = next((old_name for old_name in seen if old_name.casefold() == key), None)
            old = seen.pop(old_key, {}) if old_key is not None else {}
            seen[name] = {"description": description or old.get("description", ""),
                          "lastSeen": max(observed.get(key, 0), old.get("lastSeen", 0))}
        for name, value in seen.items():
            result.setdefault(name.casefold(), (name, value.get("description", "")))
        if persist and seen != previous:
            skill_usage_codex.atomic_json(path, seen)
    return dict(result.values())


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def _layer(path: Path) -> dict:
    layer = _json(path)
    for key in ("skills", "aliases"):
        if isinstance(layer.get(key), dict):
            layer[key] = {name.casefold(): value for name, value in layer[key].items()}
    return layer


def _settings(defaults_path: Path, state_dir: Path, home: Path | None = None) -> dict:
    layers = [_layer(defaults_path), _layer(skill_usage_codex.shared_path(state_dir, "skill_shelf.json", home=home))]
    result = {}
    for layer in layers:
        for key in ("skills", "aliases"):
            if isinstance(layer.get(key), dict):
                layer[key] = {name.casefold(): value for name, value in layer[key].items()}
        result = _merge(result, layer)
    return result


def guess_skill(row: dict, categories: list, words: dict | None = None) -> dict:
    words = _json(GUESS_PATH) if words is None else words
    metadata = row.get("metadata") or {}
    short = metadata.get("short-description")
    text = " ".join((row["id"], row["description"], short if isinstance(short, str) else "")).casefold()
    scores = {category["id"]: sum(text.count(word.casefold()) for word in words.get(category["id"], [])
                                  if isinstance(word, str) and word)
              for category in categories if category["id"] != "unsorted"}
    best = max(scores.values(), default=0)
    winners = [name for name, count in scores.items() if count == best]
    category = winners[0] if best and len(winners) == 1 else "unsorted"
    symbol = next((value.get("symbol") for value in categories if value["id"] == category), None)
    return {"category": category, "label": short if isinstance(short, str) and 0 < len(short) <= 16 else row["id"],
            "glyph": row["id"][0].upper(), "line": _new_line(row["description"]), "symbol": symbol}


def _curate(row: dict, layers: list, categories: list, words: dict) -> None:
    complete = {field for _, definition in layers if isinstance(definition, dict) for field, value in definition.items()
                if isinstance(value, str) or (field == "symbol" and value is None)}
    if {"category", "label", "glyph", "line", "symbol"} <= complete:
        row.update({"category": "unsorted", "label": row["id"], "glyph": row["id"][0].upper(),
                    "line": _new_line(row["description"]), "symbol": None})
    else:
        row.update(guess_skill(row, categories, words))
    provenance = {}
    reviewed = False
    # Weak to strong; every field can be supplied independently.
    for origin, definition in layers:
        if not isinstance(definition, dict):
            continue
        if origin in {"manual", "skill", "shipped"}:
            reviewed = True
        for field in ("category", "label", "glyph", "line", "symbol"):
            if field in definition and (isinstance(definition[field], str) or (field == "symbol" and definition[field] is None)):
                row[field] = definition[field]
                provenance[field] = origin
    row["curation"] = provenance.get("category", "guess")
    row["isNew"] = not reviewed


def _new_line(description: str) -> str:
    line = re.split(r"(?<=\u3002)|(?<=\.)\s+", description, maxsplit=1)[0]
    line = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"[`*_#>\[\]\"']", "", line).strip()
    return line if len(line) <= 40 else line[:39] + "\u2026"


def _document(path: Path | None, allowed_roots: tuple, denied_roots: tuple,
              resolve_document: Callable | None) -> str | None:
    if path is None:
        return None
    if resolve_document is not None:
        try:
            return os.fspath(resolve_document(os.fspath(path)))
        except Exception:
            return None
    candidate = path.resolve()
    def within(root):
        return candidate == root or root in candidate.parents
    if any(within(root) for root in denied_roots) or not any(within(root) for root in allowed_roots):
        return None
    return os.fspath(candidate)


def _fold_aliases(rows: dict, aliases: dict) -> None:
    for alias, target in aliases.items():
        target = str(target).casefold()
        if alias == target or alias not in rows or target not in rows:
            continue
        source, destination = rows[alias], rows[target]
        destination["aliases"].append(source["id"])
        destination["usageCount"] += source["usageCount"]
        for agent in ("claude", "codex"):
            destination["usage"][agent] += source["usage"][agent]
        destination["lastUsedAt"] = max((v for v in (destination["lastUsedAt"], source["lastUsedAt"]) if v is not None), default=None)
        destination["agents"].update(source["agents"])
        del rows[alias]


def collect_skills(home: Path, state_dir: Path, *, defaults_path: Path = DEFAULTS_PATH,
                   allowed_roots: tuple | None = None, denied_roots: tuple = (),
                   resolve_document: Callable | None = None,
                   listing_cache: ListingCache | None = None,
                   codex_usage: dict | None = None, persist_listing: bool = True) -> dict:
    home, state_dir = Path(home), Path(state_dir)
    allowed_roots = (home,) if allowed_roots is None else allowed_roots
    if resolve_document is None:
        allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)
        denied_roots = tuple(Path(root).resolve() for root in denied_roots)
    settings = _settings(defaults_path, state_dir, home)
    shipped, manual = _layer(defaults_path), _layer(skill_usage_codex.shared_path(state_dir, "skill_shelf.json", home=home))
    automatic = _layer(skill_usage_codex.shared_path(state_dir, "skill_shelf_auto.json", home=home))
    guesses = _json(GUESS_PATH)
    codex_usage = skill_usage_codex.read_usage(state_dir, home=home) if codex_usage is None else codex_usage
    documents, folders, _ = _sources(home, include_claude=False)
    rows = {}
    def add(name, description, agent, kind, plugin=None, path=None):
        name = str(name).strip()
        if not name:
            return
        row = rows.setdefault(name.casefold(), {"id": name, "description": "", "kind": kind,
                              "plugin": plugin, "agents": set(), "aliases": [], "documents": [], "metadata": {}})
        row["agents"].add(agent)
        if not row["description"] and description:
            row["description"] = " ".join(str(description).split())
        if KINDS.index(kind) < KINDS.index(row["kind"]):
            row["kind"], row["plugin"] = kind, plugin
        if path is not None:
            priority = 0 if agent == "claude" and kind == "own" else 1 if agent == "codex" else 2 if kind == "command" else 3
            row["documents"].append((priority, path))
    for path, agent, kind, plugin in documents:
        matter = frontmatter(path)
        name = path.stem if kind == "command" else matter.get("name") or path.parent.name
        add(name, matter.get("description") or "", agent, kind, plugin, path)
        metadata = matter.get("metadata")
        if isinstance(metadata, dict):
            # Document priority determines the author's declaration too.
            rows[str(name).casefold()]["metadata"] = _merge(metadata, rows[str(name).casefold()]["metadata"])
    for folder in folders:
        add(folder.name, "", "claude", "command")
    listing = _listing(home, listing_cache, state_dir, persist_listing)
    claude_docs, _ = _claude_documents(home)
    for name, description in listing.items():
        if name.casefold() in rows:
            rows[name.casefold()]["agents"].add("claude")
        else:
            add(name, description, "claude", "plugin" if ":" in name else "builtin",
                name.split(":", 1)[0] if ":" in name else None)
    for key, path in claude_docs.items():
        if key in rows and "claude" in rows[key]["agents"]:
            rows[key]["documents"].append((3, path))
            metadata = frontmatter(path).get("metadata")
            if isinstance(metadata, dict):
                rows[key]["metadata"] = _merge(metadata, rows[key]["metadata"])
    usage = _json(home / ".claude.json").get("skillUsage") or {}
    usage = {name.casefold(): value for name, value in usage.items()} if isinstance(usage, dict) else {}
    for key, row in rows.items():
        entry = usage.get(key) or {}
        entry = entry if isinstance(entry, dict) else {}
        try:
            row["usageCount"] = int(entry.get("usageCount") or 0)
        except (ValueError, TypeError):
            row["usageCount"] = 0
        claude_count = row["usageCount"]
        codex = codex_usage.get(key, {}) if "codex" in row["agents"] else {}
        codex_count = int(codex.get("count") or 0)
        row["usage"] = {"claude": claude_count, "codex": codex_count}
        row["usageCount"] += codex_count
        times = (entry.get("lastUsedAt"), codex.get("last"))
        row["lastUsedAt"] = max((last for last in times if isinstance(last, (int, float))), default=None)
    hidden = {str(name).casefold() for name in settings.get("hidden", [])}
    hidden_count = sum(key in hidden for key in rows)
    rows = {key: row for key, row in rows.items() if key not in hidden}
    _fold_aliases(rows, settings.get("aliases", {}))
    categories = list(settings.get("categories") or [])
    if not any(category.get("id") == "unsorted" for category in categories):
        categories.append(dict(UNSORTED))
    category_order = {category["id"]: index for index, category in enumerate(categories)}
    for key, row in rows.items():
        metadata = row.get("metadata") or {}
        declaration = metadata.get("pocket")
        if isinstance(declaration, dict):
            declaration = {field: value for field, value in declaration.items()
                           if field in {"category", "label", "line", "symbol"}}
        _curate(row, [("auto", automatic.get("skills", {}).get(key)),
                      ("shipped", shipped.get("skills", {}).get(key)),
                      ("skill", declaration),
                      ("manual", manual.get("skills", {}).get(key))], categories, guesses)
        row.pop("metadata", None)
        if row["category"] not in category_order:
            row["category"] = "unsorted"
        row["agents"] = [agent for agent in AGENTS if agent in row["agents"]]
        row["calls"] = {agent: prefix + row["id"] for agent, prefix in (("claude", "/"), ("codex", "$")) if agent in row["agents"]}
        candidates = sorted(row.pop("documents"), key=lambda item: item[0])
        row["docPath"] = _document(candidates[0][1] if candidates else None, allowed_roots, denied_roots, resolve_document)
    skills = sorted(rows.values(), key=lambda row: (category_order[row["category"]], -row["usageCount"],
                    row["kind"] not in ("own", "command"), row["label"], row["id"]))
    return {"generatedAt": datetime.now(timezone.utc).isoformat(), "categories": categories,
            "skills": skills, "hiddenCount": hidden_count,
            "newCount": sum(row["isNew"] for row in skills)}


def source_signature(home: Path, state_dir: Path, defaults_path: Path = DEFAULTS_PATH) -> tuple:
    _, _, watched = _sources(home)
    _, transcripts = _transcripts(home)
    watched.update(transcripts)
    watched.update(skill_usage_codex.shared_path(state_dir, name, home=home, for_write=True)
                   for name in skill_usage_codex.SHARED_NAMES)
    watched.update((home / ".claude.json", defaults_path, GUESS_PATH, state_dir / "skill_shelf.json",
                    state_dir / "skill_shelf_auto.json", state_dir / "skill_listing_seen.json",
                    state_dir / "skill_usage_codex.json"))
    return (int(time.time() // 86400),) + tuple(_stamp(path) for path in sorted(watched))


class PreparedCatalog(dict):
    """Public JSON fields plus immutable wire bodies made by the worker."""
    def __init__(self, payload):
        super().__init__(payload)
        self.body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        compressed = gzip.compress(self.body, 6) if len(self.body) >= 900 else self.body
        self.gzip_body = compressed if len(compressed) < len(self.body) else None


def send_prepared(handler, payload):
    """Keep the existing conditional-JSON helper, reusing our prepared body."""
    if not isinstance(payload, PreparedCatalog):
        handler._send_json_tagged(payload)
        return
    original = handler._send_json
    def send(status, value, extra_headers=None):
        if status != 200 or value is not payload:
            return original(status, value, extra_headers)
        accepted = handler.headers.get("Accept-Encoding", "")
        compress = payload.gzip_body is not None and any(part.strip().split(";")[0] == "gzip" for part in accepted.split(","))
        body = payload.gzip_body if compress else payload.body
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        if compress:
            handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Encoding")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    handler._send_json = send
    try:
        handler._send_json_tagged(payload)
    finally:
        handler._send_json = original


class SkillShelf:
    """One worker performs all discovery and stat calls, including refreshes."""
    def __init__(self, state_dir: Path, *, home: Path | None = None,
                 defaults_path: Path = DEFAULTS_PATH, allowed_roots: tuple | None = None,
                 denied_roots: tuple = (), resolve_document: Callable | None = None,
                 log: Callable = lambda message: None, monotonic: Callable = time.monotonic):
        self.home = Path.home() if home is None else Path(home)
        self.state_dir, self.defaults_path = Path(state_dir), Path(defaults_path)
        self.allowed_roots, self.denied_roots = allowed_roots, denied_roots
        self.resolve_document, self.log, self.monotonic = resolve_document, log, monotonic
        self.listing_cache = ListingCache()
        self._guard = threading.Lock()
        self._ready = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._payload = None
        self._signature = None
        self._last_check = None
        self._checking = False
        self._starting = False
        self.refreshes = 0
        self.last_error = None
        self._shared_migrated = False
        self.usage_worker = skill_usage_codex.CodexUsage(self.home, self.state_dir, on_update=self.refresh)

    def start(self):
        with self._guard:
            if self.thread is not None:
                return
            self._checking = True
            self._starting = True
            self.thread = threading.Thread(target=self._run, name="pocket-skill-shelf", daemon=True)
            self.thread.start()
        self.usage_worker.start()

    def stop(self):
        self.usage_worker.stop()
        self._stop.set()
        self._wake.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def refresh(self):
        with self._guard:
            self._signature = None
            self._checking = True
        self._wake.set()

    def note_request(self):
        with self._guard:
            if self._starting:
                self._wake.set()
            if self.thread is not None and not self._checking and (self._last_check is None or self.monotonic() - self._last_check >= CHECK_SECONDS):
                self._checking = True
                self._wake.set()

    def snapshot(self, wait: float = PREPARING_SECONDS) -> dict | None:
        self.note_request()
        if not self._ready.is_set():
            self._ready.wait(min(max(wait, 0.0), PREPARING_SECONDS))
        with self._guard:
            return self._payload

    def _collect(self):
        return collect_skills(self.home, self.state_dir, defaults_path=self.defaults_path,
                              allowed_roots=self.allowed_roots, denied_roots=self.denied_roots,
                              resolve_document=self.resolve_document,
                              listing_cache=self.listing_cache, codex_usage=self.usage_worker.snapshot())

    def _run(self):
        # Give the first home response a head start; opening the shelf wakes us
        # immediately. This wait is only on the worker, never on server startup.
        self._wake.wait(STARTUP_YIELD_SECONDS)
        with self._guard:
            self._starting = False
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if not self._shared_migrated:
                    skill_usage_codex.migrate_shared(self.state_dir, home=self.home, defaults_path=self.defaults_path)
                    self._shared_migrated = True
                signature = source_signature(self.home, self.state_dir, self.defaults_path)
                if self._payload is None or signature != self._signature:
                    payload = PreparedCatalog(self._collect())
                    with self._guard:
                        self._payload, self._signature = payload, signature
                        self.refreshes += 1
                    self.usage_worker.set_skills(payload["skills"])
                    self._ready.set()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.log("skill_shelf " + self.last_error)
            finally:
                with self._guard:
                    self._last_check = self.monotonic()
                    self._checking = False
            self._wake.wait()
