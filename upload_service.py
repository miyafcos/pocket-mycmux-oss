"""Validate and store image uploads without depending on the HTTP server."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from pathlib import Path
import secrets
import os
import tempfile
from typing import Any, BinaryIO


MAX_UPLOAD_BYTES: int = 4 * 1024 * 1024
MAX_VIDEO_BYTES: int = 200 * 1024 * 1024
VIDEO_CHUNK_BYTES: int = 1024 * 1024
VIDEO_CONTENT_TYPES = {"video/mp4": "mp4", "video/quicktime": "mov"}
ALLOWED_IMAGE_KINDS: tuple[str, ...] = ("jpeg", "png", "gif", "webp")

_EXTENSIONS = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "mp4": ".mp4",
    "mov": ".mov",
}
_MAX_SAVE_ATTEMPTS = 5


class UploadError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def detect_image_kind(payload: bytes) -> str | None:
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"GIF8"):
        return "gif"
    if (
        payload.startswith(b"RIFF")
        and len(payload) >= 12
        and payload[8:12] == b"WEBP"
    ):
        return "webp"
    return None


def decode_upload(payload: dict[str, Any]) -> tuple[bytes, str]:
    encoded = payload.get("dataBase64")
    if not isinstance(encoded, str) or not encoded:
        raise UploadError(400, "missingData")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise UploadError(400, "invalidBase64") from None
    if not data:
        raise UploadError(400, "missingData")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(413, "tooLarge")
    kind = detect_image_kind(data)
    if kind is None:
        raise UploadError(400, "unsupportedType")
    return data, kind


def save_upload(data: bytes, kind: str, *, root: Path, now: datetime) -> Path:
    extension = _EXTENSIONS.get(kind)
    if extension is None:
        raise UploadError(400, "unsupportedType")

    date_text = now.strftime("%Y%m%d")
    time_text = now.strftime("%Y%m%dT%H%M%SZ")
    directory = root / date_text
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UploadError(500, "saveFailed") from exc

    last_collision: FileExistsError | None = None
    for _ in range(_MAX_SAVE_ATTEMPTS):
        candidate = directory / f"{time_text}-{secrets.token_hex(4)}{extension}"
        try:
            with candidate.open("xb") as handle:
                handle.write(data)
            return candidate.resolve()
        except FileExistsError as exc:
            last_collision = exc
        except OSError as exc:
            raise UploadError(500, "saveFailed") from exc

    raise UploadError(500, "saveFailed") from last_collision


def handle_upload(
    payload: dict[str, Any], *, root: Path, now: datetime
) -> dict[str, Any]:
    data, kind = decode_upload(payload)
    path = save_upload(data, kind, root=root, now=now)
    return {"path": str(path), "bytes": len(data), "kind": kind}


def detect_video_kind(prefix: bytes) -> str | None:
    """Identify the file container from its first twelve bytes, not its name."""
    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
        return None
    brand = prefix[8:12]
    if brand in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"hvc1", b"mp4v", b"M4V "}:
        return "mp4"
    if brand == b"qt  ":
        return "mov"
    return None


def handle_video_upload(
    stream: BinaryIO, length: int, *, content_type: str, root: Path, now: datetime
) -> dict[str, Any]:
    """Keep at most one chunk in memory and publish only a complete video."""
    if length > MAX_VIDEO_BYTES:
        raise UploadError(413, "tooLarge")
    if length < 12:
        raise UploadError(400, "unsupportedType")
    if content_type not in VIDEO_CONTENT_TYPES:
        raise UploadError(415, "videoRequired")

    directory = root / now.strftime("%Y%m%d")
    temporary: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".upload-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            prefix = stream.read(12)
            kind = detect_video_kind(prefix)
            if kind is None or VIDEO_CONTENT_TYPES[content_type] != kind:
                raise UploadError(400, "unsupportedType")
            handle.write(prefix)
            remaining = length - len(prefix)
            while remaining:
                chunk = stream.read(min(VIDEO_CHUNK_BYTES, remaining))
                if not chunk:
                    raise UploadError(400, "incompleteBody")
                handle.write(chunk)
                remaining -= len(chunk)
        time_text = now.strftime("%Y%m%dT%H%M%SZ")
        for _ in range(_MAX_SAVE_ATTEMPTS):
            destination = directory / f"{time_text}-{secrets.token_hex(4)}{_EXTENSIONS[kind]}"
            try:
                # Same-volume hard linking publishes atomically without replacing
                # an existing upload if a random name ever collides.
                os.link(temporary, destination)
                return {"path": str(destination.resolve()), "bytes": length, "kind": kind}
            except FileExistsError:
                continue
        raise UploadError(500, "saveFailed")
    except (TimeoutError, ConnectionError) as exc:
        raise UploadError(400, "incompleteBody") from exc
    except OSError as exc:
        raise UploadError(500, "saveFailed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
