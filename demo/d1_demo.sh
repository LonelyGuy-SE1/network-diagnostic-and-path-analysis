#!/usr/bin/env bash
# D1 demo: Ryu discovers the diamond, the C++ probes measure both paths and
# report to the controller, a fault on the active path triggers a reroute.
#
#   ./demo/d1_demo.sh                 # uses .venv/bin/ryu-manager, kernel OVS + netem
#   RYU_MANAGER=/path/to/ryu-manager ./demo/d1_demo.sh --datapath user
#
# Logs: demo-logs/ (ryu.log, client.log, flow dumps per phase, JSON snapshots).
set -euo pipefail
cd "$(dirname "$0")/.."

RYU=${RYU_MANAGER:-$PWD/.venv/bin/ryu-manager}
make -s -C telemetry all
sudo mn -c >/dev/null 2>&1 || true
sudo pkill -f ndpa_controller.py 2>/dev/null || true
rm -rf demo-logs
rc=0
sudo python3 tests/integration/test_controller.py --ryu "$RYU" --logdir demo-logs "$@" || rc=$?
sudo chown -R "$(id -u):$(id -g)" demo-logs 2>/dev/null || true
echo
echo "controller decisions (demo-logs/ryu.log):"
grep -E " (HOST|PATHS|ACTIVE|CLASSIFY|PORT|REROUTE) " demo-logs/ryu.log || true
exit $rc
