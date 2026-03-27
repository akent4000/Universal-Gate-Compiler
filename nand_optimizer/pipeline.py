"""
Universal NAND optimisation pipeline.

    result = optimize(truth_table, verbose=True)

Takes any TruthTable and produces a shared NAND gate network
for all outputs, with full intermediate results available for
inspection and verification.
"""

from __future__ import annotations
from typing import Dict, List, Optional

from .truth_table import TruthTable
from .expr        import Expr, ZERO
from .implicant   import Implicant, espresso, implicants_to_expr, int_to_bits
from .optimize    import phase_assign, factorize, apply_shannon, elim_inv
from .nand        import (NANDBuilder, Gate, dead_code_elimination,
                          eval_network, nand_gate_count)


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-output result
# ═══════════════════════════════════════════════════════════════════════════════

class OutputResult:
    """Holds all intermediate and final results for one output."""

    def __init__(self):
        self.name:        str              = ''
        self.ones:        set              = set()
        self.is_comp:     bool             = False
        self.imps:        List[Implicant]  = []
        self.expr_sop:    Expr             = ZERO
        self.expr_fact:   Expr             = ZERO
        self.expr_shan:   Expr             = ZERO
        self.expr_clean:  Expr             = ZERO
        self.out_wire:    str              = ''
        self.gates:       List[Gate]       = []

    @property
    def n_nand(self) -> int:
        return nand_gate_count(self.gates)


# ═══════════════════════════════════════════════════════════════════════════════
#  Single-output optimisation
# ═══════════════════════════════════════════════════════════════════════════════

def _optimize_output(
    tt:       TruthTable,
    out_idx:  int,
    builder:  NANDBuilder,
    verbose:  bool,
) -> OutputResult:
    """Run the full pipeline for one output of the truth table."""
    r = OutputResult()
    name = tt.output_names[out_idx]
    r.name = name
    ones = tt.ones(out_idx)
    dc   = tt.dont_cares
    r.ones = ones

    var_names = tt.input_names
    n_vars    = tt.n_inputs

    if verbose:
        bar = '─' * 54
        print(f'\n┌{bar}┐')
        print(f'│  Output  {name:<44}│')
        print(f'└{bar}┘')
        print(f'  Ones       : {sorted(ones)}')
        print(f"  Don't cares: {sorted(dc)}")

    # [1] Espresso / QMC
    raw_imps = espresso(ones, dc, n_vars)
    raw_expr = implicants_to_expr(raw_imps, var_names)
    if verbose:
        print(f'\n  [1] Espresso / QMC')
        print(f'      f = {raw_expr}   (literals: {raw_expr.literals()})')

    # [2] Phase Assignment
    imps, is_comp = phase_assign(ones, dc, n_vars)
    expr_sop = implicants_to_expr(imps, var_names)
    r.is_comp  = is_comp
    r.imps     = imps
    r.expr_sop = expr_sop
    if verbose:
        form = '~f  (complement)' if is_comp else ' f  (direct)'
        print(f'\n  [2] Phase Assignment  →  {form}')
        print(f'      = {expr_sop}   (literals: {expr_sop.literals()})')

    # [3] Algebraic Factorization
    expr_fact = factorize(expr_sop)
    r.expr_fact = expr_fact
    if verbose:
        d = expr_fact.literals() - expr_sop.literals()
        print(f'\n  [3] Algebraic Factorization')
        print(f'      = {expr_fact}   (literals: {expr_fact.literals()}, Δ: {d:+})')

    # [4] Shannon Decomposition
    expr_shan = apply_shannon(expr_fact, var_names)
    r.expr_shan = expr_shan
    if verbose:
        d = expr_shan.literals() - expr_fact.literals()
        tag = 'improved' if d < 0 else 'no change'
        print(f'\n  [4] Shannon Decomposition  ({tag})')
        print(f'      = {expr_shan}   (literals: {expr_shan.literals()}, Δ: {d:+})')

    # [5] Redundant Inversion Elimination
    expr_clean = elim_inv(expr_shan)
    r.expr_clean = expr_clean
    if verbose:
        print(f'\n  [5] Redundant Inversion Elimination')
        print(f'      = {expr_clean}   (literals: {expr_clean.literals()})')

    # [6] NAND Conversion
    start_idx = len(builder.gates)
    out_wire  = builder.build_expr(expr_clean)

    if is_comp:
        out_wire = builder.nand(out_wire)

    r.out_wire = out_wire
    r.gates    = list(builder.gates) + [(name, 'OUTPUT', [out_wire])]

    if verbose:
        new_gates = builder.gates[start_idx:]
        print(f'\n  [6] NAND Network  ({len(new_gates)} new, '
              f'global: {nand_gate_count(builder.gates)})')
        for gn, gt, gi in new_gates + [(name, 'OUTPUT', [out_wire])]:
            if gt == 'OUTPUT':
                print(f'      OUTPUT({name}) ← {gi[0]}')
            elif gt == 'NAND':
                tied = len(gi) == 2 and gi[0] == gi[1]
                tag  = '  [NOT]' if tied else ''
                print(f'      {gn:6} = NAND({", ".join(gi)}){tag}')

    return r


