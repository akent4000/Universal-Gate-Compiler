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
from .decomposition import (DecompositionResult, ashenhurst_decompose,
                             ashenhurst_decompose_recursive,
                             RecursiveDecompositionResult,
                             multi_output_decompose,
                             SharedDecompositionResult)
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
    shared_prebuilt: Optional[Dict[str, int]] = None,
) -> None:
    """
    Per-output AIG construction with a recursive Ashenhurst-Curtis bake-off.

    `shared_prebuilt` carries h-literal bindings already materialised by the
    multi-output shared decomposition pass (if it ran).  When present, the
    per-output decomposition is skipped — the caller has already decided that
    this output is built on top of the shared h-bus, and `r.expr_fact` has
    been replaced with the corresponding g_j expression.
    """
    var_names = tt.input_names

    if verbose:
        print(f'\n  -- {r.name} : post-cross-output --')

    # Shannon decomposition and redundant inversion elimination are now handled
    # implicitly by the AIG: complement bits make NOT(NOT(x)) free, and
    # structural hashing propagates shared sub-expressions across outputs.
    r.expr_shan  = r.expr_fact
    r.expr_clean = r.expr_fact

    from .nand import expr_to_aig

    # Shared-support path: h's have already been pushed into the AIG by the
    # caller and r.expr_fact is g_j(h-names, X_free).  Just build g_j.
    if shared_prebuilt is not None:
        out_lit = expr_to_aig(r.expr_fact, aig, var_map=shared_prebuilt)
        if r.is_comp:
            out_lit = aig.make_not(out_lit)
        r.decomp      = None
        r.decomp_used = True
        r.out_lit     = out_lit
        if verbose:
            print(f'\n  [4] Shared-support decomposition (applied)')
            print(f'      OUTPUT({r.name}) <- lit {out_lit}')
        return

    # [4] Recursive Ashenhurst-Curtis / Roth-Karp Functional Decomposition
    rdecomp: Optional[RecursiveDecompositionResult] = None
    use_decomp = False

    if decompose and tt.n_inputs >= 3 and tt.n_inputs <= 12:
        if r.is_comp:
            decomp_target = (set(range(1 << tt.n_inputs)) - r.ones - tt.dont_cares)
        else:
            decomp_target = r.ones
        rdecomp = ashenhurst_decompose_recursive(
            decomp_target, tt.dont_cares, tt.n_inputs, var_names,
        )

    # [5] AIG Construction — whole-tree snapshot/restore bake-off
    if rdecomp is not None:
        snap      = aig.snapshot()
        size0     = aig.n_ands

        lit_base  = expr_to_aig(r.expr_fact, aig)
        cost_base = aig.n_ands - size0

        aig.restore(snap)

        dmap: Dict[str, int] = {}
        for node in rdecomp.pre_nodes:
            dmap[node.name] = expr_to_aig(factorize(node.expr), aig, var_map=dmap)
        lit_dec   = expr_to_aig(factorize(rdecomp.root_expr), aig, var_map=dmap)
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
            print(f'\n  [4] Recursive Ashenhurst-Curtis Decomposition  ({tag})')
            print(f'      depth={rdecomp.depth}, '
                  f'{rdecomp.n_nodes} aux node(s),  shared-AIG delta: '
                  f'baseline {cost_base} vs decomposed {cost_dec} AND nodes')
    else:
        if verbose:
            print(f'\n  [4] Ashenhurst-Curtis Decomposition  (no candidate)')
        out_lit = expr_to_aig(r.expr_fact, aig)

    if r.is_comp:
        out_lit = aig.make_not(out_lit)

    r.decomp      = None
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
        self.sta:         Optional[object]            = None   # STAResult (see sta.py)
        self.switching:   Optional[object]            = None   # SwitchingActivity (see switching.py)

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
             script: Optional[str] = None,
             bandit_horizon: int = 0,
             bandit_strategy: str = 'ucb1') -> OptimizeResult:
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

    # Phase 2.3: Shared-support Ashenhurst-Curtis across all outputs ---------
    #
    # A single joint chart over every output is searched for a bipartition
    # X_bound ∪ X_free whose column multiplicity admits a shared h-bus.
    # When found, every output is built on top of the same h-literals,
    # which is the main lever for the bin→BCD→7seg gap (215 vs 370 NAND).
    #
    # Bake-off is done on the shared AIG at whole-pipeline level: snapshot,
    # try shared build, compare to per-output recursive build, keep the
    # smaller one.
    shared_bindings: Optional[Dict[str, int]] = None
    shared: Optional[SharedDecompositionResult] = None
    shared_exprs: List[Expr] = []

    if decompose and tt.n_outputs >= 2 and 3 <= tt.n_inputs <= 12:
        per_output_targets: List[set] = []
        for idx in range(tt.n_outputs):
            r = result.outputs[tt.output_names[idx]]
            if r.is_comp:
                per_output_targets.append(
                    set(range(1 << tt.n_inputs)) - r.ones - tt.dont_cares
                )
            else:
                per_output_targets.append(set(r.ones))

        with profile_pass('Shared-support decomposition', report,
                          detail=f'{tt.n_outputs} outputs, {tt.n_inputs} vars'):
            shared = multi_output_decompose(
                per_output_targets, tt.dont_cares,
                tt.n_inputs, tt.input_names,
                output_names=tt.output_names,
            )

    if shared is not None:
        # Speculative AIG build of shared h-bus + per-output g_j.
        aig = builder._aig
        snap_shared = aig.snapshot()
        size0 = aig.n_ands

        from .nand import expr_to_aig
        dmap_shared: Dict[str, int] = {}
        for hn, he in zip(shared.h_names, shared.h_exprs):
            dmap_shared[hn] = expr_to_aig(factorize(he), aig, var_map=dmap_shared)

        shared_g_lits: List[int] = []
        for gj in shared.g_exprs:
            shared_g_lits.append(expr_to_aig(factorize(gj), aig, var_map=dmap_shared))
        cost_shared = aig.n_ands - size0

        # Per-output recursive baseline for comparison.
        aig.restore(snap_shared)
        size0 = aig.n_ands
        for idx, name in enumerate(tt.output_names):
            r = result.outputs[name]
            expr_to_aig(r.expr_fact, aig)
        cost_per_output = aig.n_ands - size0

        if cost_shared < cost_per_output:
            # Commit shared build.
            aig.restore(snap_shared)
            shared_bindings = {}
            for hn, he in zip(shared.h_names, shared.h_exprs):
                shared_bindings[hn] = expr_to_aig(
                    factorize(he), aig, var_map=shared_bindings)
            # Replace each output's expr_fact with its g_j and record it.
            for idx, name in enumerate(tt.output_names):
                r = result.outputs[name]
                r.expr_fact  = shared.g_exprs[idx]
                r.expr_shan  = shared.g_exprs[idx]
                r.expr_clean = shared.g_exprs[idx]
                shared_exprs.append(shared.g_exprs[idx])

            if verbose:
                print(f"\n  [3.5] Shared-support decomposition (applied)")
                print(f"        |Xb|={len(shared.bound_vars)}, "
                      f"k={shared.k}, mu={shared.mu}, "
                      f"AIG delta: shared {cost_shared} vs per-output {cost_per_output}")
        else:
            if verbose:
                print(f"\n  [3.5] Shared-support decomposition (rejected)")
                print(f"        |Xb|={len(shared.bound_vars)}, "
                      f"k={shared.k}, mu={shared.mu}, "
                      f"AIG delta: shared {cost_shared} vs per-output {cost_per_output}")
            # Roll back to the clean checkpoint so _phase2 sees a truly empty AIG
            # and its own bake-off works correctly.
            aig.restore(snap_shared)
            shared = None
    elif verbose and decompose and tt.n_outputs >= 2 and 3 <= tt.n_inputs <= 12:
        print(f"\n  [3.5] Shared-support decomposition (no candidate)")

    with profile_pass('AIG build (direct)', report):
        for idx, name in enumerate(tt.output_names):
            _phase2(tt, result.outputs[name], builder._aig, verbose,
                    out_idx=idx, decompose=decompose,
                    shared_prebuilt=shared_bindings)

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
    elif bandit_horizon > 0:
        # Bandit-guided synthesis: adaptively select passes.
        from .script import run_bandit
        if verbose:
            print(f"\n  [post-AIG] Bandit synthesis "
                  f"(strategy={bandit_strategy!r}, horizon={bandit_horizon})")
        with profile_pass('Bandit synthesis', report,
                          detail=f'horizon={bandit_horizon} strategy={bandit_strategy}'):
            new_aig, new_out_lits, _bandit = run_bandit(
                builder._aig, out_lits,
                horizon=bandit_horizon,
                strategy=bandit_strategy,
                verbose=verbose,
            )
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

        # Phase 3.7: Don't-Care-based rewriting (SDC + windowed ODC) ----------
        from .dont_care import dc_optimize
        _before_dc = new_aig.n_nodes
        with profile_pass("Don't-care rewriting", report,
                          detail=f'{new_aig.n_nodes} nodes'):
            new_aig, new_out_lits = dc_optimize(new_aig, new_out_lits)
        if verbose:
            print(f"\n  [7.7] Don't-Care Rewriting (SDC)")
            print(f"      AIG nodes: {_before_dc} -> {new_aig.n_nodes}")

        # Phase 3.8: second rewrite sweep picks up the DC-exposed reductions --
        from .rewrite import rewrite_aig as _rewrite
        _before_rw2 = new_aig.n_nodes
        with profile_pass('AIG rewriting (post-DC)', report,
                          detail=f'{new_aig.n_nodes} nodes'):
            new_aig, new_out_lits = _rewrite(new_aig,
                                              out_lits=new_out_lits,
                                              rounds=1)
        if verbose:
            print(f"\n  [7.8] Local AIG Rewriting (post-DC)")
            print(f"      AIG nodes: {_before_rw2} -> {new_aig.n_nodes}")

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

    # [10] Static Timing Analysis ------------------------------------------
    from .sta import compute_sta
    sta = compute_sta(result)
    if verbose:
        sta.print_summary(output_names=list(tt.output_names))

    # [11] Switching Activity Estimation -----------------------------------
    from .switching import estimate_switching as _est_sw
    sw = _est_sw(new_aig, new_out_lits, output_names=list(tt.output_names))
    result.switching = sw
    if verbose:
        print(f"\n  [SW] Switching Activity  "
              f"(total AND-node: {sw.total_activity:.4f}, "
              f"max possible: {0.25 * new_aig.n_ands:.4f})")
        for name, p in sw.output_probs.items():
            print(f"       OUT {name:<12} P(1)={p:.4f}  sw={p*(1-p):.4f}")

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


