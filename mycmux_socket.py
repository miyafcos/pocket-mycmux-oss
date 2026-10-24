"""Socket/feed port from mycmux origin/claude/check-latest-version-f43c0w.

Source: tools/pocket-mycmux/pocket_server.py (runtime, socket, feed state/thread).
Local adaptations: ASCII error codes, no auth retries, and stoppable thread IO.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

SOCKET_TIMEOUT_SECONDS = 10.0
STATUS_PROTOCOL_VERSION = 2
FEED_RECONNECT_MIN_SECONDS = 1.0
FEED_RECONNECT_MAX_SECONDS = 10.0
FEED_SILENCE_LIMIT_SECONDS = 200.0


class MycmuxError(RuntimeError):
    """Only local error codes may cross the socket boundary."""


# The one refusal a phone has to be able to read. mycmux answers a close of
# the only tab in a workspace's only pane with exactly this sentence; it is a
# fixed string of ours to compare against, never peer text to echo.
LAST_TAB_REFUSAL = "refusing to close the last tab of the last pane"


class Unauthorized(MycmuxError):
    def __init__(self) -> None:
        super().__init__("unauthorized")


def runtime_dir() -> Path:
    value = os.environ.get("MYCMUX_RUNTIME_DIR")
    return Path(value) if value else Path.home() / ".mycmux"


def read_port(path: Path) -> int:
    try:
        port = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        raise MycmuxError("socketPortUnavailable") from None
    if not 1 <= port <= 65535:
        raise MycmuxError("socketPortUnavailable")
    return port


def read_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    except UnicodeError:
        raise MycmuxError("socketTokenUnavailable") from None
    return token or None


class MycmuxSocket:
    def __init__(self, runtime: Path | None = None, timeout: float = SOCKET_TIMEOUT_SECONDS):
        if timeout <= 0:
            raise ValueError("socket timeout must be positive")
        self.runtime = runtime or runtime_dir()
        self.timeout = timeout

    @property
    def port_file(self) -> Path:
        return self.runtime / "mycmux.port"

    @property
    def token_file(self) -> Path:
        return self.runtime / "mycmux.token"

    def port(self) -> int:
        return read_port(self.port_file)

    def token(self) -> str | None:
        return read_token(self.token_file)

    def request(self, cmd: str, args: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"cmd": cmd, "args": args or {}}
        token = self.token()
        if token is not None:
            payload["token"] = token
        line = (json.dumps(payload) + "\n").encode("utf-8")
        port = self.port()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=self.timeout) as conn:
                conn.settimeout(self.timeout)
                conn.sendall(line)
                with conn.makefile("rb") as reader:
                    response_line = reader.readline()
        except OSError:
            raise MycmuxError("socketUnavailable") from None
        return decode_response(response_line, self.token_file)


def decode_response(response_line: bytes, token_file: Path) -> Any:
    if not response_line:
        raise MycmuxError("socketClosed")
    try:
        response = json.loads(response_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MycmuxError("socketInvalidResponse") from None
    if not isinstance(response, dict):
        raise MycmuxError("socketInvalidResponse")
    error = response.get("error")
    if error == "unauthorized":
        raise Unauthorized()
    if error == LAST_TAB_REFUSAL:
        raise MycmuxError("lastTabRefused")
    if error is not None:
        # Never echo peer-controlled text, even if it includes the token.
        raise MycmuxError("socketRequestFailed")
    return response.get("result")


class FeedState:
    """The upstream snapshot/changed/resync state machine, without IO."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.server_epoch: str | None = None
        self.seq: int = 0
        self.subscribed = False
        self.has_snapshot = False
        self.last_frame_at: float = 0.0
        self._next_request_id = 2
        self._pending_snapshot_ids: set[int] = set()

    def subscribe_request(self, token: str | None) -> dict[str, Any]:
        request: dict[str, Any] = {
            "v": STATUS_PROTOCOL_VERSION, "id": 1, "cmd": "status.subscribe", "args": {},
        }
        if token is not None:
            request["token"] = token
        return request

    def _snapshot_request(self, token: str | None) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._pending_snapshot_ids.add(request_id)
        request: dict[str, Any] = {
            "v": STATUS_PROTOCOL_VERSION, "id": request_id,
            "cmd": "status.snapshot", "args": {},
        }
        if token is not None:
            request["token"] = token
        return request

    def _replace(self, payload: dict[str, Any]) -> None:
        sessions: dict[str, dict[str, Any]] = {}
        for entry in payload.get("sessions") or []:
            if not isinstance(entry, dict):
                continue
            session_id = entry.get("session_id")
            status = entry.get("status")
            if isinstance(session_id, str) and isinstance(status, dict):
                sessions[session_id] = status
        self.sessions = sessions
        self.server_epoch = payload.get("server_epoch") or self.server_epoch
        self.seq = self._sequence(payload.get("seq"), 0)
        self.has_snapshot = True

    @staticmethod
    def _sequence(value: Any, default: int) -> int:
        try:
            return int(value or default)
        except (ValueError, TypeError):
            raise MycmuxError("feedInvalidFrame") from None

    def apply(self, frame: dict[str, Any], token: str | None, now: float) -> dict[str, Any] | None:
        self.last_frame_at = now
        if frame.get("error") == "unauthorized":
            raise Unauthorized()
        kind = frame.get("kind")
        if kind == "response":
            request_id = frame.get("id")
            if frame.get("error"):
                raise MycmuxError("feedRequestFailed")
            result = frame.get("result")
            if request_id == 1:
                self.subscribed = bool(isinstance(result, dict) and result.get("subscribed"))
                if not self.subscribed:
                    raise MycmuxError("feedSubscribeRejected")
            elif request_id in self._pending_snapshot_ids and isinstance(result, dict):
                self._pending_snapshot_ids.discard(request_id)
                self._replace(result)
            return None
        if kind != "event":
            return None
        event = frame.get("event")
        if event == "status.snapshot":
            self._replace(frame)
        elif event == "status.changed":
            session_id = frame.get("session_id")
            status = frame.get("status")
            if isinstance(session_id, str) and isinstance(status, dict):
                self.sessions[session_id] = status
            self.seq = self._sequence(frame.get("seq"), self.seq)
            self.server_epoch = frame.get("server_epoch") or self.server_epoch
        elif event == "status.resync_required":
            self.has_snapshot = False
            return self._snapshot_request(token)
        return None


