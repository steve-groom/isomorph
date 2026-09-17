"""Optional hardware, as an if ... generate (when()).

An instance built inside a plain Python if either exists or does not,
and two builds of the block that differ in it are two module bodies
the emitter has to tell apart by name. when() records the condition
instead of letting Python answer it: the HDL puts the instance in an
if ... generate, so the one module covers both builds, and the three
simulators leave it out when the condition is false.

Written against an rtl() parameter, which is the pairing it is for.
"""
import os
import shutil
import tempfile
import unittest

from isomorph import (analyse, emit_sv, emit_vhdl, rtl, when, signal,
                      block, always_comb, instances, Simulator)
from isomorph.emit_sv import lint_sv
from isomorph.emit_vhdl import lint_vhdl

HAVE_GHDL = shutil.which('ghdl') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)


@block
def unit (i_a, o_y):
    @always_comb
    def unit_logic ():
        o_y.next = i_a

    return instances()


@block
def top (i_a, o_y, HAS_UNIT = rtl(True)):
    with when(HAS_UNIT):
        inst_unit = unit(i_a = i_a, o_y = o_y)

    return instances()


def build (**kw):
    return top(i_a = signal(), o_y = signal(), **kw)


def written (text, name, suffix):
    directory = tempfile.mkdtemp(prefix = 'iso_when_')
    path = os.path.join(directory, name + suffix)
    with open(path, 'w', encoding = 'ascii') as f:
        f.write(text)
    return path


class WhenGenerateTests (unittest.TestCase):

    def setUp (self):
        self.sv = emit_sv(analyse(build())[0])
        self.vhdl = emit_vhdl(analyse(build())[0])
        self.off = emit_sv(analyse(build(HAS_UNIT = rtl(False)))[0])

    def test_the_instance_is_a_generate (self):
        self.assertIn('if (HAS_UNIT) begin : g_inst_unit', self.sv)
        self.assertIn('unit inst_unit (', self.sv)

    def test_vhdl_says_it_the_same_way (self):
        self.assertIn('g_inst_unit : if HAS_UNIT /= 0 generate',
                      self.vhdl)
        self.assertIn('end generate;', self.vhdl)

    def test_the_build_that_leaves_it_out_says_so_too (self):
        """Both builds are the same text, which is what makes them one
        module rather than two with invented names."""
        self.assertIn('if (HAS_UNIT) begin : g_inst_unit', self.off)
        self.assertNotIn('top_HAS_UNIT', self.off)

    def test_the_condition_is_a_parameter (self):
        self.assertIn('parameter HAS_UNIT = 1', self.sv)
        self.assertIn('parameter HAS_UNIT = 0', self.off)

    def test_the_simulator_runs_it_when_it_is_there (self):
        sim = Simulator(build())
        sim.set('i_a', 1)
        sim.eval()
        self.assertEqual(sim.get('o_y'), 1)

    def test_and_leaves_it_out_when_it_is_not (self):
        sim = Simulator(build(HAS_UNIT = rtl(False)))
        sim.set('i_a', 1)
        sim.eval()
        self.assertEqual(sim.get('o_y'), 0)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        lint_vhdl(written(self.vhdl, 'top', '.vhd'))

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_lints_it (self):
        lint_sv(written(self.sv, 'top', '.sv'), top = 'top')

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_lints_the_build_without_it (self):
        lint_sv(written(self.off, 'top', '.sv'), top = 'top')


if (__name__ == '__main__'):
    unittest.main()
