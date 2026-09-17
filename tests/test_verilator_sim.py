"""Verilator simulator backend: the emitted .sv is the reference, so
the Python and C99 backends must agree with it cycle for cycle."""
import itertools
import linecache
import shutil
import textwrap
import unittest

from isomorph import Simulator, signal

_counter = itertools.count()
HAVE_VERILATOR = shutil.which('verilator') is not None


def load (src):
    name = f'<vl{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


COUNT = textwrap.dedent('''\
    from isomorph import *
    @block
    def count8 (i_clock, i_reset, i_step, o_q):
        nxt = signal(8)

        @always_comb
        def step_comb ():
            nxt.next = (o_q + i_step)[7:0]

        @always_ff (i_clock.posedge)
        def count_logic ():
            if (i_reset):
                o_q.next = 0
            else:
                o_q.next = nxt
        return instances()
''')

WIDE = textwrap.dedent('''\
    from isomorph import *
    @block
    def wide (i_clock, i_a, o_q):
        @always_ff (i_clock.posedge)
        def wide_logic ():
            o_q.next = (i_a ^ replicate(True, 96))[95:0]
        return instances()
''')


def make_count8 ():
    return load(COUNT)['count8'](i_clock = signal(), i_reset = signal(),
                                 i_step = signal(8), o_q = signal(8))


def make_wide ():
    return load(WIDE)['wide'](i_clock = signal(), i_a = signal(96),
                              o_q = signal(96))


def drive_count8 (backend):
    sim = Simulator(make_count8(), backend = backend)
    sim.add_clock(10e-9)
    sim.set('i_reset', 1)
    sim.set('i_step', 3)
    sim.tick(2)
    sim.set('i_reset', 0)
    out = []
    for step in (1, 3, 7, 200, 56):
        sim.set('i_step', step)
        sim.tick(1)
        out.append(sim.get('o_q'))
    sim.close()
    return out


def drive_wide (backend):
    sim = Simulator(make_wide(), backend = backend)
    sim.add_clock(10e-9)
    out = []
    for value in (0, 1, (1 << 95), (1 << 96) - 1, 0xdeadbeefcafef00dbaadf00d):
        sim.set('i_a', value)
        sim.tick(1)
        out.append(sim.get('o_q'))
    sim.close()
    return out


@unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
class VerilatorBackendTests (unittest.TestCase):
    def test_count8_matches_python_and_c99 (self):
        reference = drive_count8('verilator')
        self.assertEqual(drive_count8('python'), reference)
        self.assertEqual(drive_count8('c99'), reference)
        self.assertEqual(reference[0], 1)

    def test_wide_signals (self):
        """Over 64 bits the C99 smoke bows out, so this is
        Python against Verilator."""
        reference = drive_wide('verilator')
        self.assertEqual(drive_wide('python'), reference)
        mask = (1 << 96) - 1
        self.assertEqual(reference[1], (1 ^ mask))

    def test_internal_signal_is_refused (self):
        sim = Simulator(make_count8(), backend = 'verilator')
        with self.assertRaises(Exception) as caught:
            sim.get('no_such_signal')
        self.assertIn('port', str(caught.exception))
        sim.close()

    def test_unknown_backend_names_the_choices (self):
        with self.assertRaises(Exception) as caught:
            Simulator(make_count8(), backend = 'icarus')
        self.assertIn('verilator', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
