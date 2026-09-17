"""Avalon-MM protocol checker (IFACE_PLAN I1)."""
import itertools
import linecache
import textwrap
import unittest

from isomorph import signal, Simulator
from isomorph.ifaces import avalon_mm
from isomorph.proto import AvalonMm, ProtocolError

_counter = itertools.count()


def load (src):
    name = f'<av{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


BOTH = textwrap.dedent('''\
    from isomorph import *
    @block
    def both (i_clock, i_reset, o_read, o_write, o_address):
        q = signal()
        @always_comb
        def drive ():
            o_read.next = True
            o_write.next = True
            o_address.next = 0
        @always_ff (i_clock.posedge)
        def dummy ():
            if (i_reset):
                q.next = 0
            else:
                q.next = 1
        return instances()
''')

RDV = textwrap.dedent('''\
    from isomorph import *
    @block
    def rdv_only (i_clock, i_reset, o_read, o_write, i_rdv):
        q = signal()
        @always_comb
        def drive ():
            o_read.next = False
            o_write.next = False
        @always_ff (i_clock.posedge)
        def dummy ():
            if (i_reset):
                q.next = 0
            else:
                q.next = 1
        return instances()
''')

HOLD = textwrap.dedent('''\
    from isomorph import *
    @block
    def hold (i_clock, i_reset, i_wait, i_addr, o_read, o_write, o_address):
        q = signal()
        @always_comb
        def drive ():
            o_read.next = True
            o_write.next = False
            o_address.next = i_addr
        @always_ff (i_clock.posedge)
        def dummy ():
            if (i_reset):
                q.next = 0
            else:
                q.next = 1
        return instances()
''')


def make_both (backend):
    ns = load(BOTH)
    top = ns['both'](
        i_clock = signal(), i_reset = signal(),
        o_read = signal(), o_write = signal(), o_address = signal(8))
    sim = Simulator(top, backend = backend)
    sim.add_check(AvalonMm(
        pins = {
            'read': 'o_read',
            'write': 'o_write',
            'address': 'o_address',
            'waitrequest': None,
            'readdatavalid': None,
        },
        pipelined = False,
        reset = 'i_reset'))
    return sim


class AvalonFactoryTests (unittest.TestCase):
    def test_widths (self):
        bus = avalon_mm(WIDTHA = 8, WIDTHD = 16)
        self.assertEqual(len(bus.address), 8)
        self.assertEqual(len(bus.writedata), 16)
        self.assertEqual(len(bus.readdata), 16)
        self.assertEqual(len(bus.byteenable), 2)
        self.assertEqual(len(bus.read), 1)


class AvalonCheckTests (unittest.TestCase):
    def test_x01_read_and_write_python (self):
        sim = make_both('python')
        sim.set('i_reset', 0)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('AVMM-X01', str(ctx.exception))

    def test_x01_read_and_write_c99 (self):
        sim = make_both('c99')
        sim.set('i_reset', 0)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('AVMM-X01', str(ctx.exception))

    def test_x04_rdv_without_read (self):
        ns = load(RDV)
        top = ns['rdv_only'](
            i_clock = signal(), i_reset = signal(),
            o_read = signal(), o_write = signal(), i_rdv = signal())
        sim = Simulator(top, backend = 'python')
        sim.add_check(AvalonMm(
            pins = {
                'read': 'o_read',
                'write': 'o_write',
                'readdatavalid': 'i_rdv',
                'waitrequest': None,
            },
            pipelined = True,
            reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_rdv', 1)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('AVMM-X04', str(ctx.exception))

    def test_x02_hold_under_waitrequest (self):
        ns = load(HOLD)
        top = ns['hold'](
            i_clock = signal(), i_reset = signal(),
            i_wait = signal(), i_addr = signal(8),
            o_read = signal(), o_write = signal(), o_address = signal(8))
        sim = Simulator(top, backend = 'python')
        sim.add_check(AvalonMm(
            pins = {
                'read': 'o_read',
                'write': 'o_write',
                'address': 'o_address',
                'waitrequest': 'i_wait',
                'readdatavalid': None,
            },
            pipelined = False,
            reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_wait', 1)
        sim.set('i_addr', 4)
        sim.tick()
        sim.set('i_addr', 8)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('AVMM-X02', str(ctx.exception))

    def test_reset_suppresses_x01 (self):
        sim = make_both('python')
        sim.set('i_reset', 1)
        sim.tick(2)
        self.assertEqual(sim.events(), [])

    def test_strict_false_records (self):
        ns = load(BOTH)
        top = ns['both'](
            i_clock = signal(), i_reset = signal(),
            o_read = signal(), o_write = signal(), o_address = signal(8))
        sim = Simulator(top, backend = 'python')
        chk = AvalonMm(
            pins = {
                'read': 'o_read',
                'write': 'o_write',
                'address': 'o_address',
                'waitrequest': None,
                'readdatavalid': None,
            },
            pipelined = False,
            reset = 'i_reset',
            strict = False)
        sim.add_check(chk)
        sim.set('i_reset', 0)
        sim.tick()
        self.assertTrue(chk.violations)
        self.assertEqual(chk.violations[0]['rule'], 'AVMM-X01')
        kinds = [e['kind'] for e in sim.events()]
        self.assertIn('protocol', kinds)


if __name__ == '__main__':
    unittest.main()
