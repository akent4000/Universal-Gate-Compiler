"""
Universal test suite for the NAND optimizer.

Works with any TruthTable — tests are parameterised over the
truth table's inputs, outputs, and expected values.
"""

from __future__ import annotations
from typing import Dict, Optional

from .truth_table import TruthTable
from .expr        import Lit, Not, Expr
from .implicant   import Implicant, espresso, implicants_to_expr, int_to_bits
from .optimize    import phase_assign, factorize, apply_shannon, elim_inv
from .nand        import NANDBuilder, eval_network, nand_gate_count
from .pipeline    import OptimizeResult, OutputResult, _optimize_output


# ═══════════════════════════════════════════════════════════════════════════════
#  Test runner
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunner:
    """Accumulates pass/fail counts and pretty-prints results."""

    def __init__(self):
        self.passed = 0
        self.failed = 0

    def ok(self, name: str, cond: bool, detail: str = ''):
        sym = '✓' if cond else '✗'
        msg = f'  [{sym}] {name}'
        if detail:
            msg += f'  →  {detail}'
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Generic tests
# ═══════════════════════════════════════════════════════════════════════════════

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

    print('═' * 68)
    print('UNIT TESTS')
    print('═' * 68)

    # ── T1: QMC minimisation correctness ──────────────────────────────────
    print(f'\n  ── T1: QMC minimisation {"─" * 40}')
    for idx, name in enumerate(tt.output_names):
        ones = tt.ones(idx)
        dc   = tt.dont_cares
        imps = espresso(ones, dc, n_vars)
        expr = implicants_to_expr(imps, var_names)
        errs = []
        for m in sorted(defined):
            b   = int_to_bits(m, n_vars)
            asg = {var_names[j]: b[j] for j in range(n_vars)}
            exp = tt.rows[m][idx]
            got = expr.eval(asg)
            if got != exp:
                errs.append(f'm{m}:exp{exp}≠got{got}')
        t.ok(f'QMC  {name}',
             not errs, '; '.join(errs) if errs else f'{len(imps)} implicants')

    # ── T2: Phase assignment preserves truth values ───────────────────────
    print(f'\n  ── T2: Phase assignment {"─" * 39}')
    for idx, name in enumerate(tt.output_names):
        ones = tt.ones(idx)
        dc   = tt.dont_cares
        imps, ic = phase_assign(ones, dc, n_vars)
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

    # ── T3: Factorization is truth-preserving ─────────────────────────────
    print(f'\n  ── T3: Algebraic factorization {"─" * 32}')
    for idx, name in enumerate(tt.output_names):
        ones = tt.ones(idx)
        dc   = tt.dont_cares
        imps, _ = phase_assign(ones, dc, n_vars)
        expr = implicants_to_expr(imps, var_names)
        fact = factorize(expr)
        errs = []
        for m in sorted(defined):
            b   = int_to_bits(m, n_vars)
            asg = {var_names[j]: b[j] for j in range(n_vars)}
            if expr.eval(asg) != fact.eval(asg):
                errs.append(f'm{m}')
        d = fact.literals() - expr.literals()
        t.ok(f'Factor {name} (Δ={d:+})', not errs,
             '; '.join(errs) if errs else 'equivalent')

    # ── T4: Shannon decomposition is truth-preserving ─────────────────────
    print(f'\n  ── T4: Shannon decomposition {"─" * 33}')
    for idx, name in enumerate(tt.output_names):
        ones = tt.ones(idx)
        dc   = tt.dont_cares
        imps, _ = phase_assign(ones, dc, n_vars)
        expr = implicants_to_expr(imps, var_names)
        shan = apply_shannon(expr, var_names)
        errs = []
        for m in sorted(defined):
            b   = int_to_bits(m, n_vars)
            asg = {var_names[j]: b[j] for j in range(n_vars)}
            if expr.eval(asg) != shan.eval(asg):
                errs.append(f'm{m}')
        d = shan.literals() - expr.literals()
        t.ok(f'Shannon {name} (Δ={d:+})', not errs,
             '; '.join(errs) if errs else 'equivalent')

    # ── T5: Double-inversion elimination ──────────────────────────────────
    print(f'\n  ── T5: Redundant inversion elimination {"─" * 24}')
    cases = [
        ('~~a',    Not(Not(Lit('a'))),                'a'),
        ('~~~a',   Not(Not(Not(Lit('a')))),            '~a'),
        ('~~~~a',  Not(Not(Not(Not(Lit('a'))))),       'a'),
    ]
    for label, expr, expected in cases:
        cleaned = str(elim_inv(expr))
        t.ok(f'elim_inv({label})', cleaned == expected,
             f'got "{cleaned}", want "{expected}"')

    # ── T6: Implicant coverage ────────────────────────────────────────────
    print(f'\n  ── T6: Implicant coverage {"─" * 36}')
    for idx, name in enumerate(tt.output_names):
        ones = tt.ones(idx)
        dc   = tt.dont_cares
        imps, ic = phase_assign(ones, dc, n_vars)
        target = ones if not ic else (set(range(1 << n_vars)) - ones - dc)
        uncovered = [m for m in sorted(target)
                     if not any(m in imp.covered for imp in imps)]
        t.ok(f'Coverage {name}', not uncovered,
             f'uncovered: {uncovered}' if uncovered else f'{len(imps)} PIs')

    # ── T7: NAND network simulation ──────────────────────────────────────
    print(f'\n  ── T7: NAND network simulation {"─" * 31}')
    for idx, name in enumerate(tt.output_names):
        r    = result[name]
        errs = []
        for m in sorted(defined):
            bits = int_to_bits(m, n_vars)
            inp  = {var_names[j]: bits[j] for j in range(n_vars)}
            got  = eval_network(r.gates, inp)
            exp  = tt.rows[m][idx]
            if got != exp:
                errs.append(f'm{m}:exp{exp}≠got{got}')
        t.ok(f'NAND sim {name} ({r.n_nand} gates)',
             not errs, '; '.join(errs) if errs else 'correct')

    # ── T8: Don't-care robustness ─────────────────────────────────────────
    if tt.dont_cares:
        print(f'\n  ── T8: Don\'t-care robustness {"─" * 33}')
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

    # ── T9: Full truth-table cross-check ──────────────────────────────────
    print(f'\n  ── T9: Full truth-table cross-check {"─" * 26}')
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
            print(f'     Input {m}: {", ".join(active) if active else "(none)"} ON  ✓')
    t.ok('Full truth-table', all_ok)

    # ── T10: Greedy reassociation saves gates ─────────────────────────────
    print(f'\n  ── T10: Greedy reassociation {"─" * 33}')

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

    # ── Summary ───────────────────────────────────────────────────────────
    print('\n' + '═' * 68)
    print(f'  Results: {t.passed}/{t.total} passed', end='')
    if t.failed:
        print(f'  ({t.failed} FAILED)')
    else:
        print('  — ALL TESTS PASSED ✓')
    print('═' * 68)
    return t.all_passed