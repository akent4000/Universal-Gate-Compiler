#!/usr/bin/env python3
"""
Pass QoR evaluation harness — ROADMAP P2#7.

For each experimental pass (``XAG -x``, ``+bdd``, ``+resub``, ``bandit``) run
the default script and the with-pass variant on an EPFL subset and tabulate
``(benchmark, baseline_area, with_pass_area, delta_%, wall_time_delta_%)``.

Usage::

    python3 benchmarks/run_pass_eval.py                      # default subset
    python3 benchmarks/run_pass_eval.py --subset ctrl,adder  # custom keys
    python3 benchmarks/run_pass_eval.py --quick              # 3 smallest
    python3 benchmarks/run_pass_eval.py --out pass_eval.md   # custom output

The output Markdown follows the ``benchmarks/perf_baseline.md`` convention.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from nand_optimizer.io.aiger_io import read_aiger
from nand_optimizer.script import run_bandit, run_script

EPFL_ROOT = REPO_ROOT / 'benchmarks' / 'epfl'


# Default subset — covers arithmetic (XOR-heavy) and control (AND-heavy), sizes
# from 174 to 5416 ANDs. Adjust with --subset or --quick.
DEFAULT_SUBSET = [
    'random_control/ctrl',         #  174 ANDs
    'random_control/router',       #  257
    'random_control/int2float',    #  260
    'random_control/dec',          #  304
    'random_control/cavlc',        #  693
    'random_control/priority',     #  978
    'arithmetic/adder',            # 1020   (XOR-heavy)
    'random_control/i2c',          # 1342
    'arithmetic/max',              # 2865
    'arithmetic/bar',              # 3336   (barrel shifter)
    'arithmetic/sin',              # 5416   (XOR-heavy)
]

QUICK_SUBSET = [
    'random_control/ctrl',
    'random_control/router',
    'arithmetic/adder',
]

# Pass variants. Each maps to a run_script() script or a sentinel for
# programmatic bandit invocation.
BASELINE_SCRIPT = 'rewrite; fraig; rewrite; balance'
BANDIT_SENTINEL = '<bandit>'

PASS_VARIANTS: Dict[str, str] = {
    'baseline':       BASELINE_SCRIPT,
    'XAG (-x)':       'rewrite -x; fraig; rewrite -x; balance',
    '+bdd':           'bdd; rewrite; fraig; rewrite; balance',
    '+resub':         'rewrite; fraig; resub; rewrite; balance',
    'bandit (h=20)':  BANDIT_SENTINEL,
}


@dataclass
class PassResult:
    key:       str
    variant:   str
    ands_in:   int
    ands_out:  int
    wall_sec:  float
    error:     Optional[str] = None


def _run_variant(
    aig_path: Path,
    variant:  str,
    script:   str,
) -> PassResult:
    """Load the AIG fresh, run the variant, return (ands_after, wall_sec)."""
    import gc, contextlib, io as _io

    ref_aig, ref_outs, _, _ = read_aiger(str(aig_path))
    ands_in = ref_aig.n_ands
    out_lits = list(ref_outs)

    # Silence spammy `dd.bdd.BDD.__del__` AssertionError noise emitted during
    # Python shutdown when the `bdd` pass leaves BDD nodes referenced by the
    # global cache; does not affect results.
    stderr_sink = _io.StringIO()

    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stderr(stderr_sink):
            if script == BANDIT_SENTINEL:
                new_aig, _new_outs, _bandit = run_bandit(
                    ref_aig, out_lits,
                    horizon=20, verbose=False,
                )
            else:
                new_aig, _new_outs = run_script(ref_aig, out_lits, script,
                                                verbose=False)
            # Force BDD deallocation inside the suppressed context.
            gc.collect()
        wall = time.perf_counter() - t0
        return PassResult(
            key=str(aig_path.relative_to(EPFL_ROOT)).removesuffix('.aig'),
            variant=variant,
            ands_in=ands_in,
            ands_out=new_aig.n_ands,
            wall_sec=wall,
        )
    except Exception as exc:
        wall = time.perf_counter() - t0
        return PassResult(
            key=str(aig_path.relative_to(EPFL_ROOT)).removesuffix('.aig'),
            variant=variant,
            ands_in=ands_in,
            ands_out=-1,
            wall_sec=wall,
            error=f'{type(exc).__name__}: {exc}',
        )


def _fmt_pct(delta_abs: float, base: float) -> str:
    if base <= 0:
        return '—'
    return f'{100.0 * delta_abs / base:+.1f}%'


def _render_markdown(
    subset:   List[str],
    results:  Dict[Tuple[str, str], PassResult],
    variants: List[str],
) -> str:
    """Build the pass_eval.md content.

    Per-variant tables show |bench|ands_after|Δarea|Δtime| vs baseline.
    """
    lines: List[str] = []
    lines.append('# Pass QoR Evaluation (ROADMAP P2#7)')
    lines.append('')
    lines.append(f'Generated: {time.strftime("%Y-%m-%d %H:%M:%S")}  ')
    lines.append(f'Baseline script: `{BASELINE_SCRIPT}`  ')
    lines.append(f'Benchmark corpus: EPFL subset ({len(subset)} circuits)  ')
    lines.append('Metric: `ands_after` (AIG AND+XOR node count, post-script).')
    lines.append('')
    lines.append('Values in **bold** are wins ≥ 2%; `ERR` = pass raised an exception.')
    lines.append('')

    # Per-pass tables (vs baseline).
    baseline_col = variants[0]
    for variant in variants[1:]:
        lines.append(f'## `{variant}`  vs  `{baseline_col}`')
        lines.append('')
        lines.append('| benchmark | n_inputs | baseline_area | with_pass_area |  Δarea |  Δtime |')
        lines.append('|-----------|---------:|--------------:|---------------:|-------:|-------:|')
        for key in subset:
            base = results.get((key, baseline_col))
            r    = results.get((key, variant))
            if base is None or r is None:
                continue
            n_in = '—'  # re-read benchmark to get n_inputs would be extra IO;
            #                    we look it up from baseline PassResult instead.
            # Extract n_inputs by re-reading the aig header, cheap.
            try:
                from nand_optimizer.io.aiger_io import read_aiger
                p = EPFL_ROOT / (key + '.aig')
                _, _, ins, _ = read_aiger(str(p))
                n_in = str(len(ins))
            except Exception:
                pass

            if r.error:
                lines.append(
                    f'| {key} | {n_in} | {base.ands_out} | ERR | — | — |'
                )
                continue
            d_area = r.ands_out - base.ands_out
            d_time = r.wall_sec - base.wall_sec
            area_pct = _fmt_pct(d_area, base.ands_out)
            time_pct = _fmt_pct(d_time, base.wall_sec)
            # Bold wins ≥ 2%.
            if base.ands_out and (-d_area / base.ands_out) >= 0.02:
                area_str = f'**{area_pct}**'
            else:
                area_str = area_pct
            lines.append(
                f'| {key} | {n_in} | {base.ands_out} | '
                f'{r.ands_out} | {area_str} | {time_pct} |'
            )
        lines.append('')

    # Summary: mean Δ per variant.
    lines.append('## Summary — mean Δarea across subset')
    lines.append('')
    lines.append('| variant | wins | ties | regressions | mean Δarea | mean Δtime |')
    lines.append('|---------|-----:|-----:|------------:|-----------:|-----------:|')
    for variant in variants[1:]:
        deltas_area: List[float] = []
        deltas_time: List[float] = []
        wins = ties = regs = 0
        for key in subset:
            base = results.get((key, baseline_col))
            r    = results.get((key, variant))
            if base is None or r is None or r.error or base.ands_out <= 0:
                continue
            frac_a = (r.ands_out - base.ands_out) / base.ands_out
            frac_t = ((r.wall_sec - base.wall_sec) / base.wall_sec
                      if base.wall_sec > 0 else 0.0)
            deltas_area.append(frac_a)
            deltas_time.append(frac_t)
            if frac_a < -0.005:
                wins += 1
            elif frac_a > 0.005:
                regs += 1
            else:
                ties += 1
        if not deltas_area:
            continue
        mean_a = sum(deltas_area) / len(deltas_area)
        mean_t = sum(deltas_time) / len(deltas_time)
        lines.append(
            f'| `{variant}` | {wins} | {ties} | {regs} | '
            f'{mean_a * 100:+.1f}% | {mean_t * 100:+.1f}% |'
        )
    lines.append('')

    # Raw table: all variants, ands_after.
    lines.append('## Raw `ands_after` matrix')
    lines.append('')
    header = '| benchmark | ' + ' | '.join(variants) + ' |'
    sep    = '|-----------|' + '|'.join([' ---: ' for _ in variants]) + '|'
    lines.append(header)
    lines.append(sep)
    for key in subset:
        row = [key]
        for variant in variants:
            r = results.get((key, variant))
            if r is None:
                row.append('—')
            elif r.error:
                row.append('ERR')
            else:
                row.append(str(r.ands_out))
        lines.append('| ' + ' | '.join(row) + ' |')
    lines.append('')

    # Raw wall-time matrix.
    lines.append('## Raw wall-time matrix (seconds)')
    lines.append('')
    lines.append(header)
    lines.append(sep)
    for key in subset:
        row = [key]
        for variant in variants:
            r = results.get((key, variant))
            if r is None:
                row.append('—')
            elif r.error:
                row.append('ERR')
            else:
                row.append(f'{r.wall_sec:.2f}')
        lines.append('| ' + ' | '.join(row) + ' |')
    lines.append('')

    # Errors, if any.
    errs = [(k, v, r) for (k, v), r in results.items() if r.error]
    if errs:
        lines.append('## Errors')
        lines.append('')
        for key, variant, r in errs:
            lines.append(f'- `{key}` / `{variant}`: {r.error}')
        lines.append('')

    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--subset', type=str, default=None,
                    help='comma-separated benchmark keys (default: DEFAULT_SUBSET)')
    ap.add_argument('--quick', action='store_true',
                    help='run only QUICK_SUBSET (3 smallest)')
    ap.add_argument('--variants', type=str, default=None,
                    help='comma-separated variant names (default: all)')
    ap.add_argument('--out', type=str,
                    default=str(REPO_ROOT / 'benchmarks' / 'pass_eval.md'),
                    help='output Markdown path')
    args = ap.parse_args()

    if args.quick:
        subset = QUICK_SUBSET
    elif args.subset:
        subset = [s.strip() for s in args.subset.split(',') if s.strip()]
    else:
        subset = DEFAULT_SUBSET

    if args.variants:
        wanted = [v.strip() for v in args.variants.split(',') if v.strip()]
        variants = [v for v in PASS_VARIANTS if v in wanted]
        # Always include baseline first for delta computation.
        if 'baseline' not in variants:
            variants = ['baseline'] + variants
    else:
        variants = list(PASS_VARIANTS.keys())

    print(f'Subset:   {subset}')
    print(f'Variants: {variants}')
    print(f'Output:   {args.out}')
    print()

    results: Dict[Tuple[str, str], PassResult] = {}
    total = len(subset) * len(variants)
    done = 0

    for key in subset:
        path = EPFL_ROOT / (key + '.aig')
        if not path.exists():
            print(f'  SKIP  {key}: not downloaded ({path})')
            continue
        for variant in variants:
            done += 1
            script = PASS_VARIANTS[variant]
            print(f'  [{done:2d}/{total}] {key} / {variant}', end='', flush=True)
            r = _run_variant(path, variant, script)
            results[(key, variant)] = r
            if r.error:
                print(f'  ERR after {r.wall_sec:.1f}s  ({r.error})')
            else:
                d_area = ''
                if variant != 'baseline':
                    base = results.get((key, 'baseline'))
                    if base is not None and base.ands_out > 0:
                        pct = 100.0 * (r.ands_out - base.ands_out) / base.ands_out
                        d_area = f'  ({pct:+.1f}% vs baseline)'
                print(f'  ANDs {r.ands_in} → {r.ands_out}'
                      f'  [{r.wall_sec:.2f}s]{d_area}')

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render_markdown(subset, results, variants),
                        encoding='utf-8')
    print(f'\nWrote {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
