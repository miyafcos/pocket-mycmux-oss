from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable
import uuid

from input_injector import (
    Injector,
    InjectorUnavailable,
    InputBlocked,
    MODIFIER_VKS,
    PAD_TEXT_MAX_CHARS,
    VK,
)


PAD_DEVICE_LEDGER_NAME = "pad_devices.json"
PAD_AUDIT_LOG_NAME = "pad-audit.log"
PAD_AUDIT_LOG_MAX_BYTES = 256 * 1024
PAD_PAIRING_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAD_PAIRING_CODE_LENGTH = 4
PAD_PAIRING_TIMEOUT_SECONDS = 120
PAD_PAIRING_LOCKOUT_FAILURES = 3
PAD_PAIRING_LOCKOUT_SECONDS = 300
PAD_IDLE_DISARM_SECONDS = 600
PAD_WATCHDOG_INTERVAL_SECONDS = 0.05
PAD_MAX_BATCH = 32
PAD_RATE_MOVE_PER_SEC = 240
PAD_RATE_KEY_PER_SEC = 50
PAD_RATE_SHORTCUT_PER_SEC = 20
PAD_RATE_TEXT_CHARS_PER_SEC = 2000
PAD_RATE_PING_PER_SEC = 60
PAD_RATE_STALE_PER_SEC = 60
PAD_MAX_SEQUENCE = 1_000_000_000_000
PAD_MAX_GENERATION = 9_007_199_254_740_991
PAD_LEGACY_MAX_GENERATION = PAD_MAX_SEQUENCE
PAD_GENERATION_STATE_NAME = "pad_generation.json"
PAD_SHUTDOWN_RELEASE_MAX_ATTEMPTS = 4
PAD_SHUTDOWN_RELEASE_RETRY_DELAY_SECONDS = PAD_WATCHDOG_INTERVAL_SECONDS

MB_YESNO = 0x4
MB_ICONWARNING = 0x30
MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000
PAD_PAIRING_MESSAGE_FLAGS = MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
IDYES = 6

_ALLOWED_VKS = frozenset(VK.values())
_ALLOWED_MODIFIERS = frozenset(MODIFIER_VKS)
_BUTTON_NAMES = {"l": "left", "r": "right", "m": "middle"}
_RATE_LIMITS = {
    "move": float(PAD_RATE_MOVE_PER_SEC),
    "key": float(PAD_RATE_KEY_PER_SEC),
    "shortcut": float(PAD_RATE_SHORTCUT_PER_SEC),
    "text": float(PAD_RATE_TEXT_CHARS_PER_SEC),
}
_AUDIT_FIELDS = {
    "pair_requested": frozenset(),
    "pair_approved": frozenset(("deviceId",)),
    "pair_denied": frozenset(),
    "pair_expired": frozenset(),
    "armed": frozenset(("deviceId",)),
    "disarmed": frozenset(("deviceId",)),
    "idle_disarm": frozenset(("deviceId",)),
    "connected": frozenset(("deviceId",)),
    "disconnected": frozenset(("deviceId",)),
    "rate_limited": frozenset(("deviceId", "category")),
    "stale_dropped": frozenset(("deviceId",)),
    "uipi_blocked": frozenset(("deviceId",)),
    "session_summary": frozenset(
        ("deviceId", "moveCount", "clickCount", "keyCount", "textChars")
    ),
}

_PROCESS_LOCKS: set[str] = set()
_PROCESS_LOCKS_GUARD = threading.Lock()


class PairingBusy(RuntimeError):
    pass


class PairingLocked(PairingBusy):
    pass


class PadDisarmed(RuntimeError):
    pass


class PadRateLimited(RuntimeError):
    pass


class PadSequenceExceeded(ValueError):
    pass


class PadProtocolUpgradeRequired(ValueError):
    pass


class PadGenerationExhausted(RuntimeError):
    pass


class PadShutdownReleaseError(RuntimeError):
    pass


