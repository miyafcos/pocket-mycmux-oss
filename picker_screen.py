"""Parse Claude selection screens without making terminal calls."""
from __future__ import annotations

import hashlib
import re
from typing import Any

# Claude Code 2.1.291 can draw a status chip into the overline, e.g. "▔▔▔ ◉ xhigh · /effort ▔"
# right after a model change (captured 2026-10-06), so the overline only has to start with the rule.
_TOP = re.compile(r"^\s*\u2594{3,}.*$")
_RULE = re.compile(r"^\s*\u2500{3,}\s*$")
_ROW = re.compile(
    r"^\s*(?P<cursor>[\u276f\u203a>])?\s*(?P<scroll>[\u2191\u2193])?"
    r"\s*(?P<num>\d{1,3})\.\s+(?P<text>.+?)\s*$"
)
_PART = re.compile(r"^(?P<key>\S+)\s+(?:to|for)\s+(?P<meaning>.+)$")
_INLINE = re.compile(
    r"(?<!\S)(?P<key>Enter|Esc|Escape|Tab|Space|[a-z]|\u2190/\u2192|\u2191/\u2193)"
    r"\s+(?:to|for)\s+(?P<meaning>.+)$"
)
_MORE = re.compile(r"^\s*(?:\u2026|\.{3})\s*\+\d+\s+.+$")
_ADJUST = "\u2190/\u2192 to adjust"
_MARKS = re.compile(r"^[\s\u25cf\u25c9\u25cb\u25d0-\u25d3\u25ce\u25c6\u25c7]+")
_NAMED = {"enter": "enter", "esc": "esc", "escape": "esc", "tab": "tab", "space": "space"}


def _composer_frame(lines: list[str], start: int = 0) -> tuple[int, int] | None:
    """Locate the most recent complete input frame."""
    for top in reversed(range(start, len(lines))):
        if not _RULE.fullmatch(lines[top]):
            continue
        for bottom in range(top + 1, len(lines)):
            if _RULE.fullmatch(lines[bottom]):
                content = lines[top + 1:bottom]
                if content and re.match(r"^\s*\u276f(?:\s|$)", content[0]):
                    return top, bottom
                break
    return None


def composer_text(lines: list[str], start: int = 0) -> str | None:
    """Return the text inside a complete input frame, excluding its cursor."""
    frame = _composer_frame(lines, start)
    if frame is None:
        return None
    top, bottom = frame
    content = lines[top + 1:bottom]
    first = re.sub(r"^\s*\u276f ?", "", content[0])
    return "\n".join([first, *content[1:]])


def is_working(lines: list[str]) -> bool:
    """Use only the spinner/elapsed line above the current composer."""
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return False
    frame = _composer_frame(lines)
    if frame is None:
        return False
    top, _ = frame
    return any(re.match(r"^\s*\S\s+\S+\u2026\s+\(", line)
               for line in lines[max(0, top - 12):top])


def _keys(key: str) -> list[str]:
    if key == "\u2190/\u2192":
        return ["left", "right"]
    if key == "\u2191/\u2193":
        return ["up", "down"]
    if key.lower() in _NAMED:
        return [_NAMED[key.lower()]]
    if re.fullmatch(r"[a-z]", key):
        return [key]
    return []


def action_label(meaning: str, title: str) -> tuple[str, bool | None]:
    normalized = meaning.lower()
    if "set as default" in normalized:
        return "\u65e2\u5b9a\u306b\u3059\u308b", True
    if any(value in normalized for value in (
        "use this session only", "for this session only", "this session only"
    )):
        return "\u3053\u306e\u5e2d\u3060\u3051", False
    if normalized == "cancel":
        return "\u9589\u3058\u308b", False
    if normalized == "confirm":
        return "\u6c7a\u5b9a", True if title == "Effort" else None
    if normalized == "select":
        return "\u9078\u3076", None
    if normalized == "toggle":
        return "\u5207\u308a\u66ff\u3048", None
    if normalized in {"go back", "back"}:
        return "\u623b\u308b", False
    return meaning, None


