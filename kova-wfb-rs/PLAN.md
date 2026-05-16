# UDP-Like Mesh Protocol Plan

## Current Status

The basic UDP-like mesh is now working at a sufficient level for the hackathon
prototype:

- one shared `stream_id` for the group: `0xdeadbeef`
- TTL-limited route flooding
- sender identity inside the app payload
- per-origin sequence numbers for deduplication
- periodic status messages
- three-node forwarding
- synchronized channel hopping across `36,40,48`
- Unix UTC based hop schedule with NTP-synced hosts
- sync heartbeats showing `slot_delta=0` and `channel_match=1`
- UDP-like packet loss behavior; no retransmission dependency
- Phase A mesh-group crypto implemented with ChaCha20-Poly1305
- Phase B route v2 and first E2E C2 payload mode implemented
- Tiny C2 HTTP receiver implemented for decrypting and displaying
  `node_to_c2` uplinks per node
- mesh crypto is config-only through `[mesh_crypto]`
- Python and config defaults now use the team stream ID

Current team `stream_id` is `0xdeadbeef`, which produces synthetic addr2/addr3
`57:42:de:ad:be:ef` in Wireshark/tcpdump.

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
- `app_proto.py` contains secure wrapper v1 helpers for ChaCha20-Poly1305 with
  `secure_version`, `security_domain`, `key_id`, `key_epoch`, `nonce`, and
  `ciphertext_and_tag`.
- `app_proto.py` now includes structured `status` payload helpers and validation
  (`status_version`, `uptime_s`, `battery_pct`, `peer_count`, `flags`).
- `python/tests/test_app_proto.py` covers status payload edge cases and secure
  status round-trips/authentication failure behavior.
- `python/examples/mesh_txrx.py` supports Phase A mesh-group crypto for
  non-`sync` routed payloads when `[mesh_crypto] enabled = true`.
- Mesh crypto is controlled by config only; there is no runtime CLI override for
  enabling/disabling it or changing keys.
- `mesh_txrx.py` authenticates secure mesh payloads before delivery/forwarding,
  re-encrypts mesh-group traffic after TTL decrement, and keeps a replay window.
- `mesh_txrx.py` starts each process with a randomized origin sequence number to
  avoid replay-window collisions when a node restarts under the same key epoch.
- `configs/node1.ini`, `configs/node2.ini`, and `configs/node3.ini` now use
  `stream_id = 0xdeadbeef`, producing `57:42:de:ad:be:ef` in captures.
- `python/examples/simple_txrx.py` and `python/examples/mesh_txrx.py` default to
  `0xdeadbeef` when no stream id is supplied.
- `mesh_txrx.py` now includes a local link-health state machine with transitions
  `nominal -> degraded -> isolated -> moving -> rtb` and recovery back toward
  `nominal` when links return.
- `mesh_txrx.py` now auto-generates binary `status` payloads in `--status-auto`
  mode and logs decoded structured status fields on receive.
- `mesh_txrx.py` now supports `--message-file` and `--message-file-reload` for
  binary demo payloads (for example camera snapshots).
- Live tests have validated three nodes using TTL forwarding, deduplication,
  channel hopping, and sync heartbeats.
- Secure mesh mode still needs a live three-node RF test.
- Current secure mesh mode is not node-to-C2 end-to-end encryption. It encrypts
  mesh payload bytes with the shared `mesh_group` key. Routing metadata remains
  visible on the wire.
- Route v2 now supports typed `node`/`c2` destinations and opaque E2E
  `node_to_c2` / `c2_to_node` payloads for prototype testing.
- `mesh_txrx.py` can forward opaque C2 uplinks to an HTTP `/ingest` endpoint
  without decrypting them.
- No ACK/retry/session example is implemented.

## Current Wire Visibility

Current Phase A secure mesh mode protects payload content from listeners who do
not have the mesh group key, but it does not hide routing metadata.

Plaintext/readable on the wire:

```text
802.11 synthetic MAC: 57:42:de:ad:be:ef
outer app header
route wrapper:
  origin_sender_id
  destination_id
  ttl
  origin_seq
  inner_type
  encrypted_payload_len
secure wrapper:
  secure_version
  security_domain
  key_id
  key_epoch
  nonce
```

Encrypted:

```text
actual inner payload bytes
```