class _MachineFileLock:
    """Process and machine level lock matching server.MachineFileLock semantics."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        self._key = os.fspath(path.resolve()).casefold()
        with _PROCESS_LOCKS_GUARD:
            if self._key in _PROCESS_LOCKS:
                raise RuntimeError("state directory is already in use")
            _PROCESS_LOCKS.add(self._key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a+b")
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            self.close()
            raise RuntimeError("state directory is already in use") from exc

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass
            handle.close()
        with _PROCESS_LOCKS_GUARD:
            _PROCESS_LOCKS.discard(self._key)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_message_box(text: str, title: str, flags: int) -> int:
    if os.name != "nt":
        return 0
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except (AttributeError, OSError, TypeError):
        return 0


def _foreground_title() -> str | None:
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        window = user32.GetForegroundWindow()
        if not window:
            return None
        buffer = ctypes.create_unicode_buffer(61)
        copied = user32.GetWindowTextW(window, buffer, len(buffer))
        if copied <= 0:
            return None
        return buffer.value[:60]
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _require_keys(payload: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    if set(payload) - (allowed | {"n", "g"}) or not required.issubset(payload):
        raise ValueError("pad command fields are invalid")


def _validate_top_level_identity(payload: dict[str, Any]) -> tuple[int, int]:
    if "g" not in payload or "n" not in payload:
        raise PadProtocolUpgradeRequired("pad command generation and sequence are required")
    generation = payload["g"]
    if not _is_int(generation) or generation < 1 or generation > PAD_MAX_GENERATION:
        raise ValueError("pad command generation is invalid")
    sequence = payload["n"]
    if not _is_int(sequence) or sequence < 1:
        raise ValueError("pad command sequence is invalid")
    if sequence > PAD_MAX_SEQUENCE:
        raise PadSequenceExceeded("pad command sequence exceeds maximum")
    return generation, sequence


def _with_optional_sequence(
    payload: dict[str, Any], command: dict[str, Any], *, in_batch: bool
) -> dict[str, Any]:
    if in_batch:
        if "g" in payload or "n" in payload:
            raise ValueError("batched pad command identity is invalid")
        return command
    generation, sequence = _validate_top_level_identity(payload)
    command["g"] = generation
    command["n"] = sequence
    return command


def parse_pad_command(payload: dict[str, Any]) -> dict[str, Any]:
    return _parse_pad_command(payload, in_batch=False)


def _parse_pad_command(payload: dict[str, Any], *, in_batch: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("pad command must be an object")
    command_type = payload.get("t")
    if not isinstance(command_type, str):
        raise ValueError("pad command type is invalid")

    if command_type == "batch":
        _require_keys(payload, {"t", "c"}, {"t", "c"})
        if in_batch:
            raise ValueError("nested batch commands are not allowed")
        commands = payload["c"]
        if not isinstance(commands, list) or len(commands) > PAD_MAX_BATCH:
            raise ValueError(f"batch exceeds {PAD_MAX_BATCH} commands")
        return _with_optional_sequence(
            payload,
            {
                "t": "batch",
                "c": [_parse_pad_command(command, in_batch=True) for command in commands],
            },
            in_batch=in_batch,
        )

    if command_type == "m":
        _require_keys(payload, {"t", "dx", "dy"}, {"t", "dx", "dy"})
        if not _is_number(payload["dx"]) or not _is_number(payload["dy"]):
            raise ValueError("relative movement must be finite numbers")
        return _with_optional_sequence(
            payload,
            {"t": "m", "dx": payload["dx"], "dy": payload["dy"]},
            in_batch=in_batch,
        )

    if command_type == "b":
        _require_keys(payload, {"t", "b", "d"}, {"t", "b", "d"})
        if payload["b"] not in _BUTTON_NAMES or not _is_int(payload["d"]) or payload["d"] not in (0, 1):
            raise ValueError("mouse button command is invalid")
        return _with_optional_sequence(
            payload,
            {"t": "b", "b": payload["b"], "d": payload["d"]},
            in_batch=in_batch,
        )

    if command_type == "w":
        _require_keys(payload, {"t", "dy", "dx"}, {"t", "dy", "dx"})
        dy = payload["dy"]
        dx = payload["dx"]
        if not _is_int(dy) or not _is_int(dx) or not all(
            -(2**31) <= value <= 2**31 - 1 for value in (dy, dx)
        ):
            raise ValueError("wheel deltas must be signed 32-bit integers")
        return _with_optional_sequence(
            payload,
            {"t": "w", "dy": dy, "dx": dx},
            in_batch=in_batch,
        )

    if command_type == "k":
        _require_keys(payload, {"t", "vk", "d"}, {"t", "vk", "d"})
        vk = payload["vk"]
        down = payload["d"]
        if not _is_int(vk) or vk not in _ALLOWED_VKS or not _is_int(down) or down not in (0, 1):
            raise ValueError("key command is not on the allowlist")
        return _with_optional_sequence(
            payload,
            {"t": "k", "vk": vk, "d": down},
            in_batch=in_batch,
        )

    if command_type == "combo":
        _require_keys(payload, {"t", "vk", "mods"}, {"t", "vk"})
        vk = payload["vk"]
        mods = payload.get("mods", [])
        if not _is_int(vk) or vk not in _ALLOWED_VKS or not isinstance(mods, list):
            raise ValueError("shortcut command is invalid")
        if (
            any(not isinstance(mod, str) or mod not in _ALLOWED_MODIFIERS for mod in mods)
            or len(set(mods)) != len(mods)
            or any(MODIFIER_VKS[mod] == vk for mod in mods)
        ):
            raise ValueError("shortcut modifiers are invalid")
        return _with_optional_sequence(
            payload,
            {"t": "combo", "vk": vk, "mods": list(mods)},
            in_batch=in_batch,
        )

    if command_type == "txt":
        _require_keys(payload, {"t", "s"}, {"t", "s"})
        text = payload["s"]
        if not isinstance(text, str) or len(text) > PAD_TEXT_MAX_CHARS:
            raise ValueError(f"text exceeds {PAD_TEXT_MAX_CHARS} characters")
        return _with_optional_sequence(payload, {"t": "txt", "s": text}, in_batch=in_batch)

    if command_type == "warp":
        _require_keys(payload, {"t", "mon"}, {"t", "mon"})
        monitor_id = payload["mon"]
        if not isinstance(monitor_id, str) or not monitor_id:
            raise ValueError("monitor id is invalid")
        return _with_optional_sequence(
            payload, {"t": "warp", "mon": monitor_id}, in_batch=in_batch
        )

    if command_type == "rel":
        _require_keys(payload, {"t"}, {"t"})
        return _with_optional_sequence(payload, {"t": "rel"}, in_batch=in_batch)

    if command_type == "p":
        if in_batch:
            raise ValueError("ping commands cannot be batched")
        _require_keys(payload, {"t", "id"}, {"t", "id"})
        if not _is_int(payload["id"]):
            raise ValueError("ping id is invalid")
        return _with_optional_sequence(
            payload, {"t": "p", "id": payload["id"]}, in_batch=in_batch
        )

    raise ValueError("unknown pad command type")


class PadService:
    def __init__(
        self,
        state_dir: Path,
        state_path: Path | None = None,
        injector_factory: Callable[[], Any] = Injector,
        clock: Callable[[], float] = time.monotonic,
        message_box: Callable[[str, str, int], int] = _default_message_box,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.device_path = self.state_dir / PAD_DEVICE_LEDGER_NAME
        self.audit_path = self.state_dir / PAD_AUDIT_LOG_NAME
        self.generation_state_path = (
            Path(state_path) if state_path is not None else self.state_dir / PAD_GENERATION_STATE_NAME
        )
        self._injector_factory = injector_factory
        self._clock = clock
        self._message_box = message_box
        self._operation_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._audit_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._injector: Any | None = None
        self._started = False
        self._stopping = False
        self._stopped = False
        self._pairings: dict[str, dict[str, Any]] = {}
        self._pairing_threads: set[threading.Thread] = set()
        self._active_pairing_nonce: str | None = None
        self._pairing_failures = 0
        self._lockout_until = 0.0
        self._armed_device_id: str | None = None
        self._generation_counter = PAD_LEGACY_MAX_GENERATION
        self._session_generation: int | None = None
        self._applied_seq = 0
        self._pending_release_device_id: str | None = None
        self._pending_release_error: BaseException | None = None
        self._shutdown_release_error: PadShutdownReleaseError | None = None
        self._last_activity = self._clock()
        self._rate_buckets: dict[str, dict[str, list[float]]] = {}
        self._ping_rate_buckets: dict[str, list[float]] = {}
        self._stale_rate_buckets: dict[str, list[float]] = {}
        self._stale_rate_limited: set[str] = set()
        self._session_stats: dict[str, int] | None = None
        self._machine_lock = _MachineFileLock(
            self.state_dir / f".{PAD_DEVICE_LEDGER_NAME}.lock"
        )
        self._generation_machine_lock: _MachineFileLock | None = None
        try:
            self._generation_machine_lock = _MachineFileLock(
                self.generation_state_path.parent
                / f".{self.generation_state_path.name}.lock"
            )
            self._devices = self._load_devices()
            self._generation_counter = self._load_generation()
        except BaseException:
            self._close_machine_locks()
            raise

    def start(self) -> None:
        with self._operation_lock:
            if self._stopped or self._stopping:
                raise RuntimeError("pad service has been stopped")
            if self._started:
                return
            injector = self._injector_factory()
            thread = threading.Thread(
                target=self._watchdog_loop,
                name="pocket-pad-watchdog",
                daemon=True,
            )
            self._injector = injector
            self._watchdog_thread = thread
            self._started = True
            try:
                thread.start()
            except BaseException:
                self._started = False
                self._watchdog_thread = None
                self._injector = None
                raise

    def stop(self) -> None:
        cancelled_pairing = False
        with self._state_lock:
            if self._stopped:
                return
            if not self._stopping:
                self._stopping = True
            nonce = self._active_pairing_nonce
            if nonce is not None:
                record = self._pairings.get(nonce)
                if record is not None and record["state"] == "pending":
                    record["state"] = "denied"
                    cancelled_pairing = True
                self._active_pairing_nonce = None
            thread = self._watchdog_thread
            pairing_threads = tuple(self._pairing_threads)
        if cancelled_pairing:
            self._audit("pair_denied")
        for pairing_thread in pairing_threads:
            if pairing_thread is not threading.current_thread() and pairing_thread.is_alive():
                pairing_thread.join(timeout=2.0)

        with self._operation_lock:
            with self._state_lock:
                device_id = self._armed_device_id
                release_device_id = self._pending_release_device_id or device_id
                self._armed_device_id = None
                self._session_generation = None
                summary = self._take_summary_locked(device_id)
            released = False
            for attempt in range(PAD_SHUTDOWN_RELEASE_MAX_ATTEMPTS):
                released = self._safe_release_all(release_device_id)
                if released:
                    break
                if attempt + 1 < PAD_SHUTDOWN_RELEASE_MAX_ATTEMPTS:
                    time.sleep(PAD_SHUTDOWN_RELEASE_RETRY_DELAY_SECONDS)
            terminal_error: PadShutdownReleaseError | None = None
            terminal_cause: BaseException | None = None
            if not released:
                with self._state_lock:
                    terminal_cause = self._pending_release_error
                    self._pending_release_device_id = None
                    self._pending_release_error = None
                terminal_error = PadShutdownReleaseError(
                    "pad input could not be released during shutdown"
                )
            if device_id is not None:
                self._audit("disarmed", deviceId=device_id)
            self._write_summary(device_id, summary)

        self._stop_event.set()

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._state_lock:
            self._stopped = True
            self._shutdown_release_error = terminal_error
            if thread is None or not thread.is_alive():
                self._watchdog_thread = None
        self._close_machine_locks()
        if terminal_error is not None:
            raise terminal_error from terminal_cause

    def request_pairing(self, label: str) -> dict[str, str]:
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 100:
            raise ValueError("device label must contain 1 through 100 characters")
        clean_label = label.strip()
        with self._state_lock:
            if self._stopping or self._stopped:
                raise RuntimeError("pad service has been stopped")
            now = self._clock()
            self._expire_pairing_locked(now)
            if now < self._lockout_until:
                raise PairingLocked("pairing is temporarily locked")
            if self._active_pairing_nonce is not None:
                raise PairingBusy("another pairing request is pending")
            nonce = secrets.token_urlsafe(24)
            code = "".join(
                secrets.choice(PAD_PAIRING_CODE_ALPHABET)
                for _ in range(PAD_PAIRING_CODE_LENGTH)
            )
            self._pairings[nonce] = {
                "state": "pending",
                "label": clean_label,
                "code": code,
                "deadline": now + PAD_PAIRING_TIMEOUT_SECONDS,
                "token": None,
            }
            self._active_pairing_nonce = nonce

        self._audit("pair_requested")
        thread = threading.Thread(
            target=self._pairing_worker,
            args=(nonce,),
            name="pocket-pad-pairing",
            daemon=True,
        )
        start_error: BaseException | None = None
        cancelled = False
        with self._state_lock:
            if self._stopping or self._stopped:
                record = self._pairings.get(nonce)
                if record is not None and record["state"] == "pending":
                    record["state"] = "denied"
                    self._active_pairing_nonce = None
                cancelled = True
            else:
                self._pairing_threads.add(thread)
                try:
                    thread.start()
                except BaseException as exc:
                    start_error = exc
                    self._pairing_threads.discard(thread)
                    record = self._pairings.get(nonce)
                    if record is not None and record["state"] == "pending":
                        record["state"] = "denied"
                        self._active_pairing_nonce = None
                        self._record_pairing_failure_locked(self._clock())
        if cancelled:
            self._audit("pair_denied")
            raise RuntimeError("pad service has been stopped")
        if start_error is not None:
            self._audit("pair_denied")
            raise start_error
        return {"nonce": nonce, "code": code}

    def pairing_status(self, nonce: str) -> dict[str, str]:
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("pairing nonce is invalid")
        with self._state_lock:
            self._expire_pairing_locked(self._clock())
            record = self._pairings.get(nonce)
            if record is None:
                raise ValueError("unknown pairing nonce")
            result = {"state": str(record["state"])}
            token = record.get("token")
            if record["state"] == "approved" and isinstance(token, str):
                result["token"] = token
                record["token"] = None
            return result

    def list_devices(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [
                {key: value for key, value in device.items() if key != "tokenHash"}
                for device in self._devices
            ]

    def revoke_device(self, device_id: str) -> bool:
        if not isinstance(device_id, str) or not device_id:
            return False
        with self._operation_lock:
            with self._state_lock:
                if self._stopping or self._stopped:
                    raise RuntimeError("pad service has been stopped")
                index = next(
                    (
                        index
                        for index, device in enumerate(self._devices)
                        if device["id"] == device_id and device["revokedAt"] is None
                    ),
                    None,
                )
                if index is None:
                    return False
                devices = [dict(device) for device in self._devices]
                devices[index]["revokedAt"] = _utc_now()
                self._commit_devices_locked(devices)
                was_armed = self._armed_device_id == device_id
                if was_armed:
                    self._armed_device_id = None
                    self._session_generation = None
                    summary = self._take_summary_locked(device_id)
                else:
                    summary = None
            if was_armed:
                self._safe_release_all(device_id)
                self._audit("disarmed", deviceId=device_id)
                self._write_summary(device_id, summary)
            return True

    def verify_token(self, token: str) -> str | None:
        if not isinstance(token, str) or not token:
            return None
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._state_lock:
            if self._stopping or self._stopped:
                return None
            match_index: int | None = None
            for index, device in enumerate(self._devices):
                matches = hmac.compare_digest(candidate, device["tokenHash"])
                if matches and device["revokedAt"] is None:
                    match_index = index
            if match_index is None:
                return None
            devices = [dict(device) for device in self._devices]
            devices[match_index]["lastSeenAt"] = _utc_now()
            try:
                self._commit_devices_locked(devices)
            except OSError:
                pass
            return str(self._devices[match_index]["id"])

    def arm(self, device_id: str, *, takeover: bool = False) -> dict[str, bool | int]:
        if not isinstance(takeover, bool):
            raise ValueError("pad takeover flag is invalid")
        self.start()
        with self._operation_lock:
            with self._state_lock:
                if self._stopping or self._stopped:
                    raise RuntimeError("pad service has been stopped")
                self._require_active_device_locked(device_id)
                previous = self._armed_device_id
                if previous is not None and previous != device_id and not takeover:
                    raise PadDisarmed("pad is armed by another device")
                if self._generation_counter >= PAD_MAX_GENERATION:
                    raise PadGenerationExhausted("pad generation space is exhausted")
                if previous is not None:
                    self._armed_device_id = None
                    self._session_generation = None
                    previous_summary = self._take_summary_locked(previous)
                else:
                    previous_summary = None
            released = self._safe_release_all(previous or device_id)
            if previous is not None:
                self._audit("disarmed", deviceId=previous)
                self._write_summary(previous, previous_summary)
            if not released:
                return {"armed": False, "blocked": True}
            with self._state_lock:
                generation = self._reserve_generation_locked()
                self._armed_device_id = device_id
                self._session_generation = generation
                self._applied_seq = 0
                self._last_activity = self._clock()
                self._session_stats = self._empty_summary()
            self._audit("armed", deviceId=device_id)
            return {"armed": True, "seq": 0, "generation": generation}

    def disarm(self, device_id: str) -> dict[str, bool]:
        with self._operation_lock:
            with self._state_lock:
                if self._stopping or self._stopped:
                    raise RuntimeError("pad service has been stopped")
                self._require_active_device_locked(device_id)
                if self._armed_device_id not in (None, device_id):
                    raise PadDisarmed("pad is armed by another device")
                was_armed = self._armed_device_id == device_id
                self._armed_device_id = None
                self._session_generation = None
                summary = self._take_summary_locked(device_id)
            self._safe_release_all(device_id)
            if was_armed:
                self._audit("disarmed", deviceId=device_id)
            self._write_summary(device_id, summary)
            return {"armed": False}

    def state(self, device_id: str | None = None) -> dict[str, Any]:
        injector = self._injector
        monitors: list[dict[str, Any]] = []
        virtual_desktop: dict[str, int] | None = None
        absolute = False
        if injector is not None:
            try:
                monitors = [
                    {
                        "id": monitor.id,
                        "left": monitor.left,
                        "top": monitor.top,
                        "right": monitor.right,
                        "bottom": monitor.bottom,
                        "dpi": monitor.dpi,
                        "primary": monitor.primary,
                    }
                    for monitor in injector.monitors()
                ]
                x, y, width, height = injector.virtual_rect()
                virtual_desktop = {"x": x, "y": y, "w": width, "h": height}
                absolute = bool(getattr(injector, "absolute_supported", False))
            except Exception:
                monitors = []
                virtual_desktop = None
                absolute = False
        with self._state_lock:
            if device_id is None:
                armed = self._armed_device_id is not None
                generation = None
            else:
                self._require_active_device_locked(device_id)
                armed = self._armed_device_id == device_id
                generation = self._session_generation if armed else None
        return {
            "armed": armed,
            "generation": generation,
            "absolute": absolute,
            "monitors": monitors,
            "vd": virtual_desktop,
            "limits": {
                "batch": PAD_MAX_BATCH,
                "textChars": PAD_TEXT_MAX_CHARS,
                "movePerSec": PAD_RATE_MOVE_PER_SEC,
                "keyPerSec": PAD_RATE_KEY_PER_SEC,
                "shortcutPerSec": PAD_RATE_SHORTCUT_PER_SEC,
                "textCharsPerSec": PAD_RATE_TEXT_CHARS_PER_SEC,
            },
        }

    def dispatch(self, device_id: str, command: dict[str, Any]) -> dict[str, Any]:
        self.start()
        generation, _sequence = _validate_top_level_identity(command)
        with self._operation_lock:
            with self._state_lock:
                if self._stopping or self._stopped:
                    raise RuntimeError("pad service has been stopped")
                self._require_active_device_locked(device_id)
                stale_generation = (
                    self._armed_device_id != device_id
                    or generation != self._session_generation
                )
                if stale_generation:
                    stale_allowed = self._reserve_stale_rate_locked(
                        device_id, self._clock()
                    )
                    rate_transition = False
                    if stale_allowed:
                        self._stale_rate_limited.discard(device_id)
                    elif device_id not in self._stale_rate_limited:
                        self._stale_rate_limited.add(device_id)
                        rate_transition = True
            if stale_generation:
                if stale_allowed:
                    self._audit("stale_dropped", deviceId=device_id)
                    return {"dropped": True, "reason": "staleGeneration"}
                if rate_transition:
                    self._audit("rate_limited", deviceId=device_id, category="stale")
                raise PadRateLimited("stale pad command rate exceeded")
            try:
                parsed = parse_pad_command(command)
            except PadSequenceExceeded:
                raise
            except ValueError:
                with self._state_lock:
                    if (
                        self._armed_device_id == device_id
                        and self._session_generation == generation
                    ):
                        self._armed_device_id = None
                        self._session_generation = None
                        summary = self._take_summary_locked(device_id)
                    else:
                        summary = None
                if summary is not None:
                    self._safe_release_all(device_id)
                    self._write_summary(device_id, summary)
                raise
            out_of_band_result = self._dispatch_out_of_band_command(device_id, parsed)
            if out_of_band_result is not None:
                return out_of_band_result
            rate_error: str | None = None
            idle = False
            command_to_execute = parsed
            with self._state_lock:
                if self._stopping or self._stopped:
                    raise RuntimeError("pad service has been stopped")
                self._require_active_device_locked(device_id)
                if self._armed_device_id != device_id:
                    raise PadDisarmed("pad is disarmed")
                sequence = parsed.get("n")
                if sequence is not None and sequence <= self._applied_seq:
                    if parsed["t"] == "batch":
                        retained_children = [
                            child
                            for child in parsed["c"]
                            if self._is_stale_release_child(child)
                        ]
                        if len(retained_children) != len(parsed["c"]):
                            self._audit("stale_dropped", deviceId=device_id)
                        if not retained_children:
                            return {"dropped": True, "reason": "stale"}
                        command_to_execute = {"t": "batch", "c": retained_children}
                    elif not self._is_stale_release_child(parsed):
                        self._audit("stale_dropped", deviceId=device_id)
                        return {"dropped": True, "reason": "stale"}
                now = self._clock()
                elapsed = max(0.0, now - self._last_activity)
                if elapsed >= PAD_IDLE_DISARM_SECONDS:
                    self._armed_device_id = None
                    self._session_generation = None
                    summary = self._take_summary_locked(device_id)
                    idle = True
                else:
                    costs = self._command_costs(command_to_execute)
                    rate_error = self._reserve_rate_locked(device_id, costs, now)
                    if rate_error is not None:
                        self._armed_device_id = None
                        self._session_generation = None
                        summary = self._take_summary_locked(device_id)
                    else:
                        self._last_activity = now
                        summary = None
            if idle:
                self._safe_release_all(device_id)
                self._audit("idle_disarm", deviceId=device_id)
                self._write_summary(device_id, summary)
                raise PadDisarmed("pad was disarmed after inactivity")
            if rate_error is not None:
                self._safe_release_all(device_id)
                self._audit("rate_limited", deviceId=device_id, category=rate_error)
                self._write_summary(device_id, summary)
                raise PadRateLimited(f"pad {rate_error} rate exceeded")

            try:
                result = self._execute_command(device_id, command_to_execute)
                if sequence is not None:
                    with self._state_lock:
                        self._applied_seq = max(self._applied_seq, sequence)
                return result
            except InputBlocked:
                with self._state_lock:
                    self._armed_device_id = None
                    self._session_generation = None
                    summary = self._take_summary_locked(device_id)
                self._safe_release_all(device_id)
                self._audit("uipi_blocked", deviceId=device_id)
                self._write_summary(device_id, summary)
                return {"blocked": True}
            except BaseException:
                with self._state_lock:
                    self._armed_device_id = None
                    self._session_generation = None
                    summary = self._take_summary_locked(device_id)
                self._safe_release_all(device_id)
                self._write_summary(device_id, summary)
                raise

    def foreground_snapshot(self, device_id: str | None = None) -> dict[str, Any]:
        monitor_id: str | None = None
        injector = self._injector
        if injector is not None:
            try:
                cursor_x, cursor_y = injector.cursor_pos()
                for monitor in injector.monitors():
                    if (
                        monitor.left <= cursor_x < monitor.right
                        and monitor.top <= cursor_y < monitor.bottom
                    ):
                        monitor_id = monitor.id
                        break
            except Exception:
                monitor_id = None
        try:
            title = _foreground_title()
        except BaseException:
            title = None
        if title is not None:
            title = title[:60]
        with self._state_lock:
            if device_id is None:
                armed = self._armed_device_id is not None
                generation = None
            else:
                self._require_active_device_locked(device_id)
                armed = self._armed_device_id == device_id
                generation = self._session_generation if armed else None
        return {"mon": monitor_id, "fg": title, "armed": armed, "generation": generation}

    def connection_opened(self, device_id: str) -> None:
        with self._state_lock:
            if self._stopping or self._stopped:
                raise RuntimeError("pad service has been stopped")
            self._require_known_device_locked(device_id)
        self._audit("connected", deviceId=device_id)

    def connection_closed(self, device_id: str) -> None:
        with self._operation_lock:
            with self._state_lock:
                if self._stopping or self._stopped:
                    raise RuntimeError("pad service has been stopped")
                self._require_known_device_locked(device_id)
                was_armed = self._armed_device_id == device_id
                if was_armed:
                    self._armed_device_id = None
                    self._session_generation = None
                    summary = self._take_summary_locked(device_id)
                else:
                    summary = None
            if was_armed:
                self._safe_release_all(device_id)
            self._audit("disconnected", deviceId=device_id)
            if was_armed:
                self._audit("disarmed", deviceId=device_id)
                self._write_summary(device_id, summary)

    def _pairing_worker(self, nonce: str) -> None:
        try:
            pre_event: str | None = None
            with self._state_lock:
                record = self._pairings.get(nonce)
                if (
                    record is None
                    or record["state"] != "pending"
                    or self._stopping
                    or self._stopped
                ):
                    return
                now = self._clock()
                if now >= record["deadline"]:
                    record["state"] = "expired"
                    self._active_pairing_nonce = None
                    self._record_pairing_failure_locked(now)
                    pre_event = "pair_expired"
                    code = ""
                else:
                    code = str(record["code"])
            if pre_event is not None:
                self._audit(pre_event)
                return
            text = (
                "iPhone トラックパッドの接続要求です。\n\n"
                "許可するとこの端末は PC のマウス・キーボードを操作できます。\n\n"
                f"照合コード:  {code}\n\n"
                "電話に表示されたコードと一致する場合だけ［はい］を選んでください。"
            )
            try:
                response = self._message_box(
                    text,
                    "ポケットmycmux - トラックパッド接続",
                    PAD_PAIRING_MESSAGE_FLAGS,
                )
            except BaseException:
                response = 0

            event = "pair_denied"
            device_id: str | None = None
            with self._state_lock:
                record = self._pairings.get(nonce)
                now = self._clock()
                if (
                    record is None
                    or record["state"] != "pending"
                    or self._stopping
                    or self._stopped
                ):
                    return
                if now >= record["deadline"]:
                    record["state"] = "expired"
                    self._active_pairing_nonce = None
                    self._record_pairing_failure_locked(now)
                    event = "pair_expired"
                elif response == IDYES:
                    token = secrets.token_urlsafe(32)
                    device_id = uuid.uuid4().hex
                    device = {
                        "id": device_id,
                        "tokenHash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "label": record["label"],
                        "createdAt": _utc_now(),
                        "lastSeenAt": None,
                        "revokedAt": None,
                    }
                    try:
                        self._commit_devices_locked([*self._devices, device])
                    except (OSError, UnicodeError):
                        record["state"] = "denied"
                        self._active_pairing_nonce = None
                        self._record_pairing_failure_locked(now)
                    else:
                        record["state"] = "approved"
                        record["token"] = token
                        record["deviceId"] = device_id
                        self._active_pairing_nonce = None
                        self._pairing_failures = 0
                        self._lockout_until = 0.0
                        event = "pair_approved"
                else:
                    record["state"] = "denied"
                    self._active_pairing_nonce = None
                    self._record_pairing_failure_locked(now)
            if event == "pair_approved" and device_id is not None:
                self._audit(event, deviceId=device_id)
            else:
                self._audit(event)
        finally:
            with self._state_lock:
                self._pairing_threads.discard(threading.current_thread())

    def _expire_pairing_locked(self, now: float) -> None:
        nonce = self._active_pairing_nonce
        if nonce is None:
            return
        record = self._pairings.get(nonce)
        if record is None or record["state"] != "pending":
            self._active_pairing_nonce = None
            return
        if now < record["deadline"]:
            return
        record["state"] = "expired"
        self._active_pairing_nonce = None
        self._record_pairing_failure_locked(now)
        self._audit("pair_expired")

    def _record_pairing_failure_locked(self, now: float) -> None:
        self._pairing_failures += 1
        if self._pairing_failures >= PAD_PAIRING_LOCKOUT_FAILURES:
            self._lockout_until = now + PAD_PAIRING_LOCKOUT_SECONDS
            self._pairing_failures = 0

    def _watchdog_loop(self) -> None:
        try:
            while not self._stop_event.wait(PAD_WATCHDOG_INTERVAL_SECONDS):
                try:
                    self._watchdog_once()
                except BaseException:
                    continue
        finally:
            if self._stop_event.is_set():
                self._close_machine_locks()

    def _watchdog_once(self) -> None:
        with self._operation_lock:
            with self._state_lock:
                pending_device_id = self._pending_release_device_id
            if pending_device_id is not None:
                if not self._safe_release_all(pending_device_id):
                    return
            with self._state_lock:
                device_id = self._armed_device_id
                if device_id is None:
                    return
                now = self._clock()
                if max(0.0, now - self._last_activity) < PAD_IDLE_DISARM_SECONDS:
                    return
                self._armed_device_id = None
                self._session_generation = None
                summary = self._take_summary_locked(device_id)
            self._safe_release_all(device_id)
            self._audit("idle_disarm", deviceId=device_id)
            self._write_summary(device_id, summary)

    def _safe_release_all(self, device_id: str | None) -> bool:
        injector = self._injector
        if injector is None:
            return True
        try:
            injector.release_all()
        except InputBlocked as exc:
            if device_id is not None:
                self._audit("uipi_blocked", deviceId=device_id)
            with self._state_lock:
                self._pending_release_device_id = device_id or self._pending_release_device_id
                self._pending_release_error = exc
            return False
        except Exception as exc:
            with self._state_lock:
                self._pending_release_device_id = device_id or self._pending_release_device_id
                self._pending_release_error = exc
            return False
        with self._state_lock:
            self._pending_release_device_id = None
            self._pending_release_error = None
        return True

    def _dispatch_out_of_band_command(
        self, device_id: str, command: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handle authenticated keepalive commands outside input rate state."""
        command_type = command["t"]
        if command_type != "p":
            return None
        with self._state_lock:
            if self._stopping or self._stopped:
                raise RuntimeError("pad service has been stopped")
            self._require_active_device_locked(device_id)
            if self._reserve_ping_rate_locked(device_id, self._clock()):
                return {"t": "po", "id": command["id"]}
        self._audit("rate_limited", deviceId=device_id, category="ping")
        raise PadRateLimited("pad ping rate exceeded")

    def _require_active_device_locked(self, device_id: str) -> None:
        if not isinstance(device_id, str) or not any(
            device["id"] == device_id and device["revokedAt"] is None
            for device in self._devices
        ):
            raise ValueError("unknown or revoked pad device")

    def _require_known_device_locked(self, device_id: str) -> None:
        if not isinstance(device_id, str) or not any(
            device["id"] == device_id for device in self._devices
        ):
            raise ValueError("unknown pad device")

    def _load_devices(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.device_path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("pad device ledger is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
            raise RuntimeError("pad device ledger is invalid")
        required = {"id", "tokenHash", "label", "createdAt", "lastSeenAt", "revokedAt"}
        devices: list[dict[str, Any]] = []
        ids: set[str] = set()
        for device in payload["devices"]:
            if not isinstance(device, dict) or set(device) != required:
                raise RuntimeError("pad device ledger is invalid")
            if (
                not isinstance(device["id"], str)
                or not device["id"]
                or device["id"] in ids
                or not isinstance(device["tokenHash"], str)
                or len(device["tokenHash"]) != 64
                or any(character not in "0123456789abcdef" for character in device["tokenHash"])
                or not isinstance(device["label"], str)
                or not isinstance(device["createdAt"], str)
                or device["lastSeenAt"] is not None and not isinstance(device["lastSeenAt"], str)
                or device["revokedAt"] is not None and not isinstance(device["revokedAt"], str)
            ):
                raise RuntimeError("pad device ledger is invalid")
            ids.add(device["id"])
            devices.append(dict(device))
        return devices

    def _load_generation(self) -> int:
        try:
            payload = json.loads(self.generation_state_path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return PAD_LEGACY_MAX_GENERATION
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("pad generation state is invalid") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "lastGeneration"}
            or payload.get("version") != 1
            or not _is_int(payload.get("lastGeneration"))
            or not PAD_LEGACY_MAX_GENERATION
            <= payload["lastGeneration"]
            <= PAD_MAX_GENERATION
        ):
            raise RuntimeError("pad generation state is invalid")
        return int(payload["lastGeneration"])

    def _reserve_generation_locked(self) -> int:
        if self._generation_counter >= PAD_MAX_GENERATION:
            raise PadGenerationExhausted("pad generation space is exhausted")
        generation = self._generation_counter + 1
        self.generation_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.generation_state_path.with_name(
            f"{self.generation_state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="ascii", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        {"version": 1, "lastGeneration": generation},
                        ensure_ascii=True,
                        indent=2,
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.generation_state_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        self._generation_counter = generation
        return generation

    def _close_machine_locks(self) -> None:
        generation_lock = self._generation_machine_lock
        self._generation_machine_lock = None
        if generation_lock is not None:
            generation_lock.close()
        self._machine_lock.close()

    def _commit_devices_locked(self, devices: list[dict[str, Any]]) -> None:
        self.device_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.device_path.with_name(
            f"{self.device_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps({"devices": devices}, ensure_ascii=True, indent=2) + "\n",
                encoding="ascii",
            )
            os.replace(temporary, self.device_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        self._devices = [dict(device) for device in devices]

    def _rate_state_locked(self, device_id: str, now: float) -> dict[str, list[float]]:
        buckets = self._rate_buckets.get(device_id)
        if buckets is None:
            buckets = {
                category: [capacity, now] for category, capacity in _RATE_LIMITS.items()
            }
            self._rate_buckets[device_id] = buckets
        return buckets

    def _reserve_ping_rate_locked(self, device_id: str, now: float) -> bool:
        tokens, last = self._ping_rate_buckets.get(
            device_id, [float(PAD_RATE_PING_PER_SEC), now]
        )
        effective_now = max(now, last)
        tokens = min(
            float(PAD_RATE_PING_PER_SEC),
            tokens + (effective_now - last) * PAD_RATE_PING_PER_SEC,
        )
        if tokens < 1.0:
            self._ping_rate_buckets[device_id] = [tokens, effective_now]
            return False
        self._ping_rate_buckets[device_id] = [tokens - 1.0, effective_now]
        return True

    def _reserve_stale_rate_locked(self, device_id: str, now: float) -> bool:
        tokens, last = self._stale_rate_buckets.get(
            device_id, [float(PAD_RATE_STALE_PER_SEC), now]
        )
        effective_now = max(now, last)
        tokens = min(
            float(PAD_RATE_STALE_PER_SEC),
            tokens + (effective_now - last) * PAD_RATE_STALE_PER_SEC,
        )
        if tokens < 1.0:
            self._stale_rate_buckets[device_id] = [tokens, effective_now]
            return False
        self._stale_rate_buckets[device_id] = [tokens - 1.0, effective_now]
        return True

    def _reserve_rate_locked(
        self,
        device_id: str,
        costs: dict[str, float],
        now: float,
    ) -> str | None:
        buckets = self._rate_state_locked(device_id, now)
        refilled: dict[str, float] = {}
        effective_times: dict[str, float] = {}
        for category, capacity in _RATE_LIMITS.items():
            tokens, last = buckets[category]
            effective_now = max(now, last)
            effective_times[category] = effective_now
            elapsed = effective_now - last
            refilled[category] = min(capacity, tokens + elapsed * capacity)
        for category, cost in costs.items():
            if cost > refilled[category]:
                for refill_category, tokens in refilled.items():
                    buckets[refill_category] = [
                        tokens,
                        effective_times[refill_category],
                    ]
                return category
        for category, tokens in refilled.items():
            buckets[category] = [
                tokens - costs.get(category, 0.0),
                effective_times[category],
            ]
        return None

    def _command_costs(self, command: dict[str, Any]) -> dict[str, float]:
        costs = {category: 0.0 for category in _RATE_LIMITS}

        def add(item: dict[str, Any]) -> None:
            command_type = item["t"]
            if command_type == "batch":
                for child in item["c"]:
                    add(child)
            elif command_type in ("m", "w"):
                costs["move"] += 1.0
            elif command_type in ("b", "k"):
                costs["key"] += 1.0
            elif command_type in ("combo", "warp"):
                costs["shortcut"] += 1.0
            elif command_type == "txt":
                costs["text"] += float(len(item["s"]))

        add(command)
        return costs

    @staticmethod
    def _is_stale_release_child(child: dict[str, Any]) -> bool:
        """Return whether one non-batch command must survive stale filtering."""
        command_type = child["t"]
        if command_type == "rel" or command_type == "combo":
            return True
        if command_type in ("b", "k"):
            return child["d"] == 0
        return False

    def _execute_command(self, device_id: str, command: dict[str, Any]) -> dict[str, Any]:
        injector = self._injector
        if injector is None:
            raise InjectorUnavailable("pad injector is not started")
        command_type = command["t"]
        if command_type == "batch":
            for child in command["c"]:
                self._execute_command(device_id, child)
            return {"ok": True}
        if command_type == "m":
            injector.move_relative(command["dx"], command["dy"])
            self._count_success("moveCount", 1)
        elif command_type == "b":
            injector.button(_BUTTON_NAMES[command["b"]], bool(command["d"]))
            if command["d"]:
                self._count_success("clickCount", 1)
        elif command_type == "w":
            if command["dy"]:
                injector.wheel(command["dy"])
            if command["dx"]:
                injector.wheel(command["dx"], horizontal=True)
            self._count_success("moveCount", 1)
        elif command_type == "k":
            injector.key(command["vk"], bool(command["d"]))
            if command["d"]:
                self._count_success("keyCount", 1)
        elif command_type == "combo":
            injector.combo(command["vk"], command["mods"])
            self._count_success("keyCount", 1)
        elif command_type == "txt":
            injector.type_text(command["s"])
            self._count_success("textChars", len(command["s"]))
        elif command_type == "warp":
            injector.warp_to_monitor(command["mon"])
            self._count_success("moveCount", 1)
        elif command_type == "rel":
            injector.release_all()
        elif command_type == "p":
            return {"t": "po", "id": command["id"]}
        return {"ok": True}

    def _count_success(self, field: str, amount: int) -> None:
        with self._state_lock:
            if self._session_stats is not None:
                self._session_stats[field] += amount

    @staticmethod
    def _empty_summary() -> dict[str, int]:
        return {"moveCount": 0, "clickCount": 0, "keyCount": 0, "textChars": 0}

    def _take_summary_locked(self, device_id: str | None) -> dict[str, int] | None:
        if device_id is None or self._session_stats is None:
            self._session_stats = None
            return None
        summary = dict(self._session_stats)
        self._session_stats = None
        return summary

    def _write_summary(self, device_id: str | None, summary: dict[str, int] | None) -> None:
        if device_id is None or summary is None:
            return
        self._audit("session_summary", deviceId=device_id, **summary)

    def _audit(self, event: str, **fields: Any) -> None:
        allowed = _AUDIT_FIELDS.get(event)
        if allowed is None or set(fields) != set(allowed):
            raise ValueError("audit event fields are invalid")
        # Never accept command payloads, typed text, vk sequences, or foreground titles here.
        entry = {"at": _utc_now(), "event": event, **fields}
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_lock:
                with self.audit_path.open("a", encoding="ascii", newline="\n") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=True) + "\n")
                if self.audit_path.stat().st_size <= PAD_AUDIT_LOG_MAX_BYTES:
                    return
                payload = self.audit_path.read_bytes()
                retained = payload[len(payload) // 2 :]
                newline = retained.find(b"\n")
                if newline >= 0:
                    retained = retained[newline + 1 :]
                temporary = self.audit_path.with_name(
                    f"{self.audit_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                try:
                    temporary.write_bytes(retained)
                    os.replace(temporary, self.audit_path)
                finally:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        except (OSError, UnicodeError):
            return
