#!/usr/bin/env python3
"""GUI live demo — uses an ALREADY-RUNNING Ryu (the one serving /ndpa/).

Does not touch demo/d1_demo.sh. Brings up Mininet diamond + mgmt, starts the
real C++ server/client, injects the README faults, and leaves the net up so
the dashboard can show real path states.

Preferred entry point (starts Ryu + root agent + this demo):

  ./demo/gui.sh
  # browser: http://127.0.0.1:8080/ndpa/
"""
import argparse
import json
import os
import signal
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
PATH_S2, PATH_S3 = 's1-s2-s4', 's1-s3-s4'  # via middle hop
REPORT_PORT, REST_PORT = 9100, 8080
REST = 'http://127.0.0.1:%d/ndpa/' % REST_PORT
NETEM_INTF = 's2-eth2'
STATUS_PATH = os.environ.get('NDPA_GUI_DEMO_STATUS', '/tmp/ndpa_gui_demo_status.json')


def pick_hosts(net):
    """Probe src/dst from topology/topo.json (GUI Start), else h1/h2."""
    hosts = {h.name: h for h in net.hosts}
    spec = {}
    if os.path.isfile(diamond.TOPO_JSON):
        try:
            with open(diamond.TOPO_JSON) as f:
                spec = json.load(f) or {}
        except (OSError, ValueError):
            spec = {}
    probe = spec.get('probe') or {}
    src = hosts.get(probe.get('src')) or hosts.get('h1') or list(net.hosts)[0]
    dst = hosts.get(probe.get('dst'))
    if dst is None or dst is src:
        dst = hosts.get('h2')
        if dst is None or dst is src:
            dst = next((h for h in net.hosts if h is not src), src)
    return src, dst


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
    ap.add_argument('--idle', action='store_true',
                    help='bring up Mininet + probes and hold; no auto faults')
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
    # Canned fault timeline uses the classic diamond. Idle/Start uses topo.json if present.
    saved = None
    if not args.idle and os.path.isfile(diamond.TOPO_JSON):
        saved = diamond.TOPO_JSON + '.gui_demo_bak'
        os.rename(diamond.TOPO_JSON, saved)

    net = None
    mgmt = None
    try:
        status('starting Mininet diamond + mgmt')
        net = diamond.build_net(args.datapath, controller=True)
        net.start()
        diamond.prepare_hosts(net, args.datapath)
        src, dst = pick_hosts(net)
        server_ip = dst.IP()
        mgmt = diamond.add_mgmt_link(net, host=src.name)
        status('probe pair {0} ({1}) -> {2} ({3})'.format(
            src.name, src.IP(), dst.name, dst.IP()))

        status('waiting for topology + path table')
        # Background pings from BOTH probe hosts. A one-shot ping can finish
        # before LLDP, and a sticky port map from the last graph can hide a host.
        src.cmd('ping -c 12 -i 0.2 -W 1 %s >/dev/null 2>&1 &' % server_ip)
        dst.cmd('ping -c 12 -i 0.2 -W 1 %s >/dev/null 2>&1 &' % src.IP())
        def have_paths():
            try:
                p = rest('paths')
            except OSError:
                return None
            return p if len(p.get('paths') or []) >= 1 else None
        view = wait_for(have_paths, 25, step=0.4)
        if not view:
            status('abort', error='controller did not build paths. see ryu log')
            return 1

        probe_ports = []
        for p in view.get('paths') or []:
            port = p.get('probe_port')
            if port and int(port) not in probe_ports:
                probe_ports.append(int(port))
        if not probe_ports:
            probe_ports = [PORT_A, PORT_B]
        listen = list(probe_ports)
        if PORT_LIVE not in listen:
            listen.append(PORT_LIVE)
        port_csv = ','.join(str(p) for p in listen)

        status('starting C++ server + client on udp/' + port_csv)
        dst.cmd('%s/server -p %s > %s/server.log 2>&1 &' % (
            BIN, port_csv, args.logdir))
        time.sleep(0.5)
        src.cmd('%s/client -s %s -p %s -i 50 -t 500 -w 40 -c %s:%d > %s/client.log 2>&1 &' % (
            BIN, server_ip, port_csv, diamond.MGMT_ROOT_IP, REPORT_PORT,
            args.logdir))

        ok = wait_for(lambda: all(p['state'] == 'healthy' for p in rest('paths')['paths']), 20)
        view = rest('paths')
        status('baseline', active=view.get('active'),
               paths={p['name']: p['state'] for p in view.get('paths') or []},
               healthy=bool(ok))
        if args.idle:
            status('idle (probes running, no auto faults)')
            stop = []
            signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
            signal.signal(signal.SIGINT, lambda *_: stop.append(1))
            while not stop:
                time.sleep(0.5)
            status('idle stop')
            return 0

        time.sleep(4)

        status('soft/blackhole {0} (netem loss 100% on {1})'.format(PATH_S2, NETEM_INTF))
        net.get('s2').cmd('tc qdisc replace dev %s root netem loss 100%%' % NETEM_INTF)
        moved = wait_for(lambda: rest('paths')['active'] == PATH_S3, 20)
        view = rest('paths')
        status(PATH_S2 + ' blackholed', active=view.get('active'), rerouted=bool(moved),
               paths={p['name']: p['state'] for p in view.get('paths') or []})
        time.sleep(5)

        status('repair ' + PATH_S2 + ' (clear netem)')
        net.get('s2').cmd('tc qdisc del dev %s root' % NETEM_INTF)
        wait_for(lambda: rest('paths')['paths'][0]['state'] == 'healthy', 20)
        view = rest('paths')
        status(PATH_S2 + ' repaired', active=view.get('active'),
               paths={p['name']: p['state'] for p in view.get('paths') or []})
        time.sleep(3)

        status('hard fail ' + PATH_S3 + ' (link s3-s4 down)')
        net.configLinkStatus('s3', 's4', 'down')
        moved = wait_for(lambda: rest('paths')['active'] == PATH_S2, 15)
        view = rest('paths')
        status(PATH_S3 + ' link down', active=view.get('active'), rerouted=bool(moved),
               paths={p['name']: p['state'] for p in view.get('paths') or []})
        time.sleep(5)

        status('restore ' + PATH_S3 + ' (link s3-s4 up)')
        net.configLinkStatus('s3', 's4', 'up')
        wait_for(lambda: rest('paths')['paths'][1]['state'] == 'healthy', 20)
        view = rest('paths')
        status(PATH_S3 + ' restored', active=view.get('active'),
               paths={p['name']: p['state'] for p in view.get('paths') or []})

        status('hold for GUI', hold_s=args.hold)
        time.sleep(max(0, args.hold))
        status('done')
        return 0
    finally:
        try:
            if net:
                # Narrow patterns — never pkill ryu / ndpa_controller.
                for h in net.hosts:
                    h.cmd('pkill -x client || true')
                    h.cmd('pkill -x server || true')
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
