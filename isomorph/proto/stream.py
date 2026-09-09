"""Ready/valid stream checker. Same handshake as AXI VALID/READY."""
from .check import Check
from ..execute import SimError


class Stream (Check):
    """valid must hold until ready; data stable while stalled."""

    name = 'stream'

    def __init__ (self, prefix = '', pins = None, strict = True,
                  reset = None, reset_active = 1):
        super().__init__(prefix = prefix, pins = pins, strict = strict)
        self.reset = reset
        self.reset_active = reset_active
        self.prev_valid = 0
        self.prev_ready = 0
        self.prev_data = 0

    def sample (self, sim, when):
        if when != 'edge':
            return
        if self._in_reset(sim):
            self.prev_valid = 0
            self.prev_ready = 0
            self.prev_data = 0
            return
        valid = self.get(sim, 'valid', 0)
        ready = self.get(sim, 'ready', 0)
        data = self.get(sim, 'data', 0)
        if self.prev_valid and not self.prev_ready:
            if not valid:
                self.fail(sim, 'STR-X01',
                          'valid dropped before ready')
            if data != self.prev_data:
                self.fail(sim, 'STR-X02',
                          'data changed while valid held')
        self.prev_valid = valid
        self.prev_ready = ready
        self.prev_data = data

    def _in_reset (self, sim):
        if self.reset is None:
            return False
        try:
            return int(sim.get(self.reset)) == self.reset_active
        except SimError:
            return False
