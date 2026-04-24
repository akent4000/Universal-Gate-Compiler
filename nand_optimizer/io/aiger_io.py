"""
AIGER I/O — industry-standard And-Inverter Graph interchange format.

Implements both ASCII (`aag`) and binary (`aig`) flavours of the AIGER 1.9
format used by ABC, aiger-toolkit, the EPFL combinational benchmark suite,
and Yosys `write_aiger`. Only the combinational subset is supported
(no latches, `L=0`); constraints / justice properties are ignored on read.

Specification: http://fmv.jku.at/aiger/FORMAT-20070427.pdf

Literal convention matches our internal AIG:
  lit = var * 2 + complement_bit
  var 0 = constant FALSE/TRUE (lits 0 and 1)

Typical use:

    from nand_optimizer import optimize, write_aiger, read_aiger
    r = optimize(tt)
    write_aiger(r.aig, r.out_lits, 'out.aig',
                output_names=tt.output_names, binary=True)

    aig, out_lits, in_names, out_names = read_aiger('ext.aig')
"""

from __future__ import annotations
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.aig import AIG, Lit


# ── XOR expansion helper ─────────────────────────────────────────────────────

def _expand_xor_nodes(aig: AIG, out_lits: List[Lit]) -> Tuple[AIG, List[Lit]]:
    """
    Return a pure-AND AIG equivalent to ``aig`` by expanding each native XOR
    node into 3 AND nodes: XOR(a,b) = OR(AND(a,~b), AND(~a,b)) = ~AND(~AND(a,~b), ~AND(~a,b)).

    Called by ``write_aiger`` because AIGER format supports AND gates only.
    If the AIG has no XOR nodes, returns the original AIG unchanged.
    """
    has_xor = any(e[0] == 'xor' for e in aig._nodes)
    if not has_xor:
        return aig, list(out_lits)

    new_aig = AIG()
    lit_map: Dict[int, int] = {0: 0, 1: 1}
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            nlit = new_aig.make_input(entry[1])
        elif entry[0] == 'xor':
            _, a, b = entry
            # XOR(a,b) expanded to 3 ANDs: OR(AND(a,~b), AND(~a,b))
            na, nb = lit_map[a], lit_map[b]
            nlit = new_aig.make_or(
                new_aig.make_and(na, new_aig.make_not(nb)),
                new_aig.make_and(new_aig.make_not(na), nb),
            )
        else:
            _, a, b = entry
            nlit = new_aig.make_and(lit_map[a], lit_map[b])
        lit_map[nid * 2]     = nlit
        lit_map[nid * 2 + 1] = nlit ^ 1
    new_outs = [lit_map.get(l, l) for l in out_lits]
    return new_aig.gc(new_outs)


# ── collection / mapping helpers ─────────────────────────────────────────────

def _collect(aig: AIG) -> Tuple[List[int], List[Tuple[int, int, int]]]:
    """Return (input_node_ids, and_entries).

    `and_entries` items are (node_id, lit_a, lit_b) in the order they appear
    in aig._nodes, which is topological because make_and appends in order.
    """
    inputs: List[int] = []
    ands:   List[Tuple[int, int, int]] = []
    for i, entry in enumerate(aig._nodes):
        nid = i + 1
        if entry[0] == 'input':
            inputs.append(nid)
        else:
            ands.append((nid, entry[1], entry[2]))
    return inputs, ands


def _build_var_map(
    inputs: List[int],
    ands:   List[Tuple[int, int, int]],
) -> Dict[int, int]:
    """Our node_id → AIGER variable (1..I for inputs, then I+1..I+A for ANDs)."""
    m: Dict[int, int] = {}
    for k, nid in enumerate(inputs):
        m[nid] = k + 1
    for k, (nid, _, _) in enumerate(ands):
        m[nid] = len(inputs) + k + 1
    return m


def _translate_lit(lit: Lit, node_to_var: Dict[int, int]) -> int:
    nid  = lit >> 1
    comp = lit & 1
    if nid == 0:
        return comp
    return node_to_var[nid] * 2 + comp


def _encode_uint(x: int) -> bytes:
    """AIGER 7-bit LEB128-style unsigned varint."""
    out = bytearray()
    while x & ~0x7F:
        out.append((x & 0x7F) | 0x80)
        x >>= 7
    out.append(x & 0x7F)
    return bytes(out)


