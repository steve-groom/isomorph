"""Avalon-MM handshake checker (Intel Avalon spec, IFACE_PLAN P0)."""
from .check import Check


class AvalonMm (Check):
    """Master-side sample of a memory-mapped Avalon port.

    pipelined=True: waitrequest + readdatavalid, outstanding reads.
    pipelined=False: accepted read completes the same edge.
    """

    name = 'avalon_mm'

    def __init__ (self, prefix = '', pins = None, strict = True,
                  pipelined = True, reset = None, reset_active = 1,
                  max_outstanding = 8, strict_be = False):
        super().__init__(prefix = prefix, pins = pins, strict = strict)
        self.pipelined = pipelined
        self.reset = reset
        self.reset_active = reset_active
        self.max_outstanding = max_outstanding
        self.strict_be = strict_be
        self.outstanding = 0
        self.prev_wait = 0
        self.prev_cmd = 0
        self.prev = {}

    def sample (self, sim, when):
        if when != 'edge':
            return
        if self.reset is not None:
            try:
                rst = int(sim.get(self.reset))
            except SimError:
                rst = 0
            if rst == self.reset_active:
                self.outstanding = 0
                self.prev_wait = 0
                self.prev_cmd = 0
                self.prev = {}
                return
        read = self.get(sim, 'read', 0)
        write = self.get(sim, 'write', 0)
        wait = self.get(sim, 'waitrequest', 0)
        addr = self.get(sim, 'address', 0)
        wdata = self.get(sim, 'writedata', 0)
        be = self.get(sim, 'byteenable', None)
        rdv = 0
        if self.pipelined and self.has(sim, 'readdatavalid'):
            rdv = self.get(sim, 'readdatavalid', 0)

        if read and write:
            self.fail(sim, 'AVMM-X01',
                      'read and write both asserted')

        cmd = 1 if (read or write) else 0
        if self.prev_wait and self.prev_cmd:
            for key, val in (('read', read), ('write', write),
                             ('address', addr), ('writedata', wdata),
                             ('byteenable', be)):
                if val is None:
                    continue
                if key in self.prev and self.prev[key] != val:
                    self.fail(sim, 'AVMM-X02',
                              f'{key} changed while waitrequest held')

        if write and self.strict_be and be is not None and be == 0:
            self.fail(sim, 'AVMM-X03',
                      'write with byteenable == 0')

        if self.pipelined and rdv:
            if self.outstanding <= 0:
                self.fail(sim, 'AVMM-X04',
                          'readdatavalid with no outstanding read')
            else:
                self.outstanding -= 1

        accepted = read and not wait
        if accepted:
            if self.pipelined:
                self.outstanding += 1
                if self.outstanding > self.max_outstanding:
                    self.fail(sim, 'AVMM-X05',
                              f'outstanding reads {self.outstanding} '
                              f'> {self.max_outstanding}')

        self.prev_wait = wait
        self.prev_cmd = cmd
        self.prev = {
            'read': read,
            'write': write,
            'address': addr,
            'writedata': wdata,
            'byteenable': be,
        }
