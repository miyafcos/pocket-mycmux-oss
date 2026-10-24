"""Where the pocket server meets Hermes.

The harness is one agent with one transcript per conversation, and Slack is one
mouth on it. This module is the second: it reads the same sessions and starts
turns on them, asking Hermes to post both halves back into the Slack thread so
neither side ends up holding half a conversation.

Two decisions shape everything here.

**The phone never holds the turn.** Hermes abandons a ``/chat/stream`` run when
its client disconnects, and a phone disconnects constantly -- a lock screen, a
switch to another app, a lift out of tailnet range. So the stream is held *here*:
a POST starts a turn, this process stays on the upstream stream to the end, and
the phone polls for whatever has arrived. Closing the app now costs the phone its
live view, not the work.

**The phone never holds the key.** The key that reaches Hermes dispatches
terminal-capable work as the Mac's own user, so it stays on this server and the
phone authenticates the way it already does for every other write.

Containment: a session is addressable only when it belongs to the configured home
chat. Hermes will happily run a turn on any session it has; this server will not
ask it to.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Iterator

from hermes_images import HermesImageStore

#: How long a finished turn's events stay readable, so a phone that was away can
#: still catch up on what it missed.
TURN_RETENTION_SECONDS = 15 * 60
#: Live turns held at once. A phone cannot start more than a handful; the cap is
#: what stops a stuck client from growing this without bound.
TURN_LIMIT = 24
#: Events kept per turn. A long turn's deltas are replaced by the final message,
#: which is on the terminal event, so an old middle is safe to drop.
TURN_EVENT_LIMIT = 2000
#: Upstream timeouts. The read timeout is per-chunk, not per-turn: Hermes sends a
#: keepalive comment every 30 seconds, so silence past this is a dead stream.
CONNECT_TIMEOUT = 15
STREAM_READ_TIMEOUT = 90
READ_TIMEOUT = 45

#: The roles a chat shows. Everything else Hermes records (tool results, system
#: notes) belongs to the transcript, not to a conversation.
CHAT_ROLES = ("user", "assistant")

#: The answers a phone may give an approval: Slack's four buttons (Allow Once,
#: Allow Session, Always Allow, Deny), in Hermes's own words.
APPROVAL_CHOICES = ("once", "session", "always", "deny")


class HermesError(RuntimeError):
    """A failure the phone should be told about, with a code it can render."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class HermesConfig:
    """Where Hermes is and which conversation this server may drive."""

    base_url: str
    key: str
    home_chat_id: str
    # The person the home chat belongs to, as Slack knows them. Optional: reads
    # and writes to existing rooms never need it. A room started from the phone
    # does -- Hermes groups sessions per user, so a room with no user is somebody
    # else's as far as it is concerned, and would not behave like the Slack DM.
    home_user_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.key and self.home_chat_id)

    @classmethod
    def from_mapping(cls, stored: Any) -> "HermesConfig":
        if not isinstance(stored, dict):
            return cls("", "", "")
        def text(name: str) -> str:
            value = stored.get(name)
            return value.strip() if isinstance(value, str) else ""
        return cls(text("baseUrl").rstrip("/"), text("key"), text("homeChatId"),
                   text("homeUserId"))


@dataclass
class Turn:
    """One agent turn, and everything the phone has not read yet.

    The cursor counts events since the turn began, not places in the list: a
    long turn drops its oldest deltas, and a cursor that shrank with the list
    would hand the phone events it had already shown.
    """

    id: str
    session_id: str
    started_at: float
    events: list[dict[str, Any]] = field(default_factory=list)
    #: Events retired off the front; the cursor still counts them.
    dropped: int = 0
    done: bool = False
    error: str | None = None
    finished_at: float | None = None
    #: Hermes's id for the run, from the first event that carries one. An answer to
    #: an approval is addressed to the run. Kept apart from the events because a
    #: long turn drops its oldest ones, and the first is where the id arrives.
    run_id: str | None = None
    #: Two threads write here: the one holding the upstream stream, and the one
    #: recording an approval answer. The list and its cursor move together.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def cursor(self) -> int:
        return self.dropped + len(self.events)

    def view(self, after: int) -> dict[str, Any]:
        with self._lock:
            start = min(max(after, self.dropped), self.cursor)
            return {
                "turnId": self.id,
                "sessionId": self.session_id,
                "cursor": self.cursor,
                "events": self.events[start - self.dropped:],
                # True when the phone asked from further back than is still kept,
                # so it knows to fall back on the transcript rather than assume it
                # has the whole turn.
                "truncated": after < self.dropped,
                "done": self.done,
                "error": self.error,
            }

    def append(self, event: dict[str, Any]) -> None:
        data = event.get("data")
        with self._lock:
            if self.run_id is None and isinstance(data, dict) and _looks_like_session_id(data.get("run_id")):
                self.run_id = data["run_id"]
            self.events.append(event)
            if len(self.events) > TURN_EVENT_LIMIT:
                overflow = len(self.events) - TURN_EVENT_LIMIT
                del self.events[:overflow]
                self.dropped += overflow


