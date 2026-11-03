#!/usr/bin/env python3
"""GUI live demo — uses an ALREADY-RUNNING Ryu (the one serving /ndpa/).

Does not touch demo/d1_demo.sh. Brings up Mininet diamond + mgmt, starts the
real C++ server/client, injects the README faults, and leaves the net up so
the dashboard can show real path states.

Preferred entry point (starts Ryu + root agent + this demo):

  ./demo/gui.sh

Or manually:

  .venv/bin/ryu-manager --observe-links controller/ndpa_controller.py
  sudo ./demo/gui_demo.sh
  # browser: http://127.0.0.1:8080/ndpa/
"""
import argparse
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'topology'))

from mininet.log import setLogLevel  # noqa: E402
import diamond  # noqa: E402

BIN = os.path.join(ROOT, 'telemetry')
SERVER_IP = '10.0.0.2'
PORT_A, PORT_B, PORT_LIVE = 5001, 5002, 5003
REPORT_PORT, REST_PORT = 9100, 8080
REST = 'http://127.0.0.1:%d/ndpa/' % REST_PORT
NETEM_INTF = 's2-eth2'
STATUS_PATH = os.environ.get('NDPA_GUI_DEMO_STATUS', '/tmp/ndpa_gui_demo_status.json')


def rest(what):
    with urllib.request.urlopen(REST + what, timeout=2) as r:
        return json.load(r)


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


def status(phase, **extra):
    data = {'phase': phase, 't': time.strftime('%H:%M:%S'), **extra}
    try:
        with open(STATUS_PATH, 'w') as f:
            json.dump(data, f)
            f.write('\n')
    except OSError:
        pass
    # Use .format — phase strings may contain literal '%' (e.g. "loss 100%").
    print('[{0}] {1}'.format(data['t'], phase), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--datapath', choices=['kernel', 'user'], default='kernel')
    ap.add_argument('--hold', type=int, default=20,
                    help='seconds to keep the net up after the last fault (GUI watch window)')
    ap.add_argument('--logdir', default=os.path.join(ROOT, 'demo-logs', 'gui'))
    args = ap.parse_args()
    os.makedirs(args.logdir, exist_ok=True)

    for exe in ('server', 'client'):
        if not os.access(os.path.join(BIN, exe), os.X_OK):
            status('abort', error='missing telemetry/%s — run make -C telemetry' % exe)
            return 1

    status('checking Ryu')
    if not wait_for(lambda: rest('topology') is not None, 5):
        status('abort', error='Ryu not reachable at %s — start ryu-manager first' % REST)
        return 1

    setLogLevel('warning')
    # Avoid topo.json (GUI draft) hijacking the classic diamond for this demo.
    saved = None
    if os.path.isfile(diamond.TOPO_JSON):
        saved = diamond.TOPO_JSON + '.gui_demo_bak'
        os.rename(diamond.TOPO_JSON, saved)

    net = None
    mgmt = None
    try:
        status('starting Mininet diamond + mgmt')
        net = diamond.build_net(args.datapath, controller=True)
        net.start()
        diamond.prepare_hosts(net, args.datapath)
        mgmt = diamond.add_mgmt_link(net)
        h1, h2 = net.get('h1', 'h2')

        status('waiting for topology + path table')
        h1.cmd('ping -c 2 -W 1', SERVER_IP)
        view = wait_for(lambda: (lambda p: p if len(p.get('paths') or []) >= 2 else None)(rest('paths')), 20)
        if not view:
            status('abort', error='controller did not build path A/B — see ryu log')
            return 1

        status('starting C++ server + client')
        h2.cmd('%s/server -p %d,%d,%d > %s/server.log 2>&1 &' % (
            BIN, PORT_A, PORT_B, PORT_LIVE, args.logdir))
        time.sleep(0.5)
        h1.cmd('%s/client -s %s -p %d,%d,%d -i 50 -t 500 -w 40 -c %s:%d > %s/client.log 2>&1 &' % (
            BIN, SERVER_IP, PORT_A, PORT_B, PORT_LIVE, diamond.MGMT_ROOT_IP, REPORT_PORT,
            args.logdir))

        ok = wait_for(lambda: all(p['state'] == 'healthy' for p in rest('paths')['paths']), 20)
        view = rest('paths')
        status('baseline', active=view.get('active'),
               paths={p['name']: p['state'] for p in view.get('paths') or []},
               healthy=bool(ok))
        time.sleep(4)

        status('soft/blackhole path A (netem loss 100% on {0})'.format(NETEM_INTF))
        net.get('s2').cmd('tc qdisc replace dev %s root netem loss 100%%' % NETEM_INTF)
        moved = wait_for(lambda: rest('paths')['active'] == 'B', 20)
        view = rest('paths')
        status('A blackholed', active=view.get('active'), rerouted=bool(moved),
               paths={p['name']: p['state'] for p in view.get('paths') or []})
        time.sleep(5)

        status('repair A (clear netem)')
        net.get('s2').cmd('tc qdisc del dev %s root' % NETEM_INTF)
        wait_for(lambda: rest('paths')['paths'][0]['state'] == 'healthy', 20)
        view = rest('paths')
        status('A repaired', active=view.get('active'),
               paths={p['name']: p['state'] for p in view.get('paths') or []})
        time.sleep(3)

        status('hard fail B (link s3-s4 down)')
        net.configLinkStatus('s3', 's4', 'down')
        moved = wait_for(lambda: rest('paths')['active'] == 'A', 15)
        view = rest('paths')
        status('B link down', active=view.get('active'), rerouted=bool(moved),
               paths={p['name']: p['state'] for p in view.get('paths') or []})
        time.sleep(5)

        status('restore B (link s3-s4 up)')
        net.configLinkStatus('s3', 's4', 'up')
        wait_for(lambda: rest('paths')['paths'][1]['state'] == 'healthy', 20)
        view = rest('paths')
        status('B restored', active=view.get('active'),
               paths={p['name']: p['state'] for p in view.get('paths') or []})

        status('hold for GUI', hold_s=args.hold)
        time.sleep(max(0, args.hold))
        status('done')
        return 0
    finally:
        try:
            if net:
                # Narrow patterns — never pkill ryu / ndpa_controller.
                net.get('h1').cmd('pkill -x client || true')
                net.get('h2').cmd('pkill -x server || true')
        except Exception:
            pass
        if mgmt:
            try:
                diamond.remove_mgmt_link(mgmt)
            except Exception:
                pass
        if net:
            try:
                net.stop()
            except Exception:
                pass
        if saved and os.path.isfile(saved):
            os.rename(saved, diamond.TOPO_JSON)


if __name__ == '__main__':
    sys.exit(main())
