"""
Ashenhurst-Curtis / Roth-Karp functional decomposition.

Search for a bipartition  X = X_bound ∪ X_free  of the input variables and
k ≥ 1 auxiliary subfunctions such that

    f(X) = g( h_1(X_bound), ..., h_k(X_bound),  X_free )

The classical Ashenhurst form is k = 1 (simple disjoint decomposition); the
Curtis / Roth-Karp generalisation allows k > 1.

Algorithm (Roth-Karp column multiplicity test)
----------------------------------------------
For a fixed bipartition we build the decomposition chart — a 2^|X_bound| ×
2^|X_free| grid whose rows are indexed by bound-variable assignments and
columns by free-variable assignments.  The entry at (b, f) is f(b, f),
with don't-care minterms marked None.

  μ = column multiplicity = number of distinct row patterns
      (rows with don't-care cells are greedily merged into the first
       compatible cluster).

  A decomposition with k output bits of h exists  ⇔  μ ≤ 2^k.

If μ satisfies this, each row b is assigned a k-bit code so that
  h_i(b) = bit i of code(b).
Then g is synthesised as an SOP over (h_{k-1}, ..., h_0, X_free).

We try every bipartition with |X_bound| in a configurable window and
return the decomposition that saves the most literals versus the
baseline single-output Espresso SOP.
"""

from __future__ import annotations
from itertools import combinations
from typing    import Dict, List, Optional, Set, Tuple

from .expr      import Expr, Lit, And, Or, ZERO, ONE
from .implicant import espresso, implicants_to_expr, _int_set_to_cubes


Cell = Optional[int]   # 0, 1, or None (don't-care)


# ═════════════════════════════════════════════════════════════════════════════
#  Result container
# ═════════════════════════════════════════════════════════════════════════════

class DecompositionResult:
    """Outcome of a successful Ashenhurst-Curtis decomposition."""

    def __init__(
        self,
        bound_vars: List[str],
        free_vars:  List[str],
        h_exprs:    List[Expr],
        h_names:    List[str],
        g_expr:     Expr,
        mu:         int,
        orig_lits:  int,
        new_lits:   int,
    ):
        self.bound_vars = bound_vars
        self.free_vars  = free_vars
        self.h_exprs    = h_exprs
        self.h_names    = h_names
        self.g_expr     = g_expr
        self.mu         = mu
        self.orig_lits  = orig_lits
        self.new_lits   = new_lits

    @property
    def k(self) -> int:
        return len(self.h_exprs)

    @property
    def savings(self) -> int:
        return self.orig_lits - self.new_lits

    def __str__(self) -> str:
        lines = [
            f'Decomposition  f = g( h(X_bound),  X_free )',
            f'  X_bound = {self.bound_vars}    X_free = {self.free_vars}',
            f'  column multiplicity mu = {self.mu},  k = {self.k}',
        ]
        for name, expr in zip(self.h_names, self.h_exprs):
            lines.append(f'  {name:<4} = {expr}   (literals: {expr.literals()})')
        lines.append(f'  g    = {self.g_expr}   (literals: {self.g_expr.literals()})')
        lines.append(
            f'  literals {self.orig_lits} -> {self.new_lits} '
            f'(delta {self.new_lits - self.orig_lits:+})'
        )
        return '\n'.join(lines)


# ═════════════════════════════════════════════════════════════════════════════
#  Bit-position mapping
# ═════════════════════════════════════════════════════════════════════════════
#
# Convention used throughout the project (see Implicant.int_to_bits):
#   var_names[i] maps to minterm bit (n_vars - 1 - i)  -- MSB-first.
#
# For a bipartition the bound/free "sub-minterm" value b (or f) is also
# MSB-first relative to its sub-list:
#   bound_names[j] maps to bit (nb - 1 - j) of b.

def _var_bit(i: int, n_vars: int) -> int:
    return n_vars - 1 - i


def _compose_minterm(
    b:          int,
    f:          int,
    bound_bits: List[int],
    free_bits:  List[int],
) -> int:
    """Assemble the full n_vars-wide minterm index from sub-values."""
    m  = 0
    nb = len(bound_bits)
    nf = len(free_bits)
    for j, pos in enumerate(bound_bits):
        if (b >> (nb - 1 - j)) & 1:
            m |= 1 << pos
    for j, pos in enumerate(free_bits):
        if (f >> (nf - 1 - j)) & 1:
            m |= 1 << pos
    return m


# ═════════════════════════════════════════════════════════════════════════════
#  Decomposition chart & row clustering
# ═════════════════════════════════════════════════════════════════════════════

def _rows_compatible(a: List[Cell], b: List[Cell]) -> bool:
    """Rows match on every cell where neither is a don't-care."""
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        if x != y:
            return False
    return True


