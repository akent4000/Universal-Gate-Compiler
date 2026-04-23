"""
FRAIGing — Functionally Reduced AIG.

Detects and merges functionally equivalent nodes using:
  1. Random bit-parallel simulation to build node signatures
  2. Signature-based equivalence class partitioning
  3. Z3 SAT miter to formally verify each candidate pair
  4. AIG reconstruction with merged nodes and GC

Reference: Mishchenko et al., "FRAIGs: A Unifying Representation for Logic
Synthesis and Verification" (2005).
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import random

from ..core.aig import AIG, FALSE, TRUE


# ═══════════════════════════════════════════════════════════════════════════════
#  Bit-parallel simulation
# ═══════════════════════════════════════════════════════════════════════════════

def _simulate(aig: AIG, patterns: Dict[str, int], W: int) -> Dict[int, int]:
    """Simulate all nodes; returns node_id -> W-bit signature (node 0 = FALSE)."""
    MASK = (1 << W) - 1
    sim: Dict[int, int] = {0: 0}
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            sim[nid] = patterns.get(entry[1], 0) & MASK
        else:
            _, la, lb = entry
            a = sim[aig.node_of(la)]
            if aig.is_complemented(la):
                a = (~a) & MASK
            b = sim[aig.node_of(lb)]
            if aig.is_complemented(lb):
                b = (~b) & MASK
            sim[nid] = a & b
    return sim


# ═══════════════════════════════════════════════════════════════════════════════
#  Equivalence class formation
# ═══════════════════════════════════════════════════════════════════════════════

def _form_classes(
    aig: AIG,
    sim: Dict[int, int],
    W: int,
) -> List[List[Tuple[int, bool]]]:
    """
    Partition nodes by canonical simulation signature.

    Each class is a list of (node_id, polarity_flipped) sorted by node_id.
    polarity_flipped=True means this node's actual value is the complement of
    the representative's value.  Only classes with ≥2 members are returned.

    Canonical form: canon = min(sig, ~sig & MASK); flipped = (sig > ~sig & MASK).
    Two nodes with equal canon belong to the same class; same_polarity iff both
    have equal flipped flags.
    """
    MASK = (1 << W) - 1
    table: Dict[int, List[Tuple[int, bool]]] = {}

    for nid in range(aig.n_nodes + 1):   # include node 0 (constant FALSE)
        if nid not in sim:
            continue
        sig  = sim[nid]
        comp = (~sig) & MASK
        if sig <= comp:
            canon, flipped = sig, False
        else:
            canon, flipped = comp, True
        table.setdefault(canon, []).append((nid, flipped))

    result = []
    for members in table.values():
        if len(members) >= 2:
            members.sort(key=lambda x: x[0])
            result.append(members)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Z3 miter verification
# ═══════════════════════════════════════════════════════════════════════════════

def _build_z3_exprs(aig: AIG):
    """
    Build symbolic Z3 Boolean expressions for every AIG node.
    Returns (input_vars_dict, node_exprs_dict).
    """
    import z3
    vars_: Dict[str, object] = {name: z3.Bool(f'x_{name}') for name in aig.input_names()}
    exprs: Dict[int, object] = {0: z3.BoolVal(False)}

    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            exprs[nid] = vars_[entry[1]]
        else:
            _, la, lb = entry
            ea = exprs[aig.node_of(la)]
            if aig.is_complemented(la):
                ea = z3.Not(ea)
            eb = exprs[aig.node_of(lb)]
            if aig.is_complemented(lb):
                eb = z3.Not(eb)
            exprs[nid] = z3.And(ea, eb)

    return vars_, exprs


def _check_pair(
    vars_: dict,
    exprs: dict,
    id_u:  int,
    id_v:  int,
    same_polarity: bool,
    timeout_ms:    int,
) -> Tuple[str, Optional[Dict[str, bool]]]:
    """
    Formally check whether nodes id_u and id_v are equivalent.

    same_polarity=True  → verify f_u == f_v
    same_polarity=False → verify f_u == ~f_v

    Returns one of:
        ('equiv',     None)       — formally proven equivalent
        ('different', cex_dict)   — counterexample found
        ('timeout',   None)       — solver timed out
    """
    import z3
    fu = exprs[id_u]
    fv = exprs[id_v]
    if not same_polarity:
        fv = z3.Not(fv)

    s = z3.Solver()
    s.set('timeout', timeout_ms)
    s.add(z3.Xor(fu, fv))   # SAT iff they differ → UNSAT means equivalent

    r = s.check()
    if r == z3.unsat:
        return 'equiv', None
    if r == z3.sat:
        m   = s.model()
        cex = {n: bool(z3.is_true(m[v])) for n, v in vars_.items() if m[v] is not None}
        return 'different', cex
    return 'timeout', None


# ═══════════════════════════════════════════════════════════════════════════════
#  Substitution application
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_subst(
    old_aig: AIG,
    old_out: List[int],
    subst:   Dict[int, int],
) -> Tuple[AIG, List[int]]:
    """
    Rebuild AIG replacing each node in subst with its canonical literal.

    subst[node_id] = canonical_old_lit  (may be complemented for polarity flip).
    Because representatives always have smaller IDs (topological order), the
    canonical literal is always in lit_map by the time we encounter the
    merged node — no explicit chain-following required.
    """
    new_aig  = AIG()
    lit_map: Dict[int, int] = {FALSE: FALSE, TRUE: TRUE}

    for i, entry in enumerate(old_aig._nodes):
        oid = i + 1

        if oid in subst:
            canon_new = lit_map.get(subst[oid])
            if canon_new is not None:
                lit_map[oid * 2]     = canon_new
                lit_map[oid * 2 + 1] = canon_new ^ 1
                continue
            # Fallthrough: canonical not yet in lit_map (should not happen for
            # valid substitutions, but be safe and translate normally below).

        if entry[0] == 'input':
            nlit = new_aig.make_input(entry[1])
        else:
            _, la, lb = entry
            nlit = new_aig.make_and(lit_map[la], lit_map[lb])

        lit_map[oid * 2]     = nlit
        lit_map[oid * 2 + 1] = nlit ^ 1

    new_out = [lit_map.get(l, l) for l in old_out]
    return new_aig, new_out


# ═══════════════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════════════

def fraig(
    aig:              AIG,
    out_lits:         List[int],
    n_sim_patterns:   int  = 128,
    verify_timeout_ms: int = 3000,
    rounds:           int  = 3,
) -> Tuple[AIG, List[int]]:
    """
    FRAIGing: detect and merge functionally equivalent AIG nodes.

    Uses random bit-parallel simulation to form candidate equivalence classes,
    then formally verifies each candidate pair with a Z3 miter check.
    Requires z3-solver; returns the AIG unchanged if Z3 is unavailable.

    Parameters
    ----------
    aig               : input AIG
    out_lits          : output literals (needed for GC after merging)
    n_sim_patterns    : simulation word width (more bits → fewer false candidates)
    verify_timeout_ms : per-pair Z3 timeout in milliseconds
    rounds            : max FRAIG sweeps; stops early when no merges are found

    Returns
    -------
    (new_aig, new_out_lits)
    """
    try:
        import z3  # noqa
    except ImportError:
        return aig, list(out_lits)

    rng     = random.Random(0)   # deterministic across runs
    cur_aig = aig
    cur_out = list(out_lits)

    for rnd in range(rounds):
        patterns = {name: rng.getrandbits(n_sim_patterns) for name in cur_aig.input_names()}
        sim      = _simulate(cur_aig, patterns, n_sim_patterns)
        classes  = _form_classes(cur_aig, sim, n_sim_patterns)

        if not classes:
            break

        vars_, exprs = _build_z3_exprs(cur_aig)
        subst: Dict[int, int] = {}
        n_merged = 0

        for members in classes:
            rep_id, rep_flip = members[0]
            for m_id, m_flip in members[1:]:
                if m_id in subst:
                    continue
                same_pol = (rep_flip == m_flip)
                status, cex = _check_pair(vars_, exprs, rep_id, m_id, same_pol,
                                          verify_timeout_ms)
                if status == 'equiv':
                    # Map m_id's positive literal to rep's positive literal,
                    # complemented when they have opposite polarity.
                    subst[m_id] = rep_id * 2 | (0 if same_pol else 1)
                    n_merged += 1
                elif status == 'different' and cex:
                    # A counterexample could be fed back as an extra simulation
                    # pattern to split the class faster, but we skip that here
                    # for simplicity — the next round with fresh random patterns
                    # will naturally refine the classes.
                    pass

        if not subst:
            break

        new_aig, new_out = _apply_subst(cur_aig, cur_out, subst)
        new_aig, new_out = new_aig.gc(new_out)

        delta = cur_aig.n_ands - new_aig.n_ands
        cur_aig = new_aig
        cur_out = new_out

        if delta <= 0:
            break

    return cur_aig, cur_out


def fraig_stats(
    aig:              AIG,
    out_lits:         List[int],
    n_sim_patterns:   int  = 128,
    verify_timeout_ms: int = 3000,
) -> dict:
    """
    Run one FRAIG analysis pass and return statistics without modifying the AIG.

    Returns a dict with keys: n_classes, n_candidates, n_merged, n_timeouts.
    """
    try:
        import z3  # noqa
    except ImportError:
        return {'error': 'z3 not installed'}

    rng      = random.Random(0)
    patterns = {name: rng.getrandbits(n_sim_patterns) for name in aig.input_names()}
    sim      = _simulate(aig, patterns, n_sim_patterns)
    classes  = _form_classes(aig, sim, n_sim_patterns)
    vars_, exprs = _build_z3_exprs(aig)

    n_candidates = sum(len(m) - 1 for m in classes)
    n_merged = n_timeouts = 0

    for members in classes:
        rep_id, rep_flip = members[0]
        for m_id, m_flip in members[1:]:
            same_pol = (rep_flip == m_flip)
            status, _ = _check_pair(vars_, exprs, rep_id, m_id, same_pol,
                                    verify_timeout_ms)
            if status == 'equiv':
                n_merged += 1
            elif status == 'timeout':
                n_timeouts += 1

    return {
        'n_classes':    len(classes),
        'n_candidates': n_candidates,
        'n_merged':     n_merged,
        'n_timeouts':   n_timeouts,
    }
