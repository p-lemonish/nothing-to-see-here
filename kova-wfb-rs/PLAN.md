# Simple Mesh App Protocol Plan

## Goal

Build a tiny application protocol on top of `wfb_rs` so multiple nodes can share one wifi channel and one `stream_id`, identify senders, and exchange small messages without changing the Rust radio transport.

Keep v0 simple:

- one shared wifi channel for the group
- one shared `stream_id` for the group
- sender identity lives inside the payload
- no routing, ACKs, retries, encryption, or compression until hello/data exchange is reliable

## Layering

`wfb_rs` remains the transport layer:

- injects and captures raw 802.11 frames
- filters by `stream_id`
- exposes payload bytes to Python/Rust callers

The new app protocol lives inside those payload bytes:

- identifies the sender
- identifies message type
- carries an app sequence number
- carries the application payload

## V0 Frame Format

All multi-byte fields are big-endian.

```text
offset  size  field
0       1     version
1       1     message_type
2       1     sender_id
3       1     flags
4       4     app_seq
8       2     payload_len
10      N     payload
```

Header size: 10 bytes.

Field meanings:

- `version`: protocol version. Start with `1`.
- `message_type`: what kind of message this is.
- `sender_id`: node identity, `1..255`. Reserve `0` for unknown/invalid.
- `flags`: bitfield. Start with `0`.
- `app_seq`: per-sender application sequence number.
- `payload_len`: number of bytes after the header.
- `payload`: raw application bytes.

Initial message types:

```text
0x01 = hello
0x02 = text
0x03 = data
0x04 = status
```

Reserved for later:

```text
0x10 = ack
0x20 = route_announce
0x21 = route_data
```

## Validation Rules

Drop frames when:

- total frame length is less than 10 bytes
- `version` is not `1`
- `sender_id` is `0`
- `payload_len` does not match the remaining byte count
- `message_type` is unknown and the caller has not opted into raw/unknown handling

Do not crash the receiver on malformed frames. Count and optionally print bad frames during debugging.

## Implementation Steps

1. Add a small Python protocol module at `python/src/wfb_rs_py/app_proto.py`.
2. Implement `encode_frame(...) -> bytes`.
3. Implement `decode_frame(...) -> AppFrame`.
4. Add unit tests for valid frames, malformed frames, max IDs, and payload length mismatch.
5. Update `python/examples/simple_txrx.py` with optional app-protocol flags:
   - `--sender-id`
   - `--app-proto`
   - `--message-type`
6. Keep legacy raw text mode working when `--app-proto` is not set.
7. Add a dedicated example if `simple_txrx.py` starts getting too crowded.

Current v0 implementation status:

- `app_proto.py` contains the codec and validation.
- `simple_txrx.py` supports `--app-proto`, `--sender-id`, and `--message-type`.
- Raw text mode remains the default.

## First Test

Use two dongles on the same PC.

Receiver:

```bash
export RXNIC=wlx5cffffaba18f
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$RXNIC" --stream-id 1 --app-proto --sender-id 2
```

Sender:

```bash
export TXNIC=wlx5cffffabb301
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$TXNIC" --stream-id 1 --app-proto --sender-id 1 \
  --message "hello world" --message-type hello --count 0 --tx-interval-ms 1000
```

Expected receiver output should include:

```text
sender=1 type=hello seq=... payload="hello world"
```

## Iteration Rules

- Keep the header fixed for v0 unless testing shows a real problem.
- Add new message types before changing existing fields.
- Keep sender identity in the app payload, not in the synthetic 802.11 MAC fields.
- Prefer explicit drops over guessing when a frame is malformed.
- Make each node configurable from CLI/env before hardcoding behavior.
- Only add ACK/retry/routing after one-hop broadcast is proven stable.

## Later Work

After v0 works:

- add compact status payloads for node health and RSSI
- add simple duplicate suppression using `(sender_id, app_seq)`
- add optional ACKs for important messages
- add routing fields only when there are at least three active nodes
- add binary payload schemas per message type
- consider encryption once the basic mesh behavior is observable