# ═════════════════════════════════════════════════════════════════════════════
#  Subfunction synthesis
# ═════════════════════════════════════════════════════════════════════════════

def _synth(
    ones:       Set[int],
    dont_cares: Set[int],
    n_vars:     int,
    var_names:  List[str],
) -> Expr:
    """Minimal SOP for the given on-set (handles degenerate cases)."""
    total = 1 << n_vars
    if not ones:
        return ZERO
    if len(ones) + len(dont_cares) >= total:
        return ONE
    imps = espresso(_int_set_to_cubes(ones, n_vars),
                    _int_set_to_cubes(dont_cares, n_vars),
                    n_vars)
    return implicants_to_expr(imps, var_names) if imps else ZERO


def _encode_h(
    codes:       List[int],
    k:           int,
    nb:          int,
    bound_names: List[str],
) -> List[Expr]:
    """Build h_0..h_{k-1} over X_bound; h_i(b) = bit i of codes[b]."""
    h_exprs: List[Expr] = []
    for i in range(k):
        ones_set: Set[int] = {
            b for b in range(1 << nb)
            if (codes[b] >> i) & 1
        }
        h_exprs.append(_synth(ones_set, set(), nb, bound_names))
    return h_exprs


def _build_g(
    reps:       List[List[int]],
    mu:         int,
    k:          int,
    nf:         int,
    h_names:    List[str],
    free_names: List[str],
) -> Expr:
    """
    Synthesize g over (h_{k-1}, ..., h_0, X_free).

    g-variable order uses MSB-first bit indexing:
      - g_vars[0] = h_{k-1}  ->  g-minterm bit (k + nf - 1)
      - g_vars[k] = free_names[0] -> bit (nf - 1)
      - g_vars[-1] = free_names[-1] -> bit 0

    For a (code, f_val) pair the g-minterm is  m_g = (code << nf) | f_val,
    which satisfies  (m_g >> (nf + i)) & 1 == (code >> i) & 1 == h_i.
    """
    # h_names is given in order [h_{k-1}, ..., h_0] so that variable 0
    # (MSB-most) is h_{k-1} in the output SOP.
    g_vars   = list(h_names) + list(free_names)
    n_total  = k + nf

    ones_g: Set[int] = set()
    dc_g:   Set[int] = set()

    for code in range(1 << k):
        for f_val in range(1 << nf):
            m_g = (code << nf) | f_val
            if code < mu:
                if reps[code][f_val]:
                    ones_g.add(m_g)
            else:
                # Unused code — don't-care everywhere.
                dc_g.add(m_g)

    return _synth(ones_g, dc_g, n_total, g_vars)


# ═════════════════════════════════════════════════════════════════════════════
#  Top-level search
# ═════════════════════════════════════════════════════════════════════════════

def _baseline_literals(
    ones:       Set[int],
    dont_cares: Set[int],
    n_vars:     int,
    var_names:  List[str],
) -> int:
    """Literal count of the raw (non-decomposed) SOP."""
    if not ones:
        return 0
    if len(ones) + len(dont_cares) >= (1 << n_vars):
        return 0
    imps = espresso(_int_set_to_cubes(ones, n_vars),
                    _int_set_to_cubes(dont_cares, n_vars),
                    n_vars)
    return sum(p.literal_count() for p in imps)


def _probe_mu(
    ones:       Set[int],
    dont_cares: Set[int],
    bound_bits: List[int],
    free_bits:  List[int],
    k_max:      int,
) -> Tuple[int, List[int], List[List[int]]]:
    """
    Build the chart & cluster rows; abort early (mu = 1<<30) as soon as
    the running cluster count exceeds 2**k_max.  Avoids paying the full
    clustering cost for bipartitions that cannot yield a usable k.
    """
    nb  = len(bound_bits)
    nf  = len(free_bits)
    cap = 1 << k_max

    codes: List[int]        = [0] * (1 << nb)
    reps:  List[List[Cell]] = []

    for b in range(1 << nb):
        row: List[Cell] = []
        for f in range(1 << nf):
            m = _compose_minterm(b, f, bound_bits, free_bits)
            if m in ones:
                row.append(1)
            elif m in dont_cares:
                row.append(None)
            else:
                row.append(0)

        assigned = -1
        for idx, rep in enumerate(reps):
            if _rows_compatible(row, rep):
                reps[idx] = [
                    (rc if rc is not None else rw)
                    for rc, rw in zip(rep, row)
                ]
                assigned = idx
                break
        if assigned < 0:
            reps.append(row)
            codes[b] = len(reps) - 1
            if len(reps) > cap:
                return 1 << 30, codes, []
        else:
            codes[b] = assigned

    resolved = [
        [(c if c is not None else 0) for c in rep]
        for rep in reps
    ]
    return len(reps), codes, resolved


