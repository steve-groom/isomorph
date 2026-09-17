"""The Python IR executor, against known values and against C99."""
import importlib.util
import itertools
import linecache
import os
import textwrap
import unittest

from isomorph import (analyse, sidecar, signal, Simulator, SimError)
from isomorph.execute import Executor

HERE = os.path.dirname(os.path.abspath(__file__))
CONSTRUCTS = os.path.join(HERE, 'constructs')
_counter = itertools.count()


def load (src):
    name = f'<ex{next(_counter)}>'
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


ADD2 = textwrap.dedent('''\
    from isomorph import *
    @block
    def add2 (i_a, i_b, o_sum, WIDTH = 4):
        @always_comb
        def sum_comb ():
            o_sum.next = (i_a + i_b)[WIDTH-1:0]
        return instances()
''')

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

NIB = textwrap.dedent('''\
    from isomorph import *
    @block
    def nib (i_word, i_top, o_n):
        @always_comb
        def nib_comb ():
            o_n.next = i_word.part_down(i_top, 4)
        return instances()
''')

OSC = textwrap.dedent('''\
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


class ExecuteTests (unittest.TestCase):
    def test_add2 (self):
        ns = load(ADD2)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        ex = Executor(analyse(top)[0])
        ex.set('i_a', 2)
        ex.set('i_b', 3)
        ex.eval()
        self.assertEqual(ex.get('o_sum'), 5)
        ex.set('i_a', 15)
        ex.set('i_b', 1)
        ex.eval()
        self.assertEqual(ex.get('o_sum'), 0)

    def test_add2_all_inputs (self):
        ns = load(ADD2)
        top = ns['add2'](i_a = signal(4), i_b = signal(4), o_sum = signal(4))
        ex = Executor(analyse(top)[0])
        for a in range(16):
            for b in range(16):
                ex.set('i_a', a)
                ex.set('i_b', b)
                ex.eval()
                self.assertEqual(ex.get('o_sum'), (a + b) & 15)

    def test_count4 (self):
        ns = load(COUNT)
        top = ns['count4'](i_clock = signal(), i_reset = signal(),
                           o_q = signal(4))
        ex = Executor(analyse(top)[0])
        ex.set('i_reset', 1)
        ex.tick(2)
        self.assertEqual(ex.get('o_q'), 0)
        ex.set('i_reset', 0)
        for i in range(1, 6):
            ex.tick()
            self.assertEqual(ex.get('o_q'), i)

    def test_part_down (self):
        ns = load(NIB)
        top = ns['nib'](i_word = signal(16), i_top = signal(4),
                        o_n = signal(4))
        ex = Executor(analyse(top)[0])
        ex.set('i_word', 0xABCD)
        for t, want in ((15, 0xA), (11, 0xB), (7, 0xC), (3, 0xD)):
            ex.set('i_top', t)
            ex.eval()
            self.assertEqual(ex.get('o_n'), want)

    def test_hierarchy (self):
        mod = load_file(os.path.join(CONSTRUCTS, 'hierarchy.py'))
        ex = Executor(analyse(mod.elaborate_parent())[0])
        ex.set('i_x', 1)
        ex.set('i_y', 2)
        ex.set('bus_data', 4)
        ex.eval()
        self.assertEqual(ex.get('o_z'), 7)
        self.assertEqual(ex.get('bus_ack'), 1)

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
        ex = Executor(analyse(top)[0])
        ex.set('i_addr', 0x12)
        ex.set('i_data', 0x34)
        ex.eval()
        self.assertEqual(ex.get('o_addr'), 0x12)
        self.assertEqual(ex.get('o_data'), 0x34)

    def test_comb_loop (self):
        ns = load(OSC)
        top = ns['osc'](o_q = signal())
        ex = Executor(analyse(top, allow_severe = True)[0])
        with self.assertRaises(SimError) as ctx:
            ex.eval()
        self.assertIn('combinational loop', str(ctx.exception))

    def test_sidecar (self):
        ns = load(COUNT)
        top = ns['count4'](i_clock = signal(), i_reset = signal(),
                           o_q = signal(4))
        modules, _ = analyse(top)
        data = sidecar(modules)
        self.assertEqual(data['top'], 'count4')
        ports = {p['name']: p for p in data['modules'][-1]['ports']}
        self.assertEqual(ports['o_q']['width'], 4)
        clocks = [p['clock'] for p in data['modules'][-1]['processes']
                  if p['kind'] == 'ff']
        self.assertEqual(clocks, ['i_clock'])


class SimulatorPythonTests (unittest.TestCase):
    def test_count4_api (self):
        ns = load(COUNT)
        top = ns['count4'](i_clock = signal(), i_reset = signal(),
                           o_q = signal(4))
        sim = Simulator(top, backend = 'python')
        sim.add_clock(10e-9)
        sim.set('i_reset', 1)
        sim.tick(2)
        self.assertEqual(sim.get('o_q'), 0)
        sim.set('i_reset', 0)
        seq = []
        for _ in range(5):
            sim.tick()
            seq.append(sim.get('o_q'))
        self.assertEqual(seq, [1, 2, 3, 4, 5])
        sim.close()


if __name__ == '__main__':
    unittest.main()
