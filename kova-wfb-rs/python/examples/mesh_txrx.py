#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import configparser
import json
import math
import os
import secrets
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque

from wfb_rs_py import Rx, Tx
from wfb_rs_py.app_proto import (
    ADDR_BROADCAST,
    ADDR_C2,
    ADDR_NODE,
    AppFrameError,
    CHACHA20_POLY1305_KEY_SIZE,
    MSG_DATA,
    MSG_ROUTE_ADV,
    MSG_ROUTE_DATA,
    MSG_ROUTE_V2,
    MSG_STATUS,
    MSG_SYNC,
    ROUTE_ADV_UNREACHABLE_COST,
    ROUTE_ADV_UNREACHABLE_HOPS,
    SEC_DOMAIN_C2_TO_NODE,
    SEC_DOMAIN_MESH_GROUP,
    SEC_DOMAIN_NODE_TO_C2,
    TRAFFIC_C2_DOWNLINK,
    TRAFFIC_C2_UPLINK,
    TRAFFIC_MESH,
    address_type_name,
    address_type_value,
    decode_frame,
    decode_route_adv_payload,
    decode_route_data_payload,
    decode_route_v2_payload,
    decode_secure_payload,
    decode_status_payload,
    decode_sync_payload,
    decrypt_secure_payload,
    encode_frame,
    encode_route_adv_payload,
    encode_route_data_payload,
    encode_route_v2_e2e_associated_data,
    encode_route_v2_payload,
    encode_secure_payload,
    encode_status_payload,
    encode_sync_payload,
    message_type_name,
    message_type_value,
    security_domain_name,
    security_domain_value,
    traffic_class_name,
    traffic_class_value,
    STATUS_FLAG_CRYPTO_ENABLED,
    STATUS_FLAG_DEGRADED_LINK,
    STATUS_FLAG_FORWARDING_ENABLED,
)

MAX_U16 = 0xFFFF
MAX_U32 = 0xFFFF_FFFF
DEFAULT_STREAM_ID = 0xDEAD_BEEF
CONFIG_SECTION = "mesh"
MESH_CRYPTO_SECTION = "mesh_crypto"
C2_UPLINK_CRYPTO_SECTION = "c2_uplink_crypto"
C2_DOWNLINK_CRYPTO_SECTION = "c2_downlink_crypto"
C2_UPLINK_SECTION = "c2_uplink"
LOCAL_CONTROL_SECTION = "local_control"
LOCAL_C2_TAP_SECTION = "local_c2_tap"
SECURE_ROUTE_AAD = struct.Struct("!4sBBBIBH")
DEFAULT_C2_ID = 1
LINK_STATE_NOMINAL = "nominal"
LINK_STATE_DEGRADED = "degraded"
LINK_STATE_ISOLATED = "isolated"
LINK_STATE_RECOVERY = "recovery"
LINK_STATE_MOVING = "moving"
LINK_STATE_RTB = "rtb"

KOVA_IMAGE_MAGIC = b"KOVA"
PIXEL_FMT_GRAYSCALE = 0
PIXEL_FMT_RGB = 1


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


@dataclass(frozen=True)
class E2ECryptoConfig:
    security_domain: int
    key_id: int
    key_epoch: int
    key: bytes


@dataclass(frozen=True)
class LinkHealthTransition:
    previous_state: str
    state: str
    reason: str
    inactive_ms: int
    active_peers: int


@dataclass(frozen=True)
class LocalC2Message:
    inner_type: int
    payload: bytes
    destination_id: int
    ttl: int
    source: str


class LinkHealthTracker:
    def __init__(
        self,
        *,
        sender_id: int,
        peer_timeout_ms: int,
        degraded_after_ms: int,
        isolated_after_ms: int,
        move_after_ms: int,
        rtb_after_ms: int,
        recovery_hold_ms: int,
    ):
        self._sender_id = sender_id
        self._peer_timeout_s = peer_timeout_ms / 1000.0
        self._degraded_after_ms = degraded_after_ms
        self._isolated_after_ms = isolated_after_ms
        self._move_after_ms = move_after_ms
        self._rtb_after_ms = rtb_after_ms
        self._recovery_hold_s = recovery_hold_ms / 1000.0

        now = time.monotonic()
        self._state = LINK_STATE_NOMINAL
        self._last_link_seen_at = now
        self._recovery_started_at: float | None = None
        self._peers_last_seen: dict[int, float] = {}

    @property
    def state(self) -> str:
        return self._state

    def _prune_stale_peers(self, now: float) -> None:
        stale_cutoff = now - self._peer_timeout_s
        stale_peers = [
            peer_id
            for peer_id, last_seen_at in self._peers_last_seen.items()
            if last_seen_at < stale_cutoff
        ]
        for peer_id in stale_peers:
            del self._peers_last_seen[peer_id]

    def active_peer_count(self, now: float) -> int:
        self._prune_stale_peers(now)
        return len(self._peers_last_seen)

    def note_peer(self, peer_id: int, now: float) -> None:
        if peer_id == self._sender_id:
            return
        self._peers_last_seen[peer_id] = now
        self._last_link_seen_at = now
        self._prune_stale_peers(now)

    def evaluate(self, now: float) -> LinkHealthTransition | None:
        active_peers = self.active_peer_count(now)
        inactive_ms = max(0, int((now - self._last_link_seen_at) * 1000))
        next_state = self._state
        reason = "stable"

        if active_peers > 0:
            if self._state in {
                LINK_STATE_DEGRADED,
                LINK_STATE_ISOLATED,
                LINK_STATE_MOVING,
                LINK_STATE_RTB,
            }:
                if self._recovery_started_at is None:
                    self._recovery_started_at = now
                next_state = LINK_STATE_RECOVERY
                reason = "link_reestablished"
            elif self._state == LINK_STATE_RECOVERY:
                if self._recovery_started_at is None:
                    self._recovery_started_at = now
                if (now - self._recovery_started_at) >= self._recovery_hold_s:
                    self._recovery_started_at = None
                    next_state = LINK_STATE_NOMINAL
                    reason = "links_recovered"
                else:
                    next_state = LINK_STATE_RECOVERY
                    reason = "links_recovering"
            else:
                self._recovery_started_at = None
                next_state = LINK_STATE_NOMINAL
                reason = "links_healthy"
        else:
            self._recovery_started_at = None
            if inactive_ms >= self._rtb_after_ms:
                next_state = LINK_STATE_RTB
                reason = "no_link_after_threshold"
            elif inactive_ms >= self._move_after_ms:
                next_state = LINK_STATE_MOVING
                reason = "no_link_reposition"
            elif inactive_ms >= self._isolated_after_ms:
                next_state = LINK_STATE_ISOLATED
                reason = "all_routes_exhausted"
            elif inactive_ms >= self._degraded_after_ms:
                next_state = LINK_STATE_DEGRADED
                reason = "link_degrading"
            else:
                next_state = LINK_STATE_NOMINAL
                reason = "recent_link"

        if next_state == self._state:
            return None

        previous_state = self._state
        self._state = next_state
        return LinkHealthTransition(
            previous_state=previous_state,
            state=next_state,
            reason=reason,
            inactive_ms=inactive_ms,
            active_peers=active_peers,
        )


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


def _initial_seq() -> int:
    return secrets.randbelow(MAX_U32) + 1


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


