"""
Universal test suite for the NAND optimizer.

Works with any TruthTable — tests are parameterised over the
truth table's inputs, outputs, and expected values.
"""

from __future__ import annotations
from typing import Dict, Optional

from .truth_table import TruthTable
from .expr        import Lit, Not, Expr
from .implicant   import (Implicant, espresso, implicants_to_expr, int_to_bits,
                          _expand_cubes_to_set)
from .optimize      import phase_assign, factorize, apply_shannon, elim_inv
from .decomposition  import ashenhurst_decompose
from .nand           import NANDBuilder, eval_network, nand_gate_count
from .pipeline       import OptimizeResult, OutputResult, _optimize_output


# -------------------------------------------------------------------------------
#  Test runner
# -------------------------------------------------------------------------------

class TestRunner:
    """Accumulates pass/fail counts and pretty-prints results."""

    def __init__(self):
        self.passed = 0
        self.failed = 0

    def ok(self, name: str, cond: bool, detail: str = ''):
        sym = 'OK' if cond else 'FAIL'
        msg = f'  [{sym}] {name}'
        if detail:
            msg += f'  ->  {detail}'
        print(msg)
        if cond:
            self.passed += 1
        else:
            self.failed += 1

    @property
    def total(self) -> int:
        return self.passed + self.failed

    @property
    def all_passed(self) -> bool:
        return self.failed == 0


# -------------------------------------------------------------------------------
#  Generic tests
# -------------------------------------------------------------------------------

