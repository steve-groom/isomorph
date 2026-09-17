"""A width derived from a parameter keeps the expression.

signal(WIDTH - 1) declares [WIDTH-2:0], not the [6:0] that one
elaboration happened to produce. A slice bound is already
emitted as written; this is the same rule for a declaration, and it is
the same argument as names travelling and comments travelling.

The expression is used only where it is provably the one that built the
signal: every name in it is something the module declares at that
point, and working it out with their values gives the width the design
really has. Everything else falls back to the literal, because a
declaration that said something the design does not do would be worse
than an unreadable one.
"""
import os
import shutil
import tempfile
import unittest

from isomorph import (block, signal, signals, always_comb, always_ff,
    instances, analyse, emit_sv, emit_vhdl)
from isomorph.emit_sv import lint_sv


def sv_of (top):
    return emit_sv(analyse(top)[0])


def vhdl_of (top):
    return emit_vhdl(analyse(top)[0])


@block
def widths (i_data, o_narrow, WIDTH = 8):
    """Every shape of derived width, in one block."""
    WIDTHH = WIDTH // 2
    wider = signal(WIDTH + 1)
    narrower = signal(WIDTH - 1)
    half = signal(WIDTHH + 1)
    doubled = signal(WIDTH * 2)
    plain = signal(7)
    mem = signals(4, WIDTH - 1)

    @always_comb
    def widths_comb ():
        wider.next = i_data
        narrower.next = i_data[WIDTH-2:0]
        half.next = i_data[WIDTHH:0]
        doubled.next = i_data
        plain.next = i_data[5:0]
        o_narrow.next = narrower

    return instances()


def elaborate_widths (WIDTH = 8):
    return widths(i_data = signal(WIDTH),
                  o_narrow = signal(WIDTH - 1), WIDTH = WIDTH)


class TestWidthExpression(unittest.TestCase):

    def setUp (self):
        self.sv = sv_of(elaborate_widths())

    def test_a_derived_width_keeps_the_expression (self):
        self.assertIn('logic [WIDTH-2:0] narrower', self.sv)

    def test_one_more_folds_into_the_top_bit (self):
        """WIDTH + 1 bits is bits WIDTH down to 0, not WIDTH+1-1."""
        self.assertIn('logic [WIDTH:0] wider', self.sv)

    def test_one_less_folds_the_other_way (self):
        self.assertNotIn('WIDTH-1-1', self.sv)
        self.assertNotIn('WIDTH - 1 - 1', self.sv)

    def test_a_constant_is_named_where_the_author_named_it (self):
        """WIDTHH is a localparam, and a signal may use one."""
        self.assertIn('logic [WIDTHH:0] half', self.sv)

    def test_multiplication_keeps_its_precedence (self):
        self.assertIn('logic [WIDTH*2-1:0] doubled', self.sv)

    def test_a_literal_width_stays_a_literal (self):
        self.assertIn('logic [6:0] plain', self.sv)

    def test_an_array_element_keeps_the_expression (self):
        self.assertIn('logic [WIDTH-2:0] mem [4]', self.sv)

    def test_a_port_keeps_the_expression (self):
        """The elaborate function wrote signal(WIDTH - 1), and WIDTH is
        the block's own parameter with the value that built it."""
        self.assertIn('output logic [WIDTH-2:0] o_narrow', self.sv)

    def test_the_localparam_it_needs_is_emitted (self):
        """And it says where four came from, not just four."""
        self.assertIn('localparam WIDTHH = WIDTH/2;', self.sv)

    def test_vhdl_says_the_same (self):
        text = vhdl_of(elaborate_widths())
        self.assertIn('std_logic_vector(WIDTH - 2 downto 0)', text)
        self.assertIn('std_logic_vector(WIDTH downto 0)', text)
        self.assertIn('std_logic_vector(WIDTHH downto 0)', text)

    def test_another_width_builds_the_same_text (self):
        """The point of the exercise: the declarations do not move."""
        narrow = sv_of(elaborate_widths(WIDTH = 6))
        for line in ('logic [WIDTH-2:0] narrower',
                     'logic [WIDTH:0] wider',
                     'output logic [WIDTH-2:0] o_narrow'):
            self.assertIn(line, narrow)


