"""
Resubstitution pass for wide cuts (k > 4).

For each internal AND node v whose k-feasible cut exceeds the 4-input
NPN DB (k in {5, 6, 7}), search for a small set of **divisors**
``D = {d_1, ..., d_m}`` (m <= 3) drawn from the transitive fan-in window
of v whose own supports sit inside v's cut, such that v is a function
of those divisors alone:

    v(cut)  ≡  F(d_1(cut), ..., d_m(cut))    for some F : {0,1}^m → {0,1}

If such a factorisation exists, F has m ≤ 3 inputs (so ≤ 4-bit truth
table for m=2, ≤ 8 bits for m=3) and can be synthesised via
:data:`AIG_DB_4`.  The cost is ``n_gates(F)`` plus zero for the divisors
themselves — they already exist — which is frequently below
``|MFFC(v)|`` on cross-output-shared structure.

This is the same principle as ABC's ``dc2`` resubstitution and
complements the 1-gate resub already present in :mod:`dont_care`
(which is m=2 over sim-signatures rather than over the cut TT).

Soundness
---------
The dependency check is **exact over the cut's truth-table space**:
every minterm of the k-cut is enumerated and the divisor-value tuple
pinned against v's value.  A replacement therefore produces the exact
same Boolean function as the original node, modulo the structural
equivalence of the cut dominance.
"""

from __future__ import annotations
from itertools import combinations
from typing    import Dict, List, Optional, Set, Tuple

from ..core.aig import AIG, FALSE, TRUE, Lit as AIGLit
from ..aig_db_4 import AIG_DB_4


def _shrink_template(
    tmpl_out: int,
    ops:      List[Tuple[int, int]],
    from_k:   int,
    to_k:     int,
) -> Optional[Tuple[int, List[Tuple[int, int]]]]:
    """
    Re-number a 4-input AIG_DB_4 template so its op indices are
    consistent with a smaller ``to_k``-input ordered cut.

    The AIG_DB_4 convention uses ``2+2*i`` for input i, ``3+2*i`` for ~i,
    and ``2+2*from_k + 2*g`` for gate g's output.  When applying against
    only ``to_k < from_k`` inputs, ``_apply_template`` writes gate outputs
    starting at ``2+2*to_k``, so the template's gate literals must be
    shifted down by ``2*(from_k - to_k)``.  If any op references a
    padding input (index ≥ to_k), the template genuinely uses more than
    ``to_k`` inputs and cannot be shrunk — returns ``None``.
    """
    if to_k >= from_k:
        return (tmpl_out, ops)
    delta       = 2 * (from_k - to_k)
    op_base_old = 2 + 2 * from_k
    pad_lo      = 2 + 2 * to_k
    pad_hi      = op_base_old  # padding inputs: pad_lo .. pad_hi-1

    def remap(lit: int) -> Optional[int]:
        if lit < 2:                        # constant
            return lit
        if lit < pad_lo:                   # real input
            return lit
        if lit < pad_hi:                   # padding input — template uses
            return None                    # an input we don't have
        return lit - delta                 # gate literal: shift down

    new_ops: List[Tuple[int, int]] = []
    for a, b in ops:
        ra = remap(a)
        rb = remap(b)
        if ra is None or rb is None:
            return None
        new_ops.append((ra, rb))
    new_out = remap(tmpl_out)
    if new_out is None:
        return None
    return (new_out, new_ops)


def _expand_tt_to_4(tt: int, m: int) -> int:
    """
    Replicate an ``m``-input truth table (``m`` in 0..4) into the
    16-bit 4-input format used by :data:`AIG_DB_4`.

    The minimum AIG stored in the DB for the expanded key never uses
    inputs beyond the first ``m``, because the DB is minimised and the
    expanded function is genuinely independent of the padding inputs.
    """
    if m >= 4:
        return tt & 0xFFFF
    tt &= (1 << (1 << m)) - 1
    chunk = 1 << m
    for _ in range(4 - m):
        tt |= tt << chunk
        chunk <<= 1
    return tt & 0xFFFF
