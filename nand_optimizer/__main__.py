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

EPFL combinational benchmarks (vendored in benchmarks/epfl/):
  epfl        — run the full arithmetic + random_control suite
  epfl-check  — audit the local snapshot against upstream GitHub

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
from .examples.fsm_examples import FSM_EXAMPLES
from .examples.jk_counter  import (universal_reversible_counter,
                                    run_jkcounter_regression)
from .pipeline          import optimize, hierarchical_optimize
from .tests             import run_tests
from .circ_export       import export_circ, export_fsm_circ
from .dot_export        import aig_to_dot
from .aiger_io          import write_aiger, read_aiger
from .blif_io           import write_blif, read_blif
from .verify            import miter_verify, bmc_verify
from .atpg              import run_atpg, AtpgResult
from .benchmark_runner  import run_benchmarks, BENCHMARKS
from .epfl_bench        import run_epfl, check_epfl_updates
from .property_tests    import run_property_tests
from .fsm               import (synthesize_fsm, simulate_fsm, parse_kiss,
                                minimize_states)


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


def _print_atpg(ar: AtpgResult) -> None:
    print(f'\n  ATPG:  {ar.n_detected}/{ar.n_total} faults detected'
          f'  ({ar.fault_coverage * 100:.1f}% coverage)'
          f'  |  {ar.n_undetectable} undetectable'
          f'  |  {len(ar.test_vectors)} unique test vector(s)')
    if ar.n_undetectable:
        print('  Undetectable faults:')
        for f in ar.faults:
            if not f.detectable:
                print(f'    {f.wire} SA{f.stuck_at}')
    if ar.test_vectors:
        print('  Test vectors:')
        for i, tv in enumerate(ar.test_vectors, 1):
            vec_str = '  '.join(f'{k}={v}' for k, v in tv.items())
            print(f'    [{i:3d}]  {vec_str}')


def _print_verification(tt, result):
    v = miter_verify(tt, result)
    verdict = {True: 'EQUIVALENT', False: 'MISMATCH', None: 'UNKNOWN'}[v['equivalent']]
    print(f'\n  Miter verification ({v["method"]}): {verdict}  '
          f'[{v["checked"]} minterms]')
    if v['equivalent'] is False:
        print(f'    counterexample: {v["counterexample"]}')
    return v['equivalent'] is not False


def _print_bmc(v: dict) -> bool:
    bound   = v['bound']
    checked = v['checked']
    verdict = {True: 'EQUIVALENT', False: 'MISMATCH', None: 'UNKNOWN'}[v['equivalent']]
    print(f'\n  BMC (bound={bound}): {verdict}  [{checked} step(s) verified]')
    if v['equivalent'] is False:
        ce = v['counterexample']
        print(f'    first divergence at step {ce["step"]}')
        for t, inp in enumerate(ce['inputs']):
            print(f'      t={t}:  state={ce["states"][t]}  inputs={inp}')
    return v['equivalent'] is not False


def _export_aiger_path(result, tt, path: str, label: str) -> None:
    """Write final AIG to AIGER; ASCII iff the path ends in `.aag`."""
    binary = not path.lower().endswith('.aag')
    write_aiger(result.aig, result.out_lits, path,
                input_names=tt.input_names,
                output_names=tt.output_names,
                binary=binary,
                comment=f'generated by nand_optimizer from {label}')
    kind = 'binary' if binary else 'ASCII'
    print(f'\n  AIGER ({kind}) written to: {path}')


def _export_blif_path(result, tt, path: str, label: str) -> None:
    write_blif(result.aig, result.out_lits, path,
               model_name=sanitize_for_logisim(label),
               input_names=tt.input_names,
               output_names=tt.output_names)
    print(f'\n  BLIF written to: {path}')


