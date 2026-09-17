"""VHDL-2008 emitter tests.

Ports and signals are std_logic / std_logic_vector. numeric_std is
only used inside arithmetic expressions. GHDL --std=08 analyses the
output. Hierarchy stays unflattened.
"""
import importlib.util
import itertools
import linecache
import re
import os
import shutil
import tempfile
import textwrap
import unittest

from isomorph import (block, signal, signals, enum, struct, always_ff,
    always_comb, assign, concat, replicate, bits, instances, analyse,
    emit_vhdl, emit_sv, convert, ConversionError, write_vhdl)
from isomorph.emit_vhdl import lint_vhdl

HERE = os.path.dirname(os.path.abspath(__file__))
CONSTRUCTS = os.path.join(HERE, 'constructs')
_counter = itertools.count()
HAVE_GHDL = shutil.which('ghdl') is not None


def load (src):
    name = f'<vhdl{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    namespace = {}
    exec(compile(src, name, 'exec'), namespace)
    return namespace


def load_file (path):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def emit_top (top):
    modules, warnings = analyse(top)
    return emit_vhdl(modules), modules, warnings


def ghdl_analyse (text, top):
    directory = tempfile.mkdtemp(prefix = 'iso_ghdl_')
    path = os.path.join(directory, top + '.vhd')
    with open(path, 'w', encoding = 'ascii') as f:
        f.write(text)
    lint_vhdl(path)
    return path


