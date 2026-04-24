"""``export_circ`` — combinational decoder export, plus the legacy
counter+display test harness builder."""

from __future__ import annotations
import re
from typing import List, Tuple

from ...pipeline import OptimizeResult
from ..nand      import nand_gate_count
from ._layout    import _snap, _INPUT_X, _TUNNEL_X, _MARGIN_Y, _ROW_SPACE
from ._decoder_builder import _DecoderBuilder


# ═══════════════════════════════════════════════════════════════════════════════
#  Bus-mode helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_bus_groups(
        names: List[str],
) -> List[Tuple[str, List[Tuple[int, str]]]]:
    """
    Group consecutive names matching ``{prefix}_{int}`` by common prefix.

    Returns list of ``(prefix, [(bit_index, name), …])`` in original order.
    Bit-index is the integer from the name suffix (used for Splitter bit
    assignments so the bus MSB/LSB ordering is preserved).
    Names that don't match the pattern form singleton groups.
    """
    groups: List[Tuple[str, List[Tuple[int, str]]]] = []
    cur_prefix: str | None = None
    cur_bits:   List[Tuple[int, str]] = []

    for name in names:
        m = re.match(r'^(.+)_(\d+)$', name)
        if m:
            prefix, idx = m.group(1), int(m.group(2))
            if prefix == cur_prefix:
                cur_bits.append((idx, name))
            else:
                if cur_bits:
                    groups.append((cur_prefix, cur_bits))   # type: ignore[arg-type]
                cur_prefix = prefix
                cur_bits   = [(idx, name)]
        else:
            if cur_bits:
                groups.append((cur_prefix, cur_bits))       # type: ignore[arg-type]
                cur_prefix = None
                cur_bits   = []
            groups.append((name, [(0, name)]))

    if cur_bits:
        groups.append((cur_prefix, cur_bits))               # type: ignore[arg-type]

    return groups


