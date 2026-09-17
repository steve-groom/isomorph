"""A memory image reaches all four outputs (preload()).

signal() used to take a power-on value that two simulators honoured and
no emitted HDL did, and it is gone. preload() is the opposite: it is
what an FPGA is configured with, so it becomes a declaration
initialiser in the SystemVerilog and the VHDL, which is how every
vendor tool loads a block RAM, and the two smoke backends preload the
same values. All four agree, which is the only reason it exists.

It is for a memory: a boot ROM, a program image, a lookup table. A
flip-flop that has to come up somewhere still says so in a reset
branch, because a flip-flop is reset rather than configured.
"""
import itertools
import linecache
import shutil
import textwrap
import unittest

from isomorph import (analyse, emit_sv, emit_vhdl, signal, signals,
                      preload, block, always_ff, instances, Simulator,
                      IsomorphError)

_counter = itertools.count()
HAVE_GCC = shutil.which('gcc') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)

VALUES = [0x1000 + (n * 7) for n in range(8)]

DESIGN = textwrap.dedent('''\
    from isomorph import (block, signal, signals, preload, always_ff,
                          instances)

    VALUES = %r


    @block
    def image (i_clock, i_addr, o_data):
        cells = signals(8, 16)
        preload(cells, VALUES)

        @always_ff (i_clock.posedge)
        def image_logic ():
            o_data.next = cells[i_addr]

        return instances()
''') % (VALUES,)


def load (src):
    name = f'<preload{next(_counter)}>'
    linecache.cache[name] = (len(src), None, src.splitlines(True), name)
    namespace = {}
    exec(compile(src, name, 'exec'), namespace)
    return namespace


def build ():
    ns = load(DESIGN)
    return ns['image'](i_clock = signal(), i_addr = signal(3),
                       o_data = signal(16))


def read (backend, address):
    sim = Simulator(build(), backend = backend)
    sim.add_clock(20e-9)
    sim.set('i_addr', address)
    sim.tick(1)
    return sim.get('o_data')


class ItReachesTheHdlTests (unittest.TestCase):
    def test_systemverilog_declares_the_contents (self):
        text = emit_sv(analyse(build())[0])
        self.assertIn("cells [8] = '{", text)
        self.assertIn("16'd4096", text)
        self.assertNotIn('initial', text)

    def test_vhdl_declares_the_contents (self):
        text = emit_vhdl(analyse(build())[0])
        # the array type is named for its shape, so two arrays of
        # the same shape are one type and can be connected
        self.assertIn('signal cells : iso_slv16_array8_t := (', text)
        # the same word SystemVerilog writes 16'd4096, in the sized
        # decimal bit string VHDL-2008 has. It was "0001000000000000",
        # which is no help to anyone reading a memory image
        self.assertIn('16d"4096"', text)
        self.assertNotIn('"0001000000000000"', text)


class EveryBackendAgreesTests (unittest.TestCase):
    def test_python (self):
        for address, value in enumerate(VALUES):
            self.assertEqual(read('python', address), value)

    @unittest.skipUnless(HAVE_GCC, 'gcc not installed')
    def test_c99 (self):
        for address, value in enumerate(VALUES):
            self.assertEqual(read('c99', address), value)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator (self):
        for address, value in enumerate(VALUES):
            self.assertEqual(read('verilator', address), value)


class ItRefusesNonsenseTests (unittest.TestCase):
    def test_the_image_must_be_the_whole_memory (self):
        cells = signals(8, 16)
        with self.assertRaisesRegex(IsomorphError, 'whole memory'):
            preload(cells, [1, 2, 3])

    def test_a_value_must_fit (self):
        cells = signals(4, 8)
        with self.assertRaisesRegex(IsomorphError, 'does not fit'):
            preload(cells, [0, 0, 256, 0])

    def test_it_takes_an_array_not_a_signal (self):
        with self.assertRaisesRegex(IsomorphError, 'signals\\(\\) array'):
            preload(signal(8), [1])


if __name__ == '__main__':
    unittest.main()


class ImageBaseTests (unittest.TestCase):
    """A program is read in hex and a sine table in decimal.

    Neither is readable as the other, and the author is the one who
    knows which, so preload() takes the base and both languages say
    the word the same way.
    """

    WORDS = [0x00000013, 0x00108093, 0xFEDCBA98, 0x00A00513]

    def build (self, base):
        @block
        def boot (i_clock, i_addr, o_data):
            rom = signals(4, 32)
            preload(rom, self.WORDS, base = base)

            @always_ff (i_clock.posedge)
            def boot_logic ():
                o_data.next = rom[i_addr]

            return instances()

        return boot(i_clock = signal(), i_addr = signal(2),
                    o_data = signal(32))

    def test_hex_in_both_languages (self):
        modules = analyse(self.build('hex'))[0]
        self.assertIn("32'h00108093", emit_sv(modules))
        self.assertIn('32x"00108093"', emit_vhdl(modules))

    def test_decimal_is_the_default (self):
        modules = analyse(self.build('dec'))[0]
        self.assertIn("32'd1081491", emit_sv(modules))
        self.assertIn('32d"1081491"', emit_vhdl(modules))

    def test_binary_when_the_bits_are_the_point (self):
        modules = analyse(self.build('bin'))[0]
        self.assertIn("32'b00000000000100001000000010010011",
                      emit_sv(modules))
        self.assertIn('"00000000000100001000000010010011"',
                      emit_vhdl(modules))

    def test_a_base_nobody_writes_is_refused (self):
        with self.assertRaisesRegex(IsomorphError, 'dec'):
            self.build('octal')
