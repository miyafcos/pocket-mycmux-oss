"""Small, privacy-preserving PC observations, independent of the mycmux UI."""
from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Callable

GIB = float(1 << 30)
CPU_INTERVAL_SECONDS = 5.0
CPU_IDLE_SECONDS = 120.0
CPU_WARM_SECONDS = 0.1
CORE_TTL_SECONDS = 5.0
CORE_TIMEOUT_SECONDS = 2.0
MEMORY_RULE = {"warnAvailGiB": 4, "dangerAvailGiB": 3,
               "warnCommitPct": 85, "dangerCommitPct": 95}
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}\Z")


def iso_at(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class MEMORYSTATUSEX(ctypes.Structure):
    # Fixed Windows widths also make synthetic fixtures correct on macOS.
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32)] + [
        (name, ctypes.c_uint64) for name in (
            "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
            "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


class FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def ticks(self) -> int:
        return (self.high << 32) | self.low


def memory_from_status(status: Any) -> dict[str, Any]:
    """Exactly mem_watch's GlobalMemoryStatusEx API, units, and rounding."""
    avail = status.ullAvailPhys / GIB
    limit = status.ullTotalPageFile / GIB
    used = (status.ullTotalPageFile - status.ullAvailPageFile) / GIB
    pct = used / limit * 100 if limit else 0.0
    level = "ok"
    if avail < MEMORY_RULE["warnAvailGiB"] or pct >= MEMORY_RULE["warnCommitPct"]:
        level = "warn"
    if avail < MEMORY_RULE["dangerAvailGiB"] or pct >= MEMORY_RULE["dangerCommitPct"]:
        level = "danger"
    return {"availGiB": round(avail, 2), "totalGiB": round(status.ullTotalPhys / GIB, 2),
            "loadPct": int(status.dwMemoryLoad), "commitUsedGiB": round(used, 2),
            "commitLimitGiB": round(limit, 2), "commitPct": round(pct, 1),
            "level": level, "rule": dict(MEMORY_RULE)}


class WindowsMetrics:
    def __init__(self) -> None:
        self.available = os.name == "nt"
        if not self.available:
            return
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        self.kernel.GlobalMemoryStatusEx.restype = ctypes.c_int32
        self.kernel.GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME)] * 3
        self.kernel.GetSystemTimes.restype = ctypes.c_int32
        self.kernel.GetTickCount64.argtypes = []
        self.kernel.GetTickCount64.restype = ctypes.c_uint64
        self.kernel.GetDiskFreeSpaceExW.argtypes = [ctypes.c_wchar_p] + [ctypes.POINTER(ctypes.c_uint64)] * 3
        self.kernel.GetDiskFreeSpaceExW.restype = ctypes.c_int32

    def memory(self) -> dict[str, Any]:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not self.kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError(ctypes.get_last_error())
        return memory_from_status(status)

    def times(self) -> tuple[int, int, int]:
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not self.kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError(ctypes.get_last_error())
        return idle.ticks(), kernel.ticks(), user.ticks()

    def uptime(self) -> int:
        return int(self.kernel.GetTickCount64() // 1000)

    def disk(self) -> dict[str, float]:
        available, total, free = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
        if not self.kernel.GetDiskFreeSpaceExW("C:\\", ctypes.byref(available), ctypes.byref(total), ctypes.byref(free)):
            raise OSError(ctypes.get_last_error())
        return {"freeGiB": round(free.value / GIB, 2)}


def cpu_percent(previous: tuple[int, int, int], current: tuple[int, int, int]) -> int | None:
    idle, kernel, user = (new - old for old, new in zip(previous, current))
    total = kernel + user  # GetSystemTimes kernel time includes idle time.
    if min(idle, kernel, user) < 0 or total <= 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) / total * 100)))


class CpuSampler:
    """A five-second delta while requested, with one short warm-up on restart."""
    def __init__(self, reader: Callable[[], tuple[int, int, int]], *,
                 monotonic: Callable[[], float] = time.monotonic,
                 pause: Callable[[float], Any] = time.sleep,
                 interval: float = CPU_INTERVAL_SECONDS, start_worker: bool = True) -> None:
        self.reader, self.monotonic, self.pause = reader, monotonic, pause
        self.interval, self.start_worker = interval, start_worker
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._previous: tuple[int, int, int] | None = None
        self._sampled_at = 0.0
        self._last_request = 0.0
        self._value: dict[str, Any] | None = None
        self.active = False
        self.thread: threading.Thread | None = None

    def _sample(self) -> None:
        current, at = self.reader(), self.monotonic()
        if self._previous is not None:
            pct = cpu_percent(self._previous, current)
            self._value = None if pct is None else {"pct": pct, "windowSec": round(max(0, at - self._sampled_at), 2)}
        self._previous, self._sampled_at = current, at

    def value(self) -> dict[str, Any] | None:
        with self._lock:
            if self.monotonic() - self._last_request >= CPU_IDLE_SECONDS:
                self.active = False
                self._previous = None
            self._last_request = self.monotonic()
            if not self.active:
                self._value = None
                self._sample()
                self.pause(CPU_WARM_SECONDS)
                self._sample()
                self.active = True
                if self.start_worker and self.thread is None and not self._stop.is_set():
                    self.thread = threading.Thread(target=self._run, name="pocket-pc-cpu", daemon=True)
                    self.thread.start()
            return dict(self._value) if self._value is not None else None

    def _poll(self) -> bool:
        if self.monotonic() - self._last_request >= CPU_IDLE_SECONDS or self._stop.is_set():
            self.active = False
            self._previous = None
            return False
        try:
            self._sample()
        except Exception:
            self._value = None
            self._previous = None
        return True

    def poll_once(self) -> bool:
        with self._lock:
            return self._poll()

    def _run(self) -> None:
        while True:
            with self._lock:
                remaining = CPU_IDLE_SECONDS - (self.monotonic() - self._last_request)
            self._stop.wait(max(0, min(self.interval, remaining)))
            with self._lock:
                if not self._poll():
                    self.thread = None
                    return

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self.thread
        if thread is not None:
            thread.join(timeout=1)


