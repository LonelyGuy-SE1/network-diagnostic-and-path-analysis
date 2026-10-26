"""Metrics and result handling for the benchmark (issue #17).

Plain Python, no Mininet or Ryu, so it is unit tested with tests/test_eval_metrics.py.
"""
import csv
import re
import statistics

# One per-path line of the C++ client, e.g.
# path=0 port=5001 sent=61 recv=60 loss=0.0% rtt_ms min=0.071 avg=0.106 max=0.298 jitter=0.054 ...
CLIENT_LINE = re.compile(r'path=(\d+) port=(\d+) sent=(\d+) recv=(\d+) loss=([\d.]+)% '
                         r'rtt_ms min=([\d.]+) avg=([\d.]+) max=([\d.]+) jitter=([\d.]+)')

PHASES = ('before', 'fault', 'after')

# Column order of runs.csv. See docs/benchmark.md for what each one means.
FIELDS = (
    ['run', 'mode', 'scenario', 'rep', 'fault', 'hold_s', 't_fault', 't_repair',
     'detected', 'detect_ms', 'reroute_ms', 'ctrl_first_bad_to_reroute_ms', 'recover_ms',
     'final_active', 'reroutes_total',
     'live_sent', 'live_recv', 'live_lost', 'live_loss_pct', 'outage_ms', 'gap_at_repair_ms']
    + ['live_lost_%s' % p for p in PHASES]
    + ['goodput_%s_kbps' % p for p in PHASES]
    + ['owd_%s_ms' % p for p in PHASES]
    + ['%s_%s_%s' % (path, what, p) for p in PHASES for path in 'AB' for what in ('rtt', 'loss')]
    + ['error'])

# Shown by format_comparison(): column, title, unit.
HEADLINE = [
    ('outage_ms', 'longest live outage', 'ms'),
    ('live_loss_pct', 'live packets lost', '%'),
    ('detect_ms', 'fault detected after', 'ms'),
    ('reroute_ms', 'live traffic rerouted after', 'ms'),
    ('recover_ms', 'faulty path healthy again after repair', 'ms'),
    ('goodput_fault_kbps', 'live goodput during fault', 'kbit/s'),
    ('owd_fault_ms', 'live one-way delay during fault', 'ms'),
]


def latest_by_port(text):
    """Last stats line per probe port in a client log: {port: {loss, rtt_avg, sent, recv}}."""
    out = {}
    for m in CLIENT_LINE.finditer(text):
        out[int(m.group(2))] = {'sent': int(m.group(3)), 'recv': int(m.group(4)),
                                'loss': float(m.group(5)), 'rtt_avg': float(m.group(7))}
    return out


def read_live(path):
    """Receiver CSV -> [(seq, send_ts, recv_ts, bytes)]."""
    recs = []
    with open(path) as f:
        next(f, None)
        for line in f:
            if line.strip():
                seq, sent_at, got_at, n = line.strip().split(',')
                recs.append((int(seq), float(sent_at), float(got_at), int(n)))
    return recs


def _median(xs):
    return statistics.median(xs) if xs else None


