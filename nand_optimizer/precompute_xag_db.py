"""
Precompute the 4-input XAG (XOR-AND Graph) template database.

Extends the pure-AND AIG_DB_4 by also allowing XOR operations at each cost
level.  Each DB entry stores a sequence of (a_lit, b_lit, op_kind) triples
where op_kind 0 = AND, 1 = XOR.

The benefit: XOR-rich functions (parity, adders, comparators, AES S-boxes)
are realized with far fewer gates — e.g. XOR(x0, x1) costs 1 op here vs.
3 ANDs in AIG_DB_4.

Output file ``xag_db_4.pkl`` lives alongside ``aig_db_4.pkl``.  Generated
on demand by :mod:`nand_optimizer.__init__` bootstrap the first time it is
missing; run as a script to force a rebuild.

Op-kind convention (integers so the pkl is compact):
  OP_AND = 0
  OP_XOR = 1
"""

from __future__ import annotations
import os
import pickle
import time
import multiprocessing as mp

DB_PATH = os.path.join(os.path.dirname(__file__), "xag_db_4.pkl")

OP_AND = 0
OP_XOR = 1

_BOOTSTRAP_GUARD_ENV = '_NAND_OPTIMIZER_BOOTSTRAPPING'


def _scan_pairs(task):
    """Worker: scan f1_chunk × pool2 under both AND and XOR.

    Returns {new_tt: (f1, f2, op_kind)} — the lex-smallest triple per new tt.
    """
    f1_chunk, pool2, already = task
    found = {}
    for f1 in f1_chunk:
        for f2 in pool2:
            for op_kind, new_tt in ((OP_AND, f1 & f2), (OP_XOR, f1 ^ f2)):
                if already[new_tt]:
                    continue
                triple = (f1, f2, op_kind)
                existing = found.get(new_tt)
                if existing is None or triple < existing:
                    found[new_tt] = triple
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
    # lit_to_args: lit → (f1_lit, f2_lit, op_kind)  or  lit → base_lit (for complement)
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
                print(f"[precompute_xag_db] Pool creation failed ({e!r}); "
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
                for new_tt, triple in res.items():
                    existing = merged.get(new_tt)
                    if existing is None or triple < existing:
                        merged[new_tt] = triple

            for new_tt in sorted(merged):
                if new_tt in covered:
                    continue
                f1, f2, op_kind = merged[new_tt]

                best_cost[new_tt] = cost

                lit_pos = next_lit; next_lit += 1
                lit_neg = next_lit; next_lit += 1

                lit_to_args[lit_pos] = (tt_to_lit[f1], tt_to_lit[f2], op_kind)
                lit_to_args[lit_neg] = lit_pos   # complement pointer

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

    # Build serialisable DB: tt → (out_lit_idx, [(a_idx, b_idx, op_kind), ...])
    db = {}
    for tt in range(65536):
        if tt not in tt_to_lit:
            continue

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
            if isinstance(args, int):
                # complement pointer — args IS the positive base literal
                inner = walk(args)
                visited[base_lit] = inner ^ 1
                return inner if not is_neg else inner ^ 1

            a_lit_src, b_lit_src, kind = args
            a_mapped = walk(a_lit_src)
            b_mapped = walk(b_lit_src)

            my_idx = op_idx
            op_idx += 2

            ops.append((a_mapped, b_mapped, kind))
            visited[base_lit] = my_idx

            return my_idx ^ 1 if is_neg else my_idx

        out_lit = walk(final_lit)
        db[tt] = (out_lit, ops)

    with open(output_path, 'wb') as f:
        pickle.dump(db, f, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == '__main__':
    generate_db()
