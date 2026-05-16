#!/usr/bin/env python3
"""Drone story mesh — state-driven narrative demo for Kova Labs hackathon.

Each node loads a plain .txt file (one line = one story fragment) and
broadcasts lines over the mesh as MSG_STORY frames. The node's current
state (NOMINAL/DEGRADED/RECOVERY/ISOLATED/MOVING/RTB) is determined
automatically from heartbeat health and tagged onto every outgoing fragment.

On reconnect, the node retransmits all fragments sent while isolated so
every peer gets the complete story.
"""
from __future__ import annotations

import argparse
import configparser
import os
import struct
import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Deque

from wfb_rs_py import Rx, Tx
from wfb_rs_py.app_proto import (
    AppFrameError,
    MSG_ROUTE_DATA,
    MSG_STATUS,
    MSG_STORY,
    MSG_STORY_STATE,
    decode_frame,
    decode_route_data_payload,
    encode_frame,
    encode_route_data_payload,
)

MAX_U32 = 0xFFFF_FFFF
CONFIG_SECTION = "story"

# StoryFragment wire format: fragment_id(u32) author_id(u8) node_state(u8) text_len(u16)
FRAG_HEADER = struct.Struct("!IBBH")
FRAG_HEADER_SIZE = FRAG_HEADER.size  # 8 bytes

# Author entry in MSG_STORY_STATE: author_id(u8) max_fragment_id(u32)
AUTHOR_ENTRY = struct.Struct("!BI")
AUTHOR_ENTRY_SIZE = AUTHOR_ENTRY.size  # 5 bytes


class DroneState(IntEnum):
    NOMINAL = 0
    DEGRADED = 1
    RECOVERY = 2
    ISOLATED = 3
    MOVING = 4
    RTB = 5


@dataclass(frozen=True)
class StoryFragment:
    fragment_id: int
    author_id: int
    node_state: int
    text: str

    def encode(self) -> bytes:
        text_bytes = self.text.encode("utf-8")
        return FRAG_HEADER.pack(
            self.fragment_id, self.author_id, self.node_state, len(text_bytes)
        ) + text_bytes


def _decode_story_fragment(data: bytes) -> StoryFragment:
    if len(data) < FRAG_HEADER_SIZE:
        raise ValueError(f"fragment too short: {len(data)}")
    fragment_id, author_id, node_state, text_len = FRAG_HEADER.unpack_from(data)
    text_data = data[FRAG_HEADER_SIZE:]
    if len(text_data) != text_len:
        raise ValueError(f"text_len mismatch: header={text_len} actual={len(text_data)}")
    return StoryFragment(
        fragment_id=fragment_id,
        author_id=author_id,
        node_state=node_state,
        text=text_data.decode("utf-8", errors="replace"),
    )


class LinkMonitor:
    def __init__(self, *, threshold_ms: int) -> None:
        self._threshold_s = threshold_ms / 1000.0
        self._last_seen: dict[int, float] = {}

    def update(self, peer_id: int) -> None:
        self._last_seen[peer_id] = time.monotonic()

    def healthy_peers(self) -> set[int]:
        now = time.monotonic()
        return {
            pid for pid, ts in self._last_seen.items()
            if now - ts <= self._threshold_s
        }

    def has_ever_seen_peers(self) -> bool:
        return bool(self._last_seen)


