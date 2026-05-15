from __future__ import annotations

import struct
from dataclasses import dataclass

VERSION = 1
HEADER = struct.Struct("!BBBBIH")
HEADER_SIZE = HEADER.size
MAX_U8 = 0xFF
MAX_U16 = 0xFFFF
MAX_U32 = 0xFFFF_FFFF

MSG_HELLO = 0x01
MSG_TEXT = 0x02
MSG_DATA = 0x03
MSG_STATUS = 0x04

MESSAGE_TYPES: dict[str, int] = {
    "hello": MSG_HELLO,
    "text": MSG_TEXT,
    "data": MSG_DATA,
    "status": MSG_STATUS,
}

MESSAGE_TYPE_NAMES = {value: name for name, value in MESSAGE_TYPES.items()}


class AppFrameError(ValueError):
    pass


@dataclass(frozen=True)
class AppFrame:
    version: int
    message_type: int
    sender_id: int
    flags: int
    app_seq: int
    payload: bytes

    @property
    def message_type_name(self) -> str:
        return message_type_name(self.message_type)


def _require_u8(name: str, value: int) -> None:
    if not 0 <= value <= MAX_U8:
        raise AppFrameError(f"{name} must fit in u8: {value}")


def _require_u32(name: str, value: int) -> None:
    if not 0 <= value <= MAX_U32:
        raise AppFrameError(f"{name} must fit in u32: {value}")


def message_type_value(value: str | int) -> int:
    if isinstance(value, int):
        _require_u8("message_type", value)
        return value

    text = value.strip().lower()
    if text in MESSAGE_TYPES:
        return MESSAGE_TYPES[text]

    try:
        parsed = int(text, 0)
    except ValueError as exc:
        valid = ", ".join(sorted(MESSAGE_TYPES))
        raise AppFrameError(f"unknown message type '{value}' (valid: {valid})") from exc

    _require_u8("message_type", parsed)
    return parsed


def message_type_name(value: int) -> str:
    return MESSAGE_TYPE_NAMES.get(value, f"0x{value:02x}")


def encode_frame(
    *,
    sender_id: int,
    message_type: str | int,
    app_seq: int,
    payload: bytes,
    flags: int = 0,
) -> bytes:
    if sender_id == 0:
        raise AppFrameError("sender_id 0 is reserved")
    _require_u8("sender_id", sender_id)
    msg_type = message_type_value(message_type)
    _require_u8("flags", flags)
    _require_u32("app_seq", app_seq)
    if len(payload) > MAX_U16:
        raise AppFrameError(f"payload too large for u16 length: {len(payload)}")

    return HEADER.pack(VERSION, msg_type, sender_id, flags, app_seq, len(payload)) + payload


def decode_frame(frame: bytes, *, allow_unknown_message_type: bool = False) -> AppFrame:
    if len(frame) < HEADER_SIZE:
        raise AppFrameError(f"frame too short: {len(frame)} < {HEADER_SIZE}")

    version, msg_type, sender_id, flags, app_seq, payload_len = HEADER.unpack_from(frame)
    payload = frame[HEADER_SIZE:]

    if version != VERSION:
        raise AppFrameError(f"unsupported version: {version}")
    if sender_id == 0:
        raise AppFrameError("sender_id 0 is reserved")
    if payload_len != len(payload):
        raise AppFrameError(
            f"payload_len mismatch: header={payload_len} actual={len(payload)}"
        )
    if not allow_unknown_message_type and msg_type not in MESSAGE_TYPE_NAMES:
        raise AppFrameError(f"unknown message type: 0x{msg_type:02x}")

    return AppFrame(
        version=version,
        message_type=msg_type,
        sender_id=sender_id,
        flags=flags,
        app_seq=app_seq,
        payload=payload,
    )
