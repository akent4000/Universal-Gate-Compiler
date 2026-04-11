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
import os


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
    def from_pla(
        cls,
        filepath: str,
        input_names:  Optional[List[str]] = None,
        output_names: Optional[List[str]] = None,
    ) -> 'TruthTable':
        """Load a truth table from a Berkeley PLA file on disk."""
        with open(filepath, 'r') as fh:
            content = fh.read()
        return cls.from_pla_string(content, input_names, output_names)

    @classmethod
    def from_pla_string(
        cls,
        content: str,
        input_names:  Optional[List[str]] = None,
        output_names: Optional[List[str]] = None,
    ) -> 'TruthTable':
        """
        Parse a Berkeley PLA string into a TruthTable.

        Supported directives:
          .i N      — number of inputs
          .o M      — number of outputs
          .ilb ...  — input variable names (space-separated)
          .ob  ...  — output function names (space-separated)
          .p  K     — product term count (informational, ignored)
          .type X   — on-set type: 'f' (default), 'r', 'd', or combinations
          .e / .end — end marker

        Input cube characters:
          '0' → bit is 0, '1' → bit is 1, '-' or '~' → wildcard

        Output cube characters (per output column):
          '1' → this implicant contributes to the output's on-set
          '-' → output is don't-care for inputs covered by this cube
          '0' → this implicant does NOT contribute (off-set or simply absent)

        Type 'f' (most common, default):
          Listed cubes define the ON-set.  Unlisted minterms → output 0.
        Type 'r':
          Listed cubes define the OFF-set.  Unlisted minterms → output 1.
        """
        n_inputs  = 0
        n_outputs = 0
        in_names:  Optional[List[str]] = input_names
        out_names: Optional[List[str]] = output_names
        pla_type  = 'f'

        # per-output accumulation sets
        on_bits: List[Set[int]]  = []   # on_bits[j]  = minterms where output j = 1
        dc_bits: List[Set[int]]  = []   # dc_bits[j]  = minterms where output j = dc
        def _init_sets(n: int) -> None:
            nonlocal on_bits, dc_bits
            on_bits  = [set() for _ in range(n)]
            dc_bits  = [set() for _ in range(n)]

        def _expand(pat: str) -> List[int]:
            """Enumerate all minterms matched by an input cube pattern."""
            acc = [0]
            for ch in pat:
                if ch == '1':
                    acc = [(m << 1) | 1 for m in acc]
                elif ch == '0':
                    acc = [m << 1 for m in acc]
                else:                        # '-' or '~'
                    acc = [m << 1 for m in acc] + [(m << 1) | 1 for m in acc]
            return acc

        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            tok = line.split()
            key = tok[0].lower()

            if key == '.i':
                n_inputs = int(tok[1])
            elif key == '.o':
                n_outputs = int(tok[1])
                _init_sets(n_outputs)
            elif key == '.ilb':
                in_names = tok[1:]
            elif key in ('.ob', '.olb'):
                out_names = tok[1:]
            elif key == '.p':
                pass
            elif key == '.type':
                pla_type = tok[1].lower() if len(tok) > 1 else 'f'
            elif key in ('.e', '.end'):
                break
            elif not line.startswith('.'):
                # product term row: "<input_pat> <output_pat>"
                if len(tok) < 2:
                    continue
                inp_pat, out_pat = tok[0], tok[1]
                if not on_bits:           # .o may come after first cube
                    n_outputs = len(out_pat)
                    _init_sets(n_outputs)
                covered = _expand(inp_pat)
                for j, ch in enumerate(out_pat):
                    if ch == '1':
                        on_bits[j].update(covered)
                    elif ch == '-':
                        dc_bits[j].update(covered)
                    # '0' → intentionally not in on-set; nothing to record

        # ── default names if absent ───────────────────────────────────────────
        if not in_names:
            in_names = [f'x{i}' for i in range(n_inputs)]
        if not out_names:
            out_names = [f'y{j}' for j in range(n_outputs)]

        # ── invert sets for OFF-set type ──────────────────────────────────────
        all_minterms: Set[int] = set(range(1 << n_inputs))
        if 'r' in pla_type:
            # listed cubes = off-set; on-set is the complement
            for j in range(n_outputs):
                on_bits[j] = all_minterms - on_bits[j] - dc_bits[j]

        # ── global don't-care = minterms that are dc for EVERY output ─────────
        if n_outputs > 0:
            global_dc: Set[int] = set(dc_bits[0])
            for j in range(1, n_outputs):
                global_dc &= dc_bits[j]
        else:
            global_dc = set()

        # ── build row dict ────────────────────────────────────────────────────
        rows: Dict[int, Tuple[int, ...]] = {}
        for m in all_minterms:
            if m in global_dc:
                continue
            rows[m] = tuple(1 if m in on_bits[j] else 0 for j in range(n_outputs))

        return cls(n_inputs, list(in_names), list(out_names), rows, global_dc)

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