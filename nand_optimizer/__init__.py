"""
NAND Gate Optimizer — universal combinational logic → NAND network compiler.

Pipeline:
  1. Quine-McCluskey / Espresso  — minimise to SOP
  2. Phase Assignment            — choose f or ~f per output
  3. Algebraic Factorization     — extract common sub-expressions
  4. Shannon Decomposition       — cofactor simplification
  5. Redundant Inversion Elim.   — remove double negations
  6. NAND Conversion             — global AIG hashing
     6a. Greedy Reassociation    — reorder AND-chains for cache reuse
  7. Dead Code Elimination       — remove unused gates
"""

# ─── Bootstrap: auto-generate the 4-input NPN template DB on first import ───
# aig_db_4.py is a ~5.7 MB generated file and is not tracked in git. When it
# is missing we invoke `python -m nand_optimizer.precompute_4cut` as a
# subprocess: that module's `if __name__ == '__main__':` block calls
# generate_db() which fans out to a multiprocessing Pool.  Running the
# generator in a clean child process side-steps the well-known hang that
# occurs when mp.Pool is created from inside a package's __init__.py.
#
# The subprocess inherits an env guard so that neither it nor its Pool
# workers re-enter this bootstrap: when the guard is set, we skip all
# submodule imports (including any that would hit the still-missing
# aig_db_4.py).
import os as _os

if _os.environ.get('_NAND_OPTIMIZER_BOOTSTRAPPING') == '1':
    # Inside the bootstrap subprocess (or one of its Pool workers): skip all
    # submodule imports so nothing tries to load the still-missing aig_db_4.py.
    del _os
else:
    from . import precompute_4cut as _precompute_4cut
    if not _os.path.exists(_precompute_4cut.DB_PATH):
        import subprocess as _subprocess
        import sys as _sys
        print("[nand_optimizer] aig_db_4.py not found - generating NPN template DB "
              "(one-time, parallel)...", flush=True)
        _env = dict(_os.environ)
        _env['_NAND_OPTIMIZER_BOOTSTRAPPING'] = '1'
        _subprocess.run(
            [_sys.executable, '-m', 'nand_optimizer.precompute_4cut'],
            check=True, env=_env,
        )
        del _subprocess, _sys, _env
    del _os, _precompute_4cut

    from .truth_table       import TruthTable
    from .pipeline          import optimize, OutputResult
    from .expr              import Expr, Const, Lit, Not, And, Or, ONE, ZERO, simp
    from .implicant         import (Implicant, quine_mccluskey, espresso,
                                    multi_output_espresso)
    from .optimize          import (phase_assign, factorize, brayton_factor,
                                    apply_shannon, elim_inv,
                                    multi_output_factorize)
    from .decomposition     import (DecompositionResult, ashenhurst_decompose,
                                    decompose_expr)
    from .aig               import AIG
    from .nand              import (NANDBuilder, Gate, eval_network, nand_gate_count,
                                    implicants_to_aig)
    from .rewrite           import rewrite_aig, enumerate_cuts, evaluate_cut_tt
    from .fraig             import fraig, fraig_stats
    from .dont_care         import dc_optimize, dc_stats
    from .balance           import balance_aig, aig_depth
    from .sta               import STAResult, TimingEntry, sta_aig, sta_nand, compute_sta
    from .switching         import SwitchingActivity, estimate_switching
    from .exact_synthesis   import (exact_synthesize, evaluate_template,
                                    exact_cache_stats, exact_cache_clear)
    from .circ_export       import export_circ, export_fsm_circ
    from .dot_export        import aig_to_dot
    from .aiger_io          import write_aiger, read_aiger
    from .blif_io           import write_blif, read_blif
    from .verify            import miter_verify, bmc_verify
    from .atpg              import run_atpg, AtpgResult, FaultResult
    from .tests             import run_tests
    from .profile           import ProfileReport, profile_pass
    from .benchmark_runner  import run_benchmarks, run_one_benchmark, BENCHMARKS
    from .epfl_bench        import (run_epfl, run_one_epfl,
                                    check_epfl_updates, aig_equivalence)
    from .property_tests    import run_property_tests, check_equivalence
    from .fsm               import (StateTable, Transition, FSMResult,
                                    minimize_states, encode_states,
                                    fsm_to_truth_table, synthesize_fsm,
                                    simulate_fsm, parse_kiss)
    from .structural        import StructuralModule
    from .script            import (ScriptBandit, run_bandit,
                                    DEFAULT_ARMS, DEFAULT_SCRIPT,
                                    parse_script, run_script)
    from .verilog_io        import (read_verilog, parse_verilog,
                                    verilog_to_module, VerilogError)

    __all__ = [
        # core pipeline
        'TruthTable', 'optimize', 'OutputResult',
        # expression AST
        'Expr', 'Const', 'Lit', 'Not', 'And', 'Or', 'ONE', 'ZERO', 'simp',
        # logic minimisation
        'Implicant', 'quine_mccluskey', 'espresso', 'multi_output_espresso',
        # optimisation passes
        'phase_assign', 'factorize', 'brayton_factor', 'apply_shannon', 'elim_inv',
        'multi_output_factorize',
        # functional decomposition
        'DecompositionResult', 'ashenhurst_decompose', 'decompose_expr',
        # gate network
        'AIG', 'NANDBuilder', 'Gate', 'eval_network', 'nand_gate_count',
        'implicants_to_aig',
        # AIG rewriting + exact synthesis
        'rewrite_aig', 'enumerate_cuts', 'evaluate_cut_tt',
        'fraig', 'fraig_stats',
        'dc_optimize', 'dc_stats',
        'balance_aig', 'aig_depth',
        'STAResult', 'TimingEntry', 'sta_aig', 'sta_nand', 'compute_sta',
        'SwitchingActivity', 'estimate_switching',
        'exact_synthesize', 'evaluate_template',
        'exact_cache_stats', 'exact_cache_clear',
        # export
        'export_circ', 'export_fsm_circ', 'aig_to_dot',
        # AIGER / BLIF interchange
        'write_aiger', 'read_aiger', 'write_blif', 'read_blif',
        # FSM synthesis (Phase 3)
        'StateTable', 'Transition', 'FSMResult',
        'minimize_states', 'encode_states', 'fsm_to_truth_table',
        'synthesize_fsm', 'simulate_fsm', 'parse_kiss',
        # verification
        'miter_verify', 'bmc_verify',
        # ATPG
        'run_atpg', 'AtpgResult', 'FaultResult',
        # test suites
        'run_tests',
        # benchmarks
        'run_benchmarks', 'run_one_benchmark', 'BENCHMARKS',
        'run_epfl', 'run_one_epfl', 'check_epfl_updates', 'aig_equivalence',
        # property-based testing
        'run_property_tests', 'check_equivalence',
        # profiling
        'ProfileReport', 'profile_pass',
        # structural module (Phase 3.5)
        'StructuralModule',
        # synthesis script + bandit
        'ScriptBandit', 'run_bandit', 'DEFAULT_ARMS', 'DEFAULT_SCRIPT',
        'parse_script', 'run_script',
        # Verilog front-end
        'read_verilog', 'parse_verilog', 'verilog_to_module', 'VerilogError',
    ]
