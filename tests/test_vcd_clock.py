"""The clock in the waveform.

The cycle model has no clock net: tick() runs the clocked processes
and commits them, and set() on a clock is refused, because a pin that
moves on Verilator and not on the other two is how the backends stop
agreeing. The VCD still has to show the wave, or nothing in it can be
read against anything else. It is drawn from what the simulator knows:
the instant of every edge it took, and the period add_clock gave.
"""
import itertools
import linecache
import os
import tempfile
import textwrap
import unittest

from isomorph import Simulator, signal
from isomorph.vcd import parse_vcd, values_at

_counter = itertools.count()


def load (src):
    name = f'<vcdclk{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


PARENT = textwrap.dedent('''\
    from isomorph import *

    @block
    def child (i_clock, i_d, o_q):
        @always_ff (i_clock.posedge)
        def child_logic ():
            o_q.next = i_d
        return instances()

    @block
    def parent (i_clock0, i_d, o_q):
        mid = signal()
        inst_child = child(i_clock = i_clock0, i_d = i_d, o_q = mid)
        @always_ff (i_clock0.posedge)
        def parent_logic ():
            o_q.next = mid
        return instances()
''')


def make_parent ():
    ns = load(PARENT)
    return ns['parent'](i_clock0 = signal(), i_d = signal(),
                        o_q = signal())


def dump (backend = 'python', ticks = 6, period = 20e-9):
    sim = Simulator(make_parent(), backend = backend)
    sim.add_clock(period)
    directory = tempfile.mkdtemp(prefix = 'iso_vcdclk_')
    path = os.path.join(directory, 'wave.vcd')
    sim.write_vcd(path)
    sim.set('i_d', 1)
    sim.tick(ticks)
    sim.close()
    with open(path) as handle:
        return handle.read()


class DefaultClockTests (unittest.TestCase):
    """The clock a bench means when it names none is a clock of the top
    block, never a port name found inside a child."""

    def test_the_default_clock_is_the_tops (self):
        sim = Simulator(make_parent(), backend = 'python')
        self.assertEqual(sim.domains, ['i_clock0'])
        self.assertEqual(sim.clock_name, 'i_clock0')

    def test_add_clock_registers_the_period_under_that_name (self):
        sim = Simulator(make_parent(), backend = 'python')
        sim.add_clock(20e-9)
        self.assertEqual(sim.clocks, {'i_clock0': 20})


class ClockWaveTests (unittest.TestCase):
    def test_the_clock_toggles (self):
        _, samples = parse_vcd(dump())
        series = values_at(samples, 'i_clock0')
        self.assertIn(1, [v for _, v in series])
        self.assertIn(0, [v for _, v in series])

    def test_an_edge_per_cycle_at_the_period (self):
        """Six ticks of 20 ns: a rising edge at each of them, and a
        falling edge half a period after each."""
        _, samples = parse_vcd(dump(ticks = 6))
        rises, falls, last = [], [], 0
        for t, v in values_at(samples, 'i_clock0'):
            if v == 1 and last != 1:
                rises.append(t)
            if v == 0 and last == 1:
                falls.append(t)
            last = v
        self.assertEqual(rises, [20, 40, 60, 80, 100, 120])
        self.assertEqual(falls, [30, 50, 70, 90, 110, 130])

    def test_a_childs_clock_port_is_drawn_too (self):
        _, samples = parse_vcd(dump())
        series = [v for _, v in values_at(samples, 'inst_child.i_clock')]
        self.assertIn(1, series)
        self.assertIn(0, series)

    def test_the_period_is_the_one_given (self):
        _, samples = parse_vcd(dump(ticks = 3, period = 100e-9))
        rises = [t for t, v in values_at(samples, 'i_clock0') if v == 1]
        self.assertEqual(rises[0], 100)
        self.assertEqual(rises[-1], 300)

    def test_the_data_still_lines_up_with_the_edges (self):
        """Drawing the clock must not move anything else: o_q still
        changes on a rising edge and nowhere else."""
        text = dump(ticks = 6)
        _, samples = parse_vcd(text)
        clock = dict(values_at(samples, 'i_clock0'))
        last = None
        for t, v in values_at(samples, 'o_q'):
            if v != last and last is not None:
                self.assertEqual(clock.get(t), 1,
                                 f'o_q moved at {t} with no rising edge')
            last = v


if __name__ == '__main__':
    unittest.main()
