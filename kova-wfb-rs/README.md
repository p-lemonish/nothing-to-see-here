# wfb_rs

Rust-only plaintext framing implementation:

- synthetic IEEE802.11 header injection (stream-id embedded in addr2/addr3)
- HT radiotap TX header template
- pcap capture + radiotap parsing (metadata best-effort)
- plaintext payload framing (`WFB_PROTO_VERSION = 0`)

## Terminology

Two different "channel" concepts, do not confuse:

- **Wifi channel** — physical RF channel (e.g. `36`, `52`). Set on the NIC out-of-band with `iw dev … set channel`. wfb_rs does not touch it.
- **`stream_id`** — a 32-bit logical demux tag baked into the synthetic 802.11 addr2/addr3 MACs as `57:42:<stream_id big-endian>`. Lets multiple independent wfb streams share the same RF channel; RX drops frames whose embedded `stream_id` does not match.

Two peers must agree on **both** the wifi channel and the `stream_id` to exchange traffic.
The current team stream id is `0xdeadbeef`, which appears in packet captures as
`57:42:de:ad:be:ef`.

## Runtime prerequisites

1. A `wlan*` interface capable of monitor mode (required for both TX injection and RX).
2. Privileges for raw sockets / packet capture: `CAP_NET_RAW` and `CAP_NET_ADMIN`. Either run with `sudo`, or grant caps to the built binary:

   ```bash
   sudo setcap cap_net_raw,cap_net_admin=eip ./target/debug/examples/simple_txrx
   ```

   (Caps are stripped on every rebuild — re-apply after `cargo build`.)
3. The capture interface must produce `DLT_IEEE802_11_RADIO` packets.
4. Plaintext framing only — FEC/encryption are intentionally deferred.

## System dependencies

```bash
sudo apt install libpcap-dev
```

## Putting an interface into monitor mode

```bash
iw dev              # list network interfaces
ethtool -i wlan1    # print attached driver, kernel version (should be rtl88xxau_wfb)

NIC=wlan1           # adjust

sudo nmcli dev set "$NIC" managed no
sudo ip link set "$NIC" down
sudo iw dev "$NIC" set type monitor
sudo ip link set "$NIC" up
sudo iw dev "$NIC" set channel 36 HT20 # = WiFi ch 36, High Throughput, 20MHz wide
sudo iw dev "$NIC" set power_save off

iw dev "$NIC" info        # verify type=monitor, channel set
```

Both peers must be on the same wifi channel.

## Build

```bash
cargo build                 # library only (rlib + cdylib + staticlib)
cargo build --examples      # also builds simple_txrx and bandwidth binaries
cargo build --release       # release artifacts
```

Example binaries land in `target/debug/examples/` (or `target/release/examples/`). Library artifacts land in `target/{debug,release}/`:

- `libwfb_rs.rlib`
- `libwfb_rs.a`
- `libwfb_rs.so`

If `CARGO_TARGET_DIR` is set, outputs go under `$CARGO_TARGET_DIR/...` instead.

## Examples

The two example invocation styles are equivalent — pick one:

- `cargo run --example <name> -- <args>` — builds and runs in one step. Awkward with `sudo` as it'll pollute `target/` ownership.
- `sudo ./target/debug/examples/<name> <args>` — run the prebuilt binary (after `cargo build --examples`).

### `simple_txrx` — interactive stdin chat

Both TX and RX on a single interface. Frames it injects itself are filtered out of its RX (`ignore_self_injected: true`), so to see anything received you need a **second peer** running the same example on the same wifi channel and `stream_id`.

On each peer:
```bash
sudo ./target/debug/examples/simple_txrx --iface "$NIC" --stream-id 0xdeadbeef
```

Then type a line on peer A's stdin — it appears as `RX seq=… payload="…"` on peer B.

Flags:

- `-m data` (default) | `-m rts` — synthetic frame subtype
- `--print-rssi` — include radiotap RSSI in RX output
- `-c` accepts hex (`0x1234`) or decimal

