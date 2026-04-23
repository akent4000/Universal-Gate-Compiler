"""
Export NAND gate network to Logisim Evolution .circ (XML) file.

Output structure matches Logisim Evolution 4.x:
  • Proper library declarations (#Wiring, #Gates, #Memory, #I/O, #Base)
  • Decoder sub-circuit with labeled NAND gates and Tunnel routing
  • Test harness (main) with Counter → Splitter → decoder → 7-Segment Display

Usage:
    from nand_optimizer import optimize
    from nand_optimizer.circ_export import export_circ

    result = optimize(my_truth_table)
    export_circ(result, 'output.circ')
"""

from __future__ import annotations
from typing import Dict, List, Set, Tuple
import xml.etree.ElementTree as ET

from ..pipeline import OptimizeResult
from .nand      import nand_gate_count


# ═══════════════════════════════════════════════════════════════════════════════
#  Layout constants
# ═══════════════════════════════════════════════════════════════════════════════

_GATE_REAL_SIZE   = 40
_GATE_SIZE        = 30
_INPUT_X          = 60
_TUNNEL_X         = 100
_GATE_X0          = 250
_COL_SPACE        = 140
_ROW_SPACE        = 60
_MARGIN_Y         = 60


def _snap(v: float) -> int:
    return int(round(v / 10) * 10)


# ═══════════════════════════════════════════════════════════════════════════════
#  Decoder sub-circuit builder
# ═══════════════════════════════════════════════════════════════════════════════

