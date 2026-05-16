from __future__ import annotations

import os
import struct
from dataclasses import dataclass

VERSION = 1
HEADER = struct.Struct("!BBBBIH")
HEADER_SIZE = HEADER.size
MAX_U8 = 0xFF
MAX_U16 = 0xFFFF
MAX_U32 = 0xFFFF_FFFF
MAX_U64 = 0xFFFF_FFFF_FFFF_FFFF
CHACHA20_POLY1305_KEY_SIZE = 32
CHACHA20_POLY1305_NONCE_SIZE = 12
CHACHA20_POLY1305_TAG_SIZE = 16

MSG_HELLO = 0x01
MSG_TEXT = 0x02
MSG_DATA = 0x03
MSG_STATUS = 0x04
MSG_SYNC = 0x05
MSG_ROUTE_DATA = 0x21

MESSAGE_TYPES: dict[str, int] = {
    "hello": MSG_HELLO,
    "text": MSG_TEXT,
    "data": MSG_DATA,
    "status": MSG_STATUS,
    "sync": MSG_SYNC,
    "route_data": MSG_ROUTE_DATA,
}

MESSAGE_TYPE_NAMES = {value: name for name, value in MESSAGE_TYPES.items()}
ROUTE_DATA_PAYLOAD = struct.Struct("!BBBIBH")
ROUTE_DATA_PAYLOAD_SIZE = ROUTE_DATA_PAYLOAD.size
SYNC_PAYLOAD = struct.Struct("!QIHI")
SYNC_PAYLOAD_SIZE = SYNC_PAYLOAD.size
STATUS_VERSION = 1
STATUS_PAYLOAD = struct.Struct("!BIBBB")
STATUS_PAYLOAD_SIZE = STATUS_PAYLOAD.size
STATUS_BATTERY_UNKNOWN = 0xFF
STATUS_FLAG_DEGRADED_LINK = 0x01
STATUS_FLAG_LOW_BATTERY = 0x02
STATUS_FLAG_CRYPTO_ENABLED = 0x04
STATUS_FLAG_FORWARDING_ENABLED = 0x08
ROUTE_SECURE_ASSOCIATED_DATA = struct.Struct("!BBBIBH")
ROUTE_SECURE_ASSOCIATED_DATA_SIZE = ROUTE_SECURE_ASSOCIATED_DATA.size
SECURE_VERSION = 1
SECURE_PAYLOAD = struct.Struct("!BBHI12s")
SECURE_PAYLOAD_SIZE = SECURE_PAYLOAD.size

SEC_DOMAIN_MESH_GROUP = 0x01
SEC_DOMAIN_NODE_TO_NODE_PAIRWISE = 0x02
SEC_DOMAIN_NODE_TO_C2 = 0x03
SEC_DOMAIN_C2_TO_NODE = 0x04
SEC_DOMAIN_C2_BROADCAST = 0x05
SEC_DOMAIN_REKEY_CONTROL = 0x06

SECURITY_DOMAINS: dict[str, int] = {
    "mesh_group": SEC_DOMAIN_MESH_GROUP,
    "node_to_node_pairwise": SEC_DOMAIN_NODE_TO_NODE_PAIRWISE,
    "node_to_c2": SEC_DOMAIN_NODE_TO_C2,
    "c2_to_node": SEC_DOMAIN_C2_TO_NODE,
    "c2_broadcast": SEC_DOMAIN_C2_BROADCAST,
    "rekey_control": SEC_DOMAIN_REKEY_CONTROL,
}

SECURITY_DOMAIN_NAMES = {value: name for name, value in SECURITY_DOMAINS.items()}


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


@dataclass(frozen=True)
class RouteData:
    origin_sender_id: int
    destination_id: int
    ttl: int
    origin_seq: int
    inner_type: int
    inner_payload: bytes

    @property
    def inner_type_name(self) -> str:
        return message_type_name(self.inner_type)

    @property
    def dedupe_key(self) -> tuple[int, int]:
        return (self.origin_sender_id, self.origin_seq)

    def decremented_ttl(self) -> "RouteData":
        if self.ttl <= 0:
            raise AppFrameError("cannot decrement route_data ttl below zero")
        return RouteData(
            origin_sender_id=self.origin_sender_id,
            destination_id=self.destination_id,
            ttl=self.ttl - 1,
            origin_seq=self.origin_seq,
            inner_type=self.inner_type,
            inner_payload=self.inner_payload,
        )