def _build_bus_xml(result: OptimizeResult, cone_out_x: int) -> str:
    """
    Emit bus-Pin + Splitter infrastructure for inputs and outputs.

    Input groups:  Bus Pin → Splitter (splitting) → labelled source Tunnels
    Output groups: labelled sink Tunnels → Splitter (combining) → Bus Pin

    The caller must have built the cone with ``emit_input_pins=False``,
    ``emit_input_tunnels=False``, and ``emit_output_pins=False`` so that the
    Tunnel label-nets are left unsourced for us to drive here.
    """
    lines: List[str] = []

    def _comp(lib: int, x: int, y: int, name: str, attrs=None):
        a = dict(attrs or {})
        if 'label' in a:
            a['labelfont'] = 'SansSerif bold 10'
        parts = [f'    <comp lib="{lib}" loc="({x},{y})" name="{name}">']
        for k, v in a.items():
            parts.append(f'      <a name="{k}" val="{v}"/>')
        parts.append('    </comp>')
        lines.append('\n'.join(parts))

    def _wire(x1: int, y1: int, x2: int, y2: int):
        if (x1, y1) != (x2, y2):
            lines.append(f'    <wire from="({x1},{y1})" to="({x2},{y2})"/>')

    tt = result.truth_table
    in_names: List[str] = (list(tt.input_names) if tt is not None
                            else result.aig.input_names())

    if result.outputs:
        out_names = [n for n, _ in result.outputs.items()]
    else:
        out_names = [gn for gn, gt, _ in result.builder.gates if gt == 'OUTPUT']

    # ── Input buses ──────────────────────────────────────────────────────────
    # Splitter `appear=right`: bus enters from the west at (spl_x, spl_y);
    # bits exit to the east at (spl_x+20, spl_y+10 + port_i*spacing_px).
    # We place spl_y = first_y - 10 so bit-0 exits at first_y.
    _SPL_IN_X  = _INPUT_X - 30       # 30 — Splitter loc x for input side
    _PIN_IN_X  = _INPUT_X - 50       # 10 — Bus Pin loc x for input side
    _TUN_IN_X  = _INPUT_X            # 60 — source Tunnel x (= standard Pin column)
    _spacing   = _ROW_SPACE // 10    # Logisim spacing units (1 unit = 10 px)

    row = 0
    for prefix, bit_list in _parse_bus_groups(in_names):
        n      = len(bit_list)
        first_y = _snap(_MARGIN_Y + row * _ROW_SPACE)

        if n == 1:
            _, name = bit_list[0]
            _comp(0, _INPUT_X, first_y, 'Pin', {'appearance': 'classic', 'label': name})
            _wire(_INPUT_X, first_y, _TUNNEL_X, first_y)
            _comp(0, _TUNNEL_X, first_y, 'Tunnel', {'facing': 'west', 'label': name})
        else:
            spl_y    = first_y - 10
            bus_label = prefix.upper()
            bit_assign = {f'bit{bus_bit}': str(port_i)
                          for port_i, (bus_bit, _) in enumerate(bit_list)}
            _comp(0, _PIN_IN_X, spl_y, 'Pin', {
                'appearance': 'classic',
                'label': bus_label,
                'width': str(n),
            })
            _wire(_PIN_IN_X, spl_y, _SPL_IN_X, spl_y)
            _comp(0, _SPL_IN_X, spl_y, 'Splitter', {
                'appear':   'right',
                'fanout':   str(n),
                'incoming': str(n),
                'spacing':  str(_spacing),
                **bit_assign,
            })
            for port_i, (_, name) in enumerate(bit_list):
                bit_y = first_y + port_i * _ROW_SPACE
                _wire(_SPL_IN_X + 20, bit_y, _TUN_IN_X, bit_y)
                _comp(0, _TUN_IN_X, bit_y, 'Tunnel', {'facing': 'west', 'label': name})

        row += n

    # ── Output buses ─────────────────────────────────────────────────────────
    # Splitter `facing=west`: bits enter from the west at (spl_x-20, spl_y+10 + port_i*spacing_px);
    # combined bus exits to the right at (spl_x, spl_y).
    # We set spl_y = first_y - 10 so bit-0 enters at first_y.
    _SPL_OUT_X = cone_out_x + 50    # Splitter loc x for output side
    _PIN_OUT_X = cone_out_x + 60    # Bus Pin loc x for output side
    _TUN_OUT_X = cone_out_x + 10   # sink Tunnel x — picks up relay label from cone

    out_row = 0
    for prefix, bit_list in _parse_bus_groups(out_names):
        n      = len(bit_list)
        first_y = _snap(_MARGIN_Y + out_row * _ROW_SPACE)

        if n == 1:
            _, name = bit_list[0]
            _comp(0, _TUN_OUT_X, first_y, 'Tunnel', {'facing': 'east', 'label': name})
            _wire(_TUN_OUT_X, first_y, _PIN_OUT_X, first_y)
            _comp(0, _PIN_OUT_X, first_y, 'Pin', {
                'appearance': 'classic',
                'facing':     'west',
                'type':       'output',
                'label':      name,
                'labelloc':   'east',
            })
        else:
            spl_y    = first_y - 10
            bus_label = prefix.upper()
            bit_assign = {f'bit{bus_bit}': str(port_i)
                          for port_i, (bus_bit, _) in enumerate(bit_list)}
            for port_i, (_, name) in enumerate(bit_list):
                bit_y = first_y + port_i * _ROW_SPACE
                _comp(0, _TUN_OUT_X, bit_y, 'Tunnel', {'facing': 'east', 'label': name})
                _wire(_TUN_OUT_X, bit_y, _SPL_OUT_X - 20, bit_y)
            _comp(0, _SPL_OUT_X, spl_y, 'Splitter', {
                'facing':   'west',
                'fanout':   str(n),
                'incoming': str(n),
                'spacing':  str(_spacing),
                **bit_assign,
            })
            _wire(_SPL_OUT_X, spl_y, _PIN_OUT_X, spl_y)
            _comp(0, _PIN_OUT_X, spl_y, 'Pin', {
                'appearance': 'classic',
                'facing':     'west',
                'type':       'output',
                'label':      bus_label,
                'labelloc':   'east',
                'width':      str(n),
            })

        out_row += n

    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  Test harness (main circuit)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_test_harness(result: OptimizeResult,
                        circuit_name: str) -> str:
    """
    Build 'main' circuit:
      Constant(1) → Counter → Splitter → {input pins + decoder sub-circuit}
      Clock → Counter
      decoder outputs → {output pins} + optional 7-Segment Display
    """
    tt    = result.truth_table
    n_in  = tt.n_inputs
    n_out = tt.n_outputs
    lines = []

    def comp(lib, x, y, name, attrs=None):
        parts = [f'    <comp lib="{lib}" loc="({x},{y})" name="{name}">']
        for k, v in (attrs or {}).items():
            parts.append(f'      <a name="{k}" val="{v}"/>')
        parts.append('    </comp>')
        lines.append('\n'.join(parts))

    def wire(x1, y1, x2, y2):
        if (x1, y1) != (x2, y2):
            lines.append(f'    <wire from="({x1},{y1})" to="({x2},{y2})"/>')

    # Layout
    counter_x, counter_y = 200, 200
    splitter_x = 420
    split_y    = counter_y + 90
    pin_col_x  = 500
    dec_x      = 680
    dec_y      = counter_y + 30
    out_pin_x  = 820

    # Counter
    comp(0, counter_x, counter_y + 50, 'Constant')     # enable=1
    comp(0, counter_x, counter_y + 80, 'Clock', {
        'appearance': 'NewPins',
    })
    comp(5, counter_x, counter_y, 'Counter', {
        'appearance': 'logisim_evolution',
        'max':        hex(len(tt.rows) - 1),
        'width':      str(n_in),
    })

    # Counter output → Splitter
    counter_out_x = counter_x + 100
    counter_out_y = counter_y - 10
    wire(counter_out_x, counter_out_y, splitter_x, counter_out_y)
    wire(splitter_x, counter_out_y, splitter_x, split_y)

    # Splitter (MSB=bit0 assignment for standard ordering)
    bit_assign = {f'bit{i}': str(n_in - 1 - i) for i in range(n_in)}
    comp(0, splitter_x, split_y, 'Splitter', {
        **bit_assign,
        'fanout':   str(n_in),
        'incoming': str(n_in),
        'spacing':  '2',
    })

    # Splitter outputs → input pins + decoder sub-circuit pins
    for i in range(n_in):
        sy = split_y - 10 * (n_in - 1) + i * 20
        sx = splitter_x + 20
        px = pin_col_x
        py = dec_y + 10 + i * 20  # match decoder port spacing

        wire(sx, sy, sx + 10 + i * 10, sy)
        wire(sx + 10 + i * 10, sy, sx + 10 + i * 10, py)
        wire(sx + 10 + i * 10, py, px, py)

        # Input monitor pin
        comp(0, px + 30, py, 'Pin', {
            'appearance':  'classic',
            'facing':      'west',
            'label':       tt.input_names[i],
            'labelfont':   'SansSerif bold 10',
            'type':        'output',
        })
        wire(px, py, px + 30, py)

        # Wire to decoder
        wire(px, py, dec_x, py)

    # Decoder sub-circuit instance
    comp(-1, dec_x + 60, dec_y, circuit_name, {})

    # Decoder output pins
    for i in range(n_out):
        oy = dec_y + 10 + i * 20
        ox = dec_x + 120
        comp(0, out_pin_x, oy, 'Pin', {
            'appearance':  'classic',
            'facing':      'west',
            'label':       tt.output_names[i],
            'labelfont':   'SansSerif bold 10',
            'type':        'output',
        })
        wire(ox, oy, out_pin_x, oy)

    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  Full .circ assembly
