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

_SEG7 = {
    0: 0b1111110, 1: 0b0110000, 2: 0b1101101, 3: 0b1111001,
    4: 0b0110011, 5: 0b1011011, 6: 0b1011111, 7: 0b1110000,
    8: 0b1111111, 9: 0b1111011,
}


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


def multi_7seg(n_displays: int = 2) -> TruthTable:
    """
    Binary number → N-digit 7-segment decoder (common-cathode, active-high).

    n_displays = 1  →  4-bit binary (0–9),    7 outputs
    n_displays = 2  →  7-bit binary (0–99),  14 outputs
    n_displays = 3  → 10-bit binary (0–999), 21 outputs

    Input names:   bin_{k}..bin_0      (k = n_inputs-1, MSB first)
    Output names:  dig{d}_6..dig{d}_0  (d = 0 is leftmost display;
                                        bit 6 = segment a, bit 0 = segment g)

    Values above 10**n_displays-1 are don't-cares.
    Use --bus with --circ to get bus-style Pins (BIN + DIG0, DIG1, …) in Logisim.
    """
    max_val  = 10 ** n_displays - 1
    n_inputs = max_val.bit_length()

    input_names  = [f'bin_{n_inputs - 1 - i}' for i in range(n_inputs)]
    output_names = [f'dig{d}_{6 - s}' for d in range(n_displays) for s in range(7)]

    rows: dict = {}
    for v in range(max_val + 1):
        digits = []
        tmp = v
        for _ in range(n_displays):
            digits.append(tmp % 10)
            tmp //= 10
        digits.reverse()            # most-significant digit first (display 0)
        out: list = []
        for d in digits:
            segs = _SEG7[d]
            out.extend((segs >> (6 - s)) & 1 for s in range(7))
        rows[v] = tuple(out)

    return TruthTable.from_dict(
        n_inputs     = n_inputs,
        input_names  = input_names,
        output_names = output_names,
        rows         = rows,
        dont_cares   = set(range(max_val + 1, 2 ** n_inputs)),
    )