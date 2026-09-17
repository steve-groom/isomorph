import textwrap
import unittest
import linecache
import itertools

_counter = itertools.count()


def load (src):
    """exec a block source under a unique pseudo-filename that inspect can
    read back through linecache."""
    name = f'<unit{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    namespace = {}
    exec(compile(src, name, 'exec'), namespace)
    return namespace

from isomorph import (block, signal, signals, vector, enum, struct,
    always_comb, always_ff, assign, concat, replicate, instances, analyse,
    ConversionError)
from isomorph import ir


def comb (body_source, allow_severe = False, **widths):
    """Build a block whose one comb process is the given source lines;
    signals a, b, c, d and o with the given widths (default 8)."""
    width = {n: widths.get(n, 8) for n in 'abcdo'}
    src = ('from isomorph import *\n'
           '@block\n'
           'def unit (i_clock, a, b, c, d, o, K = 5):\n'
           '    t = signal(8)\n'
           '    state = enum("S0", "S1")\n'
           '    fsm = signal(state)\n'
           '    mem = signals(4, 8)\n'
           '    @always_comb\n'
           '    def unit_comb ():\n')
    src += ''.join(f'        {line}\n' for line in body_source.splitlines())
    src += '    return instances()\n'
    namespace = load(src)
    sigs = {n: signal(width[n]) for n in 'abcdo'}
    top = namespace['unit'](i_clock = signal(), **sigs)
    return analyse(top, allow_severe)


def ff (body_source):
    src = ('from isomorph import *\n'
           '@block\n'
           'def unit (i_clock, a, b, o):\n'
           '    t = signal(8)\n'
           '    def f (x):\n'
           '        r = vector(8)\n'
           '        r[7:0] = x\n'
           '        return r\n'
           '    @always_ff (i_clock.posedge)\n'
           '    def unit_logic ():\n')
    src += ''.join(f'        {line}\n' for line in body_source.splitlines())
    src += '    return instances()\n'
    namespace = load(src)
    top = namespace['unit'](i_clock = signal(), a = signal(8), b = signal(8),
                            o = signal(8))
    return analyse(top)


def first_value (modules):
    return modules[0].processes[0].body[0].value


