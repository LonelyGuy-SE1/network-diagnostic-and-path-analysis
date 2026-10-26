#!/usr/bin/env python3
"""Unit tests for eval/metrics.py and eval/livestream.py. Plain python3, no Mininet or Ryu."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'eval'))

import metrics as m  # noqa: E402

RATE = 100.0
T0 = 1000.0
T_FAULT, T_REPAIR = 1010.0, 1020.0
SENT = 3000                      # 30 s at 100 packets/s: 10 s before, 10 s fault, 10 s after


def stream(missing=(), size=200, owd=0.0005, extra_owd=None):
    """Receiver records for a stream where the sequence numbers in `missing` were lost."""
    recs = []
    for q in range(SENT):
        if q in missing:
            continue
        s = T0 + q / RATE
        d = owd + (extra_owd(q) if extra_owd else 0.0)
        recs.append((q, s, s + d, size))
    return recs


class LiveMetrics(unittest.TestCase):
    def test_clean_stream(self):
        r = m.live_metrics(stream(), SENT, RATE, T_FAULT, T_REPAIR)
        self.assertEqual(r['live_lost'], 0)
        self.assertEqual(r['live_loss_pct'], 0.0)
        self.assertEqual(r['outage_ms'], 0.0)
        for p in m.PHASES:
            self.assertEqual(r['live_lost_%s' % p], 0)
            self.assertAlmostEqual(r['goodput_%s_kbps' % p], 160.0, delta=1.0)   # 100 pps * 200 B
            self.assertAlmostEqual(r['owd_%s_ms' % p], 0.5, places=3)
        self.assertAlmostEqual(r['gap_at_repair_ms'], 10.0, delta=0.5)

    def test_static_like_outage_lasts_until_repair(self):
        lost = set(range(1000, 2000))                     # the whole fault window
        r = m.live_metrics(stream(lost), SENT, RATE, T_FAULT, T_REPAIR)
        self.assertEqual(r['live_lost'], 1000)
        self.assertAlmostEqual(r['live_loss_pct'], 100 * 1000 / SENT)
        self.assertEqual(r['outage_ms'], 10000.0)
        self.assertEqual((r['live_lost_before'], r['live_lost_fault'], r['live_lost_after']), (0, 1000, 0))
        self.assertEqual(r['goodput_fault_kbps'], 0.0)
        self.assertIsNone(r['owd_fault_ms'])
        self.assertAlmostEqual(r['gap_at_repair_ms'], 10010.0, delta=1.0)

    def test_dynamic_like_short_outage(self):
        lost = set(range(1000, 1200))                     # rerouted after 2 s
        r = m.live_metrics(stream(lost), SENT, RATE, T_FAULT, T_REPAIR)
        self.assertEqual(r['live_lost'], 200)
        self.assertEqual(r['outage_ms'], 2000.0)
        self.assertEqual(r['live_lost_fault'], 200)
        self.assertAlmostEqual(r['goodput_fault_kbps'], 160.0 * 0.8, delta=1.0)
        self.assertAlmostEqual(r['gap_at_repair_ms'], 10.0, delta=0.5)

    def test_delay_during_fault_shows_in_owd(self):
        slow = m.live_metrics(stream(extra_owd=lambda q: 0.060 if 1000 <= q < 2000 else 0.0),
                              SENT, RATE, T_FAULT, T_REPAIR)
        self.assertAlmostEqual(slow['owd_before_ms'], 0.5, places=3)
        self.assertAlmostEqual(slow['owd_fault_ms'], 60.5, places=3)

    def test_losses_at_the_end_count(self):
        r = m.live_metrics(stream(set(range(2900, 3000))), SENT, RATE, T_FAULT, T_REPAIR)
        self.assertEqual(r['live_lost'], 100)
        self.assertEqual(r['outage_ms'], 1000.0)
        self.assertEqual(r['live_lost_after'], 100)

    def test_duplicates_counted_once(self):
        recs = stream() + [(5, T0 + 0.05, T0 + 0.06, 200)]
        self.assertEqual(m.live_metrics(recs, SENT, RATE, T_FAULT, T_REPAIR)['live_recv'], SENT)

    def test_nothing_received(self):
        r = m.live_metrics([], SENT, RATE, T_FAULT, T_REPAIR)
        self.assertIsNone(r['outage_ms'])
        self.assertEqual(r['live_sent'], SENT)
        self.assertEqual(r['live_recv'], 0)


class ClientLog(unittest.TestCase):
    LOG = (
        'path=0 port=5001 sent=61 recv=60 loss=0.0% rtt_ms min=0.071 avg=0.106 max=0.298 jitter=0.054 '
        'dup=0 reord=0 late=0\n'
        'path=1 port=5002 sent=61 recv=60 loss=0.0% rtt_ms min=0.016 avg=0.033 max=0.217 jitter=0.035 '
        'dup=0 reord=0 late=0\n'
        'path=0 port=5001 sent=121 recv=100 loss=17.5% rtt_ms min=0.071 avg=61.250 max=64.0 jitter=0.4 '
        'dup=0 reord=0 late=0\n'
        'path=1 port=5002 sent=121 recv=120 loss=0.0% rtt_ms min=0.016 avg=0.040 max=0.217 jitter=0.035 '
        'dup=0 reord=0 late=0\n')

    def test_latest_line_per_port_wins(self):
        s = m.latest_by_port(self.LOG)
        self.assertEqual(sorted(s), [5001, 5002])
        self.assertEqual(s[5001]['loss'], 17.5)
        self.assertEqual(s[5001]['rtt_avg'], 61.25)
        self.assertEqual(s[5002]['rtt_avg'], 0.04)

    def test_empty(self):
        self.assertEqual(m.latest_by_port(''), {})


class Csv(unittest.TestCase):
    def test_roundtrip_and_blanks(self):
        rows = [{'run': 'static-hard-1', 'mode': 'static', 'scenario': 'hard', 'rep': 1,
                 'outage_ms': 10000.0, 'detect_ms': None, 'final_active': None, 'extra': 'ignored'},
                {'run': 'x', 'mode': 'dynamic', 'scenario': 'hard', 'rep': 1, 'outage_ms': 2000.0,
                 'detect_ms': 30.5, 'final_active': 'B'}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'runs.csv')
            m.write_csv(path, rows)
            back = m.read_csv(path)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0]['outage_ms'], 10000.0)
        self.assertIsNone(back[0]['detect_ms'])
        self.assertEqual(back[1]['final_active'], 'B')
        self.assertNotIn('extra', back[0])

    def test_header_matches_fields(self):
        self.assertEqual(len(m.FIELDS), len(set(m.FIELDS)))
        for need in ('detect_ms', 'reroute_ms', 'recover_ms', 'outage_ms', 'goodput_fault_kbps',
                     'A_rtt_before', 'B_loss_after', 'live_lost_fault', 'owd_after_ms'):
            self.assertIn(need, m.FIELDS)


class Summary(unittest.TestCase):
    ROWS = [
        {'scenario': 'hard', 'mode': 'static', 'outage_ms': 10000.0, 'live_loss_pct': 33.0},
        {'scenario': 'hard', 'mode': 'static', 'outage_ms': 10100.0, 'live_loss_pct': 33.6},
        {'scenario': 'hard', 'mode': 'dynamic', 'outage_ms': 40.0, 'live_loss_pct': 0.4, 'detect_ms': 25.0},
        {'scenario': 'hard', 'mode': 'dynamic', 'outage_ms': 60.0, 'live_loss_pct': 0.6, 'detect_ms': 35.0},
        {'scenario': 'hard', 'mode': 'dynamic', 'error': 'boom'},
        {'scenario': 'healthy', 'mode': 'static', 'outage_ms': 0.0, 'live_loss_pct': 0.0},
    ]

    def test_means_ignore_blanks_and_failed_runs(self):
        by = {(s['scenario'], s['mode']): s for s in m.summarize(self.ROWS)}
        self.assertEqual(by[('hard', 'static')]['outage_ms'], 10050.0)
        self.assertIsNone(by[('hard', 'static')]['detect_ms'])
        self.assertEqual(by[('hard', 'dynamic')]['runs'], 2)
        self.assertEqual(by[('hard', 'dynamic')]['detect_ms'], 30.0)

    def test_comparison_text(self):
        text = m.format_comparison(self.ROWS)
        self.assertIn('hard', text)
        self.assertIn('10050.0', text)
        self.assertLess(text.index('healthy'), text.index('hard'))
        self.assertEqual(m.format_comparison([]), 'no successful runs to summarize')


class LiveStreamLoopback(unittest.TestCase):
    def test_sender_and_receiver_over_loopback(self):
        script = os.path.join(ROOT, 'eval', 'livestream.py')
        port = '55004'
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'recv.csv')
            rx = subprocess.Popen([sys.executable, script, 'recv', '--port', port, '--duration', '3',
                                   '--out', out], stdout=subprocess.DEVNULL)
            try:
                time.sleep(0.5)
                tx = subprocess.run([sys.executable, script, 'send', '--dst', '127.0.0.1', '--port', port,
                                     '--rate', '50', '--size', '120', '--duration', '1.5'],
                                    capture_output=True, text=True, timeout=20)
                self.assertIn('sent=75', tx.stdout)
                rx.wait(timeout=20)
            finally:
                if rx.poll() is None:
                    rx.kill()
            recs = m.read_live(out)
        self.assertEqual(len(recs), 75)
        self.assertEqual({n for _, _, _, n in recs}, {120})
        t_end = recs[-1][1]
        r = m.live_metrics(recs, 75, 50.0, t_end + 10, t_end + 20)
        self.assertEqual(r['live_lost'], 0)
        self.assertLess(r['owd_before_ms'], 50)


if __name__ == '__main__':
    unittest.main()
