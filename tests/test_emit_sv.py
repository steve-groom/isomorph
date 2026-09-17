"""SystemVerilog emitter tests.

Verilator (lint and C++ sim) is the reference. Yosys checks that a
parent/child design stays a hierarchy: two modules, named instances,
nothing flattened. The Python executor is not used here.
"""
import importlib.util
import itertools
import linecache
import os
import subprocess
import tempfile
import textwrap
import unittest

from isomorph import (block, signal, signals, enum, struct, always_ff,
    always_comb, assign, concat, replicate, bits, instances, analyse,
    emit_sv, convert, ConversionError, lint_sv, write_sv)
from isomorph import ir

HERE = os.path.dirname(os.path.abspath(__file__))
CONSTRUCTS = os.path.join(HERE, 'constructs')

_counter = itertools.count()


def load (src):
    name = f'<emit{next(_counter)}>'
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
    return emit_sv(modules), modules, warnings


def verilator_lint (text, top):
    directory = tempfile.mkdtemp(prefix = 'iso_lint_')
    path = os.path.join(directory, top + '.sv')
    with open(path, 'w', encoding = 'ascii') as f:
        f.write(text)
    lint_sv(path, top = top)
    return path


def yosys_script (path, script):
    result = subprocess.run(
        ['yosys', '-p', script],
        capture_output = True, text = True)
    out = result.stdout + result.stderr
    if result.returncode != 0:
        raise AssertionError('yosys failed:\n' + out)
    return out


def verilator_sim (text, top, cpp):
    """Compile the SV with Verilator to C++ and run the harness."""
    directory = tempfile.mkdtemp(prefix = 'iso_sim_')
    sv_path = os.path.join(directory, top + '.sv')
    cpp_path = os.path.join(directory, 'tb.cpp')
    mdir = os.path.join(directory, 'obj_dir')
    with open(sv_path, 'w', encoding = 'ascii') as f:
        f.write(text)
    with open(cpp_path, 'w', encoding = 'ascii') as f:
        f.write(cpp)
    cmd = [
        'verilator', '--cc', '--exe', '--build', '--sv',
        '-Wall', '-Wno-DECLFILENAME',
        '--top-module', top, '-o', 'sim', '--Mdir', mdir,
        sv_path, cpp_path,
    ]
    build = subprocess.run(cmd, capture_output = True, text = True)
    if build.returncode != 0:
        raise AssertionError(
            'verilator build failed:\n' + (build.stderr or build.stdout))
    sim = os.path.join(mdir, 'sim')
    run = subprocess.run([sim], capture_output = True, text = True)
    if run.returncode != 0:
        raise AssertionError(
            'sim failed:\n' + (run.stdout or '') + (run.stderr or ''))
    return run.stdout


# C++ harnesses ---------------------------------------------------------------

ADD2_TB = r'''
#include "Vadd2.h"
#include "verilated.h"
#include <cstdio>
int main (int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    Vadd2 *top = new Vadd2;
    top->i_a = 2; top->i_b = 3; top->eval();
    if (top->o_sum != 5) { std::printf("FAIL sum %u\n", top->o_sum); return 1; }
    top->i_a = 15; top->i_b = 1; top->eval();
    if (top->o_sum != 0) { std::printf("FAIL wrap %u\n", top->o_sum); return 1; }
    std::printf("PASS\n");
    delete top;
    return 0;
}
'''

def parent_tb (name):
    return f'''
#include "V{name}.h"
#include "verilated.h"
#include <cstdio>
int main (int argc, char **argv) {{
    Verilated::commandArgs(argc, argv);
    V{name} *dut = new V{name};
    // inst_a: 1+2 = 3, inst_b: 3+4 = 7, ack = 7[0] = 1
    dut->i_x = 1; dut->i_y = 2; dut->bus_data = 4; dut->eval();
    if (dut->o_z != 7) {{ std::printf("FAIL o_z %u\\n", dut->o_z); return 1; }}
    if (dut->bus_ack != 1) {{ std::printf("FAIL ack\\n"); return 1; }}
    dut->i_x = 63; dut->i_y = 1; dut->bus_data = 0; dut->eval();
    if (dut->o_z != 0) {{ std::printf("FAIL wrap o_z %u\\n", dut->o_z); return 1; }}
    std::printf("PASS\\n");
    delete dut;
    return 0;
}}
'''

