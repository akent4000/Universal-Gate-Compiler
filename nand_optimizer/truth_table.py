"""
Generic truth table for combinational logic.

Primary storage is a *cube cover*: a list of (input_cube, output_vals) pairs
where each input_cube is a ternary tuple (0 / 1 / DASH=-1) of length n_inputs
and output_vals is a 0/1 tuple of length n_outputs.

For backward compatibility, the integer-keyed `rows` dict and `dont_cares` set
are also populated when n_inputs <= 20 (so tests and verification code that
iterate over minterms still work).

Construction helpers:
  from_dict     — { input_int: (out0, out1, ...), ... }
  from_function — f(input_bits) -> output_bits
  from_pla      — Berkeley PLA file path
  from_pla_string — PLA content string
"""

from __future__ import annotations
from typing import Callable, Dict, List, Optional, Set, Tuple
import os

DASH = -1   # don't-care position in a ternary input cube

# Maximum n for which minterms are expanded into rows (backward compat).
_MAX_EXPAND_N = 20


def _int_to_bits(n: int, width: int) -> Tuple[int, ...]:
    return tuple((n >> (width - 1 - i)) & 1 for i in range(width))


def _expand_cube_to_ints(cube: Tuple[int, ...], n_vars: int) -> List[int]:
    """Enumerate all minterm integers covered by a ternary cube."""
    minterms = [0]
    for i, b in enumerate(cube):
        bit_pos = n_vars - 1 - i
        if b == DASH:
            minterms = minterms + [m | (1 << bit_pos) for m in minterms]
        elif b == 1:
            minterms = [m | (1 << bit_pos) for m in minterms]
    return minterms


