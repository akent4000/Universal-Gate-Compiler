"""
Quine-McCluskey prime implicant generation and minimum cover selection.

All operations work directly on cube covers — ternary arrays where each
position holds 0, 1, or DASH (= -1, "don't care").  No 2^N minterm
expansion is ever required.

Primary entry points:
  quine_mccluskey(on_cubes, dc_cubes, n_vars) -> List[Implicant]
  select_cover(primes, on_cubes)              -> List[Implicant]
  espresso(on_cubes, dc_cubes, n_vars)        -> List[Implicant]
  multi_output_espresso(on_cubes_list, dc_cubes, n_vars)

Utilities:
  int_to_bits(n, width)               -> Tuple[int, ...]
  _expand_cubes_to_set(cubes, n_vars) -> Set[int]   (small n only)
  _int_set_to_cubes(ints, n_vars)     -> List[Tuple]
"""

from __future__ import annotations
from collections import defaultdict
from typing import Dict, List, Set, Tuple

from .expr import Expr, Lit, And, Or, ZERO, ONE


DASH = -1  # wildcard / don't-care position in a ternary cube


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def int_to_bits(n: int, width: int) -> Tuple[int, ...]:
    """Convert integer to MSB-first bit tuple."""
    return tuple((n >> (width - 1 - i)) & 1 for i in range(width))


def _expand_cubes_to_set(cubes: List[Tuple[int, ...]], n_vars: int) -> Set[int]:
    """Expand ternary cubes to the set of minterm integers they cover.

    Only call for small n (n <= 20); for large n the result is exponential.
    """
    result: Set[int] = set()
    for cube in cubes:
        minterms = [0]
        for i, b in enumerate(cube):        # i=0 is MSB
            bit_pos = n_vars - 1 - i
            if b == DASH:
                minterms = minterms + [m | (1 << bit_pos) for m in minterms]
            elif b == 1:
                minterms = [m | (1 << bit_pos) for m in minterms]
            # b == 0: bit stays 0
        result.update(minterms)
    return result


def _int_set_to_cubes(ints: Set[int], n_vars: int) -> List[Tuple[int, ...]]:
    """Convert a set of minterm integers to unit (no-DASH) cubes."""
    return [int_to_bits(m, n_vars) for m in sorted(ints)]


# ─────────────────────────────────────────────────────────────────────────────
#  Implicant
# ─────────────────────────────────────────────────────────────────────────────

class Implicant:
    """
    A product term / prime implicant as a ternary cube.

    bits[i] in {0, 1, DASH}  where DASH = -1 means "don't care at position i".
    """
    DASH = DASH

    __slots__ = ('bits',)

    def __init__(self, bits: Tuple[int, ...]):
        self.bits = bits

    # ── properties ────────────────────────────────────────────────────────────

    def literal_count(self) -> int:
        return sum(1 for b in self.bits if b != DASH)

    def subsumes(self, cube: Tuple[int, ...]) -> bool:
        """True iff every minterm in *cube* is also covered by self.

        Cube-subsumption: self covers cube iff for every position i,
        self.bits[i] == DASH  or  self.bits[i] == cube[i].
        """
        return all(pb == DASH or pb == cb for pb, cb in zip(self.bits, cube))

    def covers_minterm(self, m: int) -> bool:
        mb = int_to_bits(m, len(self.bits))
        return all(ib == DASH or ib == mb[i] for i, ib in enumerate(self.bits))

    # ── combination ───────────────────────────────────────────────────────────

    def can_combine(self, other: Implicant) -> bool:
        """Combine if DASH positions match and exactly one non-DASH bit differs."""
        diffs = 0
        for a, b in zip(self.bits, other.bits):
            if (a == DASH) != (b == DASH):
                return False
            if a != b:
                diffs += 1
                if diffs > 1:
                    return False
        return diffs == 1

    def combine(self, other: Implicant) -> Implicant:
        return Implicant(
            tuple(DASH if a != b else a for a, b in zip(self.bits, other.bits))
        )

    # ── display ───────────────────────────────────────────────────────────────

    def to_term_str(self, var_names: List[str]) -> str:
        parts = [
            var_names[i] if b == 1 else f'~{var_names[i]}'
            for i, b in enumerate(self.bits) if b != DASH
        ]
        return ' & '.join(parts) if parts else '1'

    def __repr__(self) -> str:
        s = ''.join('-' if b == DASH else str(b) for b in self.bits)
        return f'<{s}>'

    def __eq__(self, other) -> bool:
        return isinstance(other, Implicant) and self.bits == other.bits

    def __hash__(self) -> int:
        return hash(self.bits)


