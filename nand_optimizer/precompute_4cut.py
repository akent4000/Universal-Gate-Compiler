"""
Precompute the 4-input NPN template database consumed by :mod:`rewrite`
and :mod:`dont_care`.

The output file ``aig_db_4.pkl`` is a binary pickle (~2 MB) and is not
tracked in git.  It is regenerated on demand by :func:`generate_db`, which
is invoked automatically from :mod:`nand_optimizer.__init__` on first
import if the file is missing.  Running this module as a script forces a
rebuild.

The search parallelises across CPU cores: within each cost level, all
(c1, c2) splits are fanned out to a worker pool.  Each worker scans
f1_chunk x pool2 under AND and returns the lex-smallest (f1, f2) pair per
newly-reached truth table; the main process merges by picking the global
min pair, so the final DB is byte-identical regardless of worker count.
"""

from __future__ import annotations
import os
import pickle
import time
import multiprocessing as mp

DB_PATH = os.path.join(os.path.dirname(__file__), "aig_db_4.pkl")

# Set in the main process before spawning Pool workers so that, when a
# worker imports `nand_optimizer.precompute_4cut` to reach `_scan_pairs`,
# the package's `__init__.py` short-circuits and does NOT try to import
# submodules that depend on the (still being generated) `aig_db_4.py`.
_BOOTSTRAP_GUARD_ENV = '_NAND_OPTIMIZER_BOOTSTRAPPING'


def _scan_pairs(task):
    """Worker: scan f1_chunk x pool2 under AND.

    `already` is a 65536-byte bitmap where b[tt] != 0 means the truth table
    `tt` is already covered at a lower cost.  Returns the lex-smallest
    (f1, f2) pair per newly-produced new_tt.
    """
    f1_chunk, pool2, already = task
    found = {}
    for f1 in f1_chunk:
        for f2 in pool2:
            new_tt = f1 & f2
            if already[new_tt]:
                continue
            pair = (f1, f2)
            existing = found.get(new_tt)
            if existing is None or pair < existing:
                found[new_tt] = pair
    return found


def _split_chunks(seq, n_chunks):
    L = len(seq)
    if L == 0 or n_chunks <= 0:
        return []
    n = min(n_chunks, L)
    k, m = divmod(L, n)
    out, start = [], 0
    for i in range(n):
        end = start + k + (1 if i < m else 0)
        out.append(list(seq[start:end]))
        start = end
    return out


def _make_bitmap(covered):
    b = bytearray(65536)
    for tt in covered:
        b[tt] = 1
    return bytes(b)