class TruthTable:
    """
    Parameters
    ----------
    n_inputs : int
        Number of input variables.
    input_names : list[str]
        Names for input variables (MSB first).
    output_names : list[str]
        Names for output functions.
    cube_cover : list of (input_cube, output_vals)
        Primary representation.  input_cube is a ternary Tuple[int,...] of
        length n_inputs (values 0, 1, or DASH=-1).  output_vals is a
        Tuple[int,...] of 0/1 values of length n_outputs.
        Only on-set entries are stored; unlisted inputs default to output 0.
    dc_cube_list : list of input cubes that are global don't-cares.
    rows : dict[int, tuple]
        Minterm-keyed output values (backward compat; populated for n<=20).
    dont_cares : set[int]
        Don't-care minterm indices (backward compat; populated for n<=20).
    """

    def __init__(
        self,
        n_inputs:     int,
        input_names:  List[str],
        output_names: List[str],
        cube_cover:   List[Tuple[Tuple[int, ...], Tuple[int, ...]]],
        dc_cube_list: Optional[List[Tuple[int, ...]]] = None,
        rows:         Optional[Dict[int, Tuple[int, ...]]] = None,
        dont_cares:   Optional[Set[int]] = None,
    ):
        assert len(input_names) == n_inputs
        self.n_inputs     = n_inputs
        self.input_names  = list(input_names)
        self.output_names = list(output_names)
        self.cube_cover   = list(cube_cover)
        self.dc_cube_list = list(dc_cube_list) if dc_cube_list else []
        self.rows         = dict(rows) if rows else {}
        self.dont_cares   = set(dont_cares) if dont_cares else set()

    # ── per-output helpers ────────────────────────────────────────────────────

    @property
    def n_outputs(self) -> int:
        return len(self.output_names)

    def ones_cubes(self, output_idx: int) -> List[Tuple[int, ...]]:
        """Input cubes where output *output_idx* is 1."""
        return [cube for cube, vals in self.cube_cover if vals[output_idx] == 1]

    @property
    def dc_cubes(self) -> List[Tuple[int, ...]]:
        """Global don't-care input cubes."""
        return list(self.dc_cube_list)

    def ones(self, output_idx: int) -> Set[int]:
        """Minterms where output *output_idx* is 1 (backward compat)."""
        if self.rows:
            return {m for m, v in self.rows.items() if v[output_idx] == 1}
        if self.n_inputs > _MAX_EXPAND_N:
            return set()
        from .implicant import _expand_cubes_to_set
        return _expand_cubes_to_set(self.ones_cubes(output_idx), self.n_inputs)

    def zeros(self, output_idx: int) -> Set[int]:
        """Minterms where output *output_idx* is 0 (backward compat)."""
        if self.rows:
            return {m for m, v in self.rows.items() if v[output_idx] == 0}
        return set()

    def expected(self, minterm: int, output_idx: int) -> Optional[int]:
        """Return expected value, or None if don't-care / undefined."""
        if minterm in self.dont_cares:
            return None
        row = self.rows.get(minterm)
        if row is None:
            return 0
        return row[output_idx]

    # ── pretty print ──────────────────────────────────────────────────────────

    def __str__(self) -> str:
        if self.n_inputs > _MAX_EXPAND_N:
            return (f'TruthTable({self.n_inputs} inputs, {self.n_outputs} outputs, '
                    f'{len(self.cube_cover)} cubes — too large to display)')
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
                continue
            lines.append(f'  {m:>3}  {bits} | {vals}')
        return '\n'.join(lines)

    def __repr__(self) -> str:
        return (f'TruthTable({self.n_inputs} inputs, '
                f'{self.n_outputs} outputs, '
                f'{len(self.cube_cover)} on-cubes, '
                f'{len(self.dc_cube_list)} dc-cubes)')

    # ── factory helpers ───────────────────────────────────────────────────────

    @classmethod
    def from_dict(
        cls,
        n_inputs:     int,
        input_names:  List[str],
        output_names: List[str],
        rows:         Dict[int, Tuple[int, ...]],
        dont_cares:   Optional[Set[int]] = None,
    ) -> 'TruthTable':
        """Construct from a plain minterm dictionary."""
        dc = set(dont_cares) if dont_cares else set()
        # Build cube_cover: one unit cube per minterm
        cube_cover = [
            (_int_to_bits(m, n_inputs), vals)
            for m, vals in sorted(rows.items())
            if m not in dc
        ]
        dc_cube_list = [_int_to_bits(m, n_inputs) for m in sorted(dc)]
        return cls(n_inputs, input_names, output_names,
                   cube_cover, dc_cube_list, rows, dc)

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

        Cubes are stored directly — no 2^N minterm expansion.
        The integer-keyed `rows` dict is populated only when n_inputs <= 20.

        Supported directives:
          .i N      — number of inputs
          .o M      — number of outputs
          .ilb ...  — input variable names
          .ob  ...  — output function names
          .p  K     — product term count (ignored)
          .type X   — on-set type: 'f' (default) or 'r'
          .e / .end — end marker

        Input cube characters:  '0' -> 0,  '1' -> 1,  '-'/'~' -> DASH
        Output cube characters: '1' -> on-set,  '-' -> dc,  '0' -> off-set
        """
        n_inputs  = 0
        n_outputs = 0
        in_names:  Optional[List[str]] = input_names
        out_names: Optional[List[str]] = output_names
        pla_type  = 'f'

        raw_cubes: List[Tuple[str, str]] = []   # (inp_pat, out_pat)

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
                if len(tok) < 2:
                    continue
                if n_outputs == 0:
                    n_outputs = len(tok[1])
                raw_cubes.append((tok[0], tok[1]))

        if not in_names:
            in_names = [f'x{i}' for i in range(n_inputs)]
        if not out_names:
            out_names = [f'y{j}' for j in range(n_outputs)]

        # ── parse cubes into ternary representation ───────────────────────────
        def _parse_in(pat: str) -> Tuple[int, ...]:
            return tuple(0 if ch == '0' else 1 if ch == '1' else DASH
                         for ch in pat)

        cube_cover:   List[Tuple[Tuple[int, ...], Tuple[int, ...]]] = []
        dc_cube_list: List[Tuple[int, ...]]                         = []

        for inp_pat, out_pat in raw_cubes:
            input_cube = _parse_in(inp_pat)
            if all(ch == '-' for ch in out_pat):
                dc_cube_list.append(input_cube)
            else:
                output_vals = tuple(1 if ch == '1' else 0 for ch in out_pat)
                cube_cover.append((input_cube, output_vals))

        # ── handle 'r' (off-set) type ─────────────────────────────────────────
        if 'r' in pla_type:
            if n_inputs > _MAX_EXPAND_N:
                raise ValueError(
                    f"'r'-type PLA with {n_inputs} inputs > {_MAX_EXPAND_N} "
                    "is not supported (would require cube complement)."
                )
            # Expand off-set cubes to minterms, compute complement
            dc_set: Set[int] = set()
            for cube in dc_cube_list:
                for m in _expand_cube_to_ints(cube, n_inputs):
                    dc_set.add(m)

            off_per: List[Set[int]] = [set() for _ in range(n_outputs)]
            for input_cube, output_vals in cube_cover:
                for m in _expand_cube_to_ints(input_cube, n_inputs):
                    for j, v in enumerate(output_vals):
                        if v == 1:
                            off_per[j].add(m)

            all_m = set(range(1 << n_inputs))
            on_per = [all_m - off_per[j] - dc_set for j in range(n_outputs)]

            all_on: Set[int] = set()
            for s in on_per:
                all_on |= s

            cube_cover = []
            for m in sorted(all_on - dc_set):
                output_vals = tuple(1 if m in on_per[j] else 0
                                    for j in range(n_outputs))
                if any(v == 1 for v in output_vals):
                    cube_cover.append((_int_to_bits(m, n_inputs), output_vals))

            dc_cube_list = [_int_to_bits(m, n_inputs) for m in sorted(dc_set)]

        # ── build rows / dont_cares for small n (backward compat) ────────────
        rows:       Dict[int, Tuple[int, ...]] = {}
        dont_cares: Set[int]                   = set()

        if n_inputs <= _MAX_EXPAND_N:
            for cube in dc_cube_list:
                for m in _expand_cube_to_ints(cube, n_inputs):
                    dont_cares.add(m)

            on_bits: List[Set[int]] = [set() for _ in range(n_outputs)]
            for input_cube, output_vals in cube_cover:
                for m in _expand_cube_to_ints(input_cube, n_inputs):
                    if m not in dont_cares:
                        for j, v in enumerate(output_vals):
                            if v == 1:
                                on_bits[j].add(m)

            all_m = set(range(1 << n_inputs))
            for m in all_m - dont_cares:
                rows[m] = tuple(1 if m in on_bits[j] else 0
                                for j in range(n_outputs))

        return cls(n_inputs, list(in_names), list(out_names),
                   cube_cover, dc_cube_list, rows, dont_cares)

    @classmethod
    def from_function(
        cls,
        n_inputs:     int,
        input_names:  List[str],
        output_names: List[str],
        func:         Callable[[Tuple[int, ...]], Tuple[int, ...]],
        dont_cares:   Optional[Set[int]] = None,
    ) -> 'TruthTable':
        """Build from a Python function mapping input-bit-tuple -> output-bit-tuple."""
        dc = dont_cares or set()
        rows: Dict[int, Tuple[int, ...]] = {}
        for m in range(1 << n_inputs):
            if m in dc:
                continue
            bits = _int_to_bits(m, n_inputs)
            rows[m] = func(bits)
        return cls.from_dict(n_inputs, input_names, output_names, rows, dc)
