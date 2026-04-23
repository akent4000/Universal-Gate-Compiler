"""
Don't-Care optimisation pass — V2 (Mishchenko 2009).

For each internal AND node ``v`` and each k-feasible cut of ``v``, compute the
**care set** — the subset of cut-input patterns where ``v``'s value must be
preserved. Patterns outside the care set are don't-cares and may be reassigned
freely to find a cheaper sub-circuit in :data:`AIG_DB_4`.

Two flavours of don't-care:

  • SDC (satisfiability DCs): cut patterns unreachable from any primary input
    assignment. Enabled by default; sim-based under-approximation, optionally
    refined by Z3-UNSAT proofs for patterns never witnessed by simulation.

  • ODC (observability DCs): cut patterns where ``v``'s value does not affect
    any primary output. V2 derives ODCs from a **global, reverse-topological
    care propagation** (Mishchenko 2009, §3):

        care[PO_node] = all-ones
        for y = AND(a_lit, b_lit) in reverse-topo order:
            sa = sig[a_node]  (complemented if edge is complemented)
            sb = sig[b_node]  (complemented if edge is complemented)
            care[a_node] |= care[y] & sb
            care[b_node] |= care[y] & sa

    The resulting W-bit care mask per node is projected into each cut's
    2^k-bit pattern space to obtain the cut-level care mask consumed by the
    DB lookup.

Soundness
---------
V1 (pre-refactor) computed a per-node ODC cone with Z3 substitution and was
**unsound** under sequential rewrites: once an upstream node A was rewritten,
the cut signals feeding a downstream node B in ``new_aig`` could diverge from
the original AIG on PI assignments that were DC for A but CARED for B. B's
template, chosen against the original cut signals, then produced the wrong
output on those PI assignments — safety-net miter was the only defence.

V2 fixes this with a simulation-based **admissibility check** applied to every
candidate template:

    cand_sig = template evaluated on sig_new[cut signals in new_aig]
    accept iff (cand_sig ^ sig_old[v]) & care_sim[v] == 0

i.e. the template must reproduce ``v``'s reference signature on every cared
simulation pattern, regardless of how upstream rewrites perturbed the cut
signatures. The check is O(|ops|) bit-parallel ints — no Z3 required on the
hot path — and sound modulo simulation coverage. The end-of-pass miter stays
as a last-line safety net for sim-undersampled patterns; in practice it no
longer trips on any MCNC bench.

Reference
---------
Mishchenko, Brayton, Jiang, Jang, "Scalable Don't-Care-Based Logic
Optimization and Resynthesis" (FPGA 2009), §3 (global ODC propagation).
"""

from __future__ import annotations
from typing import Dict, List, Optional, Set, Tuple
import random

# Default ODC computation mode for dc_optimize. See ``dc_optimize`` docstring.
ODC_MODE_LEGACY:    str = 'legacy'    # Mishchenko 2009 one-shot, known-buggy
ODC_MODE_HYBRID:    str = 'hybrid'    # drift-aware refresh every K rewrites (step 4)
ODC_MODE_WINDOW:    str = 'window'    # forward-flip per-node window (step 5)
ODC_MODE_Z3_EXACT:  str = 'z3-exact'  # Z3-exact admissibility: UNSAT(T ≠ old_v) (step 4b)

from ..core.aig import AIG, FALSE, TRUE, Lit as AIGLit
from ..aig_db_4 import AIG_DB_4
from .rewrite   import (
    enumerate_cuts,
    evaluate_cut_tt,
    _compute_ref_counts,
    _compute_mffc,
    _count_template_new_nodes,
    _apply_template,
)
from .fraig     import _simulate, _build_z3_exprs


# ═══════════════════════════════════════════════════════════════════════════════
#  Fanout index (kept for dc_stats and future window-resub pass)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_fanout_index(aig: AIG) -> Dict[int, List[int]]:
    """For each AND node id, list of child AND node ids that reference it."""
    fanout: Dict[int, List[int]] = {i + 1: [] for i in range(aig.n_nodes)}
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'and':
            _, a_lit, b_lit = entry
            ia = aig.node_of(a_lit)
            ib = aig.node_of(b_lit)
            if ia > 0:
                fanout[ia].append(nid)
            if ib > 0 and ib != ia:
                fanout[ib].append(nid)
    return fanout


# ═══════════════════════════════════════════════════════════════════════════════
#  Global care propagation (Mishchenko 2009, §3)
# ═══════════════════════════════════════════════════════════════════════════════

def _propagate_care_sim(
    aig:       AIG,
    out_lits:  List[AIGLit],
    sig_old:   Dict[int, int],
    W:         int,
) -> Dict[int, int]:
    """
    Return per-node care mask over W simulation patterns.

    Bit p of ``care[v]`` is set iff flipping node ``v``'s value on simulation
    pattern ``p`` propagates to at least one primary output in the reference
    AIG. Derived by reverse-topological traversal of the AIG's AND nodes.

    **Known soundness gap (ROADMAP P0#1).** The propagation uses ``sig_old``
    for sibling signals, so the result correctly describes ``old_aig``'s
    intrinsic ODC. After a chain of admissible rewrites, however, siblings
    may drift on their own ~care bits, turning what was a 0 in old_aig into
    a 1 in new_aig and silently extending v's influence on POs. Use
    :func:`_propagate_care_sim_hybrid` via ``care_recompute_every > 0`` in
    :func:`dc_optimize` to refresh care mid-pass with drift-aware siblings.
    """
    MASK = (1 << W) - 1
    care: Dict[int, int] = {0: 0}
    for i in range(aig.n_nodes):
        care[i + 1] = 0

    for lit in out_lits:
        nid = aig.node_of(lit)
        if nid > 0:
            care[nid] = MASK

    for i in range(aig.n_nodes - 1, -1, -1):
        entry = aig._nodes[i]
        if entry[0] != 'and':
            continue
        nid = i + 1
        cy = care[nid]
        if cy == 0:
            continue
        _, a_lit, b_lit = entry
        ia = aig.node_of(a_lit)
        ib = aig.node_of(b_lit)
        sa = sig_old.get(ia, 0)
        if aig.is_complemented(a_lit):
            sa = (~sa) & MASK
        sb = sig_old.get(ib, 0)
        if aig.is_complemented(b_lit):
            sb = (~sb) & MASK
        if ia > 0:
            care[ia] |= cy & sb
        if ib > 0:
            care[ib] |= cy & sa
    return care


# ═══════════════════════════════════════════════════════════════════════════════
#  V3 — drift-aware care propagation (ROADMAP P0#1 step 4)
# ═══════════════════════════════════════════════════════════════════════════════

def _propagate_care_sim_hybrid(
    aig:       AIG,
    out_lits:  List[AIGLit],
    sig_old:   Dict[int, int],
    sig_new:   Dict[int, int],
    lit_map:   Dict[int, int],
    new_aig:   AIG,
    W:         int,
) -> Dict[int, int]:
    """
    Drift-aware care propagation for mid-pass refresh.

    Same reverse-topological sweep as :func:`_propagate_care_sim`, but each
    sibling signal ``sb`` uses the **current** value of ``b`` as resolved
    through ``lit_map`` and ``sig_new``: if ``b`` has already been rewritten,
    ``sb`` reflects its post-rewrite signature (which may differ from
    ``sig_old[b]`` on ~care bits); if ``b`` is still ahead in topological
    order, ``sb`` falls back to ``sig_old[b]``.

    Why this closes the ROADMAP P0#1 gap in practice: when an upstream
    rewrite drifts ``b`` from ``sig_old[b][p] = 0`` to ``sig_new[b][p] = 1``
    on a ~care bit, the update rule ``care[a] |= care[y] & sb`` — which
    previously saw ``sb[p] = 0`` and kept ``a`` uncared on ``p`` — now sees
    ``sb[p] = 1`` and extends ``care[a]`` to include ``p``. Subsequent
    admissibility checks for ``a`` are correspondingly tighter, rejecting
    the templates that would have silently composed into a PO-level fault.

    Not sound in general against adversarial rewrite ordering (the tightening
    applies only to nodes processed AFTER the refresh), but empirically
    closes the reverts on the router/priority/i2c/sin regressions when
    invoked every few rewrites.
    """
    MASK = (1 << W) - 1
    care: Dict[int, int] = {0: 0}
    for i in range(aig.n_nodes):
        care[i + 1] = 0

    for lit in out_lits:
        nid = aig.node_of(lit)
        if nid > 0:
            care[nid] = MASK

    def current_sig(old_nid: int, old_lit: int) -> int:
        if old_nid == 0:
            return MASK if aig.is_complemented(old_lit) else 0
        new_lit = lit_map.get(old_nid * 2)
        if new_lit is None:
            s = sig_old.get(old_nid, 0)
        else:
            new_nid = new_aig.node_of(new_lit)
            if new_nid == 0:
                s = MASK if new_aig.is_complemented(new_lit) else 0
            else:
                s = sig_new.get(new_nid, 0)
                if new_aig.is_complemented(new_lit):
                    s = (~s) & MASK
        if aig.is_complemented(old_lit):
            s = (~s) & MASK
        return s

    for i in range(aig.n_nodes - 1, -1, -1):
        entry = aig._nodes[i]
        if entry[0] != 'and':
            continue
        nid = i + 1
        cy = care[nid]
        if cy == 0:
            continue
        _, a_lit, b_lit = entry
        ia = aig.node_of(a_lit)
        ib = aig.node_of(b_lit)
        sa = current_sig(ia, a_lit)
        sb = current_sig(ib, b_lit)
        if ia > 0:
            care[ia] |= cy & sb
        if ib > 0:
            care[ib] |= cy & sa
    return care


# ═══════════════════════════════════════════════════════════════════════════════
#  V3 — forward-flip window-local ODC (ROADMAP P0#1 step 5)
# ═══════════════════════════════════════════════════════════════════════════════