class TestWidthExpressionRefused(unittest.TestCase):
    """Where the expression is not provably the one that built it."""

    def test_a_port_may_not_name_a_localparam (self):
        """A port list is above the localparams, so naming one emits a
        module that does not compile. Found on stream_from_memory,
        where Verilator said it could not find WIDTHD."""
        @block
        def ported (i_data, o_data):
            WIDTHD = 8
            inner = signal(WIDTHD)

            @always_comb
            def ported_comb ():
                inner.next = i_data
                o_data.next = inner

            return instances()

        WIDTHD = 8
        text = sv_of(ported(i_data = signal(WIDTHD),
                            o_data = signal(WIDTHD)))
        self.assertIn('input  logic [7:0] i_data', text)
        # the signal, which is below the localparam, may use it
        self.assertIn('logic [WIDTHD-1:0] inner', text)

    def test_a_name_the_module_never_declared_is_refused (self):
        """The elaborate function's own local is not a parameter of the
        block, and matching by name alone would be a guess."""
        @block
        def anon (i_data, o_data):
            @always_comb
            def anon_comb ():
                o_data.next = i_data

            return instances()

        OUTSIDE = 8
        text = sv_of(anon(i_data = signal(OUTSIDE),
                          o_data = signal(OUTSIDE)))
        self.assertIn('[7:0] i_data', text)
        self.assertNotIn('OUTSIDE', text)

    def test_an_expression_that_does_not_give_the_width_is_refused (self):
        """A shadowed name evaluates to something else, and the
        declaration must not claim it."""
        @block
        def shadow (i_data, o_data, WIDTH = 4):
            @always_comb
            def shadow_comb ():
                o_data.next = i_data

            return instances()

        WIDTH = 8                       # not the block's WIDTH
        text = sv_of(shadow(i_data = signal(WIDTH),
                            o_data = signal(WIDTH), WIDTH = 4))
        # WIDTH is 4 in the module and the ports are 8 bits, so the
        # expression cannot be the one that built them
        self.assertIn('[7:0] i_data', text)
        self.assertNotIn('[WIDTH-1:0] i_data', text)


if __name__ == '__main__':
    unittest.main()


class TestConstantExpression(unittest.TestCase):
    """A constant is emitted as the author wrote it.

    m.constants is name -> int, so WIDTHH = WIDTH // 2 emitted
    localparam WIDTHH = 16 and the arithmetic of a block came out as a
    set of unrelated numbers.
    """

    def build (self):
        @block
        def sums (i_data, o_data, WIDTH = 8):
            HALF = WIDTH // 2
            QUARTER = HALF // 2
            SPAN = WIDTH * 2 + 1
            FIXED = 12
            FROM_PORT = len(i_data)
            held = signal(WIDTH)
            low = signal(HALF)
            tiny = signal(QUARTER)
            span_sig = signal(SPAN)
            fixed_sig = signal(FIXED)
            ported = signal(FROM_PORT)

            @always_comb
            def sums_comb ():
                held.next = i_data
                low.next = i_data[HALF-1:0]
                tiny.next = i_data[QUARTER-1:0]
                span_sig.next = i_data
                fixed_sig.next = i_data
                ported.next = i_data
                o_data.next = held

            return instances()

        return sums(i_data = signal(8), o_data = signal(8))

    def test_a_constant_over_a_parameter (self):
        self.assertIn('localparam HALF = WIDTH/2;', sv_of(self.build()))

    def test_a_constant_over_an_earlier_constant (self):
        self.assertIn('localparam QUARTER = HALF/2;', sv_of(self.build()))

    def test_precedence_is_kept (self):
        self.assertIn('localparam SPAN = WIDTH*2+1;', sv_of(self.build()))

    def test_a_plain_number_stays_a_number (self):
        self.assertIn('localparam FIXED = 12;', sv_of(self.build()))

    def test_a_port_width_becomes_a_generic_not_a_localparam (self):
        """len(i_data) names that port's width, so it is a parameter
        and the port follows it. It is not also a
        localparam: that would be the same thing said twice."""
        text = sv_of(self.build())
        self.assertIn('parameter FROM_PORT = 8', text)
        self.assertIn('logic [FROM_PORT-1:0] i_data', text)
        self.assertNotIn('localparam FROM_PORT', text)

    def test_vhdl_says_the_same (self):
        text = vhdl_of(self.build())
        self.assertIn('constant HALF : integer := WIDTH / 2;', text)
        self.assertIn('constant QUARTER : integer := HALF / 2;', text)

    def test_the_parameter_survives_being_named_only_by_a_constant (self):
        """A localparam expression can be the only place a parameter is
        named, and the parameter list is decided after the localparams
        precisely so that it is not pruned out from under one."""
        text = sv_of(self.build())
        self.assertIn('parameter WIDTH = 8', text)


