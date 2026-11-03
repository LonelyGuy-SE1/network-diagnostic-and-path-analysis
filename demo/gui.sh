#!/usr/bin/env bash
# One command for the live GUI stack.
#
#   ./demo/gui.sh              # start Ryu (bg) + root agent + auto demo
#   ./demo/gui.sh --serve      # Ryu + agent only; use dashboard Demo/Run
#   ./demo/gui.sh --hold 60    # longer watch window after faults
#
# Prompts for sudo ONCE (agent). Dashboard buttons then work without
# passwordless sudo. Does not touch ./demo/d1_demo.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HOLD=45
AUTO_DEMO=1
SERVE_FOREVER=1
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --serve) AUTO_DEMO=0; shift ;;
    --once) SERVE_FOREVER=0; shift ;;
    --hold) HOLD="${2:?}"; shift 2 ;;
    --datapath) EXTRA+=(--datapath "$2"); shift 2 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

RYU_BIN="${RYU_MANAGER:-$ROOT/.venv/bin/ryu-manager}"
if [[ ! -x "$RYU_BIN" ]]; then
  RYU_BIN="$(command -v ryu-manager || true)"
fi
if [[ -z "$RYU_BIN" ]]; then
  echo "ryu-manager not found — create .venv (see README) or set RYU_MANAGER" >&2
  exit 1
fi

make -s -C telemetry all

ryu_up() { curl -sf --max-time 1 http://127.0.0.1:8080/ndpa/topology >/dev/null 2>&1; }

RYU_STARTED=0
if ! ryu_up; then
  echo "[gui] starting Ryu → demo-logs/gui/ryu.log"
  mkdir -p demo-logs/gui
  # Prefer the invoking user even if this script was run under sudo.
  RUN_AS="${SUDO_USER:-$USER}"
  if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
    sudo -u "$SUDO_USER" -H -- "$RYU_BIN" --observe-links controller/ndpa_controller.py \
      >>demo-logs/gui/ryu.log 2>&1 &
  else
    "$RYU_BIN" --observe-links controller/ndpa_controller.py >>demo-logs/gui/ryu.log 2>&1 &
  fi
  RYU_PID=$!
  RYU_STARTED=1
  for _ in $(seq 1 50); do
    ryu_up && break
    kill -0 "$RYU_PID" 2>/dev/null || { echo "[gui] Ryu exited — see demo-logs/gui/ryu.log" >&2; exit 1; }
    sleep 0.2
  done
  ryu_up || { echo "[gui] Ryu did not open :8080" >&2; exit 1; }
  echo "[gui] Ryu ready (pid $RYU_PID)"
else
  echo "[gui] Ryu already on :8080"
fi

AGENT_ARGS=(--hold "$HOLD")
[[ "$AUTO_DEMO" -eq 1 ]] && AGENT_ARGS+=(--auto-demo)
[[ "$SERVE_FOREVER" -eq 0 ]] && AGENT_ARGS+=(--once)
# Forward datapath to demo via request file env — gui_demo reads argv from agent only for auto path.
# Auto-demo uses gui_demo.sh which accepts --datapath via EXTRA baked into a wrapper env.
export NDPA_GUI_DEMO_EXTRA="${EXTRA[*]-}"

echo "[gui] dashboard → http://127.0.0.1:8080/ndpa/"
echo "[gui] starting root agent (sudo once) — Demo/Run buttons talk to this agent"
echo "[gui] Ctrl-C stops the agent (Ryu keeps running)"

cleanup() {
  if [[ -n "${AGENT_PID:-}" ]]; then
    sudo kill "$AGENT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Keep sudo ticket warm for the agent process.
sudo -v
sudo python3 "$ROOT/demo/gui_agent.py" "${AGENT_ARGS[@]}" &
AGENT_PID=$!

# Wait for heartbeat
for _ in $(seq 1 50); do
  [[ -f /tmp/ndpa_gui_agent/heartbeat ]] && break
  sleep 0.1
done

if [[ "$AUTO_DEMO" -eq 1 ]]; then
  echo "[gui] auto demo running — watch paths/events in the browser"
fi

wait "$AGENT_PID" || true
AGENT_PID=
echo "[gui] agent stopped"
[[ "$RYU_STARTED" -eq 1 ]] && echo "[gui] Ryu still running in background (pid $RYU_PID) — stop with: pkill -f ndpa_controller"
