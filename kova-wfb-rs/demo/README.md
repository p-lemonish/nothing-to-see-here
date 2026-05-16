# Hackathon Mesh Dashboard

Small demo layer for visualizing three transmitter "drones" and the base
receiver. It runs in simulation mode by default and can also listen to real
`wfb_rs` app-protocol frames.

For a Linux VM + `uv` step-by-step setup/run guide, see
[`LINUX_VM_UV_RUNBOOK.md`](./LINUX_VM_UV_RUNBOOK.md).

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
export NIC=wlx... # replace with the Interface value from iw dev
demo/setup_monitor.sh "$NIC" 36
```

The Python demo scripts use `--iface` when supplied, otherwise `$NIC`,
`$WFB_IFACE`, `$IFACE`, or the single interface reported by `iw dev`.
If more than one interface is present, pass `--iface` explicitly.

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
  --stream-id 1
```

The dashboard decodes both direct app frames and `route_data` mesh frames.
Open `http://127.0.0.1:8765` on the base computer.

The current upstream starter configs already contain the monitor interface,
`stream_id`, and synchronized channel-hopping schedule. If the transmitter nodes
are using those configs, point the dashboard at the base node's config:

```bash
sudo -E python3 demo/dashboard_server.py \
  --source radio \
  --config configs/node1.ini
```

Explicit flags still override the config values when you need a one-off change.

## Real Transmitter Heartbeats

On each transmitter host/interface, use that node's config:

```bash
sudo -E python3 demo/node_status_tx.py --config configs/node1.ini
```

Change the config file for TX-2 and TX-3:

```bash
sudo -E python3 demo/node_status_tx.py --config configs/node2.ini
sudo -E python3 demo/node_status_tx.py --config configs/node3.ini
```

To originate a mesh-wrapped status packet instead of a direct status frame, add
`--mesh --ttl 2`. Nodes that should relay still need to run the existing
`python/examples/mesh_txrx.py` process.

To let one physical transmitter advertise additional simulated nodes, repeat
`--sim-node`. With `--sim-node`, the helper uses mesh wrapping by default so the
dashboard sees the simulated node as the route origin and the physical
transmitter as the `via` node:

```bash
sudo -E python3 demo/node_status_tx.py --config configs/node1.ini \
  --sim-node 11,SIM-11,0.32,0.44,91 \
  --sim-node 12,SIM-12,0.42,0.58,88
```

The simulated nodes are not dashboard-only objects in this mode. They are normal
radio status heartbeats with their own logical sender IDs, sequence counters,
positions, and batteries. If the physical transmitter stops, the real node and
all simulated nodes advertised through it age out together.

To make the helper follow the same channel-hopping schedule as the current mesh
configs, add:

```bash
--channel-agility --hop-channels 36,40,48 --hop-slot-ms 5000 --hop-epoch-ms 0
```

## Base Receiver Checklist

1. Plug in the base USB Wi-Fi adapter.
2. Confirm the patched driver is active with `ethtool -i "$RXNIC"`.
3. Run `demo/setup_monitor.sh "$RXNIC" 36`.
4. Start the dashboard with `--config configs/node1.ini`, matching the
   transmitter schedule.
5. Start one transmitter heartbeat and confirm TX-1 turns online.
6. Start TX-2 and TX-3.