def _parse_key_hex(value: str, *, section_name: str) -> bytes:
    try:
        key = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise ValueError(f"{section_name} key_hex must be valid hex") from exc
    if len(key) != CHACHA20_POLY1305_KEY_SIZE:
        raise ValueError(
            f"{section_name} key_hex must decode to "
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


def _route_v2_e2e_aad(
    *,
    origin_type: int,
    origin_id: int,
    destination_type: int,
    destination_id: int,
    origin_seq: int,
    traffic_class: int,
    inner_type: int,
    plaintext_len: int,
) -> bytes:
    return encode_route_v2_e2e_associated_data(
        origin_type=origin_type,
        origin_id=origin_id,
        destination_type=destination_type,
        destination_id=destination_id,
        origin_seq=origin_seq,
        traffic_class=traffic_class,
        inner_type=inner_type,
        inner_plaintext_len=plaintext_len,
    )


def _addr_label(address_type: int, address_id: int) -> str:
    if address_type == ADDR_BROADCAST:
        return "broadcast"
    return f"{address_type_name(address_type)}:{address_id}"


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:
        r1, g1, b1 = c, x, 0
    elif h < 120:
        r1, g1, b1 = x, c, 0
    elif h < 180:
        r1, g1, b1 = 0, c, x
    elif h < 240:
        r1, g1, b1 = 0, x, c
    elif h < 300:
        r1, g1, b1 = x, 0, c
    else:
        r1, g1, b1 = c, 0, x
    return (int((r1 + m) * 255), int((g1 + m) * 255), int((b1 + m) * 255))


def _build_image_frame(
    *,
    width: int,
    height: int,
    pixel_format: int,
    counter: int,
    style: str,
) -> bytes:
    if pixel_format == PIXEL_FMT_GRAYSCALE:
        pixels = bytearray(width * height)
        for y in range(height):
            for x in range(width):
                if style == "gradient":
                    hue = (counter * 13 + x * 7 + y * 3) % 360
                    _, _, v = _hsv_to_rgb(hue, 0.85, 0.5)
                    pixels[y * width + x] = v
                else:
                    pixels[y * width + x] = (counter + x + y) % 256
        return KOVA_IMAGE_MAGIC + struct.pack("!BBB", width, height, pixel_format) + bytes(pixels)

    pixels = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            if style == "gradient":
                hue = (counter * 13 + x * 7 + y * 3) % 360
                r, g, b = _hsv_to_rgb(hue, 0.85, 0.5 + 0.5 * math.cos(x / width * math.pi) * math.sin(y / height * math.pi))
            else:
                r = (counter * 7 + x * 3) % 256
                g = (counter * 11 + y * 5) % 256
                b = (counter * 17 + (x + y) * 2) % 256
            offset = (y * width + x) * 3
            pixels[offset] = r
            pixels[offset + 1] = g
            pixels[offset + 2] = b
    return KOVA_IMAGE_MAGIC + struct.pack("!BBB", width, height, pixel_format) + bytes(pixels)


def _normalize_ingest_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urllib.parse.urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("C2 HTTP forward URL must include scheme and host")
    if parsed.path in {"", "/"}:
        parsed = parsed._replace(path="/ingest")
    return urllib.parse.urlunparse(parsed)


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
        "origin_type",
        "destination_type",
        "traffic_class",
        "c2_http_forward_url",
        "dashboard_url",
        "local_control_bind_host",
        "message_type",
        "message",
        "message_file",
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
        "c2_id",
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
        "peer_timeout_ms",
        "link_degraded_after_ms",
        "link_isolated_after_ms",
        "link_move_after_ms",
        "link_rtb_after_ms",
        "link_recovery_hold_ms",
        "local_control_bind_port",
        "local_control_max_datagram_bytes",
    ):
        if key in section:
            out[key] = int(section[key], 0)

    for key in (
        "print_rssi",
        "include_self",
        "channel_agility",
        "channel_down_up",
        "sync_heartbeat",
        "message_file_reload",
        "status_auto",
        "local_control_enabled",
        "is_base",
    ):
        if key in section:
            out[key] = section.getboolean(key)

    if parser.has_section(LOCAL_CONTROL_SECTION):
        section = parser[LOCAL_CONTROL_SECTION]
        if "enabled" in section:
            out["local_control_enabled"] = section.getboolean("enabled")
        if "bind_host" in section:
            value = _optional_text(section.get("bind_host"))
            if value is not None:
                out["local_control_bind_host"] = value
        for key in ("bind_port", "max_datagram_bytes"):
            if key in section:
                out[f"local_control_{key}"] = int(section[key], 0)

    if parser.has_section(LOCAL_C2_TAP_SECTION):
        section = parser[LOCAL_C2_TAP_SECTION]
        if "enabled" in section:
            out["local_c2_tap_enabled"] = section.getboolean("enabled")
        if "target_host" in section:
            value = _optional_text(section.get("target_host"))
            if value is not None:
                out["local_c2_tap_target_host"] = value
        if "target_port" in section:
            out["local_c2_tap_target_port"] = int(section["target_port"], 0)

    if parser.has_section(C2_UPLINK_SECTION):
        section = parser[C2_UPLINK_SECTION]
        if "enabled" in section:
            out["c2_uplink_enabled"] = section.getboolean("enabled")
        for key in ("message_type", "message_template"):
            if key in section:
                value = _optional_text(section.get(key))
                if value is not None:
                    out[f"c2_uplink_{key}"] = value
        for key in ("interval_ms", "destination_id", "ttl", "start_counter"):
            if key in section:
                out[f"c2_uplink_{key}"] = int(section[key], 0)
        for key in ("image_width", "image_height"):
            if key in section:
                out[f"c2_uplink_{key}"] = int(section[key], 0)
        for key in ("image_format", "image_style"):
            if key in section:
                value = _optional_text(section.get(key))
                if value is not None:
                    out[f"c2_uplink_{key}"] = value

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

    for section_name, prefix in (
        (C2_UPLINK_CRYPTO_SECTION, "c2_uplink_crypto"),
        (C2_DOWNLINK_CRYPTO_SECTION, "c2_downlink_crypto"),
    ):
        if parser.has_section(section_name):
            section = parser[section_name]
            if "enabled" in section:
                out[f"{prefix}_enabled"] = section.getboolean("enabled")
            for key in ("security_domain", "key_hex"):
                if key in section:
                    value = _optional_text(section.get(key))
                    if value is not None:
                        out[f"{prefix}_{key}"] = value
            for key in ("key_id", "key_epoch", "replay_window"):
                if key in section:
                    out[f"{prefix}_{key}"] = int(section[key], 0)

    for section_name, prefix in (
        ("c2_uplink_key.", "c2_uplink_crypto_extra_keys"),
        ("c2_downlink_key.", "c2_downlink_crypto_extra_keys"),
    ):
        for parser_section_name in parser.sections():
            if not parser_section_name.startswith(section_name):
                continue
            section = parser[parser_section_name]
            if "enabled" in section and not section.getboolean("enabled"):
                continue
            key_spec: dict[str, object] = {"section_name": parser_section_name}
            for key in ("security_domain", "key_hex"):
                if key in section:
                    value = _optional_text(section.get(key))
                    if value is not None:
                        key_spec[key] = value
            for key in ("key_id", "key_epoch"):
                if key in section:
                    key_spec[key] = int(section[key], 0)
            out.setdefault(prefix, []).append(key_spec)

    return out


class SeenRoutes:
    def __init__(self, limit: int):
        self._limit = limit
        self._seen: set[tuple[int, ...]] = set()
        self._order: Deque[tuple[int, ...]] = deque()

    def remember(self, key: tuple[int, ...]) -> bool:
        if key in self._seen:
            return False

        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self._limit:
            old = self._order.popleft()
            self._seen.discard(old)
        return True

    def contains(self, key: tuple[int, ...]) -> bool:
        return key in self._seen


class ReplayWindow:
    def __init__(self, limit: int):
        self._limit = limit
        self._seen: set[tuple[int, ...]] = set()
        self._order: Deque[tuple[int, ...]] = deque()

    def remember(self, key: tuple[int, ...]) -> bool:
        if key in self._seen:
            return False

        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self._limit:
            old = self._order.popleft()
            self._seen.discard(old)
        return True


def _link_cost(rssi: float) -> int:
    return max(1, min(100, int(-rssi - 20)))


class RoutingTable:
    def __init__(self, is_base: bool):
        self._is_base = is_base
        self._entries: dict[int, tuple[int, int, float]] = {}

    def update(self, neighbor_id: int, cost: int, hops: int, now: float) -> None:
        self._entries[neighbor_id] = (cost, hops, now)

    def my_cost_to_base(self) -> int:
        if self._is_base:
            return 0
        entry = self._best_entry()
        return entry[0] if entry is not None else ROUTE_ADV_UNREACHABLE_COST

    def my_hops_to_base(self) -> int:
        if self._is_base:
            return 0
        entry = self._best_entry()
        return entry[1] if entry is not None else ROUTE_ADV_UNREACHABLE_HOPS

    def _best_entry(self) -> tuple[int, int] | None:
        reachable = [
            (c, h) for c, h, _ in self._entries.values()
            if c < ROUTE_ADV_UNREACHABLE_COST
        ]
        return min(reachable, key=lambda x: (x[0], x[1])) if reachable else None

    def prune_stale(self, now: float, ttl_s: float = 10.0) -> None:
        stale = [k for k, (_, __, t) in self._entries.items() if t < now - ttl_s]
        for k in stale:
            del self._entries[k]


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


