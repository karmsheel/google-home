"""Cast v2 wire protocol: protobuf framing for the CastMessage envelope.

The Google Cast control channel is TLS on TCP/8009. Every frame is a 4-byte
big-endian length followed by a serialized ``CastMessage``.

``CastMessage`` (cast_channel.proto) fields we care about:

    1  protocol_version  enum CASTV2_1_0 = 0   (varint)
    2  source_id         string               (length-delimited)
    3  destination_id    string               (length-delimited)
    4  namespace         string               (length-delimited)
    5  payload_type      enum STRING = 0      (varint)
    6  payload_utf8      string               (length-delimited)

Hermes's virtualenv has neither ``protobuf`` nor ``pychromecast`` as a
dependency, so this module hand-rolls the handful of protobuf primitives the
envelope needs. That keeps the plugin installable with zero third-party
packages.
"""
from __future__ import annotations

import json
import struct
from typing import Any

# ── namespaces ────────────────────────────────────────────────────────────────
NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"

RECEIVER_ID = "receiver-0"
DEFAULT_MEDIA_RECEIVER = "CC1AD845"

# message types
MSG_CONNECT = "CONNECT"
MSG_CLOSE = "CLOSE"
MSG_PING = "PING"
MSG_PONG = "PONG"

# protobuf wire types
_WT_VARINT = 0
_WT_64BIT = 1
_WT_LEN = 2
_WT_32BIT = 5


class CastProtocolError(Exception):
    """Raised when a frame cannot be encoded or decoded."""


# ── protobuf primitives ───────────────────────────────────────────────────────
def encode_varint(value: int) -> bytes:
    if value < 0:
        raise CastProtocolError(f"varint must be non-negative, got {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(buf: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(buf):
            raise CastProtocolError("truncated varint")
        byte = buf[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise CastProtocolError("varint too long")


def _field_bytes(number: int, payload: bytes) -> bytes:
    return encode_varint((number << 3) | _WT_LEN) + encode_varint(len(payload)) + payload


def _field_varint(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | _WT_VARINT) + encode_varint(value)


# ── envelope ──────────────────────────────────────────────────────────────────
def encode_message(
    source_id: str,
    destination_id: str,
    namespace: str,
    payload: dict[str, Any] | str,
    *,
    protocol_version: int = 0,
    payload_type: int = 0,
) -> bytes:
    """Serialize one ``CastMessage`` (without the 4-byte length prefix)."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, separators=(",", ":"))
    parts = [
        _field_varint(1, protocol_version),
        _field_bytes(2, source_id.encode("utf-8")),
        _field_bytes(3, destination_id.encode("utf-8")),
        _field_bytes(4, namespace.encode("utf-8")),
        _field_varint(5, payload_type),
        _field_bytes(6, text.encode("utf-8")),
    ]
    return b"".join(parts)


def frame(message: bytes) -> bytes:
    """Prefix a serialized message with its 4-byte big-endian length."""
    return struct.pack(">I", len(message)) + message


def decode_message(buf: bytes) -> dict[str, Any]:
    """Parse a ``CastMessage`` into a plain dict (payload decoded as JSON)."""
    fields: dict[str, Any] = {
        "protocol_version": 0,
        "source_id": "",
        "destination_id": "",
        "namespace": "",
        "payload_type": 0,
    }
    offset = 0
    while offset < len(buf):
        key, offset = _read_varint(buf, offset)
        number, wire = key >> 3, key & 0x07
        if wire == _WT_VARINT:
            value, offset = _read_varint(buf, offset)
        elif wire == _WT_LEN:
            length, offset = _read_varint(buf, offset)
            if offset + length > len(buf):
                raise CastProtocolError("truncated length-delimited field")
            value = buf[offset:offset + length]
            offset += length
        elif wire == _WT_64BIT:
            value = buf[offset:offset + 8]
            offset += 8
        elif wire == _WT_32BIT:
            value = buf[offset:offset + 4]
            offset += 4
        else:
            raise CastProtocolError(f"unsupported wire type {wire}")

        if number == 1:
            fields["protocol_version"] = value
        elif number == 2:
            fields["source_id"] = value.decode("utf-8", "replace")
        elif number == 3:
            fields["destination_id"] = value.decode("utf-8", "replace")
        elif number == 4:
            fields["namespace"] = value.decode("utf-8", "replace")
        elif number == 5:
            fields["payload_type"] = value
        elif number == 6:
            fields["payload_utf8"] = value.decode("utf-8", "replace")

    raw = fields.pop("payload_utf8", "")
    if raw:
        try:
            fields["payload"] = json.loads(raw)
        except json.JSONDecodeError:
            fields["payload"] = {"_raw": raw}
    else:
        fields["payload"] = {}
    return fields


def read_frame(reader) -> dict[str, Any]:
    """Read one length-prefixed message from a binary file-like object."""
    size_bytes = reader.read(4)
    if len(size_bytes) < 4:
        raise CastProtocolError("connection closed before a frame arrived")
    (size,) = struct.unpack(">I", size_bytes)
    if size == 0 or size > 1 << 20:
        raise CastProtocolError(f"implausible frame length {size}")
    body = reader.read(size)
    if len(body) < size:
        raise CastProtocolError("truncated frame body")
    return decode_message(body)
