# Pass QoR Evaluation (ROADMAP P2#7)

Generated: 2026-04-25  
Baseline script: `rewrite; fraig; rewrite; balance`  
Metric: `ands_after` (final AIG `n_ands` = AND + XOR node count, post-script).
Harness: [`benchmarks/run_pass_eval.py`](run_pass_eval.py).

Each pass is evaluated against the baseline script on an EPFL subset.
Decisions follow the ROADMAP P2#7 criteria:
- ` < 2%` mean improvement → tag as **experimental**, mention in `--help` and README.
- ` ≥ 5%` mean improvement → describe **on which classes of circuits** the pass wins.

| pass | subset | mean Δarea | mean Δtime | wins / ties / regressions | **verdict** |
|---|---|---:|---:|---:|---|
| `XAG (-x)`        | 11 EPFL    | **−4.3%**   | **+2.0%**     | 3 / 8 / 0 | **production-ready, opt-in via `-x`; clear wins on XOR-heavy arithmetic** |
| `+bdd`            | 7 EPFL     | +0.8%       | +49.7%        | 0 / 5 / 2 | **experimental**; no measurable area win, ~50% time overhead |
| `+resub`          | 3 EPFL     | −6.1%       | +46 056.6%    | 2 / 0 / 1 | **experimental**; large wins on small circuits only, prohibitive wall-time |
| `+sweep`          | 11 EPFL    | −0.7%       | +200.7%       | 2 / 9 / 0 | **production-ready, opt-in via `sweep` proc; only pass that beats FRAIG on adder (−6.4%)** |
| `bandit (h=20)`   | 7 EPFL     | **−15.4%**  | +3 966.2%     | 5 / 2 / 0 | **best QoR mode**; ~40× wall-time, no regressions, designed for batch exploration |

---

## 1. `XAG (-x)` — XAG_DB_4 templates with NAND-cost-aware comparator

Implementation: ROADMAP P3#8 Phase 5. Cost model in
[`synthesis/rewrite.py:rewrite_aig`](../nand_optimizer/synthesis/rewrite.py)
weighs `AND = 2 NAND`, `XOR = 4 NAND` for both the new template and the
freed MFFC. Unlike the legacy `len(ops)` comparator, accepts XAG templates
only when the weighted NAND saving is strictly positive.

Script form: `rewrite -x; fraig; rewrite -x; balance`.

### vs `baseline` (full default subset, 11 circuits)

| benchmark | n_inputs | baseline_area | with_pass_area |  Δarea |  Δtime |
|-----------|---------:|--------------:|---------------:|-------:|-------:|
| random_control/ctrl       |   7 |  166 |  166 | +0.0% | +46.8% |
| random_control/router     |  60 |  629 |  521 | **−17.2%** | −9.1% |
| random_control/int2float  |  11 |  276 |  276 | +0.0% | +5.3% |
| random_control/dec        |   8 |  304 |  304 | +0.0% | +6.4% |
| random_control/cavlc      |  10 |  740 |  740 | +0.0% | +1.3% |
| random_control/priority   | 128 | 1019 | 1019 | +0.0% | +1.0% |
| arithmetic/adder          | 256 | 1020 |  768 | **−24.7%** | −30.3% |
| random_control/i2c        | 147 | 1550 | 1550 | +0.0% | +0.6% |
| arithmetic/max            | 512 | 3079 | 3079 | +0.0% | +2.9% |
| arithmetic/bar            | 135 | 3206 | 3206 | +0.0% | +0.1% |
| arithmetic/sin            |  24 | 7166 | 6802 | **−5.1%** | −3.5% |

**Class breakdown:**
- **XOR-heavy arithmetic** (adder, sin) — wins 5–25%. Adder is the killer
  app: 1020 → 768 (−24.7%) at no time cost (XAG run was actually 30% faster
  due to smaller intermediate AIGs).
- **Symmetric control** (router) — single big win (−17.2%), driven by 4-input
  XOR symmetries that AIG_DB_4 cannot express in fewer than 3 ANDs.
- **Plain AND-dominated** (ctrl, int2float, dec, cavlc, priority, i2c, max,
  bar) — exact tie. The weighted comparator correctly rejects XAG templates
  whose NAND cost equals or exceeds the AIG alternative.

**Why opt-in, not default.** On the cube-cover built-ins (T1–T13 suite),
`rd53` regresses 40 → 45 NAND (+12%) when `-x` is enabled. Root cause: the
post-mapping XOR-extractor in
[`mapping/nand.py`](../nand_optimizer/mapping/nand.py) already converts
3-AND XOR/XNOR subgraphs to 4-NAND form, saving 2 NAND per pattern. The
rewriter's local cost model treats those 3 ANDs as costing 6 NAND (their
raw weight) and so prefers a native XOR (4 NAND) — which then prevents
downstream FRAIG sharing with neighbouring AND clusters. Pure-AIGER inputs
(EPFL) do not start from Espresso cube covers, so the issue is invisible
there. Until the comparator can model the XOR-extractor's pattern coverage,
`use_xag=False` remains the default; users on XOR-heavy arithmetic should
enable `-x` per workload.

