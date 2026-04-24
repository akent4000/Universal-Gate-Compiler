"""
Export NAND gate networks to Logisim Evolution .circ (XML) files.

This package is the successor to the former monolithic ``circ_export.py``.
The public API is unchanged — callers continue to use::

    from nand_optimizer.mapping.circ_export import (
        export_circ, export_fsm_circ, export_counter_circ,
    )

Internal layout:
  * ``_layout``           — shared coordinate constants and ``_snap``
  * ``_decoder_builder``  — ``_DecoderBuilder`` (NAND cone → XML)
  * ``_decoder``          — ``export_circ`` (combinational decoder)
  * ``_fsm``              — ``export_fsm_circ`` (FSM with D / J-K flip-flops)
  * ``_counter``          — ``export_counter_circ`` (universal JK counter)
"""

from ._decoder import export_circ
from ._fsm     import export_fsm_circ
from ._counter import export_counter_circ

__all__ = ['export_circ', 'export_fsm_circ', 'export_counter_circ']
