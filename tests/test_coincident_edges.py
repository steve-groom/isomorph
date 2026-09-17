"""Two clocks landing on the same instant, on all three backends.

The three backends have to agree about the one case a designer cannot
check by eye: a flop in one domain sampling a flop in another when both
clocks rise together. The value it must see is the one from before the
edge. Committing one domain before the other domain takes its edge
shows it the new value instead, which is something no hardware does,
and it is what the C99 and Verilator backends did until the grouped
edge existed.

The Python backend always had this right, so a comparison between the
three is what catches it.
"""
import itertools
import linecache
import shutil
import textwrap
import unittest

from isomorph import analyse, signal, Simulator, SimError
from isomorph.elaborate import Elaborated

HAVE_GCC = shutil.which('gcc') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)

DESIGN = textwrap.dedent('''\
    from isomorph import (block, signal, always_ff, always_comb, attr,
                          instances, main)


    @block
    def two_domain (i_clock_a, i_clock_b, i_d, o_a, o_b):
        stage = signal(8)


        @always_ff (i_clock_a.posedge)
        def a_logic ():
            stage.next = i_d

        # b_logic samples stage from the other clock on purpose: the
        # point of this design is what the backends do at a coincident
        # edge. The attribute goes on the flop that resolves it, which
        # is the destination, and saying so is what stops the crossing
        # check refusing the design
        attr(o_b, async_reg = 'TRUE')

        @always_ff (i_clock_b.posedge)
        def b_logic ():
            o_b.next = stage

        @always_comb
        def a_comb ():
            o_a.next = stage

        return instances()
''')


_counter = itertools.count()


def load (src):
    name = f'<coincident{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    namespace = {}
    exec(compile(src, name, 'exec'), namespace)
    return namespace


def build (backend):
    ns = load(DESIGN)
    top = ns['two_domain'](i_clock_a = signal(), i_clock_b = signal(),
                      i_d = signal(8), o_a = signal(8), o_b = signal(8))
    assert isinstance(top, Elaborated)
    sim = Simulator(top, backend = backend)
    sim.add_clock(20e-9, 'i_clock_a')
    sim.add_clock(20e-9, 'i_clock_b')
    return sim


def trace (backend, steps = 8):
    """o_a and o_b after each pair of coincident edges."""
    sim = build(backend)
    out = []
    for step in range(steps):
        sim.set('i_d', step + 1)
        sim.eval()
        sim.posedge(['i_clock_a', 'i_clock_b'])
        sim.eval()
        out.append((sim.get('o_a'), sim.get('o_b')))
    return out


class CoincidentEdgeTests (unittest.TestCase):
    def test_python_sees_the_pre_edge_value (self):
        got = trace('python')
        # o_a is what domain A just latched; o_b is what domain B saw,
        # which is what A held before this edge
        self.assertEqual([a for a, _ in got], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual([b for _, b in got], [0, 1, 2, 3, 4, 5, 6, 7])

    @unittest.skipUnless(HAVE_GCC, 'gcc not installed')
    def test_c99_agrees_with_python (self):
        self.assertEqual(trace('c99'), trace('python'))

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_agrees_with_python (self):
        self.assertEqual(trace('verilator'), trace('python'))


class ClockIsNotAnInputTests (unittest.TestCase):
    def test_setting_a_clock_is_an_error (self):
        sim = build('python')
        with self.assertRaisesRegex(SimError, 'is a clock'):
            sim.set('i_clock_a', 1)

    def test_tick_must_name_a_clock_when_there_are_two (self):
        sim = build('python')
        with self.assertRaisesRegex(SimError, 'more than one clock'):
            sim.tick(1)

    def test_tick_is_fine_once_it_names_one (self):
        sim = build('python')
        sim.set('i_d', 9)
        sim.tick(1, 'i_clock_a')
        self.assertEqual(sim.get('o_a'), 9)
