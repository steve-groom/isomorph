"""C99 cycle-accurate smoke: gcc -std=c99 compiles and runs."""
import importlib.util
import itertools
import linecache
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from isomorph import (analyse, emit_c99, convert, signal, always_comb,
    always_ff, assign, block, instances, struct, Simulator)

HERE = os.path.dirname(os.path.abspath(__file__))
CONSTRUCTS = os.path.join(HERE, 'constructs')
_counter = itertools.count()


def load (src):
    name = f'<c99{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


def load_file (path):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gcc_run (header, source, top, main_c):
    d = tempfile.mkdtemp(prefix = 'iso_c99_')
    h_path = os.path.join(d, top + '.h')
    c_path = os.path.join(d, top + '.c')
    m_path = os.path.join(d, 'main.c')
    bin_path = os.path.join(d, 'sim')
    with open(h_path, 'w') as f:
        f.write(header)
    with open(c_path, 'w') as f:
        f.write(source)
    with open(m_path, 'w') as f:
        f.write(main_c)
    cmd = ['gcc', '-std=c99', '-Wall', '-Werror', '-I', d,
           '-o', bin_path, c_path, m_path]
    build = subprocess.run(cmd, capture_output = True, text = True)
    if build.returncode != 0:
        raise AssertionError('gcc failed:\n' + build.stderr + '\n' + source)
    run = subprocess.run([bin_path], capture_output = True, text = True)
    if run.returncode != 0:
        raise AssertionError('sim failed:\n' + run.stdout + run.stderr)
    return run.stdout


ADD2_MAIN = r'''
#include "add2.h"
#include <stdio.h>
int main(void) {
    add2 s;
    add2_init(&s);
    s.i_a = 2; s.i_b = 3;
    add2_eval(&s);
    if (s.o_sum != 5ULL) { printf("FAIL sum %llu\n", (unsigned long long)s.o_sum); return 1; }
    s.i_a = 15; s.i_b = 1;
    add2_eval(&s);
    if (s.o_sum != 0ULL) { printf("FAIL wrap %llu\n", (unsigned long long)s.o_sum); return 1; }
    printf("PASS\n");
    return 0;
}
'''

PARENT_MAIN = r'''
#include "parent.h"
#include <stdio.h>
int main(void) {
    parent s;
    parent_init(&s);
    s.i_x = 1; s.i_y = 2; s.bus_data = 4;
    parent_eval(&s);
    if (s.o_z != 7ULL) { printf("FAIL o_z %llu\n", (unsigned long long)s.o_z); return 1; }
    if (s.bus_ack != 1ULL) { printf("FAIL ack\n"); return 1; }
    printf("PASS\n");
    return 0;
}
'''

COUNT_MAIN = r'''
#include "count4.h"
#include <stdio.h>
int main(void) {
    count4 s;
    count4_init(&s);
    s.i_reset = 1;
    count4_tick(&s);
    count4_tick(&s);
    if (s.o_q != 0ULL) { printf("FAIL reset %llu\n", (unsigned long long)s.o_q); return 1; }
    s.i_reset = 0;
    {
        int i;
        for (i = 1; i <= 5; i++) {
            count4_tick(&s);
            if (s.o_q != (uint64_t)i) {
                printf("FAIL tick %d got %llu\n", i, (unsigned long long)s.o_q);
                return 1;
            }
        }
    }
    printf("PASS\n");
    return 0;
}
'''


