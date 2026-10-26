#!/usr/bin/env python3
"""End-to-end check of the controller loop on the Mininet diamond (D1 demo).

Starts the Ryu app, builds the diamond with a RemoteController, and runs the
reflector on h2 and the probe client on h1. The client probes path A (udp/5001,
pinned via s2) and path B (udp/5002, pinned via s3), sends "live" traffic on
udp/5003 that follows whichever path the controller picked, and reports its
per-path stats to the controller over an out-of-band link.

Scenarios:
  1. baseline           both paths healthy, live traffic on A
  2. A blackholed       netem loss 100% on s2->s4. The link stays up, so only
                        the probe reports can see it. Expect A failed, reroute to B.
  3. A repaired         netem removed. A healthy again, live stays on B (no flapping).
  4. B link down        s3-s4 down. Port status marks B failed, reroute to A.
  5. B restored         link up, B healthy again.

    sudo RYU_MANAGER=$PWD/.venv/bin/ryu-manager python3 tests/integration/test_controller.py

Exits non-zero if a check fails. Logs, flow dumps and JSON snapshots go to --logdir.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'topology'))

from mininet.log import setLogLevel  # noqa: E402

import diamond  # noqa: E402

BIN = os.path.join(ROOT, 'telemetry')
APP = os.path.join(ROOT, 'controller', 'ndpa_controller.py')
SERVER_IP = '10.0.0.2'
PORT_A, PORT_B, PORT_LIVE = 5001, 5002, 5003
REPORT_PORT, REST_PORT = 9100, 8080
REST = 'http://127.0.0.1:%d/ndpa/' % REST_PORT
NETEM_INTF = 's2-eth2'            # s2 towards s4, requests on path A

LINE = re.compile(r'path=\d+ port=(\d+) sent=(\d+) recv=(\d+) loss=([\d.]+)% '
                  r'rtt_ms min=([\d.]+) avg=([\d.]+) max=([\d.]+) jitter=([\d.]+)')


def rest(what):
    with urllib.request.urlopen(REST + what, timeout=2) as r:
        return json.loads(r.read().decode())


def wait_for(cond, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
            if v:
                return v
        except OSError:
            pass
        time.sleep(step)
    return None


class Demo:
    def __init__(self, net, logdir):
        self.net = net
        self.logdir = logdir
        self.failures = []
        self.rows = []
        self.seen_events = 0
        self.mark_at = 0

    def log_path(self, name):
        return os.path.join(self.logdir, name)

    def save(self, name, text):
        with open(self.log_path(name), 'w') as f:
            f.write(text)

    def check(self, name, ok, detail):
        print('  %s %s: %s' % ('PASS' if ok else 'FAIL', name, detail))
        if not ok:
            self.failures.append(name)

    def events(self):
        """Print controller decisions we have not shown yet."""
        try:
            evs = rest('events')
        except OSError:
            return
        # The deque keeps the last 200; match up by position from the end.
        new = evs[self.seen_events:] if self.seen_events <= len(evs) else evs
        for e in new:
            print('    ryu %s %-8s %s' % (e['t'], e['kind'], e['msg']))
        self.seen_events = len(evs)

    def client_text(self):
        # The client starts in the background, so its log may not exist yet.
        try:
            with open(self.log_path('client.log')) as f:
                return f.read()
        except FileNotFoundError:
            return ''

    def mark(self):
        """Start of a phase: worst live loss is measured from here."""
        self.mark_at = len(self.client_text())

    def client_stats(self):
        stats = {}
        for m in LINE.finditer(self.client_text()):
            stats[int(m.group(1))] = {'loss': float(m.group(4)), 'avg': float(m.group(6))}
        return stats

    def worst_live_loss(self):
        losses = [float(m.group(4)) for m in LINE.finditer(self.client_text()[self.mark_at:])
                  if int(m.group(1)) == PORT_LIVE]
        return max(losses) if losses else None

    def paths(self):
        p = rest('paths')
        return p, {x['name']: x for x in p['paths']}

    def snapshot(self, phase):
        view, by = self.paths()
        cs = self.client_stats()
        self.save('paths-%s.json' % phase.replace(' ', '_'), json.dumps(view, indent=1))
        for s in ('s1', 's2', 's3', 's4'):
            self.save('flows-%s-%s.txt' % (phase.replace(' ', '_'), s),
                      self.net.get(s).cmd('ovs-ofctl -O OpenFlow13 dump-flows', s))
        row = {'phase': phase, 'A': by['A']['state'], 'B': by['B']['state'], 'active': view['active'],
               'stats': {port: cs.get(port) for port in (PORT_A, PORT_B, PORT_LIVE)},
               'worst': self.worst_live_loss()}
        self.rows.append(row)
        return view, by, cs

    def table(self):
        def fmt(s):
            if not s:
                return '%16s' % 'n/a'
            if s['loss'] >= 100:
                return '%5.1f%% %8s' % (s['loss'], '-')
            return '%5.1f%% %8.3f' % (s['loss'], s['avg'])
        hdr = '%-14s %-9s %-9s %-6s | %-16s | %-16s | %-16s | %s' % (
            'phase', 'A state', 'B state', 'live', 'A udp/5001', 'B udp/5002', 'live udp/5003',
            'live worst')
        sub = '%-14s %-9s %-9s %-6s | %-16s | %-16s | %-16s | %s' % (
            '', '', '', 'on', *(['  loss   avg ms'] * 3), 'in phase')
        lines = [hdr, sub, '-' * len(hdr)]
        for r in self.rows:
            worst = '%5.1f%%' % r['worst'] if r['worst'] is not None else 'n/a'
            lines.append('%-14s %-9s %-9s %-6s | %s | %s | %s | %s' % (
                r['phase'], r['A'], r['B'], r['active'],
                fmt(r['stats'][PORT_A]), fmt(r['stats'][PORT_B]), fmt(r['stats'][PORT_LIVE]), worst))
        lines.append('loss and avg are the client window (last 40 probes, 2 s) at the end of each phase;')
        lines.append('live worst is the highest windowed loss on udp/5003 seen during the phase.')
        return '\n'.join(lines)


def phase(title):
    print('\n=== %s' % title)


def run(args):
    ryu_log = open(os.path.join(args.logdir, 'ryu.log'), 'w')
    ryu = subprocess.Popen([args.ryu, '--observe-links', '--ofp-tcp-listen-port', '6653',
                            '--wsapi-port', str(REST_PORT), APP],
                           stdout=ryu_log, stderr=subprocess.STDOUT, cwd=ROOT)
    net = None
    mgmt = None
    try:
        if not wait_for(lambda: rest('topology') is not None, 20):
            print('ryu did not come up, see %s/ryu.log' % args.logdir)
            return ['ryu start']
        net = diamond.build_net(args.datapath, controller=True)
        net.start()
        diamond.prepare_hosts(net, args.datapath)
        mgmt = diamond.add_mgmt_link(net)
        h1, h2 = net.get('h1', 'h2')
        d = Demo(net, args.logdir)

        phase('1. Topology discovery (LLDP)')
        topo = wait_for(lambda: (lambda t: t if len(t['switches']) == 4 and len(t['links']) == 8 else None)(
            rest('topology')), 30)
        topo = topo or rest('topology')
        d.save('topology.json', json.dumps(topo, indent=1))
        print('  switches: %s' % ' '.join(topo['switches']))
        print('  links:    %s' % ', '.join(topo['links']))
        d.check('topology', len(topo['switches']) == 4 and len(topo['links']) == 8,
                '%d switches, %d directed links (want 4, 8)' % (len(topo['switches']), len(topo['links'])))

        phase('2. Host discovery and path table')
        h1.cmd('ping -c 2 -W 1', SERVER_IP)
        view = wait_for(lambda: (lambda p: p if len(p['paths']) == 2 else None)(rest('paths')), 10)
        view = view or rest('paths')
        d.events()
        for p in view['paths']:
            print('  path %s: %s  probe udp/%d%s' % (p['name'], '-'.join(p['route']), p['probe_port'],
                                                     '  <- live traffic' if p['active'] else ''))
        routes = sorted('-'.join(p['route']) for p in view['paths'])
        d.check('candidate paths', routes == ['s1-s2-s4', 's1-s3-s4'], ', '.join(routes) or 'none')
        if d.failures:
            print('  no path table, stopping here; see %s/ryu.log' % args.logdir)
            return d.failures
        out = h1.cmd('ping -c 3 -i 0.2 -W 1', SERVER_IP)
        d.check('ping h1 -> h2 over installed flows', ' 0% packet loss' in out,
                (re.search(r'\d+% packet loss', out) or re.search('.*', out)).group(0))

        phase('3. Baseline: probes on both paths, live traffic on udp/5003')
        h2.cmd("%s/server -p %d,%d,%d > %s 2>&1 &" % (
            BIN, PORT_A, PORT_B, PORT_LIVE, d.log_path("server.log")))
        time.sleep(0.5)
        h1.cmd('%s/client -s %s -p %d,%d,%d -i 50 -t 500 -w 40 -c %s:%d > %s 2>&1 &' % (
            BIN, SERVER_IP, PORT_A, PORT_B, PORT_LIVE, diamond.MGMT_ROOT_IP, REPORT_PORT,
            d.log_path('client.log')))
        d.mark()
        ok = wait_for(lambda: all(p['state'] == 'healthy' for p in rest('paths')['paths']), 15)
        time.sleep(3)
        d.events()
        view, by, cs = d.snapshot('baseline')
        print('  s1 flows (probe pins and live traffic):')
        flows = d.net.get('s1').cmd('ovs-ofctl -O OpenFlow13 dump-flows s1')
        for line in flows.splitlines():
            if 'priority=200' in line or 'priority=100' in line:
                print('    ' + re.sub(r'(cookie|duration|n_bytes|idle_age|table)=[^,]*, ?', '', line.strip()))
        d.check('controller gets reports', view['reports'] > 0, '%d reports, %d bad' % (
            view['reports'], view['bad_reports']))
        d.check('both paths healthy', bool(ok), 'A %s, B %s' % (by['A']['state'], by['B']['state']))
        d.check('live on A', view['active'] == 'A', 'active %s' % view['active'])
        d.check('live traffic clean', cs.get(PORT_LIVE, {}).get('loss', 100) <= 5,
                'udp/5003 loss %.1f%%' % cs.get(PORT_LIVE, {}).get('loss', 100))

        phase('4. Fault: blackhole path A (netem loss 100%% on %s, link stays UP)' % NETEM_INTF)
        d.mark()
        t0 = time.time()
        d.net.get('s2').cmd('tc qdisc replace dev %s root netem loss 100%%' % NETEM_INTF)
        moved = wait_for(lambda: rest('paths')['active'] == 'B', 20)
        t_detect = time.time() - t0
        d.events()
        time.sleep(4)            # let the client's 2 s loss window refill
        view, by, cs = d.snapshot('A blackholed')
        a = by['A']
        d.check('A classified failed by probes', a['state'] == 'failed' and not a['ports_down'],
                '%s (%s), ports down: %s' % (a['state'], a['reason'], a['ports_down'] or 'none'))
        d.check('live rerouted to B', bool(moved), 'after %.1f s' % t_detect)
        live = cs[PORT_LIVE]['loss']
        d.check('live traffic recovered', live <= 5, 'udp/5003 loss %.1f%%' % live)
        d.check('A probes still pinned to A', cs[PORT_A]['loss'] >= 90,
                'udp/5001 loss %.1f%%' % cs[PORT_A]['loss'])

        phase('5. Repair A (netem removed)')
        d.mark()
        d.net.get('s2').cmd('tc qdisc del dev %s root' % NETEM_INTF)
        ok = wait_for(lambda: rest('paths')['paths'][0]['state'] == 'healthy', 20)
        d.events()
        view, by, cs = d.snapshot('A repaired')
        d.check('A healthy again', bool(ok), by['A']['state'])
        d.check('no flap back to A', view['active'] == 'B', 'active %s' % view['active'])

        phase('6. Fault: link s3-s4 down (live traffic is on B)')
        d.mark()
        t0 = time.time()
        net.configLinkStatus('s3', 's4', 'down')
        moved = wait_for(lambda: rest('paths')['active'] == 'A', 10)
        t_detect = time.time() - t0
        d.events()
        time.sleep(4)
        view, by, cs = d.snapshot('B link down')
        d.check('B failed from port status', by['B']['state'] == 'failed' and bool(by['B']['ports_down']),
                '%s (%s)' % (by['B']['state'], by['B']['reason']))
        d.check('live rerouted to A', bool(moved), 'after %.1f s' % t_detect)
        live = cs[PORT_LIVE]['loss']
        d.check('live traffic recovered', live <= 5, 'udp/5003 loss %.1f%%' % live)

        phase('7. Restore link s3-s4')
        d.mark()
        net.configLinkStatus('s3', 's4', 'up')
        ok = wait_for(lambda: rest('paths')['paths'][1]['state'] == 'healthy', 20)
        d.events()
        view, by, cs = d.snapshot('B restored')
        d.check('B healthy again', bool(ok), by['B']['state'])

        phase('Summary (client view, last 2 s window at each phase end)')
        table = d.table()
        print(table)
        print('\nreroutes:')
        for r in view['reroutes']:
            print('  %s %s -> %s, %.0f ms after first bad sign: %s' % (
                r['t'], r['from'], r['to'], r['first_bad_to_reroute_ms'], r['reason']))
        d.save('summary.txt', table + '\n\n' + json.dumps(view['reroutes'], indent=1) + '\n')
        h1.cmd('pkill -f "%s/client"' % BIN)
        h2.cmd('pkill -f "%s/server"' % BIN)
        return d.failures
    finally:
        # Stop the controller first so teardown port events do not clutter its log.
        ryu.terminate()
        try:
            ryu.wait(5)
        except subprocess.TimeoutExpired:
            ryu.kill()
        ryu_log.close()
        if mgmt:
            diamond.remove_mgmt_link(mgmt)
        if net:
            net.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--datapath', choices=['kernel', 'user'], default='kernel')
    ap.add_argument('--logdir', default='integration-logs/controller')
    ap.add_argument('--ryu', default=os.environ.get('RYU_MANAGER')
                    or os.path.join(ROOT, '.venv', 'bin', 'ryu-manager'))
    args = ap.parse_args()
    args.logdir = os.path.abspath(args.logdir)
    os.makedirs(args.logdir, exist_ok=True)

    for exe in ('server', 'client'):
        if not os.access(os.path.join(BIN, exe), os.X_OK):
            print('missing %s/%s, run make in telemetry/ first' % (BIN, exe))
            return 1
    if not os.access(args.ryu, os.X_OK):
        print('ryu-manager not found at %s, set RYU_MANAGER' % args.ryu)
        return 1

    setLogLevel('warning')
    failures = run(args)
    if failures:
        print('\n%d check(s) failed: %s' % (len(failures), ', '.join(failures)))
        return 1
    print('\nall checks passed, logs in %s' % args.logdir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
