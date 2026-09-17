"""A small stack processor, wired to a standard interface and run.

Most of the suite drives blocks written for the test that reaches
them, which keeps a failure short to read but means nothing here is
bigger than the feature it checks. sbc4_core is the exception: a
four-bit stack machine of a few hundred lines, with a state machine, a
register file, a decode, an adder and an Avalon master, migrated by
hand from MyHDL. It is useful precisely because nobody wrote it to
suit a test.

What it is for:

  - the interfaces. It takes avalon_mm() from isomorph.ifaces as a
    real master would, so the pins and the handshake are exercised
    rather than asserted about.
  - the emitters. A few hundred lines of one design through both
    languages catches what a six-line block cannot, and ghdl and
    Verilator are made to agree that the result compiles.
  - the simulator. The core runs a program out of a memory model here,
    so the answer is the processor's own, not a signal poked from
    outside.

The processor is documented in sbc4_core.py; the short of it is a
word of eight four-bit opcodes, fetched and then executed a slot at a
time, over a stack whose top two entries are registers.
"""
import os
import tempfile
import unittest

from isomorph import (analyse, assign, block, emit_sv, emit_vhdl,
                      instances, signal, Simulator)
from isomorph.emit_sv import lint_sv
from isomorph.emit_vhdl import lint_vhdl
from isomorph.ifaces import avalon_mm

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'cores'))
from sbc4_core import sbc4_core                        # noqa: E402


HAVE_GHDL = bool(__import__('shutil').which('ghdl'))
HAVE_VERILATOR = bool(__import__('shutil').which('verilator'))

WIDTHA = 12                 # 4K words of program and stack
WIDTHD = 32                 # eight opcodes to a word

RESET_ADDRESS = 0x10        # both are fixed inside the core
STACK_ADDRESS = 0x3ff


@block
def sbc4_board (i_clock, i_clock_sreset, i_irq, avmm):
    """The core behind a whole Avalon master port.

    avalon_mm() gives a 32-bit port a byteenable, because a slave has
    to be told which lanes of a word a write means. This processor has
    no byte addressing at all - a word is its unit - so every write is
    all four lanes and the tie is the honest answer rather than a
    signal the core has to carry around.

    Writing this wrapper is also the point: a core keeps whatever port
    bundle suits it, and meeting the standard interface is a wiring
    job at the level above, which is where a board does it.
    """
    assign(avmm.byteenable, lambda: 0xF)

    inst_core = sbc4_core (
        i_clock = i_clock,
        i_clock_sreset = i_clock_sreset,

        i_irq = i_irq,
        avmm = avmm
    )

    return instances()


def build ():
    return sbc4_board (
        i_clock = signal(),
        i_clock_sreset = signal(),
        i_irq = signal(),
        avmm = avalon_mm(WIDTHA = WIDTHA, WIDTHD = WIDTHD,
                         pipelined = False, lock = False)
    )


def opcodes (*slots):
    """A fetched word, first opcode in the lowest nibble.

    The core takes slot 0 from bits 3:0 and works upward, so a program
    reads left to right here in the order it executes.
    """
    word = 0
    for index, code in enumerate(slots):
        word |= (code & 0xF) << (4 * index)
    return word


# the opcode map, from the table at the top of sbc4_core.py
PFX = 0x0                   # 0nnn, so PFX | n
LDW, STW, DUP, DRP = 0x8, 0x9, 0xA, 0xB
SWP, ADC, AXL, JNZ = 0xC, 0xD, 0xE, 0xF


class Memory:
    """A word-addressed memory that answers the core's bus.

    No wait states and no pipeline: readdata is there in the cycle
    waitrequest is low, which is the simplest thing the specification
    allows and all this core asks for. Every write is recorded so a
    test can say what the program did rather than reaching inside the
    processor to look at its stack.
    """

    def __init__ (self, program = None):
        self.words = dict(program or {})
        self.writes = []

    def step (self, sim):
        """One cycle of the slave, driven before the clock edge."""
        sim.set('avmm_waitrequest', 0)
        address = sim.get('avmm_address')
        if sim.get('avmm_write'):
            data = sim.get('avmm_writedata')
            self.words[address] = data
            self.writes.append((address, data))
        sim.set('avmm_readdata', self.words.get(address, 0))

    def run (self, sim, cycles):
        for _ in range(cycles):
            self.step(sim)
            sim.tick()