def _encode_outer_route_v2_frame(
    *,
    sender_id: int,
    app_seq: int,
    route_payload: bytes,
) -> bytes:
    return encode_frame(
        sender_id=sender_id,
        message_type=MSG_ROUTE_V2,
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
        default=config_defaults.get("stream_id", DEFAULT_STREAM_ID),
    )
    parser.add_argument(
        "--sender-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("sender_id", _default_sender_id()),
        help="local sender id (default: $WFB_SENDER_ID or $SENDER_ID)",
    )
    parser.add_argument(
        "--origin-type",
        default=config_defaults.get("origin_type"),
        help="typed route origin type for route_v2: node or c2",
    )
    parser.add_argument(
        "--destination-type",
        default=config_defaults.get("destination_type"),
        help="typed route destination type: broadcast, node, or c2",
    )
    parser.add_argument(
        "--traffic-class",
        default=config_defaults.get("traffic_class", "mesh"),
        help="traffic class: mesh, c2_uplink, or c2_downlink",
    )
    parser.add_argument(
        "--c2-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("c2_id", DEFAULT_C2_ID),
        help="default C2 id used when traffic_class=c2_uplink",
    )
    parser.add_argument(
        "--c2-http-forward-url",
        default=config_defaults.get("c2_http_forward_url"),
        help="optional C2 /ingest URL for forwarding opaque c2_uplink route_v2 packets",
    )
    parser.add_argument(
        "--dashboard-url",
        default=config_defaults.get("dashboard_url"),
        help="base URL of dashboard_server (e.g. http://127.0.0.1:8765); "
             "mesh_txrx will POST route_data observations to /ingest so dashboard "
             "can run with --source feed instead of opening the NIC directly",
    )
    parser.add_argument(
        "--destination-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("destination_id", 0),
        help="target id; for legacy mesh, 0 broadcasts to all nodes",
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
        "--message-file",
        default=config_defaults.get("message_file"),
        help="optional binary payload file to originate (requires --message-type=data)",
    )
    parser.add_argument(
        "--message-file-reload",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("message_file_reload", False),
        help="reload --message-file before each send (useful for changing snapshots)",
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
    parser.add_argument(
        "--status-auto",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("status_auto", True),
        help="auto-generate binary status payloads when --message-type=status and no payload is set",
    )
    parser.add_argument(
        "--peer-timeout-ms",
        type=int,
        default=config_defaults.get("peer_timeout_ms", 12000),
        help="peer is treated stale after this many ms without any routed packet",
    )
    parser.add_argument(
        "--link-degraded-after-ms",
        type=int,
        default=config_defaults.get("link_degraded_after_ms", 5000),
        help="enter DEGRADED after this many ms without active peers",
    )
    parser.add_argument(
        "--link-isolated-after-ms",
        type=int,
        default=config_defaults.get("link_isolated_after_ms", 15000),
        help="enter ISOLATED after this many ms without active peers",
    )
    parser.add_argument(
        "--link-move-after-ms",
        type=int,
        default=config_defaults.get("link_move_after_ms", 30000),
        help="enter MOVING after this many ms without active peers",
    )
    parser.add_argument(
        "--link-rtb-after-ms",
        type=int,
        default=config_defaults.get("link_rtb_after_ms", 60000),
        help="enter RTB after this many ms without active peers",
    )
    parser.add_argument(
        "--link-recovery-hold-ms",
        type=int,
        default=config_defaults.get("link_recovery_hold_ms", 5000),
        help="time spent in RECOVERY before returning to NOMINAL",
    )
    parser.add_argument(
        "--local-control",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("local_control_enabled", False),
        help="listen for localhost UDP commands that originate C2 uplink packets",
    )
    parser.add_argument(
        "--local-control-bind-host",
        default=config_defaults.get("local_control_bind_host", "127.0.0.1"),
    )
    parser.add_argument(
        "--local-control-bind-port",
        type=int,
        default=config_defaults.get("local_control_bind_port", 0),
    )
    parser.add_argument(
        "--local-control-max-datagram-bytes",
        type=int,
        default=config_defaults.get("local_control_max_datagram_bytes", 8192),
    )
    parser.add_argument(
        "--local-c2-tap",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("local_c2_tap_enabled", False),
        help="send opaque c2_uplink route_v2 packets to a localhost gateway tap",
    )
    parser.add_argument(
        "--local-c2-tap-target-host",
        default=config_defaults.get("local_c2_tap_target_host", "127.0.0.1"),
    )
    parser.add_argument(
        "--local-c2-tap-target-port",
        type=int,
        default=config_defaults.get("local_c2_tap_target_port", 0),
    )
    parser.add_argument(
        "--c2-uplink",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("c2_uplink_enabled", False),
        help="periodically originate node_to_c2 payloads from this normal mesh node",
    )
    parser.add_argument(
        "--c2-uplink-interval-ms",
        type=int,
        default=config_defaults.get("c2_uplink_interval_ms", 1000),
    )
    parser.add_argument(
        "--c2-uplink-image-width",
        type=int,
        default=config_defaults.get("c2_uplink_image_width", 32),
    )
    parser.add_argument(
        "--c2-uplink-image-height",
        type=int,
        default=config_defaults.get("c2_uplink_image_height", 32),
    )
    parser.add_argument(
        "--c2-uplink-image-format",
        default=config_defaults.get("c2_uplink_image_format", "rgb"),
        help="pixel format: rgb or grayscale",
    )
    parser.add_argument(
        "--c2-uplink-image-style",
        default=config_defaults.get("c2_uplink_image_style", "gradient"),
        help="visual style: gradient or random",
    )
    parser.add_argument(
        "--c2-uplink-message-type",
        default=config_defaults.get("c2_uplink_message_type", "data"),
    )
    parser.add_argument(
        "--c2-uplink-destination-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("c2_uplink_destination_id", DEFAULT_C2_ID),
    )
    parser.add_argument(
        "--c2-uplink-ttl",
        type=lambda value: int(value, 0),
        default=config_defaults.get("c2_uplink_ttl", 2),
    )
    parser.add_argument(
        "--c2-uplink-start-counter",
        type=lambda value: int(value, 0),
        default=config_defaults.get("c2_uplink_start_counter", 1),
    )
    parser.add_argument(
        "--is-base",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("is_base", False),
        help="this node is the base station; advertises cost=0/hops=0 in route_adv",
    )
    args = parser.parse_args()

    if not args.iface:
        parser.error("--iface is required unless NIC, WFB_IFACE, or IFACE is set")
    if not 1 <= args.stream_id <= MAX_U32:
        parser.error("--stream-id must be in range 1..0xffffffff")
    if args.sender_id is None:
        parser.error("--sender-id is required unless WFB_SENDER_ID or SENDER_ID is set")
    if not 1 <= args.sender_id <= 255:
        parser.error("--sender-id must be in range 1..255")
    if not 1 <= args.c2_id <= 255:
        parser.error("--c2-id must be in range 1..255")
    if not 0 <= args.destination_id <= 255:
        parser.error("--destination-id must be in range 0..255")
    if not 0 <= args.ttl <= 255:
        parser.error("--ttl must be in range 0..255")

    try:
        traffic_class = traffic_class_value(args.traffic_class)
    except AppFrameError as exc:
        parser.error(str(exc))

    origin_type_default = (
        "c2" if traffic_class == TRAFFIC_C2_DOWNLINK else "node"
    )
    try:
        origin_type = address_type_value(args.origin_type or origin_type_default)
    except AppFrameError as exc:
        parser.error(str(exc))
    if origin_type == ADDR_BROADCAST:
        parser.error("--origin-type cannot be broadcast")

    destination_id = args.destination_id
    destination_type_default: str
    if traffic_class == TRAFFIC_C2_UPLINK:
        destination_type_default = "c2"
        if destination_id == 0:
            destination_id = args.c2_id
    elif traffic_class == TRAFFIC_C2_DOWNLINK:
        destination_type_default = "node"
    else:
        destination_type_default = "broadcast" if destination_id == 0 else "node"
    try:
        destination_type = address_type_value(
            args.destination_type or destination_type_default
        )
    except AppFrameError as exc:
        parser.error(str(exc))
    if destination_type == ADDR_BROADCAST and destination_id != 0:
        parser.error("broadcast destination_id must be 0")
    if destination_type != ADDR_BROADCAST and destination_id == 0:
        parser.error("non-broadcast destination_id must be non-zero")
    if traffic_class == TRAFFIC_C2_UPLINK and destination_type != ADDR_C2:
        parser.error("traffic_class=c2_uplink requires destination_type=c2")
    if traffic_class == TRAFFIC_C2_DOWNLINK and destination_type != ADDR_NODE:
        parser.error("traffic_class=c2_downlink requires destination_type=node")
    use_route_v2 = traffic_class != TRAFFIC_MESH
    args.destination_id = destination_id
    try:
        args.c2_http_forward_url = _normalize_ingest_url(args.c2_http_forward_url)
    except ValueError as exc:
        parser.error(str(exc))

    dashboard_ingest_url: str | None = None
    if args.dashboard_url:
        raw = args.dashboard_url.rstrip("/")
        parsed_dash = urllib.parse.urlparse(raw)
        if not parsed_dash.scheme or not parsed_dash.netloc:
            parser.error("--dashboard-url must include scheme and host")
        dashboard_ingest_url = raw + "/ingest"

    if args.count < 0:
        parser.error("--count must be >= 0")
    if args.tx_interval_ms < 0:
        parser.error("--tx-interval-ms must be >= 0")
    if args.message is not None and args.message_file is not None:
        parser.error("--message and --message-file are mutually exclusive")
    if args.message_file_reload and not args.message_file:
        parser.error("--message-file-reload requires --message-file")
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
    if args.peer_timeout_ms <= 0:
        parser.error("--peer-timeout-ms must be > 0")
    if args.link_degraded_after_ms <= 0:
        parser.error("--link-degraded-after-ms must be > 0")
    if args.link_isolated_after_ms <= args.link_degraded_after_ms:
        parser.error("--link-isolated-after-ms must be > --link-degraded-after-ms")
    if args.link_move_after_ms <= args.link_isolated_after_ms:
        parser.error("--link-move-after-ms must be > --link-isolated-after-ms")
    if args.link_rtb_after_ms <= args.link_move_after_ms:
        parser.error("--link-rtb-after-ms must be > --link-move-after-ms")
    if args.link_recovery_hold_ms < 0:
        parser.error("--link-recovery-hold-ms must be >= 0")
    if args.local_control:
        if not 1 <= args.local_control_bind_port <= 65535:
            parser.error(
                "--local-control-bind-port must be in range 1..65535 when enabled"
            )
        if args.local_control_max_datagram_bytes < 256:
            parser.error("--local-control-max-datagram-bytes must be >= 256")
    if args.local_c2_tap and not 1 <= args.local_c2_tap_target_port <= 65535:
        parser.error("--local-c2-tap-target-port must be in range 1..65535")
    if args.c2_uplink:
        if args.c2_uplink_interval_ms <= 0:
            parser.error("--c2-uplink-interval-ms must be > 0")
        if not 1 <= args.c2_uplink_destination_id <= 255:
            parser.error("--c2-uplink-destination-id must be in range 1..255")
        if not 0 <= args.c2_uplink_ttl <= 255:
            parser.error("--c2-uplink-ttl must be in range 0..255")
        if args.c2_uplink_start_counter < 0:
            parser.error("--c2-uplink-start-counter must be >= 0")
    if args.c2_uplink_image_width < 1 or args.c2_uplink_image_height < 1:
        parser.error("--c2-uplink-image-width/height must be > 0")
    if args.c2_uplink_image_format not in {"rgb", "grayscale"}:
        parser.error("--c2-uplink-image-format must be rgb or grayscale")
    if args.c2_uplink_image_style not in {"gradient", "random"}:
        parser.error("--c2-uplink-image-style must be gradient or random")

    try:
        inner_type = message_type_value(args.message_type)
    except AppFrameError as exc:
        parser.error(str(exc))
    if inner_type in {MSG_ROUTE_DATA, MSG_ROUTE_V2, MSG_ROUTE_ADV}:
        parser.error("--message-type cannot be a route packet type")
    if inner_type == MSG_SYNC:
        parser.error("--message-type sync is reserved for --sync-heartbeat")
    if args.message_file is not None and inner_type != MSG_DATA:
        parser.error("--message-file requires --message-type=data")

    try:
        c2_uplink_inner_type = message_type_value(args.c2_uplink_message_type)
    except AppFrameError as exc:
        parser.error(f"--c2-uplink-message-type: {exc}")
    if c2_uplink_inner_type in {MSG_ROUTE_DATA, MSG_ROUTE_V2, MSG_SYNC}:
        parser.error("--c2-uplink-message-type is not allowed")

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
            mesh_key = _parse_key_hex(
                str(key_hex),
                section_name="[mesh_crypto]",
            )
        except ValueError as exc:
            parser.error(str(exc))
        mesh_crypto = MeshCryptoConfig(
            security_domain=domain,
            key_id=key_id,
            key_epoch=key_epoch,
            key=mesh_key,
        )

    def load_e2e_crypto(
        *,
        prefix: str,
        section_name: str,
        default_domain: int,
    ) -> E2ECryptoConfig | None:
        enabled = bool(config_defaults.get(f"{prefix}_enabled", False))
        if not enabled:
            return None
        domain = security_domain_value(
            config_defaults.get(
                f"{prefix}_security_domain",
                security_domain_name(default_domain),
            )
        )
        if domain != default_domain:
            parser.error(
                f"{section_name} security_domain must be "
                f"{security_domain_name(default_domain)}"
            )
        key_id = int(config_defaults.get(f"{prefix}_key_id", 0))
        key_epoch = int(config_defaults.get(f"{prefix}_key_epoch", 0))
        key_hex = config_defaults.get(f"{prefix}_key_hex")
        if not 1 <= key_id <= MAX_U16:
            parser.error(f"{section_name} key_id must be in range 1..65535")
        if not 0 <= key_epoch <= MAX_U32:
            parser.error(f"{section_name} key_epoch must fit in u32")
        if not key_hex:
            parser.error(f"{section_name} key_hex is required when enabled=true")
        try:
            key = _parse_key_hex(str(key_hex), section_name=section_name)
        except ValueError as exc:
            parser.error(str(exc))
        return E2ECryptoConfig(
            security_domain=domain,
            key_id=key_id,
            key_epoch=key_epoch,
            key=key,
        )

    def load_e2e_crypto_spec(
        *,
        spec: dict[str, object],
        default_domain: int,
    ) -> E2ECryptoConfig:
        section_name = str(spec.get("section_name", "[e2e_key]"))
        domain = security_domain_value(
            spec.get("security_domain", security_domain_name(default_domain))
        )
        if domain != default_domain:
            parser.error(
                f"{section_name} security_domain must be "
                f"{security_domain_name(default_domain)}"
            )
        key_id = int(spec.get("key_id", 0))
        key_epoch = int(spec.get("key_epoch", 0))
        key_hex = spec.get("key_hex")
        if not 1 <= key_id <= MAX_U16:
            parser.error(f"{section_name} key_id must be in range 1..65535")
        if not 0 <= key_epoch <= MAX_U32:
            parser.error(f"{section_name} key_epoch must fit in u32")
        if not key_hex:
            parser.error(f"{section_name} key_hex is required")
        try:
            key = _parse_key_hex(str(key_hex), section_name=section_name)
        except ValueError as exc:
            parser.error(str(exc))
        return E2ECryptoConfig(
            security_domain=domain,
            key_id=key_id,
            key_epoch=key_epoch,
            key=key,
        )

    c2_uplink_crypto = load_e2e_crypto(
        prefix="c2_uplink_crypto",
        section_name="[c2_uplink_crypto]",
        default_domain=SEC_DOMAIN_NODE_TO_C2,
    )
    c2_downlink_crypto = load_e2e_crypto(
        prefix="c2_downlink_crypto",
        section_name="[c2_downlink_crypto]",
        default_domain=SEC_DOMAIN_C2_TO_NODE,
    )
    c2_uplink_keyring: dict[tuple[int, int, int], E2ECryptoConfig] = {}
    c2_downlink_keyring: dict[tuple[int, int, int], E2ECryptoConfig] = {}

    def add_key(
        keyring: dict[tuple[int, int, int], E2ECryptoConfig],
        crypto: E2ECryptoConfig | None,
    ) -> None:
        if crypto is None:
            return
        keyring[(crypto.security_domain, crypto.key_id, crypto.key_epoch)] = crypto

    add_key(c2_uplink_keyring, c2_uplink_crypto)
    add_key(c2_downlink_keyring, c2_downlink_crypto)
    for spec in config_defaults.get("c2_uplink_crypto_extra_keys", []):
        add_key(
            c2_uplink_keyring,
            load_e2e_crypto_spec(
                spec=spec,
                default_domain=SEC_DOMAIN_NODE_TO_C2,
            ),
        )
    for spec in config_defaults.get("c2_downlink_crypto_extra_keys", []):
        add_key(
            c2_downlink_keyring,
            load_e2e_crypto_spec(
                spec=spec,
                default_domain=SEC_DOMAIN_C2_TO_NODE,
            ),
        )
    has_originated_payload = (
        args.message is not None
        or args.message_file is not None
        or (args.status_auto and inner_type == MSG_STATUS)
    )
    if has_originated_payload:
        if traffic_class == TRAFFIC_C2_UPLINK and c2_uplink_crypto is None:
            parser.error(
                "traffic_class=c2_uplink requires [c2_uplink_crypto] enabled=true"
            )
        if traffic_class == TRAFFIC_C2_DOWNLINK and c2_downlink_crypto is None:
            parser.error(
                "traffic_class=c2_downlink requires [c2_downlink_crypto] enabled=true"
            )
    if args.local_control and c2_uplink_crypto is None:
        parser.error("[local_control] requires [c2_uplink_crypto] enabled=true")
    if args.c2_uplink and c2_uplink_crypto is None:
        parser.error("[c2_uplink] requires [c2_uplink_crypto] enabled=true")

    seen = SeenRoutes(args.seen_limit)
    secure_replay = ReplayWindow(mesh_replay_window)
    pending_local_c2: Deque[LocalC2Message] = deque()
    c2_uplink_counter = int(args.c2_uplink_start_counter)
    message_file_path = Path(args.message_file) if args.message_file is not None else None

    def read_message_file(path: Path) -> bytes:
        payload = path.read_bytes()
        if len(payload) > MAX_U16:
            raise ValueError(
                f"message file payload must fit in u16 length, got {len(payload)} bytes"
            )
        return payload

    if args.message is not None:
        static_inner_payload: bytes | None = args.message.encode("utf-8")
    elif message_file_path is not None:
        try:
            static_inner_payload = read_message_file(message_file_path)
        except OSError as exc:
            parser.error(f"--message-file read error: {exc}")
        except ValueError as exc:
            parser.error(str(exc))
    else:
        static_inner_payload = None

    auto_status_payload = (
        args.status_auto
        and inner_type == MSG_STATUS
        and static_inner_payload is None
        and message_file_path is None
    )

    if args.count > 0 and static_inner_payload is None and not auto_status_payload:
        parser.error("--count requires --message, --message-file, or --status-auto")

    link_health = LinkHealthTracker(
        sender_id=args.sender_id,
        peer_timeout_ms=args.peer_timeout_ms,
        degraded_after_ms=args.link_degraded_after_ms,
        isolated_after_ms=args.link_isolated_after_ms,
        move_after_ms=args.link_move_after_ms,
        rtb_after_ms=args.link_rtb_after_ms,
        recovery_hold_ms=args.link_recovery_hold_ms,
    )
    routing_table = RoutingTable(args.is_base)
    _peer_rssi: dict[int, float] = {}
    next_route_adv_at = time.monotonic()
    started_at = time.monotonic()
    next_origin_seq = _initial_seq()
    next_outer_seq = 1
    next_rf_seq = 1
    originated_count = 0
    next_tx_at = time.monotonic()
    next_c2_uplink_at = time.monotonic()
    next_sync_at = time.monotonic()
    tx_guard_until = 0.0
    current_channel: int | None = None
    tx: Tx | None = None
    rx: Rx | None = None
    local_control_socket: socket.socket | None = None
    local_c2_tap_socket: socket.socket | None = None

    def log(message: str) -> None:
        channel = "?" if current_channel is None else str(current_channel)
        print(f"CH={channel} {message}")

    def decode_local_c2_message(data: bytes, source: str) -> LocalC2Message:
        try:
            request = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppFrameError("local control datagram must be a JSON object") from exc
        if not isinstance(request, dict):
            raise AppFrameError("local control datagram must be a JSON object")

        request_traffic_class = traffic_class_value(
            request.get("traffic_class", "c2_uplink")
        )
        if request_traffic_class != TRAFFIC_C2_UPLINK:
            raise AppFrameError("local control currently supports only c2_uplink")

        request_destination_type = address_type_value(
            request.get("destination_type", "c2")
        )
        if request_destination_type != ADDR_C2:
            raise AppFrameError("local control c2_uplink requires destination_type=c2")

        request_inner_type = message_type_value(request.get("message_type", "data"))
        if request_inner_type in {MSG_ROUTE_DATA, MSG_ROUTE_V2, MSG_SYNC}:
            raise AppFrameError("local control message_type is not allowed")

        payload_hex = request.get("payload_hex")
        if payload_hex is not None:
            if not isinstance(payload_hex, str):
                raise AppFrameError("payload_hex must be a string")
            try:
                payload = bytes.fromhex(payload_hex)
            except ValueError as exc:
                raise AppFrameError("payload_hex must be valid hex") from exc
        else:
            message = request.get("message")
            if not isinstance(message, str):
                raise AppFrameError("local control requires message or payload_hex")
            payload = message.encode("utf-8")

        if len(payload) > MAX_U16:
            raise AppFrameError(f"local control payload too large: {len(payload)}")

        destination = int(request.get("destination_id", args.c2_id))
        if not 1 <= destination <= 255:
            raise AppFrameError("destination_id must be in range 1..255")
        ttl = int(request.get("ttl", args.ttl))
        if not 0 <= ttl <= 255:
            raise AppFrameError("ttl must be in range 0..255")

        return LocalC2Message(
            inner_type=request_inner_type,
            payload=payload,
            destination_id=destination,
            ttl=ttl,
            source=source,
        )

    def drain_local_control_socket() -> None:
        if local_control_socket is None:
            return
        while True:
            try:
                data, peer = local_control_socket.recvfrom(
                    args.local_control_max_datagram_bytes
                )
            except BlockingIOError:
                return
            except OSError as exc:
                log(f'LOCAL_CONTROL recv_error="{exc}"')
                return

            source = f"{peer[0]}:{peer[1]}"
            try:
                message = decode_local_c2_message(data, source)
            except (AppFrameError, ValueError) as exc:
                log(f'LOCAL_CONTROL reject from={source} error="{exc}"')
                continue
            pending_local_c2.append(message)
            log(
                f"LOCAL_CONTROL queued from={source} "
                f"type={message_type_name(message.inner_type)} "
                f"dest=c2:{message.destination_id} ttl={message.ttl} "
                f"len={len(message.payload)} pending={len(pending_local_c2)}"
            )

    def status_flags(now: float) -> int:
        flags = STATUS_FLAG_FORWARDING_ENABLED
        if mesh_crypto is not None:
            flags |= STATUS_FLAG_CRYPTO_ENABLED
        if link_health.state in {
            LINK_STATE_DEGRADED,
            LINK_STATE_ISOLATED,
            LINK_STATE_MOVING,
            LINK_STATE_RTB,
        }:
            flags |= STATUS_FLAG_DEGRADED_LINK
        return flags

    def build_auto_status_payload(now: float) -> bytes:
        uptime_s = int(max(0, now - started_at))
        peer_count = min(255, link_health.active_peer_count(now))
        return encode_status_payload(
            uptime_s=uptime_s,
            battery_pct=None,
            peer_count=peer_count,
            flags=status_flags(now),
        )

    def build_origin_payload(now: float) -> bytes | None:
        if auto_status_payload:
            return build_auto_status_payload(now)
        if message_file_path is not None and args.message_file_reload:
            return read_message_file(message_file_path)
        return static_inner_payload

    def build_c2_uplink_payload(counter: int) -> bytes:
        pixel_format = PIXEL_FMT_RGB if args.c2_uplink_image_format == "rgb" else PIXEL_FMT_GRAYSCALE
        frame = _build_image_frame(
            width=args.c2_uplink_image_width,
            height=args.c2_uplink_image_height,
            pixel_format=pixel_format,
            counter=counter,
            style=args.c2_uplink_image_style,
        )
        if len(frame) > MAX_U16:
            raise ValueError(f"c2 uplink image payload too large: {len(frame)}")
        return frame

    def close_radio() -> None:
        nonlocal tx, rx
        if rx is not None:
            rx.close()
            rx = None
        if tx is not None:
            tx.close()
            tx = None

    def open_local_control() -> None:
        nonlocal local_control_socket
        if not args.local_control:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((args.local_control_bind_host, args.local_control_bind_port))
        sock.setblocking(False)
        local_control_socket = sock
        log(
            f"LOCAL_CONTROL listening "
            f"addr={args.local_control_bind_host}:{args.local_control_bind_port}"
        )

    def close_local_control() -> None:
        nonlocal local_control_socket
        if local_control_socket is not None:
            local_control_socket.close()
            local_control_socket = None

    def open_local_c2_tap() -> None:
        nonlocal local_c2_tap_socket
        if not args.local_c2_tap:
            return
        local_c2_tap_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log(
            f"LOCAL_C2_TAP target="
            f"{args.local_c2_tap_target_host}:{args.local_c2_tap_target_port}"
        )

    def close_local_c2_tap() -> None:
        nonlocal local_c2_tap_socket
        if local_c2_tap_socket is not None:
            local_c2_tap_socket.close()
            local_c2_tap_socket = None

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

    def e2e_crypto_for_outbound(route_traffic_class: int) -> E2ECryptoConfig | None:
        if route_traffic_class == TRAFFIC_C2_UPLINK:
            return c2_uplink_crypto
        if route_traffic_class == TRAFFIC_C2_DOWNLINK:
            return c2_downlink_crypto
        return None

    def e2e_crypto_for_inbound(route, secure) -> E2ECryptoConfig | None:
        key_id = (secure.security_domain, secure.key_id, secure.key_epoch)
        if route.traffic_class == TRAFFIC_C2_UPLINK:
            if route.destination_type == ADDR_C2 and origin_type == ADDR_C2:
                return c2_uplink_keyring.get(key_id)
            return None
        if route.traffic_class == TRAFFIC_C2_DOWNLINK:
            if route.destination_type == ADDR_NODE and origin_type == ADDR_NODE:
                return c2_downlink_keyring.get(key_id)
            return None
        return None

    def validate_secure_key(
        *,
        secure,
        crypto: E2ECryptoConfig,
    ) -> None:
        if secure.security_domain != crypto.security_domain:
            raise AppFrameError(
                "unexpected security_domain: "
                f"{secure.security_domain_name} "
                f"expected={security_domain_name(crypto.security_domain)}"
            )
        if secure.key_id != crypto.key_id:
            raise AppFrameError(
                f"unexpected key_id: {secure.key_id} expected={crypto.key_id}"
            )
        if secure.key_epoch != crypto.key_epoch:
            raise AppFrameError(
                "unexpected key_epoch: "
                f"{secure.key_epoch} expected={crypto.key_epoch}"
            )

    def encrypt_e2e_payload(
        *,
        route_origin_type: int,
        route_origin_id: int,
        route_destination_type: int,
        route_destination_id: int,
        route_origin_seq: int,
        route_traffic_class: int,
        route_inner_type: int,
        plaintext: bytes,
    ) -> tuple[bytes, E2ECryptoConfig]:
        crypto = e2e_crypto_for_outbound(route_traffic_class)
        if crypto is None:
            raise AppFrameError(
                f"no E2E crypto configured for "
                f"traffic_class={traffic_class_name(route_traffic_class)}"
            )
        aad = _route_v2_e2e_aad(
            origin_type=route_origin_type,
            origin_id=route_origin_id,
            destination_type=route_destination_type,
            destination_id=route_destination_id,
            origin_seq=route_origin_seq,
            traffic_class=route_traffic_class,
            inner_type=route_inner_type,
            plaintext_len=len(plaintext),
        )
        return (
            encode_secure_payload(
                key=crypto.key,
                security_domain=crypto.security_domain,
                key_id=crypto.key_id,
                key_epoch=crypto.key_epoch,
                plaintext=plaintext,
                associated_data=aad,
            ),
            crypto,
        )

    def route_v2_is_own(route) -> bool:
        return route.origin_type == origin_type and route.origin_id == args.sender_id

    def route_v2_delivered(route) -> bool:
        if route.destination_type == ADDR_BROADCAST:
            return True
        return (
            route.destination_type == origin_type
            and route.destination_id == args.sender_id
        )

    def decrypt_e2e_payload(route) -> tuple[bytes, object]:
        secure = decode_secure_payload(route.inner_payload)
        crypto = e2e_crypto_for_inbound(route, secure)
        if crypto is None:
            raise AppFrameError("no matching local E2E key for this endpoint")
        validate_secure_key(secure=secure, crypto=crypto)
        aad = _route_v2_e2e_aad(
            origin_type=route.origin_type,
            origin_id=route.origin_id,
            destination_type=route.destination_type,
            destination_id=route.destination_id,
            origin_seq=route.origin_seq,
            traffic_class=route.traffic_class,
            inner_type=route.inner_type,
            plaintext_len=secure.plaintext_len,
        )
        plaintext = decrypt_secure_payload(
            secure,
            key=crypto.key,
            associated_data=aad,
        )
        replay_key = (
            secure.security_domain,
            route.origin_type,
            route.origin_id,
            secure.key_epoch,
            route.origin_seq,
        )
        if not secure_replay.remember(replay_key):
            raise AppFrameError("secure replay detected")
        return plaintext, secure

    def build_c2_upload(route) -> dict[str, object]:
        return {
            "gateway_id": args.sender_id,
            "channel": current_channel,
            "received_at_ms": _utc_ms(),
            "route": {
                "origin_type": route.origin_type_name,
                "origin_id": route.origin_id,
                "destination_type": route.destination_type_name,
                "destination_id": route.destination_id,
                "ttl": route.ttl,
                "origin_seq": route.origin_seq,
                "traffic_class": route.traffic_class_name,
                "inner_type": route.inner_type_name,
                "inner_payload_hex": route.inner_payload.hex(),
            },
        }

    def emit_local_c2_tap(route, *, reason: str) -> None:
        if local_c2_tap_socket is None:
            return
        if route.traffic_class != TRAFFIC_C2_UPLINK or route.destination_type != ADDR_C2:
            return

        body = json.dumps(build_c2_upload(route), separators=(",", ":")).encode("utf-8")
        try:
            local_c2_tap_socket.sendto(
                body,
                (args.local_c2_tap_target_host, args.local_c2_tap_target_port),
            )
        except OSError as exc:
            log(
                f'LOCAL_C2_TAP send_error origin={route.origin_id} '
                f'seq={route.origin_seq} reason={reason} error="{exc}"'
            )
            return
        log(
            f"LOCAL_C2_TAP sent origin={route.origin_id} "
            f"seq={route.origin_seq} dest=c2:{route.destination_id} "
            f"reason={reason} bytes={len(body)}"
        )

    def post_c2_upload(route) -> tuple[bool, str]:
        if args.c2_http_forward_url is None:
            return False, "disabled"
        if route.traffic_class != TRAFFIC_C2_UPLINK or route.destination_type != ADDR_C2:
            return False, "not_c2_uplink"

        body = json.dumps(build_c2_upload(route), separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            args.c2_http_forward_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                response_body = response.read(4096).decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            return False, str(exc)

        if not 200 <= response.status < 300:
            return False, f"http_status={response.status} body={response_body}"
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError:
            return True, f"http_status={response.status}"
        duplicate = int(bool(parsed.get("duplicate", False)))
        return True, f"http_status={response.status} duplicate={duplicate}"

    def post_route_observation(
        *,
        origin_id: int,
        via_id: int,
        seq: int,
        inner_type: int,
        payload: bytes,
        rssi: int | None,
    ) -> None:
        if dashboard_ingest_url is None:
            return
        body = json.dumps(
            {
                "origin_id": origin_id,
                "via_id": via_id,
                "seq": seq,
                "inner_type": message_type_name(inner_type),
                "payload_hex": payload.hex(),
                "rssi": rssi,
                "freq": None,
                "bandwidth": None,
                "mcs_index": None,
                "source": "radio",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            dashboard_ingest_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=0.5) as _:
                pass
        except (OSError, urllib.error.URLError):
            pass

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

    def send_route_v2(
        *,
        inner_type: int,
        payload: bytes,
        destination_type: int,
        destination_id: int,
        ttl: int,
        traffic_class: int,
    ) -> tuple[int, E2ECryptoConfig, object]:
        nonlocal next_origin_seq, next_outer_seq, next_rf_seq
        if tx is None:
            raise RuntimeError("TX handle is not open")

        seq_sent = next_origin_seq
        e2e_payload, crypto = encrypt_e2e_payload(
            route_origin_type=origin_type,
            route_origin_id=args.sender_id,
            route_destination_type=destination_type,
            route_destination_id=destination_id,
            route_origin_seq=seq_sent,
            route_traffic_class=traffic_class,
            route_inner_type=inner_type,
            plaintext=payload,
        )
        route_payload = encode_route_v2_payload(
            origin_type=origin_type,
            origin_id=args.sender_id,
            destination_type=destination_type,
            destination_id=destination_id,
            ttl=ttl,
            origin_seq=seq_sent,
            traffic_class=traffic_class,
            inner_type=inner_type,
            inner_payload=e2e_payload,
        )
        route = decode_route_v2_payload(route_payload)
        frame = _encode_outer_route_v2_frame(
            sender_id=args.sender_id,
            app_seq=next_outer_seq,
            route_payload=route_payload,
        )
        tx.send(frame, seq=next_rf_seq)
        seen.remember((origin_type, args.sender_id, seq_sent))
        next_origin_seq = _next_seq(next_origin_seq)
        next_outer_seq = _next_seq(next_outer_seq)
        next_rf_seq = _next_seq(next_rf_seq)
        return seq_sent, crypto, route

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
        open_local_control()
        open_local_c2_tap()

        hop_desc = ",".join(str(channel) for channel in hop_channels)
        agility_desc = f" agility={hop_desc}" if args.channel_agility else ""
        log(
            f"Mesh UDP mode: sender={args.sender_id} ttl={args.ttl} "
            f"dest={args.destination_id} class={traffic_class_name(traffic_class)}"
            f"{agility_desc}, Ctrl-C to exit"
        )
        if auto_status_payload:
            payload_desc = "auto-status"
        elif message_file_path is not None:
            payload_desc = f"file:{message_file_path}"
        elif static_inner_payload is not None:
            payload_desc = "text-message"
        else:
            payload_desc = "rx-forward-only"
        log(
            f"ORIGIN type={args.message_type} payload_source={payload_desc} "
            f"peer_timeout_ms={args.peer_timeout_ms} "
            f"degraded_ms={args.link_degraded_after_ms} "
            f"isolated_ms={args.link_isolated_after_ms} "
            f"moving_ms={args.link_move_after_ms} rtb_ms={args.link_rtb_after_ms}"
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

            drain_local_control_socket()
            now = time.monotonic()
            transition = link_health.evaluate(now)
            if transition is not None:
                log(
                    f"LINK state={transition.state} prev={transition.previous_state} "
                    f"reason={transition.reason} inactive_ms={transition.inactive_ms} "
                    f"active_peers={transition.active_peers}"
                )
            tx_allowed = now >= tx_guard_until
            try:
                origin_payload = build_origin_payload(now)
            except (OSError, ValueError) as exc:
                log(f'PAYLOAD source_error="{exc}"')
                next_tx_at = now + (args.tx_interval_ms / 1000.0)
                origin_payload = None
            should_send_c2_uplink = (
                args.c2_uplink
                and now >= next_c2_uplink_at
                and tx_allowed
            )
            if should_send_c2_uplink:
                try:
                    c2_payload = build_c2_uplink_payload(c2_uplink_counter)
                    sent_seq, e2e_crypto, sent_route = send_route_v2(
                        inner_type=c2_uplink_inner_type,
                        payload=c2_payload,
                        destination_type=ADDR_C2,
                        destination_id=args.c2_uplink_destination_id,
                        ttl=args.c2_uplink_ttl,
                        traffic_class=TRAFFIC_C2_UPLINK,
                    )
                except (AppFrameError, ValueError, KeyError) as exc:
                    log(f'C2_UPLINK send_error counter={c2_uplink_counter} error="{exc}"')
                else:
                    log(
                        f"TX e2e origin={_addr_label(origin_type, args.sender_id)} "
                        f"seq={sent_seq} "
                        f"dest={_addr_label(ADDR_C2, args.c2_uplink_destination_id)} "
                        f"class={traffic_class_name(TRAFFIC_C2_UPLINK)} "
                        f"domain={security_domain_name(e2e_crypto.security_domain)} "
                        f"key_id={e2e_crypto.key_id} "
                        f"key_epoch={e2e_crypto.key_epoch} "
                        f"len={len(c2_payload)} source=c2_uplink "
                        f"counter={c2_uplink_counter}"
                    )
                    log(
                        f"TX route_v2 "
                        f"origin={_addr_label(origin_type, args.sender_id)} "
                        f"seq={sent_seq} "
                        f"dest={_addr_label(ADDR_C2, args.c2_uplink_destination_id)} "
                        f"ttl={args.c2_uplink_ttl} "
                        f"class={traffic_class_name(TRAFFIC_C2_UPLINK)} "
                        f"type={message_type_name(c2_uplink_inner_type)} "
                        f"len={len(c2_payload)} e2e=1 source=c2_uplink "
                        f"counter={c2_uplink_counter}"
                    )
                    emit_local_c2_tap(sent_route, reason="c2_uplink")
                    if args.c2_http_forward_url is not None:
                        ok, detail = post_c2_upload(sent_route)
                        log(
                            f"HTTP c2_forward origin={sent_route.origin_id} "
                            f"seq={sent_route.origin_seq} "
                            f"dest={_addr_label(sent_route.destination_type, sent_route.destination_id)} "
                            f"ok={int(ok)} detail=\"{detail}\""
                        )
                    c2_uplink_counter += 1
                next_c2_uplink_at = now + (args.c2_uplink_interval_ms / 1000.0)
            if pending_local_c2 and tx_allowed:
                control = pending_local_c2.popleft()
                try:
                    sent_seq, e2e_crypto, sent_route = send_route_v2(
                        inner_type=control.inner_type,
                        payload=control.payload,
                        destination_type=ADDR_C2,
                        destination_id=control.destination_id,
                        ttl=control.ttl,
                        traffic_class=TRAFFIC_C2_UPLINK,
                    )
                except AppFrameError as exc:
                    log(
                        f'LOCAL_CONTROL send_error source={control.source} '
                        f'error="{exc}"'
                    )
                else:
                    log(
                        f"TX e2e origin={_addr_label(origin_type, args.sender_id)} "
                        f"seq={sent_seq} "
                        f"dest={_addr_label(ADDR_C2, control.destination_id)} "
                        f"class={traffic_class_name(TRAFFIC_C2_UPLINK)} "
                        f"domain={security_domain_name(e2e_crypto.security_domain)} "
                        f"key_id={e2e_crypto.key_id} "
                        f"key_epoch={e2e_crypto.key_epoch} "
                        f"len={len(control.payload)} source=local_control"
                    )
                    log(
                        f"TX route_v2 "
                        f"origin={_addr_label(origin_type, args.sender_id)} "
                        f"seq={sent_seq} "
                        f"dest={_addr_label(ADDR_C2, control.destination_id)} "
                        f"ttl={control.ttl} "
                        f"class={traffic_class_name(TRAFFIC_C2_UPLINK)} "
                        f"type={message_type_name(control.inner_type)} "
                        f"len={len(control.payload)} e2e=1 source=local_control"
                    )
                    emit_local_c2_tap(sent_route, reason="local_control")
                    if args.c2_http_forward_url is not None:
                        ok, detail = post_c2_upload(sent_route)
                        log(
                            f"HTTP c2_forward origin={sent_route.origin_id} "
                            f"seq={sent_route.origin_seq} "
                            f"dest={_addr_label(sent_route.destination_type, sent_route.destination_id)} "
                            f"ok={int(ok)} detail=\"{detail}\""
                        )
            should_originate = (
                origin_payload is not None
                and (args.count == 0 or originated_count < args.count)
                and now >= next_tx_at
                and tx_allowed
            )
            if should_originate:
                if use_route_v2:
                    sent_seq, e2e_crypto, sent_route = send_route_v2(
                        inner_type=inner_type,
                        payload=origin_payload,
                        destination_type=destination_type,
                        destination_id=args.destination_id,
                        ttl=args.ttl,
                        traffic_class=traffic_class,
                    )
                    log(
                        f"TX e2e origin={_addr_label(origin_type, args.sender_id)} "
                        f"seq={sent_seq} "
                        f"dest={_addr_label(destination_type, args.destination_id)} "
                        f"class={traffic_class_name(traffic_class)} "
                        f"domain={security_domain_name(e2e_crypto.security_domain)} "
                        f"key_id={e2e_crypto.key_id} "
                        f"key_epoch={e2e_crypto.key_epoch} "
                        f"len={len(origin_payload)}"
                    )
                    log(
                        f"TX route_v2 "
                        f"origin={_addr_label(origin_type, args.sender_id)} "
                        f"seq={sent_seq} "
                        f"dest={_addr_label(destination_type, args.destination_id)} "
                        f"ttl={args.ttl} class={traffic_class_name(traffic_class)} "
                        f"type={args.message_type} len={len(origin_payload)} "
                        "e2e=1"
                    )
                    emit_local_c2_tap(sent_route, reason="originated")
                    if (
                        args.c2_http_forward_url is not None
                        and sent_route.traffic_class == TRAFFIC_C2_UPLINK
                    ):
                        ok, detail = post_c2_upload(sent_route)
                        log(
                            f"HTTP c2_forward origin={sent_route.origin_id} "
                            f"seq={sent_route.origin_seq} "
                            f"dest={_addr_label(sent_route.destination_type, sent_route.destination_id)} "
                            f"ok={int(ok)} detail=\"{detail}\""
                        )
                else:
                    effective_ttl = args.ttl
                    if not args.is_base:
                        my_hops = routing_table.my_hops_to_base()
                        if my_hops < ROUTE_ADV_UNREACHABLE_HOPS:
                            effective_ttl = min(args.ttl, my_hops + 1)
                    sent_seq, secured = send_route(
                        inner_type=inner_type,
                        payload=origin_payload,
                        destination_id=args.destination_id,
                        ttl=effective_ttl,
                    )
                    if secured and mesh_crypto is not None:
                        log(
                            f"TX secure origin={args.sender_id} seq={sent_seq} "
                            f"domain={security_domain_name(mesh_crypto.security_domain)} "
                            f"key_id={mesh_crypto.key_id} "
                            f"key_epoch={mesh_crypto.key_epoch} len={len(origin_payload)}"
                        )
                    log(
                        f"TX route origin={args.sender_id} seq={sent_seq} "
                        f"dest={args.destination_id} ttl={args.ttl} "
                        f"type={args.message_type} len={len(origin_payload)} "
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

            should_route_adv = now >= next_route_adv_at and tx_allowed
            if should_route_adv:
                routing_table.prune_stale(now)
                adv_cost = routing_table.my_cost_to_base()
                adv_hops = routing_table.my_hops_to_base()
                adv_payload = encode_route_adv_payload(
                    cost_to_base=adv_cost,
                    hops_to_base=adv_hops,
                )
                try:
                    sent_seq, _ = send_route(
                        inner_type=MSG_ROUTE_ADV,
                        payload=adv_payload,
                        destination_id=0,
                        ttl=0,
                    )
                except (AppFrameError, ValueError) as exc:
                    log(f'ROUTE_ADV send_error="{exc}"')
                else:
                    log(
                        f"TX route_adv origin={args.sender_id} seq={sent_seq} "
                        f"cost={adv_cost} hops={adv_hops}"
                    )
                next_route_adv_at = now + (args.tx_interval_ms / 1000.0)

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

            if frame.message_type == MSG_ROUTE_V2:
                try:
                    route_v2 = decode_route_v2_payload(frame.payload)
                except AppFrameError as exc:
                    log(
                        f'RX invalid_route_v2 from={frame.sender_id} '
                        f'len={len(frame.payload)} error="{exc}"'
                    )
                    continue

                link_health.note_peer(frame.sender_id, time.monotonic())
                prev_rssi = _peer_rssi.get(frame.sender_id, float(meta.rssi[0]))
                _peer_rssi[frame.sender_id] = prev_rssi * 0.7 + meta.rssi[0] * 0.3
                origin_label = _addr_label(route_v2.origin_type, route_v2.origin_id)
                dest_label = _addr_label(
                    route_v2.destination_type,
                    route_v2.destination_id,
                )
                if route_v2_is_own(route_v2):
                    seen.remember(route_v2.dedupe_key)
                    log(
                        f"RX own_route_v2 via={frame.sender_id} "
                        f"origin={origin_label} seq={route_v2.origin_seq} "
                        "dropped=1"
                    )
                    continue

                if seen.contains(route_v2.dedupe_key):
                    log(
                        f"RX duplicate_route_v2 via={frame.sender_id} "
                        f"origin={origin_label} seq={route_v2.origin_seq} "
                        "dropped=1"
                    )
                    continue

                try:
                    secure = decode_secure_payload(route_v2.inner_payload)
                except AppFrameError as exc:
                    log(
                        f'RX invalid_e2e via={frame.sender_id} '
                        f"origin={origin_label} seq={route_v2.origin_seq} "
                        f"class={route_v2.traffic_class_name} "
                        f'error="{exc}" dropped=1'
                    )
                    continue

                delivered_v2 = route_v2_delivered(route_v2)
                suffix = ""
                if args.print_rssi:
                    suffix = (
                        f" bw={meta.bandwidth} mcs={meta.mcs_index} "
                        f"rssi0={meta.rssi[0]}"
                    )

                if delivered_v2 and e2e_crypto_for_inbound(route_v2, secure) is not None:
                    try:
                        route_payload, secure = decrypt_e2e_payload(route_v2)
                    except AppFrameError as exc:
                        log(
                            f'RX auth_fail_v2 via={frame.sender_id} '
                            f"origin={origin_label} seq={route_v2.origin_seq} "
                            f"dest={dest_label} class={route_v2.traffic_class_name} "
                            f'type={route_v2.inner_type_name} error="{exc}" '
                            "dropped=1"
                        )
                        continue

                    seen.remember(route_v2.dedupe_key)
                    text = route_payload.decode("utf-8", errors="replace")
                    log(
                        f"RX e2e via={frame.sender_id} origin={origin_label} "
                        f"seq={route_v2.origin_seq} dest={dest_label} "
                        f"ttl={route_v2.ttl} class={route_v2.traffic_class_name} "
                        f"type={route_v2.inner_type_name} "
                        f"domain={secure.security_domain_name} "
                        f"key_id={secure.key_id} key_epoch={secure.key_epoch} "
                        f'decrypted=1 payload="{text}"{suffix}'
                    )
                else:
                    seen.remember(route_v2.dedupe_key)
                    log(
                        f"RX opaque_route via={frame.sender_id} "
                        f"origin={origin_label} seq={route_v2.origin_seq} "
                        f"dest={dest_label} ttl={route_v2.ttl} "
                        f"class={route_v2.traffic_class_name} "
                        f"type={route_v2.inner_type_name} "
                        f"domain={secure.security_domain_name} "
                        f"key_id={secure.key_id} key_epoch={secure.key_epoch} "
                        f"delivered={int(delivered_v2)} decrypt_skipped=1{suffix}"
                    )

                if (
                    args.c2_http_forward_url is not None
                    and route_v2.traffic_class == TRAFFIC_C2_UPLINK
                ):
                    ok, detail = post_c2_upload(route_v2)
                    log(
                        f"HTTP c2_forward origin={origin_label} "
                        f"seq={route_v2.origin_seq} dest={dest_label} "
                        f"ok={int(ok)} detail=\"{detail}\""
                    )
                emit_local_c2_tap(route_v2, reason="received")

                if route_v2.ttl <= 0:
                    continue

                forwarded = route_v2.decremented_ttl()
                forward_payload = encode_route_v2_payload(
                    origin_type=forwarded.origin_type,
                    origin_id=forwarded.origin_id,
                    destination_type=forwarded.destination_type,
                    destination_id=forwarded.destination_id,
                    ttl=forwarded.ttl,
                    origin_seq=forwarded.origin_seq,
                    traffic_class=forwarded.traffic_class,
                    inner_type=forwarded.inner_type,
                    inner_payload=forwarded.inner_payload,
                )
                forward_frame = _encode_outer_route_v2_frame(
                    sender_id=args.sender_id,
                    app_seq=next_outer_seq,
                    route_payload=forward_payload,
                )
                tx.send(forward_frame, seq=next_rf_seq)
                log(
                    f"TX forward_v2 origin={origin_label} "
                    f"seq={forwarded.origin_seq} dest={dest_label} "
                    f"ttl={forwarded.ttl} class={forwarded.traffic_class_name} "
                    "opaque=1"
                )
                next_outer_seq = _next_seq(next_outer_seq)
                next_rf_seq = _next_seq(next_rf_seq)
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

            link_health.note_peer(frame.sender_id, time.monotonic())
            prev_rssi = _peer_rssi.get(frame.sender_id, float(meta.rssi[0]))
            _peer_rssi[frame.sender_id] = prev_rssi * 0.7 + meta.rssi[0] * 0.3

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
                elif route.inner_type == MSG_STATUS:
                    try:
                        status = decode_status_payload(route_payload)
                    except AppFrameError as exc:
                        log(
                            f'RX invalid_status via={frame.sender_id} '
                            f'origin={route.origin_sender_id} seq={route.origin_seq} '
                            f'len={len(route_payload)} error="{exc}"'
                        )
                    else:
                        log(
                            f"RX status via={frame.sender_id} "
                            f"origin={route.origin_sender_id} seq={route.origin_seq} "
                            f"dest={route.destination_id} ttl={route.ttl} "
                            f"uptime_s={status.uptime_s} "
                            f"battery_pct={status.battery_pct} "
                            f"peer_count={status.peer_count} "
                            f"flags=0x{status.flags:02x} "
                            f"degraded={int(status.degraded_link)} "
                            f"crypto={int(status.crypto_enabled)} "
                            f"forwarding={int(status.forwarding_enabled)}"
                            f"{suffix}"
                        )
                elif route.inner_type == MSG_ROUTE_ADV:
                    try:
                        adv = decode_route_adv_payload(route_payload)
                    except AppFrameError as exc:
                        log(
                            f'RX invalid_route_adv via={frame.sender_id} '
                            f'origin={route.origin_sender_id} seq={route.origin_seq} '
                            f'len={len(route_payload)} error="{exc}"'
                        )
                    else:
                        neighbor_rssi = _peer_rssi.get(frame.sender_id, -80.0)
                        link_c = _link_cost(neighbor_rssi)
                        if adv.cost_to_base >= ROUTE_ADV_UNREACHABLE_COST:
                            via_cost = ROUTE_ADV_UNREACHABLE_COST
                            via_hops = ROUTE_ADV_UNREACHABLE_HOPS
                        else:
                            via_cost = min(
                                ROUTE_ADV_UNREACHABLE_COST - 1,
                                adv.cost_to_base + link_c,
                            )
                            via_hops = min(
                                ROUTE_ADV_UNREACHABLE_HOPS - 1,
                                adv.hops_to_base + 1,
                            )
                        routing_table.update(frame.sender_id, via_cost, via_hops, now)
                        log(
                            f"RX route_adv via={frame.sender_id} "
                            f"origin={route.origin_sender_id} "
                            f"cost_adv={adv.cost_to_base} hops_adv={adv.hops_to_base} "
                            f"my_cost={routing_table.my_cost_to_base()} "
                            f"my_hops={routing_table.my_hops_to_base()}"
                        )
                else:
                    if route_secured:
                        payload_text = "[encrypted]"
                    else:
                        try:
                            decoded = route_payload.decode("utf-8")
                            if all(
                                ch.isprintable() or ch in {"\r", "\n", "\t"}
                                for ch in decoded
                            ):
                                payload_text = f'"{decoded}"'
                            else:
                                payload_text = f"[{len(route_payload)} bytes]"
                        except UnicodeDecodeError:
                            payload_text = f"[{len(route_payload)} bytes]"
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

            if delivered and route.inner_type not in {MSG_ROUTE_ADV, MSG_SYNC}:
                post_route_observation(
                    origin_id=route.origin_sender_id,
                    via_id=frame.sender_id,
                    seq=route.origin_seq,
                    inner_type=route.inner_type,
                    payload=route_payload,
                    rssi=meta.rssi[0] if meta.rssi else None,
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
        close_local_c2_tap()
        close_local_control()
        close_radio()


if __name__ == "__main__":
    raise SystemExit(main())
