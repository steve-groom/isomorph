"""SPI mode 0/3 checker: complete bytes between chip-selects."""
from .check import Check
from ..execute import SimError


class Spi (Check):
    """cs_n active-low. Mode 0/3 sample on the leading sclk edge
    inside a selected frame. Incomplete bytes on cs_n rise fail."""

    name = 'spi'

    def __init__ (self, prefix = '', pins = None, strict = True,
                  mode = 0, reset = None, reset_active = 1):
        super().__init__(prefix = prefix, pins = pins, strict = strict)
        if mode not in (0, 1, 2, 3):
            raise SimError(f'SPI mode {mode} is not 0..3')
        self.mode = mode
        self.reset = reset
        self.reset_active = reset_active
        self.bits = 0
        self.prev_cs = 1
        self.prev_sclk = 0 if mode in (0, 1) else 1

    def sample (self, sim, when):
        if when != 'edge':
            return
        if self._in_reset(sim):
            self.bits = 0
            self.prev_cs = 1
            self.prev_sclk = 0 if self.mode in (0, 1) else 1
            return
        cs = self.get(sim, 'cs_n', 1)
        sclk = self.get(sim, 'sclk', 0)
        if self.prev_cs and not cs:
            self.bits = 0
        if not self.prev_cs and cs:
            if self.bits % 8:
                self.fail(sim, 'SPI-X01',
                          f'cs_n rose after {self.bits} bits, not a byte')
            self.bits = 0
        if not cs and self._sample_edge(self.prev_sclk, sclk):
            self.bits += 1
        self.prev_cs = cs
        self.prev_sclk = sclk

    def _sample_edge (self, prev, now):
        cpha = self.mode & 1
        cpol = 1 if self.mode in (2, 3) else 0
        if cpha == 0:
            if cpol == 0:
                return (not prev) and now
            return prev and not now
        if cpol == 0:
            return prev and not now
        return (not prev) and now

    def _in_reset (self, sim):
        if self.reset is None:
            return False
        try:
            return int(sim.get(self.reset)) == self.reset_active
        except SimError:
            return False