def _propagate_care_sim_window(
    aig:       AIG,
    out_lits:  List[AIGLit],
    sig_old:   Dict[int, int],
    W:         int,
    depth_K:   int,
) -> Dict[int, int]:
    """
    Window-local care propagation via forward-flip simulation.

    For each AND node ``v``, flip ``sig_old[v]`` and propagate through its
    fanout in old_aig up to ``depth_K`` levels. ``care[v]`` is the disjunction
    of bit-differences at the window boundary — primary outputs reached
    within the window plus fanout nodes at exactly depth ``depth_K`` (beyond
    those the analysis is conservative and assumes the flip is observed).

    Complexity: ``O(n_nodes * avg_fanout_cone_size(K) * W / 64)``. For
    typical AIGs with fanout ~3 and K=5, this is ``O(n * 250 * W/64)``
    — slower than the one-shot global sweep but localises reconvergence
    effects. Combined with the standard admissibility check, the
    resulting care is a conservative upper bound on true new_aig care
    for any depth < K rewrite chain, regardless of sibling drift.

    Sound-by-construction guarantee: if ``depth_K`` exceeds the maximum
    fanout-depth of the circuit, the result equals the global ODC; for
    smaller ``K`` it is strictly more conservative (larger care, fewer
    DC bits), which trades QoR for soundness-under-composition.
    """
    MASK = (1 << W) - 1

    # PO nodes and their index for boundary detection
    po_nids: Set[int] = set()
    for lit in out_lits:
        nid = aig.node_of(lit)
        if nid > 0:
            po_nids.add(nid)

    fanout_idx = _build_fanout_index(aig)

    # Reusable arrays: flipped_sig[nid] indexed by nid
    # We compute care for every AND node in the AIG.
    care: Dict[int, int] = {0: 0}
    for i in range(aig.n_nodes):
        care[i + 1] = 0
    for nid in po_nids:
        care[nid] = MASK

    for seed_nid in range(1, aig.n_nodes + 1):
        entry = aig._nodes[seed_nid - 1]
        if entry[0] != 'and':
            continue
        if seed_nid in po_nids:
            # care[PO] is already MASK, flipping is always observable
            continue

        flipped: Dict[int, int] = {seed_nid: (~sig_old.get(seed_nid, 0)) & MASK}
        current: List[int] = [seed_nid]
        boundary_diff = 0
        reached_po = False

        for _level in range(depth_K):
            next_level: List[int] = []
            for n in current:
                for child_nid in fanout_idx.get(n, []):
                    if child_nid in flipped:
                        continue  # already processed at an earlier level (reconvergence)
                    child_entry = aig._nodes[child_nid - 1]
                    if child_entry[0] != 'and':
                        continue
                    _, la, lb = child_entry
                    ia, ib = aig.node_of(la), aig.node_of(lb)
                    sa = flipped.get(ia, sig_old.get(ia, 0))
                    if aig.is_complemented(la):
                        sa = (~sa) & MASK
                    sb = flipped.get(ib, sig_old.get(ib, 0))
                    if aig.is_complemented(lb):
                        sb = (~sb) & MASK
                    fs = sa & sb
                    flipped[child_nid] = fs
                    next_level.append(child_nid)
                    # If this child is a PO, observe the flip immediately.
                    if child_nid in po_nids:
                        diff = (fs ^ sig_old.get(child_nid, 0)) & MASK
                        boundary_diff |= diff
                        reached_po = True
            current = next_level
            if not current:
                break

        # Boundary = nodes at exactly depth_K still unfinished (conservative:
        # count any non-zero flip there) plus POs reached earlier.
        if current and not reached_po:
            for b_nid in current:
                diff = (flipped.get(b_nid, 0) ^ sig_old.get(b_nid, 0)) & MASK
                boundary_diff |= diff
        elif current and reached_po:
            # Still count depth_K-boundary flips as observable.
            for b_nid in current:
                diff = (flipped.get(b_nid, 0) ^ sig_old.get(b_nid, 0)) & MASK
                boundary_diff |= diff

        care[seed_nid] = boundary_diff & MASK

    return care


# ═══════════════════════════════════════════════════════════════════════════════
#  Simulation-based helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _cut_pattern_bits(
    sig_old:      Dict[int, int],
    ordered_cut:  List[int],
    x:            int,
    W:            int,
) -> int:
    """
    W-bit mask: bit p set iff on simulation pattern p, cut signals take
    exactly the k-bit assignment ``x``.
    """
    MASK = (1 << W) - 1
    match = MASK
    for j, cid in enumerate(ordered_cut):
        sig = sig_old[cid]
        if (x >> j) & 1:
            match &= sig
        else:
            match &= (~sig) & MASK
    return match


def _project_cut_care(
    sig_old:      Dict[int, int],
    care_sim_v:   int,
    ordered_cut:  List[int],
    tt:           int,
    W:            int,
) -> Tuple[int, int]:
    """
    Project per-pattern care mask into 2^k-bit cut-pattern space.

    A cut pattern x is CARED iff some simulation pattern p has
    ``cut_signals(p) == x`` AND ``care_sim_v[p] == 1``. Cut patterns never
    witnessed by simulation are left as DC — a sound under-approximation;
    the admissibility check in ``dc_optimize`` re-validates on the same sim.

    Returns ``(care_mask, care_value)`` where care_mask is a 2^k-bit integer
    marking cared cut patterns and care_value is ``tt`` restricted to them.
    """
    k = len(ordered_cut)
    V = 1 << k
    care_mask = 0
    for x in range(V):
        hit = _cut_pattern_bits(sig_old, ordered_cut, x, W)
        if hit & care_sim_v:
            care_mask |= (1 << x)
    care_value = tt & care_mask
    return care_mask, care_value


# ═══════════════════════════════════════════════════════════════════════════════
#  Template signature evaluation (admissibility check)
# ═══════════════════════════════════════════════════════════════════════════════

def _template_signature(
    ops:          List[Tuple[int, int]],
    tmpl_out:     int,
    ordered_cut:  List[int],
    sig_new:      Dict[int, int],
    lit_map:      Dict[int, int],
    new_aig:      AIG,
    W:            int,
) -> Optional[int]:
    """
    Evaluate an AIG_DB_4 / exact-synth template on new_aig's current cut
    signatures. Returns the W-bit signature of the template's output, or
    None if a template literal is undefined (malformed template).

    Literal convention (matches aig_db_4 / exact_synthesis):
        0          -> FALSE
        1          -> TRUE
        2 + 2*j    -> cut input j (positive)
        3 + 2*j    -> cut input j (negative)
        2+2*n+2*g  -> op g (positive)
        3+2*n+2*g  -> op g (negative)
    """
    MASK = (1 << W) - 1
    lit_to_sig: Dict[int, int] = {0: 0, 1: MASK}

    for j, c_id in enumerate(ordered_cut):
        new_lit = lit_map[c_id * 2]
        new_nid = new_aig.node_of(new_lit)
        s = sig_new.get(new_nid, 0) if new_nid > 0 else 0
        # lit_map always maps a positive old lit to a new lit — which may
        # itself be complemented if an upstream rewrite collapsed the node
        # into something it needs to invert (e.g. structural-hash matches
        # the negation of an existing node). Handle both cases.
        if new_aig.is_complemented(new_lit):
            s = (~s) & MASK
        lit_to_sig[2 + 2 * j] = s
        lit_to_sig[3 + 2 * j] = (~s) & MASK

    op_base = 2 + 2 * len(ordered_cut)
    for g, (ta, tb) in enumerate(ops):
        if ta not in lit_to_sig or tb not in lit_to_sig:
            return None
        v = lit_to_sig[ta] & lit_to_sig[tb]
        lit_to_sig[op_base + 2 * g]     = v
        lit_to_sig[op_base + 2 * g + 1] = (~v) & MASK

    return lit_to_sig.get(tmpl_out)


def _and_signature(
    sig_new:  Dict[int, int],
    a_lit:    int,
    b_lit:    int,
    aig_new:  AIG,
    W:        int,
) -> int:
    """W-bit signature of AND(a_lit, b_lit) given current ``sig_new``."""
    MASK = (1 << W) - 1

    def _sig_of_lit(lit: int) -> int:
        nid = aig_new.node_of(lit)
        if nid == 0:
            # Constant literals: lit=0 -> FALSE (all zeros); lit=1 -> TRUE (all ones)
            return MASK if aig_new.is_complemented(lit) else 0
        s = sig_new.get(nid, 0)
        if aig_new.is_complemented(lit):
            s = (~s) & MASK
        return s

    return _sig_of_lit(a_lit) & _sig_of_lit(b_lit)


def _sync_z3_new(
    new_aig:       AIG,
    z3_exprs_new:  Dict[int, object],
    n_before:      int,
) -> None:
    """Fill z3_exprs_new for any AND nodes added to new_aig after n_before."""
    import z3 as _z3

    def _z3_of_lit(lit: int) -> object:
        nid = new_aig.node_of(lit)
        if nid == 0:
            return _z3.BoolVal(True) if new_aig.is_complemented(lit) else _z3.BoolVal(False)
        e = z3_exprs_new.get(nid)
        if e is None:
            return None
        return _z3.Not(e) if new_aig.is_complemented(lit) else e

    for i in range(n_before, new_aig.n_nodes):
        nid = i + 1
        entry = new_aig._nodes[i]
        if entry[0] == 'and' and nid not in z3_exprs_new:
            _, la, lb = entry
            ea = _z3_of_lit(la)
            eb = _z3_of_lit(lb)
            if ea is not None and eb is not None:
                z3_exprs_new[nid] = _z3.And(ea, eb)


