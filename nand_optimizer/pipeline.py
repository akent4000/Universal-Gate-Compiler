"""
Universal NAND optimisation pipeline.

    result = optimize(truth_table, verbose=True)

Takes any TruthTable and produces a shared NAND gate network
for all outputs, with full intermediate results available for
inspection and verification.
"""

from __future__ import annotations
from typing import Dict, List, Optional

from .truth_table   import TruthTable
from .expr          import Expr, ZERO
from .implicant     import Implicant, espresso, implicants_to_expr, int_to_bits
from .optimize      import phase_assign, factorize
from .implicant     import _expand_cubes_to_set
from .decomposition import DecompositionResult, ashenhurst_decompose
from .nand          import (NANDBuilder, Gate, dead_code_elimination,
                            eval_network, nand_gate_count)
from .profile       import ProfileReport, profile_pass


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
        self.decomp:      Optional[DecompositionResult] = None
        self.decomp_used: bool             = False
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
    r.ones     = tt.ones(out_idx)   # Set[int] for small n; empty for large n
    r.is_comp  = is_comp
    r.imps     = imps

    var_names  = tt.input_names

    if verbose:
        bar = '-' * 54
        print(f'\n+{bar}+')
        print(f'|  Output  {r.name:<44}|')
        print(f'+{bar}+')
        if tt.n_inputs <= 20:
            print(f'  Ones       : {sorted(r.ones)}')
            print(f"  Don't cares: {sorted(tt.dont_cares)}")
        else:
            print(f'  On-cubes   : {len(tt.ones_cubes(out_idx))} cubes')

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
    tt:           TruthTable,
    r:            OutputResult,
    aig:          AIG,
    verbose:      bool,
    out_idx:      int = 0,
    decompose:    bool = True,
) -> None:
    var_names = tt.input_names

    if verbose:
        bar = '-' * 54
        print(f'\n  -- {r.name} : post-cross-output --')

    # Shannon decomposition and redundant inversion elimination are now handled
    # implicitly by the AIG: complement bits make NOT(NOT(x)) free, and
    # structural hashing propagates shared sub-expressions across outputs.
    r.expr_shan  = r.expr_fact
    r.expr_clean = r.expr_fact

    # [4] Ashenhurst-Curtis / Roth-Karp Functional Decomposition
    h_opt:  List[Expr] = []
    g_opt:  Optional[Expr] = None
    decomp: Optional[DecompositionResult] = None
    use_decomp = False

    if decompose and tt.n_inputs >= 3 and tt.n_inputs <= 12:
        # Compute decomp_target only when needed (avoid set(range(1<<n)) for large n)
        if r.is_comp:
            decomp_target = (set(range(1 << tt.n_inputs)) - r.ones - tt.dont_cares)
        else:
            decomp_target = r.ones
        decomp = ashenhurst_decompose(
            decomp_target, tt.dont_cares, tt.n_inputs, var_names,
            h_name_prefix=f'__d{out_idx}_h',
        )

    if decomp is not None:
        for he in decomp.h_exprs:
            h_opt.append(factorize(he))
        g_opt = factorize(decomp.g_expr)

    # [5] AIG Construction — snapshot/restore for the decomposition bake-off
    from .nand import expr_to_aig
    if decomp is not None:
        # Speculative build: snapshot the shared AIG, try baseline first,
        # then try decomposition from the same starting point, keep the
        # version that grew the AIG by fewer AND nodes.  Measuring on the
        # *shared* AIG means structural-hash sharing with previously-built
        # outputs is accounted for correctly.
        snap      = aig.snapshot()
        size0     = aig.n_ands

        lit_base  = expr_to_aig(r.expr_fact, aig)
        cost_base = aig.n_ands - size0

        aig.restore(snap)

        dmap: Dict[str, int] = {}
        for hn, he in zip(decomp.h_names, h_opt):
            dmap[hn] = expr_to_aig(he, aig)
        lit_dec   = expr_to_aig(g_opt, aig, var_map=dmap)
        cost_dec  = aig.n_ands - size0

        if cost_dec < cost_base:
            out_lit    = lit_dec
            use_decomp = True
        else:
            aig.restore(snap)
            out_lit    = expr_to_aig(r.expr_fact, aig)
            use_decomp = False

        if verbose:
            tag = 'applied' if use_decomp else 'rejected'
            print(f'\n  [4] Ashenhurst-Curtis Decomposition  ({tag})')
            print(f'      |Xb|={len(decomp.bound_vars)}, mu={decomp.mu}, '
                  f'k={decomp.k},  shared-AIG delta: '
                  f'baseline {cost_base} vs decomposed {cost_dec} AND nodes')
    else:
        if verbose:
            print(f'\n  [4] Ashenhurst-Curtis Decomposition  (no candidate)')
        out_lit = expr_to_aig(r.expr_fact, aig)

    if r.is_comp:
        out_lit = aig.make_not(out_lit)

    r.decomp      = decomp
    r.decomp_used = use_decomp
    r.out_lit     = out_lit

    if verbose:
        print(f'\n  [5] AIG Network Node Generation')
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

    on_cubes = tt.ones_cubes(out_idx)
    dc_cubes = tt.dc_cubes
    n_vars   = tt.n_inputs

    imps, is_comp = _phase_assign(on_cubes, dc_cubes, n_vars)
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
        self.profile:     Optional[ProfileReport]     = None
        self.aig:         Optional[object]            = None   # final AIG after rewriting
        self.out_lits:    List[int]                   = []     # output literals into aig

    def __getitem__(self, key: str) -> OutputResult:
        return self.outputs[key]

    def __iter__(self):
        return iter(self.outputs)

    def items(self):
        return self.outputs.items()

    def values(self):
        return self.outputs.values()


