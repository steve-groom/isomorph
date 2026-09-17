"""The two emitted languages proved to be the same netlist.

Stronger than the cycle comparison in test_vhdl_equivalence, which only
covers the stimulus it was given: this covers every input, don't-cares
included. Both languages are read into one yosys session, the second
through the GHDL plugin, and equiv_simple then equiv_induct prove them
cell for cell.

Two designs cannot go through it, and neither limit is isomorph's. They
are recorded here because a limit nobody wrote down is rediscovered
every six months.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'constructs'))

import formal_equivalence as fe                       # noqa: E402
from isomorph.lib.stream_pipe import elaborate_stream_pipe   # noqa: E402
from isomorph.lib.stream_fifo import elaborate_stream_fifo   # noqa: E402
from isomorph.lib.stream_fork import elaborate_stream_fork   # noqa: E402
from isomorph.lib.stream_join import elaborate_stream_join   # noqa: E402
from isomorph.lib.ram_block import elaborate_ram_block       # noqa: E402
from isomorph.lib.stream_to_memory import (              # noqa: E402
    elaborate_stream_to_memory)
from isomorph.lib.stream_from_memory import (            # noqa: E402
    elaborate_stream_from_memory)

PROVABLE = [
    ('stream_pipe', elaborate_stream_pipe),
    ('stream_fifo', elaborate_stream_fifo),
    ('stream_fork', elaborate_stream_fork),
    ('stream_join', elaborate_stream_join),
    ('ram_block', elaborate_ram_block),
    ('stream_to_memory', elaborate_stream_to_memory),
    ('stream_from_memory', elaborate_stream_from_memory),
]


@unittest.skipUnless(fe.HAVE_PLUGIN, 'yosys ghdl plugin not installed')
class ProofTests (unittest.TestCase):
    def test_every_library_block_is_proven (self):
        for name, elaborate in PROVABLE:
            with self.subTest(block = name):
                log = fe.prove(elaborate)
                self.assertIn('Equivalence successfully proven!', log)
                self.assertIn('0 are unproven', log)

    def test_a_flip_flop_and_a_memory_both_go_through (self):
        """Memories need mapping and state needs induction; without
        either, a design that is equivalent looks unproven."""
        log = fe.prove(elaborate_stream_fifo)
        self.assertIn('Equivalence successfully proven!', log)


@unittest.skipUnless(fe.HAVE_PLUGIN, 'yosys ghdl plugin not installed')
class LimitTests (unittest.TestCase):
    """What cannot go through, and whose fault it is.

    Neither of these is a fault in the emitted HDL. Both were measured
    on this machine, GHDL 4.1.0 and yosys 0.33.
    """

    def test_a_matching_case_is_out_of_reach (self):
        """GHDL's synthesis front end drops a don't-care choice.

        `ghdl --synth` on a design with `when "1---"` reports 'choice
        with meta-value is ignored' and builds the case without that
        arm. `ghdl -a` accepts the same file and the simulator runs it
        correctly, which is why the cycle comparison passes mini_core
        with nothing skipped. The consequence worth remembering is not
        about this test: a yosys flow must not synthesise the emitted
        VHDL of a design that uses match with bits().
        """
        from mini_core import elaborate_mini_core
        with self.assertRaises(NotImplementedError) as ctx:
            fe.prove(elaborate_mini_core)
        self.assertIn('matching case', str(ctx.exception))

    def test_an_unpacked_array_port_is_out_of_reach (self):
        """yosys 0.33's Verilog front end does not parse one."""
        from lane_array import elaborate_lane_array
        with self.assertRaises(NotImplementedError) as ctx:
            fe.prove(elaborate_lane_array)
        self.assertIn('unpacked array port', str(ctx.exception))

    def test_the_emitted_vhdl_really_is_a_matching_case (self):
        """The thing GHDL drops is correct VHDL-2008, which is why the
        limit is recorded rather than worked around."""
        from isomorph import analyse, emit_vhdl
        from mini_core import elaborate_mini_core
        text = emit_vhdl(analyse(elaborate_mini_core())[0])
        self.assertIn('case? instr_v is', text)
        self.assertIn('when "1---"', text)


if __name__ == '__main__':
    unittest.main()