COUNT_TB = r'''
#include "Vcount4.h"
#include "verilated.h"
#include <cstdio>
static void tick (Vcount4 *top) {
    top->i_clock = 0; top->eval();
    top->i_clock = 1; top->eval();
    top->i_clock = 0; top->eval();
}
int main (int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    Vcount4 *top = new Vcount4;
    top->i_reset = 1;
    tick(top);
    tick(top);
    if (top->o_q != 0) { std::printf("FAIL reset %u\n", top->o_q); return 1; }
    top->i_reset = 0;
    for (int i = 1; i <= 5; i++) {
        tick(top);
        if (top->o_q != i) {
            std::printf("FAIL tick %d got %u\n", i, top->o_q);
            return 1;
        }
    }
    std::printf("PASS\n");
    delete top;
    return 0;
}
'''


class GoldenTests(unittest.TestCase):
    def test_add2_exact (self):
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
        expected = textwrap.dedent('''\
            //''' + '-' * 77 + '''
            module add2 #(
                parameter WIDTH = 4
            ) (
                input  logic [WIDTH-1:0] i_a,
                input  logic [WIDTH-1:0] i_b,
                output logic [WIDTH-1:0] o_sum
            );

                always_comb begin : sum_comb
                    o_sum = (i_a + i_b);
                end

            endmodule
        ''')
        self.assertEqual(text.strip(), expected.strip())


