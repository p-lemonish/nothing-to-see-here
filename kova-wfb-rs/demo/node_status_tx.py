#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_SRC = REPO_ROOT / "python" / "src"
DEFAULT_NODES_PATH = Path(__file__).resolve().parent / "nodes.json"
MAX_U32 = 0xFFFF_FFFF
CONFIG_SECTION = "mesh"

if str(PY_SRC) not in sys.path:
    sys.path.insert(0, str(PY_SRC))

from radio_iface import resolve_iface


@dataclass
class StatusNode:
    node_id: int
    label: str
    kind: str
    x: float
    y: float
    battery: float
    drain_per_tick: float
    seq: int = 1


def _next_seq(seq: int) -> int:
    seq = (seq + 1) & MAX_U32
    return 1 if seq == 0 else seq


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text.lower() in {"", "none", "null"}:
        return None
    return value


def _load_mesh_config(path: str | None) -> dict[str, object]:
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
        "label",
        "hop_channels",
        "channel_width",
        "channel_control",
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
        "interval_ms",
        "tx_interval_ms",
        "hop_slot_ms",
        "hop_epoch_ms",
        "channel_settle_ms",
        "channel_tx_guard_ms",
    ):
        if key in section:
            out[key] = int(section[key], 0)

    for key in (
        "x",
        "y",
        "battery",
        "battery_drain_per_min",
    ):
        if key in section:
            out[key] = float(section[key])

    for key in (
        "channel_agility",
        "channel_down_up",
        "mesh",
    ):
        if key in section:
            out[key] = section.getboolean(key)

    return out


