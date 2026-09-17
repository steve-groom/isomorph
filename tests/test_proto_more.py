"""Wishbone, AXI4-Lite and SPI protocol checkers."""
import itertools
import linecache
import textwrap
import unittest

from isomorph import signal, Simulator
from isomorph.ifaces import wishbone, axi4_lite, spi
from isomorph.proto import Wishbone, Axi4Lite, Spi, ProtocolError

_counter = itertools.count()


def load (src):
    name = f'<pr{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


DUMMY = textwrap.dedent('''\
    from isomorph import *
    @block
    def pins (i_clock, i_reset, i_a, i_b, i_c, o_q):
        @always_ff (i_clock.posedge)
        def dummy ():
            if (i_reset):
                o_q.next = 0
            else:
                o_q.next = i_a
        return instances()
''')


def sim_pins (backend = 'python'):
    ns = load(DUMMY)
    top = ns['pins'](
        i_clock = signal(), i_reset = signal(),
        i_a = signal(), i_b = signal(), i_c = signal(), o_q = signal())
    return Simulator(top, backend = backend)


class FactoryTests (unittest.TestCase):
    def test_wishbone_widths (self):
        bus = wishbone(width_a = 16, width_d = 32)
        self.assertEqual(len(bus.adr), 16)
        self.assertEqual(len(bus.dat_w), 32)
        self.assertEqual(len(bus.sel), 4)

    def test_axi_widths (self):
        bus = axi4_lite(width_a = 32, width_d = 64)
        self.assertEqual(len(bus.awaddr), 32)
        self.assertEqual(len(bus.wdata), 64)
        self.assertEqual(len(bus.wstrb), 8)

    def test_spi_pins (self):
        bus = spi()
        self.assertEqual(len(bus.ss_n), 1)
        self.assertEqual(len(bus.sclk), 1)


class WishboneTests (unittest.TestCase):
    def test_x01_stb_without_cyc (self):
        sim = sim_pins()
        sim.add_check(Wishbone(pins = {
            'cyc': 'i_a', 'stb': 'i_b', 'ack': 'i_c',
        }, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 0)
        sim.set('i_b', 1)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('WB-X01', str(ctx.exception))

    def test_x02_ack_without_stb (self):
        sim = sim_pins()
        sim.add_check(Wishbone(pins = {
            'cyc': 'i_a', 'stb': 'i_b', 'ack': 'i_c',
        }, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 1)
        sim.set('i_b', 0)
        sim.set('i_c', 1)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('WB-X02', str(ctx.exception))

    def test_legal_classic_cycle (self):
        sim = sim_pins()
        sim.add_check(Wishbone(pins = {
            'cyc': 'i_a', 'stb': 'i_b', 'ack': 'i_c',
        }, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 1)
        sim.set('i_b', 1)
        sim.set('i_c', 0)
        sim.tick()
        sim.set('i_c', 1)
        sim.tick()
        sim.set('i_a', 0)
        sim.set('i_b', 0)
        sim.set('i_c', 0)
        sim.tick()


class AxiTests (unittest.TestCase):
    def test_x01_valid_dropped (self):
        sim = sim_pins()
        sim.add_check(Axi4Lite(pins = {
            'awvalid': 'i_a', 'awready': 'i_b',
        }, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 1)
        sim.set('i_b', 0)
        sim.tick()
        sim.set('i_a', 0)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('AXI-X01', str(ctx.exception))

    def test_x03_b_without_write (self):
        sim = sim_pins()
        sim.add_check(Axi4Lite(pins = {
            'bvalid': 'i_a', 'bready': 'i_b',
        }, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 1)
        sim.set('i_b', 1)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('AXI-X03', str(ctx.exception))

    def test_x04_r_without_read (self):
        sim = sim_pins()
        sim.add_check(Axi4Lite(pins = {
            'rvalid': 'i_a', 'rready': 'i_b',
        }, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 1)
        sim.set('i_b', 1)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('AXI-X04', str(ctx.exception))

    def test_write_then_b_legal (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def wr (i_clock, i_reset, i_awv, i_awr, i_wv, i_wr,
                    i_bv, i_br, o_q):
                @always_ff (i_clock.posedge)
                def dummy ():
                    if (i_reset):
                        o_q.next = 0
                    else:
                        o_q.next = i_awv
                return instances()
        ''')
        ns = load(src)
        top = ns['wr'](
            i_clock = signal(), i_reset = signal(),
            i_awv = signal(), i_awr = signal(),
            i_wv = signal(), i_wr = signal(),
            i_bv = signal(), i_br = signal(), o_q = signal())
        sim = Simulator(top)
        sim.add_check(Axi4Lite(pins = {
            'awvalid': 'i_awv', 'awready': 'i_awr',
            'wvalid': 'i_wv', 'wready': 'i_wr',
            'bvalid': 'i_bv', 'bready': 'i_br',
        }, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_awv', 1)
        sim.set('i_awr', 1)
        sim.set('i_wv', 1)
        sim.set('i_wr', 1)
        sim.set('i_bv', 0)
        sim.set('i_br', 0)
        sim.tick()
        sim.set('i_awv', 0)
        sim.set('i_wv', 0)
        sim.set('i_bv', 1)
        sim.set('i_br', 1)
        sim.tick()


class SpiTests (unittest.TestCase):
    def test_x01_incomplete_byte (self):
        sim = sim_pins()
        sim.add_check(Spi(pins = {
            'ss_n': 'i_a', 'sclk': 'i_b',
        }, mode = 0, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 1)
        sim.set('i_b', 0)
        sim.tick()
        sim.set('i_a', 0)
        sim.tick()
        for _ in range(7):
            sim.set('i_b', 1)
            sim.tick()
            sim.set('i_b', 0)
            sim.tick()
        sim.set('i_a', 1)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('SPI-X01', str(ctx.exception))

    def test_complete_byte (self):
        sim = sim_pins()
        sim.add_check(Spi(pins = {
            'ss_n': 'i_a', 'sclk': 'i_b',
        }, mode = 0, reset = 'i_reset'))
        sim.set('i_reset', 0)
        sim.set('i_a', 1)
        sim.set('i_b', 0)
        sim.tick()
        sim.set('i_a', 0)
        sim.tick()
        for _ in range(8):
            sim.set('i_b', 1)
            sim.tick()
            sim.set('i_b', 0)
            sim.tick()
        sim.set('i_a', 1)
        sim.tick()


if __name__ == '__main__':
    unittest.main()
