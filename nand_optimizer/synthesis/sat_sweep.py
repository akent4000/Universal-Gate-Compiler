"""
SAT Sweeping — ODC-aware equivalence merging.

Extends FRAIGing with observability don't-cares (ODC): node m is replaced
by representative node rep when m's value is never observable wherever it
disagrees with rep.  Formally the SAT miter asks

    ∃ x :  obs_m(x)  ∧  (f_rep(x) ≠ f_m(x))

where obs_m(x) is the *symbolic* condition "node m contributes to at least
one primary output on input x", built directly from the AIG gate structure.
UNSAT → replacing every occurrence of m with rep leaves all output values
unchanged.

Two candidate-selection heuristics feed the Z3 checker:

  1. Standard (global-sim) buckets — same bucketing as FRAIG; captures
     globally equivalent nodes even when their care masks differ.
  2. Fill-based (ODC-sim) buckets — non-observable simulation bits are
     filled to 1 before canonicalisation; captures node pairs that agree
     on all *observable* patterns but may differ elsewhere, which FRAIG
     cannot find.

References
----------
Mishchenko, "FRAIGs: A Unifying Representation for Logic Synthesis and
Verification" (2005).
Mishchenko et al., "Scalable Don't-Care-Based Logic Optimization and
Resynthesis" (FPGA 2009), §3.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Set, Tuple
import random

from ..core.aig import AIG, FALSE, TRUE
from .fraig     import _simulate, _apply_subst, _build_z3_exprs
from .dont_care import _propagate_care_sim


# ─────────────────────────────────────────────────────────────────────────────
#  Symbolic observability
# ─────────────────────────────────────────────────────────────────────────────

def _build_z3_obs(
    aig:      AIG,
    out_lits: List[int],
    f_exprs:  Dict[int, object],
) -> Dict[int, object]:
    """
    Build a per-node symbolic observability condition.

    obs[n] is a Z3 BoolRef that evaluates to True on input x iff flipping
    node n's value on x changes at least one primary output.

    Computed by reverse-topological traversal (high node-IDs to low):

      AND(a, b): a is observable iff AND(a,b) is observable AND b = 1.
      XOR(a, b): a and b are always observable when XOR is observable.
    """
    import z3

    FALSE_Z3 = z3.BoolVal(False)
    TRUE_Z3  = z3.BoolVal(True)

    obs: Dict[int, object] = {0: FALSE_Z3}
    for i in range(aig.n_nodes):
        obs[i + 1] = FALSE_Z3

    for lit in out_lits:
        nid = aig.node_of(lit)
        if nid > 0:
            obs[nid] = TRUE_Z3

    for i in range(aig.n_nodes - 1, -1, -1):
        nid   = i + 1
        entry = aig._nodes[i]
        if entry[0] not in ('and', 'xor'):
            continue

        obs_p = obs[nid]
        if z3.is_false(obs_p):
            continue

        _, a_lit, b_lit = entry
        ia = aig.node_of(a_lit)
        ib = aig.node_of(b_lit)

        if entry[0] == 'xor':
            # XOR is sensitive to both inputs regardless of sibling value
            if ia > 0:
                cur = obs[ia]
                obs[ia] = TRUE_Z3 if z3.is_true(cur) or z3.is_true(obs_p) \
                          else z3.Or(cur, obs_p)
            if ib > 0:
                cur = obs[ib]
                obs[ib] = TRUE_Z3 if z3.is_true(cur) or z3.is_true(obs_p) \
                          else z3.Or(cur, obs_p)
        else:
            # AND: input a is observable iff this AND is observable AND sibling b = 1
            fb = f_exprs[ib]
            if aig.is_complemented(b_lit):
                fb = z3.Not(fb)
            fa = f_exprs[ia]
            if aig.is_complemented(a_lit):
                fa = z3.Not(fa)

            if ia > 0:
                contrib_a = TRUE_Z3 if z3.is_true(obs_p) and z3.is_true(fb) \
                            else z3.And(obs_p, fb)
                cur = obs[ia]
                obs[ia] = TRUE_Z3 if z3.is_true(cur) or z3.is_true(contrib_a) \
                          else z3.Or(cur, contrib_a)

            if ib > 0:
                contrib_b = TRUE_Z3 if z3.is_true(obs_p) and z3.is_true(fa) \
                            else z3.And(obs_p, fa)
                cur = obs[ib]
                obs[ib] = TRUE_Z3 if z3.is_true(cur) or z3.is_true(contrib_b) \
                          else z3.Or(cur, contrib_b)

    return obs


# ─────────────────────────────────────────────────────────────────────────────
#  Candidate class formation
# ─────────────────────────────────────────────────────────────────────────────

def _form_standard_classes(
    aig:  AIG,
    sim:  Dict[int, int],
    care: Dict[int, int],
    W:    int,
) -> List[List[Tuple[int, bool]]]:
    """
    FRAIG-style bucketing by canonical full simulation signature.
    Nodes with care == 0 (never observable in simulation) are excluded.
    """
    MASK  = (1 << W) - 1
    table: Dict[int, List[Tuple[int, bool]]] = {}

    for nid in range(1, aig.n_nodes + 1):
        if nid not in sim or care.get(nid, 0) == 0:
            continue
        sig  = sim[nid]
        comp = (~sig) & MASK
        canon, flipped = (sig, False) if sig <= comp else (comp, True)
        table.setdefault(canon, []).append((nid, flipped))

    return [
        sorted(members, key=lambda x: x[0])
        for members in table.values()
        if len(members) >= 2
    ]


def _form_fill_classes(
    aig:  AIG,
    sim:  Dict[int, int],
    care: Dict[int, int],
    W:    int,
) -> List[List[Tuple[int, bool]]]:
    """
    ODC-extended bucketing: ~care bits are filled to 1 before canonicalisation.

    Two nodes land in the same bucket iff they agree on every simulation bit
    where *both* have care = 1 (plus both are 1 on bits where exactly one
    has care = 1).  This finds candidates invisible to FRAIG because they
    differ only on non-observable bits.
    """
    MASK  = (1 << W) - 1
    table: Dict[int, List[Tuple[int, bool]]] = {}

    for nid in range(1, aig.n_nodes + 1):
        if nid not in sim or care.get(nid, 0) == 0:
            continue
        c     = care[nid]
        fill  =  (sim[nid] |  ((~c) & MASK))          # ~care → 1
        cfill = ((~sim[nid]) | ((~c) & MASK)) & MASK  # complement fill
        canon, flipped = (fill, False) if fill <= cfill else (cfill, True)
        table.setdefault(canon, []).append((nid, flipped))

    return [
        sorted(members, key=lambda x: x[0])
        for members in table.values()
        if len(members) >= 2
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  ODC-aware SAT verification
# ─────────────────────────────────────────────────────────────────────────────

def _check_pair_odc(
    vars_:         dict,
    f_exprs:       dict,
    obs:           dict,
    rep_id:        int,
    m_id:          int,
    same_polarity: bool,
    timeout_ms:    int,
) -> Tuple[str, Optional[Dict[str, bool]]]:
    """
    Ask: ∃ x : obs_m(x) ∧ (f_rep(x) ≠ f_m(x)) ?

    UNSAT → m is safely replaceable by rep everywhere.
    SAT   → returns a counterexample dict for simulation refinement.
    """
    import z3

    f_rep = f_exprs[rep_id]
    f_m   = f_exprs[m_id]
    if not same_polarity:
        f_m = z3.Not(f_m)

    obs_m = obs[m_id]

    s = z3.Solver()
    s.set('timeout', timeout_ms)

    import z3 as _z3
    if _z3.is_true(obs_m):
        # obs is unconditionally True — standard FRAIG-style miter
        s.add(z3.Xor(f_rep, f_m))
    elif _z3.is_false(obs_m):
        # m is never observable — any rep is trivially safe; treat as equiv
        return 'equiv', None
    else:
        s.add(z3.And(obs_m, z3.Xor(f_rep, f_m)))

    r = s.check()
    if r == z3.unsat:
        return 'equiv', None
    if r == z3.sat:
        mv  = s.model()
        cex = {n: bool(z3.is_true(mv[v]))
               for n, v in vars_.items() if mv[v] is not None}
        return 'different', cex
    return 'timeout', None


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

def sat_sweep(
    aig:               AIG,
    out_lits:          List[int],
    n_sim_patterns:    int = 256,
    verify_timeout_ms: int = 2000,
    rounds:            int = 2,
) -> Tuple[AIG, List[int]]:
    """
    SAT sweep: ODC-aware equivalence merging.

    Extends FRAIGing by exploiting observability don't-cares so that two
    nodes can be merged when they disagree only on inputs where neither
    contributes to any primary output.

    Algorithm per round
    -------------------
    1. Simulate the AIG with *n_sim_patterns* random bit-vectors; propagate
       care masks backwards from outputs (Mishchenko 2009 §3).
    2. Build candidate pairs using two heuristics:
         a) Standard buckets  — canonical full-simulation signature (FRAIG);
            excludes unobservable nodes (care = 0 in simulation).
         b) Fill-based buckets — canonical signature with ~care bits set to 1;
            groups nodes that agree on all observable patterns but may differ
            elsewhere — the opportunity FRAIG cannot see.
    3. Deduplicate the combined candidate list.
    4. Build symbolic Z3 expressions for every AIG node's function and
       observability condition.
    5. For each candidate pair (rep, m), SAT-check:
            ∃ x : obs_m(x) ∧ (f_rep(x) ≠ f_m(x))
       UNSAT → replace m with rep.
    6. Apply all substitutions, run GC.  Stop early if no merges found.

    Parameters
    ----------
    n_sim_patterns    : simulation word width (default 256 — wider than
                        FRAIG's 128 for better care-mask coverage)
    verify_timeout_ms : per-pair Z3 timeout in milliseconds (default 2000)
    rounds            : maximum sweeping iterations (default 2)

    Returns
    -------
    (new_aig, new_out_lits)
    """
    try:
        import z3  # noqa: F401
    except ImportError:
        return aig, list(out_lits)

    rng     = random.Random(7)
    cur_aig = aig
    cur_out = list(out_lits)

    for _rnd in range(rounds):
        W        = n_sim_patterns
        patterns = {name: rng.getrandbits(W) for name in cur_aig.input_names()}
        sim      = _simulate(cur_aig, patterns, W)
        care     = _propagate_care_sim(cur_aig, cur_out, sim, W)

        std_cls  = _form_standard_classes(cur_aig, sim, care, W)
        fill_cls = _form_fill_classes(cur_aig, sim, care, W)

        # Combine candidate pairs from both sources; deduplicate by (rep, m, polarity)
        seen:      Set[Tuple[int, int, bool]]         = set()
        all_pairs: List[Tuple[int, bool, int, bool]]  = []
        for cls_list in (std_cls, fill_cls):
            for members in cls_list:
                rep_id, rep_flip = members[0]
                for m_id, m_flip in members[1:]:
                    same_pol = (rep_flip == m_flip)
                    key = (rep_id, m_id, same_pol)
                    if key not in seen:
                        seen.add(key)
                        all_pairs.append((rep_id, rep_flip, m_id, m_flip))

        if not all_pairs:
            break

        vars_, f_exprs = _build_z3_exprs(cur_aig)
        obs            = _build_z3_obs(cur_aig, cur_out, f_exprs)

        subst:    Dict[int, int] = {}
        n_merged: int            = 0

        for rep_id, rep_flip, m_id, m_flip in all_pairs:
            if m_id in subst:
                continue  # already mapped this round
            same_pol = (rep_flip == m_flip)
            status, _cex = _check_pair_odc(
                vars_, f_exprs, obs, rep_id, m_id, same_pol, verify_timeout_ms,
            )
            if status == 'equiv':
                subst[m_id] = rep_id * 2 | (0 if same_pol else 1)
                n_merged += 1

        if not subst:
            break

        new_aig, new_out = _apply_subst(cur_aig, cur_out, subst)
        new_aig, new_out = new_aig.gc(new_out)

        if cur_aig.n_ands <= new_aig.n_ands:
            # Structural hashing may have inflated n_ands; accept if merges happened
            if n_merged == 0:
                break
        cur_aig = new_aig
        cur_out = new_out

    return cur_aig, cur_out
