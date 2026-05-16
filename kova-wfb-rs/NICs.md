# Full setup (uv + venv + bindings)

```bash
cd kova-wfb-rs

# one-time: install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# build the Rust shared library used by Python bindings
cargo build --release

# create and populate Python venv with uv
cd python
uv venv .venv
source .venv/bin/activate
uv pip install -e .
cd ..
```

# Start from here for runtime commands

```bash
iw dev
# pick the monitor-capable NIC for this node
export NIC=wlx5cffffaba18f # use your own
```

Put the NIC into monitor mode on ch36:

```bash
sudo nmcli dev set "$NIC" managed no
sudo ip link set "$NIC" down
sudo iw dev "$NIC" set type monitor
sudo ip link set "$NIC" up
sudo iw dev "$NIC" set channel 36 HT20
sudo iw dev "$NIC" set power_save off
```

Run one process per peer (same WiFi channel + stream id, different sender ids):

```bash
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/simple_txrx.py --iface "$NIC" --stream-id 1 --app-proto --sender-id 42 --message "hello 67" --message-type hello --count 0 --tx-interval-ms 1000
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/simple_txrx.py --iface "$NIC" --stream-id 1 --app-proto --sender-id 67 --message "hello 42" --message-type hello --count 0 --tx-interval-ms 1000
```

Optional: config-based mesh example:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node1.ini
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node2.ini
sudo -E "$VIRTUAL_ENV/bin/python" python/examples/mesh_txrx.py --config configs/node3.ini
```
