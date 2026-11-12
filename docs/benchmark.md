# Benchmark: static baseline vs dynamic controller

`eval/benchmark.py` runs the same faults twice on the Mininet diamond and writes one CSV row per run.

| Mode | What runs |
|---|---|
| `static` | No controller. Fixed flows (`diamond.install_static_flows`) send probes and live traffic down path A. Nothing ever moves them. This is the "static shortest path" baseline. |
| `dynamic` | The full system: Ryu app, probe reports over the management link, classification, rerouting. |

## Run it

```bash
make -C telemetry all
sudo python3 eval/benchmark.py --ryu .venv/bin/ryu-manager --reps 1         # 8 runs, about 7 minutes
sudo python3 eval/benchmark.py --ryu .venv/bin/ryu-manager                  # 3 reps each, about 25 minutes
sudo python3 eval/benchmark.py --modes static --scenarios hard --reps 2    # a subset
python3 eval/summarize.py eval/results/d2-static-vs-dynamic/runs.csv
python3 tests/test_eval_metrics.py                                         # unit tests, no Mininet needed
```

Add `--datapath user` if Open vSwitch has no kernel module. `RYU_MANAGER` can replace `--ryu`.
The script runs `mn -c` before every run, which also kills any `ryu-manager` you started by hand.
Published numbers below came from `--reps 1` into `eval/results/d2-static-vs-dynamic/` (`runs.csv`; `raw/` is local only). Other runs default to `eval/results/<timestamp>/`.

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

## Measured results (9 Nov 2026)

Host: Ubuntu, kernel Open vSwitch, Mininet diamond (GUI `topo.json` not used). Command:

```bash
sudo python3 eval/benchmark.py --ryu .venv/bin/ryu-manager --reps 1 --outdir eval/results/d2-static-vs-dynamic
```

Phases: warmup 8 s, hold 10 s, settle 12 s. Live stream: 100 pkt/s, 200-byte UDP. All 8 runs completed. `n/a` is a real outcome (no fault to detect, or static mode never classifies / reroutes). CSV: [`eval/results/d2-static-vs-dynamic/runs.csv`](../eval/results/d2-static-vs-dynamic/runs.csv).

| Scenario | Static outage | Dynamic outage | Static live loss | Dynamic live loss | Detect | Reroute | Recover |
|---|---:|---:|---:|---:|---:|---:|---:|
| healthy | 0.0 ms | 0.0 ms | 0.0 % | 0.0 % | n/a | n/a | n/a |
| soft | 60.0 ms | 30.0 ms | 6.9 % | 1.7 % | 2489.4 ms | 9.5 ms | 4500.6 ms |
| blackhole | 10020.0 ms | 3490.0 ms | 33.4 % | 11.6 % | 3510.8 ms | 7.2 ms | 4514.2 ms |
| hard | 10020.0 ms | 20.0 ms | 33.4 % | 0.1 % | 34.9 ms | 13.1 ms | 4518.2 ms |

| Scenario | Static goodput in fault | Dynamic goodput in fault | Static delay in fault | Dynamic delay in fault |
|---|---:|---:|---:|---:|
| healthy | 160.1 kbit/s | 160.0 kbit/s | 0.2 ms | 0.1 ms |
| soft | 126.9 kbit/s | 151.8 kbit/s | 60.2 ms | 0.1 ms |
| blackhole | 0.2 kbit/s | 104.1 kbit/s | 0.0 ms | 0.1 ms |
| hard | 0.0 kbit/s | 159.6 kbit/s | n/a (0 live packets) | 0.1 ms |

Static stays on path A for the whole hold, so blackhole/hard outage matches the 10 s fault. Dynamic moves live traffic to s1-s3-s4: ~2.5 s after a soft fault (two bad report windows), ~3.5 s after a silent blackhole, 35 ms after a port-down. Recover ~4.5 s is the three-report hysteresis after the fault is cleared. Soft's 60.2 ms static delay is the 60 ms netem; after reroute the live delay is 0.1 ms on path B.

## Reproducibility

Use the same machine and the same options for static and dynamic. The two modes alternate within each
repetition, so slow drift in VM speed hits both equally. Keep `runs.csv`, and attach `raw/` logs for
anything surprising.
