# Universal Gate Compiler

> **Universal logic compiler — from truth table or FSM to minimal NAND network**

Takes any combinational logic function (truth table, `.pla`, `.aig`/`.aag`, `.blif`) **or a finite-state machine** (Python `StateTable` / KISS2) and produces a globally optimised NAND-only gate network, with formal verification, Logisim Evolution export, and AIGER/BLIF interchange with ABC / Yosys / EPFL tooling.

---

## Pipeline

```
TruthTable / .pla / .aig / .blif  ──┐
                                    │
StateTable / KISS2 FSM ─────────────┤ (excitation logic → truth table)
                                    │
    ├─[1] Multi-output Espresso         — minimise to Sum-of-Products (shared cubes)
    ├─[2] Phase Assignment              — choose f or ~f (fewer literals wins)
    ├─[3] Algebraic Factorization       — Brayton kernel/co-kernel extraction
    ├─[4] Cross-output Factorization    — expose shared literals across outputs
    ├─[5] Functional Decomposition      — Ashenhurst-Curtis / Roth-Karp bipartition
    ├─[3.5] Shared-Support Decomposition — joint column-multiplicity bus across outputs
    ├─[6] AIG Construction              — structural hashing (And-Inverter Graph)
    │        └─ Greedy Reassociation    — reorder AND-chains for cache reuse
    ├─[7]  AIG Rewriting                — fanout-aware MFFC cut rewriting
    │        └─ Exact Synthesis (SAT)   — Z3-based optimal 4-cut templates
    ├─[7.3] Bi-Decomposition             — disjoint-support AND/OR/XOR split (k=5..8)
    ├─[7.4] BDD-guided Rebuild          — per-output ROBDD + sifting reorder
    ├─[7.5] FRAIGing                    — simulation + SAT equivalence merging
    ├─[7.6] SAT Resubstitution          — functional dependency test for k=5..7 cuts
    ├─[7.7] Don't-Care Optimization     — SDC + sim-based ODC (Mishchenko 2009)
    │        ├─ DC-aware NPN lookup     — care-mask padding into AIG_DB_4
    │        ├─ Window resubstitution   — 0-gate / 1-gate drop-in replacement
    │        └─ DC-masked exact synth   — SAT with `~care` as don't-care
    ├─[8] AIG Balancing                 — minimise critical-path depth (area-preserving)
    ├─[9] NAND Mapping + XOR extraction — XOR/XNOR patterns → 4-NAND
    └─ FSM wrap (Phase 3)               — D-FF / JK-FF, clock, async reset
```

All outputs share a **single** gate network — cross-output sub-expression reuse is automatic.

Steps 7–8 can be replaced by a user-supplied **synthesis script** (see below).
Default script is `"rewrite; fraig; dc; rewrite; balance"`.

---

## Installation

```bash
pip install -r requirements.txt
```

Dependencies: `z3-solver`, `hypothesis`, `pytest`. Python 3.9+ required.

---

## Quickstart

