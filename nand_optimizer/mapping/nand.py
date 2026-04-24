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

from ..core.expr import Expr, Const, Lit, Not, And, Or, Xor
from ..core.aig  import AIG, Lit as AIGLit, FALSE, TRUE


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

        if isinstance(expr, Xor):
            # XOR(a, b) — 4-NAND canonical form
            wa = self.build_expr(expr.a)
            wb = self.build_expr(expr.b)
            wg1 = self.nand(wa, wb)
            wg2 = self.nand(wa, wg1)
            wg3 = self.nand(wb, wg1)
            return self.nand(wg2, wg3)

        return ''


# ═══════════════════════════════════════════════════════════════════════════════
#  Pure AIG bridging
# ═══════════════════════════════════════════════════════════════════════════════

def expr_to_aig(expr: Expr, aig: AIG,
                var_map: Optional[Dict[str, AIGLit]] = None) -> AIGLit:
    """
    Recursively compile an Expr tree strictly into AIG literals.

    var_map lets callers override specific Lit names with pre-built AIG
    literals — used for Ashenhurst-Curtis decomposition, where auxiliary
    variables (h_i) in the g-expression must resolve to the already-built
    h_i sub-AIG rather than to fresh primary inputs.
    """
    if isinstance(expr, Const):
        return TRUE if expr.v else FALSE

    if isinstance(expr, Lit):
        if var_map is not None and expr.name in var_map:
            lit = var_map[expr.name]
        else:
            lit = aig.make_input(expr.name)
        return aig.make_not(lit) if expr.neg else lit

    if isinstance(expr, Not):
        return aig.make_not(expr_to_aig(expr.arg, aig, var_map))

    if isinstance(expr, And):
        # AND(a, b, …)
        acc = expr_to_aig(expr.args[0], aig, var_map)
        for arg in expr.args[1:]:
            acc = aig.make_and(acc, expr_to_aig(arg, aig, var_map))
        return acc

    if isinstance(expr, Or):
        # OR(a, b, …) = NAND(NOT(a), NOT(b), …) = ~AND(~a, ~b)
        acc = aig.make_not(expr_to_aig(expr.args[0], aig, var_map))
        for arg in expr.args[1:]:
            acc = aig.make_and(acc, aig.make_not(expr_to_aig(arg, aig, var_map)))
        return aig.make_not(acc)

    if isinstance(expr, Xor):
        a = expr_to_aig(expr.a, aig, var_map)
        b = expr_to_aig(expr.b, aig, var_map)
        return aig.make_xor(a, b)

    return FALSE


# ═══════════════════════════════════════════════════════════════════════════════
#  Direct AIG construction from SOP implicants
# ═══════════════════════════════════════════════════════════════════════════════

def _imp_to_aig(
    imp,
    var_names: List[str],
    aig: AIG,
    var_map: Optional[Dict[str, AIGLit]] = None,
) -> AIGLit:
    """Compile a single SOP product term to an AIG AND-chain."""
    from ..core.implicant import Implicant as _Imp
    result = TRUE
    for i, bit in enumerate(imp.bits):
        if bit == _Imp.DASH:
            continue
        name = var_names[i]
        lit = var_map[name] if (var_map and name in var_map) else aig.make_input(name)
        if bit == 0:
            lit = aig.make_not(lit)
        result = aig.make_and(result, lit)
    return result