For example, a listener can see that node `2` sent `type=status` with
`seq=123`, but cannot read the status body without the `mesh_group` key.

This is intentionally different from the planned C2 model:

```text
Current Phase A:
  mesh-group encryption
  trusted mesh nodes can decrypt and re-encrypt mesh payloads
  useful for routine node-to-node status/data

Planned Phase B:
  node-to-C2 and C2-to-node end-to-end encryption
  relays forward opaque encrypted payloads
  only the endpoint can decrypt C2 payloads
```

Implemented Phase B slice:

```text
route_v2 metadata:
  origin_type
  origin_id
  destination_type
  destination_id
  ttl
  origin_seq
  traffic_class
  inner_type
  encrypted_payload_len

E2E payload:
  secure_version
  security_domain
  key_id
  key_epoch
  nonce
  ciphertext_and_tag
```

For E2E C2 traffic, relays can still see the route metadata above. They do not
decrypt the inner payload, and they forward it unchanged while decrementing
`ttl`. The E2E associated data authenticates immutable route fields but does not
include `ttl`, because `ttl` is intentionally mutable by relays.

## Node-To-Node Test

Use two dongles on the same PC or two hosts. Both must be in monitor mode on the
same wifi channel and use the same `stream_id`.

Peer A:

```bash
export NIC=wlx5cffffaba18f
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$NIC" --stream-id 0xdeadbeef --app-proto --sender-id 67 \
  --message "hello 42" --message-type hello --count 0 --tx-interval-ms 1000
```

Peer B:

```bash
export NIC=wlxfc221c2004ce
sudo python/.venv/bin/python python/examples/simple_txrx.py \
  --iface "$NIC" --stream-id 0xdeadbeef --app-proto --sender-id 42 \
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
confidentiality, authenticity, and compromise containment. Compression and
additional status fields can wait.

The key design shift:

```text
Use layered security.

Mesh layer:
  authenticates forwarding metadata and controls spoofed flooding.

End-to-end layer:
  encrypts actual node-to-C2, C2-to-node, or private node-to-node payloads.

Relays:
  forward C2 traffic without decrypting it.

Captured node:
  may compromise its own keys, but not other nodes' C2 traffic.
