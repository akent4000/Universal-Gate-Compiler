"""
BDD-based decomposition via narrow cuts.

Build a ROBDD for each output (or a shared multi-root BDD over groups of
outputs), run sifting reordering to minimise node count, and rebuild the
output cone in the destination AIG directly from the reordered BDD.
Narrow BDD levels — those with few distinct cofactor subgraphs — are
natural decomposition boundaries: the BDD nodes at that level become
shared intermediate signals `h_i` automatically via structural hashing
in the output AIG.

This pass is expensive (`O(|BDD| * iterations)` for sifting, plus
:math:`O(2^{|support|})` to build the BDD from simulation).  Include
only on medium-sized problems — the driver enforces
``|support(f)| <= max_inputs`` per output and falls back to plain
translation otherwise.

Soundness
---------
Each rebuilt output is bake-off against the straightforward node-copy
translation: the pass snapshots the destination AIG, rebuilds from the
BDD, compares the number of *new* AND nodes added to the rebuilt cone
vs the cone size in the source AIG, and rolls back if the BDD form is
not strictly smaller.  Correctness of the BDD rebuild is guaranteed by
the canonical-ITE construction (each BDD node `(var, high, low)`
becomes `ite(var, high, low)` in the AIG).

The pass is a no-op when the `dd` package is unavailable.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Set, Tuple

from .aig import AIG, FALSE, TRUE, Lit as AIGLit


# ═══════════════════════════════════════════════════════════════════════════════
#  Cone / support utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _cone_nodes(aig: AIG, out_lit: AIGLit) -> Set[int]:
    """Set of AND-node IDs reachable from a literal (inputs excluded)."""
    result: Set[int] = set()
    stack  = [aig.node_of(out_lit)]
    while stack:
        nid = stack.pop()
        if nid <= 0 or nid in result:
            continue
        entry = aig._nodes[nid - 1]
        if entry[0] == 'input':
            continue
        result.add(nid)
        _, a, b = entry
        stack.append(aig.node_of(a))
        stack.append(aig.node_of(b))
    return result


def _input_support(aig: AIG, out_lit: AIGLit) -> List[str]:
    """Ordered list of primary input names reachable from ``out_lit``."""
    visited: Set[int] = set()
    names:   List[str] = []
    stack = [aig.node_of(out_lit)]
    while stack:
        nid = stack.pop()
        if nid <= 0 or nid in visited:
            continue
        visited.add(nid)
        entry = aig._nodes[nid - 1]
        if entry[0] == 'input':
            names.append(entry[1])
            continue
        _, a, b = entry
        stack.append(aig.node_of(a))
        stack.append(aig.node_of(b))
    # Stable order by input-declaration sequence in the source AIG
    declared = aig.input_names()
    ordered  = [n for n in declared if n in set(names)]
    return ordered


# ═══════════════════════════════════════════════════════════════════════════════
#  Truth-table evaluation over primary inputs
# ═══════════════════════════════════════════════════════════════════════════════

def _eval_tt_over_inputs(
    aig:     AIG,
    out_lit: AIGLit,
    support: List[str],
) -> int:
    """
    Compute the 2^|support|-bit truth table of ``out_lit`` as a function
    of the ordered primary inputs in ``support``.
    """
    n = len(support)
    V = 1 << n
    mask = (1 << V) - 1

    input_lit_to_idx: Dict[int, int] = {}
    for idx, name in enumerate(support):
        input_lit_to_idx[aig._input_lits[name]] = idx

    # Bit-parallel simulation: node_tt[nid] = W-bit integer
    node_tt: Dict[int, int] = {0: 0}
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            lit = aig._input_lits[entry[1]]
            if lit in input_lit_to_idx:
                idx = input_lit_to_idx[lit]
                bv = 0
                for m in range(V):
                    if (m >> idx) & 1:
                        bv |= 1 << m
                node_tt[nid] = bv
            else:
                # Input not in support — project to 0 (never referenced
                # from out_lit because it's not in the cone).
                node_tt[nid] = 0
        else:
            _, a, b = entry
            va = node_tt[aig.node_of(a)]
            if aig.is_complemented(a):
                va = (~va) & mask
            vb = node_tt[aig.node_of(b)]
            if aig.is_complemented(b):
                vb = (~vb) & mask
            node_tt[nid] = va & vb

    out_val = node_tt[aig.node_of(out_lit)]
    if aig.is_complemented(out_lit):
        out_val = (~out_val) & mask
    return out_val


# ═══════════════════════════════════════════════════════════════════════════════
#  BDD build / realise
# ═══════════════════════════════════════════════════════════════════════════════

def _build_bdd(bdd, var_names: List[str], tt: int):
    """
    Build a BDD node representing a 2^n-bit truth table ``tt``.
    Uses recursive Shannon expansion with memoisation on TT values.
    """
    n  = len(var_names)
    V  = 1 << n
    FULL = (1 << V) - 1

    cache: Dict[Tuple[int, int], object] = {}

    def rec(depth: int, cur_tt: int):
        if depth == n:
            return bdd.true if cur_tt & 1 else bdd.false

        half = 1 << (V >> (depth + 1))  # bits per sub-TT at this depth
        # We're recursing top-down: depth=0 -> first (topmost) variable.
        # Split cur_tt by variable var_names[depth]:
        #   lo_tt (var=0) = bits of cur_tt at positions where that bit is 0
        #   hi_tt (var=1) = bits of cur_tt at positions where that bit is 1
        key = (depth, cur_tt)
        if key in cache:
            return cache[key]

        idx = depth
        # Extract cofactors of cur_tt with respect to variable at bit `idx`
        # in the current minterm (global bit-position), where `depth` slice
        # covers a subset of the global minterm.
        # Simpler: we use full-depth projection.
        # Since we always fix all upper variables implicitly, rebuild the
        # TT into sub-TTs by walking bit-by-bit.
        sub_size = 1 << (n - depth - 1)     # number of minterms per cofactor
        mask_sub = (1 << sub_size) - 1

        lo = 0
        hi = 0
        # The current var_names[depth] corresponds to bit `depth` of the
        # original minterm — but we encode sub-TT indices in terms of the
        # remaining variables below. To keep the logic simple, index the
        # full minterm globally and split directly.
        for local_m in range(1 << (n - depth)):
            global_bit = (cur_tt >> local_m) & 1
            if (local_m >> 0) & 1:
                # this bit is variable at position `depth` in the slice -> hi
                hi |= global_bit << (local_m >> 1)
            else:
                lo |= global_bit << (local_m >> 1)

        lo_node = rec(depth + 1, lo)
        hi_node = rec(depth + 1, hi)
        v = bdd.var(var_names[depth])
        result = bdd.ite(v, hi_node, lo_node)
        cache[key] = result
        return result

    return rec(0, tt)


def _realize_bdd_in_aig(
    new_aig:    AIG,
    bdd_root,
    bdd,
    var_to_lit: Dict[str, AIGLit],
) -> AIGLit:
    """
    Convert a BDD rooted at ``bdd_root`` into AIG literals in ``new_aig``
    via canonical ITE composition.

    ``dd.autoref`` uses complement edges: ``.high`` / ``.low`` always
    return cofactors of the **uncomplemented** regular node, and the
    ``Function.negated`` flag records whether the incoming edge flips
    the overall function.  To stay correct we recurse on the regular
    (uncomplemented) node, memoise its AIG literal, and flip on the way
    out when the incoming edge was negated.
    """
    memo: Dict[int, AIGLit] = {}

    def rec(node) -> AIGLit:
        # Terminals
        if node == bdd.true:
            return TRUE
        if node == bdd.false:
            return FALSE

        is_neg = bool(node.negated)
        base   = (~node) if is_neg else node
        key    = int(base)
        if key not in memo:
            v_lit  = var_to_lit[base.var]
            hi_lit = rec(base.high)
            lo_lit = rec(base.low)
            a_lit  = new_aig.make_and(v_lit,     hi_lit)
            b_lit  = new_aig.make_and(v_lit ^ 1, lo_lit)
            memo[key] = new_aig.make_or(a_lit, b_lit)

        lit = memo[key]
        return lit ^ 1 if is_neg else lit

    return rec(bdd_root)


# ═══════════════════════════════════════════════════════════════════════════════
#  Pass driver
# ═══════════════════════════════════════════════════════════════════════════════

_last_stats: Dict[str, int] = {}


def last_bdd_stats() -> Dict[str, int]:
    """Counters from the most recent :func:`bdd_decompose_aig` call."""
    return dict(_last_stats)


def _dd_available() -> bool:
    try:
        import dd.autoref  # noqa: F401
        return True
    except Exception:
        return False


def bdd_decompose_aig(
    old_aig:    AIG,
    out_lits:   Optional[List[AIGLit]] = None,
    max_inputs: int                    = 16,
    reorder:    bool                   = True,
) -> Tuple[AIG, List[AIGLit]]:
    """
    BDD-guided AIG rebuild: per-output cone canonicalisation through a
    sifting-reordered ROBDD.

    For each output with ``|support| <= max_inputs``:
      1. Translate the AIG to ``new_aig`` (base copy up to that output);
      2. Build the ROBDD of the output's truth table;
      3. Run ``bdd.reorder`` to minimise BDD size (sifting);
      4. Realise the reordered BDD as AIG nodes in ``new_aig``;
      5. Keep the BDD rebuild iff strictly fewer new AND nodes result than
         the base translation would have added.

    Outputs with support larger than ``max_inputs``, or where the `dd`
    package is unavailable, are translated by plain structural copy.
    """
    if out_lits is None:
        out_lits = []

    stats = {
        'outputs_examined':        0,
        'outputs_rebuilt':         0,
        'outputs_skipped_support': 0,
        'outputs_skipped_size':    0,
        'dd_available':            0,
    }

    have_dd = _dd_available()
    stats['dd_available'] = 1 if have_dd else 0

    new_aig = AIG()
    lit_map: Dict[int, int] = {FALSE: FALSE, TRUE: TRUE}
    var_to_lit: Dict[str, AIGLit] = {}

    # Pre-create all inputs in the same declaration order.
    for name in old_aig.input_names():
        nlit = new_aig.make_input(name)
        var_to_lit[name] = nlit
        # Map old input literal → new input literal
        old_lit = old_aig._input_lits[name]
        lit_map[old_lit]       = nlit
        lit_map[old_lit ^ 1]   = nlit ^ 1

    # Copy all AND nodes in topological order (base translation into new_aig).
    for i, entry in enumerate(old_aig._nodes):
        old_id = i + 1
        if entry[0] == 'input':
            continue
        _, a, b = entry
        nlit = new_aig.make_and(lit_map[a], lit_map[b])
        lit_map[old_id * 2]     = nlit
        lit_map[old_id * 2 + 1] = nlit ^ 1

    new_outs: List[AIGLit] = []

    if not have_dd:
        # Plain copy only; report stats and return.
        new_outs = [lit_map[l] for l in out_lits]
        new_aig, new_outs = new_aig.gc(new_outs)
        _last_stats.clear()
        _last_stats.update(stats)
        return new_aig, new_outs

    from dd.autoref import BDD

    for out_lit in out_lits:
        stats['outputs_examined'] += 1

        support = _input_support(old_aig, out_lit)
        if len(support) == 0 or len(support) > max_inputs:
            stats['outputs_skipped_support'] += 1
            new_outs.append(lit_map[out_lit])
            continue

        cone_size = len(_cone_nodes(old_aig, out_lit))
        if cone_size <= 1:
            new_outs.append(lit_map[out_lit])
            continue

        try:
            tt = _eval_tt_over_inputs(old_aig, out_lit, support)
        except Exception:
            new_outs.append(lit_map[out_lit])
            continue

        bdd = BDD()
        bdd.declare(*support)
        root = _build_bdd(bdd, support, tt)
        if reorder:
            try:
                bdd.reorder()
            except Exception:
                pass

        # Bake-off: snapshot, realise BDD, compare new-AND count.
        snap   = new_aig.snapshot()
        before = new_aig.n_nodes
        try:
            new_lit = _realize_bdd_in_aig(new_aig, root, bdd, var_to_lit)
        except Exception:
            new_aig.restore(snap)
            stats['outputs_skipped_size'] += 1
            new_outs.append(lit_map[out_lit])
            continue
        added = new_aig.n_nodes - before

        if added < cone_size:
            new_outs.append(new_lit)
            stats['outputs_rebuilt'] += 1
        else:
            new_aig.restore(snap)
            stats['outputs_skipped_size'] += 1
            new_outs.append(lit_map[out_lit])

    new_aig, new_outs = new_aig.gc(new_outs)

    _last_stats.clear()
    _last_stats.update(stats)
    return new_aig, new_outs
