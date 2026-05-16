#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from wfb_rs_py import Rx, Tx
from wfb_rs_py.app_proto import (
    AppFrameError,
    MESSAGE_TYPE_NAMES,
    MSG_ROUTE_DATA,
    MSG_TEXT,
    decode_frame,
    encode_frame,
    message_type_value,
)

DEFAULT_STREAM_ID = 0xDEAD_BEEF
MAX_U32 = 0xFFFF_FFFF


def run_simple_txrx(
    iface: str,
    stream_id: int,
    timeout_ms: int,
    interval_ms: int,
    print_rssi: bool,
    include_self: bool,
    message: str | None,
    count: int,
    listen_after_ms: int,
    app_protocol: bool,
    sender_id: int | None,
    message_type: int,
) -> int:
    stop_event = threading.Event()
    rx_error: list[Exception] = []

    def make_payload(text: str, seq: int) -> bytes:
        payload = text.encode("utf-8")
        if not app_protocol:
            return payload
        if sender_id is None:
            raise ValueError("sender_id is required in app protocol mode")
        return encode_frame(
            sender_id=sender_id,
            message_type=message_type,
            app_seq=seq,
            payload=payload,
        )

    with Tx(iface=iface, stream_id=stream_id) as tx, Rx(
        iface=iface, stream_id=stream_id, ignore_self_injected=not include_self
    ) as rx:
        if message is None:
            print("Simple TX/RX mode: type lines to send, Ctrl-D or Ctrl-C to exit")
        else:
            repeat = "forever" if count == 0 else str(count)
            print(
                f'Simple TX/RX mode: broadcasting "{message}" {repeat} time(s), '
                "Ctrl-C to exit"
            )

        def rx_loop() -> None:
            while not stop_event.is_set():
                result = rx.recv_optional(timeout_ms=timeout_ms)
                if result is None:
                    continue
                payload, meta = result
                if app_protocol:
                    try:
                        frame = decode_frame(payload)
                    except AppFrameError as exc:
                        print(
                            f'RX invalid_app_frame len={len(payload)} '
                            f'truncated={int(meta.truncated)} error="{exc}"'
                        )
                        continue

                    text = frame.payload.decode("utf-8", errors="replace")
                    prefix = (
                        f"RX sender={frame.sender_id} type={frame.message_type_name} "
                        f"app_seq={frame.app_seq} len={len(frame.payload)} "
                        f"rf_seq={meta.seq} truncated={int(meta.truncated)}"
                    )
                    if print_rssi:
                        prefix += (
                            f" bw={meta.bandwidth} mcs={meta.mcs_index} "
                            f"rssi0={meta.rssi[0]}"
                        )
                    print(f'{prefix} payload="{text}"')
                    continue

                text = payload.decode("utf-8", errors="replace")
                if print_rssi:
                    print(
                        f'RX seq={meta.seq} len={len(payload)} bw={meta.bandwidth} '
                        f'mcs={meta.mcs_index} rssi0={meta.rssi[0]} truncated={int(meta.truncated)} '
                        f'payload="{text}"'
                    )
                else:
                    print(
                        f'RX seq={meta.seq} len={len(payload)} truncated={int(meta.truncated)} '
                        f'payload="{text}"'
                    )

        rx_thread = threading.Thread(target=rx_loop, name="wfb-rx", daemon=True)
        rx_thread.start()

        seq = 1
        try:
            if message is None:
                for line in sys.stdin:
                    text = line.rstrip("\n")
                    if not text:
                        continue
                    payload = make_payload(text, seq)
                    tx.send(payload, seq=seq)
                    seq += 1

                    if interval_ms > 0:
                        time.sleep(interval_ms / 1000.0)
            else:
                sent = 0
                while count == 0 or sent < count:
                    payload = make_payload(message, seq)
                    tx.send(payload, seq=seq)
                    seq += 1
                    sent += 1
                    if interval_ms <= 0 or (count != 0 and sent >= count):
                        continue
                    time.sleep(interval_ms / 1000.0)

                if listen_after_ms > 0:
                    time.sleep(listen_after_ms / 1000.0)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            rx_error.append(exc)
        finally:
            stop_event.set()
            rx_thread.join(timeout=(timeout_ms / 1000.0) + 0.2)

    if rx_error:
        raise rx_error[0]
    return 0


def _default_iface() -> str | None:
    return os.getenv("NIC") or os.getenv("WFB_IFACE") or os.getenv("IFACE")


def _default_sender_id() -> int | None:
    value = os.getenv("WFB_SENDER_ID") or os.getenv("SENDER_ID")
    if value is None:
        return None
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple wfb_rs_py TX/RX example")
    parser.add_argument(
        "--iface",
        default=_default_iface(),
        help="monitor-mode interface (default: $NIC, $WFB_IFACE, or $IFACE)",
    )
    parser.add_argument(
        "--stream-id",
        type=lambda value: int(value, 0),
        default=DEFAULT_STREAM_ID,
        help="stream id (u32, decimal or hex, default: 0xdeadbeef)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=100,
        help="RX poll timeout in milliseconds",
    )
    parser.add_argument(
        "--tx-interval-ms",
        type=int,
        default=0,
        help="optional delay between transmitted lines",
    )
    parser.add_argument(
        "--print-rssi",
        action="store_true",
        help="print antenna slot 0 RSSI in RX lines",
    )
    parser.add_argument(
        "--include-self",
        action="store_true",
        help="do not filter frames injected by this host; useful for single-NIC smoke tests",
    )
    parser.add_argument(
        "--message",
        help="send this message instead of reading lines from stdin",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="number of generated messages to send with --message; 0 means forever",
    )
    parser.add_argument(
        "--listen-after-ms",
        type=int,
        default=1000,
        help="how long to keep RX open after finite --message sends",
    )
    parser.add_argument(
        "--app-proto",
        action="store_true",
        help="wrap outgoing payloads and decode incoming payloads with the v0 app protocol",
    )
    parser.add_argument(
        "--sender-id",
        type=lambda value: int(value, 0),
        default=_default_sender_id(),
        help="local sender id for app protocol mode (default: $WFB_SENDER_ID or $SENDER_ID)",
    )
    parser.add_argument(
        "--message-type",
        default="text",
        help="app protocol message type: hello, text, data, status, sync, or numeric value",
    )
    args = parser.parse_args()

    if not args.iface:
        parser.error("--iface is required unless NIC, WFB_IFACE, or IFACE is set")
    if not 1 <= args.stream_id <= MAX_U32:
        parser.error("--stream-id must be in range 1..0xffffffff")
    if args.count < 0:
        parser.error("--count must be >= 0")
    if args.app_proto:
        if args.sender_id is None:
            parser.error("--sender-id is required with --app-proto")
        if not 1 <= args.sender_id <= 255:
            parser.error("--sender-id must be in range 1..255")
        try:
            msg_type = message_type_value(args.message_type)
        except AppFrameError as exc:
            parser.error(str(exc))
        if msg_type not in MESSAGE_TYPE_NAMES or msg_type == MSG_ROUTE_DATA:
            parser.error(
                "--message-type must be one of hello, text, data, status, or sync for app protocol mode"
            )
    else:
        msg_type = MSG_TEXT

    return run_simple_txrx(
        args.iface,
        args.stream_id,
        args.timeout_ms,
        args.tx_interval_ms,
        args.print_rssi,
        args.include_self,
        args.message,
        args.count,
        args.listen_after_ms,
        args.app_proto,
        args.sender_id,
        msg_type,
    )


if __name__ == "__main__":
    raise SystemExit(main())