class StateTransition:
    def __init__(
        self,
        *,
        degraded_timeout_ms: int,
        moving_timeout_ms: int,
        recovery_stable_ms: int,
    ) -> None:
        self._degraded_timeout_s = degraded_timeout_ms / 1000.0
        self._moving_timeout_s = moving_timeout_ms / 1000.0
        self._recovery_stable_s = recovery_stable_ms / 1000.0
        self._state = DroneState.NOMINAL
        self._entered_at = time.monotonic()
        self.isolation_start_fragment_id: int = 0

    @property
    def state(self) -> DroneState:
        return self._state

    def _enter(self, new_state: DroneState) -> None:
        self._state = new_state
        self._entered_at = time.monotonic()

    def tick(
        self, link_monitor: LinkMonitor, current_fragment_id: int
    ) -> tuple[DroneState, bool]:
        old = self._state
        age = time.monotonic() - self._entered_at
        healthy = link_monitor.healthy_peers()
        has_peers = link_monitor.has_ever_seen_peers()

        if self._state == DroneState.NOMINAL:
            if has_peers and not healthy:
                self._enter(DroneState.DEGRADED)

        elif self._state == DroneState.DEGRADED:
            if healthy:
                self._enter(DroneState.NOMINAL)
            elif age > self._degraded_timeout_s:
                self.isolation_start_fragment_id = current_fragment_id
                self._enter(DroneState.ISOLATED)

        elif self._state == DroneState.ISOLATED:
            if healthy:
                self._enter(DroneState.RECOVERY)
            elif age > self._moving_timeout_s:
                self._enter(DroneState.MOVING)

        elif self._state == DroneState.MOVING:
            if healthy:
                self._enter(DroneState.RECOVERY)
            elif age > self._moving_timeout_s:
                self._enter(DroneState.RTB)

        elif self._state == DroneState.RTB:
            if healthy:
                self._enter(DroneState.RECOVERY)

        elif self._state == DroneState.RECOVERY:
            if not healthy:
                self._enter(DroneState.DEGRADED if has_peers else DroneState.ISOLATED)
            elif age > self._recovery_stable_s:
                self._enter(DroneState.NOMINAL)

        return self._state, self._state != old


