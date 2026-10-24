"""The Mac mini's answer for the parts of the pocket server that must outlive Windows.

The phone reaches the harness through the pocket server on Windows, and Windows
sleeps, reboots and loses its network. The harness itself does not: Hermes runs
on the Mac mini. So the harness routes are offered here too, next to Hermes,
under ``/pocket-relay`` -- the same paths, the same answers, the same guards --
and the phone can keep its conversation with the harness when Windows is gone.

Two more things only a machine other than Windows can say:

* whether Windows is there at all (``win/status``): the tailnet's own view of the
  peer, and whether the pocket server on it answers. The phone uses this to tell
  "Windows is off" from "the server on Windows is down" from "I am offline".
* the token a write needs (``session``), because this process has its own.

What this module deliberately does not do: import ``server.py``. The pocket
server drags in Windows helpers and mycmux; this process must start on a Mac
with the system Python (3.9) and nothing else. ``hermes_bridge`` is shared as it
is, so a fix to the harness wire lands in both places at once.

Tailscale Serve mounts this under ``/pocket-relay`` and strips that prefix before
proxying, so both ``/pocket-relay/api/...`` and ``/api/...`` are accepted.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import hermes_bridge
import hermes_images
import relay_mirror
import mac_seats
import upload_service

PREFIX = "/pocket-relay"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8997
SERVICE = "pocket-relay"
#: Writes are only taken from the page origin the phone presents for this host.
ALLOWED_ORIGINS = frozenset(filter(None, [relay_mirror.configured_https_url(
    os.environ.get("POCKET_RELAY_ORIGIN", ""), origin_only=True)]))
CSRF_HEADER = "X-Pocket-CSRF"
#: The pocket server's own body cap (server.MAX_UPLOAD_BODY_BYTES): a message
#: carries up to four images inline.
MAX_BODY_BYTES = 6 * 1024 * 1024
HEADER_TIMEOUT_SECONDS = 10.0
BODY_TIMEOUT_SECONDS = 10.0
MAX_CONCURRENT_REQUESTS = 32
GZIP_MIN_BYTES = 900

CONFIG_PATH = Path.home() / ".pocket-relay" / "config.json"
#: The pictures a phone sent through this relay, kept as the pocket server keeps
#: its own (hermes_images): each machine holds what passed through it.
IMAGES_DIR = Path.home() / ".pocket-relay" / "hermes_images"
UPLOAD_DIR = Path.home() / ".pocket-relay" / "uploads"
IMAGE_TYPES = {".jpg": "image/jpeg", ".png": "image/png"}
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"

WIN_HEALTH_URL = relay_mirror.configured_https_url(os.environ.get("POCKET_WIN_HEALTH_URL", ""))
WIN_PEER_HOST = os.environ.get("POCKET_WIN_PEER_HOST", "").strip()
TAILSCALE_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
PROBE_INTERVAL_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 3.0

#: The harness routes, in the pocket server's shape (server.py ``_serve_api`` and
#: ``_post_request``), relative to the prefix. ``*`` is the one variable segment,
#: sliced out exactly as server.py slices it. tests/test_relay_routes.py reads
#: server.py and fails when the two lists part.
HERMES_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/hermes/threads"),
    ("GET", "/api/hermes/thread/*/messages"),
    ("GET", "/api/hermes/turn/*"),
    ("GET", "/api/hermes/image/*"),
    ("POST", "/api/hermes/threads"),
    ("POST", "/api/hermes/thread/*/send"),
    ("POST", "/api/hermes/turn/*/approval"),
)


def now_iso(clock: Callable[[], float] = time.time) -> str:
    """Local wall time with its offset, to the second."""
    return datetime.fromtimestamp(clock(), timezone.utc).astimezone().isoformat(timespec="seconds")


def relay_version(version_file: Path = VERSION_FILE) -> str:
    """The commit this copy was deployed from, as deploy_relay_mac.py stamped it."""
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    try:
        found = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=os.fspath(version_file.parent),
            capture_output=True, text=True, timeout=5)
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def load_hermes_config(path: Path = CONFIG_PATH) -> hermes_bridge.HermesConfig:
    """The relay's Hermes settings. Missing or unreadable means unconfigured, which
    the harness routes answer as hermesNotConfigured rather than as a crash."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    return hermes_bridge.HermesConfig.from_mapping(stored)


