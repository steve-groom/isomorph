"""Two things that look like hardware arithmetic and are not.

Index arithmetic on parameters, constants and unrolled loop variables is
settled before the netlist exists, so it is allowed in a clocked process.
An array of 1-bit signals indexed by a loop variable is an array
subscript, not a bit select; only the C emitter can tell them apart.
"""
import itertools
import linecache
import shutil
import textwrap
import unittest

from isomorph import (analyse, emit_sv, emit_c99, signal, Simulator,
                      ConversionError)

_counter = itertools.count()
HAVE_VERILATOR = shutil.which('verilator') is not None


def load (src):
    name = f'<ea{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


CHAIN = textwrap.dedent('''\
    from isomorph import *

    DEPTH = 4

    @block
    def chain (i_clock, i_data, o_data, STAGES = 3):
        s = signals(STAGES, WIDTH)

        @always_ff (i_clock.posedge)
        def chain_logic ():
            s[0].next = i_data
            for k in range(1, STAGES):
                s[k].next = s[k-1]

        @always_comb
        def chain_comb ():
            o_data.next = s[STAGES-1]
        return instances()
''')

REAL_ADD = textwrap.dedent('''\
    from isomorph import *

    @block
    def bad (i_clock, i_a, o_q):
        @always_ff (i_clock.posedge)
        def bad_logic ():
            o_q.next = (o_q + i_a)[7:0]
        return instances()
''')


def build (width = 1, stages = 3):
    ns = load(CHAIN.replace('WIDTH', str(width)))
    return ns['chain'](i_clock = signal(), i_data = signal(width),
                       o_data = signal(width), STAGES = stages)


class ElaborationArithmeticTests (unittest.TestCase):
    def test_loop_index_arithmetic_is_allowed_in_a_clocked_process (self):
        modules, _ = analyse(build())
        self.assertIn('s[k-1]', emit_sv(modules))

    def test_an_adder_in_a_clocked_process_converts (self):
        ns = load(REAL_ADD)
        top = ns['bad'](i_clock = signal(), i_a = signal(8),
                        o_q = signal(8))
        modules, warnings = analyse(top)
        self.assertIn('o_q <= ', emit_sv(modules))
        self.assertEqual(warnings, [])

    def test_the_same_adder_twice_is_not_said (self):
        """One adder per branch where a shared one would do is worse
        hardware and better left to the author: the fitter is the
        judge of it, and a warning on every run is noise."""
        src = REAL_ADD.replace(
            '        o_q.next = (o_q + i_a)[7:0]',
            '        if (i_a[0]):\n'
            '            o_q.next = (o_q + i_a)[7:0]\n'
            '        else:\n'
            '            o_q.next = (o_q + i_a)[7:0]')
        ns = load(src)
        top = ns['bad'](i_clock = signal(), i_a = signal(8),
                        o_q = signal(8))
        modules, warnings = analyse(top)
        self.assertEqual(warnings, [])
        self.assertIn('o_q <= ', emit_sv(modules))


class OneBitArrayTests (unittest.TestCase):
    """signals(N, 1) reached by a loop index used to emit C that read
    and wrote bits of a scalar, which did not compile."""

    def test_c99_uses_a_subscript_not_a_shift (self):
        modules, _ = analyse(build(width = 1))
        _, source = emit_c99(modules)
        self.assertIn('s->s__nxt[k]', source)
        self.assertNotIn('s->s__nxt =', source)

    def test_backends_agree_for_every_width_and_depth (self):
        backends = ['python', 'c99']
        if HAVE_VERILATOR:
            backends.append('verilator')
        for width in (1, 8):
            for stages in (2, 3):
                with self.subTest(width = width, stages = stages):
                    runs = [self.drive(b, width, stages) for b in backends]
                    for got in runs[1:]:
                        self.assertEqual(got, runs[0])
                    self.assertEqual(runs[0][-1], (1 << width) - 1)

    def drive (self, backend, width, stages):
        sim = Simulator(build(width, stages), backend = backend)
        sim.add_clock(10e-9)
        out = []
        for value in (1, 0, (1 << width) - 1):
            sim.set('i_data', value)
            sim.tick(stages)
            out.append(sim.get('o_data'))
        sim.close()
        return out


