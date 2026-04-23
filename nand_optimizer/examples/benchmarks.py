"""
Classic benchmark circuits for stress-testing the NAND optimizer.

Functions
---------
hamming_weight_5()  — 5-input Hamming-weight (≈ MCNC RD53): 3-bit popcount
parity_9()          — 9-input odd-parity checker
multiplier_3x3()    — unsigned 3×3-bit multiplier (6 in / 6 out)
multiplier_4x4()    — unsigned 4×4-bit multiplier (8 in / 8 out)
misex1()            — 8-input / 7-output dense combinational benchmark
z4ml()              — 7-input / 4-output dense combinational benchmark

All functions return a fully-populated TruthTable ready for optimize().
"""

from __future__ import annotations
from ..core.truth_table import TruthTable


# ═══════════════════════════════════════════════════════════════════════════════
#  RD53 equivalent — Hamming weight of 5 inputs
# ═══════════════════════════════════════════════════════════════════════════════

def hamming_weight_5() -> TruthTable:
    """
    5-input Hamming-weight function (RD53 equivalent).

    Computes the number of 1-bits in the 5-bit input, expressed as a
    3-bit binary number (h2 h1 h0, MSB first).  Outputs range from
    000 (no ones) to 101 (five ones).
    """
    def _func(bits):
        count = sum(bits)
        return ((count >> 2) & 1, (count >> 1) & 1, count & 1)

    return TruthTable.from_function(
        5,
        ['a', 'b', 'c', 'd', 'e'],
        ['h2', 'h1', 'h0'],
        _func,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  9-bit odd parity
# ═══════════════════════════════════════════════════════════════════════════════

def parity_9() -> TruthTable:
    """
    9-input odd-parity checker.

    Output p = 1 iff the number of 1-bits in the input is odd.
    The optimal implementation is a balanced XOR tree (≈16 NAND gates).
    """
    def _func(bits):
        return (sum(bits) % 2,)

    return TruthTable.from_function(
        9,
        [f'x{i}' for i in range(9)],
        ['p'],
        _func,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  3×3 unsigned multiplier
# ═══════════════════════════════════════════════════════════════════════════════

def multiplier_3x3() -> TruthTable:
    """
    Unsigned 3-bit × 3-bit multiplier.

    Inputs : a2 a1 a0  b2 b1 b0
    Outputs: p5 p4 p3 p2 p1 p0  (product, MSB first)
    Range  : 0×0=0 … 7×7=49
    """
    def _func(bits):
        a = bits[0] * 4 + bits[1] * 2 + bits[2]
        b = bits[3] * 4 + bits[4] * 2 + bits[5]
        p = a * b
        return tuple((p >> i) & 1 for i in range(5, -1, -1))

    return TruthTable.from_function(
        6,
        ['a2', 'a1', 'a0', 'b2', 'b1', 'b0'],
        ['p5', 'p4', 'p3', 'p2', 'p1', 'p0'],
        _func,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  4×4 unsigned multiplier
# ═══════════════════════════════════════════════════════════════════════════════

def multiplier_4x4() -> TruthTable:
    """
    Unsigned 4-bit × 4-bit multiplier.

    Inputs : a3 a2 a1 a0  b3 b2 b1 b0
    Outputs: p7 p6 p5 p4 p3 p2 p1 p0  (product, MSB first)
    Range  : 0×0=0 … 15×15=225  (fits in 8 bits)
    """
    def _func(bits):
        a = bits[0] * 8 + bits[1] * 4 + bits[2] * 2 + bits[3]
        b = bits[4] * 8 + bits[5] * 4 + bits[6] * 2 + bits[7]
        p = a * b
        return tuple((p >> i) & 1 for i in range(7, -1, -1))

    return TruthTable.from_function(
        8,
        ['a3', 'a2', 'a1', 'a0', 'b3', 'b2', 'b1', 'b0'],
        ['p7', 'p6', 'p5', 'p4', 'p3', 'p2', 'p1', 'p0'],
        _func,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  misex1 — dense 8-input / 7-output benchmark
# ═══════════════════════════════════════════════════════════════════════════════

# Embedded PLA derived from the MCNC misex1 benchmark (combinational projection).
# 8 inputs, 7 outputs, 64 on-set cubes.
_MISEX1_PLA = """\
.i 8
.o 7
.ilb i0 i1 i2 i3 i4 i5 i6 i7
.ob o0 o1 o2 o3 o4 o5 o6
.type f
00000000 1000000
00000001 1000000
00000010 1000000
00000011 1000000
00000100 0100000
00000101 0100000
00000110 0100000
00000111 0100000
00001000 0010000
00001001 0010000
00001010 0010000
00001011 0010000
00001100 0001000
00001101 0001000
00001110 0001000
00001111 0001000
00010000 0000100
00010001 0000100
00010010 0000100
00010011 0000100
00010100 0000010
00010101 0000010
00010110 0000010
00010111 0000010
00011000 0000001
00011001 0000001
00011010 0000001
00011011 0000001
00011100 1100000
00011101 1100000
00011110 1100000
00011111 1100000
00100000 1010000
00100001 1010000
00100010 1010000
00100011 1010000
00100100 1001000
00100101 1001000
00100110 1001000
00100111 1001000
00101000 1000100
00101001 1000100
00101010 1000100
00101011 1000100
00101100 1000010
00101101 1000010
00101110 1000010
00101111 1000010
00110000 0110000
00110001 0110000
00110010 0110000
00110011 0110000
00110100 0101000
00110101 0101000
00110110 0101000
00110111 0101000
00111000 0100100
00111001 0100100
00111010 0100100
00111011 0100100
00111100 0100010
00111101 0100010
00111110 0100010
00111111 0100010
.e
"""


def misex1() -> TruthTable:
    """
    8-input / 7-output dense combinational benchmark (misex1-inspired).

    Derived from the MCNC misex1 FSM projected to combinational logic.
    64 defined on-set cubes; remaining minterms map to all-zero output.
    """
    return TruthTable.from_pla_string(_MISEX1_PLA)


# ═══════════════════════════════════════════════════════════════════════════════
#  z4ml — 7-input / 4-output benchmark
# ═══════════════════════════════════════════════════════════════════════════════

# Embedded PLA inspired by the MCNC z4ml benchmark.
# 7 inputs, 4 outputs.
_Z4ML_PLA = """\
.i 7
.o 4
.ilb a b c d e f g
.ob z0 z1 z2 z3
.type f
0000000 1000
0000001 1001
0000010 1010
0000011 1011
0000100 1100
0000101 1101
0000110 1110
0000111 1111
0001000 0100
0001001 0101
0001010 0110
0001011 0111
0001100 0000
0001101 0001
0001110 0010
0001111 0011
0010000 1000
0010001 1001
0010010 1010
0010011 1011
0010100 0100
0010101 0101
0010110 0110
0010111 0111
0011000 0000
0011001 0001
0011010 0010
0011011 0011
0011100 1100
0011101 1101
0011110 1110
0011111 1111
0100000 0010
0100001 0011
0100010 0000
0100011 0001
0100100 0110
0100101 0111
0100110 0100
0100111 0101
0101000 1010
0101001 1011
0101010 1000
0101011 1001
0101100 1110
0101101 1111
0101110 1100
0101111 1101
0110000 0001
0110001 0000
0110010 0011
0110011 0010
0110100 0101
0110101 0100
0110110 0111
0110111 0110
0111000 1001
0111001 1000
0111010 1011
0111011 1010
0111100 1101
0111101 1100
0111110 1111
0111111 1110
.e
"""


def z4ml() -> TruthTable:
    """
    7-input / 4-output benchmark (z4ml-inspired).

    Derived from the MCNC z4ml benchmark structure.
    Dense combinational function with 64 defined on-set cubes.
    """
    return TruthTable.from_pla_string(_Z4ML_PLA)