def _z3_resub_admissible(
    z3_exprs_new:  Dict[int, object],
    new_aig:       AIG,
    resub_lits:    Tuple[int, ...],
    old_v_z3:      object,
    out_lits_z3:   List[object],
    v_z3:          object,
    timeout_ms:    int = 2000,
    odc_formula:   Optional[object] = None,
) -> bool:
    """
    Z3-exact admissibility check for 0-gate or 1-gate resub.

    ``resub_lits`` is a tuple of 1 lit (0-gate) or 2 lits (1-gate). The
    proposed replacement function is (for 1-lit) the lit's function, or (for
    2-lits) AND of both functions. Check: the proposed function agrees with
    old_v on all patterns where v's value matters for POs (ODC_v = 1).

    If ``odc_formula`` is supplied (precomputed via :func:`_build_odc_formula`),
    it is used directly, avoiding the per-call ``z3.substitute`` cost.
    """
    import z3 as _z3

    def _z3_of_lit(lit: int) -> Optional[object]:
        nid = new_aig.node_of(lit)
        if nid == 0:
            return _z3.BoolVal(True) if new_aig.is_complemented(lit) else _z3.BoolVal(False)
        e = z3_exprs_new.get(nid)
        if e is None:
            return None
        return _z3.Not(e) if new_aig.is_complemented(lit) else e

    if len(resub_lits) == 1:
        candidate_z3 = _z3_of_lit(resub_lits[0])
    else:
        ea = _z3_of_lit(resub_lits[0])
        eb = _z3_of_lit(resub_lits[1])
        if ea is None or eb is None:
            return False
        candidate_z3 = _z3.And(ea, eb)
    if candidate_z3 is None:
        return False

    if odc_formula is None:
        # Build ODC on the fly from out_lits_z3 (legacy path).
        odc_clauses = [_z3.Xor(po, _z3.substitute(po, (v_z3, _z3.Not(v_z3))))
                       for po in out_lits_z3]
        odc_formula = _z3.Or(*odc_clauses) if odc_clauses else _z3.BoolVal(False)

    s = _z3.Solver()
    s.set('timeout', timeout_ms)
    s.add(odc_formula)
    s.add(_z3.Xor(candidate_z3, old_v_z3))
    return s.check() == _z3.unsat


def _sync_sig_new(
    aig_new:       AIG,
    sig_new:       Dict[int, int],
    n_before:      int,
    W:             int,
    level_new:     Optional[Dict[int, int]] = None,
) -> None:
    """
    Fill ``sig_new`` for any AND nodes added to ``aig_new`` after node index
    ``n_before``. Idempotent on already-filled ids.

    When ``level_new`` is supplied, also fills per-node topological level
    (inputs = 0, ANDs = max(fanin levels) + 1) for use by the topology-aware
    resub window in :func:`_scan_resub_1gate`.
    """
    for i in range(n_before, aig_new.n_nodes):
        nid = i + 1
        entry = aig_new._nodes[i]
        if entry[0] == 'and' and nid not in sig_new:
            _, la, lb = entry
            sig_new[nid] = _and_signature(sig_new, la, lb, aig_new, W)
        if level_new is not None and nid not in level_new:
            if entry[0] == 'input':
                level_new[nid] = 0
            elif entry[0] == 'and':
                _, la, lb = entry
                na = aig_new.node_of(la)
                nb = aig_new.node_of(lb)
                level_new[nid] = max(
                    level_new.get(na, 0), level_new.get(nb, 0)
                ) + 1


def _compute_levels_old(aig: AIG) -> Dict[int, int]:
    """Per-node topological level in the reference AIG (inputs = 0)."""
    level: Dict[int, int] = {0: 0}
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            level[nid] = 0
        elif entry[0] == 'and':
            _, a, b = entry
            level[nid] = max(
                level.get(aig.node_of(a), 0),
                level.get(aig.node_of(b), 0),
            ) + 1
    return level


def _scan_resub_0gate(
    target:   int,
    care_v:   int,
    sig_new:  Dict[int, int],
    MASK:     int,
) -> Optional[int]:
    """
    Find a new_aig literal whose signature matches ``target`` on every
    cared bit. Returns the literal (``nid*2`` or ``nid*2+1``) or None.
    Constants (FALSE/TRUE) are always considered first.
    """
    if (target & care_v) == 0:
        return FALSE
    if ((MASK ^ target) & care_v) == 0:
        return TRUE
    for nid, sig in sig_new.items():
        if nid == 0:
            continue
        if ((sig ^ target) & care_v) == 0:
            return nid * 2
        if (((sig ^ MASK) ^ target) & care_v) == 0:
            return nid * 2 + 1
    return None


def _scan_resub_1gate(
    target:       int,
    care_v:       int,
    sig_new:      Dict[int, int],
    MASK:         int,
    max_window:   int,
    level_new:    Optional[Dict[int, int]] = None,
    target_level: Optional[int] = None,
) -> Tuple[Optional[Tuple[int, int]], int, int]:
    """
    Find a pair of new_aig literals whose AND signature matches ``target``
    on every cared bit.

    Returns ``(pair, n_examined, n_dropped_by_window)`` where ``pair`` is
    ``(a_lit, b_lit)`` or ``None``; ``n_examined`` is the number of ordered
    node pairs (u_nid ≤ v_nid) actually iterated in the hot loop;
    ``n_dropped_by_window`` is the number of pairs excluded purely because
    the signal set was truncated to ``max_window``.

    Window selection:
      • When ``level_new`` and ``target_level`` are supplied and the signal
        pool exceeds ``max_window``, the window keeps the ``max_window``
        nodes whose new_aig level is closest to ``target_level``
        (topology-aware — signals near the rewrite site are more likely
        to be structurally relevant resub sources).
      • Otherwise falls back to the FIFO tail (most-recently-added N),
        which approximates topology order but drifts when earlier rewrites
        grow the graph unevenly.
    """
    items: List[Tuple[int, int]] = [
        (nid, sig) for nid, sig in sig_new.items() if nid > 0
    ]
    total = len(items)

    if total > max_window:
        if level_new is not None and target_level is not None:
            items.sort(
                key=lambda it: abs(level_new.get(it[0], 0) - target_level)
            )
            items = items[:max_window]
        else:
            items = items[-max_window:]
        # Pairs including u == v: total*(total+1)/2 over the full pool,
        # max_window*(max_window+1)/2 kept. Difference = pairs dropped
        # purely due to window truncation.
        dropped_pairs = (
            total * (total + 1) // 2
            - max_window * (max_window + 1) // 2
        )
    else:
        dropped_pairs = 0

    n_examined = 0
    result: Optional[Tuple[int, int]] = None
    for i_idx, (u_nid, u_sig) in enumerate(items):
        if result is not None:
            break
        u_pos = u_sig
        u_neg = (~u_sig) & MASK
        for v_nid, v_sig in items[i_idx:]:
            n_examined += 1
            v_pos = v_sig
            v_neg = (~v_sig) & MASK
            matched = False
            for u_lit, s_u in ((u_nid * 2, u_pos), (u_nid * 2 + 1, u_neg)):
                if matched:
                    break
                for v_lit, s_v in ((v_nid * 2, v_pos), (v_nid * 2 + 1, v_neg)):
                    if (((s_u & s_v) ^ target) & care_v) == 0:
                        result = (u_lit, v_lit)
                        matched = True
                        break
            if matched:
                break
    return result, n_examined, dropped_pairs


def _reconstruct_from_old(
    aig:       AIG,
    new_aig:   AIG,
    old_id:    int,
    sig_old:   Dict[int, int],
    sig_new:   Dict[int, int],
    lit_map:   Dict[int, int],
    W:         int,
    level_new: Optional[Dict[int, int]] = None,
) -> Optional[int]:
    """
    Last-resort fallback when the admissibility check rejects every template
    and no resub candidate exists: walk ``aig``'s DAG backwards from ``old_id``
    and rebuild the original AND-tree in ``new_aig`` on top of ancestors
    whose ``sig_new`` still matches ``sig_old`` exactly.

    For each ancestor, reuse the current ``lit_map`` mapping if its signature
    matches sig_old bit-for-bit; otherwise recurse into the ancestor's fanins
    and structurally re-create the AND in new_aig. Inputs always match
    (PI signatures never change), so recursion terminates.

    Returns the new_aig literal whose signature equals ``sig_old[old_id]``
    on every PI pattern, or ``None`` if the rebuild could not finish.
    """
    MASK = (1 << W) - 1
    memo: Dict[int, int] = {}

    def rec(oid: int) -> Optional[int]:
        if oid == 0:
            return FALSE
        if oid in memo:
            return memo[oid]

        current_lit = lit_map.get(oid * 2)
        if current_lit is not None:
            cnid = new_aig.node_of(current_lit)
            csig = sig_new.get(cnid, 0) if cnid > 0 else 0
            if new_aig.is_complemented(current_lit):
                csig = (~csig) & MASK
            if csig == sig_old.get(oid, 0):
                memo[oid] = current_lit
                return current_lit

        entry = aig._nodes[oid - 1]
        if entry[0] == 'input':
            if current_lit is not None:
                memo[oid] = current_lit
                return current_lit
            return None

        _, oa, ob = entry
        ra = rec(aig.node_of(oa))
        if ra is None:
            return None
        rb = rec(aig.node_of(ob))
        if rb is None:
            return None
        if aig.is_complemented(oa):
            ra ^= 1
        if aig.is_complemented(ob):
            rb ^= 1

        n_before = new_aig.n_nodes
        result = new_aig.make_and(ra, rb)
        _sync_sig_new(new_aig, sig_new, n_before, W, level_new)
        memo[oid] = result
        return result

    import sys as _sys
    old_limit = _sys.getrecursionlimit()
    if old_limit < aig.n_nodes + 200:
        _sys.setrecursionlimit(aig.n_nodes + 200)
    try:
        return rec(old_id)
    finally:
        _sys.setrecursionlimit(old_limit)


# ═══════════════════════════════════════════════════════════════════════════════
#  DC-aware NPN database lookup
# ═══════════════════════════════════════════════════════════════════════════════

_MAX_DC_ENUM = 12   # beyond this many DC bits, skip enumeration (degenerate fns)


def _pad_care_to_16(
    care_mask:   int,
    care_value:  int,
    k:           int,
) -> Tuple[int, int]:
    """
    Extend a k-input care set to the 16-bit space used by AIG_DB_4.

    For every cared k-bit pattern y with value v, all 16-bit extensions
    ``y | (h << k)`` for h ∈ [0, 2^(4-k)) are cared with value v — the
    function doesn't depend on the high variables, so the DB_4 template
    is free to ignore them.
    """
    if k >= 4:
        return care_mask, care_value
    new_mask  = 0
    new_value = 0
    n_high    = 1 << (4 - k)
    for y in range(1 << k):
        if (care_mask >> y) & 1:
            v = (care_value >> y) & 1
            for h in range(n_high):
                x = y | (h << k)
                new_mask |= (1 << x)
                if v:
                    new_value |= (1 << x)
    return new_mask, new_value