def _load_node_defaults(path: Path = DEFAULT_NODES_PATH) -> dict[int, dict[str, object]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    nodes = data.get("nodes", []) if isinstance(data, dict) else []
    out: dict[int, dict[str, object]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        try:
            node_id = int(node["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[node_id] = node
    return out


def _default_sim_position(node_id: int) -> tuple[float, float]:
    x = 0.12 + (((node_id * 37) % 77) / 100.0)
    y = 0.14 + (((node_id * 53) % 71) / 100.0)
    return _clamp01(x), _clamp01(y)


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


def parse_sim_node(value: str) -> StatusNode:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) not in {1, 2, 4, 5}:
        raise argparse.ArgumentTypeError(
            "expected ID[,LABEL[,X,Y[,BATTERY]]]"
        )

    try:
        node_id = int(parts[0], 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sim node ID must be an integer") from exc
    if not 1 <= node_id <= 255:
        raise argparse.ArgumentTypeError("sim node ID must be in range 1..255")

    default_x, default_y = _default_sim_position(node_id)
    label = parts[1] if len(parts) >= 2 and parts[1] else f"SIM-{node_id}"
    x = default_x
    y = default_y
    battery = 100.0

    if len(parts) >= 4:
        try:
            x = float(parts[2])
            y = float(parts[3])
        except ValueError as exc:
            raise argparse.ArgumentTypeError("sim node X and Y must be numbers") from exc

    if len(parts) == 5:
        try:
            battery = float(parts[4])
        except ValueError as exc:
            raise argparse.ArgumentTypeError("sim node battery must be a number") from exc

    return StatusNode(
        node_id=node_id,
        label=label,
        kind="simulated",
        x=_clamp01(x),
        y=_clamp01(y),
        battery=max(0.0, min(100.0, battery)),
        drain_per_tick=0.0,
    )


def parse_args() -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument(
        "--config",
        help="read transmitter defaults from a configs/node*.ini [mesh] section",
    )
    config_args, _ = config_parser.parse_known_args()
    try:
        config_defaults = _load_mesh_config(config_args.config)
    except (OSError, ValueError, configparser.Error) as exc:
        raise SystemExit(f"node_status_tx.py: config error: {exc}") from exc

    interval_default = config_defaults.get(
        "interval_ms",
        config_defaults.get("tx_interval_ms", 1000),
    )
    mesh_default = config_defaults.get("mesh")

    parser = argparse.ArgumentParser(
        description="Transmit dashboard-friendly status heartbeats",
        parents=[config_parser],
    )
    parser.add_argument(
        "--iface",
        default=config_defaults.get("iface"),
        help="monitor-mode TX interface (default: $NIC, $WFB_IFACE, $IFACE, or single iw dev interface)",
    )
    parser.add_argument(
        "--stream-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("stream_id", 1),
    )
    parser.add_argument(
        "--sender-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("sender_id"),
    )
    parser.add_argument(
        "--label",
        default=config_defaults.get("label"),
        help="display label, default from demo/nodes.json or TX-<sender-id>",
    )
    parser.add_argument(
        "--x",
        type=float,
        default=config_defaults.get("x"),
        help="dashboard x position, 0..1",
    )
    parser.add_argument(
        "--y",
        type=float,
        default=config_defaults.get("y"),
        help="dashboard y position, 0..1",
    )
    parser.add_argument("--battery", type=int, default=config_defaults.get("battery", 100))
    parser.add_argument(
        "--battery-drain-per-min",
        type=float,
        default=config_defaults.get("battery_drain_per_min", 0.0),
    )
    parser.add_argument(
        "--sim-node",
        action="append",
        type=parse_sim_node,
        default=[],
        metavar="ID[,LABEL[,X,Y[,BATTERY]]]",
        help=(
            "extra logical simulated node to advertise from this transmitter; "
            "repeat for multiple nodes"
        ),
    )
    parser.add_argument("--interval-ms", type=int, default=interval_default)
    parser.add_argument(
        "--count",
        type=int,
        default=config_defaults.get("count", 0),
        help="heartbeat cycles to send; 0 means forever",
    )
    parser.add_argument(
        "--mesh",
        action=argparse.BooleanOptionalAction,
        default=mesh_default,
        help=(
            "wrap status in route_data; defaults on when --sim-node is used "
            "unless --no-mesh is passed"
        ),
    )
    parser.add_argument(
        "--ttl",
        type=lambda value: int(value, 0),
        default=config_defaults.get("ttl", 2),
    )
    parser.add_argument(
        "--destination-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("destination_id", 0),
    )
    parser.add_argument("--jitter-ms", type=int, default=40)
    parser.add_argument(
        "--channel-agility",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("channel_agility", False),
        help="follow the same synchronized channel-hopping schedule as mesh_txrx.py",
    )
    parser.add_argument(
        "--channel-control",
        choices=("own", "external"),
        default=config_defaults.get("channel_control"),
        help=(
            "own changes the interface channel; external assumes another "
            "process controls the shared interface"
        ),
    )
    parser.add_argument("--hop-channels", default=config_defaults.get("hop_channels", "36,40,48"))
    parser.add_argument("--channel-width", default=config_defaults.get("channel_width", "HT20"))
    parser.add_argument("--hop-slot-ms", type=int, default=config_defaults.get("hop_slot_ms", 5000))
    parser.add_argument("--hop-epoch-ms", type=int, default=config_defaults.get("hop_epoch_ms", 0))
    parser.add_argument(
        "--channel-settle-ms",
        type=int,
        default=config_defaults.get("channel_settle_ms", 250),
    )
    parser.add_argument(
        "--channel-tx-guard-ms",
        type=int,
        default=config_defaults.get("channel_tx_guard_ms", 250),
    )
    parser.add_argument(
        "--channel-down-up",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("channel_down_up", True),
    )
    return parser.parse_args()


def main() -> int:
    from wfb_rs_py import Tx, WfbRsError
    from wfb_rs_py.app_proto import (
        MSG_ROUTE_DATA,
        encode_frame,
        encode_route_data_payload,
    )

    args = parse_args()
    args.iface = resolve_iface(args.iface, purpose="status transmitter")
    if args.sender_id is None:
        raise SystemExit("--sender-id is required unless --config supplies sender_id")
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

    node_defaults = _load_node_defaults()
    sender_defaults = node_defaults.get(args.sender_id, {})
    label = args.label or str(sender_defaults.get("label") or f"TX-{args.sender_id}")
    x = args.x
    y = args.y
    if x is None:
        try:
            x = float(sender_defaults.get("x", 0.5))
        except (TypeError, ValueError):
            x = 0.5
    if y is None:
        try:
            y = float(sender_defaults.get("y", 0.5))
        except (TypeError, ValueError):
            y = 0.5
    drain_per_tick = args.battery_drain_per_min * (args.interval_ms / 1000.0) / 60.0
    nodes = [
        StatusNode(
            node_id=args.sender_id,
            label=label,
            kind="real",
            x=_clamp01(x),
            y=_clamp01(y),
            battery=max(0.0, min(100.0, float(args.battery))),
            drain_per_tick=drain_per_tick,
        ),
        *args.sim_node,
    ]
    for node in nodes[1:]:
        node.drain_per_tick = drain_per_tick
    duplicate_ids = {
        node.node_id
        for node in nodes
        if sum(1 for candidate in nodes if candidate.node_id == node.node_id) > 1
    }
    if duplicate_ids:
        joined = ", ".join(str(node_id) for node_id in sorted(duplicate_ids))
        raise SystemExit(f"duplicate logical node IDs: {joined}")
    if args.mesh is None:
        args.mesh = bool(args.sim_node)
    if args.channel_control is None:
        args.channel_control = "external" if args.sim_node else "own"
    if args.sim_node and not args.mesh:
        print(
            "WARN --sim-node without --mesh sends direct logical sender IDs; "
            "use --mesh to show the physical transmitter as the via node",
            file=sys.stderr,
        )

    rf_seq = 1
    sent_cycles = 0

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
        if args.channel_control == "own":
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
            f"CHANNEL {args.channel_control} iface={args.iface} channel={channel} "
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
            f"stream_id={args.stream_id} logical_nodes={len(nodes)} "
            f"mesh={int(args.mesh)} "
            f"channel_agility={int(args.channel_agility)}"
        )

        while args.count == 0 or sent_cycles < args.count:
            if args.channel_agility:
                channel, slot, next_hop_ms = _scheduled_channel(
                    channels=hop_channels,
                    slot_ms=args.hop_slot_ms,
                    epoch_ms=args.hop_epoch_ms,
                )
                if channel != current_channel:
                    switch_channel(channel, slot, next_hop_ms)
                    continue

            now = time.monotonic()
            if now < tx_guard_until:
                time.sleep(min(0.05, tx_guard_until - now))
                continue

            if tx is None:
                try:
                    open_tx()
                except WfbRsError as exc:
                    print(f'TX open_error error="{exc}"', file=sys.stderr)
                    time.sleep(0.1)
                    continue

            for node in nodes:
                status = {
                    "id": node.node_id,
                    "label": node.label,
                    "kind": node.kind,
                    "x": node.x,
                    "y": node.y,
                    "battery": round(node.battery),
                    "state": "online",
                    "message": "heartbeat",
                    "ts": round(time.time(), 3),
                    "physical_sender_id": args.sender_id,
                }
                payload = json.dumps(status, separators=(",", ":")).encode("utf-8")

                if args.mesh:
                    route_payload = encode_route_data_payload(
                        origin_sender_id=node.node_id,
                        destination_id=args.destination_id,
                        ttl=args.ttl,
                        origin_seq=node.seq,
                        inner_type="status",
                        inner_payload=payload,
                    )
                    frame = encode_frame(
                        sender_id=args.sender_id,
                        message_type=MSG_ROUTE_DATA,
                        app_seq=rf_seq,
                        payload=route_payload,
                    )
                else:
                    frame = encode_frame(
                        sender_id=node.node_id,
                        message_type="status",
                        app_seq=node.seq,
                        payload=payload,
                    )

                try:
                    tx.send(frame, seq=rf_seq)
                except WfbRsError as exc:
                    print(
                        f'TX send_error node={node.node_id} seq={rf_seq} '
                        f'error="{exc}"',
                        file=sys.stderr,
                    )
                    close_tx()
                    rf_seq = _next_seq(rf_seq)
                    break
                via = (
                    f" via={args.sender_id}"
                    if args.mesh and node.node_id != args.sender_id
                    else ""
                )
                print(
                    f"TX status node={node.node_id}{via} seq={node.seq} "
                    f"battery={round(node.battery)} len={len(payload)}"
                )
                node.seq = _next_seq(node.seq)
                node.battery = max(0.0, node.battery - node.drain_per_tick)
                rf_seq = _next_seq(rf_seq)

            sent_cycles += 1

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
