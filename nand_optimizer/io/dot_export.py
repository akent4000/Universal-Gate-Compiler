"""
Graphviz DOT export for And-Inverter Graph (AIG) structures.

Usage:
    dot_str = aig_to_dot(aig, output_lits, output_names, title="MyCircuit")
    with open("circuit.dot", "w") as f:
        f.write(dot_str)
    # Then: dot -Tpng circuit.dot -o circuit.png
    #       dot -Tsvg circuit.dot -o circuit.svg
"""

from __future__ import annotations
import re
from typing import List, Optional

from ..core.aig import AIG, Lit


def _esc(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _safe_id(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def aig_to_dot(
    aig: AIG,
    output_lits: Optional[List[Lit]] = None,
    output_names: Optional[List[str]] = None,
    title: str = "",
) -> str:
    """
    Render an AIG as a Graphviz DOT string.

    Complement edges are drawn dashed/red with an open-circle arrowhead (odot).
    Normal edges are solid black.  Primary inputs appear at the top (rank=source);
    output nodes at the bottom (rank=sink).

    Args:
        aig:          The AIG to visualize.
        output_lits:  Output literals (node_id*2 + complement_bit).
        output_names: Parallel list of names for the output nodes.
        title:        Optional graph title rendered above the diagram.
    """
    lines = ["digraph AIG {"]
    lines.append("  rankdir=TB;")
    lines.append('  node [fontname="Courier", fontsize=10];')
    lines.append("  edge [fontsize=9];")
    if title:
        lines.append(f"  label={_esc(title)};")
        lines.append("  labelloc=t;")
    lines.append("")

    # Only draw constant-0 node if actually referenced
    const_used = any(
        AIG.node_of(a) == 0 or AIG.node_of(b) == 0
        for entry in aig._nodes
        if entry[0] in ("and", "xor")
        for _, a, b in [entry]
    )
    if output_lits:
        const_used = const_used or any(AIG.node_of(l) == 0 for l in output_lits)

    if const_used:
        lines.append("  // Constant FALSE")
        lines.append('  n0 [label="0", shape=point, width=0.25, style=filled, fillcolor=black];')
        lines.append("")

    # Primary inputs
    input_ids = []
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == "input":
            _, name = entry
            input_ids.append(nid)
            lines.append(
                f'  n{nid} [label={_esc(name)}, shape=invtriangle, '
                f'style=filled, fillcolor="#AED6F1"];'
            )

    if input_ids:
        lines.append(
            "  { rank=source; " + "; ".join(f"n{nid}" for nid in input_ids) + "; }"
        )
        lines.append("")

    # AND and XOR nodes
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == "and":
            lines.append(
                f'  n{nid} [label="& [{nid}]", shape=ellipse, '
                f'style=filled, fillcolor="#F9E79F"];'
            )
        elif entry[0] == "xor":
            lines.append(
                f'  n{nid} [label="⊕ [{nid}]", shape=diamond, '
                f'style=filled, fillcolor="#FAD7A0"];'
            )

    lines.append("")
    lines.append("  // Gate edges  (red dashed = complemented input)")
    for i, entry in enumerate(aig._nodes):
        if entry[0] in ("and", "xor"):
            _, a, b = entry
            nid = i + 1
            for child_lit in (a, b):
                src  = AIG.node_of(child_lit)
                comp = AIG.is_complemented(child_lit)
                edge = f"  n{src} -> n{nid}"
                if comp:
                    edge += ' [color=red, style=dashed, arrowhead=odot]'
                lines.append(edge + ";")

    # Output nodes
    if output_lits:
        names = output_names or [f"OUT{i}" for i in range(len(output_lits))]
        lines.append("")
        lines.append("  // Circuit outputs")
        out_ids: List[str] = []
        for name, lit in zip(names, output_lits):
            oid = f"out_{_safe_id(name)}"
            out_ids.append(oid)
            lines.append(
                f'  {oid} [label={_esc(name)}, shape=rectangle, '
                f'style=filled, fillcolor="#A9DFBF"];'
            )
            src  = AIG.node_of(lit)
            comp = AIG.is_complemented(lit)
            edge = f"  n{src} -> {oid}"
            if comp:
                edge += ' [color=red, style=dashed, arrowhead=odot]'
            lines.append(edge + ";")
        lines.append("  { rank=sink; " + "; ".join(out_ids) + "; }")

    lines.append("}")
    return "\n".join(lines)
