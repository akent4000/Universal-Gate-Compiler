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


# -------------------------------------------------------------------------------
#  Per-output result
# -------------------------------------------------------------------------------

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


# -------------------------------------------------------------------------------
#  Single-output optimisation
# -------------------------------------------------------------------------------

def _phase1(
    tt:      TruthTable,
    out_idx: int,
    imps:    list,
    is_comp: bool,
    verbose: bool,
) -> OutputResult:
    r = OutputResult()
    r.name     = tt.output_names[out_idx]
    r.ones     = tt.ones(out_idx)
    r.is_comp  = is_comp
    r.imps     = imps
    
    var_names  = tt.input_names
    
    if verbose:
        bar = '-' * 54
        print(f'\n+{bar}+')
        print(f'|  Output  {r.name:<44}|')
        print(f'+{bar}+')
        print(f'  Ones       : {sorted(r.ones)}')
        print(f"  Don't cares: {sorted(tt.dont_cares)}")

    expr_sop   = implicants_to_expr(imps, var_names)
    r.expr_sop = expr_sop

    if verbose:
        form = '~f  (complement)' if is_comp else ' f  (direct)'
        print(f'\n  [1+2] Multi-output Espresso + Phase  ->  {form}')
        print(f'        = {expr_sop}   (literals: {expr_sop.literals()})')

    expr_fact   = factorize(expr_sop)
    r.expr_fact = expr_fact

    if verbose:
        d = expr_fact.literals() - expr_sop.literals()
        print(f'\n  [3] Algebraic Factorization')
        print(f'      = {expr_fact}   (literals: {expr_fact.literals()}, delta: {d:+})')
        
    return r

def _phase2(
    tt:       TruthTable,
    r:        OutputResult,
    aig:      AIG,
    verbose:  bool,
) -> None:
    var_names = tt.input_names

    if verbose:
        bar = '-' * 54
        print(f'\n  -- {r.name} : post-cross-output --')

    # [4] Shannon Decomposition
    expr_shan   = apply_shannon(r.expr_fact, var_names)
    r.expr_shan = expr_shan
    if verbose:
        d   = expr_shan.literals() - r.expr_fact.literals()
        tag = 'improved' if d < 0 else 'no change'
        print(f'\n  [4] Shannon Decomposition  ({tag})')
        print(f'      = {expr_shan}   (literals: {expr_shan.literals()}, delta: {d:+})')

    # [5] Redundant Inversion Elimination
    expr_clean   = elim_inv(expr_shan)
    r.expr_clean = expr_clean
    if verbose:
        print(f'\n  [5] Redundant Inversion Elimination')
        print(f'      = {expr_clean}   (literals: {expr_clean.literals()})')

    # [6] AIG Conversion
    from .nand import expr_to_aig
    out_lit = expr_to_aig(expr_clean, aig)
    if r.is_comp:
        out_lit = aig.make_not(out_lit)

    r.out_lit = out_lit

    if verbose:
        print(f'\n  [6] AIG Network Node Generation')
        print(f'      OUTPUT({r.name}) <- lit {out_lit}')


# -------------------------------------------------------------------------------
#  Full pipeline
# -------------------------------------------------------------------------------

# -- backward-compatible single-output helper (used by tests / external code) --

