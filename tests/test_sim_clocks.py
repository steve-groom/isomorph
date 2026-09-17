"""Per-clock tick, and Simulator.run."""
import itertools
import json
import linecache
import os
import tempfile
import textwrap
import unittest

from isomorph import analyse, emit_c99, signal, Simulator
from isomorph.proto import Wishbone
from isomorph.sim_report import report

_counter = itertools.count()


def load (src):
    name = f'<clk{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


TWO = textwrap.dedent('''\
    from isomorph import *
    @block
    def two (i_a, i_b, i_reset, o_a, o_b):
        nxt_a = signal(4)
        nxt_b = signal(4)
        assign(nxt_a, lambda: (o_a + 1)[3:0])
        assign(nxt_b, lambda: (o_b + 1)[3:0])
        @always_ff (i_a.posedge)
        def a_logic ():
            if (i_reset):
                o_a.next = 0
            else:
                o_a.next = nxt_a
        @always_ff (i_b.posedge)
        def b_logic ():
            if (i_reset):
                o_b.next = 0
            else:
                o_b.next = nxt_b
        return instances()
''')


def make_two (backend):
    ns = load(TWO)
    return ns['two'](
        i_a = signal(), i_b = signal(), i_reset = signal(),
        o_a = signal(4), o_b = signal(4))


class PerClockTests (unittest.TestCase):
    def test_c99_emits_per_clock (self):
        top = make_two('python')
        h, c = emit_c99(analyse(top)[0])
        self.assertIn('int two_clock_i_a(two *s);', h)
        self.assertIn('int two_clock_i_b(two *s);', h)
        self.assertIn('two_posedge_i_a', c)
        self.assertIn('two_posedge_i_b', c)

    def test_python_independent_counts (self):
        sim = Simulator(make_two('python'), backend = 'python')
        sim.set('i_reset', 1)
        sim.tick(2, clock = 'i_a')
        sim.tick(2, clock = 'i_b')
        sim.set('i_reset', 0)
        sim.tick(4, clock = 'i_a')
        sim.tick(2, clock = 'i_b')
        self.assertEqual(sim.get('o_a'), 4)
        self.assertEqual(sim.get('o_b'), 2)

    def test_c99_independent_counts (self):
        sim = Simulator(make_two('python'), backend = 'c99')
        sim.set('i_reset', 1)
        sim.tick(2, clock = 'i_a')
        sim.tick(2, clock = 'i_b')
        sim.set('i_reset', 0)
        sim.tick(4, clock = 'i_a')
        sim.tick(2, clock = 'i_b')
        self.assertEqual(sim.get('o_a'), 4)
        self.assertEqual(sim.get('o_b'), 2)

    def test_run_ratio (self):
        sim = Simulator(make_two('python'), backend = 'python')
        sim.add_clock(10e-9, clock = 'i_a')
        sim.add_clock(20e-9, clock = 'i_b')
        sim.set('i_reset', 1)
        sim.run(duration = 40e-9)
        sim.set('i_reset', 0)
        sim.run(duration = 40e-9)
        self.assertEqual(sim.get('o_a'), 4)
        self.assertEqual(sim.get('o_b'), 2)

    def test_c99_run_ratio (self):
        sim = Simulator(make_two('python'), backend = 'c99')
        sim.add_clock(10e-9, clock = 'i_a')
        sim.add_clock(20e-9, clock = 'i_b')
        sim.set('i_reset', 1)
        sim.run(duration = 40e-9)
        sim.set('i_reset', 0)
        sim.run(duration = 40e-9)
        self.assertEqual(sim.get('o_a'), 4)
        self.assertEqual(sim.get('o_b'), 2)


class ReportProtocolTests (unittest.TestCase):
    def test_events_in_report (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def pins (i_clock, i_reset, i_cyc, i_stb, o_q):
                @always_ff (i_clock.posedge)
                def dummy ():
                    if (i_reset):
                        o_q.next = 0
                    else:
                        o_q.next = i_cyc
                return instances()
        ''')
        ns = load(src)
        top = ns['pins'](
            i_clock = signal(), i_reset = signal(),
            i_cyc = signal(), i_stb = signal(), o_q = signal())
        sim = Simulator(top)
        chk = Wishbone(pins = {'cyc': 'i_cyc', 'stb': 'i_stb'},
                       reset = 'i_reset', strict = False)
        sim.add_check(chk)
        d = tempfile.mkdtemp(prefix = 'iso_ev_')
        log_path = os.path.join(d, 'run.ndjson')
        ev_path = os.path.join(d, 'run.events.json')
        sim.write_log(log_path)
        sim.set('i_reset', 0)
        sim.set('i_cyc', 0)
        sim.set('i_stb', 1)
        sim.tick()
        sim.write_events(ev_path)
        sim.close()
        self.assertTrue(chk.violations)
        result = report(log_path, events_path = ev_path)
        self.assertIn('protocol', result['text'])
        self.assertIn('WB-X01', result['text'])
        with open(ev_path, encoding = 'ascii') as f:
            data = json.load(f)
        self.assertEqual(data[0]['rule'], 'WB-X01')


if __name__ == '__main__':
    unittest.main()
