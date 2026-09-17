"""Simulator API, VCD, C99 ctypes backend, sim-report."""
import itertools
import json
import linecache
import os
import subprocess
import tempfile
import textwrap
import unittest

from isomorph import analyse, emit_c99, signal, Simulator, SimError, convert
from isomorph.sim import RUNTIME_DIR
from isomorph.sim_report import report
from isomorph.vcd import parse_vcd, values_at

_counter = itertools.count()


def load (src):
    name = f'<sim{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


COUNT = textwrap.dedent('''\
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

ADD2 = textwrap.dedent('''\
    from isomorph import *
    @block
    def add2 (i_a, i_b, o_sum, WIDTH = 4):
        @always_comb
        def sum_comb ():
            o_sum.next = (i_a + i_b)[WIDTH-1:0]
        return instances()
''')


def make_count4 ():
    ns = load(COUNT)
    return ns['count4'](i_clock = signal(), i_reset = signal(),
                        o_q = signal(4))


def run_count4 (backend, vcd_path = None, log_path = None):
    sim = Simulator(make_count4(), backend = backend)
    sim.add_clock(10e-9)
    sim.set('i_reset', 1)
    if vcd_path:
        sim.write_vcd(vcd_path, traces = ['o_q', 'i_reset'])
    if log_path:
        sim.write_log(log_path)
    sim.tick(2)
    sim.set('i_reset', 0)
    seq = []
    for _ in range(5):
        sim.tick()
        seq.append(sim.get('o_q'))
    sim.close()
    return seq


class SimulatorTests (unittest.TestCase):
    def test_python_count4 (self):
        self.assertEqual(run_count4('python'), [1, 2, 3, 4, 5])

    def test_c99_count4 (self):
        self.assertEqual(run_count4('c99'), [1, 2, 3, 4, 5])

    def test_python_and_c99_match_add2 (self):
        ns = load(ADD2)
        def run (backend):
            top = ns['add2'](i_a = signal(4), i_b = signal(4),
                             o_sum = signal(4))
            sim = Simulator(top, backend = backend)
            out = []
            for a in range(16):
                for b in range(16):
                    sim.set('i_a', a)
                    sim.set('i_b', b)
                    sim.eval()
                    out.append(sim.get('o_sum'))
            sim.close()
            return out
        self.assertEqual(run('python'), run('c99'))

    def test_vcd_python_count4 (self):
        d = tempfile.mkdtemp(prefix = 'iso_vcd_')
        path = os.path.join(d, 'count4.vcd')
        seq = run_count4('python', vcd_path = path)
        self.assertEqual(seq, [1, 2, 3, 4, 5])
        with open(path, encoding = 'ascii') as f:
            text = f.read()
        self.assertIn('$var wire 4', text)
        self.assertIn('o_q', text)
        _, samples = parse_vcd(text)
        series = [v for t, v in values_at(samples, 'o_q') if v is not None]
        self.assertTrue(series)
        self.assertEqual(series[-1], 5)

    def test_vcd_backends_match (self):
        d = tempfile.mkdtemp(prefix = 'iso_vcd2_')
        py_path = os.path.join(d, 'py.vcd')
        c_path = os.path.join(d, 'c.vcd')
        run_count4('python', vcd_path = py_path)
        run_count4('c99', vcd_path = c_path)
        with open(py_path, encoding = 'ascii') as f:
            py_text = f.read()
        with open(c_path, encoding = 'ascii') as f:
            c_text = f.read()
        _, py_s = parse_vcd(py_text)
        _, c_s = parse_vcd(c_text)
        py_q = values_at(py_s, 'o_q')
        c_q = values_at(c_s, 'o_q')
        self.assertEqual([v for t, v in py_q], [v for t, v in c_q])

    def test_c99_standalone_vcd (self):
        ns = load(COUNT)
        top = ns['count4'](i_clock = signal(), i_reset = signal(),
                           o_q = signal(4))
        modules, _ = analyse(top)
        header, source = emit_c99(modules)
        from isomorph.emit_c99 import emit_c99_vcd
        vcd_c = emit_c99_vcd(modules)
        d = tempfile.mkdtemp(prefix = 'iso_cvcd_')
        with open(os.path.join(d, 'count4.h'), 'w') as f:
            f.write(header)
        with open(os.path.join(d, 'count4.c'), 'w') as f:
            f.write(source)
        with open(os.path.join(d, 'count4_vcd.c'), 'w') as f:
            f.write(vcd_c)
        vcd_path = os.path.join(d, 'count4.vcd')
        main = r'''
#include "count4.h"
#include "iso_vcd.h"
int main(void) {
    count4 s;
    IsoVcd *v;
    int i;
    uint64_t t;
    count4_init(&s);
    v = iso_vcd_open("''' + vcd_path + r'''", "1 ns");
    if (v == 0) return 1;
    count4_vcd_defs(v);
    iso_vcd_finish_defs(v);
    s.i_reset = 1;
    count4_vcd_dump(v, &s);
    iso_vcd_end_dumpvars(v);
    t = 0;
    for (i = 0; i < 2; i++) {
        t += 10;
        count4_tick(&s);
        iso_vcd_time(v, t);
        count4_vcd_dump(v, &s);
    }
    s.i_reset = 0;
    for (i = 0; i < 5; i++) {
        t += 10;
        count4_tick(&s);
        iso_vcd_time(v, t);
        count4_vcd_dump(v, &s);
    }
    iso_vcd_close(v);
    return 0;
}
'''
        with open(os.path.join(d, 'main.c'), 'w') as f:
            f.write(main)
        bin_path = os.path.join(d, 'sim')
        cmd = ['gcc', '-std=c99', '-Wall', '-Werror',
               '-I', d, '-I', RUNTIME_DIR,
               '-o', bin_path,
               os.path.join(d, 'count4.c'),
               os.path.join(d, 'count4_vcd.c'),
               os.path.join(RUNTIME_DIR, 'iso_vcd.c'),
               os.path.join(RUNTIME_DIR, 'iso_log.c'),
               os.path.join(d, 'main.c')]
        build = subprocess.run(cmd, capture_output = True, text = True)
        if build.returncode != 0:
            raise AssertionError('gcc vcd failed:\n' + build.stderr)
        run = subprocess.run([bin_path], capture_output = True, text = True,
                             cwd = d)
        if run.returncode != 0:
            raise AssertionError('vcd sim failed:\n' + run.stderr)
        with open(vcd_path, encoding = 'ascii') as f:
            text = f.read()
        _, samples = parse_vcd(text)
        series = [v for t, v in values_at(samples, 'o_q')]
        self.assertEqual(series[-1], 5)

    def test_log_and_report (self):
        d = tempfile.mkdtemp(prefix = 'iso_log_')
        log_path = os.path.join(d, 'count4.ndjson')
        seq = run_count4('python', log_path = log_path)
        self.assertEqual(seq, [1, 2, 3, 4, 5])
        result = report(log_path)
        self.assertGreaterEqual(result['cycles'], 7)
        kinds = [e['kind'] for e in result['events']]
        self.assertIn('reset', kinds)
        self.assertIn('reset', result['text'])

    def test_convert_writes_sidecar_when_asked (self):
        ns = load(ADD2)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        d = tempfile.mkdtemp(prefix = 'iso_side_')
        convert(top, sv = False, vhdl = False,
                c99 = os.path.join(d, 'add2.c'))
        # nothing in isomorph reads it, so it is not written unasked
        self.assertFalse(os.path.isfile(os.path.join(d, 'add2.iso.json')))

        convert(top, sv = False, vhdl = False,
                c99 = os.path.join(d, 'add2.c'), json = True)
        path = os.path.join(d, 'add2.iso.json')
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding = 'ascii') as f:
            data = json.load(f)
        self.assertEqual(data['top'], 'add2')
        self.assertTrue(os.path.isfile(os.path.join(d, 'add2_vcd.c')))

    def test_c99_comb_loop (self):
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
        sim = Simulator(top, backend = 'c99', allow_severe = True)
        with self.assertRaises(SimError):
            sim.eval()


class ProbeTests (unittest.TestCase):
    """sim.dut.<name>: every signal of the top by the name it has."""
    def build (self):
        ns = load(COUNT)
        design = ns['count4'](i_clock = signal(), i_reset = signal(),
                              o_q = signal(4))
        return Simulator(design)

    def test_a_signal_reads_through_the_probe (self):
        sim = self.build()
        sim.add_clock(20e-9)
        sim.set('i_reset', 1)
        sim.posedge()
        self.assertEqual(sim.dut.o_q, 0)
        sim.set('i_reset', 0)
        sim.posedge()
        self.assertEqual(sim.dut.o_q, sim.get('o_q'))

    def test_an_internal_signal_is_reachable (self):
        sim = self.build()
        self.assertEqual(sim.dut.nxt, sim.get('nxt'))

    def test_assigning_drives_the_signal (self):
        sim = self.build()
        sim.dut.i_reset = 1
        self.assertEqual(sim.get('i_reset'), 1)

    def test_a_name_the_design_lacks_is_an_error (self):
        sim = self.build()
        with self.assertRaises(SimError) as caught:
            sim.dut.sample_flop
        self.assertIn('sample_flop', str(caught.exception))
        with self.assertRaises(SimError):
            sim.dut.sample_flop = 1

    def test_dir_lists_the_signals (self):
        sim = self.build()
        self.assertIn('o_q', dir(sim.dut))
        self.assertIn('nxt', dir(sim.dut))


if __name__ == '__main__':
    unittest.main()
