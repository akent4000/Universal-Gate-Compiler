"""
NAND gate network builder with:
  • global structural hashing (AIG cache)
  • peephole double-inversion elimination
  • greedy reassociation of AND-chains for maximum cache reuse
  • network simulation for verification
"""

from __future__ import annotations
from typing import Dict, List, Tuple

from .expr import Expr, Const, Lit, Not, And, Or


# ═══════════════════════════════════════════════════════════════════════════════
#  Types
# ═══════════════════════════════════════════════════════════════════════════════

Gate = Tuple[str, str, List[str]]   # (wire_name, gate_type, input_wires)


# ═══════════════════════════════════════════════════════════════════════════════
#  NANDBuilder
# ═══════════════════════════════════════════════════════════════════════════════

class NANDBuilder:
    """
    Incrementally builds a NAND-only gate network from Expr trees.

    Features:
      • Global gate cache — identical sub-expressions produce the same wire
      • Peephole NOT-NOT → identity collapse
      • Multi-input NAND decomposition with greedy reassociation:
        when folding AND(a, b, c, …) into a chain of 2-input NANDs,
        the builder picks the pair ordering that maximises cache hits
    """

    def __init__(self):
        self.gates:   List[Gate]        = []
        self.cache:   Dict[Tuple, str]  = {}
        self.inv_map: Dict[str, str]    = {}   # wire ↔ its inverse
        self.w_cnt = 0

    # ── core primitive ────────────────────────────────────────────────────────

    def nand(self, *ins: str) -> str:
        """
        Create (or reuse) a NAND gate.

        • 1 input  → inverter  (NAND with both pins tied)
        • 2 inputs → standard 2-input NAND
        • >2 inputs → greedy AND-chain decomposition into 2-input gates
        """
        # ── multi-input: greedy reassociation ─────────────────────────────
        if len(ins) > 2:
            remaining = list(ins)

            # pick starting pair already in cache (if any)
            best_i, best_j = 0, 1
            found = False
            for i in range(len(remaining)):
                for j in range(i + 1, len(remaining)):
                    pair = tuple(sorted([remaining[i], remaining[j]]))
                    if ('NAND', pair) in self.cache:
                        best_i, best_j = i, j
                        found = True
                        break
                if found:
                    break

            first, second = remaining[best_i], remaining[best_j]
            rest = [remaining[k] for k in range(len(remaining))
                    if k != best_i and k != best_j]

            # AND of starting pair: NOT(NAND(a,b))
            acc = self.nand(self.nand(first, second))

            # greedy fold of remaining (except last)
            while len(rest) > 1:
                chosen_idx = 0
                for k, x in enumerate(rest):
                    pair = tuple(sorted([acc, x]))
                    if ('NAND', pair) in self.cache:
                        chosen_idx = k
                        break
                chosen = rest.pop(chosen_idx)
                acc = self.nand(self.nand(acc, chosen))

            # final NAND with last element
            return self.nand(acc, rest[0])

        # ── normalise to 2-input ──────────────────────────────────────────
        ins = tuple(sorted(ins))
        if len(ins) == 1:
            ins = (ins[0], ins[0])

        # peephole: NOT(NOT(x)) → x
        if ins[0] == ins[1] and ins[0] in self.inv_map:
            return self.inv_map[ins[0]]

        # structural hashing
        key = ('NAND', ins)
        if key in self.cache:
            return self.cache[key]

        self.w_cnt += 1
        w = f'w{self.w_cnt}'
        self.gates.append((w, 'NAND', list(ins)))
        self.cache[key] = w

        # record inverter relationship for peephole
        if ins[0] == ins[1]:
            self.inv_map[w]      = ins[0]
            self.inv_map[ins[0]] = w

        return w

    # ── Expr → wire ───────────────────────────────────────────────────────────

    def build_expr(self, expr: Expr) -> str:
        """Recursively compile an Expr tree to NAND gates. Returns output wire."""
        if isinstance(expr, Const):
            w = f'c{expr.v}'
            if w not in self.cache:
                self.gates.append((w, 'ONE' if expr.v else 'ZERO', []))
                self.cache[w] = w
            return w

        if isinstance(expr, Lit):
            return self.nand(expr.name) if expr.neg else expr.name

        if isinstance(expr, Not):
            return self.nand(self.build_expr(expr.arg))

        if isinstance(expr, And):
            # AND(a,b) = NOT(NAND(a,b))
            return self.nand(self.nand(*[self.build_expr(a) for a in expr.args]))

        if isinstance(expr, Or):
            # OR(a,b) = NAND(NOT(a), NOT(b))
            return self.nand(*[self.nand(self.build_expr(a)) for a in expr.args])

        return ''


# ═══════════════════════════════════════════════════════════════════════════════
#  Dead Code Elimination
# ═══════════════════════════════════════════════════════════════════════════════

def dead_code_elimination(builder: NANDBuilder,
                          output_wires: List[str]) -> None:
    """
    Remove gates whose outputs are not reachable from *output_wires*.
    Modifies builder.gates **in place**.
    """
    used: set = set(output_wires)
    for name, _, ins in reversed(builder.gates):
        if name in used:
            used.update(ins)
    builder.gates = [g for g in builder.gates if g[0] in used]


# ═══════════════════════════════════════════════════════════════════════════════
#  Simulation
# ═══════════════════════════════════════════════════════════════════════════════

def eval_network(gates: List[Gate], inputs: Dict[str, int]) -> int:
    """
    Simulate a gate network in topological order.
    Returns the value on the last OUTPUT wire encountered.
    """
    wires: Dict[str, int] = dict(inputs)
    result = 0
    for name, gtype, ins in gates:
        if gtype == 'NAND':
            wires[name] = 1 - int(all(wires.get(i, 0) == 1 for i in ins))
        elif gtype == 'OUTPUT':
            result = wires.get(ins[0], 0)
            wires[name] = result
        elif gtype == 'ZERO':
            wires[name] = 0
        elif gtype == 'ONE':
            wires[name] = 1
    return result


def nand_gate_count(gates: List[Gate]) -> int:
    """Count only NAND gates (excluding OUTPUT / const pseudo-gates)."""
    return sum(1 for _, t, _ in gates if t == 'NAND')