# ── Hierarchical (multi-stage) synthesis ────────────────────────────────────

def hierarchical_optimize(
    stage_specs: list,
    post_script: Optional[str] = None,
    verbose: bool = True,
) -> 'OptimizeResult':
    """
    Multi-stage hierarchical synthesis.

    stage_specs is a list of dicts, each with:
      'tt'      : TruthTable for this stage
      'connect' : dict {input_name → source_name} — maps inputs of this stage
                  to named outputs of any previously processed stage.
                  Absent or None for the first stage (pure primary inputs).
      'rename'  : dict {old_output_name → new_output_name} (optional).

    Algorithm:
      1. Optimize every unique TruthTable independently with the default
         rewrite/fraig/balance pipeline.
      2. Walk stages in order; compose each stage's AIG into a single shared
         AIG via AIG.compose(), substituting 'connect' inputs with the
         literal produced by the previous stage(s).
      3. GC the combined AIG, then run post_script (default:
         "rewrite; fraig; balance; rewrite -z; fraig; balance").
      4. Map to NAND gates and return an OptimizeResult.
    """
    from .script import run_script
    from .nand   import aig_to_gates, nand_gate_count, NANDBuilder
    from .aig    import AIG

    default_stage_script = "rewrite; fraig; balance; rewrite -z; fraig; balance"
    if post_script is None:
        post_script = "rewrite; fraig; balance; rewrite -z; fraig; balance"

    # --- Step 1: optimise each unique TruthTable once ----------------------
    _cache: dict = {}
    for spec in stage_specs:
        tt = spec['tt']
        key = id(tt)
        if key not in _cache:
            if verbose:
                print(f"\n  [hierarchical] Optimising stage: {tt.output_names}")
            _cache[key] = optimize(tt, verbose=verbose,
                                   script=default_stage_script)

    # --- Step 2: compose stages into one AIG --------------------------------
    # Start with an empty AIG; the first stage provides primary inputs.
    combined_aig = AIG()
    available: dict = {}   # name → lit in combined_aig

    final_out_lits:  list = []
    final_out_names: list = []

    for spec in stage_specs:
        tt      = spec['tt']
        connect = spec.get('connect') or {}
        rename  = spec.get('rename')  or {}

        stage_result = _cache[id(tt)]
        stage_aig    = stage_result.aig

        # Build substitution: stage input names → lits in combined_aig
        subst = {}
        for inp in tt.input_names:
            if inp in connect:
                src = connect[inp]
                if src not in available:
                    raise KeyError(
                        f"hierarchical_optimize: source '{src}' not yet "
                        f"defined (stage tt={tt.output_names})")
                subst[inp] = available[src]
            # inputs NOT in connect become new primary inputs (handled inside compose)

        # Merge stage into combined_aig
        lit_map = combined_aig.compose(stage_aig, subst)

        # Register this stage's outputs (possibly renamed) in available pool
        stage_out_lits = [lit_map[l] for l in stage_result.out_lits]
        for out_name, lit in zip(tt.output_names, stage_out_lits):
            new_name = rename.get(out_name, out_name)
            available[new_name] = lit

        # The last stages' outputs become the final circuit outputs
        final_out_lits  = [lit_map[l] for l in stage_result.out_lits]
        final_out_names = [rename.get(n, n) for n in tt.output_names]

    # Accumulate all final outputs (all terminal stages contribute)
    # Re-collect: every stage whose output name wasn't consumed as an input
    # to a later stage is a "terminal" output.
    consumed_as_input: set = set()
    for spec in stage_specs:
        for src in (spec.get('connect') or {}).values():
            consumed_as_input.add(src)

    combined_out_lits:  list = []
    combined_out_names: list = []
    for name, lit in available.items():
        if name not in consumed_as_input:
            combined_out_lits.append(lit)
            combined_out_names.append(name)

    # --- Step 3: GC + post-composition optimisation -------------------------
    combined_aig, combined_out_lits = combined_aig.gc(combined_out_lits)

    if verbose:
        print(f"\n  [hierarchical] Combined AIG: {combined_aig.n_ands} AND nodes, "
              f"{len(combined_out_lits)} outputs")
        print(f"  [hierarchical] Post-script: {post_script!r}")

    new_aig, new_out_lits = run_script(
        combined_aig, combined_out_lits, post_script, verbose)

    if verbose:
        print(f"\n  [hierarchical] After script: {new_aig.n_ands} AND nodes")

    # --- Step 4: NAND mapping -----------------------------------------------
    final_gates, out_wires, _n_xor = aig_to_gates(new_aig, new_out_lits)

    builder = NANDBuilder()
    builder._aig   = new_aig
    builder.gates  = final_gates

    r = OptimizeResult()
    r.aig        = new_aig
    r.out_lits   = new_out_lits
    r.builder    = builder

    # Populate per-output stubs so downstream consumers (verify, export_circ,
    # iteration) can treat a hierarchical OptimizeResult like a flat one.
    for i, name in enumerate(combined_out_names):
        builder.gates.append((name, 'OUTPUT', [out_wires[i]]))
        stub = OutputResult()
        stub.name     = name
        stub.out_wire = out_wires[i]
        stub.gates    = list(builder.gates)
        r.outputs[name] = stub

    r.total_nand = nand_gate_count(builder.gates)

    if verbose:
        print(f"\n  [hierarchical] TOTAL NAND GATES: {r.total_nand}")

    return r