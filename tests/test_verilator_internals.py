"""Verilator can be asked for the flip-flops you named (roadmap 2).

The Verilator backend used to reach top-level ports and nothing else,
so a bench that wanted to know what a state machine was doing had to
run on one of the two smoke backends, which are not the design of
record. It now reads the top module's own registers under the names
they have in the Python, through verilator's own configuration file so
the annotation never reaches the SystemVerilog a fitter compiles.

Reading only. Writing a flip-flop from outside the model races the
non-blocking assignment that owns it, which is how a backdoor write
stops the three backends agreeing.
"""
import itertools
import linecache
import os
import shutil
import textwrap
import unittest

from isomorph import analyse, signal, Simulator, SimError

_counter = itertools.count()
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)

DESIGN = textwrap.dedent('''\
    from isomorph import (block, signal, enum, always_ff, always_comb,
                          concat, instances, main)

    state = enum('IDLE', 'BUSY', 'DONE')


    @block
    def peeker (i_clock, i_go, o_done):
        fsm = signal(state)
        count = signal(8)
        count_inc = signal(8)
        shifter = signal(16)

        @always_comb
        def peek_comb ():
            count_inc.next = (count + 1)[7:0]
            o_done.next = (fsm == state.DONE)

        @always_ff (i_clock.posedge)
        def peek_logic ():
            count.next = count_inc
            shifter.next = concat(shifter[14:0], i_go)
            if (fsm == state.IDLE):
                if (i_go):
                    fsm.next = state.BUSY
            if (fsm == state.BUSY):
                fsm.next = state.DONE
            if (fsm == state.DONE):
                fsm.next = state.IDLE

        return instances()
''')


def load (src):
    name = f'<peek{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    namespace = {}
    exec(compile(src, name, 'exec'), namespace)
    return namespace


def build (backend):
    ns = load(DESIGN)
    top = ns['peeker'](i_clock = signal(), i_go = signal(),
                       o_done = signal())
    sim = Simulator(top, backend = backend)
    sim.add_clock(20e-9)
    return sim


def trace (backend, steps = 6):
    sim = build(backend)
    out = []
    for step in range(steps):
        sim.set('i_go', 1 if step == 0 else 0)
        sim.tick(1)
        out.append((sim.get('count'), sim.get('fsm'),
                    sim.get('shifter'), sim.get('o_done')))
    return out


@unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
class InternalsTests (unittest.TestCase):
    def test_the_flops_are_readable_by_their_python_names (self):
        sim = build('verilator')
        for name in ('count', 'fsm', 'shifter'):
            self.assertEqual(int(sim.get(name)), 0, name)

    def test_an_enum_flop_reads_as_its_member (self):
        """sim.get(fsm) is state.IDLE, not 0, and still equals 0
       ."""
        sim = build('verilator')
        self.assertEqual(repr(sim.get('fsm')), 'state.IDLE')
        self.assertEqual(sim.get('fsm'), 0)

    def test_verilator_agrees_with_python_about_them (self):
        self.assertEqual(trace('verilator'), trace('python'))

    def test_verilator_agrees_with_c99_about_them (self):
        self.assertEqual(trace('verilator'), trace('c99'))

    def test_a_flop_cannot_be_written (self):
        sim = build('verilator')
        with self.assertRaisesRegex(SimError, 'read but not written'):
            sim.set('count', 3)

    def test_a_name_that_is_neither_says_so (self):
        sim = build('verilator')
        with self.assertRaisesRegex(SimError, 'not a port'):
            sim.get('no_such_signal')

    def test_the_annotation_stays_out_of_the_systemverilog (self):
        sim = build('verilator')
        path = os.path.join(sim._native.workdir, 'peeker.sv')
        with open(path) as handle:
            text = handle.read()
        self.assertNotIn('public', text)
        self.assertNotIn('verilator_config', text)
        vlt = os.path.join(sim._native.workdir, 'peeker.vlt')
        with open(vlt) as handle:
            config = handle.read()
        self.assertIn('public_flat_rd', config)
        self.assertNotIn('public_flat_rw', config)


if __name__ == '__main__':
    unittest.main()
