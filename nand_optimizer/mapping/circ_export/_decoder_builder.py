"""``_DecoderBuilder`` — emits the decoder sub-circuit XML for a NAND cone."""

from __future__ import annotations
from typing import Dict, List, Set, Tuple

from ...pipeline import OptimizeResult
from ._layout import (
    _snap,
    _GATE_REAL_SIZE,
    _GATE_SIZE,
    _INPUT_X,
    _TUNNEL_X,
    _GATE_X0,
    _COL_SPACE,
    _ROW_SPACE,
    _MARGIN_Y,
)


class _DecoderBuilder:

    def __init__(self, result: OptimizeResult,
                 internal_input_names: Set[str] = frozenset(),
                 emit_output_pins: bool = True,
                 emit_input_pins: bool = True,
                 emit_input_tunnels: bool = True):
        """
        Parameters
        ----------
        internal_input_names
            Input names for which *only* a Tunnel is emitted (no top-level
            Pin).  Used when the circuit is flat and these inputs are driven
            internally by other components (e.g. flip-flop Q outputs in
            :func:`export_counter_circ`).
        emit_output_pins
            If False, the final stage emits only Tunnels for each output
            (labelled after the output's driving wire), so the caller can
            place Pins and other consumers wherever they like.
        emit_input_pins
            If False, no top-level Pin components are emitted for any input
            (only the routing Tunnels), so the caller's harness can place
            the actual input Pins without duplication.
        emit_input_tunnels
            If False, the west-facing source Tunnels at _TUNNEL_X are
            suppressed for all inputs.  Set to False when the caller's
            harness supplies all signal sources via its own tunnel labels,
            eliminating the floating stub icons that appear in the cone area.
        """
        self.result  = result
        self.tt      = result.truth_table
        self.builder = result.builder
        self._lines: List[str] = []
        self._depth: Dict[str, int] = {}
        self._internal_inputs = set(internal_input_names)
        self._emit_output_pins = emit_output_pins
        self._emit_input_pins = emit_input_pins
        self._emit_input_tunnels = emit_input_tunnels
        # Hierarchical results have no truth_table; derive input names from the AIG.
        if self.tt is not None:
            self._input_names: List[str] = list(self.tt.input_names)
        else:
            self._input_names = result.aig.input_names()

    def _comp(self, lib: int, x: int, y: int, name: str,
              attrs: Dict[str, str] | None = None):
        a = dict(attrs or {})
        if 'label' in a:
            a['labelfont'] = 'SansSerif bold 10'
        parts = [f'    <comp lib="{lib}" loc="({x},{y})" name="{name}">']
        for k, v in a.items():
            parts.append(f'      <a name="{k}" val="{v}"/>')
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

    def max_gate_rows(self) -> int:
        """Maximum number of NAND gates in any single depth column.

        Call after :meth:`build` (``_depth`` must be populated).
        Used by :func:`export_counter_circ` to compute the true cone height,
        which can exceed ``n_inputs * _ROW_SPACE`` when a column is wide.
        """
        by_depth: Dict[int, int] = {}
        for gn, gt, _ in self.builder.gates:
            if gt == 'NAND':
                d = self._depth.get(gn, 0)
                by_depth[d] = by_depth.get(d, 0) + 1
        return max(by_depth.values(), default=0)

    def build(self) -> str:
        self._compute_depths()
        max_d = max(self._depth.values(), default=0)
        self.out_x: int = _snap(_GATE_X0 + (max_d + 1) * _COL_SPACE)

        # ── input pins ────────────────────────────────────────────────
        for i, name in enumerate(self._input_names):
            py = _snap(_MARGIN_Y + i * _ROW_SPACE)
            if self._emit_input_pins:
                if name not in self._internal_inputs:
                    self._comp(0, _INPUT_X, py, 'Pin', {
                        'appearance':  'classic',
                        'label':       name,
                    })
                else:
                    # Driven internally (e.g. FF feedback): emit an output-type
                    # Pin so the current value is observable on the comb-cone row.
                    self._comp(0, _INPUT_X, py, 'Pin', {
                        'appearance':  'classic',
                        'facing':      'east',
                        'type':        'output',
                        'label':       name,
                        'labelloc':    'west',
                    })
                self._wire(_INPUT_X, py, _TUNNEL_X, py)
            if self._emit_input_tunnels:
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
        out_x = self.out_x

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
            # Tunnel that picks up the driver wire and re-publishes it under
            # the logical output name so downstream consumers can grab it by
            # label.  When `emit_output_pins` is False we only publish the
            # logical-name tunnel (no probe Pin) so the harness owns every
            # visible output pin.
            self._comp(0, tx, py, 'Tunnel', {
                'facing': 'east', 'label': out_wire,
            })
            if not self._emit_output_pins:
                self._comp(0, tx, py, 'Tunnel', {
                    'facing': 'west', 'label': name,
                })
                continue
            self._wire(tx, py, out_x, py)
            self._comp(0, out_x, py, 'Pin', {
                'appearance':  'classic',
                'facing':      'west',
                'label':       name,
                'labelloc':    'east',
                'type':        'output',
            })

        return '\n'.join(self._lines)