class InterfaceTests (unittest.TestCase):
    """The port the core presents, as avalon_mm() defines it."""

    def setUp (self):
        self.modules, self.warnings = analyse(build())
        self.top = self.modules[-1]
        self.ports = {p.name: p for p in self.top.ports}

    def test_the_master_has_the_pins_the_interface_asks_for (self):
        """Not a list invented here: these are the members avalon_mm()
        makes for a non-pipelined master, and the point of the test is
        that the core drives all of them."""
        for name in ('avmm_address', 'avmm_read', 'avmm_readdata',
                     'avmm_write', 'avmm_writedata', 'avmm_byteenable',
                     'avmm_waitrequest'):
            self.assertIn(name, self.ports, name)

    def test_a_non_pipelined_master_has_no_readdatavalid (self):
        """pipelined = False, so the data is there when waitrequest
        drops and there is no later valid to wait for. A port that
        carried one would be a different handshake wearing the same
        name."""
        self.assertNotIn('avmm_readdatavalid', self.ports)

    def test_the_directions_are_the_masters_way_round (self):
        """A master drives the command and the slave answers, so
        readdata and waitrequest come in and the rest go out."""
        self.assertEqual(self.ports['avmm_address'].direction, 'out')
        self.assertEqual(self.ports['avmm_read'].direction, 'out')
        self.assertEqual(self.ports['avmm_write'].direction, 'out')
        self.assertEqual(self.ports['avmm_readdata'].direction, 'in')
        self.assertEqual(self.ports['avmm_waitrequest'].direction, 'in')

    def test_the_widths_follow_what_was_asked_for (self):
        self.assertEqual(self.ports['avmm_address'].width, WIDTHA)
        self.assertEqual(self.ports['avmm_writedata'].width, WIDTHD)
        self.assertEqual(self.ports['avmm_byteenable'].width, WIDTHD // 8)

    def test_it_analyses_without_a_severe_warning (self):
        """A latch or a combinational loop stops the build, so reaching
        here at all says neither is present. This says the rest is
        quiet too, which for a design nobody wrote to suit us is worth
        knowing."""
        severe = [w for w in self.warnings if 'severe' in w]
        self.assertEqual(severe, [])


class ConversionTests (unittest.TestCase):
    """Both languages, on a design large enough to mean something."""

    def setUp (self):
        self.modules, _ = analyse(build())

    def test_the_core_keeps_its_own_module (self):
        """Nothing is flattened: the wrapper instantiates the core and
        both are in the output under the names they were given."""
        names = [m.name for m in self.modules]
        self.assertIn('sbc4_core', names)
        self.assertIn('sbc4_board', names)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_accepts_the_systemverilog (self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, 'sbc4_board.sv')
            with open(path, 'w') as f:
                f.write(emit_sv(self.modules))
            lint_sv(path, top = 'sbc4_board')

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_accepts_the_vhdl (self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, 'sbc4_board.vhd')
            with open(path, 'w') as f:
                f.write(emit_vhdl(self.modules))
            lint_vhdl(path)


BACKENDS = ['python', 'c99']
if HAVE_VERILATOR:
    BACKENDS.append('verilator')


class ExecutionTests (unittest.TestCase):
    """The processor running, out of a memory that answers its bus.

    Every one of these runs on each backend in turn. A processor is
    the case where the three are most likely to disagree: it is all
    state, the state feeds its own next value, and a difference of one
    cycle anywhere shows up as a different program. Only the ports of
    the top are looked at, which is all Verilator can be asked for.
    """

    def start (self, program, backend = 'python'):
        sim = Simulator(build(), backend = backend)
        sim.add_clock(10e-9)
        mem = Memory(program)
        sim.set('i_irq', 0)
        sim.set('i_clock_sreset', 1)
        mem.run(sim, 2)
        sim.set('i_clock_sreset', 0)
        return sim, mem

    def reads (self, sim, mem, cycles):
        """Every address the core reads over these cycles."""
        seen = []
        for _ in range(cycles):
            mem.step(sim)
            if sim.get('avmm_read'):
                seen.append(sim.get('avmm_address'))
            sim.tick()
        return seen

    def test_it_fetches_from_the_reset_address (self):
        """The first thing any processor does. Nothing is loaded here,
        so the only question asked is where it looks."""
        for backend in BACKENDS:
            with self.subTest(backend = backend):
                sim, mem = self.start({}, backend)
                seen = self.reads(sim, mem, 8)
                sim.close()
                self.assertIn(RESET_ADDRESS, seen)

    def test_a_program_adds_and_stores_its_answer (self):
        """PFX 3, PFX 4 build a literal; DUP copies it; ADC adds the
        two; SWP and STW put the sum away.

        PFX shifts the running literal left by three and brings in the
        low bits, so 3 then 4 is (3 << 3) | 4 = 28, and 28 + 28 = 56.
        The store is what the test reads: a write is the one thing a
        processor does that is visible from outside it, and 56 is the
        answer arrived at by the design rather than asserted about it.
        """
        program = {
            RESET_ADDRESS: opcodes(PFX | 3, PFX | 4, DUP, ADC),
            RESET_ADDRESS + 1: opcodes(PFX | 1, PFX | 0, SWP, STW),
        }
        for backend in BACKENDS:
            with self.subTest(backend = backend):
                sim, mem = self.start(program, backend)
                mem.run(sim, 400)
                sim.close()
                self.assertIn(56, [data for _, data in mem.writes],
                              f'{backend}: 28 + 28 never reached memory')

    def test_it_asks_for_one_thing_at_a_time (self):
        """read and write are never both asserted. The specification
        does not say what a slave should do with that, which is
        precisely why a master must not do it."""
        program = {RESET_ADDRESS: opcodes(PFX | 1, DUP, ADC, DRP)}
        for backend in BACKENDS:
            with self.subTest(backend = backend):
                sim, mem = self.start(program, backend)
                for _ in range(200):
                    mem.step(sim)
                    self.assertFalse(sim.get('avmm_read')
                                     and sim.get('avmm_write'))
                    sim.tick()
                sim.close()

    def test_reset_puts_it_back (self):
        """Held in reset it drives no command at all, and released it
        goes back to the reset address rather than wherever it had got
        to."""
        program = {RESET_ADDRESS: opcodes(PFX | 1, DUP, ADC, DRP)}
        for backend in BACKENDS:
            with self.subTest(backend = backend):
                sim, mem = self.start(program, backend)
                mem.run(sim, 40)
                sim.set('i_clock_sreset', 1)
                mem.run(sim, 4)
                self.assertFalse(sim.get('avmm_read'))
                self.assertFalse(sim.get('avmm_write'))
                sim.set('i_clock_sreset', 0)
                seen = self.reads(sim, mem, 8)
                sim.close()
                self.assertIn(RESET_ADDRESS, seen)


if __name__ == '__main__':
    unittest.main()
