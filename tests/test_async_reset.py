"""always_ff_async_reset: the deliberately awkward escape hatch.

Asynchronous reset is wrong for general logic. It is right for a reset
synchroniser, which has no running clock to sample the release with, and
for a pointer crossing in a dual-clock FIFO. The construct exists, warns
every time, and cannot be reached from always_ff.
"""
import itertools
import linecache
import shutil
import textwrap
import unittest

from isomorph import (analyse, emit_sv, emit_vhdl, signal, Simulator,
                      IsomorphError, ConversionError, fatal_warnings)

_counter = itertools.count()
HAVE_VERILATOR = shutil.which('verilator') is not None


def load (src):
    name = f'<ar{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


SRESET = textwrap.dedent('''\
    from isomorph import *

    @block
    def sreset (i_clock, i_areset, o_sreset):
        flops = signal(3)

        @always_ff_async_reset (
                i_clock.posedge,
                i_areset.posedge,
                reason = 'reset synchroniser: no clock at power-on'
            )
        def sreset_logic ():
            if (i_areset):
                flops.next = replicate(True, 3)
            else:
                flops.next = concat(False, flops[2:1])

        @always_comb
        def sreset_comb ():
            o_sreset.next = flops[0]
        return instances()
''')


def build (src = SRESET, name = 'sreset'):
    return load(src)[name](i_clock = signal(), i_areset = signal(),
                           o_sreset = signal())


class ConstructTests (unittest.TestCase):
    """The decorator runs when the block is elaborated, so each of these
    builds the block rather than only defining it."""

    def test_always_ff_still_refuses_two_edges (self):
        src = SRESET.replace('always_ff_async_reset (', 'always_ff (')
        src = src.replace(
            "            reason = 'reset synchroniser: no clock at "
            "power-on'\n", '')
        with self.assertRaises(IsomorphError) as caught:
            build(src)
        self.assertIn('always_ff_async_reset', str(caught.exception))

    def test_reason_is_required (self):
        src = SRESET.replace(
            "            reason = 'reset synchroniser: no clock at "
            "power-on'\n", '')
        with self.assertRaises(IsomorphError) as caught:
            build(src)
        self.assertIn('reason', str(caught.exception))

    def test_reason_may_not_be_blank (self):
        src = SRESET.replace("'reset synchroniser: no clock at power-on'",
                             "'   '")
        with self.assertRaises(IsomorphError) as caught:
            build(src)
        self.assertIn('reason', str(caught.exception))

    def test_two_edges_are_required (self):
        src = SRESET.replace('            i_areset.posedge,\n', '')
        with self.assertRaises(IsomorphError) as caught:
            build(src)
        self.assertIn('two edges', str(caught.exception))

    def test_clock_and_reset_must_differ (self):
        src = SRESET.replace('            i_areset.posedge,',
                             '            i_clock.posedge,')
        with self.assertRaises(IsomorphError) as caught:
            build(src)
        self.assertIn('same signal', str(caught.exception))

    def test_body_must_be_one_if_on_the_reset (self):
        src = SRESET.replace("""        if (i_areset):
            flops.next = replicate(True, 3)
        else:
            flops.next = concat(False, flops[2:1])""",
            '        flops.next = concat(False, flops[2:1])')
        with self.assertRaises(ConversionError) as caught:
            analyse(build(src))
        self.assertIn('one if', str(caught.exception))

    def test_first_condition_must_test_the_reset (self):
        src = SRESET.replace('        if (i_areset):',
                             '        if (flops[2]):')
        with self.assertRaises(ConversionError) as caught:
            analyse(build(src))
        self.assertIn('i_areset', str(caught.exception))


class WarningTests (unittest.TestCase):
    def test_a_reason_makes_it_a_warning_not_a_fault (self):
        _, warnings = analyse(build())
        said = [w for w in warnings if 'asynchronous reset' in w]
        self.assertEqual(len(said), 1, warnings)
        self.assertNotIn('severe', said[0])
        self.assertIn('asynchronous reset on i_areset', said[0])
        self.assertIn('no clock at power-on', said[0])

    def test_nothing_about_it_is_severe (self):
        """severe is for what cannot be built. This can, and the
        construct refuses to compile without the reason that says
        why it should be."""
        _, warnings = analyse(build())
        self.assertEqual([w for w in warnings if 'severe' in w], [])
        self.assertEqual(fatal_warnings(warnings), [])


class EmitTests (unittest.TestCase):
    def setUp (self):
        self.modules, _ = analyse(build())

    def test_systemverilog_sensitivity_list (self):
        text = emit_sv(self.modules)
        self.assertIn('always_ff @(posedge i_clock or posedge i_areset)',
                      text)
        self.assertIn('// asynchronous reset: reset synchroniser', text)

    def test_vhdl_is_if_reset_elsif_clock (self):
        text = emit_vhdl(self.modules)
        self.assertIn('process (i_clock, i_areset)', text)
        self.assertIn("if i_areset = '1' then", text)
        self.assertIn('elsif rising_edge(i_clock) then', text)
        self.assertIn('-- asynchronous reset: reset synchroniser', text)

    def test_reason_is_wrapped_to_the_house_width (self):
        for text in (emit_sv(self.modules), emit_vhdl(self.modules)):
            for line in text.splitlines():
                self.assertLessEqual(len(line), 79, line)


