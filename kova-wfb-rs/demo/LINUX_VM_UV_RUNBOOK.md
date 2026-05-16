# Dashboard quick start (Linux VM + uv)

This is the short version for your current state.

Assumed already done:

1. VM/system setup
2. Driver + monitor mode setup
3. Node config files (`configs/node1.ini`, `node2.ini`, `node3.ini`)

Run all commands in Linux VM from repo root (`kova-wfb-rs`).

## 1. Refresh uv environment (section 2, steps 2 and 3)

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e python
```

If you changed Rust code since last run:

```bash
cargo build --release
```

## 2. Start dashboard receiver (base node)

```bash
sudo -E "$VIRTUAL_ENV/bin/python" demo/dashboard_server.py \
  --source radio \
  --config configs/node1.ini
```

Dashboard URL:

```text
http://127.0.0.1:8765
```

## 3. Start real node transmitters

Run one command per node:

```bash
sudo -E "$VIRTUAL_ENV/bin/python" demo/node_status_tx.py --config configs/node1.ini
sudo -E "$VIRTUAL_ENV/bin/python" demo/node_status_tx.py --config configs/node2.ini
sudo -E "$VIRTUAL_ENV/bin/python" demo/node_status_tx.py --config configs/node3.ini
```

## 4. Confirm live data

1. TX terminals show `TX status node=...`
2. Dashboard terminal shows `Radio RX iface=... stream_id=...`
3. UI nodes go online and packet counters increase
