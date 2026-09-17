"""Streams, instance lists, reset values, no async reset."""
import shutil
import itertools
import linecache
import textwrap
import unittest

from isomorph import (signal, always_ff, analyse, emit_sv, emit_c99,
    Simulator, IsomorphError, ConversionError)
from isomorph.lib import stream
from isomorph.proto import Stream, ProtocolError


_counter = itertools.count()


def load (src):
    name = f'<lib{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


class StreamTests (unittest.TestCase):
    def test_factory_width (self):
        s = stream(16)
        self.assertEqual(len(s.data), 16)
        self.assertEqual(len(s.valid), 1)

    def test_valid_dropped (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def pins (i_clock, i_reset, i_valid, i_ready, i_data, o_q):
                @always_ff (i_clock.posedge)
                def dummy ():
                    if (i_reset):
                        o_q.next = 0
                    else:
                        o_q.next = i_valid
                return instances()
        ''')
        ns = load(src)
        top = ns['pins'](
            i_clock = signal(), i_reset = signal(),
            i_valid = signal(), i_ready = signal(),
            i_data = signal(8), o_q = signal())
        sim = Simulator(top)
        sim.add_check(Stream(pins = {
            'valid': 'i_valid', 'ready': 'i_ready', 'data': 'i_data',
        }, reset = 'i_reset'))
        sim.reset()
        sim.set('i_valid', 1)
        sim.set('i_ready', 0)
        sim.set('i_data', 3)
        sim.tick()
        sim.set('i_valid', 0)
        with self.assertRaises(ProtocolError) as ctx:
            sim.tick()
        self.assertIn('STR-X01', str(ctx.exception))


class AsyncResetTests (unittest.TestCase):
    def test_always_ff_rejects_reset_edge (self):
        clk = signal()
        rst = signal()
        with self.assertRaises(IsomorphError) as ctx:
            always_ff (clk.posedge, rst.negedge)
        self.assertIn('asynchronous', str(ctx.exception))

    def test_sv_sensitivity_is_clock_only (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def r (i_clock, i_reset, o_q):
                @always_ff (i_clock.posedge)
                def r_logic ():
                    if (i_reset):
                        o_q.next = 0
                    else:
                        o_q.next = 1
                return instances()
        ''')
        ns = load(src)
        top = ns['r'](i_clock = signal(), i_reset = signal(),
                      o_q = signal())
        text = emit_sv(analyse(top)[0])
        self.assertIn('always_ff @(posedge i_clock)', text)
        self.assertNotIn('or negedge', text)
        self.assertNotIn('or posedge i_reset', text)


class InstanceListTests (unittest.TestCase):
    """A loop over instances is a generate or it is an error.

    It used to be neither: a list of children became cells_0, cells_1
    and so on, which are names nobody typed and which do
    not happen. They are cells[0], cells[1] now, the emitters write
    them back as one labelled generate, and an array too irregular to
    be a generate is a conversion error naming what varies.
    """

    def test_a_fan_out_to_separate_ports_is_an_error (self):
        """Two different output ports are not element k of anything,
        so this one has to be two named instances."""
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def add2 (i_a, i_b, o_sum, WIDTH = 4):
                @always_comb
                def sum_comb ():
                    o_sum.next = (i_a + i_b)[WIDTH-1:0]
                return instances()
            @block
            def fan (i_a, i_b, o_0, o_1):
                outs = [o_0, o_1]
                cells = []
                for k in range (2):
                    cells.append (add2 (
                        i_a = i_a,
                        i_b = i_b,
                        o_sum = outs[k]
                    ))
                return instances()
        ''')
        ns = load(src)
        top = ns['fan'](
            i_a = signal(4), i_b = signal(4),
            o_0 = signal(4), o_1 = signal(4))
        with self.assertRaises(ConversionError) as ctx:
            analyse(top)
        self.assertIn('cells[k].o_sum', str(ctx.exception))
        self.assertIn('its own name', str(ctx.exception))

    def test_named_instances_are_the_answer (self):
        """What the error asks for, and it reads better."""
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def add2 (i_a, i_b, o_sum, WIDTH = 4):
                @always_comb
                def sum_comb ():
                    o_sum.next = (i_a + i_b)[WIDTH-1:0]
                return instances()
            @block
            def fan (i_a, i_b, o_0, o_1):
                inst_low = add2(i_a = i_a, i_b = i_b, o_sum = o_0)
                inst_high = add2(i_a = i_a, i_b = i_b, o_sum = o_1)
                return instances()
        ''')
        ns = load(src)
        top = ns['fan'](
            i_a = signal(4), i_b = signal(4),
            o_0 = signal(4), o_1 = signal(4))
        modules, _ = analyse(top)
        self.assertEqual([i.name for i in modules[-1].instances],
                         ['inst_low', 'inst_high'])
        text = emit_sv(modules)
        self.assertIn('add2 inst_low (', text)
        self.assertIn('add2 inst_high (', text)
        sim = Simulator(top)
        sim.set('i_a', 2)
        sim.set('i_b', 3)
        sim.eval()
        self.assertEqual(sim.get('o_0'), 5)
        self.assertEqual(sim.get('o_1'), 5)