class StatusFeedThread(threading.Thread):
    def __init__(self, client: MycmuxSocket, log: Callable[[str], None]):
        super().__init__(name="pocket-status-feed", daemon=True)
        self.client = client
        self.log = log
        self.state = FeedState()
        self.connected = False
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._connection: socket.socket | None = None

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            conn = self._connection
            self.connected = False
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def snapshot(self) -> tuple[bool, dict[str, dict[str, Any]], dict[str, Any]]:
        with self._lock:
            return (
                self.connected, dict(self.state.sessions),
                {"server_epoch": self.state.server_epoch, "seq": self.state.seq,
                 "last_frame_at": self.state.last_frame_at,
                 "connected_since": self.connected_since, "error": self.last_error},
            )

    def run(self) -> None:
        delay = FEED_RECONNECT_MIN_SECONDS
        while not self._stop_event.is_set():
            try:
                self._serve_once()
                delay = FEED_RECONNECT_MIN_SECONDS
            except (MycmuxError, OSError, ValueError, TypeError) as exc:
                # Exception details and peer payloads can contain credentials.
                message = "unauthorized" if isinstance(exc, Unauthorized) else "feedDisconnected"
                with self._lock:
                    was_connected = self.connected_since is not None
                    if self.last_error != message:
                        self.log(message)
                    self.connected = False
                    self.connected_since = None
                    self.last_error = message
                if isinstance(exc, Unauthorized):
                    return
                if was_connected:
                    delay = FEED_RECONNECT_MIN_SECONDS
                if self._stop_event.wait(delay):
                    return
                delay = min(delay * 2, FEED_RECONNECT_MAX_SECONDS)

    def _serve_once(self) -> None:
        token = self.client.token()
        port = self.client.port()
        with self._lock:
            self.state = FeedState()
        request = self.state.subscribe_request(token)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=self.client.timeout) as conn:
                with self._lock:
                    self._connection = conn
                # Windows does not always wake a timed makefile read on shutdown.
                # Keep partial JSON lines across short reads; silence still uses
                # the upstream 200-second limit, not the cancellation interval.
                conn.settimeout(min(0.2, FEED_SILENCE_LIMIT_SECONDS))
                conn.sendall((json.dumps(request) + "\n").encode("utf-8"))
                for line in self._lines(conn):
                    frame = json.loads(line.decode("utf-8"))
                    if not isinstance(frame, dict):
                        continue
                    with self._lock:
                        follow_up = self.state.apply(frame, token, time.time())
                        ready = self.state.subscribed and self.state.has_snapshot
                        if ready and not self.connected:
                            self.connected_since = time.time()
                            self.last_error = None
                            self.log("feedConnected")
                        self.connected = ready
                    if follow_up is not None:
                        conn.sendall((json.dumps(follow_up) + "\n").encode("utf-8"))
        finally:
            with self._lock:
                self._connection = None

    def _lines(self, conn: socket.socket):
        pending = b""
        last_line = time.monotonic()
        while not self._stop_event.is_set():
            if time.monotonic() - last_line >= FEED_SILENCE_LIMIT_SECONDS:
                raise MycmuxError("feedSilent")
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise MycmuxError("feedClosed")
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line.strip():
                    last_line = time.monotonic()
                    yield line


