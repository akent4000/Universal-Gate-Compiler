# Universal Gate Compiler

> **Universal combinational logic compiler — from truth table to minimal NAND network**

Takes any combinational logic function (truth table or `.pla` file) and produces a globally optimised NAND-only gate network, with formal verification and Logisim Evolution export.

---

## Pipeline

```
TruthTable / .pla file
    │
    ├─[1] Multi-output Espresso         — minimise to Sum-of-Products (shared cubes)
    ├─[2] Phase Assignment              — choose f or ~f (fewer literals wins)
    ├─[3] Algebraic Factorization       — Brayton kernel/co-kernel extraction
    ├─[4] Cross-output Factorization    — expose shared literals across outputs
    ├─[5] Functional Decomposition      — Ashenhurst-Curtis / Roth-Karp bipartition
    ├─[6] AIG Construction              — structural hashing (And-Inverter Graph)
    │        └─ Greedy Reassociation    — reorder AND-chains for cache reuse
    ├─[7] AIG Rewriting                 — fanout-aware MFFC cut rewriting
    │        └─ Exact Synthesis (SAT)   — Z3-based optimal 4-cut templates
    ├─[8] FRAIGing                      — simulation + SAT equivalence merging
    ├─[9] AIG Balancing                 — minimise critical-path depth (area-preserving)
    └─[10] NAND Mapping + XOR extraction — XOR/XNOR patterns → 4-NAND
```

All outputs share a **single** gate network — cross-output sub-expression reuse is automatic.

Steps 7–9 can be replaced by a user-supplied **synthesis script** (see below).

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

# Property-based random equivalence tests
python -m nand_optimizer proptest
python -m nand_optimizer proptest --cases 200

# Compile a .pla file
python -m nand_optimizer path/to/circuit.pla

# Flags
python -m nand_optimizer 7seg --quiet                  # suppress verbose logs
python -m nand_optimizer 7seg --circ output.circ       # export to Logisim
python -m nand_optimizer 7seg --dot output.dot         # export AIG to Graphviz .dot
python -m nand_optimizer 7seg --verify                 # miter-based SAT verification
python -m nand_optimizer 7seg --profile                # per-pass time + memory
python -m nand_optimizer 7seg --script "balance; rewrite; fraig; balance; rewrite"
```

---

## Synthesis Scripts

The `--script` flag (and the `script=` API argument) replaces the built-in
rewrite → FRAIG → balance sequence with a custom chain of AIG-level commands,
mirroring the ABC-style synthesis flow.

```bash
# ABC-style aggressive optimisation
python -m nand_optimizer mult4 --script "balance; rewrite; fraig; balance; rewrite -z"

# Depth-first: lead with balancing, finish with exact rewriting
python -m nand_optimizer 7seg --script "balance; rewrite -z -r 2; fraig; balance"

# Area-first: multiple rewrite passes, no balancing
python -m nand_optimizer rd53 --script "rewrite -K 6; fraig; rewrite -K 6; fraig"
```

### Supported commands

| Command | Effect |
|---|---|
| `balance` | Restructure AND-trees to minimise critical-path depth (area-preserving) |
| `rewrite` | Local AIG rewriting via k-feasible cut enumeration and template matching |
| `refactor` | Alias for `rewrite` |
| `fraig` | Merge functionally equivalent nodes (simulation + SAT) |

### Flags for `rewrite` / `refactor`

| Flag | Meaning | Default |
|---|---|---|
| `-z` | Use exact (SAT-based) synthesis per cut | off |
| `-r N` | Number of rewriting rounds | 1 |
| `-K N` | Cut size (max leaves per cut) | 4 |

### Python API

```python
result = optimize(tt, script="balance; rewrite; fraig; balance; rewrite -z")
```

When `script=None` (default), the built-in sequence runs unchanged.

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
from nand_optimizer.verify import miter_verify

v = miter_verify(tt, result)
# v['equivalent'] → True / False / None
# v['method']     → 'z3' | 'exhaustive'
# v['counterexample'] → None or {input: value, ...}
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

---

## Project Structure

```
nand_optimizer/
├── truth_table.py       # TruthTable — input: dict / function / .pla
├── expr.py              # Boolean expression AST (Const, Lit, Not, And, Or)
├── implicant.py         # Quine-McCluskey + cover selection (Espresso)
├── optimize.py          # Phase assign, factorize, Shannon, elim_inv
├── decomposition.py     # Ashenhurst-Curtis / Roth-Karp functional decomposition
├── aig.py               # And-Inverter Graph (AIG) — structural hashing
├── rewrite.py           # Fanout-aware AIG rewriting (MFFC cut engine)
├── exact_synthesis.py   # SAT-based exact synthesis (Z3, 4-input cuts)
├── fraig.py             # FRAIGing — simulation + SAT equivalence merging
├── balance.py           # AIG depth balancing (area-preserving)
├── script.py            # Synthesis script parser and executor
├── nand.py              # NANDBuilder — final NAND network
├── pipeline.py          # Full multi-output pipeline (optimize())
├── verify.py            # Miter-based formal equivalence check (Z3 / exhaustive)
├── tests.py             # Universal test suite (T1–T10)
├── benchmark_runner.py  # MCNC regression runner
├── property_tests.py    # Hypothesis-based random equivalence tests
├── profile.py           # Per-pass time + memory profiler
├── circ_export.py       # Logisim Evolution 4.x .circ exporter
├── dot_export.py        # Graphviz .dot AIG visualisation exporter
├── __init__.py          # Public API
├── __main__.py          # CLI entry point
└── examples/
    ├── circuits.py      # seven_segment, two_bit_adder, bcd_to_excess3
    └── benchmarks.py    # MCNC: rd53, parity9, mult3, mult4, misex1, z4ml
```

---

## Key Design Decisions

**Shared gate network** — all outputs compile into a single `NANDBuilder` instance; sub-expressions shared across outputs are computed only once.

**AIG structural hashing** — every `(AND, a, b)` key is memoised. Identical sub-trees are deduplicated automatically regardless of which output generated them.

**Fanout-aware rewriting** — the rewriter computes the MFFC (Maximum Fanout-Free Cone) of each cut candidate; a replacement is accepted only when `new_nodes < mffc_size`, preventing gate-count inflation.

**Exact synthesis cache** — Z3 finds the provably minimal AND-tree for every distinct 4-input Boolean function and caches it in memory; the rewriter looks up this cache before attempting heuristic rewrites.

**Functional decomposition** — Roth-Karp bipartition detects "bottleneck" sub-functions inside flat truth tables and splits the problem in two, radically reducing literal counts on arithmetic circuits.

**Phase assignment** — for each output the optimiser independently chooses `f` or `~f` (whichever needs fewer SOP literals), inserting a free NOT at the output if needed.

**Synthesis scripts** — steps 7–9 (rewrite, FRAIG, balance) are fully composable via a user-supplied semicolon-separated command string, enabling circuit-specific tuning of the optimisation sequence without modifying pipeline code.

---

## Requirements

- Python 3.9+
- `z3-solver >= 4.12` — formal verification and exact synthesis
- `hypothesis >= 6.90` — property-based testing (`proptest`)
- `pytest >= 7.4` — test runner
- Logisim Evolution 4.x for `.circ` import
