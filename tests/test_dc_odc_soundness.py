"""
Regression tests for the `dc --odc` soundness gap on reconvergent-fanout
circuits (ROADMAP P0#1).

The fixture ``router_outport1_minimal.aig`` is a 14-AND / 15-input sub-circuit
delta-debugged from the TFI cone of ``outport[1]`` in EPFL ``router.aig``.
On this fixture, ``dc_optimize`` with ``use_odc=True`` admits a sequence of
V2-admissibility-valid rewrites whose composition is functionally incorrect
— the end-of-pass safety-net miter reverts the whole pass.

Two invariants must hold, one documenting the bug, one guarding against a
regression that would silently break soundness:

* ``test_safety_net_preserves_soundness`` — no matter what ``dc --odc``
  does, the returned AIG must be functionally equivalent to the input.
  The safety-net miter is the last line of defence; this test catches any
  future change that weakens it.

* ``test_fixture_still_reverts`` — until a genuine V3 fix lands, this
  fixture must trigger exactly one safety-net revert. When V3 lands and
  ``dc --odc`` produces a sound simplification, this test will fail — that
  is the intended signal to flip the assertion (and celebrate).

These tests can be run under pytest (``pytest tests/test_dc_odc_soundness.py``)
or as a standalone script (``python3 tests/test_dc_odc_soundness.py``).
"""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from nand_optimizer.io.aiger_io import read_aiger
from nand_optimizer.synthesis.dont_care import dc_optimize, last_dc_stats
from nand_optimizer.synthesis.fraig import _build_z3_exprs

FIXTURE_PATH = os.path.join(HERE, 'fixtures', 'router_outport1_minimal.aig')


def _load_fixture():
    aig, out_lits, in_names, out_names = read_aiger(FIXTURE_PATH)
    assert aig.n_ands == 14, f'fixture should have 14 ANDs, got {aig.n_ands}'
    assert len(in_names) == 15, f'fixture should have 15 inputs, got {len(in_names)}'
    return aig, out_lits


def _equivalent(a, a_outs, b, b_outs, timeout_ms: int = 20_000) -> bool:
    import z3
    if len(a_outs) != len(b_outs):
        return False
    _, ax = _build_z3_exprs(a)
    _, bx = _build_z3_exprs(b)
    diffs = []
    for la, lb in zip(a_outs, b_outs):
        na, nb = a.node_of(la), b.node_of(lb)
        ea = ax[na] if na > 0 else z3.BoolVal(False)
        eb = bx[nb] if nb > 0 else z3.BoolVal(False)
        if a.is_complemented(la):
            ea = z3.Not(ea)
        if b.is_complemented(lb):
            eb = z3.Not(eb)
        diffs.append(z3.Xor(ea, eb))
    s = z3.Solver()
    s.set('timeout', timeout_ms)
    s.add(z3.Or(*diffs) if diffs else z3.BoolVal(False))
    return s.check() == z3.unsat


def test_safety_net_preserves_soundness():
    """dc --odc output is always functionally equivalent to input."""
    aig, out_lits = _load_fixture()
    new_aig, new_outs = dc_optimize(
        aig, out_lits, use_odc=True, rounds=1, safety_check=True,
    )
    assert _equivalent(aig, out_lits, new_aig, new_outs), (
        'dc --odc returned a functionally non-equivalent AIG — safety-net '
        'miter must have failed to catch a bad rewrite. This is a hard '
        'soundness regression; investigate _miter_equivalent and the '
        'revert path in dc_optimize before merging.'
    )


def test_fixture_still_reverts():
    """Documents current known revert on this fixture.

    When V3 lands and this test fails, it is the signal to (a) confirm the
    new output is still equivalent (via ``test_safety_net_preserves_soundness``)
    and (b) flip this assertion or delete the test.
    """
    aig, out_lits = _load_fixture()
    dc_optimize(aig, out_lits, use_odc=True, rounds=1, safety_check=True)
    n_reverts = last_dc_stats()['n_safety_net_reverts']
    assert n_reverts == 1, (
        f'Expected exactly one safety-net revert on this fixture (current '
        f'known-bad behavior); got {n_reverts}. If reverts == 0 and the '
        f'soundness test passes, the V2 admissibility gap may finally be '
        f'fixed — update ROADMAP P0#1 and this assertion accordingly.'
    )


def test_no_odc_does_not_revert():
    """Sanity: the same fixture is handled cleanly without --odc."""
    aig, out_lits = _load_fixture()
    new_aig, _ = dc_optimize(
        aig, out_lits, use_odc=False, rounds=1, safety_check=True,
    )
    assert last_dc_stats()['n_safety_net_reverts'] == 0


def test_fixture_revert_is_coverage_independent():
    """Raising sim-W to 16384 must not make the revert disappear.

    This is the empirical basis for classifying the bug as a theoretical
    V2 admissibility gap rather than a sim-coverage bug — if a future
    change makes large W fix this fixture, that's actually interesting
    data and the classification should be revisited.
    """
    aig, out_lits = _load_fixture()
    dc_optimize(
        aig, out_lits, use_odc=True, rounds=1, safety_check=True,
        n_sim_patterns=16384,
    )
    assert last_dc_stats()['n_safety_net_reverts'] == 1, (
        'Revert disappeared at W=16384 — the bug may be sim-coverage-bound '
        'after all, contradicting the V2.d EPFL probe finding. Re-run the '
        'probe and update ROADMAP P0#1 hypothesis (a)/(d).'
    )


def test_z3_exact_no_revert():
    """V3 fix: odc_mode='z3-exact' must produce 0 reverts on the fixture.

    When this test passes, ``test_fixture_still_reverts`` will also fail
    (since reverts drop to 0). That is the intended signal that P0#1 is
    fixed — remove ``test_fixture_still_reverts`` and close the ROADMAP item.
    """
    from nand_optimizer.synthesis.dont_care import ODC_MODE_Z3_EXACT
    aig, out_lits = _load_fixture()
    new_aig, new_outs = dc_optimize(
        aig, out_lits, use_odc=True, rounds=1, safety_check=True,
        odc_mode=ODC_MODE_Z3_EXACT,
    )
    assert last_dc_stats()['n_safety_net_reverts'] == 0, (
        'z3-exact mode reverted — soundness regression. '
        'Check _z3_template_admissible and _z3_resub_admissible.'
    )
    assert _equivalent(aig, out_lits, new_aig, new_outs), (
        'z3-exact admitted an unsound rewrite — safety-net missed it.'
    )


def test_z3_exact_circuit_equivalence():
    """z3-exact result must be equivalent to the original circuit (hard soundness)."""
    from nand_optimizer.synthesis.dont_care import ODC_MODE_Z3_EXACT
    aig, out_lits = _load_fixture()
    new_aig, new_outs = dc_optimize(
        aig, out_lits, use_odc=True, rounds=1,
        odc_mode=ODC_MODE_Z3_EXACT,
    )
    assert _equivalent(aig, out_lits, new_aig, new_outs)


if __name__ == '__main__':
    # Allow running without pytest.
    tests = [
        test_safety_net_preserves_soundness,
        test_fixture_still_reverts,
        test_no_odc_does_not_revert,
        test_fixture_revert_is_coverage_independent,
        test_z3_exact_no_revert,
        test_z3_exact_circuit_equivalence,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f'[PASS] {t.__name__}')
        except AssertionError as e:
            failed += 1
            print(f'[FAIL] {t.__name__}: {e}')
    sys.exit(1 if failed else 0)
