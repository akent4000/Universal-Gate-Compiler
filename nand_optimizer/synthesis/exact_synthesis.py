"""
Exact Synthesis (SAT-based Boolean Matching).

Given a truth table of a Boolean function on up to ~6 variables, find the
provably minimum number of 2-input AND gates (with complemented edges, i.e.
an AIG) that implements the function.

The result is returned in the same format used by :mod:`aig_db_4`:

    (output_lit, [(a_lit, b_lit), ...])

Literal encoding (matches ``AIG_DB_4``):

    0              -> FALSE
    1              -> TRUE
    2 + 2*i        -> primary input i (positive)
    3 + 2*i        -> primary input i (negative)
    2 + 2*n + 2*j  -> gate j positive output
    3 + 2*n + 2*j  -> gate j negative output

Encoding
--------
For each minterm ``m`` and each gate ``g``, we introduce a Boolean
``v_{g,m}`` representing the gate's output at that minterm. Port selectors
use one-hot Boolean encodings, which SAT solvers handle much more
efficiently than integer If-cascades over BitVecs.

Results are cached in-memory keyed by ``(n_inputs, tt, dc)`` so repeated
queries during rewriting are O(1).
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache
# ═══════════════════════════════════════════════════════════════════════════════

Template = Tuple[int, List[Tuple[int, int]]]
_EXACT_CACHE: Dict[Tuple[int, int, int], Optional[Template]] = {}

MAX_DEFAULT_GATES = 6
DEFAULT_TIMEOUT_MS = 5000


def exact_cache_stats() -> Dict[str, int]:
    solved   = sum(1 for v in _EXACT_CACHE.values() if v is not None)
    unsolved = sum(1 for v in _EXACT_CACHE.values() if v is None)
    return {'entries': len(_EXACT_CACHE), 'solved': solved, 'unsolved': unsolved}


def exact_cache_clear() -> None:
    _EXACT_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════════════

def exact_synthesize(
    tt:         int,
    n_inputs:   int,
    max_gates:  int  = MAX_DEFAULT_GATES,
    dc:         int  = 0,
    timeout_ms: int  = DEFAULT_TIMEOUT_MS,
) -> Optional[Template]:
    """
    Return a provably minimum-size AIG template for ``tt``, or ``None`` when
    no template with at most ``max_gates`` AND gates exists within the
    solver's ``timeout_ms`` budget (also ``None`` if Z3 is not installed).

    Parameters
    ----------
    tt : int
        Truth table as an integer. Bit m is the value at minterm m.
    n_inputs : int
        Number of primary inputs (1..6 practically).
    max_gates : int
        Upper bound on gate count. The solver starts at 1 and climbs.
    dc : int
        Don't-care mask (bit m = 1 -> minterm m is a don't care).
    timeout_ms : int
        Per-k Z3 timeout in milliseconds. A timeout on any ``k`` returns
        ``None`` without proving unsat.
    """
    if n_inputs < 0:
        return None

    V    = 1 << n_inputs
    mask = (1 << V) - 1
    tt   = tt & mask
    dc   = dc & mask

    key = (n_inputs, tt, dc)
    if key in _EXACT_CACHE:
        return _EXACT_CACHE[key]

    trivial = _try_trivial(tt, dc, n_inputs, V)
    if trivial is not None:
        _EXACT_CACHE[key] = trivial
        return trivial

    try:
        import z3  # noqa: F401
    except ImportError:
        _EXACT_CACHE[key] = None
        return None

    for k in range(1, max_gates + 1):
        status, res = _solve_k(n_inputs, tt, dc, V, k, timeout_ms)
        if status == 'sat':
            _EXACT_CACHE[key] = res
            return res
        if status == 'timeout':
            # Give up — caller can retry with a larger timeout if desired.
            _EXACT_CACHE[key] = None
            return None
        # 'unsat' -> try k+1

    _EXACT_CACHE[key] = None
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Trivial solutions (0 gates)
# ═══════════════════════════════════════════════════════════════════════════════

def _input_tt(i: int, n_inputs: int) -> int:
    V  = 1 << n_inputs
    tt = 0
    for m in range(V):
        if (m >> i) & 1:
            tt |= (1 << m)
    return tt


def _try_trivial(tt: int, dc: int, n_inputs: int, V: int) -> Optional[Template]:
    all_ones  = (1 << V) - 1
    care_mask = all_ones & ~dc

    if (tt & care_mask) == 0:
        return (0, [])
    if ((all_ones ^ tt) & care_mask) == 0:
        return (1, [])
    for i in range(n_inputs):
        x_tt = _input_tt(i, n_inputs)
        if ((x_tt ^ tt) & care_mask) == 0:
            return (2 + 2 * i, [])
        if (((all_ones ^ x_tt) ^ tt) & care_mask) == 0:
            return (3 + 2 * i, [])
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Z3-based exact synthesis for fixed ``k`` AND gates
# ═══════════════════════════════════════════════════════════════════════════════

def _solve_k(
    n_inputs:   int,
    tt:         int,
    dc:         int,
    V:          int,
    k:          int,
    timeout_ms: int,
) -> Tuple[str, Optional[Template]]:
    """
    Encode the exact-synthesis query with ``k`` AND gates as a per-minterm
    Boolean formula. Returns one of:
        ('sat',    (out_lit, ops))
        ('unsat',  None)
        ('timeout', None)
    """
    import z3

    # ─── Per-minterm gate values ────────────────────────────────────────────────
    # v[g][m] = Boolean = value of gate g at minterm m
    v = [[z3.Bool(f'v_{g}_{m}') for m in range(V)] for g in range(k)]

    # ─── One-hot source selectors per (gate, port) ─────────────────────────────
    # sel[g][p][s] = Bool, true iff gate g's port p reads from source s.
    # Sources at gate g: 0..n_inputs-1 = primary inputs; n_inputs..n_inputs+g-1
    # = outputs of earlier gates.  We declare one Bool per potential source.
    sel: List[List[List[z3.BoolRef]]] = []
    neg: List[List[z3.BoolRef]] = []
    for g in range(k):
        limit = n_inputs + g
        port_sels = []
        for p in (0, 1):
            s_vars = [z3.Bool(f's_{g}_{p}_{s}') for s in range(limit)]
            port_sels.append(s_vars)
        sel.append(port_sels)
        neg.append([z3.Bool(f'n_{g}_0'), z3.Bool(f'n_{g}_1')])

    # Output source/negation selector, also one-hot.
    out_sel = [z3.Bool(f'o_{s}') for s in range(n_inputs + k)]
    out_neg = z3.Bool('o_neg')

    solver = z3.Solver()
    solver.set('timeout', timeout_ms)

    # Helper: exactly-one over a list of Bools.
    def exactly_one(bs):
        # At least one
        solver.add(z3.Or(*bs))
        # At most one (pairwise)
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                solver.add(z3.Or(z3.Not(bs[i]), z3.Not(bs[j])))

    for g in range(k):
        exactly_one(sel[g][0])
        exactly_one(sel[g][1])
    exactly_one(out_sel)
    if k >= 1:
        # Output must be from a gate (trivial solutions already handled).
        for s in range(n_inputs):
            solver.add(z3.Not(out_sel[s]))

    # Symmetry breaking: l < r as source index.
    # For each gate g, enforce "left port picks index < right port's index".
    for g in range(k):
        limit = n_inputs + g
        # For every pair (s_l, s_r) with s_l >= s_r, block the joint selection.
        for sl in range(limit):
            for sr in range(limit):
                if sl >= sr:
                    solver.add(z3.Or(z3.Not(sel[g][0][sl]),
                                      z3.Not(sel[g][1][sr])))

    # Per-gate constraint:  v[g][m] == (left@m AND right@m)
    # Build expressions for left and right values at each minterm, conditioned
    # on the one-hot selectors.
    def port_value_at(g: int, p: int, m: int) -> z3.BoolRef:
        """Boolean expression for port p of gate g at minterm m."""
        limit = n_inputs + g
        terms = []
        for s in range(limit):
            if s < n_inputs:
                bit = 1 if (m >> s) & 1 else 0
                src_val = z3.BoolVal(bool(bit))
            else:
                src_val = v[s - n_inputs][m]
            # If this selector is active, the (possibly negated) source's value.
            terms.append(
                z3.And(sel[g][p][s],
                        z3.Xor(src_val, neg[g][p]))
            )
        # exactly_one(sel) guarantees precisely one term is active.
        return z3.Or(*terms) if terms else z3.BoolVal(False)

    for g in range(k):
        for m in range(V):
            lv = port_value_at(g, 0, m)
            rv = port_value_at(g, 1, m)
            solver.add(v[g][m] == z3.And(lv, rv))

    # Output constraint per minterm (skip don't-cares).
    def output_at(m: int) -> z3.BoolRef:
        """Boolean expression for the final output at minterm m."""
        terms = []
        for s in range(n_inputs + k):
            if s < n_inputs:
                bit = 1 if (m >> s) & 1 else 0
                src_val = z3.BoolVal(bool(bit))
            else:
                src_val = v[s - n_inputs][m]
            terms.append(z3.And(out_sel[s], z3.Xor(src_val, out_neg)))
        return z3.Or(*terms) if terms else z3.BoolVal(False)

    for m in range(V):
        if (dc >> m) & 1:
            continue
        expected = bool((tt >> m) & 1)
        o = output_at(m)
        solver.add(o == z3.BoolVal(expected))

    result = solver.check()
    if result == z3.unsat:
        return ('unsat', None)
    if result != z3.sat:
        return ('timeout', None)

    # ─── Decode the model into a template ──────────────────────────────────────
    model = solver.model()

    def decode_onehot(bs: List[z3.BoolRef]) -> int:
        for i, b in enumerate(bs):
            if z3.is_true(model[b]):
                return i
        # Should not happen under exactly-one semantics.
        return 0

    def decode_bool(b: z3.BoolRef) -> bool:
        return z3.is_true(model[b])

    lit_of_src: List[int] = [0] * (n_inputs + k)
    for i in range(n_inputs):
        lit_of_src[i] = 2 + 2 * i
    op_base = 2 + 2 * n_inputs

    ops: List[Tuple[int, int]] = []
    for g in range(k):
        ls = decode_onehot(sel[g][0])
        rs = decode_onehot(sel[g][1])
        ln = decode_bool(neg[g][0])
        rn = decode_bool(neg[g][1])
        a = lit_of_src[ls] ^ (1 if ln else 0)
        b = lit_of_src[rs] ^ (1 if rn else 0)
        ops.append((a, b))
        lit_of_src[n_inputs + g] = op_base + 2 * g

    os_idx = decode_onehot(out_sel)
    on = decode_bool(out_neg)
    out_lit = lit_of_src[os_idx] ^ (1 if on else 0)

    return ('sat', (out_lit, ops))


# ═══════════════════════════════════════════════════════════════════════════════
#  Template evaluation (sanity)
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_template(
    template:  Template,
    n_inputs:  int,
) -> int:
    """Evaluate a ``(out_lit, ops)`` template and return its truth table."""
    out_lit, ops = template
    V    = 1 << n_inputs
    mask = (1 << V) - 1

    tt_of: Dict[int, int] = {0: 0, 1: mask}
    for i in range(n_inputs):
        t = _input_tt(i, n_inputs)
        tt_of[2 + 2 * i] = t
        tt_of[3 + 2 * i] = (~t) & mask

    op_base = 2 + 2 * n_inputs
    for j, (a, b) in enumerate(ops):
        v = tt_of[a] & tt_of[b]
        tt_of[op_base + 2 * j]     = v & mask
        tt_of[op_base + 2 * j + 1] = (~v) & mask

    return tt_of[out_lit]
