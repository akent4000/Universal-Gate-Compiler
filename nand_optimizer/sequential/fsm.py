"""
Phase 3 — Finite State Machine (FSM) synthesis.

Pipeline for sequential logic:

    StateTable (Mealy or Moore, possibly incompletely specified)
      -> [1] State minimization (Hopcroft partition refinement;
             STAMINA-like heuristic when don't-cares are present)
      -> [2] State encoding (Binary / One-Hot / Gray)
      -> [3] Excitation-function synthesis: one TruthTable with
             inputs  = state_bits ++ fsm_inputs
             outputs = next_state_bits ++ fsm_outputs
      -> [4] Combinational optimization via the existing pipeline
      -> [5] D flip-flop insertion around the combinational cone
             (next-state bits feed back through DFFs to state bits)

The combinational cone is acyclic by construction: state bits are treated
as primary inputs and next-state bits as primary outputs, so the feedback
edge only exists through a flip-flop and is cut for every purely
combinational pass (topological sort, rewrite, FRAIG, balance, verify).

Public API:

    StateTable, Transition
    minimize_states(stt)            # dispatches on completely/incompletely specified
    encode_states(stt, strategy)    # 'binary' | 'onehot' | 'gray'
    fsm_to_truth_table(stt, enc)    # TruthTable for the combinational cone
    synthesize_fsm(stt, ...)        # full pipeline; returns FSMResult
    simulate_fsm(fsm_result, input_seq)
    parse_kiss(text)                # KISS2 format

    FSMResult
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from ..core.truth_table import TruthTable, DASH
from ..pipeline         import OptimizeResult


# ═══════════════════════════════════════════════════════════════════════════════
#  StateTable
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Transition:
    """
    One edge of the state graph.

    Attributes
    ----------
    src     : source state name.
    inp     : ternary input cube — tuple of length n_input_bits with values
              {0, 1, DASH}.  DASH means "any value on this input bit".
    dst     : destination state name.  May be None for incompletely-specified
              FSMs where the next state is a don't-care on this edge.
    out     : ternary output cube — tuple of length n_output_bits with values
              {0, 1, DASH}.  Used only for Mealy machines; empty tuple for
              Moore.  DASH positions are don't-cares.
    """
    src: str
    inp: Tuple[int, ...]
    dst: Optional[str]
    out: Tuple[int, ...] = ()


class StateTable:
    """
    Symbolic description of a Mealy or Moore FSM.

    For Moore machines, per-state outputs live in ``state_outputs``; every
    Transition has ``out = ()``.  For Mealy machines, outputs live on the
    Transitions and ``state_outputs`` is empty.

    Partial specifications are allowed:
      • Missing (state, input) combinations are treated as global don't-cares.
      • A Transition may have ``dst = None`` (next-state DC) or DASH positions
        in ``out`` (output bit DC).
      • In Moore mode, a state whose entry in ``state_outputs`` contains
        DASH values exports those bits as don't-cares.
    """

    def __init__(
        self,
        states:            List[str],
        input_names:       List[str],
        output_names:      List[str],
        transitions:       List[Transition],
        model:             str                      = 'mealy',
        reset_state:       Optional[str]            = None,
        state_outputs:     Optional[Dict[str, Tuple[int, ...]]] = None,
        reset_input_name:  Optional[str]            = None,
        reset_polarity:    str                      = 'sync',
    ):
        if model not in ('mealy', 'moore'):
            raise ValueError(f"model must be 'mealy' or 'moore', got {model!r}")
        if not states:
            raise ValueError("states must be non-empty")
        if reset_polarity not in ('sync', 'async_low', 'async_high'):
            raise ValueError(
                f"reset_polarity must be 'sync', 'async_low', or "
                f"'async_high', got {reset_polarity!r}")
        if reset_polarity != 'sync' and reset_input_name is None:
            raise ValueError(
                f"reset_polarity={reset_polarity!r} requires reset_input_name")
        if reset_input_name is not None and reset_input_name in input_names:
            raise ValueError(
                f"reset_input_name {reset_input_name!r} must not appear in "
                f"input_names — the async reset is a separate control tract "
                f"routed directly to flip-flop CLR pins, not to the "
                f"combinational excitation cone")

        self.states            = list(states)
        self.input_names       = list(input_names)
        self.output_names      = list(output_names)
        self.transitions       = list(transitions)
        self.model             = model
        self.reset_state       = reset_state if reset_state is not None else self.states[0]
        self.state_outputs     = dict(state_outputs) if state_outputs else {}
        self.reset_input_name  = reset_input_name
        self.reset_polarity    = reset_polarity

        if self.reset_state not in self.states:
            raise ValueError(f"reset_state {self.reset_state!r} not in states")

        for t in self.transitions:
            if t.src not in self.states:
                raise ValueError(f"transition src {t.src!r} not in states")
            if t.dst is not None and t.dst not in self.states:
                raise ValueError(f"transition dst {t.dst!r} not in states")
            if len(t.inp) != len(self.input_names):
                raise ValueError(
                    f"transition input cube length {len(t.inp)} "
                    f"!= n_input_bits {len(self.input_names)}")
            if model == 'mealy' and len(t.out) != len(self.output_names):
                raise ValueError(
                    f"Mealy transition output cube length {len(t.out)} "
                    f"!= n_output_bits {len(self.output_names)}")
            if model == 'moore' and t.out:
                raise ValueError("Moore transitions must not carry output bits")

        if model == 'moore':
            for s in self.states:
                if s not in self.state_outputs:
                    raise ValueError(f"Moore FSM missing output for state {s!r}")
                if len(self.state_outputs[s]) != len(self.output_names):
                    raise ValueError(
                        f"Moore state_outputs[{s!r}] length "
                        f"{len(self.state_outputs[s])} != "
                        f"n_output_bits {len(self.output_names)}")

    # ── structural properties ────────────────────────────────────────────────

    @property
    def n_states(self) -> int:
        return len(self.states)

    @property
    def n_input_bits(self) -> int:
        return len(self.input_names)

    @property
    def n_output_bits(self) -> int:
        return len(self.output_names)

    def is_completely_specified(self) -> bool:
        """True iff every (state, input_pattern) has a fully specified edge."""
        if any(t.dst is None for t in self.transitions):
            return False
        if self.model == 'mealy':
            if any(DASH in t.out for t in self.transitions):
                return False
        else:
            for s in self.states:
                if DASH in self.state_outputs[s]:
                    return False

        # Every (state, input) pattern must be covered
        n_patterns = 1 << self.n_input_bits
        for s in self.states:
            covered: Set[int] = set()
            for t in self.transitions:
                if t.src != s:
                    continue
                for m in _expand_cube(t.inp):
                    covered.add(m)
            if len(covered) != n_patterns:
                return False
        return True

    def outgoing(self, src: str) -> List[Transition]:
        return [t for t in self.transitions if t.src == src]

    def __repr__(self) -> str:
        return (f"StateTable({self.model}, {self.n_states} states, "
                f"{self.n_input_bits} in-bits, {self.n_output_bits} out-bits, "
                f"{len(self.transitions)} transitions)")


# ═══════════════════════════════════════════════════════════════════════════════
#  Cube helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _expand_cube(cube: Tuple[int, ...]) -> List[int]:
    """Enumerate all concrete input patterns (as ints, MSB = cube[0]) covered."""
    n = len(cube)
    patterns = [0]
    for i, b in enumerate(cube):
        pos = n - 1 - i
        if b == DASH:
            patterns = patterns + [p | (1 << pos) for p in patterns]
        elif b == 1:
            patterns = [p | (1 << pos) for p in patterns]
    return patterns


def _pattern_bits(pat: int, n: int) -> Tuple[int, ...]:
    """MSB-first bit tuple of integer *pat* over *n* bits."""
    return tuple((pat >> (n - 1 - i)) & 1 for i in range(n))


def _merge_ternary(a: int, b: int) -> int:
    """Return the tighter of two ternary specifications, or -2 on conflict."""
    if a == DASH:
        return b
    if b == DASH:
        return a
    return a if a == b else -2


# ═══════════════════════════════════════════════════════════════════════════════
#  State minimization — Hopcroft partition refinement (completely specified)
# ═══════════════════════════════════════════════════════════════════════════════

def _expand_stt(stt: StateTable) -> Tuple[Dict[Tuple[str, int], Optional[str]],
                                          Dict[Tuple[str, int], Tuple[int, ...]]]:
    """
    Expand a StateTable into (delta, lam) dictionaries indexed by (state, pat).

    delta[(s, pat)] = next-state name, or None if DC.
    lam  [(s, pat)] = output vector for this (s, pat).  For Mealy it comes
                      from the matching transition (DASH positions kept).
                      For Moore it is the state's output vector.  If multiple
                      transitions cover the same (s, pat), their output
                      specifications are merged (compatible DASH fusion).
    """
    n = stt.n_input_bits
    delta: Dict[Tuple[str, int], Optional[str]]    = {}
    lam:   Dict[Tuple[str, int], Tuple[int, ...]]  = {}

    if stt.model == 'moore':
        # For Moore, outputs are fixed per state
        for s in stt.states:
            for pat in range(1 << n):
                lam[(s, pat)] = stt.state_outputs[s]

    for t in stt.transitions:
        for pat in _expand_cube(t.inp):
            key = (t.src, pat)
            # Next-state merge: conflict if two transitions disagree and
            # neither side is None.
            if key in delta:
                prev = delta[key]
                if prev is None:
                    delta[key] = t.dst
                elif t.dst is not None and prev != t.dst:
                    raise ValueError(
                        f"Conflicting next states for ({t.src}, pat={pat:0{n}b}): "
                        f"{prev!r} vs {t.dst!r}")
            else:
                delta[key] = t.dst

            # Output merge
            if stt.model == 'mealy':
                if key in lam:
                    merged = list(lam[key])
                    for i, bit in enumerate(t.out):
                        m = _merge_ternary(merged[i], bit)
                        if m == -2:
                            raise ValueError(
                                f"Conflicting output bit {i} for "
                                f"({t.src}, pat={pat:0{n}b})")
                        merged[i] = m
                    lam[key] = tuple(merged)
                else:
                    lam[key] = t.out

    # Fill holes (missing (s, pat)) with full-DC
    dc_out = tuple([DASH] * stt.n_output_bits)
    for s in stt.states:
        for pat in range(1 << n):
            key = (s, pat)
            if key not in delta:
                delta[key] = None
            if key not in lam:
                lam[key] = dc_out
    return delta, lam


def _hopcroft(stt: StateTable) -> List[Set[str]]:
    """
    Partition-refinement state minimization for a completely-specified FSM.

    Returns a list of equivalence classes (sets of state names).
    """
    delta, lam = _expand_stt(stt)
    n_patterns = 1 << stt.n_input_bits
    states     = list(stt.states)

    # Initial partition: states grouped by output behaviour across all inputs.
    # For Moore this collapses to one class per distinct state-output vector
    # (inputs don't change the output); for Mealy it groups by the full
    # output function.
    def output_signature(s: str) -> Tuple[Tuple[int, ...], ...]:
        return tuple(lam[(s, pat)] for pat in range(n_patterns))

    class_of: Dict[str, int] = {}
    signatures: Dict[Tuple, int] = {}
    partitions: List[Set[str]] = []
    for s in states:
        sig = output_signature(s)
        if sig not in signatures:
            signatures[sig] = len(partitions)
            partitions.append(set())
        cid = signatures[sig]
        partitions[cid].add(s)
        class_of[s] = cid

    # Iterative refinement: split on successor-class signatures.
    changed = True
    while changed:
        changed = False
        new_partitions: List[Set[str]] = []
        new_class_of: Dict[str, int] = {}
        for block in partitions:
            # Group by (successor-class-id vector across all inputs).
            groups: Dict[Tuple[int, ...], Set[str]] = {}
            for s in block:
                succ_sig = tuple(
                    class_of[delta[(s, pat)]] if delta[(s, pat)] is not None else -1
                    for pat in range(n_patterns))
                groups.setdefault(succ_sig, set()).add(s)
            if len(groups) > 1:
                changed = True
            for g in groups.values():
                cid = len(new_partitions)
                new_partitions.append(g)
                for s in g:
                    new_class_of[s] = cid
        partitions = new_partitions
        class_of   = new_class_of

    return partitions


def _minimize_completely_specified(stt: StateTable) -> StateTable:
    """Apply Hopcroft classes and emit a smaller, equivalent StateTable."""
    classes = _hopcroft(stt)

    # Canonical representative = lexicographically smallest state in class
    rep: Dict[str, str] = {}
    for cls in classes:
        r = min(cls)
        for s in cls:
            rep[s] = r

    # Same or fewer states → no change; bail early
    if len(classes) == len(stt.states):
        return stt

    # Renumber representatives keeping reset state first for readability
    new_states_ordered: List[str] = []
    seen: Set[str] = set()
    root = rep[stt.reset_state]
    new_states_ordered.append(root)
    seen.add(root)
    for s in stt.states:
        r = rep[s]
        if r not in seen:
            new_states_ordered.append(r)
            seen.add(r)

    new_transitions: List[Transition] = []
    seen_keys: Set[Tuple[str, Tuple[int, ...], Optional[str], Tuple[int, ...]]] = set()
    for t in stt.transitions:
        r_src = rep[t.src]
        r_dst = rep[t.dst] if t.dst is not None else None
        key   = (r_src, t.inp, r_dst, t.out)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        new_transitions.append(Transition(r_src, t.inp, r_dst, t.out))

    new_state_outputs: Dict[str, Tuple[int, ...]] = {}
    if stt.model == 'moore':
        for s in new_states_ordered:
            new_state_outputs[s] = stt.state_outputs[s]

    return StateTable(
        states            = new_states_ordered,
        input_names       = stt.input_names,
        output_names      = stt.output_names,
        transitions       = new_transitions,
        model             = stt.model,
        reset_state       = rep[stt.reset_state],
        state_outputs     = new_state_outputs,
        reset_input_name  = stt.reset_input_name,
        reset_polarity    = stt.reset_polarity,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  IS-FSM minimization (incompletely specified) — compatible-merging heuristic
# ═══════════════════════════════════════════════════════════════════════════════

def _compatible_outputs(a: Tuple[int, ...], b: Tuple[int, ...]) -> bool:
    """Two output vectors are compatible if they agree on every non-DC bit."""
    for x, y in zip(a, b):
        if x != DASH and y != DASH and x != y:
            return False
    return True


def _compatible_pairs(stt: StateTable) -> Dict[FrozenSet[str], Set[FrozenSet[str]]]:
    """
    Compute the compatibility relation on states for an IS-FSM.

    Two states s1, s2 are *output-compatible* if their output specifications
    agree on all (input, bit) positions where both are defined.  They are
    *(conditionally) compatible* if, in addition, for every input pattern
    where both have defined next-states, those next-states are also
    compatible.  The fixpoint of this implication is computed classically:

        1. Seed with all output-compatible pairs.
        2. Drop any pair whose implication class is not contained in the
           current compatibility set.
        3. Repeat until stable.

    Returns pair → set of implied-pair frozensets (may be empty).
    """
    delta, lam = _expand_stt(stt)
    states     = stt.states
    n_patterns = 1 << stt.n_input_bits

    pairs: Dict[FrozenSet[str], Set[FrozenSet[str]]] = {}
    for i, s1 in enumerate(states):
        for s2 in states[i + 1:]:
            key = frozenset((s1, s2))
            implied: Set[FrozenSet[str]] = set()
            ok = True
            for pat in range(n_patterns):
                if not _compatible_outputs(lam[(s1, pat)], lam[(s2, pat)]):
                    ok = False
                    break
                n1 = delta[(s1, pat)]
                n2 = delta[(s2, pat)]
                if n1 is None or n2 is None or n1 == n2:
                    continue
                implied.add(frozenset((n1, n2)))
            if ok:
                pairs[key] = implied

    # Fixpoint: drop any pair whose implication is not in pairs
    changed = True
    while changed:
        changed = False
        for key in list(pairs.keys()):
            for imp in pairs[key]:
                if imp not in pairs:
                    del pairs[key]
                    changed = True
                    break
    return pairs


def _maximal_compatibles(states: List[str],
                         pairs: Dict[FrozenSet[str], Set[FrozenSet[str]]]
                         ) -> List[Set[str]]:
    """
    Enumerate maximal compatible classes (cliques in the compatibility graph).

    Bron-Kerbosch over the pairwise-compatibility graph.  For small FSMs
    (n_states ≤ 32) this is fast enough; otherwise we truncate and fall
    back to a singleton cover.
    """
    neigh: Dict[str, Set[str]] = {s: set() for s in states}
    for key in pairs:
        a, b = tuple(key) if len(key) == 2 else (next(iter(key)),) * 2
        neigh[a].add(b); neigh[b].add(a)

    cliques: List[Set[str]] = []

    def bron(R: Set[str], P: Set[str], X: Set[str]):
        if not P and not X:
            cliques.append(set(R))
            return
        if len(cliques) >= 10_000:          # safety cap
            return
        for v in list(P):
            bron(R | {v}, P & neigh[v], X & neigh[v])
            P.discard(v)
            X.add(v)

    if len(states) <= 24:
        bron(set(), set(states), set())
    else:
        cliques = [{s} for s in states]

    # Ensure every state appears in at least one clique
    covered = set().union(*cliques) if cliques else set()
    for s in states:
        if s not in covered:
            cliques.append({s})
    return cliques


def _minimize_incompletely_specified(stt: StateTable) -> StateTable:
    """
    Greedy STAMINA-like cover: enumerate maximal compatibles, then pick
    the one that covers the most still-uncovered states, repeat.  The
    implication closure is already baked into the compatibility relation.
    """
    pairs    = _compatible_pairs(stt)
    cliques  = _maximal_compatibles(stt.states, pairs)

    # If no two states merge, bail early
    if all(len(c) == 1 for c in cliques):
        return stt

    remaining: Set[str] = set(stt.states)
    chosen: List[Set[str]] = []
    while remaining:
        best = max(cliques, key=lambda c: len(c & remaining))
        if not (best & remaining):
            break
        chosen.append(best & remaining)
        remaining -= best

    # Representative mapping
    rep: Dict[str, str] = {}
    for cls in chosen:
        r = min(cls)
        for s in cls:
            rep[s] = r

    if len(chosen) == len(stt.states):
        return stt

    # Derive new states + ordered list with reset first
    new_states_ordered: List[str] = []
    seen: Set[str] = set()
    root = rep[stt.reset_state]
    new_states_ordered.append(root); seen.add(root)
    for s in stt.states:
        r = rep[s]
        if r not in seen:
            new_states_ordered.append(r); seen.add(r)

    # Rebuild transitions (merging DC/conflict is handled by _expand_stt
    # downstream — here we just collect the union of edges).
    new_transitions: List[Transition] = []
    for t in stt.transitions:
        r_src = rep[t.src]
        r_dst = rep[t.dst] if t.dst is not None else None
        new_transitions.append(Transition(r_src, t.inp, r_dst, t.out))

    # Moore output merge per representative (compatible fusion)
    new_state_outputs: Dict[str, Tuple[int, ...]] = {}
    if stt.model == 'moore':
        for r in new_states_ordered:
            members = [s for s in stt.states if rep[s] == r]
            merged  = list(stt.state_outputs[members[0]])
            for m in members[1:]:
                row = stt.state_outputs[m]
                for i, b in enumerate(row):
                    merged[i] = _merge_ternary(merged[i], b)
            new_state_outputs[r] = tuple(merged)

    return StateTable(
        states            = new_states_ordered,
        input_names       = stt.input_names,
        output_names      = stt.output_names,
        transitions       = new_transitions,
        model             = stt.model,
        reset_state       = rep[stt.reset_state],
        state_outputs     = new_state_outputs,
        reset_input_name  = stt.reset_input_name,
        reset_polarity    = stt.reset_polarity,
    )


def _remove_unreachable(stt: StateTable) -> StateTable:
    """
    Drop states not reachable from the reset state.  This is a cheap
    structural pass orthogonal to equivalence-class merging — running it
    before minimization keeps the partition-refinement input small and
    the IS-FSM compatibility search tractable.
    """
    delta, _ = _expand_stt(stt)
    n_patterns = 1 << stt.n_input_bits

    reach: Set[str] = {stt.reset_state}
    stack = [stt.reset_state]
    while stack:
        s = stack.pop()
        for pat in range(n_patterns):
            nxt = delta[(s, pat)]
            if nxt is not None and nxt not in reach:
                reach.add(nxt)
                stack.append(nxt)

    if len(reach) == len(stt.states):
        return stt

    new_states = [s for s in stt.states if s in reach]
    new_trans  = [t for t in stt.transitions
                  if t.src in reach and (t.dst is None or t.dst in reach)]
    new_state_outputs = {s: v for s, v in stt.state_outputs.items() if s in reach}

    return StateTable(
        states            = new_states,
        input_names       = stt.input_names,
        output_names      = stt.output_names,
        transitions       = new_trans,
        model             = stt.model,
        reset_state       = stt.reset_state,
        state_outputs     = new_state_outputs,
        reset_input_name  = stt.reset_input_name,
        reset_polarity    = stt.reset_polarity,
    )


def minimize_states(stt: StateTable) -> StateTable:
    """
    Reduce an FSM to an equivalent machine with no redundant states.

    Completely-specified inputs → O(n log n) Hopcroft partition refinement.
    Incompletely-specified      → greedy maximal-compatible cover
                                  (STAMINA-like heuristic; NP-hard in general).

    Unreachable states are always dropped first; equivalence-class merging
    then runs on the reachable core.
    """
    stt = _remove_unreachable(stt)
    if stt.is_completely_specified():
        return _minimize_completely_specified(stt)
    return _minimize_incompletely_specified(stt)


# ═══════════════════════════════════════════════════════════════════════════════
#  State encoding
# ═══════════════════════════════════════════════════════════════════════════════

def _bits_for(n: int) -> int:
    if n <= 1:
        return 1
    w = 0
    m = n - 1
    while m > 0:
        w += 1
        m >>= 1
    return w


def _gray_code(i: int, w: int) -> Tuple[int, ...]:
    g = i ^ (i >> 1)
    return tuple((g >> (w - 1 - k)) & 1 for k in range(w))


def encode_states(stt: StateTable, strategy: str = 'binary'
                  ) -> Dict[str, Tuple[int, ...]]:
    """
    Assign a bit vector to each abstract state name.

    strategy ∈ {'binary', 'onehot', 'gray'}:
      • binary : minimum-width dense encoding 0..n-1 (MSB-first tuple)
      • onehot : width = n_states; state i → bit i set, all others 0
      • gray   : minimum-width Gray code (adjacent states differ by 1 bit)

    The reset state always gets the all-zero code for 'binary' and 'gray',
    and the first one-hot bit for 'onehot', so hardware power-on (all flip
    flops at 0) lands the FSM in the reset state without extra logic.
    """
    n = stt.n_states
    states = list(stt.states)
    # Move reset state to index 0 for encoding so the all-zero vector
    # represents the reset state under binary/gray.
    if states[0] != stt.reset_state:
        states.remove(stt.reset_state)
        states.insert(0, stt.reset_state)

    if strategy == 'binary':
        w = _bits_for(n)
        return {s: _pattern_bits(i, w) for i, s in enumerate(states)}

    if strategy == 'gray':
        w = _bits_for(n)
        return {s: _gray_code(i, w) for i, s in enumerate(states)}

    if strategy == 'onehot':
        w = max(n, 1)
        enc: Dict[str, Tuple[int, ...]] = {}
        for i, s in enumerate(states):
            bits = [0] * w
            bits[i] = 1
            enc[s] = tuple(bits)
        return enc

    raise ValueError(f"unknown encoding strategy {strategy!r}; "
                     "expected 'binary', 'onehot', or 'gray'")


# ═══════════════════════════════════════════════════════════════════════════════
#  Excitation-function truth table
# ═══════════════════════════════════════════════════════════════════════════════

def fsm_to_truth_table(
    stt:        StateTable,
    encoding:   Dict[str, Tuple[int, ...]],
    state_bit_prefix: str = 'Q',
    next_bit_prefix:  str = 'D',
    excitation:       str  = 'd',
) -> Tuple[TruthTable, List[str]]:
    """
    Project an FSM onto a single combinational TruthTable.

    Returns (truth_table, fsm_output_names).

    Truth table inputs:
      state_bits (MSB first) ++ fsm_inputs

    Truth table outputs (excitation='d', default):
      next_state_bits (MSB first) ++ fsm_outputs

    Truth table outputs (excitation='jk'):
      J_0..J_{w-1} ++ K_0..K_{w-1} ++ fsm_outputs

    For JK excitation the inverse excitation table of a JK flip-flop is:

        Q(t)  Q(t+1)   J   K
        ──────────────────────
         0      0      0   DC
         0      1      1   DC
         1      0      DC  1
         1      1      DC  0

    i.e. J_i is a don't-care whenever the current state bit Q_i = 1, and
    K_i is a don't-care whenever Q_i = 0.  We resolve those per-bit
    DCs with the "T-fill" convention that makes J_i = K_i = T_i where
    T_i = Q_i XOR Q_i(t+1) is the toggle signal: the single simplest
    concrete choice that (a) is always correct under the JK recurrence,
    and (b) collapses to the classic counter solution J=K=1 / J=K=Q0 on
    binary up/down counters, which is where JK flip-flops win biggest
    over D-flops.  Because J_i and K_i share the same function, AIG
    structural hashing merges them automatically — the extra outputs
    cost nothing in gates.

    Input patterns corresponding to unused state encodings (e.g. unused
    rows in a 3-state / 2-bit binary encoding) are emitted as don't-cares
    so downstream optimization can freely reuse those minterms.  Transitions
    whose next state is None or whose output contains DASH are likewise
    emitted with don't-care values on those bits.
    """
    if excitation not in ('d', 'jk'):
        raise ValueError(
            f"excitation must be 'd' or 'jk', got {excitation!r}")
    w_state  = len(next(iter(encoding.values())))
    n_in_bit = stt.n_input_bits
    n_out    = stt.n_output_bits
    # JK doubles the state-excitation output count (J_i + K_i per state bit).
    w_excit  = w_state if excitation == 'd' else 2 * w_state
    n_tt_in  = w_state + n_in_bit
    n_tt_out = w_excit + n_out

    state_bit_names = [f'{state_bit_prefix}{i}' for i in range(w_state)]
    if excitation == 'd':
        excit_names = [f'{next_bit_prefix}{i}' for i in range(w_state)]
    else:
        excit_names = ([f'J{i}' for i in range(w_state)] +
                       [f'K{i}' for i in range(w_state)])
    input_names     = state_bit_names + list(stt.input_names)
    output_names    = excit_names     + list(stt.output_names)

    # Expand STT to concrete (state, input-pattern) cells
    delta, lam = _expand_stt(stt)

    # Build per-minterm rows (always small enough: w_state + n_in_bit ≤ 20 typically)
    rows:       Dict[int, Tuple[int, ...]] = {}
    dont_cares: Set[int]                   = set()

    used_state_codes: Set[Tuple[int, ...]] = set(encoding.values())
    code_to_state: Dict[Tuple[int, ...], str] = {v: k for k, v in encoding.items()}

    def _excit_bits(
        s_bits:  Tuple[int, ...],
        ns_bits: Tuple[int, ...],
    ) -> Tuple[int, ...]:
        if excitation == 'd':
            return tuple(ns_bits)
        # jk: T-fill — J_i = K_i = T_i = Q_i XOR next_Q_i
        t = tuple(
            DASH if ns_bits[i] == DASH else (s_bits[i] ^ ns_bits[i])
            for i in range(w_state)
        )
        return t + t

    # Enumerate every (state_code, input_pattern) pair
    for scode in range(1 << w_state):
        s_bits = _pattern_bits(scode, w_state)
        is_used = s_bits in used_state_codes

        for ipat in range(1 << n_in_bit):
            key_m = (scode << n_in_bit) | ipat

            if not is_used:
                # Unused encoding — everything is a don't care
                dont_cares.add(key_m)
                continue

            src = code_to_state[s_bits]
            dst = delta[(src, ipat)]
            out = lam[(src, ipat)]

            # Next-state bits (DC row if dst is None)
            if dst is None:
                next_bits = tuple([DASH] * w_state)
            else:
                next_bits = encoding[dst]

            row = _excit_bits(s_bits, next_bits) + tuple(out)

            # If every bit of this row is DC, mark the whole minterm DC
            if all(b == DASH for b in row):
                dont_cares.add(key_m)
                continue

            # Replace DASH with 0 in the stored row and track a *bitwise*
            # don't-care mask.  The existing TruthTable API keeps per-minterm
            # DC only (no per-bit DC), so we widen to full DC only if every
            # bit is DC.  Per-bit DC is implicitly recovered downstream by
            # multi-output Espresso, which processes each output
            # independently: we split the DC set on a per-output basis in
            # _build_per_output_cube_cover below.
            concrete = tuple(0 if b == DASH else b for b in row)
            rows[key_m] = concrete

    # ── build per-output cube cover (this is where we inject per-bit DCs) ──
    # We take the dict-based rows but construct the cube_cover manually so
    # that each output bit can have its own DC minterms.
    per_output_dc: List[Set[int]] = [set(dont_cares) for _ in range(n_tt_out)]
    per_output_on: List[Set[int]] = [set() for _ in range(n_tt_out)]

    # Re-scan: for each used (scode, ipat) that isn't fully DC, classify
    # each output bit independently (DASH → DC for that bit).
    for scode in range(1 << w_state):
        s_bits = _pattern_bits(scode, w_state)
        if s_bits not in used_state_codes:
            continue
        src = code_to_state[s_bits]
        for ipat in range(1 << n_in_bit):
            key_m = (scode << n_in_bit) | ipat
            if key_m in dont_cares:
                continue
            dst = delta[(src, ipat)]
            out = lam[(src, ipat)]
            # Next-state bits
            if dst is None:
                ns_bits = tuple([DASH] * w_state)
            else:
                ns_bits = encoding[dst]
            combined = _excit_bits(s_bits, ns_bits) + tuple(out)
            for j, bit in enumerate(combined):
                if bit == DASH:
                    per_output_dc[j].add(key_m)
                elif bit == 1:
                    per_output_on[j].add(key_m)

    # Compose final (row_dict, dc_set) that downstream TruthTable.from_dict
    # accepts: a minterm is globally DC only if every output bit is DC there.
    global_dc: Set[int] = set(dont_cares)
    all_minterms = 1 << n_tt_in
    for m in range(all_minterms):
        if m in global_dc:
            continue
        if all(m in per_output_dc[j] for j in range(n_tt_out)):
            global_dc.add(m)

    final_rows: Dict[int, Tuple[int, ...]] = {}
    for m in range(all_minterms):
        if m in global_dc:
            continue
        final_rows[m] = tuple(
            1 if m in per_output_on[j] else 0
            for j in range(n_tt_out)
        )

    tt = TruthTable.from_dict(
        n_inputs     = n_tt_in,
        input_names  = input_names,
        output_names = output_names,
        rows         = final_rows,
        dont_cares   = global_dc,
    )
    return tt, state_bit_names


# ═══════════════════════════════════════════════════════════════════════════════
#  Full FSM synthesis
# ═══════════════════════════════════════════════════════════════════════════════

class FSMResult:
    """
    Container returned by synthesize_fsm().

    Attributes
    ----------
    stt              : the (possibly minimized) StateTable used for synthesis.
    encoding         : state name → bit vector (MSB first).
    state_bit_names  : list of flip-flop Q wire names (e.g. ['Q0', 'Q1']).
    next_bit_names   : for D excitation, list of D wire names (['D0', 'D1']).
                       For JK excitation this is empty (the combinational
                       cone emits J/K instead — see j_bit_names, k_bit_names).
    j_bit_names      : JK excitation J wire names (e.g. ['J0', 'J1']); empty
                       when excitation='d'.
    k_bit_names      : JK excitation K wire names; empty when excitation='d'.
    excitation       : 'd' | 'jk' — which flip-flop primitive the
                       combinational cone is driving.
    fsm_output_names : names of the combinational FSM outputs (non-state).
    reset_code       : bit tuple loaded on power-up.
    truth_table      : the combinational projection.
    opt_result       : OptimizeResult from optimize(truth_table).
    encoding_strategy: 'binary' | 'onehot' | 'gray'.
    """
    def __init__(self):
        self.stt:              Optional[StateTable]          = None
        self.encoding:         Dict[str, Tuple[int, ...]]    = {}
        self.encoding_strategy: str                          = ''
        self.excitation:       str                           = 'd'
        self.state_bit_names:  List[str]                     = []
        self.next_bit_names:   List[str]                     = []
        self.j_bit_names:      List[str]                     = []
        self.k_bit_names:      List[str]                     = []
        self.fsm_output_names: List[str]                     = []
        self.reset_code:       Tuple[int, ...]               = ()
        self.reset_input_name: Optional[str]                 = None
        self.reset_polarity:   str                           = 'sync'
        self.truth_table:      Optional[TruthTable]          = None
        self.opt_result:       Optional[OptimizeResult]      = None

    @property
    def n_nand(self) -> int:
        return self.opt_result.total_nand if self.opt_result else 0

    @property
    def n_flip_flops(self) -> int:
        return len(self.state_bit_names)


def synthesize_fsm(
    stt:        StateTable,
    encoding:   str           = 'binary',
    minimize:   bool          = True,
    verbose:    bool          = True,
    script:     Optional[str] = None,
    excitation: str           = 'd',
) -> FSMResult:
    """
    Full FSM → NAND synthesis.

      1. (optional) Hopcroft state minimization.
      2. State encoding per *encoding* strategy.
      3. Project to combinational TruthTable.
      4. optimize() — existing combinational pipeline.
      5. Wrap in FSMResult; caller pipes flip-flops in externally
         (e.g. via export_fsm_circ).

    Parameters
    ----------
    excitation : 'd' | 'jk'
        Target flip-flop primitive.  'd' (default) emits one D_i next-state
        bit per state bit.  'jk' emits (J_i, K_i) pairs using the T-fill
        concretion (J_i = K_i = Q_i XOR next_Q_i); structural hashing then
        merges J and K into a single shared cone.  JK typically wins on
        counters and other toggle-heavy FSMs, and loses on shift registers.
    """
    if excitation not in ('d', 'jk'):
        raise ValueError(
            f"excitation must be 'd' or 'jk', got {excitation!r}")

    from ..pipeline import optimize

    if verbose:
        print(f"\n  FSM synthesis: {stt}")
        print(f"    reset     : {stt.reset_state}")
        print(f"    encoding  : {encoding}")
        print(f"    excitation: {excitation}")
        print(f"    minimize  : {minimize}")

    if minimize:
        orig_n = stt.n_states
        stt    = minimize_states(stt)
        if verbose:
            print(f"    state min : {orig_n} -> {stt.n_states} state(s)")

    enc = encode_states(stt, encoding)

    tt, state_bit_names = fsm_to_truth_table(stt, enc, excitation=excitation)
    w_state = len(state_bit_names)

    if verbose:
        print(f"    enc width : {w_state} bit(s); "
              f"combinational TT: {tt.n_inputs} in, {tt.n_outputs} out")

    opt = optimize(tt, verbose=verbose, script=script)

    res = FSMResult()
    res.stt               = stt
    res.encoding          = enc
    res.encoding_strategy = encoding
    res.excitation        = excitation
    res.state_bit_names   = state_bit_names
    if excitation == 'd':
        res.next_bit_names = [f'D{i}' for i in range(w_state)]
        res.j_bit_names    = []
        res.k_bit_names    = []
    else:
        res.next_bit_names = []
        res.j_bit_names    = [f'J{i}' for i in range(w_state)]
        res.k_bit_names    = [f'K{i}' for i in range(w_state)]
    res.fsm_output_names  = list(stt.output_names)
    res.reset_code        = enc[stt.reset_state]
    res.reset_input_name  = stt.reset_input_name
    res.reset_polarity    = stt.reset_polarity
    res.truth_table       = tt
    res.opt_result        = opt
    return res


# ═══════════════════════════════════════════════════════════════════════════════
#  Cycle-stepping simulator — cuts feedback loops through FFs
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_fsm(
    fsm_result: FSMResult,
    input_seq:  List[Tuple[int, ...]],
    reset_seq:  Optional[List[int]] = None,
) -> List[Tuple[str, Tuple[int, ...], Tuple[int, ...]]]:
    """
    Clock the synthesized FSM through *input_seq* and return a per-cycle trace:

        [ (state_name, current_state_bits, fsm_outputs), ... ]

    The combinational NAND network is evaluated each cycle with the current
    state bits + current inputs.  The feedback edge from next-state outputs
    to the state-bit inputs is closed *here*, not inside the combinational
    graph — so topological evaluation, verification, and every AIG pass
    never see a cycle.

    reset_seq : optional list of 0/1 values parallel to ``input_seq``.  Only
    consulted when ``fsm_result.reset_polarity`` is ``'async_low'`` or
    ``'async_high'``.  On any cycle where the reset level is active, the
    state bits are forced to ``fsm_result.reset_code`` *before* the
    combinational cone is evaluated, the clock edge that would advance
    state is suppressed, and the next cycle still starts from the reset
    code — matching the datasheet semantics of a real asynchronous CLR
    input (level-sensitive override of CLK).
    """
    from ..mapping.nand import eval_network

    stt   = fsm_result.stt
    enc   = fsm_result.encoding
    gates = fsm_result.opt_result.builder.gates
    w     = len(fsm_result.state_bit_names)

    code_to_state: Dict[Tuple[int, ...], str] = {v: k for k, v in enc.items()}
    state_bits = fsm_result.reset_code
    state_bit_names = fsm_result.state_bit_names
    fsm_input_names = stt.input_names

    # Per-output evaluator: for each wire, simulate just that wire.
    # We reuse eval_network by temporarily swapping the trailing OUTPUT gate.
    if fsm_result.excitation == 'd':
        excit_out_names = list(fsm_result.next_bit_names)
    else:
        excit_out_names = (list(fsm_result.j_bit_names)
                           + list(fsm_result.k_bit_names))
    out_wires: List[str] = []
    for name in excit_out_names + fsm_result.fsm_output_names:
        r = fsm_result.opt_result.outputs.get(name)
        out_wires.append(r.out_wire if r is not None else '')

    def eval_one(wire: str, asgn: Dict[str, int]) -> int:
        # Strip trailing OUTPUT gates, append our own pointing at *wire*
        core = [g for g in gates if g[1] != 'OUTPUT']
        core.append((wire, 'OUTPUT', [wire]))
        return eval_network(core, asgn)

    async_active_level: Optional[int] = None
    if fsm_result.reset_polarity == 'async_low':
        async_active_level = 0
    elif fsm_result.reset_polarity == 'async_high':
        async_active_level = 1
    if reset_seq is not None and async_active_level is None:
        raise ValueError(
            "reset_seq was supplied but fsm_result.reset_polarity is "
            f"{fsm_result.reset_polarity!r}; expected 'async_low' or "
            "'async_high'")
    if reset_seq is not None and len(reset_seq) != len(input_seq):
        raise ValueError(
            f"reset_seq length {len(reset_seq)} != input_seq length "
            f"{len(input_seq)}")

    trace: List[Tuple[str, Tuple[int, ...], Tuple[int, ...]]] = []
    for t_idx, inputs in enumerate(input_seq):
        if len(inputs) != len(fsm_input_names):
            raise ValueError(
                f"input vector length {len(inputs)} != {len(fsm_input_names)}")

        reset_active = (async_active_level is not None
                        and reset_seq is not None
                        and reset_seq[t_idx] == async_active_level)
        if reset_active:
            state_bits = fsm_result.reset_code

        asgn: Dict[str, int] = {}
        for i, n in enumerate(state_bit_names):
            asgn[n] = state_bits[i]
        for i, n in enumerate(fsm_input_names):
            asgn[n] = inputs[i]

        # Evaluate all outputs at once by running the full gate list
        wire_vals: Dict[str, int] = dict(asgn)
        for name, gtype, ins in gates:
            if gtype == 'NAND':
                wire_vals[name] = 1 - int(all(
                    wire_vals.get(i, 0) == 1 for i in ins))
            elif gtype == 'OUTPUT':
                wire_vals[name] = wire_vals.get(ins[0], 0)
            elif gtype == 'ZERO':
                wire_vals[name] = 0
            elif gtype == 'ONE':
                wire_vals[name] = 1

        if fsm_result.excitation == 'd':
            next_state_bits = tuple(
                wire_vals.get(out_wires[i], 0) for i in range(w))
            fsm_out_offset = w
        else:
            # JK recurrence: Q(t+1) = J·~Q + ~K·Q
            j_vals = [wire_vals.get(out_wires[i],       0) for i in range(w)]
            k_vals = [wire_vals.get(out_wires[w + i],   0) for i in range(w)]
            next_state_bits = tuple(
                (j_vals[i] & (1 - state_bits[i])) |
                ((1 - k_vals[i]) & state_bits[i])
                for i in range(w)
            )
            fsm_out_offset = 2 * w
        fsm_outs = tuple(
            wire_vals.get(out_wires[fsm_out_offset + j], 0)
            for j in range(len(fsm_result.fsm_output_names)))

        state_name = code_to_state.get(state_bits, '?')
        trace.append((state_name, state_bits, fsm_outs))
        # Async CLR is level-sensitive: while asserted, clock edges are
        # ignored and the flip-flops stay pinned at reset_code.
        state_bits = fsm_result.reset_code if reset_active else next_state_bits

    return trace


# ═══════════════════════════════════════════════════════════════════════════════
#  KISS2 parser  — de-facto standard FSM description format
# ═══════════════════════════════════════════════════════════════════════════════

def parse_kiss(text: str) -> StateTable:
    """
    Parse a KISS2 (Berkeley .kiss2) finite-state-machine description.

    Directives honoured:
      .i  N        number of input bits
      .o  M        number of output bits
      .s  K        number of states (informational, ignored — we derive it)
      .p  P        number of product terms (informational)
      .r  name     reset state (default: first state encountered)
      .ilb a b ..  input bit names
      .ob  y z ..  output bit names
      .e / .end    end marker

    Each data line:    <input_cube>  <src_state>  <dst_state>  <output_cube>

    For Moore FSMs written in Mealy syntax (same output for every
    (state, input) pair originating from a given src state), callers
    should convert manually.  This parser treats all KISS2 files as Mealy.
    """
    in_names:  Optional[List[str]] = None
    out_names: Optional[List[str]] = None
    reset:     Optional[str]       = None
    n_in, n_out = 0, 0

    raw_rows: List[Tuple[str, str, str, str]] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        tok = line.split()
        head = tok[0].lower()

        if head == '.i':        n_in  = int(tok[1])
        elif head == '.o':      n_out = int(tok[1])
        elif head == '.s':      pass
        elif head == '.p':      pass
        elif head == '.r':      reset = tok[1]
        elif head == '.ilb':    in_names  = tok[1:]
        elif head == '.ob':     out_names = tok[1:]
        elif head in ('.e', '.end'): break
        elif not line.startswith('.'):
            if len(tok) < 4:
                continue
            raw_rows.append((tok[0], tok[1], tok[2], tok[3]))

    if in_names  is None: in_names  = [f'x{i}' for i in range(n_in)]
    if out_names is None: out_names = [f'y{i}' for i in range(n_out)]

    def parse_in(pat: str) -> Tuple[int, ...]:
        return tuple(0 if ch == '0' else 1 if ch == '1' else DASH for ch in pat)

    def parse_out(pat: str) -> Tuple[int, ...]:
        return tuple(0 if ch == '0' else 1 if ch == '1' else DASH for ch in pat)

    states_seen: List[str] = []
    seen_set: Set[str] = set()
    transitions: List[Transition] = []
    for inp, src, dst, out in raw_rows:
        for s in (src, dst):
            if s not in seen_set and s != '*':
                seen_set.add(s); states_seen.append(s)
        t_dst: Optional[str] = None if dst == '*' else dst
        transitions.append(Transition(
            src = src,
            inp = parse_in(inp),
            dst = t_dst,
            out = parse_out(out),
        ))

    if reset is None:
        reset = states_seen[0] if states_seen else 'S0'
    if reset not in seen_set:
        states_seen.insert(0, reset); seen_set.add(reset)

    return StateTable(
        states       = states_seen,
        input_names  = in_names,
        output_names = out_names,
        transitions  = transitions,
        model        = 'mealy',
        reset_state  = reset,
    )
