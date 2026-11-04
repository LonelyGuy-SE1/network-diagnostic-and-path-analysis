#!/usr/bin/env python3
"""Bring up Mininet + C++ probes for the dashboard Start button."""
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
PORT_A, PORT_B, PORT_LIVE = 5001, 5002, 5003
REPORT_PORT, REST_PORT = 9100, 8080
REST = 'http://127.0.0.1:%d/ndpa/' % REST_PORT


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
    msg = phase
    if extra.get('error'):
        msg = '%s: %s' % (phase, extra['error'])
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--datapath', choices=['kernel', 'user'], default='kernel')
    ap.add_argument('--idle', action='store_true', default=True,
                    help=argparse.SUPPRESS)
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
        status('idle (probes running)')
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        while not stop:
            time.sleep(0.5)
        status('idle stop')
        return 0
    finally:
        try:
            if net:
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


if __name__ == '__main__':
    sys.exit(main())