def _dc_aware_db_lookup(
    care_mask:   int,
    care_value:  int,
    k:           int,
) -> Optional[Tuple[int, List[Tuple[int, int]]]]:
    """
    Return the AIG_DB_4 template with the fewest AND ops whose truth table
    agrees with ``care_value`` on every position where ``care_mask`` is 1.

    For k < 4, extra variables are treated as don't-cares (the function
    doesn't depend on them, so the DB template can ignore them).
    """
    care_mask, care_value = _pad_care_to_16(care_mask, care_value, k)

    dc_positions: List[int] = [x for x in range(16) if not (care_mask >> x) & 1]
    n_dc = len(dc_positions)

    if n_dc > _MAX_DC_ENUM:
        best: Optional[Tuple[int, List[Tuple[int, int]]]] = None
        best_len = 10**9
        for fill in (0, (1 << 16) - 1):
            tt = care_value | (fill & ~care_mask & ((1 << 16) - 1))
            tmpl = AIG_DB_4.get(tt)
            if tmpl is not None and len(tmpl[1]) < best_len:
                best, best_len = tmpl, len(tmpl[1])
        return best

    best = None
    best_len = 10**9

    for combo in range(1 << n_dc):
        tt = care_value
        for j, pos in enumerate(dc_positions):
            if (combo >> j) & 1:
                tt |= (1 << pos)
        tmpl = AIG_DB_4.get(tt)
        if tmpl is None:
            continue
        n_ops = len(tmpl[1])
        if n_ops < best_len:
            best, best_len = tmpl, n_ops
            if best_len == 0:
                break

    return best


# ═══════════════════════════════════════════════════════════════════════════════
#  Care-mask computation (ODC from global sim care + optional Z3 SDC)
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_care_mask(
    aig:            AIG,
    sig_old:        Dict[int, int],
    z3_exprs:       Optional[dict],
    care_sim:       Dict[int, int],
    node_id:        int,
    ordered_cut:    List[int],
    tt:             int,
    W:              int,
    use_sdc:        bool,
    use_odc:        bool,
    timeout_ms:     int,
) -> Tuple[int, int]:
    """
    Return ``(care_mask, care_value)`` for the 2^k cut-pattern space.

    ODC contribution comes from the pre-computed global ``care_sim[node_id]``
    projected onto the cut. SDC contribution is optional: patterns never
    witnessed by simulation whose unsatisfiability is proved by Z3 are
    marked as DC beyond what sim already gives.

    ``care_mask`` bit x is 1 iff cut pattern x is CARED (not a DC).
    ``care_value`` is ``tt`` restricted to cared positions.
    """
    k = len(ordered_cut)
    V = 1 << k
    full = (1 << V) - 1

    if use_odc:
        # ODC projection marks every unwitnessed pattern as DC (sim-DC under-
        # approximation). Admissibility check in dc_optimize re-validates.
        care_mask, _care_value = _project_cut_care(
            sig_old, care_sim.get(node_id, 0), ordered_cut, tt, W
        )
    else:
        # No ODC: start fully cared and let SDC (if enabled) remove proven DCs.
        care_mask = full

    if use_sdc and z3_exprs is not None:
        import z3
        witnessed = 0
        for x in range(V):
            if _cut_pattern_bits(sig_old, ordered_cut, x, W):
                witnessed |= (1 << x)
        # Patterns currently marked cared but never witnessed by simulation:
        # try to prove UNSAT; if proved, remove from care_mask (genuine SDC).
        candidates = care_mask & ~witnessed
        x = 0
        while candidates:
            if candidates & 1:
                s = z3.Solver()
                s.set('timeout', timeout_ms)
                for j, cid in enumerate(ordered_cut):
                    bit = (x >> j) & 1
                    s.add(z3_exprs[cid] == z3.BoolVal(bool(bit)))
                if s.check() == z3.unsat:
                    care_mask &= ~(1 << x)
            candidates >>= 1
            x += 1

    care_value = tt & care_mask
    return care_mask, care_value


# ═══════════════════════════════════════════════════════════════════════════════
#  Z3-exact per-template admissibility check (ROADMAP P0#1 step 4b)
# ═══════════════════════════════════════════════════════════════════════════════

def _z3_build_template_expr(
    ops:          List[Tuple[int, int]],
    tmpl_out:     int,
    ordered_cut:  List[int],
    z3_exprs_old: Dict[int, object],
    lit_map:      Dict[int, int],
    aig:          AIG,
) -> Optional[object]:
    """
    Build a Z3 Boolean expression for a template applied to old_aig's cut
    signals. Returns None if any template literal refers to an unmapped node.

    Literal convention (same as _template_signature / aig_db_4):
        0          -> FALSE
        1          -> TRUE
        2 + 2*j    -> cut input j (positive)
        3 + 2*j    -> cut input j (negative)
        2+2*n+2*g  -> op g (positive)
        3+2*n+2*g  -> op g (negative)
    """
    import z3
    n = len(ordered_cut)
    lit_to_z3: Dict[int, object] = {0: z3.BoolVal(False), 1: z3.BoolVal(True)}

    for j, c_id in enumerate(ordered_cut):
        expr = z3_exprs_old.get(c_id)
        if expr is None:
            return None
        lit_to_z3[2 + 2 * j] = expr
        lit_to_z3[3 + 2 * j] = z3.Not(expr)

    op_base = 2 + 2 * n
    for g, (ta, tb) in enumerate(ops):
        if ta not in lit_to_z3 or tb not in lit_to_z3:
            return None
        v = z3.And(lit_to_z3[ta], lit_to_z3[tb])
        lit_to_z3[op_base + 2 * g]     = v
        lit_to_z3[op_base + 2 * g + 1] = z3.Not(v)

    return lit_to_z3.get(tmpl_out)


def _build_odc_formula(
    z3_exprs_old: Dict[int, object],
    v_nid:        int,
    aig:          AIG,
    out_lits:     List[AIGLit],
) -> Optional[object]:
    """
    Build the ODC formula for node ``v_nid``: a Z3 Boolean expression that is
    True iff flipping v's value changes at least one primary output.

    Computed via ``z3.substitute``: for each PO formula, substitute ``v_expr``
    with ``Not(v_expr)`` and XOR with the original. Returns ``None`` if
    ``v_expr`` is not found in ``z3_exprs_old``.

    Cost: O(n_POs × formula_size). Cache the result per ``v_nid`` to avoid
    recomputing for every cut/template of the same node.
    """
    import z3 as _z3
    v_expr = z3_exprs_old.get(v_nid)
    if v_expr is None:
        return None
    odc_clauses: List[object] = []
    for po_lit in out_lits:
        po_nid = aig.node_of(po_lit)
        if po_nid == 0:
            continue
        pe = z3_exprs_old.get(po_nid)
        if pe is None:
            continue
        if aig.is_complemented(po_lit):
            pe = _z3.Not(pe)
        pf = _z3.substitute(pe, (v_expr, _z3.Not(v_expr)))
        odc_clauses.append(_z3.Xor(pe, pf))
    if not odc_clauses:
        return _z3.BoolVal(False)
    return _z3.Or(*odc_clauses)


def _z3_template_admissible(
    z3_exprs_old:  Dict[int, object],
    v_nid:         int,
    tmpl_out:      int,
    ops:           List[Tuple[int, int]],
    ordered_cut:   List[int],
    lit_map:       Dict[int, int],
    aig:           AIG,
    out_lits:      List[AIGLit],
    timeout_ms:    int = 2000,
    use_odc:       bool = False,
    odc_formula:   Optional[object] = None,
) -> bool:
    """
    Z3-exact admissibility: return True iff the template T is sound for node v.

    Without ODC (``use_odc=False``, default): checks T ≡ old_v globally —
    no DC exploitation, but guaranteed sound for any circuit size.

    With ODC (``use_odc=True``): additionally allows T to differ from old_v
    on patterns where flipping v does NOT change any PO. The ODC formula is
    built via Z3 substitution, which is exact (not sim-based) but adds some
    overhead per call.

    Both variants are O(one SAT call) per template — roughly 0.5–2ms on
    router-sized circuits.
    """
    import z3

    tmpl_z3 = _z3_build_template_expr(
        ops, tmpl_out, ordered_cut, z3_exprs_old, lit_map, aig
    )
    if tmpl_z3 is None:
        return False

    v_expr = z3_exprs_old.get(v_nid)
    if v_expr is None:
        return False

    # Difference expression: template disagrees with old_v.
    diff = z3.Xor(tmpl_z3, v_expr)

    if not use_odc:
        # Conservative path: T must equal old_v on ALL patterns.
        s = z3.Solver()
        s.set('timeout', timeout_ms)
        s.add(diff)
        return s.check() == z3.unsat

    # ODC path: allow T to differ on patterns where v's flip does not change
    # any PO. Use pre-built ODC formula if supplied; otherwise build it here.
    if odc_formula is None:
        odc_formula = _build_odc_formula(z3_exprs_old, v_nid, aig, out_lits)

    if odc_formula is None:
        return False

    # Admissible iff ¬ ∃ PI in ODC_v where T ≠ old_v.
    s = z3.Solver()
    s.set('timeout', timeout_ms)
    s.add(odc_formula)  # PI must be in v's ODC
    s.add(diff)         # template must disagree with old_v on that PI
    return s.check() == z3.unsat


# ═══════════════════════════════════════════════════════════════════════════════
#  End-of-pass formal miter (safety net)
# ═══════════════════════════════════════════════════════════════════════════════

def _miter_equivalent(
    old_aig:   AIG,
    old_outs:  List[AIGLit],
    new_aig:   AIG,
    new_outs:  List[AIGLit],
    timeout_ms: int = 10_000,
) -> bool:
    """
    Return True iff (old_aig, old_outs) ≡ (new_aig, new_outs) on every PI
    assignment. Used as a soundness safety net: if this check fails, the
    caller reverts to the original AIG.
    """
    import z3

    if len(old_outs) != len(new_outs):
        return False

    _, old_exprs = _build_z3_exprs(old_aig)
    _, new_exprs = _build_z3_exprs(new_aig)

    diffs = []
    for lo, ln in zip(old_outs, new_outs):
        oid = old_aig.node_of(lo)
        nid = new_aig.node_of(ln)
        eo  = old_exprs[oid] if oid > 0 else z3.BoolVal(False)
        en  = new_exprs[nid] if nid > 0 else z3.BoolVal(False)
        if old_aig.is_complemented(lo):
            eo = z3.Not(eo)
        if new_aig.is_complemented(ln):
            en = z3.Not(en)
        diffs.append(z3.Xor(eo, en))

    s = z3.Solver()
    s.set('timeout', timeout_ms)
    s.add(z3.Or(*diffs) if diffs else z3.BoolVal(False))
    return s.check() == z3.unsat