```bash
# Run all built-in circuits
python -m nand_optimizer

# Single circuit
python -m nand_optimizer 7seg
python -m nand_optimizer adder
python -m nand_optimizer excess3

# MCNC benchmark suite
python -m nand_optimizer rd53
python -m nand_optimizer parity9
python -m nand_optimizer mult3
python -m nand_optimizer mult4
python -m nand_optimizer misex1
python -m nand_optimizer z4ml

# Full MCNC regression (all benchmarks with verify + profiling)
python -m nand_optimizer bench

# EPFL Combinational Benchmark Suite (vendored in benchmarks/epfl/)
python -m nand_optimizer epfl                          # full suite, z3 miter verification
python -m nand_optimizer epfl --no-verify              # skip AIG-vs-AIG CEC (much faster)
python -m nand_optimizer epfl --subset arithmetic/adder,random_control/ctrl
python -m nand_optimizer epfl-check                    # audit local snapshot vs upstream GitHub

# Property-based random equivalence tests
python -m nand_optimizer proptest
python -m nand_optimizer proptest --cases 200

# Compile a .pla file
python -m nand_optimizer path/to/circuit.pla

# Load / optimise / re-export AIGER or BLIF
python -m nand_optimizer circuit.aig --script "balance; rewrite; fraig" --aiger out.aag
python -m nand_optimizer design.blif --script "rewrite -z; fraig" --blif out.blif

# Verilog Front-end (structural + simple behavioural)
python -m nand_optimizer design.v                                    # parse and optimise
python -m nand_optimizer design.v --circ output.circ                 # export to Logisim
python -m nand_optimizer ripple_adder.v --script "balance; fraig" --aiger out.aig

# FSM synthesis (Phase 3)
python -m nand_optimizer fsm:mod4               # built-in Mod-4 counter (Moore)
python -m nand_optimizer fsm:seq101             # 101-sequence detector (Mealy)
python -m nand_optimizer fsm:mod4_rst           # counter with async-low reset
python -m nand_optimizer fsm:partial            # incompletely-specified FSM
python -m nand_optimizer fsm                    # run every built-in FSM
python -m nand_optimizer path/to/fsm.kiss2      # import KISS2 file
python -m nand_optimizer fsm:mod4 --excitation jk --encoding gray --circ mod4.circ

# Hierarchical (multi-stage) synthesis via JSON composition spec
python -m nand_optimizer --compose myPLAfiles/bcd_7seg_composition.json --circ bcd7seg.circ

# Auto-detect symmetric output groups in a PLA and synthesize hierarchically
python -m nand_optimizer myPLAfiles/Binary_to_7seg_0-99.pla --auto-compose --verify

# Bandit-guided synthesis (UCB1 / Thompson Sampling)
python -m nand_optimizer mult4 --bandit 20
python -m nand_optimizer rd53  --bandit 30 --bandit-strategy thompson

# Structural JK counter example (Phase 3.5, --bits chooses width)
python -m nand_optimizer jkcounter --bits 8 --circ counter8.circ

# Bounded Model Checking for FSMs (K clock cycles, Z3-backed)
python -m nand_optimizer fsm:seq101 --bmc-bound 16

# ATPG stuck-at fault coverage (SAT-based test pattern generation)
python -m nand_optimizer 7seg --atpg

# Flags
python -m nand_optimizer 7seg --quiet                  # suppress verbose logs
python -m nand_optimizer 7seg --circ output.circ       # export to Logisim
python -m nand_optimizer 7seg --dot output.dot         # export AIG to Graphviz .dot
python -m nand_optimizer 7seg --aiger output.aig       # binary AIGER 1.9
python -m nand_optimizer 7seg --aiger output.aag       # ASCII AIGER 1.9
python -m nand_optimizer 7seg --blif  output.blif      # Berkeley BLIF (combinational)
python -m nand_optimizer 7seg --verify                 # miter-based SAT verification
python -m nand_optimizer 7seg --profile                # per-pass time + memory
python -m nand_optimizer 7seg --atpg                   # stuck-at ATPG (SAT miter)
python -m nand_optimizer 7seg --script "balance; rewrite; fraig; dc; balance; rewrite"
python -m nand_optimizer 7seg --bandit 20              # MAB-guided pass selection
```

---

## Synthesis Scripts

The `--script` flag (and the `script=` API argument) replaces the built-in
rewrite → FRAIG → DC → rewrite → balance sequence with a custom chain of AIG-level commands,
mirroring the ABC-style synthesis flow.

```bash
# ABC-style aggressive optimisation
python -m nand_optimizer mult4 --script "balance; rewrite; fraig; balance; rewrite -z"

# Depth-first: lead with balancing, finish with exact rewriting
python -m nand_optimizer 7seg --script "balance; rewrite -z -r 2; fraig; balance"

# Area-first: multiple rewrite passes with don't-care propagation
python -m nand_optimizer rd53 --script "rewrite -K 6; fraig; dc -r 3; rewrite -K 6"

# Full Mishchenko-2009 DC flow with exact-synthesis fallback
python -m nand_optimizer mult4 --script "rewrite; fraig; dc -r 3 --dc-exact --odc; balance"
```

### Supported commands

