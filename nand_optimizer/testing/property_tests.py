"""
Property-based tests for the NAND optimiser.

Uses Hypothesis (if installed) to generate:
  * random combinational truth tables (arbitrary on-set + don't-cares)
  * random mutations that flip bits / swap outputs / add don't-cares

For every generated input, the suite runs the full pipeline and then
asserts equivalence against the original truth table via
``miter_verify`` (z3 when available, exhaustive otherwise).

If Hypothesis is missing, a deterministic pseudo-random fallback runs
a fixed number of seeds so the suite always has some coverage.
"""

from __future__ import annotations
import random
from typing import Dict, List, Optional, Set, Tuple

from ..core.truth_table import TruthTable
from ..pipeline         import optimize
from ..verify           import miter_verify


# ═══════════════════════════════════════════════════════════════════════════════
#  Generators
# ═══════════════════════════════════════════════════════════════════════════════

def _random_truth_table(n_inputs: int, n_outputs: int,
                        dc_fraction: float,
                        rng: random.Random) -> TruthTable:
    """Random TruthTable — bits drawn uniformly with random don't-cares."""
    total = 1 << n_inputs
    dcs: Set[int] = set()
    for m in range(total):
        if rng.random() < dc_fraction:
            dcs.add(m)

    rows: Dict[int, Tuple[int, ...]] = {}
    for m in range(total):
        if m in dcs:
            continue
        rows[m] = tuple(rng.randint(0, 1) for _ in range(n_outputs))

    return TruthTable.from_dict(
        n_inputs     = n_inputs,
        input_names  = [f'x{i}' for i in range(n_inputs)],
        output_names = [f'y{j}' for j in range(n_outputs)],
        rows         = rows,
        dont_cares   = dcs,
    )


def _mutate(tt: TruthTable, rng: random.Random) -> TruthTable:
    """Flip a random bit in a random defined row; occasionally toggle a don't-care."""
    rows = {m: list(vs) for m, vs in tt.rows.items()}
    dcs  = set(tt.dont_cares)

    action = rng.random()
    all_minterms = range(1 << tt.n_inputs)

    if action < 0.8 and rows:
        # flip one output bit of a defined row
        m = rng.choice(list(rows.keys()))
        j = rng.randrange(tt.n_outputs)
        rows[m][j] ^= 1
    elif action < 0.9:
        # promote a defined row to don't-care
        if rows:
            m = rng.choice(list(rows.keys()))
            del rows[m]
            dcs.add(m)
    else:
        # demote a don't-care to a defined (random) row
        if dcs:
            m = rng.choice(list(dcs))
            dcs.discard(m)
            rows[m] = [rng.randint(0, 1) for _ in range(tt.n_outputs)]

    return TruthTable.from_dict(
        n_inputs     = tt.n_inputs,
        input_names  = tt.input_names,
        output_names = tt.output_names,
        rows         = {m: tuple(v) for m, v in rows.items()},
        dont_cares   = dcs,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Single-case check
# ═══════════════════════════════════════════════════════════════════════════════

def check_equivalence(tt: TruthTable) -> Dict:
    """Optimise *tt* and run miter verification.  Raises AssertionError on mismatch."""
    result = optimize(tt, verbose=False)
    v = miter_verify(tt, result)
    if v['equivalent'] is False:
        raise AssertionError(
            f'optimiser produced non-equivalent netlist '
            f'({v["method"]}): counterexample {v["counterexample"]}'
        )
    return v


# ═══════════════════════════════════════════════════════════════════════════════
#  Hypothesis-based entry points (conditionally defined)
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from hypothesis import given, settings, strategies as st, HealthCheck
    _HAS_HYPOTHESIS = True
except ImportError:
    _HAS_HYPOTHESIS = False


if _HAS_HYPOTHESIS:

    @st.composite
    def _tt_strategy(draw,
                     max_inputs: int = 5,
                     max_outputs: int = 3,
                     dc_fraction_max: float = 0.3):
        n_inputs  = draw(st.integers(min_value=2, max_value=max_inputs))
        n_outputs = draw(st.integers(min_value=1, max_value=max_outputs))
        dc_frac   = draw(st.floats(min_value=0.0, max_value=dc_fraction_max))
        seed      = draw(st.integers(min_value=0, max_value=2**30))
        rng       = random.Random(seed)
        return _random_truth_table(n_inputs, n_outputs, dc_frac, rng)

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(tt=_tt_strategy())
    def property_random_equivalence(tt: TruthTable):
        """Every random truth table is synthesized into an equivalent NAND net."""
        check_equivalence(tt)

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(tt=_tt_strategy(), seed=st.integers(min_value=0, max_value=2**30))
    def property_mutation_equivalence(tt: TruthTable, seed: int):
        """Mutated truth tables are also synthesized equivalently."""
        mutated = _mutate(tt, random.Random(seed))
        check_equivalence(mutated)


# ═══════════════════════════════════════════════════════════════════════════════
#  Deterministic fallback runner (used when Hypothesis is unavailable or via CLI)
# ═══════════════════════════════════════════════════════════════════════════════

def run_property_tests(n_cases: int = 40,
                       seed:    int = 0xC0FFEE,
                       verbose: bool = True) -> bool:
    """
    Run ``n_cases`` random-equivalence checks.  Returns True iff all pass.

    This runner does not depend on Hypothesis, so it works in any
    environment.  When Hypothesis *is* available, callers can additionally
    invoke :func:`property_random_equivalence` / :func:`property_mutation_equivalence`
    through pytest for shrinking.
    """
    rng = random.Random(seed)
    n_pass = 0
    n_fail = 0

    bar = '=' * 72
    if verbose:
        print(f'\n{bar}\n  PROPERTY-BASED REGRESSION ({n_cases} random cases)')
        if _HAS_HYPOTHESIS:
            print('  Hypothesis found — use pytest to unlock shrinking.')
        else:
            print('  Hypothesis unavailable — running deterministic fallback.')
        print(bar)

    for i in range(n_cases):
        n_in  = rng.randint(2, 5)
        n_out = rng.randint(1, 3)
        dcf   = rng.random() * 0.3
        tt    = _random_truth_table(n_in, n_out, dcf, rng)
        try:
            v = check_equivalence(tt)
            n_pass += 1
            if verbose:
                print(f'  [{i+1:>3}/{n_cases}] n={n_in} m={n_out} '
                      f'dc={len(tt.dont_cares):>2}  OK ({v["method"]})')
        except AssertionError as e:
            n_fail += 1
            if verbose:
                print(f'  [{i+1:>3}/{n_cases}] n={n_in} m={n_out} '
                      f'dc={len(tt.dont_cares):>2}  FAIL — {e}')

    if verbose:
        print(bar)
        print(f'  Results: {n_pass}/{n_cases} passed'
              + (f'  ({n_fail} FAILED)' if n_fail else '  — ALL PASSED'))
        print(bar)

    return n_fail == 0


if __name__ == '__main__':
    import sys
    ok = run_property_tests()
    sys.exit(0 if ok else 1)
