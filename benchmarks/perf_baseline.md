# Performance baseline — `mult4`

Fixed reference for ROADMAP P1#5 ("Python-перформанс не измерен"). Re-measure
with the exact command below before and after any acceleration PR, and commit
updated numbers alongside the change.

## Command

```bash
# Wall-clock reference (no profiler overhead)
python3 -m nand_optimizer mult4 --quiet --script "rewrite; fraig; rewrite; balance"

# Profile dump (used for the hotspot table below)
python3 -m cProfile -o benchmarks/prof_mult4.out -m nand_optimizer mult4 \
        --quiet --script "rewrite; fraig; rewrite; balance"
```

View the dump interactively:

```bash
pip install snakeviz
snakeviz benchmarks/prof_mult4.out
```

## Current numbers (2026-04-25, after QMC memoisation)

| metric                                | baseline    | bit-mask | + QMC memo | Δ vs baseline |
|---------------------------------------|------------:|---------:|-----------:|---------:|
| wall-clock (no profiler), user time   | 15.8 s      | 6.9 s    | **5.0 s**  | **3.2×** |
| wall-clock under `cProfile`           | 36.0 s      | 22.5 s   | 14.3 s     | 2.5×     |
| total function calls                  | 88 108 726  | 81 177 709 | 48 311 546 | −45%   |
| result quality (NAND count for mult4) | 414         | 414      | 414        | —        |

ROADMAP P1#5 acceptance criterion was **< 50% of baseline wall-clock
(≤ 7.9 s)**; current 5.0 s clears the bar by ~3 s. The `cProfile` run is
~2.9× slower than the plain run (profiler overhead is now a large
fraction because the hot predicates are sub-microsecond); numbers below
are `cProfile` cumtimes — use them to see **proportions**, not absolute
wall time.

## Top 12 hotspots (current, sorted by self-time)

| # | function                                         | ncalls     | tottime (s) | cumtime (s) | vs bit-mask |
|---|--------------------------------------------------|-----------:|------------:|------------:|------------:|
| 1 | `mapping/nand.py:772   eval_network` *(in tests)*|      8 192 |        2.29 |        4.27 | ~same |
| 2 | `core/implicant.py:223 quine_mccluskey`          |      4 698 |        1.85 |        4.23 | **0.33× / 0.35× cum** |
| 3 | `mapping/nand.py:781   eval_network genexpr`     |  8 960 064 |        1.42 |        1.98 | ~same |
| 4 | `synthesis/decomposition.py:111 _compose_minterm`|    871 644 |        0.94 |        1.06 | ~same |
| 5 | `core/implicant.py:185 can_combine`              | 10 452 325 |        0.88 |        1.00 | **0.32×** ncalls |
| 6 | `dict.get` *(QMC cache lookup)*                  |  5 886 042 |        0.60 |        0.60 | — |
| 7 | `core/implicant.py:129 _from_masks`              |    396 837 |        0.57 |        1.01 | 0.41× ncalls |
| 8 | `core/implicant.py:136 _from_masks genexpr`      |  3 151 875 |        0.40 |        0.40 | 0.41× ncalls |
| 9 | `synthesis/decomposition.py:244 _probe_mu`       |      4 039 |        0.35 |        1.50 | ~same |
| 10| `core/implicant.py:312 select_cover`             |      4 698 |        0.34 |        0.85 | ~same |
| 11| `core/expr.py:179      simp`                     |    238 265 |        0.31 |        0.53 | ~same |
| 12| `synthesis/rewrite.py:67 evaluate_cut_tt`        |      4 333 |        0.27 |        0.35 | ~same |

Percentages are self-time / 14.3 s. The QMC entry-point still appears at
the top because the *cache check itself* runs for every call; the savings
show up in `can_combine` (33.1 M → 10.5 M ncalls, **3.2× fewer**) and
combine/_from_masks (similar 2.4× drop) — both run only on cache misses.
The new `dict.get` row at 0.60 s self is the cache lookup itself; that is
the floor any caching layer pays.

**Cache hit rate on `mult4` synthesis** (excluding T1–T13 verification
calls which run after `optimize()` returns): 1830 hits / 2693 calls =
**68% hit rate**, final cache size 863 entries. The roadmap predicted
"~2× on the synthesis portion"; measured QMC cumtime drop is 12.0 s →
4.23 s = **2.84× cumulative**, so the prediction was conservative.

## Baseline hotspots (2026-04-24, pre-rewrite) — for diff reference