class TestPortWidthGeneric(unittest.TestCase):
    """A constant that names a port's width becomes a generic.

    WIDTHD = len(i_data) is the author giving that port's width a name,
    and a name is the one thing the converter cannot invent for itself
   .
    """

    def build (self, width = 16):
        @block
        def shifter (i_clock, i_data, o_data):
            WIDTHD = len(i_data)
            HALF = WIDTHD // 2
            held = signal(WIDTHD)
            low = signal(HALF)

            @always_comb
            def shifter_comb ():
                low.next = held[HALF-1:0]
                o_data.next = held

            @always_ff (i_clock.posedge)
            def shifter_logic ():
                held.next = i_data

            return instances()

        return shifter(i_clock = signal(), i_data = signal(width),
                       o_data = signal(width))

    def test_the_width_is_a_generic (self):
        self.assertIn('parameter WIDTHD = 16', sv_of(self.build()))

    def test_both_ports_follow_it (self):
        text = sv_of(self.build())
        self.assertIn('input  logic [WIDTHD-1:0] i_data', text)
        self.assertIn('output logic [WIDTHD-1:0] o_data', text)

    def test_what_was_derived_from_it_follows_too (self):
        text = sv_of(self.build())
        self.assertIn('localparam HALF = WIDTHD/2;', text)
        self.assertIn('logic [WIDTHD-1:0] held', text)
        self.assertIn('logic [HALF-1:0] low', text)

    def test_another_width_is_the_same_text (self):
        wide = sv_of(self.build(16))
        thin = sv_of(self.build(8))
        for line in ('logic [WIDTHD-1:0] held', 'localparam HALF = WIDTHD/2;',
                     'input  logic [WIDTHD-1:0] i_data'):
            self.assertIn(line, wide)
            self.assertIn(line, thin)

    def test_vhdl_gets_a_generic (self):
        text = vhdl_of(self.build())
        self.assertIn('WIDTHD : integer := 16', text)
        self.assertIn('std_logic_vector(WIDTHD - 1 downto 0)', text)

    def test_a_bundle_member_counts (self):
        from types import SimpleNamespace

        @block
        def bridged (i_clock, bus, o_data):
            WIDTHD = len(bus.data)
            held = signal(WIDTHD)

            @always_comb
            def bridged_comb ():
                o_data.next = held

            @always_ff (i_clock.posedge)
            def bridged_logic ():
                held.next = bus.data

            return instances()

        bus = SimpleNamespace(data = signal(12))
        text = sv_of(bridged(i_clock = signal(), bus = bus,
                             o_data = signal(12)))
        self.assertIn('parameter WIDTHD = 12', text)
        self.assertIn('logic [WIDTHD-1:0] bus_data', text)

    def test_a_width_no_single_port_names_is_left_alone (self):
        """max(len(a), len(b)) names neither, and inventing a name for
        one would be cells_0 in a different hat."""
        @block
        def pairwise (i_a, i_b, o_data):
            WIDTH = max(len(i_a), len(i_b))
            held = signal(WIDTH)

            @always_comb
            def pairwise_comb ():
                held.next = i_a
                o_data.next = held

            return instances()

        text = sv_of(pairwise(i_a = signal(8), i_b = signal(8),
                              o_data = signal(8)))
        self.assertNotIn('parameter WIDTH', text)
        self.assertIn('input  logic [7:0] i_a', text)


