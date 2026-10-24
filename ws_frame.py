from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any, BinaryIO


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_PAYLOAD = 8192

_CONTINUATION = 0x0
_DATA_OPCODES = frozenset((0x1, 0x2))
_CONTROL_OPCODES = frozenset((0x8, 0x9, 0xA))
_VALID_OPCODES = frozenset((_CONTINUATION, *_DATA_OPCODES, *_CONTROL_OPCODES))


class WsProtocolError(Exception):
    def __init__(self, close_code: int, message: str = "WebSocket protocol error") -> None:
        self.close_code = close_code
        super().__init__(message)


def accept_key(sec_websocket_key: str) -> str:
    if not isinstance(sec_websocket_key, str):
        raise TypeError("Sec-WebSocket-Key must be a string")
    digest = hashlib.sha1((sec_websocket_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode: int, payload: bytes) -> bytes:
    if opcode not in _VALID_OPCODES:
        raise ValueError("unsupported server frame opcode")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    length = len(payload)
    if opcode in _CONTROL_OPCODES and length > 125:
        raise ValueError("control frame payload exceeds 125 bytes")
    first = 0x80 | opcode
    if length <= 125:
        header = bytes((first, length))
    elif length <= 0xFFFF:
        header = bytes((first, 126)) + struct.pack("!H", length)
    elif length <= 0x7FFF_FFFF_FFFF_FFFF:
        header = bytes((first, 127)) + struct.pack("!Q", length)
    else:
        raise ValueError("payload is too large for RFC6455")
    return header + payload


def parse_subprotocols(header_value: str) -> list[str]:
    if not isinstance(header_value, str):
        return []
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _header(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        value = None
    if value is not None:
        return str(value)
    try:
        items = headers.items()
    except (AttributeError, TypeError):
        return None
    target = name.casefold()
    for key, item_value in items:
        if str(key).casefold() == target:
            return str(item_value)
    return None


def is_websocket_upgrade(headers: Any) -> bool:
    upgrade = _header(headers, "Upgrade")
    connection = _header(headers, "Connection")
    version = _header(headers, "Sec-WebSocket-Version")
    key = _header(headers, "Sec-WebSocket-Key")
    if upgrade is None or upgrade.strip().casefold() != "websocket":
        return False
    connection_tokens = {
        token.strip().casefold() for token in connection.split(",")
    } if connection is not None else set()
    return "upgrade" in connection_tokens and version is not None and (
        version.strip() == "13"
    ) and key is not None and bool(key.strip())


class FrameReader:
    def __init__(self, rfile: BinaryIO, max_payload: int = WS_MAX_PAYLOAD) -> None:
        if not isinstance(max_payload, int) or isinstance(max_payload, bool) or max_payload < 0:
            raise ValueError("max_payload must be a non-negative integer")
        self.rfile = rfile
        self.max_payload = max_payload
        self._fragment_opcode: int | None = None
        self._fragment_parts: list[bytes] = []
        self._fragment_size = 0

    def _read_exact(self, length: int) -> bytes:
        if length == 0:
            return b""
        result = bytearray()
        while len(result) < length:
            chunk = self.rfile.read(length - len(result))
            if not chunk:
                raise ConnectionError("unexpected EOF while reading WebSocket frame")
            result.extend(chunk)
        return bytes(result)

    def _read_wire_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WsProtocolError(1002, "reserved bits are not supported")
        opcode = first & 0x0F
        if opcode not in _VALID_OPCODES:
            raise WsProtocolError(1002, "reserved opcode")
        if not second & 0x80:
            raise WsProtocolError(1002, "client frames must be masked")

        length_marker = second & 0x7F
        if opcode in _CONTROL_OPCODES and (not fin or length_marker > 125):
            raise WsProtocolError(1002, "invalid control frame")
        if length_marker == 126:
            payload_length = struct.unpack("!H", self._read_exact(2))[0]
            if payload_length < 126:
                raise WsProtocolError(1002, "non-canonical payload length")
        elif length_marker == 127:
            encoded_length = self._read_exact(8)
            if encoded_length[0] & 0x80:
                raise WsProtocolError(1002, "invalid 64-bit payload length")
            payload_length = struct.unpack("!Q", encoded_length)[0]
            if payload_length <= 0xFFFF:
                raise WsProtocolError(1002, "non-canonical payload length")
        else:
            payload_length = length_marker

        if payload_length > self.max_payload:
            raise WsProtocolError(1009, "message exceeds configured payload limit")
        mask = self._read_exact(4)
        encoded = self._read_exact(payload_length)
        payload = bytes(value ^ mask[index & 3] for index, value in enumerate(encoded))
        if opcode == 0x8:
            if len(payload) == 1:
                raise WsProtocolError(1002, "close frame has an invalid payload length")
            if len(payload) >= 2:
                close_code = struct.unpack("!H", payload[:2])[0]
                if (
                    close_code < 1000
                    or close_code >= 5000
                    or close_code in (1004, 1005, 1006, 1015)
                ):
                    raise WsProtocolError(1002, "close frame has an invalid status code")
                try:
                    payload[2:].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WsProtocolError(1007, "close reason is not valid UTF-8") from exc
        return fin, opcode, payload

    def read_message(self) -> tuple[int, bytes]:
        while True:
            fin, opcode, payload = self._read_wire_frame()
            if opcode in _CONTROL_OPCODES:
                return opcode, payload

            if opcode == _CONTINUATION:
                if self._fragment_opcode is None:
                    raise WsProtocolError(1002, "continuation without fragmented message")
                new_size = self._fragment_size + len(payload)
                if new_size > self.max_payload:
                    raise WsProtocolError(1009, "fragmented message exceeds payload limit")
                self._fragment_parts.append(payload)
                self._fragment_size = new_size
                if not fin:
                    continue
                message_opcode = self._fragment_opcode
                message = b"".join(self._fragment_parts)
                self._fragment_opcode = None
                self._fragment_parts = []
                self._fragment_size = 0
                return message_opcode, message

            if self._fragment_opcode is not None:
                raise WsProtocolError(1002, "new data frame during fragmented message")
            if fin:
                return opcode, payload
            self._fragment_opcode = opcode
            self._fragment_parts = [payload]
            self._fragment_size = len(payload)

    def read_frame(self) -> tuple[int, bytes]:
        """Compatibility alias for the Phase 2 channel contract."""
        return self.read_message()
