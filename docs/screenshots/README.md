# Screenshots

Live captures from `sudo ./demo/gui_demo.sh` against a running Ryu NDPA controller.

| File | Phase |
|------|--------|
| `01-dashboard-baseline.png` | Both paths healthy, live on A |
| `02-path-a-failed-reroute.png` | A blackholed (netem 100% loss), live on B |
| `03-events-reroute.png` | B link down, PORT / CLASSIFY / REROUTE |
| `04-paths-table-metrics.png` | Hold after recovery — RTT / jitter / loss |

Embedded in the root [README](../../README.md#screenshots).