def match_hermes_route(method: str, path: str) -> Optional[Tuple[str, str]]:
    """The route pattern and its variable segment, or None."""
    for verb, pattern in HERMES_ROUTES:
        if verb != method:
            continue
        if "*" not in pattern:
            if path == pattern:
                return pattern, ""
            continue
        head, tail = pattern.split("*", 1)
        if path.startswith(head) and path.endswith(tail):
            return pattern, path[len(head):len(path) - len(tail)]
    return None


# -- Windows, as seen from here ----------------------------------------------------


_peer_reason = {"last": ""}


def _note_peer_reason(reason: str) -> None:
    """Say once, in the log, why the peer could not be read -- and once when it can again."""
    if reason != _peer_reason["last"]:
        _peer_reason["last"] = reason
        log(f"tailscale peer: {reason or 'readable again'}")


def tailscale_peer_online(host: str = WIN_PEER_HOST, cli: str = TAILSCALE_CLI,
                          runner: Callable[..., Any] = subprocess.run,
                          note: Callable[[str], None] = _note_peer_reason) -> Optional[bool]:
    """The tailnet's own word on whether the Windows machine is connected.

    None when it cannot be read (the CLI is missing, slow, or the peer is not in
    the list): unknown is not the same claim as offline.
    """
    if not host:
        return None
    try:
        # The App Store app's binary decides from its environment whether it is the
        # CLI. Under launchd it guesses GUI and prints "The Tailscale GUI failed to
        # start" with exit 0 (2026-09-16); the variable makes it the CLI everywhere.
        result = runner([cli, "status", "--json"], capture_output=True, text=True,
                        timeout=PROBE_TIMEOUT_SECONDS, env=dict(os.environ, TAILSCALE_BE_CLI="1"))
        if result.returncode != 0:
            note(f"exit {result.returncode}: {(result.stderr or '').strip()[:160]}")
            return None
        output = result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        note(f"{type(exc).__name__}: {str(exc)[:160]}")
        return None
    try:
        status = json.loads(output)
    except ValueError:
        note(f"not JSON: stdout {len(output or '')} chars {(output or '')[:80]!r}, "
             f"stderr {(result.stderr or '').strip()[:160]!r}")
        return None
    peers = status.get("Peer") if isinstance(status, dict) else None
    if not isinstance(peers, dict):
        note("no Peer map in status")
        return None
    for peer in peers.values():
        if not isinstance(peer, dict):
            continue
        dns = peer.get("DNSName") if isinstance(peer.get("DNSName"), str) else ""
        configured_host = host.strip().rstrip(".").casefold()
        normalized_dns = dns.rstrip(".").casefold()
        short_host = str(peer.get("HostName") or "").casefold()
        if configured_host in {short_host, normalized_dns, normalized_dns.split(".", 1)[0]}:
            online = peer.get("Online")
            note("" if isinstance(online, bool) else "peer has no Online flag")
            return online if isinstance(online, bool) else None
    note(f"peer {host} not listed")
    return None


