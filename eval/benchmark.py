#!/usr/bin/env python3
"""Benchmark harness: static baseline vs the dynamic controller (issue #17).

For every scenario the same timeline runs in two modes on the Mininet diamond:

  static    no controller. Fixed flows send probes and live traffic down path A and
            nothing ever moves them (the "static shortest path" baseline).
  dynamic   the full system: Ryu app, probe reports over the management link,
            classification and rerouting.

Timeline per run:  warm-up -> inject fault on path A -> hold -> remove fault -> settle.
A paced UDP stream (udp/5004, eval/livestream.py) stands in for live traffic and is
what the loss, outage, goodput and delay numbers are computed from, so both modes
are measured the same way. The C++ client probes both paths in both modes.

Scenarios (see eval/faults.py):  healthy, soft, blackhole, hard.
Recovery is measured in the settle phase of every fault scenario (recover_ms,
gap_at_repair_ms, final_active).

    sudo python3 eval/benchmark.py --ryu ~/ryuenv/bin/ryu-manager
    sudo python3 eval/benchmark.py --ryu ~/ryuenv/bin/ryu-manager --quick
    sudo python3 eval/benchmark.py --modes static --scenarios hard --reps 2

Results: eval/results/<timestamp>/runs.csv (one row per run) and raw/ (logs per run).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'topology'))
sys.path.insert(0, HERE)

from mininet.log import setLogLevel  # noqa: E402

import diamond  # noqa: E402
import faults  # noqa: E402
import metrics  # noqa: E402

BIN = os.path.join(ROOT, 'telemetry')
APP = os.path.join(ROOT, 'controller', 'ndpa_controller.py')
LIVESTREAM = os.path.join(HERE, 'livestream.py')
SERVER_IP = '10.0.0.2'
PROBE_PORTS = '5001,5002'
PORT_A, PORT_B = 5001, 5002
LIVE_PORT = 5004                 # not a probe port, so the controller treats it as live traffic
REPORT_PORT, REST_PORT = 9100, 8080
REST = 'http://127.0.0.1:%d/ndpa/' % REST_PORT
SCENARIOS = {'healthy': 'none', 'soft': 'soft', 'blackhole': 'blackhole', 'hard': 'hard'}


def mn_cleanup():
    """sudo mn -c: remove leftovers of an earlier run. Harmless if mn is not on the PATH."""
    try:
        subprocess.run(['mn', '-c'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


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
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(step)
    return None


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ''


def poll_paths():
    """(state of path A, active path, full view) or None if the controller does not answer."""
    try:
        v = rest('paths')
        return {p['name']: p['state'] for p in v['paths']}.get('A'), v['active'], v
    except (OSError, ValueError, KeyError):
        return None


def start_run(mode, a, rundir):
    """Bring up controller (dynamic), network and hosts. Returns a dict of live handles."""
    h = {'ryu': None, 'ryu_log': None, 'net': None, 'mgmt': None, 'procs': []}
    dynamic = mode == 'dynamic'
    mn_cleanup()
    try:
        if dynamic:
            h['ryu_log'] = open(os.path.join(rundir, 'ryu.log'), 'w')
            h['ryu'] = subprocess.Popen(
                [a.ryu, '--observe-links', '--ofp-tcp-listen-port', '6653',
                 '--wsapi-port', str(REST_PORT), APP],
                stdout=h['ryu_log'], stderr=subprocess.STDOUT, cwd=ROOT)
            if not wait_for(lambda: rest('topology'), 20):
                raise RuntimeError('ryu did not come up, see ryu.log')
        net = h['net'] = diamond.build_net(a.datapath, controller=dynamic)
        net.start()
        diamond.prepare_hosts(net, a.datapath)
        h1 = net.get('h1')
        if dynamic:
            h['mgmt'] = diamond.add_mgmt_link(net)
            if not wait_for(lambda: (lambda t: len(t['switches']) == 4 and len(t['links']) == 8)(
                    rest('topology')), 30):
                raise RuntimeError('topology discovery did not finish')
            h1.cmd('ping -c 2 -W 1', SERVER_IP)               # lets the controller learn the hosts
            if not wait_for(lambda: len(rest('paths')['paths']) == 2, 15):
                raise RuntimeError('controller built no path table')
        else:
            diamond.install_static_flows(net)
        out = h1.cmd('ping -c 3 -i 0.2 -W 1', SERVER_IP)
        if ' 0% packet loss' not in out:
            raise RuntimeError('h1 cannot reach h2 before the test: %s' % out.strip().splitlines()[-2:])
        return h
    except Exception:
        stop_run(h)
        raise


def stop_run(h):
    for p in h['procs']:
        try:
            p.terminate()
        except OSError:
            pass
    for p in h['procs']:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    if h['ryu']:
        h['ryu'].terminate()            # first, so teardown port events stay out of its log
        try:
            h['ryu'].wait(5)
        except subprocess.TimeoutExpired:
            h['ryu'].kill()
    if h['ryu_log']:
        h['ryu_log'].close()
    if h['mgmt']:
        diamond.remove_mgmt_link(h['mgmt'])
    if h['net']:
        h['net'].stop()


def spawn(h, node, argv, logfile):
    p = node.popen(argv, stdout=open(logfile, 'w'), stderr=subprocess.STDOUT)
    h['procs'].append(p)
    return p


def run_once(mode, scenario, rep, a, rundir):
    os.makedirs(rundir, exist_ok=True)
    fault = SCENARIOS[scenario]
    dynamic = mode == 'dynamic'
    row = {'run': os.path.basename(rundir), 'mode': mode, 'scenario': scenario, 'rep': rep,
           'fault': fault, 'hold_s': a.hold}
    h = start_run(mode, a, rundir)
    try:
        net = h['net']
        h1, h2 = net.get('h1', 'h2')
        client_log = os.path.join(rundir, 'client.log')
        recv_csv = os.path.join(rundir, 'live-recv.csv')

        spawn(h, h2, [os.path.join(BIN, 'server'), '-p', PROBE_PORTS], os.path.join(rundir, 'server.log'))
        time.sleep(0.5)
        client = [os.path.join(BIN, 'client'), '-s', SERVER_IP, '-p', PROBE_PORTS,
                  '-i', '50', '-t', '500', '-w', '40']
        if dynamic:
            client += ['-c', '%s:%d' % (diamond.MGMT_ROOT_IP, REPORT_PORT)]
        spawn(h, h1, client, client_log)
        if dynamic:
            if not wait_for(lambda: all(p['state'] == 'healthy' for p in rest('paths')['paths']), 20):
                raise RuntimeError('paths never became healthy before the fault')
        else:
            time.sleep(3)

        total = a.warmup + a.hold + a.settle
        recv = h2.popen([sys.executable, LIVESTREAM, 'recv', '--port', str(LIVE_PORT),
                         '--duration', str(total + 4), '--out', recv_csv],
                        stdout=open(os.path.join(rundir, 'live-recv.log'), 'w'), stderr=subprocess.STDOUT)
        h['procs'].append(recv)
        time.sleep(0.5)
        send_log = os.path.join(rundir, 'live-send.log')
        send = h1.popen([sys.executable, LIVESTREAM, 'send', '--dst', SERVER_IP, '--port', str(LIVE_PORT),
                         '--rate', str(a.rate), '--size', str(a.size), '--duration', str(total)],
                        stdout=open(send_log, 'w'), stderr=subprocess.STDOUT)
        h['procs'].append(send)

        samples = {}
        time.sleep(a.warmup)
        samples['before'] = metrics.latest_by_port(read(client_log))

        # ---- fault ----------------------------------------------------------
        t_fault = time.time()
        faults.inject(net, fault)
        detect_t = reroute_t = None
        end_hold = t_fault + a.hold
        while time.time() < end_hold:
            if dynamic and fault != 'none':
                got = poll_paths()
                if got:
                    now = time.time()
                    if detect_t is None and got[0] != 'healthy':
                        detect_t = now
                    if reroute_t is None and got[1] != 'A':
                        reroute_t = now
            time.sleep(0.02)
        samples['fault'] = metrics.latest_by_port(read(client_log))

        # ---- repair ---------------------------------------------------------
        t_repair = time.time()
        faults.clear(net, fault)
        recover_t = None
        end_settle = t_repair + a.settle
        while time.time() < end_settle:
            if dynamic and fault != 'none' and detect_t is not None and recover_t is None:
                got = poll_paths()
                if got and got[0] == 'healthy':
                    recover_t = time.time()
            time.sleep(0.05)
        samples['after'] = metrics.latest_by_port(read(client_log))

        # ---- results --------------------------------------------------------
        row['t_fault'], row['t_repair'] = round(t_fault, 3), round(t_repair, 3)
        if dynamic:
            final = poll_paths()
            if final:
                view = final[2]
                with open(os.path.join(rundir, 'paths-final.json'), 'w') as f:
                    json.dump(view, f, indent=1)
                row['final_active'] = view['active']
                row['reroutes_total'] = len(view['reroutes'])
                if view['reroutes']:
                    row['ctrl_first_bad_to_reroute_ms'] = view['reroutes'][0]['first_bad_to_reroute_ms']
            if fault != 'none':
                row['detected'] = int(detect_t is not None)
                if detect_t is not None:
                    row['detect_ms'] = round((detect_t - t_fault) * 1000, 1)
                if reroute_t is not None:
                    row['reroute_ms'] = round((reroute_t - t_fault) * 1000, 1)
                if recover_t is not None:
                    row['recover_ms'] = round((recover_t - t_repair) * 1000, 1)

        send.wait(timeout=15)
        recv.wait(timeout=total + 15)
        sent = re.search(r'sent=(\d+)', read(send_log))
        if not sent:
            raise RuntimeError('live sender printed no packet count, see live-send.log')
        row.update(metrics.live_metrics(metrics.read_live(recv_csv), int(sent.group(1)),
                                        a.rate, t_fault, t_repair))
        for phase in metrics.PHASES:
            for name, port in (('A', PORT_A), ('B', PORT_B)):
                s = samples[phase].get(port)
                row['%s_rtt_%s' % (name, phase)] = s['rtt_avg'] if s else None
                row['%s_loss_%s' % (name, phase)] = s['loss'] if s else None
        return row
    finally:
        stop_run(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ryu', default=os.environ.get('RYU_MANAGER')
                    or os.path.join(ROOT, '.venv', 'bin', 'ryu-manager'), help='path to ryu-manager')
    ap.add_argument('--modes', default='static,dynamic')
    ap.add_argument('--scenarios', default=','.join(SCENARIOS))
    ap.add_argument('--reps', type=int, default=3, help='runs per scenario and mode')
    ap.add_argument('--warmup', type=float, default=8, help='seconds of steady state before the fault')
    ap.add_argument('--hold', type=float, default=10, help='seconds the fault stays')
    ap.add_argument('--settle', type=float, default=12, help='seconds after the repair')
    ap.add_argument('--rate', type=float, default=100, help='live stream packets per second')
    ap.add_argument('--size', type=int, default=200, help='live stream UDP payload bytes')
    ap.add_argument('--datapath', choices=['kernel', 'user'], default='kernel')
    ap.add_argument('--outdir', default=None, help='default eval/results/<timestamp>')
    ap.add_argument('--quick', action='store_true', help='1 rep, shorter phases (smoke test)')
    a = ap.parse_args()
    if a.quick:
        a.reps, a.warmup, a.hold, a.settle = 1, 5, 8, 8
    modes = [m for m in a.modes.split(',') if m]
    scenarios = [s for s in a.scenarios.split(',') if s]
    bad = [m for m in modes if m not in ('static', 'dynamic')] + [s for s in scenarios if s not in SCENARIOS]
    if bad:
        ap.error('unknown mode or scenario: %s' % ', '.join(bad))
    if os.geteuid() != 0:
        sys.exit('needs root for Mininet: sudo python3 eval/benchmark.py ...')
    for exe in ('server', 'client'):
        if not os.access(os.path.join(BIN, exe), os.X_OK):
            sys.exit('missing telemetry/%s, run: make -C telemetry all' % exe)
    if 'dynamic' in modes and not os.access(a.ryu, os.X_OK):
        sys.exit('ryu-manager not found at %s, use --ryu or set RYU_MANAGER' % a.ryu)

    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    outdir = os.path.abspath(a.outdir or os.path.join(HERE, 'results', stamp))
    os.makedirs(os.path.join(outdir, 'raw'), exist_ok=True)
    runs_csv = os.path.join(outdir, 'runs.csv')
    total_runs = a.reps * len(scenarios) * len(modes)
    per_run = a.warmup + a.hold + a.settle + 25
    print('%d runs, about %.0f minutes. Results: %s' % (total_runs, total_runs * per_run / 60, outdir))

    setLogLevel('warning')
    rows = []
    n = 0
    try:
        for rep in range(1, a.reps + 1):
            for scenario in scenarios:
                for mode in modes:          # modes alternate so slow drift hits both equally
                    n += 1
                    name = '%s-%s-%d' % (mode, scenario, rep)
                    print('[%d/%d] %s ...' % (n, total_runs, name), flush=True)
                    try:
                        row = run_once(mode, scenario, rep, a, os.path.join(outdir, 'raw', name))
                        print('        outage %s ms, live loss %s %%' % (
                            metrics.fmt(row.get('outage_ms')), metrics.fmt(row.get('live_loss_pct'))))
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:      # keep going: one bad run should not lose the rest
                        print('        FAILED: %s' % e)
                        row = {'run': name, 'mode': mode, 'scenario': scenario, 'rep': rep,
                               'error': str(e)[:200]}
                    rows.append(row)
                    metrics.write_csv(runs_csv, rows)
    except KeyboardInterrupt:
        print('\ninterrupted, keeping the %d finished run(s)' % len(rows))
    mn_cleanup()

    print('\n' + metrics.format_comparison(rows))
    failed = [r['run'] for r in rows if r.get('error')]
    if failed:
        print('failed runs (see raw/<run>/ logs): %s' % ', '.join(failed))
    print('\nCSV: %s' % runs_csv)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
