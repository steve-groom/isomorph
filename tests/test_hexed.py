"""A parameter that is a bit pattern rather than a count (hexed()).

A default written 0xC0FFEE carries its base in the source and the
emitters read it there. One a function worked out carries nothing:
four characters packed into a word arrive as 1094861636, which nobody
decodes back to what was typed. hexed() says the value is a word of a
stated width, and a word is a vector in both languages, written in
hex and read by name with no conversion round it.

A parameter that really is a number stays an integer, which is the
other half of the same idea: an index has to be integer arithmetic.
"""
import os
import shutil
import tempfile
import unittest

from isomorph import (analyse, emit_sv, emit_vhdl, hexed, signal,
                      block, always_ff, instances)
from isomorph.emit_sv import lint_sv
from isomorph.emit_vhdl import lint_vhdl

HAVE_GHDL = shutil.which('ghdl') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)


def packed (text):
    word = 0
    for char in text:
        word = ((word << 8) | ord(char))
    return word


@block
def ident (i_clock, o_id, o_base, TAG = hexed(packed('ABCD'), 32),
           ADDRESS = 0xFF00):
    @always_ff (i_clock.posedge)
    def ident_logic ():
        o_id.next = TAG
        o_base.next = ADDRESS

    return instances()


def build (**kw):
    return ident(i_clock = signal(), o_id = signal(32),
                 o_base = signal(16), **kw)


def written (text, suffix):
    directory = tempfile.mkdtemp(prefix = 'iso_hexed_')
    path = os.path.join(directory, 'ident' + suffix)
    with open(path, 'w', encoding = 'ascii') as f:
        f.write(text)
    return path


class HexedParameterTests (unittest.TestCase):

    def setUp (self):
        self.modules = analyse(build())[0]
        self.sv = emit_sv(self.modules)
        self.vhdl = emit_vhdl(self.modules)

    def test_the_vhdl_generic_is_a_vector (self):
        self.assertIn('TAG : std_logic_vector(31 downto 0) '
                      ':= x"41424344"', self.vhdl)

    def test_the_sv_parameter_is_packed (self):
        self.assertIn("parameter logic [31:0] TAG = 32'h41424344",
                      self.sv)

    def test_reading_it_needs_no_conversion (self):
        """It is already the width the port is, and saying so twice is
        noise a reviewer has to read past."""
        self.assertIn('o_id <= TAG;', self.vhdl)
        self.assertIn('o_id <= TAG;', self.sv)
        self.assertNotIn('resize(unsigned(TAG)', self.vhdl)
        self.assertNotIn("32'(TAG)", self.sv)

    def test_an_ordinary_parameter_is_still_an_integer (self):
        self.assertIn('ADDRESS : integer := 16#FF00#', self.vhdl)
        self.assertIn("parameter ADDRESS = 'hFF00", self.sv)

    def test_a_width_nobody_gave_is_the_word_it_fills (self):
        modules = analyse(build(TAG = hexed(packed('ABCD'))))[0]
        self.assertIn('TAG : std_logic_vector(31 downto 0)',
                      emit_vhdl(modules))

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        lint_vhdl(written(self.vhdl, '.vhd'))

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_lints_it (self):
        lint_sv(written(self.sv, '.sv'), top = 'ident')


if (__name__ == '__main__'):
    unittest.main()