# ═══════════════════════════════════════════════════════════════════════════════
#  Main pass
# ═══════════════════════════════════════════════════════════════════════════════

# Exhaustive PI enumeration when input count ≤ this threshold gives perfect
# admissibility coverage (1 << n_inputs sim patterns). For wider circuits we
# fall back to random sampling and the adaptive-W retry wrapper below.
SIM_ENUM_THRESHOLD = 14


# Counters updated by dc_optimize on each call; inspected by dc_stats / tests.
_last_stats: Dict[str, int] = {
    'n_templates_admitted':       0,
    'n_templates_rejected':       0,   # rejected by admissibility check
    'n_safety_net_reverts':       0,
    'n_nodes_rewritten':          0,
    'n_fallthrough_repairs':      0,   # fallthrough inadmissible → template forced
    'n_fallthrough_inadmissible': 0,   # fallthrough used despite being inadmissible
    'n_exact_solved':             0,   # exact_synthesize returned a template
    'n_exact_admitted':           0,   # exact-synth template accepted by admissibility
    'n_resub_0gate':              0,   # primary rewrite via 0-gate resub
    'n_resub_1gate':              0,   # primary rewrite via 1-gate resub
    'n_resub_1gate_examined':     0,   # 1-gate resub node-pairs iterated
    'n_resub_1gate_dropped_by_window': 0,  # pairs skipped due to window truncation
    'n_sim_retries':              0,   # adaptive-W retries after safety-net revert
    'final_sim_W':                0,   # simulation width of last successful pass
    'n_inputs':                   0,   # primary-input count of the input AIG
    'n_care_refreshes':           0,   # hybrid/window mid-pass care recomputes (V3)
    'odc_mode':                   0,   # 0=legacy, 1=hybrid, 2=window (V3)
}


def last_dc_stats() -> Dict[str, int]:
    """Return the counters updated by the most recent ``dc_optimize`` call."""
    return dict(_last_stats)


# Guards the `dc --odc` soundness-gap warning from firing multiple times per
# top-level dc_optimize() call: the multi-round wrapper and adaptive-W retry
# re-enter the same function, and we want exactly one message per user call.
_ODC_WARN_DEPTH: int = 0

# Input-count threshold above which `use_odc=True` is known to be unreliable
# on reconvergent-fanout circuits (ROADMAP P0#1).
ODC_SOUNDNESS_WARN_THRESHOLD: int = 20


def dc_optimize(
    aig:               AIG,
    out_lits:          List[AIGLit],
    cut_size:          int   = 4,
    n_sim_patterns:    int   = 128,
    timeout_ms:        int   = 1000,
    use_sdc:           bool  = True,
    use_odc:           bool  = False,
    use_exact:         bool  = False,
    exact_max_gates:   int   = 5,
    exact_timeout_ms:  int   = 2000,
    use_resub:         bool  = True,
    resub_window:      int   = 64,
    rounds:            int   = 1,
    max_nodes:         Optional[int] = None,
    safety_check:      bool  = True,
    adaptive_sim:      bool  = False,
    max_sim_W:         int   = 16384,
    odc_mode:          str   = ODC_MODE_LEGACY,
    care_refresh_every: int  = 0,
    window_depth:      int   = 5,
) -> Tuple[AIG, List[AIGLit]]:
    """
    Rewrite ``aig`` using don't-care-aware local substitutions (V2).

    For each AND node, enumerate its k-feasible cuts; for each cut, compute
    the care set from a global reverse-topological care propagation and
    look up the cheapest AIG_DB_4 template consistent with it. The template
    replaces the node only if (1) it reduces the global AND count under the
    MFFC cost model and (2) it passes the simulation-based admissibility
    check against the reference circuit's signatures.

    Requires z3-solver for the end-of-pass miter and for SDC Z3-proofs;
    returns the AIG unchanged if Z3 is unavailable.

    Parameters
    ----------
    cut_size       : max cut size k (≤ 4 uses AIG_DB_4 directly).
    n_sim_patterns : simulation word width (more bits → tighter care sets).
    timeout_ms     : per-SAT-query Z3 timeout (used only for SDC proofs +
                     end-of-pass miter).
    use_sdc        : enable Z3-proved satisfiability don't-cares (default True).
    use_odc        : enable observability don't-cares via global care
                     propagation (default False — switch on once V2 is
                     validated downstream).
    use_exact      : enable SAT-based exact synthesis as a fallback for
                     cuts of size >4 where AIG_DB_4 has no entry. The
                     admissibility check from V2.a re-validates each
                     exact-synth template, catching encoder bugs.
    exact_max_gates: upper bound on gate count passed to exact_synthesize.
    exact_timeout_ms: per-k Z3 timeout for exact_synthesize (ms).
    use_resub      : enable V2.c window resubstitution (0-gate and 1-gate
                     signal reuse). Requires ``use_odc`` since the scan
                     needs ``care_sim``. Default True.
    resub_window   : max number of new_aig signals considered per node
                     for 1-gate resub. When the pool exceeds this cap,
                     the window keeps signals whose new_aig topological
                     level is closest to the level of ``old_id`` in the
                     reference AIG (topology-aware selection). 0-gate
                     resub always scans all signals. Instrumentation
                     (``n_resub_1gate_examined`` and
                     ``n_resub_1gate_dropped_by_window`` in
                     :func:`last_dc_stats`) reports hot-loop iterations
                     vs. pairs skipped purely from window truncation.
    rounds         : number of iterative DC passes (V2.b). Each round
                     re-simulates and re-propagates care from the current
                     AIG, so later rounds can find reductions that were
                     hidden by structural state from earlier rounds. The
                     loop exits early once a round produces no net
                     reduction. Default 1 (single pass).
    max_nodes      : optional cap on AND nodes visited.
    safety_check   : run a final Z3 miter between input and output AIGs;
                     on failure, revert to the input. Always active when
                     ``use_odc`` is True.
    adaptive_sim   : if True and the safety-net miter reverts the pass,
                     re-run with ``n_sim_patterns`` doubled (up to
                     ``max_sim_W``). Default **False** — the V2.d EPFL
                     probe (router/priority/i2c/sin) showed reverts on
                     large-input circuits are caused by V2 soundness gaps
                     under reconvergent fanout, not sim undersampling:
                     every retry at 2× W also reverts, so the loop only
                     burns CPU. Kept as an opt-in for when a user has
                     evidence their revert is sim-coverage-bound.
    max_sim_W      : cap on adaptive-retry sim width. Default 16384
                     (≈2KB/signature at this width — fine up to ~50k-node
                     AIGs memory-wise).
    odc_mode       : V3 ODC computation mode. Three choices:
                     - ``'legacy'`` (default, Mishchenko 2009 one-shot).
                       Known-buggy under reconvergent fanout (ROADMAP P0#1);
                       keeps V2.d behaviour bit-for-bit.
                     - ``'hybrid'``: start with legacy care, refresh every
                       ``care_refresh_every`` rewrites using
                       :func:`_propagate_care_sim_hybrid`. Siblings resolve
                       through ``lit_map`` so drift from already-committed
                       rewrites tightens care for future rewrites.
                     - ``'window'``: replace global care with
                       :func:`_propagate_care_sim_window` (forward-flip
                       per-node, depth=``window_depth``). Conservative
                       under composition — analysis stays within each
                       node's bounded fanout window.
    care_refresh_every : refresh interval for ``odc_mode='hybrid'``. 0
                     disables refresh (behaves like legacy even in hybrid
                     mode). Default 0; invocations like
                     ``dc_optimize(..., odc_mode='hybrid', care_refresh_every=20)``
                     trigger a drift-aware propagate after every 20
                     rewritten nodes.
    window_depth   : fanout depth K for ``odc_mode='window'``. Default 5.
                     Larger K approaches global care; smaller K is more
                     conservative (more patterns cared → fewer DC) but
                     robust to longer rewrite chains.
    """
    global _ODC_WARN_DEPTH
    if _ODC_WARN_DEPTH == 0 and use_odc:
        n_inputs_top = len(aig.input_names())
        if n_inputs_top > ODC_SOUNDNESS_WARN_THRESHOLD:
            import sys
            print(
                f"WARN: dc --odc has known soundness gap on reconvergent-fanout "
                f"circuits (n_inputs={n_inputs_top} > {ODC_SOUNDNESS_WARN_THRESHOLD}); "
                f"see ROADMAP.md P0#1. The safety-net miter may revert the pass, "
                f"producing 0% QoR gain.",
                file=sys.stderr,
            )
    if odc_mode not in (ODC_MODE_LEGACY, ODC_MODE_HYBRID, ODC_MODE_WINDOW, ODC_MODE_Z3_EXACT):
        raise ValueError(
            f"Unknown odc_mode {odc_mode!r}; choose one of "
            f"{ODC_MODE_LEGACY!r}, {ODC_MODE_HYBRID!r}, "
            f"{ODC_MODE_WINDOW!r}, {ODC_MODE_Z3_EXACT!r}."
        )
    if odc_mode == ODC_MODE_HYBRID and care_refresh_every < 0:
        raise ValueError("care_refresh_every must be >= 0")
    if odc_mode == ODC_MODE_WINDOW and window_depth < 1:
        raise ValueError("window_depth must be >= 1")
    _ODC_WARN_DEPTH += 1
    try:
        return _dc_optimize_dispatch(
            aig, out_lits,
            cut_size=cut_size,
            n_sim_patterns=n_sim_patterns,
            timeout_ms=timeout_ms,
            use_sdc=use_sdc,
            use_odc=use_odc,
            use_exact=use_exact,
            exact_max_gates=exact_max_gates,
            exact_timeout_ms=exact_timeout_ms,
            use_resub=use_resub,
            resub_window=resub_window,
            rounds=rounds,
            max_nodes=max_nodes,
            safety_check=safety_check,
            adaptive_sim=adaptive_sim,
            max_sim_W=max_sim_W,
            odc_mode=odc_mode,
            care_refresh_every=care_refresh_every,
            window_depth=window_depth,
        )
    finally:
        _ODC_WARN_DEPTH -= 1


