# UDP-Like Mesh Protocol Plan

## Current Status

The basic UDP-like mesh is now working at a sufficient level for the hackathon
prototype:

- one shared `stream_id` for the group
- TTL-limited route flooding
- sender identity inside the app payload
- per-origin sequence numbers for deduplication
- periodic status messages
- three-node forwarding
- synchronized channel hopping across `36,40,48`
- Unix UTC based hop schedule with NTP-synced hosts
- sync heartbeats showing `slot_delta=0` and `channel_match=1`
- UDP-like packet loss behavior; no retransmission dependency

Observed live test results:

- two-node and three-node tests deliver status messages across the mesh
- `ttl=2` works and is acceptable for the current room-scale mesh
- duplicates and own-route returns are expected and are dropped
- node 2 showed about `75-80 ms` skew
- node 3 showed about `100 ms` skew
- both are acceptable with `hop_slot_ms=5000` and `channel_tx_guard_ms=250`

## Operating Model

Keep the protocol simple and easy to iterate on. Treat packets as UDP-like
datagrams:

- one shared logical `stream_id` for the group
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
0x05 = sync
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
- `python/examples/mesh_txrx.py` supports experimental synchronized channel
  hopping via `channel_agility`, `hop_channels`, and `hop_slot_ms`.
- `python/examples/mesh_txrx.py` uses Unix UTC time for the hop schedule,
  prints clock/schedule state, applies a TX guard after channel changes, and can
  send compact `sync` heartbeats for clock/slot/channel observability.
- Live tests have validated three nodes using TTL forwarding, deduplication,
  channel hopping, and sync heartbeats.
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

- Next: encrypt payloads with AEAD, using ChaCha20-Poly1305.
- Until secure mode is enabled, assume payloads are observable on the RF channel.

Integrity:

- CRC32C can detect accidental corruption and framing bugs.
- CRC is not authentication because hostile senders can recompute it.
- Next: use AEAD authentication tags for real tamper detection.

Availability:

- Keep traffic compact.
- Prefer periodic datagrams over connection state.
- Track last-heard timestamps per sender.
- Track loss estimates from app sequence gaps.
- Use bounded repeat sends for important datagrams.
- Current fixed-schedule channel hopping is working.
- Later: add channel-health metrics and operator-assisted/adaptive channel
  fallback.

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
- adaptive channel switching based on authenticated control frames

These may be useful later for directed critical commands or larger transfers,
but they should not be the default mesh mode.

## Next Phase: Crypto First

The mesh transport now works at a simple UDP-like level. The next phase is
confidentiality and authenticity. Compression and structured status can wait.

Implementation order:

1. Add config-based shared keys.
2. Add AEAD secure payload encode/decode helpers and tests.
3. Encrypt routed inner payloads.
4. Authenticate route metadata as associated data.
5. Drop unauthenticated secure frames before delivery or forwarding.
6. Add clear crypto logs that do not print secrets.
7. Keep plaintext mode available for debugging.
8. Add optional compression later.
9. Add structured status later.
10. Add passive channel-health metrics later.

### 1. Config-Based Shared Keys

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
- For the first version, all mesh nodes can share one symmetric group key.
- Later, support key rotation and per-peer keys.

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

Nonce strategy for v1:

```text
nonce = random 96-bit value from os.urandom(12)
```

Random nonces are simple and safe enough for the prototype as long as the same
key is not used for an enormous number of packets. Later, switch to a
deterministic nonce if we need stricter guarantees.

### 3. Authenticated Mesh Forwarding

For `route_data`, forwarding nodes must be able to authenticate route metadata
before forwarding.

Recommended first version:

- encrypt only `inner_payload`
- keep `origin_sender_id`, `destination_id`, `ttl`, `origin_seq`, and
  `inner_type` visible
- authenticate route metadata as AEAD associated data
- include `ttl` in associated data for hop authentication
- decrement TTL during forwarding
- after decrementing TTL, re-wrap/re-authenticate with the forwarding node's key

This means each hop authenticates what it forwards. It is simpler than trying to
preserve one authentication tag across mutable TTL.

Later, add end-to-end payload auth if needed:

- inner origin authentication tag over original payload
- hop authentication tag over mutable route metadata

### 4. Associated Data

Associated data must include all unencrypted metadata that must not be tampered
with.

For routed frames, include:

```text
origin_sender_id
destination_id
ttl
origin_seq
inner_type
inner_plaintext_len
```

The outer app header can remain outside the first implementation's associated
data because forwarding changes the outer sender and outer app sequence. The
route wrapper is the important mesh contract.

Rules:

- Drop frames that fail AEAD authentication.
- Do not deliver unauthenticated secure payloads.
- Do not forward unauthenticated secure `route_data`.
- Keep routing metadata visible for forwarding, but authenticated.
- Keep actual application payload confidential.
- Keep `sync` payloads plaintext for now unless we decide that channel/sync
  metadata must also be hidden.

### 5. Crypto Logging

Add enough logs to debug interoperability without leaking secrets:

```text
TX secure origin=1 seq=... key_id=1 len=...
RX secure via=2 origin=2 seq=... key_id=1 decrypted=1
RX auth_fail via=2 origin=2 seq=... key_id=1 dropped=1
```

Do not log:

- `key_hex`
- plaintext payload when secure mode is enabled, unless explicitly in debug mode
- nonces unless needed for deep debugging

### 6. Mesh Log Ergonomics

Add config options:

```ini
log_level = info
quiet_duplicates = true
```

Suggested behavior:

- `debug`: print invalid frames, duplicates, own-route drops, forwards, delivers.
- `info`: print local deliveries, local sends, forwards, auth failures.
- `quiet_duplicates = true`: suppress duplicate route drop lines.

This can happen before or after the first crypto patch. Crypto is now the main
priority.

### 7. Optional Compression

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

### 8. Structured Status Payload

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

### 9. Channel Agility Hardening

Channel agility is relevant for availability, but it must be added carefully.
The fixed Unix-time schedule is working. The remaining work is adaptive channel
selection and recovery when the mesh is degraded or lost. Automatic channel
switching decisions should happen only after authenticated control frames exist.

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

1. Add experimental fixed-schedule hopping across a configured channel list.
2. Add passive channel-health metrics.
3. Add manual `--set-channel` helper or documented `iw` commands.
4. Add config parsing for primary, backup, rendezvous, and emergency channels.
5. Add authenticated channel-intent messages.
6. Add degraded-mode scheduled hopping based on channel-health metrics.
7. Add lost-mode scan and rendezvous recovery.

Current experimental mode:

```ini
channel_agility = true
hop_channels = 36,40,48
channel_width = HT20
hop_slot_ms = 5000
hop_epoch_ms = 0
channel_settle_ms = 250
channel_tx_guard_ms = 250
channel_down_up = true
sync_heartbeat = true
sync_interval_ms = 5000
```

All nodes use Unix UTC wall-clock time to pick the same channel slot. During a
switch, `mesh_txrx.py` closes its radio handles, changes the interface channel,
reopens the handles, waits through a short TX guard window, and resumes
UDP-like send/receive. Packets lost during the switch are accepted as normal
UDP-like loss.

The `sync` heartbeat payload is 18 bytes:

```text
offset  size  field
0       8     utc_ms
8       4     slot
12      2     channel
14      4     next_hop_ms
```

Received sync logs compare the peer's UTC time, slot, and channel against the
local node. This does not discipline the local clock; NTP is still the clock
source.

## Next Implementation Steps

1. Add Python crypto dependency wiring for ChaCha20-Poly1305.
2. Add secure payload encode/decode helpers and tests.
3. Add config parsing for `secure`, `key_id`, and `key_hex`.
4. Add authenticated encryption for routed inner payloads in `mesh_txrx.py`.
5. Ensure secure frames that fail authentication are dropped before delivery or
   forwarding.
6. Keep plaintext mode available with `secure = false`.
7. Add crypto-safe logs.
8. Re-test three nodes with `ttl=2`, channel hopping, sync heartbeats, and
   encrypted status payloads.
9. Add optional log quieting if the crypto test logs become too noisy.
10. Revisit compression, structured status, and adaptive channel agility after
    crypto is stable.
