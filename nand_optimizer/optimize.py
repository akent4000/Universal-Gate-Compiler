"""
Boolean expression optimisation passes.

  • phase_assign      — choose f or ~f polarity
  • factorize         — algebraic common-subexpression extraction
  • apply_shannon     — Shannon cofactor simplification
  • elim_inv          — redundant double-negation removal
"""

from __future__ import annotations
from typing import Dict, FrozenSet, List, Set, Tuple

from .expr      import Expr, Lit, Not, And, Or, ONE, ZERO, simp
from .implicant import Implicant, espresso, implicants_to_expr


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase Assignment
# ═══════════════════════════════════════════════════════════════════════════════

def phase_assign(ones: Set[int], dont_cares: Set[int],
                 n_vars: int) -> Tuple[List[Implicant], bool]:
    """
    Choose the polarity (f or ~f) that yields fewer literals.

    Returns (implicants, is_complemented).
    If is_complemented is True, the implicants represent ~f and the
    NAND network must add an inverter at the output.
    """
    zeros = set(range(1 << n_vars)) - ones - dont_cares

    imps_f  = espresso(ones,  dont_cares, n_vars)
    imps_nf = espresso(zeros, dont_cares, n_vars)

    lits_f  = sum(p.literal_count() for p in imps_f)
    lits_nf = sum(p.literal_count() for p in imps_nf)

    if lits_nf < lits_f:
        return imps_nf, True
    return imps_f, False


# ═══════════════════════════════════════════════════════════════════════════════
#  Algebraic Factorization
# ═══════════════════════════════════════════════════════════════════════════════

def _term_lits(term: Expr) -> FrozenSet[Tuple[str, bool]]:
    """Return the frozenset of (name, neg) literals in a product term."""
    if isinstance(term, And):
        return frozenset((c.name, c.neg) for c in term.args if isinstance(c, Lit))
    if isinstance(term, Lit):
        return frozenset([(term.name, term.neg)])
    return frozenset()


def _build_term(lits: FrozenSet[Tuple[str, bool]]) -> Expr:
    if not lits:
        return ONE
    lst = [Lit(n, ng) for n, ng in sorted(lits)]
    return lst[0] if len(lst) == 1 else And(*lst)


def factorize_once(expr: Expr) -> Expr:
    """
    Single pass: find the literal appearing in the most product terms
    and factor it out.   e.g.  x·a | x·b  →  x·(a | b)
    """
    if not isinstance(expr, Or):
        return expr

    terms     = expr.args
    term_sets = [_term_lits(t) for t in terms]

    lit_occ: Dict[Tuple[str, bool], List[int]] = {}
    for i, ts in enumerate(term_sets):
        for lit in ts:
            lit_occ.setdefault(lit, []).append(i)

    candidates = [(lit, idxs) for lit, idxs in lit_occ.items() if len(idxs) > 1]
    if not candidates:
        return expr

    best_lit, best_idxs = max(candidates, key=lambda x: (len(x[1]), x[0]))
    vname, neg = best_lit
    factor = Lit(vname, neg)

    remainders    = [_build_term(term_sets[i] - {best_lit}) for i in best_idxs]
    remainder_sum = remainders[0] if len(remainders) == 1 else Or(*remainders)
    factored_part = simp(And(factor, remainder_sum))

    other = [terms[i] for i in range(len(terms)) if i not in best_idxs]
    if not other:
        return factored_part
    return simp(Or(factored_part, *other))


def factorize(expr: Expr, rounds: int = 10) -> Expr:
    """Apply factorization rounds until the expression stabilises."""
    for _ in range(rounds):
        new = factorize_once(expr)
        if str(new) == str(expr):
            break
        expr = new
    return expr


# ═══════════════════════════════════════════════════════════════════════════════
#  Shannon Decomposition
# ═══════════════════════════════════════════════════════════════════════════════