class TestMaxMin(unittest.TestCase):
    """max() and min() of two widths, which is how a block with two
    operands sizes itself.

    They are the one place the two languages want different text for
    one expression: VHDL-2008 has maximum() in std.standard, and
    SystemVerilog has no such function for a constant expression, so
    it gets the conditional that means the same thing.
    """

    def build (self, wa = 32, wb = 32):
        @block
        def pairwise (i_a, i_b, o_data):
            WIDTHA = len(i_a)
            WIDTHB = len(i_b)
            WIDTH = max(WIDTHA, WIDTHB)
            NARROW = min(WIDTHA, WIDTHB)
            held = signal(WIDTH)
            thin = signal(NARROW)

            @always_comb
            def pairwise_comb ():
                held.next = i_a
                thin.next = i_b[NARROW-1:0]
                o_data.next = held

            return instances()

        return pairwise(i_a = signal(wa), i_b = signal(wb),
                        o_data = signal(max(wa, wb)))

    def test_systemverilog_uses_a_conditional (self):
        text = sv_of(self.build())
        self.assertIn(
            'localparam WIDTH = (WIDTHA > WIDTHB ? WIDTHA : WIDTHB);', text)
        self.assertIn(
            'localparam NARROW = (WIDTHA < WIDTHB ? WIDTHA : WIDTHB);', text)

    def test_vhdl_uses_maximum (self):
        text = vhdl_of(self.build())
        self.assertIn('constant WIDTH : integer := maximum(WIDTHA, WIDTHB);',
                      text)
        self.assertIn('constant NARROW : integer := minimum(WIDTHA, WIDTHB);',
                      text)

    def test_the_function_name_is_not_taken_for_a_width (self):
        """max is an ast.Name too, and it is not something the module
        declares. Reading it as one refused the whole expression."""
        self.assertIn('WIDTHA > WIDTHB', sv_of(self.build()))

    def test_two_widths_give_the_same_text (self):
        for line in ('localparam WIDTH = (WIDTHA > WIDTHB ? WIDTHA : WIDTHB);',
                     'logic [WIDTH-1:0] held'):
            self.assertIn(line, sv_of(self.build(32, 32)))
            self.assertIn(line, sv_of(self.build(16, 8)))


