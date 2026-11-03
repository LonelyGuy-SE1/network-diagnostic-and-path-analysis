#!/usr/bin/env python3
"""Root helper for the GUI stack — run via ./demo/gui.sh (one sudo).

Listens for demo/run requests from the Ryu dashboard so the web UI never
needs passwordless sudo. Only accepts ops {demo, run}.
"""
from __future__ import print_function

import argparse
import json
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.environ.get('NDPA_GUI_AGENT', '/tmp/ndpa_gui_agent')
REQ = os.path.join(AGENT_DIR, 'request.json')
REPLY = os.path.join(AGENT_DIR, 'reply.json')
HEART = os.path.join(AGENT_DIR, 'heartbeat')
PID = os.path.join(AGENT_DIR, 'pid')
GUI_DEMO = os.path.join(ROOT, 'demo', 'gui_demo.sh')
GUI_RUN = os.path.join(ROOT, 'demo', 'gui_run.sh')
LOG_DIR = os.path.join(ROOT, 'demo-logs', 'gui')

_child = None
_stop = False


def log(msg):
    print('[gui-agent] %s' % msg, flush=True)


def atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
            f.write('\n')
    os.rename(tmp, path)


def beat():
    atomic_write(HEART, '%.3f\n' % time.time())


def on_signal(signum, _frame):
    global _stop
    _stop = True
    log('signal %s — shutting down' % signum)
    if _child and _child.poll() is None:
        try:
            _child.terminate()
        except OSError:
            pass


def run_op(op, hold=20, extra_args=None):
    global _child
    if _child and _child.poll() is None:
        return False, 'busy · a Mininet job is already running'
    if op == 'demo':
        script = GUI_DEMO
        args = [script, '--hold', str(hold)] + list(extra_args or [])
    elif op == 'run':
        script = GUI_RUN
        args = [script]
    else:
        return False, 'unknown op %r' % op
    if not os.path.isfile(script):
        return False, 'missing %s' % script
    os.makedirs(LOG_DIR, exist_ok=True)
    logpath = os.path.join(LOG_DIR, '%s.log' % op)
    log('starting %s → %s' % (op, logpath))
    with open(logpath, 'a') as lf:
        lf.write('\n--- agent %s at %s ---\n' % (op, time.strftime('%H:%M:%S')))
        lf.flush()
        _child = subprocess.Popen(args, cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT,
                                  start_new_session=True)
    return True, 'started pid %d' % _child.pid


def consume_request():
    if not os.path.isfile(REQ):
        return
    try:
        with open(REQ) as f:
            req = json.load(f)
    except (OSError, ValueError) as e:
        atomic_write(REPLY, {'ok': False, 'error': str(e)})
        try:
            os.unlink(REQ)
        except OSError:
            pass
        return
    try:
        os.unlink(REQ)
    except OSError:
        pass
    op = req.get('op')
    hold = int(req.get('hold', 20))
    ok, detail = run_op(op, hold=hold)
    atomic_write(REPLY, {'ok': ok, 'op': op, 'detail': detail, 't': time.strftime('%H:%M:%S')})
    log('request %s → %s' % (op, detail))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--auto-demo', action='store_true',
                    help='kick gui_demo once at startup')
    ap.add_argument('--hold', type=int, default=45,
                    help='gui_demo hold window (seconds)')
    ap.add_argument('--serve-forever', action='store_true', default=True,
                    help='keep listening for dashboard Demo/Run (default)')
    ap.add_argument('--once', action='store_true',
                    help='exit after auto-demo child finishes (no linger)')
    args = ap.parse_args()
    if args.once:
        args.serve_forever = False

    if os.geteuid() != 0:
        log('must run as root — use: ./demo/gui.sh')
        return 1

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    for p in (REQ, REPLY):
        try:
            os.unlink(p)
        except OSError:
            pass
    os.makedirs(AGENT_DIR, exist_ok=True)
    # World-readable heartbeat so the non-root Ryu process can see us;
    # request file is created by the controller (same tmp dir).
    os.chmod(AGENT_DIR, 0o1777)
    atomic_write(PID, '%d\n' % os.getpid())
    beat()
    log('listening in %s (dashboard Demo/Run need no passwordless sudo)' % AGENT_DIR)

    if args.auto_demo:
        ok, detail = run_op('demo', hold=args.hold)
        atomic_write(REPLY, {'ok': ok, 'op': 'demo', 'detail': detail,
                             't': time.strftime('%H:%M:%S')})
        log('auto-demo: %s' % detail)

    while not _stop:
        beat()
        consume_request()
        if args.once and _child is not None and _child.poll() is not None:
            log('child exited %s — done' % _child.returncode)
            break
        if not args.serve_forever and _child is not None and _child.poll() is not None:
            break
        time.sleep(0.4)

    if _child and _child.poll() is None:
        log('stopping child %d' % _child.pid)
        try:
            os.killpg(os.getpgid(_child.pid), signal.SIGTERM)
        except OSError:
            _child.terminate()
        try:
            _child.wait(timeout=15)
        except Exception:
            pass
    try:
        os.unlink(HEART)
        os.unlink(PID)
    except OSError:
        pass
    log('bye')
    return 0


if __name__ == '__main__':
    sys.exit(main())
