#!/usr/bin/env python3
"""End-to-end check of the telemetry engine on the Mininet diamond.

Runs the reflector on h2 and the probe client on h1 with static per-port
flows (5001 via s2, 5002 via s3), then impairs the s2 path and checks the
client's per-path stats react while the s3 path stays clean.

    sudo python3 tests/integration/test_paths.py [--datapath user] [--logdir DIR]

Exits non-zero on the first failed check. Logs go to --logdir.
"""
import argparse
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'topology'))

from mininet.log import setLogLevel  # noqa: E402

import diamond  # noqa: E402

BIN = os.path.join(ROOT, 'telemetry')
SERVER_IP = '10.0.0.2'
IMPAIRED, CLEAN = 5001, 5002      # 5001 rides s2, 5002 rides s3
IMPAIRED_LINK = ('s2', 's4')
NETEM_INTF = 's2-eth2'            # s2 towards s4, so requests on the 5001 path

LINE = re.compile(r'path=\d+ port=(\d+) sent=(\d+) recv=(\d+) loss=([\d.]+)% '
                  r'rtt_ms min=([\d.]+) avg=([\d.]+) max=([\d.]+) jitter=([\d.]+)')


def parse(output):
    """Last stats line per port -> dict."""
    stats = {}
    for m in LINE.finditer(output):
        stats[int(m.group(1))] = {
            'sent': int(m.group(2)), 'recv': int(m.group(3)),
            'loss': float(m.group(4)), 'avg': float(m.group(6)),
            'max': float(m.group(7)), 'jitter': float(m.group(8)),
        }
    return stats


class Run:
    def __init__(self, net, logdir):
        self.net = net
        self.h1 = net.get('h1')
        self.logdir = logdir
        self.failures = []

    def probe(self, name, seconds=6):
        """One fresh client run, so each phase starts with an empty window."""
        out = self.h1.cmd('timeout %d %s/client -s %s -p %d,%d -i 50 -t 500 -w 100 -d %d 2>&1'
                          % (seconds + 10, BIN, SERVER_IP, IMPAIRED, CLEAN, seconds))
        with open(os.path.join(self.logdir, 'client-%s.log' % name), 'w') as f:
            f.write(out)
        stats = parse(out)
        for port in (IMPAIRED, CLEAN):
            if port not in stats:
                print('[%s] no stats for port %d, client said:\n%s' % (name, port, out))
                sys.exit(1)
            s = stats[port]
            print('[%-9s] port %d: sent=%d recv=%d loss=%.1f%% avg=%.3f ms max=%.3f ms jitter=%.3f ms'
                  % (name, port, s['sent'], s['recv'], s['loss'], s['avg'], s['max'], s['jitter']))
        return stats[IMPAIRED], stats[CLEAN]

    def check(self, name, ok, detail):
        print('  %s %s: %s' % ('PASS' if ok else 'FAIL', name, detail))
        if not ok:
            self.failures.append(name)

    def netem(self, args):
        sw = self.net.get(IMPAIRED_LINK[0])
        out = sw.cmd('tc qdisc replace dev %s root netem %s 2>&1' % (NETEM_INTF, args))
        if out.strip():
            print('tc failed: ' + out)
            sys.exit(1)

    def clear_netem(self):
        self.net.get(IMPAIRED_LINK[0]).cmd('tc qdisc del dev %s root 2>/dev/null' % NETEM_INTF)


def run(args):
    net = diamond.build_net(args.datapath, controller=False)
    net.start()
    try:
        diamond.prepare_hosts(net, args.datapath)
        diamond.install_static_flows(net)
        h2 = net.get('h2')
        h2.cmd('%s/server -p %d,%d > %s/server.log 2>&1 &'
               % (BIN, IMPAIRED, CLEAN, args.logdir))
        time.sleep(1)
        t = Run(net, args.logdir)

        bad, good = t.probe('baseline')
        t.check('baseline loss', bad['loss'] <= 5 and good['loss'] <= 5,
                '%.1f%% / %.1f%% (<= 5%%)' % (bad['loss'], good['loss']))
        t.check('baseline rtt', bad['avg'] < 10 and good['avg'] < 10,
                '%.3f / %.3f ms (< 10 ms)' % (bad['avg'], good['avg']))

        t.netem('delay 50ms')
        bad, good = t.probe('delay')
        t.check('delayed path rtt', bad['avg'] >= 40, '%.3f ms (>= 40 ms)' % bad['avg'])
        t.check('clean path rtt', good['avg'] < 10, '%.3f ms (< 10 ms)' % good['avg'])

        t.netem('loss 20%')
        bad, good = t.probe('loss', seconds=8)
        t.check('lossy path loss', 8 <= bad['loss'] <= 40, '%.1f%% (8..40%%)' % bad['loss'])
        t.check('clean path loss', good['loss'] <= 5, '%.1f%% (<= 5%%)' % good['loss'])
        t.clear_netem()

        net.configLinkStatus(*IMPAIRED_LINK, 'down')
        bad, good = t.probe('linkdown')
        t.check('down path loss', bad['loss'] >= 90, '%.1f%% (>= 90%%)' % bad['loss'])
        t.check('clean path loss', good['loss'] <= 5, '%.1f%% (<= 5%%)' % good['loss'])

        net.configLinkStatus(*IMPAIRED_LINK, 'up')
        time.sleep(1)
        bad, good = t.probe('linkup')
        t.check('recovered path loss', bad['loss'] <= 5, '%.1f%% (<= 5%%)' % bad['loss'])
        t.check('recovered path rtt', bad['avg'] < 10, '%.3f ms (< 10 ms)' % bad['avg'])

        for sw in ('s1', 's4'):
            with open(os.path.join(args.logdir, 'flows-%s.txt' % sw), 'w') as f:
                f.write(net.get(sw).cmd('ovs-ofctl -O OpenFlow13 dump-flows', sw))
        return t.failures
    finally:
        net.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--datapath', choices=['kernel', 'user'], default='kernel')
    ap.add_argument('--logdir', default='integration-logs')
    args = ap.parse_args()
    args.logdir = os.path.abspath(args.logdir)
    os.makedirs(args.logdir, exist_ok=True)

    for exe in ('server', 'client'):
        if not os.access(os.path.join(BIN, exe), os.X_OK):
            print('missing %s/%s, run make in telemetry/ first' % (BIN, exe))
            return 1

    setLogLevel('warning')
    failures = run(args)
    if failures:
        print('\n%d check(s) failed: %s' % (len(failures), ', '.join(failures)))
        return 1
    print('\nall checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
