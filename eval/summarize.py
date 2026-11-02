#!/usr/bin/env python3
"""Print the static-vs-dynamic comparison for a runs.csv from eval/benchmark.py.

    python3 eval/summarize.py eval/results/<run>/runs.csv [--out summary.csv]
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runs_csv')
    ap.add_argument('--out', help='also write the per-scenario means to this CSV')
    a = ap.parse_args()
    rows = metrics.read_csv(a.runs_csv)
    print(metrics.format_comparison(rows))
    if a.out:
        summary = metrics.summarize(rows)
        cols = ['scenario', 'mode', 'runs'] + [c for c, _, _ in metrics.HEADLINE]
        with open(a.out, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols, restval='')
            w.writeheader()
            for s in summary:
                w.writerow({k: ('' if v is None else v) for k, v in s.items()})
        print('wrote %s' % a.out)


if __name__ == '__main__':
    main()
