"""
AIG balancing pass — minimize logical depth while preserving area.

The algorithm (one sweep, topological order):

  1. Compute reference counts (fanout) in the old AIG.
  2. Process nodes forward.  For each AND node v:
     a. Recursively collect the AND-tree leaves by descending through
        AND children that have a *single* fanout (ref == 1) and are
        reached via a positive-polarity edge (no inversion boundary).
     b. Map every collected leaf literal into the new AIG via lit_map.
     c. Combine those new-AIG literals into a minimum-depth binary tree
        using a min-heap: at each step pair the two *shallowest* nodes,
        so equal-depth siblings merge first and the critical path grows
        by at most one level per round.

Area is preserved: for n leaves the balanced tree has exactly n-1 AND
nodes, identical to the original chain.  Structural hashing inside
make_and() deduplicates combinations that already exist in new_aig, so
shared sub-expressions across outputs are never duplicated.

Absorbed intermediate nodes (ref == 1) are still emitted standalone when
the topological loop reaches them; the resulting "dead" AIG nodes are
pruned by the backward reachability pass in aig_to_gates().
"""

from __future__ import annotations
import heapq
from typing import Dict, List, Tuple

from ..core.aig import AIG, Lit as AIGLit, FALSE, TRUE
from .rewrite    import _compute_ref_counts


# ═══════════════════════════════════════════════════════════════════════════════
#  Level (depth) computation
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_levels(aig: AIG) -> Dict[int, int]:
    """Level (critical-path depth from any primary input) for each node ID."""
    levels: Dict[int, int] = {0: 0}
    for i, entry in enumerate(aig._nodes):
        node_id = i + 1
        if entry[0] == 'input':
            levels[node_id] = 0
        else:  # 'and' or 'xor' — both cost 1 level
            _, lit_a, lit_b = entry
            lev_a = levels.get(aig.node_of(lit_a), 0)
            lev_b = levels.get(aig.node_of(lit_b), 0)
            levels[node_id] = max(lev_a, lev_b) + 1
    return levels


def aig_depth(aig: AIG, out_lits: List[AIGLit]) -> int:
    """Return the maximum logic depth (critical-path length) of the AIG."""
    if not out_lits:
        return 0
    levels = _compute_levels(aig)
    return max(levels.get(aig.node_of(l), 0) for l in out_lits)


# ═══════════════════════════════════════════════════════════════════════════════
#  AND-tree leaf collection
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_and_leaves(
    node_id: int,
    aig:     AIG,
    ref:     Dict[int, int],
    lit_map: Dict[AIGLit, AIGLit],
) -> List[AIGLit]:
    """
    Recursively collect leaves of the AND tree rooted at node_id in old AIG.

    A child literal is expanded (absorbed into the tree) when:
      • the edge is positive polarity (no inversion across the boundary)
      • the child is an AND node (not a primary input)
      • the child has exactly one fanout (ref == 1)

    All other literals become leaves and are mapped to new-AIG lits via
    lit_map (already populated for all nodes with id < node_id since we
    process in topological order).
    """
    entry = aig._nodes[node_id - 1]
    if entry[0] == 'input':
        return [lit_map[node_id * 2]]

    _, lit_a, lit_b = entry
    leaves: List[AIGLit] = []

    for lit in (lit_a, lit_b):
        child_id = aig.node_of(lit)
        if (
            not aig.is_complemented(lit)              # no inversion boundary
            and child_id > 0                           # not constant node
            and aig._nodes[child_id - 1][0] == 'and'  # is AND, not input
            and ref.get(child_id, 0) == 1              # single consumer
        ):
            leaves.extend(_collect_and_leaves(child_id, aig, ref, lit_map))
        else:
            leaves.append(lit_map[lit])

    return leaves


# ═══════════════════════════════════════════════════════════════════════════════
#  Minimum-depth AND tree construction
# ═══════════════════════════════════════════════════════════════════════════════