def generate_db(output_path: str = DB_PATH, *,
                verbose: bool = True,
                workers: int | None = None) -> None:
    if workers is None:
        workers = os.cpu_count() or 1
    workers = max(1, int(workers))

    start_time = time.time()

    x0, x1, x2, x3 = 0xAAAA, 0xCCCC, 0xF0F0, 0xFF00
    lits_tt = {
        0: 0, 1: 0xFFFF,
        2: x0, 3: (~x0) & 0xFFFF,
        4: x1, 5: (~x1) & 0xFFFF,
        6: x2, 7: (~x2) & 0xFFFF,
        8: x3, 9: (~x3) & 0xFFFF,
    }

    best_cost = {tt: 9999 for tt in range(65536)}
    lit_to_args = {i: None for i in range(10)}
    tt_to_lit = {}
    next_lit = 10

    for lit, tt in lits_tt.items():
        best_cost[tt] = 0
        tt_to_lit[tt] = lit

    pools = {0: list(lits_tt.values())}
    covered = set(lits_tt.values())

    use_parallel = workers > 1
    mp_pool = None
    prev_env = None

    if use_parallel:
        prev_env = os.environ.get(_BOOTSTRAP_GUARD_ENV)
        os.environ[_BOOTSTRAP_GUARD_ENV] = '1'
        try:
            ctx = mp.get_context('spawn')
            mp_pool = ctx.Pool(processes=workers)
        except Exception as e:
            if verbose:
                print(f"[precompute_4cut] Pool creation failed ({e!r}); "
                      f"falling back to serial scan.", flush=True)
            if prev_env is None:
                os.environ.pop(_BOOTSTRAP_GUARD_ENV, None)
            else:
                os.environ[_BOOTSTRAP_GUARD_ENV] = prev_env
            use_parallel = False
            mp_pool = None

    try:
        for cost in range(1, 16):
            pools[cost] = []

            splits = [(c1, cost - 1 - c1) for c1 in range(cost)
                      if c1 >= (cost - 1 - c1)]

            tasks = []
            bitmap = _make_bitmap(covered)
            for c1, c2 in splits:
                p1, p2_list = pools[c1], pools[c2]
                if not p1 or not p2_list:
                    continue
                p2 = tuple(p2_list)
                if use_parallel:
                    n_chunks = min(workers * 4, len(p1))
                    for chunk in _split_chunks(p1, n_chunks):
                        tasks.append((chunk, p2, bitmap))
                else:
                    tasks.append((list(p1), p2, bitmap))

            merged = {}
            if use_parallel and tasks:
                iterator = mp_pool.imap_unordered(_scan_pairs, tasks)
            else:
                iterator = (_scan_pairs(t) for t in tasks)

            for res in iterator:
                for new_tt, pair in res.items():
                    existing = merged.get(new_tt)
                    if existing is None or pair < existing:
                        merged[new_tt] = pair

            for new_tt in sorted(merged):
                if new_tt in covered:
                    continue
                f1, f2 = merged[new_tt]

                best_cost[new_tt] = cost

                lit_pos = next_lit; next_lit += 1
                lit_neg = next_lit; next_lit += 1

                lit_to_args[lit_pos] = (tt_to_lit[f1], tt_to_lit[f2])
                lit_to_args[lit_neg] = lit_pos

                tt_to_lit[new_tt] = lit_pos
                tt_to_lit[(~new_tt) & 0xFFFF] = lit_neg

                pools[cost].append(new_tt)
                covered.add(new_tt)

                neg_tt = (~new_tt) & 0xFFFF
                if best_cost[neg_tt] > cost:
                    best_cost[neg_tt] = cost
                    pools[cost].append(neg_tt)
                    covered.add(neg_tt)

            if verbose:
                print(f"Cost {cost}... Covered: {len(covered)} / 65536", flush=True)

            if len(covered) == 65536:
                break
    finally:
        if mp_pool is not None:
            mp_pool.close()
            mp_pool.join()
        if use_parallel:
            if prev_env is None:
                os.environ.pop(_BOOTSTRAP_GUARD_ENV, None)
            else:
                os.environ[_BOOTSTRAP_GUARD_ENV] = prev_env

    if verbose:
        elapsed = time.time() - start_time
        print(f"Total time: {elapsed:.2f}s. Covered: {len(covered)} "
              f"(workers={workers})")
        print(f"Writing db to {output_path}...")

    db = {}
    for tt in range(65536):
        if tt not in tt_to_lit: continue

        final_lit = tt_to_lit[tt]

        ops = []
        visited = {}
        op_idx = 10

        def walk(lit):
            nonlocal op_idx
            if lit < 10:
                return lit

            is_neg = (lit % 2 == 1)
            base_lit = lit - 1 if is_neg else lit

            if base_lit in visited:
                res = visited[base_lit]
                return res ^ 1 if is_neg else res

            args = lit_to_args[base_lit]
            a_mapped = walk(args[0])
            b_mapped = walk(args[1])

            my_idx = op_idx
            op_idx += 2

            ops.append((a_mapped, b_mapped))
            visited[base_lit] = my_idx

            return my_idx ^ 1 if is_neg else my_idx

        out_lit = walk(final_lit)
        db[tt] = (out_lit, ops)

    with open(output_path, 'wb') as f:
        pickle.dump(db, f, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == '__main__':
    generate_db()