def _decode_uint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Return (value, new_pos). Raises on truncated stream."""
    x = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("AIGER: truncated varint")
        b = buf[pos]
        pos += 1
        x |= (b & 0x7F) << shift
        if not (b & 0x80):
            return x, pos
        shift += 7


# ═══════════════════════════════════════════════════════════════════════════════
#  Writer
# ═══════════════════════════════════════════════════════════════════════════════

def write_aiger(
    aig:          AIG,
    out_lits:     Sequence[Lit],
    path:         str,
    input_names:  Optional[Sequence[str]] = None,
    output_names: Optional[Sequence[str]] = None,
    binary:       bool = True,
    comment:      Optional[str] = None,
) -> None:
    """Serialise an AIG to an AIGER file.

    Args:
        aig:          source And-Inverter Graph.
        out_lits:     primary-output literals (order preserved in file).
        path:         destination path.
        input_names:  optional override (defaults to names registered in aig).
        output_names: optional names (default `o<i>`).
        binary:       write `aig` delta-compressed format (default) or `aag` ASCII.
        comment:      optional trailing UTF-8 comment section.
    """
    # AIGER supports AND gates only; expand any native XOR nodes first.
    aig, out_lits = _expand_xor_nodes(aig, list(out_lits))

    inputs, ands = _collect(aig)
    I = len(inputs)
    A = len(ands)
    L = 0
    O = len(out_lits)
    M = I + A

    node_to_var = _build_var_map(inputs, ands)

    aig_ands: List[Tuple[int, int, int]] = []
    for nid, a_lit, b_lit in ands:
        lhs = node_to_var[nid] * 2
        r0  = _translate_lit(a_lit, node_to_var)
        r1  = _translate_lit(b_lit, node_to_var)
        if r0 < r1:
            r0, r1 = r1, r0
        if not (lhs > r0 >= r1):
            raise ValueError(
                f"AIGER ordering violated for node {nid}: "
                f"lhs={lhs}, rhs0={r0}, rhs1={r1}"
            )
        aig_ands.append((lhs, r0, r1))

    aig_outs = [_translate_lit(l, node_to_var) for l in out_lits]

    in_syms = list(input_names) if input_names is not None else [
        aig._nodes[nid - 1][1] for nid in inputs
    ]
    if len(in_syms) != I:
        raise ValueError(f"input_names length {len(in_syms)} != I={I}")
    out_syms = list(output_names) if output_names is not None else [
        f'o{i}' for i in range(O)
    ]
    if len(out_syms) != O:
        raise ValueError(f"output_names length {len(out_syms)} != O={O}")

    if binary:
        _write_binary(path, M, I, L, O, A, aig_ands, aig_outs,
                      in_syms, out_syms, comment)
    else:
        _write_ascii(path, M, I, L, O, A, aig_ands, aig_outs,
                     in_syms, out_syms, comment)


def _write_symbols_and_comment(fh, in_syms, out_syms, comment) -> None:
    """Append AIGER symbol table and comment block. `fh` is a text stream."""
    for i, name in enumerate(in_syms):
        fh.write(f'i{i} {name}\n')
    for i, name in enumerate(out_syms):
        fh.write(f'o{i} {name}\n')
    if comment is not None:
        fh.write('c\n')
        fh.write(comment)
        if not comment.endswith('\n'):
            fh.write('\n')


def _write_ascii(path, M, I, L, O, A, ands, outs,
                 in_syms, out_syms, comment):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'aag {M} {I} {L} {O} {A}\n')
        for k in range(I):
            f.write(f'{(k + 1) * 2}\n')
        for olit in outs:
            f.write(f'{olit}\n')
        for lhs, r0, r1 in ands:
            f.write(f'{lhs} {r0} {r1}\n')
        _write_symbols_and_comment(f, in_syms, out_syms, comment)


def _write_binary(path, M, I, L, O, A, ands, outs,
                  in_syms, out_syms, comment):
    # Binary AIGER: header + outputs in ASCII; AND gates delta-encoded;
    # symbol/comment blocks appended as ASCII lines.
    with open(path, 'wb') as f:
        f.write(f'aig {M} {I} {L} {O} {A}\n'.encode('ascii'))
        for olit in outs:
            f.write(f'{olit}\n'.encode('ascii'))
        for lhs, r0, r1 in ands:
            f.write(_encode_uint(lhs - r0))
            f.write(_encode_uint(r0 - r1))
        # Symbols + comments are textual tail even in binary mode.
        buf = []
        for i, name in enumerate(in_syms):
            buf.append(f'i{i} {name}\n')
        for i, name in enumerate(out_syms):
            buf.append(f'o{i} {name}\n')
        if comment is not None:
            buf.append('c\n')
            buf.append(comment if comment.endswith('\n') else comment + '\n')
        if buf:
            f.write(''.join(buf).encode('utf-8'))


# ═══════════════════════════════════════════════════════════════════════════════
#  Reader
# ═══════════════════════════════════════════════════════════════════════════════

def read_aiger(path: str) -> Tuple[AIG, List[Lit], List[str], List[str]]:
    """Load an AIGER (.aag or .aig) file into a fresh AIG.

    Returns (aig, output_lits, input_names, output_names). Latches are
    rejected (combinational only). Default names `i<k>` / `o<k>` are
    provided when the symbol table is absent.
    """
    with open(path, 'rb') as f:
        data = f.read()

    # Split off the header line.
    nl = data.find(b'\n')
    if nl < 0:
        raise ValueError("AIGER: missing header")
    header = data[:nl].decode('ascii').split()
    body_start = nl + 1

    if len(header) != 6 or header[0] not in ('aag', 'aig'):
        raise ValueError(f"AIGER: bad header {header!r}")
    fmt = header[0]
    M, I, L, O, A = (int(x) for x in header[1:6])
    if L != 0:
        raise ValueError(f"AIGER: sequential designs not supported (L={L})")

    if fmt == 'aag':
        return _read_ascii(data[body_start:].decode('utf-8'), M, I, O, A)
    return _read_binary(data, body_start, M, I, O, A)


def _parse_symbols(tail_lines: List[str], I: int, O: int) -> Tuple[List[str], List[str]]:
    in_names  = [f'i{k}' for k in range(I)]
    out_names = [f'o{k}' for k in range(O)]
    for line in tail_lines:
        if not line or line[0] == 'c':
            break  # comment section starts, stop parsing symbols
        if not line:
            continue
        tag = line[0]
        if tag not in ('i', 'o', 'l'):
            continue
        try:
            head, name = line.split(None, 1)
        except ValueError:
            continue
        idx = int(head[1:])
        if tag == 'i' and 0 <= idx < I:
            in_names[idx] = name
        elif tag == 'o' and 0 <= idx < O:
            out_names[idx] = name
    return in_names, out_names


def _read_ascii(body: str, M: int, I: int, O: int, A: int):
    lines = body.split('\n')
    idx = 0

    def take() -> str:
        nonlocal idx
        while idx < len(lines) and lines[idx] == '':
            idx += 1
        if idx >= len(lines):
            raise ValueError("AIGER: unexpected EOF")
        s = lines[idx]
        idx += 1
        return s

    input_lits_raw = [int(take()) for _ in range(I)]
    out_lits_raw   = [int(take()) for _ in range(O)]
    and_triples: List[Tuple[int, int, int]] = []
    for _ in range(A):
        parts = take().split()
        and_triples.append((int(parts[0]), int(parts[1]), int(parts[2])))

    in_names, out_names = _parse_symbols(lines[idx:], I, O)
    aig, out_lits = _build_aig_from_aiger(
        M, I, input_lits_raw, and_triples, out_lits_raw, in_names,
    )
    return aig, out_lits, in_names, out_names


def _read_binary(data: bytes, pos: int, M: int, I: int, O: int, A: int):
    # Binary format: inputs are implicit (2,4,...,2I). Output lits are one
    # per ASCII line. Then A*2 varints for the AND section. Then ASCII tail.
    def read_line() -> str:
        nonlocal pos
        nl = data.find(b'\n', pos)
        if nl < 0:
            raise ValueError("AIGER: truncated body")
        s = data[pos:nl].decode('ascii')
        pos = nl + 1
        return s

    out_lits_raw = [int(read_line()) for _ in range(O)]

    and_triples: List[Tuple[int, int, int]] = []
    for k in range(A):
        lhs = 2 * (I + k + 1)
        d0, pos = _decode_uint(data, pos)
        d1, pos = _decode_uint(data, pos)
        r0 = lhs - d0
        r1 = r0 - d1
        and_triples.append((lhs, r0, r1))

    # Tail: ASCII symbols + optional comment block.
    tail = data[pos:].decode('utf-8', errors='replace').split('\n')
    in_names, out_names = _parse_symbols(tail, I, O)

    input_lits_raw = [2 * (k + 1) for k in range(I)]
    aig, out_lits = _build_aig_from_aiger(
        M, I, input_lits_raw, and_triples, out_lits_raw, in_names,
    )
    return aig, out_lits, in_names, out_names


def _build_aig_from_aiger(
    M:            int,
    I:            int,
    input_lits:   List[int],
    and_triples:  List[Tuple[int, int, int]],
    out_lits_raw: List[int],
    in_names:     List[str],
) -> Tuple[AIG, List[Lit]]:
    # Map AIGER variable index → our internal positive literal.
    var_to_lit: Dict[int, int] = {0: 0}  # AIGER var 0 = const-false positive lit
    aig = AIG()
    for k, alit in enumerate(input_lits):
        if alit & 1:
            raise ValueError(f"AIGER: input {k} declared as complemented literal")
        var = alit >> 1
        var_to_lit[var] = aig.make_input(in_names[k])

    def translate(alit: int) -> Lit:
        var  = alit >> 1
        comp = alit & 1
        if var not in var_to_lit:
            raise ValueError(f"AIGER: forward reference to var {var}")
        return var_to_lit[var] ^ comp

    for lhs, r0, r1 in and_triples:
        a_lit = translate(r0)
        b_lit = translate(r1)
        out   = aig.make_and(a_lit, b_lit)
        var_to_lit[lhs >> 1] = out

    out_lits = [translate(l) for l in out_lits_raw]
    return aig, out_lits