def _dc_optimize_dispatch(
    aig:               AIG,
    out_lits:          List[AIGLit],
    cut_size:          int,
    n_sim_patterns:    int,
    timeout_ms:        int,
    use_sdc:           bool,
    use_odc:           bool,
    use_exact:         bool,
    exact_max_gates:   int,
    exact_timeout_ms:  int,
    use_resub:         bool,
    resub_window:      int,
    rounds:            int,
    max_nodes:         Optional[int],
    safety_check:      bool,
    adaptive_sim:      bool,
    max_sim_W:         int,
    odc_mode:          str,
    care_refresh_every: int,
    window_depth:      int,
) -> Tuple[AIG, List[AIGLit]]:
    """Inner dispatch for :func:`dc_optimize`. Handles multi-round iteration
    and adaptive-W retries. Kept separate so the outer wrapper can own the
    one-shot soundness-gap warning without duplicating control flow.
    """
    # Multi-round wrapper (V2.b). Each round re-simulates the current AIG
    # and re-propagates care from scratch, so later rounds can find DC
    # opportunities that were structurally hidden in earlier rounds.
    if rounds > 1:
        cur_aig  = aig
        cur_out  = list(out_lits)
        accum: Dict[str, int] = {k: 0 for k in _last_stats}
        for _r in range(rounds):
            prev_nodes = cur_aig.n_nodes
            cur_aig, cur_out = dc_optimize(
                cur_aig, cur_out,
                cut_size=cut_size,
                n_sim_patterns=n_sim_patterns,
                timeout_ms=timeout_ms,
                use_sdc=use_sdc,
                use_odc=use_odc,
                use_exact=use_exact,
                exact_max_gates=exact_max_gates,
                exact_timeout_ms=exact_timeout_ms,
                use_resub=use_resub,
                resub_window=resub_window,
                rounds=1,
                max_nodes=max_nodes,
                safety_check=safety_check,
                adaptive_sim=adaptive_sim,
                max_sim_W=max_sim_W,
                odc_mode=odc_mode,
                care_refresh_every=care_refresh_every,
                window_depth=window_depth,
            )
            for k in _last_stats:
                # final_sim_W is a width, not a tally — take the max across
                # rounds instead of summing.
                if k == 'final_sim_W':
                    accum[k] = max(accum[k], _last_stats[k])
                elif k == 'n_inputs':
                    accum[k] = _last_stats[k]
                else:
                    accum[k] += _last_stats[k]
            if cur_aig.n_nodes >= prev_nodes:
                break
        _last_stats.update(accum)
        return cur_aig, cur_out

    _last_stats.update({
        'n_templates_admitted':       0,
        'n_templates_rejected':       0,
        'n_safety_net_reverts':       0,
        'n_nodes_rewritten':          0,
        'n_fallthrough_repairs':      0,
        'n_fallthrough_inadmissible': 0,
        'n_exact_solved':             0,
        'n_exact_admitted':           0,
        'n_resub_0gate':              0,
        'n_resub_1gate':              0,
        'n_resub_1gate_examined':     0,
        'n_resub_1gate_dropped_by_window': 0,
        'n_sim_retries':              0,
        'final_sim_W':                0,
        'n_inputs':                   len(aig.input_names()),
        'n_care_refreshes':           0,
        'odc_mode':                   {
            ODC_MODE_LEGACY: 0,
            ODC_MODE_HYBRID: 1,
            ODC_MODE_WINDOW: 2,
            ODC_MODE_Z3_EXACT: 3,
        }[odc_mode],
    })

    # Adaptive-W retry wrapper (V2.d). When the safety-net miter reverts a
    # pass on a circuit with n_inputs > SIM_ENUM_THRESHOLD, the revert almost
    # always indicates sim undersampling of the admissibility check — doubling
    # W and re-running typically resolves it. The loop caps at ``max_sim_W``;
    # for circuits where exhaustive PI enumeration already runs, no retries
    # are attempted (the first-pass coverage is already perfect).
    n_inputs_here = len(aig.input_names())
    exhaustive_regime = n_inputs_here <= SIM_ENUM_THRESHOLD
    cur_W   = n_sim_patterns
    # Capped at 2 based on the V2.d EPFL probe: on every circuit where the
    # first pass reverts, the second retry at 2×W reverts too — further
    # doubling just compounds the wasted work.
    MAX_RETRIES = 2

    result_aig = aig
    result_out: List[AIGLit] = list(out_lits)

    for attempt in range(MAX_RETRIES + 1):
        result_aig, result_out, reverted = _dc_optimize_once(
            aig, out_lits,
            cut_size=cut_size,
            n_sim_patterns=cur_W,
            timeout_ms=timeout_ms,
            use_sdc=use_sdc,
            use_odc=use_odc,
            use_exact=use_exact,
            exact_max_gates=exact_max_gates,
            exact_timeout_ms=exact_timeout_ms,
            use_resub=use_resub,
            resub_window=resub_window,
            max_nodes=max_nodes,
            safety_check=safety_check,
            odc_mode=odc_mode,
            care_refresh_every=care_refresh_every,
            window_depth=window_depth,
        )
        if not reverted:
            break
        if not adaptive_sim or exhaustive_regime or cur_W >= max_sim_W:
            break
        cur_W = min(cur_W * 2, max_sim_W)
        _last_stats['n_sim_retries'] += 1

    _last_stats['final_sim_W'] = cur_W
    return result_aig, result_out


