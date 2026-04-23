"""
Regression runner for the MCNC-style benchmark suite.

    from nand_optimizer.benchmark_runner import run_benchmarks
    run_benchmarks()                  # all benchmarks
    run_benchmarks(['rd53', 'z4ml'])  # a subset

Each benchmark is optimised, miter-verified (z3 or exhaustive
fallback), and profiled.  A summary table reports:

    benchmark | inputs | outputs | NAND gates | verify | wall (s)
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple

from ..core.truth_table import TruthTable
from ..pipeline         import optimize
from ..verify           import miter_verify
from .profile           import ProfileReport

from ..examples.benchmarks import (
    hamming_weight_5,
    parity_9,
    multiplier_3x3,
    multiplier_4x4,
    misex1,
    z4ml,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Benchmark registry
# ═══════════════════════════════════════════════════════════════════════════════

BENCHMARKS: Dict[str, Tuple[str, callable]] = {
    'rd53':    ('RD53 (5-bit popcount)',   hamming_weight_5),
    'parity9': ('9-bit parity',            parity_9),
    'mult3':   ('3x3 multiplier',          multiplier_3x3),
    'mult4':   ('4x4 multiplier',          multiplier_4x4),
    'misex1':  ('misex1 (8 in / 7 out)',   misex1),
    'z4ml':    ('z4ml (7 in / 4 out)',     z4ml),
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_one_benchmark(key: str,
                      verbose: bool = False,
                      verify:  bool = True,
                      profile: bool = True) -> Dict:
    """Run a single benchmark and return a result row."""
    if key not in BENCHMARKS:
        raise KeyError(f'Unknown benchmark {key!r}. '
                       f'Available: {", ".join(BENCHMARKS)}')

    label, factory = BENCHMARKS[key]
    tt = factory()

    result = optimize(tt, verbose=verbose, profile=profile)

    row: Dict = {
        'key':        key,
        'label':      label,
        'n_inputs':   tt.n_inputs,
        'n_outputs':  tt.n_outputs,
        'n_nand':     result.total_nand,
        'verify':     None,
        'verify_method': None,
        'seconds':    result.profile.total_seconds if result.profile else None,
    }

    if verify:
        v = miter_verify(tt, result)
        row['verify']        = v['equivalent']
        row['verify_method'] = v['method']
        if v['equivalent'] is False:
            row['counterexample'] = v['counterexample']

    return row


def run_benchmarks(keys: Optional[List[str]] = None,
                   verbose: bool = False,
                   verify:  bool = True,
                   profile: bool = True) -> List[Dict]:
    """
    Run a set of MCNC-style benchmarks and pretty-print the regression table.

    Parameters
    ----------
    keys : list[str] | None
        Subset of benchmark keys to run.  ``None`` means run everything.
    verbose : bool
        Forward to ``optimize()``.  Usually False for bulk regression runs.
    verify : bool
        If True, run miter verification after synthesis.
    profile : bool
        If True, collect per-pass timings via ``ProfileReport``.

    Returns
    -------
    list[dict]
        One row per benchmark.
    """
    keys = keys or list(BENCHMARKS)
    rows: List[Dict] = []

    bar = '=' * 78
    print(f'\n{bar}')
    print('  MCNC REGRESSION SUITE')
    print(bar)

    for k in keys:
        label, _ = BENCHMARKS[k]
        print(f'\n  >> Running {k} ({label}) ...')
        row = run_one_benchmark(k, verbose=verbose,
                                verify=verify, profile=profile)
        rows.append(row)

        verdict = _verdict(row)
        extra = ''
        if row.get('seconds') is not None:
            extra = f'  [{row["seconds"]:.2f}s]'
        print(f'     NAND gates = {row["n_nand"]:4d}   {verdict}{extra}')

    _print_table(rows)
    return rows


def _verdict(row: Dict) -> str:
    if row.get('verify') is True:
        return f'verified ({row.get("verify_method", "?")})'
    if row.get('verify') is False:
        return f'MISMATCH ({row.get("verify_method", "?")})'
    if row.get('verify') is None and 'verify_method' in row:
        return f'unknown ({row.get("verify_method", "?")})'
    return 'not verified'


def _print_table(rows: List[Dict]) -> None:
    bar = '-' * 78
    print(f'\n{bar}')
    print('  SUMMARY')
    print(bar)
    hdr = (f'  {"Benchmark":<24}{"in":>4} {"out":>4} {"NAND":>6} '
           f'{"verify":>12} {"time (s)":>10}')
    print(hdr)
    print('  ' + '-' * 76)
    n_fail = 0
    for r in rows:
        t = f'{r["seconds"]:10.3f}' if r.get('seconds') is not None else f'{"—":>10}'
        v = _verdict(r)
        if r.get('verify') is False:
            n_fail += 1
        print(f'  {r["label"]:<24}{r["n_inputs"]:>4} {r["n_outputs"]:>4} '
              f'{r["n_nand"]:>6} {v:>12} {t}')
    print(bar)
    total_nand = sum(r['n_nand'] for r in rows)
    total_time = sum((r['seconds'] or 0.0) for r in rows)
    print(f'  {"TOTAL":<24}{"":>4} {"":>4} {total_nand:>6} '
          f'{n_fail} mismatch  {total_time:10.3f}')
    print(bar)
