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

## Current numbers (2026-04-24, after Implicant bit-mask rewrite)

| metric                                | baseline    | current  | Δ        |
|---------------------------------------|------------:|---------:|---------:|
| wall-clock (no profiler), user time   | 15.8 s      | **6.9 s**| **2.3×** |
| wall-clock under `cProfile`           | 36.0 s      | 22.5 s   | 1.6×     |
| total function calls                  | 88 108 726  | 81 177 709 | −8%    |
| result quality (NAND count for mult4) | 414         | 414      | —        |

ROADMAP P1#5 acceptance criterion was **< 50% of baseline wall-clock
(≤ 7.9 s)**; current 6.9 s clears the bar by ~1 s. The `cProfile` run is
~3.3× slower than the plain run post-rewrite (profiler overhead is now a
larger fraction because the hot predicates are sub-microsecond); numbers
below are `cProfile` cumtimes — use them to see **proportions**, not
absolute wall time.

## Top 10 hotspots (current, sorted by self-time)

| # | function                                         | ncalls     | tottime (s) | cumtime (s) | vs baseline |
|---|--------------------------------------------------|-----------:|------------:|------------:|------------:|
| 1 | `core/implicant.py:194 quine_mccluskey`          |      4 698 |        5.60 |       12.00 | 0.90× / 0.52× cum |
| 2 | `core/implicant.py:156 can_combine`              | 33 136 624 |        2.71 |        3.04 | **0.20×** |
| 3 | `mapping/nand.py:772   eval_network` *(in tests)*|      8 192 |        2.40 |        4.44 | ~same |
| 4 | `mapping/nand.py:781   eval_network genexpr`     |  8 996 352 |        1.47 |        2.04 | ~same |
| 5 | `core/implicant.py:100 _from_masks` *(new)*      |    950 464 |        1.39 |        2.49 | — |
| 6 | `core/implicant.py:107 _from_masks genexpr`      |  7 695 904 |        0.99 |        0.99 | — |
| 7 | `synthesis/decomposition.py:111 _compose_minterm`|    871 644 |        0.94 |        1.07 | ~same |
| 8 | `core/implicant.py:162 combine`                  |    950 464 |        0.40 |        2.89 | 0.26× / 1.2× cum |
| 9 | `synthesis/decomposition.py:244 _probe_mu`       |      4 039 |        0.36 |        1.52 | ~same |
| 10| `core/implicant.py:265 select_cover`             |      4 698 |        0.35 |        0.87 | 1.00× / 0.26× cum |

Percentages are self-time / 22.5 s. `subsumes` dropped out of the top list
entirely (was 1.86 s self; now well under 0.1 s via `subsumes_masks`).
`quine_mccluskey` is still the parent of row 2 (`can_combine`); together
they are **~12.0 s cumulative (53% of the profile), down from 22.9 s (64%)**.

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

**Implicant bit-mask rewrite (done, 2.3× wall-clock win).** `can_combine` went
from 13.26 s → 2.71 s (4.9× self-time); `subsumes` dropped out of the top-10
entirely; QMC cumulative collapsed from 22.9 s → 12.0 s. Two `int` bit-masks
(`_care`, `_value`) on every `Implicant` let the hot predicates reduce to
`(a._value ^ b._value).bit_count() == 1` and `(self._value ^ cube_value) &
self._care == 0` — C-level `int` ops, no Python-level tuple iteration.
`_cube_masks()` precomputes on-cube masks once at the top of `select_cover`
so the coverage matrix doesn't recompute them O(|primes|·|cubes|) times.

**Still-expensive spots remain worth attacking if the <50% target gets
tightened:**

1. **`_from_masks` at 2.49 s cumtime (11% of profile).** The tuple `bits`
   reconstruction is purely for external consumers — `mapping/nand.py` reads
   `imp.bits`, and QMC hashes on `imp.bits` as a dict key. If we switch the
   hash key to `(n, care, value)` (all ints, same uniqueness) and make
   `.bits` a lazy property, `_from_masks` drops to a 4-line `__new__`. This
   is a ~1 s additional win.

2. **Redundant QMC invocations in decomposition bake-off.** `quine_mccluskey`
   runs **4 698 times** on `mult4` — once per bipartition probe across
   Ashenhurst search. Either (a) memoize on the cube-cover tuple (keys repeat
   across `_probe_mu` invocations for the same output), or (b) prune the
   bipartition search before re-synthesising. Potentially another 2× on the
   synthesis portion.

3. **`run_tests()` is now 63% of the wall-clock** (14.2 s cumtime / 22.5 s
   profiled). T12's `_count_with` simulates `eval_network` 2^n times to
   cross-check exact synthesis. If the ROADMAP target gets re-scoped to
   "synthesis wall-clock excluding test harness," we should add a `--no-tests`
   flag so the measurement doesn't include validation-only work.

4. **Numpy for FRAIG simulation — still unjustified on `mult4`.** FRAIG cumtime
   remains ~0.17 s. The original ROADMAP hypothesis is only worth retesting
   on an EPFL benchmark where FRAIG does nontrivial work (e.g.
   `arithmetic/sin`, 5 416 ANDs). **Do not start work on numpy-FRAIG without
   first capturing a second profile** on such a circuit.

## Reproduction checklist

- [ ] `git log` shows HEAD at the time of measurement
- [ ] `python3 --version` recorded (baseline above: 3.13)
- [ ] CPU / core count recorded if cross-machine comparison is needed
- [ ] Raw profile at `benchmarks/prof_mult4.out` (binary pickle of pstats)

## Revision history

| date       | commit  | wall-clock | note                                             |
|------------|---------|-----------:|--------------------------------------------------|
| 2026-04-24 | acf07d4 |   15.8 s   | initial baseline                                 |
| 2026-04-24 | *pending* |  **6.9 s** | `Implicant` bit-mask rewrite (P1#5 criterion met) |