```

Do not use one universal key for all traffic except as a temporary debug mode.

Implementation order:

1. Keep current plaintext mode for debugging only.
2. Add secure wrapper v1 with security domain and key epoch.
3. Add config sections for separate mesh, C2, identity, and trusted-C2 keys.
4. Implement mesh-group AEAD for node-to-node broadcast traffic.
5. Implement node-to-C2 opaque payloads that relays forward but cannot decrypt.
6. Implement C2-to-node opaque payloads that only the target node can decrypt.
7. Add C2 command signatures and expiry checks.
8. Add replay windows per security domain.
9. Add C2-signed revocation and rekey messages.
10. Only after that, work on adaptive channel switching messages.

### 1. Trust Domains

Treat node-to-node mesh security and node-to-C2 security as separate trust
domains.

Security domains:

```text
0x01 = mesh_group
0x02 = node_to_node_pairwise
0x03 = node_to_c2
0x04 = c2_to_node
0x05 = c2_broadcast
0x06 = rekey_control
```

Key classes:

```text
K_node_mesh_group_epoch_N
K_node_pair_A_B_epoch_N
K_node_X_to_c2_epoch_N
K_c2_to_node_X_epoch_N
K_c2_broadcast_epoch_N
```

Purpose:

- `mesh_group`: routine mesh status, presence, neighbor summaries, and
  low-sensitivity coordination.
- `node_to_node_pairwise`: private node-to-node payloads when needed.
- `node_to_c2`: node reports back to C2; other drones may relay but cannot
  decrypt.
- `c2_to_node`: commands or tasking from C2 to one node; other drones may relay
  but cannot decrypt.
- `c2_broadcast`: C2 messages intended for all currently trusted nodes.
- `rekey_control`: C2-signed revocation and key epoch updates.

The first prototype may use a mesh group key for routine node-to-node traffic,
but that is only for local mesh traffic. It must not protect C2 commands or C2
uplink payloads.

### 2. Captured Node Assumptions

Assume any field node may be physically captured.

A captured node may reveal:

- its own private identity key
- its current node-to-C2 keys
- the current mesh group key if stored locally
- local logs and plaintext stored on disk
- recent RAM contents

The system should limit blast radius:

- captured node cannot decrypt other nodes' C2 traffic
- captured node cannot decrypt C2 commands intended for other nodes
- captured node cannot decrypt old traffic after forward-secret sessions exist
- captured node can be revoked from future group keys
- captured node cannot forge C2 commands without the C2 private key
- captured node cannot read future traffic after revocation and rekey

### 3. Identity And Authorization Model

Use asymmetric identity keys plus symmetric packet keys.

Each node has:

```text
node_id
node_public_key
node_private_key
```

C2 has:

```text
c2_id
c2_public_key
c2_private_key
```

Every node knows the C2 public key. C2 knows each authorized node public key.

Recommended primitives:

- AEAD packets: ChaCha20-Poly1305 or XChaCha20-Poly1305.
- C2 command signatures: Ed25519.
- Later session establishment: X25519 key agreement plus signatures.

For the prototype, static symmetric keys in config are acceptable. The packet
format must still carry `security_domain`, `key_id`, and `key_epoch` from the
start so key rotation does not require another packet redesign.

### 4. Secure Wrapper V1

Add encryption/authentication as an inner payload wrapper. Do not change the
10-byte base app header immediately. The secure wrapper lives inside routed
payloads and, later, direct app payloads.

Secure payload wrapper:

```text
offset  size  field
0       1     secure_version
1       1     security_domain
2       2     key_id
4       4     key_epoch
8       12    nonce
20      N     ciphertext_and_tag
```

Field meanings:

- `secure_version`: start with `1`.
- `security_domain`: one of the domain values above.
- `key_id`: key selector inside that domain.
- `key_epoch`: key generation used for replay control, rotation, and revocation.
- `nonce`: 96-bit AEAD nonce.
- `ciphertext_and_tag`: encrypted payload plus AEAD authentication tag.

Nonce strategy for v1:

```text
nonce = random 96-bit value from os.urandom(12)
```

Random nonces are simple and acceptable for the prototype. Later, use a
deterministic nonce or XChaCha20-Poly1305 if packet volume or nonce collision
risk becomes a concern.

### 5. Layered Packet Model

Separate hop authentication from end-to-end encryption.

Packet shape:

```text
outer route wrapper:
  origin_type
  origin_id
  destination_type
  destination_id
  ttl
  route_seq
  priority
  traffic_class
  inner_payload_len
  inner_payload

outer hop authentication:
  proves this is valid mesh traffic
  prevents random spoofed flooding
  allows relays to forward safely

inner end-to-end encryption:
  protects actual payload from relays and unrelated captured nodes
```

For normal mesh broadcast status, the inner payload can use the mesh group key.

For C2 traffic:

```text
Node 42 -> C2:
  outer: mesh-forwardable route packet
  inner: encrypted with K_node42_to_c2_epoch_N

C2 -> Node 42:
  outer: mesh-forwardable route packet
  inner: encrypted with K_c2_to_node42_epoch_N
