"""
Automatic hierarchical decomposition spec generator.

Given a flat truth table whose outputs can be split into two equal-size groups
that realise the SAME combinational function through different intermediate
signals, this module:

  1. Detects the symmetric output groups by checking that both groups produce
     the same set of distinct output-column patterns across all care minterms.
  2. Assigns binary codes to the distinct patterns (the "intermediate" state).
  3. Generates two PLA strings:
       <stem>_intermediate.pla   (n_inputs  → 2*k outputs: k bits per group)
       <stem>_decoder.pla        (k inputs  → group_width outputs)
  4. Returns a composition-spec dict (JSON-compatible) that wires the stages
     together so hierarchical_optimize() can synthesize them.

Usage:
    result = auto_generate_spec(tt, stem="my_circuit", output_dir="/tmp")
    if result:
        spec_dict     = result['spec']
        inter_file    = result['intermediate_pla']   # (filename, pla_string)
        dec_file      = result['decoder_pla']        # (filename, pla_string)
"""

from __future__ import annotations

import itertools
import math
import re
from typing import Dict, List, Optional, Set, Tuple

from .core.truth_table import TruthTable


# ── helpers ──────────────────────────────────────────────────────────────────

def _output_pattern(
    tt: TruthTable,
    group_indices: List[int],
    minterm: int,
) -> Optional[Tuple[int, ...]]:
    """
    Return the output tuple for `group_indices` at `minterm`,
    or None if any output is a don't-care there.
    """
    pat = []
    for idx in group_indices:
        v = tt.expected(minterm, idx)
        if v is None:
            return None
        pat.append(v)
    return tuple(pat)


def _group_patterns(
    tt: TruthTable,
    group_indices: List[int],
) -> Dict[int, Optional[Tuple[int, ...]]]:
    """Map each minterm to its output pattern for the given group."""
    return {m: _output_pattern(tt, group_indices, m) for m in range(1 << tt.n_inputs)}


def _distinct_care_patterns(
    patterns: Dict[int, Optional[Tuple[int, ...]]],
) -> Set[Tuple[int, ...]]:
    return {v for v in patterns.values() if v is not None}


# ── symmetric-group detection ─────────────────────────────────────────────────

def _prefix_of(name: str) -> str:
    """Extract the alphabetic prefix before the first digit or trailing separator."""
    m = re.match(r'^([A-Za-z]+[_]?)', name)
    return m.group(1) if m else ''


def _find_symmetric_groups(
    tt: TruthTable,
) -> Optional[Tuple[List[int], List[int], Set[Tuple[int, ...]]]]:
    """
    Find two equal-size groups of outputs with identical sets of distinct
    care-minterm patterns.

    Search order:
      1. First half vs second half of output list.
      2. Grouping by common name prefix (e.g. "t_" vs "u_").
      3. Exhaustive C(m, m/2) search for m ≤ 16.

    Returns (g1_indices, g2_indices, distinct_patterns) or None.
    """
    m = tt.n_outputs
    if m < 2 or m % 2 != 0:
        return None
    half = m // 2

    def _try(g1: List[int], g2: List[int]) -> Optional[Set[Tuple[int, ...]]]:
        p1 = _group_patterns(tt, g1)
        p2 = _group_patterns(tt, g2)
        d1 = _distinct_care_patterns(p1)
        d2 = _distinct_care_patterns(p2)
        return d1 if d1 == d2 else None

    # Heuristic 1: positional split
    g1, g2 = list(range(half)), list(range(half, m))
    d = _try(g1, g2)
    if d is not None:
        return g1, g2, d

    # Heuristic 2: name-prefix grouping
    by_prefix: Dict[str, List[int]] = {}
    for i, name in enumerate(tt.output_names):
        by_prefix.setdefault(_prefix_of(name), []).append(i)
    prefix_groups = [v for v in by_prefix.values() if len(v) == half]
    if len(prefix_groups) == 2:
        g1, g2 = prefix_groups[0], prefix_groups[1]
        d = _try(g1, g2)
        if d is not None:
            return g1, g2, d

    # Heuristic 3: exhaustive (bounded)
    if m > 16:
        return None
    all_idx = list(range(m))
    for combo in itertools.combinations(all_idx, half):
        g1 = list(combo)
        g2 = [i for i in all_idx if i not in set(g1)]
        d = _try(g1, g2)
        if d is not None:
            return g1, g2, d

    return None


# ── code assignment ───────────────────────────────────────────────────────────

def _assign_codes_by_first_appearance(
    patterns_by_minterm: Dict[int, Optional[Tuple[int, ...]]],
    n_inputs: int,
) -> Tuple[List[Tuple[int, ...]], Dict[Tuple[int, ...], int], int]:
    """
    Assign binary codes in order of first minterm appearance.

    Walking minterms 0, 1, 2, … the first time a new distinct pattern is seen
    it gets the next code.  For a BCD-structured function this naturally
    produces the natural binary (BCD) encoding of the digit values, which
    is far more compressible than any lexicographic ordering of patterns.
    """
    seen: Dict[Tuple[int, ...], int] = {}
    ordered: List[Tuple[int, ...]] = []
    for m in range(1 << n_inputs):
        p = patterns_by_minterm.get(m)
        if p is not None and p not in seen:
            seen[p] = len(ordered)
            ordered.append(p)
    k = max(1, math.ceil(math.log2(max(len(ordered), 2))))
    return ordered, seen, k


def _int_to_bits(v: int, k: int) -> List[int]:
    """Return k-bit big-endian (MSB first) representation of v."""
    return [(v >> (k - 1 - i)) & 1 for i in range(k)]


# ── PLA generators ────────────────────────────────────────────────────────────