def tailscale_self_login(cli: str = TAILSCALE_CLI,
                         runner: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """The Mac mini's own tailnet login, never a caller-provided identity."""
    try:
        result = runner([cli, "status", "--json"], capture_output=True, text=True,
                        timeout=PROBE_TIMEOUT_SECONDS,
                        env=dict(os.environ, TAILSCALE_BE_CLI="1"))
        if result.returncode != 0:
            return None
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(status, dict):
        return None
    own, users = status.get("Self"), status.get("User")
    if not isinstance(own, dict) or not isinstance(users, dict):
        return None
    user = users.get(str(own.get("UserID")))
    login = user.get("LoginName") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None


class WinWatcher:
    """Asks every few seconds whether Windows and its pocket server are there.

    Probing in the background, not per request, is what keeps ``win/status``
    instant for the phone even while Windows is timing out: the answer is at
    most one interval old, and ``checkedAt`` says exactly how old.
    """

    def __init__(
        self,
        health_url: str = WIN_HEALTH_URL,
        opener: Callable[..., Any] = urllib.request.urlopen,
        peer_reader: Callable[[], Optional[bool]] = tailscale_peer_online,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        interval: float = PROBE_INTERVAL_SECONDS,
        timeout: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.health_url = relay_mirror.configured_https_url(health_url)
        self.opener = opener
        self.peer_reader = peer_reader
        self.clock = clock
        self.monotonic = monotonic
        self.interval = interval
        self.timeout = timeout
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "winPeerOnline": None, "winApiOk": False, "winApiMs": None,
            "checkedAt": None, "lastOkAt": None,
        }
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _api(self) -> Tuple[bool, Optional[int]]:
        if not self.health_url:
            return False, None
        started = self.monotonic()
        try:
            with self.opener(urllib.request.Request(self.health_url, method="GET"),
                             timeout=self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                payload = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False, None
        elapsed = int(round((self.monotonic() - started) * 1000))
        ok = status == 200 and isinstance(payload, dict) and payload.get("server") == "ok"
        # A time only for an answer that counted: a fast 502 from the proxy in
        # front of a dead server is not a latency worth showing.
        return ok, elapsed if ok else None

    def probe_once(self) -> Dict[str, Any]:
        api_ok, api_ms = self._api()
        peer = self.peer_reader()
        checked = now_iso(self.clock)
        with self._lock:
            self._state.update(winPeerOnline=peer, winApiOk=api_ok, winApiMs=api_ms, checkedAt=checked)
            if api_ok:
                self._state["lastOkAt"] = checked
            return dict(self._state)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="win-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.probe_once()
            except Exception as exc:  # the watcher must outlive any one probe
                log(f"win probe failed: {type(exc).__name__}")
            self._stop.wait(self.interval)


# -- HTTP ---------------------------------------------------------------------------


def log(message: str) -> None:
    sys.stderr.write(f"{now_iso()} {message}\n")
    sys.stderr.flush()


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        hermes: hermes_bridge.HermesClient,
        watcher: WinWatcher,
        version: str,
        allowed_origins: frozenset = ALLOWED_ORIGINS,
        clock: Callable[[], float] = time.time,
        mirror: Optional[relay_mirror.Mirror] = None,
        own_login: Callable[[], Optional[str]] = tailscale_self_login,
        monotonic: Callable[[], float] = time.monotonic,
        seats: Optional[mac_seats.MacSeatsWatcher] = None,
    ) -> None:
        super().__init__(address, RelayHandler)
        self.hermes = hermes
        self.watcher = watcher
        self.version = version
        self.allowed_origins = allowed_origins
        self.clock = clock
        self.mirror = mirror
        self.mac_seats = seats
        self.own_login = own_login
        self._login_clock = monotonic
        self._login_lock = threading.Lock()
        self._cached_login: Optional[str] = None
        self._login_expires_at = 0.0
        self.csrf_token = secrets.token_urlsafe(32)
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    def get_own_login(self) -> Optional[str]:
        """Share successful lookups for 300 seconds; never reuse a failed lookup."""
        with self._login_lock:
            if self._cached_login and self._login_clock() < self._login_expires_at:
                return self._cached_login
            self._cached_login = self.own_login()
            if self._cached_login:
                self._login_expires_at = self._login_clock() + 300.0
            return self._cached_login

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Connection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "PocketRelay"
    sys_version = ""
    server: RelayServer

    def version_string(self) -> str:
        return self.server_version

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HEADER_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: Any) -> None:
        # Method, route and status only: query strings and bodies stay out.
        pass

    def _route_path(self) -> Tuple[str, Dict[str, list]]:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == PREFIX or path.startswith(PREFIX + "/"):
            path = path[len(PREFIX):] or "/"
        return path, parse_qs(parsed.query)

    # -- reads --------------------------------------------------------------------

    def do_GET(self) -> None:
        path, query = self._route_path()
        if path.startswith("/api/mirror/"):
            self._serve_mirror(path)
            return
        if path == "/api/mac/seats":
            if not self._authorize_owner():
                return
            if self.server.mac_seats is None:
                self._send_json(503, {"error": "seatsUnavailable"})
            else:
                self.server.mac_seats.note_request()
                self._send_json(200, self.server.mac_seats.status())
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True, "service": SERVICE, "version": self.server.version,
                                  "time": now_iso(self.server.clock)})
            return
        if path == "/api/win/status":
            self._send_json(200, self.server.watcher.status())
            return
        if path == "/api/session":
            # The pocket server's shape. There is no web build here to be stale
            # against, so the build is this relay's commit and never stale.
            self._send_json(200, {"csrfToken": self.server.csrf_token,
                                  "build": self.server.version, "stale": False})
            return
        matched = match_hermes_route("GET", path)
        if matched is None:
            self._send_json(404, {"error": "not found"})
            return
        pattern, captured = matched
        hermes = self.server.hermes
        if pattern == "/api/hermes/threads":
            self._serve_hermes(lambda: hermes.threads(
                hermes_bridge.bounded_int(query.get("limit", [""])[0], default=60, maximum=200)))
        elif pattern == "/api/hermes/thread/*/messages":
            self._serve_hermes(lambda: hermes.messages(
                captured,
                hermes_bridge.bounded_int(query.get("limit", [""])[0], default=60, maximum=200)))
        elif pattern == "/api/hermes/turn/*":
            self._serve_hermes(lambda: hermes.turn(
                captured, hermes_bridge.bounded_int(query.get("after", [""])[0], default=0, maximum=10**7)))
        elif pattern == "/api/hermes/image/*":
            # A picture kept when it was sent, as the pocket server serves it: the
            # bytes as a file, or the harness's own refusal code.
            session_id, _, name = captured.partition("/")
            try:
                target, payload = hermes.image(session_id, name)
            except hermes_bridge.HermesError as exc:
                self._send_json(exc.status, {"error": exc.code})
                return
            self._send_bytes(payload, IMAGE_TYPES.get(Path(target).suffix.lower(), "application/octet-stream"))
        else:  # pragma: no cover - HERMES_ROUTES and this dispatch are kept together
            self._send_json(404, {"error": "not found"})

    def _authorize_owner(self) -> bool:
        logins = self.headers.get_all("Tailscale-User-Login", [])
        if len(logins) != 1 or not logins[0]:
            self._send_json(403, {"error": "notAllowed"})
            return False
        own = self.server.get_own_login()
        if not own or logins[0].casefold() != own.casefold():
            self._send_json(403, {"error": "notAllowed"})
            return False
        return True

    def _serve_mirror(self, path: str) -> None:
        if not self._authorize_owner():
            return
        kind = path[len("/api/mirror/"):]
        talk_id = None
        if kind.startswith("talk/"):
            talk_id = kind[len("talk/"):]
            kind = "talk"
            if not relay_mirror.valid_talk_id(talk_id):
                self._send_json(404, {"error": "notMirrored"})
                return
        if kind not in ("overview", "lines", "talk", "pc") or self.server.mirror is None:
            self._send_json(404, {"error": "notMirrored"})
            return
        answer = self.server.mirror.read(kind, talk_id)
        if answer is None:
            self._send_json(404, {"error": "notMirrored"})
        else:
            self._send_json(200, answer)

    # -- writes -------------------------------------------------------------------

    def do_POST(self) -> None:
        path, _ = self._route_path()
        # Guards first, for every path, as the pocket server does: an unknown
        # write is refused for want of proof before it is refused for want of a route.
        video_stream = path == "/api/upload/stream"
        if not self._authorize_mutation(video_stream=video_stream):
            return
        if video_stream:
            self._upload_video()
            return
        matched = match_hermes_route("POST", path)
        if matched is None:
            self._send_json(404, {"error": "not found"})
            return
        pattern, captured = matched
        try:
            payload = self._read_json_body()
        except _BodyTooLarge:
            self._send_json(413, {"error": "tooLarge"})
            return
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        hermes = self.server.hermes
        # An approval carries an answer, not a message: it is read as server.py
        # reads it, before the chat body is parsed.
        if pattern == "/api/hermes/turn/*/approval":
            if set(payload) - {"requestId", "choice"}:
                self._send_json(400, {"error": "invalid JSON body"})
                return
            self._serve_hermes(lambda: hermes.approve(
                captured, payload.get("requestId"), payload.get("choice")))
            return
        try:
            message = hermes_bridge.chat_message(payload)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if pattern == "/api/hermes/threads":
            self._serve_hermes(lambda: hermes.start_room(message))
        else:
            self._serve_hermes(lambda: hermes.start_turn(captured, message))

    def do_PUT(self) -> None:
        self._send_json(405, {"error": "method not allowed"})

    do_DELETE = do_PUT
    do_PATCH = do_PUT

    def _authorize_mutation(self, *, video_stream: bool = False) -> bool:
        origin = self.headers.get("Origin")
        if origin is None or origin == "null" or origin not in self.server.allowed_origins:
            self._send_json(403, {"error": "originDenied"})
            return False
        content_type = self.headers.get_content_type().lower()
        if video_stream:
            if content_type not in upload_service.VIDEO_CONTENT_TYPES:
                self._send_json(415, {"error": "videoRequired"})
                return False
        elif content_type != "application/json":
            self._send_json(415, {"error": "jsonRequired"})
            return False
        token = self.headers.get(CSRF_HEADER)
        if not isinstance(token, str) or not secrets.compare_digest(token, self.server.csrf_token):
            self._send_json(403, {"error": "csrfDenied"})
            return False
        return True

    def _upload_video(self) -> None:
        self.close_connection = True
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                raise upload_service.UploadError(411, "lengthRequired")
            if len(lengths) != 1 or len(lengths[0]) > 20 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise upload_service.UploadError(400, "invalidLength")
            length = int(lengths[0])
            if length > upload_service.MAX_VIDEO_BYTES:
                raise upload_service.UploadError(413, "tooLarge")
            if self.headers.get("Transfer-Encoding") is not None:
                raise upload_service.UploadError(400, "invalidLength")
            self.connection.settimeout(max(30, BODY_TIMEOUT_SECONDS))
            result = upload_service.handle_video_upload(
                self.rfile, length, content_type=self.headers.get_content_type().lower(),
                root=UPLOAD_DIR, now=datetime.fromtimestamp(self.server.clock(), timezone.utc))
        except upload_service.UploadError as exc:
            self._send_json(exc.status, {"error": exc.reason})
            return
        self._send_json(200, result)

    def _read_json_body(self) -> Dict[str, Any]:
        self.connection.settimeout(BODY_TIMEOUT_SECONDS)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge()
        if length < 1:
            raise ValueError("invalid request body length")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    # -- answers ------------------------------------------------------------------

    def _serve_hermes(self, operation: Callable[[], Any]) -> None:
        try:
            self._send_json(200, operation())
        except hermes_bridge.HermesError as exc:
            self._send_json(exc.status, {"error": exc.code})
        except Exception as exc:
            log(f"hermes {type(exc).__name__}")
            self._send_json(500, {"error": "hermesFailed"})

    def _send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        encoding = None
        accepted = self.headers.get("Accept-Encoding", "") if self.headers else ""
        if len(body) >= GZIP_MIN_BYTES and any(
                part.strip().split(";")[0] == "gzip" for part in accepted.split(",")):
            squeezed = gzip.compress(body, 6)
            if len(squeezed) < len(body):
                body, encoding = squeezed, "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        """A file as the pocket server sends one: inline, uncached, never sniffed."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BodyTooLarge(Exception):
    pass


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 config_path: Path = CONFIG_PATH,
                 seats_config_path: Path = mac_seats.CONFIG_PATH) -> RelayServer:
    config = load_hermes_config(config_path)
    if not config.configured:
        log(f"hermes not configured ({config_path}); harness routes answer hermesNotConfigured")
    watcher = WinWatcher()
    hermes = hermes_bridge.HermesClient(config, images=hermes_images.HermesImageStore(IMAGES_DIR))
    mirror = relay_mirror.Mirror(watcher)
    seats = None
    try:
        seats_config = mac_seats.load_settings(seats_config_path)
        collector = mac_seats.SeatCollector(seats_config)
        seats = mac_seats.MacSeatsWatcher(collector.collect, output_path=seats_config.output_path)
    except (OSError, ValueError, RuntimeError) as exc:
        log("mac seats config unavailable: " + type(exc).__name__)
    return RelayServer((host, port), hermes, watcher, relay_version(), mirror=mirror, seats=seats)


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="pocket-relay: harness and Windows status on the Mac mini")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--config", default=os.fspath(CONFIG_PATH))
    parser.add_argument("--mac-seats-config", default=os.fspath(mac_seats.CONFIG_PATH))
    args = parser.parse_args(argv)
    httpd = build_server(args.host, args.port, Path(args.config), Path(args.mac_seats_config))
    # One answer before the first request, so win/status never says "never checked".
    httpd.watcher.probe_once()
    httpd.watcher.start()
    httpd.mirror.start()
    if httpd.mac_seats is not None:
        httpd.mac_seats.start()
    log(f"{SERVICE} {httpd.version} listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    finally:
        if httpd.mac_seats is not None:
            httpd.mac_seats.stop()
        httpd.mirror.stop()
        httpd.watcher.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