def run_one(key, verbose=True, circ_path=None,
            verify=False, profile=False, dot_path=None, script=None,
            aiger_path=None, blif_path=None, atpg=False,
            bandit_horizon=0, bandit_strategy='ucb1'):
    label, factory = CIRCUITS[key]
    print(f'\n{chr(9619) * 68}')
    print(f'  {label}')
    print(f'{chr(9619) * 68}')
    tt     = factory()
    result = optimize(tt, verbose=verbose, profile=profile, script=script,
                      bandit_horizon=bandit_horizon,
                      bandit_strategy=bandit_strategy)
    ok     = run_tests(tt, result, verbose=verbose)

    if verify:
        ok = _print_verification(tt, result) and ok

    if atpg:
        out_wires = [result[name].out_wire for name in tt.output_names]
        _print_atpg(run_atpg(result.builder.gates, tt.input_names, out_wires))

    if circ_path:
        export_circ(result, circ_path, sanitize_for_logisim(label))

    if dot_path and result.aig is not None:
        dot_str = aig_to_dot(result.aig, result.out_lits, tt.output_names, title=label)
        with open(dot_path, 'w') as f:
            f.write(dot_str)
        print(f'\n  DOT graph written to: {dot_path}')

    if aiger_path and result.aig is not None:
        _export_aiger_path(result, tt, aiger_path, label)
    if blif_path and result.aig is not None:
        _export_blif_path(result, tt, blif_path, label)

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

def _run_fsm(key_or_path: str, encoding: str, verbose: bool,
             circ_path: str = None, script: str = None,
             skip_minimize: bool = False,
             excitation: str = 'd',
             bmc_bound: int = None) -> bool:
    """Phase 3 dispatch: built-in FSM example or KISS2 file -> synthesize -> test."""
    if key_or_path in FSM_EXAMPLES:
        label, factory = FSM_EXAMPLES[key_or_path]
        stt = factory()
    else:
        if not os.path.exists(key_or_path):
            print(f"Error: FSM file '{key_or_path}' not found.")
            return False
        label = os.path.basename(key_or_path)
        with open(key_or_path, 'r') as fh:
            stt = parse_kiss(fh.read())

    print(f'\n{chr(9619) * 68}')
    print(f'  FSM  {label}')
    print(f'{chr(9619) * 68}')
    print(f'  {stt}')
    print(f'  reset     : {stt.reset_state}')
    print(f'  inputs    : {stt.input_names}')
    print(f'  outputs   : {stt.output_names}')

    res = synthesize_fsm(stt, encoding=encoding, minimize=not skip_minimize,
                         verbose=verbose, script=script,
                         excitation=excitation)

    # Self-check: simulate and confirm outputs match the reference semantics
    ok = _simulate_and_check(res, stt, verbose=verbose)

    if bmc_bound is not None:
        ok = _print_bmc(bmc_verify(res, bound=bmc_bound)) and ok

    if circ_path:
        export_fsm_circ(res, circ_path, sanitize_for_logisim(label))

    return ok


def _simulate_and_check(fsm_result, orig_stt, verbose: bool = True) -> bool:
    """
    Sanity-check a synthesized FSM by stepping it through random input
    sequences and comparing against a direct interpreter of the original
    StateTable.  Returns True iff every cycle matches on defined bits.
    """
    import random
    from .fsm import _expand_stt
    from .truth_table import DASH as _DASH

    # Reference interpreter
    delta, lam = _expand_stt(orig_stt)

    n_in = orig_stt.n_input_bits
    # Deterministic random sequence
    rng = random.Random(1234)
    if n_in == 0:
        seq = [tuple()] * 16
    else:
        seq = [tuple(rng.randint(0, 1) for _ in range(n_in))
               for _ in range(32)]

    trace = simulate_fsm(fsm_result, seq)

    # Walk reference machine
    state = orig_stt.reset_state
    ok = True
    n_checked = 0
    for (obs_state, obs_bits, obs_out), inputs in zip(trace, seq):
        pat = 0
        for k, b in enumerate(inputs):
            pat |= b << (n_in - 1 - k)
        ref_out = lam[(state, pat)]
        for k, (o, r) in enumerate(zip(obs_out, ref_out)):
            if r == _DASH:
                continue
            if o != r:
                if verbose:
                    print(f'    [FAIL] cycle {n_checked}: output bit {k} '
                          f'obs={o}, ref={r}, state={state}, inputs={inputs}')
                ok = False
                break
        # Advance reference
        nxt = delta[(state, pat)]
        if nxt is not None:
            state = nxt
        n_checked += 1

    sym = 'OK' if ok else 'FAIL'
    ff_label = 'D-FF' if fsm_result.excitation == 'd' else 'JK-FF'
    print(f'\n  [{sym}] FSM simulation cross-check ({n_checked} cycles, '
          f'{fsm_result.n_nand} NAND + {fsm_result.n_flip_flops} {ff_label})')
    return ok


