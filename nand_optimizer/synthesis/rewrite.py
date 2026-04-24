"""
DAG-aware AIG rewriting pass with global fanout awareness.

For each internal AND node v, enumerate the small k-feasible cuts whose
truth table matches a library template (either the precomputed
:mod:`aig_db_4` or, optionally, on-demand :mod:`exact_synthesis`).  The
replacement is only accepted when it provably reduces the total number
of AND nodes reachable from the circuit's outputs — i.e. the cost
heuristic explicitly accounts for which old nodes can actually be freed
(MFFC, maximum fanout-free cone) and which new nodes would be saved by
structural hashing in the output AIG.

This fixes the historical overeager-replacement bug where a template
substitution would duplicate logic shared with other outputs.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Set, Tuple

from ..core.aig import AIG, Lit as AIGLit, TRUE, FALSE
from ..aig_db_4 import AIG_DB_4

def _xag_db() -> dict:
    """Lazy import of XAG_DB_4 — returns empty dict if pkl missing."""
    try:
        from ..xag_db_4 import XAG_DB_4
        return XAG_DB_4
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  Cut enumeration  (generalised over k ≤ 6)
# ═══════════════════════════════════════════════════════════════════════════════

def enumerate_cuts(aig: AIG, k: int = 4) -> List[List[Set[int]]]:
    """
    For every node v in the AIG, enumerate the set of k-feasible cuts
    (i.e. subsets of ancestors of v with cardinality ≤ k that dominate v).
    Uses the classic bounded-combination recursion.
    """
    n_nodes = aig.n_nodes
    cuts: List[List[Set[int]]] = [[] for _ in range(n_nodes + 1)]
    cuts[0] = [set()]  # constant node

    for i, entry in enumerate(aig._nodes):
        node_id = i + 1
        cuts[node_id].append({node_id})     # trivial cut

        if entry[0] in ('and', 'xor'):
            _, lit_a, lit_b = entry
            id_a = aig.node_of(lit_a)
            id_b = aig.node_of(lit_b)
            for c_a in cuts[id_a]:
                for c_b in cuts[id_b]:
                    c_union = c_a | c_b
                    if len(c_union) <= k and c_union not in cuts[node_id]:
                        cuts[node_id].append(c_union)

    return cuts


# ═══════════════════════════════════════════════════════════════════════════════
#  Cut truth-table evaluation  (generalised over k ≤ 6)
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_cut_tt(aig: AIG, node_id: int, cut: Set[int]) -> Tuple[int, List[int]]:
    """
    Compute the 2^k-bit truth table of ``aig[node_id]`` as a Boolean
    function of the cut variables. Returns (tt_as_int, ordered_cut).
    """
    ordered_cut = sorted(cut)
    k           = len(ordered_cut)
    V           = 1 << k
    mask        = (1 << V) - 1

    tt_val: Dict[int, int] = {0: 0}
    for j, c_id in enumerate(ordered_cut):
        bv = 0
        for m in range(V):
            if (m >> j) & 1:
                bv |= (1 << m)
        tt_val[c_id] = bv

    min_id = min(ordered_cut) if ordered_cut else 0
    for i in range(min_id - 1, node_id):
        if i < 0:
            continue
        curr_id = i + 1
        if curr_id in tt_val:
            continue
        entry = aig._nodes[i]
        if entry[0] not in ('and', 'xor'):
            continue

        _, lit_a, lit_b = entry
        id_a = aig.node_of(lit_a)
        id_b = aig.node_of(lit_b)
        if id_a not in tt_val or id_b not in tt_val:
            continue

        val_a = tt_val[id_a]
        if aig.is_complemented(lit_a):
            val_a = (~val_a) & mask
        val_b = tt_val[id_b]
        if aig.is_complemented(lit_b):
            val_b = (~val_b) & mask

        if entry[0] == 'xor':
            tt_val[curr_id] = val_a ^ val_b
        else:
            tt_val[curr_id] = val_a & val_b

    return tt_val.get(node_id, 0) & mask, ordered_cut


# ═══════════════════════════════════════════════════════════════════════════════
#  Reference counting + MFFC
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_ref_counts(aig: AIG, out_lits: List[int]) -> Dict[int, int]:
    """
    For each AND node id, count the number of fanouts:
        • edges from other AND nodes that reference it
        • circuit outputs that reference it
    """
    ref: Dict[int, int] = {i + 1: 0 for i in range(aig.n_nodes)}
    for entry in aig._nodes:
        if entry[0] in ('and', 'xor'):
            _, a_lit, b_lit = entry
            ida = aig.node_of(a_lit)
            idb = aig.node_of(b_lit)
            if ida > 0:
                ref[ida] = ref.get(ida, 0) + 1
            if idb > 0:
                ref[idb] = ref.get(idb, 0) + 1
    for lit in out_lits:
        nid = aig.node_of(lit)
        if nid > 0:
            ref[nid] = ref.get(nid, 0) + 1
    return ref


def _compute_mffc(
    aig:      AIG,
    root_id:  int,
    cut:      Set[int],
    ref_old:  Dict[int, int],
) -> Set[int]:
    """
    Compute the MFFC (maximum fanout-free cone) of ``root_id`` with
    boundary ``cut``. An AND node n is in the MFFC iff it is reachable
    from the root by descending only through AND nodes, none of which
    escape the cone via an externally-fanning-out edge.

    Returns the set of AND node IDs (inclusive of root_id) that would
    become unused if root_id were replaced.
    """
    mffc: Set[int] = set()
    local_ref: Dict[int, int] = {}

    def kill(n: int) -> None:
        if n == 0 or n in cut or n in mffc:
            return
        entry = aig._nodes[n - 1]
        if entry[0] == 'input':
            return
        mffc.add(n)
        _, a_lit, b_lit = entry  # works for both 'and' and 'xor'
        for child_lit in (a_lit, b_lit):
            ch = aig.node_of(child_lit)
            if ch == 0 or ch in cut:
                continue
            child_entry = aig._nodes[ch - 1]
            if child_entry[0] == 'input':
                continue
            if ch not in local_ref:
                local_ref[ch] = ref_old[ch]
            local_ref[ch] -= 1
            if local_ref[ch] == 0:
                kill(ch)

    kill(root_id)
    return mffc


# ═══════════════════════════════════════════════════════════════════════════════
#  Template cost + apply  (no-mutate cost model)
# ═══════════════════════════════════════════════════════════════════════════════

_VIRTUAL_BASE = 1 << 30                 # far above any realistic lit index


def _count_template_new_nodes(
    new_aig:         AIG,
    ordered_cut:     List[int],
    template_out:    int,
    ops:             List[Tuple[int, int]],
    cut_size:        int,
    lit_map:         Dict[int, int],
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    How many new AND / XOR nodes would applying this template add to ``new_aig``?

    Does NOT mutate ``new_aig``.  Uses structural-hash lookup for collisions
    with existing nodes plus a virtual-literal table to model intra-template
    sharing.  Returns (n_new_and, n_new_xor, final_lit).  If the template is
    malformed or references a literal that is not producible, returns
    (None, None, None).
    """
    sim: Dict[int, int]            = {0: FALSE, 1: TRUE}
    for j, c_id in enumerate(ordered_cut):
        base = lit_map[c_id * 2]
        sim[2 + 2 * j]     = base
        sim[3 + 2 * j + 0] = base                # unused slot (even key above)
        sim[3 + 2 * j]     = base ^ 1

    op_base         = 2 + 2 * cut_size
    virtual_counter = _VIRTUAL_BASE
    virtual_map:     Dict[Tuple[int, int], int] = {}
    n_new_and = 0
    n_new_xor = 0

    def fresh_virtual() -> int:
        nonlocal virtual_counter
        v = virtual_counter
        virtual_counter += 2
        return v

    for op_idx, op_entry in enumerate(ops):
        if len(op_entry) == 3:
            ta, tb, op_kind = op_entry
        else:
            ta, tb = op_entry
            op_kind = 0   # AND (backward compat with AIG_DB_4)
        if ta not in sim or tb not in sim:
            return (None, None, None)
        m_a = sim[ta]
        m_b = sim[tb]

        if op_kind == 1:   # XOR
            # Constant propagation for XOR
            if m_a == FALSE:
                result = m_b
            elif m_b == FALSE:
                result = m_a
            elif m_a == TRUE:
                result = m_b ^ 1
            elif m_b == TRUE:
                result = m_a ^ 1
            elif m_a == m_b:
                result = FALSE
            elif m_a == (m_b ^ 1):
                result = TRUE
            else:
                if m_a > m_b:
                    m_a, m_b = m_b, m_a
                vkey = (1, m_a, m_b)
                existing = new_aig.get_xor(m_a, m_b)
                if existing is not None:
                    result = existing
                elif vkey in virtual_map:
                    result = virtual_map[vkey]
                else:
                    result = fresh_virtual()
                    virtual_map[vkey] = result
                    n_new_xor += 1
        else:              # AND
            if m_a > m_b:
                m_a, m_b = m_b, m_a
            # Constant propagation — replicates AIG.make_and's fast-path.
            if m_a == FALSE:
                result = FALSE
            elif m_a == TRUE:
                result = m_b
            elif m_a == m_b:
                result = m_a
            elif m_a == (m_b ^ 1):
                result = FALSE
            else:
                vkey = (0, m_a, m_b)
                existing = new_aig.get_and(m_a, m_b)
                if existing is not None:
                    result = existing
                elif vkey in virtual_map:
                    result = virtual_map[vkey]
                else:
                    result = fresh_virtual()
                    virtual_map[vkey] = result
                    n_new_and += 1

        sim[op_base + 2 * op_idx]     = result
        sim[op_base + 2 * op_idx + 1] = result ^ 1

    if template_out not in sim:
        return (None, None, None)
    return (n_new_and, n_new_xor, sim[template_out])


