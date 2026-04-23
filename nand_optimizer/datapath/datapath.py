"""
Parametrized datapath-block generators for nand_optimizer.

All generators receive a StructuralModule *m* and work exclusively on AIG
literals (int).  Sub-expressions are automatically shared via AIG structural
hashing — e.g. a ripple-carry chain reused across multiple adder bits is
never duplicated.

Public generators
-----------------
Arithmetic:
    half_adder(m, a, b)                    → (sum_lit, carry_lit)
    full_adder(m, a, b, cin)               → (sum_lit, cout_lit)
    ripple_adder(m, a_lits, b_lits, cin)   → (sum_lits, cout_lit)

Comparators / detectors:
    eq_comparator(m, a_lits, b_lits)       → lit  (a == b)
    zero_detect(m, a_lits)                 → lit  (a == 0)
    ones_detect(m, a_lits)                 → lit  (a == all-ones)

Ripple carry / borrow (for toggle-enable chains):
    ripple_up_carry(m, q_lits, carry_in)   → List[Lit]  length n+1
    ripple_down_borrow(m, q_lits, borrow_in) → List[Lit]  length n+1

Bus utilities:
    mux2_bus(m, sel, a_lits, b_lits)       → List[Lit]  sel=1→a, sel=0→b

Encoding / priority:
    priority_encoder(m, in_lits)           → (valid_lit, encoded_lits)

Sequential excitation:
    jk_excitation(m, q_lit, q_next_lit)    → (j_lit, k_lit)
"""

from __future__ import annotations
import math
from typing import List, Optional, Tuple

from ..core.aig  import Lit, TRUE, FALSE
from .structural import StructuralModule


# ── Arithmetic ────────────────────────────────────────────────────────────────

def half_adder(m: StructuralModule, a: Lit, b: Lit) -> Tuple[Lit, Lit]:
    """1-bit half adder.  Returns (sum, carry)."""
    s = m.xor2(a, b)
    c = m.and2(a, b)
    return s, c


def full_adder(m: StructuralModule,
               a: Lit, b: Lit, cin: Lit) -> Tuple[Lit, Lit]:
    """1-bit full adder.  Returns (sum, cout)."""
    ab   = m.xor2(a, b)
    s    = m.xor2(ab, cin)
    c1   = m.and2(a, b)
    c2   = m.and2(ab, cin)
    cout = m.or2(c1, c2)
    return s, cout


def ripple_adder(
    m:      StructuralModule,
    a_lits: List[Lit],
    b_lits: List[Lit],
    cin:    Optional[Lit] = None,
) -> Tuple[List[Lit], Lit]:
    """
    N-bit ripple-carry adder, LSB-first.

    Returns (sum_lits, cout).  Both buses must have the same width.
    """
    if len(a_lits) != len(b_lits):
        raise ValueError("ripple_adder: a and b buses must be the same width")
    carry = cin if cin is not None else m.const0()
    sums: List[Lit] = []
    for a, b in zip(a_lits, b_lits):
        s, carry = full_adder(m, a, b, carry)
        sums.append(s)
    return sums, carry


# ── Comparators / Detectors ───────────────────────────────────────────────────

def eq_comparator(
    m:      StructuralModule,
    a_lits: List[Lit],
    b_lits: List[Lit],
) -> Lit:
    """
    N-bit equality comparator.  Returns 1 iff a == b.

    Implementation: bit-wise XNOR, then AND-tree.
    """
    if len(a_lits) != len(b_lits):
        raise ValueError("eq_comparator: buses must have the same width")
    if not a_lits:
        return m.const1()
    eqs = [m.xnor2(a, b) for a, b in zip(a_lits, b_lits)]
    return m.and_tree(eqs)


def zero_detect(m: StructuralModule, a_lits: List[Lit]) -> Lit:
    """Return 1 iff all bits of *a_lits* are 0 (NOR == AND of complements)."""
    if not a_lits:
        return m.const1()
    nots = [m.not1(a) for a in a_lits]
    return m.and_tree(nots)


def ones_detect(m: StructuralModule, a_lits: List[Lit]) -> Lit:
    """Return 1 iff all bits of *a_lits* are 1."""
    if not a_lits:
        return m.const1()
    return m.and_tree(list(a_lits))


# ── Ripple carry / borrow ─────────────────────────────────────────────────────

