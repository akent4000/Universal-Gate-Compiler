"""
Boolean expression optimisation passes.

  • phase_assign      — choose f or ~f polarity
  • factorize         — algebraic common-subexpression extraction (Brayton)
  • apply_shannon     — Shannon cofactor simplification
  • elim_inv          — redundant double-negation removal
"""

from __future__ import annotations
from functools import reduce
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from ..core.expr      import Expr, Lit, Not, And, Or, ONE, ZERO, simp
from ..core.implicant import (Implicant, espresso, implicants_to_expr,
                              _expand_cubes_to_set, _int_set_to_cubes)


# Cube    = set of (var_name, negated) literals
# Cover   = list of cubes (SOP representation)
Cube  = FrozenSet[Tuple[str, bool]]
Cover = List[Cube]


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase Assignment
# ═══════════════════════════════════════════════════════════════════════════════

def phase_assign(
    on_cubes: List[Tuple[int, ...]],
    dc_cubes: List[Tuple[int, ...]],
    n_vars:   int,
) -> Tuple[List[Implicant], bool]:
    """
    Choose the polarity (f or ~f) that yields fewer literals.

    Returns (implicants, is_complemented).
    If is_complemented is True, the implicants represent ~f and the
    NAND network must add an inverter at the output.

    For n_vars > 20, computing the full off-set is infeasible; the
    on-set polarity is returned unconditionally in that case.
    """
    imps_f = espresso(on_cubes, dc_cubes, n_vars)

    if n_vars > 20:
        return imps_f, False

    on_set  = _expand_cubes_to_set(on_cubes, n_vars)
    dc_set  = _expand_cubes_to_set(dc_cubes, n_vars)
    zeros   = set(range(1 << n_vars)) - on_set - dc_set
    zeros_cubes = _int_set_to_cubes(zeros, n_vars)

    imps_nf = espresso(zeros_cubes, dc_cubes, n_vars)

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

    Kept for reference / fallback; the main `factorize` pipeline uses
    Brayton's kernel-based algorithm below.
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


# ───────────────────────────────────────────────────────────────────────────────
#  Brayton's algebraic factorization (kernels & co-kernels)
# ───────────────────────────────────────────────────────────────────────────────
#
# An SOP expression F is represented as a *cover*: a list of cubes, where each
# cube is a frozenset of (var_name, negated) literal pairs.
#
# Kernel:     a cube-free sub-SOP obtained by algebraic division F / c for
#             some cube c.  "Cube-free" means no single literal is shared by
#             every cube of the kernel.
# Co-kernel:  the cube c used to divide F down to that kernel.
#
# Brayton's theorem: two SOPs share a non-trivial multi-cube algebraic divisor
# iff their kernel sets intersect in a cube-free expression with ≥ 2 cubes.
# For a single SOP, its own kernels (and intersections of its kernels) are
# exactly the candidate multi-cube factors.
#
# Example:  f = a·c + a·d + b·c + b·d
#   • co-kernel a → kernel (c + d)
#   • co-kernel b → kernel (c + d)
#   • co-kernel c → kernel (a + b)
#   • co-kernel d → kernel (a + b)
# Dividing f by (c+d) yields quotient (a+b), remainder 0, so
#   f = (a+b)·(c+d)   —   4 literals instead of 8.


def _term_to_cube(t: Expr) -> Optional[Cube]:
    """Convert a single product term (Lit, And of Lits, or ONE) to a cube."""
    if isinstance(t, Lit):
        return frozenset([(t.name, t.neg)])
    if t == ONE:
        return frozenset()
    if isinstance(t, And):
        lits: Set[Tuple[str, bool]] = set()
        for c in t.args:
            if not isinstance(c, Lit):
                return None
            lits.add((c.name, c.neg))
        return frozenset(lits)
    return None


def _expr_to_cover(expr: Expr) -> Optional[Cover]:
    """Convert an SOP expression to a cover.  Returns None if not pure SOP."""
    if expr == ZERO:
        return []
    if isinstance(expr, Or):
        cover: Cover = []
        for t in expr.args:
            c = _term_to_cube(t)
            if c is None:
                return None
            cover.append(c)
        return cover
    c = _term_to_cube(expr)
    return None if c is None else [c]


def _cube_to_expr(cube: Cube) -> Expr:
    if not cube:
        return ONE
    lits = [Lit(n, neg) for n, neg in sorted(cube)]
    return lits[0] if len(lits) == 1 else And(*lits)


def _cover_to_expr(cover: Cover) -> Expr:
    if not cover:
        return ZERO
    terms = [_cube_to_expr(c) for c in cover]
    return terms[0] if len(terms) == 1 else Or(*terms)


def _largest_common_cube(cover: Cover) -> Cube:
    """Intersection of all cubes in the cover."""
    if not cover:
        return frozenset()
    return reduce(lambda a, b: a & b, cover)


def _algebraic_divide(f: Cover, g: Cover) -> Cover:
    """
    Algebraic quotient q = f / g.

    q is the largest set of cubes such that every product qi·gj
    (i.e. qi ∪ gj treated as a cube) appears in f.  Returns [] if g
    does not algebraically divide f.
    """
    if not g or not f:
        return []
    quotient: Optional[Set[Cube]] = None
    for gi in g:
        partial: Set[Cube] = set()
        for c in f:
            if gi <= c:
                partial.add(c - gi)
        if quotient is None:
            quotient = partial
        else:
            quotient &= partial
        if not quotient:
            return []
    return list(quotient or [])


