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


# ═════════════════════════════════════════════════════════════════════════════
#  Recursive Ashenhurst-Curtis decomposition
# ═════════════════════════════════════════════════════════════════════════════
#
# After one round of ashenhurst_decompose the resulting h_i (over X_bound) and
# g (over h_names + X_free) are themselves boolean functions and may admit
# further decomposition.  We recurse until either the support becomes small
# enough for the 4-input NPN database to handle (|support| ≤ 4), a depth cap
# is hit, or the literal count stops improving.
#
# The bake-off against the monolithic baseline happens at the AIG level in
# pipeline.py (snapshot/restore on the shared AIG).  Here we only use literal
# counts as a fast local heuristic.

def _expr_ones(expr: Expr, var_names: List[str]) -> Set[int]:
    """Enumerate the ones-set of an Expr over the given variables (MSB-first)."""
    n = len(var_names)
    ones: Set[int] = set()
    for m in range(1 << n):
        asg = {var_names[i]: (m >> (n - 1 - i)) & 1 for i in range(n)}
        if expr.eval(asg):
            ones.add(m)
    return ones


def _build_sop(
    ones:       Set[int],
    dont_cares: Set[int],
    n_vars:     int,
    var_names:  List[str],
) -> Expr:
    """Minimal SOP for the given on-set (handles degenerate cases)."""
    return _synth(ones, dont_cares, n_vars, var_names)


class DecompNode:
    """
    One intermediate signal in a recursive decomposition.

    `name`        — symbolic name; references earlier node names or primary
                    input names inside `expr`.
    `expr`        — Expr tree defining this signal.
    `input_names` — variables used by expr (may mix primary inputs and earlier
                    DecompNode names).  Provided for introspection; consumers
                    can also just walk the Expr and build AIG literals
                    top-to-bottom with a growing var_map.
    """

    __slots__ = ('name', 'expr', 'input_names')

    def __init__(self, name: str, expr: Expr, input_names: List[str]):
        self.name        = name
        self.expr        = expr
        self.input_names = list(input_names)

    def __str__(self) -> str:
        return f'{self.name} = {self.expr}   ({self.expr.literals()} lits)'


class RecursiveDecompositionResult:
    """
    Flat, dependency-ordered sequence of sub-functions produced by recursive
    Ashenhurst-Curtis decomposition.

    Consumer pattern:
        dmap = {}
        for node in result.pre_nodes:
            dmap[node.name] = expr_to_aig(node.expr, aig, var_map=dmap)
        out_lit = expr_to_aig(result.root_expr, aig, var_map=dmap)
    """

    def __init__(
        self,
        pre_nodes:  List[DecompNode],
        root_expr:  Expr,
        root_vars:  List[str],
        orig_lits:  int,
        new_lits:   int,
        depth:      int,
    ):
        self.pre_nodes = pre_nodes
        self.root_expr = root_expr
        self.root_vars = list(root_vars)
        self.orig_lits = orig_lits
        self.new_lits  = new_lits
        self.depth     = depth

    @property
    def savings(self) -> int:
        return self.orig_lits - self.new_lits

    @property
    def n_nodes(self) -> int:
        return len(self.pre_nodes)

    def __str__(self) -> str:
        lines = [
            f'RecursiveDecomposition  depth={self.depth}  '
            f'lits {self.orig_lits} -> {self.new_lits} '
            f'(delta {self.new_lits - self.orig_lits:+})',
        ]
        for n in self.pre_nodes:
            lines.append(f'  {n}')
        lines.append(f'  root({",".join(self.root_vars)}) = {self.root_expr}'
                     f'   ({self.root_expr.literals()} lits)')
        return '\n'.join(lines)


