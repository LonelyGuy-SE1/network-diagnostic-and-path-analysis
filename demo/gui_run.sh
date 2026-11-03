#!/usr/bin/env bash
# Started by the dashboard Run button (via POST /ndpa/run).
# Tears down any old Mininet net and brings up topology/topo.json (or the diamond).
# Must already be root (controller spawns: sudo -n bash demo/gui_run.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
# Do NOT use `mn -c` — it `killall ryu-manager` and kills the live controller.
pkill -f 'topology/diamond.py' 2>/dev/null || true
for br in s1 s2 s3 s4; do ovs-vsctl --if-exists del-br "$br" 2>/dev/null || true; done
exec python3 topology/diamond.py --mgmt
