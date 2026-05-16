# Hackathon Mesh Dashboard

Small demo layer for visualizing three transmitter "drones" and the base
receiver. It runs in simulation mode by default and can also listen to real
`wfb_rs` app-protocol frames.

## Hardware Shape

- The dashboard computer is the **base receiver node**.
- Each "drone" is a separate transmitter computer with one USB Wi-Fi adapter.
- All adapters must use the same physical Wi-Fi channel and `stream_id`.
- The base receiver only needs RX for this dashboard. The transmitter nodes send
  periodic status heartbeats.

## One-Time Build

Run this on the base computer and on each transmitter computer:

```bash
cargo build --release
```

The Python helpers load `target/release/libwfb_rs.so` automatically. If your
library lives elsewhere, set `WFB_RS_LIB_PATH=/absolute/path/to/libwfb_rs.so`.

## Put Adapters In Monitor Mode

Find the adapter name:

```bash
iw dev
```

Then configure it. Use the same channel on every machine:

```bash
demo/setup_monitor.sh "$NIC" 36
```

## Simulated Dashboard

```bash
python3 demo/dashboard_server.py --source sim
```

Open `http://127.0.0.1:8765`.

The jamming toggles stop simulated heartbeats for selected nodes. The dashboard
marks nodes stale and then down when no heartbeat arrives.

## Real Radio Receiver

Put the receiver interface into monitor mode on the same wifi channel as the
transmitters, then run:

```bash
sudo -E python3 demo/dashboard_server.py \
  --source radio \
  --iface "$RXNIC" \
  --stream-id 1
```

The dashboard decodes both direct app frames and `route_data` mesh frames.
Open `http://127.0.0.1:8765` on the base computer.

The current upstream starter configs enable synchronized channel hopping across
channels `36,40,48`. If the transmitter nodes are using those configs, run the
base dashboard with the same schedule:

```bash
sudo -E python3 demo/dashboard_server.py \
  --source radio \
  --iface "$RXNIC" \
  --stream-id 1 \
  --channel-agility \
  --hop-channels 36,40,48 \
  --hop-slot-ms 5000 \
  --hop-epoch-ms 0
```

## Real Transmitter Heartbeats

On each transmitter host/interface:

```bash
sudo -E python3 demo/node_status_tx.py \
  --iface "$TXNIC" \
  --stream-id 1 \
  --sender-id 1 \
  --label TX-1 \
  --x 0.18 \
  --y 0.34 \
  --battery 96 \
  --interval-ms 1000
```

Change `--sender-id`, `--label`, `--x`, and `--y` for TX-2 and TX-3.
Example positions matching `demo/nodes.json`:

```bash
# TX-1
sudo -E python3 demo/node_status_tx.py --iface "$TXNIC" --stream-id 1 \
  --sender-id 1 --label TX-1 --x 0.18 --y 0.34 --battery 96

# TX-2
sudo -E python3 demo/node_status_tx.py --iface "$TXNIC" --stream-id 1 \
  --sender-id 2 --label TX-2 --x 0.54 --y 0.22 --battery 89

# TX-3
sudo -E python3 demo/node_status_tx.py --iface "$TXNIC" --stream-id 1 \
  --sender-id 3 --label TX-3 --x 0.82 --y 0.48 --battery 82
```

To originate a mesh-wrapped status packet instead of a direct status frame, add
`--mesh --ttl 2`. Nodes that should relay still need to run the existing
`python/examples/mesh_txrx.py` process.

To make the helper follow the same channel-hopping schedule as the current mesh
configs, add:

```bash
--channel-agility --hop-channels 36,40,48 --hop-slot-ms 5000 --hop-epoch-ms 0
```

## Base Receiver Checklist

1. Plug in the base USB Wi-Fi adapter.
2. Confirm the patched driver is active with `ethtool -i "$RXNIC"`.
3. Run `demo/setup_monitor.sh "$RXNIC" 36`.
4. Start the dashboard with either fixed-channel mode or channel-agility mode,
   matching the transmitters.
5. Start one transmitter heartbeat and confirm TX-1 turns online.
6. Start TX-2 and TX-3.