class WidthRuleTests(unittest.TestCase):
    def test_const_takes_target_width (self):
        modules, _ = comb('o.next = 5')
        v = first_value(modules)
        self.assertEqual((v.op, v.width), ('const', 8))

    def test_const_too_wide_for_target (self):
        with self.assertRaisesRegex(ConversionError, 'does not fit in 8 bits'):
            comb('o.next = 256')

    def test_const_takes_partner_width (self):
        modules, _ = comb('o.next = a & 3')
        v = first_value(modules)
        self.assertEqual(v.args[1].width, 8)

    def test_const_wider_than_partner (self):
        with self.assertRaisesRegex(ConversionError, 'wider than 8-bit'):
            comb('o.next = a & 300')

    def test_add_is_max_plus_one_and_must_be_sliced (self):
        with self.assertRaisesRegex(ConversionError,
                                    'result truncated: 9-bit'):
            comb('o.next = a + b')
        modules, _ = comb('o.next = (a + b)[7:0]')
        v = first_value(modules)
        # sliced straight back to the width of what went in, which is
        # the widening undone: the sum is asked for at eight bits
        # rather than taken at nine and then cut down again
        self.assertEqual((v.op, v.width), ('binop', 8))
        self.assertEqual([a.width for a in v.args], [8, 8])

    def test_logic_width_mismatch_warns (self):
        modules, warnings = comb('o.next = a & c', c = 4)
        self.assertTrue(any('& on 8 and 4' in w for w in warnings))
        self.assertEqual(first_value(modules).width, 8)

    def test_narrow_value_is_extended (self):
        modules, _ = comb('o.next = c', c = 4)
        v = first_value(modules)
        self.assertEqual((v.op, v.width, v.args[0].width), ('extend', 8, 4))

    def test_concat_and_replicate (self):
        modules, _ = comb('o.next = concat(replicate(a[7], 4), a[3:0])')
        v = first_value(modules)
        self.assertEqual((v.op, v.width), ('concat', 8))
        self.assertEqual((v.args[0].op, v.args[0].width), ('replicate', 4))

    def test_unsized_int_in_concat_warns_bool_does_not (self):
        _, warnings = comb('o.next = concat(a[6:0], False)')
        self.assertFalse(any('unsized' in w for w in warnings))
        _, warnings = comb('o.next = concat(a[6:0], 0)')
        self.assertTrue(any('unsized constant 0' in w for w in warnings))

    def test_const_in_a_concat_is_sized_and_does_not_warn (self):
        """const(0, 4) says how wide it is, so warning that it took
        the width it was given would be nonsense."""
        modules, warnings = comb('o.next = concat(const(0, 4), a[3:0])')
        self.assertFalse(any('unsized' in w for w in warnings))
        v = first_value(modules)
        self.assertEqual((v.op, v.width), ('concat', 8))
        self.assertEqual((v.args[0].op, v.args[0].width), ('const', 4))

    def test_ones_is_a_fill_of_that_width (self):
        modules, _ = comb('o.next = ones(8)')
        v = first_value(modules)
        self.assertEqual((v.op, v.width, v.value), ('replicate', 8, 8))
        self.assertEqual((v.args[0].op, v.args[0].value), ('const', 1))

    def test_ones_follows_a_parameter (self):
        modules, _ = comb('o.next = ones(K)', o = 5)
        v = first_value(modules)
        self.assertEqual((v.op, v.width), ('replicate', 5))
        self.assertEqual(v.args[1].value, 'K')

    def test_zeroes_is_a_fill_of_that_width (self):
        modules, _ = comb('o.next = zeroes(8)')
        v = first_value(modules)
        self.assertEqual((v.op, v.width, v.value), ('replicate', 8, 8))
        self.assertEqual((v.args[0].op, v.args[0].value), ('const', 0))

    def test_a_house_name_that_was_not_imported_says_so (self):
        """The block body is read from the AST, so an unimported name
        never raises where Python would: it arrives as a call to
        nothing."""
        src = textwrap.dedent("""\
            from isomorph import (block, signal, always_comb, instances)

            @block
            def unit (o):
                @always_comb
                def o_comb ():
                    o.next = ones(8)
                return instances()
        """)
        design = load(src)['unit'](o = signal(8))
        with self.assertRaisesRegex(ConversionError, 'does not import'):
            analyse(design)

    def test_a_shift_in_ff_is_not_checked (self):
        """HOUSE_STYLE's rule is about adders: it says precalculate
        arithmetic next values, and every example in it is an adder.
        A shift was never in it, by a constant or by a signal."""
        for src in ('o.next = (a >> 1)[7:0]', 'o.next = (a >> b)[7:0]'):
            modules, _ = ff(src)
            v = first_value(modules)
            self.assertEqual(v.op, 'slice')
            self.assertEqual(v.args[0].value, '>>')

    def test_shift_by_signal_in_comb_is_ok (self):
        modules, _ = comb('o.next = (a >> b)[7:0]')
        v = first_value(modules)
        self.assertEqual(v.op, 'slice')
        self.assertEqual(v.args[0].value, '>>')

    def test_compare_is_one_bit (self):
        modules, _ = comb('o.next = concat(replicate(False, 7), a == b)')
        self.assertEqual(first_value(modules).args[1].width, 1)


