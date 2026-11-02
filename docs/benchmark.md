# Benchmark: static baseline vs dynamic controller (issue #17)

`eval/benchmark.py` runs the same faults twice on the Mininet diamond and writes one CSV row per run.

| Mode | What runs |
|---|---|
| `static` | No controller. Fixed flows (`diamond.install_static_flows`) send probes and live traffic down path A. Nothing ever moves them. This is the "static shortest path" baseline. |
| `dynamic` | The full system: Ryu app, probe reports over the management link, classification, rerouting. |

## Run it

```bash
make -C telemetry all
sudo python3 eval/benchmark.py --ryu ~/ryuenv/bin/ryu-manager --quick      # about 6 minutes, 1 run each
sudo python3 eval/benchmark.py --ryu ~/ryuenv/bin/ryu-manager              # 3 runs each, about 25 minutes
sudo python3 eval/benchmark.py --modes static --scenarios hard --reps 2    # a subset
python3 eval/summarize.py eval/results/<timestamp>/runs.csv                # table again, any time
python3 tests/test_eval_metrics.py                                         # unit tests, no Mininet needed
```

Add `--datapath user` if Open vSwitch has no kernel module. `RYU_MANAGER` can replace `--ryu`.
The script runs `mn -c` before every run, which also kills any `ryu-manager` you started by hand.
Output goes to `eval/results/<timestamp>/`: `runs.csv`, and `raw/<mode>-<scenario>-<rep>/` with the
client, server, Ryu and live-stream logs and the controller's final `paths-final.json`.

## Timeline of a run

```
start network (+ Ryu) -> probes on both paths -> live stream starts
  |--- warm-up ---|--- fault on path A, held ---|--- fault removed, settle ---|
                  t_fault                       t_repair
```

Defaults: warm-up 8 s, hold 10 s, settle 12 s (`--warmup --hold --settle`).

## Scenarios

| Scenario | Fault (all on path A, `eval/faults.py`) | Who can see it |
|---|---|---|
| `healthy` | none | nothing to detect: baseline numbers, and the cost of probing |
| `soft` | `tc netem delay 60ms loss 20%` on s2-eth2, link stays up | only the probe reports (above the 5 % loss and 50 ms RTT "degraded" thresholds) |
| `blackhole` | `tc netem loss 100%` on s2-eth2, link stays up | only the probe reports ("failed" at 50 % loss) |
| `hard` | `link s2 s4 down` | OpenFlow port status, so it is fast |

**Recovery** (the issue's fourth scenario) is measured in the settle phase of every fault scenario:
`recover_ms`, `gap_at_repair_ms`, `final_active`, and `live_lost_after`.

## How the numbers are measured

The "live traffic" is a paced UDP stream (`eval/livestream.py`, udp/5004, 100 packets/s of 200 bytes
by default) from h1 to h2. Each packet carries a sequence number and its send time, and the receiver
logs every arrival. Both modes are measured from this stream, so the comparison does not depend on
the controller. The C++ client probes both paths in both modes (udp/5001 and 5002).

| Column | Meaning |
|---|---|
| `outage_ms` | longest run of consecutive lost live packets, times the packet interval. Meaningful for blackholes and link failures; for random loss (`soft`) read `live_loss_pct` instead |
| `live_sent`, `live_recv`, `live_lost`, `live_loss_pct` | whole run |
| `live_lost_before / _fault / _after` | lost packets by when they were sent (before the fault, during it, after the repair) |
| `goodput_before / _fault / _after_kbps` | delivered payload per second in each window (throughput of the paced stream, not a link-capacity test) |
| `owd_before / _fault / _after_ms` | median one-way delay of the live stream in each window (sender and receiver share a clock) |
| `gap_at_repair_ms` | gap between the last packet that arrived before the repair and the first after it: about one packet interval if service was flowing, the leftover outage if it was not |
| `detect_ms` | dynamic only: fault injected until the controller no longer reports path A as healthy |
| `reroute_ms` | dynamic only: fault injected until the controller's active path is not A |
| `ctrl_first_bad_to_reroute_ms` | the controller's own figure, measured from the first bad report |
| `recover_ms` | dynamic only: repair until path A is classified healthy again (includes the controller's 3-report hysteresis) |
| `final_active`, `reroutes_total` | dynamic only: which path carries live traffic at the end (it should stay on B, no flapping), and the number of reroutes |
| `A_rtt_before/fault/after`, `A_loss_...`, `B_...` | the C++ client's own per-path RTT (avg, ms) and windowed loss (%) at the end of each phase |
| `detected` | dynamic fault runs: 1 if the controller noticed the fault during the hold |
| `error` | set if the run failed; the remaining runs still go on |

`detect_ms`, `reroute_ms` and `recover_ms` come from polling the controller's REST view every few
tens of milliseconds, so they are good to about that. `t_fault` is taken just before the fault
command, so the command's own latency (tens of ms) is included. As the README says, absolute
numbers inside Mininet are limited by scheduler jitter, so compare static against dynamic, not
against hardware figures.

## What to expect

Static cannot recover while the fault lasts, so its outage should be close to `--hold`. Dynamic
should lose packets only until detection and reroute: tens of milliseconds for `hard`, about two
seconds for `blackhole` and `soft` (two bad one-second reports). These are expectations from the
controller's design and the D1 demo, not measured results. The CSV is where the real numbers come from.

## Reproducibility

Use the same machine and the same options for static and dynamic. The two modes alternate within each
repetition, so slow drift in VM speed hits both equally. Keep `runs.csv`, and attach `raw/` logs for
anything surprising.
