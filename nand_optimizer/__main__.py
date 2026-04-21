#!/usr/bin/env python3
"""
Entry point:
    python run.py [circuit] [flags]              # from project root
    python -m nand_optimizer [circuit] [flags]   # module invocation
    python nand_optimizer/__main__.py [circuit]  # direct script

Available circuits:
  7seg      — BCD to 7-segment decoder (default)
  adder     — 2-bit adder
  excess3   — BCD to Excess-3 converter
  all       — run all examples

MCNC benchmarks:
  rd53      — 5-bit Hamming weight (popcount)
  parity9   — 9-bit odd parity
  mult3     — 3x3 unsigned multiplier
  mult4     — 4x4 unsigned multiplier
  misex1    — dense 8-in / 7-out combinational
  z4ml      — dense 7-in / 4-out combinational
  bench     — run the full MCNC regression suite

Property-based testing:
  proptest  — random equivalence checks (Hypothesis optional)

Flags:
  --quiet              suppress per-step verbose output
  --circ <file.circ>   export Logisim .circ file
  --verify             run miter-based formal equivalence check
  --profile            collect per-pass time + peak memory
  --cases N            number of cases for proptest (default 40)
"""

import sys
import os
import argparse

# -- Make imports work regardless of how the script is invoked -----------------
if __name__ == '__main__' and __package__ is None:
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_here)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    __package__ = 'nand_optimizer'

from .examples.circuits   import seven_segment, two_bit_adder, bcd_to_excess3
from .examples.benchmarks import (hamming_weight_5, parity_9,
                                  multiplier_3x3, multiplier_4x4,
                                  misex1, z4ml)
from .pipeline          import optimize
from .tests             import run_tests
from .circ_export       import export_circ
from .dot_export        import aig_to_dot
from .verify            import miter_verify
from .benchmark_runner  import run_benchmarks, BENCHMARKS
from .property_tests    import run_property_tests


CIRCUITS = {
    '7seg':    ('7-Segment Decoder',      seven_segment),
    'adder':   ('2-Bit Adder',            two_bit_adder),
    'excess3': ('BCD \u2192 Excess-3',    bcd_to_excess3),
    # MCNC-style stress tests
    'rd53':    ('RD53 (5-bit popcount)',  hamming_weight_5),
    'parity9': ('9-bit parity',           parity_9),
    'mult3':   ('3x3 multiplier',         multiplier_3x3),
    'mult4':   ('4x4 multiplier',         multiplier_4x4),
    'misex1':  ('misex1',                 misex1),
    'z4ml':    ('z4ml',                   z4ml),
}


def _print_verification(tt, result):
    v = miter_verify(tt, result)
    verdict = {True: 'EQUIVALENT', False: 'MISMATCH', None: 'UNKNOWN'}[v['equivalent']]
    print(f'\n  Miter verification ({v["method"]}): {verdict}  '
          f'[{v["checked"]} minterms]')
    if v['equivalent'] is False:
        print(f'    counterexample: {v["counterexample"]}')
    return v['equivalent'] is not False


def run_one(key, verbose=True, circ_path=None,
            verify=False, profile=False, dot_path=None, script=None):
    label, factory = CIRCUITS[key]
    print(f'\n{chr(9619) * 68}')
    print(f'  {label}')
    print(f'{chr(9619) * 68}')
    tt     = factory()
    result = optimize(tt, verbose=verbose, profile=profile, script=script)
    ok     = run_tests(tt, result, verbose=verbose)

    if verify:
        ok = _print_verification(tt, result) and ok

    if circ_path:
        export_circ(result, circ_path, sanitize_for_logisim(label))

    if dot_path and result.aig is not None:
        dot_str = aig_to_dot(result.aig, result.out_lits, tt.output_names, title=label)
        with open(dot_path, 'w') as f:
            f.write(dot_str)
        print(f'\n  DOT graph written to: {dot_path}')

    return ok

import re