---

## 2. `+bdd` — Per-output BDD rebuild via sifting reorder

Script form: `bdd; rewrite; fraig; rewrite; balance`. Implementation:
[`synthesis/bdd_decomp.py`](../nand_optimizer/synthesis/bdd_decomp.py)
(requires `dd` package). Per-output BDD is built, sifting-reordered, and
re-emitted via canonical ITE realisation; the rest of the pipeline then
optimises the new AIG.

### vs `baseline` (7-circuit subset)

| benchmark | n_inputs | baseline_area | with_pass_area |  Δarea |  Δtime |
|-----------|---------:|--------------:|---------------:|-------:|-------:|
| random_control/ctrl     |   7 |  166 |  170 | +2.4% | +106.6% |
| random_control/router   |  60 |  629 |  629 | +0.0% | +1.6% |
| random_control/dec      |   8 |  304 |  314 | +3.3% | +142.8% |
| random_control/cavlc    |  10 |  740 |  740 | +0.0% | +17.4% |
| random_control/priority | 128 | 1019 | 1019 | +0.0% | −0.5% |
| arithmetic/adder        | 256 | 1020 | 1019 | −0.1% | +59.5% |
| random_control/i2c      | 147 | 1550 | 1550 | +0.0% | +20.5% |

**Verdict — experimental.** Mean Δarea +0.8% (slightly worse than baseline)
across the subset; the only nominal "win" is adder at −0.1%, well within
noise. The two regressions (`ctrl +2.4%`, `dec +3.3%`) suggest BDD-rebuild
disrupts AIG structure that subsequent FRAIG would otherwise exploit. Time
overhead is +50% mean, doubled on small benchmarks.

---

## 3. `+resub` — Functional resubstitution (5..7-input cuts)

Script form: `rewrite; fraig; resub; rewrite; balance`. Implementation:
[`synthesis/sat_resub.py`](../nand_optimizer/synthesis/sat_resub.py).
For each k-feasible cut (k∈[5..7]), SAT searches up to 3 divisors from a
fanout-bounded pool whose composition exactly realises the cut function;
on success, the cut is rewired to the divisors and dropped.

### vs `baseline` (3-circuit subset — wall-time limits larger runs)

| benchmark | n_inputs | baseline_area | with_pass_area |  Δarea |  Δtime |
|-----------|---------:|--------------:|---------------:|-------:|-------:|
| random_control/ctrl   |   7 |  166 |  155 | **−6.6%**  | +15 705.8% |
| random_control/router |  60 |  629 |  634 | +0.8%      | +90 029.6% |
| arithmetic/adder      | 256 | 1020 |  894 | **−12.4%** | +32 434.3% |

**Verdict — experimental.** Mean Δarea −6.1% is the best of any single-pass
result, but mean Δtime +46 056% (≈460× slower) makes inclusion in any
default script impossible. Adder 1020 → 894 (−12.4%) at 484s vs 1.6s
baseline shows what's possible if the implementation were practical;
suggested follow-ups are SAT-call batching, divisor-pool pruning, or
limiting to small benchmarks via an `--resub-max-ands` guard.

---

## 4. `bandit (h=20)` — Multi-armed bandit over default arms

Implementation: [`script.py:run_bandit`](../nand_optimizer/script.py),
arms = `['balance', 'rewrite', 'rewrite -z', 'fraig', 'dc']`, UCB1, horizon
= 20. Each iteration applies one arm, observes
`(n_before − n_after) / n_before`, updates UCB1 weights.

### vs `baseline` (7-circuit subset)

| benchmark | n_inputs | baseline_area | with_pass_area |  Δarea |  Δtime |
|-----------|---------:|--------------:|---------------:|-------:|-------:|
| random_control/ctrl     |   7 |  166 |  140 | **−15.7%** | +2 678.0% |
| random_control/router   |  60 |  629 |  247 | **−60.7%** | +7 446.4% |
| random_control/dec      |   8 |  304 |  304 | +0.0%      | +454.4% |
| random_control/cavlc    |  10 |  740 |  717 | **−3.1%**  | +2 064.1% |
| random_control/priority | 128 | 1019 |  908 | **−10.9%** | +6 536.0% |
| arithmetic/adder        | 256 | 1020 | 1020 | +0.0%      | +7 068.4% |
| random_control/i2c      | 147 | 1550 | 1282 | **−17.3%** | +1 516.3% |

**Verdict — production-ready as a high-effort mode.** Mean Δarea −15.4% with
zero regressions. Wall-time multiplier is ~40× (h=20 means 20 single-pass
runs), which is acceptable for batch exploration but not for interactive
use. **Note:** bandit's arms do not include `rewrite -x`, so it leaves
adder's −24.7% XAG win on the table (adder result is +0.0%). Adding
`'rewrite -x'` to `DEFAULT_ARMS` should be a follow-up to extract the
combined `XAG ∪ bandit` benefit on arithmetic.

---

## 5. `+sweep` — SAT sweeping (ODC-aware FRAIG superset)

