"""
BLIF I/O — Berkeley Logic Interchange Format for combinational networks.

BLIF is the canonical text interchange used by SIS, ABC, Yosys
`write_blif`, VTR, and the MCNC/ISCAS benchmark archives. Spec:
  https://www.cs.upc.edu/~jordicf/gavina/BIB/files/blifMv.pdf  (§2: BLIF core)

This module reads and writes the classic (non-MV) combinational subset:
  .model <name>
  .inputs <...>
  .outputs <...>
  .names <inputs...> <output>
    <cube> <value>
    ...
  .end

Latches (`.latch`) and sub-circuits (`.subckt`) are unsupported.

Writer strategy (`write_blif`):
  Every AIG node gets a wire `n<id>`. Each AND node emits a single
  `.names` with exactly two fanins and the cube that encodes the
  required polarity of each fanin. Outputs are emitted as 1-input
  `.names` aliases (buffers or inverters as needed). Constant-0 and
  constant-1 outputs are emitted as 0-input `.names`.

Reader strategy (`read_blif`):
  Each `.names` is interpreted as a sum-of-products over its fanins
  and converted into the growing AIG via AIG.make_or / make_and.
  Off-set (`<cube> 0`) cubes are supported uniformly: the result is
  complemented when the cover is declared as the off-set.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Sequence, Tuple

from .aig import AIG, Lit, FALSE, TRUE


# ═══════════════════════════════════════════════════════════════════════════════
#  Writer
# ═══════════════════════════════════════════════════════════════════════════════

def write_blif(
    aig:          AIG,
    out_lits:     Sequence[Lit],
    path:         str,
    model_name:   str = 'circuit',
    input_names:  Optional[Sequence[str]] = None,
    output_names: Optional[Sequence[str]] = None,
) -> None:
    """Serialise an AIG to a BLIF file.

    Each AND node is rendered as a 2-input `.names` with the cube adjusted
    to absorb fanin complementation (e.g. `AND(~a, b)` becomes the cube
    ``01 1``). Output aliases are 1-input buffers / inverters.
    """
    # ── gather inputs + and-nodes in topological order ──────────────────────
    in_nids:  List[int] = []
    and_list: List[Tuple[int, int, int]] = []
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            in_nids.append(nid)
        else:
            and_list.append((nid, entry[1], entry[2]))

    # Resolve user-provided names (or fall back to those registered in aig).
    in_syms = list(input_names) if input_names is not None else [
        aig._nodes[nid - 1][1] for nid in in_nids
    ]
    if len(in_syms) != len(in_nids):
        raise ValueError(f"input_names length {len(in_syms)} != {len(in_nids)}")

    O = len(out_lits)
    out_syms = list(output_names) if output_names is not None else [
        f'o{i}' for i in range(O)
    ]
    if len(out_syms) != O:
        raise ValueError(f"output_names length {len(out_syms)} != O={O}")

    # Net name for each AIG node. Primary inputs use their symbolic names.
    net_of: Dict[int, str] = {nid: sym for nid, sym in zip(in_nids, in_syms)}
    for nid, _, _ in and_list:
        net_of[nid] = f'n{nid}'

    def net_for_lit(lit: Lit) -> Tuple[Optional[str], int]:
        """Return (underlying_net, complement_bit). Const has net=None."""
        nid = lit >> 1
        comp = lit & 1
        if nid == 0:
            return None, comp
        return net_of[nid], comp

    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'.model {model_name}\n')
        f.write('.inputs ' + ' '.join(in_syms) + '\n')
        f.write('.outputs ' + ' '.join(out_syms) + '\n')

        for nid, a_lit, b_lit in and_list:
            a_net, a_c = net_for_lit(a_lit)
            b_net, b_c = net_for_lit(b_lit)
            out_net = net_of[nid]

            # Both fanins non-constant: standard 2-input AND with adjusted cube.
            if a_net is not None and b_net is not None:
                f.write(f'.names {a_net} {b_net} {out_net}\n')
                ca = '0' if a_c else '1'
                cb = '0' if b_c else '1'
                f.write(f'{ca}{cb} 1\n')
                continue

            # One fanin constant: make_and constant-propagates, so this only
            # happens for degenerate AIGs. Handle defensively.
            if a_net is None and b_net is None:
                val = (1 - a_c) & (1 - b_c)
                _write_constant(f, out_net, val)
                continue
            only_net, only_c = (b_net, b_c) if a_net is None else (a_net, a_c)
            const_val = 1 - (a_c if a_net is None else b_c)
            if const_val == 0:
                _write_constant(f, out_net, 0)
            else:
                f.write(f'.names {only_net} {out_net}\n')
                f.write(('0 1\n' if only_c else '1 1\n'))

        # Output aliases.
        for sym, olit in zip(out_syms, out_lits):
            src_net, src_c = net_for_lit(olit)
            if src_net is None:
                _write_constant(f, sym, 1 - src_c)
                continue
            if src_net == sym and not src_c:
                # A primary output driven by a primary input with the same name
                # and no inversion: emit a buffer alias, otherwise netname
                # collisions confuse downstream BLIF readers.
                f.write(f'.names {src_net} {sym}\n1 1\n')
                continue
            f.write(f'.names {src_net} {sym}\n')
            f.write('0 1\n' if src_c else '1 1\n')

        f.write('.end\n')


def _write_constant(f, net: str, val: int) -> None:
    """Emit `.names` for a constant net (0 or 1)."""
    f.write(f'.names {net}\n')
    if val:
        f.write(' 1\n')
    # val == 0 → no cube lines means constant 0


# ═══════════════════════════════════════════════════════════════════════════════
#  Reader
# ═══════════════════════════════════════════════════════════════════════════════

def read_blif(path: str) -> Tuple[AIG, List[Lit], List[str], List[str], str]:
    """Parse a BLIF file into an AIG.

    Returns (aig, output_lits, input_names, output_names, model_name).
    Each `.names` block is materialised as a sum-of-products over the
    declared fanins and added to the AIG via structural hashing.
    Unsupported constructs (`.latch`, `.subckt`, `.mlatch`) raise.
    """
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()

    blocks = _split_logical_lines(src)

    model_name = 'circuit'
    inputs:  List[str] = []
    outputs: List[str] = []
    gates:   List[Tuple[List[str], List[Tuple[str, int]]]] = []
    # Each gate: (net_names_including_output, [(cube, value), ...])
    i = 0
    while i < len(blocks):
        line = blocks[i].strip()
        if not line or line.startswith('#'):
            i += 1
            continue
        toks = line.split()
        head = toks[0]
        if head == '.model':
            model_name = toks[1] if len(toks) > 1 else model_name
            i += 1
        elif head == '.inputs':
            inputs.extend(toks[1:])
            i += 1
        elif head == '.outputs':
            outputs.extend(toks[1:])
            i += 1
        elif head == '.names':
            nets = toks[1:]
            covers: List[Tuple[str, int]] = []
            i += 1
            while i < len(blocks):
                ln = blocks[i].strip()
                if not ln or ln.startswith('#'):
                    i += 1
                    continue
                if ln.startswith('.'):
                    break
                parts = ln.split()
                if len(nets) == 1:
                    # 0-input: constant block, `1` on its own line means 1.
                    val = int(parts[0]) if len(parts) == 1 else int(parts[1])
                    covers.append(('', val))
                else:
                    cube = parts[0]
                    val  = int(parts[1]) if len(parts) > 1 else 1
                    covers.append((cube, val))
                i += 1
            gates.append((nets, covers))
        elif head in ('.end',):
            break
        elif head in ('.latch', '.subckt', '.mlatch', '.gate'):
            raise ValueError(f"BLIF: unsupported directive {head!r}")
        else:
            # Unknown directive: skip it (ABC emits `.exdc`, `.default_input_arrival`,
            # etc.; none of those affect the netlist topology).
            i += 1

    aig = AIG()
    net_lit: Dict[str, Lit] = {}
    for name in inputs:
        net_lit[name] = aig.make_input(name)

    for nets, covers in gates:
        out_name = nets[-1]
        fanins   = nets[:-1]
        lit = _cover_to_lit(aig, fanins, covers, net_lit)
        net_lit[out_name] = lit

    out_lits = []
    for name in outputs:
        if name not in net_lit:
            raise ValueError(f"BLIF: output {name!r} has no driver")
        out_lits.append(net_lit[name])

    return aig, out_lits, inputs, outputs, model_name


def _split_logical_lines(src: str) -> List[str]:
    """Join BLIF backslash-continued physical lines into logical lines."""
    out: List[str] = []
    buf = ''
    for raw in src.splitlines():
        s = raw.rstrip()
        if s.endswith('\\'):
            buf += s[:-1] + ' '
            continue
        buf += s
        out.append(buf)
        buf = ''
    if buf:
        out.append(buf)
    return out


def _cover_to_lit(
    aig:      AIG,
    fanins:   List[str],
    covers:   List[Tuple[str, int]],
    net_lit:  Dict[str, Lit],
) -> Lit:
    """Compile one `.names` cover into an AIG literal.

    All cube values in a single `.names` are expected to agree (on-set xor
    off-set — BLIF does not allow mixing). If the cover describes the
    off-set, the result is complemented at the end.
    """
    if not covers:
        # Empty cover = constant 0.
        return FALSE

    values = {v for _, v in covers}
    if values - {0, 1}:
        raise ValueError("BLIF: cube value must be 0 or 1")
    if values == {0, 1}:
        raise ValueError("BLIF: mixed on-set/off-set covers not supported")
    is_offset = (values == {0})

    fan_lits = [net_lit.get(n) for n in fanins]
    for k, l in enumerate(fan_lits):
        if l is None:
            raise ValueError(f"BLIF: fanin {fanins[k]!r} has no driver")

    # Degenerate 0-fanin case.
    if not fanins:
        # A single cube `1` = constant 1; empty cover (handled above) = 0.
        val = covers[0][1]
        lit = TRUE if val == 1 else FALSE
        return lit ^ 1 if is_offset else lit

    # Sum-of-products: OR over cubes of AND over literals.
    result = FALSE
    for cube, _val in covers:
        if len(cube) != len(fanins):
            raise ValueError(
                f"BLIF: cube width {len(cube)} != fanin count {len(fanins)}"
            )
        prod = TRUE
        for ch, flit in zip(cube, fan_lits):
            if ch == '-':
                continue
            if ch == '1':
                prod = aig.make_and(prod, flit)
            elif ch == '0':
                prod = aig.make_and(prod, flit ^ 1)
            else:
                raise ValueError(f"BLIF: unknown cube character {ch!r}")
        result = aig.make_or(result, prod)

    return result ^ 1 if is_offset else result
