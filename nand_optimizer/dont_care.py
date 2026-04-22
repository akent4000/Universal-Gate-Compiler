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

from .aig      import AIG, FALSE, TRUE, Lit as AIGLit
from .aig_db_4 import AIG_DB_4
from .rewrite  import (
    enumerate_cuts,
    evaluate_cut_tt,
    _compute_ref_counts,
    _compute_mffc,
    _count_template_new_nodes,
    _apply_template,
)
from .fraig    import _simulate, _build_z3_exprs


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


def _sync_sig_new(
    aig_new:       AIG,
    sig_new:       Dict[int, int],
    n_before:      int,
    W:             int,
) -> None:
    """
    Fill ``sig_new`` for any AND nodes added to ``aig_new`` after node index
    ``n_before``. Idempotent on already-filled ids.
    """
    for i in range(n_before, aig_new.n_nodes):
        nid = i + 1
        if nid in sig_new:
            continue
        entry = aig_new._nodes[i]
        if entry[0] == 'and':
            _, la, lb = entry
            sig_new[nid] = _and_signature(sig_new, la, lb, aig_new, W)


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
    target:     int,
    care_v:     int,
    sig_new:    Dict[int, int],
    MASK:       int,
    max_window: int,
) -> Optional[Tuple[int, int]]:
    """
    Find a pair of new_aig literals whose AND signature matches ``target``
    on every cared bit. Returns (a_lit, b_lit) or None.

    Restricts the search to the most recently added ``max_window`` signals
    to bound the O(window²) pair enumeration.
    """
    items: List[Tuple[int, int]] = [
        (nid, sig) for nid, sig in sig_new.items() if nid > 0
    ]
    if len(items) > max_window:
        # Most-recently-added signals are closest to the current node in
        # topological order and tend to be the most productive resub sources.
        items = items[-max_window:]

    for i_idx, (u_nid, u_sig) in enumerate(items):
        u_pos = u_sig
        u_neg = (~u_sig) & MASK
        for v_nid, v_sig in items[i_idx:]:
            v_pos = v_sig
            v_neg = (~v_sig) & MASK
            for u_lit, s_u in ((u_nid * 2, u_pos), (u_nid * 2 + 1, u_neg)):
                for v_lit, s_v in ((v_nid * 2, v_pos), (v_nid * 2 + 1, v_neg)):
                    if (((s_u & s_v) ^ target) & care_v) == 0:
                        return (u_lit, v_lit)
    return None


def _reconstruct_from_old(
    aig:      AIG,
    new_aig:  AIG,
    old_id:   int,
    sig_old:  Dict[int, int],
    sig_new:  Dict[int, int],
    lit_map:  Dict[int, int],
    W:        int,
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
        _sync_sig_new(new_aig, sig_new, n_before, W)
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
}