class HermesClient:
    """Reads Hermes' sessions, and starts turns that reach Slack too."""

    def __init__(
        self,
        config: HermesConfig,
        opener: Callable[..., Any] = urllib.request.urlopen,
        monotonic: Callable[[], float] = time.monotonic,
        images: HermesImageStore | None = None,
    ) -> None:
        self.config = config
        self.opener = opener
        self.monotonic = monotonic
        # The pictures a phone sent, kept here because the Hermes transcript keeps
        # only their placeholders (hermes_images). None means nothing is kept and
        # a row is answered as the transcript has it.
        self.images = images
        self._turns: dict[str, Turn] = {}
        self._lock = threading.Lock()

    # -- wire -----------------------------------------------------------------

    def _request(
        self,
        path: str,
        *,
        body: Any = None,
        stream: bool = False,
        refusals: dict[int, tuple[int, str]] | None = None,
    ) -> Any:
        """One call to Hermes. ``refusals`` gives an upstream status a meaning of
        its own on this call, where the general one would say the wrong thing."""
        if not self.config.configured:
            raise HermesError(503, "hermesNotConfigured")
        request = urllib.request.Request(
            self.config.base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.config.key}",
                **({"Content-Type": "application/json"} if body is not None else {}),
                **({"Accept": "text/event-stream"} if stream else {}),
            },
            method="POST" if body is not None else "GET",
        )
        timeout = STREAM_READ_TIMEOUT if stream else READ_TIMEOUT
        try:
            response = self.opener(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise HermesError(*(refusals or {}).get(exc.code, _upstream_failure(exc.code))) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise HermesError(503, "hermesUnreachable") from exc
        if stream:
            return response
        with response:
            try:
                return json.loads(response.read())
            except (ValueError, UnicodeDecodeError) as exc:
                raise HermesError(502, "hermesBadResponse") from exc

    # -- reads ----------------------------------------------------------------

    def threads(self, limit: int = 60) -> dict[str, Any]:
        """The conversation's rooms, newest first.

        Slack's own shape: the chat itself is one room and every thread is
        another. That is not a choice made here -- Hermes already keys a session
        per thread, which is why the two stay in step without anything syncing
        them.
        """
        payload = self._request(f"/api/sessions?limit={int(limit)}")
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise HermesError(502, "hermesBadResponse")
        rooms = [
            {
                "id": row.get("id"),
                "title": row.get("title") or "",
                "preview": row.get("preview") or "",
                "threadId": row.get("thread_id"),
                "isRoot": not row.get("thread_id"),
                "messageCount": row.get("message_count") or 0,
                # Always a number of seconds. The session store has written
                # both an epoch float and an ISO string over its life, and a
                # field that is sometimes one and sometimes the other is a
                # decoding failure on the phone rather than a missing time.
                "lastActive": _epoch(row.get("last_active")),
                "model": row.get("model"),
            }
            for row in rows
            if isinstance(row, dict) and self._is_home(row)
        ]
        return {"threads": rooms, "homeChatId": self.config.home_chat_id}

    def _is_home(self, row: dict[str, Any]) -> bool:
        return (
            row.get("chat_id") == self.config.home_chat_id
            and isinstance(row.get("id"), str)
            and bool(row.get("id"))
        )

    def _require_home_session(self, session_id: str) -> dict[str, Any]:
        """The session row, or a refusal.

        Checked on every write: Hermes would run a turn on any session it has,
        and this server must only ever drive the one conversation it is for.
        """
        if not _looks_like_session_id(session_id):
            raise HermesError(400, "hermesSessionInvalid")
        payload = self._request(f"/api/sessions/{urllib.parse.quote(session_id)}")
        row = payload.get("session") if isinstance(payload, dict) else None
        if not isinstance(row, dict) or not self._is_home(row):
            raise HermesError(404, "hermesSessionNotAllowed")
        return row

    def messages(self, session_id: str, limit: int = 60) -> dict[str, Any]:
        self._require_home_session(session_id)
        # order=latest: with a limit and no order, Hermes pages from the start of the
        # session, so a conversation past sixty rows lost its newest ones -- the answer
        # included -- and a long tool turn gets there fast (2026-09-16, measured on a
        # 63-row room: rows 15026-15085 came back, the answer was 15088).
        payload = self._request(
            f"/api/sessions/{urllib.parse.quote(session_id)}/messages?limit={int(limit)}&order=latest")
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise HermesError(502, "hermesBadResponse")
        messages = [_message(row) for row in rows if isinstance(row, dict)]
        if self.images is not None:
            # The pictures the phone sent go back on the rows they were sent with;
            # the transcript itself holds only a placeholder per image.
            messages = self.images.attach(session_id, messages)
        return {
            "sessionId": session_id,
            "messages": messages,
            # A phone that left the room mid-turn and came back picks the turn up
            # again from here, rather than showing a question with nothing under it
            # until the reply lands.
            "activeTurnId": self.active_turn(session_id),
        }

    def active_turn(self, session_id: str) -> str | None:
        """The newest turn this server is still running in the session, if any."""
        with self._lock:
            running = [row for row in self._turns.values() if row.session_id == session_id and not row.done]
        return max(running, key=lambda row: row.started_at).id if running else None

    def image(self, session_id: str, name: str) -> tuple[Any, bytes]:
        """A picture the phone sent earlier, as a path and its bytes.

        Only names this server gave out on a transcript row resolve; anything
        else is unknown rather than a file lookup. No upstream check: the room
        was checked when the picture was kept, and a name is a content hash.
        """
        found = self.images.read(session_id, name) if self.images is not None else None
        if found is None:
            raise HermesError(404, "hermesImageUnknown")
        return found

    # -- writes ---------------------------------------------------------------

    def start_turn(self, session_id: str, message: Any) -> dict[str, Any]:
        """Begin a turn and answer at once with something to follow it by.

        The upstream stream is held by this process until the turn ends, so the
        phone locking its screen costs it the live view and nothing else.
        """
        self._require_home_session(session_id)
        self._retire_old_turns()
        if self.images is not None:
            # Kept before the turn starts: the transcript will write placeholders
            # for these, and the room reads the transcript back within seconds.
            self.images.keep(session_id, message)
        turn = Turn(id=uuid.uuid4().hex, session_id=session_id, started_at=self.monotonic())
        with self._lock:
            if sum(1 for row in self._turns.values() if not row.done) >= TURN_LIMIT:
                raise HermesError(429, "hermesTooManyTurns")
            self._turns[turn.id] = turn
        thread = threading.Thread(
            target=self._drive_turn, args=(turn, message), name=f"hermes-turn-{turn.id[:8]}", daemon=True)
        thread.start()
        return {"turnId": turn.id, "sessionId": session_id}

    def start_room(self, message: Any) -> dict[str, Any]:
        """Open a new conversation in the home chat and start its first turn.

        The phone's composer on the room list means "new", the way typing into a
        Slack DM (not into a thread) does. Replying inside an existing room still
        goes through start_turn; this is only for the first word of something new.

        The room is created carrying the home chat, as a Slack DM row would: without
        a chat id it is filtered out of the list, refused by start_turn, and refused
        by the origin mirror (an API-sourced session has no chat to post into). It
        is created without a thread id, which makes it a root of the chat.

        No title is sent. Hermes titles a session itself -- a derived title from the
        opening words, then the model's own a moment later -- and a title written
        here would either block that upgrade or, if it matched an existing title,
        make Hermes delete the new row and answer 400. The phone shows the opening
        words until Hermes's title arrives.
        """
        if not self.config.configured:
            raise HermesError(503, "hermesNotConfigured")
        if not self.config.home_user_id:
            # Refuse rather than create a room that belongs to nobody: it would run,
            # but not as the person the DM is for.
            raise HermesError(503, "hermesHomeUserMissing")
        created = self._request("/api/sessions", body={
            "source": "slack",
            "chat_id": self.config.home_chat_id,
            "chat_type": "dm",
            "user_id": self.config.home_user_id,
        })
        row = created.get("session") if isinstance(created, dict) else None
        session_id = row.get("id") if isinstance(row, dict) else None
        if not _looks_like_session_id(session_id):
            raise HermesError(502, "hermesBadResponse")
        started = self.start_turn(session_id, message)
        return {**started, "created": True}

    def _drive_turn(self, turn: Turn, message: Any) -> None:
        try:
            response = self._request(
                f"/api/sessions/{urllib.parse.quote(turn.session_id)}/chat/stream",
                body={
                    "message": message,
                    # The whole point: the chat this session lives in gets both
                    # halves, so Slack never holds half a conversation.
                    "mirror_to_origin": True,
                    # This server stays on the stream for the whole turn and the
                    # phone can answer, so a command that needs approval is asked
                    # about, as in Slack, instead of refused for want of anyone to ask.
                    "approvals": "ask",
                },
                stream=True,
            )
        except HermesError as exc:
            self._finish(turn, error=exc.code)
            return
        try:
            with response:
                for event in _sse_events(response):
                    turn.append(event)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            self._finish(turn, error=f"streamEnded: {type(exc).__name__}")
            return
        self._finish(turn)

    def _finish(self, turn: Turn, error: str | None = None) -> None:
        turn.error = error
        turn.done = True
        turn.finished_at = self.monotonic()

    def turn(self, turn_id: str, after: int = 0) -> dict[str, Any]:
        with self._lock:
            found = self._turns.get(turn_id)
        if found is None:
            raise HermesError(404, "hermesTurnUnknown")
        return found.view(after)

    def approve(self, turn_id: str, request_id: Any, choice: Any) -> dict[str, Any]:
        """Answer an approval the harness is waiting on, in a turn this server runs.

        Only one of this server's own turns, only while it runs, and only the
        answers Slack's buttons give. The request id goes along so the answer
        settles exactly the prompt the phone showed, never another one waiting
        behind it.
        """
        if choice not in APPROVAL_CHOICES:
            raise HermesError(400, "hermesApprovalChoiceInvalid")
        if not (isinstance(request_id, str) and 1 <= len(request_id) <= 128
                and all(character.isalnum() or character in "_-" for character in request_id)):
            raise HermesError(400, "hermesApprovalRequestInvalid")
        with self._lock:
            found = self._turns.get(turn_id)
        if found is None:
            raise HermesError(404, "hermesTurnUnknown")
        if found.done or found.run_id is None:
            raise HermesError(409, "hermesApprovalGone")
        answered = self._request(
            f"/v1/runs/{urllib.parse.quote(found.run_id)}/approval",
            body={"choice": choice, "request_id": request_id},
            # Already answered, timed out, or the run is over: the prompt is gone,
            # which is not the Mac failing and not a session the phone may not read.
            refusals={404: (409, "hermesApprovalGone"), 409: (409, "hermesApprovalGone")},
        )
        resolved = answered.get("resolved") if isinstance(answered, dict) else None
        # Written into the turn, so every phone following it -- and one that comes
        # back to the room later and replays the turn -- sees the prompt settled and
        # what was chosen, instead of a card still asking.
        found.append({"event": "approval.answered", "data": {"request_id": request_id, "choice": choice}})
        return {"turnId": turn_id, "choice": choice,
                "resolved": resolved if isinstance(resolved, int) and not isinstance(resolved, bool) else 0}

    def _retire_old_turns(self) -> None:
        now = self.monotonic()
        with self._lock:
            for key, row in list(self._turns.items()):
                if row.done and row.finished_at is not None and now - row.finished_at > TURN_RETENTION_SECONDS:
                    del self._turns[key]


def _upstream_failure(status: int) -> tuple[int, str]:
    """The upstream refusal, kept at its own status.

    Flattening everything to 502 told the phone "the Mac is broken" for a
    session that simply is not its to read, which is a different thing and a
    different screen.
    """
    if status in (401, 403):
        # Not passed through: the phone has no key to fix, and 401 would send
        # it to its own sign-in. This is the server's own misconfiguration.
        return 503, "hermesUnauthorized"
    if status == 404:
        return 404, "hermesSessionNotAllowed"
    if status == 429:
        return 429, "hermesBusy"
    return 502, "hermesUpstreamError"


def _epoch(value: Any) -> float | None:
    """Seconds since 1970, from a number or an ISO-ish string, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        for attempt in (text, text.split(".", 1)[0]):
            try:
                return datetime.fromisoformat(attempt).timestamp()
            except ValueError:
                continue
    return None


def _looks_like_session_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 200
        and all(character.isalnum() or character in "_-:." for character in value)
    )


def _message(row: dict[str, Any]) -> dict[str, Any]:
    """One transcript row as a chat shows it.

    Tool rows keep their name and lose their payload: a chat wants to say "it
    ran html-report", not to paste the result of it.

    ``images`` is always present and starts empty: the transcript carries no
    pixels, and only the image store (``HermesImageStore.attach``) fills it, with
    the paths of pictures this server kept when the phone sent them.
    """
    role = row.get("role") if row.get("role") in ("user", "assistant", "tool") else "assistant"
    content = row.get("content")
    identity = row.get("id")
    return {
        # A string, whatever the store keyed it with. It is only an identity to
        # the phone, and a field that is an int here and a string there fails
        # the whole transcript rather than one row (2026-09-16).
        "id": str(identity) if identity is not None else None,
        "role": role,
        "text": content if isinstance(content, str) and role != "tool" else "",
        "toolName": row.get("tool_name") if role == "tool" else None,
        "timestamp": row.get("timestamp"),
        "reasoning": row.get("reasoning_content") or row.get("reasoning") or None,
        "images": [],
    }


def _sse_events(response: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Whole SSE frames from a byte stream, ignoring comments and keepalives."""
    name = ""
    data: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield _frame(name, "\n".join(data))
            name, data = "", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].lstrip())
    if data:
        yield _frame(name, "\n".join(data))