class SliceTests(unittest.TestCase):
    def test_inclusive_slice (self):
        modules, _ = comb('o.next = concat(a[3:0], a[7:4])')
        v = first_value(modules)
        self.assertEqual(v.args[0].value, (4, 0))
        self.assertEqual(v.args[0].width, 4)

    def test_reversed_slice (self):
        with self.assertRaisesRegex(ConversionError, 'reversed slice'):
            comb('o.next = a[0:7]')

    def test_slice_out_of_range (self):
        with self.assertRaisesRegex(ConversionError, 'outside bits 7:0'):
            comb('o.next = concat(a[8:1], False)')

    def test_open_slice_is_an_error (self):
        with self.assertRaisesRegex(ConversionError, 'open slices'):
            comb('o.next = a[7:]')

    def test_part_select_shape (self):
        modules, _ = comb(
            'o.next = concat(replicate(False, 4), a[b*4 + 3:b*4])')
        v = first_value(modules).args[1]
        self.assertEqual((v.op, v.width), ('part', 4))

    def test_part_select_method (self):
        modules, _ = comb('o.next = concat(replicate(False, 4), a.part(b, 4))')
        v = first_value(modules).args[1]
        self.assertEqual((v.op, v.width), ('part', 4))

    def test_part_select_down (self):
        modules, _ = comb(
            'o.next = concat(replicate(False, 4), a.part_down(b, 4))')
        v = first_value(modules).args[1]
        self.assertEqual((v.op, v.width), ('part_down', 4))
        self.assertIn('-:', ir.render(v))

    def test_bad_variable_slice (self):
        with self.assertRaisesRegex(ConversionError, r'sig\.part'):
            comb('o.next = concat(replicate(False, 4), a[b*2 + 3:b])')

    def test_bit_index_by_signal (self):
        modules, _ = comb('o.next = concat(replicate(False, 7), a[b])')
        v = first_value(modules).args[1]
        self.assertEqual((v.op, v.width, v.args[1].op), ('bit', 1, 'ref'))

    def test_bit_target_with_signal_index (self):
        modules, _ = comb('o.next = 0\no.next[b[2:0]] = a[0]')
        target = modules[0].processes[0].body[1].target
        self.assertEqual((target.op, target.width), ('bit', 1))


class StatementTests(unittest.TestCase):
    def test_constant_if_is_pruned (self):
        modules, _ = comb('o.next = a\nif (K == 5):\n    o.next = b')
        body = modules[0].processes[0].body
        self.assertIsInstance(body[1], ir.If)
        self.assertIsNone(body[1].branches[0][0])

    def test_elif_else_chain (self):
        modules, _ = comb('o.next = a\n'
                          'if (a == 0):\n    o.next = b\n'
                          'elif (a == 1):\n    o.next = c\n'
                          'else:\n    o.next = d')
        branches = modules[0].processes[0].body[1].branches
        self.assertEqual(len(branches), 3)
        self.assertIsNone(branches[2][0])

    def test_for_range (self):
        modules, _ = comb('o.next = 0\n'
                          'for i in range(8):\n    o.next[i] = a[i]')
        loop = modules[0].processes[0].body[1]
        self.assertEqual((loop.var, loop.start, loop.stop), ('i', 0, 8))

    def test_enum_compare (self):
        modules, _ = comb('o.next = 0\nif (fsm == state.S1):\n    o.next = a')
        cond = modules[0].processes[0].body[1].branches[0][0]
        self.assertEqual((cond.op, cond.args[1].op), ('cmp', 'enum'))

    def test_array_element_constant_and_signal_index (self):
        modules, _ = comb('o.next = mem[2]')
        self.assertEqual(first_value(modules).value, 'mem[2]')

    def test_and_or_keywords_rejected (self):
        with self.assertRaisesRegex(ConversionError, 'not hardware operators'):
            comb('o.next = concat(replicate(False, 7), a[0] and b[0])')

    def test_augmented_assignment_rejected (self):
        with self.assertRaisesRegex(ConversionError, 'augmented'):
            comb('o.next += 1')


