"""
Structural AIG constructor — builds logic directly into an AIG without
going through a TruthTable, enabling synthesis of functions with >20 inputs.

Public API:

    StructuralModule(name, inputs)         create a module with named inputs
    m.input(name)                          AIG literal for a primary input
    m.const0() / m.const1()               constant literals (FALSE / TRUE)
    m.not1(a)                              complement edge (no new node)
    m.and2(a, b)                           2-input AND
    m.nand2(a, b)                          2-input NAND
    m.or2(a, b)                            OR (De Morgan)
    m.nor2(a, b)                           NOR
    m.xor2(a, b)                           XOR
    m.xnor2(a, b)                          XNOR
    m.mux2(sel, a, b)                      MUX: sel=1 → a,  sel=0 → b
    m.and_tree(lits)                       multi-input AND fold
    m.or_tree(lits)                        multi-input OR fold
    m.add_output(name, lit)                declare a named output
    m.finalize(script, verbose)            run synthesis; return OptimizeResult

All primitives exploit AIG structural hashing: calling and2(a, b) with the
same arguments twice returns the same literal without a new node.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Sequence, Tuple

from .aig import AIG, Lit, FALSE, TRUE


# ── Minimal truth-table stub for circ_export compatibility ───────────────────

class _MockTT:
    """
    Provides the handful of attributes that export_circ and _DecoderBuilder
    read from a TruthTable (input_names, output_names, n_inputs, n_outputs).
    The structural path never has a concrete truth table.
    """
    def __init__(self, input_names: List[str], output_names: List[str]):
        self.input_names  = list(input_names)
        self.output_names = list(output_names)
        self.n_inputs     = len(input_names)
        self.n_outputs    = len(output_names)


# ═══════════════════════════════════════════════════════════════════════════════
#  StructuralModule
# ═══════════════════════════════════════════════════════════════════════════════

class StructuralModule:
    """
    Incrementally builds an AIG from RTL-style primitives.

    All methods return AIG literals (int).  Structural hashing is transparent:
    calling the same primitive with the same arguments always returns the same
    literal without allocating a new AIG node.

    After all outputs are declared with add_output(), call finalize() to run
    the synthesis script and emit the final NAND gate list.
    """

    def __init__(self, name: str, inputs: Sequence[str]):
        self._name     = name
        self._aig      = AIG()
        self._in_lits: Dict[str, Lit] = {}
        self._out_lits: List[Tuple[str, Lit]] = []   # ordered (out_name, lit)

        for inp in inputs:
            lit = self._aig.make_input(inp)
            self._in_lits[inp] = lit

    # ── primary input access ─────────────────────────────────────────────────

    def input(self, name: str) -> Lit:
        """Return the positive literal for *name*; create as primary input if new."""
        if name not in self._in_lits:
            self._in_lits[name] = self._aig.make_input(name)
        return self._in_lits[name]

    # ── constant literals ────────────────────────────────────────────────────

    def const0(self) -> Lit:
        return FALSE

    def const1(self) -> Lit:
        return TRUE

    # ── single-bit primitives ─────────────────────────────────────────────────

    def not1(self, a: Lit) -> Lit:
        """Complement a literal — O(1), no new node."""
        return self._aig.make_not(a)

    def and2(self, a: Lit, b: Lit) -> Lit:
        return self._aig.make_and(a, b)

    def nand2(self, a: Lit, b: Lit) -> Lit:
        return self._aig.make_nand(a, b)

    def or2(self, a: Lit, b: Lit) -> Lit:
        return self._aig.make_or(a, b)

    def nor2(self, a: Lit, b: Lit) -> Lit:
        return self._aig.make_not(self._aig.make_or(a, b))

    def xor2(self, a: Lit, b: Lit) -> Lit:
        return self._aig.make_xor(a, b)

    def xnor2(self, a: Lit, b: Lit) -> Lit:
        return self._aig.make_not(self._aig.make_xor(a, b))

    def mux2(self, sel: Lit, a: Lit, b: Lit) -> Lit:
        """2-to-1 multiplexer: sel=1 selects *a*, sel=0 selects *b*."""
        return self._aig.make_or(
            self._aig.make_and(sel, a),
            self._aig.make_and(self._aig.make_not(sel), b),
        )

    # ── multi-input reductions ────────────────────────────────────────────────

    def and_tree(self, lits: Sequence[Lit]) -> Lit:
        """Fold *lits* through AND.  Empty → TRUE."""
        if not lits:
            return TRUE
        acc = lits[0]
        for lit in lits[1:]:
            acc = self._aig.make_and(acc, lit)
        return acc

    def or_tree(self, lits: Sequence[Lit]) -> Lit:
        """Fold *lits* through OR.  Empty → FALSE."""
        if not lits:
            return FALSE
        acc = lits[0]
        for lit in lits[1:]:
            acc = self._aig.make_or(acc, lit)
        return acc

    # ── output declaration ────────────────────────────────────────────────────

    def add_output(self, name: str, lit: Lit) -> None:
        """Declare *name* as a primary output driven by *lit*."""
        self._out_lits.append((name, lit))

    # ── synthesis ─────────────────────────────────────────────────────────────

    def finalize(
        self,
        script:  Optional[str] = None,
        verbose: bool           = False,
    ):
        """
        Run the synthesis pipeline on the accumulated AIG.

        *script* is an ABC-style semicolon-separated command string
        (e.g. ``'rewrite; fraig; balance'``).  Defaults to that exact
        string when omitted.

        Returns an ``OptimizeResult``-compatible object whose ``.builder``,
        ``.outputs``, ``.aig``, ``.out_lits``, and ``.total_nand`` fields
        are populated.  The ``.truth_table`` field contains a lightweight
        stub sufficient for ``export_circ``.
        """
        from .pipeline  import OptimizeResult, OutputResult
        from .nand      import aig_to_gates, nand_gate_count, NANDBuilder
        from .script    import run_script

        if not self._out_lits:
            raise ValueError(
                "No outputs declared; call add_output() before finalize()")

        out_names = [n for n, _ in self._out_lits]
        out_lits  = [l for _, l in self._out_lits]
        aig       = self._aig

        effective_script = script if script is not None else 'rewrite; fraig; balance'

        if verbose:
            print(f'\n  [Structural] {self._name}: '
                  f'{aig.n_inputs} inputs, {aig.n_ands} AND nodes initially')
            print(f'  [Structural] Script: {effective_script!r}')

        new_aig, new_out_lits = run_script(
            aig, out_lits, effective_script, verbose)

        if verbose:
            print(f'  [Structural] After script: {new_aig.n_ands} AND nodes')

        # NAND gate mapping
        final_gates, out_wires, _n_xor = aig_to_gates(new_aig, new_out_lits)

        builder        = NANDBuilder()
        builder._aig   = new_aig
        builder.gates  = final_gates

        for name, wire in zip(out_names, out_wires):
            builder.gates.append((name, 'OUTPUT', [wire]))

        total_nand = nand_gate_count(builder.gates)

        # Build OptimizeResult
        result            = OptimizeResult()
        result.aig        = new_aig
        result.out_lits   = new_out_lits
        result.builder    = builder
        result.total_nand = total_nand
        result.truth_table = _MockTT(list(self._in_lits.keys()), out_names)

        for name, wire in zip(out_names, out_wires):
            r          = OutputResult()
            r.name     = name
            r.out_wire = wire
            r.gates    = list(builder.gates)
            result.outputs[name] = r

        if verbose:
            print(f'  [Structural] Total NAND gates: {total_nand}')

        return result
