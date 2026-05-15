```bash
sudo python/.venv/bin/python python/examples/mesh_txrx.py --config configs/node1.ini
sudo python/.venv/bin/python python/examples/mesh_txrx.py --config configs/node2.ini
sudo python/.venv/bin/python python/examples/mesh_txrx.py --config configs/node3.ini
```
```bash
iw dev
# take note of RX and TX NIC of wifi dongles
export RXNIC=wlx5cffffaba18f # use your own..
export TXNIC=wlx5cffffabb301 # use your own..
```

Run this to get both ifaces into monitor mode and up (on ch36)

```bash
sudo nmcli dev set "$TXNIC" managed no
sudo ip link set "$TXNIC" down
sudo iw dev "$TXNIC" set type monitor
sudo ip link set "$TXNIC" up
sudo iw dev "$TXNIC" set channel 36 HT20
sudo iw dev "$TXNIC" set power_save off

sudo nmcli dev set "$RXNIC" managed no
sudo ip link set "$RXNIC" down
sudo iw dev "$RXNIC" set type monitor
sudo ip link set "$RXNIC" up
sudo iw dev "$RXNIC" set channel 36 HT20
sudo iw dev "$RXNIC" set power_save off
```

```bash
sudo python/.venv/bin/python python/examples/simple_txrx.py --iface "$TXNIC" --stream-id 1 --app-proto --sender-id 42 --message "hello 67" --message-type hello --count 0 --tx-interval-ms 1000
sudo python/.venv/bin/python python/examples/simple_txrx.py --iface "$RXNIC" --stream-id 1 --app-proto --sender-id 67 --message "hello 42" --message-type hello --count 0 --tx-interval-ms 1000
```
