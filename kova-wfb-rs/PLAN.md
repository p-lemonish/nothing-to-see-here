# UDP-Like Node-To-Node App Protocol Plan

## Current Goal

Keep the protocol simple and easy to iterate on. For now, treat packets as
UDP-like datagrams:

- one shared wifi channel for the group
- one shared `stream_id` for the group
- sender identity lives inside the app payload
- receivers may drop packets without requiring retransmission
- application data must be safe if a frame is lost
- no ACK/retry/session state in the implementation yet

This keeps the first usable system focused on observable node-to-node comms
instead of building a TCP-like transport too early.

## Layering

`wfb_rs` remains the radio transport:

- injects and captures raw 802.11 frames
- filters by `stream_id`
- exposes payload bytes to Python/Rust callers

The app protocol lives inside those payload bytes:

- identifies the sender
- identifies message type
- carries an app sequence number
- carries application payload bytes

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
- `app_seq`: per-sender sequence number for observability and duplicate detection.
- `payload_len`: number of bytes after the header.
- `payload`: raw application bytes.

Active message types:

```text
0x01 = hello
0x02 = text
0x03 = data
0x04 = status
0x21 = route_data
```

Reserved concept-only message types for later:

```text
0x10 = ack
0x11 = syn
0x12 = syn_ack
0x13 = fin
0x14 = fin_ack
0x20 = route_announce
```

Do not implement or depend on the reserved message types until the UDP-like path
is useful and stable.

## Validation Rules

Drop frames when:

- total frame length is less than 10 bytes
- `version` is not `1`
- `sender_id` is `0`
- `payload_len` does not match the remaining byte count
- `message_type` is unknown

Do not crash the receiver on malformed frames. Count and optionally print bad
frames during debugging.

## Implemented Status

- `python/src/wfb_rs_py/app_proto.py` contains the v0 codec and validation.
- `python/examples/simple_txrx.py` supports `--app-proto`, `--sender-id`, and
  `--message-type`.
- Raw text mode remains the default when `--app-proto` is not set.
- `app_proto.py` contains `route_data` encode/decode helpers for TTL flooding.
- `python/examples/mesh_txrx.py` can receive, deduplicate, deliver, and forward
  `route_data`.
- `python/examples/mesh_txrx.py` supports `--config` for simple node config files.
- `configs/node1.ini`, `configs/node2.ini`, and `configs/node3.ini` are starter
  configs. Set each `iface` from `iw dev`.
- No ACK/retry/session example is implemented.

## Node-To-Node Test

Use two dongles on the same PC or two hosts. Both must be in monitor mode on the
same wifi channel and use the same `stream_id`.

Receiver:

```bash
export RXNIC=wlx5cffffaba18f
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$RXNIC" --stream-id 1 --app-proto --sender-id 67
```

Sender:

```bash
export TXNIC=wlxfc221c2004ce
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$TXNIC" --stream-id 1 --app-proto --sender-id 42 \
  --message "hello 67" --message-type hello --count 0 --tx-interval-ms 1000
```

Expected receiver output:

```text
RX sender=42 type=hello app_seq=... len=8 rf_seq=... payload="hello 67"
```

## UDP-Like Operating Rules

- Use `hello` for node discovery and quick manual tests.
- Use `status` for periodic node health/state snapshots.
- Use `data` for application datagrams.
- Keep payloads small and self-contained.
- Make messages idempotent where possible.
- For state-like data, use "latest value wins" semantics.
- Repeat important state periodically instead of requiring ACKs.
- Use `app_seq` for logging, duplicate detection, and packet-loss estimates.
- Do not block the sender waiting for any receiver.

## Simple Reliability Without ACKs

Before adding ACKs, prefer approaches that preserve UDP-like behavior:

- send important datagrams more than once
- add small random jitter between repeats
- include full state snapshots periodically
- keep per-sender `last_seen_seq` for packet-loss estimates
- drop duplicate `(sender_id, app_seq)` frames when needed
- degrade gracefully when peers stop being heard

Example repeat strategy:

```text
send seq=100 repeat=1
wait 50-150 ms
send seq=100 repeat=2
wait 50-150 ms
send seq=100 repeat=3
```

