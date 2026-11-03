# SDN-Based Network Diagnostic and Path Analysis Tool

[![CI](https://github.com/LonelyGuy-SE1/network-diagnostic-and-path-analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/LonelyGuy-SE1/network-diagnostic-and-path-analysis/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%20(Ryu)%20%7C%203.12%20(CI)-blue.svg)](https://www.python.org/)
[![C++](https://img.shields.io/badge/C++-POSIX%20sockets-00599C.svg)](telemetry/)
[![SDN](https://img.shields.io/badge/SDN-Ryu%20%2B%20OpenFlow%201.3-orange.svg)](controller/)
[![Emulator](https://img.shields.io/badge/Emulator-Mininet%20%2B%20OVS-green.svg)](topology/)

A socket-based network diagnostic application that measures **delay (RTT)**, **packet loss**, and **path characteristics** on every candidate path between hosts, correlates those measurements with a live **SDN topology**, identifies **failed / degraded** paths, and compares alternatives so live traffic can be steered onto the best path.

> Absolute RTT accuracy inside Mininet is limited by scheduler jitter. Results are interpreted relative to a healthy baseline, not as hardware-grade numbers.

---

## Objectives

| Goal | How this project meets it |
|------|---------------------------|
| Implement UDP diagnostic probes | C++ client/reflector (`telemetry/`) with raw POSIX sockets, sequence numbers, timeouts |
| Measure round-trip time | Per-path RTT min / avg / max via `CLOCK_MONOTONIC` |
| Detect packet loss | Sliding window over probe sequences → loss % |
| Implement path analysis | `controller/analysis.py`: healthy / degraded / failed + hysteresis |
| Discover SDN topology | Ryu LLDP + host learning (`ndpa_controller.py`) |
| Correlate app measurements with paths | Reports joined to enumerated switch routes; REST `/ndpa/paths` |
| Identify failed paths | Probe loss thresholds + OpenFlow port-down events |
| Compare alternative paths | Rank candidates and reroute live traffic (udp/5003) |

---

## Features

1. **Topology discovery**: switches, links, and host attachment points.
2. **Per-path UDP probing**: each candidate path pinned by destination port (`5001`, `5002`, …).
3. **Telemetry**: RTT (min/avg/max), jitter, and loss over an out-of-band TCP channel.
4. **Classification**: healthy / degraded / failed with hold-time to avoid flapping.
5. **Reroute**: live traffic moved to the best healthy alternative; decisions logged with timestamps.
6. **Live dashboard**: `frontend/` at http://127.0.0.1:8080/ndpa/ (served by Ryu).

---

## Architecture

```mermaid
graph TD
    subgraph DP["Data plane (Mininet hosts)"]
        C["C++ client (h1)"] -- "UDP probes, one dst port per path" --> S["C++ reflector (h2)"]
        S -- "Echo" --> C
    end

    subgraph CP["Control plane"]
        R["Ryu NDPA controller<br/>topology · path table · analysis · REST"]
        UI["Dashboard frontend/<br/>/ndpa/"]
    end

    subgraph NS["Substrate (OVS)"]
        SW1((s1)) --- SW2((s2))
        SW1 --- SW3((s3))
        SW2 --- SW4((s4))
        SW3 --- SW4
    end

    C --- SW1
    S --- SW4
    C -- "JSON reports TCP :9100 (mgmt veth)" --> R
    R -- "OpenFlow FlowMods" --> NS
    NS -- "Port status / stats" --> R
    UI -- "GET /ndpa/paths · events · topology" --> R
```

### Default diamond paths

| Route | Probe port | Role |
|-------|------------|------|
| s1 → s2 → s4 | udp/5001 | Candidate path |
| s1 → s3 → s4 | udp/5002 | Candidate path |
| follows active route | udp/5003 | Live traffic |

### Design decisions

* **Path pinning by UDP port**: forward match `udp_dst`, echo match `udp_src`, so all paths can be measured at once.
* **Soft vs hard faults**: `tc netem` (probes see loss; link stays up) vs link removal / port-down (OpenFlow status).
* **Client reports, controller decides**: analysis stays next to the topology.
* **Control off the data path**: OpenFlow on loopback; telemetry on a dedicated mgmt veth (`diamond.py --mgmt`).
* **No ARP flooding**: static ARP + proactive flows (the topology has a loop).

### Tech stack

| Layer | Pieces |
|-------|--------|
| Probes | C++ `telemetry/{client,server,stats,report}.cpp`, `protocol.h` |
| Controller | Python / Ryu 4.34 (`controller/ndpa_controller.py`, `analysis.py`) |
| Emulation | Mininet + Open vSwitch (`topology/diamond.py`) |
| UI | Static `frontend/` served by Ryu WSGI |
| CI | GitHub Actions (lint, C++ build/tests, Mininet integration) |

---

## Repository layout

```
protocol.h          shared 24-byte probe packet
telemetry/          C++ client, reflector, stats, Makefile
controller/         Ryu app + path classifier
frontend/           live dashboard (HTML/CSS/JS)
topology/           Mininet diamond topology
demo/               runnable demos and helpers
tests/              unit + integration
eval/               benchmark hooks
docs/screenshots/   dashboard captures
.github/workflows/  CI
```

---

## How to run

Tested on Ubuntu. Ryu 4.34 needs **Python 3.9** in `.venv`.

### 1. System packages

```bash
sudo apt install mininet openvswitch-switch
```

### 2. Ryu venv

```bash
uv venv --python 3.9 .venv
uv pip install "setuptools<66" wheel pbr eventlet==0.30.2
uv pip install --no-build-isolation ryu==4.34
```

### 3. Build + unit tests

```bash
make -C telemetry all test
python3 tests/test_analysis.py
python3 tests/test_eval_metrics.py
```

### 4. Launch the live stack

```bash
./demo/gui.sh
```

This starts the controller, brings up the Mininet diamond, runs probes, and opens the fault timeline. Dashboard: http://127.0.0.1:8080/ndpa/

```bash
./demo/gui.sh --serve    # controller + agent only; drive faults from the UI
./demo/gui.sh --hold 60  # longer watch window after the demo timeline
```

Headless end-to-end check (no dashboard):

```bash
sudo ./demo/d1_demo.sh
```

Set `RYU_MANAGER` if `ryu-manager` is not on `.venv/bin`. Pass `--datapath user` when the OVS kernel module is unavailable.

---

## Evaluation scenarios

| # | Scenario | Trigger |
|---|----------|---------|
| 1 | Healthy baseline | No faults |
| 2 | Soft failure | Blackhole / `tc netem` loss on a path (link stays up) |
| 3 | Hard failure | Link removed or port down mid-transfer |
| 4 | Recovery | Fault cleared |

**Metrics:** RTT and loss per path (before/after reroute), detection time, recovery time.

---

## Screenshots

From a live GUI run against Ryu at `/ndpa/` (real C++ probes + Mininet diamond).

| # | Capture | What it shows |
|---|---------|----------------|
| 1 | Baseline | Both paths healthy; live on s1→s2→s4 |
| 2 | Soft blackhole | Path s1→s2→s4 failed (100% loss); live moved to s1→s3→s4 |
| 3 | Hard failure | Alternate path failed on port-down; PORT / CLASSIFY / REROUTE events |
| 4 | Metrics | RTT min/avg/max, jitter, loss after recovery |

### Baseline

![Baseline: both paths healthy](docs/screenshots/01-dashboard-baseline.png)

### Soft blackhole and reroute

![Path failed, live traffic moved](docs/screenshots/02-path-a-failed-reroute.png)

### Hard failure and event log

![Port-down failure with REROUTE events](docs/screenshots/03-events-reroute.png)

### Paths table metrics

![Paths table with RTT / jitter / loss](docs/screenshots/04-paths-table-metrics.png)

---

## Video demo

```text
Video: <add link after recording>
```

Suggested walkthrough: start `./demo/gui.sh`, show healthy paths, blackhole one path, show classification and reroute, clear the fault, show recovery.

---

## Team contributions

| Member | Focus |
|--------|--------|
| [LonelyGuy-SE1](https://github.com/LonelyGuy-SE1) | Probe engine, Ryu controller and analysis, Mininet topology, demos, dashboard, CI |
| [divyansi](https://github.com/divyansi) | Early repository and project setup |

---

## CI

[![CI](https://github.com/LonelyGuy-SE1/network-diagnostic-and-path-analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/LonelyGuy-SE1/network-diagnostic-and-path-analysis/actions/workflows/ci.yml)

Workflow: [`.github/workflows/ci.yml`](.github/workflows/ci.yml)

| Job | What it runs |
|-----|----------------|
| **Lint** | `py_compile` + `flake8` |
| **Build and unit tests** | C++ build/tests, analysis and eval unit tests |
| **Mininet integration** | Path-pinning integration test |
| **Controller demo** | Full `d1_demo` on `main` pushes (also runnable via Actions → Run workflow) |
