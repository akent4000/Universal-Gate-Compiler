"""
EPFL Combinational Benchmark Suite runner.

Vendors a snapshot of the `lsils/benchmarks` repository under
[benchmarks/epfl/](../benchmarks/epfl/) — arithmetic (adder, bar, div, hyp,
log2, max, multiplier, sin, sqrt, square) and random_control (arbiter, cavlc,
ctrl, dec, i2c, int2float, mem_ctrl, priority, router, voter). Each `.aig` is
pinned to an upstream commit + sha256 via `benchmarks/epfl/manifest.json`;
`check_epfl_updates()` re-fetches the raw files from GitHub and diffs the
hashes, without touching the local tree.

Public entry points:

    run_epfl(subset=None, script='rewrite; fraig; balance',
             verify=True, timeout=60.0)
    check_epfl_updates(timeout=30.0)

Run via CLI:

    python -m nand_optimizer epfl               # arithmetic + random_control
    python -m nand_optimizer epfl --subset arithmetic/adder,random_control/ctrl
    python -m nand_optimizer epfl --no-verify --script "balance; rewrite"
    python -m nand_optimizer epfl-check         # audit upstream for updates
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core.aig    import AIG, Lit
from ..io.aiger_io import read_aiger
from ..script      import run_script


# ── paths ────────────────────────────────────────────────────────────────────

_PKG_DIR     = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT   = os.path.dirname(_PKG_DIR)
EPFL_ROOT    = os.path.join(_REPO_ROOT, 'benchmarks', 'epfl')
MANIFEST     = os.path.join(EPFL_ROOT, 'manifest.json')


# ── manifest ────────────────────────────────────────────────────────────────

def load_manifest() -> Dict[str, Any]:
    if not os.path.exists(MANIFEST):
        raise FileNotFoundError(
            f'EPFL manifest not found at {MANIFEST}. '
            f'Benchmarks directory may be missing from this checkout.'
        )
    with open(MANIFEST, 'r', encoding='utf-8') as f:
        return json.load(f)


def _abs_path(rel: str) -> str:
    return os.path.join(EPFL_ROOT, rel)


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
#  AIG-vs-AIG combinational equivalence check (SAT miter)
# ═══════════════════════════════════════════════════════════════════════════════

def _aig_to_z3(z3mod, aig: AIG, out_lits: List[Lit], input_vars):
    """Translate an AIG to a list of z3 BoolRefs for the given outputs.

    `input_vars` is a dict {input_name: z3.Bool} shared across both AIGs so
    that they see the same primary-input symbols.
    """
    wire: Dict[int, Any] = {0: z3mod.BoolVal(False)}   # node 0 = const-FALSE
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            name = entry[1]
            wire[nid] = input_vars[name]
        else:
            a_lit, b_lit = entry[1], entry[2]
            wire[nid] = z3mod.And(_resolve(z3mod, wire, a_lit),
                                  _resolve(z3mod, wire, b_lit))

    return [_resolve(z3mod, wire, l) for l in out_lits]


def _resolve(z3mod, wire: Dict[int, Any], lit: Lit):
    nid  = lit >> 1
    comp = lit & 1
    val  = wire[nid]
    return z3mod.Not(val) if comp else val


def aig_equivalence(
    ref_aig: AIG, ref_outs: List[Lit],
    new_aig: AIG, new_outs: List[Lit],
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Combinational equivalence check between two AIGs with matching interfaces.

    Returns {'equivalent': True/False/None, 'method': 'z3'|'z3-timeout'|'unavailable',
             'seconds': float, 'counterexample': {input_name: 0|1} | None}.
    """
    t0 = time.perf_counter()
    try:
        import z3
    except ImportError:
        return {'equivalent': None, 'method': 'unavailable',
                'seconds': 0.0, 'counterexample': None}

    if len(ref_outs) != len(new_outs):
        return {'equivalent': False, 'method': 'interface-mismatch',
                'seconds': 0.0, 'counterexample': None,
                'reason': f'output count {len(ref_outs)} vs {len(new_outs)}'}

    # Shared primary-input variables — use names from ref_aig.
    in_names = list(ref_aig.input_names())
    if list(new_aig.input_names()) != in_names:
        return {'equivalent': False, 'method': 'interface-mismatch',
                'seconds': 0.0, 'counterexample': None,
                'reason': 'input-name order differs'}

    input_vars = {n: z3.Bool(n) for n in in_names}
    ref_form = _aig_to_z3(z3, ref_aig, ref_outs, input_vars)
    new_form = _aig_to_z3(z3, new_aig, new_outs, input_vars)

    xors = [z3.Xor(a, b) for a, b in zip(ref_form, new_form)]
    miter = z3.Or(*xors) if xors else z3.BoolVal(False)

    s = z3.Solver()
    s.set('timeout', int(timeout * 1000))
    s.add(miter)
    status = s.check()
    dt = time.perf_counter() - t0

    if status == z3.unsat:
        return {'equivalent': True, 'method': 'z3',
                'seconds': dt, 'counterexample': None}
    if status == z3.sat:
        m = s.model()
        cex = {n: (1 if z3.is_true(m.eval(input_vars[n], model_completion=True))
                    else 0) for n in in_names}
        return {'equivalent': False, 'method': 'z3',
                'seconds': dt, 'counterexample': cex}
    return {'equivalent': None, 'method': 'z3-timeout',
            'seconds': dt, 'counterexample': None}


