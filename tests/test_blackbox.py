"""Blackboxes.

Something the fitter has and isomorph does not: a phase-locked loop, a
vendor FIFO, a hard controller. The design instantiates it by name with
a port map so the fitted hierarchy is the one that was written, and the
real thing is handed to the tool separately.

No model of it goes in the @block tree, so the three built-in
simulators decline to run a design containing one. Simulating it is a
harness under Verilator that links the vendor's own model.
"""
import os
import shutil
import tempfile
import unittest

from isomorph import (analyse, blackbox, block, signal, always_comb,
                      assign, instances, open_port, emit_sv, emit_vhdl,
                      emit_c99, Simulator, IsomorphError, ConversionError,
                      write_sv_files, write_vhdl_files)
from isomorph.emit_sv import lint_sv
from isomorph.emit_vhdl import lint_vhdl

HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)
HAVE_GHDL = shutil.which('ghdl') is not None


def altpll ():
    return blackbox('altpll',
                    inputs = {'inclk0': 1, 'areset': 1},
                    outputs = {'c0': 1, 'c1': 1, 'locked': 1},
                    params = {'operation_mode': 'NORMAL',
                              'clk0_divide_by': 1},
                    source = 'Quartus megafunction from the IP catalogue')


def board ():
    pll = altpll()

    @block
    def top (i_clock, i_areset, o_clock, o_locked):
        inst_pll = pll (
            inclk0 = i_clock,
            areset = i_areset,

            c0 = o_clock,
            c1 = open_port(),
            locked = o_locked
        )
        return instances()

    return top(i_clock = signal(), i_areset = signal(),
               o_clock = signal(), o_locked = signal())


class DeclarationTests (unittest.TestCase):
    def test_a_port_cannot_be_both_directions (self):
        with self.assertRaises(IsomorphError) as ctx:
            blackbox('part', inputs = {'d': 1}, outputs = {'d': 1})
        self.assertIn('two ports', str(ctx.exception))

    def test_no_ports_at_all (self):
        with self.assertRaises(IsomorphError) as ctx:
            blackbox('part')
        self.assertIn('no ports', str(ctx.exception))

    def test_a_width_is_a_positive_int (self):
        with self.assertRaises(IsomorphError):
            blackbox('part', inputs = {'d': 0})

    def test_the_name_is_the_fitters_name (self):
        with self.assertRaises(IsomorphError) as ctx:
            blackbox('not a name', inputs = {'d': 1})
        self.assertIn('module name', str(ctx.exception))


class ConnectionTests (unittest.TestCase):
    def test_an_unknown_port (self):
        with self.assertRaises(IsomorphError) as ctx:
            altpll()(inclk0 = signal(), areset = signal(), c0 = signal(),
                     c1 = signal(), locked = signal(), nonsense = signal())
        self.assertIn('nonsense', str(ctx.exception))

    def test_a_port_left_out (self):
        with self.assertRaises(IsomorphError) as ctx:
            altpll()(inclk0 = signal(), areset = signal(), c0 = signal())
        self.assertIn('not connected', str(ctx.exception))

    def test_an_input_may_not_be_open (self):
        with self.assertRaises(IsomorphError) as ctx:
            altpll()(inclk0 = open_port(), areset = signal(),
                     c0 = signal(), c1 = signal(), locked = signal())
        self.assertIn('may not be left open', str(ctx.exception))

    def test_a_width_that_does_not_match (self):
        with self.assertRaises(IsomorphError) as ctx:
            altpll()(inclk0 = signal(8), areset = signal(),
                     c0 = signal(), c1 = signal(), locked = signal())
        self.assertIn('does not resize', str(ctx.exception))

    def test_an_output_may_be_open (self):
        modules, warnings = analyse(board())
        self.assertEqual(warnings, [])
        top = modules[-1]
        self.assertIsNone(top.instances[0].ports['c1'])


