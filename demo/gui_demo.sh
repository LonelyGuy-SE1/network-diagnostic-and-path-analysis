#!/usr/bin/env bash
# GUI demo wrapper — does NOT touch ./demo/d1_demo.sh.
#
# IMPORTANT: do NOT run d1_demo.sh for the dashboard. That script
#   sudo pkill -f ndpa_controller.py
# and will kill the Ryu you started for /ndpa/.
#
# Preferred: ./demo/gui.sh  (Ryu + agent + this demo, one sudo)
# Manual:    start ryu-manager, then sudo ./demo/gui_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! curl -sf --max-time 2 http://127.0.0.1:8080/ndpa/paths >/dev/null; then
  echo "Ryu is not serving http://127.0.0.1:8080/ndpa/ — start it FIRST in another terminal:"
  echo "  .venv/bin/ryu-manager --observe-links controller/ndpa_controller.py"
  echo
  echo "Do not use ./demo/d1_demo.sh for the GUI (it kills ndpa_controller)."
  exit 1
fi

make -s -C telemetry all
# NEVER run `mn -c` here — Mininet cleanup does `killall ryu-manager`
# and would murder the dashboard controller in the other terminal.
pkill -f 'topology/diamond.py' 2>/dev/null || true
# Best-effort leftover OVS bridges only (no process massacre).
for br in s1 s2 s3 s4; do ovs-vsctl --if-exists del-br "$br" 2>/dev/null || true; done
exec python3 demo/gui_demo.py "$@"
