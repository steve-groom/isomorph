"""An array of instances is one generate.

A loop over instances used to become cells_0, cells_1 and so on: names
nobody typed, which is the MyHDL behaviour this project was a reaction
to, and which it forbids. They are cells[0], cells[1] now, the two
HDL emitters write them back as one labelled generate, and an array too
irregular to be a generate is a conversion error naming what varies.

The SystemVerilog spelling is measured, not assumed. Quartus Prime
Standard 25.1 rejects a genvar declared inside the loop header, which
is the compact form every other tool takes, so the declaration goes at
module level and the loop is wrapped in generate / endgenerate.
"""
import itertools
import linecache
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

from isomorph import (analyse, emit_sv, emit_vhdl, emit_c99, signal,
                      signals, ConversionError, write_sv_files,
                      write_vhdl_files)
from isomorph.emit_sv import lint_sv
from isomorph.emit_vhdl import lint_vhdl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lockstep import Lockstep, available            # noqa: E402

CONSTRUCTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'constructs')
sys.path.insert(0, CONSTRUCTS)

from lane_array import elaborate_lane_array          # noqa: E402

HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)
HAVE_GHDL = shutil.which('ghdl') is not None

_counter = itertools.count()


def load (src):
    name = f'<arr{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


LEAF = '''\
from isomorph import *
@block
def leaf (i_clock, i_d, o_q, WIDTH = 8):
    @always_ff (i_clock.posedge)
    def leaf_logic ():
        o_q.next = i_d
    return instances()
'''


def build (body, **ports):
    """Elaborate. The array check runs in analyse(), so a test that
    wants the error calls checked() instead."""
    ns = load(LEAF + textwrap.dedent(body))
    return ns['unit'](**ports)


def checked (body, **ports):
    return analyse(build(body, **ports))


class NamingTests (unittest.TestCase):
    def test_members_are_named_by_index (self):
        modules, warnings = analyse(elaborate_lane_array())
        self.assertEqual(warnings, [])
        top = modules[-1]
        self.assertEqual([i.name for i in top.instances],
                         ['cells[0]', 'cells[1]', 'cells[2]', 'cells[3]'])
        self.assertTrue(all(i.array == 'cells' for i in top.instances))

    def test_nothing_is_called_cells_0 (self):
        """The invented name this item exists to remove."""
        modules, _ = analyse(elaborate_lane_array())
        for text in (emit_sv(modules), emit_vhdl(modules)):
            self.assertNotIn('cells_0', text)
            self.assertNotIn('cells_1', text)


class SystemVerilogTests (unittest.TestCase):
    def text (self):
        return emit_sv(analyse(elaborate_lane_array())[0])

    def test_one_generate_with_a_module_level_genvar (self):
        text = self.text()
        self.assertIn('    genvar k;', text)
        self.assertIn('    generate', text)
        self.assertIn('for (k = 0; k < 4; k++) begin : cells', text)
        self.assertIn('    endgenerate', text)
        self.assertEqual(text.count('begin : cells'), 1)

    def test_the_genvar_is_not_declared_inside_the_loop (self):
        """Quartus Prime Standard will not parse that form."""
        self.assertNotIn('for (genvar', self.text())

    def test_a_shared_port_is_shared_and_an_array_is_indexed (self):
        text = self.text()
        self.assertIn('.i_clock(i_clock)', text)
        self.assertIn('.i_d(i_data[k])', text)
        self.assertIn('.o_q(o_data[k])', text)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_lints (self):
        modules, _ = analyse(elaborate_lane_array())
        directory = tempfile.mkdtemp(prefix = 'iso_arr_sv_')
        try:
            written = write_sv_files(modules, directory)
            lint_sv([p for p in written if p.endswith('.sv')],
                    top = 'lane_array')
        finally:
            shutil.rmtree(directory, ignore_errors = True)


class VhdlTests (unittest.TestCase):
    def text (self):
        return emit_vhdl(analyse(elaborate_lane_array())[0])

    def test_one_for_generate (self):
        text = self.text()
        self.assertIn('cells : for k in 0 to 3 generate', text)
        self.assertIn('inst : entity work.lane', text)
        self.assertIn('i_d => i_data(k)', text)
        self.assertIn('end generate;', text)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_lints (self):
        modules, _ = analyse(elaborate_lane_array())
        directory = tempfile.mkdtemp(prefix = 'iso_arr_vhd_')
        try:
            written = write_vhdl_files(modules, directory)
            lint_vhdl([p for p in written if p.endswith('.vhd')])
        finally:
            shutil.rmtree(directory, ignore_errors = True)


class C99Tests (unittest.TestCase):
    def test_one_array_of_child_structs (self):
        header, _ = emit_c99(analyse(elaborate_lane_array())[0])
        self.assertIn('    lane cells[4];', header)
        self.assertNotIn('lane cells[0];', header)