def implicants_to_aig(
    imps: List,
    var_names: List[str],
    aig: AIG,
    var_map: Optional[Dict[str, AIGLit]] = None,
) -> AIGLit:
    """
    Build AIG directly from SOP implicants using Shannon recursive decomposition.

    Replaces the chain: implicants_to_expr → apply_shannon → elim_inv → expr_to_aig.
    Shannon cofactoring is implemented as AIG literal pointer manipulation with no
    Expr tree allocation: choose the most-occurring variable, partition implicants
    into cofactors, and combine with AIG make_and/make_or — all as integer ops.
    """
    from ..core.implicant import Implicant as _Imp

    if not imps:
        return FALSE

    if len(imps) == 1:
        return _imp_to_aig(imps[0], var_names, aig, var_map)

    # Find best Shannon split: variable appearing in the most implicants
    n_vars = len(var_names)
    var_count = [0] * n_vars
    for imp in imps:
        for i, bit in enumerate(imp.bits):
            if bit != _Imp.DASH:
                var_count[i] += 1

    best_idx = max(range(n_vars), key=lambda i: var_count[i])

    if var_count[best_idx] < 2:
        # No split variable covers 2+ implicants — flatten to OR-of-AND-terms
        result = FALSE
        for imp in imps:
            result = aig.make_or(result, _imp_to_aig(imp, var_names, aig, var_map))
        return result

    # Partition into cofactors, stripping the split variable and deduplicating
    var_name = var_names[best_idx]
    x_lit = var_map[var_name] if (var_map and var_name in var_map) else aig.make_input(var_name)

    pos_seen: Dict[tuple, object] = {}
    neg_seen: Dict[tuple, object] = {}
    for imp in imps:
        bit = imp.bits[best_idx]
        new_bits = list(imp.bits)
        new_bits[best_idx] = _Imp.DASH
        key = tuple(new_bits)
        stripped = _Imp(key, imp.covered)
        if bit == 1 or bit == _Imp.DASH:
            pos_seen[key] = stripped
        if bit == 0 or bit == _Imp.DASH:
            neg_seen[key] = stripped

    f1 = implicants_to_aig(list(pos_seen.values()), var_names, aig, var_map)
    f0 = implicants_to_aig(list(neg_seen.values()), var_names, aig, var_map)

    # MUX(x, f1, f0) — AIG constant propagation eliminates degenerate cases
    if f1 == f0:                return f1
    if f0 == FALSE:             return aig.make_and(x_lit, f1)
    if f1 == FALSE:             return aig.make_and(aig.make_not(x_lit), f0)
    if f1 == TRUE:              return aig.make_or(x_lit, f0)
    if f0 == TRUE:              return aig.make_or(aig.make_not(x_lit), f1)
    return aig.make_or(
        aig.make_and(x_lit, f1),
        aig.make_and(aig.make_not(x_lit), f0),
    )


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

def _detect_xor_patterns(
    aig: AIG,
    needed_lits: set,
    output_lits: List[AIGLit],
    overrides: Dict[int, Tuple[AIGLit, AIGLit]],
) -> Dict[int, Tuple[AIGLit, AIGLit]]:
    """
    Scan the AIG for XOR/XNOR subgraphs eligible for 4-NAND emission.

    Pattern (3 AND nodes):
        n3 = AND(~n1, ~n2)
        n1 = AND(p, q)
        n2 = AND(p^1, q^1)        ← complement-pair of n1's inputs

    The output ~n3 = OR(n1, n2) = XOR(p, q^1)  (since XOR(a,b) = a·~b + ~a·b
    maps to p=a, q=~b).  For a XNOR structure, ~n3 = XNOR(p, q).

    Returns {n3_id: (op1_lit, op2_lit)} where op1_lit=p, op2_lit=q^1.

    Only patterns where n1 and n2 are exclusively consumed by n3 are returned;
    this guarantees we can replace the 5-gate standard form with 4 NAND gates.
    """
    # Count how many times each literal appears as a fanin across all AND nodes
    # and output wires (respecting overrides).
    lit_fans: Dict[int, int] = {}
    for i, entry in enumerate(aig._nodes):
        if entry[0] != 'and':
            continue
        nid = i + 1
        if nid in overrides:
            a_lit, b_lit = overrides[nid]
        else:
            _, a_lit, b_lit = entry
        lit_fans[a_lit] = lit_fans.get(a_lit, 0) + 1
        lit_fans[b_lit] = lit_fans.get(b_lit, 0) + 1
    for lit in output_lits:
        lit_fans[lit] = lit_fans.get(lit, 0) + 1

    xor_map: Dict[int, Tuple[AIGLit, AIGLit]] = {}

    for i, entry in enumerate(aig._nodes):
        if entry[0] != 'and':
            continue
        n3_id = i + 1
        if n3_id in overrides:
            continue

        _, lit_p, lit_q = entry
        # Both fan-ins of n3 must be complemented (n3 = AND(~n1, ~n2))
        if not (lit_p & 1) or not (lit_q & 1):
            continue

        n1_id = lit_p >> 1
        n2_id = lit_q >> 1
        if n1_id == 0 or n2_id == 0:
            continue
        if n1_id in overrides or n2_id in overrides:
            continue

        e1 = aig._nodes[n1_id - 1]
        e2 = aig._nodes[n2_id - 1]
        if e1[0] != 'and' or e2[0] != 'and':
            continue

        _, p, q = e1    # n1 = AND(p, q)
        _, r, s = e2    # n2 = AND(r, s)

        # XOR criterion: n2's inputs are the bitwise complements of n1's
        if not (p ^ 1 == r and q ^ 1 == s):
            continue

        # Exclusivity: n1 and n2 must be consumed ONLY by n3
        n1_neg = n1_id * 2 + 1
        n2_neg = n2_id * 2 + 1
        if lit_fans.get(n1_neg, 0) != 1:
            continue
        if lit_fans.get(n2_neg, 0) != 1:
            continue
        if (n1_id * 2) in needed_lits:
            continue   # positive polarity of n1 required elsewhere
        if (n2_id * 2) in needed_lits:
            continue   # positive polarity of n2 required elsewhere

        # XOR operands: p (first input of n1) and q^1 (complement of second)
        xor_map[n3_id] = (p, q ^ 1)

    return xor_map


