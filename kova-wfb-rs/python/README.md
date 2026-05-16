# wfb-rs-py

Standalone Python bindings for `wfb_rs` using `ctypes` over the C ABI (`libwfb_rs.so`).

## Prerequisites

1. Build the Rust shared library:

```bash
cd ../
cargo build --release
```

2. Ensure the dynamic library is discoverable:
   - Set `WFB_RS_LIB_PATH` to the full `libwfb_rs.so` path, or
   - Keep the default build output in `../target/release/libwfb_rs.so`.

## Install

From this directory (`wfb_rs/python`):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

The current team stream id is `0xdeadbeef`, which appears in Wireshark/tcpdump
as `57:42:de:ad:be:ef`.

```python
from wfb_rs_py import Tx, Rx

with Tx(iface="wlan0", stream_id=0xdeadbeef) as tx:
    tx.send(b"hello", seq=1)

with Rx(iface="wlan0", stream_id=0xdeadbeef) as rx:
    result = rx.recv_optional(timeout_ms=100)
    if result is not None:
        payload, meta = result
        print(payload, meta)
```

Runnable example script:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py --iface "$NIC" --stream-id 0xdeadbeef
```

If `NIC`, `WFB_IFACE`, or `IFACE` is set, `--iface` can be omitted. For a
single-adapter smoke test that should also show up in Wireshark/tcpdump:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --message "hello world" --include-self --stream-id 0xdeadbeef
```

For a repeated broadcast while bringing up peers on the same wifi channel and
`stream_id`:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --message "hello world" --count 0 --tx-interval-ms 1000 --stream-id 0xdeadbeef
```

To include the v0 app header with a compact sender id, use app protocol mode.
All peers that should hear each other still use the same `stream_id`; each node
gets its own `--sender-id`.

Peer A:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --iface "$NIC" --stream-id 0xdeadbeef --app-proto --sender-id 1 \
  --message "hello from node 1" --message-type hello --count 0 --tx-interval-ms 1000
```

Peer B:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --iface "$NIC" --stream-id 0xdeadbeef --app-proto --sender-id 2 \
  --message "hello from node 2" --message-type hello --count 0 --tx-interval-ms 1000
```

TTL-limited mesh flooding uses the `route_data` wrapper. Run this on each relay
or receiver node with a unique sender id:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/mesh_txrx.py \
  --iface "$NIC" --stream-id 0xdeadbeef --sender-id 67
```

To originate a broadcast routed message:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/mesh_txrx.py \
  --iface "$NIC" --stream-id 0xdeadbeef --sender-id 42 \
  --message "battery=91" --message-type status --destination-id 0 \
  --ttl 2 --count 0 --tx-interval-ms 1000
```

For normal multi-node testing, prefer config files. Edit each file's `iface`
from `iw dev`, then run:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/mesh_txrx.py --config ../configs/node1.ini
sudo -E "$VIRTUAL_ENV/bin/python" examples/mesh_txrx.py --config ../configs/node2.ini
sudo -E "$VIRTUAL_ENV/bin/python" examples/mesh_txrx.py --config ../configs/node3.ini
```

The starter configs enable experimental synchronized channel hopping across
channels `36,40,48` with 5-second slots, anchored to Unix UTC time
(`hop_epoch_ms = 0`). They also send compact `sync` heartbeats every 5 seconds
so logs show clock skew, slot agreement, and channel agreement between nodes.
To hold a node on its current channel while debugging, add `--no-channel-agility`.

The starter configs enable `[mesh_crypto]` by default. Keep the same `key_id`,
`key_epoch`, and 32-byte `key_hex` on every node. Non-`sync` mesh payloads are
encrypted/authenticated with ChaCha20-Poly1305. `sync` heartbeats remain
plaintext for observability. To temporarily debug plaintext mesh traffic, set
`[mesh_crypto] enabled = false` in each node config.

For prototype C2 end-to-end encryption, switch a sender to `route_v2` with
`traffic_class=c2_uplink`. The cloud C2 HTTP receiver decrypts uploaded opaque
payloads and displays them per node:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/c2_http_server.py \
  --config ../configs/c2-local.ini --host 0.0.0.0 --port 8080
```

Relays forward encrypted payloads without decrypting them. Prefer a dedicated
RX-only gateway process to bridge RF packets to the cloud:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/c2_gateway.py --config ../configs/c2-gateway.ini
```

To reuse a node already running on the same PC, use the auto config instead.
The node shares opaque C2 uplinks over a localhost UDP tap, so the gateway does
not depend on RF self-capture from the same adapter:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/c2_gateway.py --config ../configs/c2-gateway-auto.ini
```

The auto config listens on `127.0.0.1:17801`. The node configs send local C2 tap
events there while still transmitting the same opaque packet over RF.

The normal mesh process can keep running as-is:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/mesh_txrx.py --config ../configs/node1.ini
```

To originate a C2-bound encrypted datagram from a node:

```bash
python examples/c2_send.py \
  --config ../configs/node1.ini \
  --message "node 1 c2 test"
```

The running node keeps normal channel agility and logs `TX route_v2 ...
class=c2_uplink ... source=local_control`. Relay logs should show
`RX opaque_route ... decrypt_skipped=1`; the gateway logs `HTTP c2_forward ...
ok=1`; the cloud page shows decrypted payloads per node.
If the gateway shares a dongle with a mesh process, run it with
`--no-channel-agility` or use a dedicated gateway dongle.

The node configs also include a `[c2_uplink]` section. When enabled, the normal
`mesh_txrx.py --config ../configs/nodeN.ini` process periodically sends
`node N c2 test {counter}` as an encrypted C2 uplink while continuing normal
mesh status and channel hopping. The C2 dashboard shows one latest row per node,
updated in place as the counter changes.

## Tests

```bash
pip install -e .[test]
pytest -q
```

Runtime tests auto-skip when `libwfb_rs.so` is not available.
