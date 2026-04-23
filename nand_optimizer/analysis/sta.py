"""
Static Timing Analysis (STA) for AIG and NAND gate networks.

Forward pass  : arrival_time[v] = max(arrival_time[fanins]) + gate_delay
Backward pass : required_time[v] = min(required_time[fanouts]) - gate_delay
Slack         : slack[v] = required_time[v] - arrival_time[v]
                Negative slack means the timing constraint is violated.

Delay model: unit delay — every AND or NAND gate costs 1 time unit.
Primary inputs default to arrival time 0; per-input overrides accepted.
Inversions on AIG complement edges are treated as free (no extra delay).
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..core.aig import AIG, Lit as AIGLit


# ── Timing entry ──────────────────────────────────────────────────────────────

@dataclass
class TimingEntry:
    """Timing data for a single AIG node or NAND wire."""
    arrival:  float
    required: float

    @property
    def slack(self) -> float:
        return self.required - self.arrival


# ── STA result ────────────────────────────────────────────────────────────────

class STAResult:
    """
    Full STA result produced by compute_sta().

    Attributes
    ----------
    aig_timing : dict[node_id → TimingEntry]
        Timing for every AIG node (node 0 = constant, node_id ≥ 1).
    nand_timing : dict[wire_name → TimingEntry]
        Timing for every wire in the NAND gate list (including PI wires,
        intermediate gate outputs, and the OUTPUT pseudo-wires).
    max_arrival : float
        Critical-path length — the maximum arrival time across all output
        signals in the NAND network (or AIG if no NAND network available).
    critical_path_aig : list[int]
        Node IDs from the deepest primary input to the latest output in the
        AIG, in topological (input-first) order.
    critical_path_nand : list[str]
        Wire names from the PI to the latest output in the NAND network,
        in topological (input-first) order.
    """

    def __init__(self) -> None:
        self.aig_timing:          Dict[int, TimingEntry] = {}
        self.nand_timing:         Dict[str, TimingEntry] = {}
        self.max_arrival:         float                  = 0.0
        self.critical_path_aig:   List[int]              = []
        self.critical_path_nand:  List[str]              = []

    # ── reporting ─────────────────────────────────────────────────────────────

    def print_summary(
        self,
        output_names: Optional[List[str]] = None,
        *,
        n_tail: int = 8,
    ) -> None:
        """Print a compact timing report to stdout."""
        print(f"\n  [STA] Critical-path depth : {self.max_arrival:.1f} gate delays")

        if self.critical_path_nand:
            tail = self.critical_path_nand[-n_tail:]
            print(f"  [STA] Critical path (tail): {' → '.join(tail)}")

        if output_names and self.nand_timing:
            print("  [STA] Output slacks:")
            for nm in output_names:
                if nm in self.nand_timing:
                    t = self.nand_timing[nm]
                    print(f"          {nm:<14}  arr={t.arrival:5.1f}  "
                          f"req={t.required:5.1f}  slack={t.slack:+.1f}")

    def critical_wires(self, n: int = 10) -> List[Tuple[str, float]]:
        """Return the n wires with smallest slack (most timing-critical)."""
        return sorted(
            self.nand_timing.items(),
            key=lambda kv: kv[1].slack,
        )[:n]


# ── AIG-level STA ─────────────────────────────────────────────────────────────

def _aig_forward(
    aig: AIG,
    input_delays: Dict[str, float],
) -> Dict[int, float]:
    """Compute arrival times for every AIG node (topological forward pass)."""
    arrival: Dict[int, float] = {0: 0.0}   # node 0 = constant
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            arrival[nid] = input_delays.get(entry[1], 0.0)
        else:
            _, lit_a, lit_b = entry
            arr_a = arrival.get(aig.node_of(lit_a), 0.0)
            arr_b = arrival.get(aig.node_of(lit_b), 0.0)
            arrival[nid] = max(arr_a, arr_b) + 1.0
    return arrival


def _aig_backward(
    aig: AIG,
    out_lits: List[AIGLit],
    arrival: Dict[int, float],
    constraint: float,
) -> Dict[int, float]:
    """Compute required times for every AIG node (reverse topological pass)."""
    INF = float('inf')
    required: Dict[int, float] = {}

    # Outputs must satisfy the timing constraint.
    for lit in out_lits:
        nid = aig.node_of(lit)
        old = required.get(nid, INF)
        required[nid] = min(old, constraint)

    # Backward sweep.
    for i in range(len(aig._nodes) - 1, -1, -1):
        nid  = i + 1
        entry = aig._nodes[i]
        req  = required.get(nid, INF)
        if req == INF:
            # Dead node — give it slack=0 so it doesn't pollute statistics.
            req = arrival.get(nid, 0.0)
            required[nid] = req
        if entry[0] == 'input':
            continue
        _, lit_a, lit_b = entry
        for lit in (lit_a, lit_b):
            cnid = aig.node_of(lit)
            old = required.get(cnid, INF)
            required[cnid] = min(old, req - 1.0)

    # Fix up inputs/constant that were never written (no live path to output).
    for nid in range(len(aig._nodes) + 1):
        if nid not in required:
            required[nid] = arrival.get(nid, 0.0)

    return required


def _aig_critical_path(
    aig: AIG,
    out_lits: List[AIGLit],
    arrival: Dict[int, float],
) -> List[int]:
    """Trace the critical path in the AIG from PI to output (input-first)."""
    if not out_lits:
        return []
    # Start at the latest-arriving output.
    start_lit = max(out_lits, key=lambda l: arrival.get(aig.node_of(l), 0.0))
    nid = aig.node_of(start_lit)

    path: List[int] = []
    seen: set = set()
    while nid > 0 and nid not in seen:
        seen.add(nid)
        path.append(nid)
        entry = aig._nodes[nid - 1]
        if entry[0] == 'input':
            break
        _, lit_a, lit_b = entry
        na = aig.node_of(lit_a)
        nb = aig.node_of(lit_b)
        nid = na if arrival.get(na, 0.0) >= arrival.get(nb, 0.0) else nb

    path.reverse()
    return path


def sta_aig(
    aig: AIG,
    out_lits: List[AIGLit],
    *,
    input_delays: Optional[Dict[str, float]] = None,
    output_constraint: Optional[float] = None,
) -> Tuple[Dict[int, TimingEntry], List[int], float]:
    """
    Run STA on an AIG.

    Parameters
    ----------
    aig : AIG
    out_lits : list of output literals
    input_delays : {input_name: arrival_time} overrides (default 0 for all)
    output_constraint : required arrival time at outputs.
        Defaults to max_arrival so that critical-path slack = 0.

    Returns
    -------
    timing : dict[node_id → TimingEntry]
    critical_path : list[node_id] in topological order (PI first)
    max_arrival : float
    """
    ids     = input_delays or {}
    arrival = _aig_forward(aig, ids)

    max_arr = max(
        (arrival.get(aig.node_of(l), 0.0) for l in out_lits),
        default=0.0,
    )
    constraint = output_constraint if output_constraint is not None else max_arr
    required   = _aig_backward(aig, out_lits, arrival, constraint)

    timing: Dict[int, TimingEntry] = {
        nid: TimingEntry(
            arrival  = arrival.get(nid, 0.0),
            required = required.get(nid, arrival.get(nid, 0.0)),
        )
        for nid in range(len(aig._nodes) + 1)
    }

    critical = _aig_critical_path(aig, out_lits, arrival)
    return timing, critical, max_arr


# ── NAND-level STA ────────────────────────────────────────────────────────────

Gate = Tuple[str, str, List[str]]   # (name, type, inputs)


def sta_nand(
    gates: List[Gate],
    *,
    input_delays: Optional[Dict[str, float]] = None,
    output_constraint: Optional[float] = None,
) -> Tuple[Dict[str, TimingEntry], List[str], float]:
    """
    Run STA on a NAND gate network.

    Delay model: every NAND gate = 1 time unit; OUTPUT pseudo-gates = 0.
    Gate list is assumed to be in topological order (as produced by the
    pipeline), but primary inputs are handled gracefully even when
    they appear only as fanins of later gates.

    Parameters
    ----------
    gates : list of (name, type, inputs) tuples
    input_delays : {wire_name: arrival_time} overrides (default 0)
    output_constraint : required arrival at outputs (default = max_arrival)

    Returns
    -------
    timing : dict[wire_name → TimingEntry]
    critical_path : list[wire_name] in topological order (PI first)
    max_arrival : float
    """
    ids: Dict[str, float] = input_delays or {}

    # ── Forward pass ──────────────────────────────────────────────────────────
    arrival: Dict[str, float] = {}

    for name, gtype, inputs in gates:
        if gtype == 'OUTPUT':
            # Zero-cost pseudo-gate; arrival of the output label tracks its driver.
            src = inputs[0] if inputs else name
            arrival[name] = arrival.get(src, ids.get(src, 0.0))
        elif gtype == 'NAND':
            parent = max(
                (arrival.get(w, ids.get(w, 0.0)) for w in inputs),
                default=0.0,
            )
            arrival[name] = parent + 1.0
        else:
            arrival[name] = ids.get(name, 0.0)

    # Fill primary-input wires (appear in gate inputs, never as a gate name).
    for _, _, inputs in gates:
        for w in inputs:
            if w not in arrival:
                arrival[w] = ids.get(w, 0.0)

    output_wires = [name for name, gtype, _ in gates if gtype == 'OUTPUT']

    max_arr = max((arrival.get(w, 0.0) for w in output_wires), default=0.0)
    constraint = output_constraint if output_constraint is not None else max_arr

    # ── Backward pass ─────────────────────────────────────────────────────────
    INF = float('inf')
    required: Dict[str, float] = {}

    for w in output_wires:
        required[w] = constraint

    for name, gtype, inputs in reversed(gates):
        req = required.get(name, INF)
        if req == INF:
            req = arrival.get(name, 0.0)
            required[name] = req

        if gtype == 'OUTPUT':
            src = inputs[0] if inputs else name
            old = required.get(src, INF)
            required[src] = min(old, req)
        elif gtype == 'NAND':
            req_child = req - 1.0
            for w in inputs:
                old = required.get(w, INF)
                required[w] = min(old, req_child)

    # Fix up any wires still at INF (PI wires not reached by backward pass).
    for w in arrival:
        if required.get(w, INF) == INF:
            required[w] = arrival[w]

    # ── Build timing dict ─────────────────────────────────────────────────────
    all_wires = set(arrival) | set(required)
    timing: Dict[str, TimingEntry] = {
        w: TimingEntry(
            arrival  = arrival.get(w, 0.0),
            required = required.get(w, arrival.get(w, 0.0)),
        )
        for w in all_wires
    }

    # ── Critical path trace ───────────────────────────────────────────────────
    gate_map: Dict[str, Tuple[str, List[str]]] = {
        n: (gt, inp) for n, gt, inp in gates
    }

    if not output_wires:
        critical: List[str] = []
    else:
        start = max(output_wires, key=lambda w: arrival.get(w, 0.0))
        path: List[str] = [start]
        cur = start
        seen: set = set()

        for _ in range(100_000):   # safety cap
            seen.add(cur)
            entry = gate_map.get(cur)
            if entry is None:
                break   # primary input
            gt, inputs = entry
            if gt == 'OUTPUT':
                nxt = inputs[0] if inputs else None
            elif gt == 'NAND':
                nxt = (
                    max(inputs, key=lambda w: arrival.get(w, 0.0))
                    if inputs else None
                )
            else:
                nxt = None
            if nxt is None or nxt in seen:
                break
            path.append(nxt)
            cur = nxt

        path.reverse()
        critical = path

    return timing, critical, max_arr


# ── Convenience entry point ───────────────────────────────────────────────────

def compute_sta(result: object) -> STAResult:
    """
    Run STA on the final AIG and NAND network inside an OptimizeResult.

    Populates and returns an STAResult; also stores it on result.sta.
    """
    sta = STAResult()

    aig      = getattr(result, 'aig', None)
    out_lits = getattr(result, 'out_lits', None) or []
    builder  = getattr(result, 'builder', None)
    gates    = getattr(builder, 'gates', None) if builder else None

    if aig is not None and out_lits:
        aig_t, aig_path, max_arr = sta_aig(aig, out_lits)
        sta.aig_timing        = aig_t
        sta.critical_path_aig = aig_path
        sta.max_arrival       = max_arr

    if gates:
        nand_t, nand_path, max_arr_n = sta_nand(gates)
        sta.nand_timing         = nand_t
        sta.critical_path_nand  = nand_path
        sta.max_arrival         = max_arr_n   # NAND level is authoritative

    result.sta = sta   # type: ignore[attr-defined]
    return sta