```

Relays only need enough metadata to forward. They must not decrypt C2 payloads
unless they are the endpoint.

### 6. Forwarding Rule

Replace the earlier "decrypt and rewrap every hop" approach with layered
forwarding.

Forwarding node behavior:

- authenticate the outer route/hop wrapper
- drop packets that fail hop authentication
- drop packets from revoked origins when revocation data is available
- decrement TTL
- update only mutable routing metadata
- do not decrypt end-to-end payload unless it is the destination
- re-authenticate the hop wrapper if required by the chosen hop-auth design

This gives mesh availability without exposing C2 content to the mesh.

### 7. Route Addressing For C2

The current route format uses:

```text
destination_id, 0 means broadcast
```

That is sufficient for the current three-node mesh, but C2 needs a clearer
address model. Add destination and origin types before implementing opaque C2
traffic.

Better route payload:

```text
origin_type
origin_id
destination_type
destination_id
ttl
origin_seq
traffic_class
inner_type
inner_payload_len
inner_payload
```

Types:

```text
0x00 = mesh broadcast
0x01 = node
0x02 = c2
```

Traffic classes:

```text
0x01 = mesh_status
0x02 = mesh_data
0x03 = c2_uplink
0x04 = c2_downlink
0x05 = c2_broadcast
0x06 = rekey_control
```

This avoids overloading drone node IDs with C2 IDs. If we need a fast
intermediate step, reserve the high ID range:

```text
0 = mesh broadcast
1..239 = drone node IDs
240 = C2 broadcast / any C2
241..254 = specific C2 IDs
255 = reserved
```

The typed route format is cleaner and should be preferred.

### 8. Associated Data

Each encrypted message should authenticate the metadata that defines who sent
it, who should receive it, what key epoch applies, and how it should be handled.

Associated data for secure routed payloads should include:

```text
security_domain
sender_id
recipient_id or broadcast marker
sequence_number
key_epoch
message_type
expiry time
payload length
route origin_type/origin_id
route destination_type/destination_id
traffic_class
```

The hop-auth layer should also authenticate mutable route metadata as needed,
including TTL after each forwarding mutation.

Rules:

- Drop frames that fail AEAD authentication.
- Do not deliver unauthenticated secure payloads.
- Do not forward packets that fail required hop authentication.
- Keep routing metadata visible for forwarding, but authenticated.
- Keep actual C2 payloads confidential from relays.
- Keep `sync` payloads plaintext for now unless we decide that channel/sync
  metadata must also be hidden.

### 9. Command Authorization

Encryption alone says someone with the key produced a packet. C2 commands also
need explicit command authority.

For C2-to-node commands, use a C2 signature over a command envelope:

```text
command_id
target_node_id
issued_at
expires_at
command_type
command_args
mission_id
key_epoch
```

Flow:

```text
C2 signs command.
C2 encrypts signed command to node 42.
Mesh forwards opaque encrypted blob.
Node 42 decrypts.
Node 42 verifies C2 signature.
Node 42 checks expiry, target_node_id, command_id replay window.
Node 42 executes only if valid.
```

High-risk commands must require a valid C2 signature even if the encrypted
transport succeeds.

### 10. Replay Protection

Track replay windows separately for each security domain.

Replay windows:

```text
mesh_group from node X
node_to_node_pairwise from node X
node_to_c2 from node X
c2_to_node from C2
c2_broadcast from C2
rekey_control from C2
```

Do not use one global sequence space for everything. The existing per-origin
route sequence is useful for mesh deduplication, but crypto replay protection
needs domain-specific windows and key epochs.

Reject packets when:

- sequence is outside the accepted replay window
- `key_epoch` is too old or not yet valid
- `expires_at` is in the past
- command target does not match the local node
- C2 signature is missing or invalid for command traffic

### 11. Revocation And Rekey

Add three compromise-containment mechanisms.

Revocation list:

```text
revoked_node_ids
revocation_epoch
issued_at
signature_by_c2
```

Nodes reject mesh traffic from revoked nodes after receiving and verifying the
update.

Group rekey:

```text
C2 -> node 1: new mesh key encrypted to node 1
C2 -> node 2: new mesh key encrypted to node 2
C2 -> node 3: new mesh key encrypted to node 3
```

The revoked node does not receive the new key.

Short key epochs:

```text
mission_epoch
key_epoch
valid_from
valid_until
```

Packets outside the valid window are rejected after the configured grace period.

### 12. Config Shape

Prototype static-key config:

```ini
[node]
node_id = 42

[node_identity]
identity_private_key_file = secrets/node42_ed25519.key
identity_public_key_file = secrets/node42_ed25519.pub

[trusted_c2]
c2_id = 1
c2_public_key_file = secrets/c2_ed25519.pub

[mesh_crypto]
enabled = true
security_domain = mesh_group
key_id = 1001
key_epoch = 7
key_hex = ...

[c2_uplink_crypto]
enabled = true
security_domain = node_to_c2
key_id = 4201
key_epoch = 7
key_hex = ...

[c2_downlink_crypto]
enabled = true
security_domain = c2_to_node
key_id = 4202
key_epoch = 7
key_hex = ...

