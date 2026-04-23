"""
ATPG – Automatic Test Pattern Generation for single stuck-at faults.

Single stuck-at fault model
----------------------------
A SA0 fault on wire *w* forces w=0 regardless of inputs.
A SA1 fault on wire *w* forces w=1 regardless of inputs.

Strategy per fault
------------------
1. Random simulation pre-filter (64 patterns) – detects ~85 % of faults at
   negligible cost.
2. Z3 SAT miter – encodes the good circuit and the faulty circuit (same
   primary inputs, identical gate equations except at the fault site which
   is clamped to the stuck value).  The miter asserts that at least one
   primary output disagrees.  SAT witness → test vector; UNSAT → fault
   is undetectable (redundant or constant path).

Public API
----------
run_atpg(gates, input_names, out_wires, verbose, random_seed) → AtpgResult
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import random as _random

Gate = Tuple[str, str, List[str]]   # (wire_name, gate_type, [input_wires])


# ─── result types ────────────────────────────────────────────────────────────

@dataclass
class FaultResult:
    wire:        str
    stuck_at:    int                           # 0 or 1
    detectable:  bool
    test_vector: Optional[Dict[str, int]] = None   # input_name → 0/1


@dataclass
class AtpgResult:
    faults:         List[FaultResult]      = field(default_factory=list)
    n_total:        int                    = 0
    n_detected:     int                    = 0
    n_undetectable: int                    = 0
    fault_coverage: float                  = 0.0
    test_vectors:   List[Dict[str, int]]   = field(default_factory=list)


# ─── simulation helpers ──────────────────────────────────────────────────────

def _sim_good(gates: List[Gate], inp: Dict[str, int]) -> Dict[str, int]:
    wires = dict(inp)
    for name, gtype, ins in gates:
        if gtype == 'NAND':
            wires[name] = 1 - (wires[ins[0]] & wires[ins[1]])
        elif gtype == 'ZERO':
            wires[name] = 0
        elif gtype == 'ONE':
            wires[name] = 1
    return wires


def _sim_faulty(
    gates:      List[Gate],
    inp:        Dict[str, int],
    fault_wire: str,
    stuck_at:   int,
) -> Dict[str, int]:
    wires = dict(inp)
    if fault_wire in wires:          # primary-input fault
        wires[fault_wire] = stuck_at
    for name, gtype, ins in gates:
        if gtype == 'NAND':
            wires[name] = 1 - (wires[ins[0]] & wires[ins[1]])
        elif gtype == 'ZERO':
            wires[name] = 0
        elif gtype == 'ONE':
            wires[name] = 1
        if name == fault_wire:       # gate-output fault: override after computation
            wires[name] = stuck_at
    return wires


def _random_detect(
    gates:       List[Gate],
    input_names: List[str],
    out_wires:   List[str],
    fault_wire:  str,
    stuck_at:    int,
    rng:         _random.Random,
    n_patterns:  int = 64,
) -> Optional[Dict[str, int]]:
    for _ in range(n_patterns):
        inp    = {n: rng.randint(0, 1) for n in input_names}
        good   = _sim_good(gates, inp)
        faulty = _sim_faulty(gates, inp, fault_wire, stuck_at)
        if any(good.get(ow) != faulty.get(ow) for ow in out_wires):
            return inp
    return None


# ─── SAT-based detection ─────────────────────────────────────────────────────

def _sat_detect(
    gates:       List[Gate],
    input_names: List[str],
    out_wires:   List[str],
    fault_wire:  str,
    stuck_at:    int,
) -> Optional[Dict[str, int]]:
    """
    Build a (good ‖ faulty) miter in Z3.

    Both circuits share the same primary-input Bool variables so the solver
    finds a single input assignment that makes at least one output disagree.
    """
    import z3

    inp_vars: Dict[str, object] = {n: z3.Bool(n) for n in input_names}

    # ── good circuit ─────────────────────────────────────────────────────────
    good: Dict[str, object] = dict(inp_vars)
    for name, gtype, ins in gates:
        if gtype == 'NAND':
            good[name] = z3.Not(z3.And(good[ins[0]], good[ins[1]]))
        elif gtype == 'ZERO':
            good[name] = z3.BoolVal(False)
        elif gtype == 'ONE':
            good[name] = z3.BoolVal(True)

    # ── faulty circuit (shares input variables) ───────────────────────────────
    faulty: Dict[str, object] = dict(inp_vars)
    if fault_wire in faulty:         # primary-input fault
        faulty[fault_wire] = z3.BoolVal(bool(stuck_at))
    for name, gtype, ins in gates:
        if gtype == 'NAND':
            faulty[name] = z3.Not(z3.And(faulty[ins[0]], faulty[ins[1]]))
        elif gtype == 'ZERO':
            faulty[name] = z3.BoolVal(False)
        elif gtype == 'ONE':
            faulty[name] = z3.BoolVal(True)
        if name == fault_wire:       # gate-output fault: clamp after expression
            faulty[name] = z3.BoolVal(bool(stuck_at))

    # ── miter: at least one primary output disagrees ─────────────────────────
    xors = [z3.Xor(good[ow], faulty[ow])
            for ow in out_wires if ow in good and ow in faulty]
    if not xors:
        return None

    s = z3.Solver()
    s.add(z3.Or(*xors))
    if s.check() != z3.sat:
        return None

    m = s.model()
    return {
        n: (1 if z3.is_true(m.eval(z3.Bool(n), model_completion=True)) else 0)
        for n in input_names
    }


# ─── public entry point ───────────────────────────────────────────────────────

def run_atpg(
    gates:       List[Gate],
    input_names: List[str],
    out_wires:   List[str],
    verbose:     bool = False,
    random_seed: int  = 42,
) -> AtpgResult:
    """
    Run single stuck-at ATPG on a NAND gate netlist.

    Parameters
    ----------
    gates        ``result.builder.gates`` — NAND/ZERO/ONE entries; no OUTPUT.
    input_names  Primary input names in the same order as ``tt.input_names``.
    out_wires    Observation wires — ``result[name].out_wire`` per output.
    verbose      Print one line per fault.
    random_seed  Seed for the random simulation pre-filter.

    Returns
    -------
    AtpgResult
    """
    rng = _random.Random(random_seed)

    # Fault sites: primary inputs + every NAND gate output wire
    fault_sites: List[str] = list(input_names)
    for name, gtype, _ in gates:
        if gtype == 'NAND':
            fault_sites.append(name)

    result = AtpgResult()
    seen:  List[Dict[str, int]] = []

    for wire in fault_sites:
        for stuck_at in (0, 1):
            # Phase 1: fast random simulation
            tv = _random_detect(gates, input_names, out_wires, wire, stuck_at, rng)
            # Phase 2: SAT if random missed it
            if tv is None:
                tv = _sat_detect(gates, input_names, out_wires, wire, stuck_at)

            fr = FaultResult(
                wire=wire,
                stuck_at=stuck_at,
                detectable=(tv is not None),
                test_vector=tv,
            )
            result.faults.append(fr)

            if tv is not None and tv not in seen:
                seen.append(tv)
                result.test_vectors.append(tv)

            if verbose:
                tag = f'  {wire:20s} SA{stuck_at}: '
                print(tag + (f'detected   tv={tv}' if tv is not None
                             else 'UNDETECTABLE'))

    result.n_total        = len(result.faults)
    result.n_detected     = sum(1 for f in result.faults if f.detectable)
    result.n_undetectable = result.n_total - result.n_detected
    result.fault_coverage = (result.n_detected / result.n_total
                             if result.n_total > 0 else 1.0)
    return result
