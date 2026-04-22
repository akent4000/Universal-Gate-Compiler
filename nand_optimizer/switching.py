"""
Switching Activity Estimation for And-Inverter Graphs.

Propagates signal probabilities from primary inputs forward through the AIG
(independence approximation) and computes the switching activity of every node.

  P(AND(a, b)) ≈ P(a) · P(b)          (inputs treated as uncorrelated)
  sw(x) = P(x=1) · P(x=0)             (peaks at 0.25 when P = 0.5)

This is the foundation for power-aware synthesis: high-activity nodes are
candidates for glitch reduction, buffer insertion, or AND-tree restructuring.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .aig import AIG, FALSE, TRUE


@dataclass
class SwitchingActivity:
    """
    Per-node switching activity for an AIG.

    signal_prob[nid]  — P(node outputs 1), for every node including inputs.
    switch_prob[nid]  — p·(1−p), AND nodes only (inputs excluded).
    input_prob[name]  — P(input=1) keyed by input name (convenience).
    """
    signal_prob:  Dict[int, float]
    switch_prob:  Dict[int, float]   # AND nodes only
    out_lits:     List[int]
    output_names: List[str]
    input_names:  List[str]
    input_prob:   Dict[str, float]

    # ── literal helpers ──────────────────────────────────────────────────────

    def prob_of_lit(self, lit: int) -> float:
        """P(lit = 1), respecting the complement bit."""
        nid = AIG.node_of(lit)
        if nid == 0:
            return 0.0 if lit == FALSE else 1.0
        p = self.signal_prob[nid]
        return (1.0 - p) if AIG.is_complemented(lit) else p

    # ── output-level views ───────────────────────────────────────────────────

    @property
    def output_probs(self) -> Dict[str, float]:
        """P(output = 1) for each primary output."""
        return {name: self.prob_of_lit(lit)
                for name, lit in zip(self.output_names, self.out_lits)}

    @property
    def output_switch(self) -> Dict[str, float]:
        """Switching activity sw = p·(1−p) for each primary output."""
        return {name: p * (1.0 - p) for name, p in self.output_probs.items()}

    # ── aggregate ────────────────────────────────────────────────────────────

    @property
    def total_activity(self) -> float:
        """Sum of sw over all AND nodes (theoretical max = 0.25 · n_ands)."""
        return sum(self.switch_prob.values())

    def top_nodes(self, n: int = 10) -> List[Tuple[int, float]]:
        """Return the *n* most active AND nodes as (node_id, sw) pairs."""
        return sorted(self.switch_prob.items(), key=lambda kv: -kv[1])[:n]

    # ── reporting ────────────────────────────────────────────────────────────

    def report(self) -> str:
        lines = [
            "  Switching Activity  (independence approx.)",
            f"  {'Signal':<18} {'P(1)':>8}  {'sw=p(1-p)':>10}",
            "  " + "-" * 42,
        ]
        for name, p in self.input_prob.items():
            lines.append(f"  {'IN  ' + name:<18} {p:>8.4f}  {p*(1-p):>10.4f}")
        for name, p in self.output_probs.items():
            lines.append(f"  {'OUT ' + name:<18} {p:>8.4f}  {p*(1-p):>10.4f}")
        lines += [
            "  " + "-" * 42,
            f"  {'Total AND-node activity':<29} {self.total_activity:>10.4f}",
        ]
        return "\n".join(lines)


def estimate_switching(
    aig: AIG,
    out_lits: Optional[List[int]] = None,
    input_probs: Optional[Dict[str, float]] = None,
    output_names: Optional[List[str]] = None,
) -> SwitchingActivity:
    """
    Estimate switching activity for every node in *aig*.

    Parameters
    ----------
    aig : AIG
        The And-Inverter Graph to analyse.
    out_lits : list of int, optional
        Output literals.  Used only to populate ``SwitchingActivity.out_lits``.
    input_probs : dict, optional
        ``{input_name: probability}`` overrides.  Defaults to 0.5 (max entropy)
        for any input not listed.
    output_names : list of str, optional
        Names for each output literal.  Defaults to ``['o0', 'o1', ...]``.

    Returns
    -------
    SwitchingActivity
    """
    if input_probs is None:
        input_probs = {}
    if out_lits is None:
        out_lits = []
    if output_names is None:
        output_names = [f"o{i}" for i in range(len(out_lits))]

    signal_prob: Dict[int, float] = {}
    switch_prob: Dict[int, float] = {}
    input_prob_named: Dict[str, float] = {}
    input_names_ordered: List[str] = []

    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            name = entry[1]
            input_names_ordered.append(name)
            p = float(input_probs.get(name, 0.5))
            signal_prob[nid] = p
            input_prob_named[name] = p
        else:
            _, la, lb = entry
            na, nb = AIG.node_of(la), AIG.node_of(lb)
            pa = (0.0 if la == FALSE else 1.0) if na == 0 else signal_prob[na]
            if AIG.is_complemented(la):
                pa = 1.0 - pa
            pb = (0.0 if lb == FALSE else 1.0) if nb == 0 else signal_prob[nb]
            if AIG.is_complemented(lb):
                pb = 1.0 - pb
            p = pa * pb
            signal_prob[nid] = p
            switch_prob[nid] = p * (1.0 - p)

    return SwitchingActivity(
        signal_prob=signal_prob,
        switch_prob=switch_prob,
        out_lits=list(out_lits),
        output_names=list(output_names),
        input_names=input_names_ordered,
        input_prob=input_prob_named,
    )
