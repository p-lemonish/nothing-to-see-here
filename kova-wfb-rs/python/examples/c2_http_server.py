#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import html
import json
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any

from wfb_rs_py.app_proto import (
    ADDR_C2,
    AppFrameError,
    CHACHA20_POLY1305_KEY_SIZE,
    SEC_DOMAIN_NODE_TO_C2,
    TRAFFIC_C2_UPLINK,
    address_type_name,
    address_type_value,
    decode_secure_payload,
    decrypt_secure_payload,
    encode_route_v2_e2e_associated_data,
    message_type_name,
    message_type_value,
    security_domain_name,
    security_domain_value,
    traffic_class_name,
    traffic_class_value,
)

MAX_EVENTS = 1000


@dataclass(frozen=True)
class C2Key:
    security_domain: int
    key_id: int
    key_epoch: int
    key: bytes
    label: str


class C2EventStore:
    def __init__(self, keyring: dict[tuple[int, int, int], C2Key], max_events: int):
        self._keyring = keyring
        self._max_events = max_events
        self._events: deque[dict[str, Any]] = deque()
        self._latest_by_node: dict[int, dict[str, Any]] = {}
        self._seen: set[tuple[int, int, int, int, int, int]] = set()
        self._lock = Lock()

    def ingest(self, upload: dict[str, Any], *, client_ip: str) -> dict[str, Any]:
        route = upload.get("route")
        if not isinstance(route, dict):
            raise AppFrameError("upload must contain a route object")

        origin_type = _field_address_type(route, "origin_type")
        origin_id = _field_u8(route, "origin_id")
        destination_type = _field_address_type(route, "destination_type")
        destination_id = _field_u8(route, "destination_id")
        ttl = _field_u8(route, "ttl")
        origin_seq = _field_u32(route, "origin_seq")
        traffic_class = _field_traffic_class(route, "traffic_class")
        inner_type = _field_message_type(route, "inner_type")
        inner_payload_hex = route.get("inner_payload_hex")
        if not isinstance(inner_payload_hex, str):
            raise AppFrameError("route.inner_payload_hex must be a hex string")

        if destination_type != ADDR_C2:
            raise AppFrameError("only destination_type=c2 uploads are accepted")
        if traffic_class != TRAFFIC_C2_UPLINK:
            raise AppFrameError("only traffic_class=c2_uplink uploads are accepted")

        try:
            inner_payload = bytes.fromhex(inner_payload_hex)
        except ValueError as exc:
            raise AppFrameError("route.inner_payload_hex is not valid hex") from exc

        secure = decode_secure_payload(inner_payload)
        if secure.security_domain != SEC_DOMAIN_NODE_TO_C2:
            raise AppFrameError(
                "unexpected security_domain: "
                f"{secure.security_domain_name}; expected=node_to_c2"
            )

        key = self._keyring.get(
            (secure.security_domain, secure.key_id, secure.key_epoch)
        )
        if key is None:
            raise AppFrameError(
                "no key for secure payload: "
                f"domain={secure.security_domain_name} "
                f"key_id={secure.key_id} key_epoch={secure.key_epoch}"
            )

        aad = encode_route_v2_e2e_associated_data(
            origin_type=origin_type,
            origin_id=origin_id,
            destination_type=destination_type,
            destination_id=destination_id,
            origin_seq=origin_seq,
            traffic_class=traffic_class,
            inner_type=inner_type,
            inner_plaintext_len=secure.plaintext_len,
        )
        plaintext = decrypt_secure_payload(secure, key=key.key, associated_data=aad)
        dedupe_key = (
            origin_type,
            origin_id,
            origin_seq,
            secure.security_domain,
            secure.key_id,
            secure.key_epoch,
        )

        event = {
            "received_at_ms": _utc_ms(),
            "client_ip": client_ip,
            "gateway_id": upload.get("gateway_id"),
            "gateway_channel": upload.get("channel"),
            "origin_type": address_type_name(origin_type),
            "origin_id": origin_id,
            "destination_type": address_type_name(destination_type),
            "destination_id": destination_id,
            "ttl": ttl,
            "origin_seq": origin_seq,
            "traffic_class": traffic_class_name(traffic_class),
            "inner_type": message_type_name(inner_type),
            "security_domain": secure.security_domain_name,
            "key_id": secure.key_id,
            "key_epoch": secure.key_epoch,
            "key_label": key.label,
            "payload_len": len(plaintext),
            "payload_text": plaintext.decode("utf-8", errors="replace"),
            "duplicate": False,
        }

        with self._lock:
            if dedupe_key in self._seen:
                event["duplicate"] = True
                return event

            self._seen.add(dedupe_key)
            self._events.appendleft(event)
            self._latest_by_node[origin_id] = event
            while len(self._events) > self._max_events:
                old = self._events.pop()
                self._seen.discard(
                    (
                        address_type_value(old["origin_type"]),
                        old["origin_id"],
                        old["origin_seq"],
                        security_domain_value(old["security_domain"]),
                        old["key_id"],
                        old["key_epoch"],
                    )
                )
        return event

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def latest(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._latest_by_node[node_id]
                for node_id in sorted(self._latest_by_node)
            ]


