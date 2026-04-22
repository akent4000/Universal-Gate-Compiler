"""
Synthesis script parser and executor.

A synthesis script is a semicolon-separated sequence of AIG optimization
commands, mirroring ABC-style scripts, e.g.:

    balance; rewrite; fraig; dc; rewrite; balance

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
dc [-K N] [-D N] [-T N] [-W N] [-r N] [--no-sdc] [--odc] [--dc-exact] [--no-resub]
                     don't-care-based local rewriting
                       -K N       cut size                     (default 4)
                       -D N       max fanout-cone size for ODC (default 200)
                       -T N       Z3 timeout per query, in ms  (default 1000)
                       -W N       resub window size            (default 64)
                       -r N       iterative rounds (V2.b)      (default 1)
                       --no-sdc   disable satisfiability DCs
                       --odc      enable observability DCs (V2 sim-based
                                  admissibility check; sound modulo sim
                                  coverage, protected by end-of-pass miter)
                       --dc-exact enable SAT-based exact-synth fallback
                                  for cuts with k > 4 (requires -K >= 5)
                       --no-resub disable V2.c window resubstitution
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple

from .aig import AIG

AIGLit = int

DEFAULT_SCRIPT = "rewrite; fraig; dc; rewrite; balance"


def parse_script(script: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Parse a semicolon-separated synthesis script into (cmd, kwargs) pairs."""
    commands: List[Tuple[str, Dict[str, Any]]] = []
    for part in script.split(';'):
        tokens = part.strip().split()
        if not tokens:
            continue
        cmd = tokens[0].lower()
        if cmd not in ('balance', 'rewrite', 'refactor', 'fraig', 'dc'):
            raise ValueError(
                f"Unknown synthesis command '{tokens[0]}'. "
                f"Supported: balance, rewrite, refactor, fraig, dc"
            )
        kwargs: Dict[str, Any] = {}
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == '-z' and cmd in ('rewrite', 'refactor'):
                kwargs['use_exact'] = True
            elif tok == '--no-sdc' and cmd == 'dc':
                kwargs['use_sdc'] = False
            elif tok == '--odc' and cmd == 'dc':
                kwargs['use_odc'] = True
            elif tok == '--no-odc' and cmd == 'dc':
                kwargs['use_odc'] = False
            elif tok == '--dc-exact' and cmd == 'dc':
                kwargs['use_exact'] = True
            elif tok == '--no-resub' and cmd == 'dc':
                kwargs['use_resub'] = False
            elif tok in ('-r', '-K', '-D', '-T', '-W'):
                if i + 1 >= len(tokens):
                    raise ValueError(
                        f"Flag {tok} requires an integer argument "
                        f"(in '{part.strip()}')"
                    )
                val = int(tokens[i + 1])
                if tok == '-r':
                    if cmd not in ('rewrite', 'refactor', 'dc'):
                        raise ValueError(f"Flag -r is only valid for 'rewrite', 'refactor', or 'dc'")
                    kwargs['rounds'] = val
                elif tok == '-K':
                    kwargs['cut_size'] = val
                elif tok == '-D':
                    if cmd != 'dc':
                        raise ValueError(f"Flag -D is only valid for 'dc'")
                    kwargs['tfo_cap'] = val
                elif tok == '-T':
                    if cmd != 'dc':
                        raise ValueError(f"Flag -T is only valid for 'dc'")
                    kwargs['timeout_ms'] = val
                elif tok == '-W':
                    if cmd != 'dc':
                        raise ValueError(f"Flag -W is only valid for 'dc'")
                    kwargs['resub_window'] = val
                i += 1
            else:
                raise ValueError(
                    f"Unknown flag '{tok}' for command '{cmd}'."
                )
            i += 1
        commands.append((cmd, kwargs))
    return commands


def _fmt_flags(kwargs: Dict[str, Any]) -> str:
    parts = []
    if kwargs.get('use_exact'):
        parts.append('-z')
    if kwargs.get('use_sdc') is False:
        parts.append('--no-sdc')
    if kwargs.get('use_odc') is True:
        parts.append('--odc')
    elif kwargs.get('use_odc') is False:
        parts.append('--no-odc')
    if kwargs.get('use_exact') is True and 'use_exact' in kwargs:
        parts.append('--dc-exact')
    if kwargs.get('use_resub') is False:
        parts.append('--no-resub')
    if 'resub_window' in kwargs:
        parts.append(f"-W {kwargs['resub_window']}")
    if 'rounds' in kwargs:
        parts.append(f"-r {kwargs['rounds']}")
    if 'cut_size' in kwargs:
        parts.append(f"-K {kwargs['cut_size']}")
    if 'tfo_cap' in kwargs:
        parts.append(f"-D {kwargs['tfo_cap']}")
    if 'timeout_ms' in kwargs:
        parts.append(f"-T {kwargs['timeout_ms']}")
    return ' '.join(parts)


def run_script(
    aig: AIG,
    out_lits: List[AIGLit],
    script: str,
    verbose: bool = True,
) -> Tuple[AIG, List[AIGLit]]:
    """Apply a synthesis script to an AIG, returning (new_aig, new_out_lits)."""
    from .rewrite    import rewrite_aig
    from .balance    import balance_aig, aig_depth
    from .fraig      import fraig as _fraig
    from .dont_care  import dc_optimize

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

        elif cmd == 'dc':
            aig, out_lits = dc_optimize(aig, out_lits, **kwargs)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

    return aig, out_lits