def _compute_needed_lits(aig: AIG, output_lits: List[AIGLit]) -> set:
    """Backward reachability: compute which AIG literals are actually needed."""
    needed: set = set(output_lits)
    for i in range(len(aig._nodes) - 1, -1, -1):
        node_id = i + 1
        if (node_id * 2) in needed or (node_id * 2 + 1) in needed:
            entry = aig._nodes[i]
            if entry[0] in ('and', 'xor'):
                needed.add(entry[1])
                needed.add(entry[2])
    return needed


def _bubble_push(
    aig: AIG,
    needed_lits: set,
    overrides: Dict[int, Tuple[AIGLit, AIGLit]],
    output_lits_set: set,
) -> bool:
    """
    One round of bubble pushing.

    For each AND node w whose positive polarity is needed, and which was
    created by the pattern  w = AND(pos_v, c)  (one fan-in is the *positive*
    polarity of another AND node v = AND(a, b)):

        w computes  AND(AND(a,b), c) = AND3(a,b,c).

    If an alternative AND node  m = AND(a, c)  or  m = AND(b, c)  already
    exists in the AIG *and* its positive polarity is already required by some
    other consumer, we can rewrite:

        w = AND(pos_v, c)  →  w = AND(b, pos_m)   [using AND(a,c)]
     or w = AND(pos_v, c)  →  w = AND(a, pos_m)   [using AND(b,c)]

    This removes pos_v from the fan-in of w.  If pos_v has no remaining
    consumers, its inverter gate is eliminated — saving 1 NAND gate.

    Topological safety: we only accept m with node_id < w's node_id so
    pos_m is already emitted when the forward gate pass reaches w.

    Returns True if at least one rewrite was applied.
    """
    # Build current input view (respecting already applied overrides).
    def inputs_of(node_id: int) -> Tuple[AIGLit, AIGLit]:
        if node_id in overrides:
            return overrides[node_id]
        e = aig._nodes[node_id - 1]
        return e[1], e[2]

    # pos_lit → set of AND-node IDs that currently consume it as a fan-in
    pos_consumers: Dict[AIGLit, List[int]] = {}
    for i, entry in enumerate(aig._nodes):
        if entry[0] != 'and':
            continue
        node_id = i + 1
        a_lit, b_lit = inputs_of(node_id)
        for lit in (a_lit, b_lit):
            if lit & 1 == 0 and lit in needed_lits:   # positive polarity
                pos_consumers.setdefault(lit, []).append(node_id)

    changed = False

    for i, entry in enumerate(aig._nodes):
        if entry[0] != 'and':
            continue
        v_id   = i + 1
        pos_v  = v_id * 2

        if pos_v not in needed_lits:
            continue

        a_lit, b_lit = inputs_of(v_id)
        consumers = list(pos_consumers.get(pos_v, []))

        for w_id in consumers:
            wa, wb = inputs_of(w_id)
            # Identify which fan-in of w is pos_v and which is c
            if wa == pos_v:
                c_lit = wb
            elif wb == pos_v:
                c_lit = wa
            else:
                continue   # already restructured away

            # Try AND(a_lit, c_lit)
            m_ac_pos = aig.get_and(a_lit, c_lit)
            if (m_ac_pos is not None
                    and m_ac_pos in needed_lits
                    and m_ac_pos != pos_v
                    and (m_ac_pos >> 1) < w_id):   # topological safety
                overrides[w_id] = (b_lit, m_ac_pos)
                pos_consumers.setdefault(m_ac_pos, []).append(w_id)
                pos_consumers[pos_v] = [x for x in pos_consumers[pos_v] if x != w_id]
                changed = True
                continue

            # Try AND(b_lit, c_lit)
            m_bc_pos = aig.get_and(b_lit, c_lit)
            if (m_bc_pos is not None
                    and m_bc_pos in needed_lits
                    and m_bc_pos != pos_v
                    and (m_bc_pos >> 1) < w_id):   # topological safety
                overrides[w_id] = (a_lit, m_bc_pos)
                pos_consumers.setdefault(m_bc_pos, []).append(w_id)
                pos_consumers[pos_v] = [x for x in pos_consumers[pos_v] if x != w_id]
                changed = True

    # Drop pos_lits that are no longer consumed by any AND node and are not
    # directly an output literal (output lits are never removable here).
    for pos_lit, consumers in pos_consumers.items():
        if not consumers and pos_lit in needed_lits and pos_lit not in output_lits_set:
            needed_lits.discard(pos_lit)

    return changed