def _try_bipartition(
    ones:           Set[int],
    dont_cares:     Set[int],
    n_vars:         int,
    var_names:      List[str],
    bound_indices:  Tuple[int, ...],
    k_max:          int,
    h_name_prefix:  str,
    baseline_lits:  int,
) -> Optional[DecompositionResult]:
    """Attempt a single bipartition; return result if mu ≤ 2^k_max and k < nb."""
    free_indices = tuple(i for i in range(n_vars) if i not in bound_indices)

    bound_bits   = [_var_bit(i, n_vars) for i in bound_indices]
    free_bits    = [_var_bit(i, n_vars) for i in free_indices]

    nb = len(bound_indices)
    nf = len(free_indices)

    mu, codes, reps = _probe_mu(ones, dont_cares, bound_bits, free_bits, k_max)
    if mu < 2 or mu > (1 << k_max):
        return None

    # Minimum k such that 2^k >= mu
    k = 1
    while (1 << k) < mu:
        k += 1
    if k >= nb:
        return None

    bound_names = [var_names[i] for i in bound_indices]
    free_names  = [var_names[i] for i in free_indices]
    h_names     = [f'{h_name_prefix}{k - 1 - i}' for i in range(k)]

    h_exprs_lsb_first = _encode_h(codes, k, nb, bound_names)
    h_exprs_msb_first = list(reversed(h_exprs_lsb_first))

    g_expr = _build_g(reps, mu, k, nf, h_names, free_names)

    new_lits = g_expr.literals() + sum(h.literals() for h in h_exprs_msb_first)

    return DecompositionResult(
        bound_vars = bound_names,
        free_vars  = free_names,
        h_exprs    = h_exprs_msb_first,
        h_names    = h_names,
        g_expr     = g_expr,
        mu         = mu,
        orig_lits  = baseline_lits,
        new_lits   = new_lits,
    )


def ashenhurst_decompose(
    ones:           Set[int],
    dont_cares:     Set[int],
    n_vars:         int,
    var_names:      List[str],
    min_bound_size: int = 2,
    max_bound_size: int = 5,
    k_max:          int = 3,
    h_name_prefix:  str = 'h',
    require_gain:   bool = True,
) -> Optional[DecompositionResult]:
    """
    Search every bipartition X = X_bound ∪ X_free with
    min_bound_size ≤ |X_bound| ≤ max_bound_size and return the
    decomposition that saves the most literals.

    Parameters
    ----------
    ones, dont_cares, n_vars, var_names
        Definition of the function to decompose.
    min_bound_size, max_bound_size
        Search window over |X_bound|.  Clamped to [2, n_vars-1] internally.
    k_max
        Maximum number of h-subfunctions.  A bipartition is discarded if
        mu > 2^k_max (the column multiplicity is too high).
    h_name_prefix
        Auxiliary variable name prefix (default 'h' → h0, h1, ...).
    require_gain
        If True, only decompositions with strictly fewer literals than the
        baseline SOP are returned.  Set False to inspect any decomposition
        that the chart-multiplicity test admits.
    """
    if not ones or n_vars < 3:
        return None

    lo = max(2, min_bound_size)
    hi = min(n_vars - 1, max_bound_size)
    if lo > hi:
        return None

    # Pay the baseline Espresso cost once and reuse across all bipartitions.
    baseline_lits = _baseline_literals(ones, dont_cares, n_vars, var_names)

    best: Optional[DecompositionResult] = None

    for r in range(lo, hi + 1):
        for bound_indices in combinations(range(n_vars), r):
            cand = _try_bipartition(
                ones, dont_cares, n_vars, var_names,
                bound_indices, k_max, h_name_prefix,
                baseline_lits,
            )
            if cand is None:
                continue
            if require_gain and cand.savings <= 0:
                continue
            if best is None or cand.savings > best.savings or (
                cand.savings == best.savings
                and cand.new_lits < best.new_lits
            ):
                best = cand

    return best


def decompose_expr(
    expr:           Expr,
    var_names:      List[str],
    min_bound_size: int = 2,
    max_bound_size: int = 6,
    k_max:          int = 3,
    h_name_prefix:  str = 'h',
) -> Optional[DecompositionResult]:
    """
    Convenience wrapper: decompose a fully-specified Expr (no don't-cares)
    whose support is a subset of var_names.

    Returns None if no profitable decomposition is found.
    """
    n_vars = len(var_names)
    ones:  Set[int] = set()
    for m in range(1 << n_vars):
        asg = {var_names[i]: (m >> (n_vars - 1 - i)) & 1 for i in range(n_vars)}
        if expr.eval(asg):
            ones.add(m)
    return ashenhurst_decompose(
        ones, set(), n_vars, var_names,
        min_bound_size=min_bound_size,
        max_bound_size=max_bound_size,
        k_max=k_max,
        h_name_prefix=h_name_prefix,
    )
