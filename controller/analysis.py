"""Path analysis for the NDPA controller. No Ryu imports (unit-testable)."""

HEALTHY, DEGRADED, FAILED, UNKNOWN = 'healthy', 'degraded', 'failed', 'unknown'
RANK = {HEALTHY: 0, DEGRADED: 1, FAILED: 2, UNKNOWN: 3}

DEGRADED_LOSS_PCT = 5.0
FAILED_LOSS_PCT = 50.0
DEGRADED_RTT_MS = 50.0  # Mininet baseline is << 1 ms; 50 ms is a real stall

# Consecutive agreeing reports before a state flip (worse is faster than recover).
WORSE_REPORTS = 2
BETTER_REPORTS = 3
HOLD_TIME_S = 5.0  # min gap between reroutes unless the active path has failed


def simple_paths(adj, src, dst, max_hops=8):
    """Loop-free switch paths src -> dst. adj is {dpid: {nbr: out_port}}."""
    out = []

    def walk(node, seen):
        if node == dst:
            out.append(list(seen))
            return
        if len(seen) > max_hops:
            return
        for nbr in sorted(adj.get(node, {})):
            if nbr not in seen:
                seen.append(nbr)
                walk(nbr, seen)
                seen.pop()

    walk(src, [src])
    return sorted(out, key=lambda p: (len(p), p))


def classify_sample(loss_pct, rtt_avg_ms):
    """One report -> (state, reason)."""
    if loss_pct >= FAILED_LOSS_PCT:
        return FAILED, 'loss %.1f%% >= %.0f%%' % (loss_pct, FAILED_LOSS_PCT)
    if loss_pct >= DEGRADED_LOSS_PCT:
        return DEGRADED, 'loss %.1f%% >= %.0f%%' % (loss_pct, DEGRADED_LOSS_PCT)
    if rtt_avg_ms >= DEGRADED_RTT_MS:
        return DEGRADED, 'rtt %.1f ms >= %.0f ms' % (rtt_avg_ms, DEGRADED_RTT_MS)
    return HEALTHY, 'loss %.1f%%, rtt %.3f ms' % (loss_pct, rtt_avg_ms)


def route_name(route):
    """Stable path id from switch dpids."""
    return '-'.join('s%d' % d for d in route)


class PathState:
    """State of one candidate path, driven by probe reports and port events."""

    def __init__(self, idx, route, probe_port, ports=()):
        self.idx = idx
        self.route = list(route)
        self.name = route_name(self.route)
        self.probe_port = probe_port
        # Inter-switch ports only — host attachment ports are shared by every
        # path and must not flip the whole table to failed.
        self.ports = set(ports)
        self.state = UNKNOWN
        self.reason = 'no reports yet'
        self.last = None
        self.last_ts = 0.0
        self.down = set()
        self.first_bad_ts = None
        self._pending = None
        self._count = 0

    def _commit(self, state, reason):
        old = self.state
        self.state, self.reason = state, reason
        self._pending, self._count = None, 0
        if state == HEALTHY:
            self.first_bad_ts = None
        return old

    def update(self, loss_pct, rtt_avg_ms, now, sample=None):
        """Feed one report. Returns the old state if the state changed, else None."""
        if self.down:
            return None
        if sample is not None and float(sample.get('loss', loss_pct) or 0) >= 100.0:
            sample = dict(sample)
            sample['rtt_min'] = sample['rtt_avg'] = sample['rtt_max'] = None
            sample['jitter'] = None
        self.last = sample if sample is not None else {'loss': loss_pct, 'rtt_avg': rtt_avg_ms}
        self.last_ts = now
        cand, reason = classify_sample(loss_pct, rtt_avg_ms)
        if cand != HEALTHY and self.first_bad_ts is None:
            self.first_bad_ts = now
        if cand == self.state:
            self.reason = reason
            self._pending, self._count = None, 0
            if cand == HEALTHY:
                self.first_bad_ts = None
            return None
        if self.state == UNKNOWN:
            return self._commit(cand, reason)
        need = WORSE_REPORTS if RANK[cand] > RANK[self.state] else BETTER_REPORTS
        if self._pending == cand:
            self._count += 1
        else:
            self._pending, self._count = cand, 1
        if self._count >= need:
            return self._commit(cand, reason)
        return None

    def set_port(self, key, down, now):
        """Port status for a (dpid, port) on this path. Returns old state on change."""
        if key not in self.ports:
            return None
        if down:
            self.down.add(key)
            if self.first_bad_ts is None:
                self.first_bad_ts = now
            self.last = {
                'loss': 100.0, 'rtt_min': None, 'rtt_avg': None, 'rtt_max': None,
                'jitter': None, 'port': self.probe_port,
            }
            self.last_ts = now
            if self.state != FAILED:
                return self._commit(FAILED, 'port s%d:%d down' % key)
            self.reason = 'port s%d:%d down' % key
            return None
        self.down.discard(key)
        if not self.down:
            self.reason = 'port s%d:%d up, waiting for probes' % key
        return None


def choose_active(paths, active_idx, now, last_reroute_ts):
    """(new_idx, reason) or (None, reason). Move only when the active path is
    bad, never because a recovered path looks better (avoids flapping)."""
    active = paths[active_idx]
    if active.state not in (DEGRADED, FAILED):
        return None, 'active path %s' % active.state
    others = [p for p in paths if p.idx != active_idx]
    if not others:
        return None, 'no alternative path'

    def key(p):
        last = p.last or {}
        return (RANK[p.state], last.get('rtt_avg', 0.0), last.get('loss', 0.0), p.idx)

    best = sorted(others, key=key)[0]
    if RANK[best.state] >= RANK[active.state]:
        return None, 'no better alternative (best %s is %s)' % (best.name, best.state)
    if active.state != FAILED and now - last_reroute_ts < HOLD_TIME_S:
        return None, 'hold time'
    return best.idx, '%s is %s (%s), %s is %s' % (active.name, active.state, active.reason,
                                                  best.name, best.state)
