"""Shared layout constants for the .circ exporters."""

from __future__ import annotations


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