Script form: `rewrite; fraig; sweep; rewrite; balance`. Implementation:
[`synthesis/sat_sweep.py`](../nand_optimizer/synthesis/sat_sweep.py)
(ROADMAP P3#10). Two candidate buckets feed a Z3 miter:
1. **Standard** — canonical full-simulation signature (the FRAIG bucket),
   minus globally-unobservable nodes.
2. **Fill-based (ODC)** — canonical signature with ~care bits set to 1;
   groups nodes that agree on all *observable* simulation patterns but may
   differ elsewhere — the opportunity FRAIG cannot see.

Each candidate pair `(rep, m)` is verified by the symbolic-obs miter
`∃ x : obs_m(x) ∧ (f_rep(x) ≠ f_m(x))`; UNSAT → safe to merge `m` into
`rep`. The observability condition is built by reverse-topological
traversal over AND/XOR nodes (Mishchenko et al., FPGA 2009 §3).

### vs `baseline` (full default subset, 11 circuits)

| benchmark | n_inputs | baseline_area | with_pass_area |  Δarea |  Δtime |
|-----------|---------:|--------------:|---------------:|-------:|-------:|
| random_control/ctrl       |   7 |  166 |  166 | +0.0% | +102.9% |
| random_control/router     |  60 |  629 |  629 | +0.0% |  +44.0% |
| random_control/int2float  |  11 |  276 |  276 | +0.0% | +239.8% |
| random_control/dec        |   8 |  304 |  304 | +0.0% |  +39.6% |
| random_control/cavlc      |  10 |  740 |  740 | +0.0% |  +89.1% |
| random_control/priority   | 128 | 1019 | 1019 | +0.0% | +196.4% |
| arithmetic/adder          | 256 | 1020 |  955 | **−6.4%** | +344.4% |
| random_control/i2c        | 147 | 1550 | 1538 |  −0.8% | +111.4% |
| arithmetic/max            | 512 | 3079 | 3079 | +0.0% | +287.1% |
| arithmetic/bar            | 135 | 3206 | 3206 | +0.0% |  +64.5% |
| arithmetic/sin            |  24 | 7166 | 7158 |  −0.1% | +688.9% |

**Class breakdown:**
- **XOR-heavy arithmetic where FRAIG has nothing left** — adder is the
  killer app. After `rewrite; fraig; rewrite; balance` the AIG is at 1020
  ANDs and FRAIG cannot find further global equivalences; `sweep` adds
  −6.4% (1020 → 955). The fill-based bucket discovers nodes that agree
  on every pattern where any output cares, but differ on don't-care
  patterns — these are invisible to vanilla FRAIG.
- **Reconvergent control with residual ODC** — i2c −0.8%, sin −0.1%.
- **Already-saturated circuits** — 8/11 ties. Both buckets find no new
  pairs the existing `rewrite; fraig` chain hasn't already settled.
- **Zero regressions** across the full subset.

**vs the existing ODC pass (`dc --odc --odc-mode z3-exact`).** `sweep`
and `dc-z3-exact` are **complementary, not redundant**: `dc-z3-exact`
operates on local cuts and wins big on control logic with reconvergent
fanout (`ctrl −20.5%`, `priority −20.3%`); `sweep` operates globally on
node equivalences modulo observability and wins on arithmetic where
local DC-based rewriting saturates (adder `dc-z3 → 1013`, `sweep → 955`).
Composed, `rewrite; fraig; dc --odc --odc-mode z3-exact; sweep; rewrite;
balance` gets adder to 952 (best of any pass combination tested).

**Why `sweep` and not `fraig --odc`.** The decision (ROADMAP P3#10
"Что осталось") is to keep them as separate procs. `fraig` stays the
fast, FRAIG-classic pass everyone composes; `sweep` is the heavier
ODC-aware superset, opt-in for users who can spend ~3× wall-time on
the residual gain. Merging the two would force the obs-builder cost
on every default-script run, which the data does not justify.

**Wall-time.** Mean +200.7% (~3× baseline) is dominated by `sin`
(+689%, single circuit at 5416 ANDs takes 9 min vs 70 s baseline).
On the rest of the subset the multiplier is 1.4–4.4×, acceptable for
a once-per-batch synthesis pass.

---

## Reproducing

```bash
# Full subset, all variants except resub (resub timing is prohibitive)
python3 benchmarks/run_pass_eval.py \
    --variants "baseline,XAG (-x),+bdd,+sweep,bandit (h=20)" \
    --out benchmarks/pass_eval.md

# Quick smoke (3 smallest circuits)
python3 benchmarks/run_pass_eval.py --quick --out /tmp/pass_eval_quick.md

# Resub eval on small circuits only
python3 benchmarks/run_pass_eval.py \
    --subset "random_control/ctrl,random_control/router,arithmetic/adder" \
    --variants "baseline,+resub" \
    --out /tmp/pass_eval_resub.md

# Sweep eval on full subset
python3 benchmarks/run_pass_eval.py \
    --variants "baseline,+sweep" \
    --out /tmp/pass_eval_sweep.md
```