class PortTypeTests(unittest.TestCase):
    def test_ports_are_std_logic_not_unsigned (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def add2 (i_a, i_b, o_sum, WIDTH = 4):
                @always_comb
                def sum_comb ():
                    o_sum.next = (i_a + i_b)[WIDTH-1:0]
                return instances()
        ''')
        ns = load(src)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        text, _, _ = emit_top(top)
        entity = text.split('architecture', 1)[0]
        self.assertIn('std_logic_vector(WIDTH - 1 downto 0)', entity)
        self.assertNotIn('unsigned', entity)
        self.assertNotIn('signed', entity)
        self.assertNotIn('std_logic_arith', text)
        self.assertIn('ieee.numeric_std.all', text)
        # the arithmetic is numeric_std, and no wider than it has to
        # be: both operands are WIDTH bits and so is the sum
        self.assertIn('std_logic_vector(unsigned(i_a) + unsigned(i_b))', text)
        self.assertNotIn('resize', text)
        if HAVE_GHDL:
            ghdl_analyse(text, 'add2')


class HierarchyTests(unittest.TestCase):
    def setUp (self):
        mod = load_file(os.path.join(CONSTRUCTS, 'hierarchy.py'))
        self.text, self.modules, _ = emit_top(mod.elaborate_parent())

    def test_two_entities_named_instances (self):
        # one build of each block, so neither name carries its
        # parameters, and the two instances of add2 differ by instance
        # name alone - which is what an instance name is for
        names = [m.name for m in self.modules]
        self.assertEqual(names, ['add2', 'parent'])
        self.assertIn('entity add2 is', self.text)
        self.assertIn('entity parent is', self.text)
        self.assertIn('inst_a : entity work.add2', self.text)
        self.assertIn('inst_b : entity work.add2', self.text)
        self.assertNotIn('__', self.text)
        parent = self.text[self.text.index('entity parent is'):]
        self.assertNotIn('sum_comb', parent)

    def test_ghdl (self):
        if not HAVE_GHDL:
            self.skipTest('ghdl not on PATH')
        ghdl_analyse(self.text, 'parent')


class ConstructTests(unittest.TestCase):
    def test_enum_and_clocked_process (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def fsm1 (i_clock, i_reset, o_run):
                state = enum('IDLE', 'RUN')
                fsm = signal(state)
                @always_comb
                def out_comb ():
                    o_run.next = (fsm == state.RUN)
                @always_ff (i_clock.posedge)
                def fsm_logic ():
                    if (i_reset):
                        fsm.next = state.IDLE
                    else:
                        fsm.next = state.RUN
                return instances()
        ''')
        ns = load(src)
        top = ns['fsm1'](i_clock = signal(), i_reset = signal(),
                         o_run = signal())
        text, _, _ = emit_top(top)
        self.assertIn('type state is (IDLE, RUN);', text)
        self.assertIn('signal fsm : state;', text)
        self.assertIn('fsm_logic : process (i_clock) is', text)
        self.assertIn('rising_edge(i_clock)', text)
        self.assertIn('fsm <= IDLE;', text)
        self.assertIn("if i_reset = '1' then", text)
        if HAVE_GHDL:
            ghdl_analyse(text, 'fsm1')

    def test_match_bits_is_case_question (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def dec (instr, o_add, o_other):
                @always_comb
                def dec_comb ():
                    o_add.next = False
                    o_other.next = False
                    match instr:
                        case bits('1???'):
                            o_add.next = True
                        case bits('01??'):
                            o_other.next = True
                        case _:
                            pass
                return instances()
        ''')
        ns = load(src)
        top = ns['dec'](instr = signal(4), o_add = signal(),
                        o_other = signal())
        text, _, _ = emit_top(top)
        self.assertIn('case?', text)
        self.assertIn('"1---"', text)
        self.assertIn('when others =>', text)
        if HAVE_GHDL:
            ghdl_analyse(text, 'dec')

    def test_concat_replicate (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def sext (i_n, o_w):
                @always_comb
                def sext_comb ():
                    o_w.next = concat(replicate(i_n[3], 4), i_n)
                return instances()
        ''')
        ns = load(src)
        top = ns['sext'](i_n = signal(4), o_w = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('i_n(3) & i_n(3)', text)
        self.assertIn(' & ', text)
        if HAVE_GHDL:
            ghdl_analyse(text, 'sext')

    def test_part_down (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def nib (i_word, i_top, o_n):
                @always_comb
                def nib_comb ():
                    o_n.next = i_word.part_down(i_top, 4)
                return instances()
        ''')
        ns = load(src)
        top = ns['nib'](i_word = signal(16), i_top = signal(4),
                        o_n = signal(4))
        text, _, _ = emit_top(top)
        self.assertIn('downto', text)
        self.assertIn(' - 3', text)
        if HAVE_GHDL:
            ghdl_analyse(text, 'nib')

    def test_struct_record (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def pack (i_addr, i_data, o_addr, o_data):
                cmd_t = struct('cmd_t', address = 8, write = 1, data = 8)
                cmd = signal(cmd_t)
                @always_comb
                def pack_comb ():
                    cmd.address.next = i_addr
                    cmd.write.next = True
                    cmd.data.next = i_data
                    o_addr.next = cmd.address
                    o_data.next = cmd.data
                return instances()
        ''')
        ns = load(src)
        top = ns['pack'](i_addr = signal(8), i_data = signal(8),
                         o_addr = signal(8), o_data = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('type cmd_t is record', text)
        self.assertIn('signal cmd : cmd_t;', text)
        self.assertIn('cmd_v.address', text)
        if HAVE_GHDL:
            ghdl_analyse(text, 'pack')

    def test_fsm_encoding_attributes (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def fsm1 (i_clock, i_reset, o_run):
                state = enum('IDLE', 'RUN', encoding = 'one_hot')
                fsm = signal(state)
                @always_comb
                def out_comb ():
                    o_run.next = (fsm == state.RUN)
                @always_ff (i_clock.posedge)
                def fsm_logic ():
                    if (i_reset):
                        fsm.next = state.IDLE
                    else:
                        fsm.next = state.RUN
                return instances()
        ''')
        ns = load(src)
        top = ns['fsm1'](i_clock = signal(), i_reset = signal(),
                         o_run = signal())
        text, _, _ = emit_top(top)
        self.assertIn('attribute fsm_encoding of fsm', text)
        # onehot: one_hot is rejected by Quartus and by Efinity, and
        # the VHDL carries the same spelling as the SystemVerilog
        self.assertIn('"onehot"', text)
        self.assertNotIn('one_hot', text)
        if HAVE_GHDL:
            ghdl_analyse(text, 'fsm1')

    def test_mini_core_ghdl (self):
        if not HAVE_GHDL:
            self.skipTest('ghdl not on PATH')
        mod = load_file(os.path.join(CONSTRUCTS, 'mini_core.py'))
        top = mod.mini_core (
            i_clock = signal(), i_reset = signal(),
            i_word = signal(16), i_a = signal(8), i_b = signal(8),
            o_q = signal(8), o_done = signal()
        )
        text, _, _ = emit_top(top)
        self.assertNotIn('unsigned', text.split('architecture', 1)[0])
        ghdl_analyse(text, 'mini_core')


class ConvertTests(unittest.TestCase):
    def test_convert_writes_vhdl (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def add2 (i_a, i_b, o_sum, WIDTH = 4):
                @always_comb
                def sum_comb ():
                    o_sum.next = (i_a + i_b)[WIDTH-1:0]
                return instances()
        ''')
        ns = load(src)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        path = os.path.join(tempfile.mkdtemp(prefix = 'iso_vhd_'), 'add2.vhd')
        convert(top, sv = False, vhdl = path)
        with open(path) as f:
            text = f.read()
        self.assertIn('entity add2 is', text)
        if HAVE_GHDL:
            lint_vhdl(path)


class ParameterCommentTests (unittest.TestCase):
    def test_a_generic_carries_its_comment (self):
        src = textwrap.dedent("""\
            from isomorph import *

            @block
            def marked (i_clock, o_q,
                    # what kind of block this is
                    ID_CLASS = 1129206866,
                    WIDTH = 8):             # how wide the count is
                @always_ff (i_clock.posedge)
                def q_logic ():
                    o_q.next = (o_q + 1)[WIDTH-1:0]
                    if (o_q == 0):
                        o_q.next = ID_CLASS[WIDTH-1:0]
                return instances()
        """)
        top = load(src)['marked'](i_clock = signal(), o_q = signal(8))
        modules, _ = analyse(top)
        text = emit_vhdl(modules)
        self.assertIn('    -- what kind of block this is', text)
        self.assertIn('ID_CLASS : integer := 1129206866;', text)
        self.assertIn('-- how wide the count is', text)


class BaseTests (unittest.TestCase):
    """A constant written 0xdead is 16#dead# in VHDL, not 57005."""

    SRC = textwrap.dedent("""\
        from isomorph import *

        @block
        def marks (i_clock, o_q):
            MARK = 0xdead
            NIBBLE = 0b1011
            PLAIN = 57005
            @always_ff (i_clock.posedge)
            def marks_logic ():
                o_q.next = MARK
                if (o_q[3:0] == NIBBLE):
                    o_q.next = PLAIN
            return instances()
    """)

    def test_the_base_reaches_the_vhdl (self):
        top = load(self.SRC)['marks'](i_clock = signal(), o_q = signal(16))
        modules, _ = analyse(top)
        text = emit_vhdl(modules)
        self.assertIn('constant MARK : integer := 16#dead#;', text)
        self.assertIn('constant NIBBLE : integer := 2#1011#;', text)
        self.assertIn('constant PLAIN : integer := 57005;', text)


if __name__ == '__main__':
    unittest.main()


class OneBitSliceTests (unittest.TestCase):
    """A one-bit value is std_logic here, and VHDL will not match
    x(n downto n), a one-element vector, against it."""

    SRC = textwrap.dedent('''\
        from isomorph import *
        @block
        def shifter (i_clock, i_data, o_data):
            chain = signal(4)

            @always_ff (i_clock.posedge)
            def shift_logic ():
                chain.next = concat(chain[2:0], i_data)

            @always_comb
            def out_comb ():
                o_data.next = chain[3:3]
            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['shifter'](i_clock = signal(), i_data = signal(),
                             o_data = signal())

    def test_width_one_slice_is_an_index (self):
        modules, _ = analyse(self.build())
        text = emit_vhdl(modules)
        self.assertIn('chain(3)', text)
        self.assertNotIn('3 downto 3', text)

    def test_wider_slices_stay_slices (self):
        modules, _ = analyse(self.build())
        self.assertIn('chain(2 downto 0)', emit_vhdl(modules))

    @unittest.skipUnless(shutil.which('ghdl'), 'ghdl not installed')
    def test_it_analyses (self):
        modules, _ = analyse(self.build())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'shifter.vhd')
            write_vhdl(modules, path)
            lint_vhdl(path)


class WideValueTests (unittest.TestCase):
    """VHDL's integer is 32 bits and signed, so anything from
    0x80000000 up cannot be written as a number: to_unsigned(2147483648,
    32) is rejected outright by a vendor tool. Such a value has to
    reach the HDL as bits."""

    SRC = textwrap.dedent('''\
        from isomorph import *

        WINDOW = 0xC0000000


        @block
        def word_port (i_address, o_hit, o_word, WORD = 0xDEADBEEF):

            @always_comb
            def decode ():
                o_hit.next = (i_address >= WINDOW)
                o_word.next = WORD

            return instances()


        @block
        def pair (i_address, o_hit0, o_word0, o_hit1, o_word1):

            port0 = word_port(i_address = i_address, o_hit = o_hit0,
                              o_word = o_word0, WORD = 0xDEADBEEF)
            port1 = word_port(i_address = i_address, o_hit = o_hit1,
                              o_word = o_word1, WORD = 0x00C0FFEE)

            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['pair'](i_address = signal(32), o_hit0 = signal(),
                          o_word0 = signal(32), o_hit1 = signal(),
                          o_word1 = signal(32))

    def text (self):
        modules, _ = analyse(self.build())
        return emit_vhdl(modules)

    def test_no_literal_past_the_end_of_integer (self):
        text = self.text()
        stripped = re.sub(r'"[^"]*"', '""', text)
        for number in re.findall(r'\b\d+\b', stripped):
            self.assertLessEqual(int(number), 2 ** 31 - 1,
                                 f'{number} will not fit a VHDL integer')

    def test_a_wide_constant_is_compared_as_bits (self):
        text = self.text()
        self.assertIn('unsigned\'(x"C0000000")', text)
        self.assertNotIn('to_unsigned(3221225472', text)

    def test_a_wide_generic_is_a_vector (self):
        text = self.text()
        self.assertIn('WORD : std_logic_vector(31 downto 0)', text)
        self.assertIn('x"DEADBEEF"', text)
        self.assertIn('x"00C0FFEE"', text)
        # declared a vector, so it is read as itself and not converted
        self.assertNotIn('to_unsigned(WORD', text)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_sees_nothing_out_of_bounds (self):
        text = self.text()
        directory = tempfile.mkdtemp(prefix = 'iso_ghdl_')
        try:
            path = os.path.join(directory, 'pair.vhd')
            with open(path, 'w', encoding = 'ascii') as f:
                f.write(text)
            warnings = lint_vhdl(path)
        finally:
            shutil.rmtree(directory, ignore_errors = True)
        self.assertNotIn('violates bounds', warnings)


class GenericMapTests(unittest.TestCase):
    """An instantiation names the generics the entity declares.

    ir.Instance.params is the overrides, which is what SystemVerilog
    emits. VHDL restated the child's whole parameter set, so a string
    parameter - which entity_lines does not declare, because nothing in
    a body can read one - went into the generic map against an entity
    that had no such generic. A RAM wrapper emitted IMAGE => "" and
    ghdl said 'no declaration for "image"'. Nineteen of the fpga
    tree's fifty-five designs would not analyse.
    """

    def build (self):
        @block
        def store (i_clock, i_data, o_data, DEPTH = 4, IMAGE = ''):
            # the memory image is read at elaboration and leaves no
            # trace in the body, as a preload filename does
            kept = signal(8)

            @always_comb
            def store_comb ():
                o_data.next = kept

            @always_ff (i_clock.posedge)
            def store_logic ():
                kept.next = i_data

            return instances()

        @block
        def holder (i_clock, i_data, o_data):
            inst_store = store(i_clock = i_clock, i_data = i_data,
                               o_data = o_data, IMAGE = 'boot.hex')
            return instances()

        return holder(i_clock = signal(), i_data = signal(8),
                      o_data = signal(8))

    def test_a_string_parameter_is_not_in_the_generic_map (self):
        text, _, _ = emit_top(self.build())
        self.assertNotIn('IMAGE =>', text)
        self.assertNotIn('IMAGE :', text)

    def test_the_entity_declares_what_the_map_names (self):
        text, _, _ = emit_top(self.build())
        declared = set(re.findall(r'^\s+(\w+) : integer', text,
                                  re.MULTILINE))
        mapped = set(re.findall(r'^\s+(\w+) => ', text, re.MULTILINE))
        ports = set(re.findall(r'^\s+(\w+)\s+: (?:in|out) ', text,
                               re.MULTILINE))
        self.assertEqual(mapped - ports - declared, set())

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        text, _, _ = emit_top(self.build())
        ghdl_analyse(text, 'holder')



class FoldedGenericTests(unittest.TestCase):
    """A parameter elaboration folded away is a generic in neither
    language.

    holder's BYTES only configures its child, and the child bakes the
    value into its own body, so nothing in holder's text names it.
    SystemVerilog has always pruned a parameter like that. VHDL
    declared it anyway, which left a generic that looks overridable
    and changes nothing, and gave one design two different interfaces
    for a mixed-language flow to bind by name.
    """

    def build (self):
        @block
        def leaf (i_clock, i_address, i_data, o_data, BYTES = 1024):
            mem = signals(BYTES // 4, 32)

            @always_ff (i_clock.posedge)
            def leaf_logic ():
                o_data.next = mem[i_address]
                mem[i_address].next = i_data

            return instances()

        @block
        def holder (i_clock, i_address, i_data, o_data, BYTES = 1024):
            inst = leaf(i_clock = i_clock, i_address = i_address,
                        i_data = i_data, o_data = o_data, BYTES = BYTES)
            return instances()

        return holder(i_clock = signal(), i_address = signal(8),
                      i_data = signal(32), o_data = signal(32))

    def generics (self, text, entity):
        body = text.split(f'entity {entity} is')[1].split('end entity')[0]
        return set(re.findall(r'^\s+(\w+) : integer', body, re.MULTILINE))

    def test_the_child_keeps_the_generic_its_body_names (self):
        text, _, _ = emit_top(self.build())
        self.assertEqual(self.generics(text, 'leaf'), {'BYTES'})

    def test_the_parent_declares_none (self):
        text, _, _ = emit_top(self.build())
        self.assertEqual(self.generics(text, 'holder'), set())

    def test_both_languages_declare_the_same_generics (self):
        vhdl, _, _ = emit_top(self.build())
        sv = emit_sv(analyse(self.build())[0])
        in_vhdl = set(re.findall(r'^\s+(\w+) : integer :=', vhdl,
                                 re.MULTILINE))
        in_sv = set(re.findall(r'^\s+parameter (\w+) =', sv, re.MULTILINE))
        self.assertEqual(in_vhdl, in_sv)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        text, _, _ = emit_top(self.build())
        ghdl_analyse(text, 'holder')


class NumericLiteralTests(unittest.TestCase):
    """A constant in an expression reads as the number it is.

    Verilog has a sized decimal literal and VHDL does not, so a
    constant used to land as a bit string. "1010101110100" is -2700,
    which nobody reads and no reviewer can check. numeric_std is
    already in scope wherever isomorph emits arithmetic.
    """

    def build (self):
        @block
        def pick (i_sel, o_data, o_zero):
            @always_comb
            def pick_comb ():
                o_data.next = 3600 if i_sel else -2700
                o_zero.next = 0

            return instances()

        return pick(i_sel = signal(), o_data = signal(13),
                    o_zero = signal(13))

    def test_a_positive_constant_is_to_unsigned (self):
        text, _, _ = emit_top(self.build())
        self.assertIn('std_logic_vector(to_unsigned(3600, 13))', text)

    def test_a_negative_constant_is_to_signed (self):
        text, _, _ = emit_top(self.build())
        self.assertIn('std_logic_vector(to_signed(-2700, 13))', text)

    def test_no_bit_string_is_left_to_read (self):
        text, _, _ = emit_top(self.build())
        self.assertNotIn('"1010101110100"', text)
        self.assertNotIn('"0111000010000"', text)

    def test_a_word_of_one_bit_is_an_aggregate (self):
        """(others => '0') is what every VHDL style guide asks for: it
        says which bit rather than the bit thirteen times, and it
        neither has to be counted nor changed when the width does.

        Only where the target says how wide it is. A qualified
        std_logic_vector'(others => '0') is rejected -- the type mark
        being unconstrained leaves the aggregate no index range -- so
        this is the right-hand side of an assignment and nowhere else.
        """
        text, _, _ = emit_top(self.build())
        # a signal, not a variable: nothing in this process reads
        # back what it drives, so there is nothing to stand in for
        self.assertIn("o_zero <= (others => '0');", text)
        self.assertNotIn('"0000000000000"', text)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        text, _, _ = emit_top(self.build())
        ghdl_analyse(text, 'pick')


class SingleBitLengthTests(unittest.TestCase):
    """A one-bit signal is a std_logic and has no 'length.

    The width of a widened operand is written as x'length so an entity
    follows its own generic rather than the number this build
    elaborated with. VHDL has no such attribute on a scalar, and ghdl
    says so: object prefix must be an array. A one-bit counter is
    enough to hit it, and a two-beat state machine is where that comes
    from.
    """

    def build (self):
        @block
        def beat (i_clock, o_beat):
            count = signal(1)

            @always_ff (i_clock.posedge)
            def beat_logic ():
                count.next = (count + 1)[0:0]

            assign(o_beat, lambda: count)
            return instances()

        return beat(i_clock = signal(), o_beat = signal())

    def test_no_length_on_a_scalar (self):
        text, _, _ = emit_top(self.build())
        self.assertNotIn("count'length", text)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        text, _, _ = emit_top(self.build())
        ghdl_analyse(text, 'beat')


class NumericRoundTripTests(unittest.TestCase):
    """A value that is already unsigned is not converted to it again.

    Every conversion here hands back a std_logic_vector, so an operand
    that arrives converted was wrapped straight back up:
    unsigned(std_logic_vector(resize(unsigned(count), 5) + ...)) is
    what every (count + 1)[n-1:0] emitted, and the middle two cancel.
    The type is read off the text, which is what a reviewer has to do
    with it as well.
    """

    def build (self):
        @block
        def tally (i_clock, i_step, o_count, o_high):
            count = signal(4)

            @always_ff (i_clock.posedge)
            def tally_logic ():
                count.next = (count + 1)[3:0]

            assign(o_count, lambda: count)
            assign(o_high, lambda: count >= 12)
            return instances()

        return tally(i_clock = signal(), i_step = signal(),
                     o_count = signal(4), o_high = signal())

    def test_the_round_trip_is_gone (self):
        text, _, _ = emit_top(self.build())
        self.assertNotIn('unsigned(std_logic_vector(', text)

    def test_the_sum_is_still_there (self):
        text, _, _ = emit_top(self.build())
        self.assertIn('std_logic_vector(unsigned(count) + 1)', text)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        text, _, _ = emit_top(self.build())
        ghdl_analyse(text, 'tally')

    def test_a_type_read_off_the_text (self):
        from isomorph.emit_vhdl import numeric_type, numeric, matched
        self.assertEqual(numeric_type('unsigned(x)'), 'unsigned')
        self.assertEqual(numeric_type('to_unsigned(3, 8)'), 'unsigned')
        self.assertEqual(numeric_type('-signed(i_theta)'), 'signed')
        self.assertEqual(numeric_type('resize(unsigned(a), 5)'), 'unsigned')
        self.assertEqual(numeric_type('shift_right(signed(a), 2)'), 'signed')
        self.assertEqual(
            numeric_type('resize(unsigned(a), 5) + resize(unsigned(b), 5)'),
            'unsigned')
        self.assertIsNone(numeric_type('a'))
        self.assertIsNone(numeric_type("count'length + 1"))

    def test_a_concatenation_is_a_vector_whatever_built_it (self):
        """& gives a std_logic_vector however numeric the pieces were,
        so nothing about one may be read as a numeric type."""
        from isomorph.emit_vhdl import numeric_type, numeric
        self.assertIsNone(numeric_type('unsigned(a) & unsigned(b)'))
        self.assertEqual(
            numeric('unsigned', 'std_logic_vector(unsigned(a) & unsigned(b))'),
            'unsigned(std_logic_vector(unsigned(a) & unsigned(b)))')

    def test_two_calls_side_by_side_are_not_one (self):
        """The parentheses are counted rather than trusted: this text
        starts with the prefix and ends with a bracket without being a
        conversion of anything."""
        from isomorph.emit_vhdl import matched
        self.assertIsNone(
            matched('std_logic_vector(a) & std_logic_vector(b)',
                    'std_logic_vector('))
        self.assertEqual(matched('std_logic_vector(a)',
                                 'std_logic_vector('), 'a')

    def test_a_signed_value_is_not_made_unsigned (self):
        from isomorph.emit_vhdl import numeric
        self.assertEqual(numeric('unsigned', 'std_logic_vector(signed(a))'),
                         'unsigned(std_logic_vector(signed(a)))')


class EqualityCompareTests(unittest.TestCase):
    """Two values converted to vectors only to be compared as vectors.

    The conversions cancel in a pair the way numeric() cancels one, and
    numeric_std compares two unsigned values of unequal length where
    two vectors of unequal length is an error, so the shorter line is
    the safer one as well. Only when both sides are conversions of the
    same numeric type: an enumeration, a bit string and a signed value
    beside an unsigned one are all left where they are.
    """

    def build (self):
        @block
        def match (i_clock, o_hit, LIMIT = 9):
            count = signal(4)

            @always_ff (i_clock.posedge)
            def match_logic ():
                if (count == LIMIT - 1):
                    count.next = 0
                else:
                    count.next = (count + 1)[3:0]

            assign(o_hit, lambda: count == LIMIT - 1)
            return instances()

        return match(i_clock = signal(), o_hit = signal())

    def test_neither_side_is_converted (self):
        text, _, _ = emit_top(self.build())
        self.assertIn('unsigned(count)', text)
        self.assertNotIn('std_logic_vector(resize(unsigned(count), '
                         "count'length + 1)) =", text)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_analyses_it (self):
        text, _, _ = emit_top(self.build())
        ghdl_analyse(text, 'match')

    def test_an_enumeration_is_compared_as_it_is (self):
        @block
        def walk (i_clock, o_done):
            steps = enum('IDLE', 'RUN', 'DONE')
            fsm = signal(steps)

            @always_ff (i_clock.posedge)
            def walk_logic ():
                if (fsm == steps.IDLE):
                    fsm.next = steps.RUN
                else:
                    fsm.next = steps.DONE

            assign(o_done, lambda: fsm == steps.DONE)
            return instances()

        text, _, _ = emit_top(walk(i_clock = signal(), o_done = signal()))
        self.assertIn('fsm = IDLE', text)
        self.assertNotIn('unsigned(fsm)', text)