def _apply_template(
    new_aig:         AIG,
    ordered_cut:     List[int],
    template_out:    int,
    ops:             List[Tuple[int, int]],
    cut_size:        int,
    lit_map:         Dict[int, int],
) -> int:
    """Actually instantiate the template in ``new_aig``; return its out literal."""
    sim: Dict[int, int] = {0: FALSE, 1: TRUE}
    for j, c_id in enumerate(ordered_cut):
        base = lit_map[c_id * 2]
        sim[2 + 2 * j]     = base
        sim[3 + 2 * j]     = base ^ 1

    op_base = 2 + 2 * cut_size
    for op_idx, op_entry in enumerate(ops):
        if len(op_entry) == 3:
            ta, tb, op_kind = op_entry
        else:
            ta, tb = op_entry
            op_kind = 0   # AND
        m_a = sim[ta]
        m_b = sim[tb]
        if op_kind == 1:
            created = new_aig.make_xor(m_a, m_b)
        else:
            created = new_aig.make_and(m_a, m_b)
        sim[op_base + 2 * op_idx]     = created
        sim[op_base + 2 * op_idx + 1] = created ^ 1

    return sim[template_out]


# ═══════════════════════════════════════════════════════════════════════════════
#  Main rewriter
# ═══════════════════════════════════════════════════════════════════════════════

