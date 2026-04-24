"""``export_counter_circ`` — universal reversible JK counter export."""

from __future__ import annotations
from typing import List

from ...pipeline import OptimizeResult
from ..nand      import nand_gate_count
from ._layout    import (
    _snap,
    _GATE_REAL_SIZE,
    _GATE_SIZE,
    _ROW_SPACE,
    _MARGIN_Y,
)
from ._decoder_builder import _DecoderBuilder


# ═══════════════════════════════════════════════════════════════════════════════
#  Universal reversible JK counter export — combinational cone + JK flip-flops
# ═══════════════════════════════════════════════════════════════════════════════

def export_counter_circ(result: OptimizeResult, path: str,
                        bits: int,
                        circuit_name: str = 'main',
                        reset_pin: str = 'RESET_N',
                        reset_polarity: str = 'async_low',
                        use_bus: bool = False) -> None:
    """
    Export the universal reversible JK counter (ЛР №2) to a Logisim
    Evolution .circ file.

    By default, every input and output is a separate 1-bit Pin.
    With ``use_bus=True``, the bit-groups D[0..bits-1] and LIMIT[0..bits-1]
    are collapsed into bus Pins (D_BUS / LIMIT_BUS) with Splitters, and all
    Q outputs are merged into Q_BUS.

    Parameters
    ----------
    reset_polarity
        ``'async_low'`` (default, active-low) inserts a NAND-tied inverter.
        ``'async_high'`` routes the pin directly.
    use_bus
        When True, replace scalar D/LIMIT inputs and Q outputs with buses.
    """
    if reset_polarity not in ('async_low', 'async_high'):
        raise ValueError(
            f"reset_polarity must be 'async_low' or 'async_high', "
            f"got {reset_polarity!r}")

    # Build the combinational cone, suppressing Pins for Q{i} (driven by FFs)
    # and for J{i}/K{i} outputs (consumed by FFs).
    q_inputs = {f'Q{i}' for i in range(bits)}
    cone_builder = _DecoderBuilder(
        result,
        internal_input_names=q_inputs,
        emit_output_pins=False,
        emit_input_pins=False,
        # Suppress the floating west-facing tunnel stubs in the cone: the
        # harness provides all signal sources via its own labelled tunnels.
        emit_input_tunnels=False,
    )
    cone_xml = cone_builder.build()

    # ── harness: flip-flops + input/output pins + reset ────────────────────
    lines: List[str] = []

    def comp(lib, x, y, name, attrs=None):
        a = dict(attrs or {})
        if 'label' in a:
            a['labelfont'] = 'SansSerif bold 10'
        parts = [f'    <comp lib="{lib}" loc="({x},{y})" name="{name}">']
        for k, v in a.items():
            parts.append(f'      <a name="{k}" val="{v}"/>')
        parts.append('    </comp>')
        lines.append('\n'.join(parts))

    def wire(x1, y1, x2, y2):
        if (x1, y1) != (x2, y2):
            lines.append(f'    <wire from="({x1},{y1})" to="({x2},{y2})"/>')

    def tun(x, y, label, facing=None):
        attrs: dict = {}
        if facing:
            attrs['facing'] = facing
        attrs['label'] = label
        comp(0, x, y, 'Tunnel', attrs)

    reset_tunnel = 'CLR'

    # ── Layout ────────────────────────────────────────────────────────────
    # The cone occupies y ≈ _MARGIN_Y .. cone_bottom_y.  We use the larger of
    # the input-row count and the actual deepest gate column (which can have
    # more rows than there are inputs in a complex circuit).
    n_input_rows = len(result.aig.input_names())
    n_gate_rows  = cone_builder.max_gate_rows()
    cone_bottom_y = _MARGIN_Y + max(n_input_rows, n_gate_rows) * _ROW_SPACE
    base_y = _snap(cone_bottom_y + 120)

    ctrl_x = 120                 # single input-pin column
    inv_x  = ctrl_x + 90        # NAND inverter for reset (inputs at ctrl_x+50)

    # Vertical positions — every group is flush (40 px step) with a 50 px gap
    # separating the CLK/RESET, LOAD/UP, D, and LIMIT groups.
    clk_y     = base_y
    rst_y     = clk_y  + 40
    load_y    = rst_y  + 40
    up_y      = load_y + 40
    d_start   = up_y   + 50                         # D0 … D(bits-1)
    lim_start = d_start + bits * 40 + 50            # LIMIT(bits-1) … LIMIT0

    # Flip-flop column — sits to the right of the cone's x-range but below its
    # y-range, so the two regions do not overlap.
    ff_x    = 450
    ff_y0   = clk_y - 10   # FF{i}.J pin lands at ff_y0 + 10 = clk_y
    ff_step = 120

    # Q output pin column
    out_col_x = ff_x + 260   # 710 when ff_x = 450

    # ── CLK input pin ─────────────────────────────────────────────────────
    comp(0, ctrl_x, clk_y, 'Pin', {
        'appearance': 'classic',
        'label':      'CLK',
    })
    wire(ctrl_x, clk_y, ctrl_x + 40, clk_y)
    tun(ctrl_x + 40, clk_y, 'CLK')

    # ── Asynchronous reset tract ───────────────────────────────────────────
    comp(0, ctrl_x, rst_y, 'Pin', {
        'appearance': 'classic',
        'label':      reset_pin,
    })
    if reset_polarity == 'async_high':
        wire(ctrl_x, rst_y, ctrl_x + 40, rst_y)
        tun(ctrl_x + 40, rst_y, reset_tunnel)
    else:
        # Active-low → invert with a NAND-as-inverter (both inputs tied)
        wire(ctrl_x, rst_y, inv_x - _GATE_REAL_SIZE, rst_y)
        comp(1, inv_x, rst_y, 'NAND Gate', {
            'size':   str(_GATE_SIZE),
            'inputs': '2',
            'label':  'RESET_INV',
        })
        wire(inv_x - _GATE_REAL_SIZE, rst_y,
             inv_x - _GATE_REAL_SIZE, rst_y - 10)
        wire(inv_x - _GATE_REAL_SIZE, rst_y,
             inv_x - _GATE_REAL_SIZE, rst_y + 10)
        wire(inv_x, rst_y, inv_x + 20, rst_y)
        tun(inv_x + 20, rst_y, reset_tunnel)

    # ── Primary input pins ────────────────────────────────────────────────
    def _emit_input_pin(name: str, py: int) -> None:
        comp(0, ctrl_x, py, 'Pin', {
            'appearance': 'classic',
            'label':      name,
        })
        wire(ctrl_x, py, ctrl_x + 40, py)
        tun(ctrl_x + 40, py, name)

    def _emit_bus_input(bus_name: str, bit_names: List[str], first_bit_y: int) -> None:
        """Bus pin + right-fanout Splitter.  first_bit_y is the y of bit-0 (top)."""
        n = len(bit_names)
        comp(0, ctrl_x, first_bit_y - 10, 'Pin', {
            'appearance': 'classic',
            'label':      bus_name,
            'width':      str(n),
        })
        wire(ctrl_x, first_bit_y - 10, ctrl_x + 10, first_bit_y - 10)
        comp(0, ctrl_x + 10, first_bit_y - 10, 'Splitter', {
            'appear':   'right',
            'fanout':   str(n),
            'incoming': str(n),
            'spacing':  '4',
        })
        for i, bit_name in enumerate(bit_names):
            bit_y = first_bit_y + i * 40
            wire(ctrl_x + 30, bit_y, ctrl_x + 40, bit_y)
            tun(ctrl_x + 40, bit_y, bit_name)

    _emit_input_pin('LOAD', load_y)
    _emit_input_pin('UP',   up_y)

    d_names = [f'D{i}' for i in range(bits)]
    if use_bus:
        _emit_bus_input('D_BUS', d_names, d_start)
    else:
        for i, name in enumerate(d_names):
            _emit_input_pin(name, d_start + i * 40)

    lim_names = [f'LIMIT{i}' for i in range(bits)]
    if use_bus:
        _emit_bus_input('LIMIT_BUS', lim_names, lim_start)
    else:
        for i, name in enumerate(lim_names):
            _emit_input_pin(name, lim_start + i * 40)

    # ── J-K Flip-Flops — one per bit ──────────────────────────────────────
    # Logisim Evolution J-K FF pin offsets from loc (AbstractFlipFlop source):
    #   J (-10,+10)  K (-10,+30)  CLK (-10,+50)
    #   Preset (+20,0)  CLR (+20,+60)  Q (+50,+10)  ~Q (+50,+50)
    for i in range(bits):
        fy = ff_y0 + i * ff_step

        comp(4, ff_x, fy, 'J-K Flip-Flop', {
            'appearance': 'logisim_evolution',
            'label':      f'Q{i}',
        })
        # J pin
        tun(ff_x - 50, fy + 10, f'J{i}', 'east')
        wire(ff_x - 50, fy + 10, ff_x - 10, fy + 10)
        # K pin
        tun(ff_x - 50, fy + 30, f'K{i}', 'east')
        wire(ff_x - 50, fy + 30, ff_x - 10, fy + 30)
        # CLK pin
        tun(ff_x - 50, fy + 50, 'CLK', 'east')
        wire(ff_x - 50, fy + 50, ff_x - 10, fy + 50)
        # CLR pin — active-high; stub down to reset tunnel
        wire(ff_x + 20, fy + 60, ff_x + 20, fy + 80)
        tun(ff_x + 20, fy + 80, reset_tunnel, 'north')
        # Q output
        wire(ff_x + 50, fy + 10, ff_x + 70, fy + 10)
        tun(ff_x + 70, fy + 10, f'Q{i}')

    # ── Q output pins (scalar) or Q_BUS (bus mode) ────────────────────────
    if use_bus:
        # Combining splitter: bit-i enters from the left; bus exits right.
        # Splitter with facing="west" at loc=(spl_x, spl_y):
        #   bit-i connector at (spl_x - 20, spl_y + 10 + i*40)
        #   combined bus connector at (spl_x, spl_y) going right
        spl_x = out_col_x - 10   # = 700 when out_col_x = 710
        spl_y = ff_y0             # top-aligns bus with first FF

        for i in range(bits):
            tq_y = spl_y + 10 + i * 40
            tun(out_col_x - 40, tq_y, f'Q{i}', 'east')
            wire(out_col_x - 40, tq_y, spl_x - 20, tq_y)

        comp(0, spl_x, spl_y, 'Splitter', {
            'facing':   'west',
            'fanout':   str(bits),
            'incoming': str(bits),
            'spacing':  '4',
        })
        wire(spl_x, spl_y, out_col_x, spl_y)
        comp(0, out_col_x, spl_y, 'Pin', {
            'appearance': 'classic',
            'facing':     'west',
            'type':       'output',
            'label':      'Q_BUS',
            'labelloc':   'east',
            'width':      str(bits),
        })
    else:
        for i in range(bits):
            py = ff_y0 + i * ff_step + 10
            tun(out_col_x - 40, py, f'Q{i}', 'east')
            wire(out_col_x - 40, py, out_col_x, py)
            comp(0, out_col_x, py, 'Pin', {
                'appearance': 'classic',
                'facing':     'west',
                'type':       'output',
                'label':      f'Q{i}',
                'labelloc':   'east',
            })

    harness_xml = '\n'.join(lines)

    # Splitter needs to appear in the Wiring lib and toolbar when use_bus=True.
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
  <lib desc="#Plexers" name="3"/>
  <lib desc="#Memory" name="4"/>
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
{splitter_toolbar_entry}    <tool lib="1" name="NAND Gate"/>
    <tool lib="4" name="J-K Flip-Flop"/>
  </toolbar>
  <circuit name="{circuit_name}">
    <a name="appearance" val="logisim_evolution"/>
    <a name="circuit" val="{circuit_name}"/>
    <a name="simulationFrequency" val="1.0"/>
{cone_xml}
{harness_xml}
  </circuit>
</project>
'''

    with open(path, 'w', encoding='utf-8') as f:
        f.write(xml)

    n_nand = nand_gate_count(result.builder.gates)
    polarity_note = 'active-low' if reset_polarity == 'async_low' else 'active-high'
    bus_note = ', bus I/O' if use_bus else ''
    print(f'  Exported counter {path}  '
          f'({n_nand} NAND gates + {bits} J-K flip-flops, '
          f'{2 + 2 * bits} inputs, {bits} outputs, '
          f'{polarity_note} async reset on {reset_pin!r}{bus_note})')
