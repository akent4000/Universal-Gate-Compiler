"""Functional tests for the three built-in example circuits.

Invokes the full ``optimize()`` pipeline and asserts the T1..T13 universal
test suite (``run_tests``) passes — this covers QMC correctness, phase
assignment, factorization, Shannon decomposition, inversion elimination,
implicant coverage, NAND simulation, don't-care robustness, full
truth-table cross-check, greedy reassociation, Ashenhurst-Curtis
decomposition, exact-synthesis template correctness, and rewrite
equivalence.
"""
from __future__ import annotations

import pytest

from nand_optimizer.pipeline      import optimize
from nand_optimizer.testing.tests import run_tests

from conftest import BUILTIN_FACTORIES


@pytest.mark.parametrize('key', sorted(BUILTIN_FACTORIES))
def test_builtin_circuit_passes_full_suite(key: str) -> None:
    factory = BUILTIN_FACTORIES[key]
    tt      = factory()
    result  = optimize(tt, verbose=False)
    assert run_tests(tt, result, verbose=False), \
        f'T1..T13 universal suite failed for built-in circuit {key!r}'
