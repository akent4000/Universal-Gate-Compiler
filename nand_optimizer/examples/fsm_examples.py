"""
Built-in FSM examples for Phase 3 regression testing.

Each factory returns a StateTable.  Combined with synthesize_fsm() and
simulate_fsm() they produce a full Phase 3 smoke test.
"""

from __future__ import annotations
from ..fsm import StateTable, Transition
from ..truth_table import DASH


# ═══════════════════════════════════════════════════════════════════════════════
#  Sequence detector: emit y=1 whenever the last three inputs were "101".
# ═══════════════════════════════════════════════════════════════════════════════

def seq_detector_101() -> StateTable:
    """
    Mealy FSM that pulses output y=1 on the cycle that completes the
    pattern "1,0,1" on input x.  Overlapping sequences count: after a
    match on cycle N, the trailing "1" is also the first "1" of the next
    potential match.

    States:
      S0  — start / just saw something that isn't useful
      S1  — just saw a single "1"
      S2  — saw "1" followed by "0"   (so next "1" completes "101")
    """
    transitions = [
        # (src, input, dst, out)
        Transition('S0', (0,), 'S0', (0,)),
        Transition('S0', (1,), 'S1', (0,)),
        Transition('S1', (0,), 'S2', (0,)),
        Transition('S1', (1,), 'S1', (0,)),
        Transition('S2', (0,), 'S0', (0,)),
        Transition('S2', (1,), 'S1', (1,)),    # completed 101 → output 1
    ]
    return StateTable(
        states       = ['S0', 'S1', 'S2'],
        input_names  = ['x'],
        output_names = ['y'],
        transitions  = transitions,
        model        = 'mealy',
        reset_state  = 'S0',
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Modulo-4 up counter (Moore machine, no data inputs).
# ═══════════════════════════════════════════════════════════════════════════════

def mod4_counter() -> StateTable:
    """
    4-state Moore counter with 2-bit output equal to the state index.

    No data inputs; a single "enable" input could be added but we keep
    this one minimal to stress the 0-input corner of the truth-table
    projection (combinational cone has inputs = state-bits only).
    """
    transitions = [
        Transition('Q0', (), 'Q1', ()),
        Transition('Q1', (), 'Q2', ()),
        Transition('Q2', (), 'Q3', ()),
        Transition('Q3', (), 'Q0', ()),
    ]
    return StateTable(
        states       = ['Q0', 'Q1', 'Q2', 'Q3'],
        input_names  = [],
        output_names = ['y1', 'y0'],
        transitions  = transitions,
        model        = 'moore',
        reset_state  = 'Q0',
        state_outputs = {
            'Q0': (0, 0),
            'Q1': (0, 1),
            'Q2': (1, 0),
            'Q3': (1, 1),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  FSM with provably redundant states — exercises Hopcroft.
# ═══════════════════════════════════════════════════════════════════════════════

def redundant_detector() -> StateTable:
    """
    Same external behaviour as seq_detector_101() but with two extra
    "duplicate" states (S1b, S2b) that Hopcroft must collapse.

    Used to verify that minimize_states() on a completely-specified FSM
    reduces 5 states → 3 states with identical I/O.
    """
    transitions = [
        # Original S0/S1/S2 edges
        Transition('S0',  (0,), 'S0',  (0,)),
        Transition('S0',  (1,), 'S1',  (0,)),
        Transition('S1',  (0,), 'S2',  (0,)),
        Transition('S1',  (1,), 'S1b', (0,)),   # diverts to a twin of S1
        Transition('S2',  (0,), 'S0',  (0,)),
        Transition('S2',  (1,), 'S1',  (1,)),

        # Twin states — transitions identical to their originals
        Transition('S1b', (0,), 'S2b', (0,)),
        Transition('S1b', (1,), 'S1',  (0,)),
        Transition('S2b', (0,), 'S0',  (0,)),
        Transition('S2b', (1,), 'S1b', (1,)),
    ]
    return StateTable(
        states       = ['S0', 'S1', 'S2', 'S1b', 'S2b'],
        input_names  = ['x'],
        output_names = ['y'],
        transitions  = transitions,
        model        = 'mealy',
        reset_state  = 'S0',
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Incompletely-specified FSM — exercises IS-FSM minimization.
# ═══════════════════════════════════════════════════════════════════════════════

def partial_detector() -> StateTable:
    """
    Partial specification of the 101 detector: several (state, input)
    combinations are omitted so the minimiser has to treat them as
    don't-cares.  The canonical 3-state machine is still a valid cover.
    """
    transitions = [
        Transition('A', (0,), 'A', (0,)),
        Transition('A', (1,), 'B', (0,)),
        Transition('B', (0,), 'C', (0,)),
        # (B, 1) missing — DC
        Transition('C', (0,), 'A', (0,)),
        Transition('C', (1,), 'B', (1,)),
        # Extra "limbo" state, never entered by spec
        Transition('D', (0,), 'A', (DASH,)),
        Transition('D', (1,), 'D', (DASH,)),
    ]
    return StateTable(
        states       = ['A', 'B', 'C', 'D'],
        input_names  = ['x'],
        output_names = ['y'],
        transitions  = transitions,
        model        = 'mealy',
        reset_state  = 'A',
    )


FSM_EXAMPLES = {
    'seq101':       ('101-sequence detector (Mealy)',       seq_detector_101),
    'mod4':         ('Mod-4 up counter (Moore)',            mod4_counter),
    'redundant':    ('Redundant 101 detector (Hopcroft)',   redundant_detector),
    'partial':      ('Partial 101 detector (IS-FSM)',       partial_detector),
}