def _utc_ms() -> int:
    return int(time.time() * 1000)


def _field_u8(route: dict[str, Any], name: str) -> int:
    value = _field_int(route, name)
    if not 0 <= value <= 0xFF:
        raise AppFrameError(f"route.{name} must fit in u8")
    return value


def _field_u32(route: dict[str, Any], name: str) -> int:
    value = _field_int(route, name)
    if not 0 <= value <= 0xFFFF_FFFF:
        raise AppFrameError(f"route.{name} must fit in u32")
    return value


def _field_int(route: dict[str, Any], name: str) -> int:
    value = route.get(name)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise AppFrameError(f"route.{name} must be an integer")


def _field_address_type(route: dict[str, Any], name: str) -> int:
    value = route.get(name)
    if value is None:
        raise AppFrameError(f"route.{name} is required")
    return address_type_value(value)


def _field_traffic_class(route: dict[str, Any], name: str) -> int:
    value = route.get(name)
    if value is None:
        raise AppFrameError(f"route.{name} is required")
    return traffic_class_value(value)


def _field_message_type(route: dict[str, Any], name: str) -> int:
    value = route.get(name)
    if value is None:
        raise AppFrameError(f"route.{name} is required")
    return message_type_value(value)


def _parse_key_hex(value: str, *, section_name: str) -> bytes:
    try:
        key = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise AppFrameError(f"{section_name} key_hex must be valid hex") from exc
    if len(key) != CHACHA20_POLY1305_KEY_SIZE:
        raise AppFrameError(
            f"{section_name} key_hex must decode to "
            f"{CHACHA20_POLY1305_KEY_SIZE} bytes, got {len(key)}"
        )
    return key


def load_keyring(config_paths: list[str]) -> dict[tuple[int, int, int], C2Key]:
    keyring: dict[tuple[int, int, int], C2Key] = {}
    for config_path in config_paths:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {config_path}")

        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path)
        for section_name in parser.sections():
            if section_name == "c2_uplink_crypto" or section_name.startswith(
                "c2_uplink_key."
            ):
                section = parser[section_name]
                if "enabled" in section and not section.getboolean("enabled"):
                    continue
                domain = security_domain_value(
                    section.get("security_domain", "node_to_c2")
                )
                if domain != SEC_DOMAIN_NODE_TO_C2:
                    raise AppFrameError(
                        f"{section_name} must use security_domain=node_to_c2"
                    )
                key_id = section.getint("key_id")
                key_epoch = section.getint("key_epoch")
                key_hex = section.get("key_hex")
                if key_hex is None:
                    raise AppFrameError(f"{section_name} key_hex is required")
                key = C2Key(
                    security_domain=domain,
                    key_id=key_id,
                    key_epoch=key_epoch,
                    key=_parse_key_hex(key_hex, section_name=section_name),
                    label=section_name,
                )
                keyring[(domain, key_id, key_epoch)] = key

    if not keyring:
        raise AppFrameError("no node_to_c2 keys loaded")
    return keyring


