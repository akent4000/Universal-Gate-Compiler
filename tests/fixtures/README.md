# Test fixtures

Minimal AIG fixtures extracted from larger circuits, preserved for
regression testing of specific bug classes.

## `router_outport1_minimal.aig`

**Size:** 14 ANDs, 15 inputs, 1 output (29 total node ids).

**Origin:** `benchmarks/epfl/random_control/router.aig` (EPFL combinational
suite), output `outport[1]` (one of 30 POs; the other 29 do not trip the
bug). Delta-debugged by iteratively replacing internal AND nodes with
constants (`0` / `1`) and keeping the substitution whenever
``dc_optimize(use_odc=True)`` still tripped its end-of-pass safety-net
miter. 252 ANDs / 60 inputs in the original cone → 14 / 15 at the local
minimum of single-node substitution.

**What it reproduces:** ROADMAP P0#1 — the V2 admissibility check in
[nand_optimizer/dont_care.py](../../nand_optimizer/dont_care.py) accepts a
sequence of individually-valid DC rewrites whose *composition* is
functionally incorrect. The bug survives raising ``n_sim_patterns`` to
16384 and is not a coverage artefact — see
[ROADMAP.md §P0#1](../../ROADMAP.md) for the theoretical analysis.

**What it does NOT reproduce:** the bug is specific to ``use_odc=True``;
the same fixture is handled cleanly without ODC.

**Regression tests:** [tests/test_dc_odc_soundness.py](../test_dc_odc_soundness.py).
Run with ``pytest tests/test_dc_odc_soundness.py`` or standalone:
``python3 tests/test_dc_odc_soundness.py``.

**When this fixture's revert disappears** (``test_fixture_still_reverts``
starts failing), the V3 fix has likely landed — confirm correctness via
``test_safety_net_preserves_soundness`` and delete or flip the revert
assertion.
