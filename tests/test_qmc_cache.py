"""Regression tests for the `quine_mccluskey` memoization cache.

The cache is a transparent optimisation — a cached call must return a list
that is *equivalent* to a fresh recompute (same set of prime implicants,
sorted the same way), and callers must be free to mutate the returned
list without poisoning the cache.

Memoisation is exercised end-to-end by `test_builtin_circuits.py` and
`test_mcnc_benchmarks.py`, which would surface any semantic drift; this
file targets the cache contract directly so a regression points at the
right module.
"""
from __future__ import annotations

import random

import pytest

from nand_optimizer.core.implicant import (
    DASH,
    Implicant,
    _qmc_cache_clear,
    _qmc_cache_size,
    espresso,
    int_to_bits,
    quine_mccluskey,
)


def _random_cubes(n_vars: int, n_cubes: int, seed: int):
    """Random ternary cube cover."""
    rng = random.Random(seed)
    cubes = set()
    while len(cubes) < n_cubes:
        cubes.add(tuple(rng.choice((0, 1, DASH)) for _ in range(n_vars)))
    return list(cubes)


def _bits_set(prims):
    return tuple(sorted(p.bits for p in prims))


def test_cache_hit_matches_fresh_compute():
    _qmc_cache_clear()
    on = [int_to_bits(m, 4) for m in (1, 2, 5, 6, 9, 10, 13)]
    dc = [int_to_bits(0, 4)]

    first = quine_mccluskey(on, dc, 4)
    assert _qmc_cache_size() == 1, 'first call should populate the cache'

    second = quine_mccluskey(on, dc, 4)
    assert _qmc_cache_size() == 1, 'second call should hit, not refill'

    # Equal sets of primes; cached call returns a fresh list.
    assert _bits_set(first) == _bits_set(second)
    assert second is not first


def test_cache_returns_independent_list():
    _qmc_cache_clear()
    on = [(0, 1), (1, 0)]
    first = quine_mccluskey(on, [], 2)
    first.clear()
    second = quine_mccluskey(on, [], 2)
    assert second, 'mutating the returned list must not poison the cache'


def test_cache_is_order_invariant_on_inputs():
    _qmc_cache_clear()
    on = [int_to_bits(m, 5) for m in (1, 4, 9, 17, 22, 30)]
    dc = [int_to_bits(m, 5) for m in (0, 7)]

    primes_a = quine_mccluskey(on, dc, 5)
    primes_b = quine_mccluskey(list(reversed(on)), list(reversed(dc)), 5)
    primes_c = quine_mccluskey(on + on, dc, 5)   # duplicates collapse

    assert _bits_set(primes_a) == _bits_set(primes_b) == _bits_set(primes_c)
    # All three calls share one cache entry — frozenset key is canonical.
    assert _qmc_cache_size() == 1


def test_distinct_n_vars_get_distinct_cache_entries():
    _qmc_cache_clear()
    cubes = [(0, 0), (1, 1)]
    quine_mccluskey(cubes, [], 2)
    quine_mccluskey(cubes, [], 3)   # nonsensical width but exercises key
    assert _qmc_cache_size() == 2


def test_empty_inputs_are_cached():
    _qmc_cache_clear()
    assert quine_mccluskey([], [], 4) == []
    assert _qmc_cache_size() == 1
    assert quine_mccluskey([], [], 4) == []
    assert _qmc_cache_size() == 1


@pytest.mark.parametrize('seed', range(20))
def test_cached_vs_uncached_agree_random(seed):
    n_vars  = 4 + (seed % 3)               # 4..6
    n_cubes = 2 + (seed % 8)               # 2..9
    on = _random_cubes(n_vars, n_cubes, seed)
    dc = _random_cubes(n_vars, max(0, n_cubes // 2), seed * 31 + 7)

    _qmc_cache_clear()
    fresh = quine_mccluskey(on, dc, n_vars)
    cached = quine_mccluskey(on, dc, n_vars)
    assert _bits_set(fresh) == _bits_set(cached)


def test_espresso_consumers_unaffected():
    """`espresso()` calls QMC; the cover it produces must still be valid."""
    _qmc_cache_clear()
    on = [int_to_bits(m, 4) for m in (3, 5, 7, 11, 13, 14, 15)]
    cover_first  = espresso(on, [], 4)
    cover_second = espresso(on, [], 4)

    assert _bits_set(cover_first) == _bits_set(cover_second)
    # Cover must still cover every on-cube (sanity).
    for c in on:
        cube = Implicant(c)
        assert any(p.subsumes_masks(cube._care, cube._value)
                   for p in cover_first)