### `bandwidth` — self-generating throughput test

Separate `--role tx` and `--role rx` processes on two peers. The TX side generates payload on its own (no stdin).

Receiver:

```bash
sudo ./target/debug/examples/bandwidth --role rx --iface "$NIC" --stream-id 0xdeadbeef
```

Sender:

```bash
sudo ./target/debug/examples/bandwidth --role tx --iface "$NIC" --stream-id 0xdeadbeef
```

Useful knobs:

- `--payload-size` (default `1200`)
- `--tx-interval-us` (default `0`, as fast as possible)
- `--report-ms` (default `1000`)

## Sniffing wfb_rs traffic with tcpdump

wfb_rs-injected frames carry a fixed addr2/addr3 of `57:42:<stream_id big-endian>`. For `--stream-id 0xdeadbeef` that is `57:42:de:ad:be:ef`:

```bash
sudo tcpdump -i "$NIC" -y IEEE802_11_RADIO -nn -e -vvv 'wlan addr2 57:42:de:ad:be:ef'
```

Useful Wireshark filters:

```text
wlan.addr == 57:42:de:ad:be:ef
wlan.addr contains 57:42:de:ad:be:ef
```

To see all wfb_rs traffic regardless of `stream_id`, drop the filter and grep visually for addr2 starting `57:42:` — tcpdump's BPF for `wlan[N:M]` indexing on radiotap captures is not reliable across versions.

```bash
sudo tcpdump -y IEEE802_11_RADIO -nn -e -vvv 'wlan[10:2] = 0x5742'
```

## C ABI

Public C ABI declarations live in `include/wfb_rs.h`. Regenerate with `cbindgen`:

```bash
make gen-header
# install cbindgen if missing:
cargo install cbindgen
```

Build and link the C smoke binaries:

```bash
make c-smoke
```

This produces:

- `examples_c/smoke_shared` (links `libwfb_rs.so`)
- `examples_c/smoke_static` (links `libwfb_rs.a`)

Minimal manual link:

```bash
# shared
cc -O2 -Wall -Wextra -Iinclude -o smoke_shared examples_c/smoke.c \
   -L"${CARGO_TARGET_DIR:-target}/release" -lwfb_rs

# static
cc -O2 -Wall -Wextra -Iinclude -o smoke_static examples_c/smoke.c \
   "${CARGO_TARGET_DIR:-target}/release/libwfb_rs.a" -lpcap -ldl -lpthread -lm
```

### ABI stability

Minimal v1 ABI: opaque TX/RX handles plus lifecycle/send/recv calls. Intentionally narrow; future extensions should be additive.

## Python bindings (ctypes)

Standalone bindings under `python/`, loaded over `libwfb_rs.so` via `ctypes`.

```bash
cargo build --release
python -m venv .venv
source .venv/bin/activate
pip install -e python
```

If the loader cannot find the shared library, point it explicitly:

```bash
export WFB_RS_LIB_PATH=/absolute/path/to/libwfb_rs.so
```

Usage:

```python
from wfb_rs_py import Tx, Rx

with Tx(iface="wlan0", stream_id=0xdeadbeef) as tx:
    tx.send(b"hello", seq=1)

with Rx(iface="wlan0", stream_id=0xdeadbeef) as rx:
    maybe_frame = rx.recv_optional(timeout_ms=100)
    if maybe_frame is not None:
        payload, meta = maybe_frame
        print(payload, meta.seq)
```

Runnable example:

```bash
cd python
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py --stream-id 0xdeadbeef
```

If `NIC`, `WFB_IFACE`, or `IFACE` is set, the Python example uses it as the
default interface. To smoke-test one adapter and see the injected frame in
Wireshark/tcpdump:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --message "hello world" --include-self --stream-id 0xdeadbeef
```

To keep broadcasting while other peers come online on the same wifi channel and
`stream_id`:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --message "hello world" --count 0 --tx-interval-ms 1000 --stream-id 0xdeadbeef
```

