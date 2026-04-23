"""QoR regression snapshot.

Runs ``optimize()`` on the nine reference circuits and compares the total
NAND gate count against ``benchmarks/qor_baseline.json``. A current count
that exceeds ``baseline * (1 + tolerance_pct/100)`` fails the test — this
is the primary guard against silent synthesis-quality regressions.

Lower counts are accepted silently; run ``python3 tests/_refresh_qor_baseline.py``
to pin the new numbers in after an intentional improvement.
"""
from __future__ import annotations

import json
import math
import os

import pytest

from nand_optimizer.pipeline import optimize

from conftest import ALL_FACTORIES, REPO_ROOT


BASELINE_PATH = os.path.join(REPO_ROOT, 'benchmarks', 'qor_baseline.json')


def _load_baseline() -> dict:
    with open(BASELINE_PATH) as f:
        return json.load(f)


@pytest.fixture(scope='module')
def baseline() -> dict:
    return _load_baseline()


@pytest.mark.parametrize('key', sorted(ALL_FACTORIES))
def test_qor_within_tolerance(key: str, baseline: dict) -> None:
    circuits  = baseline['circuits']
    tolerance = baseline['_tolerance_pct']
    assert key in circuits, \
        f'{key!r} missing from qor_baseline.json; add it or remove it from conftest'

    factory  = ALL_FACTORIES[key]
    tt       = factory()
    result   = optimize(tt, verbose=False)
    current  = result.total_nand
    expected = circuits[key]
    cap      = math.ceil(expected * (1 + tolerance / 100))

    assert current <= cap, (
        f'QoR regression on {key!r}: {current} NAND vs baseline {expected} '
        f'(cap {cap} at +{tolerance}%). '
        f'If this is intentional, rerun tests/_refresh_qor_baseline.py.'
    )