def _frame(name: str, payload: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except ValueError:
        parsed = {"raw": payload}
    return {"event": name or (parsed.get("event") if isinstance(parsed, dict) else "") or "message",
            "data": parsed}


def bounded_int(raw: Any, *, default: int, maximum: int) -> int:
    """A query integer, clamped rather than refused.

    A phone that asks for more than is sensible gets the cap; one that asks for
    nonsense gets the default. Neither is worth an error page on a read.
    """
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return max(0, min(value, maximum))


#: Data URLs a phone may attach. JPEG and PNG only: the room shows photos and
#: screenshots, and an unbounded media type is an unbounded decoder surface.
IMAGE_PREFIXES = ("data:image/jpeg;base64,", "data:image/png;base64,")
#: One turn's attachments. Hermes carries them inline in the message, so this is
#: also what keeps a single request from growing past the body cap.
MAX_IMAGES = 4
MAX_TEXT_CHARACTERS = 8000


def chat_message(payload: dict[str, Any]) -> Any:
    """The multimodal message Hermes takes, from what the phone sent.

    A plain string when there is only text, because that is the shape every
    Hermes client sends and the one its own tests cover. The list form appears
    only when there is an image to carry.
    """
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    if set(payload) - {"text", "images"}:
        raise ValueError("invalid JSON body")
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    text = text.strip()
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ValueError("text is too long")
    images = payload.get("images", [])
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError("invalid JSON body")
    for item in images:
        if not isinstance(item, str) or not item.startswith(IMAGE_PREFIXES):
            raise ValueError("invalid JSON body")
    if not text and not images:
        raise ValueError("text must not be empty")
    if not images:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend({"type": "image_url", "image_url": {"url": item}} for item in images)
    return parts
