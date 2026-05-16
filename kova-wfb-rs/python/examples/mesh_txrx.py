#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import os
import struct
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque

from wfb_rs_py import Rx, Tx
from wfb_rs_py.app_proto import (
    AppFrameError,
    CHACHA20_POLY1305_KEY_SIZE,
    MSG_ROUTE_DATA,
    MSG_SYNC,
    SEC_DOMAIN_MESH_GROUP,
    decode_frame,
    decode_route_data_payload,
    decode_secure_payload,
    decode_sync_payload,
    decrypt_secure_payload,
    encode_frame,
    encode_route_data_payload,
    encode_secure_payload,
    encode_sync_payload,
    message_type_value,
    security_domain_name,
    security_domain_value,
)

MAX_U16 = 0xFFFF
MAX_U32 = 0xFFFF_FFFF
CONFIG_SECTION = "mesh"
MESH_CRYPTO_SECTION = "mesh_crypto"
SECURE_ROUTE_AAD = struct.Struct("!4sBBBIBH")


@dataclass(frozen=True)
class ScheduleState:
    utc_ms: int
    slot: int
    slot_elapsed_ms: int
    channel: int
    next_hop_ms: int


@dataclass(frozen=True)
class MeshCryptoConfig:
    security_domain: int
    key_id: int
    key_epoch: int
    key: bytes


def _default_iface() -> str | None:
    return os.getenv("NIC") or os.getenv("WFB_IFACE") or os.getenv("IFACE")


def _default_sender_id() -> int | None:
    value = os.getenv("WFB_SENDER_ID") or os.getenv("SENDER_ID")
    if value is None:
        return None
    return int(value, 0)


def _next_seq(seq: int) -> int:
    seq = (seq + 1) & MAX_U32
    return 1 if seq == 0 else seq


def _parse_channel_list(value: object | None) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        return [int(v) for v in value]
    channels: list[int] = []
    for part in str(value).split(","):
        text = part.strip()
        if text:
            channels.append(int(text, 0))
    return channels


def _parse_key_hex(value: str) -> bytes:
    try:
        key = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise ValueError("mesh_crypto key_hex must be valid hex") from exc
    if len(key) != CHACHA20_POLY1305_KEY_SIZE:
        raise ValueError(
            "mesh_crypto key_hex must decode to "
            f"{CHACHA20_POLY1305_KEY_SIZE} bytes, got {len(key)}"
        )
    return key


def _secure_route_aad(
    *,
    origin_sender_id: int,
    destination_id: int,
    ttl: int,
    origin_seq: int,
    inner_type: int,
    plaintext_len: int,
) -> bytes:
    if not 0 <= plaintext_len <= MAX_U16:
        raise AppFrameError(f"secure plaintext_len must fit in u16: {plaintext_len}")
    return SECURE_ROUTE_AAD.pack(
        b"rtv1",
        origin_sender_id,
        destination_id,
        ttl,
        origin_seq,
        inner_type,
        plaintext_len,
    )


def _utc_ms() -> int:
    return int(time.time() * 1000)


def _ntp_sync_status() -> str:
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"

    if result.returncode != 0:
        return "unavailable"
    value = result.stdout.strip().lower()
    if value in {"yes", "true", "1"}:
        return "yes"
    if value in {"no", "false", "0"}:
        return "no"
    return "unknown"


def _run_privileged(cmd: list[str]) -> None:
    full_cmd = cmd if os.geteuid() == 0 else ["sudo", *cmd]
    result = subprocess.run(
        full_cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"command failed ({' '.join(full_cmd)}): {detail}")