class ModuleTests (unittest.TestCase):
    def test_it_is_a_module_with_ports_and_no_body (self):
        modules, _ = analyse(board())
        part = modules[0]
        self.assertEqual(part.name, 'altpll')
        self.assertTrue(part.blackbox)
        self.assertEqual([p.name for p in part.ports],
                         ['inclk0', 'areset', 'c0', 'c1', 'locked'])
        self.assertEqual([p.direction for p in part.ports],
                         ['in', 'in', 'out', 'out', 'out'])
        self.assertEqual(part.processes, [])
        self.assertEqual(part.assigns, [])


class SystemVerilogTests (unittest.TestCase):
    def text (self):
        return emit_sv(analyse(board())[0])

    def test_the_stub_is_marked_and_parameterised (self):
        text = self.text()
        self.assertIn('(* black_box *)', text)
        self.assertIn('module altpll #(', text)
        self.assertIn('parameter operation_mode = "NORMAL"', text)
        self.assertIn('parameter clk0_divide_by = 1', text)

    def test_the_instance_carries_the_parameters (self):
        text = self.text()
        self.assertIn('.operation_mode(', text)
        self.assertIn('"NORMAL"', text)
        self.assertIn('inst_pll (', text)
        self.assertIn('.inclk0(i_clock)', text)

    def test_an_open_output_is_open (self):
        self.assertIn('.c1()', self.text())

    def test_the_stub_is_listed_apart_from_the_design (self):
        """A linter wants the stub; the fitter is given the vendor's
        own file and two modules of a name is an error there."""
        modules, _ = analyse(board())
        directory = tempfile.mkdtemp(prefix = 'iso_bb_')
        try:
            write_sv_files(modules, os.path.join(directory, 'top'))
            base = os.path.join(directory, 'top')
            with open(os.path.join(base, 'top.f')) as handle:
                design = handle.read()
            with open(os.path.join(base, 'top_blackbox.f')) as handle:
                stubs = handle.read()
            self.assertIn('top.sv', design)
            self.assertNotIn('altpll.sv', design)
            self.assertIn('altpll.sv', stubs)
        finally:
            shutil.rmtree(directory, ignore_errors = True)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_lints_with_the_stub (self):
        modules, _ = analyse(board())
        directory = tempfile.mkdtemp(prefix = 'iso_bb_sv_')
        try:
            written = write_sv_files(modules, directory)
            lint_sv([p for p in written if p.endswith('.sv')], top = 'top')
        finally:
            shutil.rmtree(directory, ignore_errors = True)


class VhdlTests (unittest.TestCase):
    def text (self):
        return emit_vhdl(analyse(board())[0])

    def test_a_component_not_an_entity (self):
        text = self.text()
        self.assertIn('component altpll is', text)
        self.assertIn('end component;', text)
        self.assertNotIn('entity altpll is', text)
        self.assertNotIn('entity work.altpll', text)
        self.assertIn('inst_pll : altpll', text)

    def test_the_generic_map_carries_both_kinds (self):
        text = self.text()
        self.assertIn('operation_mode : string := "NORMAL"', text)
        self.assertIn('operation_mode => "NORMAL"', text)
        self.assertIn('clk0_divide_by => 1', text)

    def test_no_file_is_written_for_it (self):
        modules, _ = analyse(board())
        directory = tempfile.mkdtemp(prefix = 'iso_bb_vhd_')
        try:
            written = write_vhdl_files(modules, directory)
            names = [os.path.basename(p) for p in written]
            self.assertNotIn('altpll.vhd', names)
            self.assertIn('top.vhd', names)
        finally:
            shutil.rmtree(directory, ignore_errors = True)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_lints (self):
        modules, _ = analyse(board())
        directory = tempfile.mkdtemp(prefix = 'iso_bb_g_')
        try:
            written = write_vhdl_files(modules, directory)
            lint_vhdl([p for p in written if p.endswith('.vhd')])
        finally:
            shutil.rmtree(directory, ignore_errors = True)