class ArrayParameterTests (unittest.TestCase):
    """A parameter that sizes an array has three uses to reach: the
    declaration, the loop over it and the index into it. It used to
    fold into all three, so declaring it would have been a parameter
    that changed nothing. Reaching two of the three would be worse
    than reaching none: the loop would write past the end."""

    def test_an_array_parameter_reaches_every_use (self):
        modules, _ = analyse(build())
        text = emit_sv(modules)
        self.assertIn('parameter STAGES', text)
        self.assertIn('logic s [STAGES];', text)
        self.assertIn('k < STAGES', text)
        self.assertIn('s[STAGES-1]', text)

    def test_two_stage_counts_are_one_module (self):
        """The point of the exercise: one block, one module, and the
        count at the instance."""
        from isomorph import block, instances, convert
        ns = load(CHAIN.replace('WIDTH', '1'))
        chain = ns['chain']

        @block
        def pair (i_clock, i_data, o_a, o_b):
            a = chain(i_clock = i_clock, i_data = i_data, o_data = o_a,
                      STAGES = 2)
            b = chain(i_clock = i_clock, i_data = i_data, o_data = o_b,
                      STAGES = 5)
            return instances()

        top = pair(i_clock = signal(), i_data = signal(),
                   o_a = signal(), o_b = signal())
        modules, _ = analyse(top)
        text = emit_sv(modules)
        self.assertEqual(text.count('module chain'), 1)
        self.assertIn('chain #(.STAGES(5))', text)

    def test_only_a_width_named_parameter_renames_a_width (self):
        """Matching on value alone renamed any width that happened to
        equal any parameter, which in VHDL became a generic that would
        resize the ports."""
        src = CHAIN.replace('WIDTH', '3').replace('STAGES = 3',
                                                  'TIMEOUT = 3')
        src = src.replace('STAGES', 'TIMEOUT')
        ns = load(src)
        top = ns['chain'](i_clock = signal(), i_data = signal(3),
                          o_data = signal(3), TIMEOUT = 3)
        text = emit_sv(analyse(top)[0])
        self.assertIn('logic [2:0]', text)
        # the index into a TIMEOUT-deep array says TIMEOUT-1 and should;
        # what must not happen is a width declared from it
        self.assertNotIn('[TIMEOUT-1:0]', text)

    def test_a_width_named_parameter_still_does (self):
        src = CHAIN.replace('WIDTH', 'WIDTHD').replace(
            'def chain (i_clock, i_data, o_data, STAGES = 3):',
            'def chain (i_clock, i_data, o_data, STAGES = 3, WIDTHD = 3):')
        ns = load(src)
        top = ns['chain'](i_clock = signal(), i_data = signal(3),
                          o_data = signal(3), STAGES = 3, WIDTHD = 3)
        text = emit_sv(analyse(top)[0])
        self.assertIn('WIDTHD-1', text)


TAIL = textwrap.dedent("""\
    from isomorph import *

    @block
    def tail (i_clock, i_beat, o_last, COUNT = 4):
        @always_ff (i_clock.posedge)
        def tail_logic ():
            o_last.next = (i_beat == COUNT - 1)

        return instances()
""")


class IntegerArithmeticWidthTests (unittest.TestCase):
    """COUNT - 1 is integer arithmetic in both languages, so it is 32
    bits wide whatever COUNT is. Beside a two-bit signal that is what
    Verilator calls WIDTHEXPAND, and the fix is the width it is read
    at: to_unsigned(COUNT - 1, 4) in the VHDL, and the same thing said
    as 4'(COUNT - 1) in the SystemVerilog rather than the bare
    subtraction that used to be emitted."""

    def build (self):
        ns = load(TAIL)
        return ns['tail'](i_clock = signal(), i_beat = signal(2),
                          o_last = signal())

    def test_both_sides_of_the_compare_are_one_width (self):
        text = emit_sv(analyse(self.build())[0])
        self.assertIn("(4'(i_beat) == 4'(COUNT - 1))", text)

    def test_the_vhdl_already_said_it (self):
        from isomorph import emit_vhdl
        text = emit_vhdl(analyse(self.build())[0])
        self.assertIn('to_unsigned((COUNT - 1)', text)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_has_nothing_to_say (self):
        import os, tempfile
        from isomorph.emit_sv import lint_sv
        directory = tempfile.mkdtemp(prefix = 'iso_int_')
        path = os.path.join(directory, 'tail.sv')
        with open(path, 'w', encoding = 'ascii') as f:
            f.write(emit_sv(analyse(self.build())[0]))
        lint_sv(path, top = 'tail')


if __name__ == '__main__':
    unittest.main()