def sanitize_for_logisim(label: str) -> str:
    """
    Turns any string into a secure name for Logisim-evolution.
    """
    safe_name = re.sub(r'[\s\-]', '_', label)
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '', safe_name)
    safe_name = re.sub(r'_+', '_', safe_name).strip('_')
    if not safe_name or not safe_name[0].isalpha():
        safe_name = 'Circ_' + safe_name
        
    return safe_name

def main():
    parser = argparse.ArgumentParser(description="Universal NAND Gate Optimizer")
    parser.add_argument('circuit', nargs='?', default='all',
                        help='Circuit / benchmark key, "all", "bench", '
                             '"proptest", or a .pla file path')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Suppress detailed optimization logs')
    parser.add_argument('--circ', metavar='FILE',
                        help='Export final network to Logisim Evolution (.circ) file')
    parser.add_argument('--verify', action='store_true',
                        help='Run miter-based formal equivalence check (z3 if available)')
    parser.add_argument('--profile', action='store_true',
                        help='Collect per-pass time and peak memory')
    parser.add_argument('--cases', type=int, default=40,
                        help='Number of cases for "proptest" (default: 40)')
    parser.add_argument('--dot', metavar='FILE',
                        help='Export final AIG to Graphviz .dot file')
    parser.add_argument('--script', metavar='SCRIPT',
                        help='Synthesis script: semicolon-separated AIG commands, '
                             'e.g. "balance; rewrite; fraig; balance; rewrite -z". '
                             'Commands: balance, rewrite [-z] [-r N] [-K N], '
                             'refactor [-z] [-r N] [-K N], fraig. '
                             'Replaces the built-in rewrite/fraig/balance sequence.')
    args = parser.parse_args()

    target  = args.circuit
    verbose = not args.quiet

    # Benchmark regression suite
    if target == 'bench':
        rows = run_benchmarks(verbose=False, verify=args.verify or True,
                              profile=args.profile or True)
        n_fail = sum(1 for r in rows if r.get('verify') is False)
        sys.exit(0 if n_fail == 0 else 1)

    # Property-based regression
    if target == 'proptest':
        ok = run_property_tests(n_cases=args.cases, verbose=verbose)
        sys.exit(0 if ok else 1)

    if target.endswith('.pla') or target.endswith('.espresso'):
        from .truth_table import TruthTable
        if not os.path.exists(target):
            print(f"Error: PLA file '{target}' not found.")
            sys.exit(1)
        label = os.path.basename(target)
        print(f'\n{"#" * 40}')
        print(f'  {label}')
        print(f'{"#" * 40}')
        tt     = TruthTable.from_pla(target)
        result = optimize(tt, verbose=verbose, profile=args.profile,
                          script=args.script)
        ok     = run_tests(tt, result, verbose=verbose)
        if args.verify:
            ok = _print_verification(tt, result) and ok
        if args.circ:
            export_circ(result, args.circ, sanitize_for_logisim(label))
        if args.dot and result.aig is not None:
            dot_str = aig_to_dot(result.aig, result.out_lits, tt.output_names,
                                 title=label)
            with open(args.dot, 'w') as f:
                f.write(dot_str)
            print(f'\n  DOT graph written to: {args.dot}')
        sys.exit(0 if ok else 1)

    if target == 'all':
        ok = True
        for key in CIRCUITS.keys():
            if not run_one(key, verbose, args.circ,
                           verify=args.verify, profile=args.profile,
                           dot_path=args.dot, script=args.script):
                ok = False
        sys.exit(0 if ok else 1)

    if target in CIRCUITS:
        ok = run_one(target, verbose, args.circ,
                     verify=args.verify, profile=args.profile,
                     dot_path=args.dot, script=args.script)
        sys.exit(0 if ok else 1)

    avail = ", ".join(list(CIRCUITS) + ['all', 'bench', 'proptest'])
    print(f'\nUnknown circuit "{target}".\nAvailable: {avail}\n')
    sys.exit(1)


if __name__ == '__main__':
    main()