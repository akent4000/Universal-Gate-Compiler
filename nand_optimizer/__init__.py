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

from .truth_table  import TruthTable
from .pipeline     import optimize, OutputResult
from .expr         import Expr, Const, Lit, Not, And, Or, ONE, ZERO, simp
from .implicant    import Implicant, quine_mccluskey, espresso
from .optimize     import phase_assign, factorize, apply_shannon, elim_inv
from .nand         import NANDBuilder, Gate, eval_network, nand_gate_count
from .circ_export  import export_circ

__all__ = [
    'TruthTable', 'optimize', 'OutputResult',
    'Expr', 'Const', 'Lit', 'Not', 'And', 'Or', 'ONE', 'ZERO', 'simp',
    'Implicant', 'quine_mccluskey', 'espresso',
    'phase_assign', 'factorize', 'apply_shannon', 'elim_inv',
    'NANDBuilder', 'Gate', 'eval_network', 'nand_gate_count',
    'export_circ',
]