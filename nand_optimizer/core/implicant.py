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
from typing import Dict, FrozenSet, List, Set, Tuple

from .expr import Expr, Lit, And, Or, ZERO, ONE


DASH = -1  # wildcard / don't-care position in a ternary cube


# ─────────────────────────────────────────────────────────────────────────────
#  QMC memoization
# ─────────────────────────────────────────────────────────────────────────────
#
# `quine_mccluskey()` is the single most-called espresso primitive — on the
# `mult4` profile it fires 4 698 times because Ashenhurst-Curtis bake-off
# rebuilds the chart for every bipartition probe AND for every (h_i, g)
# subproblem after a successful split. Most of those calls reuse the same
# (on-set, dc-set, n_vars) triple. We hash on the unordered cube covers
# (frozensets) so callers don't have to canonicalise their inputs.
#
# `Implicant` is effectively immutable (all `__slots__` are write-once in
# `__init__` / `_from_masks`), so handing the same object to multiple callers
# is safe; we still return a fresh list so callers may freely sort/extend it.

_QMC_CacheKey = Tuple[FrozenSet[Tuple[int, ...]], FrozenSet[Tuple[int, ...]], int]
_QMC_CACHE: Dict[_QMC_CacheKey, Tuple['Implicant', ...]] = {}
_QMC_CACHE_MAX = 8192   # safety cap; per-circuit working set is far smaller


def _qmc_cache_clear() -> None:
    """Drop every cached prime-implicant list. Test helper."""
    _QMC_CACHE.clear()