class CheckTests(unittest.TestCase):
    def test_double_driver (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (a, o):\n'
               '    @always_comb\n'
               '    def p_comb ():\n'
               '        o.next = a\n'
               '    @always_comb\n'
               '    def q_comb ():\n'
               '        o.next = a\n'
               '    return instances()\n')
        namespace = load(src)
        with self.assertRaisesRegex(ConversionError, 'driven by both'):
            analyse(namespace['unit'](a = signal(8), o = signal(8)))

    def test_incomplete_comb_is_a_latch (self):
        with self.assertRaisesRegex(ConversionError, 'latch'):
            comb('if (a[0]):\n    o.next = b')

    def test_a_latch_can_be_carried_with_allow_severe (self):
        modules, warnings = comb('if (a[0]):\n    o.next = b',
                                 allow_severe = True)
        self.assertTrue(any('severe:' in w and 'latch' in w for w in warnings))
        self.assertEqual(len(modules), 1)

    def test_inverter_ring_stops_the_run (self):
        with self.assertRaisesRegex(ConversionError, 'combinational loop'):
            comb('o.next = ~o')

    def test_reading_what_this_process_already_assigned_is_not_a_loop (self):
        """The house idiom: a legal base value, then stacked overrides.

        Blocking order makes the second read the value from the line
        above, not a ring, and the emitted always_comb says so."""
        modules, warnings = comb('o.next = 0\n'
                                 'if (a[0]):\n'
                                 '    o.next = o | b')
        self.assertFalse(any('combinational loop' in w for w in warnings))
        self.assertEqual(len(modules), 1)

    def test_two_inverter_ring_stops_the_run (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def ring (o_q):\n'
               '    a = signal()\n'
               '    b = signal()\n'
               '    @always_comb\n'
               '    def a_comb ():\n'
               '        a.next = ~b\n'
               '    @always_comb\n'
               '    def b_comb ():\n'
               '        b.next = ~a\n'
               '    @always_comb\n'
               '    def out_comb ():\n'
               '        o_q.next = a\n'
               '    return instances()\n')
        ns = load(src)
        with self.assertRaisesRegex(ConversionError, 'combinational loop'):
            analyse(ns['ring'](o_q = signal()))
        modules, warnings = analyse(ns['ring'](o_q = signal()),
                                    allow_severe = True)
        self.assertTrue(any('combinational loop' in w for w in warnings))
        self.assertEqual(modules[-1].name, 'ring')

    def test_complete_with_else_does_not_warn (self):
        _, warnings = comb('if (a[0]):\n    o.next = b\nelse:\n    o.next = c')
        self.assertFalse(any('every path' in w for w in warnings))

    def test_ff_arithmetic_is_allowed (self):
        """Where to put an adder is style. Hoisting the repeat into a
        comb process is better form and isomorph does not insist."""
        modules, warnings = ff('o.next = (a + b)[7:0]')
        self.assertEqual([w for w in warnings if 'built' in w], [])

    def test_ff_magnitude_compare_is_allowed (self):
        """A comparator in a clocked process was an error on the
        grounds that it is a subtractor. So is an adder, and that was
        always allowed."""
        modules, warnings = ff('if (a < b):\n    o.next = a')
        self.assertEqual([w for w in warnings if 'built' in w], [])
        self.assertEqual(modules[0].processes[0].kind, 'ff')

    def test_ff_function_call_banned (self):
        with self.assertRaisesRegex(ConversionError,
                                    'clocked process: call it'):
            ff('o.next = f(a)')

    def test_ff_select_allowed (self):
        modules, _ = ff('if (a == b):\n    o.next = a\n'
                        'else:\n    o.next = concat(a[6:0], b[7])')
        self.assertEqual(modules[0].processes[0].kind, 'ff')

    def test_read_before_write (self):
        with self.assertRaisesRegex(ConversionError, 'read before assigned'):
            comb('t.next = o\no.next = a')

    def test_read_after_write_is_ok (self):
        modules, _ = comb('o.next = a\nt.next = o')
        self.assertEqual(len(modules[0].processes[0].body), 2)

    def test_unused_port_warns (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (a, unused_i, o):\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        o.next = a\n'
               '    return instances()\n')
        namespace = load(src)
        _, warnings = analyse(namespace['unit'](
            a = signal(8), unused_i = signal(8), o = signal(8)))
        self.assertTrue(any('unused port unused_i' in w for w in warnings))

    def test_unused_signal_warns (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (a, o):\n'
               '    dead = signal(8)\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        o.next = a\n'
               '    return instances()\n')
        namespace = load(src)
        _, warnings = analyse(namespace['unit'](a = signal(8), o = signal(8)))
        self.assertTrue(any('unused signal dead' in w for w in warnings))

    def test_vhdl_reserved_word_is_an_error (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (signal, o):\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        o.next = signal\n'
               '    return instances()\n')
        namespace = load(src)
        with self.assertRaisesRegex(ConversionError, 'VHDL reserved word'):
            analyse(namespace['unit'](signal = signal(), o = signal()))

    def test_sv_reserved_word_is_an_error (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (logic, o):\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        o.next = logic\n'
               '    return instances()\n')
        namespace = load(src)
        with self.assertRaisesRegex(ConversionError,
                                    'SystemVerilog reserved word'):
            analyse(namespace['unit'](logic = signal(), o = signal()))

    def test_vhdl_reserved_fails_even_for_sv_only (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (signal, o):\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        o.next = signal\n'
               '    return instances()\n')
        namespace = load(src)
        from isomorph import convert
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), 'unit.sv')
        with self.assertRaisesRegex(ConversionError, 'VHDL reserved word'):
            convert(namespace['unit'](signal = signal(), o = signal()),
                    sv = path, vhdl = False)

    def test_sv_reserved_fails_even_for_vhdl_only (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (logic, o):\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        o.next = logic\n'
               '    return instances()\n')
        namespace = load(src)
        from isomorph import convert
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), 'unit.vhd')
        with self.assertRaisesRegex(ConversionError,
                                    'SystemVerilog reserved word'):
            convert(namespace['unit'](logic = signal(), o = signal()),
                    sv = False, vhdl = path)

    def test_vhdl_case_collision_is_an_error (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (o):\n'
               '    state = enum("IDLE", "RUN")\n'
               '    run = signal()\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        run.next = True\n'
               '        o.next = run\n'
               '    return instances()\n')
        namespace = load(src)
        with self.assertRaisesRegex(ConversionError, 'case-insensitive'):
            analyse(namespace['unit'](o = signal()))


