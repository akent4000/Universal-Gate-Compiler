#!/usr/bin/env python3
"""Regenerate ``benchmarks/qor_baseline.json`` from the current optimizer.

Run whenever an intentional synthesis change (new pass, tuning, DB rebuild)
shifts the reference NAND counts. Preserves ``_comment``, ``_script``, and
``_tolerance_pct`` from the existing file; only the ``circuits`` block is
refreshed. Keep the JSON under review — a large, unexplained drop is as
suspicious as a regression.
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE      = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from nand_optimizer.pipeline import optimize

sys.path.insert(0, HERE)
from conftest import ALL_FACTORIES  # noqa: E402

BASELINE_PATH = os.path.join(REPO_ROOT, 'benchmarks', 'qor_baseline.json')


def main() -> int:
    with open(BASELINE_PATH) as f:
        doc = json.load(f)

    new_circuits: dict = {}
    print(f'Refreshing QoR baseline ({len(ALL_FACTORIES)} circuits)...')
    for key in sorted(ALL_FACTORIES):
        t0     = time.time()
        tt     = ALL_FACTORIES[key]()
        result = optimize(tt, verbose=False)
        new_circuits[key] = result.total_nand
        print(f'  {key:>8}: {result.total_nand:>4} NAND  '
              f'({time.time() - t0:.1f}s)', flush=True)

    doc['circuits'] = new_circuits
    with open(BASELINE_PATH, 'w') as f:
        json.dump(doc, f, indent=2)
        f.write('\n')
    print(f'\nWrote {BASELINE_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
