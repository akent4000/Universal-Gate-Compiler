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

from .truth_table import TruthTable
from .pipeline    import OptimizeResult
from .nand        import eval_network
from .implicant   import int_to_bits


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
