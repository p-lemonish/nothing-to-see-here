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

Peer A:

```bash
export NIC=wlx5cffffaba18f
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$NIC" --stream-id 1 --app-proto --sender-id 67 \
  --message "hello 42" --message-type hello --count 0 --tx-interval-ms 1000
```

Peer B:

```bash
export NIC=wlxfc221c2004ce
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$NIC" --stream-id 1 --app-proto --sender-id 42 \
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

## Next Phase: Security, Compression, And Status

The mesh transport now works at a simple UDP-like level. The next phase should
improve operational usefulness without changing the radio layer or adding ACKs.

Implementation order:

1. Mesh log ergonomics.
2. AEAD secure payload wrapper.
3. Config-based shared keys.
4. Authenticated mesh forwarding.
5. Optional compression.
6. Structured status payloads.
7. Channel-health metrics and channel agility.

### 1. Mesh Log Ergonomics

Add config options:

```ini
log_level = info
quiet_duplicates = true
```

Suggested behavior:

- `debug`: print invalid frames, duplicates, own-route drops, forwards, delivers.
- `info`: print local deliveries, local sends, forwards, auth failures.
- `quiet_duplicates = true`: suppress duplicate route drop lines.

This should be implemented before crypto so three-node tests remain readable.

### 2. AEAD Secure Payload Wrapper

Add encryption/authentication as an inner payload wrapper. Do not change the
10-byte base app header and do not change the `route_data` wrapper.

Use:

```text
ChaCha20-Poly1305
```

Secure payload wrapper:

```text
offset  size  field
0       4     key_id
4       12    nonce
16      N     ciphertext_and_tag
```

`ciphertext_and_tag` contains the encrypted inner payload plus the AEAD tag.

Associated data must include all unencrypted metadata that must not be tampered
with:

For direct app frames:

```text
version, message_type, sender_id, flags, app_seq, payload_len
```

For routed frames, include both the outer app header and route wrapper metadata:

```text
outer app header
origin_sender_id
destination_id
ttl
origin_seq
inner_type
inner_payload_len
```

Rules:

- Drop frames that fail AEAD authentication.
- Do not deliver unauthenticated secure payloads.
- Do not forward unauthenticated `route_data`.
- Keep routing metadata visible for forwarding, but authenticated.
- Keep actual application payload confidential.

### 3. Config-Based Shared Keys

Start with static shared keys in config files for hackathon speed.

Config example:

```ini
secure = true
key_id = 1
key_hex = 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
```

Rules:

- `key_hex` must decode to 32 bytes for ChaCha20-Poly1305.
- `key_id` is sent in the secure wrapper.
- Receivers choose the right key by `key_id`.
- Do not print keys in logs.
- Keep keys out of packet captures and screenshots.

Nonce strategy for v1:

```text
nonce = sender_id(1 byte) || app_seq(4 bytes) || origin_seq_or_zero(4 bytes) || counter(3 bytes)
```

This is simple but must still guarantee no nonce reuse for the same key. If this
gets complicated, use a random 96-bit nonce and accept the tiny collision risk
for the hackathon demo.

### 4. Authenticated Mesh Forwarding

For `route_data`, forwarding nodes must be able to authenticate route metadata
before forwarding.

Recommended first version:

- encrypt only `inner_payload`
- authenticate route metadata as associated data
- keep `origin_sender_id`, `destination_id`, `ttl`, `origin_seq`, and
  `inner_type` visible
- decrement TTL during forwarding
- after decrementing TTL, re-wrap/re-authenticate with the forwarding node's key

This means each hop authenticates what it forwards. It is simpler than trying to
preserve an end-to-end tag across TTL mutation.

Later, add end-to-end payload auth if needed:

- inner origin authentication tag over original payload
- hop authentication tag over mutable route metadata

### 5. Optional Compression

Add compression only after AEAD is working.

Reason:

- Tiny status/hello messages may get larger when compressed.
- Compression is an optimization, not a safety feature.
- Compression can leak information in some designs if attacker-controlled text
  is compressed with secrets.

Config:

```ini
compression = none
compress_min_bytes = 128
```

Initial options:

```text
none
zstd
```

Rules:

- Compress before encryption.
- Do not compress payloads below `compress_min_bytes`.
- Include compression mode in authenticated metadata or in a small encrypted
  payload header.
- Start with `none` as default.

### 6. Structured Status Payload

Replace free-text status messages with a small binary status payload once secure
transport is stable.

V1 status payload:

```text
offset  size  field
0       1     status_version
1       4     uptime_s
5       1     battery_pct, 255 means unknown
6       1     peer_count
7       1     flags
```

Possible flags:

```text
0x01 = degraded_link
0x02 = low_battery
0x04 = crypto_enabled
0x08 = forwarding_enabled
```

Later status fields:

- last-heard peer summaries
- RSSI summaries
- retry/loss estimates
- GPS/position if available

### 7. Channel Agility

Channel agility is relevant for availability, but it must be added carefully.
Unsynchronized hopping can make the mesh worse by causing healthy nodes to miss
each other. Automatic channel switching should happen only after authenticated
control frames exist.

Start with a conservative model:

- fixed primary channel
- backup channel list
- shared hopping schedule
- channel-health/jam score
- channel blacklisting
- rendezvous windows
- emergency fallback channel
- wide scan mode after prolonged silence

Config shape:

```ini
channel_agility = false
primary_channel = 36
primary_width = HT20
backup_channels = 40,44,48
rendezvous_channel = 36
emergency_channel = 36
hop_slot_ms = 5000
rendezvous_slot_ms = 1000
lost_scan_dwell_ms = 1500
jam_score_threshold = 10
```

Normal mode:

- Use the best-known channel, normally `primary_channel`.
- Keep sending normal mesh status/route traffic.
- Track channel health from receive gaps, malformed frame rate, RSSI when
  available, auth failures, and local packet counters.

Degraded mode:

- Enter when jam/loss score exceeds threshold.
- Rotate through the pre-agreed `backup_channels` schedule.
- Include periodic rendezvous windows on `rendezvous_channel`.
- Send compact signed/encrypted "I am here" status packets during scheduled
  slots.

Lost mode:

- Enter after prolonged silence from all expected peers.
- Scan all allowed configured channels.
- Listen longer than one full expected beacon/status interval per channel.
- Transmit short authenticated presence packets only in scheduled slots.
- Return to normal mode when authenticated peers are heard again.

Example schedule:

```text
t0-t1: channel 36
t1-t2: channel 40
t2-t3: channel 44
t3-t4: channel 48
t4-t5: rendezvous channel 36
repeat
```

Rules:

- Do not automatically switch channels before AEAD authentication exists.
- Do not accept unsigned channel-change instructions.
- Keep an emergency rendezvous channel that every node knows.
- Keep channel lists explicit in config and obey the local regulatory domain.
- Prefer non-DFS channels for initial tests.
- Log channel state transitions clearly.
- If clocks are not synchronized, use longer rendezvous/listen windows.

Implementation stages:

1. Add passive channel-health metrics only; no switching.
2. Add manual `--set-channel` helper or documented `iw` commands.
3. Add config parsing for primary, backup, rendezvous, and emergency channels.
4. Add authenticated channel-intent messages.
5. Add degraded-mode scheduled hopping.
6. Add lost-mode scan and rendezvous recovery.

## Next Implementation Steps

1. Add `log_level` and `quiet_duplicates` to `mesh_txrx.py` and config files.
2. Add secure payload encode/decode helpers and tests.
3. Add config parsing for `secure`, `key_id`, and `key_hex`.
4. Add authenticated encryption for routed inner payloads.
5. Ensure unauthenticated secure frames are dropped and not forwarded.
6. Add optional compression after encryption works.
7. Add structured status payloads after secure transport is stable.
8. Add passive channel-health metrics.
9. Add authenticated channel-agility controls and rendezvous behavior.
