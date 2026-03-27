"""
Quine-McCluskey prime implicant generation and minimum cover selection.

Works with any number of variables — the bit width is inferred
from the Implicant.bits tuple length.
"""

from __future__ import annotations
from typing import Dict, FrozenSet, List, Set, Tuple

from .expr import Expr, Lit, And, Or, ZERO, ONE


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def int_to_bits(n: int, width: int) -> Tuple[int, ...]:
    """Convert integer to tuple of bits (MSB first)."""
    return tuple((n >> (width - 1 - i)) & 1 for i in range(width))


# ═══════════════════════════════════════════════════════════════════════════════
#  Implicant
# ═══════════════════════════════════════════════════════════════════════════════

class Implicant:
    """
    A product term / prime implicant.

    bits[i] ∈ {0, 1, DASH}  where DASH means "don't care at position i".
    covered = frozenset of original minterms subsumed by this implicant.
    """
    DASH = -1

    def __init__(self, bits: Tuple[int, ...], covered: FrozenSet[int]):
        self.bits    = bits
        self.covered = covered

    # ── properties ────────────────────────────────────────────────────────────

    def literal_count(self) -> int:
        return sum(1 for b in self.bits if b != self.DASH)

    def covers_minterm(self, m: int) -> bool:
        mb = int_to_bits(m, len(self.bits))
        return all(ib == self.DASH or ib == mb[i] for i, ib in enumerate(self.bits))

    # ── combination ───────────────────────────────────────────────────────────

    def can_combine(self, other: Implicant) -> bool:
        """Two implicants combine if DASH positions match and exactly one bit differs."""
        diffs = 0
        for a, b in zip(self.bits, other.bits):
            if (a == self.DASH) != (b == self.DASH):
                return False
            if a != b:
                diffs += 1
        return diffs == 1

    def combine(self, other: Implicant) -> Implicant:
        new = tuple(self.DASH if a != b else a for a, b in zip(self.bits, other.bits))
        return Implicant(new, self.covered | other.covered)

    # ── display ───────────────────────────────────────────────────────────────

    def to_term_str(self, var_names: List[str]) -> str:
        parts = [
            var_names[i] if b == 1 else f'~{var_names[i]}'
            for i, b in enumerate(self.bits) if b != self.DASH
        ]
        return ' & '.join(parts) if parts else '1'

    def __repr__(self) -> str:
        s = ''.join('-' if b == self.DASH else str(b) for b in self.bits)
        return f'<{s} {{{",".join(str(c) for c in sorted(self.covered))}}}>'

    def __eq__(self, other) -> bool:
        return isinstance(other, Implicant) and self.bits == other.bits

    def __hash__(self) -> int:
        return hash(self.bits)


# ═══════════════════════════════════════════════════════════════════════════════
#  QMC  →  prime implicants
# ═══════════════════════════════════════════════════════════════════════════════

def quine_mccluskey(ones: Set[int], dont_cares: Set[int],
                    n_vars: int) -> List[Implicant]:
    """Return all prime implicants for (ones ∪ dont_cares) over *n_vars* inputs."""
    all_terms = ones | dont_cares
    if not all_terms:
        return []

    current: List[Implicant] = [
        Implicant(int_to_bits(m, n_vars), frozenset({m}))
        for m in sorted(all_terms)
    ]
    primes: Set[Implicant] = set()

    while current:
        combined: List[Implicant] = []
        used: Set[int] = set()

        for i in range(len(current)):
            for j in range(i + 1, len(current)):
                if current[i].can_combine(current[j]):
                    merged = current[i].combine(current[j])
                    if merged not in combined:
                        combined.append(merged)
                    used.add(i)
                    used.add(j)

        for i, imp in enumerate(current):
            if i not in used:
                primes.add(imp)

        current = combined

    return sorted(primes, key=lambda p: p.bits)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cover selection  (essential PIs + greedy)
# ═══════════════════════════════════════════════════════════════════════════════

def select_cover(primes: List[Implicant], ones: Set[int]) -> List[Implicant]:
    """
    Select a minimal cover:
      1. Find essential prime implicants (only PI covering a minterm).
      2. Greedily cover remaining minterms (most coverage, fewest literals).
    """
    if not ones:
        return []

    coverage: Dict[int, List[Implicant]] = {m: [] for m in ones}
    for pi in primes:
        for m in ones:
            if m in pi.covered:
                coverage[m].append(pi)

    selected:  List[Implicant] = []
    remaining: Set[int]        = set(ones)

    # essential PIs
    changed = True
    while changed and remaining:
        changed = False
        for m in sorted(remaining):
            avail = [pi for pi in coverage[m] if pi not in selected]
            if len(avail) == 1:
                pi = avail[0]
                selected.append(pi)
                remaining -= pi.covered & remaining
                changed = True

    # greedy cover
    while remaining:
        best = max(
            (pi for pi in primes if pi not in selected),
            key=lambda pi: (len(pi.covered & remaining), -pi.literal_count()),
            default=None,
        )
        if best is None:
            break
        selected.append(best)
        remaining -= best.covered

    return selected


def espresso(ones: Set[int], dont_cares: Set[int],
             n_vars: int) -> List[Implicant]:
    """Full QMC minimisation: prime generation + cover selection."""
    primes = quine_mccluskey(ones, dont_cares, n_vars)
    return select_cover(primes, ones)


# ═══════════════════════════════════════════════════════════════════════════════
#  Conversion helpers
# ═══════════════════════════════════════════════════════════════════════════════

def implicants_to_expr(imps: List[Implicant],
                       var_names: List[str]) -> Expr:
    """Convert a list of prime implicants to a SOP Expr tree."""
    if not imps:
        return ZERO
    terms: List[Expr] = []
    for imp in imps:
        lits = [
            Lit(var_names[i], b == 0)
            for i, b in enumerate(imp.bits) if b != Implicant.DASH
        ]
        if not lits:
            terms.append(ONE)
        elif len(lits) == 1:
            terms.append(lits[0])
        else:
            terms.append(And(*lits))
    return terms[0] if len(terms) == 1 else Or(*terms)