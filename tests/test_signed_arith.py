"""Signedness in the cycle simulators.

signed() only marks an expression; the bits underneath are the same.
That is enough for + - and the bitwise operators, whose two's complement
result is identical either way, and not enough for * and >>. Both cycle
simulators used to ignore it everywhere, so a signed multiply returned a
wrong answer with no warning. Verilator is the reference.
"""
import itertools
import linecache
import shutil
import textwrap
import unittest

from isomorph import Simulator, signal

_counter = itertools.count()
HAVE_VERILATOR = shutil.which('verilator') is not None


def load (src):
    name = f'<sg{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    ns = {}
    exec(compile(src, name, 'exec'), ns)
    return ns


MUL = textwrap.dedent('''\
    from isomorph import *
    @block
    def mul (i_clock, i_a, i_b, o_q):
        p = signal(32)

        @always_comb
        def mul_comb ():
            p.next = (i_a.signed() * i_b.signed())[31:0]

        @always_ff (i_clock.posedge)
        def mul_logic ():
            o_q.next = p
        return instances()
''')

SHIFT = textwrap.dedent('''\
    from isomorph import *
    @block
    def shifter (i_clock, i_a, i_n, o_arith, o_logic):
        @always_comb
        def shift_comb ():
            o_arith.next = (i_a.signed() >> i_n)[15:0]
            o_logic.next = (i_a >> i_n)[15:0]
        return instances()
''')


def truncate (value, width):
    return value & ((1 << width) - 1)


def signed_of (raw, width):
    raw = truncate(raw, width)
    if raw >> (width - 1):
        return raw - (1 << width)
    return raw


class SignedMultiplyTests (unittest.TestCase):
    CASES = [(3, 5), (0xffff, 1), (0xffff, 0xffff), (0x8000, 2),
             (0xfffe, 3), (0x7fff, 0x7fff), (0x8000, 0x8000), (0, 0xffff)]

    def drive (self, backend):
        ns = load(MUL)
        top = ns['mul'](i_clock = signal(), i_a = signal(16),
                        i_b = signal(16), o_q = signal(32))
        sim = Simulator(top, backend = backend)
        sim.add_clock(10e-9)
        out = []
        for a, b in self.CASES:
            sim.set('i_a', a)
            sim.set('i_b', b)
            sim.tick(1)
            out.append(sim.get('o_q'))
        sim.close()
        return out

    def expected (self):
        return [truncate(signed_of(a, 16) * signed_of(b, 16), 32)
                for a, b in self.CASES]

    def test_python (self):
        self.assertEqual(self.drive('python'), self.expected())

    def test_c99 (self):
        self.assertEqual(self.drive('c99'), self.expected())

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_is_the_reference (self):
        reference = self.drive('verilator')
        self.assertEqual(reference, self.expected())
        self.assertEqual(self.drive('python'), reference)
        self.assertEqual(self.drive('c99'), reference)


class ArithmeticShiftTests (unittest.TestCase):
    CASES = [(0x8000, 1), (0x8000, 4), (0xffff, 3), (0x7fff, 2), (0x4000, 0)]

    def drive (self, backend):
        ns = load(SHIFT)
        top = ns['shifter'](i_clock = signal(), i_a = signal(16),
                            i_n = signal(4), o_arith = signal(16),
                            o_logic = signal(16))
        sim = Simulator(top, backend = backend)
        sim.add_clock(10e-9)
        out = []
        for a, n in self.CASES:
            sim.set('i_a', a)
            sim.set('i_n', n)
            sim.eval()
            out.append((sim.get('o_arith'), sim.get('o_logic')))
        sim.close()
        return out

    def expected (self):
        return [(truncate(signed_of(a, 16) >> n, 16), truncate(a >> n, 16))
                for a, n in self.CASES]

    def test_signed_shift_is_arithmetic (self):
        self.assertEqual(self.drive('python'), self.expected())
        self.assertEqual(self.drive('c99'), self.expected())

    def test_unsigned_shift_stays_logical (self):
        arith, logic = self.drive('python')[0]
        self.assertEqual(logic, 0x4000)
        self.assertEqual(arith, 0xc000)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_agrees (self):
        self.assertEqual(self.drive('verilator'), self.expected())


if __name__ == '__main__':
    unittest.main()
