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
        # Structural choice chain (ROADMAP P3#9): singly-linked list over node IDs
        # where every member computes the same Boolean function (same polarity)
        # but with a structurally different AIG implementation. The class
        # representative is the first node encountered; _choice_next[rep] points
        # to the next alternative, which in turn points to the next, and so on
        # until a node with no entry (terminator). Rewriter uses these to enumerate
        # cuts at alternative roots, so a cut that is poor at one structural
        # variant may match a template at another.
        self._choice_next: Dict[int, int]       = {}

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

    # ── structural choice chain (ROADMAP P3#9) ───────────────────────────────

    def add_choice(self, rep_id: int, alt_id: int) -> None:
        """
        Append alt_id to rep_id's choice chain.

        Precondition (caller-enforced): nodes rep_id and alt_id compute the
        same Boolean function, same polarity. No validation is done here —
        callers typically detect equivalence via simulation + SAT.

        No-ops when rep_id == alt_id, when either is a non-gate node, or when
        alt_id is already somewhere in rep_id's chain.
        """
        if rep_id <= 0 or alt_id <= 0 or rep_id == alt_id:
            return
        if rep_id > len(self._nodes) or alt_id > len(self._nodes):
            return
        if self._nodes[rep_id - 1][0] == 'input' or self._nodes[alt_id - 1][0] == 'input':
            return
        # Walk rep's chain to the tail; stop early if alt_id is already linked.
        cur = rep_id
        while cur in self._choice_next:
            nxt = self._choice_next[cur]
            if nxt == alt_id:
                return
            cur = nxt
            if cur == alt_id:
                return
        self._choice_next[cur] = alt_id

    def choice_class(self, nid: int) -> List[int]:
        """
        Return all node IDs in nid's choice chain, starting at nid itself.

        The current implementation treats chains as rooted at any node and
        walks forward; it does NOT reconstruct a predecessor. Callers that
        need the full equivalence class should pass the representative
        (lowest ID in the class, returned by ``choice_rep``).
        """
        out = [nid]
        cur = nid
        while cur in self._choice_next:
            cur = self._choice_next[cur]
            out.append(cur)
        return out

    def choice_rep(self, nid: int) -> int:
        """
        Return the representative (chain head) for nid's choice class.

        Computed by linear scan — OK because choice chains are short (≤ 4
        alternatives in practice). Use sparingly on hot paths.
        """
        if nid not in self._choice_next and not any(v == nid for v in self._choice_next.values()):
            return nid
        for head, nxt in self._choice_next.items():
            seen = {head}
            cur = nxt
            while True:
                if cur == nid:
                    return head
                seen.add(cur)
                if cur not in self._choice_next:
                    break
                cur = self._choice_next[cur]
                if cur in seen:
                    break
        return nid

    def n_choice_links(self) -> int:
        return len(self._choice_next)

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

        # Drop any choice links that reference rolled-back node IDs.
        if self._choice_next:
            self._choice_next = {
                k: v for k, v in self._choice_next.items()
                if k <= n and v <= n
            }

    # ── garbage collection ───────────────────────────────────────────────────

    def gc(self, out_lits: List[Lit]) -> Tuple['AIG', List[Lit]]:
        """
        Return a compacted copy containing only nodes reachable from out_lits.

        Dead nodes (translated but never referenced by any output) are removed
        from both _nodes, _hash, and _xhash.  Structural hashing in the rebuilt
        AIG may additionally merge node pairs that happen to become identical
        after dead-code removal.

        Choice chains (ROADMAP P3#9): every member of a chain that contains a
        live node is itself kept live, and the chain is rebuilt in the new
        AIG. Members that are not children of live nodes must still be
        instantiated so that cut-matching can use them as alternative roots;
        they are added via a dedicated DFS pass before the topological rebuild.
        """
        # DFS from outputs to mark reachable node IDs. Choice-chain members are
        # considered live when any node in their class is live, so the rewriter
        # can reach the alternative implementation through the chain.
        reachable: set = set()
        stack = [self.node_of(lit) for lit in out_lits if self.node_of(lit) > 0]

        # Build choice-class membership: node_id → rep_id; members[rep_id] = [ids]
        class_members: Dict[int, List[int]] = {}
        visited_link: set = set()
        for head in list(self._choice_next.keys()):
            if head in visited_link:
                continue
            chain = [head]
            cur = head
            while cur in self._choice_next:
                cur = self._choice_next[cur]
                if cur in visited_link:
                    chain = []
                    break
                chain.append(cur)
            for m in chain:
                visited_link.add(m)
            if chain:
                rep = chain[0]
                class_members[rep] = chain
        node_to_rep: Dict[int, int] = {}
        for rep, members in class_members.items():
            for m in members:
                node_to_rep[m] = rep

        def _mark_subdag(start: int) -> None:
            st = [start]
            while st:
                nid = st.pop()
                if nid in reachable or nid <= 0:
                    continue
                reachable.add(nid)
                entry = self._nodes[nid - 1]
                if entry[0] in ('and', 'xor'):
                    _, a_lit, b_lit = entry
                    for child_lit in (a_lit, b_lit):
                        ch = self.node_of(child_lit)
                        if ch > 0 and ch not in reachable:
                            st.append(ch)

        while stack:
            nid = stack.pop()
            if nid in reachable:
                continue
            _mark_subdag(nid)
            # Bring in every choice-class sibling of any newly-reached node.
            # Repeat until closure, since adding a sibling may pull in new
            # intermediates that themselves belong to other classes.
            changed = True
            while changed:
                changed = False
                for live in list(reachable):
                    rep = node_to_rep.get(live)
                    if rep is None:
                        continue
                    for sib in class_members[rep]:
                        if sib not in reachable:
                            _mark_subdag(sib)
                            changed = True

        # Rebuild in topological order, skipping unreachable nodes.
        new_aig = AIG()
        lit_map: Dict[Lit, Lit] = {FALSE: FALSE, TRUE: TRUE}
        old_to_new_id: Dict[int, int] = {}
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
            old_to_new_id[nid] = nlit >> 1

        # Rebuild choice chains, skipping entries whose endpoints collapsed
        # into the same new node (structural hashing may merge alternatives).
        for rep, members in class_members.items():
            new_ids: List[int] = []
            for m in members:
                if m in old_to_new_id:
                    nid_new = old_to_new_id[m]
                    if nid_new > 0 and nid_new not in new_ids:
                        new_ids.append(nid_new)
            for a, b in zip(new_ids, new_ids[1:]):
                new_aig.add_choice(a, b)

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

        # Preserve choice chains from `other`, remapping node IDs through
        # lit_map. Chains that collapse (both ends resolve to the same new
        # node via structural hashing) are dropped.
        if other._choice_next:
            visited_link: set = set()
            for head in list(other._choice_next.keys()):
                if head in visited_link:
                    continue
                chain = [head]
                cur = head
                while cur in other._choice_next:
                    cur = other._choice_next[cur]
                    if cur in visited_link:
                        chain = []
                        break
                    chain.append(cur)
                for m in chain:
                    visited_link.add(m)
                new_ids: List[int] = []
                for m in chain:
                    nlit = lit_map.get(m * 2)
                    if nlit is None:
                        continue
                    nnid = nlit >> 1
                    if nnid > 0 and nnid not in new_ids:
                        new_ids.append(nnid)
                for a, b in zip(new_ids, new_ids[1:]):
                    self.add_choice(a, b)
        return lit_map