def _run_aig_file(path: str, verbose: bool,
                  script: str = None,
                  aiger_path: str = None,
                  blif_path: str = None,
                  dot_path: str = None) -> bool:
    """Load an AIG from AIGER/BLIF, optionally re-run a synthesis script,
    and re-export. Skips the TruthTable pipeline entirely (the AIG never
    has an associated cube cover)."""
    if not os.path.exists(path):
        print(f"Error: input file '{path}' not found.")
        return False

    label = os.path.basename(path)
    print(f'\n{chr(9619) * 68}')
    print(f'  {label}')
    print(f'{chr(9619) * 68}')

    if path.endswith('.blif'):
        aig, out_lits, in_names, out_names, _model = read_blif(path)
    else:
        aig, out_lits, in_names, out_names = read_aiger(path)
    print(f'  loaded: {aig.n_inputs} inputs, {len(out_lits)} outputs, '
          f'{aig.n_ands} AND nodes')

    if script:
        from .script import run_script
        aig, out_lits = run_script(aig, out_lits, script, verbose)
        print(f'  after script: {aig.n_ands} AND nodes')

    if aiger_path:
        binary = not aiger_path.lower().endswith('.aag')
        write_aiger(aig, out_lits, aiger_path,
                    input_names=in_names, output_names=out_names,
                    binary=binary,
                    comment=f'nand_optimizer: {label}')
        kind = 'binary' if binary else 'ASCII'
        print(f'\n  AIGER ({kind}) written to: {aiger_path}')
    if blif_path:
        write_blif(aig, out_lits, blif_path,
                   model_name=sanitize_for_logisim(label),
                   input_names=in_names, output_names=out_names)
        print(f'\n  BLIF written to: {blif_path}')
    if dot_path:
        dot_str = aig_to_dot(aig, out_lits, out_names, title=label)
        with open(dot_path, 'w') as f:
            f.write(dot_str)
        print(f'\n  DOT graph written to: {dot_path}')

    return True


