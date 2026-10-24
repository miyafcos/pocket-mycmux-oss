from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import threading
import time
from typing import NamedTuple, Sequence


PAD_TEXT_MAX_CHARS = 500
MONITOR_CACHE_TTL_SECONDS = 3.0
PER_MONITOR_AWARE_V2 = -4
MONITORINFOF_PRIMARY = 0x00000001
MDT_EFFECTIVE_DPI = 0
DEFAULT_MONITOR_DPI = 96

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
WHEEL_DELTA = 120

BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

VK: dict[str, int] = {
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "rightWin": 0x5C,
    "rightCtrl": 0xA3,
    "rightAlt": 0xA5,
    "enter": 0x0D,
    "esc": 0x1B,
    "tab": 0x09,
    "back": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "space": 0x20,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pageUp": 0x21,
    "pageDown": 0x22,
    "printScreen": 0x2C,
    "volumeMute": 0xAD,
    "volumeDown": 0xAE,
    "volumeUp": 0xAF,
    "mediaNext": 0xB0,
    "mediaPrev": 0xB1,
    "mediaStop": 0xB2,
    "mediaPlayPause": 0xB3,
}
VK.update({chr(code).lower(): code for code in range(0x41, 0x5B)})
VK.update({f"f{number}": 0x6F + number for number in range(1, 13)})

MODIFIER_VKS = {name: VK[name] for name in ("ctrl", "alt", "shift", "win")}
EXTENDED_VKS = frozenset(
    {
        VK["pageUp"],
        VK["pageDown"],
        VK["end"],
        VK["home"],
        VK["left"],
        VK["up"],
        VK["right"],
        VK["down"],
        VK["insert"],
        VK["delete"],
        VK["printScreen"],
        VK["win"],
        VK["rightWin"],
        VK["rightCtrl"],
        VK["rightAlt"],
        VK["volumeMute"],
        VK["volumeDown"],
        VK["volumeUp"],
        VK["mediaNext"],
        VK["mediaPrev"],
        VK["mediaStop"],
        VK["mediaPlayPause"],
    }
)


class InjectorUnavailable(RuntimeError):
    pass


class InputBlocked(RuntimeError):
    def __init__(self, errno: int) -> None:
        self.errno = errno
        super().__init__(f"SendInput inserted fewer events than requested (errno={errno})")


ULONG_PTR = wintypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    )


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    )


class Monitor(NamedTuple):
    id: str
    left: int
    top: int
    right: int
    bottom: int
    dpi: int
    primary: bool


HMONITOR = getattr(wintypes, "HMONITOR", wintypes.HANDLE)
MONITORENUMPROC = (
    ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    if os.name == "nt"
    else None
)

_user32 = None
_shcore = None
if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    )
    _user32.SendInput.restype = wintypes.UINT
    _user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
    _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    _user32.GetThreadDpiAwarenessContext.argtypes = ()
    _user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    _user32.GetAwarenessFromDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
    _user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    _user32.EnumDisplayMonitors.argtypes = (
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        MONITORENUMPROC,
        wintypes.LPARAM,
    )
    _user32.EnumDisplayMonitors.restype = wintypes.BOOL
    _user32.GetMonitorInfoW.argtypes = (HMONITOR, ctypes.POINTER(MONITORINFOEXW))
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    _user32.GetCursorPos.restype = wintypes.BOOL

    try:
        _shcore = ctypes.WinDLL("shcore", use_last_error=True)
        _shcore.GetDpiForMonitor.argtypes = (
            HMONITOR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        )
        _shcore.GetDpiForMonitor.restype = ctypes.c_long
    except (AttributeError, OSError):
        _shcore = None


_monitor_cache: tuple[Monitor, ...] = ()
_monitor_cache_at = 0.0
_monitor_cache_lock = threading.Lock()


def _invalidate_monitor_cache() -> None:
    global _monitor_cache, _monitor_cache_at

    with _monitor_cache_lock:
        _monitor_cache = ()
        _monitor_cache_at = 0.0


def ensure_dpi_awareness() -> bool:
    if _user32 is None:
        return False
    if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(PER_MONITOR_AWARE_V2)):
        return True
    context = _user32.GetThreadDpiAwarenessContext()
    if not context:
        return False
    awareness = _user32.GetAwarenessFromDpiAwarenessContext(context)
    return awareness in (1, 2)


