"""
Synthesis script parser and executor.

A synthesis script is a semicolon-separated sequence of AIG optimization
commands, mirroring ABC-style scripts, e.g.:

    balance; rewrite; fraig; balance; rewrite -z

Commands
--------
balance              minimise critical-path depth (area-preserving)
rewrite  [-z] [-r N] [-K N]
                     local AIG rewriting via k-feasible cuts
                       -z       use exact (SAT-based) sub-circuit synthesis
                       -r N     number of rewriting rounds  (default 1)
                       -K N     cut size                    (default 4)
refactor [-z] [-r N] [-K N]
                     alias for rewrite (factored-form replacement)
fraig                merge equivalent nodes (simulation + SAT)
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple

from .aig import AIG

AIGLit = int

DEFAULT_SCRIPT = "rewrite; fraig; balance"


def parse_script(script: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Parse a semicolon-separated synthesis script into (cmd, kwargs) pairs."""
    commands: List[Tuple[str, Dict[str, Any]]] = []
    for part in script.split(';'):
        tokens = part.strip().split()
        if not tokens:
            continue
        cmd = tokens[0].lower()
        if cmd not in ('balance', 'rewrite', 'refactor', 'fraig'):
            raise ValueError(
                f"Unknown synthesis command '{tokens[0]}'. "
                f"Supported: balance, rewrite, refactor, fraig"
            )
        kwargs: Dict[str, Any] = {}
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == '-z':
                kwargs['use_exact'] = True
            elif tok in ('-r', '-K'):
                if i + 1 >= len(tokens):
                    raise ValueError(
                        f"Flag {tok} requires an integer argument "
                        f"(in '{part.strip()}')"
                    )
                val = int(tokens[i + 1])
                kwargs['rounds' if tok == '-r' else 'cut_size'] = val
                i += 1
            else:
                raise ValueError(
                    f"Unknown flag '{tok}' for command '{cmd}'. "
                    f"Supported: -z, -r <N>, -K <N>"
                )
            i += 1
        commands.append((cmd, kwargs))
    return commands


def _fmt_flags(kwargs: Dict[str, Any]) -> str:
    parts = []
    if kwargs.get('use_exact'):
        parts.append('-z')
    if 'rounds' in kwargs:
        parts.append(f"-r {kwargs['rounds']}")
    if 'cut_size' in kwargs:
        parts.append(f"-K {kwargs['cut_size']}")
    return ' '.join(parts)


def run_script(
    aig: AIG,
    out_lits: List[AIGLit],
    script: str,
    verbose: bool = True,
) -> Tuple[AIG, List[AIGLit]]:
    """Apply a synthesis script to an AIG, returning (new_aig, new_out_lits)."""
    from .rewrite import rewrite_aig
    from .balance import balance_aig, aig_depth
    from .fraig   import fraig as _fraig

    commands = parse_script(script)
    n = len(commands)

    for step, (cmd, kwargs) in enumerate(commands, 1):
        if verbose:
            flags = _fmt_flags(kwargs)
            tag   = f"{cmd} {flags}".strip()
            print(f"\n  [script {step}/{n}] {tag}")

        n_before = aig.n_nodes

        if cmd == 'balance':
            d_before     = aig_depth(aig, out_lits)
            aig, out_lits = balance_aig(aig, out_lits)
            d_after      = aig_depth(aig, out_lits)
            if verbose:
                print(f"      depth: {d_before} -> {d_after}  "
                      f"(nodes: {n_before} -> {aig.n_nodes})")

        elif cmd in ('rewrite', 'refactor'):
            aig, out_lits = rewrite_aig(aig, out_lits, **kwargs)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

        elif cmd == 'fraig':
            aig, out_lits = _fraig(aig, out_lits)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

    return aig, out_lits
