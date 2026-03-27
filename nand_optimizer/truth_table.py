"""
Generic truth table for combinational logic.

A TruthTable describes an N-input, M-output logic function with
optional don't-care rows.  It is the single input to the optimiser
pipeline, so any combinational circuit can be optimised just by
constructing the right TruthTable.

Construction helpers:
  • from_dict     — { input_int: (out0, out1, …), … }
  • from_function — f(input_bits) → output_bits
"""

from __future__ import annotations
from typing import Callable, Dict, List, Optional, Set, Tuple


class TruthTable:
    """
    Parameters
    ----------
    n_inputs : int
        Number of input variables.
    input_names : list[str]
        Names for input variables (MSB first).  len == n_inputs.
    output_names : list[str]
        Names for output functions.
    rows : dict[int, tuple[int, ...]]
        Mapping minterm-index → output values.
        Each value tuple has len == len(output_names).
        Minterms not present (and not in *dont_cares*) default to 0.
    dont_cares : set[int]
        Input combinations whose output is don't-care.
    """

    def __init__(
        self,
        n_inputs:     int,
        input_names:  List[str],
        output_names: List[str],
        rows:         Dict[int, Tuple[int, ...]],
        dont_cares:   Optional[Set[int]] = None,
    ):
        assert len(input_names) == n_inputs
        width = len(output_names)
        for m, vals in rows.items():
            assert len(vals) == width, \
                f'Row {m}: expected {width} outputs, got {len(vals)}'
            assert 0 <= m < (1 << n_inputs), f'Minterm {m} out of range'

        self.n_inputs     = n_inputs
        self.input_names  = list(input_names)
        self.output_names = list(output_names)
        self.rows         = dict(rows)
        self.dont_cares   = set(dont_cares) if dont_cares else set()

    # ── per-output helpers ────────────────────────────────────────────────────

    @property
    def n_outputs(self) -> int:
        return len(self.output_names)

    def ones(self, output_idx: int) -> Set[int]:
        """Minterms where output *output_idx* is 1."""
        return {m for m, v in self.rows.items() if v[output_idx] == 1}

    def zeros(self, output_idx: int) -> Set[int]:
        """Minterms where output *output_idx* is 0."""
        all_defined = set(self.rows.keys())
        return {m for m in all_defined if self.rows[m][output_idx] == 0}

    def expected(self, minterm: int, output_idx: int) -> Optional[int]:
        """Return expected value, or None if don't-care / undefined."""
        if minterm in self.dont_cares:
            return None
        row = self.rows.get(minterm)
        if row is None:
            return 0   # undefined → 0 by default
        return row[output_idx]

    # ── pretty print ──────────────────────────────────────────────────────────

    def __str__(self) -> str:
        hdr_in  = ' '.join(f'{n:>3}' for n in self.input_names)
        hdr_out = ' '.join(f'{n:>3}' for n in self.output_names)
        lines   = [f'  {"#":>3}  {hdr_in} | {hdr_out}']
        lines.append('  ' + '─' * (len(lines[0]) - 2))

        for m in range(1 << self.n_inputs):
            bits = '  '.join(
                str((m >> (self.n_inputs - 1 - i)) & 1)
                for i in range(self.n_inputs)
            )
            if m in self.dont_cares:
                vals = '  '.join('x' for _ in self.output_names)
            elif m in self.rows:
                vals = '  '.join(str(v) for v in self.rows[m])
            else:
                continue  # skip absent rows
            lines.append(f'  {m:>3}  {bits} | {vals}')
        return '\n'.join(lines)

    def __repr__(self) -> str:
        return (f'TruthTable({self.n_inputs} inputs, '
                f'{self.n_outputs} outputs, '
                f'{len(self.rows)} rows, '
                f'{len(self.dont_cares)} don\'t-cares)')

    # ── factory helpers ───────────────────────────────────────────────────────

    @classmethod
    def from_dict(
        cls,
        n_inputs:     int,
        input_names:  List[str],
        output_names: List[str],
        rows:         Dict[int, Tuple[int, ...]],
        dont_cares:   Optional[Set[int]] = None,
    ) -> TruthTable:
        """Construct from a plain dictionary."""
        return cls(n_inputs, input_names, output_names, rows, dont_cares)

    @classmethod
    def from_function(
        cls,
        n_inputs:     int,
        input_names:  List[str],
        output_names: List[str],
        func:         Callable[[Tuple[int, ...]], Tuple[int, ...]],
        dont_cares:   Optional[Set[int]] = None,
    ) -> TruthTable:
        """
        Build from a Python function that maps input-bit-tuple → output-bit-tuple.

        *func* is called for every non-don't-care input combination.
        """
        dc = dont_cares or set()
        rows: Dict[int, Tuple[int, ...]] = {}
        for m in range(1 << n_inputs):
            if m in dc:
                continue
            bits = tuple((m >> (n_inputs - 1 - i)) & 1 for i in range(n_inputs))
            rows[m] = func(bits)
        return cls(n_inputs, input_names, output_names, rows, dc)