class PowerOnTests (unittest.TestCase):
    """Every register powers up at zero, on every backend.

    Isomorph emits no power-on values, so the hardware comes up with
    whatever it comes up with. A simulator that started a register
    somewhere else would be describing a design nobody is going to get,
    which is the MyHDL failure in a new place. signal() used to take a
    reset= that did exactly that, in two backends of three.
    """

    SRC = textwrap.dedent('''\
        from isomorph import *
        @block
        def hold (i_clock, i_reset, o_q):
            q = signal(4)
            assign(o_q, lambda: q)
            @always_ff (i_clock.posedge)
            def hold_logic ():
                if (i_reset):
                    q.next = 9
                else:
                    q.next = q
            return instances()
    ''')

    def build (self, backend):
        ns = load(self.SRC)
        top = ns['hold'](
            i_clock = signal(), i_reset = signal(), o_q = signal(4))
        return Simulator(top, backend = backend)

    def test_python_starts_at_zero (self):
        sim = self.build('python')
        sim.eval()
        self.assertEqual(sim.get('q'), 0)
        self.assertEqual(sim.get('o_q'), 0)

    @unittest.skipUnless(shutil.which('gcc'), 'gcc not installed')
    def test_c99_starts_at_zero (self):
        sim = self.build('c99')
        sim.eval()
        self.assertEqual(sim.get('q'), 0)
        self.assertEqual(sim.get('o_q'), 0)

    def test_the_reset_branch_is_what_puts_it_there (self):
        sim = self.build('python')
        sim.add_clock(20e-9)
        sim.set('i_reset', 1)
        sim.tick(1)
        self.assertEqual(sim.get('q'), 9)

    def test_signal_no_longer_takes_a_power_on_value (self):
        with self.assertRaises(TypeError):
            signal(4, reset = 9)


class SharedEnumTests (unittest.TestCase):
    """An enum declared at module scope is not a local of any block, so
    it used to reach the emitters unnamed and emit `None fsm;`."""

    SRC = textwrap.dedent('''\
        from isomorph import *

        mode_t = enum('IDLE', 'RUN')

        @block
        def user (i_clock, i_go, o_running):
            fsm = signal(mode_t)

            @always_ff (i_clock.posedge)
            def fsm_logic ():
                if (i_go):
                    fsm.next = mode_t.RUN
                else:
                    fsm.next = mode_t.IDLE

            @always_comb
            def out_comb ():
                o_running.next = (fsm == mode_t.RUN)
            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['user'](i_clock = signal(), i_go = signal(),
                          o_running = signal())

    def test_module_scope_enum_is_named (self):
        modules, _ = analyse(self.build())
        self.assertIn('mode_t', modules[-1].enums)

    def test_emitted_type_is_the_variable_name (self):
        modules, _ = analyse(self.build())
        text = emit_sv(modules)
        self.assertIn('} mode_t;', text)
        self.assertIn('mode_t fsm;', text)
        self.assertNotIn('None', text)


class RebindTests (unittest.TestCase):
    """Hardware that a rebound name threw away is an error.

    instances() collects what the block's names still point at, so
    binding a name twice takes the first object with it and the
    hardware disappears without a word. MyHDL had the same trap.
    """

    def build (self, src):
        ns = load(src)
        return ns['unit'](i_clock = signal(), i_d = signal(8),
                          o_q = signal(8))

    def test_a_second_process_of_the_same_name_is_caught (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def unit (i_clock, i_d, o_q):
                a = signal(8)
                @always_ff (i_clock.posedge)
                def unit_logic ():
                    a.next = i_d
                @always_ff (i_clock.posedge)
                def unit_logic ():
                    o_q.next = a
                return instances()
        ''')
        with self.assertRaisesRegex(IsomorphError, 'lost a process'):
            self.build(src)

    def test_a_second_instance_of_the_same_name_is_caught (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def hold (i_clock, i_d, o_q):
                @always_ff (i_clock.posedge)
                def hold_logic ():
                    o_q.next = i_d
                return instances()
            @block
            def unit (i_clock, i_d, o_q):
                mid = signal(8)
                inst = hold(i_clock = i_clock, i_d = i_d, o_q = mid)
                inst = hold(i_clock = i_clock, i_d = mid, o_q = o_q)
                return instances()
        ''')
        with self.assertRaisesRegex(IsomorphError, 'lost a instance'):
            self.build(src)

    def test_a_chain_held_in_a_list_is_an_error (self):
        """A list still holds them against the rebinding check, but a
        chain is not an array: each cell takes a different wire, so
        there is no generate to write and the two want real names."""
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def hold (i_clock, i_d, o_q):
                @always_ff (i_clock.posedge)
                def hold_logic ():
                    o_q.next = i_d
                return instances()
            @block
            def unit (i_clock, i_d, o_q):
                mid = signal(8)
                cells = []
                cells.append(hold(i_clock = i_clock, i_d = i_d,
                                  o_q = mid))
                cells.append(hold(i_clock = i_clock, i_d = mid,
                                  o_q = o_q))
                return instances()
        ''')
        with self.assertRaises(ConversionError) as ctx:
            analyse(self.build(src))
        self.assertIn('cells[k].i_d', str(ctx.exception))
