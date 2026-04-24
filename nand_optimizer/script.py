"""
Synthesis script parser, executor, and bandit-guided search.

A synthesis script is a semicolon-separated sequence of AIG optimization
commands, mirroring ABC-style scripts, e.g.:

    balance; rewrite; fraig; dc; rewrite; balance

Commands
--------
balance              minimise critical-path depth (area-preserving)
rewrite  [-z] [-x] [-r N] [-K N]
                     local AIG rewriting via k-feasible cuts
                       -z       use exact (SAT-based) sub-circuit synthesis
                       -x       enable XAG templates (AND+XOR) from XAG_DB_4;
                                comparator weighs AND=2 NANDs, XOR=4 NANDs
                       -r N     number of rewriting rounds  (default 1)
                       -K N     cut size                    (default 4)
refactor [-z] [-r N] [-K N]
                     alias for rewrite (factored-form replacement)
fraig                merge equivalent nodes (simulation + SAT)
dc [-K N] [-T N] [-W N] [-r N] [-C N] [--no-sdc] [--odc] [--dc-exact] [--no-resub]
                     don't-care-based local rewriting
                       -K N       cut size                     (default 4)
                       -T N       Z3 timeout per query, in ms  (default 1000)
                       -W N       resub window size            (default 64)
                       -r N       iterative rounds (V2.b)      (default 1)
                       -C N       care propagation rounds      (default 1)
                       --no-sdc   disable satisfiability DCs
                       --odc      enable observability DCs (V2 sim-based
                                  admissibility check; sound modulo sim
                                  coverage, protected by end-of-pass miter)
                       --odc-mode M  ODC mode: legacy (default), z3-exact
                                  (sound exact Z3 admissibility — closes the
                                  ROADMAP P0#1 soundness gap, ~3-30× slower),
                                  hybrid (drift-aware care refresh; use with
                                  --care-refresh-every N), window (forward-
                                  flip fanout-bounded care; use with
                                  --window-depth K)
                       --care-refresh-every N  refresh care every N rewrites
                                  (hybrid mode only; 0 = off; default 0)
                       --window-depth K  fanout window depth (window mode;
                                  default 5)
                       --dc-exact enable SAT-based exact-synth fallback
                                  for cuts with k > 4 (requires -K >= 5)
                       --no-resub disable V2.c window resubstitution
bidec [-K N] [-k N] [-r N] [-z]
                     disjoint-support bi-decomposition (AND/OR/XOR)
                       -K N   max cut size      (default 8)
                       -k N   min cut size      (default 5)
                       -r N   rounds            (default 1)
                       -z     SAT exact synthesis for halves with >4 inputs
bdd [-K N]           BDD-guided per-output rebuild via sifting reorder
                       -K N   max supports size (default 16)
resub [-K N] [-k N] [-M N] [-D N] [-r N]
                     functional resubstitution for wide cuts (dc2-style)
                       -K N   max cut size      (default 7)
                       -k N   min cut size      (default 5)
                       -M N   max divisors m    (default 3)
                       -D N   divisor pool cap  (default 20)
                       -r N   rounds            (default 1)

Bandit-guided search
--------------------
ScriptBandit selects synthesis commands adaptively using UCB1 or Thompson
Sampling.  Reward is the fractional reduction in AIG node count per step.
Use run_bandit() for a single-circuit bandit session, or pass
bandit_horizon > 0 to pipeline.optimize() to invoke it post-AIG.
"""
from __future__ import annotations
import math
from random import Random
from typing import Any, Dict, List, Optional, Tuple

from .core.aig import AIG

AIGLit = int

#  ``dc`` is currently excluded from the default script: ``dc --odc`` has a
#  soundness gap on reconvergent-fanout circuits (ROADMAP P0#1) that causes
#  the safety-net miter to revert the whole pass on several EPFL designs,
#  producing 0% QoR gain. Users who want DC can still call it explicitly
#  via ``--script "... dc ..."``.
DEFAULT_SCRIPT = "rewrite; fraig; rewrite; balance"

DEFAULT_ARMS: List[str] = [
    'balance',
    'rewrite',
    'rewrite -z',
    'fraig',
    'dc',
]


# ---------------------------------------------------------------------------
#  Multi-Armed Bandit controller
# ---------------------------------------------------------------------------