class TestCastFollowsTheGeneric(unittest.TestCase):
    """A slice of an expression is emitted as a width cast, and folding
    the width there is what stops a module following its own generic
   .

    34'(...) is right at one width and wrong at every other, so a
    design that overrides the generic is one Verilator refuses.
    """

    def build (self, width = 32):
        @block
        def widen (i_a, i_b, o_data):
            WIDTHA = len(i_a)
            WIDTHR = len(o_data)
            WIDE = WIDTHA + 2
            product = signal(WIDE)
            narrow = signal(WIDTHA - 1)
            full = signal(WIDTHA)
            low = signal(WIDTHA)

            @always_comb
            def widen_comb ():
                product.next = (i_a.signed() * i_b.signed())[WIDTHA+1:0]
                narrow.next = (i_a + i_b)[WIDTHA-2:0]
                full.next = (i_a + i_b)[WIDTHA-1:0]
                low.next = (i_a.signed() * i_b.signed())[WIDTHA-1:0]
                o_data.next = product

            return instances()

        return widen(i_a = signal(width), i_b = signal(width),
                     o_data = signal(width + 2))

    def test_the_cast_carries_the_expression (self):
        text = sv_of(self.build())
        self.assertIn("(WIDTHA+2)'(", text)
        self.assertNotIn("34'(", text)

    def test_a_minus_one_bound_is_just_the_name (self):
        """[NAME-1:0] is NAME bits, so the cast is the name itself with
        no arithmetic beside it.

        The product really is cut down to WIDTHA from twice that, so
        the cast is doing something and the bound has to follow the
        generic like any other."""
        text = sv_of(self.build())
        self.assertIn("low = WIDTHA'(", text)
        self.assertNotIn("low = 32'(", text)

    def test_a_sum_back_at_its_own_width_needs_no_cast (self):
        """WIDTHA bits plus WIDTHA bits, taken at WIDTHA bits, is the
        widening undone rather than a truncation, and there is nothing
        left for a cast to say."""
        text = sv_of(self.build())
        self.assertIn('full = (i_a + i_b);', text)
        self.assertNotIn("full = WIDTHA'(", text)

    def test_another_bound_carries_its_arithmetic (self):
        """[WIDTHA-2:0] is WIDTHA-1 bits, not WIDTHA."""
        self.assertIn("narrow = (WIDTHA-1)'(", sv_of(self.build()))

    def test_the_cast_size_is_parenthesised (self):
        """size'(expr) binds tighter than arithmetic, so WIDTHA+2'(x)
        would be WIDTHA + (2'(x)), a different and legal expression."""
        self.assertNotIn("WIDTHA+2'(", sv_of(self.build()))

    def test_two_widths_differ_only_in_the_defaults (self):
        wide = sv_of(self.build(32)).splitlines()
        thin = sv_of(self.build(16)).splitlines()
        changed = [a for a, b in zip(wide, thin) if a != b]
        self.assertEqual(len(wide), len(thin))
        for line in changed:
            self.assertIn('parameter', line)

    def test_vhdl_resize_carries_it_too (self):
        self.assertIn('WIDTHA + 2', vhdl_of(self.build()))


@unittest.skipUnless(shutil.which('verilator'), 'verilator not installed')
class TestOverrideLints(unittest.TestCase):
    """The emitted module, instantiated at a width it was not built at.

    This is what a parameterised module is for and it is the one test
    that says whether the generics are real.
    """

    def test_a_wider_build_instantiates_at_a_narrower_width (self):
        @block
        def scaler (i_a, i_b, o_data):
            WIDTHA = len(i_a)
            WIDTHR = len(o_data)
            wide = signal(WIDTHA + 2)

            @always_comb
            def scaler_comb ():
                wide.next = (i_a.signed() * i_b.signed())[WIDTHA+1:0]
                o_data.next = wide

            return instances()

        built = scaler(i_a = signal(32), i_b = signal(32),
                       o_data = signal(34))
        directory = tempfile.mkdtemp(prefix = 'iso_override_')
        module = os.path.join(directory, 'scaler.sv')
        with open(module, 'w', encoding = 'ascii') as handle:
            handle.write(emit_sv(analyse(built)[0]))
        wrapper = os.path.join(directory, 'over.sv')
        with open(wrapper, 'w', encoding = 'ascii') as handle:
            handle.write(
                'module over (\n'
                '    input  logic [15:0] i_a,\n'
                '    input  logic [15:0] i_b,\n'
                '    output logic [17:0] o_data\n'
                ');\n'
                '    scaler #(.WIDTHA(16), .WIDTHR(18)) inst (\n'
                '        .i_a(i_a), .i_b(i_b), .o_data(o_data));\n'
                'endmodule\n')
        # raises ConversionError if verilator rejects it
        lint_sv([wrapper, module], top = 'over')


