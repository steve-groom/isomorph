"""Wishbone B4 classic checker (IFACE_PLAN I2)."""
from .check import Check
from ..execute import SimError


class Wishbone (Check):
    """Classic cycle: CYC+STB until ACK. Sampled on the clock edge."""

    name = 'wishbone'

    def __init__ (self, prefix = '', pins = None, strict = True,
                  reset = None, reset_active = 1):
        super().__init__(prefix = prefix, pins = pins, strict = strict)
        self.reset = reset
        self.reset_active = reset_active
        self.prev = {}

    def sample (self, sim, when):
        if when != 'edge':
            return
        if self._in_reset(sim):
            self.prev = {}
            return
        cyc = self.get(sim, 'cyc', 0)
        stb = self.get(sim, 'stb', 0)
        we = self.get(sim, 'we', 0)
        adr = self.get(sim, 'adr', 0)
        dat_w = self.get(sim, 'dat_w', 0)
        sel = self.get(sim, 'sel', None)
        ack = self.get(sim, 'ack', 0)
        err = self.get(sim, 'err', 0)
        rty = self.get(sim, 'rty', 0)

        if stb and not cyc:
            self.fail(sim, 'WB-X01', 'stb asserted without cyc')
        if ack and not stb:
            self.fail(sim, 'WB-X02', 'ack without stb')
        if err and not cyc:
            self.fail(sim, 'WB-X02', 'err without cyc')
        if rty and not cyc:
            self.fail(sim, 'WB-X02', 'rty without cyc')
        if self.prev.get('stb') and self.prev.get('cyc') and not self.prev.get('ack'):
            for key, val in (('adr', adr), ('we', we), ('dat_w', dat_w),
                             ('sel', sel), ('stb', stb), ('cyc', cyc)):
                if val is None:
                    continue
                if key in self.prev and self.prev[key] != val:
                    self.fail(sim, 'WB-X03',
                              f'{key} changed before ack')
        if self.prev.get('stb') and not stb and self.prev.get('cyc') and cyc:
            if not self.prev.get('ack'):
                self.fail(sim, 'WB-X04',
                          'stb dropped before ack')

        self.prev = {
            'cyc': cyc, 'stb': stb, 'we': we, 'adr': adr,
            'dat_w': dat_w, 'sel': sel, 'ack': ack,
        }

    def _in_reset (self, sim):
        if self.reset is None:
            return False
        try:
            return int(sim.get(self.reset)) == self.reset_active
        except SimError:
            return False