# ═══════════════════════════════════════════════════════════════════════════════
#  Full pipeline
# ═══════════════════════════════════════════════════════════════════════════════

class OptimizeResult:
    """Container for full optimisation results."""

    def __init__(self):
        self.truth_table: Optional[TruthTable]       = None
        self.outputs:     Dict[str, OutputResult]     = {}
        self.builder:     Optional[NANDBuilder]       = None
        self.total_nand:  int                         = 0

    def __getitem__(self, key: str) -> OutputResult:
        return self.outputs[key]

    def __iter__(self):
        return iter(self.outputs)

    def items(self):
        return self.outputs.items()

    def values(self):
        return self.outputs.values()


def optimize(tt: TruthTable, verbose: bool = True) -> OptimizeResult:
    """
    Run the full NAND optimisation pipeline on a truth table.

    Returns an OptimizeResult with per-output details and a shared
    gate list.  All tests can be run against the returned object.
    """
    if verbose:
        print('╔══════════════════════════════════════════════════════╗')
        print('║   NAND OPTIMIZER                                     ║')
        print('║   QMC → Phase → Factor → Shannon → InvElim → NAND   ║')
        print('╚══════════════════════════════════════════════════════╝')
        print(f'\n  Truth table: {tt.n_inputs} inputs, {tt.n_outputs} outputs')
        print(f'  Inputs : {tt.input_names}')
        print(f'  Outputs: {tt.output_names}')

    builder = NANDBuilder()
    result  = OptimizeResult()
    result.truth_table = tt
    result.builder     = builder

    for idx, name in enumerate(tt.output_names):
        result.outputs[name] = _optimize_output(tt, idx, builder, verbose)

    # [7] Dead Code Elimination
    output_wires = [r.out_wire for r in result.outputs.values()]
    dead_code_elimination(builder, output_wires)

    # refresh per-output gate lists after DCE
    for r in result.outputs.values():
        out_gate = r.gates[-1]
        r.gates  = list(builder.gates) + [out_gate]

    result.total_nand = nand_gate_count(builder.gates)

    # ── Summary ───────────────────────────────────────────────────────────
    if verbose:
        print('\n' + '═' * 68)
        print('OPTIMISATION SUMMARY')
        print('═' * 68)
        print(f'  {"Out":<8} {"Phase":<6} {"Expression":<45}')
        print('  ' + '─' * 64)
        for name, r in result.outputs.items():
            form = '~f' if r.is_comp else ' f'
            s = str(r.expr_clean)
            if len(s) > 43:
                s = s[:40] + '…'
            print(f'  {name:<8} {form:<6} {s:<45}')
        print('  ' + '─' * 64)
        print(f'  {"TOTAL SHARED NAND GATES":48} {result.total_nand}')

        print('\n' + '═' * 68)
        print('GLOBAL NAND GATE LIST')
        print('═' * 68)
        print(f'  Inputs: {", ".join(tt.input_names)}\n')
        print('  ┌── Gates ──')
        for gn, gt, gi in builder.gates:
            if gt == 'NAND':
                tied = len(gi) == 2 and gi[0] == gi[1]
                if tied:
                    print(f'  │  {gn:6} = NOT({gi[0]})')
                else:
                    print(f'  │  {gn:6} = NAND({", ".join(gi)})')
        print('  │')
        print('  ├── Outputs ──')
        for name, r in result.outputs.items():
            print(f'  │  {name} ← {r.out_wire}')
        print('  └── DONE.\n')

    return result