class C99Tests(unittest.TestCase):
    def test_add2 (self):
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
        h, c = emit_c99(analyse(top)[0])
        self.assertIn('typedef struct add2', h)
        self.assertIn('int add2_eval(add2 *s);', h)
        out = gcc_run(h, c, 'add2', ADD2_MAIN)
        self.assertIn('PASS', out)

    def test_hierarchy (self):
        mod = load_file(os.path.join(CONSTRUCTS, 'hierarchy.py'))
        h, c = emit_c99(analyse(mod.elaborate_parent())[0])
        # one build of each, so each is called what it is
        self.assertIn('typedef struct add2', h)
        self.assertIn('typedef struct parent', h)
        self.assertIn('    add2 inst_a;', h)
        self.assertIn('add2_eval(&s->inst_a);', c)
        out = gcc_run(h, c, 'parent', PARENT_MAIN)
        self.assertIn('PASS', out)

    def test_clocked_counter (self):
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
        h, c = emit_c99(analyse(top)[0])
        self.assertIn('o_q__nxt', h)
        self.assertIn('s->o_q = s->o_q__nxt;', c)
        out = gcc_run(h, c, 'count4', COUNT_MAIN)
        self.assertIn('PASS', out)

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
        h, c = emit_c99(analyse(top)[0])
        main = r'''
#include "nib.h"
#include <stdio.h>
int main(void) {
    nib s;
    nib_init(&s);
    s.i_word = 0xABCDULL;
    s.i_top = 15; nib_eval(&s);
    if (s.o_n != 0xAULL) { printf("FAIL 15 %llu\n", (unsigned long long)s.o_n); return 1; }
    s.i_top = 11; nib_eval(&s);
    if (s.o_n != 0xBULL) { printf("FAIL 11 %llu\n", (unsigned long long)s.o_n); return 1; }
    s.i_top = 7; nib_eval(&s);
    if (s.o_n != 0xCULL) { printf("FAIL 7 %llu\n", (unsigned long long)s.o_n); return 1; }
    s.i_top = 3; nib_eval(&s);
    if (s.o_n != 0xDULL) { printf("FAIL 3 %llu\n", (unsigned long long)s.o_n); return 1; }
    printf("PASS\n");
    return 0;
}
'''
        out = gcc_run(h, c, 'nib', main)
        self.assertIn('PASS', out)

    def test_struct_fields (self):
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
        h, c = emit_c99(analyse(top)[0])
        main = r'''
#include "pack.h"
#include <stdio.h>
int main(void) {
    pack s;
    pack_init(&s);
    s.i_addr = 0x12; s.i_data = 0x34;
    pack_eval(&s);
    if (s.o_addr != 0x12ULL) { printf("FAIL addr %llu\n", (unsigned long long)s.o_addr); return 1; }
    if (s.o_data != 0x34ULL) { printf("FAIL data %llu\n", (unsigned long long)s.o_data); return 1; }
    printf("PASS\n");
    return 0;
}
'''
        out = gcc_run(h, c, 'pack', main)
        self.assertIn('PASS', out)

    def test_comb_loop_returns_error (self):
        src = textwrap.dedent('''\
            from isomorph import *
            @block
            def osc (o_q):
                a = signal()
                b = signal()
                @always_comb
                def p_a ():
                    a.next = ~b
                @always_comb
                def p_b ():
                    b.next = a
                @always_comb
                def out_comb ():
                    o_q.next = a
                return instances()
        ''')
        ns = load(src)
        top = ns['osc'](o_q = signal())
        # the loop stops a conversion now, so this reaches the runtime
        # detector only by asking for it
        h, c = emit_c99(analyse(top, allow_severe = True)[0])
        self.assertIn('_live_eq', c)
        self.assertIn('return 1;', c)
        main = r'''
#include "osc.h"
int main(void) {
    osc s;
    osc_init(&s);
    if (osc_eval(&s) == 0) return 1;
    return 0;
}
'''
        out = gcc_run(h, c, 'osc', main)
        self.assertEqual(out, '')

    def test_convert_writes_c (self):
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
        d = tempfile.mkdtemp(prefix = 'iso_c99w_')
        path = os.path.join(d, 'add2.c')
        convert(top, sv = False, vhdl = False, c99 = path)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isfile(os.path.join(d, 'add2.h')))


if __name__ == '__main__':
    unittest.main()


