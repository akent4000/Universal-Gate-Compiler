# Pass QoR Evaluation (ROADMAP P2#7)

Generated: 2026-04-25 02:48:43  
Baseline script: `rewrite; fraig; rewrite; balance`  
Benchmark corpus: EPFL subset (3 circuits)  
Metric: `ands_after` (AIG AND+XOR node count, post-script).

Values in **bold** are wins ≥ 2%; `ERR` = pass raised an exception.

## `XAG (-x)`  vs  `baseline`

| benchmark | n_inputs | baseline_area | with_pass_area |  Δarea |  Δtime |
|-----------|---------:|--------------:|---------------:|-------:|-------:|
| random_control/ctrl | 7 | 166 | 166 | +0.0% | +38.7% |
| random_control/router | 60 | 638 | 549 | **-13.9%** | -2.4% |
| arithmetic/adder | 256 | 1020 | 1020 | +0.0% | +2.3% |

## Summary — mean Δarea across subset

| variant | wins | ties | regressions | mean Δarea | mean Δtime |
|---------|-----:|-----:|------------:|-----------:|-----------:|
| `XAG (-x)` | 1 | 2 | 0 | -4.6% | +12.9% |

## Raw `ands_after` matrix

| benchmark | baseline | XAG (-x) |
|-----------| ---: | ---: |
| random_control/ctrl | 166 | 166 |
| random_control/router | 638 | 549 |
| arithmetic/adder | 1020 | 1020 |

## Raw wall-time matrix (seconds)

| benchmark | baseline | XAG (-x) |
|-----------| ---: | ---: |
| random_control/ctrl | 0.14 | 0.20 |
| random_control/router | 0.36 | 0.35 |
| arithmetic/adder | 1.59 | 1.63 |