class TestMeasuredButUnnamed(unittest.TestCase):
    """A block that measures its ports but names no width says so.

    Writing len(i_a) is the author saying the block sizes itself from
    its ports. If no one port's width is named, nothing can become a
    generic and the module comes out frozen at the width it was built
    with - which is the same design it was before, but the author had
    every reason to think otherwise. The silent case is the dangerous
    one, so it is a warning.
    """

    def warnings_of (self, top):
        return [w for w in analyse(top)[1] if 'measured from' in w]

    def build (self, named):
        if named:
            @block
            def pairwise (i_a, i_b, o_data):
                WIDTHA = len(i_a)
                WIDTHB = len(i_b)
                WIDTHO = len(o_data)
                WIDTH = max(WIDTHA, WIDTHB)
                held = signal(WIDTH)

                @always_comb
                def pairwise_comb ():
                    held.next = i_a | i_b
                    o_data.next = held

                return instances()
        else:
            @block
            def pairwise (i_a, i_b, o_data):
                WIDTH = max(len(i_a), len(i_b))
                held = signal(WIDTH)

                @always_comb
                def pairwise_comb ():
                    held.next = i_a | i_b
                    o_data.next = held

                return instances()

        return pairwise(i_a = signal(8), i_b = signal(8),
                        o_data = signal(8))

    def test_a_width_that_names_no_port_warns (self):
        found = self.warnings_of(self.build(named = False))
        self.assertEqual(len(found), 1)
        self.assertIn('WIDTH', found[0])
        self.assertIn('i_a, i_b', found[0])

    def test_naming_them_silences_it (self):
        self.assertEqual(self.warnings_of(self.build(named = True)), [])

    def test_the_warning_invents_no_name (self):
        """It says to write a constant that is exactly len(i_a); it
        does not suggest what to call it, because that is the one
        thing the converter may not decide."""
        found = self.warnings_of(self.build(named = False))[0]
        self.assertIn('len(i_a)', found)
        self.assertNotIn('I_A', found)

    def test_a_block_that_measures_nothing_is_quiet (self):
        @block
        def plain (i_a, o_data):
            @always_comb
            def plain_comb ():
                o_data.next = i_a

            return instances()

        self.assertEqual(
            self.warnings_of(plain(i_a = signal(8), o_data = signal(8))), [])


class TestTwoBuildsMerge(unittest.TestCase):
    """A block that names its widths is one module at every width.

    The answer to the complaint this line of work started from: a module named after the value of
    a parameter. merge_builds compares the emitted text now rather
    than the IR, because a width that follows a generic is the same
    text at every width while the IR still holds the number it
    elaborated with.
    """

    def build (self):
        @block
        def sized (i_clock, i_data, o_data):
            WIDTHD = len(i_data)
            HALF = WIDTHD // 2
            held = signal(WIDTHD)
            low = signal(HALF)

            @always_comb
            def sized_comb ():
                low.next = held[HALF-1:0]
                o_data.next = held

            @always_ff (i_clock.posedge)
            def sized_logic ():
                held.next = i_data

            return instances()

        @block
        def rig (i_clock, i_big, i_thin, o_big, o_thin):
            wide = sized(i_clock = i_clock, i_data = i_big,
                         o_data = o_big)
            thin = sized(i_clock = i_clock, i_data = i_thin,
                         o_data = o_thin)
            return instances()

        return rig(i_clock = signal(), i_big = signal(16),
                   i_thin = signal(8), o_big = signal(16),
                   o_thin = signal(8))

    def test_systemverilog_emits_one_module (self):
        text = sv_of(self.build())
        self.assertEqual(text.count('module sized'), 1)
        self.assertNotIn('sized_WIDTHD', text)
        self.assertNotIn('sized_HALF', text)

    def test_the_difference_is_an_override (self):
        text = sv_of(self.build())
        self.assertIn('sized wide (', text)
        self.assertIn('sized #(.WIDTHD(8)) thin (', text)

    def test_vhdl_emits_one_entity_and_a_generic_map (self):
        text = vhdl_of(self.build())
        self.assertEqual(text.count('entity sized is'), 1)
        self.assertIn('WIDTHD => 8', text)

    def test_a_block_that_names_nothing_still_splits (self):
        """Two widths with no name for either are two modules, which
        is what d821c15 fixed and this must not undo."""
        @block
        def bare (i_data, o_data):
            @always_comb
            def bare_comb ():
                o_data.next = i_data

            return instances()

        @block
        def two (i_big, i_thin, o_big, o_thin):
            a = bare(i_data = i_big, o_data = o_big)
            b = bare(i_data = i_thin, o_data = o_thin)
            return instances()

        text = sv_of(two(i_big = signal(8), i_thin = signal(4),
                         o_big = signal(8), o_thin = signal(4)))
        self.assertEqual(text.count('module bare'), 2)
