"""pipeline(), and the naming of a namespace of hardware."""
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace

from isomorph import (block, signal, always_comb, concat, instances,
                      analyse,
                      emit_sv, IsomorphError, Simulator, write_sv_files)
from isomorph.emit_sv import lint_sv
from isomorph.lib import stream
from isomorph.lib import stream_pipe, stream_fifo, stream_join, pipeline

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'examples'))
from dataflow import (elaborate_dataflow,             # noqa: E402
                      test_dataflow as bench_dataflow)

HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)


@block
def chain_of_three (i_clock, i_reset, i_stream, o_stream):
    before = stream(8)
    inst_first = stream_pipe(i_clock = i_clock, i_reset = i_reset,
                             i_stream = i_stream, o_stream = before)
    # three stages, one comment
    chain = pipeline(before, o_stream, [
        ('front', stream_pipe),
        ('middle', stream_fifo, {'DEPTH': 4}),
        ('back', stream_pipe),
    ], i_clock, i_reset)
    return instances()


def elaborate_chain ():
    return chain_of_three(i_clock = signal(), i_reset = signal(),
                          i_stream = stream(8), o_stream = stream(8))


class NamingTests (unittest.TestCase):
    def test_instances_and_links_are_named_by_path (self):
        modules, warnings = analyse(elaborate_chain())
        self.assertEqual(warnings, [])
        top = modules[-1]
        self.assertEqual([i.name for i in top.instances],
                         ['inst_first', 'chain_front', 'chain_middle',
                          'chain_back'])
        names = {s.name for s in top.signals}
        self.assertIn('chain_front_out_valid', names)
        self.assertIn('chain_middle_out_data', names)
        self.assertNotIn('chain_back_out_valid', names)
        text = emit_sv(modules)
        self.assertIn('stream_pipe chain_front (', text)
        self.assertIn('.o_stream_valid(chain_front_out_valid)', text)
        self.assertIn('.i_stream_valid(chain_front_out_valid)', text)

    def test_stages_sit_on_the_callers_line (self):
        """Ordered after the instance above the call, and the comment
        above the call attaches to the chain exactly once."""
        modules, _ = analyse(elaborate_chain())
        top = modules[-1]
        lines = [i.line for i in top.instances]
        self.assertEqual(sorted(lines), lines)
        self.assertLess(lines[0], lines[1])
        self.assertEqual(lines[1], lines[2])
        text = emit_sv(modules)
        self.assertEqual(text.count('three stages, one comment'), 1)
        self.assertLess(text.index('inst_first ('),
                        text.index('chain_front ('))

    def test_a_nested_namespace_is_walked (self):
        """The same rule applies to a namespace the block builds by
        hand, one level or two."""
        @block
        def nest (i_a, o_b):
            group = SimpleNamespace(
                inner = SimpleNamespace(wire = signal(4)),
                flat = signal(4))

            @always_comb
            def nest_comb ():
                group.inner.wire.next = i_a
                group.flat.next = group.inner.wire
                o_b.next = group.flat

            return instances()

        modules, warnings = analyse(nest(i_a = signal(4), o_b = signal(4)))
        self.assertEqual(warnings, [])
        names = {s.name for s in modules[-1].signals}
        self.assertEqual(names, {'group_inner_wire', 'group_flat'})

    def test_an_instance_in_a_namespace_is_not_lost (self):
        """instances() looks inside a namespace as it looks inside a
        list, so a stage held there is not reported lost."""
        modules, _ = analyse(elaborate_chain())
        self.assertEqual(len(modules[-1].instances), 4)