class _DecoderBuilder:

    def __init__(self, result: OptimizeResult):
        self.result  = result
        self.tt      = result.truth_table
        self.builder = result.builder
        self._lines: List[str] = []
        self._depth: Dict[str, int] = {}
        # Hierarchical results have no truth_table; derive input names from the AIG.
        if self.tt is not None:
            self._input_names: List[str] = list(self.tt.input_names)
        else:
            self._input_names = result.aig.input_names()

    def _comp(self, lib: int, x: int, y: int, name: str,
              attrs: Dict[str, str] | None = None):
        parts = [f'    <comp lib="{lib}" loc="({x},{y})" name="{name}">']
        for k, v in (attrs or {}).items():
            parts.append(f'      <a name="{k}" val="{v}"/>')
        parts.append(f'      <a name="labelfont" val="SansSerif bold 10"/>')
        parts.append('    </comp>')
        self._lines.append('\n'.join(parts))

    def _wire(self, x1: int, y1: int, x2: int, y2: int):
        if (x1, y1) != (x2, y2):
            self._lines.append(
                f'    <wire from="({x1},{y1})" to="({x2},{y2})"/>')

    def _compute_depths(self):
        for n in self._input_names:
            self._depth[n] = -1
        for gn, gt, ins in self.builder.gates:
            if gt in ('ZERO', 'ONE'):
                self._depth[gn] = -1
            elif gt == 'NAND':
                d = max((self._depth.get(i, -1) for i in ins), default=-1) + 1
                self._depth[gn] = d

    def build(self) -> str:
        self._compute_depths()

        # ── input pins ────────────────────────────────────────────────
        for i, name in enumerate(self._input_names):
            py = _snap(_MARGIN_Y + i * _ROW_SPACE)
            self._comp(0, _INPUT_X, py, 'Pin', {
                'appearance':  'classic',
                'label':       name,
                'labelfont':   'SansSerif bold 14',
            })
            self._wire(_INPUT_X, py, _TUNNEL_X, py)
            self._comp(0, _TUNNEL_X, py, 'Tunnel', {
                'facing': 'west',
                'label':  name,
            })

        # ── constant sources ──────────────────────────────────────────
        ci = 0
        for gn, gt, _ in self.builder.gates:
            if gt not in ('ZERO', 'ONE'):
                continue
            py = _snap(_MARGIN_Y + (len(self._input_names) + ci) * _ROW_SPACE)
            ci += 1
            val = '0x1' if gt == 'ONE' else '0x0'
            self._comp(0, _INPUT_X, py, 'Constant', {'value': val})
            self._wire(_INPUT_X, py, _TUNNEL_X, py)
            self._comp(0, _TUNNEL_X, py, 'Tunnel', {
                'facing': 'west',
                'label':  gn,
            })

        # ── NAND gates ────────────────────────────────────────────────
        by_depth: Dict[int, List[Tuple[str, str, List[str]]]] = {}
        for gn, gt, ins in self.builder.gates:
            if gt == 'NAND':
                d = self._depth[gn]
                by_depth.setdefault(d, []).append((gn, gt, ins))

        for d in sorted(by_depth):
            col_x = _snap(_GATE_X0 + d * _COL_SPACE)
            for row, (gn, _, ins) in enumerate(by_depth[d]):
                gx = col_x
                gy = _snap(_MARGIN_Y + row * _ROW_SPACE)
                in_x = gx - _GATE_REAL_SIZE
                in_yt = gy - 10
                in_yb = gy + 10

                # Gate with label
                self._comp(1, gx, gy, 'NAND Gate', {
                    'size':      str(_GATE_SIZE),
                    'inputs':    '2',
                    'label':     gn,
                    'labelfont': 'SansSerif bold 14',
                })

                # Input tunnels directly on pins
                is_not = (len(ins) == 2 and ins[0] == ins[1])
                if is_not:
                    self._comp(0, in_x, in_yt, 'Tunnel', {
                        'facing': 'east', 'label': ins[0],
                    })
                    self._comp(0, in_x, in_yb, 'Tunnel', {
                        'facing': 'east', 'label': ins[0],
                    })
                else:
                    self._comp(0, in_x, in_yt, 'Tunnel', {
                        'facing': 'east', 'label': ins[0],
                    })
                    self._comp(0, in_x, in_yb, 'Tunnel', {
                        'facing': 'east', 'label': ins[1],
                    })

                # Output tunnel on gate output
                self._comp(0, gx, gy, 'Tunnel', {
                    'facing': 'west', 'label': gn,
                })

        # ── output pins ───────────────────────────────────────────────
        max_d = max(self._depth.values(), default=0)
        out_x = _snap(_GATE_X0 + (max_d + 1) * _COL_SPACE)

        # Regular optimize() fills result.outputs; hierarchical_optimize() leaves
        # it empty — fall back to extracting (name, wire) from OUTPUT gates.
        if self.result.outputs:
            output_pairs = [(name, r.out_wire)
                            for name, r in self.result.outputs.items()]
        else:
            output_pairs = [(gn, gi[0])
                            for gn, gt, gi in self.builder.gates
                            if gt == 'OUTPUT']

        for i, (name, out_wire) in enumerate(output_pairs):
            py = _snap(_MARGIN_Y + i * _ROW_SPACE)
            tx = out_x - 40
            self._comp(0, tx, py, 'Tunnel', {
                'facing': 'east', 'label': out_wire,
            })
            self._wire(tx, py, out_x, py)
            self._comp(0, out_x, py, 'Pin', {
                'appearance':  'classic',
                'facing':      'west',
                'label':       name,
                'labelfont':   'SansSerif bold 14',
                'labelloc':    'east',
                'type':        'output',
            })

        return '\n'.join(self._lines)


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
            'type':        'output',
        })
        wire(ox, oy, out_pin_x, oy)

    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  Full .circ assembly
# ═══════════════════════════════════════════════════════════════════════════════

def export_circ(result: OptimizeResult, path: str,
                circuit_name: str = 'decoder') -> None:
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
    """
    tt = result.truth_table
    decoder_xml = _DecoderBuilder(result).build()

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
  <main name="decoder"/>
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
    <tool lib="1" name="NOT Gate"/>
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
    print(f'  Exported {path}  ({n_nand} NAND gates, {n_in} inputs, {n_out} outputs)')


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
    from ..sequential.fsm import FSMResult  # local import to avoid cycles

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
            'labelfont':  'SansSerif bold 14',
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
            'labelfont':  'SansSerif bold 14',
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
            'labelfont':  'SansSerif bold 14',
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