def aig_to_gates(
    aig: AIG,
    output_lits: List[AIGLit],
) -> Tuple[List[Gate], List[str], int]:
    """
    Translates an optimized AIG into a minimal NAND gate list.

    Every AIG node `v = AND(a, b)` natively produces `~v = NAND(a, b)` for
    free.  We emit `v = NOT(~v)` only when the positive polarity is actually
    consumed.

    Two structural pre-passes run before gate emission:
      • Bubble-pushing  — rewrites AND3 patterns to eliminate positive-polarity
        inverters when an alternative sub-AND is already computed.
      • XOR extraction  — detects 3-AND XOR/XNOR subgraphs (AND(~n1,~n2) where
        n1=AND(p,q), n2=AND(p^1,q^1)) and replaces each exclusive pattern with
        the canonical 4-NAND form, saving ≥1 gate per XOR vs the naive 5-gate
        translation.

    Returns (gates, out_wires, n_xor_extracted).
    """
    # ── 1. Compute initial needed-literal set ────────────────────────────────
    needed_lits = _compute_needed_lits(aig, output_lits)

    # ── 2. Bubble-push: reduce positive-polarity fan-ins ────────────────────
    overrides: Dict[int, Tuple[AIGLit, AIGLit]] = {}
    output_lits_set = set(output_lits)
    for _ in range(8):   # iterate to fixed point (usually ≤ 3 rounds)
        if not _bubble_push(aig, needed_lits, overrides, output_lits_set):
            break

    # ── 3. Re-expand needed_lits for overridden nodes ───────────────────────
    needed_lits = set(output_lits)
    for i in range(len(aig._nodes) - 1, -1, -1):
        node_id = i + 1
        pos_lit = node_id * 2
        neg_lit = node_id * 2 + 1
        if pos_lit in needed_lits or neg_lit in needed_lits:
            if node_id in overrides:
                a_lit, b_lit = overrides[node_id]
            else:
                entry = aig._nodes[i]
                if entry[0] not in ('and', 'xor'):
                    continue
                _, a_lit, b_lit = entry
            needed_lits.add(a_lit)
            needed_lits.add(b_lit)

    # ── 4. XOR extraction ───────────────────────────────────────────────────
    # Detect patterns  n3=AND(~n1,~n2), n1=AND(p,q), n2=AND(p^1,q^1)
    # where n1 and n2 are exclusively consumed by n3.
    xor_map = _detect_xor_patterns(aig, needed_lits, output_lits, overrides)

    # Build the set of inner XOR nodes (n1, n2) that are subsumed.
    xor_inner: set = set()
    for n3_id, (op1, op2) in xor_map.items():
        e = aig._nodes[n3_id - 1]
        xor_inner.add(e[1] >> 1)   # n1_id
        xor_inner.add(e[2] >> 1)   # n2_id

    # Re-expand needed_lits with XOR roots handled specially: add operands
    # directly and skip the inner nodes entirely.
    if xor_map:
        needed_lits = set(output_lits)
        for i in range(len(aig._nodes) - 1, -1, -1):
            node_id = i + 1
            pos_lit = node_id * 2
            neg_lit = node_id * 2 + 1
            if pos_lit not in needed_lits and neg_lit not in needed_lits:
                continue
            if node_id in xor_inner:
                continue   # inputs reachable via the XOR root path
            if node_id in xor_map:
                op1, op2 = xor_map[node_id]
                needed_lits.add(op1)
                needed_lits.add(op2)
                continue
            if node_id in overrides:
                a_lit, b_lit = overrides[node_id]
            else:
                entry = aig._nodes[i]
                if entry[0] not in ('and', 'xor'):
                    continue
                _, a_lit, b_lit = entry
            needed_lits.add(a_lit)
            needed_lits.add(b_lit)

    # ── 5. Gate emission (forward pass) ──────────────────────────────────────
    w_cnt = 0
    def new_wire() -> str:
        nonlocal w_cnt; w_cnt += 1
        return f'w{w_cnt}'

    gates: List[Gate] = []
    lit_to_wire: Dict[AIGLit, str] = {}

    if TRUE in needed_lits or FALSE in needed_lits:
        gates.append(('c1', 'ONE',  []))
        gates.append(('c0', 'ZERO', []))
        lit_to_wire[TRUE]  = 'c1'
        lit_to_wire[FALSE] = 'c0'

    for i, entry in enumerate(aig._nodes):
        node_id = i + 1
        pos_lit = node_id * 2
        neg_lit = node_id * 2 + 1

        if pos_lit not in needed_lits and neg_lit not in needed_lits:
            continue

        if node_id in xor_inner:
            continue   # subsumed by the XOR-root 4-NAND emission below

        if entry[0] == 'input':
            lit_to_wire[pos_lit] = entry[1]
            if neg_lit in needed_lits:
                w_neg = new_wire()
                gates.append((w_neg, 'NAND', [entry[1], entry[1]]))
                lit_to_wire[neg_lit] = w_neg
            continue

        if node_id in xor_map:
            # 4-NAND canonical XOR: ~n3 = XOR(op1, op2)
            #   g1 = NAND(op1, op2)
            #   g2 = NAND(op1, g1)
            #   g3 = NAND(op2, g1)
            #   out= NAND(g2, g3)   ← this is neg_lit (~n3)
            op1, op2 = xor_map[node_id]
            w1 = lit_to_wire[op1]
            w2 = lit_to_wire[op2]
            wg1 = new_wire(); gates.append((wg1, 'NAND', [w1, w2]))
            wg2 = new_wire(); gates.append((wg2, 'NAND', [w1, wg1]))
            wg3 = new_wire(); gates.append((wg3, 'NAND', [w2, wg1]))
            wout = new_wire(); gates.append((wout, 'NAND', [wg2, wg3]))
            lit_to_wire[neg_lit] = wout
            if pos_lit in needed_lits:
                w_pos = new_wire()
                gates.append((w_pos, 'NAND', [wout, wout]))
                lit_to_wire[pos_lit] = w_pos
            continue

        if entry[0] == 'xor':
            # Native XOR node — 4-NAND form; positive literal = XOR output
            _, a_lit, b_lit = entry
            wa = lit_to_wire[a_lit]
            wb = lit_to_wire[b_lit]
            wg1 = new_wire(); gates.append((wg1, 'NAND', [wa, wb]))
            wg2 = new_wire(); gates.append((wg2, 'NAND', [wa, wg1]))
            wg3 = new_wire(); gates.append((wg3, 'NAND', [wb, wg1]))
            wout = new_wire(); gates.append((wout, 'NAND', [wg2, wg3]))
            lit_to_wire[pos_lit] = wout   # XOR(a,b) = positive output
            if neg_lit in needed_lits:
                w_neg = new_wire()
                gates.append((w_neg, 'NAND', [wout, wout]))
                lit_to_wire[neg_lit] = w_neg
            continue

        # Standard AND node — use overridden inputs if available
        if node_id in overrides:
            a_lit, b_lit = overrides[node_id]
        else:
            _, a_lit, b_lit = entry

        wa = lit_to_wire[a_lit]
        wb = lit_to_wire[b_lit]

        w_neg = new_wire()
        gates.append((w_neg, 'NAND', [wa, wb]))
        lit_to_wire[neg_lit] = w_neg

        if pos_lit in needed_lits:
            w_pos = new_wire()
            gates.append((w_pos, 'NAND', [w_neg, w_neg]))
            lit_to_wire[pos_lit] = w_pos

    out_wires = [lit_to_wire[l] for l in output_lits]
    return gates, out_wires, len(xor_map)

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