def _enumerate_kernels(f: Cover, max_cubes: int = 48) -> List[Cover]:
    """
    Enumerate kernels of f via the Brayton-McMullen recursion.

    Each returned cover is cube-free and has ≥ 2 cubes.  Deduplication is
    via a hash set of frozenset(cover).  A size cap keeps pathological SOPs
    from blowing up the recursion.
    """
    if len(f) > max_cubes:
        return []

    all_lits: Set[Tuple[str, bool]] = set()
    for c in f:
        all_lits |= c
    lits_list = sorted(all_lits)
    lit_idx   = {l: i for i, l in enumerate(lits_list)}

    results: List[Cover] = []
    seen:    Set[frozenset] = set()

    def recur(g: Cover, j: int) -> None:
        # Strip the largest common cube so g becomes cube-free.
        common = _largest_common_cube(g)
        if common:
            g = _algebraic_divide(g, [common])
        if len(g) < 2:
            return

        key = frozenset(g)
        if key in seen:
            return
        seen.add(key)
        results.append(list(g))

        # Try each literal (in a canonical order) as the seed for a
        # smaller kernel of g.
        for i in range(j, len(lits_list)):
            lit = lits_list[i]
            cubes_with = [c for c in g if lit in c]
            if len(cubes_with) < 2:
                continue
            co = _largest_common_cube(cubes_with)
            # Canonical-order dedup: skip if the new co-kernel introduces
            # a literal with an index < i (that branch is explored elsewhere).
            if any(lit_idx.get(l, i) < i for l in co):
                continue
            q = _algebraic_divide(g, [co])
            if len(q) >= 2:
                recur(q, i + 1)

    recur(list(f), 0)
    return results


def _multiply_covers(q: Cover, k: Cover) -> Optional[Set[Cube]]:
    """
    Algebraic product q·k as a set of cubes.  Returns None if q and k
    share variable support (then the product is not algebraic).
    """
    q_vars = {n for cube in q for n, _ in cube}
    k_vars = {n for cube in k for n, _ in cube}
    if q_vars & k_vars:
        return None
    product: Set[Cube] = set()
    for qi in q:
        for kj in k:
            product.add(qi | kj)
    return product


def _best_divisor(
    f: Cover,
) -> Optional[Tuple[Cover, Cover, Cover]]:
    """
    Find the kernel k of f that maximises literal savings when we write
    f = q·k + r.  Returns (q, k, r) or None if no profitable kernel exists.
    """
    if len(f) < 2:
        return None

    kernels   = _enumerate_kernels(f)
    orig_lits = sum(len(c) for c in f)
    f_set     = set(f)

    best: Optional[Tuple[Cover, Cover, Cover]] = None
    best_gain = 0

    for k in kernels:
        if len(k) < 2:
            continue
        q = _algebraic_divide(f, k)
        if not q:
            continue
        qk = _multiply_covers(q, k)
        if qk is None or not qk.issubset(f_set):
            continue

        r = [c for c in f if c not in qk]
        new_lits = (sum(len(c) for c in q)
                    + sum(len(c) for c in k)
                    + sum(len(c) for c in r))
        gain = orig_lits - new_lits
        if gain > best_gain or (
            gain == best_gain
            and best is not None
            and (len(q) + len(k) + len(r)) < (len(best[0]) + len(best[1]) + len(best[2]))
        ):
            best_gain = gain
            best      = (q, k, r)

    return best


def _factor_cover(f: Cover) -> Expr:
    """
    Recursively factor a cover:
      1. pull out the largest common cube (single-cube divisor);
      2. pick the best kernel k, write f = q·k + r, recurse on each piece.
    """
    if not f:
        return ZERO
    if len(f) == 1:
        return _cube_to_expr(f[0])

    common = _largest_common_cube(f)
    if common:
        quot = _algebraic_divide(f, [common])
        return simp(And(_cube_to_expr(common), _factor_cover(quot)))

    best = _best_divisor(f)
    if best is None:
        return _cover_to_expr(f)

    q, k, r = best
    q_expr = _factor_cover(q)
    k_expr = _factor_cover(k)
    prod   = simp(And(q_expr, k_expr))
    if r:
        return simp(Or(prod, _factor_cover(r)))
    return prod


def brayton_factor(expr: Expr) -> Expr:
    """
    Apply Brayton's kernel-based algebraic factorization to an SOP.

    Returns the factored expression if it uses strictly fewer literals
    than the input; otherwise returns the input unchanged.  If the input
    is not a pure SOP, falls back to `factorize_once`.
    """
    cover = _expr_to_cover(expr)
    if cover is None:
        return factorize_once(expr)
    if len(cover) < 2:
        return expr

    factored = _factor_cover(cover)
    if factored.literals() < expr.literals():
        return factored
    return expr


def factorize(expr: Expr, rounds: int = 10) -> Expr:
    """
    Algebraic factorization driver.

    Runs Brayton's kernel-based factorizer until a fixed point (or
    `rounds` iterations).  After each Brayton pass we also try the
    literal-greedy `factorize_once`, in case the residual expression
    is no longer a pure SOP and Brayton bailed out.
    """
    for _ in range(rounds):
        new = brayton_factor(expr)
        if new.literals() >= expr.literals():
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