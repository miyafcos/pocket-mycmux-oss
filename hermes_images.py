"""The pictures a phone sent to the harness, kept so the room can show them again.

Hermes keeps a text-only transcript. A multimodal user message is written as its
text plus one ``[screenshot]`` line per image (``agent/session_persistence.py``,
``_durable_content``), and the pixels are gone once the turn has run. Slack has
the picture because the origin mirror uploads it; the phone, reading the
transcript back, had only the words and a placeholder (2026-09-16).

So the pixels are kept here, on the server that forwarded them, keyed by the
room. When the room is read, each user row that carries placeholders is joined
back to the send it came from -- same words, same number of pictures, in order --
and answered with the paths the phone can draw. A row nobody sent from a phone
(a picture attached in Slack, a send from before this store existed) keeps its
placeholders and gets no picture: nothing is invented.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: What the Hermes transcript writes in place of each image part.
SCREENSHOT_MARK = "[screenshot]"
#: The data URLs a phone may attach (hermes_bridge.IMAGE_PREFIXES), by file suffix.
IMAGE_SUFFIXES = {"data:image/jpeg;base64,": ".jpg", "data:image/png;base64,": ".png"}
#: A stored picture's name: the content hash and its suffix. Anything else in a
#: request is not a name this store gave out.
NAME_PATTERN = re.compile(r"^[0-9a-f]{16}\.(jpg|png)$")
#: The transcript's clock is the Mac's; the ledger's is this machine's. A row may
#: not carry a time earlier than the send that produced it by more than this.
CLOCK_SKEW_SECONDS = 300.0
#: The path the phone reads a picture from, relative to the pocket API root.
IMAGE_ENDPOINT = "hermes/image"


@dataclass(frozen=True)
class SentImages:
    """One send from a phone: its words and the names of its pictures."""

    text: str
    names: list[str]
    at: float


class HermesImageStore:
    """Pictures by room, and the ledger that joins them to transcript rows."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.clock = clock

    # -- writing --------------------------------------------------------------

    def keep(self, session_id: str, message: Any) -> list[str]:
        """Store the pictures of a message about to be sent; return their names.

        A plain string carries nothing and is not recorded. The ledger entry is
        written only when at least one picture decoded, because an entry with no
        names could never match a row that has placeholders.
        """
        if not isinstance(message, list) or not _is_session_dir_name(session_id):
            return []
        text = "\n".join(
            part.get("text", "") for part in message
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
        names: list[str] = []
        for part in message:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            reference = part.get("image_url")
            url = reference.get("url") if isinstance(reference, dict) else reference
            decoded = decode_data_url(url)
            if decoded is None:
                continue
            payload, suffix = decoded
            name = hashlib.sha256(payload).hexdigest()[:16] + suffix
            folder = self.root / session_id
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            if not target.exists():
                target.write_bytes(payload)
            names.append(name)
        if names:
            with (self.root / session_id / "sends.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps({"text": text, "names": names, "at": self.clock()}) + "\n")
        return names

    # -- reading --------------------------------------------------------------

    def attach(self, session_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Put each stored picture back on the transcript row it was sent with.

        Rows and sends are walked oldest first and each send is used once, so the
        same words sent twice with a picture each land on their own rows. A row
        is joined only when its words and its number of placeholders both match,
        and it is not older than the send (allowing for the two clocks).
        """
        pending = self._ledger(session_id)
        if not pending:
            return rows
        ordered = sorted(range(len(rows)), key=lambda i: (rows[i].get("timestamp") is None, rows[i].get("timestamp") or 0.0, i))
        for index in ordered:
            row = rows[index]
            if row.get("role") != "user" or not isinstance(row.get("text"), str):
                continue
            words, count = split_marks(row["text"])
            if count == 0:
                continue
            stamp = row.get("timestamp")
            for position, sent in enumerate(pending):
                if sent.text != words or len(sent.names) != count:
                    continue
                if isinstance(stamp, (int, float)) and stamp < sent.at - CLOCK_SKEW_SECONDS:
                    continue
                row["images"] = [f"{IMAGE_ENDPOINT}/{session_id}/{name}" for name in sent.names]
                row["text"] = words
                del pending[position]
                break
        return rows

    def read(self, session_id: str, name: str) -> tuple[Path, bytes] | None:
        """A stored picture by the name this store gave it, or None."""
        if not _is_session_dir_name(session_id) or not NAME_PATTERN.match(name or ""):
            return None
        target = self.root / session_id / name
        try:
            return target, target.read_bytes()
        except OSError:
            return None

    def _ledger(self, session_id: str) -> list[SentImages]:
        if not _is_session_dir_name(session_id):
            return []
        try:
            lines = (self.root / session_id / "sends.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[SentImages] = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            names = record.get("names")
            if not isinstance(record.get("text"), str) or not isinstance(names, list) or not names:
                continue
            if not all(isinstance(name, str) and NAME_PATTERN.match(name) for name in names):
                continue
            at = record.get("at")
            entries.append(SentImages(record["text"], names, float(at) if isinstance(at, (int, float)) else 0.0))
        return entries


def decode_data_url(url: Any) -> tuple[bytes, str] | None:
    """The bytes and file suffix of an attachable data URL, or None."""
    if not isinstance(url, str):
        return None
    for prefix, suffix in IMAGE_SUFFIXES.items():
        if url.startswith(prefix):
            try:
                return base64.b64decode(url[len(prefix):], validate=True), suffix
            except (ValueError, TypeError):
                return None
    return None


def split_marks(text: str) -> tuple[str, int]:
    """A transcript row's words and how many image placeholders end it.

    The transcript writes the text part first and one ``[screenshot]`` line per
    image after it, joined by newlines; an image-only message is placeholders
    alone. Only trailing placeholders count, so a person who typed the word
    ``[screenshot]`` mid-message is not read as having attached a picture.
    """
    lines = text.split("\n")
    count = 0
    while lines and lines[-1] == SCREENSHOT_MARK:
        lines.pop()
        count += 1
    return "\n".join(lines), count


def _is_session_dir_name(value: Any) -> bool:
    """A session id that is also a safe directory name on every platform.

    Narrower than hermes_bridge's own rule: a colon is a legal session id
    character there and an illegal file name character here (Windows).
    """
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 200
        and all(character.isalnum() or character in "_-." for character in value)
        and value.strip(".") != ""
    )