def _qmc_cache_size() -> int:
    return len(_QMC_CACHE)


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

    Internal representation is two int bit-masks (`_care`, `_value`) of width
    `_n` — bit position `(n - 1 - i)` holds the value at `bits[i]`. DASH bits
    are 0 in `_care`; `_value` is always masked by `_care` so XOR/AND on the
    masks produces correct ternary semantics with a single `int.bit_count()`
    popcount. The `bits` tuple is retained for external consumers (see
    mapping/nand.py, hashing into `Dict[Tuple, Implicant]`, etc.).
    """
    DASH = DASH

    __slots__ = ('bits', '_care', '_value', '_n')

    def __init__(self, bits: Tuple[int, ...]):
        self.bits = bits
        n = len(bits)
        care = 0
        value = 0
        for i, b in enumerate(bits):
            if b == DASH:
                continue
            pos = n - 1 - i
            care |= 1 << pos
            if b == 1:
                value |= 1 << pos
        self._n     = n
        self._care  = care
        self._value = value

    @classmethod
    def _from_masks(cls, care: int, value: int, n: int) -> Implicant:
        """Build an Implicant directly from bit-masks (skips per-position loop).

        Reconstructs `bits` as MSB-first ternary tuple so external consumers
        of `self.bits` keep working.
        """
        bits = tuple(
            ((value >> (n - 1 - i)) & 1) if (care >> (n - 1 - i)) & 1 else DASH
            for i in range(n)
        )
        self = cls.__new__(cls)
        self.bits   = bits
        self._n     = n
        self._care  = care
        self._value = value
        return self

    # ── properties ────────────────────────────────────────────────────────────

    def literal_count(self) -> int:
        return self._care.bit_count()

    def subsumes(self, cube: Tuple[int, ...]) -> bool:
        """True iff every minterm in *cube* is also covered by self.

        Cube-subsumption: self covers cube iff for every position i,
        self.bits[i] == DASH  or  self.bits[i] == cube[i].

        Accepts either a ternary cube tuple or precomputed (care, value) masks
        via `subsumes_masks`.
        """
        n = self._n
        cube_care  = 0
        cube_value = 0
        for i, b in enumerate(cube):
            if b == DASH:
                continue
            pos = n - 1 - i
            cube_care |= 1 << pos
            if b == 1:
                cube_value |= 1 << pos
        return (self._care & ~cube_care) == 0 \
           and ((self._value ^ cube_value) & self._care) == 0

    def subsumes_masks(self, cube_care: int, cube_value: int) -> bool:
        """Fast path for `subsumes` when the cube's masks are precomputed."""
        return (self._care & ~cube_care) == 0 \
           and ((self._value ^ cube_value) & self._care) == 0

    def covers_minterm(self, m: int) -> bool:
        # minterm m is a plain integer — every bit is a "care" bit.
        return ((self._value ^ m) & self._care) == 0

    # ── combination ───────────────────────────────────────────────────────────

    def can_combine(self, other: Implicant) -> bool:
        """Combine if DASH positions match and exactly one non-DASH bit differs."""
        if self._care != other._care:
            return False
        return (self._value ^ other._value).bit_count() == 1

    def combine(self, other: Implicant) -> Implicant:
        # Precondition: self.can_combine(other) — callers enforce this.
        # The differing bit becomes DASH; all others are preserved.
        diff = self._value ^ other._value
        new_care  = self._care & ~diff
        new_value = self._value & new_care
        return Implicant._from_masks(new_care, new_value, self._n)

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

    Result is memoised on the unordered (on, dc, n_vars) triple — see the
    cache notes at the top of this module.
    """
    on_key = frozenset(on_cubes)
    dc_key = frozenset(dc_cubes)
    cache_key: _QMC_CacheKey = (on_key, dc_key, n_vars)
    cached = _QMC_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    all_cubes: Dict[Tuple[int, ...], Implicant] = {}
    for c in on_cubes + dc_cubes:
        all_cubes[c] = Implicant(c)

    if not all_cubes:
        _QMC_CACHE[cache_key] = ()
        return []

    current = dict(all_cubes)
    primes: Dict[Tuple[int, ...], Implicant] = {}

    while current:
        groups: Dict[int, List[Implicant]] = defaultdict(list)
        for imp in current.values():
            # _value is care-masked, so bit_count == explicit "1"-bits.
            groups[imp._value.bit_count()].append(imp)

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

    result = sorted(primes.values(), key=lambda p: p.bits)

    if len(_QMC_CACHE) >= _QMC_CACHE_MAX:
        # FIFO eviction — the cache mostly fills inside one optimize() call,
        # so dropping the oldest entry rarely throws away a hot one.
        _QMC_CACHE.pop(next(iter(_QMC_CACHE)))
    _QMC_CACHE[cache_key] = tuple(result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Cover selection  (essential PIs + greedy)
# ─────────────────────────────────────────────────────────────────────────────

def _cube_masks(cube: Tuple[int, ...]) -> Tuple[int, int]:
    """Ternary cube → (care, value) int bit-masks (MSB = bit `n-1`)."""
    n = len(cube)
    care = 0
    value = 0
    for i, b in enumerate(cube):
        if b == DASH:
            continue
        pos = n - 1 - i
        care |= 1 << pos
        if b == 1:
            value |= 1 << pos
    return care, value


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

    # Precompute (care, value) for every on_cube — `subsumes` is called
    # O(|primes| * |on_cubes|) times in the coverage matrix plus another
    # O(|primes| * |uncov|) per greedy pick; recomputing tuple→mask every
    # call would dominate the function.
    cube_masks: List[Tuple[int, int]] = [_cube_masks(c) for c in on_cubes]

    coverage: List[List[int]] = [
        [j for j, p in enumerate(primes)
           if p.subsumes_masks(cube_masks[i][0], cube_masks[i][1])]
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
                pj = primes[j]
                for k in list(uncov):
                    cc, cv = cube_masks[k]
                    if pj.subsumes_masks(cc, cv):
                        uncov.discard(k)
                changed = True

    # 2. Greedy cover
    while uncov:
        best = max(
            (j for j in range(len(primes)) if j not in sel_set),
            key=lambda j: (
                sum(1 for k in uncov
                      if primes[j].subsumes_masks(cube_masks[k][0],
                                                  cube_masks[k][1])),
                -primes[j].literal_count(),
            ),
            default=None,
        )
        if best is None:
            break
        selected.append(primes[best])
        sel_set.add(best)
        pb = primes[best]
        for k in list(uncov):
            cc, cv = cube_masks[k]
            if pb.subsumes_masks(cc, cv):
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

    cube_masks: List[Tuple[int, int]] = [_cube_masks(c) for c in on_cubes]

    coverage: List[List[int]] = [
        [j for j, p in enumerate(primes)
           if p.subsumes_masks(cube_masks[i][0], cube_masks[i][1])]
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
                pj = primes[j]
                for k in list(uncov):
                    cc, cv = cube_masks[k]
                    if pj.subsumes_masks(cc, cv):
                        uncov.discard(k)
                changed = True

    while uncov:
        best = max(
            (j for j in range(len(primes)) if j not in sel_set),
            key=lambda j: (
                sum(1 for k in uncov
                      if primes[j].subsumes_masks(cube_masks[k][0],
                                                  cube_masks[k][1])),
                1 if bits_freq.get(primes[j].bits, 1) > 1 else 0,
                -primes[j].literal_count(),
            ),
            default=None,
        )
        if best is None:
            break
        selected.append(primes[best])
        sel_set.add(best)
        pb = primes[best]
        for k in list(uncov):
            cc, cv = cube_masks[k]
            if pb.subsumes_masks(cc, cv):
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
