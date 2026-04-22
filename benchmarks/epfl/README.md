# EPFL Combinational Benchmark Suite (vendored snapshot)

Industry-standard benchmark collection for combinational logic
synthesis, published and maintained by the [EPFL Integrated Systems
Laboratory (LSI)](https://www.epfl.ch/labs/lsi/). The files in this
directory are a direct copy of `.aig` (binary AIGER 1.9) sources from
the upstream repository, pinned to one commit:

- **Upstream repository:** <https://github.com/lsils/benchmarks>
- **Pinned commit:** [`5d342c70d5`](https://github.com/lsils/benchmarks/tree/5d342c70d557b5371ce24c20d9d277fb191197f3)
- **Snapshot date:** 2026-03-03
- **Integrity:** per-file SHA-256 recorded in [manifest.json](manifest.json)

Running the suite:

```bash
python -m nand_optimizer epfl                   # full suite, z3 miter verification
python -m nand_optimizer epfl --no-verify       # skip equivalence check (much faster)
python -m nand_optimizer epfl --subset arithmetic/adder,random_control/ctrl
python -m nand_optimizer epfl --script "balance; rewrite; fraig; balance"
python -m nand_optimizer epfl-check             # audit this snapshot vs upstream HEAD
```

`epfl-check` is a separate on-demand command: it re-downloads each pinned file,
compares SHA-256 against [manifest.json](manifest.json), and queries the GitHub
API for the current HEAD of `lsils/benchmarks` — flagging any local drift or
available upstream updates without touching the working tree.

### Arithmetic ([arithmetic/](arithmetic/))

| File | PIs | POs | ANDs | Size | Upstream |
|------|----:|----:|-----:|-----:|----------|
| [`adder.aig`](arithmetic/adder.aig) | 256 | 129 | 1020 | 7.2 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/adder.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/adder.aig) |
| [`bar.aig`](arithmetic/bar.aig) | 135 | 128 | 3336 | 13.8 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/bar.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/bar.aig) |
| [`div.aig`](arithmetic/div.aig) | 128 | 128 | 57247 | 173.4 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/div.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/div.aig) |
| [`hyp.aig`](arithmetic/hyp.aig) | 256 | 128 | 214335 | 578.3 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/hyp.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/hyp.aig) |
| [`log2.aig`](arithmetic/log2.aig) | 32 | 32 | 32060 | 94.3 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/log2.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/log2.aig) |
| [`max.aig`](arithmetic/max.aig) | 512 | 130 | 2865 | 17.5 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/max.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/max.aig) |
| [`multiplier.aig`](arithmetic/multiplier.aig) | 128 | 128 | 27062 | 79.0 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/multiplier.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/multiplier.aig) |
| [`sin.aig`](arithmetic/sin.aig) | 24 | 25 | 5416 | 14.9 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/sin.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/sin.aig) |
| [`sqrt.aig`](arithmetic/sqrt.aig) | 128 | 64 | 24618 | 67.9 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/sqrt.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/sqrt.aig) |
| [`square.aig`](arithmetic/square.aig) | 64 | 128 | 18484 | 49.8 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/square.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/arithmetic/square.aig) |

### Random / control ([random_control/](random_control/))

| File | PIs | POs | ANDs | Size | Upstream |
|------|----:|----:|-----:|-----:|----------|
| [`arbiter.aig`](random_control/arbiter.aig) | 256 | 129 | 11839 | 44.1 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/arbiter.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/arbiter.aig) |
| [`cavlc.aig`](random_control/cavlc.aig) | 10 | 11 | 693 | 2.4 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/cavlc.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/cavlc.aig) |
| [`ctrl.aig`](random_control/ctrl.aig) | 7 | 26 | 174 | 1.1 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/ctrl.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/ctrl.aig) |
| [`dec.aig`](random_control/dec.aig) | 8 | 256 | 304 | 6.5 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/dec.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/dec.aig) |
| [`i2c.aig`](random_control/i2c.aig) | 147 | 142 | 1342 | 7.4 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/i2c.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/i2c.aig) |
| [`int2float.aig`](random_control/int2float.aig) | 11 | 7 | 260 | 1.0 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/int2float.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/int2float.aig) |
| [`mem_ctrl.aig`](random_control/mem_ctrl.aig) | 1204 | 1231 | 46836 | 189.2 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/mem_ctrl.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/mem_ctrl.aig) |
| [`priority.aig`](random_control/priority.aig) | 128 | 8 | 978 | 4.0 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/priority.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/priority.aig) |
| [`router.aig`](random_control/router.aig) | 60 | 30 | 257 | 2.2 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/router.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/router.aig) |
| [`voter.aig`](random_control/voter.aig) | 1001 | 1 | 13758 | 44.3 KB | [view](https://github.com/lsils/benchmarks/blob/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/voter.aig) · [raw](https://raw.githubusercontent.com/lsils/benchmarks/5d342c70d557b5371ce24c20d9d277fb191197f3/random_control/voter.aig) |

## License

EPFL benchmarks are released by LSI under the MIT License;
see the upstream [LICENSE](https://github.com/lsils/benchmarks/blob/master/LICENSE)
file. This repository vendors the files unchanged for reproducibility.

## Citation

> L. Amarú, P.-E. Gaillardon, G. De Micheli, *"The EPFL Combinational
> Benchmark Suite"*, Proc. of the 24th International Workshop on Logic &
> Synthesis (IWLS), 2015. — <https://infoscience.epfl.ch/record/207551>
