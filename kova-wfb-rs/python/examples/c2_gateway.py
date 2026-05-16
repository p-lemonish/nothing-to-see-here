#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import json
import os
import subprocess
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque

from wfb_rs_py import Rx
from wfb_rs_py.app_proto import (
    ADDR_C2,
    AppFrameError,
    MSG_ROUTE_V2,
    TRAFFIC_C2_UPLINK,
    decode_frame,
    decode_route_v2_payload,
    decode_secure_payload,
)

MAX_U32 = 0xFFFF_FFFF
DEFAULT_STREAM_ID = 0xDEAD_BEEF
MESH_SECTION = "mesh"
GATEWAY_SECTION = "c2_gateway"


@dataclass(frozen=True)
class ScheduleState:
    utc_ms: int
    slot: int
    slot_elapsed_ms: int
    channel: int
    next_hop_ms: int


@dataclass(frozen=True)
class RunningMeshNode:
    pid: int
    iface: str
    stream_id: int | None
    config_path: str | None


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


def _default_iface() -> str | None:
    return os.getenv("NIC") or os.getenv("WFB_IFACE") or os.getenv("IFACE")


def _default_gateway_id() -> int:
    value = os.getenv("WFB_GATEWAY_ID") or os.getenv("GATEWAY_ID")
    if value is None:
        return 0
    return int(value, 0)


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text.lower() in {"", "none", "null"}:
        return None
    return value


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


def _load_config(path: str | None) -> dict[str, object]:
    if path is None:
        return {}

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)
    out: dict[str, object] = {}

    for section_name in (MESH_SECTION, GATEWAY_SECTION):
        if not parser.has_section(section_name):
            continue
        section = parser[section_name]
        for key in (
            "iface",
            "c2_http_forward_url",
            "local_tap_bind_host",
            "hop_channels",
            "channel_width",
        ):
            if key in section:
                value = _optional_text(section.get(key))
                if value is not None:
                    out[key] = value
        for key in (
            "stream_id",
            "gateway_id",
            "rx_timeout_ms",
            "seen_limit",
            "hop_slot_ms",
            "hop_epoch_ms",
            "channel_settle_ms",
            "local_tap_bind_port",
            "local_tap_max_datagram_bytes",
        ):
            if key in section:
                out[key] = int(section[key], 0)
        for key in (
            "rx_enabled",
            "print_rssi",
            "include_self",
            "channel_agility",
            "channel_down_up",
            "local_tap_enabled",
        ):
            if key in section:
                out[key] = section.getboolean(key)

    return out


