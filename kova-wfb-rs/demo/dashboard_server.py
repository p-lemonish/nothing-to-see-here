#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import json
import mimetypes
import os
import random
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_SRC = REPO_ROOT / "python" / "src"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_NODES_PATH = Path(__file__).resolve().parent / "nodes.json"
BASE_NODE_ID = 0
MAX_U32 = 0xFFFF_FFFF
CONFIG_SECTION = "mesh"

if str(PY_SRC) not in sys.path:
    sys.path.insert(0, str(PY_SRC))

from radio_iface import resolve_iface


@dataclass
class NodeRecord:
    node_id: int
    label: str
    kind: str = "real"
    x: float = 0.5
    y: float = 0.5
    role: str = "drone"
    last_seen: float | None = None
    packets: int = 0
    lost_packets: int = 0
    duplicate_packets: int = 0
    last_seq: int | None = None
    rssi: int | None = None
    freq: int | None = None
    bandwidth: int | None = None
    mcs_index: int | None = None
    via: int | None = None
    battery: int | None = None
    cost_to_base: int | None = None
    hops_to_base: int | None = None
    message: str = ""
    source: str = "config"


@dataclass
class LinkRecord:
    from_id: int
    to_id: int
    last_seen: float
    packets: int = 0
    rssi: int | None = None
    source: str = "radio"


@dataclass
class DashboardStats:
    source: str
    started_at: float = field(default_factory=time.time)
    radio_packets: int = 0
    simulated_packets: int = 0
    malformed_packets: int = 0
    radio_status: str = "idle"
    radio_error: str | None = None
    radio_channel: int | None = None
    radio_slot: int | None = None
    radio_next_hop_ms: int | None = None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _next_seq(seq: int) -> int:
    seq = (seq + 1) & MAX_U32
    return 1 if seq == 0 else seq


def _safe_text(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace").strip()


def _parse_csv_ints(value: str | None) -> list[int]:
    if value is None:
        return []
    out: list[int] = []
    for part in value.split(","):
        text = part.strip()
        if text:
            out.append(int(text, 0))
    return out


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
        "hop_channels",
        "channel_width",
    ):
        if key in section:
            value = _optional_text(section.get(key))
            if value is not None:
                out[key] = value

    for key in (
        "stream_id",
        "radio_timeout_ms",
        "rx_timeout_ms",
        "hop_slot_ms",
        "hop_epoch_ms",
        "channel_settle_ms",
        "sim_interval_ms",
    ):
        if key in section:
            out[key] = int(section[key], 0)

    for key in (
        "channel_agility",
        "channel_down_up",
    ):
        if key in section:
            out[key] = section.getboolean(key)

    for key in (
        "source",
        "host",
    ):
        if key in section:
            value = _optional_text(section.get(key))
            if value is not None:
                out[key] = value

    if "port" in section:
        out["port"] = int(section["port"], 0)

    for key in (
        "stale_after_s",
        "down_after_s",
    ):
        if key in section:
            out[key] = float(section[key])

    return out


def _utc_ms() -> int:
    return int(time.time() * 1000)


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


def parse_status_payload(payload: bytes) -> dict[str, Any]:
    text = _safe_text(payload)
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        parsed.setdefault("message", text)
        return parsed

    values: dict[str, Any] = {}
    normalized = text.replace(";", " ").replace(",", " ")
    for token in normalized.split():
        if "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        int_value = _coerce_int(raw_value)
        float_value = _coerce_float(raw_value)
        if int_value is not None and str(int_value) == raw_value:
            values[key] = int_value
        elif float_value is not None:
            values[key] = float_value
        else:
            values[key] = raw_value

    if values:
        values.setdefault("message", text)
        return values

    return {"message": text}