def _build_balanced_and(
    new_aig:    AIG,
    leaves:     List[AIGLit],
    lit_levels: Dict[AIGLit, int],
) -> AIGLit:
    """
    Combine leaf literals with AND into the shallowest possible binary tree.

    Uses a min-heap keyed on (level, tiebreaker).  Always pairs the two
    shallowest nodes so equal-depth leaves merge together and the critical
    path grows by at most one level per round.
    """
    if not leaves:
        return TRUE
    if len(leaves) == 1:
        return leaves[0]

    # Build min-heap: (level, index, literal)
    heap: List[Tuple[int, int, AIGLit]] = []
    for idx, lit in enumerate(leaves):
        lev = lit_levels.get(lit, 0)
        heapq.heappush(heap, (lev, idx, lit))

    counter = len(leaves)

    while len(heap) > 1:
        lev_a, _, lit_a = heapq.heappop(heap)
        lev_b, _, lit_b = heapq.heappop(heap)
        new_lit = new_aig.make_and(lit_a, lit_b)
        new_lev = max(lev_a, lev_b) + 1
        lit_levels[new_lit]       = new_lev
        lit_levels[new_lit ^ 1]   = new_lev
        heapq.heappush(heap, (new_lev, counter, new_lit))
        counter += 1

    return heap[0][2]


# ═══════════════════════════════════════════════════════════════════════════════
#  Main pass
# ═══════════════════════════════════════════════════════════════════════════════

def balance_aig(
    old_aig:  AIG,
    out_lits: List[AIGLit],
) -> Tuple[AIG, List[AIGLit]]:
    """
    Restructure an AIG to minimize logical depth while preserving area.

    Parameters
    ----------
    old_aig : AIG
        The AIG to rebalance.
    out_lits : list[int]
        Circuit output literals (used only for reference-count computation).

    Returns
    -------
    new_aig : AIG
        Rebalanced AIG with equivalent Boolean function.
    new_out_lits : list[int]
        Output literals remapped into new_aig.
    """
    ref = _compute_ref_counts(old_aig, out_lits)

    new_aig    = AIG()
    lit_map:    Dict[AIGLit, AIGLit] = {FALSE: FALSE, TRUE: TRUE}
    lit_levels: Dict[AIGLit, int]    = {FALSE: 0, TRUE: 0}

    for i, entry in enumerate(old_aig._nodes):
        old_id = i + 1

        if entry[0] == 'input':
            nlit = new_aig.make_input(entry[1])
            lit_map[old_id * 2]       = nlit
            lit_map[old_id * 2 + 1]   = nlit ^ 1
            lit_levels[nlit]           = 0
            lit_levels[nlit ^ 1]       = 0
            continue

        if entry[0] == 'xor':
            # XOR nodes are not AND-trees; copy through and track depth.
            _, old_a, old_b = entry
            new_a = lit_map[old_a]
            new_b = lit_map[old_b]
            result_lit = new_aig.make_xor(new_a, new_b)
            lev = max(lit_levels.get(new_a, 0), lit_levels.get(new_b, 0)) + 1
            lit_levels[result_lit]       = lev
            lit_levels[result_lit ^ 1]   = lev
            lit_map[old_id * 2]     = result_lit
            lit_map[old_id * 2 + 1] = result_lit ^ 1
            continue

        # AND node: collect leaves and build balanced tree.
        leaves = _collect_and_leaves(old_id, old_aig, ref, lit_map)

        # Build minimum-depth AND tree in new_aig.
        result_lit = _build_balanced_and(new_aig, leaves, lit_levels)

        # Propagate level for the complement if not set by _build_balanced_and.
        if (result_lit ^ 1) not in lit_levels:
            lit_levels[result_lit ^ 1] = lit_levels.get(result_lit, 0)

        lit_map[old_id * 2]     = result_lit
        lit_map[old_id * 2 + 1] = result_lit ^ 1

    new_out_lits = [lit_map[l] for l in out_lits]
    return new_aig, new_out_lits