class ArgumentTests (unittest.TestCase):
    def build (self, stages, **extra):
        @block
        def unit (i_clock, i_reset, i_stream, o_stream):
            chain = pipeline(i_stream, o_stream, stages, i_clock,
                             i_reset, **extra)
            return instances()
        return unit(i_clock = signal(), i_reset = signal(),
                    i_stream = stream(8), o_stream = stream(8))

    def test_no_stages (self):
        with self.assertRaises(IsomorphError):
            self.build([])

    def test_a_name_used_twice (self):
        with self.assertRaises(IsomorphError) as ctx:
            self.build([('a', stream_pipe), ('a', stream_pipe)])
        self.assertIn('named twice', str(ctx.exception))

    def test_a_name_that_is_not_an_identifier (self):
        with self.assertRaises(IsomorphError) as ctx:
            self.build([('a-b', stream_pipe)])
        self.assertIn('identifier', str(ctx.exception))

    def test_a_stage_that_is_not_a_block (self):
        with self.assertRaises(IsomorphError) as ctx:
            self.build([('a', lambda: None)])
        self.assertIn('not a @block', str(ctx.exception))

    def test_a_stage_without_stream_ports (self):
        @block
        def plain (i_a, o_b):
            @always_comb
            def plain_comb ():
                o_b.next = i_a
            return instances()

        with self.assertRaises(IsomorphError) as ctx:
            self.build([('a', plain)])
        self.assertIn('no i_stream', str(ctx.exception))

    def test_a_parameter_the_stage_does_not_take (self):
        with self.assertRaises(IsomorphError) as ctx:
            self.build([('a', stream_pipe, {'DEPTH': 4})])
        self.assertIn("no parameter 'DEPTH'", str(ctx.exception))

    def test_a_clocked_stage_with_no_clock_given (self):
        @block
        def unit (i_stream, o_stream):
            chain = pipeline(i_stream, o_stream, [('a', stream_pipe)])
            return instances()

        with self.assertRaises(IsomorphError) as ctx:
            unit(i_stream = stream(8), o_stream = stream(8))
        self.assertIn('i_clock', str(ctx.exception))

    def test_a_clockless_stage_needs_no_clock (self):
        @block
        def widen (i_stream, o_stream):
            @always_comb
            def widen_comb ():
                o_stream.valid.next = i_stream.valid
                o_stream.data.next = i_stream.data
                i_stream.ready.next = o_stream.ready
            return instances()

        @block
        def unit (i_stream, o_stream):
            chain = pipeline(i_stream, o_stream, [('a', widen)])
            return instances()

        modules, warnings = analyse(unit(i_stream = stream(8),
                                         o_stream = stream(8)))
        self.assertEqual(warnings, [])
        self.assertEqual(modules[-1].instances[0].name, 'chain_a')

    def test_out_width_sizes_the_link (self):
        @block
        def twice (i_stream, o_stream):
            @always_comb
            def twice_comb ():
                o_stream.valid.next = i_stream.valid
                o_stream.data.next = concat(i_stream.data, i_stream.data)
                i_stream.ready.next = o_stream.ready
            return instances()

        @block
        def unit (i_clock, i_reset, i_stream, o_stream):
            chain = pipeline(i_stream, o_stream, [
                ('grow', twice, {'out_width': 16}),
                ('hold', stream_pipe),
            ], i_clock, i_reset)
            return instances()

        modules, warnings = analyse(unit(
            i_clock = signal(), i_reset = signal(),
            i_stream = stream(8), o_stream = stream(16)))
        self.assertEqual(warnings, [])
        widths = {s.name: s.width for s in modules[-1].signals}
        self.assertEqual(widths['chain_grow_out_data'], 16)


class ExampleTests (unittest.TestCase):
    """examples/dataflow.py: the composed design, its bench on every
    backend, and the SystemVerilog through the linter."""

    def test_python (self):
        sim = Simulator(elaborate_dataflow(), backend = 'python')
        try:
            bench_dataflow(sim)
        finally:
            sim.close()

    @unittest.skipUnless(shutil.which('gcc'), 'gcc not installed')
    def test_c99 (self):
        sim = Simulator(elaborate_dataflow(), backend = 'c99')
        try:
            bench_dataflow(sim)
        finally:
            sim.close()

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator (self):
        sim = Simulator(elaborate_dataflow(), backend = 'verilator')
        try:
            bench_dataflow(sim)
        finally:
            sim.close()

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_lints (self):
        modules, warnings = analyse(elaborate_dataflow())
        self.assertEqual(warnings, [])
        directory = tempfile.mkdtemp(prefix = 'iso_dataflow_')
        try:
            written = write_sv_files(modules, directory)
            lint_sv([p for p in written if p.endswith('.sv')],
                    top = 'dataflow')
        finally:
            shutil.rmtree(directory, ignore_errors = True)

    def test_two_depths_are_two_modules (self):
        modules, _ = analyse(elaborate_dataflow())
        names = {m.name for m in modules}
        self.assertIn('stream_fifo_DEPTH_2', names)
        self.assertIn('stream_fifo_DEPTH_4', names)


if __name__ == '__main__':
    unittest.main()
