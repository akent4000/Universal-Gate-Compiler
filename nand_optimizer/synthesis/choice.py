"""
Structural choice nodes — ABC ``compress2rs``-style (ROADMAP P3#9).

The rewriter, balancer and FRAIGer are each locally destructive: every pass
overwrites the structure the previous one produced, so a sequence like
``rewrite; fraig; rewrite; balance`` picks a *single* structural variant
per node at every step. This pass keeps several variants alive at once:

  1. Run N different synthesis scripts on *copies* of the incoming AIG,
     producing N structurally different but functionally identical graphs.
  2. Merge all variants into a single combined AIG (inputs match by name,
     outputs collected from every variant).
  3. Simulate to group nodes by canonical Boolean function, then chain
     each class via ``AIG.add_choice``.
  4. Pick the variant whose outputs produce the smallest reachable AND
     count as the primary output set; the other variants remain reachable
     via choice chains, and the downstream rewriter can root cuts at any
     choice alternative (see ``synthesis/rewrite.py``).

The result is a single AIG the rest of the pipeline treats normally — the
only extra state is the choice chain, which all subsequent passes preserve
through their ``gc()``/``compose()`` calls.

References
----------
Mishchenko, Chatterjee, Brayton, "DAG-aware AIG rewriting" (DAC 2006).
Mishchenko et al., "Integrating Logic Synthesis, Technology Mapping, and
Retiming" (ICCAD 2006).
"""

from __future__ import annotations
import copy
import random
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.aig import AIG, FALSE, TRUE


# ─────────────────────────────────────────────────────────────────────────────
#  Default variant scripts
# ─────────────────────────────────────────────────────────────────────────────
#
# These are the script strings used when the caller does not supply an
# explicit list. The selection is deliberately small (four variants) because
# the cost scales linearly: each variant is a full synthesis run, and the
# combined AIG that feeds the rewriter contains the union of their gates.

DEFAULT_CHOICE_SCRIPTS: List[str] = [
    "",                              # baseline (untouched copy)
    "balance",                       # depth-min structural
    "rewrite",                       # locally-optimised structural
    "rewrite; fraig",                # FRAIG-canonical structural
]


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run_variant(aig: AIG, out_lits: List[int], script: str) -> Tuple[AIG, List[int]]:
    """Run one synthesis script on a deep copy, returning (variant_aig, outs)."""
    variant = copy.deepcopy(aig)
    outs    = list(out_lits)
    if not script.strip():
        return variant, outs
    # Deferred import avoids circular imports (script → run_script → passes).
    from ..script import run_script
    variant, outs = run_script(variant, outs, script, verbose=False)
    return variant, outs


def _reachable_node_count(aig: AIG, out_lits: List[int]) -> int:
    """Count AND/XOR nodes reachable from out_lits (choice chains ignored)."""
    seen: set = set()
    stack = [aig.node_of(l) for l in out_lits if aig.node_of(l) > 0]
    while stack:
        nid = stack.pop()
        if nid in seen or nid <= 0:
            continue
        seen.add(nid)
        entry = aig._nodes[nid - 1]
        if entry[0] in ('and', 'xor'):
            _, la, lb = entry
            stack.append(aig.node_of(la))
            stack.append(aig.node_of(lb))
    return sum(
        1 for nid in seen
        if aig._nodes[nid - 1][0] in ('and', 'xor')
    )


def _simulate_combined(
    combined: AIG,
    n_patterns: int,
    seed: int,
) -> Dict[int, int]:
    """Bit-parallel random simulation over ``combined``. Same as FRAIG's sim."""
    MASK = (1 << n_patterns) - 1
    rng  = random.Random(seed)
    patterns = {name: rng.getrandbits(n_patterns) for name in combined.input_names()}

    sim: Dict[int, int] = {0: 0}
    for i, entry in enumerate(combined._nodes):
        nid = i + 1
        if entry[0] == 'input':
            sim[nid] = patterns.get(entry[1], 0) & MASK
        else:
            _, la, lb = entry
            a = sim[combined.node_of(la)]
            if combined.is_complemented(la):
                a = (~a) & MASK
            b = sim[combined.node_of(lb)]
            if combined.is_complemented(lb):
                b = (~b) & MASK
            sim[nid] = (a ^ b) if entry[0] == 'xor' else (a & b)
    return sim