@dataclass(frozen=True)
class SyncStatus:
    utc_ms: int
    slot: int
    channel: int
    next_hop_ms: int


@dataclass(frozen=True)
class StatusPayload:
    status_version: int
    uptime_s: int
    battery_pct: int | None
    peer_count: int
    flags: int

    @property
    def degraded_link(self) -> bool:
        return bool(self.flags & STATUS_FLAG_DEGRADED_LINK)

    @property
    def low_battery(self) -> bool:
        return bool(self.flags & STATUS_FLAG_LOW_BATTERY)

    @property
    def crypto_enabled(self) -> bool:
        return bool(self.flags & STATUS_FLAG_CRYPTO_ENABLED)

    @property
    def forwarding_enabled(self) -> bool:
        return bool(self.flags & STATUS_FLAG_FORWARDING_ENABLED)


@dataclass(frozen=True)
class SecurePayload:
    secure_version: int
    security_domain: int
    key_id: int
    key_epoch: int
    nonce: bytes
    ciphertext_and_tag: bytes

    @property
    def security_domain_name(self) -> str:
        return security_domain_name(self.security_domain)

    @property
    def plaintext_len(self) -> int:
        return len(self.ciphertext_and_tag) - CHACHA20_POLY1305_TAG_SIZE


def _require_u8(name: str, value: int) -> None:
    if not 0 <= value <= MAX_U8:
        raise AppFrameError(f"{name} must fit in u8: {value}")


def _require_u16(name: str, value: int) -> None:
    if not 0 <= value <= MAX_U16:
        raise AppFrameError(f"{name} must fit in u16: {value}")


def _require_u32(name: str, value: int) -> None:
    if not 0 <= value <= MAX_U32:
        raise AppFrameError(f"{name} must fit in u32: {value}")


def _require_u64(name: str, value: int) -> None:
    if not 0 <= value <= MAX_U64:
        raise AppFrameError(f"{name} must fit in u64: {value}")


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


def security_domain_value(value: str | int) -> int:
    if isinstance(value, int):
        _require_u8("security_domain", value)
        return value

    text = value.strip().lower()
    if text in SECURITY_DOMAINS:
        return SECURITY_DOMAINS[text]

    try:
        parsed = int(text, 0)
    except ValueError as exc:
        valid = ", ".join(sorted(SECURITY_DOMAINS))
        raise AppFrameError(
            f"unknown security domain '{value}' (valid: {valid})"
        ) from exc

    _require_u8("security_domain", parsed)
    return parsed


def security_domain_name(value: int) -> str:
    return SECURITY_DOMAIN_NAMES.get(value, f"0x{value:02x}")


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


def encode_route_data_payload(
    *,
    origin_sender_id: int,
    destination_id: int,
    ttl: int,
    origin_seq: int,
    inner_type: str | int,
    inner_payload: bytes,
) -> bytes:
    if origin_sender_id == 0:
        raise AppFrameError("origin_sender_id 0 is reserved")
    _require_u8("origin_sender_id", origin_sender_id)
    _require_u8("destination_id", destination_id)
    _require_u8("ttl", ttl)
    _require_u32("origin_seq", origin_seq)
    msg_type = message_type_value(inner_type)
    if msg_type == MSG_ROUTE_DATA:
        raise AppFrameError("route_data cannot carry nested route_data")
    if len(inner_payload) > MAX_U16:
        raise AppFrameError(
            f"inner_payload too large for u16 length: {len(inner_payload)}"
        )

    return (
        ROUTE_DATA_PAYLOAD.pack(
            origin_sender_id,
            destination_id,
            ttl,
            origin_seq,
            msg_type,
            len(inner_payload),
        )
        + inner_payload
    )


def decode_route_data_payload(payload: bytes) -> RouteData:
    if len(payload) < ROUTE_DATA_PAYLOAD_SIZE:
        raise AppFrameError(
            f"route_data payload too short: {len(payload)} < {ROUTE_DATA_PAYLOAD_SIZE}"
        )

    (
        origin_sender_id,
        destination_id,
        ttl,
        origin_seq,
        inner_type,
        inner_payload_len,
    ) = ROUTE_DATA_PAYLOAD.unpack_from(payload)
    inner_payload = payload[ROUTE_DATA_PAYLOAD_SIZE:]

    if origin_sender_id == 0:
        raise AppFrameError("origin_sender_id 0 is reserved")
    if inner_payload_len != len(inner_payload):
        raise AppFrameError(
            "inner_payload_len mismatch: "
            f"header={inner_payload_len} actual={len(inner_payload)}"
        )
    if inner_type == MSG_ROUTE_DATA:
        raise AppFrameError("route_data cannot carry nested route_data")
    if inner_type not in MESSAGE_TYPE_NAMES:
        raise AppFrameError(f"unknown route_data inner_type: 0x{inner_type:02x}")

    return RouteData(
        origin_sender_id=origin_sender_id,
        destination_id=destination_id,
        ttl=ttl,
        origin_seq=origin_seq,
        inner_type=inner_type,
        inner_payload=inner_payload,
    )


