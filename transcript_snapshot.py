"""Bounded, immutable byte snapshots for transcript cache validation.

Metadata and the NTFS journal cannot see every writable-mapping mutation.
Read and compare the actual bytes on every observation, including unchanged
size/mtime. Equal blocks share their old immutable storage. Parsing can then
reuse a turn only when its entire old prefix is byte-for-byte identical.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any


BLOCK_BYTES = 256 * 1024
MAX_CACHE_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class Snapshot:
    size: int
    blocks: tuple[bytes, ...]

    @classmethod
    def read(cls, handle: Any, size: int, previous: Snapshot | None = None) -> Snapshot:
        handle.seek(0)
        blocks = []
        remaining = size
        while remaining:
            expected = min(BLOCK_BYTES, remaining)
            block = handle.read(expected)
            if len(block) != expected:
                raise OSError("transcript shrank while taking a snapshot")
            index = len(blocks)
            if previous is not None and index < len(previous.blocks) and block == previous.blocks[index]:
                block = previous.blocks[index]
            blocks.append(block)
            remaining -= expected
        return cls(size, tuple(blocks))

    def matches(self, handle: Any) -> bool:
        handle.seek(0)
        return all(handle.read(len(block)) == block for block in self.blocks)

    def extends(self, previous: Snapshot) -> bool:
        if self.size <= previous.size:
            return False
        return all(self.blocks[index][:len(block)] == block
                   for index, block in enumerate(previous.blocks))

    def reader(self) -> Reader:
        return Reader(self)


class Reader:
    """The seek/read subset used by the existing backward JSONL scanner."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.position = 0

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_CUR:
            offset += self.position
        elif whence == os.SEEK_END:
            offset += self.snapshot.size
        elif whence != os.SEEK_SET:
            raise ValueError("invalid seek origin")
        if offset < 0:
            raise ValueError("negative seek")
        self.position = offset
        return offset

    def read(self, size: int = -1) -> bytes:
        end = self.snapshot.size if size < 0 else min(self.snapshot.size, self.position + size)
        if self.position >= end:
            return b""
        parts = []
        while self.position < end:
            index, offset = divmod(self.position, BLOCK_BYTES)
            count = min(end - self.position, len(self.snapshot.blocks[index]) - offset)
            parts.append(self.snapshot.blocks[index][offset:offset + count])
            self.position += count
        return parts[0] if len(parts) == 1 else b"".join(parts)