| # | function                                         | tottime (s) | cumtime (s) |
|---|--------------------------------------------------|------------:|------------:|
| 1 | `core/implicant.py:99  can_combine`              |       13.26 |       13.26 |
| 2 | `core/implicant.py:140 quine_mccluskey`          |        6.21 |       22.89 |
| 3 | `mapping/nand.py:772   eval_network`             |        2.35 |        4.36 |
| 4 | `core/implicant.py:85  subsumes`                 |        1.86 |        2.76 |
| 5 | `core/implicant.py:111 combine`                  |        1.57 |        2.39 |
| 6 | `synthesis/decomposition.py:111 _compose_minterm`|        0.98 |        1.10 |
| 7 | `synthesis/decomposition.py:244 _probe_mu`       |        0.38 |        1.57 |
| 8 | `core/implicant.py:198 select_cover`             |        0.35 |        3.39 |
| 9 | `core/expr.py:179       simp`                    |        0.33 |        0.57 |
| 10| `synthesis/rewrite.py:67 evaluate_cut_tt`        |        0.28 |        0.36 |

Under the old implementation, QMC's inner-loop predicates (`can_combine` +
`subsumes` + `combine`) cost 16.7 s self-time together — half the whole
profile. Post-rewrite they cost 4.5 s combined.

## Per-phase breakdown (`cumtime`)

| phase                                | where                                  | cumtime | share |
|--------------------------------------|----------------------------------------|--------:|------:|
| `run_tests()` (T1–T13)               | `testing/tests.py:56`                  |  21.5 s |   60% |
|   ↳ `_count_with` exhaustive T12     | `testing/tests.py:251`                 |   4.5 s |   12% |
| `optimize()` (synthesis pipeline)    | `pipeline.py:276`                      |  14.2 s |   39% |
|   ↳ `_phase2` phase-assign + decomp  | `pipeline.py:103`                      |  10.7 s |   30% |
|     ↳ `ashenhurst_decompose_*`       | `synthesis/decomposition.py:649`       |  10.7 s |   30% |
|     ↳ `phase_assign`                 | `synthesis/optimize.py:29`             |  12.6 s |   35% |
|   ↳ `_optimize_output` (phase 3)     | `pipeline.py:214`                      |   4.5 s |   13% |
| `rewrite_aig` (3 passes)             | `synthesis/rewrite.py:336`             |   0.44 s|   1.2%|
| `fraig`                              | `synthesis/fraig.py:208`               |   0.17 s|   0.5%|
| `balance_aig`                        | `synthesis/balance.py:152`             |   0.003 s|  0.0%|

> `_phase2`'s sub-rows overlap because `phase_assign` itself calls
> `quine_mccluskey`, which also runs inside the decomposition bake-off. The
> shared leaf (QMC / `can_combine`) is what dominates both.

## Takeaways — status and next steps

**Implicant bit-mask rewrite (done, 2.3× wall-clock).** `can_combine` went
from 13.26 s → 2.71 s (4.9× self-time); `subsumes` dropped out of the top-10
entirely; QMC cumulative collapsed from 22.9 s → 12.0 s. Two `int` bit-masks
(`_care`, `_value`) on every `Implicant` let the hot predicates reduce to
`(a._value ^ b._value).bit_count() == 1` and `(self._value ^ cube_value) &
self._care == 0` — C-level `int` ops, no Python-level tuple iteration.

**QMC memoisation (done, +1.4× on top → 3.2× cumulative wall).** The
4 698 `quine_mccluskey` calls on `mult4` reuse 68% of their (on-set,
dc-set, n_vars) triples across Ashenhurst-Curtis bake-off probes.
Module-level `Dict[(frozenset, frozenset, n) → Tuple[Implicant]]` cache
in [core/implicant.py](../nand_optimizer/core/implicant.py) with FIFO
eviction at 8192 entries. `Implicant` is effectively immutable (write-once
slots), so cached prime lists are safely shared. Regression coverage in
[tests/test_qmc_cache.py](../tests/test_qmc_cache.py).

**Still-expensive spots remain worth attacking if the <50% target gets
tightened further:**

1. **`_from_masks` at 1.01 s cumtime (7% of profile).** The tuple `bits`
   reconstruction is purely for external consumers — `mapping/nand.py` reads
   `imp.bits`, and QMC hashes on `imp.bits` as a dict key. If we switch the
   hash key to `(n, care, value)` (all ints, same uniqueness) and make
   `.bits` a lazy property, `_from_masks` drops to a 4-line `__new__`. ~0.5 s
   additional win.