class NamedConstantTests(unittest.TestCase):
    def test_named_constant_is_a_ref (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (o):\n'
               '    RESET = 5\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        o.next = RESET\n'
               '    return instances()\n')
        namespace = load(src)
        modules, _ = analyse(namespace['unit'](o = signal(8)))
        v = modules[0].processes[0].body[0].value
        # the name survives into the HDL, sized to what it is assigned
        # to rather than padded out by the value it happens to hold.
        # The cast is the target's width, which is a property of the
        # module and the same in every build, so two builds still
        # merge; the width is said here rather than
        # left to the tool's context rules, and VHDL always said it
        self.assertEqual(v.op, 'extend')
        self.assertEqual(v.width, 8)
        self.assertEqual(v.args[0].op, 'ref')
        self.assertEqual(v.args[0].value, 'RESET')
        self.assertEqual(v.width, 8)

    def test_slice_bound_keeps_parameter_name (self):
        modules, _ = comb('o.next = a[K-1:0]', o = 5)
        sl = first_value(modules)
        self.assertEqual(sl.op, 'slice')
        self.assertEqual(sl.args[1].op, 'binop')
        self.assertEqual(sl.args[1].args[0].value, 'K')


class MatchTests(unittest.TestCase):
    def test_bits_match (self):
        modules, _ = comb(
            'o.next = 0\n'
            'match a:\n'
            '    case bits(\'1???????\'):\n'
            '        o.next = b\n'
            '    case _:\n'
            '        o.next = c'
        )
        m = modules[0].processes[0].body[1]
        self.assertIsInstance(m, ir.Match)
        self.assertEqual(m.arms[0][0].op, 'bits')
        self.assertEqual(m.arms[0][0].value, '1???????')
        self.assertIsNone(m.arms[1][0])

    def test_enum_match_unique (self):
        modules, _ = comb(
            'o.next = 0\n'
            'match fsm:\n'
            '    case state.S0:\n'
            '        o.next = a\n'
            '    case state.S1:\n'
            '        o.next = b'
        )
        m = modules[0].processes[0].body[1]
        self.assertTrue(m.unique)
        self.assertEqual(m.arms[0][0].op, 'enum')

    def test_complete_enum_match_is_not_a_latch (self):
        modules, warnings = comb(
            'match fsm:\n'
            '    case state.S0:\n'
            '        o.next = a\n'
            '    case state.S1:\n'
            '        o.next = b')
        self.assertFalse(any('latch' in w or 'every path' in w
                             for w in warnings))
        self.assertEqual(len(modules[0].processes[0].body), 1)

    def test_match_without_default_is_a_latch (self):
        with self.assertRaisesRegex(ConversionError, 'latch'):
            comb(
                'match a:\n'
                '    case bits(\'00000000\'):\n'
                '        o.next = b')

    def test_enum_match_missing_member (self):
        with self.assertRaisesRegex(ConversionError, 'missing enum members'):
            comb(
                'o.next = 0\n'
                'match fsm:\n'
                '    case state.S0:\n'
                '        o.next = a'
            )