| Command | Effect |
|---|---|
| `balance`  | Restructure AND-trees to minimise critical-path depth (area-preserving) |
| `rewrite`  | Local AIG rewriting via k-feasible cut enumeration and template matching |
| `refactor` | Alias for `rewrite` |
| `fraig`    | Merge functionally equivalent nodes (simulation + SAT) |
| `dc`       | Don't-care-aware rewriting (SDC + sim-based ODC, window resub, DC-masked exact synthesis) |
| `bidec`    | Disjoint-support bi-decomposition `f = g(X) op h(Y)` for AND/OR/XOR (k=5..8 cuts) |
| `bdd`      | Per-output ROBDD rebuild + sifting reorder + ITE realisation (requires `dd`) |
| `resub`    | SAT-style functional resubstitution with up to 3 divisors for wide cuts (k=5..7) |

### Flags for `rewrite` / `refactor`

| Flag | Meaning | Default |
|---|---|---|
| `-z` | Use exact (SAT-based) synthesis per cut | off |
| `-r N` | Number of rewriting rounds | 1 |
| `-K N` | Cut size (max leaves per cut) | 4 |

### Flags for `dc`

| Flag | Meaning | Default |
|---|---|---|
| `-K N`        | Cut size (max leaves per cut) | 4 |
| `-T N`        | Z3 timeout per miter, ms | — |
| `-W N`        | Resubstitution window size | 64 |
| `-r N`        | Iterative DC rounds (early-exit on no progress) | 1 |
| `-C N`        | Care propagation rounds (fixed-point tightening) | 1 |
| `--no-sdc`    | Disable satisfiability-DC pattern discovery | off |
| `--odc`       | Enable sim-based observability-DC propagation (Mishchenko 2009) | off |
| `--dc-exact`  | Fall back to DC-masked exact synthesis for cuts > 4 | off |
| `--no-resub`  | Disable 0-gate / 1-gate window resubstitution | off |

### Flags for `bidec` / `bdd` / `resub`

| Command | Flag | Meaning | Default |
|---|---|---|---|
| `bidec` | `-K N` / `-k N` | Max / min cut size | 8 / 5 |
| `bidec` | `-r N`          | Rounds | 1 |
| `bidec` | `-z`            | SAT exact synthesis for halves with >4 inputs | off |
| `bdd`   | `-K N`          | Max support size per output | 16 |
| `resub` | `-K N` / `-k N` | Max / min cut size | 7 / 5 |
| `resub` | `-M N`          | Max divisors in dependency test | 3 |
| `resub` | `-D N`          | Divisor pool cap | 20 |
| `resub` | `-r N`          | Rounds | 1 |

### Python API

```python
result = optimize(tt, script="balance; rewrite; fraig; dc -r 3 --odc; balance; rewrite -z")
```

When `script=None` (default), the built-in `"rewrite; fraig; dc; rewrite; balance"` sequence runs unchanged.

---

## Bandit-Guided Synthesis

Instead of a hand-tuned script, let a Multi-Armed Bandit pick the next pass
adaptively. Each arm is a single-command script (`balance`, `rewrite`,
`rewrite -z`, `fraig`, `dc`); reward is the fractional AIG-node reduction
`(n_before - n_after) / n_before` observed after the step. Implemented in
[nand_optimizer/script.py](nand_optimizer/script.py) as `ScriptBandit` with
**UCB1** (default) and **Thompson Sampling** strategies.

```bash
# CLI: 20-step horizon, UCB1
python -m nand_optimizer mult4 --bandit 20

# Thompson Sampling on rd53
python -m nand_optimizer rd53 --bandit 30 --bandit-strategy thompson
```

```python
from nand_optimizer import optimize, run_bandit, ScriptBandit, DEFAULT_ARMS

# Directly from optimize()
result = optimize(tt, bandit_horizon=20, bandit_strategy='ucb1')

# Or orchestrate a standalone bandit session
trace = run_bandit(aig, out_lits, horizon=30, strategy='thompson')
```

`--bandit HORIZON` overrides `--script`. Custom arm lists are accepted via the
Python API (`ScriptBandit(arms=[...])`); the default set is `DEFAULT_ARMS`.

