#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import os
import time
from collections import deque
from pathlib import Path
from typing import Deque

from wfb_rs_py import Rx, Tx
from wfb_rs_py.app_proto import (
    AppFrameError,
    MSG_ROUTE_DATA,
    decode_frame,
    decode_route_data_payload,
    encode_frame,
    encode_route_data_payload,
    message_type_value,
)

MAX_U32 = 0xFFFF_FFFF
CONFIG_SECTION = "mesh"


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
    section = parser[CONFIG_SECTION] if parser.has_section(CONFIG_SECTION) else parser["DEFAULT"]

    out: dict[str, object] = {}
    for key in (
        "iface",
        "message_type",
        "message",
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
    ):
        if key in section:
            out[key] = int(section[key], 0)

    for key in ("print_rssi", "include_self"):
        if key in section:
            out[key] = section.getboolean(key)

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

    try:
        inner_type = message_type_value(args.message_type)
    except AppFrameError as exc:
        parser.error(str(exc))
    if inner_type == MSG_ROUTE_DATA:
        parser.error("--message-type cannot be route_data")

    seen = SeenRoutes(args.seen_limit)
    inner_payload = args.message.encode("utf-8") if args.message is not None else None
    next_origin_seq = 1
    next_outer_seq = 1
    next_rf_seq = 1
    originated_count = 0
    next_tx_at = time.monotonic()

    with Tx(iface=args.iface, stream_id=args.stream_id) as tx, Rx(
        iface=args.iface,
        stream_id=args.stream_id,
        ignore_self_injected=not args.include_self,
    ) as rx:
        print(
            f"Mesh UDP mode: sender={args.sender_id} ttl={args.ttl} "
            f"dest={args.destination_id}, Ctrl-C to exit"
        )

        try:
            while True:
                now = time.monotonic()
                should_originate = (
                    inner_payload is not None
                    and (args.count == 0 or originated_count < args.count)
                    and now >= next_tx_at
                )
                if should_originate:
                    route_payload = encode_route_data_payload(
                        origin_sender_id=args.sender_id,
                        destination_id=args.destination_id,
                        ttl=args.ttl,
                        origin_seq=next_origin_seq,
                        inner_type=inner_type,
                        inner_payload=inner_payload,
                    )
                    frame = _encode_outer_route_frame(
                        sender_id=args.sender_id,
                        app_seq=next_outer_seq,
                        route_payload=route_payload,
                    )
                    tx.send(frame, seq=next_rf_seq)
                    seen.remember((args.sender_id, next_origin_seq))
                    print(
                        f"TX route origin={args.sender_id} seq={next_origin_seq} "
                        f"dest={args.destination_id} ttl={args.ttl} "
                        f"type={args.message_type} len={len(inner_payload)}"
                    )
                    next_origin_seq = _next_seq(next_origin_seq)
                    next_outer_seq = _next_seq(next_outer_seq)
                    next_rf_seq = _next_seq(next_rf_seq)
                    originated_count += 1
                    next_tx_at = now + (args.tx_interval_ms / 1000.0)

                result = rx.recv_optional(timeout_ms=args.rx_timeout_ms)
                if result is None:
                    continue

                payload, meta = result
                try:
                    frame = decode_frame(payload)
                except AppFrameError as exc:
                    print(
                        f'RX invalid_app_frame len={len(payload)} '
                        f'truncated={int(meta.truncated)} error="{exc}"'
                    )
                    continue

                if frame.message_type != MSG_ROUTE_DATA:
                    continue

                try:
                    route = decode_route_data_payload(frame.payload)
                except AppFrameError as exc:
                    print(
                        f'RX invalid_route from={frame.sender_id} '
                        f'len={len(frame.payload)} error="{exc}"'
                    )
                    continue

                if route.origin_sender_id == args.sender_id:
                    seen.remember(route.dedupe_key)
                    print(
                        f"RX own_route via={frame.sender_id} "
                        f"origin_seq={route.origin_seq} dropped=1"
                    )
                    continue

                if not seen.remember(route.dedupe_key):
                    print(
                        f"RX duplicate_route via={frame.sender_id} "
                        f"origin={route.origin_sender_id} seq={route.origin_seq} "
                        "dropped=1"
                    )
                    continue

                delivered = route.destination_id in (0, args.sender_id)
                suffix = ""
                if args.print_rssi:
                    suffix = (
                        f" bw={meta.bandwidth} mcs={meta.mcs_index} "
                        f"rssi0={meta.rssi[0]}"
                    )

                if delivered:
                    text = route.inner_payload.decode("utf-8", errors="replace")
                    print(
                        f'RX deliver via={frame.sender_id} '
                        f'origin={route.origin_sender_id} seq={route.origin_seq} '
                        f'dest={route.destination_id} ttl={route.ttl} '
                        f'type={route.inner_type_name} payload="{text}"{suffix}'
                    )
                else:
                    print(
                        f"RX transit via={frame.sender_id} "
                        f"origin={route.origin_sender_id} seq={route.origin_seq} "
                        f"dest={route.destination_id} ttl={route.ttl}{suffix}"
                    )

                if route.ttl <= 0:
                    continue

                forwarded = route.decremented_ttl()
                forward_payload = encode_route_data_payload(
                    origin_sender_id=forwarded.origin_sender_id,
                    destination_id=forwarded.destination_id,
                    ttl=forwarded.ttl,
                    origin_seq=forwarded.origin_seq,
                    inner_type=forwarded.inner_type,
                    inner_payload=forwarded.inner_payload,
                )
                forward_frame = _encode_outer_route_frame(
                    sender_id=args.sender_id,
                    app_seq=next_outer_seq,
                    route_payload=forward_payload,
                )
                tx.send(forward_frame, seq=next_rf_seq)
                print(
                    f"TX forward origin={forwarded.origin_sender_id} "
                    f"seq={forwarded.origin_seq} dest={forwarded.destination_id} "
                    f"ttl={forwarded.ttl}"
                )
                next_outer_seq = _next_seq(next_outer_seq)
                next_rf_seq = _next_seq(next_rf_seq)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
