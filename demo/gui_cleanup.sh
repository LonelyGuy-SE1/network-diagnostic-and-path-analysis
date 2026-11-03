#!/usr/bin/env bash
# Tear down leftover Mininet/OVS bits without `mn -c` (that killall ryu-manager).
set -uo pipefail
pkill -f 'demo/gui_demo.py' 2>/dev/null || true
pkill -f 'topology/diamond.py' 2>/dev/null || true
pkill -x client 2>/dev/null || true
pkill -x server 2>/dev/null || true
sleep 0.3
if command -v ovs-vsctl >/dev/null 2>&1; then
  for br in $(ovs-vsctl list-br 2>/dev/null | grep -E '^s[0-9]+$' || true); do
    ovs-vsctl --if-exists del-br "$br" >/dev/null 2>&1 || true
  done
fi
# Half-built veths from a killed Mininet: "RTNETLINK answers: File exists"
while read -r name; do
  [ -n "$name" ] || continue
  ip link delete "$name" 2>/dev/null || true
done < <(ip -o link show 2>/dev/null | sed -n 's/^[0-9]*: \([^:@]*\).*/\1/p' | grep -E '^(s[0-9]+-eth|h[0-9]+-eth|root-eth)' || true)
# Host netns Mininet sometimes leaves behind
if command -v ip >/dev/null 2>&1; then
  for ns in $(ip netns list 2>/dev/null | awk '{print $1}' | grep -E '^(h[0-9]+|s[0-9]+)$' || true); do
    ip netns delete "$ns" 2>/dev/null || true
  done
fi
exit 0
