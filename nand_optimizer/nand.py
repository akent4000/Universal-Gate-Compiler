"""
NAND gate network builder backed by an And-Inverter Graph (AIG).

The AIG is the primary internal representation:
  • Structural hashing is done over integer literals (node_id*2+complement),
    which is faster and more canonical than the previous string/tuple keys.
  • Inversions are free (just flip the complement bit), so NOT-NOT collapses
    automatically without a separate inv_map scan.
  • Constant propagation is built into AIG.make_and.

Public interface (NANDBuilder) is unchanged so all consumers — pipeline,
tests, circ_export — continue to work without modification.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple

from .expr import Expr, Const, Lit, Not, And, Or
from .aig  import AIG, Lit as AIGLit, FALSE, TRUE


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

    Internally uses an AIG for all structural hashing.  Wire names are
    assigned on demand and kept in a bidirectional map alongside the AIG
    literals so that:
      • cache and inv_map (kept for backward-compat / subclass access) stay
        consistent with the AIG at all times.
      • Double inversions collapse automatically: NAND(NAND(x,x), NAND(x,x))
        → the AIG resolves NOT(NOT(x)) = x and returns the original wire.

    Greedy reassociation for multi-input NAND chains uses AIG's has_and()
    to detect already-computed pairs without string key lookups.
    """

    def __init__(self):
        self._aig:    AIG            = AIG()
        self._wl:     Dict[str, AIGLit] = {}   # wire name  → AIG literal
        self._lw:     Dict[AIGLit, str] = {}   # AIG literal → wire name

        # ── backward-compatible public attributes ────────────────────────
        self.gates:   List[Gate]     = []
        self.cache:   Dict           = {}   # ('NAND', (a,b)) → wire
        self.inv_map: Dict[str, str] = {}   # wire ↔ its inverse
        self.w_cnt:   int            = 0

    # ── internal helpers ─────────────────────────────────────────────────────

    def _reg(self, wire: str, lit: AIGLit) -> None:
        """Register a bidirectional wire ↔ literal mapping."""
        self._wl[wire] = lit
        self._lw[lit]  = wire

    def _get_lit(self, wire: str) -> AIGLit:
        """
        Return the AIG literal for a wire.
        Unknown wires are registered as primary inputs (first use).
        """
        if wire not in self._wl:
            lit = self._aig.make_input(wire)
            self._reg(wire, lit)
        return self._wl[wire]

    def _ensure_const(self, lit: AIGLit) -> str:
        """Emit a ZERO/ONE pseudo-gate for a constant literal if not yet done."""
        if lit in self._lw:
            return self._lw[lit]
        wire      = 'c1' if lit == TRUE else 'c0'
        gate_type = 'ONE' if lit == TRUE else 'ZERO'
        self.gates.append((wire, gate_type, []))
        self._reg(wire, lit)
        return wire

    def _new_wire(self) -> str:
        self.w_cnt += 1
        return f'w{self.w_cnt}'

    # ── core primitive ────────────────────────────────────────────────────────

    def nand(self, *ins: str) -> str:
        """
        Create (or reuse) a NAND gate.

        • 1 input  → inverter  (NAND with both pins tied)
        • 2 inputs → standard 2-input NAND, backed by AIG lookup
        • >2 inputs → greedy AND-chain decomposition
        """
        # ── multi-input: greedy reassociation ─────────────────────────────
        if len(ins) > 2:
            remaining = list(ins)

            # Find a starting pair that is already in the AIG structural hash
            best_i, best_j = 0, 1
            found = False
            for i in range(len(remaining)):
                for j in range(i + 1, len(remaining)):
                    lit_i = self._get_lit(remaining[i])
                    lit_j = self._get_lit(remaining[j])
                    if self._aig.has_and(lit_i, lit_j):
                        best_i, best_j = i, j
                        found = True
                        break
                if found:
                    break

            first, second = remaining[best_i], remaining[best_j]
            rest = [remaining[k] for k in range(len(remaining))
                    if k != best_i and k != best_j]

            # AND of starting pair: NOT(NAND(a, b))
            acc = self.nand(self.nand(first, second))

            # Greedy fold of remaining (except last)
            while len(rest) > 1:
                lit_acc = self._get_lit(acc)
                chosen_idx = 0
                for k, x in enumerate(rest):
                    lit_x = self._get_lit(x)
                    if self._aig.has_and(lit_acc, lit_x):
                        chosen_idx = k
                        break
                chosen = rest.pop(chosen_idx)
                acc = self.nand(self.nand(acc, chosen))

            return self.nand(acc, rest[0])

        # ── normalise to 2-input ──────────────────────────────────────────
        ins = tuple(sorted(ins))
        if len(ins) == 1:
            ins = (ins[0], ins[0])

        # ── AIG lookup (primary hash) ─────────────────────────────────────
        lit_a = self._get_lit(ins[0])
        lit_b = self._get_lit(ins[1])

        # NAND(a, b) = NOT(AND(a, b))
        # make_nand handles constant propagation and structural hashing
        nand_lit = self._aig.make_nand(lit_a, lit_b)

        # Constant propagation produced TRUE/FALSE
        if nand_lit == TRUE or nand_lit == FALSE:
            return self._ensure_const(nand_lit)

        # AIG cache hit: this literal already has a wire
        if nand_lit in self._lw:
            wire = self._lw[nand_lit]
            # Keep string cache in sync for subclass / backward-compat reads
            self.cache[('NAND', ins)] = wire
            return wire

        # ── cache miss: emit a new NAND gate ─────────────────────────────
        wire = self._new_wire()
        self.gates.append((wire, 'NAND', list(ins)))
        self._reg(wire, nand_lit)

        # String cache (backward compat)
        self.cache[('NAND', ins)] = wire

        # inv_map: record inverter relationship for subclass access
        if ins[0] == ins[1]:
            self.inv_map[wire]    = ins[0]
            self.inv_map[ins[0]] = wire

        return wire

    # ── Expr → wire ───────────────────────────────────────────────────────────

    def build_expr(self, expr: Expr) -> str:
        """Recursively compile an Expr tree to NAND gates. Returns output wire."""
        if isinstance(expr, Const):
            lit  = TRUE if expr.v else FALSE
            wire = f'c{expr.v}'
            if wire not in self._wl:
                self.gates.append((wire, 'ONE' if expr.v else 'ZERO', []))
                self._reg(wire, lit)
            return wire

        if isinstance(expr, Lit):
            # Positive literal: return the variable's wire name directly.
            # _get_lit will register it as a primary input on first nand() use.
            return self.nand(expr.name) if expr.neg else expr.name

        if isinstance(expr, Not):
            return self.nand(self.build_expr(expr.arg))

        if isinstance(expr, And):
            # AND(a, b, …) = NOT(NAND(a, b, …))
            return self.nand(self.nand(*[self.build_expr(a) for a in expr.args]))

        if isinstance(expr, Or):
            # OR(a, b, …) = NAND(NOT(a), NOT(b), …)
            return self.nand(*[self.nand(self.build_expr(a)) for a in expr.args])

        return ''


