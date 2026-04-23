"""
Lightweight per-pass performance profiling.

Measures wall time and peak memory (via ``tracemalloc``) for each
named optimisation pass (Espresso, Factorisation, Shannon, AIG
rewriting, NAND mapping, ...).

Usage
-----
    from nand_optimizer import ProfileReport, profile_pass

    report = ProfileReport()
    with profile_pass('Espresso', report):
        imps = espresso(ones, dc, n)
    ...
    report.print()

The ``ProfileReport`` object accumulates rows and pretty-prints a
sorted table so exponential blow-ups become obvious early.
"""

from __future__ import annotations
from contextlib import contextmanager
from time import perf_counter
from typing import Dict, List, Optional

try:
    import tracemalloc
    _HAS_TRACEMALLOC = True
except ImportError:
    _HAS_TRACEMALLOC = False


class ProfileReport:
    """Accumulates per-pass timing and memory samples."""

    def __init__(self):
        self.rows: List[Dict] = []

    def add(self, name: str, seconds: float,
            mem_peak_kb: Optional[float] = None,
            detail: str = '') -> None:
        self.rows.append({
            'name':    name,
            'seconds': seconds,
            'mem_kb':  mem_peak_kb,
            'detail':  detail,
        })

    @property
    def total_seconds(self) -> float:
        return sum(r['seconds'] for r in self.rows)

    def print(self, title: str = 'PROFILING REPORT') -> None:
        if not self.rows:
            return
        bar = '-' * 72
        print(f'\n{bar}\n{title}\n{bar}')
        hdr = f'  {"Pass":<28} {"Time (s)":>10} {"Peak (KB)":>12}  Detail'
        print(hdr)
        print('  ' + '-' * 70)
        for r in self.rows:
            mem = f'{r["mem_kb"]:12.1f}' if r['mem_kb'] is not None else f'{"—":>12}'
            print(f'  {r["name"]:<28} {r["seconds"]:10.4f} {mem}  {r["detail"]}')
        print('  ' + '-' * 70)
        print(f'  {"TOTAL":<28} {self.total_seconds:10.4f}')
        print(bar)

    def as_dict(self) -> Dict:
        return {
            'total_seconds': self.total_seconds,
            'passes':        list(self.rows),
        }


@contextmanager
def profile_pass(name: str,
                 report: Optional[ProfileReport],
                 detail: str = ''):
    """
    Context manager that records time + peak memory of a block.

    If *report* is None this is a no-op (zero overhead beyond a
    time.perf_counter pair), so callers can freely wrap passes
    regardless of whether profiling is enabled.
    """
    if report is None:
        yield
        return

    if _HAS_TRACEMALLOC:
        started_here = not tracemalloc.is_tracing()
        if started_here:
            tracemalloc.start()
        # Snapshot baseline
        base_current, _base_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

    t0 = perf_counter()
    try:
        yield
    finally:
        dt = perf_counter() - t0
        mem_kb: Optional[float] = None
        if _HAS_TRACEMALLOC:
            _cur, peak = tracemalloc.get_traced_memory()
            mem_kb = max(0.0, (peak - base_current) / 1024.0)
            if started_here:
                tracemalloc.stop()
        report.add(name, dt, mem_kb, detail)


def profile_call(report: Optional[ProfileReport], name: str, fn, *args, **kwargs):
    """Profile a single function call and return its result."""
    with profile_pass(name, report):
        return fn(*args, **kwargs)