def run_tests(tt: TruthTable, result: OptimizeResult,
              verbose: bool = True) -> bool:
    """
    Run the full test suite against a truth table and its optimisation result.
    Returns True if all tests pass.
    """
    t = TestRunner()
    var_names = tt.input_names
    n_vars    = tt.n_inputs
    defined   = set(tt.rows.keys())

    print('-' * 68)
    print('UNIT TESTS')
    print('-' * 68)

    # -- T1: QMC minimisation correctness ----------------------------------
    print(f'\n  -- T1: QMC minimisation {"-" * 40}')
    for idx, name in enumerate(tt.output_names):
        on_cubes = tt.ones_cubes(idx)
        dc_cubes = tt.dc_cubes
        imps = espresso(on_cubes, dc_cubes, n_vars)
        expr = implicants_to_expr(imps, var_names)
        errs = []
        for m in sorted(defined):
            b   = int_to_bits(m, n_vars)
            asg = {var_names[j]: b[j] for j in range(n_vars)}
            exp = tt.rows[m][idx]
            got = expr.eval(asg)
            if got != exp:
                errs.append(f'm{m}:exp{exp}!=got{got}')
        t.ok(f'QMC  {name}',
             not errs, '; '.join(errs) if errs else f'{len(imps)} implicants')

    # -- T2: Phase assignment preserves truth values -----------------------
    print(f'\n  -- T2: Phase assignment {"-" * 39}')
    for idx, name in enumerate(tt.output_names):
        on_cubes = tt.ones_cubes(idx)
        dc_cubes = tt.dc_cubes
        imps, ic = phase_assign(on_cubes, dc_cubes, n_vars)
        expr = implicants_to_expr(imps, var_names)
        errs = []
        for m in sorted(defined):
            b   = int_to_bits(m, n_vars)
            asg = {var_names[j]: b[j] for j in range(n_vars)}
            got = expr.eval(asg)
            got = 1 - got if ic else got
            exp = tt.rows[m][idx]
            if got != exp:
                errs.append(f'm{m}')
        form = '~f' if ic else ' f'
        t.ok(f'Phase({form}) {name}', not errs,
             '; '.join(errs) if errs else 'correct')

    # -- T3: Factorization is truth-preserving -----------------------------
    print(f'\n  -- T3: Algebraic factorization {"-" * 32}')
    for idx, name in enumerate(tt.output_names):
        on_cubes = tt.ones_cubes(idx)
        dc_cubes = tt.dc_cubes
        imps, _ = phase_assign(on_cubes, dc_cubes, n_vars)
        expr = implicants_to_expr(imps, var_names)
        fact = factorize(expr)
        errs = []
        for m in sorted(defined):
            b   = int_to_bits(m, n_vars)
            asg = {var_names[j]: b[j] for j in range(n_vars)}
            if expr.eval(asg) != fact.eval(asg):
                errs.append(f'm{m}')
        d = fact.literals() - expr.literals()
        t.ok(f'Factor {name} (delta={d:+})', not errs,
             '; '.join(errs) if errs else 'equivalent')

    # -- T4: Shannon decomposition is truth-preserving ---------------------
    print(f'\n  -- T4: Shannon decomposition {"-" * 33}')
    for idx, name in enumerate(tt.output_names):
        on_cubes = tt.ones_cubes(idx)
        dc_cubes = tt.dc_cubes
        imps, _ = phase_assign(on_cubes, dc_cubes, n_vars)
        expr = implicants_to_expr(imps, var_names)
        shan = apply_shannon(expr, var_names)
        errs = []
        for m in sorted(defined):
            b   = int_to_bits(m, n_vars)
            asg = {var_names[j]: b[j] for j in range(n_vars)}
            if expr.eval(asg) != shan.eval(asg):
                errs.append(f'm{m}')
        d = shan.literals() - expr.literals()
        t.ok(f'Shannon {name} (delta={d:+})', not errs,
             '; '.join(errs) if errs else 'equivalent')

    # -- T5: Double-inversion elimination ----------------------------------
    print(f'\n  -- T5: Redundant inversion elimination {"-" * 24}')
    cases = [
        ('~~a',    Not(Not(Lit('a'))),                'a'),
        ('~~~a',   Not(Not(Not(Lit('a')))),            '~a'),
        ('~~~~a',  Not(Not(Not(Not(Lit('a'))))),       'a'),
    ]
    for label, expr, expected in cases:
        cleaned = str(elim_inv(expr))
        t.ok(f'elim_inv({label})', cleaned == expected,
             f'got "{cleaned}", want "{expected}"')

    # -- T6: Implicant coverage --------------------------------------------
    print(f'\n  -- T6: Implicant coverage {"-" * 36}')
    for idx, name in enumerate(tt.output_names):
        on_cubes = tt.ones_cubes(idx)
        dc_cubes = tt.dc_cubes
        imps, ic = phase_assign(on_cubes, dc_cubes, n_vars)
        # Expand cubes to minterm sets for the coverage check (small n only).
        on_set = _expand_cubes_to_set(on_cubes, n_vars)
        dc_set = _expand_cubes_to_set(dc_cubes, n_vars)
        target = on_set if not ic else (set(range(1 << n_vars)) - on_set - dc_set)
        uncovered = [m for m in sorted(target)
                     if not any(imp.covers_minterm(m) for imp in imps)]
        t.ok(f'Coverage {name}', not uncovered,
             f'uncovered: {uncovered}' if uncovered else f'{len(imps)} PIs')

    # -- T7: NAND network simulation --------------------------------------
    print(f'\n  -- T7: NAND network simulation {"-" * 31}')
    for idx, name in enumerate(tt.output_names):
        r    = result[name]
        errs = []
        for m in sorted(defined):
            bits = int_to_bits(m, n_vars)
            inp  = {var_names[j]: bits[j] for j in range(n_vars)}
            got  = eval_network(r.gates, inp)
            exp  = tt.rows[m][idx]
            if got != exp:
                errs.append(f'm{m}:exp{exp}!=got{got}')
        t.ok(f'NAND sim {name} ({r.n_nand} gates)',
             not errs, '; '.join(errs) if errs else 'correct')

    # -- T8: Don't-care robustness -----------------------------------------
    if tt.dont_cares:
        print(f'\n  -- T8: Don\'t-care robustness {"-" * 33}')
        crash = False
        for idx, name in enumerate(tt.output_names):
            r = result[name]
            try:
                for m in sorted(tt.dont_cares):
                    bits = int_to_bits(m, n_vars)
                    inp  = {var_names[j]: bits[j] for j in range(n_vars)}
                    eval_network(r.gates, inp)
            except Exception as e:
                crash = True
                print(f'     CRASH {name}: {e}')
        t.ok('Don\'t-care eval (no crash)', not crash)

    # -- T9: Full truth-table cross-check ----------------------------------
    print(f'\n  -- T9: Full truth-table cross-check {"-" * 26}')
    all_ok = True
    for m in sorted(defined):
        bits  = int_to_bits(m, n_vars)
        inp   = {var_names[j]: bits[j] for j in range(n_vars)}
        row_ok = True
        for idx, name in enumerate(tt.output_names):
            r   = result[name]
            got = eval_network(r.gates, inp)
            exp = tt.rows[m][idx]
            if got != exp:
                row_ok  = False
                all_ok = False
                print(f'     Input {m} output {name}: expected {exp}, got {got}')
        if row_ok and verbose:
            active = [n for i, n in enumerate(tt.output_names)
                      if tt.rows[m][i] == 1]
            print(f'     Input {m}: {", ".join(active) if active else "(none)"} ON  OK')
    t.ok('Full truth-table', all_ok)

    # -- T10: Greedy reassociation saves gates -----------------------------
    print(f'\n  -- T10: Greedy reassociation {"-" * 33}')

    class _LeftFoldBuilder(NANDBuilder):
        def nand(self, *ins):
            if len(ins) > 2:
                acc = ins[0]
                for i in range(1, len(ins) - 1):
                    step = self.nand(acc, ins[i])
                    acc  = self.nand(step)
                return self.nand(acc, ins[-1])
            ins = tuple(sorted(ins))
            if len(ins) == 1: ins = (ins[0], ins[0])
            if ins[0] == ins[1] and ins[0] in self.inv_map:
                return self.inv_map[ins[0]]
            key = ('NAND', ins)
            if key in self.cache:
                return self.cache[key]
            self.w_cnt += 1
            w = f'w{self.w_cnt}'
            self.gates.append((w, 'NAND', list(ins)))
            self.cache[key] = w
            if ins[0] == ins[1]:
                self.inv_map[w] = ins[0]
                self.inv_map[ins[0]] = w
            return w

    def _count_with(cls):
        b  = cls()
        rs = {}
        for idx, name in enumerate(tt.output_names):
            rs[name] = _optimize_output(tt, idx, b, verbose=False)
        from .nand import dead_code_elimination
        wires = [r.out_wire for r in rs.values()]
        dead_code_elimination(b, wires)
        for r in rs.values():
            out = r.gates[-1]
            r.gates = list(b.gates) + [out]
        return nand_gate_count(b.gates), rs

    cnt_lf, _     = _count_with(_LeftFoldBuilder)
    cnt_gr, rs_gr = _count_with(NANDBuilder)
    saved = cnt_lf - cnt_gr

    gr_errs = 0
    for m in sorted(defined):
        bits = int_to_bits(m, n_vars)
        inp  = {var_names[j]: bits[j] for j in range(n_vars)}
        for idx, name in enumerate(tt.output_names):
            if eval_network(rs_gr[name].gates, inp) != tt.rows[m][idx]:
                gr_errs += 1

    t.ok('Greedy reassociation correctness', gr_errs == 0,
         f'{gr_errs} errors' if gr_errs else 'correct')
    t.ok(f'Greedy reassociation savings', saved >= 0,
         f'left-fold={cnt_lf}, greedy={cnt_gr}, saved={saved}')

    # -- T11: Ashenhurst-Curtis / Roth-Karp decomposition -------------------
    # Only attempted for moderate fan-in (exhaustive chart build is 2^n).
    if 3 <= n_vars <= 10:
        print(f'\n  -- T11: Functional decomposition {"-" * 29}')
        for idx, name in enumerate(tt.output_names):
            ones = tt.ones(idx)
            dc   = tt.dont_cares
            dec  = ashenhurst_decompose(ones, dc, n_vars, var_names)
            if dec is None:
                t.ok(f'Decompose {name}', True, 'no profitable decomposition')
                continue

            errs = 0
            for m in range(1 << n_vars):
                asg = {var_names[j]: int_to_bits(m, n_vars)[j]
                       for j in range(n_vars)}
                for hn, he in zip(dec.h_names, dec.h_exprs):
                    asg[hn] = he.eval(asg)
                got = dec.g_expr.eval(asg)
                exp = tt.expected(m, idx)
                if exp is None:
                    continue
                if got != exp:
                    errs += 1

            t.ok(
                f'Decompose {name}  '
                f'(|Xb|={len(dec.bound_vars)}, mu={dec.mu}, k={dec.k}, '
                f'{dec.orig_lits}->{dec.new_lits} lits)',
                errs == 0,
                f'{errs} mismatches' if errs else 'equivalent',
            )

    # -- T12: Exact synthesis template correctness --------------------------
    # Pick a handful of tricky truth tables and verify that
    # exact_synthesize produces a template whose evaluation matches.
    print(f'\n  -- T12: Exact synthesis correctness {"-" * 26}')
    try:
        from .exact_synthesis import exact_synthesize, evaluate_template
    except ImportError:
        exact_synthesize = None

    if exact_synthesize is not None:
        cases = [
            ('XOR n=2',      0b0110,     2),
            ('MAJ n=3',      0b11101000, 3),
            ('~x2 & ~x3 n=4', 0x000F,    4),
            ('const 0 n=3',  0,          3),
            ('x1 n=3',       0xCC,       3),
        ]
        for label, target, n in cases:
            tmpl = exact_synthesize(target, n, timeout_ms=5000)
            if tmpl is None:
                t.ok(f'Exact {label}', False, 'no result (timeout or no z3?)')
                continue
            got = evaluate_template(tmpl, n)
            t.ok(f'Exact {label}', got == target,
                 f'ops={len(tmpl[1])}, out_lit={tmpl[0]}'
                 if got == target else
                 f'mismatch: 0x{got:x} vs 0x{target:x}')

    # -- T13: Fanout-aware rewrite preserves logic --------------------------
    print(f'\n  -- T13: Rewrite equivalence {"-" * 34}')
    from .rewrite import rewrite_aig
    from .aig import AIG
    from .nand import aig_to_gates
    src_aig = result.builder._aig
    src_out = [result.outputs[nm].out_lit for nm in tt.output_names]
    new_aig, new_out = rewrite_aig(src_aig, out_lits=src_out, rounds=1)
    gates, wires, _ = aig_to_gates(new_aig, new_out)
    rewrite_errs = 0
    for m in sorted(defined):
        bits = int_to_bits(m, n_vars)
        inp  = {var_names[j]: bits[j] for j in range(n_vars)}
        sim_inputs = dict(inp)
        sim_inputs.setdefault('c0', 0)
        sim_inputs.setdefault('c1', 1)
        for j, outname in enumerate(tt.output_names):
            # Construct per-output gate list terminated by OUTPUT.
            sub_gates = list(gates) + [(outname, 'OUTPUT', [wires[j]])]
            got = eval_network(sub_gates, sim_inputs)
            exp = tt.rows[m][j]
            if got != exp:
                rewrite_errs += 1
    t.ok(f'Rewrite output equivalence '
         f'(AIG {src_aig.n_nodes} -> {new_aig.n_nodes} nodes)',
         rewrite_errs == 0,
         f'{rewrite_errs} mismatches' if rewrite_errs else 'all minterms match')

    # -- Summary -----------------------------------------------------------
    print('\n' + '-' * 68)
    print(f'  Results: {t.passed}/{t.total} passed', end='')
    if t.failed:
        print(f'  ({t.failed} FAILED)')
    else:
        print('  — ALL TESTS PASSED OK')
    print('-' * 68)
    return t.all_passed