class StoryFileReader:
    def __init__(self, path: str, *, loop: bool = True) -> None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"story file not found: {path}")
        lines = [
            line.rstrip()
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            raise ValueError(f"story file contains no usable lines: {path}")
        self._lines = lines
        self._loop = loop
        self._idx = 0

    def next_line(self) -> str | None:
        if self._idx >= len(self._lines):
            if self._loop:
                self._idx = 0
            else:
                return None
        line = self._lines[self._idx]
        self._idx += 1
        return line


class OutgoingBuffer:
    def __init__(self) -> None:
        self._frags: list[StoryFragment] = []

    def add(self, frag: StoryFragment) -> None:
        self._frags.append(frag)

    def since(self, fragment_id: int) -> list[StoryFragment]:
        return [f for f in self._frags if f.fragment_id > fragment_id]


class StoryLog:
    def __init__(self, *, max_entries: int = 500) -> None:
        self._max = max_entries
        self._store: dict[tuple[int, int], StoryFragment] = {}
        self._order: Deque[tuple[int, int]] = deque()
        self.peer_max_seen: dict[int, int] = {}

    def merge(self, fragments: list[StoryFragment]) -> list[StoryFragment]:
        new: list[StoryFragment] = []
        for frag in fragments:
            key = (frag.author_id, frag.fragment_id)
            if key not in self._store:
                self._store[key] = frag
                self._order.append(key)
                new.append(frag)
                prev = self.peer_max_seen.get(frag.author_id, 0)
                if frag.fragment_id > prev:
                    self.peer_max_seen[frag.author_id] = frag.fragment_id
        while len(self._order) > self._max:
            old_key = self._order.popleft()
            self._store.pop(old_key, None)
        return new

    def last_n(self, n: int) -> list[StoryFragment]:
        keys = list(self._order)[-n:]
        return [self._store[k] for k in keys if k in self._store]

    def encode_state_payload(self, last_n: int = 10) -> bytes:
        fragments = self.last_n(last_n)
        author_entries = sorted(self.peer_max_seen.items())
        parts: list[bytes] = [struct.pack("!B", len(author_entries))]
        for author_id, max_fid in author_entries:
            parts.append(AUTHOR_ENTRY.pack(author_id, max_fid))
        parts.append(struct.pack("!B", len(fragments)))
        for frag in fragments:
            encoded = frag.encode()
            parts.append(struct.pack("!H", len(encoded)))
            parts.append(encoded)
        return b"".join(parts)

    @staticmethod
    def decode_state_payload(data: bytes) -> tuple[dict[int, int], list[StoryFragment]]:
        offset = 0
        if offset + 1 > len(data):
            raise ValueError("payload too short for author_count")
        author_count = data[offset]
        offset += 1

        peer_max: dict[int, int] = {}
        for _ in range(author_count):
            if offset + AUTHOR_ENTRY_SIZE > len(data):
                raise ValueError("payload truncated in author entries")
            author_id, max_fid = AUTHOR_ENTRY.unpack_from(data, offset)
            peer_max[author_id] = max_fid
            offset += AUTHOR_ENTRY_SIZE

        if offset + 1 > len(data):
            raise ValueError("payload too short for fragment_count")
        fragment_count = data[offset]
        offset += 1

        fragments: list[StoryFragment] = []
        for _ in range(fragment_count):
            if offset + 2 > len(data):
                raise ValueError("payload truncated in fragment size field")
            (frag_len,) = struct.unpack_from("!H", data, offset)
            offset += 2
            if offset + frag_len > len(data):
                raise ValueError("payload truncated in fragment data")
            fragments.append(_decode_story_fragment(data[offset : offset + frag_len]))
            offset += frag_len

        return peer_max, fragments


class _SeenRoutes:
    def __init__(self, limit: int) -> None:
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


def _next_seq(seq: int) -> int:
    seq = (seq + 1) & MAX_U32
    return 1 if seq == 0 else seq


def _default_iface() -> str | None:
    return os.getenv("NIC") or os.getenv("WFB_IFACE") or os.getenv("IFACE")


def _default_sender_id() -> int | None:
    value = os.getenv("WFB_SENDER_ID") or os.getenv("SENDER_ID")
    if value is None:
        return None
    return int(value, 0)


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
    for key in ("iface", "story_file"):
        if key in section:
            val = section.get(key, "").strip()
            if val and val.lower() not in {"none", "null"}:
                out[key] = val
    for key in (
        "stream_id", "sender_id", "destination_id", "ttl",
        "tx_interval_ms", "rx_timeout_ms", "seen_limit",
        "heartbeat_interval_ms", "state_broadcast_interval_ms",
        "retransmit_interval_ms", "catchup_wait_ms",
        "heartbeat_threshold_ms", "degraded_timeout_ms",
        "moving_timeout_ms", "recovery_stable_ms",
    ):
        if key in section:
            out[key] = int(section[key], 0)
    for key in ("loop", "print_rssi", "include_self"):
        if key in section:
            out[key] = section.getboolean(key)
    return out


def _state_label(node_state: int) -> str:
    try:
        return DroneState(node_state).name
    except ValueError:
        return f"STATE_{node_state}"


def _format_fragment(frag: StoryFragment) -> str:
    return f"[Node {frag.author_id} | {_state_label(frag.node_state):<8}] {frag.text}"


def main() -> int:
    config_pre = argparse.ArgumentParser(add_help=False)
    config_pre.add_argument("--config")
    config_args, _ = config_pre.parse_known_args()
    try:
        cfg = _load_config(config_args.config)
    except (OSError, ValueError, configparser.Error) as exc:
        raise SystemExit(f"story_mesh.py: config error: {exc}") from exc

    parser = argparse.ArgumentParser(
        description="Drone story mesh — state-driven narrative demo",
        parents=[config_pre],
    )
    parser.add_argument("--iface", default=cfg.get("iface", _default_iface()))
    parser.add_argument(
        "--stream-id", type=lambda v: int(v, 0), default=cfg.get("stream_id", 1)
    )
    parser.add_argument(
        "--sender-id",
        type=lambda v: int(v, 0),
        default=cfg.get("sender_id", _default_sender_id()),
    )
    parser.add_argument(
        "--destination-id",
        type=lambda v: int(v, 0),
        default=cfg.get("destination_id", 0),
    )
    parser.add_argument(
        "--ttl", type=lambda v: int(v, 0), default=cfg.get("ttl", 2)
    )
    parser.add_argument(
        "--story-file",
        default=cfg.get("story_file"),
        help="path to .txt file with one story fragment per line",
    )
    parser.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=cfg.get("loop", True),
        help="restart from line 1 when the story file is exhausted",
    )
    parser.add_argument(
        "--tx-interval-ms",
        type=int,
        default=cfg.get("tx_interval_ms", 2000),
        help="ms between consecutive story fragment transmissions",
    )
    parser.add_argument(
        "--heartbeat-interval-ms",
        type=int,
        default=cfg.get("heartbeat_interval_ms", 1000),
    )
    parser.add_argument(
        "--state-broadcast-interval-ms",
        type=int,
        default=cfg.get("state_broadcast_interval_ms", 4000),
    )
    parser.add_argument(
        "--retransmit-interval-ms",
        type=int,
        default=cfg.get("retransmit_interval_ms", 200),
        help="ms between each fragment during retransmit burst on reconnect",
    )
    parser.add_argument(
        "--catchup-wait-ms",
        type=int,
        default=cfg.get("catchup_wait_ms", 6000),
        help="ms to wait for a state-broadcast before unconditional retransmit",
    )
    parser.add_argument(
        "--heartbeat-threshold-ms",
        type=int,
        default=cfg.get("heartbeat_threshold_ms", 6000),
        help="ms without a peer heartbeat before that peer is considered stale",
    )
    parser.add_argument(
        "--degraded-timeout-ms",
        type=int,
        default=cfg.get("degraded_timeout_ms", 15000),
    )
    parser.add_argument(
        "--moving-timeout-ms",
        type=int,
        default=cfg.get("moving_timeout_ms", 20000),
    )
    parser.add_argument(
        "--recovery-stable-ms",
        type=int,
        default=cfg.get("recovery_stable_ms", 8000),
    )
    parser.add_argument(
        "--rx-timeout-ms", type=int, default=cfg.get("rx_timeout_ms", 50)
    )
    parser.add_argument(
        "--seen-limit", type=int, default=cfg.get("seen_limit", 4096)
    )
    parser.add_argument(
        "--print-rssi",
        action="store_true",
        default=cfg.get("print_rssi", False),
    )
    parser.add_argument(
        "--include-self",
        action="store_true",
        default=cfg.get("include_self", False),
    )
    args = parser.parse_args()

    if not args.iface:
        parser.error("--iface is required (or set NIC / WFB_IFACE / IFACE)")
    if args.stream_id == 0:
        parser.error("--stream-id must be non-zero")
    if args.sender_id is None:
        parser.error("--sender-id is required (or set WFB_SENDER_ID / SENDER_ID)")
    if not 1 <= args.sender_id <= 255:
        parser.error("--sender-id must be 1..255")
    if not 0 <= args.destination_id <= 255:
        parser.error("--destination-id must be 0..255")
    if not 0 <= args.ttl <= 255:
        parser.error("--ttl must be 0..255")
    if not args.story_file:
        parser.error("--story-file is required")

    try:
        reader = StoryFileReader(args.story_file, loop=args.loop)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    link_monitor = LinkMonitor(threshold_ms=args.heartbeat_threshold_ms)
    state_machine = StateTransition(
        degraded_timeout_ms=args.degraded_timeout_ms,
        moving_timeout_ms=args.moving_timeout_ms,
        recovery_stable_ms=args.recovery_stable_ms,
    )
    outgoing = OutgoingBuffer()
    story_log = StoryLog()
    seen_routes = _SeenRoutes(args.seen_limit)

    next_origin_seq = 1
    next_outer_seq = 1
    next_rf_seq = 1
    next_fragment_id = 1
    current_fragment_id = 0

    now = time.monotonic()
    next_tx_at = now + args.tx_interval_ms / 1000.0
    next_hb_at = now
    next_state_bcast_at = now + args.state_broadcast_interval_ms / 1000.0

    retransmit_queue: list[StoryFragment] = []
    retransmit_at = 0.0
    recovery_entered_at: float | None = None
    catchup_done = False

    tx: Tx | None = None
    rx: Rx | None = None

    def log(msg: str) -> None:
        print(f"[{state_machine.state.name:<8}] {msg}", flush=True)

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

    def close_radio() -> None:
        nonlocal tx, rx
        if rx is not None:
            rx.close()
            rx = None
        if tx is not None:
            tx.close()
            tx = None

    def send_route(*, inner_type: int, payload: bytes) -> None:
        nonlocal next_origin_seq, next_outer_seq, next_rf_seq
        if tx is None:
            return
        seq = next_origin_seq
        route_payload = encode_route_data_payload(
            origin_sender_id=args.sender_id,
            destination_id=args.destination_id,
            ttl=args.ttl,
            origin_seq=seq,
            inner_type=inner_type,
            inner_payload=payload,
        )
        frame = encode_frame(
            sender_id=args.sender_id,
            message_type=MSG_ROUTE_DATA,
            app_seq=next_outer_seq,
            payload=route_payload,
        )
        tx.send(frame, seq=next_rf_seq)
        seen_routes.remember((args.sender_id, seq))
        next_origin_seq = _next_seq(next_origin_seq)
        next_outer_seq = _next_seq(next_outer_seq)
        next_rf_seq = _next_seq(next_rf_seq)

    def forward_route(route: object) -> None:
        nonlocal next_outer_seq, next_rf_seq
        if tx is None or route.ttl <= 0:  # type: ignore[union-attr]
            return
        fwd = route.decremented_ttl()  # type: ignore[union-attr]
        fwd_payload = encode_route_data_payload(
            origin_sender_id=fwd.origin_sender_id,
            destination_id=fwd.destination_id,
            ttl=fwd.ttl,
            origin_seq=fwd.origin_seq,
            inner_type=fwd.inner_type,
            inner_payload=fwd.inner_payload,
        )
        fwd_frame = encode_frame(
            sender_id=args.sender_id,
            message_type=MSG_ROUTE_DATA,
            app_seq=next_outer_seq,
            payload=fwd_payload,
        )
        tx.send(fwd_frame, seq=next_rf_seq)
        next_outer_seq = _next_seq(next_outer_seq)
        next_rf_seq = _next_seq(next_rf_seq)

    try:
        open_radio()
        log(
            f"story_mesh started node={args.sender_id} iface={args.iface} "
            f"stream={args.stream_id} file={args.story_file}"
        )

        while True:
            now = time.monotonic()

            # --- TX: heartbeat ---
            if now >= next_hb_at:
                send_route(inner_type=MSG_STATUS, payload=b"hb")
                next_hb_at = now + args.heartbeat_interval_ms / 1000.0

            # --- TX: next story fragment ---
            if now >= next_tx_at:
                line = reader.next_line()
                if line is not None:
                    frag = StoryFragment(
                        fragment_id=next_fragment_id,
                        author_id=args.sender_id,
                        node_state=int(state_machine.state),
                        text=line,
                    )
                    next_fragment_id += 1
                    current_fragment_id = frag.fragment_id
                    outgoing.add(frag)
                    story_log.merge([frag])
                    send_route(inner_type=MSG_STORY, payload=frag.encode())
                    print(_format_fragment(frag), flush=True)
                next_tx_at = now + args.tx_interval_ms / 1000.0

            # --- TX: story state broadcast ---
            if now >= next_state_bcast_at:
                send_route(
                    inner_type=MSG_STORY_STATE,
                    payload=story_log.encode_state_payload(last_n=10),
                )
                next_state_bcast_at = now + args.state_broadcast_interval_ms / 1000.0

            # --- TX: retransmit burst on reconnect ---
            if retransmit_queue and now >= retransmit_at:
                frag = retransmit_queue.pop(0)
                send_route(inner_type=MSG_STORY, payload=frag.encode())
                log(
                    f"RETX fragment_id={frag.fragment_id} "
                    f"remaining={len(retransmit_queue)}"
                )
                retransmit_at = now + args.retransmit_interval_ms / 1000.0

            # --- Fallback catchup: no state broadcast received within window ---
            if (
                recovery_entered_at is not None
                and not catchup_done
                and not retransmit_queue
                and now - recovery_entered_at > args.catchup_wait_ms / 1000.0
            ):
                missed = outgoing.since(state_machine.isolation_start_fragment_id - 1)
                if missed:
                    log(
                        f"CATCHUP fallback: no state-broadcast received, "
                        f"retransmitting {len(missed)} fragments from isolation"
                    )
                    retransmit_queue.extend(missed)
                    retransmit_at = now
                catchup_done = True

            # --- RX ---
            result = rx.recv_optional(timeout_ms=args.rx_timeout_ms)

            # Tick state machine once per loop iteration
            new_state, transitioned = state_machine.tick(link_monitor, current_fragment_id)
            if transitioned:
                log(f"STATE → {new_state.name}")
                if new_state == DroneState.RECOVERY:
                    recovery_entered_at = time.monotonic()
                    catchup_done = False
                elif new_state not in (DroneState.RECOVERY,):
                    recovery_entered_at = None
                    catchup_done = False

            if result is None:
                continue

            raw, meta = result

            try:
                frame = decode_frame(raw, allow_unknown_message_type=True)
            except AppFrameError as exc:
                log(f"RX invalid frame: {exc}")
                continue

            if frame.message_type != MSG_ROUTE_DATA:
                continue

            try:
                route = decode_route_data_payload(frame.payload)
            except AppFrameError as exc:
                log(f"RX invalid route: {exc}")
                continue

            # Track both the forwarding node and the origin as live peers
            link_monitor.update(frame.sender_id)
            link_monitor.update(route.origin_sender_id)

            # Skip frames we originated
            if route.origin_sender_id == args.sender_id:
                seen_routes.remember(route.dedupe_key)
                continue

            # Dedup
            if not seen_routes.remember(route.dedupe_key):
                continue

            delivered = route.destination_id in (0, args.sender_id)

            if delivered:
                if route.inner_type == MSG_STORY:
                    try:
                        frag = _decode_story_fragment(route.inner_payload)
                    except ValueError as exc:
                        log(f"RX invalid story fragment: {exc}")
                    else:
                        for new_frag in story_log.merge([frag]):
                            print(_format_fragment(new_frag), flush=True)

                elif route.inner_type == MSG_STORY_STATE:
                    try:
                        peer_max, frags = StoryLog.decode_state_payload(route.inner_payload)
                    except ValueError as exc:
                        log(f"RX invalid story state: {exc}")
                    else:
                        # Merge peer knowledge
                        for author_id, max_fid in peer_max.items():
                            if author_id != args.sender_id:
                                prev = story_log.peer_max_seen.get(author_id, 0)
                                if max_fid > prev:
                                    story_log.peer_max_seen[author_id] = max_fid

                        for new_frag in story_log.merge(frags):
                            print(_format_fragment(new_frag), flush=True)

                        # Catchup: retransmit fragments peers missed while we were isolated
                        if (
                            state_machine.state == DroneState.RECOVERY
                            and recovery_entered_at is not None
                            and not catchup_done
                            and not retransmit_queue
                        ):
                            my_max_at_peer = peer_max.get(args.sender_id, 0)
                            missed = outgoing.since(my_max_at_peer)
                            if missed:
                                log(
                                    f"CATCHUP: peer has our fragments up to "
                                    f"{my_max_at_peer}, retransmitting {len(missed)}"
                                )
                                retransmit_queue.extend(missed)
                                retransmit_at = time.monotonic()
                            catchup_done = True

                elif route.inner_type == MSG_STATUS:
                    pass  # heartbeat — link_monitor already updated above

                else:
                    if args.print_rssi:
                        suffix = f" rssi0={meta.rssi[0]}"
                    else:
                        suffix = ""
                    log(
                        f"RX via={frame.sender_id} origin={route.origin_sender_id} "
                        f"type={route.inner_type_name}{suffix}"
                    )

            # Forward to the rest of the mesh
            forward_route(route)

    except KeyboardInterrupt:
        return 130
    finally:
        close_radio()


if __name__ == "__main__":
    raise SystemExit(main())
