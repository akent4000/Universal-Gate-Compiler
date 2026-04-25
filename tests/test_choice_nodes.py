"""
Regression tests for structural choice nodes (ROADMAP P3#9).

``build_choices`` runs several structural variants of the same circuit
and links functionally-equivalent nodes from different variants via
``AIG.add_choice``. ``rewrite_aig(use_choices=True)`` then enumerates
cuts at choice alternatives, so a cut with no template match at the
original node may match at a structural sibling.

This file checks three invariants:

* ``test_choice_primitives_roundtrip`` — the AIG core primitives
  (``add_choice``, ``choice_class``, ``choice_rep``, ``n_choice_links``
  plus ``gc``/``compose``/``snapshot``/``restore`` carryover) behave as
  documented. A regression here breaks every downstream pass that walks
  a chain.
* ``test_build_choices_soundness_*`` — the combined AIG returned by
  ``build_choices`` must be functionally equivalent to the input, every
  installed choice link must be a real equivalence (SAT-UNSAT), and
  ``rewrite_aig(use_choices=True)`` on the combined AIG must likewise
  preserve the function. A regression means the build pass is admitting
  unsound links — compare to the ``random_control/router.aig`` incident
  during the P3#9 landing, where 256-pattern sim alone produced 7/42
  false-positive links before Z3 verification was added.
* ``test_choice_rewrite_does_not_regress`` — on the built-in assoc
  circuit, choice-aware rewrite must not produce more ANDs than baseline
  rewrite. Not a strict improvement test because small inputs give
  identical results; the intent is a lower bound against future
  regressions in cut selection.
"""
from __future__ import annotations
import os
import sys
from typing import List, Tuple

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from nand_optimizer.core.aig           import AIG
from nand_optimizer.synthesis.choice   import build_choices
from nand_optimizer.synthesis.rewrite  import rewrite_aig
from nand_optimizer.synthesis.fraig    import _build_z3_exprs


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

def _equivalent(a: AIG, a_outs: List[int],
                b: AIG, b_outs: List[int],
                timeout_ms: int = 30_000) -> bool:
    """Formally check every output pair via a miter; UNSAT ⇒ equivalent."""
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


def _build_multi_and_or() -> Tuple[AIG, List[int]]:
    """8-input OR-of-ANDs — ``(a0&b0)|(a1&b1)|(a2&b2)|(a3&b3)``."""
    aig = AIG()
    a = [aig.make_input(f'a{i}') for i in range(4)]
    b = [aig.make_input(f'b{i}') for i in range(4)]
    pairs = [aig.make_and(a[i], b[i]) for i in range(4)]
    o = aig.make_or(aig.make_or(pairs[0], pairs[1]),
                    aig.make_or(pairs[2], pairs[3]))
    return aig, [o]


# ─────────────────────────────────────────────────────────────────────────────
#  Primitives
# ─────────────────────────────────────────────────────────────────────────────

def test_choice_primitives_roundtrip():
    aig = AIG()
    x = aig.make_input('x')
    y = aig.make_input('y')
    z = aig.make_input('z')
    n1 = aig.make_and(x, y)             # node 4
    n2 = aig.make_and(n1, z)            # node 5
    # Deliberately put three unrelated AND nodes into the same chain.
    n3 = aig.make_and(x, z)             # node 6
    n4 = aig.make_and(y, z)             # node 7

    a, b, c = aig.node_of(n2), aig.node_of(n3), aig.node_of(n4)
    aig.add_choice(a, b)
    aig.add_choice(a, c)
    assert aig.n_choice_links() == 2
    assert aig.choice_class(a) == [a, b, c]
    assert aig.choice_rep(c) == a
    assert aig.choice_rep(b) == a
    # Duplicate adds are idempotent.
    aig.add_choice(a, b)
    assert aig.n_choice_links() == 2

    # Nodes outside any chain return themselves as rep.
    assert aig.choice_rep(aig.node_of(n1)) == aig.node_of(n1)

    # GC must preserve choice chains that touch a live node.
    new, new_outs = aig.gc([n2])
    assert new.n_choice_links() >= 1, 'chain collapsed during gc'
    # Rep of the translated root should map to a live chain head.
    live_head = new.node_of(new_outs[0])
    assert new.choice_rep(live_head) == live_head or \
           live_head in new.choice_class(new.choice_rep(live_head))


