"""``export_fsm_circ`` — FSM cone + flip-flop harness export."""

from __future__ import annotations
from typing import List

from ..nand      import nand_gate_count
from ._layout    import _GATE_REAL_SIZE, _GATE_SIZE
from ._decoder_builder import _DecoderBuilder


# ═══════════════════════════════════════════════════════════════════════════════
#  FSM export — combinational cone + D flip-flops + clock
# ═══════════════════════════════════════════════════════════════════════════════

def export_fsm_circ(fsm_result, path: str,
                    circuit_name: str = 'fsm_core',
                    flipflop: str = 'auto') -> None:
    """
    Export a synthesized FSM to a Logisim Evolution .circ file.

    Structure:
      • Sub-circuit ``<circuit_name>`` — the combinational cone, identical
        to ``export_circ`` output: state bits and FSM inputs enter as Pin
        components; excitation bits (D or J/K) and FSM outputs leave as
        Pin components.
      • Main circuit — instantiates the sub-circuit once, inserts one
        Logisim flip-flop per state bit, wires the excitation pins through
        the flip-flops back to state-bit pins (Q), and connects a shared
        Clock driver.  FSM inputs become top-level input Pins; FSM outputs
        become top-level output Pins.

    Parameters
    ----------
    flipflop : 'd' | 'jk' | 'auto'
        Flip-flop primitive to use in the harness.  'auto' (default) picks
        the primitive that matches the FSMResult's excitation strategy: a
        D_FF when excitation='d', a JK_FF when excitation='jk'.  Explicit
        'd' / 'jk' overrides are rejected if they would contradict the
        synthesized cone, since the combinational outputs must match the
        flip-flop's input protocol.

    Reset behaviour: flip-flops power up to 0, which corresponds to the
    reset_state under the 'binary' and 'gray' encodings (see encode_states).
    One-hot reset requires an explicit preset; this is left to the user.
    """
    from ...sequential.fsm import FSMResult  # local import to avoid cycles

    if not isinstance(fsm_result, FSMResult):
        raise TypeError("export_fsm_circ expects an FSMResult")

    if flipflop == 'auto':
        flipflop = fsm_result.excitation
    if flipflop not in ('d', 'jk'):
        raise ValueError(
            f"flipflop must be 'd', 'jk', or 'auto', got {flipflop!r}")
    if flipflop != fsm_result.excitation:
        raise ValueError(
            f"flipflop={flipflop!r} does not match FSMResult.excitation="
            f"{fsm_result.excitation!r}; re-synthesize with a matching "
            f"excitation= argument to synthesize_fsm()")

    opt_result = fsm_result.opt_result
    tt         = opt_result.truth_table
    decoder_xml = _DecoderBuilder(opt_result).build()

    # ── harness: flip-flops + feedback + input/output pins ────────────────
    lines: List[str] = []

    def comp(lib, x, y, name, attrs=None):
        parts = [f'    <comp lib="{lib}" loc="({x},{y})" name="{name}">']
        for k, v in (attrs or {}).items():
            parts.append(f'      <a name="{k}" val="{v}"/>')
        parts.append('    </comp>')
        lines.append('\n'.join(parts))

    def wire(x1, y1, x2, y2):
        if (x1, y1) != (x2, y2):
            lines.append(f'    <wire from="({x1},{y1})" to="({x2},{y2})"/>')

    def tun(x, y, facing, label):
        comp(0, x, y, 'Tunnel', {'facing': facing, 'label': label})

    state_bits  = fsm_result.state_bit_names
    fsm_inputs  = fsm_result.stt.input_names
    fsm_outputs = fsm_result.fsm_output_names
    n_state     = len(state_bits)

    # Async reset tract: bypass the combinational cone and land directly on
    # the CLR pin of every flip-flop.  Logisim's Memory-library flip-flops
    # treat the CLR pin as active-high asynchronous clear, so for
    # 'async_low' we invert the user's signal through a NAND-tied inverter
    # before distributing it on the shared CLR tunnel.
    async_reset   = fsm_result.reset_polarity in ('async_low', 'async_high')
    reset_pin     = fsm_result.reset_input_name
    reset_tunnel  = '__CLR__' if async_reset else None

    # Layout
    core_x, core_y = 500, 100
    clock_x, clock_y = 120, 80
    ff_x = 300
    ff_row_y = 180
    ff_spacing = 80
    fsm_in_x = 120
    fsm_in_y = 200
    fsm_out_x = 800
    fsm_out_y = 200

    # Clock
    comp(0, clock_x, clock_y, 'Clock', {'appearance': 'NewPins'})
    tun(clock_x, clock_y, 'east', 'CLK')

    # Async reset pin (if requested) — routed directly to the shared CLR
    # tunnel, never entering the combinational excitation cone.
    if async_reset:
        reset_px = clock_x
        reset_py = clock_y + 60
        comp(0, reset_px, reset_py, 'Pin', {
            'appearance': 'classic',
            'label':      reset_pin,
            'labelfont':  'SansSerif bold 10',
        })
        if fsm_result.reset_polarity == 'async_high':
            # Direct: user's active-high signal matches Logisim's CLR.
            tun(reset_px + 40, reset_py, 'west', reset_tunnel)
            wire(reset_px, reset_py, reset_px + 40, reset_py)
        else:
            # async_low: invert via a NAND-tied-inputs, then distribute.
            inv_x = reset_px + 80
            wire(reset_px, reset_py, inv_x - _GATE_REAL_SIZE, reset_py)
            comp(1, inv_x, reset_py, 'NAND Gate', {
                'size':   str(_GATE_SIZE),
                'inputs': '2',
                'label':  'RESET_INV',
            })
            # Tie both NAND inputs to the reset pin
            wire(inv_x - _GATE_REAL_SIZE, reset_py,
                 inv_x - _GATE_REAL_SIZE, reset_py - 10)
            wire(inv_x - _GATE_REAL_SIZE, reset_py,
                 inv_x - _GATE_REAL_SIZE, reset_py + 10)
            tun(inv_x + 10, reset_py, 'west', reset_tunnel)
            wire(inv_x, reset_py, inv_x + 10, reset_py)

    # Sub-circuit instance
    comp(-1, core_x, core_y, circuit_name, {})

    # Primary inputs (FSM input bits) — routed via tunnels into the sub-circuit
    for i, name in enumerate(fsm_inputs):
        py = fsm_in_y + i * 40
        comp(0, fsm_in_x, py, 'Pin', {
            'appearance': 'classic',
            'label':      name,
            'labelfont':  'SansSerif bold 10',
        })
        tun(fsm_in_x + 40, py, 'west', name)

    # Flip-flops — one per state bit
    if flipflop == 'd':
        for i, (d_wire, q_wire) in enumerate(
                zip(fsm_result.next_bit_names, state_bits)):
            fy = ff_row_y + i * ff_spacing
            # Tunnel D-input coming from sub-circuit output
            tun(ff_x - 60, fy, 'east', d_wire)
            wire(ff_x - 60, fy, ff_x - 30, fy)
            # D Flip-Flop component (Logisim Memory library = lib 4)
            comp(4, ff_x, fy, 'D Flip-Flop', {
                'appearance': 'logisim_evolution',
                'label':      q_wire,
            })
            # Clock tunnel on the FF
            tun(ff_x - 10, fy + 20, 'north', 'CLK')
            # Q-output tunnel feeds the state-bit wire back into the cone
            tun(ff_x + 30, fy, 'west', q_wire)
            # Async CLR tunnel: shared control tract, routed to the south
            # side of the FF (where Logisim Evolution places the CLR pin).
            if async_reset:
                tun(ff_x - 30, fy + 20, 'north', reset_tunnel)
    else:  # jk
        for i, (j_wire, k_wire, q_wire) in enumerate(zip(
                fsm_result.j_bit_names,
                fsm_result.k_bit_names,
                state_bits)):
            fy = ff_row_y + i * ff_spacing
            # Tunnel J-input (upper) and K-input (lower) from sub-circuit
            tun(ff_x - 60, fy - 10, 'east', j_wire)
            wire(ff_x - 60, fy - 10, ff_x - 30, fy - 10)
            tun(ff_x - 60, fy + 10, 'east', k_wire)
            wire(ff_x - 60, fy + 10, ff_x - 30, fy + 10)
            # J-K Flip-Flop component (Logisim Memory library = lib 4)
            comp(4, ff_x, fy, 'J-K Flip-Flop', {
                'appearance': 'logisim_evolution',
                'label':      q_wire,
            })
            # Clock tunnel on the FF
            tun(ff_x - 10, fy + 20, 'north', 'CLK')
            # Q-output tunnel feeds the state-bit wire back into the cone
            tun(ff_x + 30, fy, 'west', q_wire)
            # Async CLR tunnel (see D-FF branch above)
            if async_reset:
                tun(ff_x - 30, fy + 20, 'north', reset_tunnel)

    # Outputs (FSM output bits) — routed via tunnels from sub-circuit
    for i, name in enumerate(fsm_outputs):
        py = fsm_out_y + i * 40
        tun(fsm_out_x - 40, py, 'east', name)
        comp(0, fsm_out_x, py, 'Pin', {
            'appearance': 'classic',
            'facing':     'west',
            'type':       'output',
            'label':      name,
            'labelfont':  'SansSerif bold 10',
        })

    harness_xml = '\n'.join(lines)

    xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<project source="4.1.0" version="1.0">
  This file is intended to be loaded by Logisim-evolution v4.1.0(https://github.com/logisim-evolution/).

  <lib desc="#Wiring" name="0">
    <tool name="Pin">
      <a name="appearance" val="classic"/>
    </tool>
  </lib>
  <lib desc="#Gates" name="1"/>
  <lib desc="#Base" name="2"/>
  <lib desc="#Plexers" name="3"/>
  <lib desc="#Memory" name="4"/>
  <main name="main"/>
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
    <tool lib="1" name="NAND Gate"/>
    <tool lib="4" name="D Flip-Flop"/>
  </toolbar>
  <circuit name="{circuit_name}">
    <a name="appearance" val="logisim_evolution"/>
    <a name="circuit" val="{circuit_name}"/>
    <a name="circuitnamedboxfixedsize" val="true"/>
    <a name="simulationFrequency" val="1.0"/>
{decoder_xml}
  </circuit>
  <circuit name="main">
    <a name="appearance" val="logisim_evolution"/>
    <a name="circuit" val="main"/>
    <a name="simulationFrequency" val="1.0"/>
{harness_xml}
  </circuit>
</project>
'''

    with open(path, 'w', encoding='utf-8') as f:
        f.write(xml)

    n_nand = nand_gate_count(opt_result.builder.gates)
    ff_label = 'D' if flipflop == 'd' else 'JK'
    reset_note = ''
    if async_reset:
        reset_note = (f', async-{fsm_result.reset_polarity.split("_")[1]} '
                      f'reset on {reset_pin!r}')
    print(f'  Exported FSM {path}  '
          f'({n_nand} NAND gates + {n_state} {ff_label} flip-flops, '
          f'{len(fsm_inputs)} inputs, {len(fsm_outputs)} outputs'
          f'{reset_note})')
