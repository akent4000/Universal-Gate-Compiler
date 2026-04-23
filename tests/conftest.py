"""Pytest configuration for the UGC test suite.

Ensures the repository root is on ``sys.path`` so ``import nand_optimizer``
works regardless of where pytest is launched from, and exposes the registry
of benchmark factories that tests parameterise over.
"""
from __future__ import annotations

import os
import sys

HERE      = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from nand_optimizer.examples.circuits   import (seven_segment, two_bit_adder,
                                                 bcd_to_excess3)
from nand_optimizer.examples.benchmarks import (hamming_weight_5, parity_9,
                                                 multiplier_3x3, multiplier_4x4,
                                                 misex1, z4ml)

BUILTIN_FACTORIES = {
    '7seg':    seven_segment,
    'adder':   two_bit_adder,
    'excess3': bcd_to_excess3,
}

MCNC_FACTORIES = {
    'rd53':    hamming_weight_5,
    'parity9': parity_9,
    'mult3':   multiplier_3x3,
    'misex1':  misex1,
    'z4ml':    z4ml,
    'mult4':   multiplier_4x4,
}

ALL_FACTORIES = {**BUILTIN_FACTORIES, **MCNC_FACTORIES}
