"""Property-based regression on random truth tables.

Runs a small batch of random `(n_inputs, n_outputs, dc_fraction)` truth
tables through the full pipeline and verifies each with ``miter_verify``.
Kept at 20 cases for CI budget; the full 200-case run is available via
``python -m nand_optimizer proptest --cases 200``.

When Hypothesis is installed, the richer strategy-based variants in
``nand_optimizer.testing.property_tests`` are also exposed here so pytest
can pick up shrinking.
"""
from __future__ import annotations

from nand_optimizer.testing.property_tests import run_property_tests


def test_random_equivalence_small_batch() -> None:
    assert run_property_tests(n_cases=20, seed=0xC0FFEE, verbose=False), \
        'run_property_tests reported failures — rerun with verbose=True'


try:
    from nand_optimizer.testing.property_tests import (
        property_random_equivalence,
        property_mutation_equivalence,
    )
except ImportError:
    pass
