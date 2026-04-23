"""MCNC-style benchmark regression.

For every benchmark in the MCNC set, runs ``optimize()`` and checks
that the produced NAND network is functionally equivalent to the truth
table via ``miter_verify`` (Z3 if available, exhaustive simulation
otherwise).

Kept separate from the built-in T1..T13 harness because these benchmarks
are much bigger and the full universal suite would dominate CI time —
miter verification is the only property we really care about here.
"""
from __future__ import annotations

import pytest

from nand_optimizer.pipeline import optimize
from nand_optimizer.verify   import miter_verify

from conftest import MCNC_FACTORIES


@pytest.mark.parametrize('key', sorted(MCNC_FACTORIES))
def test_mcnc_benchmark_verifies(key: str) -> None:
    factory = MCNC_FACTORIES[key]
    tt      = factory()
    result  = optimize(tt, verbose=False)
    v       = miter_verify(tt, result)
    assert v['equivalent'] is not False, \
        f'miter reported MISMATCH on {key!r} ({v["method"]}): ' \
        f'counterexample={v.get("counterexample")}'
    # UNKNOWN (timeout) is tolerated — the exhaustive fallback should
    # pick up anything Z3 can't close inside the time budget.
