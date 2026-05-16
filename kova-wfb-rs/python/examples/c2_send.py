#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import json
import socket
from pathlib import Path


LOCAL_CONTROL_SECTION = "local_control"
DEFAULT_HOST = "127.0.0.1"


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
    out: dict[str, object] = {}
    if parser.has_section(LOCAL_CONTROL_SECTION):
        section = parser[LOCAL_CONTROL_SECTION]
        if "bind_host" in section:
            value = _optional_text(section.get("bind_host"))
            if value is not None:
                out["host"] = value
        if "bind_port" in section:
            out["port"] = int(section["bind_port"], 0)
    return out


def main() -> int:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config")
    config_args, _ = config_parser.parse_known_args()
    try:
        config_defaults = _load_config(config_args.config)
    except (OSError, ValueError, configparser.Error) as exc:
        raise SystemExit(f"c2_send.py: config error: {exc}") from exc

    parser = argparse.ArgumentParser(
        description="Submit one local C2 uplink request to a running mesh_txrx.py node",
        parents=[config_parser],
    )
    parser.add_argument("--host", default=config_defaults.get("host", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=config_defaults.get("port", 0))
    parser.add_argument("--message", help="UTF-8 payload text")
    parser.add_argument("--payload-hex", help="raw payload bytes as hex")
    parser.add_argument("--message-type", default="data")
    parser.add_argument("--destination-id", type=lambda value: int(value, 0), default=1)
    parser.add_argument("--ttl", type=lambda value: int(value, 0), default=2)
    parser.add_argument("--timeout-ms", type=int, default=500)
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("--port is required and must be in range 1..65535")
    if args.message is None and args.payload_hex is None:
        parser.error("--message or --payload-hex is required")
    if args.message is not None and args.payload_hex is not None:
        parser.error("--message and --payload-hex are mutually exclusive")
    if not 1 <= args.destination_id <= 255:
        parser.error("--destination-id must be in range 1..255")
    if not 0 <= args.ttl <= 255:
        parser.error("--ttl must be in range 0..255")

    request: dict[str, object] = {
        "traffic_class": "c2_uplink",
        "destination_type": "c2",
        "destination_id": args.destination_id,
        "ttl": args.ttl,
        "message_type": args.message_type,
    }
    if args.payload_hex is not None:
        request["payload_hex"] = args.payload_hex
    else:
        request["message"] = args.message

    payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(args.timeout_ms / 1000.0)
        sock.sendto(payload, (args.host, args.port))
    finally:
        sock.close()

    print(
        f"queued c2_uplink host={args.host} port={args.port} "
        f"type={args.message_type} len={len(payload)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
