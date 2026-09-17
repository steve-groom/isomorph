"""Timing constraints, from the clocks and the crossings.

A supplement to whatever already defines the clocks, carrying the one
thing no vendor tool can work out for itself: which signals cross
between domains, which needs the design analysed.

The dialects differ, so the vendor is named rather than guessed. The
Quartus output has been through quartus_sta on a MAX 10, which read
it with no errors and no critical warnings, created both clocks, and
analysed only same-clock transfers, meaning the cuts took.
"""
import os
import shutil
import tempfile
import unittest

from isomorph import (analyse, block, signal, always_ff, always_comb,
                      attr, clock, concat, instances, emit_sdc, write_sdc,
                      IsomorphError, ConversionError, VENDORS)
from isomorph.entry import parse


def dual (period_a = 20e-9, period_b = 31e-9, declare = True,
          synchronised = True):
    @block
    def dc (i_wr_clock, i_rd_clock, i_d, o_q, o_flag):
        if declare:
            clock(i_wr_clock, period = period_a)
            clock(i_rd_clock, period = period_b)
        wrptr = signal()
        sync = signal(2)
        if synchronised:
            attr(sync, async_reg = 'TRUE')

        @always_ff (i_wr_clock.posedge)
        def wr_logic ():
            wrptr.next = (not wrptr)

        @always_ff (i_rd_clock.posedge)
        def sync_logic ():
            sync.next = concat(sync[0], wrptr)

        @always_comb
        def out_comb ():
            o_q.next = i_d
            o_flag.next = sync[1]

        return instances()

    return dc(i_wr_clock = signal(), i_rd_clock = signal(),
              i_d = signal(8), o_q = signal(8), o_flag = signal())


def one_clock ():
    @block
    def single (i_clock, i_d, o_q):
        clock(i_clock, period = 10e-9)

        @always_ff (i_clock.posedge)
        def hold_logic ():
            o_q.next = i_d

        return instances()

    return single(i_clock = signal(), i_d = signal(8), o_q = signal(8))


class ClockDeclarationTests (unittest.TestCase):
    def test_a_period_is_seconds_and_reaches_the_port (self):
        modules, _ = analyse(one_clock())
        port = [p for p in modules[-1].ports if p.name == 'i_clock'][0]
        self.assertAlmostEqual(port.period_ns, 10.0)

    def test_a_period_must_be_positive (self):
        with self.assertRaises(IsomorphError) as ctx:
            clock(signal(), period = 0)
        self.assertIn('positive', str(ctx.exception))

    def test_it_takes_a_signal (self):
        with self.assertRaises(IsomorphError):
            clock('i_clock', period = 20e-9)

    def test_nothing_in_the_hdl_changes (self):
        """A period is for the constraints and for nothing else."""
        from isomorph import emit_sv
        with_period = emit_sv(analyse(one_clock())[0])

        @block
        def single (i_clock, i_d, o_q):
            @always_ff (i_clock.posedge)
            def hold_logic ():
                o_q.next = i_d
            return instances()

        without = emit_sv(analyse(single(
            i_clock = signal(), i_d = signal(8), o_q = signal(8)))[0])
        self.assertEqual(with_period, without)


class VendorTests (unittest.TestCase):
    def test_an_unknown_vendor_is_refused (self):
        with self.assertRaises(ConversionError) as ctx:
            emit_sdc(analyse(one_clock())[0], 'lattice')
        self.assertIn('dialects differ', str(ctx.exception))

    def test_every_named_vendor_writes_something (self):
        modules, _ = analyse(dual())
        for vendor in VENDORS:
            with self.subTest(vendor = vendor):
                text = emit_sdc(modules, vendor)
                self.assertIn(f'for {vendor}', text)
                self.assertIn('create_clock', text)

    def test_uncertainty_is_quartus_only (self):
        modules, _ = analyse(dual())
        self.assertIn('derive_clock_uncertainty',
                      emit_sdc(modules, 'quartus'))
        self.assertNotIn('derive_clock_uncertainty',
                         emit_sdc(modules, 'vivado'))
        self.assertNotIn('derive_clock_uncertainty',
                         emit_sdc(modules, 'efinity'))


class ClockTests (unittest.TestCase):
    def test_the_declared_period_is_in_nanoseconds (self):
        text = emit_sdc(analyse(dual())[0], 'quartus')
        self.assertIn('-period 20.000', text)
        self.assertIn('-period 31.000', text)
        self.assertIn('[get_ports {i_wr_clock}]', text)

    def test_nothing_is_created_when_nothing_is_declared (self):
        """The usual case on a board whose vendor tool defines them."""
        text = emit_sdc(analyse(dual(declare = False))[0], 'quartus')
        self.assertNotIn('create_clock', text)
        self.assertIn('No port declared a period', text)


class CrossingTests (unittest.TestCase):
    def test_the_crossing_is_listed_and_cut (self):
        text = emit_sdc(analyse(dual())[0], 'quartus')
        self.assertIn('wrptr', text)
        self.assertIn('i_wr_clock -> i_rd_clock', text)
        self.assertIn('set_false_path -from [get_clocks {i_wr_clock}] '
                      '-to [get_clocks {i_rd_clock}]', text)
        self.assertIn('set_false_path -from [get_clocks {i_rd_clock}] '
                      '-to [get_clocks {i_wr_clock}]', text)

    def test_the_group_alternative_is_offered_not_taken (self):
        """A group asserts the clocks are unrelated, which only the
        author knows, so the line is a comment."""
        text = emit_sdc(analyse(dual())[0], 'quartus')
        for line in text.splitlines():
            if 'set_clock_groups' in line:
                self.assertTrue(line.strip().startswith('#'), line)

    def test_an_unsynchronised_crossing_is_named_and_not_cut (self):
        """Cutting the timing on a crossing with no synchroniser would
        hide it, so it is listed and left alone."""
        modules, warnings = analyse(dual(synchronised = False),
                                    allow_severe = True)
        text = emit_sdc(modules, 'quartus')
        self.assertIn('NO SYNCHRONISER', text)
        self.assertNotIn('set_false_path', text)
        self.assertTrue(any('crossing' in w for w in warnings))

    def test_a_single_clock_design_has_none (self):
        text = emit_sdc(analyse(one_clock())[0], 'quartus')
        self.assertIn('The design has none', text)
        self.assertNotIn('set_false_path', text)


class CommandLineTests (unittest.TestCase):
    def test_the_vendor_is_required (self):
        with self.assertRaises(IsomorphError) as ctx:
            parse(['--sdc'])
        self.assertIn('names a vendor', str(ctx.exception))

    def test_an_unknown_vendor_is_refused (self):
        with self.assertRaises(IsomorphError):
            parse(['--sdc', 'lattice'])

    def test_it_counts_as_writing (self):
        opts = parse(['--sdc', 'quartus'])
        self.assertEqual(opts.sdc, 'quartus')
        self.assertTrue(opts.writing)
        self.assertFalse(opts.run)


class FileTests (unittest.TestCase):
    def test_it_writes_where_it_is_told (self):
        modules, _ = analyse(dual())
        directory = tempfile.mkdtemp(prefix = 'iso_sdc_')
        try:
            path = os.path.join(directory, 'dc.sdc')
            write_sdc(modules, path, 'quartus')
            with open(path) as handle:
                self.assertIn('create_clock', handle.read())
        finally:
            shutil.rmtree(directory, ignore_errors = True)


if __name__ == '__main__':
    unittest.main()