class ScriptBandit:
    """UCB1 / Thompson-Sampling bandit for synthesis command selection.

    Each arm is a single-command script string (e.g. ``'rewrite'``,
    ``'fraig'``).  At each step :meth:`select` returns the arm index to
    pull; after executing it call :meth:`update` with the observed reward
    (fractional AIG-node reduction: ``(n_before - n_after) / n_before``).
    """

    def __init__(
        self,
        arms:     Optional[List[str]] = None,
        horizon:  int   = 20,
        strategy: str   = 'ucb1',
        c:        float = 1.0,
        seed:     int   = 0,
    ) -> None:
        self.arms     = list(arms) if arms is not None else list(DEFAULT_ARMS)
        self.horizon  = horizon
        self.strategy = strategy
        self.c        = c
        self._rng     = Random(seed)

        n = len(self.arms)
        self._counts: List[int]   = [0]   * n
        self._sum:    List[float] = [0.0] * n
        # Beta-distribution parameters for Thompson Sampling (prior = Beta(1,1))
        self._alpha:  List[float] = [1.0] * n
        self._beta:   List[float] = [1.0] * n
        self._t: int = 0

    # -- selection policy -------------------------------------------------------

    def select(self) -> int:
        """Return the index of the arm to pull next."""
        for i, cnt in enumerate(self._counts):
            if cnt == 0:
                return i

        if self.strategy == 'ucb1':
            log_t = math.log(self._t)
            def _ucb(i: int) -> float:
                mu = self._sum[i] / self._counts[i]
                return mu + self.c * math.sqrt(2.0 * log_t / self._counts[i])
            return max(range(len(self.arms)), key=_ucb)

        if self.strategy == 'thompson':
            samples = [
                self._rng.betavariate(self._alpha[i], self._beta[i])
                for i in range(len(self.arms))
            ]
            return max(range(len(self.arms)), key=lambda i: samples[i])

        raise ValueError(
            f"Unknown strategy {self.strategy!r}; choose 'ucb1' or 'thompson'"
        )

    # -- update -----------------------------------------------------------------

    def update(self, arm: int, reward: float) -> None:
        """Record *reward* for a pull of *arm*."""
        self._counts[arm] += 1
        self._sum[arm]    += reward
        self._t           += 1
        if reward > 0:
            self._alpha[arm] += 1.0
        else:
            self._beta[arm]  += 1.0

    # -- introspection ----------------------------------------------------------

    @property
    def mean_rewards(self) -> List[float]:
        return [
            self._sum[i] / self._counts[i] if self._counts[i] > 0 else 0.0
            for i in range(len(self.arms))
        ]

    def best_arm(self) -> int:
        """Index of arm with the highest empirical mean reward."""
        return max(range(len(self.arms)), key=lambda i: self.mean_rewards[i])

    def report(self) -> str:
        lines = [f'ScriptBandit ({self.strategy}, horizon={self.horizon}):']
        mu_list = self.mean_rewards
        for i, arm in enumerate(self.arms):
            n   = self._counts[i]
            mu  = mu_list[i]
            bar = '#' * max(0, round(mu * 40))
            lines.append(
                f'  [{i}] {arm!r:<22}  pulls={n:3d}  mean={mu:+.4f}  {bar}'
            )
        lines.append(f'  best arm: {self.arms[self.best_arm()]!r}')
        return '\n'.join(lines)