class MeshState:
    def __init__(self, *, source: str, stale_after: float, down_after: float):
        self._lock = threading.Lock()
        self.nodes: dict[int, NodeRecord] = {}
        self.links: dict[tuple[int, int], LinkRecord] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=80)
        self.jammed_sim_nodes: set[int] = set()
        self.stale_after = stale_after
        self.down_after = down_after
        self.stats = DashboardStats(source=source)

        self.nodes[BASE_NODE_ID] = NodeRecord(
            node_id=BASE_NODE_ID,
            label="BASE",
            kind="receiver",
            role="base",
            x=0.5,
            y=0.88,
            last_seen=time.time(),
            source="local",
        )

    def apply_node_config(self, config: dict[str, Any]) -> None:
        for raw_node in config.get("nodes", []):
            if not isinstance(raw_node, dict):
                continue
            node_id = _coerce_int(raw_node.get("id"))
            if node_id is None:
                continue
            with self._lock:
                node = self._ensure_node_locked(node_id)
                node.label = str(raw_node.get("label", node.label))
                node.kind = str(raw_node.get("kind", node.kind))
                node.role = str(raw_node.get("role", node.role))
                x_value = _coerce_float(raw_node.get("x"))
                y_value = _coerce_float(raw_node.get("y"))
                if x_value is not None:
                    node.x = _clamp01(x_value)
                if y_value is not None:
                    node.y = _clamp01(y_value)

    def node_meta(self, node_id: int) -> dict[str, Any]:
        with self._lock:
            node = self._ensure_node_locked(node_id)
            return {
                "id": node.node_id,
                "label": node.label,
                "kind": node.kind,
                "role": node.role,
                "x": node.x,
                "y": node.y,
                "battery": node.battery,
            }

    def set_radio_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.stats.radio_status = status
            self.stats.radio_error = error
            if error:
                self._append_event_locked(
                    {
                        "kind": "radio",
                        "severity": "error",
                        "message": error,
                    }
                )

    def set_radio_channel(
        self,
        *,
        channel: int | None,
        slot: int | None = None,
        next_hop_ms: int | None = None,
    ) -> None:
        with self._lock:
            self.stats.radio_channel = channel
            self.stats.radio_slot = slot
            self.stats.radio_next_hop_ms = next_hop_ms

    def set_sim_jammed(self, node_id: int, jammed: bool) -> None:
        with self._lock:
            if jammed:
                self.jammed_sim_nodes.add(node_id)
            else:
                self.jammed_sim_nodes.discard(node_id)
            self._append_event_locked(
                {
                    "kind": "jam",
                    "node": node_id,
                    "severity": "warning" if jammed else "info",
                    "message": f"Node {node_id} jamming {'enabled' if jammed else 'cleared'}",
                }
            )

    def reset_sim(self) -> None:
        with self._lock:
            self.jammed_sim_nodes.clear()
            self._append_event_locked(
                {
                    "kind": "jam",
                    "severity": "info",
                    "message": "Simulation jamming cleared",
                }
            )

    def ingest_observation(
        self,
        *,
        origin_id: int,
        via_id: int,
        seq: int | None,
        payload: bytes,
        message_type: str,
        rssi: int | None,
        freq: int | None,
        bandwidth: int | None,
        mcs_index: int | None,
        source: str,
        kind: str,
    ) -> None:
        now = time.time()
        payload_text = _safe_text(payload)
        status = parse_status_payload(payload)
        message = str(status.get("message") or message_type)

        with self._lock:
            node = self._ensure_node_locked(origin_id)
            label = status.get("label") or status.get("name")
            if label is not None:
                node.label = str(label)
            node.kind = str(status.get("kind") or kind or node.kind)
            node.source = source
            node.last_seen = now
            node.packets += 1
            node.rssi = rssi
            node.freq = freq
            node.bandwidth = bandwidth
            node.mcs_index = mcs_index
            node.via = via_id
            node.message = message

            x_value = _coerce_float(status.get("x"))
            y_value = _coerce_float(status.get("y"))
            if x_value is not None:
                node.x = _clamp01(x_value)
            if y_value is not None:
                node.y = _clamp01(y_value)

            battery = _coerce_int(status.get("battery"))
            if battery is not None:
                node.battery = max(0, min(100, battery))

            if seq is not None:
                if node.last_seq == seq:
                    node.duplicate_packets += 1
                elif node.last_seq is not None and seq > node.last_seq + 1:
                    node.lost_packets += seq - node.last_seq - 1
                node.last_seq = seq

            if source == "sim":
                self.stats.simulated_packets += 1
            else:
                self.stats.radio_packets += 1

            self._touch_link_locked(BASE_NODE_ID, via_id, now, rssi, source)
            if via_id != origin_id:
                self._touch_link_locked(via_id, origin_id, now, rssi, source)

            self._append_event_locked(
                {
                    "kind": "rx",
                    "node": origin_id,
                    "via": via_id,
                    "seq": seq,
                    "rssi": rssi,
                    "source": source,
                    "message_type": message_type,
                    "message": message,
                    "payload": payload_text,
                    "freq": freq,
                    "bandwidth": bandwidth,
                    "mcs_index": mcs_index,
                }
            )

    def ingest_malformed(self, message: str) -> None:
        with self._lock:
            self.stats.malformed_packets += 1
            self._append_event_locked(
                {
                    "kind": "malformed",
                    "severity": "warning",
                    "message": message,
                }
            )

    def ingest_route_adv(
        self,
        *,
        origin_id: int,
        cost_to_base: int,
        hops_to_base: int,
    ) -> None:
        with self._lock:
            node = self._ensure_node_locked(origin_id)
            node.cost_to_base = cost_to_base
            node.hops_to_base = hops_to_base

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            nodes = [self._node_to_dict_locked(node, now) for node in self.nodes.values()]
            links = [
                self._link_to_dict_locked(link, now)
                for link in self.links.values()
                if now - link.last_seen <= max(self.down_after * 2.0, 8.0)
            ]
            return {
                "now": now,
                "nodes": sorted(nodes, key=lambda item: (item["role"] != "base", item["id"])),
                "links": sorted(links, key=lambda item: (item["from"], item["to"])),
                "events": list(self.events),
                "jammed": sorted(self.jammed_sim_nodes),
                "stats": {
                    "source": self.stats.source,
                    "uptime_s": round(now - self.stats.started_at, 1),
                    "radio_packets": self.stats.radio_packets,
                    "simulated_packets": self.stats.simulated_packets,
                    "malformed_packets": self.stats.malformed_packets,
                    "radio_status": self.stats.radio_status,
                    "radio_error": self.stats.radio_error,
                    "radio_channel": getattr(self.stats, "radio_channel", None),
                    "radio_slot": getattr(self.stats, "radio_slot", None),
                    "radio_next_hop_ms": getattr(self.stats, "radio_next_hop_ms", None),
                },
            }

    def _ensure_node_locked(self, node_id: int) -> NodeRecord:
        if node_id not in self.nodes:
            self.nodes[node_id] = NodeRecord(node_id=node_id, label=f"TX-{node_id}")
        return self.nodes[node_id]

    def _touch_link_locked(
        self,
        from_id: int,
        to_id: int,
        now: float,
        rssi: int | None,
        source: str,
    ) -> None:
        if from_id == to_id:
            return
        self._ensure_node_locked(from_id)
        self._ensure_node_locked(to_id)
        key = (from_id, to_id)
        link = self.links.get(key)
        if link is None:
            self.links[key] = LinkRecord(
                from_id=from_id,
                to_id=to_id,
                last_seen=now,
                packets=1,
                rssi=rssi,
                source=source,
            )
            return
        link.last_seen = now
        link.packets += 1
        link.rssi = rssi
        link.source = source

    def _append_event_locked(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        event.setdefault("severity", "info")
        self.events.appendleft(event)

    def _node_to_dict_locked(self, node: NodeRecord, now: float) -> dict[str, Any]:
        if node.role == "base":
            age_ms = 0
            state = "online"
        elif node.last_seen is None:
            age_ms = None
            state = "down"
        else:
            age = now - node.last_seen
            age_ms = round(age * 1000)
            if age > self.down_after:
                state = "down"
            elif age > self.stale_after:
                state = "stale"
            else:
                state = "online"

        total_expected = node.packets + node.lost_packets
        loss_rate = node.lost_packets / total_expected if total_expected else 0.0
        return {
            "id": node.node_id,
            "label": node.label,
            "kind": node.kind,
            "role": node.role,
            "x": node.x,
            "y": node.y,
            "state": state,
            "age_ms": age_ms,
            "packets": node.packets,
            "lost_packets": node.lost_packets,
            "duplicate_packets": node.duplicate_packets,
            "loss_rate": round(loss_rate, 4),
            "last_seq": node.last_seq,
            "rssi": node.rssi,
            "freq": node.freq,
            "bandwidth": node.bandwidth,
            "mcs_index": node.mcs_index,
            "via": node.via,
            "battery": node.battery,
            "cost_to_base": node.cost_to_base,
            "hops_to_base": node.hops_to_base,
            "message": node.message,
            "source": node.source,
            "jammed": node.node_id in self.jammed_sim_nodes,
        }

    def _link_to_dict_locked(self, link: LinkRecord, now: float) -> dict[str, Any]:
        age = now - link.last_seen
        if age > self.down_after:
            state = "down"
        elif age > self.stale_after:
            state = "stale"
        else:
            state = "online"
        return {
            "from": link.from_id,
            "to": link.to_id,
            "age_ms": round(age * 1000),
            "packets": link.packets,
            "rssi": link.rssi,
            "source": link.source,
            "state": state,
        }


def _handle_ingest(state: MeshState, data: dict[str, Any]) -> None:
    origin_id = _coerce_int(data.get("origin_id"))
    if origin_id is None:
        return
    via_id = _coerce_int(data.get("via_id")) or origin_id
    seq = _coerce_int(data.get("seq"))
    inner_type = str(data.get("inner_type", "data"))
    try:
        payload = bytes.fromhex(str(data.get("payload_hex", "")))
    except ValueError:
        payload = b""
    state.ingest_observation(
        origin_id=origin_id,
        via_id=via_id,
        seq=seq,
        payload=payload,
        message_type=inner_type,
        rssi=_coerce_int(data.get("rssi")),
        freq=_coerce_int(data.get("freq")),
        bandwidth=_coerce_int(data.get("bandwidth")),
        mcs_index=_coerce_int(data.get("mcs_index")),
        source=str(data.get("source", "radio")),
        kind="real",
    )


def load_node_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"nodes": []}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _extract_rssi(meta: Any) -> int | None:
    antenna_count = getattr(meta, "antenna_count", 0)
    rssi_values = getattr(meta, "rssi", ())
    if not rssi_values:
        return None
    first = int(rssi_values[0])
    if antenna_count <= 0 and first == 0:
        return None
    return first


