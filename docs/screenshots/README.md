# Screenshots

Live captures from `./demo/gui.sh` against a running Ryu NDPA controller (default diamond, probe h1 → h2).

| File | Phase |
|------|--------|
| `01-dashboard-baseline.png` | Both paths healthy, live on s1→s2→s4 |
| `02-blackhole-reroute.png` | s1→s2→s4 blackholed (netem 100% loss), live on s1→s3→s4 |
| `03-events-reroute.png` | Alternate path link down, PORT / CLASSIFY / REROUTE |
| `04-paths-table-metrics.png` | After recovery — RTT / jitter / loss |

Embedded in the root [README](../../README.md#screenshots).