def rewrite_aig(
    old_aig:          AIG,
    out_lits:         Optional[List[int]]  = None,
    rounds:           int                  = 1,
    cut_size:         int                  = 4,
    use_exact:        bool                 = False,
    exact_max_gates:  int                  = 6,
    exact_timeout_ms: int                  = 2000,
    use_xag:          bool                 = False,
) -> Tuple[AIG, List[int]]:
    """
    Fanout-aware rewriting pass.

    Parameters
    ----------
    old_aig : AIG
        The AIG to rewrite.
    out_lits : list[int] | None
        Circuit output literals (needed for reference counting).
    rounds : int
        Number of rewriting sweeps.
    cut_size : int
        Maximum cut size k.  AIG_DB_4 covers k≤4; larger cuts require
        ``use_exact`` or they'll fall through with no match.
    use_exact : bool
        If True, fall back to :func:`exact_synthesize` for truth tables
        that are outside the 4-input DB.
    exact_max_gates, exact_timeout_ms
        Limits for the SAT-based exact synthesiser.
    """
    if out_lits is None:
        out_lits = []

    xag_db = _xag_db() if use_xag else {}
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

            if entry[0] == 'xor':
                # XOR nodes are not yet rewritten against templates; copy through.
                _, old_a, old_b = entry
                nlit = new_aig.make_xor(lit_map[old_a], lit_map[old_b])
                lit_map[old_id * 2]     = nlit
                lit_map[old_id * 2 + 1] = nlit ^ 1
                continue

            _, old_a, old_b = entry
            new_a = lit_map[old_a]
            new_b = lit_map[old_b]

            # NAND-weighted cost model: AND → 2 NANDs, XOR → 4 NANDs.
            # Default translation emits one AND in new_aig (or reuses existing).
            base_exists = new_aig.has_and(new_a, new_b)
            base_cost   = 0 if base_exists else 2

            # best_choice: None -> base translation.
            best_choice: Optional[Tuple[int, List[int], int, List[Tuple[int, int]]]] = None
            best_net = base_cost          # net new-NAND count (lower is better)

            for cut in cuts[old_id]:
                if len(cut) <= 1:
                    continue
                if len(cut) > cut_size:
                    continue
                if old_id in cut:
                    # trivial self-cut is useless
                    continue

                tt, ordered_cut = evaluate_cut_tt(current_aig, old_id, cut)
                k              = len(ordered_cut)

                # Collect candidate templates: AIG_DB_4 (AND-only) and XAG_DB_4
                # (AND+XOR). The weighted comparator below picks the best.
                candidates: List[Tuple[int, List[Tuple[int, ...]]]] = []
                if k <= 4 and tt in AIG_DB_4:
                    candidates.append(AIG_DB_4[tt])
                if k <= 4 and tt in xag_db:
                    candidates.append(xag_db[tt])
                if not candidates and use_exact:
                    try:
                        from .exact_synthesis import exact_synthesize
                        tmpl = exact_synthesize(
                            tt, k,
                            max_gates   = exact_max_gates,
                            timeout_ms  = exact_timeout_ms,
                        )
                        if tmpl is not None:
                            candidates.append(tmpl)
                    except ImportError:
                        pass

                if not candidates:
                    continue

                # MFFC cost in NAND units: sum weights of each freed node.
                mffc = _compute_mffc(current_aig, old_id, cut, ref_old)
                mffc_nand = 0
                for n in mffc:
                    kind = current_aig._nodes[n - 1][0]
                    mffc_nand += 4 if kind == 'xor' else 2

                for template in candidates:
                    tmpl_out, ops = template
                    n_new_and, n_new_xor, final_lit = _count_template_new_nodes(
                        new_aig, ordered_cut, tmpl_out, ops, k, lit_map,
                    )
                    if final_lit is None:
                        continue

                    # Net change in NAND gates if we apply this template:
                    #   ΔN = (2·n_new_and + 4·n_new_xor) - mffc_nand
                    # (MFFC nodes become dead after replacement; aig_to_gates
                    # drops them, so their NAND cost is recovered.)
                    n_new_nand = 2 * n_new_and + 4 * n_new_xor
                    net = n_new_nand - mffc_nand
                    if net < best_net:
                        best_net    = net
                        best_choice = (tmpl_out, ordered_cut, k, ops)

            if best_choice is not None:
                tmpl_out, ordered_cut, k_sel, ops = best_choice
                applied = _apply_template(
                    new_aig, ordered_cut, tmpl_out, ops, k_sel, lit_map,
                )
            else:
                applied = new_aig.make_and(new_a, new_b)

            lit_map[old_id * 2]     = applied
            lit_map[old_id * 2 + 1] = applied ^ 1

        current_out = [lit_map[l] for l in current_out]
        current_aig, current_out = new_aig.gc(current_out)

    return current_aig, current_out
