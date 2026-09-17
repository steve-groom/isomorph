"""The emitted VHDL against the emitted SystemVerilog's answers.

The two outputs have to be proved equivalent, and there is a
practical form of that for when a formal tool is not to hand: the same stimulus through GHDL, compared every cycle.
Formal is not to hand here, because eqy is not installed and yosys has
no ghdl plugin, so neither language can be read into one netlist.

The reference for the recorded answers is whichever backend the
machine can run, preferring Verilator, which is the design of record.
A divergence is an emitter bug: both languages come from one
intermediate form, so there is nothing else it could be.
"""
import os
import random
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'constructs'))

import vhdl_lockstep as vl                            # noqa: E402
from isomorph.lib.stream_pipe import elaborate_stream_pipe  # noqa: E402
from isomorph.lib.stream_fifo import elaborate_stream_fifo  # noqa: E402
from isomorph.lib.stream_fork import elaborate_stream_fork  # noqa: E402
from isomorph.lib.stream_join import elaborate_stream_join  # noqa: E402
from isomorph.lib.stream_to_memory import (              # noqa: E402
    elaborate_stream_to_memory)
from isomorph.lib.stream_from_memory import (            # noqa: E402
    elaborate_stream_from_memory)

HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)
REFERENCE = 'verilator' if HAVE_VERILATOR else 'python'


def noise (names, seed):
    """Random stimulus over the named ports and their widths."""
    rng = random.Random(seed)

    def stimulus (cycle):
        return {name: rng.randint(0, (1 << width) - 1)
                for name, width in names.items()}

    return stimulus


def with_reset (names, seed, reset, held = 3):
    inner = noise(names, seed)

    def stimulus (cycle):
        applied = inner(cycle)
        applied[reset] = 1 if cycle < held else 0
        return applied

    return stimulus


@unittest.skipUnless(vl.HAVE_GHDL, 'ghdl not installed')
class EquivalenceTests (unittest.TestCase):
    """Each design's VHDL asked to produce the same answers."""

    def check (self, elaborate, stimulus, cycles = 40, settle = 4,
               allow_skips = True):
        text = vl.run(elaborate, stimulus, cycles = cycles,
                      backend = REFERENCE, settle = settle)
        self.assertIn('vhdl matched every cycle', text)
        if not allow_skips:
            self.assertIn('0 checks skipped', text)

    def test_mini_core (self):
        """Nothing skipped: every output is compared every cycle, so
        a pass here is a real one rather than a run where VHDL had no
        opinion."""
        from mini_core import elaborate_mini_core
        self.check(elaborate_mini_core, with_reset(
            {'i_word': 16, 'i_a': 8, 'i_b': 8}, 1, 'i_reset'),
            allow_skips = False)

    def test_stream_pipe (self):
        self.check(elaborate_stream_pipe, with_reset(
            {'i_stream_valid': 1, 'i_stream_data': 8,
             'o_stream_ready': 1}, 2, 'i_reset'),
            allow_skips = False)

    def test_stream_fifo (self):
        self.check(elaborate_stream_fifo, with_reset(
            {'i_stream_valid': 1, 'i_stream_data': 8,
             'o_stream_ready': 1}, 3, 'i_reset'),
            allow_skips = False)

    def test_stream_fork (self):
        self.check(elaborate_stream_fork, with_reset(
            {'i_stream_valid': 1, 'i_stream_data': 8,
             'o_a_ready': 1, 'o_b_ready': 1}, 4, 'i_reset'))

    def test_stream_join (self):
        """No clock at all: the comparison is on the settle."""
        self.check(elaborate_stream_join, noise(
            {'i_a_valid': 1, 'i_a_data': 8, 'i_b_valid': 1,
             'i_b_data': 4, 'o_stream_ready': 1}, 5),
            settle = 0)

    def test_stream_to_memory (self):
        self.check(elaborate_stream_to_memory, with_reset(
            {'i_start': 1, 'i_count': 5, 'i_stream_valid': 1,
             'i_stream_data': 8}, 6, 'i_reset'))

    def test_stream_from_memory (self):
        self.check(elaborate_stream_from_memory, with_reset(
            {'i_start': 1, 'i_count': 5, 'rd_data': 8,
             'o_stream_ready': 1}, 7, 'i_reset'))


@unittest.skipUnless(vl.HAVE_GHDL, 'ghdl not installed')
class UndefinedTests (unittest.TestCase):
    """VHDL starts a register at 'U' and the other three start it at
    zero, because isomorph emits no power-on value on purpose. An
    output reading state nothing has written is therefore undefined
    here and zero there, so the check binds only where VHDL has
    committed to a value, and the skips are counted so that a run
    which matched only because everything was unknown cannot look
    like a pass."""

    def test_a_design_reading_unwritten_memory_skips_and_says_so (self):
        text = vl.run(elaborate_stream_from_memory, with_reset(
            {'i_start': 1, 'i_count': 5, 'rd_data': 8,
             'o_stream_ready': 1}, 7, 'i_reset'),
            cycles = 40, backend = REFERENCE, settle = 4)
        self.assertIn('vhdl matched every cycle', text)
        self.assertNotIn('0 checks skipped', text)


@unittest.skipUnless(vl.HAVE_GHDL, 'ghdl not installed')
class TeethTests (unittest.TestCase):
    """The comparison has to be able to fail, or it says nothing."""

    def test_a_planted_difference_is_caught (self):
        from mini_core import elaborate_mini_core
        stimulus = with_reset({'i_word': 16, 'i_a': 8, 'i_b': 8},
                              11, 'i_reset')
        top, clock, inputs, outputs, vectors = vl.record(
            elaborate_mini_core, stimulus, 20, REFERENCE)
        # one output, one cycle, one bit different
        applied, expected = vectors[12]
        expected = dict(expected)
        expected['o_q'] ^= 1
        vectors[12] = (applied, expected)
        text = vl.testbench(top, clock, inputs, outputs, vectors)
        self.assertIn('cycle 12: o_q is not', text)

    def test_an_array_port_says_so_rather_than_passing (self):
        from lane_array import elaborate_lane_array
        with self.assertRaises(NotImplementedError) as ctx:
            vl.run(elaborate_lane_array, lambda cycle: {}, cycles = 2)
        self.assertIn('array port', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