def enumerate_monitors() -> tuple[Monitor, ...]:
    global _monitor_cache, _monitor_cache_at

    if _user32 is None or MONITORENUMPROC is None:
        raise InjectorUnavailable("monitor enumeration requires Windows")

    with _monitor_cache_lock:
        now = time.monotonic()
        if _monitor_cache and now - _monitor_cache_at < MONITOR_CACHE_TTL_SECONDS:
            return _monitor_cache

        monitors: list[Monitor] = []
        callback_errors: list[Exception] = []

        def visit_monitor(
            monitor_handle: int,
            _device_context: int,
            _monitor_rect: ctypes.POINTER(wintypes.RECT),
            _data: int,
        ) -> bool:
            try:
                info = MONITORINFOEXW()
                info.cbSize = ctypes.sizeof(MONITORINFOEXW)
                if not _user32.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
                    error = ctypes.get_last_error()
                    raise InjectorUnavailable(
                        f"GetMonitorInfoW failed with Win32 error {error}"
                    )

                dpi_x = wintypes.UINT(DEFAULT_MONITOR_DPI)
                dpi_y = wintypes.UINT(DEFAULT_MONITOR_DPI)
                # ponytail: ceiling=96 DPI on a failed legacy shcore query;
                # upgrade path=GetDpiForWindow once a window handle exists.
                dpi = DEFAULT_MONITOR_DPI
                if _shcore is not None:
                    dpi_result = _shcore.GetDpiForMonitor(
                        monitor_handle,
                        MDT_EFFECTIVE_DPI,
                        ctypes.byref(dpi_x),
                        ctypes.byref(dpi_y),
                    )
                    if dpi_result == 0 and dpi_x.value > 0:
                        dpi = int(dpi_x.value)
                rect = info.rcMonitor
                monitors.append(
                    Monitor(
                        id=info.szDevice,
                        left=int(rect.left),
                        top=int(rect.top),
                        right=int(rect.right),
                        bottom=int(rect.bottom),
                        dpi=dpi,
                        primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    )
                )
                return True
            except Exception as exc:
                callback_errors.append(exc)
                return False

        callback = MONITORENUMPROC(visit_monitor)
        result = _user32.EnumDisplayMonitors(None, None, callback, 0)
        if callback_errors:
            raise callback_errors[0]
        if not result:
            error = ctypes.get_last_error()
            raise InjectorUnavailable(
                f"EnumDisplayMonitors failed with Win32 error {error}"
            )
        if not monitors:
            raise InjectorUnavailable("no active display monitor was found")

        _monitor_cache = tuple(monitors)
        _monitor_cache_at = time.monotonic()
        return _monitor_cache


def clamp_to_desktop(
    x: float,
    y: float,
    rects: tuple[tuple[int, int, int, int], ...],
) -> tuple[float, float]:
    """Return the nearest pixel coordinate in the union of monitor rectangles."""
    if not rects:
        raise ValueError("at least one monitor rectangle is required")
    for left, top, right, bottom in rects:
        if right <= left or bottom <= top:
            raise ValueError("monitor rectangles must have positive width and height")
    for left, top, right, bottom in rects:
        if left <= x < right and top <= y < bottom:
            return x, y

    best_point: tuple[float, float] | None = None
    best_distance_squared = math.inf
    for left, top, right, bottom in rects:
        candidate_x = min(max(x, left), right - 1)
        candidate_y = min(max(y, top), bottom - 1)
        distance_squared = (candidate_x - x) ** 2 + (candidate_y - y) ** 2
        if distance_squared < best_distance_squared:
            best_point = candidate_x, candidate_y
            best_distance_squared = distance_squared
    if best_point is None:
        raise ValueError("at least one valid monitor rectangle is required")
    return best_point