class BackendTests (unittest.TestCase):
    def test_every_backend_agrees_every_cycle (self):
        backends = available()
        if len(backends) < 2:
            self.skipTest('needs at least two backends')
        step = Lockstep(elaborate_lane_array, backends)
        try:
            step.reset('i_reset')
            for cycle in range(60):
                for k in range(4):
                    step.set(f'i_data[{k}]', (cycle * 7 + k * 31) & 0xff)
                step.eval()
                step.tick(1)
        finally:
            step.close()


class IrregularTests (unittest.TestCase):
    """An array that is not one child repeated over one index is an
    error, and the message says which port broke it."""

    def test_a_port_taking_unrelated_wires (self):
        with self.assertRaises(ConversionError) as ctx:
            checked('''
                @block
                def unit (i_clock, i_a, o_0, o_1):
                    outs = [o_0, o_1]
                    cells = []
                    for k in range(2):
                        cells.append(leaf(i_clock = i_clock, i_d = i_a,
                                          o_q = outs[k]))
                    return instances()
            ''', i_clock = signal(), i_a = signal(8), o_0 = signal(8),
                 o_1 = signal(8))
        self.assertIn('cells[k].o_q', str(ctx.exception))
        self.assertIn('its own name', str(ctx.exception))

    def test_two_different_children (self):
        with self.assertRaises(ConversionError) as ctx:
            checked('''
                @block
                def other (i_clock, i_d, o_q):
                    @always_comb
                    def other_comb ():
                        o_q.next = i_d
                    return instances()
                @block
                def unit (i_clock, i_data, o_data):
                    cells = []
                    cells.append(leaf(i_clock = i_clock, i_d = i_data[0],
                                      o_q = o_data[0]))
                    cells.append(other(i_clock = i_clock, i_d = i_data[1],
                                       o_q = o_data[1]))
                    return instances()
            ''', i_clock = signal(), i_data = signals(2, 8),
                 o_data = signals(2, 8))
        self.assertIn('holds a', str(ctx.exception))

    def test_two_parameter_sets (self):
        """A parameter that differs specialises the child, so the two
        members are two modules and the message says so."""
        with self.assertRaises(ConversionError) as ctx:
            checked('''
                @block
                def unit (i_clock, i_data, o_data):
                    cells = []
                    for k in range(2):
                        cells.append(leaf(i_clock = i_clock,
                                          i_d = i_data[k],
                                          o_q = o_data[k],
                                          WIDTH = 8 + k))
                    return instances()
            ''', i_clock = signal(), i_data = signals(2, 8),
                 o_data = signals(2, 8))
        self.assertIn('holds a leaf and a leaf_WIDTH_9',
                      str(ctx.exception))

    def test_an_index_out_of_order (self):
        """Element k has to be the member's own index."""
        with self.assertRaises(ConversionError) as ctx:
            checked('''
                @block
                def unit (i_clock, i_data, o_data):
                    cells = []
                    for k in range(2):
                        cells.append(leaf(i_clock = i_clock,
                                          i_d = i_data[1 - k],
                                          o_q = o_data[k]))
                    return instances()
            ''', i_clock = signal(), i_data = signals(2, 8),
                 o_data = signals(2, 8))
        self.assertIn('cells[k].i_d', str(ctx.exception))


class GenvarTests (unittest.TestCase):
    def test_a_module_that_already_calls_something_k (self):
        """The genvar is a module-level name, so it dodges the ones
        already there rather than shadowing them."""
        top = build('''
            @block
            def unit (i_clock, i_data, o_data):
                k = 3
                cells = []
                for n in range(2):
                    cells.append(leaf(i_clock = i_clock, i_d = i_data[n],
                                      o_q = o_data[n]))
                return instances()
        ''', i_clock = signal(), i_data = signals(2, 8),
             o_data = signals(2, 8))
        text = emit_sv(analyse(top)[0])
        self.assertIn('genvar k0;', text)
        self.assertIn('for (k0 = 0; k0 < 2; k0++)', text)

    def test_two_arrays_take_two_genvars (self):
        top = build('''
            @block
            def unit (i_clock, i_data, o_data, o_more):
                first = []
                second = []
                for n in range(2):
                    first.append(leaf(i_clock = i_clock, i_d = i_data[n],
                                      o_q = o_data[n]))
                for n in range(2):
                    second.append(leaf(i_clock = i_clock, i_d = i_data[n],
                                       o_q = o_more[n]))
                return instances()
        ''', i_clock = signal(), i_data = signals(2, 8),
             o_data = signals(2, 8), o_more = signals(2, 8))
        text = emit_sv(analyse(top)[0])
        self.assertIn('genvar k;', text)
        self.assertIn('genvar k0;', text)
        self.assertIn('begin : first', text)
        self.assertIn('begin : second', text)


if __name__ == '__main__':
    unittest.main()
