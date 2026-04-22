# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Universal-Gate-Compiler is a combinational logic synthesizer that compiles truth tables and Boolean functions into globally optimized NAND-only gate networks. It supports multi-output functions, don't-cares, Berkeley PLA input, Logisim Evolution `.circ` export, and formal SAT-based equivalence verification.

## Setup and Commands

```bash
pip install -r requirements.txt   # z3-solver, dd, pytest, hypothesis
```

**Run built-in circuits (also runs T1–T10 tests automatically):**
```bash
python -m nand_optimizer 7seg
python -m nand_optimizer adder
python -m nand_optimizer excess3
python -m nand_optimizer all          # all three circuits
```

**MCNC benchmark regression:**
```bash
python -m nand_optimizer bench
```

**Property-based testing:**
```bash
python -m nand_optimizer proptest
python -m nand_optimizer proptest --cases 200
```

**Individual circuit with flags:**
```bash
python -m nand_optimizer rd53 --verify --profile
python -m nand_optimizer 7seg --circ out.circ --dot aig.dot
python -m nand_optimizer 7seg --script "balance; rewrite; fraig; balance"
```

**Pytest:**
```bash
pytest                                # runs pytest-discoverable tests if any
```

**Programmatic API:**
```python
from nand_optimizer import optimize, TruthTable, run_tests
tt = TruthTable.from_dict({0: 1, 1: 0, 2: 1, 3: 1}, n_inputs=2)
result = optimize(tt, verbose=True, script="rewrite; fraig; balance")
run_tests(tt, result)                 # returns bool, prints T1–T10
```

## Pipeline Architecture

The compiler is a 10-stage pipeline. Data flows as:

```
TruthTable (cube-cover)
  → [1] Multi-output Espresso (quine_mccluskey + greedy cover)   implicant.py
  → [2] Phase assignment (choose f or ~f per output)             optimize.py
  → [3] Brayton algebraic factorization (kernel extraction)      optimize.py
  → [4] Ashenhurst-Curtis functional decomposition (optional)    decomposition.py
  → [5] Expr AST → AIG (structural hashing, complement edges)    aig.py / nand.py
  → [6] AIG rewriting (k-feasible cuts + NPN DB / exact synth)   rewrite.py
  → [7] FRAIGing (simulation signatures + SAT merging)           fraig.py
  → [8] AIG balancing (AND-tree restructuring, min critical path) balance.py
  → [9] XOR/XNOR pattern extraction                              nand.py
  → [10] NAND gate mapping (bubble pushing, dead-code GC)        nand.py
  → Formal verification (miter + Z3, or exhaustive)             verify.py
  → .circ / .dot export                                          circ_export.py / dot_export.py
```

`pipeline.py` orchestrates all stages and returns `OptimizeResult` (per-output intermediates + aggregate NAND count + AIG + timing profile).

## Key Modules