def encode_sync_payload(
    *,
    utc_ms: int,
    slot: int,
    channel: int,
    next_hop_ms: int,
) -> bytes:
    _require_u64("utc_ms", utc_ms)
    _require_u32("slot", slot)
    _require_u16("channel", channel)
    _require_u32("next_hop_ms", next_hop_ms)
    return SYNC_PAYLOAD.pack(utc_ms, slot, channel, next_hop_ms)


def decode_sync_payload(payload: bytes) -> SyncStatus:
    if len(payload) != SYNC_PAYLOAD_SIZE:
        raise AppFrameError(
            f"sync payload length mismatch: expected={SYNC_PAYLOAD_SIZE} "
            f"actual={len(payload)}"
        )

    utc_ms, slot, channel, next_hop_ms = SYNC_PAYLOAD.unpack(payload)
    return SyncStatus(
        utc_ms=utc_ms,
        slot=slot,
        channel=channel,
        next_hop_ms=next_hop_ms,
    )


def encode_status_payload(
    *,
    uptime_s: int,
    battery_pct: int | None,
    peer_count: int,
    flags: int = 0,
    status_version: int = STATUS_VERSION,
) -> bytes:
    _require_u8("status_version", status_version)
    if status_version != STATUS_VERSION:
        raise AppFrameError(f"unsupported status_version: {status_version}")
    _require_u32("uptime_s", uptime_s)
    _require_u8("peer_count", peer_count)
    _require_u8("flags", flags)

    encoded_battery_pct = STATUS_BATTERY_UNKNOWN
    if battery_pct is not None:
        if not 0 <= battery_pct <= 100:
            raise AppFrameError(
                "battery_pct must be in range 0..100 or None for unknown"
            )
        encoded_battery_pct = battery_pct

    return STATUS_PAYLOAD.pack(
        status_version,
        uptime_s,
        encoded_battery_pct,
        peer_count,
        flags,
    )


def decode_status_payload(payload: bytes) -> StatusPayload:
    if len(payload) != STATUS_PAYLOAD_SIZE:
        raise AppFrameError(
            f"status payload length mismatch: expected={STATUS_PAYLOAD_SIZE} "
            f"actual={len(payload)}"
        )

    status_version, uptime_s, battery_pct, peer_count, flags = STATUS_PAYLOAD.unpack(payload)
    if status_version != STATUS_VERSION:
        raise AppFrameError(f"unsupported status_version: {status_version}")

    decoded_battery_pct: int | None
    if battery_pct == STATUS_BATTERY_UNKNOWN:
        decoded_battery_pct = None
    elif 0 <= battery_pct <= 100:
        decoded_battery_pct = battery_pct
    else:
        raise AppFrameError(
            "status battery_pct must be in range 0..100 or "
            f"{STATUS_BATTERY_UNKNOWN} for unknown, got {battery_pct}"
        )

    return StatusPayload(
        status_version=status_version,
        uptime_s=uptime_s,
        battery_pct=decoded_battery_pct,
        peer_count=peer_count,
        flags=flags,
    )


def encode_route_secure_associated_data(
    *,
    origin_sender_id: int,
    destination_id: int,
    ttl: int,
    origin_seq: int,
    inner_type: str | int,
    inner_plaintext_len: int,
) -> bytes:
    if origin_sender_id == 0:
        raise AppFrameError("origin_sender_id 0 is reserved")
    _require_u8("origin_sender_id", origin_sender_id)
    _require_u8("destination_id", destination_id)
    _require_u8("ttl", ttl)
    _require_u32("origin_seq", origin_seq)
    _require_u16("inner_plaintext_len", inner_plaintext_len)
    msg_type = message_type_value(inner_type)
    if msg_type == MSG_ROUTE_DATA:
        raise AppFrameError("route_data cannot carry nested route_data")
    if msg_type not in MESSAGE_TYPE_NAMES:
        raise AppFrameError(f"unknown route secure inner_type: 0x{msg_type:02x}")

    return ROUTE_SECURE_ASSOCIATED_DATA.pack(
        origin_sender_id,
        destination_id,
        ttl,
        origin_seq,
        msg_type,
        inner_plaintext_len,
    )


