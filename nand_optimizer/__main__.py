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

# ── Make imports work regardless of how the script is invoked ─────────────────
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
    choice    = '7seg'
    verbose   = True
    circ_path = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--quiet':
            verbose = False
        elif args[i] == '--circ' and i + 1 < len(args):
            i += 1
            circ_path = args[i]
        elif not args[i].startswith('-'):
            choice = args[i]
        i += 1

    if choice == 'all':
        all_ok = True
        for key in CIRCUITS:
            path = None
            if circ_path:
                base, ext = os.path.splitext(circ_path)
                path = f'{base}_{key}{ext}'
            if not run_one(key, verbose, path):
                all_ok = False
        sys.exit(0 if all_ok else 1)

    if choice not in CIRCUITS:
        print(f'Unknown circuit "{choice}".')
        print(f'Available: {", ".join(CIRCUITS)} | all')
        sys.exit(1)

    ok = run_one(choice, verbose, circ_path)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()