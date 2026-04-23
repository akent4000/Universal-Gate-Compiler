"""
Formal equivalence verification via Miter circuits.

miter_verify(tt, result)
    Checks that the synthesized NAND network is functionally identical to
    the original TruthTable for every non-don't-care input combination.

Strategy
--------
1. If ``z3`` is installed: encode both the truth table (as a canonical SOP) and
   the gate network as z3 Boolean formulas.  The "miter" asserts that at least
   one output disagrees (OR of all output XORs).  If the solver returns UNSAT,
   the circuits are proven equivalent.

2. Fallback (no z3): exhaustive simulation over all defined minterms — identical
   to T7/T9 in tests.py, but returned as a structured dict.

Return value
------------
A dict with keys:
  'method'         — 'z3' | 'exhaustive'
  'equivalent'     — True / False / None (None = z3 returned 'unknown')
  'counterexample' — None, or dict {input_name: 0/1} witnessing a mismatch
  'checked'        — number of input combinations verified
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple

from .core.truth_table import TruthTable
from .pipeline         import OptimizeResult
from .mapping.nand     import eval_network
from .core.implicant   import int_to_bits


# ═══════════════════════════════════════════════════════════════════════════════
#  Public entry point
# ═══════════════════════════════════════════════════════════════════════════════

def miter_verify(
    tt:     TruthTable,
    result: OptimizeResult,
) -> Dict:
    """
    Formally verify that *result* implements *tt*.

    Tries z3 first; falls back to exhaustive simulation if z3 is absent.
    """
    try:
        import z3 as _z3  # noqa: F401 — just to test availability
        return _miter_z3(tt, result)
    except ImportError:
        return _miter_exhaustive(tt, result)


# ═══════════════════════════════════════════════════════════════════════════════
#  z3-based miter
# ═══════════════════════════════════════════════════════════════════════════════

def _miter_z3(tt: TruthTable, result: OptimizeResult) -> Dict:
    import z3

    n = tt.n_inputs
    var_names = tt.input_names
    defined   = {m for m in tt.rows if m not in tt.dont_cares}

    # ── input variables ───────────────────────────────────────────────────────
    inp_vars: Dict[str, object] = {name: z3.Bool(name) for name in var_names}

    # ── expected output formulas (canonical SOP from the truth table) ─────────
    # For each output j: OR of (AND of input literals) for every on-set minterm.
    # Don't-care minterms are excluded from the miter condition entirely via a
    # guard clause in the miter formula.
    expected: Dict[str, object] = {}
    for j, out_name in enumerate(tt.output_names):
        terms = []
        for m in sorted(defined):
            if tt.rows[m][j] != 1:
                continue
            bits = int_to_bits(m, n)
            lits = [
                inp_vars[var_names[i]] if bits[i] == 1
                else z3.Not(inp_vars[var_names[i]])
                for i in range(n)
            ]
            terms.append(z3.And(*lits) if len(lits) > 1 else lits[0])
        expected[out_name] = z3.Or(*terms) if terms else z3.BoolVal(False)

    # ── synthesized network as z3 formulas ────────────────────────────────────
    wire: Dict[str, object] = dict(inp_vars)
    for gate_name, gtype, ins in result.builder.gates:
        if gtype == 'NAND':
            wire[gate_name] = z3.Not(z3.And(wire[ins[0]], wire[ins[1]]))
        elif gtype == 'ZERO':
            wire[gate_name] = z3.BoolVal(False)
        elif gtype == 'ONE':
            wire[gate_name] = z3.BoolVal(True)
        # OUTPUT pseudo-gates are not in builder.gates; handled below.

    # ── build miter: OR of all output XORs ────────────────────────────────────
    # Guard: only fire for minterms that are NOT don't-cares.
    # We express this as: the miter is active only when the input combination
    # is NOT any of the don't-care minterms.
    xors = []
    for out_name in tt.output_names:
        r         = result[out_name]
        synth_out = wire.get(r.out_wire, z3.BoolVal(False))
        exp_out   = expected[out_name]
        xors.append(z3.Xor(synth_out, exp_out))

    miter_body = z3.Or(*xors) if xors else z3.BoolVal(False)

    # Exclude don't-care input combinations from the miter obligation.
    if tt.dont_cares:
        dc_clauses = []
        for m in tt.dont_cares:
            bits = int_to_bits(m, n)
            lits = [
                inp_vars[var_names[i]] if bits[i] == 1
                else z3.Not(inp_vars[var_names[i]])
                for i in range(n)
            ]
            dc_clauses.append(z3.And(*lits) if len(lits) > 1 else lits[0])
        is_dc    = z3.Or(*dc_clauses)
        miter    = z3.And(z3.Not(is_dc), miter_body)
    else:
        miter = miter_body

    # ── solve ─────────────────────────────────────────────────────────────────
    solver = z3.Solver()
    solver.add(miter)
    status = solver.check()

    if status == z3.unsat:
        return {
            'method':         'z3',
            'equivalent':     True,
            'counterexample': None,
            'checked':        len(defined),
        }
    elif status == z3.sat:
        model = solver.model()
        ce = {}
        for name, var in inp_vars.items():
            val = model[var]
            ce[name] = 1 if z3.is_true(val) else 0
        return {
            'method':         'z3',
            'equivalent':     False,
            'counterexample': ce,
            'checked':        len(defined),
        }
    else:
        return {
            'method':         'z3',
            'equivalent':     None,
            'counterexample': None,
            'checked':        len(defined),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  Exhaustive simulation fallback
# ═══════════════════════════════════════════════════════════════════════════════

def _miter_exhaustive(tt: TruthTable, result: OptimizeResult) -> Dict:
    n         = tt.n_inputs
    var_names = tt.input_names
    defined   = sorted(m for m in tt.rows if m not in tt.dont_cares)

    for m in defined:
        bits = int_to_bits(m, n)
        inp  = {var_names[i]: bits[i] for i in range(n)}
        for j, out_name in enumerate(tt.output_names):
            r   = result[out_name]
            got = eval_network(r.gates, inp)
            exp = tt.rows[m][j]
            if got != exp:
                return {
                    'method':         'exhaustive',
                    'equivalent':     False,
                    'counterexample': inp,
                    'checked':        m,
                }

    return {
        'method':         'exhaustive',
        'equivalent':     True,
        'counterexample': None,
        'checked':        len(defined),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Bounded Model Checking for FSMs (Phase 3)
# ═══════════════════════════════════════════════════════════════════════════════

def _per_output_dc_sets(fsm_result) -> Dict[str, set]:
    """
    Compute per-output don't-care minterm sets from an FSMResult.

    For completely-specified FSMs every set is empty.  For IS-FSMs, any
    transition with a missing dst (next-state DC) or a DASH output bit
    populates the corresponding set so the BMC miter can suppress false
    alarms on those positions.
    """
    from .sequential.fsm   import _expand_stt, _pattern_bits
    from .core.truth_table import DASH as _DASH

    stt        = fsm_result.stt
    enc        = fsm_result.encoding
    excitation = fsm_result.excitation
    w          = len(fsm_result.state_bit_names)
    n_in       = stt.n_input_bits
    tt         = fsm_result.truth_table

    delta, lam = _expand_stt(stt)
    used_codes  = set(enc.values())
    code_to_st  = {v: k for k, v in enc.items()}

    global_dc = set(tt.dont_cares)
    per_dc: Dict[str, set] = {nm: set(global_dc) for nm in tt.output_names}

    for scode in range(1 << w):
        s_bits = _pattern_bits(scode, w)
        if s_bits not in used_codes:
            # Unused state encoding: all outputs DC
            for nm in per_dc:
                for ipat in range(1 << n_in):
                    per_dc[nm].add((scode << n_in) | ipat)
            continue

        src = code_to_st[s_bits]
        for ipat in range(1 << n_in):
            key_m = (scode << n_in) | ipat
            if key_m in global_dc:
                continue

            dst = delta[(src, ipat)]
            out = lam[(src, ipat)]

            # Excitation bits: DC when dst is None (no next-state specified)
            if dst is None:
                if excitation == 'd':
                    for i in range(w):
                        per_dc[f'D{i}'].add(key_m)
                else:
                    for i in range(w):
                        per_dc[f'J{i}'].add(key_m)
                        per_dc[f'K{i}'].add(key_m)

            # FSM output bits: DC when the output specification has DASH
            for j_out, nm in enumerate(stt.output_names):
                if j_out < len(out) and out[j_out] == _DASH:
                    per_dc[nm].add(key_m)

    return per_dc


def bmc_verify(
    fsm_result,
    bound: int = 10,
) -> Dict:
    """
    Bounded Model Checking for a synthesised FSM.

    Unrolls *bound* clock cycles symbolically via Z3.  At each time-frame t
    (0 … K-1) both the reference TruthTable SOP and the synthesised NAND
    network are evaluated under the same state bits and free input variables.
    State is advanced along the reference (TT-SOP) trajectory; a miter XOR
    fires whenever the synthesised network deviates on any *specified* output
    or next-state bit.  Don't-care positions (IS-FSM missing transitions or
    DASH output bits) are excluded from the miter so no false alarms arise.

    Initial state : ``fsm_result.reset_code`` (flip-flops at power-up).
    Free variables: one Z3 Bool per FSM input per time step.
    UNSAT          ⇒ no deviation for any input sequence of length ≤ K.

    Return value
    ------------
    dict with keys:
      'method'         — 'bmc'
      'bound'          — K
      'equivalent'     — True / False / None  (None = z3 unknown)
      'counterexample' — None, or dict:
                           'step'   : first divergence step (0-based)
                           'inputs' : [{name: 0/1}, …] one entry per step
                           'states' : [bits_tuple, …] K+1 reference state codes
      'checked'        — number of time frames verified
    """
    try:
        import z3
    except ImportError:
        return {
            'method': 'bmc', 'bound': bound,
            'equivalent': None, 'counterexample': None, 'checked': 0,
        }

    stt         = fsm_result.stt
    tt          = fsm_result.truth_table
    opt         = fsm_result.opt_result
    excitation  = fsm_result.excitation
    state_names = fsm_result.state_bit_names        # e.g. ['Q0', 'Q1']
    in_names    = list(stt.input_names)             # FSM data inputs
    out_names   = list(fsm_result.fsm_output_names) # FSM outputs (non-state)
    w           = len(state_names)
    n_in        = len(in_names)
    n_out       = len(out_names)
    reset_code  = fsm_result.reset_code

    # ── TT SOP: on-set minterm lists per output ───────────────────────────────
    tt_in_names = list(tt.input_names)   # state_bits ++ fsm_inputs
    n_tt_in     = tt.n_inputs
    defined     = {m for m in tt.rows if m not in tt.dont_cares}
    onset: Dict[str, List[int]] = {nm: [] for nm in tt.output_names}
    for m in sorted(defined):
        for j, nm in enumerate(tt.output_names):
            if tt.rows[m][j] == 1:
                onset[nm].append(m)

    # ── Per-output DC sets (IS-FSM support) ───────────────────────────────────
    per_dc = _per_output_dc_sets(fsm_result)

    def _sop(name: str, wire: Dict) -> object:
        """Z3 SOP formula for TT output *name* given symbolic wire assignments."""
        terms = []
        for m in onset[name]:
            bits = int_to_bits(m, n_tt_in)
            lits = [
                wire[tt_in_names[i]] if bits[i] else z3.Not(wire[tt_in_names[i]])
                for i in range(n_tt_in)
            ]
            terms.append(z3.And(*lits) if len(lits) > 1 else lits[0])
        return z3.Or(*terms) if terms else z3.BoolVal(False)

    def _is_dc(nm: str, wire: Dict) -> object:
        """Z3 formula: True iff current state+input is DC for output *nm*."""
        terms = []
        for m in per_dc.get(nm, ()):
            bits = int_to_bits(m, n_tt_in)
            lits = [
                wire[tt_in_names[i]] if bits[i] else z3.Not(wire[tt_in_names[i]])
                for i in range(n_tt_in)
            ]
            terms.append(z3.And(*lits) if len(lits) > 1 else lits[0])
        return z3.Or(*terms) if terms else z3.BoolVal(False)

    def _ref_ns(wire: Dict, s_t: List) -> List:
        """Reference next-state bits from TT SOP."""
        if excitation == 'd':
            return [_sop(f'D{i}', wire) for i in range(w)]
        # JK: Q(t+1) = J·~Q(t) | ~K·Q(t)
        return [
            z3.Or(z3.And(_sop(f'J{i}', wire), z3.Not(s_t[i])),
                  z3.And(z3.Not(_sop(f'K{i}', wire)), s_t[i]))
            for i in range(w)
        ]

    def _syn_ns(ws: Dict, s_t: List) -> List:
        """Synthesised next-state bits from evaluated NAND wire dict."""
        if excitation == 'd':
            return [ws.get(opt[f'D{i}'].out_wire, z3.BoolVal(False))
                    for i in range(w)]
        # JK: Q(t+1) = J·~Q(t) | ~K·Q(t)
        return [
            z3.Or(z3.And(ws.get(opt[f'J{i}'].out_wire, z3.BoolVal(False)),
                         z3.Not(s_t[i])),
                  z3.And(z3.Not(ws.get(opt[f'K{i}'].out_wire, z3.BoolVal(False))),
                         s_t[i]))
            for i in range(w)
        ]

    # ── BMC unrolling ─────────────────────────────────────────────────────────
    solver = z3.Solver()

    # Named state variables sv[t][i] = state bit i at time step t (0…bound)
    sv = [[z3.Bool(f'_bmc_s{i}_t{t}') for i in range(w)]
          for t in range(bound + 1)]
    # Anchor step-0 state to reset_code
    for i in range(w):
        solver.add(sv[0][i] == z3.BoolVal(bool(reset_code[i])))

    # Free input variables iv[t][j] = FSM input j at step t
    iv = [[z3.Bool(f'_bmc_i{j}_t{t}') for j in range(n_in)]
          for t in range(bound)]

    miter: List = []

    for t in range(bound):
        wire: Dict = {state_names[i]: sv[t][i] for i in range(w)}
        wire.update({in_names[j]: iv[t][j] for j in range(n_in)})

        # ── Synthesised NAND evaluation ───────────────────────────────────────
        ws: Dict = dict(wire)
        for gn, gt, ins in opt.builder.gates:
            if gt == 'NAND':
                ws[gn] = z3.Not(z3.And(ws[ins[0]], ws[ins[1]]))
            elif gt == 'ZERO':
                ws[gn] = z3.BoolVal(False)
            elif gt == 'ONE':
                ws[gn] = z3.BoolVal(True)

        syn_ns  = _syn_ns(ws, sv[t])
        syn_out = [ws.get(opt[nm].out_wire, z3.BoolVal(False)) for nm in out_names]

        # ── Reference TT SOP evaluation ───────────────────────────────────────
        ref_ns  = _ref_ns(wire, sv[t])
        ref_out = [_sop(nm, wire) for nm in out_names]

        # State transition constraint: reference drives state forward
        for i in range(w):
            solver.add(sv[t + 1][i] == ref_ns[i])

        # Miter: XOR guarded by Not(is_dc) to suppress IS-FSM false alarms
        for j in range(n_out):
            nm = out_names[j]
            clause = z3.Xor(ref_out[j], syn_out[j])
            dc     = _is_dc(nm, wire)
            miter.append(z3.And(z3.Not(dc), clause) if per_dc.get(nm) else clause)
        for i in range(w):
            dc_nm  = f'D{i}' if excitation == 'd' else f'J{i}'
            clause = z3.Xor(ref_ns[i], syn_ns[i])
            dc     = _is_dc(dc_nm, wire)
            miter.append(z3.And(z3.Not(dc), clause) if per_dc.get(dc_nm) else clause)

    if not miter:
        return {'method': 'bmc', 'bound': bound, 'equivalent': True,
                'counterexample': None, 'checked': 0}

    solver.add(z3.Or(*miter))
    status = solver.check()

    if status == z3.unsat:
        return {'method': 'bmc', 'bound': bound, 'equivalent': True,
                'counterexample': None, 'checked': bound}

    if status == z3.sat:
        mdl = solver.model()

        inp_trace = [
            {in_names[j]: (1 if z3.is_true(mdl[iv[t][j]]) else 0)
             for j in range(n_in)}
            for t in range(bound)
        ]
        state_trace = [
            tuple(1 if z3.is_true(mdl[sv[t][i]]) else 0 for i in range(w))
            for t in range(bound + 1)
        ]
        first_step = _bmc_first_mismatch(fsm_result, inp_trace, state_trace, per_dc)
        return {
            'method': 'bmc', 'bound': bound, 'equivalent': False,
            'counterexample': {
                'step':   first_step,
                'inputs': inp_trace,
                'states': state_trace,
            },
            'checked': bound,
        }

    return {'method': 'bmc', 'bound': bound, 'equivalent': None,
            'counterexample': None, 'checked': bound}


def _bmc_first_mismatch(
    fsm_result,
    inp_trace:   List[Dict],
    state_trace: List[Tuple],
    per_dc:      Optional[Dict[str, set]] = None,
) -> int:
    """Concrete simulation to find the first step where ref and synth diverge."""
    tt          = fsm_result.truth_table
    opt         = fsm_result.opt_result
    excitation  = fsm_result.excitation
    state_names = fsm_result.state_bit_names
    in_names    = list(fsm_result.stt.input_names)
    out_names   = list(fsm_result.fsm_output_names)
    tt_in_names = list(tt.input_names)
    w           = len(state_names)
    tt_out_idx  = {nm: j for j, nm in enumerate(tt.output_names)}
    if per_dc is None:
        per_dc = {}

    for t, step_inp in enumerate(inp_trace):
        state    = state_trace[t]
        combined = {state_names[i]: state[i] for i in range(w)}
        combined.update(step_inp)

        # TT minterm index (MSB-first over tt_in_names order)
        m = 0
        for nm in tt_in_names:
            m = (m << 1) | combined.get(nm, 0)
        ref_row = tt.rows.get(m)
        if ref_row is None:
            continue  # globally DC minterm — skip

        # Evaluate NAND network
        ws: Dict = dict(combined)
        for gn, gt, ins in opt.builder.gates:
            if gt == 'NAND':
                ws[gn] = 1 - int(ws.get(ins[0], 0) == 1 and ws.get(ins[1], 0) == 1)
            elif gt == 'ZERO':
                ws[gn] = 0
            elif gt == 'ONE':
                ws[gn] = 1

        # Compare FSM outputs (skip per-output DC positions)
        for nm in out_names:
            if m in per_dc.get(nm, ()):
                continue
            r = opt.outputs.get(nm)
            if ref_row[tt_out_idx[nm]] != (ws.get(r.out_wire, 0) if r else 0):
                return t

        # Compare excitation / next-state bits (skip DC)
        if excitation == 'd':
            for i in range(w):
                dc_nm = f'D{i}'
                if m in per_dc.get(dc_nm, ()):
                    continue
                r = opt.outputs.get(dc_nm)
                if ref_row[tt_out_idx[dc_nm]] != (ws.get(r.out_wire, 0) if r else 0):
                    return t
        else:
            for i in range(w):
                j_nm = f'J{i}'; k_nm = f'K{i}'
                if m in per_dc.get(j_nm, ()):
                    continue  # J and K are always DC together
                j_r = opt.outputs.get(j_nm)
                k_r = opt.outputs.get(k_nm)
                j_ref = ref_row[tt_out_idx[j_nm]]
                k_ref = ref_row[tt_out_idx[k_nm]]
                j_syn = ws.get(j_r.out_wire, 0) if j_r else 0
                k_syn = ws.get(k_r.out_wire, 0) if k_r else 0
                ns_ref = (j_ref & (1 - state[i])) | ((1 - k_ref) & state[i])
                ns_syn = (j_syn & (1 - state[i])) | ((1 - k_syn) & state[i])
                if ns_ref != ns_syn:
                    return t

    return -1
