"""
Bi-decomposition pass (AND / OR / XOR) for cuts beyond 4 inputs.

For each internal AND node v whose k-feasible cut exceeds the 4-input
NPN database, try to factor the cut's truth table f(x_0..x_{k-1}) as

    f = g(X)  OP  h(Y),   OP in {AND, OR, XOR}

with **disjoint** supports X, Y (X ∪ Y = cut, X ∩ Y = ∅). If such a
partition exists and each half has support ≤ 4 it is synthesised via
:data:`AIG_DB_4` (or via :func:`exact_synthesize` if enabled), combined
with one AND / OR / XOR, and plugged into ``new_aig`` behind the same
MFFC cost-gate used by the rewriter.

Algorithm (Mishchenko et al., DAC '09, §4):
  * For each partition (X, Y) with |X|, |Y| ≤ 4:
      - AND test:  f = g · h  iff  g(x) := ∃y.f(x,y), h(y) := ∃x.f(x,y)
                   and  f(x,y) == g(x) & h(y)  everywhere.
      - OR  test:  dual with universal quantification.
      - XOR test:  f(x,y) ⊕ f(0,y) independent of y, and the remainder
                   independent of x; then g(x) = f(x,0), h(y) = f(0,y) ⊕ f(0,0).
  * Cost: n_new(g) + n_new(h) + 1 (AND/OR)  or  + 3 (XOR).

The pass runs as a single sweep, then a structural-hash GC pass, mirroring
:func:`rewrite_aig`.  It never accepts a decomposition whose net change in
reachable AND nodes is non-negative.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Set, Tuple

from ..core.aig import AIG, FALSE, TRUE, Lit as AIGLit
from ..aig_db_4 import AIG_DB_4
from .rewrite   import (
    enumerate_cuts,
    evaluate_cut_tt,
    _compute_ref_counts,
    _compute_mffc,
    _count_template_new_nodes,
    _apply_template,
    _VIRTUAL_BASE,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Truth-table helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _project_tt_to_sub(
    tt:           int,
    k:            int,
    sub_indices:  List[int],
    fixed_bits:   int,
    fixed_indices: List[int],
) -> int:
    """
    Project a k-input truth table onto the sub-variables ``sub_indices``
    with the other variables pinned to ``fixed_bits`` (aligned to
    ``fixed_indices``).

    Returns a 2^len(sub_indices)-bit integer.
    """
    a = len(sub_indices)
    V_sub = 1 << a
    result = 0
    for x in range(V_sub):
        # Reconstruct the original k-bit minterm index.
        m = fixed_bits   # already has fixed-variable bits placed
        for j, idx in enumerate(sub_indices):
            if (x >> j) & 1:
                m |= 1 << idx
        if (tt >> m) & 1:
            result |= 1 << x
    return result


def _quantify(
    tt:        int,
    k:         int,
    keep:      List[int],
    drop:      List[int],
    universal: bool,
) -> int:
    """
    ∃drop or ∀drop of ``tt`` over the ``drop`` variables, returning a
    2^len(keep)-bit truth table indexed by ``keep``.
    """
    a = len(keep)
    b = len(drop)
    V_keep = 1 << a
    V_drop = 1 << b
    result = 0
    for x in range(V_keep):
        base = 0
        for j, idx in enumerate(keep):
            if (x >> j) & 1:
                base |= 1 << idx
        if universal:
            acc = 1
            for y in range(V_drop):
                m = base
                for j, idx in enumerate(drop):
                    if (y >> j) & 1:
                        m |= 1 << idx
                if not ((tt >> m) & 1):
                    acc = 0
                    break
        else:
            acc = 0
            for y in range(V_drop):
                m = base
                for j, idx in enumerate(drop):
                    if (y >> j) & 1:
                        m |= 1 << idx
                if (tt >> m) & 1:
                    acc = 1
                    break
        if acc:
            result |= 1 << x
    return result


def _composed_tt(
    tt_g:  int,
    tt_h:  int,
    k:     int,
    X:     List[int],
    Y:     List[int],
    op:    str,
) -> int:
    """Reconstruct f(x_0..x_{k-1}) from g(X), h(Y), op for verification."""
    V = 1 << k
    result = 0
    for m in range(V):
        xv = 0
        for j, idx in enumerate(X):
            if (m >> idx) & 1:
                xv |= 1 << j
        yv = 0
        for j, idx in enumerate(Y):
            if (m >> idx) & 1:
                yv |= 1 << j
        gv = (tt_g >> xv) & 1
        hv = (tt_h >> yv) & 1
        if op == 'and':
            v = gv & hv
        elif op == 'or':
            v = gv | hv
        else:
            v = gv ^ hv
        if v:
            result |= 1 << m
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Core bi-decomposition search
# ═══════════════════════════════════════════════════════════════════════════════

BidecResult = Tuple[str, List[int], List[int], int, int]  # (op, X, Y, tt_g, tt_h)


def bi_decompose_tt(
    tt:           int,
    k:            int,
    max_half:     int = 4,
) -> Optional[BidecResult]:
    """
    Search for a disjoint-support bi-decomposition of ``tt`` on ``k`` vars.

    Returns ``(op, X, Y, tt_g, tt_h)`` or ``None``. Both halves satisfy
    ``|X|, |Y| >= 1`` and ``|X|, |Y| <= max_half``. The partition visited
    first that yields the smallest max(|X|, |Y|) wins — this heuristic
    favours balanced splits which tend to give cheaper templates.
    """
    if k < 2:
        return None

    V   = 1 << k
    mask = (1 << V) - 1
    tt &= mask

    best: Optional[BidecResult] = None
    best_imbalance = k + 1     # prefer balanced partitions

    # Enumerate each subset X of {0..k-1} with 1 <= |X| <= k-1.
    # Use the lex-smaller half to dedupe (mask < complement).
    for submask in range(1, V):
        comp = ((1 << k) - 1) ^ submask
        if submask >= comp:
            continue    # symmetric; the other half handles this partition

        X = [i for i in range(k) if (submask >> i) & 1]
        Y = [i for i in range(k) if (comp    >> i) & 1]
        a, b = len(X), len(Y)
        if a == 0 or b == 0:
            continue
        if a > max_half or b > max_half:
            continue

        imbalance = abs(a - b)
        if best is not None and imbalance >= best_imbalance:
            # We already have something at least as balanced; skip.
            continue

        # --- AND test ---------------------------------------------------------
        g_and = _quantify(tt, k, X, Y, universal=False)   # ∃Y. f
        h_and = _quantify(tt, k, Y, X, universal=False)   # ∃X. f
        if _composed_tt(g_and, h_and, k, X, Y, 'and') == tt:
            cand = ('and', X, Y, g_and, h_and)
            if best is None or imbalance < best_imbalance:
                best = cand
                best_imbalance = imbalance
                continue

        # --- OR test ----------------------------------------------------------
        g_or = _quantify(tt, k, X, Y, universal=True)
        h_or = _quantify(tt, k, Y, X, universal=True)
        if _composed_tt(g_or, h_or, k, X, Y, 'or') == tt:
            cand = ('or', X, Y, g_or, h_or)
            if best is None or imbalance < best_imbalance:
                best = cand
                best_imbalance = imbalance
                continue

        # --- XOR test ---------------------------------------------------------
        # g(X) := f(X, y0 = all-zero in Y), h(Y) := f(x0=0, Y) ⊕ f(0,0)
        g_xor = _project_tt_to_sub(tt, k, X, 0, Y)
        h_raw = _project_tt_to_sub(tt, k, Y, 0, X)
        f00   = tt & 1
        h_xor = h_raw ^ (((1 << (1 << b)) - 1) if f00 else 0)
        if _composed_tt(g_xor, h_xor, k, X, Y, 'xor') == tt:
            cand = ('xor', X, Y, g_xor, h_xor)
            if best is None or imbalance < best_imbalance:
                best = cand
                best_imbalance = imbalance
                continue

    return best


# ═══════════════════════════════════════════════════════════════════════════════
#  Template instantiation (AND/OR/XOR combinator over two sub-templates)
# ═══════════════════════════════════════════════════════════════════════════════

def _expand_tt_to_4(tt: int, m: int) -> int:
    """Replicate an m-input TT (m in 0..4) into the 16-bit 4-input format."""
    if m >= 4:
        return tt & 0xFFFF
    tt &= (1 << (1 << m)) - 1
    chunk = 1 << m
    for _ in range(4 - m):
        tt |= tt << chunk
        chunk <<= 1
    return tt & 0xFFFF


def _shrink_template(
    tmpl_out: int,
    ops:      List[Tuple[int, int]],
    from_k:   int,
    to_k:     int,
):
    """Renumber gate literals so a 4-input AIG_DB_4 template can be applied
    against a ``to_k``-input ordered cut.  Returns ``None`` if the template
    actually references a padding input (i.e. genuinely uses more inputs
    than we have)."""
    if to_k >= from_k:
        return (tmpl_out, ops)
    delta       = 2 * (from_k - to_k)
    op_base_old = 2 + 2 * from_k
    pad_lo      = 2 + 2 * to_k
    pad_hi      = op_base_old

    def remap(lit: int):
        if lit < 2:
            return lit
        if lit < pad_lo:
            return lit
        if lit < pad_hi:
            return None
        return lit - delta

    new_ops: List[Tuple[int, int]] = []
    for a, b in ops:
        ra = remap(a); rb = remap(b)
        if ra is None or rb is None:
            return None
        new_ops.append((ra, rb))
    new_out = remap(tmpl_out)
    if new_out is None:
        return None
    return (new_out, new_ops)


def _fetch_template(
    tt:          int,
    k:           int,
    use_exact:   bool,
    exact_gates: int,
    exact_tmo:   int,
):
    """Return (out_lit, ops) or None, with op indices re-scaled for ``k`` inputs."""
    if k <= 4:
        key = _expand_tt_to_4(tt, k)
        tmpl = AIG_DB_4.get(key)
        if tmpl is not None:
            shrunk = _shrink_template(tmpl[0], tmpl[1], 4, k)
            if shrunk is not None:
                return shrunk
    if use_exact:
        try:
            from .exact_synthesis import exact_synthesize
            return exact_synthesize(
                tt, k,
                max_gates  = exact_gates,
                timeout_ms = exact_tmo,
            )
        except ImportError:
            return None
    return None


def _count_bidec_new_nodes(
    new_aig:     AIG,
    ordered_cut: List[int],
    X:           List[int],
    Y:           List[int],
    op:          str,
    tpl_g,
    tpl_h,
    lit_map:     Dict[int, int],
) -> Tuple[Optional[int], Optional[int]]:
    """
    Speculatively synthesise f = g(X) op h(Y) in ``new_aig`` using a
    snapshot/restore bake-off, so the cost is an exact count of ANDs
    that would be added.  Returns (n_new, final_lit) or (None, None).
    """
    out_lit_g, ops_g = tpl_g
    out_lit_h, ops_h = tpl_h

    # Remap ordered_cut to sub-cuts for g and h.
    sub_cut_g = [ordered_cut[i] for i in X]
    sub_cut_h = [ordered_cut[i] for i in Y]

    snap = new_aig.snapshot()
    before = new_aig.n_nodes
    try:
        g_lit = _apply_template(new_aig, sub_cut_g, out_lit_g, ops_g, len(X), lit_map)
        h_lit = _apply_template(new_aig, sub_cut_h, out_lit_h, ops_h, len(Y), lit_map)
        if op == 'and':
            final = new_aig.make_and(g_lit, h_lit)
        elif op == 'or':
            final = new_aig.make_or(g_lit, h_lit)
        else:  # xor
            final = new_aig.make_xor(g_lit, h_lit)
        n_new = new_aig.n_nodes - before
    finally:
        new_aig.restore(snap)

    return (n_new, final)


def _apply_bidec(
    new_aig:     AIG,
    ordered_cut: List[int],
    X:           List[int],
    Y:           List[int],
    op:          str,
    tpl_g,
    tpl_h,
    lit_map:     Dict[int, int],
) -> int:
    """Actually instantiate the bi-decomposed sub-network; return out literal."""
    out_lit_g, ops_g = tpl_g
    out_lit_h, ops_h = tpl_h

    sub_cut_g = [ordered_cut[i] for i in X]
    sub_cut_h = [ordered_cut[i] for i in Y]

    g_lit = _apply_template(new_aig, sub_cut_g, out_lit_g, ops_g, len(X), lit_map)
    h_lit = _apply_template(new_aig, sub_cut_h, out_lit_h, ops_h, len(Y), lit_map)

    if op == 'and':
        return new_aig.make_and(g_lit, h_lit)
    if op == 'or':
        return new_aig.make_or(g_lit, h_lit)
    return new_aig.make_xor(g_lit, h_lit)


# ═══════════════════════════════════════════════════════════════════════════════
#  Pass driver
# ═══════════════════════════════════════════════════════════════════════════════

_last_stats: Dict[str, int] = {}


def last_bidec_stats() -> Dict[str, int]:
    """Counters from the most recent :func:`bidec_aig` invocation."""
    return dict(_last_stats)


def bidec_aig(
    old_aig:          AIG,
    out_lits:         Optional[List[int]] = None,
    rounds:           int                 = 1,
    cut_size:         int                 = 8,
    min_cut:          int                 = 5,
    use_exact:        bool                = False,
    exact_max_gates:  int                 = 6,
    exact_timeout_ms: int                 = 2000,
) -> Tuple[AIG, List[int]]:
    """
    Bi-decomposition pass over wide cuts (``min_cut <= k <= cut_size``).

    For each AND node, try every k-feasible cut with k in the band, look
    for a disjoint-support AND/OR/XOR partition whose halves are each
    ≤ 4 inputs (or ≤ ``exact_max_gates`` if ``use_exact=True``), and
    replace the node iff the resulting sub-network strictly reduces the
    MFFC cost — same gate-accounting as :func:`rewrite_aig`.

    The pass runs its own AIG-rebuild → gc loop, so it can be freely
    composed with other passes in a synthesis script.
    """
    if out_lits is None:
        out_lits = []

    stats = {
        'nodes_examined':    0,
        'decomps_found':     0,
        'decomps_applied':   0,
        'ops_and':           0,
        'ops_or':            0,
        'ops_xor':           0,
    }

    current_aig = old_aig
    current_out = list(out_lits)

    for _ in range(rounds):
        cuts    = enumerate_cuts(current_aig, k=cut_size)
        ref_old = _compute_ref_counts(current_aig, current_out)

        new_aig = AIG()
        lit_map: Dict[int, int] = {FALSE: FALSE, TRUE: TRUE}

        for i, entry in enumerate(current_aig._nodes):
            old_id = i + 1
            if entry[0] == 'input':
                nlit = new_aig.make_input(entry[1])
                lit_map[old_id * 2]     = nlit
                lit_map[old_id * 2 + 1] = nlit ^ 1
                continue

            _, old_a, old_b = entry
            new_a = lit_map[old_a]
            new_b = lit_map[old_b]

            base_exists = new_aig.has_and(new_a, new_b)
            base_cost   = 0 if base_exists else 1

            best_choice = None
            best_net    = base_cost

            # Only consider cuts genuinely larger than the NPN DB.
            for cut in cuts[old_id]:
                if len(cut) < min_cut or len(cut) > cut_size:
                    continue
                if old_id in cut:
                    continue

                tt, ordered_cut = evaluate_cut_tt(current_aig, old_id, cut)
                k              = len(ordered_cut)
                stats['nodes_examined'] += 1

                max_half = 4 if not use_exact else 6
                dec = bi_decompose_tt(tt, k, max_half=max_half)
                if dec is None:
                    continue

                op, X, Y, tt_g, tt_h = dec
                stats['decomps_found'] += 1

                tpl_g = _fetch_template(
                    tt_g, len(X), use_exact, exact_max_gates, exact_timeout_ms
                )
                if tpl_g is None:
                    continue
                tpl_h = _fetch_template(
                    tt_h, len(Y), use_exact, exact_max_gates, exact_timeout_ms
                )
                if tpl_h is None:
                    continue

                mffc      = _compute_mffc(current_aig, old_id, cut, ref_old)
                mffc_size = len(mffc)

                n_new, final_lit = _count_bidec_new_nodes(
                    new_aig, ordered_cut, X, Y, op, tpl_g, tpl_h, lit_map,
                )
                if final_lit is None:
                    continue

                net = n_new - mffc_size
                if net < best_net:
                    best_net    = net
                    best_choice = (op, X, Y, tpl_g, tpl_h, ordered_cut)

            if best_choice is not None:
                op, X, Y, tpl_g, tpl_h, ordered_cut = best_choice
                applied = _apply_bidec(
                    new_aig, ordered_cut, X, Y, op, tpl_g, tpl_h, lit_map,
                )
                stats['decomps_applied'] += 1
                stats[f'ops_{op}']       += 1
            else:
                applied = new_aig.make_and(new_a, new_b)

            lit_map[old_id * 2]     = applied
            lit_map[old_id * 2 + 1] = applied ^ 1

        current_out = [lit_map[l] for l in current_out]
        current_aig, current_out = new_aig.gc(current_out)

    _last_stats.clear()
    _last_stats.update(stats)
    return current_aig, current_out