def ripple_up_carry(
    m:        StructuralModule,
    q_lits:   List[Lit],
    carry_in: Optional[Lit] = None,
) -> List[Lit]:
    """
    Compute carry bits for binary up-counting (LSB first).

    Returns a list of length ``len(q_lits) + 1`` where entry *i* is the
    toggle-enable (carry-into) for bit *i*:

        carries[0] = carry_in  (default: TRUE — always toggle bit 0)
        carries[i+1] = carries[i] & q_lits[i]

    The linear ladder avoids the O(n²) AND-tree cost of a naive popcount.
    """
    c = carry_in if carry_in is not None else m.const1()
    carries = [c]
    for q in q_lits:
        c = m.and2(c, q)
        carries.append(c)
    return carries


def ripple_down_borrow(
    m:         StructuralModule,
    q_lits:    List[Lit],
    borrow_in: Optional[Lit] = None,
) -> List[Lit]:
    """
    Compute borrow bits for binary down-counting (LSB first).

    Returns a list of length ``len(q_lits) + 1`` where entry *i* is the
    toggle-enable (borrow-into) for bit *i*:

        borrows[0] = borrow_in  (default: TRUE)
        borrows[i+1] = borrows[i] & ~q_lits[i]
    """
    b = borrow_in if borrow_in is not None else m.const1()
    borrows = [b]
    for q in q_lits:
        b = m.and2(b, m.not1(q))
        borrows.append(b)
    return borrows


# ── Bus utilities ─────────────────────────────────────────────────────────────

def mux2_bus(
    m:      StructuralModule,
    sel:    Lit,
    a_lits: List[Lit],
    b_lits: List[Lit],
) -> List[Lit]:
    """
    Bit-wise 2-to-1 multiplexer.  sel=1 selects *a_lits*, sel=0 selects *b_lits*.

    Both buses must have the same width.
    """
    if len(a_lits) != len(b_lits):
        raise ValueError("mux2_bus: buses must have the same width")
    return [m.mux2(sel, a, b) for a, b in zip(a_lits, b_lits)]


# ── Priority encoder ──────────────────────────────────────────────────────────

def priority_encoder(
    m:       StructuralModule,
    in_lits: List[Lit],
) -> Tuple[Lit, List[Lit]]:
    """
    Priority encoder: highest-index active input wins.

    Returns ``(valid, encoded)`` where:
      *valid*   — 1 iff at least one input is active.
      *encoded* — ⌈log₂ n⌉ output bits (binary index of the highest active
                  input; undefined when valid=0).

    Implementation uses a cascade of grant signals to suppress lower-priority
    inputs so each bit is computed as a simple OR over granted indices.
    """
    n = len(in_lits)
    if n == 0:
        return m.const0(), []

    w = max(1, math.ceil(math.log2(n))) if n > 1 else 1
    valid = m.or_tree(list(in_lits))

    # Grant signals: grant[i] is 1 iff in_lits[i] is active AND no higher
    # index is active.  Built top-down so grant[n-1] = in_lits[n-1].
    grant: List[Lit] = [m.const0()] * n
    any_higher = m.const0()
    for i in range(n - 1, -1, -1):
        grant[i] = m.and2(in_lits[i], m.not1(any_higher))
        any_higher = m.or2(grant[i], any_higher)

    # Output bit k: OR over grant[i] for all i where bit k of i is 1.
    encoded: List[Lit] = []
    for k in range(w):
        terms = [grant[i] for i in range(n) if (i >> k) & 1]
        encoded.append(m.or_tree(terms) if terms else m.const0())

    return valid, encoded


# ── Sequential excitation ─────────────────────────────────────────────────────

def jk_excitation(
    m:          StructuralModule,
    q_lit:      Lit,
    q_next_lit: Lit,
) -> Tuple[Lit, Lit]:
    """
    T-fill JK excitation for one flip-flop bit.

    Returns ``(J, K)`` where ``J = K = T = Q XOR Q_next``.

    Under the T-fill concretion:
      • T=1, Q=0 → J=1, K=1 → SET    (Q_next = 1 ✓)
      • T=1, Q=1 → J=1, K=1 → RESET  (Q_next = 0 ✓)
      • T=0       → J=0, K=0 → HOLD   (Q_next = Q ✓)

    Structural hashing ensures J and K resolve to the same AIG literal —
    no extra gate is added despite nominally two outputs.
    """
    t = m.xor2(q_lit, q_next_lit)
    return t, t
