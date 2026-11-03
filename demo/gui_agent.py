#!/usr/bin/env python3
"""Root helper for the GUI stack — run via ./demo/gui.sh (one sudo).

Listens for Start (op=run) from the Ryu dashboard so the web UI never
needs passwordless sudo.
"""
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
GUI_DEMO_PY = os.path.join(ROOT, 'demo', 'gui_demo.py')
LOG_DIR = os.path.join(ROOT, 'demo-logs', 'gui')

_child = None
_stop = False
_last_op = None


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


def _stop_child():
    global _child
    if _child is None:
        return
    if _child.poll() is None:
        log('stopping previous job pid %s' % _child.pid)
        try:
            os.killpg(os.getpgid(_child.pid), signal.SIGTERM)
        except OSError:
            try:
                _child.terminate()
            except OSError:
                pass
        try:
            _child.wait(timeout=4)
        except Exception:
            try:
                os.killpg(os.getpgid(_child.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                _child.wait(timeout=2)
            except Exception:
                pass
    _child = None
    _cleanup_ifaces()


def _cleanup_ifaces():
    cleanup = os.path.join(ROOT, 'demo', 'gui_cleanup.sh')
    if os.path.isfile(cleanup):
        subprocess.call(['bash', cleanup], cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write_reply(payload):
    body = dict(payload)
    body.setdefault('t', time.strftime('%H:%M:%S'))
    body.setdefault('ts', time.time())
    atomic_write(REPLY, body)


def _reap_child():
    """If Mininet/probes died on their own, say so and wipe leftover veths."""
    global _child
    if _child is None:
        return
    rc = _child.poll()
    if rc is None:
        return
    log('child exited %s' % rc)
    if rc != 0:
        _write_reply({
            'ok': False, 'op': _last_op or 'run',
            'detail': 'dataplane exited (code %s). leftover links cleaned; press Start to retry.' % rc,
        })
        _cleanup_ifaces()
    _child = None


def run_op(op):
    global _child, _last_op
    _stop_child()
    if op != 'run':
        return False, 'unknown op %r' % op
    if not os.path.isfile(GUI_DEMO_PY):
        return False, 'missing %s' % GUI_DEMO_PY
    os.makedirs(LOG_DIR, exist_ok=True)
    logpath = os.path.join(LOG_DIR, 'run.log')
    log('starting Mininet + probes → %s' % logpath)
    _cleanup_ifaces()
    args = [sys.executable, GUI_DEMO_PY, '--idle']
    with open(logpath, 'a') as lf:
        lf.write('\n--- agent %s at %s ---\n' % (op, time.strftime('%H:%M:%S')))
        lf.flush()
        _child = subprocess.Popen(args, cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT,
                                  start_new_session=True)
    _last_op = 'run'
    return True, 'started pid %d' % _child.pid


def _have_iface(intf):
    return subprocess.call(['ip', 'link', 'show', 'dev', intf],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def apply_fault(req):
    """Immediate tc/netem or link down — does not need a free Mininet child slot."""
    action = req.get('action')
    intf = req.get('intf')
    if not intf or not isinstance(intf, str) or '/' in intf or '..' in intf:
        return False, 'bad intf'
    if not _have_iface(intf):
        return False, 'interface %s not found · is Mininet up?' % intf
    loss = float(req.get('loss_pct', 15))
    try:
        if action == 'degrade':
            subprocess.check_call(
                ['tc', 'qdisc', 'replace', 'dev', intf, 'root', 'netem',
                 'loss', '%.1f%%' % loss],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            return True, 'netem loss %.1f%% on %s' % (loss, intf)
        if action == 'blackhole':
            subprocess.check_call(
                ['tc', 'qdisc', 'replace', 'dev', intf, 'root', 'netem', 'loss', '100%'],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            return True, 'netem loss 100%% on %s' % intf
        if action == 'clear':
            subprocess.call(['tc', 'qdisc', 'del', 'dev', intf, 'root'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.call(['ip', 'link', 'set', intf, 'up'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, 'cleared soft/hard fault on %s' % intf
        if action == 'link_down':
            subprocess.call(['tc', 'qdisc', 'del', 'dev', intf, 'root'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call(['ip', 'link', 'set', intf, 'down'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            return True, 'link down %s' % intf
        if action == 'link_up':
            subprocess.check_call(['ip', 'link', 'set', intf, 'up'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            subprocess.call(['tc', 'qdisc', 'del', 'dev', intf, 'root'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, 'link up %s' % intf
    except subprocess.CalledProcessError as e:
        return False, 'fault cmd failed: %s' % e
    return False, 'bad action %r' % action


def consume_request():
    if not os.path.isfile(REQ):
        return
    try:
        with open(REQ) as f:
            req = json.load(f)
    except (OSError, ValueError) as e:
        _write_reply({'ok': False, 'error': str(e)})
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
    if op == 'fault':
        ok, detail = apply_fault(req)
    else:
        ok, detail = run_op(op)
    _write_reply({
        'ok': ok, 'op': op, 'detail': detail, 'id': req.get('id'),
    })
    log('request %s -> %s' % (op, detail))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--serve-forever', action='store_true', default=True,
                    help='keep listening for dashboard Start (default)')
    ap.add_argument('--once', action='store_true',
                    help='exit after the child finishes (no linger)')
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
    log('listening in %s (dashboard Start needs no passwordless sudo)' % AGENT_DIR)

    while not _stop:
        beat()
        consume_request()
        dead = _child is not None and _child.poll() is not None
        rc = _child.returncode if dead else None
        _reap_child()
        if dead and (args.once or not args.serve_forever):
            log('child exited %s — done' % rc)
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
