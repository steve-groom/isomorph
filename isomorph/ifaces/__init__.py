"""Bundle factories for the interfaces a design might speak.

Standards only. A port of one product's own design belongs with that
design, and the ports isomorph.lib speaks - a ready/valid stream, a
block RAM read and write - belong with the library, in isomorph.lib.
"""
from .avalon import avalon_mm, avalon_st
from .wishbone import wishbone
from .axi import axi4_lite
from .spi import spi, spim, spis, spis_tri
from .gpio import gpio, irq
from .i2c import i2c
from .uart import uart
from .config import config
from .memory import sdram, hyperram

__all__ = ['avalon_mm', 'avalon_st', 'wishbone', 'axi4_lite',
           'spi', 'spim', 'spis', 'spis_tri',
           'gpio', 'irq', 'i2c', 'uart', 'config',
           'sdram', 'hyperram']
