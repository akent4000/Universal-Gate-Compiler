"""
And-Inverter Graph (AIG) — canonical Boolean function representation.

All Boolean functions are encoded using only:
  • 2-input AND nodes
  • Complemented edges (inversions are free — just flip a bit)

Literal encoding (AIGER convention):
  lit = node_id * 2 + complement_bit
  FALSE = 0   (constant-zero literal)
  TRUE  = 1   (constant-one literal, i.e. NOT(FALSE))

Structural hashing: AND(a, b) and AND(b, a) map to the same node.
Constant propagation is built into make_and.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple

# ── Type alias ───────────────────────────────────────────────────────────────

Lit = int   # AIG literal: node_id * 2 + complement_bit


# ── Module-level constants ───────────────────────────────────────────────────

FALSE: Lit = 0   # constant-zero literal
TRUE:  Lit = 1   # constant-one literal


# ═══════════════════════════════════════════════════════════════════════════════
#  AIG
# ═══════════════════════════════════════════════════════════════════════════════

class AIG:
    """
    Incrementally-built And-Inverter Graph with structural hashing.

    Nodes are numbered starting from 1 (node 0 = implicit constant).
    Each node is either a primary input or a 2-input AND gate.
    Inversions are represented as the complement bit of a literal, so
    NOT(x) = x ^ 1 — no extra node needed.

    The _nodes list stores entries in topological order so that the AIG
    can be converted to a gate list by a single forward pass.
    """

    def __init__(self):
        # Each entry: ('input', name) | ('and', lit_a, lit_b)
        self._nodes:      List                  = []
        # Structural hash: (normalized_lit_a, normalized_lit_b) → output_lit
        self._hash:       Dict[Tuple[Lit, Lit], Lit] = {}
        # Primary input name → positive literal
        self._input_lits: Dict[str, Lit]        = {}

    # ── size / info ──────────────────────────────────────────────────────────

    @property
    def n_inputs(self) -> int:
        return len(self._input_lits)

    @property
    def n_ands(self) -> int:
        return len(self._nodes) - len(self._input_lits)

    @property
    def n_nodes(self) -> int:
        return len(self._nodes)

    def input_names(self) -> List[str]:
        return list(self._input_lits.keys())

    # ── literal helpers (static) ─────────────────────────────────────────────

    @staticmethod
    def node_of(lit: Lit) -> int:
        """Node ID encoded in a literal (1-indexed; 0 = constant)."""
        return lit >> 1

    @staticmethod
    def is_complemented(lit: Lit) -> bool:
        return bool(lit & 1)

    # ── construction ─────────────────────────────────────────────────────────

    def make_input(self, name: str) -> Lit:
        """Return the positive literal for a primary input, creating it if new."""
        if name not in self._input_lits:
            node_id = len(self._nodes) + 1   # 1-indexed; 0 = constant
            self._nodes.append(('input', name))
            lit = node_id * 2                 # positive literal
            self._input_lits[name] = lit
        return self._input_lits[name]

    def make_and(self, a: Lit, b: Lit) -> Lit:
        """
        Return a literal for AND(a, b), with structural hashing and
        constant propagation.
        """
        # Constant propagation
        if a == FALSE or b == FALSE:   return FALSE
        if a == TRUE:                  return b
        if b == TRUE:                  return a
        if a == b:                     return a
        if a == (b ^ 1):               return FALSE   # a & ~a = 0

        # Normalise for canonical form (smaller literal first)
        if a > b:
            a, b = b, a

        key = (a, b)
        if key in self._hash:
            return self._hash[key]

        # New AND node
        node_id = len(self._nodes) + 1
        self._nodes.append(('and', a, b))
        out_lit = node_id * 2          # positive output literal
        self._hash[key] = out_lit
        return out_lit

    def make_not(self, a: Lit) -> Lit:
        """Complement a literal — O(1), no new node."""
        return a ^ 1

    def make_or(self, a: Lit, b: Lit) -> Lit:
        """OR(a, b) = ~(~a & ~b)  (De Morgan)."""
        return self.make_not(
            self.make_and(self.make_not(a), self.make_not(b))
        )

    def make_nand(self, a: Lit, b: Lit) -> Lit:
        """NAND(a, b) = ~AND(a, b)."""
        return self.make_not(self.make_and(a, b))

    def make_xor(self, a: Lit, b: Lit) -> Lit:
        """XOR(a, b) = (a & ~b) | (~a & b)."""
        return self.make_or(
            self.make_and(a, self.make_not(b)),
            self.make_and(self.make_not(a), b),
        )

    # ── cache inspection (non-mutating) ──────────────────────────────────────

    def has_and(self, a: Lit, b: Lit) -> bool:
        """Return True if AND(a, b) is already in the structural hash."""
        if a > b:
            a, b = b, a
        return (a, b) in self._hash

    def get_and(self, a: Lit, b: Lit) -> Optional[Lit]:
        """Return the cached literal for AND(a, b), or None if not present."""
        if a > b:
            a, b = b, a
        return self._hash.get((a, b))