def _optimize_output(
    tt:      TruthTable,
    out_idx: int,
    builder: NANDBuilder,
    verbose: bool = False,
) -> OutputResult:
    from .optimize import phase_assign as _phase_assign, apply_shannon, elim_inv

    ones    = tt.ones(out_idx)
    dc      = tt.dont_cares
    n_vars  = tt.n_inputs

    imps, is_comp = _phase_assign(ones, dc, n_vars)
    r = _phase1(tt, out_idx, imps, is_comp, verbose)
    
    var_names = tt.input_names

    # [4] Shannon Decomposition
    expr_shan   = apply_shannon(r.expr_fact, var_names)
    r.expr_shan = expr_shan

    # [5] Redundant Inversion Elimination
    expr_clean   = elim_inv(expr_shan)
    r.expr_clean = expr_clean

    # [6] NAND mapping (legacy pathway used for backward-compat and tests)
    wire = builder.build_expr(expr_clean)
    r.out_wire = wire if not is_comp else builder.nand(wire)
    
    out_gate = (r.name, 'OUTPUT', [r.out_wire])
    builder.gates.append(out_gate)
    r.gates = list(builder.gates)
    
    return r

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
    if verbose:
        print('======================================================')
        print('   NAND OPTIMIZER                                     ')
        print('   MO-QMC -> Phase -> Factor -> XFactor ->            ')
        print('   Shannon -> InvElim -> NAND                         ')
        print('======================================================')
        print(f'\n  Truth table: {tt.n_inputs} inputs, {tt.n_outputs} outputs')
        print(f'  Inputs : {tt.input_names}')
        print(f'  Outputs: {tt.output_names}')

    dc     = tt.dont_cares
    n_vars = tt.n_inputs

    # Phase 2.1 Espresso 
    all_ones  = [tt.ones(i)  for i in range(tt.n_outputs)]
    
    mo_comp = [len(tt.ones(i)) > (1 << (tt.n_inputs - 1)) for i in range(tt.n_outputs)]
    
    try:
        from .optimize import multi_output_espresso
        all_targets = [
            (set(range(1<<n_vars)) - all_ones[i] - dc) if mo_comp[i] else all_ones[i] 
            for i in range(tt.n_outputs)
        ]
        mo_f = multi_output_espresso(all_targets, dc, n_vars)
        if verbose:
            print(f'\n  [2.1] Multi-output Espresso  ({len(mo_f.implicants)} shared prime implicant(s) across outputs)')
        mo_imps = mo_f.get_assignments()
    except ImportError:
        # Fallback to standard espresso
        from .implicant import espresso
        from .optimize import phase_assign as _phase_assign
        mo_imps = []
        for i in range(tt.n_outputs):
            imps, is_comp = _phase_assign(all_ones[i], dc, n_vars)
            mo_imps.append(imps)
            mo_comp[i] = is_comp

    builder = NANDBuilder()
    result  = OptimizeResult()
    result.truth_table = tt
    result.builder     = builder
    
    for idx, name in enumerate(tt.output_names):
        result.outputs[name] = _phase1(tt, idx, mo_imps[idx], mo_comp[idx], verbose)
        
    # Phase 2.2 Cross-output Factorization
    try:
        from .optimize import cross_output_factorize
        facts_before = [result.outputs[name].expr_fact for name in tt.output_names]
        facts_after = cross_output_factorize(facts_before)
        
        if verbose:
            n_improved = sum(1 for a, b in zip(facts_before, facts_after) if a.literals() - b.literals() > 0)
            saved = sum(a.literals() - b.literals() for a, b in zip(facts_before, facts_after))
            tag = f'{n_improved} output(s) improved, delta literals: {saved:+}' if n_improved else 'no change'
            print(f'\n  [2.2] Cross-output Factorization  ({tag})')

        for name, new_expr in zip(tt.output_names, facts_after):
            result.outputs[name].expr_fact = new_expr
    except ImportError:
        pass

    # Phase 2: per-output steps [4]-[6] -------------------------------------
    builder._aig = getattr(builder, '_aig', None)
    if builder._aig is None:
        from .aig import AIG
        builder._aig = AIG()
        
    for idx, name in enumerate(tt.output_names):
        _phase2(tt, result.outputs[name], builder._aig, verbose)
        
    # Phase 3: DAG-aware AIG Rewriting --------------------------------------
    if verbose:
        print("\n  [7] Local AIG Rewriting")
        
    from .rewrite import rewrite_aig
    out_lits = [result.outputs[name].out_lit for name in tt.output_names]
    new_aig, new_out_lits = rewrite_aig(builder._aig, out_lits=out_lits, rounds=2)
    
    if verbose:
        print(f"      AIG nodes: {builder._aig.n_nodes} -> {new_aig.n_nodes}")

    # [8] Final NAND Topology Emission --------------------------------------
    from .nand import aig_to_gates
    final_gates, out_wires = aig_to_gates(new_aig, new_out_lits)
    
    builder.gates = final_gates

    for out_idx, name in enumerate(tt.output_names):
        r = result.outputs[name]
        r.out_wire = out_wires[out_idx]
        out_gate = (name, 'OUTPUT', [r.out_wire])
        builder.gates.append(out_gate)
        # Tests evaluate r.gates individually, so they need the full shared network
        r.gates = list(builder.gates)

    result.total_nand = nand_gate_count(builder.gates)

    # -- Summary -----------------------------------------------------------
    if verbose:
        print('\n' + '-' * 68)
        print('OPTIMISATION SUMMARY')
        print('-' * 68)
        print(f'  {"Out":<8} {"Phase":<6} {"Expression":<45}')
        print('  ' + '-' * 64)
        for name, r in result.outputs.items():
            form = '~f' if r.is_comp else ' f'
            s = str(r.expr_clean)
            if len(s) > 43:
                s = s[:40] + '...'
            print(f'  {name:<8} {form:<6} {s:<45}')
        print('  ' + '-' * 64)
        print(f'  {"TOTAL SHARED NAND GATES":48} {result.total_nand}')

        print('\n' + '-' * 68)
        print('GLOBAL NAND GATE LIST')
        print('-' * 68)
        print(f'  Inputs: {", ".join(tt.input_names)}\n')
        print('  +-- Gates --')
        for gn, gt, gi in builder.gates:
            if gt == 'NAND':
                tied = len(gi) == 2 and gi[0] == gi[1]
                if tied:
                    print(f'  |  {gn:6} = NOT({gi[0]})')
                else:
                    print(f'  |  {gn:6} = NAND({", ".join(gi)})')
            elif gt == 'OUTPUT':
                print(f'  |  OUTPUT({gn}) <- {gi[0]}')
        print('  |')
        print('  +-- Outputs --')
        for name, r in result.outputs.items():
            print(f'  |  {name} <- {r.out_wire}')
        print('  +-- DONE.\n')

    return result