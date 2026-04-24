"""
Regression tests for SAT sweeping (ROADMAP P3#10).

`sat_sweep` extends FRAIG with observability don't-cares (ODC): two nodes
may be merged when they disagree only on input patterns where neither
contributes to any primary output. The merger is verified by a Z3 miter:

    ∃ x : obs_m(x) ∧ (f_rep(x) ≠ f_m(x))      UNSAT → safe to merge

Three invariants are checked here:

* ``test_soundness_on_built_in`` — on a small built-in circuit, the
  result must be functionally equivalent to the input. Guards against
  any future change to the symbolic-obs builder that would weaken the
  miter.
* ``test_subsumes_fraig`` — on the same circuit, ``n_ands`` after
  ``sat_sweep`` must be ``≤`` ``n_ands`` after FRAIG alone. ``sat_sweep``
  is by construction a superset of FRAIG; a regression here means the
  ODC-aware bucketing has stopped catching the FRAIG-equivalences.
* ``test_finds_odc_merges_on_adder`` — on EPFL ``adder.aig`` (the
  designated ODC-rich workload from [pass_eval.md](benchmarks/pass_eval.md)),
  ``sat_sweep`` must merge strictly more than FRAIG. This is the
  empirical rationale for shipping the pass; if it ever stops winning
  here, the heuristic has drifted.

The adder test is tagged ``@pytest.mark.skipif(not HAS_EPFL)`` so the
suite still runs on machines without the EPFL corpus downloaded.
"""
from __future__ import annotations
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from nand_optimizer.core.aig import AIG
from nand_optimizer.synthesis.fraig import _build_z3_exprs, fraig
from nand_optimizer.synthesis.sat_sweep import sat_sweep
from nand_optimizer.io.aiger_io import read_aiger

ADDER_PATH = os.path.join(REPO_ROOT, 'benchmarks', 'epfl', 'arithmetic', 'adder.aig')
HAS_EPFL = os.path.exists(ADDER_PATH)


def _equivalent(a, a_outs, b, b_outs, timeout_ms: int = 30_000) -> bool:
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


def _build_assoc_aig():
    """Two associativity-equivalent ANDs: (a&b)&c vs a&(b&c)."""
    aig = AIG()
    a = aig.make_input('a')
    b = aig.make_input('b')
    c = aig.make_input('c')
    n1 = aig.make_and(aig.make_and(a, b), c)
    n2 = aig.make_and(a, aig.make_and(b, c))
    return aig, [n1, n2]


def test_soundness_on_built_in():
    aig, outs = _build_assoc_aig()
    new_aig, new_outs = sat_sweep(aig, outs)
    assert _equivalent(aig, outs, new_aig, new_outs), (
        'sat_sweep returned a functionally non-equivalent AIG. The Z3 miter '
        'inside _check_pair_odc must have admitted an unsound merge — '
        'investigate _build_z3_obs and the ODC-mode check before merging.'
    )


def test_subsumes_fraig():
    aig, outs = _build_assoc_aig()
    fr_aig,    _ = fraig(aig, outs)
    sweep_aig, _ = sat_sweep(aig, outs)
    assert sweep_aig.n_ands <= fr_aig.n_ands, (
        f'sat_sweep merged fewer nodes than FRAIG '
        f'({sweep_aig.n_ands} vs {fr_aig.n_ands}). sat_sweep should be '
        f'a superset of FRAIG by construction (standard buckets are the '
        f'same canonical signatures FRAIG uses).'
    )


@pytest.mark.skipif(not HAS_EPFL, reason='EPFL adder.aig not downloaded')
def test_finds_odc_merges_on_adder():
    """
    On EPFL adder, FRAIG cannot merge anything (1020 → 1020) but sat_sweep
    finds ODC-modulo equivalences worth ≥ 5% of the AND count.
    See benchmarks/pass_eval.md §5 for the empirical baseline.
    """
    aig, outs, _, _ = read_aiger(ADDER_PATH)
    base = aig.n_ands

    fr_aig,    _ = fraig(aig, list(outs))
    sweep_aig, sweep_outs = sat_sweep(aig, list(outs))

    assert sweep_aig.n_ands < fr_aig.n_ands, (
        f'sat_sweep failed to find merges past FRAIG on adder '
        f'({sweep_aig.n_ands} vs {fr_aig.n_ands}). The ODC fill-based '
        f'buckets must have stopped grouping reconvergent equivalences; '
        f'check _form_fill_classes and _propagate_care_sim coverage.'
    )

    saved_pct = 100.0 * (fr_aig.n_ands - sweep_aig.n_ands) / fr_aig.n_ands
    assert saved_pct >= 4.0, (
        f'sat_sweep merge yield on adder dropped to {saved_pct:.1f}% '
        f'(historical baseline: −6.4%). Investigate before relaxing '
        f'this threshold.'
    )

    assert _equivalent(aig, list(outs), sweep_aig, sweep_outs), (
        'sat_sweep produced a non-equivalent adder — hard soundness '
        'regression. Inspect the symbolic obs builder for the new failure.'
    )
    assert sweep_aig.n_ands < base, 'sweep should reduce adder below baseline ANDs'


if __name__ == '__main__':
    test_soundness_on_built_in()
    test_subsumes_fraig()
    if HAS_EPFL:
        test_finds_odc_merges_on_adder()
    print('OK')
