"""
XOR-And-Inverter Graph (XAG) — canonical Boolean function representation.

All Boolean functions are encoded using:
  • 2-input AND nodes
  • 2-input XOR nodes  ← first-class, not expanded to 3 ANDs
  • Complemented edges (inversions are free — just flip a bit)

Literal encoding (AIGER convention):
  lit = node_id * 2 + complement_bit
  FALSE = 0   (constant-zero literal)
  TRUE  = 1   (constant-one literal, i.e. NOT(FALSE))

Structural hashing: AND(a,b) and AND(b,a) map to the same node; same for XOR.
Constant propagation is built into make_and and make_xor.
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
    Incrementally-built XOR-And-Inverter Graph with structural hashing.

    Nodes are numbered starting from 1 (node 0 = implicit constant).
    Each node is either a primary input, a 2-input AND gate, or a 2-input
    XOR gate.  Inversions are represented as the complement bit of a literal,
    so NOT(x) = x ^ 1 — no extra node needed.

    The _nodes list stores entries in topological order so that the graph
    can be converted to a gate list by a single forward pass.
    """

    def __init__(self):
        # Each entry: ('input', name) | ('and', lit_a, lit_b) | ('xor', lit_a, lit_b)
        self._nodes:      List                  = []
        # Structural hash for AND: (normalized_lit_a, normalized_lit_b) → output_lit
        self._hash:       Dict[Tuple[Lit, Lit], Lit] = {}
        # Structural hash for XOR: (normalized_lit_a, normalized_lit_b) → output_lit
        self._xhash:      Dict[Tuple[Lit, Lit], Lit] = {}
        # Primary input name → positive literal
        self._input_lits: Dict[str, Lit]        = {}

    # ── size / info ──────────────────────────────────────────────────────────

    @property
    def n_inputs(self) -> int:
        return len(self._input_lits)

    @property
    def n_ands(self) -> int:
        """Total gate nodes (AND + XOR). Name kept for backward compatibility."""
        return len(self._nodes) - len(self._input_lits)

    @property
    def n_xors(self) -> int:
        return sum(1 for e in self._nodes if e[0] == 'xor')

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

    def make_xor(self, a: Lit, b: Lit) -> Lit:
        """
        Return a native first-class XOR node for XOR(a, b).

        Constant propagation + structural hashing.  Canonical form: smaller
        literal first.  The result is the positive literal of the XOR node;
        complement with ^ 1 to get XNOR.
        """
        # Constant propagation
        if a == FALSE:    return b
        if b == FALSE:    return a
        if a == TRUE:     return b ^ 1
        if b == TRUE:     return a ^ 1
        if a == b:        return FALSE
        if a == (b ^ 1):  return TRUE

        # Commutative normalization
        if a > b:
            a, b = b, a

        key = (a, b)
        if key in self._xhash:
            return self._xhash[key]

        # New XOR node
        node_id = len(self._nodes) + 1
        self._nodes.append(('xor', a, b))
        out_lit = node_id * 2
        self._xhash[key] = out_lit
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

    def has_xor(self, a: Lit, b: Lit) -> bool:
        """Return True if XOR(a, b) is already in the structural hash."""
        if a > b:
            a, b = b, a
        return (a, b) in self._xhash

    def get_xor(self, a: Lit, b: Lit) -> Optional[Lit]:
        """Return the cached literal for XOR(a, b), or None if not present."""
        if a > b:
            a, b = b, a
        return self._xhash.get((a, b))

    # ── speculative construction (snapshot / restore) ────────────────────────

    def snapshot(self) -> Tuple[int, List[str]]:
        """Capture current AIG state for a later restore()."""
        return (len(self._nodes), list(self._input_lits.keys()))

    def restore(self, snap: Tuple[int, List[str]]) -> None:
        """Roll the AIG back to a previous snapshot()."""
        n, kept_input_names = snap
        del self._nodes[n:]

        kept: Dict[str, Lit] = {}
        for name in kept_input_names:
            if name in self._input_lits:
                kept[name] = self._input_lits[name]
        self._input_lits = kept

        self._hash.clear()
        self._xhash.clear()
        for i, entry in enumerate(self._nodes):
            nid_lit = (i + 1) * 2
            if entry[0] == 'and':
                _, a, b = entry
                if a > b:
                    a, b = b, a
                self._hash[(a, b)] = nid_lit
            elif entry[0] == 'xor':
                _, a, b = entry
                if a > b:
                    a, b = b, a
                self._xhash[(a, b)] = nid_lit

    # ── garbage collection ───────────────────────────────────────────────────

    def gc(self, out_lits: List[Lit]) -> Tuple['AIG', List[Lit]]:
        """
        Return a compacted copy containing only nodes reachable from out_lits.

        Dead nodes (translated but never referenced by any output) are removed
        from both _nodes, _hash, and _xhash.  Structural hashing in the rebuilt
        AIG may additionally merge node pairs that happen to become identical
        after dead-code removal.
        """
        # DFS from outputs to mark reachable node IDs.
        reachable: set = set()
        stack = [self.node_of(lit) for lit in out_lits if self.node_of(lit) > 0]
        while stack:
            nid = stack.pop()
            if nid in reachable:
                continue
            reachable.add(nid)
            entry = self._nodes[nid - 1]
            if entry[0] in ('and', 'xor'):
                _, a_lit, b_lit = entry
                for child_lit in (a_lit, b_lit):
                    ch = self.node_of(child_lit)
                    if ch > 0 and ch not in reachable:
                        stack.append(ch)

        # Rebuild in topological order, skipping unreachable nodes.
        new_aig = AIG()
        lit_map: Dict[Lit, Lit] = {FALSE: FALSE, TRUE: TRUE}
        for i, entry in enumerate(self._nodes):
            nid = i + 1
            if nid not in reachable:
                continue
            if entry[0] == 'input':
                nlit = new_aig.make_input(entry[1])
            elif entry[0] == 'and':
                _, a_lit, b_lit = entry
                nlit = new_aig.make_and(lit_map[a_lit], lit_map[b_lit])
            else:  # 'xor'
                _, a_lit, b_lit = entry
                nlit = new_aig.make_xor(lit_map[a_lit], lit_map[b_lit])
            lit_map[nid * 2]     = nlit
            lit_map[nid * 2 + 1] = nlit ^ 1

        new_outs = [lit_map.get(lit, lit) for lit in out_lits]
        return new_aig, new_outs

    # ── hierarchical composition ─────────────────────────────────────────────

    def compose(self, other: 'AIG', substitution: Dict[str, 'Lit']) -> Dict[int, 'Lit']:
        """
        Merge another AIG's nodes into self, substituting named inputs with literals.

        substitution maps input names in `other` to literals already valid in self.
        Inputs of `other` NOT in substitution become new primary inputs of self.

        Returns a lit_map: other's node literal → corresponding literal in self,
        covering both positive (nid*2) and complemented (nid*2+1) forms plus
        the constants 0 and 1.
        """
        lit_map: Dict[int, int] = {0: 0, 1: 1}
        for i, entry in enumerate(other._nodes):
            nid = i + 1
            if entry[0] == 'input':
                name = entry[1]
                nlit = substitution.get(name, self.make_input(name))
            elif entry[0] == 'and':
                _, a_lit, b_lit = entry
                nlit = self.make_and(lit_map[a_lit], lit_map[b_lit])
            else:  # 'xor'
                _, a_lit, b_lit = entry
                nlit = self.make_xor(lit_map[a_lit], lit_map[b_lit])
            lit_map[nid * 2]     = nlit
            lit_map[nid * 2 + 1] = nlit ^ 1
        return lit_map