class StructTests(unittest.TestCase):
    def test_field_read_and_write (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (i_addr, i_data, o_addr, o_data):\n'
               '    cmd_t = struct("cmd_t", address = 8, write = 1,'
               ' data = 8)\n'
               '    cmd = signal(cmd_t)\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        cmd.address.next = i_addr\n'
               '        cmd.write.next = True\n'
               '        cmd.data.next = i_data\n'
               '        o_addr.next = cmd.address\n'
               '        o_data.next = cmd.data\n'
               '    return instances()\n')
        namespace = load(src)
        modules, _ = analyse(namespace['unit'](
            i_addr = signal(8), i_data = signal(8),
            o_addr = signal(8), o_data = signal(8)))
        assigns = [s for s in modules[0].processes[0].body
                   if isinstance(s, ir.Assign)]
        self.assertEqual(assigns[0].target.op, 'field')
        self.assertEqual(assigns[0].target.value, 'address')
        self.assertEqual(assigns[0].target.width, 8)
        self.assertEqual(assigns[3].value.op, 'field')
        self.assertEqual(assigns[3].value.value, 'address')
        # packed: address is the MSB, data occupies [7:0]
        self.assertEqual(assigns[2].target.args[1].value, 0)

    def test_next_then_field (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (i_addr, o_addr):\n'
               '    cmd_t = struct("cmd_t", address = 8, data = 8)\n'
               '    cmd = signal(cmd_t)\n'
               '    @always_comb\n'
               '    def unit_comb ():\n'
               '        cmd.next.address = i_addr\n'
               '        cmd.next.data = 0\n'
               '        o_addr.next = cmd.address\n'
               '    return instances()\n')
        namespace = load(src)
        modules, _ = analyse(namespace['unit'](
            i_addr = signal(8), o_addr = signal(8)))
        tgt = modules[0].processes[0].body[0].target
        self.assertEqual((tgt.op, tgt.value), ('field', 'address'))


class FunctionTests(unittest.TestCase):
    def test_function_width_from_call_site (self):
        src = ('from isomorph import *\n'
               '@block\n'
               'def unit (a, o):\n'
               '    def widen (x):\n'
               '        r = vector(8)\n'
               '        r[7:0] = concat(replicate(x[3], 4), x[3:0])\n'
               '        return r\n'
               '    @always_comb\n'
               '    def p_comb ():\n'
               '        o.next = widen(a[3:0])\n'
               '    return instances()\n')
        namespace = load(src)
        modules, _ = analyse(namespace['unit'](a = signal(8), o = signal(8)))
        f = modules[0].functions[0]
        self.assertEqual((f.name, f.params[0][1], f.width), ('widen', 4, 8))
        self.assertEqual(first_value(modules).op, 'call')


if __name__ == '__main__':
    unittest.main()


