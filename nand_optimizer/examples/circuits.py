"""
7-Segment Display Decoder truth table.

    Digit  X3 X2 X1 X0 │ a  b  c  d  e  f  g
      0     0  0  0  0  │ 1  1  1  1  1  1  0
      1     0  0  0  1  │ 0  1  1  0  0  0  0
      2     0  0  1  0  │ 1  1  0  1  1  0  1
      3     0  0  1  1  │ 1  1  1  1  0  0  1
      4     0  1  0  0  │ 0  1  1  0  0  1  1
      5     0  1  0  1  │ 1  0  1  1  0  1  1
      6     0  1  1  0  │ 1  0  1  1  1  1  1
      7     0  1  1  1  │ 1  1  1  0  0  0  0
      8     1  0  0  0  │ 1  1  1  1  1  1  1
      9     1  0  0  1  │ 1  1  1  1  0  1  1
    10-15                  x  x  x  x  x  x  x   (don't care)
"""

from ..core.truth_table import TruthTable


def seven_segment() -> TruthTable:
    """Standard BCD-to-7-segment decoder (common-anode, active-high)."""
    return TruthTable.from_dict(
        n_inputs     = 4,
        input_names  = ['x3', 'x2', 'x1', 'x0'],
        output_names = list('abcdefg'),
        rows = {
            0:  (1, 1, 1, 1, 1, 1, 0),
            1:  (0, 1, 1, 0, 0, 0, 0),
            2:  (1, 1, 0, 1, 1, 0, 1),
            3:  (1, 1, 1, 1, 0, 0, 1),
            4:  (0, 1, 1, 0, 0, 1, 1),
            5:  (1, 0, 1, 1, 0, 1, 1),
            6:  (1, 0, 1, 1, 1, 1, 1),
            7:  (1, 1, 1, 0, 0, 0, 0),
            8:  (1, 1, 1, 1, 1, 1, 1),
            9:  (1, 1, 1, 1, 0, 1, 1),
        },
        dont_cares = set(range(10, 16)),
    )


def two_bit_adder() -> TruthTable:
    """
    2-bit adder:  A(a1,a0) + B(b1,b0) = (Cout, S1, S0)

    4 inputs, 3 outputs.  No don't-cares.
    """
    def add_fn(bits):
        a1, a0, b1, b0 = bits
        a = (a1 << 1) | a0
        b = (b1 << 1) | b0
        s = a + b
        return ((s >> 2) & 1, (s >> 1) & 1, s & 1)

    return TruthTable.from_function(
        n_inputs     = 4,
        input_names  = ['a1', 'a0', 'b1', 'b0'],
        output_names = ['cout', 's1', 's0'],
        func         = add_fn,
    )


def bcd_to_excess3() -> TruthTable:
    """
    BCD to Excess-3 code converter.

    4 inputs (BCD digit 0-9), 4 outputs (Excess-3).
    Inputs 10-15 are don't-cares.
    """
    rows = {}
    for d in range(10):
        e3 = d + 3
        rows[d] = (
            (e3 >> 3) & 1,
            (e3 >> 2) & 1,
            (e3 >> 1) & 1,
            e3 & 1,
        )
    return TruthTable.from_dict(
        n_inputs     = 4,
        input_names  = ['b3', 'b2', 'b1', 'b0'],
        output_names = ['e3', 'e2', 'e1', 'e0'],
        rows         = rows,
        dont_cares   = set(range(10, 16)),
    )