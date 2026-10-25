#!/usr/bin/env python3
"""Unit tests for controller/analysis.py. Plain python3, no Ryu or Mininet."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'controller'))

import analysis as a  # noqa: E402

# Diamond: s1: 2->s2 3->s3, s2/s3: 1->s1 2->s4, s4: 1->s2 2->s3
DIAMOND = {1: {2: 2, 3: 3}, 2: {1: 1, 4: 2}, 3: {1: 1, 4: 2}, 4: {2: 1, 3: 2}}


class Paths(unittest.TestCase):
    def test_diamond(self):
        self.assertEqual(a.simple_paths(DIAMOND, 1, 4), [[1, 2, 4], [1, 3, 4]])

    def test_same_switch(self):
        self.assertEqual(a.simple_paths(DIAMOND, 1, 1), [[1]])

    def test_unreachable(self):
        self.assertEqual(a.simple_paths({1: {}, 4: {}}, 1, 4), [])


class Classify(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(a.classify_sample(0, 0.2)[0], a.HEALTHY)
        self.assertEqual(a.classify_sample(5, 0.2)[0], a.DEGRADED)
        self.assertEqual(a.classify_sample(0, 60)[0], a.DEGRADED)
        self.assertEqual(a.classify_sample(50, 0)[0], a.FAILED)
        self.assertEqual(a.classify_sample(100, 0)[0], a.FAILED)

    def test_hysteresis(self):
        p = a.PathState(0, [1, 2, 4], 5001)
        self.assertEqual(p.update(0, 0.2, 1), a.UNKNOWN)   # first report commits
        self.assertEqual(p.state, a.HEALTHY)
        self.assertIsNone(p.update(100, 0, 2))              # one bad report is not enough
        self.assertEqual(p.state, a.HEALTHY)
        self.assertEqual(p.update(100, 0, 3), a.HEALTHY)
        self.assertEqual(p.state, a.FAILED)
        self.assertEqual(p.first_bad_ts, 2)
        p.update(0, 0.2, 4)
        p.update(0, 0.2, 5)
        self.assertEqual(p.state, a.FAILED)                 # needs 3 good reports
        p.update(0, 0.2, 6)
        self.assertEqual(p.state, a.HEALTHY)
        self.assertIsNone(p.first_bad_ts)

    def test_port_down_is_immediate_and_sticky(self):
        p = a.PathState(0, [1, 2, 4], 5001, ports={(1, 2), (2, 2)})
        p.update(0, 0.2, 1)
        self.assertEqual(p.set_port((2, 2), True, 2), a.HEALTHY)
        self.assertEqual(p.state, a.FAILED)
        for t in range(3, 8):
            p.update(0, 0.2, t)                             # probes ignored while down
        self.assertEqual(p.state, a.FAILED)
        p.set_port((2, 2), False, 8)
        for t in range(9, 12):
            p.update(0, 0.2, t)
        self.assertEqual(p.state, a.HEALTHY)

    def test_other_ports_ignored(self):
        p = a.PathState(0, [1, 2, 4], 5001, ports={(1, 2)})
        self.assertIsNone(p.set_port((3, 2), True, 1))
        self.assertEqual(p.state, a.UNKNOWN)


class Choose(unittest.TestCase):
    def make(self, sa, sb):
        pa, pb = a.PathState(0, [1, 2, 4], 5001), a.PathState(1, [1, 3, 4], 5002)
        pa.state, pb.state = sa, sb
        return [pa, pb]

    def test_stay_when_healthy(self):
        self.assertIsNone(a.choose_active(self.make(a.HEALTHY, a.HEALTHY), 0, 100, 0)[0])

    def test_move_on_failure(self):
        self.assertEqual(a.choose_active(self.make(a.FAILED, a.HEALTHY), 0, 100, 99)[0], 1)

    def test_no_move_if_alternative_not_better(self):
        self.assertIsNone(a.choose_active(self.make(a.FAILED, a.FAILED), 0, 100, 0)[0])
        self.assertIsNone(a.choose_active(self.make(a.DEGRADED, a.DEGRADED), 0, 100, 0)[0])

    def test_hold_time_for_degraded_only(self):
        paths = self.make(a.DEGRADED, a.HEALTHY)
        self.assertIsNone(a.choose_active(paths, 0, 100, 98)[0])
        self.assertEqual(a.choose_active(paths, 0, 100, 90)[0], 1)

    def test_non_preemptive(self):
        # B is active and healthy, A recovered: stay on B.
        self.assertIsNone(a.choose_active(self.make(a.HEALTHY, a.HEALTHY), 1, 100, 0)[0])


if __name__ == '__main__':
    unittest.main()