class SimulationTests (unittest.TestCase):
    """The assert acts while the reset is held, with no clock edge."""

    def drive (self, backend):
        sim = Simulator(build(), backend = backend)
        sim.add_clock(10e-9)
        sim.set('i_areset', 0)
        sim.tick(5)
        out = [sim.get('o_sreset')]             # released

        sim.set('i_areset', 1)
        sim.eval()
        out.append(sim.get('o_sreset'))         # asserted with no clock

        sim.set('i_areset', 0)
        sim.eval()
        out.append(sim.get('o_sreset'))         # release is synchronous
        sim.tick(1)
        out.append(sim.get('o_sreset'))
        sim.tick(2)
        out.append(sim.get('o_sreset'))         # chain has walked through
        sim.close()
        return out

    def test_python (self):
        self.assertEqual(self.drive('python'), [0, 1, 1, 1, 0])

    def test_c99 (self):
        self.assertEqual(self.drive('c99'), [0, 1, 1, 1, 0])

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_agrees (self):
        self.assertEqual(self.drive('verilator'), self.drive('python'))


class SyncAsyncNetTests (unittest.TestCase):
    """A reset synchroniser clocks the very net it then resets with.
    Verilator calls that SYNCASYNCNET and is right to everywhere but
    here, so the one net carries a waiver and the file passes its own
    lint."""

    SRC = textwrap.dedent("""\
        from isomorph import *

        @block
        def held (i_clock, i_areset, o_q):
            flops = signals(2)

            @always_ff_async_reset (i_clock.posedge, i_areset.posedge,
                reason = 'reset synchroniser')
            def flops_logic ():
                if (i_areset):
                    flops[0].next = True
                    flops[1].next = True
                else:
                    flops[0].next = False
                    flops[1].next = flops[0]

            @always_ff_async_reset (i_clock.posedge, flops[1].posedge,
                reason = 'the synchronised reset')
            def q_logic ():
                if (flops[1]):
                    o_q.next = False
                else:
                    o_q.next = (not o_q)

            return instances()
    """)

    PLAIN = textwrap.dedent("""\
        from isomorph import *

        @block
        def plain (i_clock, i_areset, o_q):
            @always_ff_async_reset (i_clock.posedge, i_areset.posedge,
                reason = 'a port is not clocked here')
            def q_logic ():
                if (i_areset):
                    o_q.next = False
                else:
                    o_q.next = (not o_q)
            return instances()
    """)

    def test_the_reset_net_is_waived (self):
        design = load(self.SRC)['held'](
            i_clock = signal(), i_areset = signal(), o_q = signal())
        modules, _ = analyse(design)
        text = emit_sv(modules)
        self.assertIn('/* verilator lint_off SYNCASYNCNET */', text)
        self.assertIn('/* verilator lint_on SYNCASYNCNET */', text)

    def test_a_reset_this_module_does_not_clock_is_not_waived (self):
        design = load(self.PLAIN)['plain'](
            i_clock = signal(), i_areset = signal(), o_q = signal())
        modules, _ = analyse(design)
        self.assertNotIn('SYNCASYNCNET', emit_sv(modules))


if __name__ == '__main__':
    unittest.main()


class AnnouncedAndNotFatalTests (unittest.TestCase):
    """An asynchronous reset is announced and still converts.

    Latch and combinational loop stop the run because neither can be
    built. always_ff_async_reset is the opposite case: it is the
    construct you call out on purpose, it will not compile without
    the reason that says why, and that reason reaches the HDL. It is
    neither fatal nor severe; severe is kept for what cannot be
    built."""

    SRC = textwrap.dedent('''\
        from isomorph import *
        @block
        def sync (i_clock, i_areset, o_sreset):
            chain = signal(2)
            @always_ff_async_reset (i_clock.posedge, i_areset.posedge,
                                    reason = 'reset synchroniser')
            def sync_logic ():
                if (i_areset):
                    chain.next = 0b11
                else:
                    chain.next = concat(chain[0], False)
            @always_comb
            def sync_comb ():
                o_sreset.next = chain[1]
            return instances()
    ''')

    def test_it_warns_and_still_converts (self):
        ns = load(self.SRC)
        top = ns['sync'](i_clock = signal(), i_areset = signal(),
                         o_sreset = signal())
        modules, warnings = analyse(top)
        said = [w for w in warnings if 'asynchronous reset' in w]
        self.assertTrue(said)
        self.assertEqual(fatal_warnings(warnings), [])
        self.assertEqual(modules[-1].name, 'sync')

    def test_the_reason_reaches_the_systemverilog (self):
        ns = load(self.SRC)
        top = ns['sync'](i_clock = signal(), i_areset = signal(),
                         o_sreset = signal())
        text = emit_sv(analyse(top)[0])
        self.assertIn('reset synchroniser', text)