class EnumScopeTests(unittest.TestCase):
    """Two modules may call a state the same thing.

    A C define is global and a state machine's states are not. One peripheral
    calls its first state IDLE and so does the bridge above it, one of
    them one-hot and the other sequential, and the first define won: the
    bridge's IDLE compiled as 1 instead of 0 and only the C99 backend
    was wrong. SystemVerilog scopes an enum to its module and VHDL to
    its entity; C has to be told.
    """

    SRC = textwrap.dedent('''\
        from isomorph import *

        one_hot_state = enum('IDLE', 'ACTIVE', encoding = 'one_hot')
        plain_state = enum('IDLE', 'BUSY', 'DONE')


        @block
        def leaf (i_clock, i_go, o_active):
            fsm = signal(one_hot_state)

            @always_ff (i_clock.posedge)
            def leaf_logic ():
                if (fsm == one_hot_state.IDLE):
                    if (i_go):
                        fsm.next = one_hot_state.ACTIVE
                if (fsm == one_hot_state.ACTIVE):
                    fsm.next = one_hot_state.IDLE

            @always_comb
            def leaf_comb ():
                o_active.next = (fsm == one_hot_state.ACTIVE)

            return instances()


        @block
        def top (i_clock, i_go, o_done):
            active = signal()
            fsm = signal(plain_state)

            inst_leaf = leaf(i_clock = i_clock, i_go = i_go,
                             o_active = active)

            @always_ff (i_clock.posedge)
            def top_logic ():
                if (fsm == plain_state.IDLE):
                    if (i_go):
                        fsm.next = plain_state.BUSY
                if (fsm == plain_state.BUSY):
                    if (active):
                        fsm.next = plain_state.DONE
                if (fsm == plain_state.DONE):
                    fsm.next = plain_state.IDLE

            @always_comb
            def top_comb ():
                o_done.next = (fsm == plain_state.DONE)

            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['top'](i_clock = signal(), i_go = signal(),
                         o_done = signal())

    def test_the_two_idles_get_different_macros (self):
        """Both IDLEs are zero now, because the first state declared
        is zero in every encoding, so the module prefix is
        the only thing keeping the two apart. That is the point."""
        _, source = emit_c99(analyse(self.build())[0])
        self.assertIn('#define leaf__IDLE 0ULL', source)
        self.assertIn('#define top__IDLE 0ULL', source)
        self.assertNotIn('#define IDLE ', source)

    def test_one_hot_reaches_c99_as_it_was_encoded (self):
        _, source = emit_c99(analyse(self.build())[0])
        # leaf is one_hot: IDLE all zeros, ACTIVE its own bit and bit 0
        self.assertIn('#define leaf__ACTIVE 3ULL', source)

    @unittest.skipUnless(shutil.which('gcc'), 'gcc not installed')
    def test_c99_agrees_with_python_about_the_states (self):
        rows = {}
        for backend in ('python', 'c99'):
            sim = Simulator(self.build(), backend = backend)
            sim.add_clock(20e-9)
            out = []
            for step in range(8):
                sim.set('i_go', 1 if step == 0 else 0)
                sim.tick(1)
                out.append((sim.get('fsm'), sim.get('o_done')))
            rows[backend] = out
        self.assertEqual(rows['c99'], rows['python'])


class ConstantScopeTests(unittest.TestCase):
    """Two modules may call a constant the same thing.

    A C define is global and a module constant is not. Two
    peripherals in one design each called the first character of their
    identifier ID_BYTE_0, one of them 'R' and the other 'C', and the
    first define won. The widths still came from the right module, so
    the second one's VERSION of 1 was read as its neighbour's 2 masked
    to one bit, which is 0: a peripheral answered with the
    name and version of the one beside it. Nothing failed to compile
    and only the C99 backend was wrong.
    """

    SRC = textwrap.dedent('''\
        from isomorph import *


        @block
        def leaf (o_tag, TAG = 'R', VERSION = 2):
            TAG_BYTE = ord(TAG)

            @always_comb
            def leaf_comb ():
                o_tag.next = TAG_BYTE + VERSION

            return instances()


        @block
        def top (o_leaf_tag, o_tag, TAG = 'C', VERSION = 1):
            TAG_BYTE = ord(TAG)

            inst_leaf = leaf(o_tag = o_leaf_tag)

            @always_comb
            def top_comb ():
                o_tag.next = TAG_BYTE + VERSION

            return instances()
    ''')

    def build (self):
        ns = load(self.SRC)
        return ns['top'](o_leaf_tag = signal(16), o_tag = signal(16))

    def test_the_two_versions_get_different_macros (self):
        _, source = emit_c99(analyse(self.build())[0])
        self.assertIn('#define leaf__VERSION 2ULL', source)
        self.assertIn('#define top__VERSION 1ULL', source)
        self.assertIn('#define leaf__TAG_BYTE 82ULL', source)
        self.assertIn('#define top__TAG_BYTE 67ULL', source)
        self.assertNotIn('#define VERSION ', source)
        self.assertNotIn('#define TAG_BYTE ', source)

    @unittest.skipUnless(shutil.which('gcc'), 'gcc not installed')
    def test_c99_agrees_with_python_about_the_constants (self):
        rows = {}
        for backend in ('python', 'c99'):
            sim = Simulator(self.build(), backend = backend)
            sim.eval()
            rows[backend] = (sim.get('o_tag'), sim.get('o_leaf_tag'))
        self.assertEqual(rows['python'], (68, 84))
        self.assertEqual(rows['c99'], rows['python'])


class ShadowTests (unittest.TestCase):
    """The C99 backend keeps a register's next value in a field of its
    own. That field's name must never be one the Python could have
    used, or the two collide in the struct and gcc refuses the file.
    A double underscore is not a legal VHDL identifier, so convert
    refuses it in the Python and the shadow can safely be spelt with
    one. Found by stream_from_memory, which has a flop called credit
    and a wire called credit_nxt."""

    def test_a_signal_named_like_the_old_shadow_does_not_collide (self):
        from isomorph import block, always_ff, always_comb, instances

        @block
        def unit (i_clock, i_up, o_credit):
            credit_nxt = signal(2)

            @always_comb
            def next_comb ():
                credit_nxt.next = (o_credit + i_up)[1:0]

            @always_ff (i_clock.posedge)
            def hold_logic ():
                o_credit.next = credit_nxt

            return instances()

        modules, _ = analyse(unit(i_clock = signal(), i_up = signal(),
                                  o_credit = signal(2)))
        header, source = emit_c99(modules)
        self.assertIn('uint64_t credit_nxt;', header)
        self.assertIn('uint64_t o_credit__nxt;', header)
        self.assertEqual(header.count('credit_nxt;'), 1)

    def test_a_double_underscore_is_refused (self):
        from isomorph import block, always_comb, instances, ConversionError

        @block
        def unit (i_a, o_b):
            a__b = signal()

            @always_comb
            def unit_comb ():
                a__b.next = i_a
                o_b.next = a__b

            return instances()

        with self.assertRaises(ConversionError) as ctx:
            analyse(unit(i_a = signal(), o_b = signal()))
        self.assertIn('VHDL identifier', str(ctx.exception))
