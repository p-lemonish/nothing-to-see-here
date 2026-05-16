#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_SRC = REPO_ROOT / "python" / "src"
MAX_U32 = 0xFFFF_FFFF

if str(PY_SRC) not in sys.path:
    sys.path.insert(0, str(PY_SRC))


def _next_seq(seq: int) -> int:
    seq = (seq + 1) & MAX_U32
    return 1 if seq == 0 else seq


def _parse_csv_ints(value: str | None) -> list[int]:
    if value is None:
        return []
    out: list[int] = []
    for part in value.split(","):
        text = part.strip()
        if text:
            out.append(int(text, 0))
    return out


def _utc_ms() -> int:
    return int(time.time() * 1000)


def _scheduled_channel(
    *,
    channels: list[int],
    slot_ms: int,
    epoch_ms: int,
) -> tuple[int, int, int]:
    elapsed_ms = max(0, _utc_ms() - epoch_ms)
    slot = elapsed_ms // slot_ms
    channel = channels[slot % len(channels)]
    next_hop_ms = slot_ms - (elapsed_ms % slot_ms)
    return channel, slot, next_hop_ms


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transmit dashboard-friendly status heartbeats")
    parser.add_argument("--iface", required=True, help="monitor-mode TX interface")
    parser.add_argument("--stream-id", type=lambda value: int(value, 0), default=1)
    parser.add_argument("--sender-id", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--label", help="display label, default TX-<sender-id>")
    parser.add_argument("--x", type=float, default=0.5, help="dashboard x position, 0..1")
    parser.add_argument("--y", type=float, default=0.5, help="dashboard y position, 0..1")
    parser.add_argument("--battery", type=int, default=100)
    parser.add_argument("--battery-drain-per-min", type=float, default=0.0)
    parser.add_argument("--interval-ms", type=int, default=1000)
    parser.add_argument("--count", type=int, default=0, help="0 means forever")
    parser.add_argument("--mesh", action="store_true", help="wrap status in route_data")
    parser.add_argument("--ttl", type=lambda value: int(value, 0), default=2)
    parser.add_argument("--destination-id", type=lambda value: int(value, 0), default=0)
    parser.add_argument("--jitter-ms", type=int, default=40)
    parser.add_argument(
        "--channel-agility",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="follow the same synchronized channel-hopping schedule as mesh_txrx.py",
    )
    parser.add_argument("--hop-channels", default="36,40,48")
    parser.add_argument("--channel-width", default="HT20")
    parser.add_argument("--hop-slot-ms", type=int, default=5000)
    parser.add_argument("--hop-epoch-ms", type=int, default=0)
    parser.add_argument("--channel-settle-ms", type=int, default=250)
    parser.add_argument("--channel-tx-guard-ms", type=int, default=250)
    parser.add_argument(
        "--channel-down-up",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args()


def main() -> int:
    from wfb_rs_py import Tx
    from wfb_rs_py.app_proto import (
        MSG_ROUTE_DATA,
        encode_frame,
        encode_route_data_payload,
    )

    args = parse_args()
    if not 1 <= args.sender_id <= 255:
        raise SystemExit("--sender-id must be in range 1..255")
    if not 0 <= args.destination_id <= 255:
        raise SystemExit("--destination-id must be in range 0..255")
    if args.stream_id == 0:
        raise SystemExit("--stream-id must be non-zero")
    if args.interval_ms <= 0:
        raise SystemExit("--interval-ms must be > 0")
    if args.count < 0:
        raise SystemExit("--count must be >= 0")
    hop_channels = _parse_csv_ints(args.hop_channels)
    if args.channel_agility:
        if not hop_channels:
            raise SystemExit("--hop-channels is required with --channel-agility")
        if any(channel <= 0 for channel in hop_channels):
            raise SystemExit("--hop-channels must contain positive channel numbers")
        if args.hop_slot_ms <= 0:
            raise SystemExit("--hop-slot-ms must be > 0")
        if args.channel_settle_ms < 0:
            raise SystemExit("--channel-settle-ms must be >= 0")
        if args.channel_tx_guard_ms < 0:
            raise SystemExit("--channel-tx-guard-ms must be >= 0")

    seq = 1
    sent = 0
    label = args.label or f"TX-{args.sender_id}"
    battery = max(0.0, min(100.0, float(args.battery)))
    drain_per_tick = args.battery_drain_per_min * (args.interval_ms / 1000.0) / 60.0

    tx: Tx | None = None
    current_channel: int | None = None
    tx_guard_until = 0.0

    def close_tx() -> None:
        nonlocal tx
        if tx is not None:
            tx.close()
            tx = None

    def open_tx() -> None:
        nonlocal tx
        tx = Tx(iface=args.iface, stream_id=args.stream_id)

    def switch_channel(channel: int, slot: int, next_hop_ms: int) -> None:
        nonlocal current_channel, tx_guard_until
        close_tx()
        _set_channel(
            iface=args.iface,
            channel=channel,
            width=args.channel_width,
            down_up=args.channel_down_up,
            settle_ms=args.channel_settle_ms,
        )
        open_tx()
        current_channel = channel
        tx_guard_until = time.monotonic() + (args.channel_tx_guard_ms / 1000.0)
        print(
            f"CHANNEL active iface={args.iface} channel={channel} "
            f"slot={slot} next_hop_ms={next_hop_ms}"
        )

    try:
        if args.channel_agility:
            channel, slot, next_hop_ms = _scheduled_channel(
                channels=hop_channels,
                slot_ms=args.hop_slot_ms,
                epoch_ms=args.hop_epoch_ms,
            )
            switch_channel(channel, slot, next_hop_ms)
        else:
            open_tx()

        print(
            f"Status TX sender={args.sender_id} iface={args.iface} "
            f"stream_id={args.stream_id} mesh={int(args.mesh)} "
            f"channel_agility={int(args.channel_agility)}"
        )

        while args.count == 0 or sent < args.count:
            if args.channel_agility:
                channel, slot, next_hop_ms = _scheduled_channel(
                    channels=hop_channels,
                    slot_ms=args.hop_slot_ms,
                    epoch_ms=args.hop_epoch_ms,
                )
                if channel != current_channel:
                    switch_channel(channel, slot, next_hop_ms)
                    continue

            if tx is None:
                open_tx()

            now = time.monotonic()
            if now < tx_guard_until:
                time.sleep(min(0.05, tx_guard_until - now))
                continue

            status = {
                "id": args.sender_id,
                "label": label,
                "kind": "real",
                "x": max(0.0, min(1.0, args.x)),
                "y": max(0.0, min(1.0, args.y)),
                "battery": round(battery),
                "state": "online",
                "message": "heartbeat",
                "ts": round(time.time(), 3),
            }
            payload = json.dumps(status, separators=(",", ":")).encode("utf-8")

            if args.mesh:
                route_payload = encode_route_data_payload(
                    origin_sender_id=args.sender_id,
                    destination_id=args.destination_id,
                    ttl=args.ttl,
                    origin_seq=seq,
                    inner_type="status",
                    inner_payload=payload,
                )
                frame = encode_frame(
                    sender_id=args.sender_id,
                    message_type=MSG_ROUTE_DATA,
                    app_seq=seq,
                    payload=route_payload,
                )
            else:
                frame = encode_frame(
                    sender_id=args.sender_id,
                    message_type="status",
                    app_seq=seq,
                    payload=payload,
                )

            tx.send(frame, seq=seq)
            print(f"TX status seq={seq} battery={round(battery)} len={len(payload)}")
            sent += 1
            seq = _next_seq(seq)
            battery = max(0.0, battery - drain_per_tick)

            jitter_s = (
                random.uniform(0, args.jitter_ms / 1000.0)
                if args.jitter_ms > 0
                else 0.0
            )
            time.sleep((args.interval_ms / 1000.0) + jitter_s)
    except KeyboardInterrupt:
        return 130
    finally:
        close_tx()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