def parse_script(script: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Parse a semicolon-separated synthesis script into (cmd, kwargs) pairs."""
    commands: List[Tuple[str, Dict[str, Any]]] = []
    for part in script.split(';'):
        tokens = part.strip().split()
        if not tokens:
            continue
        cmd = tokens[0].lower()
        if cmd not in ('balance', 'rewrite', 'refactor', 'fraig', 'dc',
                       'bidec', 'bdd', 'resub'):
            raise ValueError(
                f"Unknown synthesis command '{tokens[0]}'. "
                f"Supported: balance, rewrite, refactor, fraig, dc, "
                f"bidec, bdd, resub"
            )
        kwargs: Dict[str, Any] = {}
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == '-z' and cmd in ('rewrite', 'refactor', 'bidec'):
                kwargs['use_exact'] = True
            elif tok == '-x' and cmd in ('rewrite', 'refactor'):
                kwargs['use_xag'] = True
            elif tok == '--no-sdc' and cmd == 'dc':
                kwargs['use_sdc'] = False
            elif tok == '--odc' and cmd == 'dc':
                kwargs['use_odc'] = True
            elif tok == '--no-odc' and cmd == 'dc':
                kwargs['use_odc'] = False
            elif tok == '--odc-mode' and cmd == 'dc':
                if i + 1 >= len(tokens):
                    raise ValueError("--odc-mode requires a mode argument")
                from .synthesis.dont_care import (
                    ODC_MODE_LEGACY, ODC_MODE_HYBRID, ODC_MODE_WINDOW, ODC_MODE_Z3_EXACT
                )
                m = tokens[i + 1]
                if m not in (ODC_MODE_LEGACY, ODC_MODE_HYBRID, ODC_MODE_WINDOW, ODC_MODE_Z3_EXACT):
                    raise ValueError(
                        f"Unknown --odc-mode {m!r}; choose: legacy, hybrid, window, z3-exact"
                    )
                kwargs['odc_mode'] = m
                i += 1
            elif tok == '--care-refresh-every' and cmd == 'dc':
                if i + 1 >= len(tokens):
                    raise ValueError("--care-refresh-every requires an integer argument")
                kwargs['care_refresh_every'] = int(tokens[i + 1])
                i += 1
            elif tok == '--window-depth' and cmd == 'dc':
                if i + 1 >= len(tokens):
                    raise ValueError("--window-depth requires an integer argument")
                kwargs['window_depth'] = int(tokens[i + 1])
                i += 1
            elif tok == '--dc-exact' and cmd == 'dc':
                kwargs['use_exact'] = True
            elif tok == '--no-resub' and cmd == 'dc':
                kwargs['use_resub'] = False
            elif tok in ('-r', '-K', '-T', '-W', '-C', '-k', '-M', '-D'):
                if i + 1 >= len(tokens):
                    raise ValueError(
                        f"Flag {tok} requires an integer argument "
                        f"(in '{part.strip()}')"
                    )
                val = int(tokens[i + 1])
                if tok == '-r':
                    if cmd not in ('rewrite', 'refactor', 'dc', 'bidec', 'resub'):
                        raise ValueError(f"Flag -r is only valid for 'rewrite', "
                                         f"'refactor', 'dc', 'bidec', or 'resub'")
                    kwargs['rounds'] = val
                elif tok == '-K':
                    if cmd == 'bdd':
                        kwargs['max_inputs'] = val
                    elif cmd == 'bidec':
                        kwargs['cut_size'] = val
                    elif cmd == 'resub':
                        kwargs['cut_size_max'] = val
                    else:
                        kwargs['cut_size'] = val
                elif tok == '-k':
                    if cmd == 'bidec':
                        kwargs['min_cut'] = val
                    elif cmd == 'resub':
                        kwargs['cut_size_min'] = val
                    else:
                        raise ValueError(f"Flag -k is only valid for 'bidec' or 'resub'")
                elif tok == '-M':
                    if cmd != 'resub':
                        raise ValueError(f"Flag -M is only valid for 'resub'")
                    kwargs['max_m'] = val
                elif tok == '-D':
                    if cmd != 'resub':
                        raise ValueError(f"Flag -D is only valid for 'resub'")
                    kwargs['max_divisors'] = val
                elif tok == '-T':
                    if cmd != 'dc':
                        raise ValueError(f"Flag -T is only valid for 'dc'")
                    kwargs['timeout_ms'] = val
                elif tok == '-W':
                    if cmd != 'dc':
                        raise ValueError(f"Flag -W is only valid for 'dc'")
                    kwargs['resub_window'] = val
                elif tok == '-C':
                    if cmd != 'dc':
                        raise ValueError(f"Flag -C is only valid for 'dc'")
                    kwargs['care_rounds'] = val
                i += 1
            else:
                raise ValueError(
                    f"Unknown flag '{tok}' for command '{cmd}'."
                )
            i += 1
        commands.append((cmd, kwargs))
    return commands


def _fmt_flags(kwargs: Dict[str, Any]) -> str:
    parts = []
    if kwargs.get('use_exact'):
        parts.append('-z')
    if kwargs.get('use_xag'):
        parts.append('-x')
    if kwargs.get('use_sdc') is False:
        parts.append('--no-sdc')
    if kwargs.get('use_odc') is True:
        parts.append('--odc')
    elif kwargs.get('use_odc') is False:
        parts.append('--no-odc')
    if kwargs.get('use_exact') is True and 'use_exact' in kwargs:
        parts.append('--dc-exact')
    if kwargs.get('use_resub') is False:
        parts.append('--no-resub')
    if 'resub_window' in kwargs:
        parts.append(f"-W {kwargs['resub_window']}")
    if 'rounds' in kwargs:
        parts.append(f"-r {kwargs['rounds']}")
    if 'care_rounds' in kwargs:
        parts.append(f"-C {kwargs['care_rounds']}")
    if 'cut_size' in kwargs:
        parts.append(f"-K {kwargs['cut_size']}")
    if 'timeout_ms' in kwargs:
        parts.append(f"-T {kwargs['timeout_ms']}")
    return ' '.join(parts)


def run_script(
    aig: AIG,
    out_lits: List[AIGLit],
    script: str,
    verbose: bool = True,
) -> Tuple[AIG, List[AIGLit]]:
    """Apply a synthesis script to an AIG, returning (new_aig, new_out_lits)."""
    from .synthesis.rewrite    import rewrite_aig
    from .synthesis.balance    import balance_aig, aig_depth
    from .synthesis.fraig      import fraig as _fraig
    from .synthesis.dont_care  import dc_optimize

    commands = parse_script(script)
    n = len(commands)

    for step, (cmd, kwargs) in enumerate(commands, 1):
        if verbose:
            flags = _fmt_flags(kwargs)
            tag   = f"{cmd} {flags}".strip()
            print(f"\n  [script {step}/{n}] {tag}")

        n_before = aig.n_nodes

        if cmd == 'balance':
            d_before     = aig_depth(aig, out_lits)
            aig, out_lits = balance_aig(aig, out_lits)
            d_after      = aig_depth(aig, out_lits)
            if verbose:
                print(f"      depth: {d_before} -> {d_after}  "
                      f"(nodes: {n_before} -> {aig.n_nodes})")

        elif cmd in ('rewrite', 'refactor'):
            aig, out_lits = rewrite_aig(aig, out_lits, **kwargs)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

        elif cmd == 'fraig':
            aig, out_lits = _fraig(aig, out_lits)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

        elif cmd == 'dc':
            dc_kwargs = dict(kwargs)
            if 'care_rounds' in dc_kwargs:
                dc_kwargs['rounds'] = dc_kwargs.pop('care_rounds')
            aig, out_lits = dc_optimize(aig, out_lits, **dc_kwargs)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

        elif cmd == 'bidec':
            from .synthesis.bidec import bidec_aig
            aig, out_lits = bidec_aig(aig, out_lits, **kwargs)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

        elif cmd == 'bdd':
            from .synthesis.bdd_decomp import bdd_decompose_aig
            aig, out_lits = bdd_decompose_aig(aig, out_lits, **kwargs)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

        elif cmd == 'resub':
            from .synthesis.sat_resub import resub_aig
            aig, out_lits = resub_aig(aig, out_lits, **kwargs)
            if verbose:
                print(f"      nodes: {n_before} -> {aig.n_nodes}")

    return aig, out_lits


# ---------------------------------------------------------------------------
#  Bandit-controlled synthesis loop
# ---------------------------------------------------------------------------

def run_bandit(
    aig:      AIG,
    out_lits: List[AIGLit],
    arms:     Optional[List[str]] = None,
    horizon:  int   = 20,
    strategy: str   = 'ucb1',
    c:        float = 1.0,
    verbose:  bool  = True,
    seed:     int   = 0,
) -> Tuple[AIG, List[AIGLit], ScriptBandit]:
    """Bandit-controlled synthesis: adaptively select and apply passes.

    Each of the *horizon* steps the bandit chooses a command from *arms*,
    executes it via :func:`run_script`, observes the fractional AIG-node
    reduction as the reward, and updates its statistics.

    Returns ``(aig, out_lits, bandit)`` so the caller can inspect the
    accumulated arm statistics via :meth:`ScriptBandit.report`.
    """
    bandit = ScriptBandit(arms, horizon=horizon, strategy=strategy,
                          c=c, seed=seed)

    for step in range(1, horizon + 1):
        arm_idx  = bandit.select()
        cmd      = bandit.arms[arm_idx]
        n_before = aig.n_nodes

        aig, out_lits = run_script(aig, out_lits, cmd, verbose=False)

        n_after = aig.n_nodes
        reward  = (n_before - n_after) / max(n_before, 1)
        bandit.update(arm_idx, reward)

        if verbose:
            print(
                f'  [bandit {step:2d}/{horizon}] {cmd!r:<22}  '
                f'nodes: {n_before} -> {n_after}  reward={reward:+.4f}'
            )

    if verbose:
        print(f'\n{bandit.report()}')

    return aig, out_lits, bandit