# Canonical read validation ported from scripts/mycmux_agent_cli.py.
def validate_status_result(result: Any, expected_session: str | None) -> Any:
    """Validate the canonical state response and reject ambiguous targeting."""
    if not isinstance(result, dict) or not isinstance(result.get("sessions"), list):
        raise RuntimeError("mycmux returned an invalid session.state_view schema")
    lifecycle_values = {"alive", "exited", "orphaned", "unknown"}
    activity_values = {"streaming", "running_silent", "idle", "unknown"}
    attention_values = {"none", "input", "approval", "rate_limited", "error", "done"}
    health_values = {"fresh", "stale", "degraded"}
    ui_state_values = {"working", "idle", "waiting", "done", "unknown"}

    def non_negative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    seen: set[str] = set()
    matches: list[dict[str, Any]] = []
    for entry in result["sessions"]:
        if not isinstance(entry, dict):
            raise RuntimeError("mycmux returned an invalid session.state_view entry")
        session_id = entry.get("session_id")
        view = entry.get("view")
        if not isinstance(session_id, str) or not session_id or not isinstance(view, dict):
            raise RuntimeError("mycmux returned an invalid session.state_view entry")
        if view.get("session_id") != session_id:
            raise RuntimeError("mycmux returned mismatched session.state_view identifiers")
        epoch = view.get("session_epoch")
        attention = view.get("attention")
        if (
            "session_epoch" not in view
            or (epoch is not None and not non_negative_int(epoch))
            or not non_negative_int(view.get("session_revision"))
            # mycmux v0.80.0 (state_view.input_revision_nullable) reports null
            # when the input line is unknown, e.g. for an exited seat.
            or not (
                entry.get("input_revision") is None
                or non_negative_int(entry.get("input_revision"))
            )
            or view.get("lifecycle") not in lifecycle_values
            or view.get("activity") not in activity_values
            or not isinstance(attention, dict)
            or attention.get("kind") not in attention_values
            or "attention_id" not in attention
            or not (
                attention.get("attention_id") is None
                or isinstance(attention.get("attention_id"), str)
            )
            or view.get("health") not in health_values
            or entry.get("ui_state") not in ui_state_values
        ):
            raise RuntimeError("mycmux returned an incomplete canonical session state")
        if session_id in seen:
            raise RuntimeError("mycmux returned duplicate session state")
        seen.add(session_id)
        if session_id == expected_session:
            matches.append(entry)
    if expected_session is not None:
        if len(matches) != 1:
            raise RuntimeError(
                "session.state_view did not return exactly one session"
            )
        return {**result, "sessions": matches}
    return result