def render_html(latest_events: list[dict[str, Any]]) -> bytes:
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="refresh" content="2">',
        "<title>Kova C2</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;margin:24px;background:#f7f7f5;color:#151515}",
        "h1{font-size:24px;margin:0 0 16px}",
        "table{border-collapse:collapse;width:100%;background:white}",
        "th,td{border:1px solid #ddd;padding:6px 8px;text-align:left;font-size:14px}",
        "th{background:#eee}",
        "code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}",
        ".payload{max-width:52rem;white-space:pre-wrap;word-break:break-word}",
        ".empty{color:#666}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>Kova C2 Payloads</h1>",
    ]
    if not latest_events:
        parts.append('<p class="empty">No payloads received yet.</p>')
    else:
        parts.append("<table>")
        parts.append(
            "<tr><th>Node</th><th>UTC ms</th><th>Seq</th><th>Type</th>"
            "<th>Gateway</th><th>Key</th><th>Payload</th></tr>"
        )
        for event in latest_events:
            parts.append(
                "<tr>"
                f"<td>{event['origin_id']}</td>"
                f"<td>{event['received_at_ms']}</td>"
                f"<td>{event['origin_seq']}</td>"
                f"<td>{html.escape(str(event['inner_type']))}</td>"
                f"<td>{html.escape(str(event.get('gateway_id')))}</td>"
                f"<td>{event['key_id']}:{event['key_epoch']}</td>"
                f"<td class=\"payload\"><code>{html.escape(str(event['payload_text']))}</code></td>"
                "</tr>"
            )
        parts.append("</table>")
    parts.extend(["</body>", "</html>"])
    return "\n".join(parts).encode("utf-8")


def make_handler(store: C2EventStore):
    class C2Handler(BaseHTTPRequestHandler):
        server_version = "KovaC2/0.1"

        def do_GET(self) -> None:
            if self.path in {"/", "/index.html"}:
                body = render_html(store.latest())
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                return
            if self.path == "/events":
                body = json.dumps({"events": store.events()}, indent=2).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json")
                return
            if self.path == "/latest":
                body = json.dumps({"nodes": store.latest()}, indent=2).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json")
                return
            if self.path == "/health":
                self._send(HTTPStatus.OK, b'{"ok":true}\n', "application/json")
                return
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

        def do_POST(self) -> None:
            if self.path != "/ingest":
                self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise AppFrameError("invalid Content-Length")
                upload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(upload, dict):
                    raise AppFrameError("request body must be a JSON object")
                event = store.ingest(upload, client_ip=self.client_address[0])
            except (AppFrameError, ValueError, json.JSONDecodeError) as exc:
                body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self._send(HTTPStatus.BAD_REQUEST, body, "application/json")
                return

            body = json.dumps(
                {
                    "ok": True,
                    "duplicate": event["duplicate"],
                    "origin_id": event["origin_id"],
                    "origin_seq": event["origin_seq"],
                }
            ).encode("utf-8")
            self._send(HTTPStatus.OK, body, "application/json")

        def log_message(self, fmt: str, *args: object) -> None:
            print(
                f"{self.address_string()} - - "
                f"[{self.log_date_time_string()}] {fmt % args}"
            )

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return C2Handler


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tiny C2 HTTP receiver for route_v2 node_to_c2 payloads",
    )
    parser.add_argument(
        "--config",
        action="append",
        required=True,
        help="config file containing [c2_uplink_key.*] or [c2_uplink_crypto]",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--max-events", type=int, default=MAX_EVENTS)
    args = parser.parse_args()

    if args.max_events < 1:
        parser.error("--max-events must be > 0")

    keyring = load_keyring(args.config)
    store = C2EventStore(keyring, max_events=args.max_events)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(
        f"C2 HTTP listening on http://{args.host}:{args.port} "
        f"with {len(keyring)} node_to_c2 keys"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