# ─────────────────────────────────────────────────────────────────────────────
#  build_choices soundness
# ─────────────────────────────────────────────────────────────────────────────

def test_build_choices_preserves_function():
    aig, outs = _build_multi_and_or()
    combined, comb_outs, _ = build_choices(aig, outs)
    assert _equivalent(aig, outs, combined, comb_outs), (
        'build_choices produced a combined AIG whose primary outputs do not '
        'match the source. Inspect the primary-variant selector in '
        'synthesis/choice.py:build_choices — it picks the smallest variant '
        'as the output, and a buggy variant run would be caught here.'
    )


def test_build_choices_links_are_equivalences():
    """Every installed choice link must survive a Z3 miter."""
    import z3
    aig, outs = _build_multi_and_or()
    combined, _, _ = build_choices(aig, outs)

    _, exprs = _build_z3_exprs(combined)
    visited = set()
    for head in list(combined._choice_next.keys()):
        if head in visited:
            continue
        chain = [head]
        cur = head
        while cur in combined._choice_next:
            cur = combined._choice_next[cur]
            chain.append(cur)
        for m in chain:
            visited.add(m)
        rep = chain[0]
        for alt in chain[1:]:
            s = z3.Solver()
            s.set('timeout', 5000)
            s.add(z3.Xor(exprs[rep], exprs[alt]))
            assert s.check() == z3.unsat, (
                f'Unsound choice link {rep} ~ {alt}: SAT witness exists. '
                f'build_choices must verify every candidate pair via '
                f'_check_pair (FRAIG-style Z3 miter) before calling '
                f'AIG.add_choice — simulation alone produced 7/42 false '
                f'positives during the P3#9 landing incident.'
            )


def test_rewrite_with_choices_preserves_function():
    aig, outs = _build_multi_and_or()
    combined, comb_outs, _ = build_choices(aig, outs)
    r, r_outs = rewrite_aig(combined, list(comb_outs), use_choices=True)
    assert _equivalent(aig, outs, r, r_outs), (
        'rewrite_aig(use_choices=True) produced a non-equivalent result. '
        'The template-apply path in synthesis/rewrite.py must only consume '
        'cuts from choice siblings whose leaves are already translated '
        '(check the `c >= old_id` guard) and must only treat siblings as '
        'same-function (build_choices links must be SAT-verified).'
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Regression guard: choice-aware rewrite must not regress baseline
# ─────────────────────────────────────────────────────────────────────────────

def test_choice_rewrite_does_not_regress():
    aig, outs = _build_multi_and_or()

    r_plain, _ = rewrite_aig(aig, outs, use_choices=False)

    combined, comb_outs, _ = build_choices(aig, outs)
    r_choice, _ = rewrite_aig(combined, list(comb_outs), use_choices=True)

    assert r_choice.n_ands <= r_plain.n_ands, (
        f'choice-aware rewrite produced more ANDs '
        f'({r_choice.n_ands}) than baseline rewrite ({r_plain.n_ands}). '
        f'Cut selection must pick the base AND-translation when no '
        f'choice-rooted template beats it — see the base_cost / best_net '
        f'comparator in synthesis/rewrite.py.'
    )


if __name__ == '__main__':
    test_choice_primitives_roundtrip()
    test_build_choices_preserves_function()
    test_build_choices_links_are_equivalences()
    test_rewrite_with_choices_preserves_function()
    test_choice_rewrite_does_not_regress()
    print('OK')