def _decompose_rec(
    ones:              Set[int],
    dont_cares:        Set[int],
    n_vars:            int,
    var_names:         List[str],
    depth:             int,
    max_depth:         int,
    min_literal_gain:  int,
    support_threshold: int,
    k_max:             int,
    min_bound_size:    int,
    max_bound_size:    int,
    emitted:           List[DecompNode],
    name_counter:      List[int],
) -> Tuple[Expr, int]:
    """
    Decompose (ones, dcs) over var_names.  Append any newly-emitted h-nodes
    to `emitted` (dependency-ordered) and return (root_expr, total_lits)
    where total_lits is the sum of literals in every node emitted *by this
    call's subtree* plus root_expr.  If no gainful decomposition exists
    at this node, returns the baseline SOP and zero emitted nodes.
    """
    baseline = _build_sop(ones, dont_cares, n_vars, var_names)
    baseline_lits = baseline.literals()

    if (depth >= max_depth
            or (depth > 0 and n_vars <= support_threshold)
            or not ones
            or len(ones) + len(dont_cares) >= (1 << n_vars)):
        return baseline, baseline_lits

    prefix = f'__rd{depth}_{name_counter[0]}_'
    name_counter[0] += 1
    cand = ashenhurst_decompose(
        ones, dont_cares, n_vars, var_names,
        min_bound_size=min_bound_size,
        max_bound_size=min(max_bound_size, n_vars - 1),
        k_max=k_max,
        h_name_prefix=prefix,
        require_gain=True,
    )
    if cand is None or cand.savings <= 0:
        return baseline, baseline_lits

    # Speculative: try to recurse on each h_i and on g.  Collect into a local
    # buffer; only commit if the sum beats the baseline.
    local: List[DecompNode] = []
    total_lits = 0

    for h_name, h_expr, h_lits in zip(
        cand.h_names, cand.h_exprs,
        [he.literals() for he in cand.h_exprs],
    ):
        nb = len(cand.bound_vars)
        if nb <= support_threshold or h_lits == 0:
            # Small enough already — keep as a leaf.
            local.append(DecompNode(h_name, h_expr, cand.bound_vars))
            total_lits += h_lits
            continue

        h_ones = _expr_ones(h_expr, cand.bound_vars)
        sub_root, sub_lits = _decompose_rec(
            h_ones, set(), nb, cand.bound_vars,
            depth + 1, max_depth, min_literal_gain, support_threshold,
            k_max, min_bound_size, max_bound_size,
            local, name_counter,
        )
        local.append(DecompNode(h_name, sub_root, cand.bound_vars))
        total_lits += sub_lits

    g_vars  = list(cand.h_names) + list(cand.free_vars)
    g_nvars = len(g_vars)
    g_lits_flat = cand.g_expr.literals()

    if g_nvars <= support_threshold or g_lits_flat == 0:
        g_root = cand.g_expr
        g_lits = g_lits_flat
    else:
        g_ones = _expr_ones(cand.g_expr, g_vars)
        g_root, g_lits = _decompose_rec(
            g_ones, set(), g_nvars, g_vars,
            depth + 1, max_depth, min_literal_gain, support_threshold,
            k_max, min_bound_size, max_bound_size,
            local, name_counter,
        )

    total_lits += g_lits

    # Whole-subtree bake-off: if recursion did not beat the flat baseline, fall back.
    if total_lits >= baseline_lits:
        return baseline, baseline_lits

    emitted.extend(local)
    return g_root, total_lits


