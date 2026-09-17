"""Every pin factory in isomorph.ifaces builds, elaborates as a block
port bundle, and converts to SystemVerilog and VHDL."""
import os
import tempfile
import unittest
from types import SimpleNamespace

from isomorph import (block, signal, always_ff, always_comb, instances,
                      analyse, write_sv, write_vhdl)
from isomorph import ifaces


def members (bundle):
    return {k: v for k, v in vars(bundle).items()
            if hasattr(v, 'width')}


@block
def loopback (i_clock, bus):
    """Register every input member of a bundle into an output member of
    the same width where one exists; else just read it."""
    ins = [v for v in members(bus).values()]
    acc = signal(max(len(s) for s in ins))

    @always_ff (i_clock.posedge)
    def ff ():
        acc.next = ins[0]

    return instances()


class FactoryTests (unittest.TestCase):
    def test_avalon_mm_widths (self):
        bus = ifaces.avalon_mm(WIDTHA = 8, WIDTHD = 32)
        self.assertEqual(len(bus.address), 8)
        self.assertEqual(len(bus.byteenable), 4)
        self.assertEqual(len(bus.lock), 1)

    def test_avalon_mm_no_byteenable_for_bytes (self):
        bus = ifaces.avalon_mm(WIDTHA = 8, WIDTHD = 8)
        self.assertFalse(hasattr(bus, 'byteenable'))
        bus = ifaces.avalon_mm(WIDTHA = 8, WIDTHD = 9)
        self.assertEqual(len(bus.byteenable), 1)

    def test_no_pin_carries_a_power_on_value (self):
        """A chip select is active low and idles high, and it is the
        design that holds it there.

        These factories used to declare it, which set the value in two
        simulators of three and in no emitted HDL. A pin that has to be
        high before the first clock is driven high in a reset branch."""
        self.assertFalse(hasattr(ifaces.spi().ss_n, 'reset'))
        self.assertFalse(hasattr(ifaces.spi(tristate = True).ss_n, 'reset'))
        self.assertFalse(hasattr(ifaces.uart().txd, 'reset'))
        self.assertFalse(hasattr(ifaces.sdram().cs_n, 'reset'))
        self.assertFalse(hasattr(ifaces.hyperram().cs_n, 'reset'))

    def test_one_spi_factory_covers_both_ends (self):
        """A master and a slave carry the same four pins.

        There used to be spim, spis and spis_tri. The first two were
        the same four signals under two names -- direction here comes
        from what drives what, not from the factory -- and the third
        differed only in splitting miso. They were also the only pin
        factories calling the select ss_n, which the SPI checker does
        not look for, so attaching it by prefix to one of them read a
        select that was never there and passed everything.
        """
        bus = ifaces.spi()
        self.assertEqual(sorted(vars(bus)), ['miso', 'mosi', 'sclk', 'ss_n'])
        tri = ifaces.spi(tristate = True)
        self.assertEqual(sorted(vars(tri)),
                         ['miso_i', 'miso_o', 'miso_oe', 'mosi', 'sclk',
                          'ss_n'])
        for gone in ('spim', 'spis', 'spis_tri'):
            self.assertFalse(hasattr(ifaces, gone), gone)

    def test_the_language_package_knows_no_part_numbers (self):
        """Bus standards belong here; a particular chip does not.

        adc121s021 and bga7204 are a Texas Instruments ADC and an
        Infineon front end. A part that puts a second data line on SPI
        has an interface of its own, and it belongs to the part rather
        than to SPI. All of them live in the design tree that uses
        them."""
        for name in ('adc121s021', 'bga7204'):
            self.assertFalse(hasattr(ifaces, name), name)

    def test_sdram_params_ride_along (self):
        d = ifaces.sdram(DATA_WIDTH = 8, CAS_LATENCY = 2)
        self.assertEqual(d.CAS_LATENCY, 2)
        self.assertEqual(len(d.dqm), 1)
        self.assertEqual(len(d.dq_o), 8)

    def test_every_factory_converts (self):
        factories = [getattr(ifaces, n) for n in ifaces.__all__]
        with tempfile.TemporaryDirectory() as tmp:
            for f in factories:
                with self.subTest(f.__name__):
                    top = loopback(i_clock = signal(), bus = f())
                    modules, warnings = analyse(top)
                    write_sv(modules, os.path.join(tmp, f.__name__ + '.sv'))
                    write_vhdl(modules, os.path.join(tmp, f.__name__ + '.vhd'))
                    text = open(os.path.join(tmp, f.__name__ + '.sv')).read()
                    for name in members(f()):
                        self.assertIn('bus_' + name, text)


if __name__ == '__main__':
    unittest.main()
