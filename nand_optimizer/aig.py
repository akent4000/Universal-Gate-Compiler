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

    # ── speculative construction (snapshot / restore) ────────────────────────
    #
    # Used by passes that want to try several alternative sub-networks and
    # pick the one that grows the AIG the least.  snapshot() captures the
    # node count + input table; restore(snap) truncates _nodes back to that
    # state, drops any inputs created after the snapshot, and reconstructs
    # _hash from the surviving AND nodes.  Literals obtained *before* the
    # snapshot stay valid; literals issued *after* it must be discarded.

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
        for i, entry in enumerate(self._nodes):
            if entry[0] == 'and':
                _, a, b = entry
                if a > b:
                    a, b = b, a
                self._hash[(a, b)] = (i + 1) * 2

    # ── garbage collection ───────────────────────────────────────────────────

    def gc(self, out_lits: List[Lit]) -> Tuple['AIG', List[Lit]]:
        """
        Return a compacted copy containing only nodes reachable from out_lits.

        Dead nodes (translated but never referenced by any output) are removed
        from both _nodes and _hash.  Structural hashing in the rebuilt AIG may
        additionally merge node pairs that happen to become identical after
        dead-code removal.

        Typical caller: rewrite_aig(), once per round, to prevent phantom
        MFFC-child translations from bloating the hash table.
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
            if entry[0] == 'and':
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
            else:
                _, a_lit, b_lit = entry
                nlit = new_aig.make_and(lit_map[a_lit], lit_map[b_lit])
            lit_map[nid * 2]     = nlit
            lit_map[nid * 2 + 1] = nlit ^ 1

        new_outs = [lit_map.get(lit, lit) for lit in out_lits]
        return new_aig, new_outs

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