def ashenhurst_decompose_recursive(
    ones:              Set[int],
    dont_cares:        Set[int],
    n_vars:            int,
    var_names:         List[str],
    min_bound_size:    int  = 2,
    max_bound_size:    int  = 5,
    k_max:             int  = 3,
    max_depth:         int  = 4,
    min_literal_gain:  int  = 2,
    support_threshold: int  = 4,
    require_gain:      bool = True,
) -> Optional[RecursiveDecompositionResult]:
    """
    Recursive Ashenhurst-Curtis / Roth-Karp decomposition.

    After a successful single-level decomposition  f = g(h_1..h_k, X_free),
    each h_i (over X_bound) and g (over h_names + X_free) is recursively
    decomposed until |support| ≤ support_threshold (default 4 — where
    the 4-input NPN database takes over), depth hits max_depth, or the
    literal gain dips below min_literal_gain.

    Returns None if no profitable decomposition exists at the top level.
    The returned result is consumed by the AIG builder, which performs a
    whole-tree snapshot/restore bake-off against the monolithic baseline.
    """
    if not ones or n_vars < 3:
        return None
    if len(ones) + len(dont_cares) >= (1 << n_vars):
        return None

    baseline_lits = _baseline_literals(ones, dont_cares, n_vars, var_names)

    emitted:      List[DecompNode] = []
    name_counter: List[int]        = [0]

    root_expr, total_lits = _decompose_rec(
        ones, dont_cares, n_vars, var_names,
        depth             = 0,
        max_depth         = max_depth,
        min_literal_gain  = min_literal_gain,
        support_threshold = support_threshold,
        k_max             = k_max,
        min_bound_size    = min_bound_size,
        max_bound_size    = max_bound_size,
        emitted           = emitted,
        name_counter      = name_counter,
    )

    # Actual depth of the tree (max over emitted node prefixes) — for display.
    max_emitted_depth = 0
    for node in emitted:
        # Prefix format: __rd{depth}_{counter}_...
        if node.name.startswith('__rd'):
            try:
                d = int(node.name[4:].split('_', 1)[0])
                if d + 1 > max_emitted_depth:
                    max_emitted_depth = d + 1
            except ValueError:
                pass

    if require_gain and (len(emitted) == 0 or total_lits >= baseline_lits):
        return None

    return RecursiveDecompositionResult(
        pre_nodes = emitted,
        root_expr = root_expr,
        root_vars = var_names,
        orig_lits = baseline_lits,
        new_lits  = total_lits,
        depth     = max_emitted_depth,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Shared-support decomposition across multiple outputs
# ═════════════════════════════════════════════════════════════════════════════
#
# For a vector of outputs (f_1, ..., f_n) with common inputs, we search for a
# single bipartition X = X_bound ∪ X_free and a single set of h-functions
# h_1..h_k over X_bound such that every f_j factors as
#
#     f_j(X) = g_j( h_1(X_bound), ..., h_k(X_bound),  X_free )
#
# The joint decomposition chart has one row per X_bound assignment and
# (n_outputs × 2^|X_free|) columns — one column per (output index, X_free
# assignment) pair.  Column multiplicity μ over this widened chart tells us
# the minimum k that serves ALL outputs simultaneously; each g_j is then
# synthesised against its per-output slice of the clustered reps.
#
# This is the critical pass for the bin→BCD→7seg benchmark gap: the 4-bit
# BCD bus is discovered as four h-functions shared by every 7-segment output.

class SharedDecompositionResult:
    """Outcome of a successful multi-output shared-support decomposition."""

    def __init__(
        self,
        bound_vars: List[str],
        free_vars:  List[str],
        h_exprs:    List[Expr],
        h_names:    List[str],
        g_exprs:    List[Expr],
        output_names: List[str],
        mu:         int,
        orig_lits:  int,
        new_lits:   int,
    ):
        self.bound_vars   = bound_vars
        self.free_vars    = free_vars
        self.h_exprs      = h_exprs
        self.h_names      = h_names
        self.g_exprs      = g_exprs
        self.output_names = output_names
        self.mu           = mu
        self.orig_lits    = orig_lits
        self.new_lits     = new_lits

    @property
    def k(self) -> int:
        return len(self.h_exprs)

    @property
    def n_outputs(self) -> int:
        return len(self.g_exprs)

    @property
    def savings(self) -> int:
        return self.orig_lits - self.new_lits

    def __str__(self) -> str:
        lines = [
            f'Shared decomposition over {self.n_outputs} outputs',
            f'  X_bound = {self.bound_vars}    X_free = {self.free_vars}',
            f'  mu = {self.mu},  k = {self.k}',
        ]
        for name, expr in zip(self.h_names, self.h_exprs):
            lines.append(f'  {name:<6} = {expr}   ({expr.literals()} lits)')
        for oname, gexpr in zip(self.output_names, self.g_exprs):
            lines.append(f'  {oname:<6} = {gexpr}   ({gexpr.literals()} lits)')
        lines.append(
            f'  literals {self.orig_lits} -> {self.new_lits} '
            f'(delta {self.new_lits - self.orig_lits:+})'
        )
        return '\n'.join(lines)


def _joint_probe_mu(
    outputs_ones: List[Set[int]],
    dont_cares:   Set[int],
    bound_bits:   List[int],
    free_bits:    List[int],
    k_max:        int,
) -> Tuple[int, List[int], List[List[int]]]:
    """
    Build the joint decomposition chart over all outputs.

    Row b is the concatenation of every output's (2^|X_free|)-wide slice;
    rows are clustered with the same don't-care-aware compatibility rule
    as the single-output probe.  Aborts early if the running cluster count
    exceeds 2^k_max.

    Returns (mu, codes[], reps_per_row) — reps are resolved to 0/1
    (don't-cares filled with 0), ordered so that reps[code][j*2^nf + f_val]
    is the clustered value of output j at (code, f_val).
    """
    nb = len(bound_bits)
    nf = len(free_bits)
    nj = len(outputs_ones)
    cap = 1 << k_max
    row_w = nj * (1 << nf)

    codes: List[int]        = [0] * (1 << nb)
    reps:  List[List[Cell]] = []

    for b in range(1 << nb):
        row: List[Cell] = [0] * row_w
        for f in range(1 << nf):
            m = _compose_minterm(b, f, bound_bits, free_bits)
            if m in dont_cares:
                for j in range(nj):
                    row[j * (1 << nf) + f] = None
            else:
                for j in range(nj):
                    row[j * (1 << nf) + f] = 1 if m in outputs_ones[j] else 0

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


def _try_joint_bipartition(
    outputs_ones:  List[Set[int]],
    dont_cares:    Set[int],
    n_vars:        int,
    var_names:     List[str],
    output_names:  List[str],
    bound_indices: Tuple[int, ...],
    k_max:         int,
    h_name_prefix: str,
    baseline_lits: int,
    per_output_baseline: List[int],
) -> Optional[SharedDecompositionResult]:
    """Attempt one shared bipartition; return result if μ ≤ 2^k_max, k < nb."""
    free_indices = tuple(i for i in range(n_vars) if i not in bound_indices)

    bound_bits   = [_var_bit(i, n_vars) for i in bound_indices]
    free_bits    = [_var_bit(i, n_vars) for i in free_indices]

    nb = len(bound_indices)
    nf = len(free_indices)
    nj = len(outputs_ones)

    mu, codes, reps = _joint_probe_mu(
        outputs_ones, dont_cares, bound_bits, free_bits, k_max,
    )
    if mu < 2 or mu > (1 << k_max):
        return None

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
    h_lits = sum(h.literals() for h in h_exprs_msb_first)

    # Per-output reps: reps_j[code][f_val] = reps[code][j*2^nf + f_val]
    g_exprs: List[Expr] = []
    g_total_lits = 0
    free_w = 1 << nf
    for j in range(nj):
        reps_j: List[List[int]] = [
            reps[c][j * free_w : (j + 1) * free_w]
            for c in range(mu)
        ]
        g_j = _build_g(reps_j, mu, k, nf, h_names, free_names)
        g_exprs.append(g_j)
        g_total_lits += g_j.literals()

    new_lits = h_lits + g_total_lits

    return SharedDecompositionResult(
        bound_vars = bound_names,
        free_vars  = free_names,
        h_exprs    = h_exprs_msb_first,
        h_names    = h_names,
        g_exprs    = g_exprs,
        output_names = list(output_names),
        mu         = mu,
        orig_lits  = baseline_lits,
        new_lits   = new_lits,
    )


def multi_output_decompose(
    outputs_ones:   List[Set[int]],
    dont_cares:     Set[int],
    n_vars:         int,
    var_names:      List[str],
    output_names:   Optional[List[str]] = None,
    min_bound_size: int  = 2,
    max_bound_size: int  = 8,
    k_max:          int  = 4,
    h_name_prefix:  str  = 'mh',
    require_gain:   bool = True,
) -> Optional[SharedDecompositionResult]:
    """
    Shared-support Ashenhurst-Curtis decomposition for a group of outputs.

    For each bipartition of inputs, a single joint decomposition chart is
    built over *all* outputs and a single set of h-functions is searched.
    When successful, the returned h_exprs are physically shared across
    every g_j — the compiler has recovered an intermediate bus that lives
    above the individual outputs (e.g. the 4-bit BCD bus for a multi-digit
    7-segment decoder).

    Parameters
    ----------
    outputs_ones
        One ones-set per output (minterm indices).
    dont_cares
        Global don't-care minterms shared by every output.
    n_vars, var_names
        Input dimension and names (MSB-first).
    output_names
        Names of the outputs, solely for pretty-printing.
    min_bound_size, max_bound_size
        Search window over |X_bound|.
    k_max
        Maximum number of shared h-functions (μ ≤ 2^k_max).  Defaults to 4
        to comfortably host a 4-bit intermediate bus (e.g. BCD).
    require_gain
        If True, only return the decomposition when its literal count is
        strictly below the aggregate monolithic baseline.
    """
    if not outputs_ones or n_vars < 3:
        return None
    nj = len(outputs_ones)
    if output_names is None:
        output_names = [f'f{j}' for j in range(nj)]
    if len(output_names) != nj:
        raise ValueError('output_names length must match outputs_ones length')

    # Skip completely empty / completely full outputs when they can't benefit
    # the search (but keep them so indices align).
    per_output_baseline: List[int] = [
        _baseline_literals(outputs_ones[j], dont_cares, n_vars, var_names)
        for j in range(nj)
    ]
    baseline_lits = sum(per_output_baseline)
    if baseline_lits == 0:
        return None

    lo = max(2, min_bound_size)
    hi = min(n_vars - 1, max_bound_size)
    if lo > hi:
        return None

    best: Optional[SharedDecompositionResult] = None

    for r in range(lo, hi + 1):
        for bound_indices in combinations(range(n_vars), r):
            cand = _try_joint_bipartition(
                outputs_ones, dont_cares, n_vars, var_names, output_names,
                bound_indices, k_max, h_name_prefix,
                baseline_lits, per_output_baseline,
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
