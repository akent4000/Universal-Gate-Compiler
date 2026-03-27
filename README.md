# 🔲 NAND Optimizer

> **Universal combinational logic compiler — from truth table to minimal NAND network**

Takes any combinational logic function (as a truth table) and produces a globally optimised NAND-only gate network, complete with formal verification and Logisim Evolution export.

---

## Pipeline

```
TruthTable
    │
    ├─[1] Quine-McCluskey / Espresso   — minimise to Sum-of-Products
    ├─[2] Phase Assignment             — choose f or ~f (fewer literals wins)
    ├─[3] Algebraic Factorization      — extract common sub-expressions
    ├─[4] Shannon Decomposition        — cofactor simplification
    ├─[5] Redundant Inversion Elim.    — remove double negations
    ├─[6] NAND Conversion              — global AIG structural hashing
    │        └─ Greedy Reassociation   — reorder AND-chains for cache reuse
    └─[7] Dead Code Elimination        — remove unreachable gates
```

All outputs share a **single** gate network — cross-output sub-expression reuse is automatic.

---

## Quickstart

```bash
# Run default example (BCD → 7-segment decoder)
python run.py

# Other built-in circuits
python run.py adder       # 2-bit adder
python run.py excess3     # BCD → Excess-3
python run.py all         # run all examples

# Suppress verbose output
python run.py --quiet

# Export to Logisim Evolution
python run.py 7seg --circ output.circ

# Module invocation (equivalent)
python -m nand_optimizer [args]
```

---

## API

### Define a truth table

```python
from nand_optimizer import TruthTable, optimize

# From a dictionary: { minterm_index: (out0, out1, ...) }
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
    func         = lambda bits: ...,   # bits is a tuple of 0/1
)
```

### Optimise

```python
result = optimize(tt, verbose=True)

# Inspect per-output results
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

### Export to Logisim Evolution

```python
from nand_optimizer import export_circ

export_circ(result, 'my_circuit.circ', circuit_name='decoder')
```

---

## Built-in Examples

| Circuit | Inputs | Outputs | Description |
|---|---|---|---|
| `7seg` | 4 | 7 (a–g) | BCD → 7-segment display decoder with don't-cares for digits 10–15 |
| `adder` | 4 | 3 | 2-bit adder: A + B = (Cout, S1, S0) |
| `excess3` | 4 | 4 | BCD → Excess-3 code converter |

---

## Tests

The test suite (`tests.py`) runs automatically after every optimisation:

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

---

## Project Structure

```
nand_optimizer/
├── truth_table.py    # TruthTable — the single input to the pipeline
├── expr.py           # Boolean expression AST (Const, Lit, Not, And, Or)
├── implicant.py      # Quine-McCluskey + cover selection (Espresso)
├── optimize.py       # Phase assign, factorize, Shannon, elim_inv
├── nand.py           # NANDBuilder — structural hashing + greedy reassociation
├── pipeline.py       # Full multi-output pipeline (optimize())
├── tests.py          # Universal test suite
├── circ_export.py    # Logisim Evolution 4.x .circ exporter
├── __init__.py       # Public API
├── __main__.py       # CLI entry point
└── examples/
    └── circuits.py   # seven_segment, two_bit_adder, bcd_to_excess3
```

---

## Key Design Decisions

**Shared gate network across outputs** — all outputs are compiled into a single `NANDBuilder` instance, so sub-expressions that appear in multiple outputs are computed only once.

**Structural hashing (AIG cache)** — every `(NAND, (a, b))` key is memoised. Identical sub-expressions are automatically deduplicated regardless of which output generated them.

**Greedy reassociation** — when folding a multi-input AND-chain into 2-input NANDs, the builder checks its cache and picks the pairing order that maximises reuse. A naïve left-fold is used as a baseline in T10 to prove savings.

**Phase assignment** — for each output the optimiser independently decides whether to implement `f` or `~f` (whichever requires fewer SOP literals), inserting a free NOT at the output if needed.

---

## Requirements

- Python 3.9+
- No external dependencies (stdlib only)
- Logisim Evolution 4.x for `.circ` import