# ─────────────────────────────────────────────────────────────────────────────
#  QMC → prime implicants
# ─────────────────────────────────────────────────────────────────────────────

def quine_mccluskey(
    on_cubes: List[Tuple[int, ...]],
    dc_cubes: List[Tuple[int, ...]],
    n_vars:   int,
) -> List[Implicant]:
    """Return all prime implicants for (on_cubes ∪ dc_cubes).

    Operates directly on ternary cubes — no 2^N expansion needed.
    Two cubes combine iff their DASH masks are identical and they differ
    in exactly one non-DASH bit; that bit becomes DASH in the merged cube.
    Grouping by "count of explicit 1-bits" ensures all valid pairs are tried
    in adjacent groups (changing one 0→1 shifts ones_count by exactly 1).
    """
    all_cubes: Dict[Tuple[int, ...], Implicant] = {}
    for c in on_cubes + dc_cubes:
        all_cubes[c] = Implicant(c)

    if not all_cubes:
        return []

    def ones_count(bits: Tuple[int, ...]) -> int:
        return sum(1 for b in bits if b == 1)

    current = dict(all_cubes)
    primes: Dict[Tuple[int, ...], Implicant] = {}

    while current:
        groups: Dict[int, List[Implicant]] = defaultdict(list)
        for imp in current.values():
            groups[ones_count(imp.bits)].append(imp)

        used: Set[Tuple[int, ...]] = set()
        next_level: Dict[Tuple[int, ...], Implicant] = {}

        for k in sorted(groups):
            if k + 1 not in groups:
                continue
            for a in groups[k]:
                for b in groups[k + 1]:
                    if a.can_combine(b):
                        merged = a.combine(b)
                        used.add(a.bits)
                        used.add(b.bits)
                        next_level[merged.bits] = merged

        for bits, imp in current.items():
            if bits not in used:
                primes[bits] = imp

        current = next_level

    return sorted(primes.values(), key=lambda p: p.bits)


# ─────────────────────────────────────────────────────────────────────────────
#  Cover selection  (essential PIs + greedy)
# ─────────────────────────────────────────────────────────────────────────────

def select_cover(
    primes:   List[Implicant],
    on_cubes: List[Tuple[int, ...]],
) -> List[Implicant]:
    """Select a minimal cover of on_cubes using prime implicants.

    Coverage via cube subsumption: prime P covers on_cube C iff P.subsumes(C),
    i.e. every minterm in C is also in P.  No minterm enumeration needed.

    1. Essential PIs: unique prime subsuming some on_cube.
    2. Greedy: most remaining on_cubes covered, then fewest literals.
    """
    if not on_cubes:
        return []

    coverage: List[List[int]] = [
        [j for j, p in enumerate(primes) if p.subsumes(on_cubes[i])]
        for i in range(len(on_cubes))
    ]

    selected: List[Implicant] = []
    sel_set:  Set[int]         = set()
    uncov:    Set[int]         = set(range(len(on_cubes)))

    # 1. Essential PIs
    changed = True
    while changed and uncov:
        changed = False
        for i in list(uncov):
            avail = [j for j in coverage[i] if j not in sel_set]
            if len(avail) == 1:
                j = avail[0]
                selected.append(primes[j])
                sel_set.add(j)
                for k in list(uncov):
                    if primes[j].subsumes(on_cubes[k]):
                        uncov.discard(k)
                changed = True

    # 2. Greedy cover
    while uncov:
        best = max(
            (j for j in range(len(primes)) if j not in sel_set),
            key=lambda j: (
                sum(1 for k in uncov if primes[j].subsumes(on_cubes[k])),
                -primes[j].literal_count(),
            ),
            default=None,
        )
        if best is None:
            break
        selected.append(primes[best])
        sel_set.add(best)
        for k in list(uncov):
            if primes[best].subsumes(on_cubes[k]):
                uncov.discard(k)

    return selected