def _link_choice_classes(
    combined: AIG,
    sim:      Dict[int, int],
    n_patterns: int,
    variant_node_sets: List[set],
    verify_timeout_ms: int = 2000,
) -> int:
    """
    Link functionally-equivalent nodes from *different variants* via
    ``add_choice``. Same-variant equivalents are left alone because they
    have already been structurally hashed inside that variant's run —
    linking them would duplicate structure the variant itself chose.

    Simulation groups are candidate classes only — every pair is then
    verified by a Z3 miter (UNSAT ⇒ equivalent). This matches FRAIG's
    discipline and is required for soundness: the rewriter treats a
    choice link as *proof* that two nodes compute the same function,
    and a false-positive link corrupts the circuit.

    Returns the number of choice links added.
    """
    try:
        import z3  # noqa
    except ImportError:
        # Without Z3 we cannot verify. Rather than silently fall back to
        # simulation-only linking (which ROADMAP P0 soundness guidance
        # forbids), we skip choice-link creation entirely. The rewriter
        # then behaves as the single-variant rewriter.
        return 0

    from .fraig import _build_z3_exprs, _check_pair

    MASK = (1 << n_patterns) - 1

    # Bucket by raw signature (same-polarity only).
    buckets: Dict[int, List[int]] = {}
    for nid in range(1, combined.n_nodes + 1):
        entry = combined._nodes[nid - 1]
        if entry[0] not in ('and', 'xor'):
            continue
        sig = sim.get(nid, 0)
        buckets.setdefault(sig, []).append(nid)

    vars_, exprs = _build_z3_exprs(combined)
    n_links = 0

    for sig, members in buckets.items():
        if len(members) < 2 or sig == 0 or sig == MASK:
            continue
        # Order candidates by topological position so the representative
        # is always the earliest.
        members.sort()
        rep = None
        for nid in members:
            if rep is None:
                rep = nid
                continue
            if _variant_of(nid, variant_node_sets) == _variant_of(rep, variant_node_sets):
                # Same variant: already handled by that variant's own
                # structural hashing. Skip to avoid redundant links.
                continue
            status, _ = _check_pair(
                vars_, exprs, rep, nid, same_polarity=True,
                timeout_ms=verify_timeout_ms,
            )
            if status == 'equiv':
                combined.add_choice(rep, nid)
                n_links += 1
            # 'different' / 'timeout' → leave unlinked; the rewriter
            # still has the baseline cut-matching path.
    return n_links


def _variant_of(nid: int, variant_node_sets: List[set]) -> int:
    for i, s in enumerate(variant_node_sets):
        if nid in s:
            return i
    return -1


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_choices(
    aig:        AIG,
    out_lits:   List[int],
    scripts:    Optional[Sequence[str]] = None,
    n_sim_patterns: int = 256,
    verify_timeout_ms: int = 2000,
    seed:       int = 0,
    verbose:    bool = False,
) -> Tuple[AIG, List[int], int]:
    """
    Build a combined AIG containing N structural variants of the input
    circuit with choice chains linking functionally-equivalent nodes.

    Parameters
    ----------
    aig, out_lits
        The source AIG and its primary output literals.
    scripts
        Sequence of synthesis-script strings. Each produces one variant.
        Empty string = baseline (untouched). Defaults to
        :data:`DEFAULT_CHOICE_SCRIPTS`.
    n_sim_patterns
        Bit-parallel simulation width. 256 usually hashes distinct
        functions into distinct buckets well below the Birthday threshold
        for circuits below ~2^20 distinct-function nodes.
    seed
        RNG seed for simulation patterns.
    verbose
        Print per-variant node counts and final link count.

    Returns
    -------
    (combined_aig, combined_out_lits, n_choice_links)
        The combined AIG with choice chains installed, the output-literal
        list taken from the **smallest** variant, and the number of
        cross-variant choice links added.
    """
    if scripts is None:
        scripts = DEFAULT_CHOICE_SCRIPTS

    # Run every variant on a deep copy.
    variants: List[Tuple[AIG, List[int]]] = []
    for i, s in enumerate(scripts):
        v_aig, v_outs = _run_variant(aig, list(out_lits), s)
        variants.append((v_aig, v_outs))
        if verbose:
            print(f"  [choice variant {i}] {s!r:<24} → "
                  f"{v_aig.n_nodes} nodes, {v_aig.n_ands} gates")

    # Build the combined AIG by composing every variant. Inputs dedupe by name.
    combined = AIG()
    variant_outs: List[List[int]] = []
    variant_node_sets: List[set]  = []
    for i, (v_aig, v_outs) in enumerate(variants):
        # nodes_before is the count of gate nodes BEFORE composing this variant;
        # anything added after the composition up to the next boundary belongs
        # to variant i. The compose() helper may also reuse existing nodes
        # when structural hashing matches — reused nodes are shared across
        # variants and intentionally appear in each variant's set.
        nodes_before = combined.n_nodes
        lit_map = combined.compose(v_aig, substitution={})
        nodes_after = combined.n_nodes
        variant_node_sets.append(set(range(nodes_before + 1, nodes_after + 1)))
        mapped = [lit_map[l] for l in v_outs]
        variant_outs.append(mapped)

    # Simulate and link choice classes across variants.
    sim = _simulate_combined(combined, n_sim_patterns, seed)
    n_links = _link_choice_classes(
        combined, sim, n_sim_patterns, variant_node_sets,
        verify_timeout_ms=verify_timeout_ms,
    )

    # Pick the smallest variant's outputs as primaries. The other variants
    # stay reachable via choice chains so the rewriter can find them.
    primary_idx = min(
        range(len(variants)),
        key=lambda i: _reachable_node_count(combined, variant_outs[i]),
    )

    if verbose:
        sizes = [_reachable_node_count(combined, o) for o in variant_outs]
        print(f"  [choice] variant sizes (reachable ANDs): {sizes}")
        print(f"  [choice] primary variant = {primary_idx} "
              f"({sizes[primary_idx]} gates)")
        print(f"  [choice] linked {n_links} cross-variant equivalences")

    # GC removes dead nodes but preserves choice chains that touch live nodes
    # (see AIG.gc): the alternative variants' structural scaffolding remains
    # in the AIG for the rewriter to consume, but unreferenced side-graphs
    # get pruned.
    combined, primary_outs = combined.gc(variant_outs[primary_idx])
    return combined, primary_outs, n_links
