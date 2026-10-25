"""Path analysis for the NDPA controller (issues #9, #13, #14).

No Ryu imports here, so the logic can be unit tested with plain python3.
The Ryu app in ndpa_controller.py feeds it reports and port events.
"""

HEALTHY, DEGRADED, FAILED, UNKNOWN = 'healthy', 'degraded', 'failed', 'unknown'
RANK = {HEALTHY: 0, DEGRADED: 1, FAILED: 2, UNKNOWN: 3}

# Thresholds (issue #3 still has to confirm them). Baseline RTT on the
# Mininet diamond is well under 1 ms, so 50 ms means something is wrong.
DEGRADED_LOSS_PCT = 5.0
FAILED_LOSS_PCT = 50.0
DEGRADED_RTT_MS = 50.0

# Hysteresis: a path needs this many consecutive reports agreeing on a new
# state before the state changes. Going bad is faster than coming back.
WORSE_REPORTS = 2
BETTER_REPORTS = 3

# Minimum gap between two reroutes, unless the active path has failed.
HOLD_TIME_S = 5.0


def simple_paths(adj, src, dst, max_hops=8):
    """All loop-free switch paths src -> dst. adj is {dpid: {nbr: out_port}}.
    Shortest first, ties broken by dpid order, so the order is stable."""
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


class PathState:
    """State of one candidate path, driven by probe reports and port events."""

    def __init__(self, idx, route, probe_port, ports=()):
        self.idx = idx
        self.name = chr(ord('A') + idx)
        self.route = list(route)              # dpids, e.g. [1, 2, 4]
        self.probe_port = probe_port
        self.ports = set(ports)               # (dpid, port) the path uses
        self.state = UNKNOWN
        self.reason = 'no reports yet'
        self.last = None                      # last sample dict
        self.last_ts = 0.0
        self.down = set()                     # ports on this path that are down
        self.first_bad_ts = None              # first bad report of the current episode
        self._pending = None
        self._count = 0

    def label(self):
        return '%s (%s)' % (self.name, '-'.join('s%d' % d for d in self.route))

    def _commit(self, state, reason):
        old = self.state
        self.state, self.reason = state, reason
        self._pending, self._count = None, 0
        if state == HEALTHY:
            self.first_bad_ts = None
        return old

    def update(self, loss_pct, rtt_avg_ms, now, sample=None):
        """Feed one report. Returns the old state if the state changed, else None."""
        self.last = sample if sample is not None else {'loss': loss_pct, 'rtt_avg': rtt_avg_ms}
        self.last_ts = now
        cand, reason = classify_sample(loss_pct, rtt_avg_ms)
        if cand != HEALTHY and self.first_bad_ts is None:
            self.first_bad_ts = now
        if self.down:
            # A down port wins over the probes until the port comes back.
            return None
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
            if self.state != FAILED:
                return self._commit(FAILED, 'port s%d:%d down' % key)
            self.reason = 'port s%d:%d down' % key
            return None
        self.down.discard(key)
        if not self.down:
            # Stay failed until the probes confirm the path works again.
            self.reason = 'port s%d:%d up, waiting for probes' % key
        return None


def choose_active(paths, active_idx, now, last_reroute_ts):
    """Decide whether live traffic should move. Returns (new_idx, reason) or
    (None, reason). Non-preemptive: we only move when the active path is bad,
    never just because the old path came back, which avoids flapping."""
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
