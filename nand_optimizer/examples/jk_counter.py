"""
Built-in example — universal reversible counter on JK flip-flops (ЛР №2).

Demonstrates the structural AIG synthesis path: the circuit has 3·bits + 2
inputs (Q, D, LIMIT, LOAD, UP), making a TruthTable with 2^(3·8+2) = 2^26
≈ 67 M minterms completely infeasible.  The structural path builds the AIG
directly from datapath blocks in milliseconds.

Factory
-------
    universal_reversible_counter(bits=8) → OptimizeResult

Combinational cone ports
------------------------
    Inputs:  Q[0..bits-1]     current state (feedback from JK flip-flop Q pins)
             LOAD              parallel load enable (synchronous)
             UP                direction: 1 = count up, 0 = count down
             D[0..bits-1]      parallel load data
             LIMIT[0..bits-1]  roll-over limit for UP / wrap target for DOWN

    Outputs: J[0..bits-1]     JK flip-flop J inputs
             K[0..bits-1]     JK flip-flop K inputs

Async reset (active-low RESET_N) is a separate control tract routed
directly to the CLR pins of the JK flip-flops — it is *not* part of the
combinational cone synthesized here.

Semantics
---------
    LOAD=1:
        Q_next[i] = D[i]                       (parallel load)

    LOAD=0, UP=1, Q == LIMIT:
        Q_next[i] = 0                           (upper roll-over to 0)

    LOAD=0, UP=1, Q != LIMIT:
        Q_next[i] = Q[i] XOR C[i]              (count up via ripple carry)

    LOAD=0, UP=0, Q == 0:
        Q_next[i] = LIMIT[i]                   (lower wrap to LIMIT)

    LOAD=0, UP=0, Q != 0:
        Q_next[i] = Q[i] XOR B[i]              (count down via ripple borrow)

where:
    C[i] = ripple carry into bit i  (C[0]=1, C[i+1] = C[i] & Q[i])
    B[i] = ripple borrow into bit i (B[0]=1, B[i+1] = B[i] & ~Q[i])

J/K excitation (per bit)
------------------------
    J_i = LOAD·D_i
        + ~LOAD·Count_enable·T_i
        + ~LOAD·~UP·Z·LIMIT_i

    K_i = LOAD·~D_i
        + ~LOAD·Count_enable·T_i
        + ~LOAD·UP·Rollover
        + ~LOAD·~UP·Z·~LIMIT_i

where:
    T_i         = (UP & C_i) | (~UP & B_i)
    Count_enable = ~Rollover & (UP | ~Z)
    Rollover    = UP & EQ(Q, LIMIT)
    Z           = zero_detect(Q)

Count_enable suppresses the toggle term whenever the counter is about to
perform a terminal action (UP-rollover or DOWN-wrap), preventing the
carry/borrow chain from interfering with the wrap logic.

CLI
---
    python -m nand_optimizer jkcounter --bits 8 --circ counter8.circ
"""

from __future__ import annotations
from typing import List, Tuple