This costs airtime, but avoids ACK storms and per-peer retry state.

## Integrity, Confidentiality, And Availability

The target is still the CIA triad, but it should be added in layers that do not
break the simple datagram path.

Confidentiality:

- Later: encrypt payloads with AEAD, likely ChaCha20-Poly1305.
- For now: assume payloads are observable on the RF channel.

Integrity:

- CRC32C can detect accidental corruption and framing bugs.
- CRC is not authentication because hostile senders can recompute it.
- Later: use AEAD authentication tags or HMAC for real tamper detection.

Availability:

- Keep traffic compact.
- Prefer periodic datagrams over connection state.
- Track last-heard timestamps per sender.
- Track loss estimates from app sequence gaps.
- Use bounded repeat sends for important datagrams.
- Later: add channel-health metrics and operator-assisted channel fallback.

## Mesh Direction

Do not start with a full reliable mesh transport. First add TTL-limited flooding
for UDP-like datagrams.

Direct messages stay as plain app frames. Mesh/flooded messages use
`message_type = route_data` and wrap the inner payload with routing metadata.

### `route_data` Payload Format

All multi-byte fields are big-endian.

```text
offset  size  field
0       1     origin_sender_id
1       1     destination_id, 0 means broadcast
2       1     ttl
3       4     origin_seq
7       1     inner_type
8       2     inner_payload_len
10      N     inner_payload
```

Route wrapper overhead: 10 bytes inside the app payload.

Field meanings:

- `origin_sender_id`: node that originally created the routed message.
- `destination_id`: target node, or `0` for broadcast.
- `ttl`: remaining forwards allowed.
- `origin_seq`: per-origin sequence number used for deduplication.
- `inner_type`: application message type carried inside the route wrapper.
- `inner_payload_len`: length of `inner_payload`.
- `inner_payload`: carried application bytes.

Rules:

- Deduplicate using `(origin_sender_id, origin_seq)`, not the outer sender.
- Deliver a routed frame locally when `destination_id == 0` or
  `destination_id == my_sender_id`.
- Drop duplicates before delivering or forwarding.
- If `ttl > 0`, forward the same routed message with `ttl - 1`.
- When forwarding, set the outer app header `sender_id` to the forwarding node.
- Keep the inner `origin_sender_id` and `origin_seq` unchanged.
- Prefer periodic state snapshots for "everyone knows current state".
- Avoid every node ACKing every broadcast packet.

Example:

```text
Node 42 creates:
  outer sender_id=42
  message_type=route_data
  origin_sender_id=42
  destination_id=0
  ttl=3
  origin_seq=500
  inner_type=status
  inner_payload="battery=91"

Node 67 forwards:
  outer sender_id=67
  message_type=route_data
  origin_sender_id=42
  destination_id=0
  ttl=2
  origin_seq=500
  inner_type=status
  inner_payload="battery=91"
```

Both frames represent the same routed message for deduplication:

```text
(origin_sender_id=42, origin_seq=500)
```

### TTL Defaults

Start conservative:

- one-hop direct test: no `route_data`
- small room test with three nodes: `ttl=2`
- larger test: `ttl=3`
- avoid TTL values above `5` until airtime impact is measured

TTL is about availability and spam control. It increases the chance that nearby
nodes receive a message without letting one packet bounce around forever.

## Later Concepts Only

Keep these out of the implementation until the UDP-like node-to-node protocol is
stable:

- stop-and-wait ACK
- SYN/SYN_ACK/FIN/FIN_ACK session lifecycle
- sliding windows
- route-aware ACKs
- automatic channel switching

These may be useful later for directed critical commands or larger transfers,
but they should not be the default mesh mode.

## Next Implementation Steps

1. Keep testing `simple_txrx.py --app-proto` across two or three dongles.
2. Keep testing `mesh_txrx.py` across three dongles with `ttl=2`.
3. Tune `configs/node*.ini` for each physical node's `iface` and `sender_id`.
4. Add `status` payload examples for node health and last-heard reporting.
5. Add optional repeated sends for important datagrams.
6. Add CRC32C wrapper only after the UDP-like path is stable.
7. Add AEAD encryption/authentication after message flow and key handling are clear.