def scan_picker(lines: list[str]) -> dict[str, Any] | None:
    """Parse only a footer-shaped dialog which has no composer below it."""
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return None
    footer = None
    parts = []
    for index in range(len(lines) - 1, max(-1, len(lines) - 16), -1):
        candidates = lines[index].strip().split(" \u00b7 ")
        matches = [_PART.fullmatch(part) for part in candidates]
        if not all(matches):
            continue
        if len(matches) < 2 and not (
            matches[0]["key"].lower() in {"esc", "escape"}
            and matches[0]["meaning"].lower() == "cancel"
        ):
            continue
        footer, parts = index, [(part, match["key"], match["meaning"])
                                for part, match in zip(candidates, matches)]
        break
    if footer is None:
        return None
    if composer_text(lines, footer + 1) is not None:
        return None
    top = next((index for index in range(footer - 1, -1, -1)
                if _TOP.fullmatch(lines[index])), None)
    if top is None:
        top = max(0, footer - 30)
        for index in range(footer - 1, top - 1, -1):
            line = lines[index]
            if _RULE.fullmatch(line) or (
                re.match(r"^\s*\u276f\s*\S", line) and not _ROW.fullmatch(line)
            ):
                top = index + 1
                break
        content_start = top
    else:
        content_start = top + 1
    title_index = next((index for index in range(content_start, footer)
                        if lines[index].strip()), None)
    if title_index is None:
        return None
    title = lines[title_index].strip()
    rows, notes, body = [], [], []
    adjust = None
    above = below = False
    more_text = None
    content_started = False
    inline_parts = []
    content_lines = lines[title_index + 1:footer]
    for offset, line in enumerate(content_lines):
        row = _ROW.fullmatch(line)
        if row:
            content_started = True
            rows.append({
                "number": int(row["num"]),
                "text": re.sub(r"[ \t]?\u2714", "", row["text"]).strip(),
                "cursor": bool(row["cursor"]),
                "checked": "\u2714" in row["text"],
            })
            above = above or row["scroll"] == "\u2191"
            below = below or row["scroll"] == "\u2193"
        elif _MORE.fullmatch(line):
            content_started = True
            more_text = line.strip()
        elif _ADJUST in line:
            content_started = True
            adjust = {"text": _MARKS.sub("", line.split(_ADJUST, 1)[0]).strip(),
                      "keys": ["left", "right"]}
        elif line.strip():
            # A scale begins with its Faster/Smarter legend. Keep both the
            # legend and the following scale exactly as they were rendered.
            scale = ("\u2500" in line or "\u2501" in line
                     or (offset + 1 < len(content_lines)
                         and "\u2500" in content_lines[offset + 1]))
            if content_started or scale:
                content_started = True
                body.append(line)
                inline = _INLINE.search(line)
                if inline:
                    inline_parts.append((inline[0], inline["key"], inline["meaning"]))
            else:
                notes.append(line.strip())
    actions, extra_keys = [], []
    for text, key, meaning in [*parts, *inline_parts]:
        keys = _keys(key)
        if not keys:
            continue
        if meaning.lower() in {"navigate", "adjust"}:
            extra_keys.extend(keys)
            if meaning.lower() == "adjust" and keys == ["left", "right"] and adjust is None:
                adjust = {"text": "", "keys": keys}
            continue
        if len(keys) != 1:
            continue
        label, persists = action_label(meaning, title)
        action = {"key": keys[0], "text": text, "label": label, "persists": persists}
        if action not in actions:
            actions.append(action)
    keys = list(dict.fromkeys([
        *(["up", "down"] if rows else []),
        *(adjust["keys"] if adjust else []),
        *extra_keys, *(action["key"] for action in actions),
    ]))
    if not keys:
        return None
    more = None
    if above or below or more_text:
        more = {"above": above, "below": below or bool(more_text and not above),
                "text": more_text}
    return {
        "kind": "picker", "agent": "claude", "title": title, "notes": notes,
        "rows": rows, "more": more, "adjust": adjust, "body": body,
        "actions": actions, "keys": keys,
        "fingerprint": hashlib.sha256("\n".join(lines[top:footer + 1]).encode("utf-8")).hexdigest(),
    }