from ..structural import StructuralModule
from ..datapath   import (
    eq_comparator, zero_detect,
    ripple_up_carry, ripple_down_borrow,
    jk_excitation,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Combinational cone synthesis
# ═══════════════════════════════════════════════════════════════════════════════

def universal_reversible_counter(bits: int = 8):
    """
    Build and synthesize the JK-excitation combinational cone for an
    *bits*-bit universal reversible counter.

    Returns an ``OptimizeResult`` whose ``.builder.gates`` contains the final
    NAND network and ``.outputs`` maps each ``'J{i}'`` / ``'K{i}'`` name to
    its output wire.
    """
    q_names     = [f'Q{i}'     for i in range(bits)]
    d_names     = [f'D{i}'     for i in range(bits)]
    limit_names = [f'LIMIT{i}' for i in range(bits)]
    all_inputs  = q_names + ['LOAD', 'UP'] + d_names + limit_names

    m = StructuralModule('universal_reversible_counter', all_inputs)

    q     = [m.input(n) for n in q_names]
    d     = [m.input(n) for n in d_names]
    limit = [m.input(n) for n in limit_names]
    LOAD  = m.input('LOAD')
    UP    = m.input('UP')

    nLOAD = m.not1(LOAD)
    nUP   = m.not1(UP)

    # ── Terminal conditions ───────────────────────────────────────────────────

    # Rollover: counting UP and Q has reached LIMIT → wrap to 0
    Rollover  = m.and2(UP, eq_comparator(m, q, limit))
    nRollover = m.not1(Rollover)

    # Z: Q == 0 → when counting DOWN, wrap to LIMIT
    Z  = zero_detect(m, q)
    nZ = m.not1(Z)

    # ── Toggle-enable chains ──────────────────────────────────────────────────

    # C[i] = carry into bit i for UP counting
    # C[0]=1, C[i+1] = C[i] & Q[i]  →  C[i] = Q[0] & Q[1] & ... & Q[i-1]
    C = ripple_up_carry(m, q)       # length = bits + 1; C[i] is T_i for UP

    # B[i] = borrow into bit i for DOWN counting
    # B[0]=1, B[i+1] = B[i] & ~Q[i]
    B = ripple_down_borrow(m, q)    # length = bits + 1; B[i] is T_i for DOWN

    # ── Shared sub-expressions (structural hashing ensures no duplication) ────

    # Count_enable = ~Rollover & (UP | ~Z)
    # Suppresses toggle during terminal events to avoid carry/borrow overlap.
    Count_enable = m.and2(nRollover, m.or2(UP, nZ))

    # ~LOAD & UP & Rollover  — used in K (UP-wrap clears all bits to 0)
    nLOAD_UP_Rollover = m.and2(m.and2(nLOAD, UP), Rollover)

    # ~LOAD & ~UP & Z  — shared prefix for DOWN-wrap terms
    nLOAD_nUP_Z = m.and2(m.and2(nLOAD, nUP), Z)

    # ~LOAD & Count_enable  — shared prefix for toggle terms
    nLOAD_CE = m.and2(nLOAD, Count_enable)

    # ── Per-bit JK logic ──────────────────────────────────────────────────────

    for i in range(bits):
        # T_i: combined toggle enable — UP path uses carry, DOWN path uses borrow
        T_i = m.or2(m.and2(UP, C[i]), m.and2(nUP, B[i]))

        # J_i = LOAD·D_i
        #      + ~LOAD·Count_enable·T_i   (normal counting)
        #      + ~LOAD·~UP·Z·LIMIT_i      (DOWN-wrap: set if LIMIT bit = 1)
        j_load   = m.and2(LOAD, d[i])
        j_count  = m.and2(nLOAD_CE, T_i)
        j_wrap   = m.and2(nLOAD_nUP_Z, limit[i])
        J_i = m.or2(m.or2(j_load, j_count), j_wrap)

        # K_i = LOAD·~D_i
        #      + ~LOAD·Count_enable·T_i   (normal counting — same toggle)
        #      + ~LOAD·UP·Rollover         (UP-wrap: reset all bits to 0)
        #      + ~LOAD·~UP·Z·~LIMIT_i     (DOWN-wrap: clear if LIMIT bit = 0)
        k_load   = m.and2(LOAD, m.not1(d[i]))
        k_count  = m.and2(nLOAD_CE, T_i)           # same literal as j_count via hashing
        k_wrap_up   = nLOAD_UP_Rollover              # shared; clears all bits
        k_wrap_down = m.and2(nLOAD_nUP_Z, m.not1(limit[i]))
        K_i = m.or2(m.or2(k_load, k_count), m.or2(k_wrap_up, k_wrap_down))

        m.add_output(f'J{i}', J_i)
        m.add_output(f'K{i}', K_i)

    return m.finalize(script='rewrite; fraig; balance', verbose=False)


# ═══════════════════════════════════════════════════════════════════════════════
#  Python reference model
# ═══════════════════════════════════════════════════════════════════════════════

def _reference_step(
    q:     List[int],
    load:  int,
    up:    int,
    d:     List[int],
    limit: List[int],
) -> List[int]:
    """One-cycle behavioral reference (no reset)."""
    bits      = len(q)
    q_val     = sum(q[i] << i     for i in range(bits))
    d_val     = sum(d[i] << i     for i in range(bits))
    limit_val = sum(limit[i] << i for i in range(bits))

    if load:
        nxt = d_val
    elif up:
        nxt = 0 if q_val == limit_val else q_val + 1
    else:
        nxt = limit_val if q_val == 0 else q_val - 1

    return [(nxt >> i) & 1 for i in range(bits)]


# ═══════════════════════════════════════════════════════════════════════════════
#  Cycle-accurate simulation helper
# ═══════════════════════════════════════════════════════════════════════════════

def _eval_jk(
    opt,
    q:     List[int],
    load:  int,
    up:    int,
    d:     List[int],
    limit: List[int],
    bits:  int,
) -> Tuple[List[int], List[int]]:
    """Evaluate J[0..bits-1] and K[0..bits-1] from the synthesized NAND network."""
    asgn: dict = {}
    for i in range(bits):
        asgn[f'Q{i}']     = q[i]
        asgn[f'D{i}']     = d[i]
        asgn[f'LIMIT{i}'] = limit[i]
    asgn['LOAD'] = load
    asgn['UP']   = up

    wire_vals = dict(asgn)
    for name, gtype, ins in opt.builder.gates:
        if gtype == 'NAND':
            wire_vals[name] = 1 - int(all(wire_vals.get(w, 0) == 1 for w in ins))
        elif gtype == 'OUTPUT':
            wire_vals[name] = wire_vals.get(ins[0], 0)
        elif gtype == 'ZERO':
            wire_vals[name] = 0
        elif gtype == 'ONE':
            wire_vals[name] = 1

    j = [wire_vals.get(f'J{i}', 0) for i in range(bits)]
    k = [wire_vals.get(f'K{i}', 0) for i in range(bits)]
    return j, k


def _apply_jk(q: List[int], j: List[int], k: List[int]) -> List[int]:
    """Apply JK recurrence: Q_next[i] = J[i]·~Q[i] + ~K[i]·Q[i]."""
    return [
        (j[i] & (1 - q[i])) | ((1 - k[i]) & q[i])
        for i in range(len(q))
    ]


# ═══════════════════════════════════════════════════════════════════════════════
#  Regression
# ═══════════════════════════════════════════════════════════════════════════════

def run_jkcounter_regression(bits: int = 8, verbose: bool = True) -> bool:
    """
    Cycle-accurate regression against the Python reference model.

    Covers four scenarios × 256 random stimuli each:
      1. count-up   — LOAD=0, UP=1, LIMIT=max
      2. count-down — LOAD=0, UP=0, LIMIT=max
      3. parallel load — LOAD=1, random D each cycle
      4. rollover — LOAD=0, UP=1, LIMIT=half-max (verifies UP-wrap to 0)

    Returns True iff every synthesized Q_next matches the reference.
    """
    import random
    rng     = random.Random(42)
    max_val = (1 << bits) - 1
    half    = max_val >> 1

    opt = universal_reversible_counter(bits)

    def to_bits(v: int) -> List[int]:
        return [(v >> i) & 1 for i in range(bits)]

    scenarios = [
        ('count-up',   0, 1, max_val),
        ('count-down', 0, 0, max_val),
        ('parallel-load', 1, 0, max_val),
        ('rollover',   0, 1, half),
    ]

    all_ok = True
    for label, load_fixed, up_fixed, limit_val in scenarios:
        limit = to_bits(limit_val)
        # For count-up start at 0, count-down start at limit_val, else random
        if up_fixed and not load_fixed:
            q = [0] * bits
        elif not up_fixed and not load_fixed:
            q = to_bits(limit_val)
        else:
            q = [0] * bits

        ok = True
        for step in range(256):
            d    = to_bits(rng.randint(0, max_val))
            load = load_fixed
            up   = up_fixed

            j, k           = _eval_jk(opt, q, load, up, d, limit, bits)
            q_next_synth   = _apply_jk(q, j, k)
            q_next_ref     = _reference_step(q, load, up, d, limit)

            if q_next_synth != q_next_ref:
                if verbose:
                    print(f'  [FAIL] {label} step {step}: '
                          f'q={q}, load={load}, up={up}, d={d}, limit={limit}')
                    print(f'         synth={q_next_synth}  ref={q_next_ref}')
                ok = False
                all_ok = False
                break

            q = q_next_synth

        sym = 'OK' if ok else 'FAIL'
        if verbose:
            print(f'  [{sym}] JK counter {bits}-bit: {label} (256 steps)')

    return all_ok