| Module | Role |
|--------|------|
| `pipeline.py` | Main `optimize()` entry point; wires all stages; `OptimizeResult` / `OutputResult` |
| `aig.py` | And-Inverter Graph: structural hashing, complement edges (lit = node*2 + inv), GC, snapshot/restore |
| `nand.py` | `NANDBuilder`; `expr_to_aig()`; `aig_to_gates()` with bubble pushing and XOR detection |
| `rewrite.py` | K-feasible cut enumeration; MFFC cost model; NPN DB lookup + exact synthesis |
| `fraig.py` | Bit-parallel simulation → equivalence classes → Z3 SAT miter to merge |
| `balance.py` | AND-tree leaves → min-heap → minimum-depth recombination |
| `exact_synthesis.py` | SAT-based Boolean matching for small functions; in-memory `(n, tt, dc) → template` cache |
| `implicant.py` | `Implicant` (ternary cube); `quine_mccluskey()`; `multi_output_espresso()` |
| `optimize.py` | `phase_assign()`; `factorize()` / `brayton_factor()`; `multi_output_factorize()` |
| `decomposition.py` | `ashenhurst_decompose()`: Roth-Karp column-multiplicity → bound/free partition |
| `expr.py` | Boolean AST (`Const`, `Lit`, `Not`, `And`, `Or`); `simp()` constant propagation |
| `truth_table.py` | `TruthTable`: cube-cover; loaders `from_dict`, `from_function`, `from_pla` |
| `verify.py` | `miter_verify()`: Z3 UNSAT proof or exhaustive sim for n ≤ 20 |
| `script.py` | Parses and executes synthesis scripts (`balance; rewrite -z; fraig`) |
| `aig_db_4.py` | Precomputed NPN templates for all 2^16 4-input Boolean functions (5.7 MB, auto-generated on first import, gitignored) |
| `precompute_4cut.py` | Builds `aig_db_4.py`; parallelises across CPU cores via `mp.Pool`. On first import `__init__.py` spawns `python -m nand_optimizer.precompute_4cut` as a subprocess to avoid the Pool-in-package-init hang; an env guard (`_NAND_OPTIMIZER_BOOTSTRAPPING=1`) makes `__init__.py` skip submodule imports for that subprocess and its workers |
| `tests.py` | T1–T10 test suite run via `run_tests()`; `TestRunner` class |
| `__main__.py` | CLI: circuit dispatch, argument parsing, output formatting |

## Important Design Details

**AIG literal encoding:** `lit = node_id * 2 + complement_bit`. Constant FALSE = 0, TRUE = 1. Complement edges make inversions free (just flip the low bit). Structural hashing normalizes `AND(a,b)` and `AND(b,a)` to the same node.

**Synthesis scripts:** User-supplied ABC-style command strings passed via `--script` or the `script=` parameter replace the hardcoded pass sequence. Default is `"rewrite; fraig; balance"`. Supported: `balance`, `rewrite [-z] [-r N] [-K N]`, `fraig`.

**MFFC cost model in rewriting:** A cut replacement is accepted only if `n_new_gates < mffc_size`. This prevents gate inflation from falsely counting nodes that survive via structural hashing into new locations.

**Functional decomposition bake-off:** `decomposition.py` uses snapshot/restore on the AIG to speculatively try a decomposition and compare gate counts before committing.

**Multi-output sharing:** All outputs share a single `NANDBuilder` / `AIG`, so sub-expression reuse across outputs is automatic via structural hashing.

**T1–T10 tests** run automatically after every `optimize()` call within the built-in circuit runners. They cover: QMC correctness, phase assignment, factorization, Shannon decomp, inversion elimination, implicant coverage, NAND simulation, don't-care robustness, full cross-check, and greedy reassociation.

## Context Hygiene (for agents)

**Heavy/generated files — do not read without explicit request:**
- [nand_optimizer/aig_db_4.py](nand_optimizer/aig_db_4.py) — 5.5 MB of precomputed NPN templates, auto-generated by [precompute_4cut.py](nand_optimizer/precompute_4cut.py) on first `import nand_optimizer` (bootstrap lives at the top of [nand_optimizer/__init__.py](nand_optimizer/__init__.py)). Gitignored. Treat as a data blob; never open for exploration. If you need to understand how entries are consumed, read [nand_optimizer/rewrite.py](nand_optimizer/rewrite.py) around line 358 instead.
- [TODO_done.md](TODO_done.md) — archive of completed roadmap items with full implementation notes. Read only when the user asks about the history/rationale of a specific completed feature.

**Active roadmap:** [TODO.md](TODO.md) holds only pending `[ ]` tasks plus one-line per-phase summaries. This is the file to consult for "what's next".

**Broad code searches:** for multi-query exploration (e.g. "how does FRAIG interact with rewrite?", "where is MFFC computed?"), delegate to the `Explore` subagent with `thoroughness="quick"` or `"medium"`. This keeps grep/read noise out of the main context. Use direct `Grep`/`Read` only when the target file or symbol is already known.
