#!/usr/bin/env python3
"""
Entry point:
    python run.py [circuit] [flags]              # from project root
    python -m nand_optimizer [circuit] [flags]   # module invocation
    python nand_optimizer/__main__.py [circuit]   # direct script

Available circuits:
  7seg      — BCD to 7-segment decoder (default)
  adder     — 2-bit adder
  excess3   — BCD to Excess-3 converter
  all       — run all examples

Flags:
  --quiet              suppress per-step verbose output
  --circ <file.circ>   export Logisim .circ file
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

from .examples.circuits import seven_segment, two_bit_adder, bcd_to_excess3
from .pipeline    import optimize
from .tests       import run_tests
from .circ_export import export_circ


CIRCUITS = {
    '7seg':    ('7-Segment Decoder',      seven_segment),
    'adder':   ('2-Bit Adder',            two_bit_adder),
    'excess3': ('BCD \u2192 Excess-3',    bcd_to_excess3),
}


def run_one(key, verbose=True, circ_path=None):
    label, factory = CIRCUITS[key]
    print(f'\n{chr(9619) * 68}')
    print(f'  {label}')
    print(f'{chr(9619) * 68}')
    tt     = factory()
    result = optimize(tt, verbose=verbose)
    ok     = run_tests(tt, result, verbose=verbose)

    if circ_path:
        export_circ(result, circ_path, sanitize_for_logisim(label))

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
                        help='Circuit to run: "all", a key (e.g. "7seg"), or a path to a .pla file')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Suppress detailed optimization logs')
    parser.add_argument('--circ', metavar='FILE',
                        help='Export final network to Logisim Evolution (.circ) file')
    args = parser.parse_args()

    target  = args.circuit
    verbose = not args.quiet

    if target.endswith('.pla') or target.endswith('.espresso'):
        from .truth_table import TruthTable
        if not os.path.exists(target):
            print(f"Error: PLA file '{target}' not found.")
            sys.exit(1)
        # We need run_pla logic here.
        label = os.path.basename(target)
        print(f'\n{"#" * 40}')
        print(f'  {label}')
        print(f'{"#" * 40}')
        from .pipeline import optimize
        tt     = TruthTable.from_pla(target)
        result = optimize(tt, verbose=verbose)
        from .tests import run_tests
        ok     = run_tests(tt, result, verbose=verbose)
        if args.circ:
            from .circ_export import export_circ
            export_circ(result, args.circ, sanitize_for_logisim(label))
        sys.exit(0 if ok else 1)

    if target == 'all':
        ok = True
        for key in CIRCUITS.keys():
            if not run_one(key, verbose, args.circ):
                ok = False
        sys.exit(0 if ok else 1)

    if target in CIRCUITS:
        ok = run_one(target, verbose, args.circ)
        sys.exit(0 if ok else 1)

    print(f'\nUnknown circuit "{target}".\nAvailable: {", ".join(CIRCUITS.keys())} | all\n')
    sys.exit(1)


if __name__ == '__main__':
    main()