def _dc_optimize_once(
    aig:               AIG,
    out_lits:          List[AIGLit],
    cut_size:          int,
    n_sim_patterns:    int,
    timeout_ms:        int,
    use_sdc:           bool,
    use_odc:           bool,
    use_exact:         bool,
    exact_max_gates:   int,
    exact_timeout_ms:  int,
    use_resub:         bool,
    resub_window:      int,
    max_nodes:         Optional[int],
    safety_check:      bool,
    odc_mode:          str = ODC_MODE_LEGACY,
    care_refresh_every: int = 0,
    window_depth:      int = 5,
) -> Tuple[AIG, List[AIGLit], bool]:
    """
    Single adaptive-sim-W attempt. Does not reset counters (the outer
    :func:`dc_optimize` owns stats lifecycle). Returns
    ``(new_aig, new_out, reverted)`` where ``reverted`` is True iff the
    safety-net miter tripped and the original AIG was returned.
    """
    try:
        import z3  # noqa: F401
    except ImportError:
        return aig, list(out_lits), False

    if aig.n_ands == 0:
        return aig, list(out_lits), False

    # Exhaustive PI enumeration when input count is small — gives perfect
    # admissibility coverage. Always prefer exhaustive when it fits: random
    # sampling at W=2^n has ~37% collision rate (birthday) and misses ~46% of
    # patterns on average, which was the root cause of spurious reverts on
    # ctrl.aig (n=7) at W=128 in the V2.d EPFL probe.
    input_names_list = aig.input_names()
    n_inputs = len(input_names_list)
    if n_inputs <= SIM_ENUM_THRESHOLD:
        n_sim_patterns = 1 << n_inputs
        patterns = {
            name: sum(
                ((p >> j) & 1) << p
                for p in range(n_sim_patterns)
            )
            for j, name in enumerate(input_names_list)
        }
    else:
        rng = random.Random(0)
        patterns = {name: rng.getrandbits(n_sim_patterns)
                    for name in input_names_list}

    # Pre-compute _z3_exact_active flag (needed for need_z3 decision below).
    _z3_exact_active_pre = (
        use_odc
        and odc_mode == ODC_MODE_Z3_EXACT
        and aig.n_ands <= 3000
    )

    MASK    = (1 << n_sim_patterns) - 1
    sig_old = _simulate(aig, patterns, n_sim_patterns)
    need_z3 = use_sdc or _z3_exact_active_pre
    _, z3_exprs = _build_z3_exprs(aig) if need_z3 else (None, None)

    if use_odc:
        if odc_mode == ODC_MODE_WINDOW:
            care_sim = _propagate_care_sim_window(
                aig, out_lits, sig_old, n_sim_patterns, window_depth
            )
        else:
            # 'legacy' and 'hybrid' both start from the Mishchenko 2009 one-shot.
            # Hybrid will refresh mid-pass via _propagate_care_sim_hybrid.
            care_sim = _propagate_care_sim(aig, out_lits, sig_old, n_sim_patterns)
    else:
        care_sim = {}

    cuts    = enumerate_cuts(aig, k=cut_size)
    ref_old = _compute_ref_counts(aig, out_lits)
    # Reference-AIG levels feed the topology-aware resub window: when the
    # new_aig signal pool exceeds resub_window, we keep signals whose
    # new_aig level is closest to the level of old_id in the reference.
    level_old = _compute_levels_old(aig) if use_resub else {}

    new_aig = AIG()
    lit_map: Dict[int, int] = {FALSE: FALSE, TRUE: TRUE}
    sig_new: Dict[int, int] = {0: 0}
    level_new: Dict[int, int] = {} if use_resub else None  # type: ignore[assignment]

    # z3-exact mode: maintain Z3 formulas for new_aig nodes (over old_aig PIs),
    # enabling exact admissibility and resub checks for all admitted signals.
    #
    # Performance notes:
    # • Per-SAT call: ~0.7ms for small (257-AND) circuits; ~5–10ms for large
    #   (1000–5000 AND) circuits (deeper formula = harder SAT).
    # • z3-exact is well-suited for medium circuits (n_ands ≤ ~2000). For
    #   larger circuits the overhead exceeds the QoR benefit. Two thresholds:
    #   1. n_ands × n_POs > 50_000: skip ODC, use exact-equivalence (T ≡ old_v).
    #   2. n_ands > 3000: disable z3-exact entirely, fall back to legacy.
    #      At this size per-SAT cost is ~10ms × O(5×n_ands) checks = minutes.
    _z3_exact_active = False
    _z3_exact_use_odc = False
    z3_exprs_new: Optional[Dict[int, object]] = None
    odc_formula_cache: Optional[Dict[int, object]] = None

    if use_odc and odc_mode == ODC_MODE_Z3_EXACT:
        if aig.n_ands <= 3000:
            _z3_exact_active = True  # Matches _z3_exact_active_pre
            _n_pos = sum(1 for lo in out_lits if aig.node_of(lo) > 0)
            _z3_exact_use_odc = aig.n_ands * _n_pos <= 50_000
            import z3 as _z3_mod
            z3_exprs_new = {0: _z3_mod.BoolVal(False)}
            odc_formula_cache = {} if _z3_exact_use_odc else None
        # If n_ands > 3000: silently fall back to legacy admissibility. The
        # end-of-pass safety-net miter still guards against unsound rewrites.

    nodes_visited = 0

    for i, entry in enumerate(aig._nodes):
        old_id = i + 1

        if entry[0] == 'input':
            nlit = new_aig.make_input(entry[1])
            new_nid = new_aig.node_of(nlit)
            sig_new[new_nid] = patterns.get(entry[1], 0) & MASK
            if level_new is not None:
                level_new[new_nid] = 0
            if z3_exprs_new is not None and z3_exprs is not None:
                z3_exprs_new[new_nid] = z3_exprs.get(old_id, _z3_mod.BoolVal(False))
            lit_map[old_id * 2]     = nlit
            lit_map[old_id * 2 + 1] = nlit ^ 1
            continue

        # V3 hybrid ODC refresh (step 4): every ``care_refresh_every`` nodes,
        # recompute care_sim using the current new_aig + sig_new state so that
        # drifted siblings of already-committed rewrites tighten care for the
        # remaining topological sweep.
        if (use_odc
                and odc_mode == ODC_MODE_HYBRID
                and care_refresh_every > 0
                and nodes_visited > 0
                and nodes_visited % care_refresh_every == 0):
            care_sim = _propagate_care_sim_hybrid(
                aig, out_lits, sig_old, sig_new, lit_map, new_aig,
                n_sim_patterns,
            )
            _last_stats['n_care_refreshes'] += 1

        _, old_a, old_b = entry
        new_a = lit_map[old_a]
        new_b = lit_map[old_b]

        base_exists = new_aig.has_and(new_a, new_b)
        base_cost   = 0 if base_exists else 1

        # Fallthrough admissibility: structural AND of already-rewritten
        # fanins may diverge on care[v] bits if upstream rewrites introduced
        # uncared-at-upstream perturbations that compose at v's fanin AND.
        # Computing this up-front lets us force a corrective template below
        # when the fallthrough would break soundness.
        if use_odc:
            fall_sig = _and_signature(sig_new, new_a, new_b, new_aig, n_sim_patterns)
            fall_admissible = (
                ((fall_sig ^ sig_old[old_id]) & care_sim.get(old_id, MASK)) == 0
            )
        else:
            fall_admissible = True

        do_dc = (max_nodes is None) or (nodes_visited < max_nodes)
        nodes_visited += 1

        best_choice: Optional[Tuple[int, List[int], int, List[Tuple[int, int]]]] = None
        best_net = base_cost
        # Fallback pool when fallthrough is inadmissible: the cheapest
        # admissible template regardless of MFFC cost.
        best_admissible_choice: Optional[Tuple[int, List[int], int, List[Tuple[int, int]]]] = None
        best_admissible_net = 10**9
        # Largest MFFC observed across cuts — used as the savings baseline
        # for V2.c resub (which doesn't fix a particular cut but still frees
        # old_id's fanout-free cone when it replaces the node).
        max_mffc_size = 0

        if do_dc:
            for cut in cuts[old_id]:
                if len(cut) <= 1 or len(cut) > cut_size or old_id in cut:
                    continue

                tt, ordered_cut = evaluate_cut_tt(aig, old_id, cut)
                k = len(ordered_cut)

                care_mask, care_value = _compute_care_mask(
                    aig, sig_old, z3_exprs, care_sim, old_id,
                    ordered_cut, tt, n_sim_patterns,
                    use_sdc, use_odc, timeout_ms,
                )

                template = _dc_aware_db_lookup(care_mask, care_value, k)
                from_exact = False
                if template is None and use_exact and k > 4:
                    # V2.d: AIG_DB_4 covers only k ≤ 4; for wider cuts
                    # fall back to SAT-based exact synthesis with the
                    # same care mask. The admissibility check below still
                    # runs, so any encoder bug can at worst cause
                    # rejection, never unsoundness.
                    from .exact_synthesis import exact_synthesize
                    V_k  = 1 << k
                    full = (1 << V_k) - 1
                    dc_mask = full & ~care_mask
                    template = exact_synthesize(
                        care_value, k,
                        max_gates=exact_max_gates,
                        dc=dc_mask,
                        timeout_ms=exact_timeout_ms,
                    )
                    if template is not None:
                        _last_stats['n_exact_solved'] += 1
                        from_exact = True
                if template is None:
                    continue

                tmpl_out, ops = template

                # Admissibility check: the template must reproduce v's
                # reference value on every cared pattern.
                #
                # Two modes (selected by odc_mode):
                #   V2 (legacy/hybrid/window): sim-based — fast, but W random
                #     patterns may miss the bad PI assignment on large circuits.
                #   Z3-exact: uses Z3 to prove T ≡ old_v (or T matches on
                #     ODC_v patterns) across ALL PI assignments — no sampling
                #     blind-spot. Closes the admissibility coverage gap for
                #     circuits with >20 inputs (ROADMAP P0#1 step 4b).
                if use_odc and odc_mode != ODC_MODE_Z3_EXACT:
                    cand_sig = _template_signature(
                        ops, tmpl_out, ordered_cut, sig_new, lit_map,
                        new_aig, n_sim_patterns,
                    )
                    if cand_sig is None:
                        continue
                    if (cand_sig ^ sig_old[old_id]) & care_sim.get(old_id, MASK) != 0:
                        _last_stats['n_templates_rejected'] += 1
                        continue
                elif use_odc and _z3_exact_active:
                    # Z3-exact path: prove T is admissible in old_aig.
                    # use_odc=True here enables ODC-aware DC bits in the DB
                    # lookup above; the Z3 check validates them exactly.
                    # Cache ODC(v) across all cuts/templates for this node.
                    if odc_formula_cache is not None:
                        if old_id not in odc_formula_cache:
                            odc_formula_cache[old_id] = _build_odc_formula(
                                z3_exprs, old_id, aig, out_lits
                            )
                        _odc_f = odc_formula_cache[old_id]
                    else:
                        _odc_f = None
                    ok = _z3_template_admissible(
                        z3_exprs, old_id,
                        tmpl_out, ops, ordered_cut,
                        lit_map, aig, out_lits,
                        timeout_ms=timeout_ms,
                        use_odc=_z3_exact_use_odc,
                        odc_formula=_odc_f,
                    )
                    if not ok:
                        _last_stats['n_templates_rejected'] += 1
                        continue
                if from_exact:
                    _last_stats['n_exact_admitted'] += 1

                mffc      = _compute_mffc(aig, old_id, cut, ref_old)
                mffc_size = len(mffc)
                if mffc_size > max_mffc_size:
                    max_mffc_size = mffc_size

                n_new, final_lit = _count_template_new_nodes(
                    new_aig, ordered_cut, tmpl_out, ops, k, lit_map,
                )
                if final_lit is None:
                    continue

                net = n_new - mffc_size
                if net < best_net:
                    best_net    = net
                    best_choice = (tmpl_out, ordered_cut, k, ops)
                if n_new < best_admissible_net:
                    best_admissible_net    = n_new
                    best_admissible_choice = (tmpl_out, ordered_cut, k, ops)

        # When fallthrough is not admissible, we MUST use a template that
        # reproduces sig_old[v] on care[v] — otherwise the care-violation
        # cascades and the safety-net miter trips. Pick the cheapest
        # admissible template (by absolute new-node count, not net), even
        # if it doesn't beat MFFC cost. If no admissible template exists,
        # keep best_choice as is and let the safety-net miter catch it.
        if use_odc and not fall_admissible and best_admissible_choice is not None:
            best_choice = best_admissible_choice
            _last_stats['n_fallthrough_repairs'] += 1

        # V2.c — Window resubstitution as a PRIMARY rewrite path. When an
        # existing signal already satisfies v's cared signature, we can
        # replace v with it for free (0 gates) and reclaim its MFFC.
        # A 1-gate AND of two existing signals saves ``mffc - 1`` gates
        # in the same way. Competes with ``best_choice`` on MFFC cost.
        primary_resub_0: Optional[int] = None
        primary_resub_1: Optional[Tuple[int, int]] = None
        if use_odc and use_resub and do_dc and max_mffc_size > 0:
            target = sig_old[old_id]
            care_v = care_sim.get(old_id, MASK)

            # 0-gate resub: signal already exists.
            net_0 = -max_mffc_size
            if net_0 < best_net:
                cand = _scan_resub_0gate(target, care_v, sig_new, MASK)
                if cand is not None:
                    # z3-exact mode: verify the candidate signal is truly
                    # admissible for v (checking all PI assignments, not just
                    # the sim-sampled care bits).
                    if (_z3_exact_active
                            and z3_exprs_new is not None
                            and z3_exprs is not None):
                        _v_z3 = z3_exprs.get(old_id)
                        # Use cached ODC formula for resub check too.
                        if odc_formula_cache is not None:
                            if old_id not in odc_formula_cache:
                                odc_formula_cache[old_id] = _build_odc_formula(
                                    z3_exprs, old_id, aig, out_lits
                                )
                            _odc_v = odc_formula_cache[old_id]
                        else:
                            _odc_v = None
                        _po_z3_dummy: List[object] = []  # not used when odc_v supplied
                        if _v_z3 is not None and _z3_resub_admissible(
                            z3_exprs_new, new_aig, (cand,),
                            _v_z3, _po_z3_dummy, _v_z3, timeout_ms,
                            odc_formula=_odc_v if _z3_exact_use_odc else None,
                        ):
                            primary_resub_0 = cand
                            best_net = net_0
                            best_choice = None
                    else:
                        primary_resub_0 = cand
                        best_net = net_0
                        best_choice = None  # resub wins over any template

            # 1-gate resub: AND of two existing signals. Only worthwhile
            # when it beats current best_net.
            if primary_resub_0 is None:
                net_1 = 1 - max_mffc_size
                if net_1 < best_net:
                    pair, n_exam, n_drop = _scan_resub_1gate(
                        target, care_v, sig_new, MASK, resub_window,
                        level_new, level_old.get(old_id),
                    )
                    _last_stats['n_resub_1gate_examined']          += n_exam
                    _last_stats['n_resub_1gate_dropped_by_window'] += n_drop
                    if pair is not None:
                        # z3-exact mode: verify the AND of both candidates.
                        _accept_pair = True
                        if (_z3_exact_active
                                and z3_exprs_new is not None
                                and z3_exprs is not None):
                            _v_z3 = z3_exprs.get(old_id)
                            _po_z3 = []
                            for _pl in out_lits:
                                _pn = aig.node_of(_pl)
                                if _pn == 0:
                                    continue
                                _pe = z3_exprs.get(_pn)
                                if _pe is None:
                                    continue
                                if aig.is_complemented(_pl):
                                    import z3 as _z3m
                                    _pe = _z3m.Not(_pe)
                                _po_z3.append(_pe)
                            if _v_z3 is not None:
                                if odc_formula_cache is not None:
                                    if old_id not in odc_formula_cache:
                                        odc_formula_cache[old_id] = _build_odc_formula(
                                            z3_exprs, old_id, aig, out_lits
                                        )
                                    _odc_pair = odc_formula_cache[old_id]
                                else:
                                    _odc_pair = None
                                _accept_pair = _z3_resub_admissible(
                                    z3_exprs_new, new_aig, pair,
                                    _v_z3, [], _v_z3, timeout_ms,
                                    odc_formula=_odc_pair if _z3_exact_use_odc else None,
                                )
                            else:
                                _accept_pair = False
                        if _accept_pair:
                            primary_resub_1 = pair
                            best_net = net_1
                            best_choice = None

        # Zero-cost / 1-gate signal resub fallback. When neither the
        # fallthrough nor any enumerated template is admissible, scan
        # already-built new_aig signals for one whose signature matches
        # sig_old[v] on care[v]; else scan AND-pairs of signals with
        # polarity. Soundness safety net that keeps the pass away from
        # the outer miter when V2.c's primary path didn't fire.
        resub_applied: Optional[int] = None
        resub_pair:    Optional[Tuple[int, int]] = None
        if (use_odc
                and not fall_admissible
                and best_admissible_choice is None):
            target = sig_old[old_id]
            care_v = care_sim.get(old_id, MASK)
            resub_applied = _scan_resub_0gate(target, care_v, sig_new, MASK)
            if resub_applied is None:
                resub_pair, n_exam, n_drop = _scan_resub_1gate(
                    target, care_v, sig_new, MASK, resub_window,
                    level_new, level_old.get(old_id),
                )
                _last_stats['n_resub_1gate_examined']          += n_exam
                _last_stats['n_resub_1gate_dropped_by_window'] += n_drop

        if primary_resub_0 is not None:
            applied = primary_resub_0
            # 0-gate resub reuses an existing lit — update z3_exprs_new mapping
            # for old_id so downstream nodes see the correct z3 formula.
            if z3_exprs_new is not None:
                _r0_nid = new_aig.node_of(primary_resub_0)
                if _r0_nid > 0:
                    _e = z3_exprs_new.get(_r0_nid)
                    if _e is not None:
                        # Will be recorded via lit_map below.
                        pass
            _last_stats['n_resub_0gate']     += 1
            _last_stats['n_nodes_rewritten'] += 1
        elif primary_resub_1 is not None:
            n_before = new_aig.n_nodes
            applied = new_aig.make_and(primary_resub_1[0], primary_resub_1[1])
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns, level_new)
            if z3_exprs_new is not None:
                _sync_z3_new(new_aig, z3_exprs_new, n_before)
            _last_stats['n_resub_1gate']     += 1
            _last_stats['n_nodes_rewritten'] += 1
        elif best_choice is not None:
            tmpl_out, ordered_cut, k_sel, ops = best_choice
            n_before = new_aig.n_nodes
            applied = _apply_template(
                new_aig, ordered_cut, tmpl_out, ops, k_sel, lit_map,
            )
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns, level_new)
            if z3_exprs_new is not None:
                _sync_z3_new(new_aig, z3_exprs_new, n_before)
            _last_stats['n_templates_admitted'] += 1
            _last_stats['n_nodes_rewritten']    += 1
        elif resub_applied is not None:
            applied = resub_applied
            _last_stats['n_fallthrough_repairs'] += 1
        elif resub_pair is not None:
            n_before = new_aig.n_nodes
            applied = new_aig.make_and(resub_pair[0], resub_pair[1])
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns, level_new)
            if z3_exprs_new is not None:
                _sync_z3_new(new_aig, z3_exprs_new, n_before)
            _last_stats['n_fallthrough_repairs'] += 1
        elif use_odc and not fall_admissible:
            # Last-resort: the fallthrough AND of rewritten fanins would
            # break soundness, no admissible template exists, and no resub
            # candidate matches. Walk the reference AIG's DAG backwards
            # and structurally rebuild the original sub-graph on top of
            # ancestors whose signatures are still intact.
            rebuilt = _reconstruct_from_old(
                aig, new_aig, old_id, sig_old, sig_new, lit_map,
                n_sim_patterns, level_new,
            )
            if rebuilt is not None:
                applied = rebuilt
                _last_stats['n_fallthrough_repairs'] += 1
            else:
                n_before = new_aig.n_nodes
                applied = new_aig.make_and(new_a, new_b)
                _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns, level_new)
                if z3_exprs_new is not None:
                    _sync_z3_new(new_aig, z3_exprs_new, n_before)
                _last_stats['n_fallthrough_inadmissible'] += 1
                import os
                if os.environ.get('NAND_DC_DEBUG'):
                    print(f"    [dc] node old_id={old_id} fallthrough INADMISSIBLE, rebuild failed")
                    target = sig_old[old_id]
                    care_v = care_sim.get(old_id, MASK)
                    print(f"      n_cuts_enumerated = {len(cuts[old_id])}")
                    print(f"      care_v popcount   = {bin(care_v).count('1')}/{n_sim_patterns}")
                    diff_bits = (fall_sig ^ target) & care_v
                    print(f"      fall diff bits    = {bin(diff_bits).count('1')}")
        else:
            n_before = new_aig.n_nodes
            applied = new_aig.make_and(new_a, new_b)
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns, level_new)
            if z3_exprs_new is not None:
                _sync_z3_new(new_aig, z3_exprs_new, n_before)

        lit_map[old_id * 2]     = applied
        lit_map[old_id * 2 + 1] = applied ^ 1

        # Sync z3_exprs_new for 0-gate resub path (reuses existing lit, no new node).
        if z3_exprs_new is not None:
            _z3_new_nid = new_aig.node_of(applied)
            if _z3_new_nid > 0 and _z3_new_nid not in z3_exprs_new:
                # Fallthrough AND that wasn't added above — fill it now.
                _sync_z3_new(new_aig, z3_exprs_new, max(0, _z3_new_nid - 1))

    new_out = [lit_map.get(l, l) for l in out_lits]
    new_aig, new_out = new_aig.gc(new_out)

    # Safety-net miter: V2's admissibility check is sim-based, so sim-
    # undersampled DC claims could in theory slip through. Always active
    # when use_odc is True.
    if safety_check or use_odc:
        try:
            if not _miter_equivalent(aig, out_lits, new_aig, new_out):
                _last_stats['n_safety_net_reverts'] += 1
                return aig, list(out_lits), True
        except Exception:
            _last_stats['n_safety_net_reverts'] += 1
            return aig, list(out_lits), True

    return new_aig, new_out, False