from .rewrite  import (
    enumerate_cuts,
    evaluate_cut_tt,
    _compute_ref_counts,
    _compute_mffc,
    _count_template_new_nodes,
    _apply_template,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Simulate every AND node's TT over a fixed cut
# ═══════════════════════════════════════════════════════════════════════════════

def _simulate_over_cut(
    aig:         AIG,
    ordered_cut: List[int],
) -> Dict[int, int]:
    """
    Return a map ``node_id -> 2^k-bit truth table`` for every AND node
    reachable from the cut (and for each cut variable itself).

    Nodes whose support doesn't fit under ``ordered_cut`` return ``None``
    — omitted from the result, since they can't serve as divisors.
    """
    k    = len(ordered_cut)
    V    = 1 << k
    mask = (1 << V) - 1

    tt: Dict[int, int] = {0: 0}
    cut_set = set(ordered_cut)

    # Each cut variable i contributes its standard single-variable TT.
    for j, c_id in enumerate(ordered_cut):
        bv = 0
        for m in range(V):
            if (m >> j) & 1:
                bv |= 1 << m
        tt[c_id] = bv

    # Topological sweep: only assign a TT if both operands already have one.
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if nid in tt:
            continue
        if entry[0] == 'input':
            if nid in cut_set:
                continue
            # Input below the cut that is *not* part of it — such a node
            # cannot be evaluated under the cut, so we leave it out.
            continue
        _, a_lit, b_lit = entry
        ida = aig.node_of(a_lit)
        idb = aig.node_of(b_lit)
        if ida not in tt or idb not in tt:
            continue
        va = tt[ida]
        if aig.is_complemented(a_lit):
            va = (~va) & mask
        vb = tt[idb]
        if aig.is_complemented(b_lit):
            vb = (~vb) & mask
        tt[nid] = va ^ vb if entry[0] == 'xor' else va & vb

    return tt


# ═══════════════════════════════════════════════════════════════════════════════
#  Functional dependency check + F-TT extraction
# ═══════════════════════════════════════════════════════════════════════════════

def _functional_dependency(
    target_tt:    int,
    divisor_tts:  List[int],
    V:            int,
) -> Optional[int]:
    """
    Does ``target_tt`` factor through the divisors, i.e. is there a unique
    function F such that ``target_tt[m] == F(div_1[m], ..., div_m[m])``
    for every minterm m of the V-size cut space?

    Returns F's truth table (as a 2^m-bit int over the divisors as
    variables) or ``None`` if no such F exists (some divisor-tuple
    maps to both 0 and 1 in the target).
    """
    m = len(divisor_tts)
    tuple_to_val: Dict[int, int] = {}
    F_tt = 0

    for p in range(V):
        div_tup = 0
        for j, dt in enumerate(divisor_tts):
            div_tup |= ((dt >> p) & 1) << j
        t_val = (target_tt >> p) & 1
        if div_tup in tuple_to_val:
            if tuple_to_val[div_tup] != t_val:
                return None
        else:
            tuple_to_val[div_tup] = t_val
            if t_val:
                F_tt |= 1 << div_tup

    return F_tt


# ═══════════════════════════════════════════════════════════════════════════════
#  Divisor selection heuristic
# ═══════════════════════════════════════════════════════════════════════════════

def _select_divisors(
    aig:          AIG,
    tt_over_cut:  Dict[int, int],
    root_id:      int,
    ordered_cut:  List[int],
    mffc:         Set[int],
    max_divisors: int,
) -> List[int]:
    """
    Pick up to ``max_divisors`` candidate-divisor node IDs.

    Prefers AND nodes topologically before ``root_id`` that are **not**
    inside v's MFFC (otherwise the resubstitution wouldn't save gates
    since the divisor dies with v).  Cut variables themselves are always
    included first because they are "free" to reuse.
    """
    divisors: List[int] = []
    # Cut variables come first (cheapest — no existing AND consumed).
    for c_id in ordered_cut:
        if c_id in tt_over_cut:
            divisors.append(c_id)

    # Internal AND nodes in TFI of root_id, topologically earlier, that have
    # a well-defined TT over the cut and are *outside* the root's MFFC.
    for nid in sorted(tt_over_cut.keys()):
        if nid >= root_id:
            break
        if nid in mffc:
            continue
        if nid in ordered_cut:
            continue
        # Skip primary-input nodes (already covered via cut list above).
        entry = aig._nodes[nid - 1]
        if entry[0] not in ('and', 'xor'):
            continue
        divisors.append(nid)
        if len(divisors) >= max_divisors:
            break

    return divisors


# ═══════════════════════════════════════════════════════════════════════════════
#  Main pass
# ═══════════════════════════════════════════════════════════════════════════════

_last_stats: Dict[str, int] = {}


def last_resub_stats() -> Dict[str, int]:
    """Counters from the most recent :func:`resub_aig` invocation."""
    return dict(_last_stats)


def resub_aig(
    old_aig:         AIG,
    out_lits:        Optional[List[int]] = None,
    rounds:          int                 = 1,
    cut_size_max:    int                 = 7,
    cut_size_min:    int                 = 5,
    max_divisors:    int                 = 20,
    max_m:           int                 = 3,
) -> Tuple[AIG, List[int]]:
    """
    Functional (cut-exact) resubstitution pass.

    For every AND node v with a k-cut in ``[cut_size_min, cut_size_max]``:
      1. Evaluate v's 2^k TT over the cut.
      2. Pick up to ``max_divisors`` candidate divisors from TFI(v)
         outside v's MFFC, plus the cut variables themselves.
      3. For each ``m ∈ {1, 2, ..., max_m}`` and each m-tuple of
         divisors, test whether v's TT depends only on the tuple
         (functional dependency check).  If so, F's TT is determined.
      4. Synthesise F via :data:`AIG_DB_4` (m ≤ 4) and score against
         v's MFFC cost.  Replace iff net gates strictly improve.

    Reuses the same cost model as :func:`rewrite_aig` so this pass is
    scheduled-safe — it never inflates gate count.
    """
    if out_lits is None:
        out_lits = []

    stats = {
        'nodes_examined': 0,
        'replacements':   0,
        'm1_hits':        0,
        'm2_hits':        0,
        'm3_hits':        0,
    }

    current_aig = old_aig
    current_out = list(out_lits)

    for _ in range(rounds):
        cuts    = enumerate_cuts(current_aig, k=cut_size_max)
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

            if entry[0] == 'xor':
                _, old_a, old_b = entry
                nlit = new_aig.make_xor(lit_map[old_a], lit_map[old_b])
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

            for cut in cuts[old_id]:
                k = len(cut)
                if k < cut_size_min or k > cut_size_max:
                    continue
                if old_id in cut:
                    continue

                tt, ordered_cut = evaluate_cut_tt(current_aig, old_id, cut)
                V = 1 << k
                stats['nodes_examined'] += 1

                # TTs of all eligible nodes (cut + AND TFI) over this cut.
                tt_over_cut = _simulate_over_cut(current_aig, ordered_cut)
                if old_id not in tt_over_cut:
                    continue

                mffc      = _compute_mffc(current_aig, old_id, cut, ref_old)
                mffc_size = len(mffc)
                if mffc_size == 0:
                    continue

                divisors = _select_divisors(
                    current_aig, tt_over_cut, old_id,
                    ordered_cut, mffc, max_divisors,
                )
                if not divisors:
                    continue

                # Try m = 1, 2, 3 — break early when an m finds a match.
                for m in range(1, max_m + 1):
                    if m > len(divisors):
                        break

                    found_m = False
                    for combo in combinations(divisors, m):
                        # All divisors are either cut inputs or earlier AND
                        # nodes; both have lit_map entries by this point.
                        if any((d * 2) not in lit_map for d in combo):
                            continue
                        div_tts = [tt_over_cut[d] for d in combo]
                        F_tt    = _functional_dependency(tt, div_tts, V)
                        if F_tt is None:
                            continue

                        F_key = _expand_tt_to_4(F_tt, m)
                        template = AIG_DB_4.get(F_key)
                        if template is None:
                            continue
                        tmpl_out, ops = template
                        shrunk = _shrink_template(tmpl_out, ops, 4, m)
                        if shrunk is None:
                            continue
                        tmpl_out, ops = shrunk

                        virt_cut = list(combo)
                        n_new, final_lit = _count_template_new_nodes(
                            new_aig, virt_cut, tmpl_out, ops, m, lit_map,
                        )
                        if final_lit is None:
                            continue

                        net = n_new - mffc_size
                        if net < best_net:
                            best_net    = net
                            best_choice = (virt_cut, tmpl_out, ops, m)
                            found_m = True

                    if found_m:
                        break

            if best_choice is not None:
                virt_cut, tmpl_out, ops, m = best_choice
                applied = _apply_template(
                    new_aig, virt_cut, tmpl_out, ops, m, lit_map,
                )
                stats['replacements'] += 1
                stats[f'm{m}_hits']   += 1
            else:
                applied = new_aig.make_and(new_a, new_b)

            lit_map[old_id * 2]     = applied
            lit_map[old_id * 2 + 1] = applied ^ 1

        current_out = [lit_map[l] for l in current_out]
        current_aig, current_out = new_aig.gc(current_out)

    _last_stats.clear()
    _last_stats.update(stats)
    return current_aig, current_out