def optimize(tt: TruthTable, verbose: bool = True,
             profile: bool = False, decompose: bool = True,
             balance: bool = True,
             script: Optional[str] = None) -> OptimizeResult:
    if verbose:
        print('======================================================')
        print('   NAND OPTIMIZER                                     ')
        print('   MO-QMC -> Phase -> Factor -> XFactor ->            ')
        print('   AIG (direct) -> Rewrite -> NAND                   ')
        print('======================================================')
        print(f'\n  Truth table: {tt.n_inputs} inputs, {tt.n_outputs} outputs')
        print(f'  Inputs : {tt.input_names}')
        print(f'  Outputs: {tt.output_names}')

    report: Optional[ProfileReport] = ProfileReport() if profile else None

    n_vars   = tt.n_inputs
    dc_cubes = tt.dc_cubes

    # Phase 2.1 Espresso (per-output with phase assignment)
    mo_imps: list = []
    mo_comp: list = []
    with profile_pass('Espresso', report,
                      detail=f'{tt.n_outputs} outputs, {n_vars} vars'):
        for i in range(tt.n_outputs):
            on_cubes = tt.ones_cubes(i)
            imps, is_comp = phase_assign(on_cubes, dc_cubes, n_vars)
            mo_imps.append(imps)
            mo_comp.append(is_comp)

    builder = NANDBuilder()
    result  = OptimizeResult()
    result.truth_table = tt
    result.builder     = builder
    result.profile     = report

    with profile_pass('Factorization (per-output)', report):
        for idx, name in enumerate(tt.output_names):
            result.outputs[name] = _phase1(tt, idx, mo_imps[idx], mo_comp[idx], verbose)

    # Phase 2.2 Cross-output Factorization
    try:
        from .optimize import cross_output_factorize
        facts_before = [result.outputs[name].expr_fact for name in tt.output_names]
        with profile_pass('Cross-output factorization', report):
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

    with profile_pass('AIG build (direct)', report):
        for idx, name in enumerate(tt.output_names):
            _phase2(tt, result.outputs[name], builder._aig, verbose,
                    out_idx=idx, decompose=decompose)

    out_lits = [result.outputs[name].out_lit for name in tt.output_names]

    if script is not None:
        # User-supplied synthesis script replaces the fixed rewrite/fraig/balance
        # sequence.  Each semicolon-separated command is applied in order.
        from .script import run_script
        if verbose:
            print(f"\n  [post-AIG] Synthesis script: {script!r}")
        with profile_pass('Synthesis script', report, detail=script):
            new_aig, new_out_lits = run_script(
                builder._aig, out_lits, script, verbose)
    else:
        # Phase 3: DAG-aware AIG Rewriting ------------------------------------
        if verbose:
            print("\n  [7] Local AIG Rewriting (fanout-aware)")

        from .rewrite import rewrite_aig
        with profile_pass('AIG rewriting', report,
                          detail=f'{builder._aig.n_nodes} input nodes'):
            new_aig, new_out_lits = rewrite_aig(builder._aig,
                                                 out_lits=out_lits,
                                                 rounds=1)

        if verbose:
            print(f"      AIG nodes: {builder._aig.n_nodes} -> {new_aig.n_nodes}")

        # Phase 3.5: FRAIGing — simulation + SAT equivalence merging ----------
        from .fraig import fraig as _fraig
        _before_fraig = new_aig.n_nodes
        with profile_pass('FRAIGing', report, detail=f'{new_aig.n_nodes} nodes'):
            new_aig, new_out_lits = _fraig(new_aig, new_out_lits)
        if verbose:
            print(f"\n  [7.5] FRAIGing (simulation + SAT equivalence merging)")
            print(f"      AIG nodes: {_before_fraig} -> {new_aig.n_nodes}")

        # Phase 4: AIG Balancing (depth reduction) ----------------------------
        if balance:
            from .balance import balance_aig, aig_depth
            depth_before = aig_depth(new_aig, new_out_lits)
            bal_aig, bal_out_lits = balance_aig(new_aig, new_out_lits)
            depth_after  = aig_depth(bal_aig, bal_out_lits)

            if verbose:
                print(f"\n  [8] AIG Balancing (depth)")
                print(f"      Depth: {depth_before} -> {depth_after}  "
                      f"(AIG nodes: {new_aig.n_nodes} -> {bal_aig.n_nodes})")

            new_aig      = bal_aig
            new_out_lits = bal_out_lits

    result.aig      = new_aig
    result.out_lits = new_out_lits

    # [9] Final NAND Topology Emission (with XOR extraction) ----------------
    from .nand import aig_to_gates
    with profile_pass('AIG -> NAND mapping', report,
                      detail=f'{new_aig.n_nodes} nodes'):
        final_gates, out_wires, n_xor = aig_to_gates(new_aig, new_out_lits)

    if verbose:
        tag = f'{n_xor} XOR/XNOR pattern(s) → 4-NAND' if n_xor else 'no XOR patterns'
        print(f"\n  [9] XOR Extraction  ({tag})")

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
        print(f'  {"Out":<8} {"Phase":<6} {"Dec":<4} {"Expression":<41}')
        print('  ' + '-' * 64)
        for name, r in result.outputs.items():
            form = '~f' if r.is_comp else ' f'
            dec  = 'yes' if r.decomp_used else '-'
            s = str(r.expr_clean)
            if len(s) > 39:
                s = s[:36] + '...'
            print(f'  {name:<8} {form:<6} {dec:<4} {s:<41}')
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

    if report is not None:
        report.print()

    return result