# ═══════════════════════════════════════════════════════════════════════════════
#  Pure AIG bridging
# ═══════════════════════════════════════════════════════════════════════════════

def expr_to_aig(expr: Expr, aig: AIG) -> AIGLit:
    """Recursively compile an Expr tree strictly into AIG literals."""
    if isinstance(expr, Const):
        return TRUE if expr.v else FALSE

    if isinstance(expr, Lit):
        lit = aig.make_input(expr.name)
        return aig.make_not(lit) if expr.neg else lit

    if isinstance(expr, Not):
        return aig.make_not(expr_to_aig(expr.arg, aig))

    if isinstance(expr, And):
        # AND(a, b, …)
        acc = expr_to_aig(expr.args[0], aig)
        for arg in expr.args[1:]:
            acc = aig.make_and(acc, expr_to_aig(arg, aig))
        return acc

    if isinstance(expr, Or):
        # OR(a, b, …) = NAND(NOT(a), NOT(b), …) = ~AND(~a, ~b)
        acc = aig.make_not(expr_to_aig(expr.args[0], aig))
        for arg in expr.args[1:]:
            acc = aig.make_and(acc, aig.make_not(expr_to_aig(arg, aig)))
        return aig.make_not(acc)

    return FALSE


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
#  AIG to NAND Conversion
# ═══════════════════════════════════════════════════════════════════════════════

def aig_to_gates(aig: AIG, output_lits: List[AIGLit]) -> Tuple[List[Gate], List[str]]:
    """
    Translates an optimized AIG into a minimal NAND gate list.
    Every AIG node `v = AND(a, b)` natively produces `~v = NAND(a, b)`.
    We generate `v = NOT(~v)` only if the positive literal is actually used.
    """
    # Quick connectivity analysis to see which polarities are actually needed.
    # We trace backward from output_lits.
    needed_lits: set[AIGLit] = set(output_lits)
    
    # We trace backwards from ends of AIG
    for i in range(len(aig._nodes) - 1, -1, -1):
        node_id = i + 1
        pos_lit = node_id * 2
        neg_lit = node_id * 2 + 1
        
        # If either polarity is needed, we will compute this node, which requires its inputs
        if pos_lit in needed_lits or neg_lit in needed_lits:
            entry = aig._nodes[i]
            if entry[0] == 'and':
                _, lit_a, lit_b = entry
                needed_lits.add(lit_a)
                needed_lits.add(lit_b)
                
    # Now generate gates
    w_cnt = 0
    def new_wire() -> str:
        nonlocal w_cnt; w_cnt += 1
        return f'w{w_cnt}'
        
    gates: List[Gate] = []
    lit_to_wire: Dict[AIGLit, str] = {}
    
    # Constants
    const_pos = 'c1' if TRUE in needed_lits or FALSE in needed_lits else None
    const_neg = 'c0' if TRUE in needed_lits or FALSE in needed_lits else None
    if const_pos:
        gates.append((const_pos, 'ONE', []))
        gates.append((const_neg, 'ZERO', []))
        lit_to_wire[TRUE] = const_pos
        lit_to_wire[FALSE] = const_neg
        
    # Forward pass
    for i, entry in enumerate(aig._nodes):
        node_id = i + 1
        pos_lit = node_id * 2
        neg_lit = node_id * 2 + 1
        
        if pos_lit not in needed_lits and neg_lit not in needed_lits:
            continue
            
        if entry[0] == 'input':
            wire = entry[1]
            lit_to_wire[pos_lit] = wire
            if neg_lit in needed_lits:
                w_neg = new_wire()
                gates.append((w_neg, 'NAND', [wire, wire]))
                lit_to_wire[neg_lit] = w_neg
            continue
            
        # AND node
        _, a_lit, b_lit = entry
        wa = lit_to_wire[a_lit]
        wb = lit_to_wire[b_lit]
        
        # ~v = NAND(a, b)
        w_neg = new_wire()
        gates.append((w_neg, 'NAND', [wa, wb]))
        lit_to_wire[neg_lit] = w_neg
        
        # v = NOT(~v)
        if pos_lit in needed_lits:
            w_pos = new_wire()
            gates.append((w_pos, 'NAND', [w_neg, w_neg]))
            lit_to_wire[pos_lit] = w_pos
            
    out_wires = [lit_to_wire[l] for l in output_lits]
    return gates, out_wires

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

