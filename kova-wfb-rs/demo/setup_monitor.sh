#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 <iface> [wifi-channel]" >&2
  echo "example: $0 wlxfc221c2004ce 36" >&2
  exit 2
fi

NIC="$1"
CHANNEL="${2:-36}"

sudo nmcli dev set "$NIC" managed no || true
sudo ip link set "$NIC" down
sudo iw dev "$NIC" set type monitor
sudo ip link set "$NIC" up
sudo iw dev "$NIC" set channel "$CHANNEL" HT20
sudo iw dev "$NIC" set power_save off

iw dev "$NIC" info
