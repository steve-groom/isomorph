"""C99 smoke versus Verilator C++ on the same stimulus.

Verilator is the design of record. A mismatch is a C99 emitter bug.
"""
import itertools
import linecache
import os
import subprocess
import tempfile
import textwrap
import unittest

from isomorph import analyse, emit_c99, emit_sv, signal

_counter = itertools.count()


def gcc_run (header, source, top, main_c):
    d = tempfile.mkdtemp(prefix = 'iso_cmp_c_')
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
        raise AssertionError('c99 sim failed:\n' + run.stdout + run.stderr)
    return run.stdout


def verilator_run (text, top, cpp):
    directory = tempfile.mkdtemp(prefix = 'iso_cmp_v_')
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
            'verilator sim failed:\n'
            + (run.stdout or '') + (run.stderr or ''))
    return run.stdout


def traces (modules, top, c_main, cpp_main):
    sv = emit_sv(modules)
    h, c = emit_c99(modules)
    return gcc_run(h, c, top, c_main), verilator_run(sv, top, cpp_main)


ADD2_SRC = textwrap.dedent('''\
    from isomorph import *
    @block
    def add2 (i_a, i_b, o_sum, WIDTH = 4):
        @always_comb
        def sum_comb ():
            o_sum.next = (i_a + i_b)[WIDTH-1:0]
        return instances()
''')

ADD2_C = r'''
#include "add2.h"
#include <stdio.h>
int main(void) {
    add2 s;
    int a, b;
    add2_init(&s);
    for (a = 0; a < 16; a++) {
        for (b = 0; b < 16; b++) {
            s.i_a = (uint64_t)a;
            s.i_b = (uint64_t)b;
            add2_eval(&s);
            printf("%d %d %llu\n", a, b, (unsigned long long)s.o_sum);
        }
    }
    return 0;
}
'''

ADD2_CPP = r'''
#include "Vadd2.h"
#include "verilated.h"
#include <cstdio>
int main (int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    Vadd2 *top = new Vadd2;
    for (int a = 0; a < 16; a++) {
        for (int b = 0; b < 16; b++) {
            top->i_a = a; top->i_b = b; top->eval();
            std::printf("%d %d %u\n", a, b, top->o_sum);
        }
    }
    delete top;
    return 0;
}
'''

COUNT_SRC = textwrap.dedent('''\
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

COUNT_C = r'''
#include "count4.h"
#include <stdio.h>
int main(void) {
    count4 s;
    int i;
    count4_init(&s);
    s.i_reset = 1;
    count4_tick(&s);
    count4_tick(&s);
    printf("R %llu\n", (unsigned long long)s.o_q);
    s.i_reset = 0;
    for (i = 0; i < 20; i++) {
        count4_tick(&s);
        printf("T %d %llu\n", i, (unsigned long long)s.o_q);
    }
    return 0;
}
'''

COUNT_CPP = r'''
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
    std::printf("R %u\n", top->o_q);
    top->i_reset = 0;
    for (int i = 0; i < 20; i++) {
        tick(top);
        std::printf("T %d %u\n", i, top->o_q);
    }
    delete top;
    return 0;
}
'''

NIB_SRC = textwrap.dedent('''\
    from isomorph import *
    @block
    def nib (i_word, i_top, o_n):
        @always_comb
        def nib_comb ():
            o_n.next = i_word.part_down(i_top, 4)
        return instances()
''')

NIB_C = r'''
#include "nib.h"
#include <stdio.h>
int main(void) {
    nib s;
    int t;
    nib_init(&s);
    s.i_word = 0xABCDULL;
    for (t = 3; t < 16; t++) {
        s.i_top = (uint64_t)t;
        nib_eval(&s);
        printf("%d %llu\n", t, (unsigned long long)s.o_n);
    }
    return 0;
}
'''

NIB_CPP = r'''
#include "Vnib.h"
#include "verilated.h"
#include <cstdio>
int main (int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    Vnib *top = new Vnib;
    top->i_word = 0xABCD;
    for (int t = 3; t < 16; t++) {
        top->i_top = t; top->eval();
        std::printf("%d %u\n", t, top->o_n);
    }
    delete top;
    return 0;
}
'''


def load (src):
    name = f'<cmp{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


class C99VsVerilatorTests(unittest.TestCase):
    def test_add2_all_inputs (self):
        ns = load(ADD2_SRC)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        c_out, v_out = traces(analyse(top)[0], 'add2', ADD2_C, ADD2_CPP)
        self.assertEqual(c_out, v_out)

    def test_count4_ticks (self):
        ns = load(COUNT_SRC)
        top = ns['count4'](i_clock = signal(), i_reset = signal(),
                           o_q = signal(4))
        c_out, v_out = traces(analyse(top)[0], 'count4', COUNT_C, COUNT_CPP)
        self.assertEqual(c_out, v_out)

    def test_part_down_all_tops (self):
        ns = load(NIB_SRC)
        top = ns['nib'](i_word = signal(16), i_top = signal(4),
                        o_n = signal(4))
        c_out, v_out = traces(analyse(top)[0], 'nib', NIB_C, NIB_CPP)
        self.assertEqual(c_out, v_out)


if __name__ == '__main__':
    unittest.main()