class VersionProbe:
    """Share one bounded Rust-only request and cache its result for five seconds."""
    def __init__(self, operation: Callable[[], Any], *,
                 monotonic: Callable[[], float] = time.monotonic,
                 timeout: float = CORE_TIMEOUT_SECONDS) -> None:
        self.operation, self.monotonic, self.timeout = operation, monotonic, timeout
        self._lock = threading.Lock()
        self._future: Future | None = None
        self._checked: float | None = None
        self._core, self._version = False, None

    def _start(self) -> Future:
        future: Future = Future()
        def run() -> None:
            try:
                future.set_result(self.operation())
            except Exception:
                # An exception can contain peer text, paths, or credentials.
                future.set_result(None)
        threading.Thread(target=run, name="pocket-pc-core", daemon=True).start()
        return future

    def value(self) -> dict[str, Any]:
        with self._lock:
            if self._checked is None or self.monotonic() - self._checked >= CORE_TTL_SECONDS:
                if self._future is None or self._future.done():
                    self._future = self._start()
                try:
                    result = self._future.result(timeout=self.timeout)
                except FutureTimeout:
                    result = None
                self._core = isinstance(result, dict)
                version = result.get("app_version") if self._core else None
                self._version = version if isinstance(version, str) and VERSION.fullmatch(version) else None
                self._checked = self.monotonic()
            return {"core": self._core, "version": self._version,
                    "checkedAgeSec": max(0, int(self.monotonic() - self._checked))}


def seats_from_overview(overview: Any) -> dict[str, Any] | None:
    if not isinstance(overview, dict) or not isinstance(overview.get("workspaces"), list):
        return None
    result: dict[str, Any] = {"agents": 0, "working": 0, "attention": 0, "stopped": 0,
                              "unobserved": 0, "idle": 0, "tabsTotal": 0,
                              "byAgent": {"claude": 0, "codex": 0, "other": 0}, "source": "overview"}
    for workspace in overview["workspaces"]:
        if not isinstance(workspace, dict):
            continue
        for pane in workspace.get("panes", []):
            if not isinstance(pane, dict):
                continue
            rows = pane.get("tabs")
            for row in rows if isinstance(rows, list) else [pane]:
                if not isinstance(row, dict):
                    continue
                result["tabsTotal"] += 1
                agent = row.get("agent", "none")
                if agent == "none":
                    continue
                result["agents"] += 1
                result["byAgent"][agent if agent in ("claude", "codex") else "other"] += 1
                status = row.get("status")
                result[status if status in ("working", "attention", "stopped", "idle") else "unobserved"] += 1
    total = overview.get("bridgeCounts", {}).get("t")
    if isinstance(total, int) and not isinstance(total, bool) and total >= result["tabsTotal"]:
        result["tabsTotal"] = total
    return result


class PCStatus:
    def __init__(self, *, metrics: Any = None, cpu: Any = None,
                 version_reader: Callable[[], Any] = lambda: None,
                 ui_reader: Callable[[], Any] = lambda: None,
                 feed_reader: Callable[[], Any] = lambda: False,
                 overview_reader: Callable[[], Any] = lambda: None,
                 build_reader: Callable[[], str] = lambda: "unknown",
                 started_at: str | None = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.metrics = metrics if metrics is not None else WindowsMetrics()
        self.cpu = cpu if cpu is not None else (CpuSampler(self.metrics.times, monotonic=monotonic)
                                               if self.metrics.available else None)
        self.version = VersionProbe(version_reader, monotonic=monotonic)
        self.ui_reader, self.feed_reader, self.overview_reader = ui_reader, feed_reader, overview_reader
        self.build_reader, self.clock = build_reader, clock
        self.started_at = started_at or iso_at(clock())

    @staticmethod
    def _read(operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except Exception:
            return None

    def snapshot(self) -> dict[str, Any]:
        # These independent reads remain available when the socket or UI fails.
        native = self.metrics.available
        memory = self._read(self.metrics.memory) if native else None
        cpu = self._read(self.cpu.value) if self.cpu is not None else None
        uptime = self._read(self.metrics.uptime) if native else None
        disk = self._read(self.metrics.disk) if native else None
        mycmux = self.version.value()
        ui = self._read(self.ui_reader)
        mycmux.update(ui=ui if isinstance(ui, bool) else None,
                      feed=self._read(self.feed_reader) is True)
        seats = seats_from_overview(self._read(self.overview_reader))
        return {"generatedAt": iso_at(self.clock()), "memory": memory, "cpu": cpu,
                "uptimeSec": uptime, "disk": disk, "mycmux": mycmux,
                "pocketServer": {"build": self.build_reader(), "startedAt": self.started_at},
                "seats": seats, "seatsReason": "overviewNotReady" if seats is None else None}

    def stop(self) -> None:
        if self.cpu is not None:
            self.cpu.stop()