def normalize_absolute(
    x: int,
    y: int,
    rect: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Normalize a virtual desktop (x, y, width, height) to 0..65535."""
    virtual_x, virtual_y, width, height = rect
    if width <= 1 or height <= 1:
        raise ValueError("virtual desktop dimensions must both exceed one pixel")
    normalized_x = round((x - virtual_x) * 65535 / (width - 1))
    normalized_y = round((y - virtual_y) * 65535 / (height - 1))
    return (
        min(max(int(normalized_x), 0), 65535),
        min(max(int(normalized_y), 0), 65535),
    )


def _virtual_rect_for(monitors: tuple[Monitor, ...]) -> tuple[int, int, int, int]:
    if not monitors:
        raise InjectorUnavailable("no active display monitor was found")
    left = min(monitor.left for monitor in monitors)
    top = min(monitor.top for monitor in monitors)
    right = max(monitor.right for monitor in monitors)
    bottom = max(monitor.bottom for monitor in monitors)
    return left, top, right - left, bottom - top


def _mouse_event(
    flags: int,
    *,
    dx: int = 0,
    dy: int = 0,
    mouse_data: int = 0,
) -> INPUT:
    event = INPUT()
    event.type = INPUT_MOUSE
    event.mi = MOUSEINPUT(
        dx=dx,
        dy=dy,
        mouseData=mouse_data,
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return event


def _keyboard_event(vk: int, *, down: bool) -> INPUT:
    flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0
    if not down:
        flags |= KEYEVENTF_KEYUP
    event = INPUT()
    event.type = INPUT_KEYBOARD
    event.ki = KEYBDINPUT(
        wVk=vk,
        wScan=0,
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return event


def _unicode_event(unit: int, *, down: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE if down else KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    event = INPUT()
    event.type = INPUT_KEYBOARD
    event.ki = KEYBDINPUT(
        wVk=0,
        wScan=unit,
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return event


class Injector:
    absolute_supported: bool

    def __init__(self) -> None:
        if _user32 is None:
            raise InjectorUnavailable("input injection requires Windows")
        self.absolute_supported = ensure_dpi_awareness()
        _invalidate_monitor_cache()
        cursor_x, cursor_y = self.cursor_pos()
        self._pos = float(cursor_x), float(cursor_y)
        self._held_keys: dict[int, float] = {}
        self._held_buttons: set[str] = set()
        self._held_button_times: dict[str, float] = {}
        self._held_unicode_units: list[tuple[int, float, int]] = []
        self._held_order: dict[tuple[str, int | str], int] = {}
        self._held_sequence = 0
        self._held_lock = threading.Lock()
        # ponytail: ceiling=one serialized producer for state-changing input;
        # upgrade path=a dedicated operation queue if multiple producers are added.

    def monitors(self) -> tuple[Monitor, ...]:
        return enumerate_monitors()

    def virtual_rect(self) -> tuple[int, int, int, int]:
        return _virtual_rect_for(self.monitors())

    def cursor_pos(self) -> tuple[int, int]:
        if _user32 is None:
            raise InjectorUnavailable("cursor position requires Windows")
        point = wintypes.POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            error = ctypes.get_last_error()
            raise InjectorUnavailable(f"GetCursorPos failed with Win32 error {error}")
        return int(point.x), int(point.y)

    def _monitor_rects(self) -> tuple[tuple[int, int, int, int], ...]:
        return tuple(
            (monitor.left, monitor.top, monitor.right, monitor.bottom)
            for monitor in self.monitors()
        )

    def move_relative(self, dx: float, dy: float) -> tuple[int, int]:
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError("relative movement must be finite")
        actual_x, actual_y = self.cursor_pos()
        tracked_x, tracked_y = self._pos
        if math.hypot(actual_x - tracked_x, actual_y - tracked_y) > 2.0:
            tracked_x, tracked_y = float(actual_x), float(actual_y)
        target = clamp_to_desktop(
            tracked_x + dx,
            tracked_y + dy,
            self._monitor_rects(),
        )
        requested = round(target[0]), round(target[1])
        moved = self.move_absolute(*requested)
        self._pos = target if moved == requested else (float(moved[0]), float(moved[1]))
        return moved

    def move_absolute(self, x: int, y: int) -> tuple[int, int]:
        if not self.absolute_supported:
            raise InjectorUnavailable("absolute movement requires DPI awareness")
        monitors = self.monitors()
        rects = tuple(
            (monitor.left, monitor.top, monitor.right, monitor.bottom)
            for monitor in monitors
        )
        clamped_x, clamped_y = clamp_to_desktop(x, y, rects)
        pixel_x = int(round(clamped_x))
        pixel_y = int(round(clamped_y))
        pixel_x_float, pixel_y_float = clamp_to_desktop(pixel_x, pixel_y, rects)
        pixel_x = int(pixel_x_float)
        pixel_y = int(pixel_y_float)
        normalized_x, normalized_y = normalize_absolute(
            pixel_x,
            pixel_y,
            _virtual_rect_for(monitors),
        )
        flags = (
            MOUSEEVENTF_MOVE
            | MOUSEEVENTF_ABSOLUTE
            | MOUSEEVENTF_VIRTUALDESK
            | MOUSEEVENTF_MOVE_NOCOALESCE
        )
        self._send([_mouse_event(flags, dx=normalized_x, dy=normalized_y)])
        self._pos = float(pixel_x), float(pixel_y)
        return pixel_x, pixel_y

    def warp_to_monitor(self, monitor_id: str) -> tuple[int, int]:
        for monitor in self.monitors():
            if monitor.id == monitor_id:
                center_x = monitor.left + (monitor.right - monitor.left) // 2
                center_y = monitor.top + (monitor.bottom - monitor.top) // 2
                return self.move_absolute(center_x, center_y)
        raise ValueError(f"unknown monitor id: {monitor_id}")

    def button(self, name: str, down: bool) -> None:
        if name not in BUTTON_FLAGS:
            raise ValueError(f"unknown mouse button: {name}")
        down_flag, up_flag = BUTTON_FLAGS[name]
        self._send([_mouse_event(down_flag if down else up_flag)])
        with self._held_lock:
            if down:
                self._held_buttons.add(name)
                self._held_button_times[name] = time.monotonic()
                self._held_sequence += 1
                self._held_order[("button", name)] = self._held_sequence
            else:
                self._held_buttons.discard(name)
                self._held_button_times.pop(name, None)
                self._held_order.pop(("button", name), None)

    def wheel(self, delta: int, horizontal: bool = False) -> None:
        mouse_data = ctypes.c_int32(delta).value & 0xFFFFFFFF
        flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        self._send([_mouse_event(flags, mouse_data=mouse_data)])

    def key(self, vk: int, down: bool) -> None:
        self._validate_vk(vk)
        self._send([_keyboard_event(vk, down=down)])
        with self._held_lock:
            if down:
                self._held_keys[vk] = time.monotonic()
                self._held_sequence += 1
                self._held_order[("key", vk)] = self._held_sequence
            else:
                self._held_keys.pop(vk, None)
                self._held_order.pop(("key", vk), None)

    def combo(self, vk: int, mods: Sequence[str] = ()) -> None:
        self._validate_vk(vk)
        modifier_names = tuple(mods)
        if len(set(modifier_names)) != len(modifier_names):
            raise ValueError("duplicate modifiers are not allowed")
        try:
            modifier_vks = tuple(MODIFIER_VKS[name] for name in modifier_names)
        except KeyError as exc:
            raise ValueError(f"unknown modifier: {exc.args[0]}") from None
        if vk in modifier_vks:
            raise ValueError("combo target cannot also be a modifier")
        with self._held_lock:
            if vk in self._held_keys:
                raise ValueError("combo target is already held")
            modifiers_to_press = tuple(
                modifier for modifier in modifier_vks if modifier not in self._held_keys
            )
        events = [_keyboard_event(modifier, down=True) for modifier in modifiers_to_press]
        events.extend((_keyboard_event(vk, down=True), _keyboard_event(vk, down=False)))
        events.extend(
            _keyboard_event(modifier, down=False)
            for modifier in reversed(modifiers_to_press)
        )
        try:
            self._send(events)
        except InputBlocked as original_error:
            cleanup = [_keyboard_event(vk, down=False)]
            cleanup.extend(
                _keyboard_event(modifier, down=False)
                for modifier in reversed(modifiers_to_press)
            )
            try:
                self._send(cleanup)
            except InputBlocked:
                for possible_vk in (*modifiers_to_press, vk):
                    self._remember_possibly_held_key(possible_vk)
            raise original_error

    def type_text(self, text: str) -> None:
        if len(text) > PAD_TEXT_MAX_CHARS:
            raise ValueError(f"text exceeds {PAD_TEXT_MAX_CHARS} characters")
        encoded = text.encode("utf-16-le")
        units = tuple(
            int.from_bytes(encoded[offset : offset + 2], "little")
            for offset in range(0, len(encoded), 2)
        )
        events: list[INPUT] = []
        for unit in units:
            events.extend(
                (_unicode_event(unit, down=True), _unicode_event(unit, down=False))
            )
        if events:
            try:
                self._send(events)
            except InputBlocked:
                cleanup = [
                    _unicode_event(unit, down=False) for unit in reversed(units)
                ]
                try:
                    self._send(cleanup)
                except InputBlocked:
                    for unit in units:
                        self._remember_possibly_held_unicode(unit)
                raise

    def release_all(self) -> None:
        with self._held_lock:
            held_keys = tuple(
                (vk, pressed_at, self._held_order[("key", vk)])
                for vk, pressed_at in self._held_keys.items()
            )
            held_buttons = tuple(
                (
                    name,
                    self._held_button_times[name],
                    self._held_order[("button", name)],
                )
                for name in self._held_buttons
            )
            held_unicode_units = tuple(self._held_unicode_units)
        releases: list[tuple[float, int, str, int | str, INPUT]] = [
            (pressed_at, order, "key", vk, _keyboard_event(vk, down=False))
            for vk, pressed_at, order in held_keys
        ]
        releases.extend(
            (
                pressed_at,
                order,
                "button",
                name,
                _mouse_event(BUTTON_FLAGS[name][1]),
            )
            for name, pressed_at, order in held_buttons
        )
        releases.extend(
            (
                pressed_at,
                order,
                "unicode",
                unit,
                _unicode_event(unit, down=False),
            )
            for unit, pressed_at, order in held_unicode_units
        )
        releases.sort(key=lambda release: (release[0], release[1]), reverse=True)
        if not releases:
            return
        self._send([release[4] for release in releases])
        with self._held_lock:
            for pressed_at, order, kind, identifier, _event in releases:
                if kind == "key":
                    vk = int(identifier)
                    if (
                        self._held_keys.get(vk) == pressed_at
                        and self._held_order.get(("key", vk)) == order
                    ):
                        self._held_keys.pop(vk, None)
                        self._held_order.pop(("key", vk), None)
                elif kind == "button":
                    name = str(identifier)
                    if (
                        self._held_button_times.get(name) == pressed_at
                        and self._held_order.get(("button", name)) == order
                    ):
                        self._held_buttons.discard(name)
                        self._held_button_times.pop(name, None)
                        self._held_order.pop(("button", name), None)
                else:
                    unit = int(identifier)
                    self._held_unicode_units = [
                        entry
                        for entry in self._held_unicode_units
                        if entry != (unit, pressed_at, order)
                    ]

    def held(self) -> tuple[frozenset[int], frozenset[str]]:
        with self._held_lock:
            return frozenset(self._held_keys), frozenset(self._held_buttons)

    def _remember_possibly_held_key(self, vk: int) -> None:
        with self._held_lock:
            if vk not in self._held_keys:
                self._held_keys[vk] = time.monotonic()
                self._held_sequence += 1
                self._held_order[("key", vk)] = self._held_sequence

    def _remember_possibly_held_unicode(self, unit: int) -> None:
        with self._held_lock:
            self._held_sequence += 1
            self._held_unicode_units.append(
                (unit, time.monotonic(), self._held_sequence)
            )

    def _send(self, events: list[INPUT]) -> None:
        if not events:
            return
        if _user32 is None:
            raise InjectorUnavailable("input injection requires Windows")
        count = len(events)
        event_array = (INPUT * count)(*events)
        ctypes.set_last_error(0)
        sent = _user32.SendInput(count, event_array, ctypes.sizeof(INPUT))
        if sent != count:
            raise InputBlocked(ctypes.get_last_error())

    @staticmethod
    def _validate_vk(vk: int) -> None:
        if not isinstance(vk, int) or isinstance(vk, bool) or not 1 <= vk <= 254:
            raise ValueError("virtual-key code must be an integer from 1 through 254")
