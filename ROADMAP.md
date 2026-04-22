## Roadmap & Claude Agent Workflows

Pending tasks from [TODO.md](TODO.md), mapped to the Claude model that best fits each
implementation effort. Run them via `claude` CLI or the Agent SDK.

### Opus 4.7 — deep algorithmic reasoning / research-level

Tasks that require multi-step theoretical analysis, novel graph representations, or
integrating ML research papers into a running compiler:

| Task | Phase | Why Opus |
|---|---|---|
| Bi-decomposition (OR/AND/XOR) in `rewrite.py` — SAT-based variable partitioning | 2 | Needs cofactor-matrix analysis + correctness proof under DC |
| BDD-based decomposition via narrow-width profiles | 2 | ROBDD width profiling + sifting interaction with AIG snapshot/restore |
| SAT-based resubstitution for k=5–7 cuts (`dc2`-style) | 2 | Cost model + divisor selection heuristics across ODC boundary |
| XAG (XOR-AND Graph) — first-class XOR nodes + new NPN DB | 2.7 | Major AIG-API redesign; rewrite/FRAIG/balance all need adaptation |
| MIG (Majority-Inverter Graph) backend | 2.7 | MAJ(a,b,c) axiomatics + mapping from AIG lits |
| Fixed-point care propagation (`care_rounds_internal`) | 2.7 | Reconvergent fanout analysis; must stay sound under multi-round DC |
| DeepGate-style GNN embeddings for AIG nodes | 8 | GNN architecture design + training corpus from EPFL runs |
| DNAS differentiable search for NAND templates | 8 | PyTorch relaxation + straight-through estimator + Z3 binarisation |
| ML-accelerated SAT branching hints (NeuroSAT-style) | 8 | Z3 phase-selection API + NeuroSAT-like inference in the hot path |
| DRiLLS-style RL agent for synthesis script control | 8 | A2C/PPO on AIG feature vectors; pre-training loop on MCNC/EPFL |

```bash
claude --model claude-opus-4-7 "implement bi-decomposition pass in rewrite.py …"
```

---

### Sonnet 4.6 — substantial but well-specified implementation

Tasks with a clear algorithmic spec where the main challenge is correct integration with
the existing pipeline:

| Task | Phase | Notes |
|---|---|---|
| DC V2 instrumentation: topology-aware resub window | 2.7 | Add `level_new` proximity ranking; benchmark on EPFL |
| DC V2: adaptive sim patterns for n > 14 inputs | 2.7 | `n_sim_patterns` scaling + `n_safety_net_reverts` counter |
| FSM–datapath integration (`backend='structural'`) | 3.5 | Wire `next_state_fn` callback into `synthesize_fsm()` |
| ATPG via stuck-at SAT miter | 7 | One SAT instance per fault; fault-coverage metric |
| Static Timing Analysis (arrival time + slack graph) | 4 | Topological traversal of AIG with gate-delay model |
| Delay/Area trade-off heuristics in `balance.py` | 4 | Accept depth regression when it saves ≥ N gates |
| Standard Cell mapping (tree-covering DP) | 5 | Library file parser + dynamic-programming cut cover |
| LUT mapping (K-feasible cut → FPGA macro) | 5 | Integrate with existing cut enumeration in `rewrite.py` |
| GPU simulation in `fraig.py` (CuPy tensors) | 4.5 | `[N_nodes × B_vectors]` AND/XOR batched; CPU fallback |
| Parallel NPN-lookup batching (`rewrite.py`) | 4.5 | Scatter/gather over 65536-entry `AIG_DB_4` on GPU |
| Verilog front-end (structural + simple behavioural) | 5 | `pyverilog` → AIG; wire with existing pipeline |
| Incremental synthesis (dirty-subgraph tracking) | 4 | Mark modified cones; skip clean passes |
| Parallelisation of rewrite/FRAIG across output cones | 4 | `multiprocessing` + shared structural-hash manager |
| SAT sweeping + window-based optimisation | 2.7 | Bounded-depth cone extraction + local DC + SAT minimise |
| Multi-Armed Bandit script controller (UCB1) | 8 | `ScriptBandit` class in `script.py`; no neural net needed |
| QoR predictor MLP (area/depth from AIG stats + script) | 8 | Supervised on own benchmark logs; prune script search space |
| Package restructuring into subpackages | 6 | Mechanical but must keep bootstrap + all imports intact |
| Switching activity estimation (probability propagation) | 7 | `p_sw` rule for AND/NOT; base for power-aware rewrites |
| SCOAP testability metrics | 7 | Controllability / observability formulae over AIG |

```bash
claude --model claude-sonnet-4-6 "add ATPG stuck-at SAT miter in verify.py …"
```

---

### Haiku 4.5 — mechanical / additive changes

Small, self-contained additions where the logic is obvious and the risk of breakage is low:

| Task | Phase | Notes |
|---|---|---|
| Add `n_resub_1gate_examined` / `_dropped_by_window` counters to `last_dc_stats()` | 2.7 | Two integer counters + log line |
| `--log-aig` CLI flag to dump per-run AIG stats for ML corpus | 8 | Append JSON row to `~/.nand_optimizer_runs.jsonl` |
| `--bdd-decomp` flag wiring (just the CLI plumbing, not the algorithm) | 2 | One argparse entry + `pipeline.py` branch |
| Expose `care_rounds_internal` as `dc -C N` script flag | 2.7 | One flag parse + pass-through to `dc_optimize()` |
| Add `jk_counter` to `__main__.py` dispatch table | 3.5 | One `elif` case; module already exists |
| Add import aliases to `__init__.py` for `StructuralModule`, `adder`, `mux` | 3.5 | Three re-export lines |
| Update project-structure section of this README after Phase 6 rename | 6 | Text edit only |
| Pin `dd` version in `requirements.txt` | 4 | One line change |

```bash
claude --model claude-haiku-4-5-20251001 "add --log-aig flag to __main__.py …"
```