def espresso(
    on_cubes: List[Tuple[int, ...]],
    dc_cubes: List[Tuple[int, ...]],
    n_vars:   int,
) -> List[Implicant]:
    """Full QMC minimisation: prime generation + cover selection."""
    primes = quine_mccluskey(on_cubes, dc_cubes, n_vars)
    return select_cover(primes, on_cubes)


# ─────────────────────────────────────────────────────────────────────────────
#  Multi-output cover selection
# ─────────────────────────────────────────────────────────────────────────────

def _select_cover_shared(
    primes:    List[Implicant],
    on_cubes:  List[Tuple[int, ...]],
    bits_freq: Dict[Tuple[int, ...], int],
) -> List[Implicant]:
    """Cover selection with tiebreak bonus for primes shared across outputs."""
    if not on_cubes:
        return []

    coverage: List[List[int]] = [
        [j for j, p in enumerate(primes) if p.subsumes(on_cubes[i])]
        for i in range(len(on_cubes))
    ]

    selected: List[Implicant] = []
    sel_set:  Set[int]         = set()
    uncov:    Set[int]         = set(range(len(on_cubes)))

    changed = True
    while changed and uncov:
        changed = False
        for i in list(uncov):
            avail = [j for j in coverage[i] if j not in sel_set]
            if len(avail) == 1:
                j = avail[0]
                selected.append(primes[j])
                sel_set.add(j)
                for k in list(uncov):
                    if primes[j].subsumes(on_cubes[k]):
                        uncov.discard(k)
                changed = True

    while uncov:
        best = max(
            (j for j in range(len(primes)) if j not in sel_set),
            key=lambda j: (
                sum(1 for k in uncov if primes[j].subsumes(on_cubes[k])),
                1 if bits_freq.get(primes[j].bits, 1) > 1 else 0,
                -primes[j].literal_count(),
            ),
            default=None,
        )
        if best is None:
            break
        selected.append(primes[best])
        sel_set.add(best)
        for k in list(uncov):
            if primes[best].subsumes(on_cubes[k]):
                uncov.discard(k)

    return selected


def multi_output_espresso(
    on_cubes_list: List[List[Tuple[int, ...]]],
    dc_cubes:      List[Tuple[int, ...]],
    n_vars:        int,
) -> List[List[Implicant]]:
    """Multi-output minimisation with shared prime implicant preference."""
    n = len(on_cubes_list)
    if n == 0:
        return []
    if n == 1:
        return [espresso(on_cubes_list[0], dc_cubes, n_vars)]

    primes_per: List[List[Implicant]] = [
        quine_mccluskey(on_cubes_list[i], dc_cubes, n_vars)
        for i in range(n)
    ]

    bits_freq: Dict[Tuple[int, ...], int] = {}
    for primes in primes_per:
        for pi in primes:
            bits_freq[pi.bits] = bits_freq.get(pi.bits, 0) + 1

    return [
        _select_cover_shared(primes_per[i], on_cubes_list[i], bits_freq)
        for i in range(n)
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Conversion helpers
# ─────────────────────────────────────────────────────────────────────────────

def implicants_to_expr(imps: List[Implicant], var_names: List[str]) -> Expr:
    """Convert a list of prime implicants to a SOP Expr tree."""
    if not imps:
        return ZERO
    terms: List[Expr] = []
    for imp in imps:
        lits = [
            Lit(var_names[i], b == 0)
            for i, b in enumerate(imp.bits) if b != DASH
        ]
        if not lits:
            terms.append(ONE)
        elif len(lits) == 1:
            terms.append(lits[0])
        else:
            terms.append(And(*lits))
    return terms[0] if len(terms) == 1 else Or(*terms)