class HangingTests (unittest.TestCase):
    """Every way a name can fail to be connected at both ends.

    A signal read by something and driven by nothing is the one worth
    having: it simulates as zero and builds as whatever the fitter
    leaves. isomorph used to ask only whether a name was read or
    driven, so that one counted as used and was never mentioned.
    """

    SRC = textwrap.dedent('''\
        from isomorph import *

        VALUES = [3, 1, 4, 1]


        @block
        def hanging (i_a, o_y, o_never):
            floating = signal(8)        # read below, driven by nothing
            dead = signal(8)            # driven below, read by nothing
            rom = signals(4, 8)         # driven by preload, read below
            preload(rom, VALUES)

            @always_comb
            def hanging_comb ():
                o_y.next = i_a | floating | rom[0]
                dead.next = i_a

            return instances()
    ''')

    def warnings (self):
        ns = load(self.SRC)
        top = ns['hanging'](i_a = signal(8), o_y = signal(8),
                            o_never = signal(8))
        return [' '.join(w.split()) for w in analyse(top)[1]]

    def test_a_floating_signal_is_named (self):
        found = [w for w in self.warnings() if 'undriven signal' in w]
        self.assertEqual(len(found), 1)
        self.assertIn('floating', found[0])

    def test_a_signal_nothing_reads_is_named (self):
        found = [w for w in self.warnings() if 'unread signal' in w]
        self.assertEqual(len(found), 1)
        self.assertIn('dead', found[0])

    def test_a_port_at_neither_end_is_named (self):
        found = [w for w in self.warnings() if 'unused port' in w]
        self.assertEqual(len(found), 1)
        self.assertIn('o_never', found[0])

    def test_a_preloaded_memory_is_driven_by_its_contents (self):
        self.assertFalse([w for w in self.warnings() if 'rom' in w])


class SignExtendTests (unittest.TestCase):
    """sign_extend(value, width): value widened to width, sign kept.

    The second argument is the width of the answer, which is the whole
    point and the reason this is not replicate() renamed. The old
    spelling made the reader check that the repeated bit was the
    slice's top one, that the slice was three wide, and that WIDTHD-3
    still matched if the slice ever changed.
    """

    def test_it_matches_the_concat_it_replaces (self):
        """concat(replicate(a[2], 5), a[2:0]) written the short way."""
        modules, _ = comb('o.next = sign_extend(a[2:0], 8)')
        v = first_value(modules)
        self.assertEqual((v.op, v.width, v.signed), ('extend', 8, True))
        self.assertEqual(v.args[0].width, 3)

    def test_a_single_bit_is_n_copies_of_it (self):
        """So the old one-bit spelling keeps working under the new
        name: extending one bit to n is n copies."""
        modules, _ = comb('o.next = sign_extend(a[3], 8)')
        v = first_value(modules)
        self.assertEqual((v.op, v.width), ('extend', 8))

    def test_the_width_it_already_has_changes_nothing (self):
        modules, _ = comb('o.next = sign_extend(a[7:0], 8)')
        v = first_value(modules)
        self.assertEqual(v.op, 'slice')

    def test_asking_for_fewer_bits_is_an_error (self):
        """Not a silent truncation, which is the whole house rule."""
        with self.assertRaises(ConversionError) as ctx:
            comb('o.next = sign_extend(a[7:0], 4)')
        self.assertIn('width of the answer', str(ctx.exception))

    def test_replicate_still_takes_a_sized_constant (self):
        """replicate(const(0b0111, 4), 2) is 0b01110111, which is one
        of the two things the operator is still good for."""
        modules, warnings = comb('o.next = replicate(const(0b0111, 4), 2)')
        self.assertFalse(any('unsized' in w for w in warnings))
        v = first_value(modules)
        self.assertEqual((v.op, v.width), ('replicate', 8))

    def test_a_negative_const_is_the_pattern_it_makes (self):
        """const(-1, 8) is eight ones, not an illegal 8'd-1."""
        modules, _ = comb('o.next = const(-1, 8)')
        v = first_value(modules)
        self.assertEqual((v.op, v.width, v.value), ('const', 8, 255))