def _set_channel(
    *,
    iface: str,
    channel: int,
    width: str,
    down_up: bool,
    settle_ms: int,
) -> None:
    if down_up:
        _run_privileged(["ip", "link", "set", iface, "down"])
        try:
            _run_privileged(["iw", "dev", iface, "set", "channel", str(channel), width])
        except RuntimeError:
            _run_privileged(["ip", "link", "set", iface, "up"])
            _run_privileged(["iw", "dev", iface, "set", "channel", str(channel), width])
        else:
            _run_privileged(["ip", "link", "set", iface, "up"])
    else:
        _run_privileged(["iw", "dev", iface, "set", "channel", str(channel), width])
    if settle_ms > 0:
        time.sleep(settle_ms / 1000.0)


def _schedule_state(
    channels: list[int],
    slot_ms: int,
    epoch_ms: int,
) -> ScheduleState:
    now_ms = _utc_ms()
    elapsed_ms = max(0, now_ms - epoch_ms)
    slot = elapsed_ms // slot_ms
    slot_elapsed_ms = elapsed_ms % slot_ms
    return ScheduleState(
        utc_ms=now_ms,
        slot=slot,
        slot_elapsed_ms=slot_elapsed_ms,
        channel=channels[slot % len(channels)],
        next_hop_ms=slot_ms - slot_elapsed_ms,
    )


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text.lower() in {"", "none", "null"}:
        return None
    return value


def _load_config(path: str | None) -> dict[str, object]:
    if path is None:
        return {}

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)
    section = (
        parser[CONFIG_SECTION]
        if parser.has_section(CONFIG_SECTION)
        else parser["DEFAULT"]
    )

    out: dict[str, object] = {}
    for key in (
        "iface",
        "message_type",
        "message",
        "hop_channels",
        "channel_width",
    ):
        if key in section:
            value = _optional_text(section.get(key))
            if value is not None:
                out[key] = value

    for key in (
        "stream_id",
        "sender_id",
        "destination_id",
        "ttl",
        "count",
        "tx_interval_ms",
        "rx_timeout_ms",
        "seen_limit",
        "hop_slot_ms",
        "hop_epoch_ms",
        "channel_settle_ms",
        "channel_tx_guard_ms",
        "sync_interval_ms",
    ):
        if key in section:
            out[key] = int(section[key], 0)

    for key in (
        "print_rssi",
        "include_self",
        "channel_agility",
        "channel_down_up",
        "sync_heartbeat",
    ):
        if key in section:
            out[key] = section.getboolean(key)

    if parser.has_section(MESH_CRYPTO_SECTION):
        section = parser[MESH_CRYPTO_SECTION]
        if "enabled" in section:
            out["mesh_crypto_enabled"] = section.getboolean("enabled")
        for key in ("security_domain", "key_hex"):
            if key in section:
                value = _optional_text(section.get(key))
                if value is not None:
                    out[f"mesh_crypto_{key}"] = value
        for key in ("key_id", "key_epoch", "replay_window"):
            if key in section:
                out[f"mesh_crypto_{key}"] = int(section[key], 0)

    return out


class SeenRoutes:
    def __init__(self, limit: int):
        self._limit = limit
        self._seen: set[tuple[int, int]] = set()
        self._order: Deque[tuple[int, int]] = deque()

    def remember(self, key: tuple[int, int]) -> bool:
        if key in self._seen:
            return False

        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self._limit:
            old = self._order.popleft()
            self._seen.discard(old)
        return True

    def contains(self, key: tuple[int, int]) -> bool:
        return key in self._seen


class ReplayWindow:
    def __init__(self, limit: int):
        self._limit = limit
        self._seen: set[tuple[int, int, int, int]] = set()
        self._order: Deque[tuple[int, int, int, int]] = deque()

    def remember(self, key: tuple[int, int, int, int]) -> bool:
        if key in self._seen:
            return False

        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self._limit:
            old = self._order.popleft()
            self._seen.discard(old)
        return True


def _encode_outer_route_frame(
    *,
    sender_id: int,
    app_seq: int,
    route_payload: bytes,
) -> bytes:
    return encode_frame(
        sender_id=sender_id,
        message_type=MSG_ROUTE_DATA,
        app_seq=app_seq,
        payload=route_payload,
    )