def _decode_cmdline(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _cmd_option(tokens: list[str], name: str) -> str | None:
    prefix = f"{name}="
    for index, token in enumerate(tokens):
        if token == name and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _resolve_proc_path(pid: int, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        cwd = Path.cwd()
    return cwd / path


def _discover_running_mesh_node() -> RunningMeshNode | None:
    proc = Path("/proc")
    for entry in sorted(proc.iterdir(), key=lambda item: item.name):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue

        tokens = _decode_cmdline(entry / "cmdline")
        if not tokens:
            continue
        if not any(token.endswith("mesh_txrx.py") for token in tokens):
            continue

        config_path_text = _cmd_option(tokens, "--config")
        config_defaults: dict[str, object] = {}
        config_path: str | None = None
        if config_path_text is not None:
            resolved = _resolve_proc_path(pid, config_path_text)
            config_path = str(resolved)
            try:
                config_defaults = _load_config(str(resolved))
            except (OSError, ValueError, configparser.Error):
                config_defaults = {}

        iface = _cmd_option(tokens, "--iface") or config_defaults.get("iface")
        if iface is None:
            continue

        stream_id_text = _cmd_option(tokens, "--stream-id")
        stream_id: int | None = None
        if stream_id_text is not None:
            try:
                stream_id = int(stream_id_text, 0)
            except ValueError:
                stream_id = None
        elif "stream_id" in config_defaults:
            stream_id = int(config_defaults["stream_id"])

        return RunningMeshNode(
            pid=pid,
            iface=str(iface),
            stream_id=stream_id,
            config_path=config_path,
        )

    return None


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


def _post_c2_upload(
    *,
    c2_http_forward_url: str,
    gateway_id: int,
    channel: int | None,
    route,
) -> tuple[bool, str]:
    upload = {
        "gateway_id": gateway_id,
        "channel": channel,
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
    body = json.dumps(upload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        c2_http_forward_url,
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


def _post_c2_upload_object(
    *,
    c2_http_forward_url: str,
    upload: dict[str, object],
) -> tuple[bool, str]:
    body = json.dumps(upload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        c2_http_forward_url,
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


def main() -> int:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config")
    config_args, _ = config_parser.parse_known_args()
    try:
        config_defaults = _load_config(config_args.config)
    except (OSError, ValueError, configparser.Error) as exc:
        raise SystemExit(f"c2_gateway.py: config error: {exc}") from exc

    parser = argparse.ArgumentParser(
        description="RX-only gateway that forwards opaque c2_uplink route_v2 packets to C2",
        parents=[config_parser],
    )
    parser.add_argument(
        "--iface",
        default=config_defaults.get("iface", _default_iface()),
        help=(
            "monitor-mode interface; if omitted, c2_gateway tries to reuse "
            "the iface from a running mesh_txrx.py process"
        ),
    )
    parser.add_argument(
        "--rx",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("rx_enabled", True),
        help="enable RF RX capture; disable for localhost tap-only mode",
    )
    parser.add_argument(
        "--stream-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("stream_id", DEFAULT_STREAM_ID),
    )
    parser.add_argument(
        "--gateway-id",
        type=lambda value: int(value, 0),
        default=config_defaults.get("gateway_id", _default_gateway_id()),
    )
    parser.add_argument(
        "--c2-http-forward-url",
        default=config_defaults.get("c2_http_forward_url"),
        help="C2 /ingest URL, for example http://80.69.173.183:8080/ingest",
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
        help="capture frames injected by this host; useful when gateway and sender share a host",
    )
    parser.add_argument(
        "--channel-agility",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("channel_agility", False),
        help="hop across configured channels on the same wall-clock schedule as the mesh",
    )
    parser.add_argument(
        "--hop-channels",
        default=config_defaults.get("hop_channels"),
        help="comma-separated channel schedule, for example 36,40,48",
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
    )
    parser.add_argument(
        "--local-tap",
        action=argparse.BooleanOptionalAction,
        default=config_defaults.get("local_tap_enabled", False),
        help="listen for opaque C2 uploads from mesh_txrx over localhost UDP",
    )
    parser.add_argument(
        "--local-tap-bind-host",
        default=config_defaults.get("local_tap_bind_host", "127.0.0.1"),
    )
    parser.add_argument(
        "--local-tap-bind-port",
        type=int,
        default=config_defaults.get("local_tap_bind_port", 0),
    )
    parser.add_argument(
        "--local-tap-max-datagram-bytes",
        type=int,
        default=config_defaults.get("local_tap_max_datagram_bytes", 65535),
    )
    args = parser.parse_args()

    discovered_node: RunningMeshNode | None = None
    if args.rx and not args.iface:
        discovered_node = _discover_running_mesh_node()
        if discovered_node is not None:
            args.iface = discovered_node.iface
            if "stream_id" not in config_defaults and discovered_node.stream_id is not None:
                args.stream_id = discovered_node.stream_id

    if args.rx and not args.iface:
        parser.error(
            "no receiving interface is setup for C2 uplink: pass --iface, "
            "set NIC/WFB_IFACE/IFACE, or start mesh_txrx.py with a config "
            "that contains iface"
        )
    if not args.rx and not args.local_tap:
        parser.error("--no-rx requires --local-tap")
    if not 1 <= args.stream_id <= MAX_U32:
        parser.error("--stream-id must be in range 1..0xffffffff")
    if not 0 <= args.gateway_id <= 255:
        parser.error("--gateway-id must fit in u8")
    if args.rx_timeout_ms <= 0:
        parser.error("--rx-timeout-ms must be > 0")
    if args.seen_limit < 1:
        parser.error("--seen-limit must be > 0")
    if args.local_tap:
        if not 1 <= args.local_tap_bind_port <= 65535:
            parser.error("--local-tap-bind-port must be in range 1..65535")
        if args.local_tap_max_datagram_bytes < 256:
            parser.error("--local-tap-max-datagram-bytes must be >= 256")
    try:
        c2_http_forward_url = _normalize_ingest_url(args.c2_http_forward_url)
    except ValueError as exc:
        parser.error(str(exc))
    if c2_http_forward_url is None:
        parser.error("--c2-http-forward-url is required")

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

    seen = SeenRoutes(args.seen_limit)
    current_channel: int | None = None
    rx: Rx | None = None
    local_tap_socket: socket.socket | None = None

    def log(message: str) -> None:
        channel = "?" if current_channel is None else str(current_channel)
        print(f"CH={channel} {message}")

    def close_radio() -> None:
        nonlocal rx
        if rx is not None:
            rx.close()
            rx = None

    def open_radio() -> None:
        nonlocal rx
        if not args.rx:
            return
        rx = Rx(
            iface=args.iface,
            stream_id=args.stream_id,
            ignore_self_injected=not args.include_self,
        )

    def open_local_tap() -> None:
        nonlocal local_tap_socket
        if not args.local_tap:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((args.local_tap_bind_host, args.local_tap_bind_port))
        sock.setblocking(False)
        local_tap_socket = sock
        log(
            f"LOCAL_TAP listening "
            f"addr={args.local_tap_bind_host}:{args.local_tap_bind_port}"
        )

    def close_local_tap() -> None:
        nonlocal local_tap_socket
        if local_tap_socket is not None:
            local_tap_socket.close()
            local_tap_socket = None

    def drain_local_tap() -> None:
        if local_tap_socket is None:
            return
        while True:
            try:
                data, peer = local_tap_socket.recvfrom(args.local_tap_max_datagram_bytes)
            except BlockingIOError:
                return
            except OSError as exc:
                log(f'LOCAL_TAP recv_error="{exc}"')
                return

            source = f"{peer[0]}:{peer[1]}"
            try:
                upload = json.loads(data.decode("utf-8"))
                if not isinstance(upload, dict):
                    raise ValueError("tap payload must be a JSON object")
                route = upload.get("route")
                if not isinstance(route, dict):
                    raise ValueError("tap payload route must be a JSON object")
                origin_id = int(route.get("origin_id"))
                origin_seq = int(route.get("origin_seq"))
                key = (origin_id, origin_seq)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log(f'LOCAL_TAP reject from={source} error="{exc}"')
                continue

            if not seen.remember(key):
                log(
                    f"LOCAL_TAP duplicate origin={origin_id} "
                    f"seq={origin_seq} dropped=1"
                )
                continue

            ok, detail = _post_c2_upload_object(
                c2_http_forward_url=c2_http_forward_url,
                upload=upload,
            )
            log(
                f"HTTP c2_forward source=local_tap origin=node:{origin_id} "
                f"seq={origin_seq} ok={int(ok)} detail=\"{detail}\""
            )

    def current_schedule_state() -> ScheduleState | None:
        if not args.channel_agility:
            return None
        return _schedule_state(hop_channels, args.hop_slot_ms, args.hop_epoch_ms)

    def switch_channel(state: ScheduleState) -> None:
        nonlocal current_channel
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
        log(
            f"CHANNEL active iface={args.iface} width={args.channel_width} "
            f"utc_ms={state.utc_ms} slot={state.slot} "
            f"next_hop_ms={state.next_hop_ms}"
        )

    try:
        if args.rx and args.channel_agility:
            switch_channel(
                _schedule_state(hop_channels, args.hop_slot_ms, args.hop_epoch_ms)
            )
        elif args.rx:
            open_radio()
        open_local_tap()

        log(
            f"C2 gateway RX mode: gateway_id={args.gateway_id} "
            f"forward_url={c2_http_forward_url}, Ctrl-C to exit"
        )
        if discovered_node is not None:
            log(
                f"RX source=running_mesh_node pid={discovered_node.pid} "
                f"iface={discovered_node.iface} "
                f"stream_id=0x{args.stream_id:08x} "
                f"config={discovered_node.config_path or 'unknown'}"
            )
        else:
            log(
                f"RX source=configured_iface iface={args.iface} "
                f"stream_id=0x{args.stream_id:08x}"
            )
        if not args.rx:
            log("RX source=disabled")

        while True:
            drain_local_tap()
            state = current_schedule_state()
            if args.rx and state is not None and state.channel != current_channel:
                switch_channel(state)
                continue

            if not args.rx:
                time.sleep(args.rx_timeout_ms / 1000.0)
                continue

            if rx is None:
                raise RuntimeError("RX handle is not open")

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

            if frame.message_type != MSG_ROUTE_V2:
                continue

            try:
                route = decode_route_v2_payload(frame.payload)
            except AppFrameError as exc:
                log(
                    f'RX invalid_route_v2 from={frame.sender_id} '
                    f'len={len(frame.payload)} error="{exc}"'
                )
                continue

            if route.traffic_class != TRAFFIC_C2_UPLINK or route.destination_type != ADDR_C2:
                continue

            secure_desc = "unknown"
            try:
                secure = decode_secure_payload(route.inner_payload)
            except AppFrameError as exc:
                log(
                    f'RX invalid_e2e via={frame.sender_id} '
                    f"origin=node:{route.origin_id} seq={route.origin_seq} "
                    f'error="{exc}" dropped=1'
                )
                continue
            else:
                secure_desc = (
                    f"domain={secure.security_domain_name} "
                    f"key_id={secure.key_id} key_epoch={secure.key_epoch}"
                )

            dedupe_key = route.dedupe_key
            if not seen.remember(dedupe_key):
                log(
                    f"RX duplicate_c2_uplink via={frame.sender_id} "
                    f"origin={route.origin_type_name}:{route.origin_id} "
                    f"seq={route.origin_seq} dropped=1"
                )
                continue

            suffix = ""
            if args.print_rssi:
                suffix = (
                    f" bw={meta.bandwidth} mcs={meta.mcs_index} "
                    f"rssi0={meta.rssi[0]}"
                )

            ok, detail = _post_c2_upload(
                c2_http_forward_url=c2_http_forward_url,
                gateway_id=args.gateway_id,
                channel=current_channel,
                route=route,
            )
            log(
                f"HTTP c2_forward via={frame.sender_id} "
                f"origin={route.origin_type_name}:{route.origin_id} "
                f"seq={route.origin_seq} dest={route.destination_type_name}:{route.destination_id} "
                f"{secure_desc} ok={int(ok)} detail=\"{detail}\"{suffix}"
            )
    except KeyboardInterrupt:
        return 130
    finally:
        close_local_tap()
        close_radio()


if __name__ == "__main__":
    raise SystemExit(main())