---

## Hierarchical / Multi-Stage Synthesis

For circuits that factor naturally into sequential stages (binary → BCD →
7-seg, address decoders, FSM look-ahead tables), the compiler accepts a
**composition spec** that wires together multiple independently-synthesized
stages into one shared AIG:

```bash
python -m nand_optimizer --compose myPLAfiles/bcd_7seg_composition.json \
                        --circ bcd_7seg.circ --verify
```

Each stage is a `.pla` plus a `connect` map binding its inputs to outputs of
earlier stages. `hierarchical_optimize()` in
[nand_optimizer/pipeline.py](nand_optimizer/pipeline.py) optimizes each stage
once, composes them via `AIG.compose()` (substituting shared signals), GCs
the combined AIG, then runs `rewrite; fraig; balance; rewrite -z; fraig;
balance` over the whole network. Cross-stage sub-expressions are automatically
merged by structural hashing.

**Auto-composition** — `--auto-compose` inspects a `.pla`, detects symmetric
output groups that implement the *same* function through different
intermediate signals, synthesizes an intermediate bus, and runs hierarchical
synthesis automatically. Module: [nand_optimizer/auto_compose.py](nand_optimizer/auto_compose.py).

```bash
python -m nand_optimizer myPLAfiles/Binary_to_7seg_0-99.pla \
                        --auto-compose --verify --circ bin7seg.circ
```

---

## Bounded Model Checking (FSM)

For synthesized FSMs, `--bmc-bound K` unrolls the sequential network for K
clock cycles symbolically via Z3 and miters it against the reference
`StateTable`. UNSAT proves no divergence on any input sequence of length ≤ K;
a SAT witness pinpoints the first divergence cycle with an input trace.
Implemented in [nand_optimizer/verify.py](nand_optimizer/verify.py) as
`bmc_verify()`.

```bash
python -m nand_optimizer fsm:seq101 --bmc-bound 16
python -m nand_optimizer path/to/fsm.kiss2 --bmc-bound 12 --encoding gray
```

```python
from nand_optimizer import synthesize_fsm, bmc_verify
res = synthesize_fsm(stt, encoding='binary')
v   = bmc_verify(res, bound=20)
# v['equivalent'] → True / False / None; v['counterexample'] → step + inputs + states
```

---

## Built-in Circuits

