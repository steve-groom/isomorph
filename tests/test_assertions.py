"""A Python assert in a process, in all four outputs.

The invariant travels: it becomes an immediate assertion in the
SystemVerilog and the VHDL, a real check in the C99, and a raise in the
Python interpreter. The message travels with it, which is the whole
point of writing one, and Verilator is built with --assert so the one
backend that reads the emitted SystemVerilog actually checks them.
"""
import itertools
import linecache
import shutil
import textwrap
import unittest

from isomorph import (analyse, emit_sv, emit_vhdl, signal, Simulator,
                      SimError, ConversionError)

_counter = itertools.count()
HAVE_GCC = shutil.which('gcc') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)

DESIGN = textwrap.dedent('''\
    from isomorph import (block, signal, always_ff, always_comb,
                          instances, main)


    @block
    def invariant_block (i_clock, i_d, o_q):
        seen = signal(8)

        @always_ff (i_clock.posedge)
        def guard_logic ():
            assert i_d != 0xff, 'i_d is never all ones'
            seen.next = i_d

        @always_comb
        def guard_comb ():
            o_q.next = seen

        return instances()
''')


def load (src):
    name = f'<assertion{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    namespace = {}
    exec(compile(src, name, 'exec'), namespace)
    return namespace


def build (backend = 'python'):
    ns = load(DESIGN)
    top = ns['invariant_block'](i_clock = signal(), i_d = signal(8),
                        o_q = signal(8))
    sim = Simulator(top, backend = backend)
    sim.add_clock(20e-9)
    return sim


def elaborated ():
    ns = load(DESIGN)
    return ns['invariant_block'](i_clock = signal(), i_d = signal(8),
                         o_q = signal(8))


class MessageTravelsTests (unittest.TestCase):
    def test_systemverilog_carries_the_message (self):
        text = emit_sv(analyse(elaborated())[0])
        self.assertIn('assert (', text)
        self.assertIn('$error("i_d is never all ones")', text)
        self.assertIn('`ifndef SYNTHESIS', text)

    def test_vhdl_carries_the_message (self):
        text = emit_vhdl(analyse(elaborated())[0])
        self.assertIn('report "i_d is never all ones"', text)
        self.assertIn('severity error', text)
        self.assertIn('translate_off', text)

    def test_a_message_that_cannot_be_a_literal_is_refused (self):
        src = DESIGN.replace("'i_d is never all ones'", 'f"{i_d} is bad"')
        ns = load(src)
        with self.assertRaisesRegex(ConversionError, 'plain string'):
            analyse(ns['invariant_block'](i_clock = signal(), i_d = signal(8),
                                  o_q = signal(8)))


class ItActuallyFiresTests (unittest.TestCase):
    """Holding the invariant passes; breaking it stops the run."""

    def run_backend (self, backend, value):
        sim = build(backend)
        sim.set('i_d', value)
        sim.tick(1)
        return sim.get('o_q')

    def test_python_holds_then_fires (self):
        self.assertEqual(self.run_backend('python', 0x12), 0x12)
        with self.assertRaisesRegex(SimError, 'never all ones'):
            self.run_backend('python', 0xff)

    @unittest.skipUnless(HAVE_GCC, 'gcc not installed')
    def test_c99_holds_then_fires (self):
        self.assertEqual(self.run_backend('c99', 0x12), 0x12)
        with self.assertRaisesRegex(SimError, 'never all ones'):
            self.run_backend('c99', 0xff)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_holds_then_fires (self):
        self.assertEqual(self.run_backend('verilator', 0x12), 0x12)
        # verilator ends the process on a failed assertion by default,
        # which would take the whole bench with it; the shim asks for a
        # recorded error instead, so it arrives here
        with self.assertRaisesRegex(SimError, 'assertion'):
            self.run_backend('verilator', 0xff)


if __name__ == '__main__':
    unittest.main()