def _intermediate_pla(
    tt: TruthTable,
    g1_idx: List[int],
    g2_idx: List[int],
    patterns1: Dict[int, Optional[Tuple[int, ...]]],
    patterns2: Dict[int, Optional[Tuple[int, ...]]],
    code_of: Dict[Tuple[int, ...], int],
    k: int,
    g1_prefix: str,
    g2_prefix: str,
) -> str:
    """PLA: original inputs → 2k-bit intermediate codes (k bits per group, MSB first)."""
    n = tt.n_inputs
    # Output names: g1_{k-1}..g1_0  g2_{k-1}..g2_0  (MSB listed first)
    g1_names = [f'{g1_prefix}{k - 1 - i}' for i in range(k)]
    g2_names = [f'{g2_prefix}{k - 1 - i}' for i in range(k)]

    lines = [
        f'.i {n}',
        f'.o {2 * k}',
        f'.ilb {" ".join(tt.input_names)}',
        f'.ob {" ".join(g1_names + g2_names)}',
    ]

    rows = []
    for m in range(1 << n):
        in_str = ''.join(str((m >> (n - 1 - i)) & 1) for i in range(n))
        p1, p2 = patterns1[m], patterns2[m]
        if p1 is None or p2 is None:
            rows.append(f'{in_str} {"-" * (2 * k)}')
        else:
            out_str = (''.join(str(b) for b in _int_to_bits(code_of[p1], k)) +
                       ''.join(str(b) for b in _int_to_bits(code_of[p2], k)))
            rows.append(f'{in_str} {out_str}')

    lines.append(f'.p {len(rows)}')
    lines.extend(rows)
    lines.append('.e')
    return '\n'.join(lines)


def _decoder_pla(
    patterns: List[Tuple[int, ...]],
    out_names: List[str],
    k: int,
) -> str:
    """PLA: k-bit code → output group (unused codes are don't-cares)."""
    width = len(patterns[0])
    in_names = [f'd{k - 1 - i}' for i in range(k)]  # MSB first: d_{k-1}..d_0

    lines = [
        f'.i {k}',
        f'.o {width}',
        f'.ilb {" ".join(in_names)}',
        f'.ob {" ".join(out_names)}',
    ]

    rows = []
    for code in range(1 << k):
        in_str = ''.join(str(b) for b in _int_to_bits(code, k))
        if code < len(patterns):
            out_str = ''.join(str(b) for b in patterns[code])
        else:
            out_str = '-' * width
        rows.append(f'{in_str} {out_str}')

    lines.append(f'.p {len(rows)}')
    lines.extend(rows)
    lines.append('.e')
    return '\n'.join(lines)


# ── public API ────────────────────────────────────────────────────────────────

def auto_generate_spec(
    tt: TruthTable,
    stem: str = 'circuit',
) -> Optional[dict]:
    """
    Try to find a 2-stage hierarchical decomposition of `tt`.

    Returns a dict with keys:
      'spec'             — composition spec for hierarchical_optimize() / --compose
      'intermediate_pla' — (filename, pla_string) for the intermediate stage
      'decoder_pla'      — (filename, pla_string) for the shared decoder

    Returns None if no decomposition could be found (either the function has
    no symmetric output groups, or n_inputs > 20).
    """
    if tt.n_inputs > 20:
        return None

    found = _find_symmetric_groups(tt)
    if found is None:
        return None

    g1_idx, g2_idx, distinct = found
    g1_names = [tt.output_names[i] for i in g1_idx]
    g2_names = [tt.output_names[i] for i in g2_idx]

    g1_prefix = 'g1_'
    g2_prefix = 'g2_'

    p1 = _group_patterns(tt, g1_idx)
    p2 = _group_patterns(tt, g2_idx)

    # Assign codes in minterm order so the intermediate function maps to the
    # natural binary representation of the "digit" (BCD-like encoding).
    # Group-1 patterns determine the code assignment (group-2 uses the same decoder).
    patterns, code_of, k = _assign_codes_by_first_appearance(p1, tt.n_inputs)

    inter_str = _intermediate_pla(tt, g1_idx, g2_idx, p1, p2,
                                   code_of, k, g1_prefix, g2_prefix)

    dec_out_names = [f'seg{i}' for i in range(len(g1_names))]
    dec_str = _decoder_pla(patterns, dec_out_names, k)

    inter_file = f'{stem}_intermediate.pla'
    dec_file   = f'{stem}_decoder.pla'

    # Intermediate output name lists (MSB first, same order as PLA .ob)
    g1_inter_names = [f'{g1_prefix}{k - 1 - i}' for i in range(k)]
    g2_inter_names = [f'{g2_prefix}{k - 1 - i}' for i in range(k)]
    dec_in_names   = [f'd{k - 1 - i}' for i in range(k)]

    spec = {
        'stages': [
            {
                'id': 'intermediate',
                'pla': inter_file,
            },
            {
                'id': 'group1',
                'pla': dec_file,
                'connect': {dec_in_names[i]: g1_inter_names[i] for i in range(k)},
                'rename':  {dec_out_names[i]: g1_names[i]       for i in range(len(g1_names))},
            },
            {
                'id': 'group2',
                'pla': dec_file,
                'connect': {dec_in_names[i]: g2_inter_names[i] for i in range(k)},
                'rename':  {dec_out_names[i]: g2_names[i]       for i in range(len(g2_names))},
            },
        ]
    }

    return {
        'spec':             spec,
        'intermediate_pla': (inter_file, inter_str),
        'decoder_pla':      (dec_file,   dec_str),
        'k':                k,
        'n_patterns':       len(patterns),
        'group1_names':     g1_names,
        'group2_names':     g2_names,
    }
