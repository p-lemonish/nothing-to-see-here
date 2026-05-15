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

```python
from wfb_rs_py import Tx, Rx

with Tx(iface="wlan0", stream_id=1) as tx:
    tx.send(b"hello", seq=1)

with Rx(iface="wlan0", stream_id=1) as rx:
    result = rx.recv_optional(timeout_ms=100)
    if result is not None:
        payload, meta = result
        print(payload, meta)
```

Runnable example script:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py --iface "$NIC" --stream-id 1
```

If `NIC`, `WFB_IFACE`, or `IFACE` is set, `--iface` can be omitted. For a
single-adapter smoke test that should also show up in Wireshark/tcpdump:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --message "hello world" --include-self --stream-id 1
```

For a repeated broadcast while bringing up peers on the same wifi channel and
`stream_id`:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --message "hello world" --count 0 --tx-interval-ms 1000 --stream-id 1
```

To include the v0 app header with a compact sender id, use app protocol mode.
All peers that should hear each other still use the same `stream_id`; each node
gets its own `--sender-id`.

Receiver:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --iface "$RXNIC" --stream-id 1 --app-proto --sender-id 2
```

Sender:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" examples/simple_txrx.py \
  --iface "$TXNIC" --stream-id 1 --app-proto --sender-id 1 \
  --message "hello world" --message-type hello --count 0 --tx-interval-ms 1000
```

## Tests

```bash
pip install -e .[test]
pytest -q
```

Runtime tests auto-skip when `libwfb_rs.so` is not available.