def last_dc_stats() -> Dict[str, int]:
    """Return the counters updated by the most recent ``dc_optimize`` call."""
    return dict(_last_stats)


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
    tfo_cap:           int   = 200,    # retained for CLI compatibility; ignored in V2
    max_nodes:         Optional[int] = None,
    safety_check:      bool  = True,
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
                     for 1-gate resub. 0-gate resub always scans all.
    rounds         : number of iterative DC passes (V2.b). Each round
                     re-simulates and re-propagates care from the current
                     AIG, so later rounds can find reductions that were
                     hidden by structural state from earlier rounds. The
                     loop exits early once a round produces no net
                     reduction. Default 1 (single pass).
    tfo_cap        : ignored in V2 (retained for CLI compatibility).
    max_nodes      : optional cap on AND nodes visited.
    safety_check   : run a final Z3 miter between input and output AIGs;
                     on failure, revert to the input. Always active when
                     ``use_odc`` is True.
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
                tfo_cap=tfo_cap,
                max_nodes=max_nodes,
                safety_check=safety_check,
            )
            for k in _last_stats:
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
    })

    try:
        import z3  # noqa: F401
    except ImportError:
        return aig, list(out_lits)

    if aig.n_ands == 0:
        return aig, list(out_lits)

    # Exhaustive PI enumeration when input count is small — gives perfect
    # admissibility coverage and eliminates the one mult4-style safety-net
    # revert caused by sim undersampling. For larger inputs, fall back to
    # random sampling at the requested width.
    input_names_list = aig.input_names()
    n_inputs = len(input_names_list)
    SIM_ENUM_THRESHOLD = 14   # W ≤ 16384 — cheap in bit-parallel
    if n_inputs <= SIM_ENUM_THRESHOLD and (1 << n_inputs) > n_sim_patterns:
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

    MASK    = (1 << n_sim_patterns) - 1
    sig_old = _simulate(aig, patterns, n_sim_patterns)
    _, z3_exprs = _build_z3_exprs(aig) if use_sdc else (None, None)

    care_sim = _propagate_care_sim(aig, out_lits, sig_old, n_sim_patterns) \
               if use_odc else {}

    cuts    = enumerate_cuts(aig, k=cut_size)
    ref_old = _compute_ref_counts(aig, out_lits)

    new_aig = AIG()
    lit_map: Dict[int, int] = {FALSE: FALSE, TRUE: TRUE}
    sig_new: Dict[int, int] = {0: 0}

    nodes_visited = 0

    for i, entry in enumerate(aig._nodes):
        old_id = i + 1

        if entry[0] == 'input':
            nlit = new_aig.make_input(entry[1])
            new_nid = new_aig.node_of(nlit)
            sig_new[new_nid] = patterns.get(entry[1], 0) & MASK
            lit_map[old_id * 2]     = nlit
            lit_map[old_id * 2 + 1] = nlit ^ 1
            continue

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

                # Admissibility check: the template evaluated on new_aig's
                # current cut signatures must reproduce v's reference
                # signature on every cared simulation pattern. This is the
                # core V2 invariant — it catches soundness violations from
                # sequential upstream rewrites that the V1 pass missed.
                if use_odc:
                    cand_sig = _template_signature(
                        ops, tmpl_out, ordered_cut, sig_new, lit_map,
                        new_aig, n_sim_patterns,
                    )
                    if cand_sig is None:
                        continue
                    if (cand_sig ^ sig_old[old_id]) & care_sim.get(old_id, MASK) != 0:
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
                    primary_resub_0 = cand
                    best_net = net_0
                    best_choice = None  # resub wins over any template

            # 1-gate resub: AND of two existing signals. Only worthwhile
            # when it beats current best_net.
            if primary_resub_0 is None:
                net_1 = 1 - max_mffc_size
                if net_1 < best_net:
                    pair = _scan_resub_1gate(
                        target, care_v, sig_new, MASK, resub_window,
                    )
                    if pair is not None:
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
                resub_pair = _scan_resub_1gate(
                    target, care_v, sig_new, MASK, resub_window,
                )

        if primary_resub_0 is not None:
            applied = primary_resub_0
            _last_stats['n_resub_0gate']     += 1
            _last_stats['n_nodes_rewritten'] += 1
        elif primary_resub_1 is not None:
            n_before = new_aig.n_nodes
            applied = new_aig.make_and(primary_resub_1[0], primary_resub_1[1])
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns)
            _last_stats['n_resub_1gate']     += 1
            _last_stats['n_nodes_rewritten'] += 1
        elif best_choice is not None:
            tmpl_out, ordered_cut, k_sel, ops = best_choice
            n_before = new_aig.n_nodes
            applied = _apply_template(
                new_aig, ordered_cut, tmpl_out, ops, k_sel, lit_map,
            )
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns)
            _last_stats['n_templates_admitted'] += 1
            _last_stats['n_nodes_rewritten']    += 1
        elif resub_applied is not None:
            applied = resub_applied
            _last_stats['n_fallthrough_repairs'] += 1
        elif resub_pair is not None:
            n_before = new_aig.n_nodes
            applied = new_aig.make_and(resub_pair[0], resub_pair[1])
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns)
            _last_stats['n_fallthrough_repairs'] += 1
        elif use_odc and not fall_admissible:
            # Last-resort: the fallthrough AND of rewritten fanins would
            # break soundness, no admissible template exists, and no resub
            # candidate matches. Walk the reference AIG's DAG backwards
            # and structurally rebuild the original sub-graph on top of
            # ancestors whose signatures are still intact.
            rebuilt = _reconstruct_from_old(
                aig, new_aig, old_id, sig_old, sig_new, lit_map, n_sim_patterns,
            )
            if rebuilt is not None:
                applied = rebuilt
                _last_stats['n_fallthrough_repairs'] += 1
            else:
                n_before = new_aig.n_nodes
                applied = new_aig.make_and(new_a, new_b)
                _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns)
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
            _sync_sig_new(new_aig, sig_new, n_before, n_sim_patterns)

        lit_map[old_id * 2]     = applied
        lit_map[old_id * 2 + 1] = applied ^ 1

    new_out = [lit_map.get(l, l) for l in out_lits]
    new_aig, new_out = new_aig.gc(new_out)

    # Safety-net miter: V2's admissibility check is sim-based, so sim-
    # undersampled DC claims could in theory slip through. Always active
    # when use_odc is True.
    if safety_check or use_odc:
        try:
            if not _miter_equivalent(aig, out_lits, new_aig, new_out):
                _last_stats['n_safety_net_reverts'] += 1
                return aig, list(out_lits)
        except Exception:
            _last_stats['n_safety_net_reverts'] += 1
            return aig, list(out_lits)

    return new_aig, new_out


def dc_stats(
    aig:            AIG,
    out_lits:       List[AIGLit],
    cut_size:       int = 4,
    n_sim_patterns: int = 128,
    timeout_ms:     int = 1000,
    use_sdc:        bool = True,
    use_odc:        bool = True,
    tfo_cap:        int  = 200,    # ignored in V2
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
