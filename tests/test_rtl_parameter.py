"""A parameter the emitted HDL keeps as a parameter (rtl()).

A condition on an ordinary parameter is answered while the design
elaborates and the branch it does not take never reaches the IR, so
two values of it are two module bodies and the emitter has to tell
them apart by name. That is right for a width-dependent branch, where
the arm that was not taken is often only well formed at the other
width, and wrong for a flag that selects a feature: there the two
builds want to be one module, instantiated with different values.

rtl() marks the second kind. The condition survives into the HDL, the
bodies come out the same text, and merge_builds unifies them the way
it already unified a block built at two widths.
"""
import os
import shutil
import tempfile
import unittest

from isomorph import (analyse, emit_sv, emit_vhdl, rtl, signal, block,
                      always_comb, instances)
from isomorph.emit_sv import lint_sv
from isomorph.emit_vhdl import lint_vhdl

HAVE_GHDL = shutil.which('ghdl') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)


@block
def core (i_a, o_y, HAS_M = rtl(True)):
    @always_comb
    def core_logic ():
        o_y.next = False
        if (HAS_M):
            o_y.next = i_a

    return instances()


@block
def folded (i_a, o_y, HAS_M = True):
    @always_comb
    def folded_logic ():
        o_y.next = False
        if (HAS_M):
            o_y.next = i_a

    return instances()


@block
def pair (i_a, o_big, o_lean):
    inst_big = core(i_a = i_a, o_y = o_big, HAS_M = rtl(True))
    inst_lean = core(i_a = i_a, o_y = o_lean, HAS_M = rtl(False))

    return instances()


def written (text, name, suffix):
    directory = tempfile.mkdtemp(prefix = 'iso_rtl_')
    path = os.path.join(directory, name + suffix)
    with open(path, 'w', encoding = 'ascii') as f:
        f.write(text)
    return path


class RtlParameterTests (unittest.TestCase):

    def setUp (self):
        self.modules = analyse(pair(i_a = signal(), o_big = signal(),
                                    o_lean = signal()))[0]
        self.sv = emit_sv(self.modules)
        self.vhdl = emit_vhdl(self.modules)

    def test_two_builds_are_one_module (self):
        self.assertEqual(self.sv.count('module core'), 1)
        self.assertEqual(self.vhdl.count('entity core is'), 1)

    def test_no_build_is_named_after_its_value (self):
        """The whole point: a name says what the block is, and what it
        was built with is on the instance."""
        self.assertNotIn('core_HAS_M', self.sv)
        self.assertNotIn('core_HAS_M', self.vhdl)

    def test_the_value_is_on_the_instance (self):
        self.assertIn('core #(.HAS_M(0)) inst_lean', self.sv)
        self.assertIn('HAS_M => 0', self.vhdl)

    def test_the_default_instance_says_nothing (self):
        self.assertIn('core inst_big', self.sv)

    def test_the_condition_reaches_the_hdl (self):
        self.assertIn('if (HAS_M) begin', self.sv)

    def test_an_integer_generic_is_true_when_it_is_not_zero (self):
        """Narrowing it to one bit first would answer for a value of
        two with the bit it kept."""
        self.assertIn('if HAS_M /= 0 then', self.vhdl)
        self.assertNotIn('to_unsigned(HAS_M, 1)', self.vhdl)

    def test_a_plain_parameter_still_folds (self):
        """A width-dependent branch keeps the arm it took and drops the
        other, which is what lets the other one be ill formed."""
        modules = analyse(folded(i_a = signal(), o_y = signal(),
                                 HAS_M = False))[0]
        text = emit_sv(modules)
        self.assertNotIn('HAS_M', text)
        self.assertNotIn('o_y = i_a', text)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        lint_vhdl(written(self.vhdl, 'pair', '.vhd'))

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_lints_it (self):
        lint_sv(written(self.sv, 'pair', '.sv'), top = 'pair')


if (__name__ == '__main__'):
    unittest.main()
