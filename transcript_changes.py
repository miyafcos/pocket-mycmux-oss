"""Read-only evidence that an NTFS file grew without rewriting older data.

A file USN alone is not enough: its Reason field is not valid. Read the journal
interval, including the previous record because NTFS coalesces repeated writes
until close. Unavailable or incomplete evidence always means a fresh scan.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import os
import struct
from typing import Any

# This checks journaled writes, not arbitrary memory-mapped mutations. NTFS
# does not expose unflushed writable mappings in the change journal.


QUERY_JOURNAL = 0x000900F4
READ_FILE_USN = 0x000900EB
READ_UNPRIVILEGED_JOURNAL = 0x000903AB
DATA_EXTEND = 0x00000002
CLOSE = 0x80000000
# Metadata changes can accompany an append; any other reason needs a rescan.
APPEND_REASONS = DATA_EXTEND | CLOSE | 0x00008000
JOURNAL_BLOCK_BYTES = 64 * 1024
JOURNAL_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ChangeToken:
    file_id: int
    usn: int
    journal_id: int


@lru_cache(maxsize=1)
def _api() -> Any:
    from ctypes import wintypes as w
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.DeviceIoControl.argtypes = [
        w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD,
        ctypes.POINTER(w.DWORD), w.LPVOID,
    ]
    dll.DeviceIoControl.restype = w.BOOL
    dll.CreateFileW.argtypes = [
        w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE,
    ]
    dll.CreateFileW.restype = w.HANDLE
    dll.CloseHandle.argtypes = [w.HANDLE]
    dll.CloseHandle.restype = w.BOOL
    return dll


def _control(native: int, code: int, data: bytes = b"", size: int = 4096) -> bytes:
    from ctypes import wintypes as w
    output = ctypes.create_string_buffer(size)
    used = w.DWORD()
    if not _api().DeviceIoControl(
        native, code, data or None, len(data), output, size, ctypes.byref(used), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return output.raw[:used.value]


def _journal(native: int) -> tuple[int, int, int]:
    data = _control(native, QUERY_JOURNAL)
    if len(data) < 56:
        raise ValueError("short change journal information")
    journal_id, first, next_usn, lowest = struct.unpack_from("<Qqqq", data)
    return journal_id, max(first, lowest), next_usn


def _capture(native: int) -> ChangeToken:
    raw = _control(native, READ_FILE_USN, struct.pack("<HH", 2, 2))
    if len(raw) < 60:
        raise ValueError("short file change record")
    length, major, minor, file_id, _, usn = struct.unpack_from("<IHHQQq", raw)
    if length > len(raw) or length < 60 or major != 2 or minor != 0 or not file_id:
        raise ValueError("unsupported file change record")
    journal_id, first, next_usn = _journal(native)
    if not first <= usn < next_usn or usn == 0:
        raise ValueError("file change record is no longer available")
    return ChangeToken(file_id, usn, journal_id)


def capture(handle: Any) -> ChangeToken | None:
    if os.name != "nt":
        return None
    try:
        import msvcrt
        return _capture(msvcrt.get_osfhandle(handle.fileno()))
    except (OSError, ValueError, struct.error):
        return None


def at_path(path: Any) -> ChangeToken | None:
    """Query metadata without reading transcript bytes or retaining a handle."""
    if os.name != "nt":
        return None
    try:
        # No write access or elevation. Open the file, not the raw volume.
        native = _api().CreateFileW(os.fspath(path), 0, 7, None, 3, 0x02000000, None)
        if native == ctypes.c_void_p(-1).value:
            return None
        try:
            return _capture(native)
        finally:
            _api().CloseHandle(native)
    except (OSError, ValueError, struct.error):
        return None


def _append_only(native: int, before: ChangeToken, after: ChangeToken) -> bool:
    if before.file_id != after.file_id or before.journal_id != after.journal_id:
        return False
    journal_id, first, next_usn = _journal(native)
    if journal_id != before.journal_id or not first <= before.usn <= after.usn < next_usn:
        return False
    start = before.usn
    seen_before = False
    seen_after = False
    saw_extend = False
    used = 0
    while start <= after.usn and used < JOURNAL_MAX_BYTES:
        request = struct.pack("<qIIQQQ", start, 0xFFFFFFFF, 0, 0, 0, journal_id)
        data = _control(native, READ_UNPRIVILEGED_JOURNAL, request, JOURNAL_BLOCK_BYTES)
        used += len(data)
        if len(data) < 8:
            return False
        following = struct.unpack_from("<q", data)[0]
        if following <= start:
            return False
        position = 8
        previous_usn = start - 1
        while position < len(data):
            if position + 60 > len(data):
                return False
            length, major, minor, file_id, _, usn = struct.unpack_from("<IHHQQq", data, position)
            if (length < 60 or position + length > len(data)
                    or major != 2 or minor != 0 or usn <= previous_usn):
                return False
            previous_usn = usn
            reason = struct.unpack_from("<I", data, position + 40)[0]
            if file_id == before.file_id and before.usn <= usn <= after.usn:
                seen_before |= usn == before.usn
                seen_after |= usn == after.usn
                # A closed record predating the snapshot cannot hide a later
                # overwrite. An open record can: repeated reasons are coalesced.
                baseline_closed = usn == before.usn and reason & CLOSE
                if baseline_closed:
                    if before.usn == after.usn:
                        return False
                else:
                    if not reason or reason & ~APPEND_REASONS:
                        return False
                    saw_extend |= bool(reason & DATA_EXTEND)
            position += length
        if following > after.usn:
            # A journal reset or trimmed interval must not certify old bytes.
            final_id, final_first, _ = _journal(native)
            return (seen_before and seen_after and saw_extend
                    and final_id == journal_id and final_first <= before.usn)
        start = following
    return False


def append_only(handle: Any, before: ChangeToken | None, after: ChangeToken | None) -> bool:
    if os.name != "nt" or before is None or after is None:
        return False
    try:
        import msvcrt
        return _append_only(msvcrt.get_osfhandle(handle.fileno()), before, after)
    except (OSError, ValueError, struct.error):
        return False
