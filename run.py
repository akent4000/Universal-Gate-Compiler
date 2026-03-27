#!/usr/bin/env python3
"""
Universal Gate Compiler — entry point.

Usage:
    python run.py              # default: 7-segment decoder
    python run.py adder        # 2-bit adder
    python run.py excess3      # BCD → Excess-3
    python run.py all          # run all examples
    python run.py --quiet      # suppress verbose output

Alternative invocations (all equivalent):
    python -m nand_optimizer [args]
    python nand_optimizer/__main__.py [args]
"""

from nand_optimizer.__main__ import main

if __name__ == '__main__':
    main()