| Key | Inputs | Outputs | Description |
|---|---|---|---|
| `7seg` | 4 | 7 (a–g) | BCD → 7-segment decoder (don't-cares for 10–15) |
| `adder` | 4 | 3 | 2-bit adder: A + B = (Cout, S1, S0) |
| `excess3` | 4 | 4 | BCD → Excess-3 code converter |
| `rd53` | 5 | 4 | 5-bit Hamming weight (popcount) |
| `parity9` | 9 | 1 | 9-bit odd parity (XOR-tree stress test) |
| `mult3` | 6 | 6 | 3×3 unsigned multiplier |
| `mult4` | 8 | 8 | 4×4 unsigned multiplier |
| `misex1` | 8 | 7 | Dense FSM-output benchmark |
| `z4ml` | 7 | 4 | Dense combinational benchmark |

### Built-in FSM examples (Phase 3)

| Key | Model | Description |
|---|---|---|
| `fsm:seq101`    | Mealy | 3-state `101`-sequence detector (overlapping) |
| `fsm:mod4`      | Moore | Mod-4 up-counter (no data inputs) |
| `fsm:mod4_rst`  | Moore | Mod-4 counter with async-low `RESET_N` |
| `fsm:redundant` | Mealy | 5-state machine (Hopcroft collapses to 3) |
| `fsm:partial`   | Mealy | Incompletely-specified FSM (DC-aware minimisation) |

---

## Finite-State Machine Synthesis (Phase 3)

The synthesizer ingests a `StateTable` (Mealy or Moore, with ternary input/output cubes
and don't-cares) or a standard KISS2 file, and emits a combinational excitation
cone plus flip-flop primitives in one `.circ` file.

```python
from nand_optimizer import StateTable, Transition, synthesize_fsm, simulate_fsm, export_fsm_circ

stt = StateTable(
    states       = ['S0', 'S1', 'S2'],
    input_names  = ['x'],
    output_names = ['y'],
    transitions  = [
        Transition('S0', (0,), 'S0', (0,)),
        Transition('S0', (1,), 'S1', (0,)),
        Transition('S1', (0,), 'S2', (0,)),
        Transition('S1', (1,), 'S1', (0,)),
        Transition('S2', (0,), 'S0', (0,)),
        Transition('S2', (1,), 'S1', (1,)),
    ],
    model       = 'mealy',
    reset_state = 'S0',
)

res = synthesize_fsm(stt, encoding='binary', excitation='d', minimize=True)
export_fsm_circ(res, 'detector.circ', circuit_name='seq101')

# Cycle-accurate simulation (optional reset_seq for async clear)
trace = simulate_fsm(res, input_seq=[(1,), (0,), (1,)])
```

### Feature matrix

| Feature | Options |
|---|---|
| FSM model         | Mealy / Moore |
| State encoding    | `binary`, `onehot`, `gray` (reset → all-zero in binary/gray) |
| Flip-flop         | `d` (D-FF) or `jk` (JK-FF via T-fill; structural hashing fuses `J_i = K_i`) |
| Reset             | synchronous / async-low / async-high (separate control tract, no combinational loop) |
| State minimisation | Hopcroft partition refinement (completely-specified) + implication table + Bron-Kerbosch + greedy cover (incompletely-specified) |
| Input formats     | Python `StateTable`, KISS2 (`.kiss` / `.kiss2`) |
| Output           | Two-level `.circ` (combinational cone + memory primitives + clock) |

### CLI flags for FSM mode

| Flag | Meaning |
|---|---|
| `--encoding {binary,onehot,gray}` | State encoding strategy (default `binary`) |
| `--excitation {d,jk}` | Flip-flop primitive (default `d`) |
| `--no-state-min` | Skip Hopcroft/implication-table minimisation |

---

## AIGER / BLIF Interchange

Direct interop with ABC, Yosys, Mockturtle and the EPFL benchmark suite.

- **AIGER 1.9** — both ASCII (`.aag`) and binary (`.aig`) with 7-bit LEB128 delta-coded AND gates
- **BLIF** — combinational `.model` / `.inputs` / `.outputs` / `.names` subset (arbitrary SOP on read, 2-input AND on write)
- Symbol tables (`i<k>`, `o<k>`) and comments preserved. Sequential constructs (`.latch`, `.subckt`) rejected with a clear error.

```python
from nand_optimizer import read_aiger, write_aiger, read_blif, write_blif

# Round-trip: load → optimise → re-export
aig, out_lits, inames, onames = read_aiger('design.aig')
# ... run a synthesis script over (aig, out_lits) ...
write_aiger(aig, out_lits, 'opt.aag', input_names=inames, output_names=onames, binary=False)
```

CLI loads either format directly: `python -m nand_optimizer design.aig --script "rewrite; fraig" --aiger out.aag`.

---

## API

### Define a truth table

```python
from nand_optimizer import TruthTable, optimize

tt = TruthTable.from_dict(
    n_inputs     = 2,
    input_names  = ['a', 'b'],
    output_names = ['and', 'or', 'xor'],
    rows = {
        0: (0, 0, 0),
        1: (0, 1, 1),
        2: (0, 1, 1),
        3: (1, 1, 0),
    },
)

# Or from a Python function
tt = TruthTable.from_function(
    n_inputs     = 4,
    input_names  = ['a1', 'a0', 'b1', 'b0'],
    output_names = ['cout', 's1', 's0'],
    func         = lambda bits: ...,
)

# Or from a Berkeley PLA file
tt = TruthTable.from_pla('path/to/circuit.pla')
```

### Optimise

```python
result = optimize(tt, verbose=True)

# With a custom synthesis script
result = optimize(tt, script="balance; rewrite; fraig; balance; rewrite -z")

for name, r in result.items():
    print(f'{name}: {r.n_nand} NAND gates, wire={r.out_wire}')
    print(f'  SOP:   {r.expr_sop}')
    print(f'  Final: {r.expr_clean}')

print(f'Total shared gates: {result.total_nand}')
```

### Simulate

```python
from nand_optimizer import eval_network

inputs = {'a': 1, 'b': 0}
val = eval_network(result['xor'].gates, inputs)
print(val)  # → 1
```

### Formal verification

```python
from nand_optimizer.verify import miter_verify, bmc_verify

# Combinational: SAT miter (Z3) with exhaustive fallback for n ≤ 20
v = miter_verify(tt, result)
# v['equivalent'] → True / False / None
# v['method']     → 'z3' | 'exhaustive'
# v['counterexample'] → None or {input: value, ...}

# Sequential (FSM): K-cycle Bounded Model Checking
v = bmc_verify(fsm_result, bound=16)
# v['equivalent'] → True (UNSAT proves no divergence ≤ K)
# v['counterexample'] → None or {'step': t, 'inputs': [...], 'states': [...]}
```

### Export to Logisim Evolution

```python
from nand_optimizer import export_circ

export_circ(result, 'my_circuit.circ', circuit_name='decoder')
```

### Visualise the AIG (Graphviz)

Dump the final (post-rewrite) And-Inverter Graph to a `.dot` file for visual
bottleneck analysis. Complemented edges are drawn red/dashed with an
open-circle arrowhead; primary inputs sit at the top, outputs at the bottom.

```python
from nand_optimizer import optimize, aig_to_dot

result = optimize(tt, verbose=False)
dot = aig_to_dot(result.aig, result.out_lits,
                 output_names=tt.output_names,
                 title='my circuit')
with open('circuit.dot', 'w') as f:
    f.write(dot)
```

Then render with Graphviz:

```bash
dot -Tpng circuit.dot -o circuit.png
dot -Tsvg circuit.dot -o circuit.svg
```

---

## Tests

The test suite runs automatically after every optimisation:

| Test | What it checks |
|---|---|
| T1 · QMC minimisation | Espresso output matches truth table on all defined minterms |
| T2 · Phase assignment | Complemented/direct form still correct after polarity choice |
| T3 · Factorization | Algebraic rewriting is truth-preserving |
| T4 · Shannon decomp. | Cofactor expansion is truth-preserving |
| T5 · Inversion elim. | `~~x → x`, `~~~x → ~x`, etc. |
| T6 · Implicant coverage | Every required minterm is covered by at least one PI |
| T7 · NAND simulation | Gate network produces correct output for every input combination |
| T8 · Don't-care robustness | Network evaluates without crashing on don't-care inputs |
| T9 · Full cross-check | All outputs correct simultaneously for every row |
| T10 · Greedy reassociation | Greedy ordering saves gates vs. naïve left-fold; result is still correct |

Run the full property-based suite (requires `hypothesis`):

```bash
python -m nand_optimizer proptest --cases 100
```

### EPFL Combinational Benchmark Suite

A pinned snapshot of the industry-standard [EPFL Combinational Benchmark
Suite](https://github.com/lsils/benchmarks) (arithmetic + random_control, 20
`.aig` files, ~1.5 MB total) is vendored under
[benchmarks/epfl/](benchmarks/epfl/). Per-file SHA-256 hashes, source URLs,
and the pinned upstream commit live in
[benchmarks/epfl/manifest.json](benchmarks/epfl/manifest.json); the table of
benchmarks with hyperlinks to each upstream source is in
[benchmarks/epfl/README.md](benchmarks/epfl/README.md).

```bash
python -m nand_optimizer epfl                    # run the suite with z3 CEC
python -m nand_optimizer epfl --no-verify        # synthesis only, no miter
python -m nand_optimizer epfl --subset arithmetic/adder,random_control/voter
python -m nand_optimizer epfl-check              # diff local snapshot vs GitHub
```

`epfl-check` is a separate on-demand command: it re-fetches every pinned file
from `raw.githubusercontent.com`, re-hashes it against the manifest, and queries
the GitHub API for the current HEAD of `lsils/benchmarks`. It flags both local
drift (hash mismatch) and upstream drift (HEAD moved beyond the pinned commit)
without touching the working tree.

---

## Project Structure

```
nand_optimizer/
├── truth_table.py       # TruthTable — input: dict / function / .pla
├── expr.py              # Boolean expression AST (Const, Lit, Not, And, Or)
├── implicant.py         # Quine-McCluskey + cover selection (Espresso)
├── optimize.py          # Phase assign, factorize, Shannon, elim_inv
├── decomposition.py     # Ashenhurst-Curtis / Roth-Karp functional decomposition
├── aig.py               # And-Inverter Graph (AIG) — structural hashing, GC
├── rewrite.py           # Fanout-aware AIG rewriting (MFFC cut engine)
├── exact_synthesis.py   # SAT-based exact synthesis (Z3, up to 5–6-input cuts)
├── fraig.py             # FRAIGing — simulation + SAT equivalence merging
├── dont_care.py         # Don't-care-aware rewrite (SDC + sim-based ODC, V2)
├── bidec.py             # Disjoint-support bi-decomposition (AND/OR/XOR, k=5..8)
├── bdd_decomp.py        # ROBDD rebuild via sifting + ITE realisation (needs `dd`)
├── sat_resub.py         # Functional resubstitution for wide cuts (dc2-style)
├── auto_compose.py      # Symmetric-output detection + hierarchical spec generator
├── balance.py           # AIG depth balancing (area-preserving)
├── script.py            # Synthesis script parser + executor + ScriptBandit (UCB1 / Thompson)
├── aig_db_4.py          # Precomputed 4-input NPN template DB (auto-generated, gitignored)
├── precompute_4cut.py   # Parallel generator for aig_db_4.py
├── nand.py              # NANDBuilder — final NAND network + XOR/XNOR extraction
├── pipeline.py          # Full multi-output pipeline (optimize() + hierarchical_optimize())
├── fsm.py               # StateTable, Hopcroft + IS-FSM minimisation, state encoding,
│                        # excitation logic, D/JK flip-flop backend, KISS2 parser
├── structural.py        # StructuralModule — gate-level RTL construction (no TruthTable)
├── datapath.py          # Parametric datapath blocks (adders, comparators, mux)
├── verify.py            # Miter + BMC formal equivalence (Z3 / exhaustive / bounded unroll)
├── tests.py             # Universal test suite (T1–T10)
├── benchmark_runner.py  # MCNC regression runner
├── property_tests.py    # Hypothesis-based random equivalence tests
├── profile.py           # Per-pass time + memory profiler
├── circ_export.py       # Logisim Evolution 4.x .circ exporter (combinational + FSM)
├── dot_export.py        # Graphviz .dot AIG visualisation exporter
├── aiger_io.py          # AIGER 1.9 reader/writer (ASCII .aag + binary .aig)
├── blif_io.py           # Berkeley BLIF reader/writer (combinational subset)
├── verilog_io.py        # Verilog front-end — structural + behavioural syntax (Phase 5)
├── epfl_bench.py        # EPFL Combinational Benchmark Suite runner + audit
├── sta.py               # Static Timing Analysis — arrival times, slack, critical path
├── atpg.py              # Automatic Test Pattern Generation (stuck-at SAT)
├── switching.py         # Switching Activity Estimation — power-aware metrics
├── __init__.py          # Public API (+ bootstrap for aig_db_4.py)
├── __main__.py          # CLI entry point
└── examples/
    ├── circuits.py      # seven_segment, two_bit_adder, bcd_to_excess3
    ├── benchmarks.py    # MCNC: rd53, parity9, mult3, mult4, misex1, z4ml
    ├── fsm_examples.py  # seq101, mod4, mod4_rst, redundant, partial
    └── jk_counter.py    # 8-bit universal reversible JK counter (Phase 3.5)
```

---

## Key Design Decisions

**Shared gate network** — all outputs compile into a single `NANDBuilder` instance; sub-expressions shared across outputs are computed only once.

**AIG structural hashing** — every `(AND, a, b)` key is memoised. Identical sub-trees are deduplicated automatically regardless of which output generated them.

**Fanout-aware rewriting** — the rewriter computes the MFFC (Maximum Fanout-Free Cone) of each cut candidate; a replacement is accepted only when `new_nodes < mffc_size`, preventing gate-count inflation.

**Exact synthesis cache** — Z3 finds the provably minimal AND-tree for every distinct 4-input Boolean function and caches it in memory; the rewriter looks up this cache before attempting heuristic rewrites.

**Functional decomposition** — Roth-Karp bipartition detects "bottleneck" sub-functions inside flat truth tables and splits the problem in two, radically reducing literal counts on arithmetic circuits.

**Phase assignment** — for each output the optimiser independently chooses `f` or `~f` (whichever needs fewer SOP literals), inserting a free NOT at the output if needed.

**Synthesis scripts** — steps 7–8 (rewrite, FRAIG, DC, balance) are fully composable via a user-supplied semicolon-separated command string, enabling circuit-specific tuning of the optimisation sequence without modifying pipeline code.

**Don't-care optimisation (Mishchenko 2009)** — sound sim-based ODC propagation with three safety layers: per-cut admissibility check (template signature ≡ reference on cared bits), reconstruction-from-old fallback, and end-of-pass safety-net miter. Window resubstitution (0-gate / 1-gate) and DC-masked exact synthesis extend coverage beyond the 4-input NPN database. For `n_inputs ≤ 14` the admissibility check uses exhaustive PI enumeration (all `2^n_inputs` patterns), giving perfect per-node coverage; above the threshold random bit-parallel sampling is used. The `last_dc_stats()` API surfaces per-pass instrumentation (`n_nodes_rewritten`, `n_templates_admitted`, `n_resub_{0,1}gate`, `n_safety_net_reverts`, `final_sim_W`, `n_inputs`) for diagnostic use.

**FSM-to-AIG via excitation logic** — a `StateTable` is projected to a `TruthTable` with inputs `Q_i ++ fsm_inputs` and outputs `D_i ++ fsm_outputs` (or `(J_i, K_i)` pairs for JK mode); unused-encoding patterns and DASH cubes become per-output don't-cares, giving Espresso / factorization maximum freedom. Feedback loops are broken by construction: the combinational cone is always acyclic, and `D → FF → Q` is closed only in the exporter.

**JK structural fusion** — in JK mode `J_i ≡ K_i ≡ T_i = Q_i XOR Q_i(t+1)`, so AIG structural hashing collapses both excitation functions into one shared cone — no duplicate gates.

**AIGER / BLIF interop** — direct round-trip with ABC, Yosys and the EPFL benchmark suite. Binary AIGER uses LEB128 delta-coded AND gates; BLIF reader accepts arbitrary SOP cubes (reduced to AIG via `make_or` + `make_and`), writer emits canonical 2-input `.names`.

---

## Structural / Datapath API

For circuits with > 20 inputs where truth-table enumeration is infeasible, build the AIG
directly from RTL primitives and let the standard synthesis passes finish the job.

```python
from nand_optimizer.structural import StructuralModule
from nand_optimizer.datapath import adder, mux, comparator

# 8-bit ripple-carry adder
m = StructuralModule(n_inputs=16, input_names=[f'a{i}' for i in range(8)] + [f'b{i}' for i in range(8)])
sums, cout = adder(m, m.inputs[:8], m.inputs[8:])
result = m.compile(output_lits=sums + [cout])

# Arbitrary gate-level construction
from nand_optimizer.structural import StructuralModule
m = StructuralModule(n_inputs=4)
a, b, c, d = m.inputs
ab  = m.make_and(a, b)
cd  = m.make_and(c, d)
out = m.make_or(ab, cd)
result = m.compile(output_lits=[out])
```

Run the vendored 8-bit JK-counter example:
```bash
python -m nand_optimizer.examples.jk_counter
```

---

## Requirements

- Python 3.9+
- `z3-solver >= 4.12` — formal verification and exact synthesis
- `hypothesis >= 6.90` — property-based testing (`proptest`)
- `pytest >= 7.4` — test runner
- Logisim Evolution 4.x for `.circ` import