def live_metrics(recs, sent, rate, t_fault, t_repair):
    """Loss, outage, goodput and delay of the numbered live stream.

    recs: receiver records, sent: packets the sender sent, rate: packets per second,
    t_fault / t_repair: wall-clock times the fault was injected and removed.
    A packet belongs to the before / fault / after window by when it was sent.
    """
    got = {}
    for seq, sent_at, got_at, n in recs:
        got.setdefault(seq, (sent_at, got_at, n))   # a duplicate does not count twice
    out = {'live_sent': sent, 'live_recv': len(got)}
    keys = (['live_loss_pct', 'live_lost', 'outage_ms', 'gap_at_repair_ms']
            + ['live_lost_%s' % p for p in PHASES]
            + ['goodput_%s_kbps' % p for p in PHASES] + ['owd_%s_ms' % p for p in PHASES])
    if not got or sent <= 0:
        out.update({k: None for k in keys})
        return out

    # Send time of packet q is t0 + q / rate; t0 is estimated from what arrived.
    t0 = _median([s - q / rate for q, (s, _, _) in got.items()])
    t_end = t0 + sent / rate
    window = {'before': (t0, t_fault), 'fault': (t_fault, t_repair), 'after': (t_repair, t_end)}

    def which(q):
        t = t0 + q / rate
        return 'before' if t < t_fault else ('fault' if t < t_repair else 'after')

    lost = {p: 0 for p in PHASES}
    nbytes = {p: 0 for p in PHASES}
    owd = {p: [] for p in PHASES}
    longest = run = 0
    for q in range(sent):
        if q in got:
            run = 0
            p = which(q)
            nbytes[p] += got[q][2]
            owd[p].append((got[q][1] - got[q][0]) * 1000.0)
        else:
            lost[which(q)] += 1
            run += 1
            longest = max(longest, run)

    n_lost = sum(lost.values())
    out['live_lost'] = n_lost
    out['live_loss_pct'] = 100.0 * n_lost / sent
    out['outage_ms'] = longest * 1000.0 / rate
    for p in PHASES:
        start, end = window[p]
        span = max(end - start, 1e-9)
        out['live_lost_%s' % p] = lost[p]
        out['goodput_%s_kbps' % p] = nbytes[p] * 8 / 1000.0 / span
        out['owd_%s_ms' % p] = _median(owd[p])

    # Gap between the last packet that arrived before the repair and the first after it:
    # about one packet interval if service was flowing, the whole outage if it was not.
    times = sorted(r for _, r, _ in got.values())
    before = [t for t in times if t <= t_repair]
    after = [t for t in times if t > t_repair]
    out['gap_at_repair_ms'] = (after[0] - before[-1]) * 1000.0 if before and after else None
    return out


def write_csv(path, rows):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore', restval='')
        w.writeheader()
        for r in rows:
            w.writerow({k: ('' if v is None else v) for k, v in r.items()})


def read_csv(path):
    """runs.csv -> list of dicts; numbers become floats, empty cells become None."""
    rows = []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            row = {}
            for k, v in r.items():
                if v == '' or v is None:
                    row[k] = None
                    continue
                try:
                    row[k] = float(v)
                except ValueError:
                    row[k] = v
            rows.append(row)
    return rows


def mean(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else None


def summarize(rows, columns=None):
    """Mean of each numeric column per (scenario, mode), plus the run count."""
    columns = columns or [c for c, _, _ in HEADLINE]
    groups = {}
    for r in rows:
        if r.get('error'):
            continue
        groups.setdefault((r['scenario'], r['mode']), []).append(r)
    out = []
    for (scenario, mode), rs in sorted(groups.items()):
        item = {'scenario': scenario, 'mode': mode, 'runs': len(rs)}
        for c in columns:
            item[c] = mean([r.get(c) for r in rs])
        out.append(item)
    return out


def fmt(v):
    return '-' if v is None else ('%.1f' % v if abs(v) < 1e5 else '%.0f' % v)


def format_comparison(rows):
    """Text table: for each scenario, static baseline next to the dynamic controller."""
    summary = summarize(rows)
    if not summary:
        return 'no successful runs to summarize'
    by = {(s['scenario'], s['mode']): s for s in summary}
    scenarios = sorted({s['scenario'] for s in summary},
                       key=lambda s: ['healthy', 'soft', 'blackhole', 'hard'].index(s)
                       if s in ('healthy', 'soft', 'blackhole', 'hard') else 9)
    lines = []
    for sc in scenarios:
        st, dy = by.get((sc, 'static')), by.get((sc, 'dynamic'))
        runs = ', '.join('%s x%d' % (m, by[(sc, m)]['runs']) for m in ('static', 'dynamic') if (sc, m) in by)
        lines.append('%s  (%s)' % (sc, runs))
        lines.append('  %-42s %12s %12s' % ('', 'static', 'dynamic'))
        for col, title, unit in HEADLINE:
            lines.append('  %-42s %12s %12s' % ('%s [%s]' % (title, unit),
                                                fmt(st[col]) if st else '-',
                                                fmt(dy[col]) if dy else '-'))
        lines.append('')
    lines.append('values are means over the repeated runs; "-" means not applicable or not measured')
    return '\n'.join(lines)