def _require_aead_key(key: bytes) -> None:
    if len(key) != CHACHA20_POLY1305_KEY_SIZE:
        raise AppFrameError(
            "ChaCha20-Poly1305 key must be "
            f"{CHACHA20_POLY1305_KEY_SIZE} bytes, got {len(key)}"
        )


def _chacha20_poly1305(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    except ImportError as exc:
        raise AppFrameError(
            "secure payloads require the 'cryptography' Python package"
        ) from exc
    return ChaCha20Poly1305(key)


def _secure_auth_header(
    *,
    secure_version: int,
    security_domain: int,
    key_id: int,
    key_epoch: int,
) -> bytes:
    return SECURE_PAYLOAD.pack(
        secure_version,
        security_domain,
        key_id,
        key_epoch,
        b"\x00" * CHACHA20_POLY1305_NONCE_SIZE,
    )[:8]


def _secure_aad(payload: SecurePayload, associated_data: bytes) -> bytes:
    return (
        _secure_auth_header(
            secure_version=payload.secure_version,
            security_domain=payload.security_domain,
            key_id=payload.key_id,
            key_epoch=payload.key_epoch,
        )
        + associated_data
    )


def encode_secure_payload(
    *,
    key: bytes,
    security_domain: str | int,
    key_id: int,
    key_epoch: int,
    plaintext: bytes,
    associated_data: bytes,
    nonce: bytes | None = None,
) -> bytes:
    _require_aead_key(key)
    domain = security_domain_value(security_domain)
    _require_u16("key_id", key_id)
    _require_u32("key_epoch", key_epoch)
    if nonce is None:
        nonce = os.urandom(CHACHA20_POLY1305_NONCE_SIZE)
    if len(nonce) != CHACHA20_POLY1305_NONCE_SIZE:
        raise AppFrameError(
            "ChaCha20-Poly1305 nonce must be "
            f"{CHACHA20_POLY1305_NONCE_SIZE} bytes, got {len(nonce)}"
        )

    auth_header = _secure_auth_header(
        secure_version=SECURE_VERSION,
        security_domain=domain,
        key_id=key_id,
        key_epoch=key_epoch,
    )
    ciphertext_and_tag = _chacha20_poly1305(key).encrypt(
        nonce,
        plaintext,
        auth_header + associated_data,
    )
    return (
        SECURE_PAYLOAD.pack(SECURE_VERSION, domain, key_id, key_epoch, nonce)
        + ciphertext_and_tag
    )


def decode_secure_payload(payload: bytes) -> SecurePayload:
    if len(payload) < SECURE_PAYLOAD_SIZE + CHACHA20_POLY1305_TAG_SIZE:
        raise AppFrameError(
            "secure payload too short: "
            f"{len(payload)} < {SECURE_PAYLOAD_SIZE + CHACHA20_POLY1305_TAG_SIZE}"
        )

    secure_version, domain, key_id, key_epoch, nonce = SECURE_PAYLOAD.unpack_from(
        payload
    )
    ciphertext_and_tag = payload[SECURE_PAYLOAD_SIZE:]
    if secure_version != SECURE_VERSION:
        raise AppFrameError(f"unsupported secure_version: {secure_version}")
    if domain not in SECURITY_DOMAIN_NAMES:
        raise AppFrameError(f"unknown security_domain: 0x{domain:02x}")

    return SecurePayload(
        secure_version=secure_version,
        security_domain=domain,
        key_id=key_id,
        key_epoch=key_epoch,
        nonce=nonce,
        ciphertext_and_tag=ciphertext_and_tag,
    )


def decrypt_secure_payload(
    payload: SecurePayload,
    *,
    key: bytes,
    associated_data: bytes,
) -> bytes:
    _require_aead_key(key)
    try:
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:
        raise AppFrameError(
            "secure payloads require the 'cryptography' Python package"
        ) from exc
    try:
        return _chacha20_poly1305(key).decrypt(
            payload.nonce,
            payload.ciphertext_and_tag,
            _secure_aad(payload, associated_data),
        )
    except InvalidTag as exc:
        raise AppFrameError("secure payload authentication failed") from exc


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
