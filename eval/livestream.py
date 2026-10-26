#!/usr/bin/env python3
"""Paced UDP stream used as "live traffic" by the benchmark (issue #17).

The sender numbers every packet and stamps it with the wall-clock send time.
The receiver logs (seq, send time, receive time, bytes) for every packet, so
the harness can work out loss, service gaps, one-way delay and goodput without
asking the controller anything. Both run inside Mininet hosts on one machine,
so they share a clock.

    livestream.py recv --port 5004 --duration 40 --out live-recv.csv
    livestream.py send --dst 10.0.0.2 --port 5004 --rate 100 --size 200 --duration 30
"""
import argparse
import socket
import struct
import time

HDR = struct.Struct('!Id')       # sequence number, send time (seconds since the epoch)


def send(a):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pad = b'\0' * max(0, a.size - HDR.size)
    total = int(a.duration * a.rate)
    step = 1.0 / a.rate
    start = time.perf_counter()
    errors = 0
    for seq in range(total):
        wait = start + seq * step - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        try:
            sock.sendto(HDR.pack(seq, time.time()) + pad, (a.dst, a.port))
        except OSError:
            errors += 1          # e.g. ENOBUFS; the packet counts as sent and lost
    print('sent=%d errors=%d' % (total, errors), flush=True)


def recv(a):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind(('0.0.0.0', a.port))
    sock.settimeout(0.2)
    end = time.perf_counter() + a.duration
    rows = []
    while time.perf_counter() < end:
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        now = time.time()
        if len(data) >= HDR.size:
            seq, sent_at = HDR.unpack_from(data)
            rows.append((seq, sent_at, now, len(data)))
    with open(a.out, 'w') as f:
        f.write('seq,send_ts,recv_ts,bytes\n')
        for seq, sent_at, now, n in rows:
            f.write('%d,%.6f,%.6f,%d\n' % (seq, sent_at, now, n))
    print('received=%d' % len(rows), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('send')
    s.add_argument('--dst', required=True)
    s.add_argument('--port', type=int, default=5004)
    s.add_argument('--rate', type=float, default=100, help='packets per second')
    s.add_argument('--size', type=int, default=200, help='UDP payload bytes')
    s.add_argument('--duration', type=float, required=True, help='seconds')
    s.set_defaults(fn=send)
    r = sub.add_parser('recv')
    r.add_argument('--port', type=int, default=5004)
    r.add_argument('--duration', type=float, required=True, help='seconds to listen')
    r.add_argument('--out', required=True, help='CSV file written at the end')
    r.set_defaults(fn=recv)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
