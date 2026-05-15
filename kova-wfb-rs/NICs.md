```bash
export RXNIC=wlx5cffffaba18f
export TXNIC=wlx5cffffabb301

for NIC in "$RXNIC" "$TXNIC"; do
  sudo nmcli dev set "$NIC" managed no
  sudo ip link set "$NIC" down
  sudo iw dev "$NIC" set type monitor
  sudo ip link set "$NIC" up
  sudo iw dev "$NIC" set channel 36 HT20
  sudo iw dev "$NIC" set power_save off
  iw dev "$NIC" info
done
```

```bash
sudo python/.venv/bin/python python/examples/simple_txrx.py --iface "$TXNIC" --message "hello world" --count 0 --tx-interval-ms 1000 --stream-id 6767
sudo python/.venv/bin/python python/examples/simple_txrx.py --iface "$RXNIC" --stream-id 6767
```
