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
from .balance           import balance_aig, aig_depth
from .exact_synthesis   import (exact_synthesize, evaluate_template,
                                exact_cache_stats, exact_cache_clear)
from .circ_export       import export_circ
from .dot_export        import aig_to_dot
from .verify            import miter_verify
from .tests             import run_tests
from .profile           import ProfileReport, profile_pass
from .benchmark_runner  import run_benchmarks, run_one_benchmark, BENCHMARKS
from .property_tests    import run_property_tests, check_equivalence

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
    'balance_aig', 'aig_depth',
    'exact_synthesize', 'evaluate_template',
    'exact_cache_stats', 'exact_cache_clear',
    # export
    'export_circ', 'aig_to_dot',
    # verification
    'miter_verify',
    # test suites
    'run_tests',
    # benchmarks
    'run_benchmarks', 'run_one_benchmark', 'BENCHMARKS',
    # property-based testing
    'run_property_tests', 'check_equivalence',
    # profiling
    'ProfileReport', 'profile_pass',
]