2. **`run_tests()` is now 63% of the wall-clock** (14.2 s cumtime / 22.5 s
   profiled in the bit-mask era; similar share now). T12's `_count_with`
   simulates `eval_network` 2^n times to cross-check exact synthesis. If the
   ROADMAP target gets re-scoped to "synthesis wall-clock excluding test
   harness," we should add a `--no-tests` flag so the measurement doesn't
   include validation-only work.

3. **Numpy-FRAIG hypothesis is now decisively rejected** — see the EPFL
   `sin` profile section below.

---

## EPFL `sin` profile — FRAIG decision (2026-04-25)

Captured to settle the open question from the previous revision: "do not
start work on numpy-FRAIG without first capturing a second profile on a
FRAIG-heavy circuit." `arithmetic/sin` (24 inputs, 25 outputs, 5 416 ANDs)
was the recommended target.

```bash
python3 -c "
from nand_optimizer.io.aiger_io import read_aiger
from nand_optimizer.script import run_script
import cProfile
aig, outs, *_ = read_aiger('benchmarks/epfl/arithmetic/sin.aig')
cProfile.run(\"run_script(aig, list(outs), 'rewrite; fraig; balance', verbose=False)\",
             'benchmarks/prof_sin.out')
"
```

Wall-clock: **50.9 s** plain, **87.5 s** under cProfile. Result quality is
not the point of this profile (the script `rewrite; fraig; balance` actually
inflates sin from 5 416 → 7 191 ANDs — that is a separate pass-ordering
issue, captured in pass_eval.md).

| stage          | cumtime  | share | notes |
|----------------|---------:|------:|-------|
| `rewrite_aig`  | 55.13 s  |  63%  | dominated by `evaluate_cut_tt` (53.0 s) — bit-parallel TT eval over k-feasible cuts |
| `fraig`        | 32.31 s  |  37%  | dominated by `_check_pair` Z3 SAT calls (30.1 s) |
| `balance_aig`  |  0.06 s  |  0.1% | trivial |

**FRAIG inner breakdown** (32.31 s total):

| component                       | cumtime  | share of FRAIG |
|---------------------------------|---------:|---------------:|
| `_check_pair` (Z3 SAT)          | 30.15 s  | **93%**        |
| `_build_z3_exprs`               |  1.76 s  | 5.5%           |
| `_simulate` (sim signatures)    |  0.016 s | 0.05%          |
| `_form_classes` (bucketing)     |  0.010 s | 0.03%          |

**Decision: numpy-FRAIG is dead on arrival.** The simulation phase that
numpy would accelerate is **0.026 s out of 32.3 s — under 0.1%** of FRAIG
time even on a FRAIG-heavy circuit. Z3 SAT solving is the actual
bottleneck. The right levers are:

- **P3#10 SAT sweeping** (in progress, [sat_sweep.py](../nand_optimizer/synthesis/sat_sweep.py)) —
  ODC-aware merging, more aggressive equivalence discovery per Z3 call.
- **Incremental Z3 solver** (push/pop instead of fresh context per pair) —
  same idea as P3#12 for `resub`.
- **Better candidate pruning before SAT** (sim-based filtering of class
  pairs unlikely to be equivalent).

Numpy-FRAIG (and any GPU-FRAIG variant in TODO Phase 4.5) should be
removed from the active backlog: there is no profile in which the
simulation cost is non-trivial relative to Z3.

For comparison, on `mult4` (cube-cover front-end, no AIG ingestion) FRAIG
was 0.17 s out of 22.5 s = **0.5%**; on `sin` it is 32.3 s out of 87.5 s =
**37%**. So the original concern that mult4 was the wrong benchmark was
correct — it just turns out the conclusion (numpy-FRAIG isn't worth it)
holds at both ends of the FRAIG-share spectrum, for the same reason
(simulation isn't where the time goes).

Raw profile dump: `benchmarks/prof_sin.out`.

## Reproduction checklist

- [ ] `git log` shows HEAD at the time of measurement
- [ ] `python3 --version` recorded (baseline above: 3.13)
- [ ] CPU / core count recorded if cross-machine comparison is needed
- [ ] Raw profile at `benchmarks/prof_mult4.out` (binary pickle of pstats)

## Revision history

| date       | commit  | wall-clock | note                                             |
|------------|---------|-----------:|--------------------------------------------------|
| 2026-04-24 | acf07d4 |   15.8 s   | initial baseline                                 |
| 2026-04-24 | d9c3124 |   6.9 s    | `Implicant` bit-mask rewrite (P1#5 criterion met) |
| 2026-04-25 | *pending* |  **5.0 s** | QMC memoisation (P1#5 ч.2 #1 done)             |