[c2_broadcast_crypto]
enabled = true
security_domain = c2_broadcast
key_id = 9001
key_epoch = 7
key_hex = ...
```

For the prototype, static keys in config are fine. For hostile deployment, keys
must be provisioned securely and rotated.

### 13. Crypto Logging

Add enough logs to debug interoperability without leaking secrets:

```text
TX secure origin=1 seq=... domain=mesh_group key_id=1001 key_epoch=7 len=...
RX secure via=2 origin=2 seq=... domain=mesh_group key_epoch=7 decrypted=1
RX auth_fail via=2 origin=2 seq=... domain=mesh_group key_epoch=7 dropped=1
RX route via=67 origin=node42 dest=c2 ttl=2 class=c2_uplink key_epoch=7 forwarded=1
RX c2_downlink target=node42 key_epoch=7 decrypt_ok=1 sig_ok=1 cmd_id=812 expires_ok=1
RX c2_downlink target=node43 not_for_me=1 forwarded=1 decrypt_skipped=1
```

Do not log:

- `key_hex`
- plaintext C2 payloads
- decrypted command bodies except in explicit local debug mode
- nonces except in deep debug mode
- signatures and session material in normal logs

Do not write decrypted C2 payloads to persistent disk unless required.

### 14. Practical Implementation Phases

Phase A - secure mesh baseline:

- implemented: add secure wrapper with `security_domain` and `key_epoch`
- implemented: encrypt/authenticate mesh status/data with the mesh group key
- implemented: add replay window for mesh group traffic
- implemented: authenticate route metadata in AEAD associated data
- implemented: config-only crypto controls
- implemented: team stream id `0xdeadbeef`
- current limitation: route metadata is plaintext; only the inner payload bytes
  are encrypted
- next: live-test secure mesh mode across three nodes

Phase B - opaque C2 traffic:

- implemented: add `route_v2` with `origin_type`, `destination_type`, and
  `traffic_class`
- implemented: add `node_to_c2` and `c2_to_node` config sections
- implemented: let relays forward C2 packets without decrypting
- implemented: only matching local endpoints decrypt when they have the key
- implemented: add optional local C2/gateway config with multiple uplink keys
- implemented: add a small C2 HTTP server that decrypts uploaded
  `node_to_c2` payloads and displays them per node
- implemented: add optional HTTP forwarding from `mesh_txrx.py` to C2 `/ingest`
- current limitation: route metadata has E2E integrity but no separate outer hop
  authentication layer yet
- next: deploy/run the C2 HTTP receiver on the UpCloud host and RF-test
  `c2_uplink` uploads through the mesh

Phase C - C2 command authority:

- add C2 signing key
- nodes verify C2 signatures for commands
- add `command_id`, `issued_at`, `expires_at`, and `target_node_id`
- reject expired, replayed, mistargeted, or unsigned commands

Phase D - compromise containment:

- add key epoch handling
- add C2-signed revocation messages
- add C2-driven group rekey
- stop accepting old epochs after a grace period

Phase E - adaptive anti-jam control:

- accept channel-change or hopping-plan messages only when signed by C2 or an
  authorized role
- keep fixed schedule fallback
- keep rendezvous channel

### 15. Mesh Log Ergonomics

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

### 16. Optional Compression

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

### 17. Structured Status Payload

The binary status payload format is now implemented at the protocol codec level.
This gives a stable payload contract for status data regardless of transport
mode.

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

Current runtime behavior:

- `mesh_txrx.py` emits structured binary status payloads by default when
  `message_type=status` and no explicit payload is provided.
- Status `flags` now carry runtime link/crypto/forwarding state for local
  decision and demo observability.

### 18. Channel Agility Hardening

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

1. Re-test three nodes with `[mesh_crypto] enabled = true`, `stream_id =
   0xdeadbeef`, `ttl=2`, channel hopping, and sync heartbeats.
2. Confirm Wireshark/tcpdump filtering on `57:42:de:ad:be:ef`.
3. Keep current plaintext mode available for debugging only.
4. Add typed route addressing with `origin_type`, `destination_type`, and
   `traffic_class`.
5. Add config sections for C2 keys: `[c2_uplink_crypto]`,
   `[c2_downlink_crypto]`, `[c2_broadcast_crypto]`, `[node_identity]`, and
   `[trusted_c2]`.
6. Implement node-to-C2 opaque payloads: relays can forward, only C2 can
   decrypt.
7. Implement C2-to-node opaque payloads: relays can forward, only the target
   node can decrypt.
8. Add C2 command signatures, expiry checks, target checks, and command replay
   windows.
9. Add key epochs to every secure packet and reject stale epochs after a grace
    period.
10. Add C2-signed revocation and group rekey messages.
11. Re-test three nodes with secure mesh status and opaque C2 test payloads.
12. Only after that, work on adaptive channel switching messages.
13. Keep channel hopping decisions based on authenticated data only.
