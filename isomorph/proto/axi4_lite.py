"""AXI4-Lite handshake checker (IFACE_PLAN I3). No ID, no burst."""
from .check import Check
from ..execute import SimError


class Axi4Lite (Check):
    """VALID must hold until READY. B/R without a prior handshake fail."""

    name = 'axi4_lite'

    def __init__ (self, prefix = '', pins = None, strict = True,
                  reset = None, reset_active = 1, max_outstanding = 4):
        super().__init__(prefix = prefix, pins = pins, strict = strict)
        self.reset = reset
        self.reset_active = reset_active
        self.max_outstanding = max_outstanding
        self.aw_pend = 0
        self.w_pend = 0
        self.b_pend = 0
        self.ar_pend = 0
        self.prev = {}

    def sample (self, sim, when):
        if when != 'edge':
            return
        if self._in_reset(sim):
            self.aw_pend = self.w_pend = self.b_pend = self.ar_pend = 0
            self.prev = {}
            return
        chans = {
            'aw': ('awvalid', 'awready', ('awaddr', 'awprot')),
            'w': ('wvalid', 'wready', ('wdata', 'wstrb')),
            'b': ('bvalid', 'bready', ('bresp',)),
            'ar': ('arvalid', 'arready', ('araddr', 'arprot')),
            'r': ('rvalid', 'rready', ('rdata', 'rresp')),
        }
        now = {}
        for name, (vkey, rkey, payload) in chans.items():
            valid = self.get(sim, vkey, 0)
            ready = self.get(sim, rkey, 0)
            pay = {p: self.get(sim, p, 0) for p in payload}
            now[name] = {'valid': valid, 'ready': ready, 'pay': pay}
            prev = self.prev.get(name)
            if prev and prev['valid'] and not prev['ready']:
                if not valid:
                    self.fail(sim, 'AXI-X01',
                              f'{vkey} dropped before {rkey}')
                for p in payload:
                    if prev['pay'].get(p) != pay.get(p):
                        self.fail(sim, 'AXI-X02',
                                  f'{p} changed while {vkey} held')
            if valid and ready:
                self._handshake(sim, name)

        self.prev = now

    def _handshake (self, sim, name):
        if name == 'aw':
            self.aw_pend += 1
            self._fold_write(sim)
        elif name == 'w':
            self.w_pend += 1
            self._fold_write(sim)
        elif name == 'b':
            if self.b_pend <= 0:
                self.fail(sim, 'AXI-X03',
                          'bvalid handshake with no outstanding write')
            else:
                self.b_pend -= 1
        elif name == 'ar':
            self.ar_pend += 1
            if self.ar_pend > self.max_outstanding:
                self.fail(sim, 'AXI-X05',
                          f'outstanding reads {self.ar_pend} '
                          f'> {self.max_outstanding}')
        elif name == 'r':
            if self.ar_pend <= 0:
                self.fail(sim, 'AXI-X04',
                          'rvalid handshake with no outstanding read')
            else:
                self.ar_pend -= 1

    def _fold_write (self, sim):
        while self.aw_pend and self.w_pend:
            self.aw_pend -= 1
            self.w_pend -= 1
            self.b_pend += 1
            if self.b_pend > self.max_outstanding:
                self.fail(sim, 'AXI-X05',
                          f'outstanding writes {self.b_pend} '
                          f'> {self.max_outstanding}')

    def _in_reset (self, sim):
        if self.reset is None:
            return False
        try:
            return int(sim.get(self.reset)) == self.reset_active
        except SimError:
            return False