The Python example also has an optional v0 app protocol header for compact
sender identity. Keep the same wifi channel and `stream_id` for the group; give
each node a distinct `--sender-id`.

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
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node1.ini
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node2.ini
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node3.ini
```

The starter configs enable experimental synchronized channel hopping across
channels `36,40,48` with 5-second slots, anchored to Unix UTC time
(`hop_epoch_ms = 0`). They also send compact `sync` heartbeats every 5 seconds
so logs show clock skew, slot agreement, and channel agreement between nodes.
To hold a node on its current channel while debugging, add `--no-channel-agility`:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py \
  --config configs/node1.ini --no-channel-agility
```

The starter configs enable `[mesh_crypto]` by default. Keep the same `key_id`,
`key_epoch`, and 32-byte `key_hex` on every node. Non-`sync` mesh payloads are
encrypted/authenticated with ChaCha20-Poly1305; relays authenticate, decrypt,
decrement TTL, and re-encrypt mesh-group traffic for the next hop. `sync`
heartbeats remain plaintext for observability. To temporarily debug plaintext
mesh traffic, set `[mesh_crypto] enabled = false` in each node config.

Expected secure-mode log shape:

```text
CH=36 TX secure origin=1 seq=... domain=mesh_group key_id=1001 key_epoch=1 len=13
CH=36 RX secure via=2 origin=2 seq=... domain=mesh_group key_id=1001 key_epoch=1 decrypted=1
CH=36 RX deliver via=2 origin=2 seq=... dest=0 ttl=2 type=status payload=[encrypted]
```

For prototype C2 end-to-end encryption, use `route_v2` by switching a node to
`traffic_class=c2_uplink`. Relays forward these packets as opaque ciphertext and
do not decrypt the payload. The cloud C2 HTTP receiver decrypts the uploaded
opaque payloads and shows them in a small web UI:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/c2_http_server.py \
  --config configs/c2-local.ini --host 0.0.0.0 --port 8080
```

Then open:

```text
http://80.69.173.183:8080/
```

A dedicated RX-only RF gateway can bridge mesh C2 uplinks to the cloud without
changing the normal node process. Edit `configs/c2-gateway.ini` with the
gateway dongle iface, then run:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/c2_gateway.py --config configs/c2-gateway.ini
```

If the gateway should reuse a node already running on the same PC, leave the
gateway iface unset and use the auto config. The node shares opaque C2 uplinks
over a localhost UDP tap, so the gateway does not depend on RF self-capture from
the same adapter:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/c2_gateway.py --config configs/c2-gateway-auto.ini
```

The auto config listens on `127.0.0.1:17801`. The node configs send local C2
tap events there while still transmitting the same opaque packet over RF.

The normal mesh processes can keep running unchanged:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node1.ini
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node2.ini
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node3.ini
```

If the gateway shares the exact same dongle with another process, avoid two
processes fighting over channel changes. Either run the gateway with
`--no-channel-agility` and let the mesh process own hopping, or use a dedicated
gateway dongle.

To originate an opaque C2-bound datagram from a node:

```bash
python/.venv/bin/python python/examples/c2_send.py \
  --config configs/node1.ini \
  --message "node 1 c2 test"
```

The running node keeps its normal channel agility and emits `TX route_v2 ...
class=c2_uplink ... source=local_control`. Relay nodes should log
`RX opaque_route ... decrypt_skipped=1`. The gateway logs `HTTP c2_forward ...
ok=1`, and the cloud page shows the decrypted payload under the origin node.

The node configs also include a `[c2_uplink]` section. When enabled, the normal
`mesh_txrx.py --config configs/nodeN.ini` process periodically sends
`node N c2 test {counter}` as an encrypted C2 uplink while continuing normal
mesh status and channel hopping. The C2 dashboard shows one latest row per node,
updated in place as the counter changes.