def main():
    parser = argparse.ArgumentParser(description="Universal NAND Gate Optimizer")
    parser.add_argument('circuit', nargs='?', default='all',
                        help='Circuit / benchmark key, "all", "bench", '
                             '"proptest", "fsm:<key>", a .pla, .kiss2, '
                             '.v, or .sv file path')
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
    parser.add_argument('--subset', metavar='LIST',
                        help='Comma-separated EPFL benchmark keys to run '
                             '(e.g. "arithmetic/adder,random_control/ctrl"). '
                             'Default: full suite.')
    parser.add_argument('--no-verify', action='store_true',
                        help='Skip the z3 AIG-vs-AIG miter in the EPFL runner.')
    parser.add_argument('--verify-timeout', type=float, default=60.0,
                        help='Per-benchmark z3 timeout in seconds (default: 60).')
    parser.add_argument('--dot', metavar='FILE',
                        help='Export final AIG to Graphviz .dot file')
    parser.add_argument('--aiger', metavar='FILE',
                        help='Export final AIG to AIGER format (.aig binary or .aag ASCII '
                             'based on file extension; ASCII if extension is neither)')
    parser.add_argument('--blif', metavar='FILE',
                        help='Export final AIG to Berkeley BLIF (combinational, 2-input .names)')
    parser.add_argument('--script', metavar='SCRIPT',
                        help='Synthesis script: semicolon-separated AIG commands, '
                             'e.g. "balance; rewrite; fraig; balance; rewrite -z". '
                             'Commands: balance, rewrite [-z] [-r N] [-K N], '
                             'refactor [-z] [-r N] [-K N], fraig. '
                             'Replaces the built-in rewrite/fraig/balance sequence.')
    parser.add_argument('--bandit', type=int, default=0, metavar='HORIZON',
                        help='Enable bandit-guided synthesis with HORIZON steps. '
                             'Adaptively selects passes (balance/rewrite/fraig/dc) '
                             'using a multi-armed bandit. Overrides --script.')
    parser.add_argument('--bandit-strategy', choices=['ucb1', 'thompson'],
                        default='ucb1',
                        help='Bandit arm-selection strategy (default: ucb1)')
    parser.add_argument('--encoding', choices=['binary', 'onehot', 'gray'],
                        default='binary',
                        help='State encoding strategy for FSM synthesis (default: binary)')
    parser.add_argument('--no-state-min', action='store_true',
                        help='Skip Hopcroft/STAMINA state minimization')
    parser.add_argument('--excitation', choices=['d', 'jk'], default='d',
                        help='Flip-flop primitive for FSM synthesis: '
                             'd (default) emits one D_i next-state bit per '
                             'state bit; jk emits (J_i, K_i) pairs via the '
                             'T-fill concretion (good for counters/toggles)')
    parser.add_argument('--atpg', action='store_true',
                        help='Run stuck-at ATPG via SAT miter and report fault coverage')
    parser.add_argument('--bmc-bound', type=int, default=None, metavar='K',
                        help='For FSM synthesis: run Bounded Model Checking (BMC) '
                             'for K clock cycles via Z3 (requires z3-solver). '
                             'UNSAT proves no divergence from the reference TT for '
                             'any input sequence of length ≤ K. Typical: 10–20.')
    parser.add_argument('--bits', type=int, default=8,
                        help='Bit width for jkcounter structural example (default: 8)')
    parser.add_argument('--compose', metavar='JSON_FILE',
                        help='Hierarchical multi-stage synthesis: path to a JSON '
                             'composition spec. See myPLAfiles/bcd_7seg_composition.json '
                             'for an example.')
    parser.add_argument('--auto-compose', action='store_true',
                        help='Automatically detect symmetric output groups in a .pla '
                             'file and run hierarchical synthesis. Generates intermediate '
                             'PLA files and a composition spec in the same directory as '
                             'the input .pla.')
    args = parser.parse_args()

    target          = args.circuit
    verbose         = not args.quiet
    bmc_bound       = args.bmc_bound
    bandit_horizon  = args.bandit
    bandit_strategy = args.bandit_strategy
    # --bandit overrides --script
    if bandit_horizon > 0:
        args.script = None

    # FSM synthesis dispatch
    if target.startswith('fsm:'):
        key = target[len('fsm:'):]
        ok = _run_fsm(key, args.encoding, verbose,
                      circ_path=args.circ, script=args.script,
                      skip_minimize=args.no_state_min,
                      excitation=args.excitation,
                      bmc_bound=bmc_bound)
        sys.exit(0 if ok else 1)
    if target == 'fsm':
        ok = True
        for key in FSM_EXAMPLES.keys():
            if not _run_fsm(key, args.encoding, verbose,
                            circ_path=None, script=args.script,
                            skip_minimize=args.no_state_min,
                            excitation=args.excitation,
                            bmc_bound=bmc_bound):
                ok = False
        sys.exit(0 if ok else 1)
    if target.endswith('.kiss') or target.endswith('.kiss2'):
        ok = _run_fsm(target, args.encoding, verbose,
                      circ_path=args.circ, script=args.script,
                      skip_minimize=args.no_state_min,
                      excitation=args.excitation,
                      bmc_bound=bmc_bound)
        sys.exit(0 if ok else 1)

    # Structural JK counter (Phase 3.5 example)
    if target == 'jkcounter':
        bits = args.bits
        print(f'\n{chr(9619) * 68}')
        print(f'  Universal Reversible JK Counter ({bits}-bit, structural AIG)')
        print(f'{chr(9619) * 68}')
        result = universal_reversible_counter(bits)
        print(f'\n  Synthesized: {result.total_nand} NAND gates  '
              f'({bits} JK-FF, {3 * bits + 2} inputs, {2 * bits} outputs)')
        ok = run_jkcounter_regression(bits, verbose=verbose)
        if args.circ:
            export_circ(result, args.circ,
                        sanitize_for_logisim(f'JKCounter_{bits}bit'))
            print(f'\n  Logisim .circ written to: {args.circ}')
        sys.exit(0 if ok else 1)

    # Benchmark regression suite
    if target == 'bench':
        rows = run_benchmarks(verbose=False, verify=args.verify or True,
                              profile=args.profile or True)
        n_fail = sum(1 for r in rows if r.get('verify') is False)
        sys.exit(0 if n_fail == 0 else 1)

    # EPFL combinational benchmarks
    if target == 'epfl':
        subset = [s.strip() for s in args.subset.split(',')] if args.subset else None
        rows = run_epfl(subset=subset,
                        script=args.script or 'rewrite; fraig; balance',
                        verify=not args.no_verify,
                        timeout=args.verify_timeout,
                        verbose=verbose)
        n_fail = sum(1 for r in rows if r.get('verify') is False
                                         or 'error' in r)
        sys.exit(0 if n_fail == 0 else 1)

    if target == 'epfl-check':
        res = check_epfl_updates(timeout=args.verify_timeout)
        ok = (not res['drifts']
              and res.get('head_commit') == res['pinned_commit'])
        sys.exit(0 if ok else 1)

    # Property-based regression
    if target == 'proptest':
        ok = run_property_tests(n_cases=args.cases, verbose=verbose)
        sys.exit(0 if ok else 1)

    if getattr(args, 'auto_compose', False) and target.endswith('.pla'):
        from .truth_table    import TruthTable as _TT
        from .auto_compose   import auto_generate_spec as _auto_spec
        import json as _json, os as _os

        pla_path = target
        if not _os.path.exists(pla_path):
            print(f"Error: PLA file '{pla_path}' not found.")
            sys.exit(1)

        tt = _TT.from_pla(pla_path)
        label = _os.path.basename(pla_path)
        stem  = _os.path.splitext(label)[0]
        pla_dir = _os.path.dirname(_os.path.abspath(pla_path))

        print(f'\n{"#" * 40}')
        print(f'  {label}  (auto-compose)')
        print(f'{"#" * 40}')
        print(f'  Inputs : {tt.input_names}')
        print(f'  Outputs: {tt.output_names}')

        result_info = _auto_spec(tt, stem=stem)

        if result_info is None:
            print('\n  [auto-compose] No symmetric output groups found.')
            print('  Falling back to flat synthesis...')
            result = optimize(tt, verbose=not args.quiet, profile=args.profile,
                              script=args.script)
            ok = run_tests(tt, result, verbose=not args.quiet)
            if args.verify:
                ok = _print_verification(tt, result) and ok
            if args.circ:
                export_circ(result, args.circ, sanitize_for_logisim(stem))
            sys.exit(0 if ok else 1)

        k          = result_info['k']
        n_pats     = result_info['n_patterns']
        g1_names   = result_info['group1_names']
        g2_names   = result_info['group2_names']
        print(f'\n  [auto-compose] Found symmetric groups:')
        print(f'    Group 1 ({len(g1_names)} outputs): {g1_names}')
        print(f'    Group 2 ({len(g2_names)} outputs): {g2_names}')
        print(f'    Distinct patterns: {n_pats}  →  {k}-bit intermediate')

        # Write generated PLAs
        inter_name, inter_str = result_info['intermediate_pla']
        dec_name,   dec_str   = result_info['decoder_pla']
        inter_path = _os.path.join(pla_dir, inter_name)
        dec_path   = _os.path.join(pla_dir, dec_name)
        with open(inter_path, 'w') as _f:
            _f.write(inter_str)
        with open(dec_path, 'w') as _f:
            _f.write(dec_str)
        print(f'  Wrote: {inter_path}')
        print(f'  Wrote: {dec_path}')

        # Write JSON spec
        spec_dict = result_info['spec']
        spec_path = _os.path.join(pla_dir, f'{stem}_auto_spec.json')
        with open(spec_path, 'w') as _f:
            _json.dump(spec_dict, _f, indent=2)
        print(f'  Wrote: {spec_path}')

        # Build stage_specs with resolved paths
        _tt_cache: dict = {}
        stage_specs = []
        for s in spec_dict['stages']:
            sp = _os.path.join(pla_dir, s['pla'])
            if sp not in _tt_cache:
                _tt_cache[sp] = _TT.from_pla(sp)
            stage_specs.append({
                'tt':      _tt_cache[sp],
                'connect': s.get('connect'),
                'rename':  s.get('rename'),
            })

        result = hierarchical_optimize(
            stage_specs,
            post_script=args.script,
            verbose=not args.quiet,
        )
        print(f'\n  Total NAND gates: {result.total_nand}')

        if args.circ:
            export_circ(result, args.circ, sanitize_for_logisim(stem))

        if args.verify:
            combined_out_names = [gn for gn, gt, _ in result.builder.gates
                                  if gt == 'OUTPUT']
            if combined_out_names == list(tt.output_names):
                result.truth_table = tt
                _print_verification(tt, result)
            else:
                print('\n  [verify] Output order differs after hierarchical synthesis; '
                      'skipping formal verification.')

        sys.exit(0)

    if args.compose:
        import json, os as _os
        spec_path = args.compose
        if not _os.path.exists(spec_path):
            print(f"Error: composition spec '{spec_path}' not found.")
            sys.exit(1)
        with open(spec_path) as _f:
            spec = json.load(_f)

        from .truth_table import TruthTable as _TT
        _pla_cache: dict = {}
        stage_specs = []
        spec_dir = _os.path.dirname(_os.path.abspath(spec_path))
        for s in spec['stages']:
            pla_path = s['pla']
            if not _os.path.isabs(pla_path):
                pla_path = _os.path.join(spec_dir, pla_path)
            if pla_path not in _pla_cache:
                _pla_cache[pla_path] = _TT.from_pla(pla_path)
            stage_specs.append({
                'tt':      _pla_cache[pla_path],
                'connect': s.get('connect'),
                'rename':  s.get('rename'),
            })

        label = _os.path.basename(spec_path)
        print(f'\n{"#" * 40}')
        print(f'  {label}  (hierarchical)')
        print(f'{"#" * 40}')

        result = hierarchical_optimize(
            stage_specs,
            post_script=args.script,
            verbose=not args.quiet,
        )
        print(f'\n  Total NAND gates: {result.total_nand}')

        if args.circ and result.aig is not None:
            all_in  = list(result.aig._input_lits.keys())
            out_names_list = [
                n for n, _ in zip(
                    [n for spec in spec['stages']
                       for n in (_pla_cache[
                           spec['pla'] if _os.path.isabs(spec['pla'])
                           else _os.path.join(spec_dir, spec['pla'])
                       ].output_names if spec.get('rename') is None
                           else [spec['rename'].get(x, x) for x in _pla_cache[
                                   spec['pla'] if _os.path.isabs(spec['pla'])
                                   else _os.path.join(spec_dir, spec['pla'])
                               ].output_names])],
                    result.out_lits
                )
            ]
            export_circ(result, args.circ, sanitize_for_logisim(label))
            print(f'  Logisim .circ written to: {args.circ}')
        sys.exit(0)

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
        if args.atpg:
            out_wires = [result[name].out_wire for name in tt.output_names]
            _print_atpg(run_atpg(result.builder.gates, tt.input_names, out_wires))
        if args.circ:
            export_circ(result, args.circ, sanitize_for_logisim(label))
        if args.dot and result.aig is not None:
            dot_str = aig_to_dot(result.aig, result.out_lits, tt.output_names,
                                 title=label)
            with open(args.dot, 'w') as f:
                f.write(dot_str)
            print(f'\n  DOT graph written to: {args.dot}')
        if args.aiger and result.aig is not None:
            _export_aiger_path(result, tt, args.aiger, label)
        if args.blif and result.aig is not None:
            _export_blif_path(result, tt, args.blif, label)
        sys.exit(0 if ok else 1)

    # Verilog input
    if target.endswith('.v') or target.endswith('.sv'):
        from .verilog_io import read_verilog, VerilogError
        if not os.path.exists(target):
            print(f"Error: Verilog file '{target}' not found.")
            sys.exit(1)
        label = os.path.basename(target)
        print(f'\n{chr(9619) * 68}')
        print(f'  {label}')
        print(f'{chr(9619) * 68}')
        try:
            result = read_verilog(target, script=args.script, verbose=verbose)
        except VerilogError as exc:
            print(f'\n  Verilog error: {exc}')
            sys.exit(1)
        tt = result.truth_table
        print(f'\n  Synthesized: {result.total_nand} NAND gates  '
              f'({tt.n_inputs} input bits, {tt.n_outputs} output bits)')
        if args.circ:
            export_circ(result, args.circ, sanitize_for_logisim(label))
        if args.dot and result.aig is not None:
            dot_str = aig_to_dot(result.aig, result.out_lits, tt.output_names,
                                 title=label)
            with open(args.dot, 'w') as f:
                f.write(dot_str)
            print(f'\n  DOT graph written to: {args.dot}')
        if args.aiger and result.aig is not None:
            _export_aiger_path(result, tt, args.aiger, label)
        if args.blif and result.aig is not None:
            _export_blif_path(result, tt, args.blif, label)
        sys.exit(0)

    # AIGER / BLIF input: load → rerun synthesis script → (optional) re-export
    if (target.endswith('.aig') or target.endswith('.aag')
            or target.endswith('.blif')):
        ok = _run_aig_file(target, verbose,
                           script=args.script,
                           aiger_path=args.aiger,
                           blif_path=args.blif,
                           dot_path=args.dot)
        sys.exit(0 if ok else 1)

    if target == 'all':
        ok = True
        for key in CIRCUITS.keys():
            if not run_one(key, verbose, args.circ,
                           verify=args.verify, profile=args.profile,
                           dot_path=args.dot, script=args.script,
                           aiger_path=args.aiger, blif_path=args.blif,
                           atpg=args.atpg,
                           bandit_horizon=bandit_horizon,
                           bandit_strategy=bandit_strategy):
                ok = False
        sys.exit(0 if ok else 1)

    if target in CIRCUITS:
        ok = run_one(target, verbose, args.circ,
                     verify=args.verify, profile=args.profile,
                     dot_path=args.dot, script=args.script,
                     aiger_path=args.aiger, blif_path=args.blif,
                     atpg=args.atpg,
                     bandit_horizon=bandit_horizon,
                     bandit_strategy=bandit_strategy)
        sys.exit(0 if ok else 1)

    avail = ", ".join(list(CIRCUITS) + ['all', 'bench', 'epfl', 'epfl-check', 'proptest'])
    print(f'\nUnknown circuit "{target}".\nAvailable: {avail}\n')
    sys.exit(1)


if __name__ == '__main__':
    main()