def main() -> int:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config")
    config_args, _ = config_parser.parse_known_args()
    try:
        config_defaults = _load_config(config_args.config)
    except (OSError, ValueError, configparser.Error) as exc:
        raise SystemExit(f"mesh_txrx.py: config error: {exc}") from exc

    parser = argparse.ArgumentParser(
        description="UDP-like TTL mesh flooding example for wfb_rs_py",
        parents=[config_parser],
    )
    parser.add_argument(
        "--iface",
        default=config_defaults.get("iface", _default_iface()),
        help="monitor-mode interface (default: $NIC, $WFB_IFACE, or $IFACE)",
    )
    parser.add_argument(
        "--stream-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("stream_id", 1),
    )
    parser.add_argument(
        "--sender-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("sender_id", _default_sender_id()),
        help="local sender id (default: $WFB_SENDER_ID or $SENDER_ID)",
    )
    parser.add_argument(
        "--destination-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("destination_id", 0),
        help="target node id; 0 broadcasts to all nodes",
    )
    parser.add_argument(
        "--ttl",
        type=lambda value: int(value, 0),
        default=config_defaults.get("ttl", 2),
    )
    parser.add_argument(
        "--message-type",
        default=config_defaults.get("message_type", "status"),
        help="inner message type: hello, text, data, or status",
    )
    parser.add_argument(
        "--message",
        default=config_defaults.get("message"),
        help="optional message to originate",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=config_defaults.get("count", 0),
        help="number of originated messages to send; 0 means forever when --message is set",
    )
    parser.add_argument(
        "--tx-interval-ms",
        type=int,
        default=config_defaults.get("tx_interval_ms", 1000),
    )
    parser.add_argument(
        "--rx-timeout-ms",
        type=int,
        default=config_defaults.get("rx_timeout_ms", 50),
    )
    parser.add_argument(
        "--seen-limit",
        type=int,
        default=config_defaults.get("seen_limit", 4096),
    )
    parser.add_argument(
        "--print-rssi",
        action="store_true",
        default=config_defaults.get("print_rssi", False),
    )
    parser.add_argument(
        "--include-self",
        action="store_true",
        default=config_defaults.get("include_self", False),
        help="do not filter frames injected by this host; mainly for debugging",
    )
    parser.add_argument(
        "--channel-agility",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("channel_agility", False),
        help="hop across configured channels on a shared wall-clock schedule",
    )
    parser.add_argument(
        "--hop-channels",
        default=config_defaults.get("hop_channels"),
        help="comma-separated channel schedule, for example 36,40,48",
    )
    parser.add_argument(
        "--channel-width",
        default=config_defaults.get("channel_width", "HT20"),
        help="iw channel width argument, for example HT20",
    )
    parser.add_argument(
        "--hop-slot-ms",
        type=int,
        default=config_defaults.get("hop_slot_ms", 5000),
        help="duration of each channel slot",
    )
    parser.add_argument(
        "--hop-epoch-ms",
        type=int,
        default=config_defaults.get("hop_epoch_ms", 0),
        help="shared Unix epoch offset in milliseconds for hop schedule",
    )
    parser.add_argument(
        "--channel-settle-ms",
        type=int,
        default=config_defaults.get("channel_settle_ms", 250),
        help="delay after each channel change before reopening radio handles",
    )
    parser.add_argument(
        "--channel-tx-guard-ms",
        type=int,
        default=config_defaults.get("channel_tx_guard_ms", 250),
        help="listen-only guard time after channel changes before originating packets",
    )
    parser.add_argument(
        "--channel-down-up",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("channel_down_up", True),
        help="bring the interface down/up around each channel change",
    )
    parser.add_argument(
        "--sync-heartbeat",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("sync_heartbeat", False),
        help="send compact routed sync heartbeats with UTC time, slot, and channel",
    )
    parser.add_argument(
        "--sync-interval-ms",
        type=int,
        default=config_defaults.get("sync_interval_ms", 5000),
        help="interval between sync heartbeat packets",
    )
    args = parser.parse_args()

    if not args.iface:
        parser.error("--iface is required unless NIC, WFB_IFACE, or IFACE is set")
    if args.stream_id == 0:
        parser.error("--stream-id must be non-zero")
    if args.sender_id is None:
        parser.error("--sender-id is required unless WFB_SENDER_ID or SENDER_ID is set")
    if not 1 <= args.sender_id <= 255:
        parser.error("--sender-id must be in range 1..255")
    if not 0 <= args.destination_id <= 255:
        parser.error("--destination-id must be in range 0..255")
    if not 0 <= args.ttl <= 255:
        parser.error("--ttl must be in range 0..255")
    if args.count < 0:
        parser.error("--count must be >= 0")
    if args.tx_interval_ms < 0:
        parser.error("--tx-interval-ms must be >= 0")
    if args.rx_timeout_ms <= 0:
        parser.error("--rx-timeout-ms must be > 0")
    if args.seen_limit < 1:
        parser.error("--seen-limit must be > 0")
    hop_channels = _parse_channel_list(args.hop_channels)
    if args.channel_agility:
        if not hop_channels:
            parser.error("--hop-channels is required when --channel-agility is enabled")
        if any(channel <= 0 for channel in hop_channels):
            parser.error("--hop-channels must contain positive channel numbers")
        if args.hop_slot_ms <= 0:
            parser.error("--hop-slot-ms must be > 0")
        if args.channel_settle_ms < 0:
            parser.error("--channel-settle-ms must be >= 0")
    if args.channel_tx_guard_ms < 0:
        parser.error("--channel-tx-guard-ms must be >= 0")
    if args.sync_interval_ms <= 0:
        parser.error("--sync-interval-ms must be > 0")

    try:
        inner_type = message_type_value(args.message_type)
    except AppFrameError as exc:
        parser.error(str(exc))
    if inner_type == MSG_ROUTE_DATA:
        parser.error("--message-type cannot be route_data")
    if inner_type == MSG_SYNC:
        parser.error("--message-type sync is reserved for --sync-heartbeat")

    mesh_crypto_enabled = bool(config_defaults.get("mesh_crypto_enabled", False))
    mesh_replay_window = int(config_defaults.get("mesh_crypto_replay_window", 4096))
    if mesh_replay_window < 1:
        parser.error("[mesh_crypto] replay_window must be > 0")

    mesh_crypto: MeshCryptoConfig | None = None
    if mesh_crypto_enabled:
        domain = security_domain_value(
            config_defaults.get("mesh_crypto_security_domain", "mesh_group")
        )
        if domain != SEC_DOMAIN_MESH_GROUP:
            parser.error("Phase A only supports mesh_crypto security_domain=mesh_group")
        key_id = int(config_defaults.get("mesh_crypto_key_id", 0))
        key_epoch = int(config_defaults.get("mesh_crypto_key_epoch", 0))
        key_hex = config_defaults.get("mesh_crypto_key_hex")
        if not 1 <= key_id <= MAX_U16:
            parser.error("[mesh_crypto] key_id must be in range 1..65535")
        if not 0 <= key_epoch <= MAX_U32:
            parser.error("[mesh_crypto] key_epoch must fit in u32")
        if not key_hex:
            parser.error("[mesh_crypto] key_hex is required when enabled=true")
        try:
            mesh_key = _parse_key_hex(str(key_hex))
        except ValueError as exc:
            parser.error(str(exc))
        mesh_crypto = MeshCryptoConfig(
            security_domain=domain,
            key_id=key_id,
            key_epoch=key_epoch,
            key=mesh_key,
        )

    seen = SeenRoutes(args.seen_limit)
    secure_replay = ReplayWindow(mesh_replay_window)
    inner_payload = args.message.encode("utf-8") if args.message is not None else None
    next_origin_seq = 1
    next_outer_seq = 1
    next_rf_seq = 1
    originated_count = 0
    next_tx_at = time.monotonic()
    next_sync_at = time.monotonic()
    tx_guard_until = 0.0
    current_channel: int | None = None
    tx: Tx | None = None
    rx: Rx | None = None

    def log(message: str) -> None:
        channel = "?" if current_channel is None else str(current_channel)
        print(f"CH={channel} {message}")

    def close_radio() -> None:
        nonlocal tx, rx
        if rx is not None:
            rx.close()
            rx = None
        if tx is not None:
            tx.close()
            tx = None

    def open_radio() -> None:
        nonlocal tx, rx
        tx = Tx(iface=args.iface, stream_id=args.stream_id)
        try:
            rx = Rx(
                iface=args.iface,
                stream_id=args.stream_id,
                ignore_self_injected=not args.include_self,
            )
        except Exception:
            tx.close()
            tx = None
            raise

    def current_schedule_state() -> ScheduleState | None:
        if not args.channel_agility:
            return None
        return _schedule_state(hop_channels, args.hop_slot_ms, args.hop_epoch_ms)

    def encrypt_mesh_payload(
        *,
        origin_sender_id: int,
        destination_id: int,
        ttl: int,
        origin_seq: int,
        inner_type: int,
        plaintext: bytes,
    ) -> bytes:
        if mesh_crypto is None or inner_type == MSG_SYNC:
            return plaintext
        aad = _secure_route_aad(
            origin_sender_id=origin_sender_id,
            destination_id=destination_id,
            ttl=ttl,
            origin_seq=origin_seq,
            inner_type=inner_type,
            plaintext_len=len(plaintext),
        )
        return encode_secure_payload(
            key=mesh_crypto.key,
            security_domain=mesh_crypto.security_domain,
            key_id=mesh_crypto.key_id,
            key_epoch=mesh_crypto.key_epoch,
            plaintext=plaintext,
            associated_data=aad,
        )

    def decrypt_mesh_payload(route) -> tuple[bytes, bool]:
        if mesh_crypto is None or route.inner_type == MSG_SYNC:
            return route.inner_payload, False

        secure = decode_secure_payload(route.inner_payload)
        if secure.security_domain != mesh_crypto.security_domain:
            raise AppFrameError(
                "unexpected security_domain: "
                f"{secure.security_domain_name} "
                f"expected={security_domain_name(mesh_crypto.security_domain)}"
            )
        if secure.key_id != mesh_crypto.key_id:
            raise AppFrameError(
                f"unexpected key_id: {secure.key_id} expected={mesh_crypto.key_id}"
            )
        if secure.key_epoch != mesh_crypto.key_epoch:
            raise AppFrameError(
                "unexpected key_epoch: "
                f"{secure.key_epoch} expected={mesh_crypto.key_epoch}"
            )

        replay_key = (
            secure.security_domain,
            route.origin_sender_id,
            secure.key_epoch,
            route.origin_seq,
        )
        aad = _secure_route_aad(
            origin_sender_id=route.origin_sender_id,
            destination_id=route.destination_id,
            ttl=route.ttl,
            origin_seq=route.origin_seq,
            inner_type=route.inner_type,
            plaintext_len=secure.plaintext_len,
        )
        plaintext = decrypt_secure_payload(
            secure,
            key=mesh_crypto.key,
            associated_data=aad,
        )
        if not secure_replay.remember(replay_key):
            raise AppFrameError("secure replay detected")
        return plaintext, True

    def send_route(
        *,
        inner_type: int,
        payload: bytes,
        destination_id: int,
        ttl: int,
    ) -> tuple[int, bool]:
        nonlocal next_origin_seq, next_outer_seq, next_rf_seq
        if tx is None:
            raise RuntimeError("TX handle is not open")

        seq_sent = next_origin_seq
        routed_payload = encrypt_mesh_payload(
            origin_sender_id=args.sender_id,
            destination_id=destination_id,
            ttl=ttl,
            origin_seq=seq_sent,
            inner_type=inner_type,
            plaintext=payload,
        )
        route_payload = encode_route_data_payload(
            origin_sender_id=args.sender_id,
            destination_id=destination_id,
            ttl=ttl,
            origin_seq=seq_sent,
            inner_type=inner_type,
            inner_payload=routed_payload,
        )
        frame = _encode_outer_route_frame(
            sender_id=args.sender_id,
            app_seq=next_outer_seq,
            route_payload=route_payload,
        )
        tx.send(frame, seq=next_rf_seq)
        seen.remember((args.sender_id, seq_sent))
        next_origin_seq = _next_seq(next_origin_seq)
        next_outer_seq = _next_seq(next_outer_seq)
        next_rf_seq = _next_seq(next_rf_seq)
        return seq_sent, routed_payload is not payload

    def build_sync_payload() -> tuple[bytes, ScheduleState | None]:
        state = current_schedule_state()
        if state is None:
            return (
                encode_sync_payload(
                    utc_ms=_utc_ms(),
                    slot=0,
                    channel=current_channel or 0,
                    next_hop_ms=0,
                ),
                None,
            )

        return (
            encode_sync_payload(
                utc_ms=state.utc_ms,
                slot=state.slot,
                channel=state.channel,
                next_hop_ms=state.next_hop_ms,
            ),
            state,
        )

    def switch_channel(state: ScheduleState) -> None:
        nonlocal current_channel, tx_guard_until
        close_radio()
        _set_channel(
            iface=args.iface,
            channel=state.channel,
            width=args.channel_width,
            down_up=args.channel_down_up,
            settle_ms=args.channel_settle_ms,
        )
        open_radio()
        current_channel = state.channel
        tx_guard_until = time.monotonic() + (args.channel_tx_guard_ms / 1000.0)
        log(
            f"CHANNEL active iface={args.iface} width={args.channel_width} "
            f"utc_ms={state.utc_ms} slot={state.slot} "
            f"next_hop_ms={state.next_hop_ms} tx_guard_ms={args.channel_tx_guard_ms}"
        )

    ntp_status = _ntp_sync_status()

    try:
        if args.channel_agility:
            switch_channel(
                _schedule_state(hop_channels, args.hop_slot_ms, args.hop_epoch_ms)
            )
        else:
            open_radio()

        hop_desc = ",".join(str(channel) for channel in hop_channels)
        agility_desc = f" agility={hop_desc}" if args.channel_agility else ""
        log(
            f"Mesh UDP mode: sender={args.sender_id} ttl={args.ttl} "
            f"dest={args.destination_id}{agility_desc}, Ctrl-C to exit"
        )
        state = current_schedule_state()
        if state is not None:
            log(
                f"CLOCK source=unix_utc ntp_synced={ntp_status} "
                f"utc_ms={state.utc_ms} hop_epoch_ms={args.hop_epoch_ms} "
                f"slot_ms={args.hop_slot_ms} slot={state.slot} "
                f"next_hop_ms={state.next_hop_ms}"
            )
        else:
            log(
                f"CLOCK source=unix_utc ntp_synced={ntp_status} "
                f"utc_ms={_utc_ms()} schedule=off"
            )

        while True:
            state = current_schedule_state()
            if state is not None:
                if state.channel != current_channel:
                    switch_channel(state)
                    continue

            if tx is None or rx is None:
                raise RuntimeError("radio handles are not open")

            now = time.monotonic()
            tx_allowed = now >= tx_guard_until
            should_originate = (
                inner_payload is not None
                and (args.count == 0 or originated_count < args.count)
                and now >= next_tx_at
                and tx_allowed
            )
            if should_originate:
                sent_seq, secured = send_route(
                    inner_type=inner_type,
                    payload=inner_payload,
                    destination_id=args.destination_id,
                    ttl=args.ttl,
                )
                if secured and mesh_crypto is not None:
                    log(
                        f"TX secure origin={args.sender_id} seq={sent_seq} "
                        f"domain={security_domain_name(mesh_crypto.security_domain)} "
                        f"key_id={mesh_crypto.key_id} "
                        f"key_epoch={mesh_crypto.key_epoch} len={len(inner_payload)}"
                    )
                log(
                    f"TX route origin={args.sender_id} seq={sent_seq} "
                    f"dest={args.destination_id} ttl={args.ttl} "
                    f"type={args.message_type} len={len(inner_payload)} "
                    f"secure={int(secured)}"
                )
                originated_count += 1
                next_tx_at = now + (args.tx_interval_ms / 1000.0)

            should_sync = (
                args.sync_heartbeat
                and now >= next_sync_at
                and tx_allowed
            )
            if should_sync:
                sync_payload, sync_state = build_sync_payload()
                sent_seq, _ = send_route(
                    inner_type=MSG_SYNC,
                    payload=sync_payload,
                    destination_id=0,
                    ttl=args.ttl,
                )
                if sync_state is None:
                    log(
                        f"TX sync origin={args.sender_id} seq={sent_seq} "
                        f"dest=0 ttl={args.ttl} utc_ms={_utc_ms()} "
                        f"slot=0 channel={current_channel or 0} next_hop_ms=0"
                    )
                else:
                    log(
                        f"TX sync origin={args.sender_id} seq={sent_seq} "
                        f"dest=0 ttl={args.ttl} utc_ms={sync_state.utc_ms} "
                        f"slot={sync_state.slot} channel={sync_state.channel} "
                        f"next_hop_ms={sync_state.next_hop_ms}"
                    )
                next_sync_at = now + (args.sync_interval_ms / 1000.0)

            result = rx.recv_optional(timeout_ms=args.rx_timeout_ms)
            if result is None:
                continue

            payload, meta = result
            try:
                frame = decode_frame(payload)
            except AppFrameError as exc:
                log(
                    f'RX invalid_app_frame len={len(payload)} '
                    f'truncated={int(meta.truncated)} error="{exc}"'
                )
                continue

            if frame.message_type != MSG_ROUTE_DATA:
                continue

            try:
                route = decode_route_data_payload(frame.payload)
            except AppFrameError as exc:
                log(
                    f'RX invalid_route from={frame.sender_id} '
                    f'len={len(frame.payload)} error="{exc}"'
                )
                continue

            if route.origin_sender_id == args.sender_id:
                seen.remember(route.dedupe_key)
                log(
                    f"RX own_route via={frame.sender_id} "
                    f"origin_seq={route.origin_seq} dropped=1"
                )
                continue

            if seen.contains(route.dedupe_key):
                log(
                    f"RX duplicate_route via={frame.sender_id} "
                    f"origin={route.origin_sender_id} seq={route.origin_seq} "
                    "dropped=1"
                )
                continue

            try:
                route_payload, route_secured = decrypt_mesh_payload(route)
            except AppFrameError as exc:
                log(
                    f'RX auth_fail via={frame.sender_id} '
                    f'origin={route.origin_sender_id} seq={route.origin_seq} '
                    f'type={route.inner_type_name} error="{exc}" dropped=1'
                )
                continue

            seen.remember(route.dedupe_key)
            if route_secured and mesh_crypto is not None:
                log(
                    f"RX secure via={frame.sender_id} "
                    f"origin={route.origin_sender_id} seq={route.origin_seq} "
                    f"domain={security_domain_name(mesh_crypto.security_domain)} "
                    f"key_id={mesh_crypto.key_id} "
                    f"key_epoch={mesh_crypto.key_epoch} decrypted=1"
                )

            delivered = route.destination_id in (0, args.sender_id)
            suffix = ""
            if args.print_rssi:
                suffix = (
                    f" bw={meta.bandwidth} mcs={meta.mcs_index} "
                    f"rssi0={meta.rssi[0]}"
                )

            if delivered:
                if route.inner_type == MSG_SYNC:
                    try:
                        sync = decode_sync_payload(route_payload)
                    except AppFrameError as exc:
                        log(
                            f'RX invalid_sync via={frame.sender_id} '
                            f'origin={route.origin_sender_id} seq={route.origin_seq} '
                            f'len={len(route_payload)} error="{exc}"'
                        )
                    else:
                        local_state = current_schedule_state()
                        local_channel = current_channel or 0
                        if local_state is None:
                            local_slot = "?"
                            slot_delta = "?"
                        else:
                            local_slot = str(local_state.slot)
                            slot_delta = str(local_state.slot - sync.slot)
                        log(
                            f"RX sync via={frame.sender_id} "
                            f"origin={route.origin_sender_id} seq={route.origin_seq} "
                            f"utc_ms={sync.utc_ms} skew_ms={_utc_ms() - sync.utc_ms} "
                            f"slot={sync.slot} local_slot={local_slot} "
                            f"slot_delta={slot_delta} channel={sync.channel} "
                            f"local_channel={local_channel} "
                            f"channel_match={int(local_channel == sync.channel)} "
                            f"next_hop_ms={sync.next_hop_ms}{suffix}"
                        )
                else:
                    text = route_payload.decode("utf-8", errors="replace")
                    payload_text = (
                        "[encrypted]"
                        if route_secured
                        else f'"{text}"'
                    )
                    log(
                        f'RX deliver via={frame.sender_id} '
                        f'origin={route.origin_sender_id} seq={route.origin_seq} '
                        f'dest={route.destination_id} ttl={route.ttl} '
                        f"type={route.inner_type_name} payload={payload_text}"
                        f"{suffix}"
                    )
            else:
                log(
                    f"RX transit via={frame.sender_id} "
                    f"origin={route.origin_sender_id} seq={route.origin_seq} "
                    f"dest={route.destination_id} ttl={route.ttl}{suffix}"
                )

            if route.ttl <= 0:
                continue

            forwarded = route.decremented_ttl()
            forward_inner_payload = encrypt_mesh_payload(
                origin_sender_id=forwarded.origin_sender_id,
                destination_id=forwarded.destination_id,
                ttl=forwarded.ttl,
                origin_seq=forwarded.origin_seq,
                inner_type=forwarded.inner_type,
                plaintext=route_payload,
            )
            forward_payload = encode_route_data_payload(
                origin_sender_id=forwarded.origin_sender_id,
                destination_id=forwarded.destination_id,
                ttl=forwarded.ttl,
                origin_seq=forwarded.origin_seq,
                inner_type=forwarded.inner_type,
                inner_payload=forward_inner_payload,
            )
            forward_frame = _encode_outer_route_frame(
                sender_id=args.sender_id,
                app_seq=next_outer_seq,
                route_payload=forward_payload,
            )
            tx.send(forward_frame, seq=next_rf_seq)
            forward_secured = forward_inner_payload is not route_payload
            if forward_secured and mesh_crypto is not None:
                log(
                    f"TX secure_forward origin={forwarded.origin_sender_id} "
                    f"seq={forwarded.origin_seq} "
                    f"domain={security_domain_name(mesh_crypto.security_domain)} "
                    f"key_id={mesh_crypto.key_id} "
                    f"key_epoch={mesh_crypto.key_epoch}"
                )
            log(
                f"TX forward origin={forwarded.origin_sender_id} "
                f"seq={forwarded.origin_seq} dest={forwarded.destination_id} "
                f"ttl={forwarded.ttl} secure={int(forward_secured)}"
            )
            next_outer_seq = _next_seq(next_outer_seq)
            next_rf_seq = _next_seq(next_rf_seq)
    except KeyboardInterrupt:
        return 130
    finally:
        close_radio()


if __name__ == "__main__":
    raise SystemExit(main())