def shannon_on_var(expr: Expr, var: str) -> Expr:
    """
    f = x·f|_{x=1}  +  ~x·f|_{x=0}
    Keep result only if it has strictly fewer literals.
    """
    f1 = simp(expr.sub(var, 1))
    f0 = simp(expr.sub(var, 0))

    if f1 == f0:    return f1
    if f1 == ONE:   return simp(Or(Lit(var), f0))
    if f1 == ZERO:  return simp(And(Lit(var, True), f0))
    if f0 == ONE:   return simp(Or(Lit(var, True), f1))
    if f0 == ZERO:  return simp(And(Lit(var), f1))

    candidate = simp(Or(And(Lit(var), f1), And(Lit(var, True), f0)))
    return candidate if candidate.literals() < expr.literals() else expr


def apply_shannon(expr: Expr, var_names: List[str]) -> Expr:
    """Try Shannon decomposition on every variable; keep the best."""
    best = expr
    for v in var_names:
        if v not in expr.vars():
            continue
        candidate = shannon_on_var(expr, v)
        if candidate.literals() < best.literals():
            best = candidate
    return best


# ═══════════════════════════════════════════════════════════════════════════════
#  Redundant Inversion Elimination
# ═══════════════════════════════════════════════════════════════════════════════

def elim_inv(expr: Expr) -> Expr:
    """Recursively remove double inversions: ~~x → x, ~~~~x → x, etc."""
    if isinstance(expr, Not):
        inner = elim_inv(expr.arg)
        if isinstance(inner, Not):
            return elim_inv(inner.arg)
        return Not(inner)
    if isinstance(expr, And):
        return simp(And(*[elim_inv(a) for a in expr.args]))
    if isinstance(expr, Or):
        return simp(Or(*[elim_inv(a) for a in expr.args]))
    return expr


# ═══════════════════════════════════════════════════════════════════════════════
#  Cross-output algebraic factorization  (2.2)
# ═══════════════════════════════════════════════════════════════════════════════

def _sop_terms(e: Expr) -> List[Expr]:
    """Return top-level product terms of a SOP expression."""
    return list(e.args) if isinstance(e, Or) else [e]


def _factor_shared_lit(
    expr:    Expr,
    lit_key: Tuple[str, bool],
) -> Expr:
    """
    If *lit_key* appears in ≥ 2 product terms of *expr*, factor it out.
    x·a | x·b | c  →  x·(a | b) | c
    """
    if not isinstance(expr, Or):
        return expr

    terms     = expr.args
    term_sets = [_term_lits(t) for t in terms]
    idxs      = [i for i, ts in enumerate(term_sets) if lit_key in ts]

    if len(idxs) < 2:
        return expr

    vname, neg     = lit_key
    factor         = Lit(vname, neg)
    remainders     = [_build_term(term_sets[i] - {lit_key}) for i in idxs]
    remainder_sum  = remainders[0] if len(remainders) == 1 else Or(*remainders)
    factored_part  = simp(And(factor, remainder_sum))
    other          = [terms[i] for i in range(len(terms)) if i not in idxs]
    if not other:
        return factored_part
    return simp(Or(factored_part, *other))


def multi_output_factorize(exprs: List[Expr]) -> List[Expr]:
    """
    Cross-output algebraic factorization.

    Scans the top-level product terms of every output expression to find
    literals that appear in 2+ *different* outputs.  For each such shared
    literal (processed highest-sharing first), every output that contains
    it in ≥ 2 of its own terms has it factored out — the same heuristic as
    factorize_once, but the decision to factor is driven by cross-output
    frequency rather than intra-output repetition alone.

    This exposes common And sub-trees so the AIG structurally hashes them
    to a single shared gate regardless of which output originally introduced
    the computation.
    """
    n = len(exprs)
    if n <= 1:
        return list(exprs)

    # Collect per-literal occurrence counts across distinct outputs
    lit_out_count: Dict[Tuple[str, bool], int] = {}
    for expr in exprs:
        present: Set[Tuple[str, bool]] = set()
        for t in _sop_terms(expr):
            present |= _term_lits(t)
        for lit in present:
            lit_out_count[lit] = lit_out_count.get(lit, 0) + 1

    # Shared literals: present in 2+ different output expressions
    shared = sorted(
        (lit for lit, cnt in lit_out_count.items() if cnt > 1),
        key=lambda lit: -lit_out_count[lit],   # most-shared first
    )
    if not shared:
        return list(exprs)

    result = list(exprs)
    for lit_key in shared:
        result = [_factor_shared_lit(e, lit_key) for e in result]

    return result