def process_radio_payload(state: MeshState, payload: bytes, meta: Any) -> None:
    from wfb_rs_py.app_proto import (
        AppFrameError,
        MSG_ROUTE_ADV,
        MSG_ROUTE_DATA,
        MSG_SYNC,
        ROUTE_ADV_UNREACHABLE_COST,
        decode_frame,
        decode_route_adv_payload,
        decode_route_data_payload,
        decode_sync_payload,
    )

    try:
        frame = decode_frame(payload)
        rssi = _extract_rssi(meta)
        freq = int(getattr(meta, "freq", 0)) or None
        bandwidth = int(getattr(meta, "bandwidth", 0)) or None
        mcs_index = int(getattr(meta, "mcs_index", 0))

        if frame.message_type == MSG_ROUTE_DATA:
            route = decode_route_data_payload(frame.payload)

            if route.inner_type == MSG_ROUTE_ADV:
                try:
                    adv = decode_route_adv_payload(route.inner_payload)
                    if adv.cost_to_base < ROUTE_ADV_UNREACHABLE_COST:
                        state.ingest_route_adv(
                            origin_id=route.origin_sender_id,
                            cost_to_base=adv.cost_to_base,
                            hops_to_base=adv.hops_to_base,
                        )
                except AppFrameError:
                    pass
                return

            routed_payload = route.inner_payload
            if route.inner_type == MSG_SYNC:
                sync = decode_sync_payload(route.inner_payload)
                routed_payload = json.dumps(
                    {
                        "message": "sync",
                        "utc_ms": sync.utc_ms,
                        "slot": sync.slot,
                        "channel": sync.channel,
                        "next_hop_ms": sync.next_hop_ms,
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            state.ingest_observation(
                origin_id=route.origin_sender_id,
                via_id=frame.sender_id,
                seq=route.origin_seq,
                payload=routed_payload,
                message_type=route.inner_type_name,
                rssi=rssi,
                freq=freq,
                bandwidth=bandwidth,
                mcs_index=mcs_index,
                source="radio",
                kind="real",
            )
            return

        direct_payload = frame.payload
        if frame.message_type == MSG_SYNC:
            sync = decode_sync_payload(frame.payload)
            direct_payload = json.dumps(
                {
                    "message": "sync",
                    "utc_ms": sync.utc_ms,
                    "slot": sync.slot,
                    "channel": sync.channel,
                    "next_hop_ms": sync.next_hop_ms,
                },
                separators=(",", ":"),
            ).encode("utf-8")

        state.ingest_observation(
            origin_id=frame.sender_id,
            via_id=frame.sender_id,
            seq=frame.app_seq,
            payload=direct_payload,
            message_type=frame.message_type_name,
            rssi=rssi,
            freq=freq,
            bandwidth=bandwidth,
            mcs_index=mcs_index,
            source="radio",
            kind="real",
        )
    except AppFrameError as exc:
        state.ingest_malformed(str(exc))


def radio_loop(
    *,
    state: MeshState,
    stop_event: threading.Event,
    iface: str,
    stream_id: int,
    timeout_ms: int,
    channel_agility: bool,
    hop_channels: list[int],
    channel_width: str,
    hop_slot_ms: int,
    hop_epoch_ms: int,
    channel_settle_ms: int,
    channel_down_up: bool,
) -> None:
    rx: Any | None = None
    current_channel: int | None = None

    def close_rx() -> None:
        nonlocal rx
        if rx is not None:
            rx.close()
            rx = None

    def open_rx() -> None:
        nonlocal rx
        from wfb_rs_py import Rx

        rx = Rx(iface=iface, stream_id=stream_id, ignore_self_injected=True)

    try:
        state.set_radio_status("opening")

        if channel_agility:
            channel, slot, next_hop_ms = _scheduled_channel(
                channels=hop_channels,
                slot_ms=hop_slot_ms,
                epoch_ms=hop_epoch_ms,
            )
            state.set_radio_status("hopping")
            _set_channel(
                iface=iface,
                channel=channel,
                width=channel_width,
                down_up=channel_down_up,
                settle_ms=channel_settle_ms,
            )
            current_channel = channel
            state.set_radio_channel(
                channel=channel,
                slot=slot,
                next_hop_ms=next_hop_ms,
            )

        open_rx()
        state.set_radio_status("connected")

        while not stop_event.is_set():
            if channel_agility:
                channel, slot, next_hop_ms = _scheduled_channel(
                    channels=hop_channels,
                    slot_ms=hop_slot_ms,
                    epoch_ms=hop_epoch_ms,
                )
                state.set_radio_channel(
                    channel=channel,
                    slot=slot,
                    next_hop_ms=next_hop_ms,
                )
                if channel != current_channel:
                    state.set_radio_status("hopping")
                    close_rx()
                    _set_channel(
                        iface=iface,
                        channel=channel,
                        width=channel_width,
                        down_up=channel_down_up,
                        settle_ms=channel_settle_ms,
                    )
                    current_channel = channel
                    open_rx()
                    state.set_radio_status("connected")
                    continue

            if rx is None:
                open_rx()

            result = rx.recv_optional(timeout_ms=timeout_ms)
            if result is None:
                continue
            payload, meta = result
            process_radio_payload(state, payload, meta)
    except Exception as exc:
        state.set_radio_status("error", f"{type(exc).__name__}: {exc}")
    finally:
        close_rx()


def sim_loop(
    *,
    state: MeshState,
    stop_event: threading.Event,
    interval_s: float,
) -> None:
    seq_by_node: defaultdict[int, int] = defaultdict(int)
    battery_by_node = {1: 96, 2: 89, 3: 82}
    next_tick = time.monotonic()

    while not stop_event.is_set():
        now = time.monotonic()
        if now < next_tick:
            stop_event.wait(min(0.1, next_tick - now))
            continue

        snapshot = state.snapshot()
        jammed = set(snapshot["jammed"])

        for node_id in (1, 2, 3):
            if node_id in jammed:
                continue

            meta = state.node_meta(node_id)
            seq_by_node[node_id] = _next_seq(seq_by_node[node_id])
            battery_by_node[node_id] = max(8, battery_by_node[node_id] - (1 if random.random() < 0.04 else 0))

            if node_id == 3 and 2 not in jammed:
                via_id = 2
                rssi = -61 + random.randint(-4, 3)
            else:
                via_id = node_id
                rssi = -42 - (node_id * 5) + random.randint(-3, 3)

            payload = json.dumps(
                {
                    "id": node_id,
                    "label": meta["label"],
                    "kind": "simulated",
                    "x": meta["x"],
                    "y": meta["y"],
                    "battery": battery_by_node[node_id],
                    "state": "online",
                    "message": "heartbeat",
                },
                separators=(",", ":"),
            ).encode("utf-8")

            state.ingest_observation(
                origin_id=node_id,
                via_id=via_id,
                seq=seq_by_node[node_id],
                payload=payload,
                message_type="status",
                rssi=rssi,
                freq=5180,
                bandwidth=20,
                mcs_index=1,
                source="sim",
                kind="simulated",
            )

        next_tick = time.monotonic() + interval_s


def make_handler(state: MeshState, static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/events":
                self._serve_events()
                return
            if parsed.path == "/api/state":
                self._send_json(state.snapshot())
                return
            if parsed.path == "/":
                self._serve_static("index.html")
                return

            relative = parsed.path.lstrip("/")
            self._serve_static(relative)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/sim/jam":
                data = self._read_json_body()
                query = parse_qs(parsed.query)
                node_id = _coerce_int(data.get("node_id") or data.get("node") or query.get("node", [None])[0])
                jammed = data.get("jammed")
                if jammed is None:
                    jammed = query.get("jammed", ["true"])[0]
                jammed_bool = str(jammed).lower() in {"1", "true", "yes", "on"}
                if node_id is None:
                    self._send_json({"error": "node_id is required"}, status=400)
                    return
                state.set_sim_jammed(node_id, jammed_bool)
                self._send_json(state.snapshot())
                return

            if parsed.path == "/api/sim/reset":
                state.reset_sim()
                self._send_json(state.snapshot())
                return

            if parsed.path == "/ingest":
                data = self._read_json_body()
                _handle_ingest(state, data)
                self._send_json({"ok": True})
                return

            self._send_json({"error": "not found"}, status=404)

        def _serve_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(state.snapshot(), separators=(",", ":"))
                    self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return

        def _serve_static(self, relative: str) -> None:
            try:
                target = (static_dir / relative).resolve()
                target.relative_to(static_dir.resolve())
            except ValueError:
                self._send_json({"error": "not found"}, status=404)
                return

            if not target.exists() or not target.is_file():
                self._send_json({"error": "not found"}, status=404)
                return

            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def parse_args() -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument(
        "--config",
        help="read radio defaults from a configs/node*.ini [mesh] section",
    )
    config_args, _ = config_parser.parse_known_args()
    try:
        config_defaults = _load_mesh_config(config_args.config)
    except (OSError, ValueError, configparser.Error) as exc:
        raise SystemExit(f"dashboard_server.py: config error: {exc}") from exc

    radio_timeout_default = config_defaults.get(
        "radio_timeout_ms",
        config_defaults.get("rx_timeout_ms", 100),
    )

    parser = argparse.ArgumentParser(
        description="Live dashboard for wfb_rs transmitter drones",
        parents=[config_parser],
    )
    parser.add_argument("--host", default=config_defaults.get("host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=config_defaults.get("port", 8765))
    parser.add_argument(
        "--source",
        choices=("sim", "radio", "both", "feed"),
        default=config_defaults.get("source", "sim"),
        help="sim=simulated, radio=raw NIC, both=sim+radio, feed=HTTP push from mesh_txrx",
    )
    parser.add_argument(
        "--iface",
        default=config_defaults.get("iface"),
        help=(
            "monitor-mode interface for radio source "
            "(default: $NIC, $WFB_IFACE, $IFACE, or single iw dev interface)"
        ),
    )
    parser.add_argument(
        "--stream-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("stream_id", 1),
    )
    parser.add_argument("--radio-timeout-ms", type=int, default=radio_timeout_default)
    parser.add_argument(
        "--channel-agility",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("channel_agility", False),
        help="follow the synchronized channel-hopping schedule used by mesh_txrx.py",
    )
    parser.add_argument(
        "--hop-channels",
        default=config_defaults.get("hop_channels", "36,40,48"),
        help="comma-separated channel schedule used with --channel-agility",
    )
    parser.add_argument(
        "--channel-width",
        default=config_defaults.get("channel_width", "HT20"),
    )
    parser.add_argument(
        "--hop-slot-ms",
        type=int,
        default=config_defaults.get("hop_slot_ms", 5000),
    )
    parser.add_argument(
        "--hop-epoch-ms",
        type=int,
        default=config_defaults.get("hop_epoch_ms", 0),
    )
    parser.add_argument(
        "--channel-settle-ms",
        type=int,
        default=config_defaults.get("channel_settle_ms", 250),
    )
    parser.add_argument(
        "--channel-down-up",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("channel_down_up", True),
        help="bring interface down/up around channel changes",
    )
    parser.add_argument(
        "--sim-interval-ms",
        type=int,
        default=config_defaults.get("sim_interval_ms", 900),
    )
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_PATH)
    parser.add_argument(
        "--stale-after-s",
        type=float,
        default=config_defaults.get("stale_after_s", 2.5),
    )
    parser.add_argument(
        "--down-after-s",
        type=float,
        default=config_defaults.get("down_after_s", 6.0),
    )
    args = parser.parse_args()
    if args.source not in {"sim", "radio", "both"}:
        parser.error("--source must be one of: sim, radio, both")
    return args


def main() -> int:
    args = parse_args()
    if args.source in {"radio", "both"}:
        args.iface = resolve_iface(args.iface, purpose=f"--source {args.source}")
    hop_channels = _parse_csv_ints(args.hop_channels)
    if args.channel_agility:
        if not hop_channels:
            raise SystemExit("--hop-channels is required when --channel-agility is enabled")
        if any(channel <= 0 for channel in hop_channels):
            raise SystemExit("--hop-channels must contain positive channel numbers")
        if args.hop_slot_ms <= 0:
            raise SystemExit("--hop-slot-ms must be > 0")
        if args.channel_settle_ms < 0:
            raise SystemExit("--channel-settle-ms must be >= 0")

    state = MeshState(
        source=args.source,
        stale_after=args.stale_after_s,
        down_after=args.down_after_s,
    )
    state.apply_node_config(load_node_config(args.nodes))

    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    if args.source in {"sim", "both"}:
        threads.append(
            threading.Thread(
                target=sim_loop,
                name="demo-sim",
                kwargs={
                    "state": state,
                    "stop_event": stop_event,
                    "interval_s": args.sim_interval_ms / 1000.0,
                },
                daemon=True,
            )
        )

    if args.source in {"radio", "both"}:
        threads.append(
            threading.Thread(
                target=radio_loop,
                name="radio-rx",
                kwargs={
                    "state": state,
                    "stop_event": stop_event,
                    "iface": args.iface,
                    "stream_id": args.stream_id,
                    "timeout_ms": args.radio_timeout_ms,
                    "channel_agility": args.channel_agility,
                    "hop_channels": hop_channels,
                    "channel_width": args.channel_width,
                    "hop_slot_ms": args.hop_slot_ms,
                    "hop_epoch_ms": args.hop_epoch_ms,
                    "channel_settle_ms": args.channel_settle_ms,
                    "channel_down_up": args.channel_down_up,
                },
                daemon=True,
            )
        )

    for thread in threads:
        thread.start()

    class _Server(ThreadingHTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True

    handler = make_handler(state, STATIC_DIR)
    server = _Server((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard listening on {url}")
    if args.source in {"radio", "both"}:
        print(f"Radio RX iface={args.iface} stream_id={args.stream_id}")
        if args.channel_agility:
            print(
                "Channel agility "
                f"channels={','.join(str(channel) for channel in hop_channels)} "
                f"slot_ms={args.hop_slot_ms} epoch_ms={args.hop_epoch_ms}"
            )

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopping dashboard")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