def dc_stats(
    aig:            AIG,
    out_lits:       List[AIGLit],
    cut_size:       int = 4,
    n_sim_patterns: int = 128,
    timeout_ms:     int = 1000,
    use_sdc:        bool = True,
    use_odc:        bool = True,
) -> dict:
    """
    Analyse the AIG without modifying it; return per-pass DC statistics.

    Keys: n_cuts_examined, n_cuts_with_dc, total_dc_bits, n_templates_found.
    """
    try:
        import z3  # noqa: F401
    except ImportError:
        return {'error': 'z3 not installed'}

    if aig.n_ands == 0:
        return {'n_cuts_examined': 0, 'n_cuts_with_dc': 0, 'total_dc_bits': 0,
                'n_templates_found': 0}

    rng       = random.Random(0)
    patterns  = {name: rng.getrandbits(n_sim_patterns)
                 for name in aig.input_names()}
    sig_old   = _simulate(aig, patterns, n_sim_patterns)
    _, z3_exprs = _build_z3_exprs(aig) if use_sdc else (None, None)
    care_sim = _propagate_care_sim(aig, out_lits, sig_old, n_sim_patterns) \
               if use_odc else {}

    cuts = enumerate_cuts(aig, k=cut_size)

    n_examined = 0
    n_with_dc  = 0
    total_dc   = 0
    n_found    = 0

    for i, entry in enumerate(aig._nodes):
        old_id = i + 1
        if entry[0] != 'and':
            continue

        for cut in cuts[old_id]:
            if len(cut) <= 1 or len(cut) > cut_size or old_id in cut:
                continue

            tt, ordered_cut = evaluate_cut_tt(aig, old_id, cut)
            k = len(ordered_cut)
            full = (1 << (1 << k)) - 1

            care_mask, _care_value = _compute_care_mask(
                aig, sig_old, z3_exprs, care_sim, old_id,
                ordered_cut, tt, n_sim_patterns,
                use_sdc, use_odc, timeout_ms,
            )

            n_examined += 1
            dc_bits = bin(full & ~care_mask).count('1')
            if dc_bits:
                n_with_dc += 1
                total_dc  += dc_bits

            if _dc_aware_db_lookup(care_mask, _care_value, k) is not None:
                n_found += 1

    return {
        'n_cuts_examined':   n_examined,
        'n_cuts_with_dc':    n_with_dc,
        'total_dc_bits':     total_dc,
        'n_templates_found': n_found,
    }