# ═══════════════════════════════════════════════════════════════════════════════
#  Single-benchmark and full-suite runners
# ═══════════════════════════════════════════════════════════════════════════════

def _aig_depth(aig: AIG, out_lits: List[Lit]) -> int:
    """Logic depth (max AND-gate level from PIs to any PO)."""
    level: List[int] = [0] * (len(aig._nodes) + 1)
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            level[nid] = 0
        else:
            a_nid = entry[1] >> 1
            b_nid = entry[2] >> 1
            level[nid] = max(level[a_nid], level[b_nid]) + 1
    return max((level[l >> 1] for l in out_lits), default=0)


def run_one_epfl(
    key:     str,
    script:  str = 'rewrite; fraig; balance',
    verify:  bool = True,
    timeout: float = 60.0,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Run a single EPFL benchmark (e.g. 'arithmetic/adder').

    Returns a row dict ready for the summary table.
    """
    path = _abs_path(key + '.aig')
    if not os.path.exists(path):
        raise FileNotFoundError(f'benchmark file missing: {path}')

    ref_aig, ref_outs, in_names, out_names = read_aiger(path)
    depth_before = _aig_depth(ref_aig, ref_outs)
    ands_before  = ref_aig.n_ands

    t0 = time.perf_counter()
    new_aig, new_outs = run_script(ref_aig, list(ref_outs), script, verbose)
    script_dt = time.perf_counter() - t0

    # Re-parse the on-disk reference AIG for the miter (run_script may mutate).
    if verify:
        ref_aig2, ref_outs2, _, _ = read_aiger(path)
        v = aig_equivalence(ref_aig2, ref_outs2, new_aig, new_outs,
                            timeout=timeout)
    else:
        v = {'equivalent': None, 'method': 'skipped',
             'seconds': 0.0, 'counterexample': None}

    return {
        'key':          key,
        'n_inputs':     len(in_names),
        'n_outputs':    len(out_names),
        'ands_before':  ands_before,
        'ands_after':   new_aig.n_ands,
        'depth_before': depth_before,
        'depth_after':  _aig_depth(new_aig, new_outs),
        'script':       script,
        'script_sec':   script_dt,
        'verify':       v['equivalent'],
        'verify_method': v['method'],
        'verify_sec':   v['seconds'],
        'counterexample': v.get('counterexample'),
    }


def run_epfl(
    subset:  Optional[Iterable[str]] = None,
    script:  str = 'rewrite; fraig; balance',
    verify:  bool = True,
    timeout: float = 60.0,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """Run the EPFL suite (or a subset) and print a summary table.

    `subset` items are manifest keys like 'arithmetic/adder' (with or without
    the .aig extension). `None` runs every benchmark in the manifest.
    """
    manifest = load_manifest()
    all_keys = [k[:-4] if k.endswith('.aig') else k
                for k in sorted(manifest['files'])]
    if subset is None:
        keys = all_keys
    else:
        norm = [s[:-4] if s.endswith('.aig') else s for s in subset]
        unknown = [k for k in norm if k not in all_keys]
        if unknown:
            raise KeyError(f'Unknown EPFL benchmarks: {unknown}. '
                           f'Available: {all_keys}')
        keys = norm

    bar = '=' * 88
    print(f'\n{bar}')
    print('  EPFL COMBINATIONAL BENCHMARK SUITE')
    print(f'  script  : {script}')
    print(f'  verify  : {"z3 miter (timeout=%.0fs)" % timeout if verify else "OFF"}')
    print(f'  commit  : {manifest["upstream_commit"][:10]}')
    print(bar)

    rows: List[Dict[str, Any]] = []
    for k in keys:
        print(f'\n  >> {k}')
        try:
            row = run_one_epfl(k, script=script, verify=verify,
                               timeout=timeout, verbose=verbose)
        except Exception as e:
            print(f'     ERROR: {e}')
            rows.append({'key': k, 'error': str(e)})
            continue
        rows.append(row)
        delta = row['ands_after'] - row['ands_before']
        pct   = (100.0 * delta / row['ands_before']) if row['ands_before'] else 0.0
        vs    = _verdict_text(row)
        print(f"     ANDs {row['ands_before']} → {row['ands_after']}  "
              f"({delta:+d}, {pct:+.1f}%)  "
              f"depth {row['depth_before']} → {row['depth_after']}  "
              f"[{row['script_sec']:.2f}s synth, {vs}]")

    _print_epfl_table(rows, manifest)
    return rows


def _verdict_text(row: Dict[str, Any]) -> str:
    v = row.get('verify')
    m = row.get('verify_method', '')
    if v is True:
        return f'verified ({row["verify_sec"]:.1f}s)'
    if v is False:
        return 'MISMATCH'
    if m == 'skipped':
        return 'verify skipped'
    if m == 'z3-timeout':
        return f'verify timeout ({row["verify_sec"]:.0f}s)'
    if m == 'unavailable':
        return 'z3 missing'
    return 'verify unknown'


def _print_epfl_table(rows: List[Dict[str, Any]], manifest: Dict[str, Any]) -> None:
    bar = '-' * 88
    print(f'\n{bar}')
    print('  SUMMARY')
    print(bar)
    print(f'  {"benchmark":<28}{"I":>4} {"O":>4} '
          f'{"AND in":>8} {"AND out":>8} {"Δ%":>7} '
          f'{"depth":>11} {"verify":>16}')
    print('  ' + '-' * 86)
    n_fail = 0
    n_to   = 0
    for r in rows:
        if 'error' in r:
            print(f'  {r["key"]:<28}ERROR: {r["error"]}')
            n_fail += 1
            continue
        pct = (100.0 * (r["ands_after"] - r["ands_before"]) / r["ands_before"]
               if r["ands_before"] else 0.0)
        depth = f'{r["depth_before"]}→{r["depth_after"]}'
        vs = _verdict_text(r)
        if r.get('verify') is False:
            n_fail += 1
        if r.get('verify_method') == 'z3-timeout':
            n_to += 1
        print(f'  {r["key"]:<28}{r["n_inputs"]:>4} {r["n_outputs"]:>4} '
              f'{r["ands_before"]:>8} {r["ands_after"]:>8} {pct:>6.1f}% '
              f'{depth:>11} {vs:>16}')
    print(bar)
    total_in  = sum(r.get('ands_before', 0) for r in rows if 'error' not in r)
    total_out = sum(r.get('ands_after', 0) for r in rows if 'error' not in r)
    total_pct = (100.0 * (total_out - total_in) / total_in) if total_in else 0.0
    print(f'  TOTAL ANDs: {total_in} → {total_out} ({total_pct:+.1f}%)  '
          f'mismatches: {n_fail}   verify-timeouts: {n_to}')
    print(bar)


# ═══════════════════════════════════════════════════════════════════════════════
#  Upstream update check
# ═══════════════════════════════════════════════════════════════════════════════

def check_epfl_updates(timeout: float = 30.0) -> Dict[str, Any]:
    """Compare local manifest sha256 against the live upstream raw files.

    Does *not* mutate the local tree. Prints a diff and returns structured
    results.

    The manifest pins every benchmark to a specific upstream commit, so this
    check can fail in two ways:
      - `drift`    : the file at the pinned commit no longer hashes the same
                     (shouldn't happen on GitHub; indicates local tampering).
      - `upstream` : HEAD in `lsils/benchmarks` has moved past the pinned
                     commit (a new canonical version of the suite may exist).
    """
    manifest = load_manifest()
    pinned_commit = manifest['upstream_commit']

    print(f'\n{"=" * 78}')
    print('  EPFL BENCHMARK UPDATE CHECK')
    print(f'  pinned commit : {pinned_commit}')
    print(f'  upstream repo : {manifest["upstream_repo"]}')
    print('=' * 78)

    # 1) Compare local hashes against re-fetched files at the pinned commit.
    drifts: List[Tuple[str, str]] = []   # (key, reason)
    ok_count = 0
    for key, entry in sorted(manifest['files'].items()):
        local_path = _abs_path(key)
        if not os.path.exists(local_path):
            drifts.append((key, 'missing locally'))
            continue

        local_hash = _file_sha256(local_path)
        if local_hash != entry['sha256']:
            drifts.append((key, f'local hash {local_hash[:10]} != '
                                f'manifest {entry["sha256"][:10]}'))
            continue

        upstream_hash, err = _fetch_sha256(entry['source_url'], timeout=timeout)
        if err:
            drifts.append((key, f'fetch error: {err}'))
            continue
        if upstream_hash != entry['sha256']:
            drifts.append((key, f'upstream-at-pin hash {upstream_hash[:10]} '
                                f'!= manifest {entry["sha256"][:10]}'))
            continue
        ok_count += 1
        print(f'  [ok]   {key}  {entry["sha256"][:10]}')

    # 2) Check whether upstream HEAD has advanced past the pinned commit.
    head_info = _fetch_upstream_head(timeout=timeout)

    print(f'\n  {ok_count} / {len(manifest["files"])} files match manifest + upstream at pin.')
    if drifts:
        print('\n  drift:')
        for k, r in drifts:
            print(f'    [drift] {k}  — {r}')

    if head_info.get('error'):
        print(f'\n  HEAD check failed: {head_info["error"]}')
    else:
        head_sha = head_info['sha']
        print(f'\n  upstream HEAD : {head_sha}')
        if head_sha == pinned_commit:
            print('  status        : pin is up-to-date with HEAD.')
        else:
            print(f'  status        : upstream has advanced. '
                  f'Consider re-pinning to {head_sha[:10]}.')
            print(f'                  date: {head_info.get("date", "?")}')
            print(f'                  url : {head_info.get("url", "?")}')

    print('=' * 78)

    return {
        'pinned_commit': pinned_commit,
        'head_commit':   head_info.get('sha'),
        'head_error':    head_info.get('error'),
        'ok':            ok_count,
        'total':         len(manifest['files']),
        'drifts':        drifts,
    }


def _fetch_sha256(url: str, timeout: float) -> Tuple[Optional[str], Optional[str]]:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'nand_optimizer-epfl-check'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return hashlib.sha256(data).hexdigest(), None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, str(e)


# ═══════════════════════════════════════════════════════════════════════════════
#  Download helper
# ═══════════════════════════════════════════════════════════════════════════════

# All files relative to EPFL_ROOT; source path in lsils/benchmarks is
# combinational/aig/{key}.
_ALL_FILES = [
    'arithmetic/adder.aig',
    'arithmetic/bar.aig',
    'arithmetic/div.aig',
    'arithmetic/hyp.aig',
    'arithmetic/log2.aig',
    'arithmetic/max.aig',
    'arithmetic/multiplier.aig',
    'arithmetic/sin.aig',
    'arithmetic/sqrt.aig',
    'arithmetic/square.aig',
    'random_control/arbiter.aig',
    'random_control/cavlc.aig',
    'random_control/ctrl.aig',
    'random_control/dec.aig',
    'random_control/i2c.aig',
    'random_control/int2float.aig',
    'random_control/mem_ctrl.aig',
    'random_control/priority.aig',
    'random_control/router.aig',
    'random_control/voter.aig',
]

_UPSTREAM_REPO = 'https://github.com/lsils/benchmarks'
_RAW_BASE      = 'https://raw.githubusercontent.com/lsils/benchmarks'


def download_epfl(
    subset:  Optional[Iterable[str]] = None,
    commit:  Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Fetch EPFL benchmark .aig files from GitHub and write manifest.json.

    If *commit* is None the current HEAD of lsils/benchmarks is resolved first.
    If *subset* is None every file in `_ALL_FILES` is downloaded.

    Returns {'ok': int, 'failed': list-of-key, 'commit': sha}.
    """
    # Resolve commit.
    if commit is None:
        head = _fetch_upstream_head(timeout=timeout)
        if 'error' in head:
            raise RuntimeError(f'Could not resolve lsils/benchmarks HEAD: {head["error"]}')
        commit = head['sha']

    # Normalise subset keys (strip .aig extension for comparison, then re-add).
    if subset is not None:
        norm = set(s if s.endswith('.aig') else s + '.aig' for s in subset)
        files = [f for f in _ALL_FILES if f in norm]
        unknown = norm - set(_ALL_FILES)
        if unknown:
            raise KeyError(f'Unknown EPFL benchmarks: {sorted(unknown)}')
    else:
        files = list(_ALL_FILES)

    os.makedirs(EPFL_ROOT, exist_ok=True)
    for cat in ('arithmetic', 'random_control'):
        os.makedirs(os.path.join(EPFL_ROOT, cat), exist_ok=True)

    manifest_files: Dict[str, Any] = {}
    failed: List[str] = []

    for key in files:
        src_url = f'{_RAW_BASE}/{commit}/combinational/aig/{key}'
        local   = _abs_path(key)
        print(f'  download {key} ...', end=' ', flush=True)
        try:
            req = urllib.request.Request(
                src_url, headers={'User-Agent': 'nand_optimizer-epfl-download'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            with open(local, 'wb') as f:
                f.write(data)
            sha = hashlib.sha256(data).hexdigest()
            manifest_files[key] = {'sha256': sha, 'source_url': src_url}
            print(f'ok ({len(data)} bytes)')
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError) as e:
            print(f'FAILED: {e}')
            failed.append(key)

    # Update manifest: preserve existing entries for files not in this batch.
    existing: Dict[str, Any] = {}
    if os.path.exists(MANIFEST):
        with open(MANIFEST, 'r', encoding='utf-8') as f:
            try:
                existing = json.load(f).get('files', {})
            except json.JSONDecodeError:
                pass
    existing.update(manifest_files)

    manifest = {
        'upstream_commit': commit,
        'upstream_repo':   _UPSTREAM_REPO,
        'files':           existing,
    }
    with open(MANIFEST, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')

    print(f'\n  manifest written: {MANIFEST}')
    print(f'  downloaded {len(manifest_files)}, failed {len(failed)}')
    return {'ok': len(manifest_files), 'failed': failed, 'commit': commit}


def _fetch_upstream_head(timeout: float) -> Dict[str, Any]:
    """Query GitHub's REST API for the current HEAD of the benchmarks repo."""
    url = 'https://api.github.com/repos/lsils/benchmarks/commits/master'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent':  'nand_optimizer-epfl-check',
            'Accept':      'application/vnd.github+json',
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode('utf-8'))
        sha = payload.get('sha', '')
        date = payload.get('commit', {}).get('author', {}).get('date', '?')
        return {'sha': sha, 'date': date, 'url': payload.get('html_url', '?')}
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ValueError, OSError) as e:
        return {'error': str(e)}