# ═══════════════════════════════════════════════════════════════════════════════

def export_circ(result: OptimizeResult, path: str,
                circuit_name: str = 'decoder',
                use_bus: bool = False) -> None:
    """
    Export to Logisim Evolution 4.x .circ file.

    Parameters
    ----------
    result : OptimizeResult
        Output of ``optimize(truth_table)``.
    path : str
        Destination file path.
    circuit_name : str
        Name for the decoder sub-circuit.
    use_bus : bool
        When True, input and output pins whose names follow the
        ``{prefix}_{int}`` convention are grouped into multi-bit bus Pins
        with Splitters.  Input bus → BUS_LABEL (wide Pin); output groups →
        one wide output Pin per group (e.g. DIG0, DIG1).
    """
    tt = result.truth_table

    if use_bus:
        cone_builder = _DecoderBuilder(
            result,
            emit_input_pins=False,
            emit_input_tunnels=False,
            emit_output_pins=False,
        )
        cone_xml   = cone_builder.build()
        bus_xml    = _build_bus_xml(result, cone_builder.out_x)
        decoder_xml = cone_xml + ('\n' + bus_xml if bus_xml else '')
    else:
        decoder_xml = _DecoderBuilder(result).build()

    splitter_lib_entry = '''    <tool name="Splitter">
      <a name="facing" val="west"/>
    </tool>
''' if use_bus else ''

    splitter_toolbar_entry = '''    <tool lib="0" name="Splitter">
      <a name="facing" val="west"/>
    </tool>
''' if use_bus else ''

    xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<project source="4.1.0" version="1.0">
  This file is intended to be loaded by Logisim-evolution v4.1.0(https://github.com/logisim-evolution/).

  <lib desc="#Wiring" name="0">
{splitter_lib_entry}    <tool name="Pin">
      <a name="appearance" val="classic"/>
    </tool>
  </lib>
  <lib desc="#Gates" name="1"/>
  <lib desc="#Base" name="2"/>
  <main name="{circuit_name}"/>
  <options>
    <a name="gateUndefined" val="ignore"/>
    <a name="simlimit" val="1000"/>
    <a name="simrand" val="0"/>
  </options>
  <mappings>
    <tool lib="2" map="Button2" name="Menu Tool"/>
    <tool lib="2" map="Button3" name="Menu Tool"/>
    <tool lib="2" map="Ctrl Button1" name="Menu Tool"/>
  </mappings>
  <toolbar>
    <tool lib="2" name="Poke Tool"/>
    <tool lib="2" name="Edit Tool"/>
    <sep/>
    <tool lib="0" name="Pin"/>
    <tool lib="0" name="Pin">
      <a name="facing" val="west"/>
      <a name="type" val="output"/>
    </tool>
{splitter_toolbar_entry}    <tool lib="1" name="NOT Gate"/>
    <tool lib="1" name="AND Gate"/>
    <tool lib="1" name="OR Gate"/>
    <tool lib="1" name="NAND Gate"/>
  </toolbar>
  <circuit name="{circuit_name}">
    <a name="appearance" val="logisim_evolution"/>
    <a name="circuit" val="{circuit_name}"/>
    <a name="circuitnamedboxfixedsize" val="true"/>
    <a name="simulationFrequency" val="1.0"/>
{decoder_xml}
  </circuit>
</project>
'''

    with open(path, 'w', encoding='utf-8') as f:
        f.write(xml)

    n_nand = nand_gate_count(result.builder.gates)
    if tt is not None:
        n_in  = tt.n_inputs
        n_out = tt.n_outputs
    else:
        n_in  = len(result.aig.input_names()) if result.aig is not None else '?'
        n_out = sum(1 for _, gt, _ in result.builder.gates if gt == 'OUTPUT')
    bus_note = ', bus I/O' if use_bus else ''
    print(f'  Exported {path}  ({n_nand} NAND gates, {n_in} inputs, {n_out} outputs{bus_note})')