class HierarchyTests(unittest.TestCase):
    """The output keeps child modules and named instances."""

    def setUp (self):
        mod = load_file(os.path.join(CONSTRUCTS, 'hierarchy.py'))
        self.text, self.modules, _ = emit_top(mod.elaborate_parent())
        self.child = self.modules[0].name
        self.top = self.modules[-1].name

    def test_two_modules_child_first (self):
        # this design holds one build of each block, so neither name
        # carries its parameters; see ParameterNameTests below
        names = [m.name for m in self.modules]
        self.assertEqual(names, ['add2', 'parent'])
        self.assertEqual(self.text.count('module ' + self.child), 1)
        self.assertEqual(self.text.count('module ' + self.top), 1)
        self.assertLess(self.text.index('module ' + self.child),
                        self.text.index('module ' + self.top))
        self.assertNotIn('__', self.child)
        self.assertNotIn('__', self.top)

    def test_named_instances_not_inlined (self):
        self.assertIn(self.child + ' inst_a (', self.text)
        self.assertIn(self.child + ' inst_b (', self.text)
        self.assertIn('.i_a(i_x)', self.text)
        self.assertIn('.o_sum(mid)', self.text)
        parent = self.text[self.text.index('module ' + self.top):]
        self.assertNotIn('sum_comb', parent)
        self.assertNotIn('always_comb', parent)

    def test_bundle_expands_to_named_ports (self):
        self.assertIn('bus_data', self.text)
        self.assertIn('bus_ack', self.text)
        self.assertIn('bus_ack =', self.text)
        self.assertIn('o_z[0]', self.text)

    def test_same_parameters_share_one_module (self):
        self.assertEqual(self.text.count('module ' + self.child), 1)
        self.assertEqual(self.text.count(self.child + ' inst_'), 2)

    def test_specialised_parameter_is_a_second_module (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def leaf (
                    i_a,
                    o_y,
                    WIDTH
                ):
                @always_comb
                def y_comb ():
                    o_y.next = i_a
                return instances()
            @block
            def top (
                    i_a,
                    o_lo,
                    o_hi
                ):
                inst_lo = leaf (
                    i_a = i_a,
                    o_y = o_lo,
                    WIDTH = 4
                )
                inst_hi = leaf (
                    i_a = i_a,
                    o_y = o_hi,
                    WIDTH = 8
                )
                return instances()
        ''')
        ns = load(src)
        top = ns['top'](i_a = signal(4), o_lo = signal(4), o_hi = signal(8))
        text, modules, _ = emit_top(top)
        names = [m.name for m in modules]
        self.assertEqual(set(names),
                         {'leaf_WIDTH_4', 'leaf_WIDTH_8', 'top'})
        self.assertIn('leaf_WIDTH_4 inst_lo (', text)
        self.assertIn('leaf_WIDTH_8 inst_hi (', text)
        self.assertNotIn('__', text)

    def test_yosys_keeps_hierarchy (self):
        path = verilator_lint(self.text, self.top)
        log = yosys_script(
            path,
            f'read_verilog -sv {path}; hierarchy -top {self.top}; ls')
        self.assertIn('\\' + self.top, log.replace(' ', ''))
        self.assertRegex(log, r'\b' + self.child + r'\b')
        self.assertRegex(log, r'\b' + self.top + r'\b')
        self.assertNotIn('Failed to', log)

    def test_verilator_sim_through_two_instances (self):
        out = verilator_sim(self.text, self.top, parent_tb(self.top))
        self.assertIn('PASS', out)


class ConstructTests(unittest.TestCase):
    def test_enum_always_ff_nonblocking (self):
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
        self.assertIn('typedef enum logic {', text)
        self.assertIn('IDLE = 1\'b0', text)
        self.assertIn('always_ff @(posedge i_clock) begin : fsm_logic', text)
        self.assertIn('fsm <= IDLE;', text)
        self.assertIn('o_run = (fsm == RUN);', text)
        ff = text.split('always_ff', 1)[1]
        self.assertRegex(ff, r'\bfsm <= ')
        self.assertNotRegex(ff, r'\bfsm = ')
        verilator_lint(text, 'fsm1')

    def test_match_bits_is_casez (self):
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
        text, modules, _ = emit_top(top)
        match = [s for s in modules[0].processes[0].body
                 if isinstance(s, ir.Match)][0]
        self.assertFalse(match.unique)
        self.assertIn('casez (instr)', text)
        self.assertIn("4'b1???:", text)
        self.assertIn('default: begin', text)
        verilator_lint(text, 'dec')

    def test_match_enum_unique_case (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def dec (i_go, o_idle):
                state = enum('IDLE', 'RUN')
                fsm = signal(state)
                @always_comb
                def dec_comb ():
                    fsm.next = state.IDLE
                    if (i_go):
                        fsm.next = state.RUN
                    o_idle.next = False
                    match fsm:
                        case state.IDLE:
                            o_idle.next = True
                        case state.RUN:
                            o_idle.next = False
                return instances()
        ''')
        ns = load(src)
        top = ns['dec'](i_go = signal(), o_idle = signal())
        text, modules, _ = emit_top(top)
        match = [s for s in modules[0].processes[0].body
                 if isinstance(s, ir.Match)][0]
        self.assertTrue(match.unique)
        self.assertIn('unique case (fsm)', text)
        self.assertIn('IDLE: begin', text)
        verilator_lint(text, 'dec')

    def test_part_select_and_bit_write (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def mux4 (i_word, i_slot, o_nibble, o_onehot):
                @always_comb
                def mux_comb ():
                    o_nibble.next = i_word[i_slot*4 + 3:i_slot*4]
                    o_onehot.next = 0
                    o_onehot.next[o_nibble[1:0]] = o_nibble[2]
                return instances()
        ''')
        ns = load(src)
        top = ns['mux4'](i_word = signal(16), i_slot = signal(2),
                         o_nibble = signal(4), o_onehot = signal(4))
        text, _, _ = emit_top(top)
        self.assertIn('+: 4]', text)
        self.assertIn('o_onehot[', text)
        verilator_lint(text, 'mux4')

    def test_concat_replicate_sign_extend (self):
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
        compact = text.replace(' ', '')
        self.assertIn('{4{i_n[3]}}', compact)
        self.assertIn('{{4{i_n[3]}},i_n}', compact)
        verilator_lint(text, 'sext')

    def test_named_constant_and_slice_bounds (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def rst (o_pc, WIDTH = 8):
                RESET = 16
                @always_comb
                def rst_comb ():
                    o_pc.next = RESET
                    o_pc.next = o_pc[WIDTH-1:0]
                return instances()
        ''')
        ns = load(src)
        top = ns['rst'](o_pc = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('localparam RESET =', text)
        self.assertIn('RESET', text.split('rst_comb', 1)[1])
        self.assertIn('[WIDTH-1:0]', text)
        verilator_lint(text, 'rst')

    def test_function_keeps_name_and_call_site_width (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def unit (a, o):
                def widen (x):
                    r = vector(8)
                    r[7:0] = concat(replicate(x[3], 4), x[3:0])
                    return r
                @always_comb
                def p_comb ():
                    o.next = widen(a)
                return instances()
        ''')
        ns = load(src)
        top = ns['unit'](a = signal(4), o = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('function automatic logic [7:0] widen;', text)
        self.assertIn('input logic [3:0] x;', text)
        self.assertIn('o = widen(', text)
        verilator_lint(text, 'unit')

    def test_array_and_for (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def or_mem (i_mem, i_sel, o_q):
                @always_comb
                def or_comb ():
                    o_q.next = 0
                    for i in range(4):
                        o_q.next = o_q | i_mem[i]
                    o_q.next = i_mem[i_sel]
                return instances()
        ''')
        ns = load(src)
        top = ns['or_mem'](i_mem = signals(4, 8), i_sel = signal(2),
                           o_q = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('i_mem [4]', text)
        self.assertIn('for (int i = 0; i < 4; i++) begin', text)
        verilator_lint(text, 'or_mem')

    def test_comments_travel (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def cmt (a, o):
                t = signal(8)    # scratch
                @always_comb
                def cmt_comb ():
                    # base
                    o.next = a
                    o.next = t
                return instances()
        ''')
        ns = load(src)
        top = ns['cmt'](a = signal(8), o = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('// scratch', text)
        self.assertIn('// base', text)

    def test_docstring_is_a_slash_comment (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def add2 (i_a, i_b, o_sum, WIDTH = 4):
                """WIDTH-bit add, carry dropped by an explicit slice."""
                @always_comb
                def sum_comb ():
                    o_sum.next = (i_a + i_b)[WIDTH-1:0]
                return instances()
        ''')
        ns = load(src)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        text, _, _ = emit_top(top)
        self.assertIn('// WIDTH-bit add, carry dropped by an explicit slice.',
                      text)
        self.assertNotIn('/*', text)
        self.assertTrue(text.lstrip().startswith('//'))

    def test_elif_is_priority_if (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def pri (
                    i_sel,
                    o_y
                ):
                @always_comb
                def pri_comb ():
                    o_y.next = 0
                    if (i_sel[0]):
                        o_y.next = 1
                    elif (i_sel[1]):
                        o_y.next = 2
                    else:
                        o_y.next = 3
                return instances()
        ''')
        ns = load(src)
        top = ns['pri'](i_sel = signal(2), o_y = signal(8))
        text, _, _ = emit_top(top)
        # a plain if: the chain has the order it was written with, and
        # nothing asserts that one of its branches must fire
        self.assertNotIn('priority if', text)
        self.assertIn('end else if', text)
        self.assertIn('end else begin', text)
        self.assertNotIn('unique if', text)
        verilator_lint(text, 'pri')

    def test_exclusive_elif_is_a_plain_if (self):
        """unique if is IEEE 1800 and Quartus Prime Standard rejects it
        with a syntax error, so a chain the analyser could prove
        exclusive goes out as the plain if the author wrote."""
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def uni (
                    i_sel,
                    o_y
                ):
                @always_comb
                def uni_comb ():
                    if (i_sel == 0):
                        o_y.next = 1
                    elif (i_sel == 1):
                        o_y.next = 2
                    else:
                        o_y.next = 3
                return instances()
        ''')
        ns = load(src)
        top = ns['uni'](i_sel = signal(2), o_y = signal(8))
        text, _, _ = emit_top(top)
        self.assertNotIn('unique if', text)
        self.assertNotIn('priority if', text)
        self.assertIn("if (i_sel == 2'd0) begin", text)
        verilator_lint(text, 'uni')

    def test_stacked_if_is_plain_if (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def stk (
                    i_sel,
                    o_y
                ):
                @always_comb
                def stk_comb ():
                    o_y.next = 0
                    if (i_sel == 0):
                        o_y.next = 1
                    if (i_sel == 1):
                        o_y.next = 2
                return instances()
        ''')
        ns = load(src)
        top = ns['stk'](i_sel = signal(2), o_y = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('if (i_sel ==', text)
        self.assertNotIn('priority if', text)
        self.assertNotIn('unique if', text)
        verilator_lint(text, 'stk')

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
        self.assertIn('-: 4]', text)
        verilator_lint(text, 'nib')

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
        # onehot, not one_hot. Measured 2026-09-13: Quartus Prime
        # 25.1std calls one_hot an "Invalid value" and Efinity 2026.1
        # says "unknown fsm encoding ignored" and builds the machine
        # binary. onehot is taken by both, and on Efinity a
        # three-state machine really does come out three flops wide.
        self.assertIn('fsm_encoding = "onehot"', text)
        self.assertIn('syn_encoding = "onehot"', text)
        self.assertNotIn('one_hot', text)
        # the first state declared is zero in every encoding, because
        # a full reset clears every flop and a cleared machine has to
        # rest in a state the design named
        self.assertIn("2'd0", text)  # IDLE = 0
        self.assertIn("2'd3", text)  # RUN  = 3, its own bit and bit 0
        verilator_lint(text, 'fsm1')

    def test_the_python_spelling_is_still_one_hot (self):
        """Only what reaches the HDL changed. enum(encoding =
        'one_hot') is a Python name and stays as it was written."""
        from isomorph import enum
        state = enum('IDLE', 'RUN', encoding = 'one_hot')
        self.assertEqual(state.encoding, 'one_hot')
        self.assertEqual([m.value for m in state.members], [0, 3])

    def test_struct_fields (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def pack (
                    i_addr,
                    i_data,
                    o_addr,
                    o_data,
                    o_we
                ):
                cmd_t = struct('cmd_t', address = 8, write = 1, data = 8)
                cmd = signal(cmd_t)
                @always_comb
                def pack_comb ():
                    cmd.address.next = i_addr
                    cmd.write.next = True
                    cmd.data.next = i_data
                    o_addr.next = cmd.address
                    o_data.next = cmd.data
                    o_we.next = cmd.write
                return instances()
        ''')
        ns = load(src)
        top = ns['pack'](i_addr = signal(8), i_data = signal(8),
                         o_addr = signal(8), o_data = signal(8),
                         o_we = signal())
        text, _, _ = emit_top(top)
        self.assertIn('typedef struct packed', text)
        self.assertIn('cmd_t cmd;', text)
        self.assertIn('cmd.address =', text)
        self.assertIn('o_addr = cmd.address;', text)
        verilator_lint(text, 'pack')

    def test_constant_if_unwrapped (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def unit (a, o, FLAG = True):
                @always_comb
                def unit_comb ():
                    o.next = a
                    if (FLAG):
                        o.next = ~a
                return instances()
        ''')
        ns = load(src)
        top = ns['unit'](a = signal(8), o = signal(8))
        text, _, _ = emit_top(top)
        self.assertNotIn('if (FLAG)', text)
        self.assertIn('o = (~a);', text)

    def test_open_port_emits_empty_connection (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def leaf (i_a, o_y, o_unused):
                @always_comb
                def y_comb ():
                    o_y.next = i_a
                    o_unused.next = i_a[0]
                return instances()
            @block
            def top (i_a, o_y):
                inst = leaf(i_a = i_a, o_y = o_y, o_unused = open_port())
                return instances()
        ''')
        ns = load(src)
        top = ns['top'](i_a = signal(8), o_y = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('.o_unused()', text)

    def test_blocking_comb_nonblocking_ff (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def mix (i_clock, i_a, o_c, o_q):
                @always_comb
                def mix_comb ():
                    o_c.next = i_a
                @always_ff (i_clock.posedge)
                def mix_logic ():
                    o_q.next = i_a
                return instances()
        ''')
        ns = load(src)
        top = ns['mix'](i_clock = signal(), i_a = signal(8),
                        o_c = signal(8), o_q = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('o_c = i_a;', text)
        self.assertIn('o_q <= i_a;', text)

    def test_a_parameter_carries_its_comment (self):
        """A port's comment reaches the HDL and a parameter's did not,
        though both are written in the same signature."""
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
        text = emit_sv(modules)
        self.assertIn('    // what kind of block this is', text)
        self.assertIn('parameter ID_CLASS = 1129206866,', text)
        self.assertIn('// how wide the count is', text)
        verilator_lint(text, 'marked')

    def test_a_constant_keeps_the_base_it_was_written_in (self):
        """0xdead is dead in the HDL, the same argument as names and
        comments travelling."""
        src = textwrap.dedent("""\
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
        top = load(src)['marks'](i_clock = signal(), o_q = signal(16))
        modules, _ = analyse(top)
        text = emit_sv(modules)
        self.assertIn("localparam MARK = 'hdead;", text)
        self.assertIn("localparam NIBBLE = 'b1011;", text)
        self.assertIn('localparam PLAIN = 57005;', text)
        verilator_lint(text, 'marks')

    def test_a_nested_bound_keeps_its_brackets (self):
        """(STAGES-1)*WIDTHD-1 is not STAGES-1*WIDTHD-1. The bound is
        written without sized literals, and dropping the brackets on
        the way made HDL that was silently a different number."""
        src = textwrap.dedent("""\
            from isomorph import *

            @block
            def shifter (i_clock, i_data, o_data, STAGES = 3, WIDTHD = 8):
                sample = signal(STAGES * WIDTHD)
                @always_ff (i_clock.posedge)
                def seq_logic ():
                    sample.next = concat(sample[(STAGES-1)*WIDTHD-1:0],
                                         i_data)
                @always_comb
                def o_comb ():
                    o_data.next = sample[STAGES*WIDTHD-1:(STAGES-1)*WIDTHD]
                return instances()
        """)
        top = load(src)['shifter'](i_clock = signal(), i_data = signal(8),
                                   o_data = signal(8))
        text, _, _ = emit_top(top)
        self.assertIn('(STAGES-1)*WIDTHD-1', text)
        self.assertIn('(STAGES-1)*WIDTHD', text)
        self.assertNotIn('STAGES-1*WIDTHD', text)
        verilator_lint(text, 'shifter')

    def test_mini_core_lints (self):
        mod = load_file(os.path.join(CONSTRUCTS, 'mini_core.py'))
        top = mod.mini_core (
            i_clock = signal(), i_reset = signal(),
            i_word = signal(16), i_a = signal(8), i_b = signal(8),
            o_q = signal(8), o_done = signal()
        )
        text, _, _ = emit_top(top)
        self.assertIn('always_comb begin : decode_comb', text)
        self.assertIn('always_ff @(posedge i_clock) begin : fsm_logic', text)
        self.assertIn('+: 4]', text)
        self.assertIn('casez (instr)', text)
        # the replicate count follows the generic, so a build at
        # another WIDTH is the same module text
        self.assertIn('{(WIDTH-4){i_a[', text)
        verilator_lint(text, 'mini_core')


class SimTests(unittest.TestCase):
    """Verilator C++ is the reference."""

    def test_add2_verilator_cpp (self):
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
        out = verilator_sim(text, 'add2', ADD2_TB)
        self.assertIn('PASS', out)

    def test_clocked_counter_verilator_cpp (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def count4 (i_clock, i_reset, o_q):
                nxt = signal(4)
                assign(nxt, lambda: (o_q + 1)[3:0])
                @always_ff (i_clock.posedge)
                def count_logic ():
                    if (i_reset):
                        o_q.next = 0
                    else:
                        o_q.next = nxt
                return instances()
        ''')
        ns = load(src)
        top = ns['count4'](i_clock = signal(), i_reset = signal(),
                           o_q = signal(4))
        text, _, _ = emit_top(top)
        out = verilator_sim(text, 'count4', COUNT_TB)
        self.assertIn('PASS', out)

    def test_part_down_verilator_cpp (self):
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
        cpp = r'''
#include "Vnib.h"
#include "verilated.h"
#include <cstdio>
int main (int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    Vnib *top = new Vnib;
    top->i_word = 0xABCD;
    top->i_top = 15; top->eval();
    if (top->o_n != 0xA) { std::printf("FAIL 15 %u\n", top->o_n); return 1; }
    top->i_top = 11; top->eval();
    if (top->o_n != 0xB) { std::printf("FAIL 11 %u\n", top->o_n); return 1; }
    top->i_top = 7; top->eval();
    if (top->o_n != 0xC) { std::printf("FAIL 7 %u\n", top->o_n); return 1; }
    top->i_top = 3; top->eval();
    if (top->o_n != 0xD) { std::printf("FAIL 3 %u\n", top->o_n); return 1; }
    std::printf("PASS\n");
    delete top;
    return 0;
}
'''
        out = verilator_sim(text, 'nib', cpp)
        self.assertIn('PASS', out)

    def test_struct_verilator_cpp (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def pack (
                    i_addr,
                    i_data,
                    o_addr,
                    o_data,
                    o_we
                ):
                cmd_t = struct('cmd_t', address = 8, write = 1, data = 8)
                cmd = signal(cmd_t)
                @always_comb
                def pack_comb ():
                    cmd.address.next = i_addr
                    cmd.write.next = True
                    cmd.data.next = i_data
                    o_addr.next = cmd.address
                    o_data.next = cmd.data
                    o_we.next = cmd.write
                return instances()
        ''')
        ns = load(src)
        top = ns['pack'](i_addr = signal(8), i_data = signal(8),
                         o_addr = signal(8), o_data = signal(8),
                         o_we = signal())
        text, _, _ = emit_top(top)
        cpp = r'''
#include "Vpack.h"
#include "verilated.h"
#include <cstdio>
int main (int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    Vpack *top = new Vpack;
    top->i_addr = 0x12; top->i_data = 0x34; top->eval();
    if (top->o_addr != 0x12) { std::printf("FAIL addr %u\n", top->o_addr); return 1; }
    if (top->o_data != 0x34) { std::printf("FAIL data %u\n", top->o_data); return 1; }
    if (top->o_we != 1) { std::printf("FAIL we\n"); return 1; }
    top->i_addr = 0xFF; top->i_data = 0x01; top->eval();
    if (top->o_addr != 0xFF || top->o_data != 0x01) { std::printf("FAIL 2\n"); return 1; }
    std::printf("PASS\n");
    delete top;
    return 0;
}
'''
        out = verilator_sim(text, 'pack', cpp)
        self.assertIn('PASS', out)


class ConvertTests(unittest.TestCase):
    def test_convert_writes_sv (self):
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
        path = os.path.join(tempfile.mkdtemp(prefix = 'iso_cv_'), 'add2.sv')
        convert(top, sv = path)
        with open(path) as f:
            text = f.read()
        self.assertIn('module add2', text)
        lint_sv(path, top = 'add2')

    def test_truncation_still_an_error (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def add2 (i_a, i_b, o_sum):
                @always_comb
                def sum_comb ():
                    o_sum.next = i_a + i_b
                return instances()
        ''')
        ns = load(src)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        with self.assertRaisesRegex(ConversionError, 'result truncated'):
            analyse(top)


if __name__ == '__main__':
    unittest.main()


class SeparatorTests (unittest.TestCase):
    """One file holds every module, and each carries its own header
    comment, so something has to say where one ends."""

    SRC = textwrap.dedent('''\
        from isomorph import *

        @block
        def leaf (i_clock, i_d, o_q):
            """What the leaf is for."""
            @always_ff (i_clock.posedge)
            def ff ():
                o_q.next = i_d
            return instances()

        @block
        def top (i_clock, i_d, o_q):
            """What the top is for."""
            mid = signal()
            inst = leaf(i_clock = i_clock, i_d = i_d, o_q = mid)
            @always_ff (i_clock.posedge)
            def ff ():
                o_q.next = mid
            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['top'](i_clock = signal(), i_d = signal(),
                         o_q = signal())

    def text (self):
        return emit_sv(analyse(self.build())[0])

    def test_a_bar_precedes_every_module (self):
        lines = self.text().splitlines()
        bars = [n for n, line in enumerate(lines)
                if line.startswith('//---')]
        modules = [n for n, line in enumerate(lines)
                   if line.startswith('module ')]
        self.assertEqual(len(bars), 2, 'one bar per module')
        self.assertEqual(len(modules), 2)
        for bar, module in zip(bars, modules):
            self.assertLess(bar, module)

    def test_the_bar_reaches_column_79 (self):
        for line in self.text().splitlines():
            if line.startswith('//---'):
                self.assertEqual(len(line), 79, line)

    def test_each_module_keeps_its_own_header (self):
        text = self.text()
        self.assertIn('// What the leaf is for.', text)
        self.assertIn('// What the top is for.', text)


class ParameterNameTests (unittest.TestCase):
    """A block is called what it is, unless the design builds it twice.

    A parameter that differs from its default is baked into the module
    during elaboration - a width, a counter limit, the contents of a
    memory - so two blocks built from one source with different
    parameters really are two modules and cannot share a name. But
    three uarts at one baud rate are one module instantiated three
    times, which is what an instance name is for, and calling that
    module uart_SYSTEM_CLOCK_50000000 tells nobody anything.
    """

    SRC = textwrap.dedent('''\
        from isomorph import *


        @block
        def leaf (i_a, o_y, WIDTH = 4):
            @always_comb
            def leaf_comb ():
                o_y.next = (i_a + 1)[WIDTH-1:0]

            return instances()


        @block
        def one_build (i_a, o_p, o_q):
            inst_p = leaf(i_a = i_a, o_y = o_p, WIDTH = 8)
            inst_q = leaf(i_a = i_a, o_y = o_q, WIDTH = 8)
            return instances()


        @block
        def two_builds (i_a, o_p, o_q):
            inst_p = leaf(i_a = i_a, o_y = o_p, WIDTH = 8)
            inst_q = leaf(i_a = i_a, o_y = o_q, WIDTH = 6)
            return instances()
    ''')

    def build (self, name):
        ns = load(self.SRC)
        return ns[name](i_a = signal(8), o_p = signal(8), o_q = signal(8))

    def test_one_build_is_named_after_the_block (self):
        text, modules, _ = emit_top(self.build('one_build'))
        self.assertEqual([m.name for m in modules], ['leaf', 'one_build'])
        # one module, two instances, told apart by instance name alone
        self.assertEqual(text.count('module leaf'), 1)
        self.assertIn('leaf inst_p (', text)
        self.assertIn('leaf inst_q (', text)
        self.assertNotIn('WIDTH_8', text)

    def test_two_builds_are_still_told_apart (self):
        text, modules, _ = emit_top(self.build('two_builds'))
        names = {m.name for m in modules}
        self.assertEqual(names, {'leaf_WIDTH_8', 'leaf_WIDTH_6',
                                 'two_builds'})
        self.assertIn('leaf_WIDTH_8 inst_p (', text)
        self.assertIn('leaf_WIDTH_6 inst_q (', text)


class ParameterWidthTests (unittest.TestCase):
    """A parameter that stays a parameter is one module, whatever it
    is set to.

    A number has no width of its own: it takes the width of where it is
    used, and a parameter is a number. Sizing one by the value it
    happens to hold - 200 needs eight bits, 3 needs two - would pad the
    body differently in each build, and two bodies that differ only in
    the padding are two modules in the emitted HDL where the source
    says one.

    Where it is used is said explicitly, though. o_word = 16'(WORD) is
    the target's width, which is a property of the module rather than
    of the value, so it is the same text in every build and the two
    still merge. Leaving it bare made the assignment depend on the
    tool's context rules, and once a localparam carries its own
    expression rather than a number Verilator reads that as a
    truncation and fails the lint.
    """

    SRC = textwrap.dedent('''\
        from isomorph import *


        @block
        def leaf (i_count, o_hit, o_word, LIMIT = 200, WORD = 7):

            @always_comb
            def leaf_comb ():
                o_hit.next = (i_count == LIMIT)
                o_word.next = WORD

            return instances()


        @block
        def two (i_count, o_a, o_wa, o_b, o_wb):

            inst_a = leaf(i_count = i_count, o_hit = o_a, o_word = o_wa,
                          LIMIT = 200, WORD = 7)
            inst_b = leaf(i_count = i_count, o_hit = o_b, o_word = o_wb,
                          LIMIT = 3, WORD = 4095)

            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['two'](i_count = signal(16), o_a = signal(),
                         o_wa = signal(16), o_b = signal(),
                         o_wb = signal(16))

    def test_one_module_and_an_override (self):
        text, _, _ = emit_top(self.build())
        self.assertEqual(text.count('module leaf'), 1)
        self.assertIn('leaf inst_a (', text)
        self.assertIn('leaf #(.LIMIT(3), .WORD(4095)) inst_b (', text)
        self.assertNotIn('LIMIT_3', text)
        self.assertNotIn('WORD_4095', text)

    def test_the_parameter_is_not_padded (self):
        text, _, _ = emit_top(self.build())
        # the body reads the parameter by name, sized to the target
        # and never to the value the parameter happens to hold
        self.assertIn('o_hit = (i_count == LIMIT);', text)
        self.assertIn("o_word = 16'(WORD);", text)
        self.assertNotIn("3'(WORD)", text)
        self.assertNotIn("12'(WORD)", text)


class NegativeLiteralTests (unittest.TestCase):
    """The sign goes outside a sized literal.

    8'sd-1 is not a number to Verilog: it reads the base and finds no
    digits, and verilator says so. -8'sd1 is the same value written
    where the tools can read it. VHDL was always right here, because
    it writes the bit pattern rather than a signed decimal.
    """

    SRC = textwrap.dedent('''\
        from isomorph import *
        @block
        def neg (o_a, o_b):
            @always_comb
            def neg_comb ():
                o_a.next = -const(1, 8)
                o_b.next = const(-1, 8)
            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['neg'](o_a = signal(8), o_b = signal(8))

    def test_the_sign_is_outside_the_literal (self):
        text, _, _ = emit_top(self.build())
        self.assertIn("o_a = -8'sd1;", text)
        self.assertNotIn("8'sd-1", text)

    def test_a_negative_const_is_its_bit_pattern (self):
        """const(-1, 8) is eight ones, and needs no sign at all."""
        text, _, _ = emit_top(self.build())
        self.assertIn("o_b = 8'd255;", text)

    def test_both_are_the_same_value (self):
        from isomorph import Simulator
        sim = Simulator(self.build())
        sim.eval()
        self.assertEqual(sim.get('o_a'), 255)
        self.assertEqual(sim.get('o_b'), 255)

    def test_it_lints (self):
        """The spelling this replaces did not: verilator reports
        'Number is missing value digits' on 8'sd-1."""
        text, _, _ = emit_top(self.build())
        verilator_lint(text, 'neg')


class ItemOrderTests(unittest.TestCase):
    """Two continuous assignments written on one source line.

    Items are emitted where they were written, and the key is the line,
    what kind of item it is and its name. Two assigns made in a loop
    share all of those, and sorting then reached for the items
    themselves, which do not compare: TypeError, '<' not supported
    between instances of 'ContAssign'.
    """

    def build (self):
        @block
        def fan (i_data, o_one, o_two, o_three):
            """One input to three outputs, assigned in a loop."""
            for port in (o_one, o_two, o_three):
                assign(port, lambda p = port: i_data)
            return instances()

        return fan(i_data = signal(), o_one = signal(),
                   o_two = signal(), o_three = signal())

    def test_it_emits_at_all (self):
        text = emit_sv(analyse(self.build())[0])
        self.assertEqual(text.count('assign o_'), 3)

    def test_the_vhdl_too (self):
        from isomorph import emit_vhdl
        text = emit_vhdl(analyse(self.build())[0])
        self.assertIn('o_one <= i_data;', text)
        self.assertIn('o_three <= i_data;', text)