class RefusalTests (unittest.TestCase):
    """No model of it goes in the @block tree, so nothing here runs it."""

    def test_every_backend_declines_and_names_it (self):
        for backend in ('python', 'c99', 'verilator'):
            with self.subTest(backend = backend):
                with self.assertRaises(ConversionError) as ctx:
                    Simulator(board(), backend = backend)
                message = str(ctx.exception)
                self.assertIn('altpll', message)
                self.assertIn('Verilator harness', message)

    def test_the_c99_emitter_declines (self):
        with self.assertRaises(ConversionError) as ctx:
            emit_c99(analyse(board())[0])
        self.assertIn('altpll', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()


class CommentPragmaTests (unittest.TestCase):
    """A comment is a directive to a Verilog tool if it starts with the
    wrong word, and comments here travel into the HDL verbatim.

    Measured on verilator 5.020: a comment whose first word is
    "verilator", in any case and with or without a space after the
    slashes, is parsed as a pragma and fails the lint. One with the
    word anywhere else is an ordinary comment.
    """

    def unit (self, comment):
        @block
        def top (i_a, o_b):
            @always_comb
            def top_comb ():
                o_b.next = i_a
            return instances()

        top.block.__doc__ = comment
        return top(i_a = signal(), o_b = signal())

    def test_a_leading_directive_word_is_refused (self):
        for word in ('verilator', 'Verilator', 'synopsys', 'synthesis',
                     'pragma'):
            with self.subTest(word = word):
                with self.assertRaises(ConversionError) as ctx:
                    emit_sv(analyse(self.unit(f'{word} reads this'))[0])
                self.assertIn('reads it as a directive',
                              str(ctx.exception))

    def test_the_word_elsewhere_is_an_ordinary_comment (self):
        text = emit_sv(analyse(
            self.unit('the Verilator harness links the model'))[0])
        self.assertIn('// the Verilator harness links the model', text)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_what_the_guard_is_protecting_against (self):
        """The comment the guard refuses really does fail the lint."""
        directory = tempfile.mkdtemp(prefix = 'iso_prag_')
        try:
            path = os.path.join(directory, 'unit.sv')
            with open(path, 'w', encoding = 'ascii') as handle:
                handle.write('// Verilator harness links the model.\n'
                             'module unit (input logic a,'
                             ' output logic b);\n'
                             '    assign b = a;\n'
                             'endmodule\n')
            with self.assertRaises(ConversionError) as ctx:
                lint_sv(path, top = 'unit')
            self.assertIn('Unknown verilator comment', str(ctx.exception))
        finally:
            shutil.rmtree(directory, ignore_errors = True)


class BidirectionalTests (unittest.TestCase):
    """A vendor part often has one. Tristate is out of scope for good,
    so the argument exists to be refused with a reason: without it the
    error is that a port is missing, which reads like a mistake in the
    declaration rather than the limit it is.

    Checked against the real port list of Xilinx IOBUF, taken from the
    public cell library.
    """

    def test_an_inout_is_refused_with_a_reason (self):
        with self.assertRaises(IsomorphError) as ctx:
            blackbox('IOBUF', inputs = {'I': 1, 'T': 1},
                     outputs = {'O': 1}, inouts = {'IO': 1})
        message = str(ctx.exception)
        self.assertIn('bidirectional', message)
        self.assertIn('output enable', message)
        self.assertIn('no bidirectional port', message)

    def test_the_reason_does_not_claim_one_vendor_for_all (self):
        """Efinity requires the split at the top level and generates
        the buffer itself. Quartus, Vivado and Lattice take an inout
        there. Saying the first is how it is done would be wrong."""
        with self.assertRaises(IsomorphError) as ctx:
            blackbox('IOBUF', inputs = {'I': 1}, outputs = {'O': 1},
                     inouts = {'IO': 1})
        message = str(ctx.exception)
        self.assertIn('Efinity', message)
        self.assertIn('Quartus, Vivado and Lattice', message)

    def test_a_part_with_no_inout_is_unaffected (self):
        part = blackbox('BUFG', inputs = {'I': 1}, outputs = {'O': 1})
        self.assertEqual(sorted(part.inputs), ['I'])
