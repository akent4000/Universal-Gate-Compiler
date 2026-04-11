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

import sys
import os
import subprocess

def ensure_dependencies():
    """Автоматически проверяет и устанавливает библиотеки из requirements.txt."""
    req_file = os.path.join(os.path.dirname(__file__), 'requirements.txt')
    if not os.path.exists(req_file):
        return

    try:
        import pkg_resources
        with open(req_file, 'r', encoding='utf-8') as f:
            requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        pkg_resources.require(requirements)
    except Exception:
        print("Установка недостающих библиотек из requirements.txt...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_file])
        print("Готово!\n")

from nand_optimizer.__main__ import main

if __name__ == '__main__':
    ensure_dependencies()
    main()