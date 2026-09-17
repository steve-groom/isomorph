"""The library blocks.

Each block carries its own bench in its file, in the house shape:
elaborate_<name>() builds the ports and test_<name>(sim) drives them
with a protocol checker on every stream. This runs every one of those
benches on every backend this machine has, converts every block to
both languages and lints them, and holds two of them in lockstep. A
block whose bench passes here is one the fitter will be handed.
"""
import os
import random
import shutil
import sys
import tempfile
import unittest

from isomorph import Simulator, analyse, write_sv_files, write_vhdl_files
from isomorph.emit_sv import lint_sv
from isomorph.emit_vhdl import lint_vhdl
from isomorph.lib import (stream_pipe, stream_fifo, stream_fork,
    stream_join, ram_block, stream_to_memory, stream_from_memory)
from isomorph.lib.stream_pipe import (elaborate_stream_pipe,
    test_stream_pipe as bench_stream_pipe)
from isomorph.lib.stream_fifo import (elaborate_stream_fifo,
    test_stream_fifo as bench_stream_fifo)
from isomorph.lib.stream_fork import (elaborate_stream_fork,
    test_stream_fork as bench_stream_fork)
from isomorph.lib.stream_join import (elaborate_stream_join,
    test_stream_join as bench_stream_join)
from isomorph.lib.ram_block import (elaborate_ram_block,
    test_ram_block as bench_ram_block)
from isomorph.lib.stream_to_memory import (
    elaborate_stream_to_memory,
    test_stream_to_memory as bench_stream_to_memory)
from isomorph.lib.stream_from_memory import (
    elaborate_stream_from_memory,
    test_stream_from_memory as bench_stream_from_memory)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lockstep import Lockstep, available          # noqa: E402

HAVE_GCC = shutil.which('gcc') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)
HAVE_GHDL = shutil.which('ghdl') is not None

BLOCKS = [
    ('stream_pipe', elaborate_stream_pipe, bench_stream_pipe),
    ('stream_fifo', elaborate_stream_fifo, bench_stream_fifo),
    ('stream_fork', elaborate_stream_fork, bench_stream_fork),
    ('stream_join', elaborate_stream_join, bench_stream_join),
    ('ram_block', elaborate_ram_block, bench_ram_block),
    ('stream_to_memory', elaborate_stream_to_memory, bench_stream_to_memory),
    ('stream_from_memory', elaborate_stream_from_memory,
     bench_stream_from_memory),
]


def run_bench (elaborate, bench, backend):
    sim = Simulator(elaborate(), backend = backend)
    try:
        bench(sim)
    finally:
        sim.close()


class BenchTests (unittest.TestCase):
    """Every block's own bench, on every backend."""

    def test_python (self):
        for name, elaborate, bench in BLOCKS:
            with self.subTest(block = name):
                run_bench(elaborate, bench, 'python')

    @unittest.skipUnless(HAVE_GCC, 'gcc not installed')
    def test_c99 (self):
        for name, elaborate, bench in BLOCKS:
            with self.subTest(block = name):
                run_bench(elaborate, bench, 'c99')

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator (self):
        for name, elaborate, bench in BLOCKS:
            with self.subTest(block = name):
                run_bench(elaborate, bench, 'verilator')


class ConvertTests (unittest.TestCase):
    """Every block converts to both languages without a warning about
    itself, and lints where the linters are installed."""

    def test_converts_clean (self):
        for name, elaborate, _ in BLOCKS:
            with self.subTest(block = name):
                modules, warnings = analyse(elaborate())
                self.assertEqual(warnings, [], name)
                self.assertEqual(modules[-1].name, name)

    @unittest.skipUnless(HAVE_VERILATOR, 'verilator not installed')
    def test_verilator_lint (self):
        for name, elaborate, _ in BLOCKS:
            with self.subTest(block = name):
                modules, _ = analyse(elaborate())
                directory = tempfile.mkdtemp(prefix = 'iso_lib_sv_')
                try:
                    written = write_sv_files(modules, directory)
                    lint_sv([p for p in written if p.endswith('.sv')],
                            top = name)
                finally:
                    shutil.rmtree(directory, ignore_errors = True)

    @unittest.skipUnless(HAVE_GHDL, 'ghdl not installed')
    def test_ghdl_lint (self):
        for name, elaborate, _ in BLOCKS:
            with self.subTest(block = name):
                modules, _ = analyse(elaborate())
                directory = tempfile.mkdtemp(prefix = 'iso_lib_vhd_')
                try:
                    written = write_vhdl_files(modules, directory)
                    lint_vhdl([p for p in written if p.endswith('.vhd')])
                finally:
                    shutil.rmtree(directory, ignore_errors = True)


class LockstepTests (unittest.TestCase):
    """The FIFO and the memory reader, every port and flip-flop of the
    top compared across backends every cycle under random stalls."""

    def setUp (self):
        self.backends = available()
        if len(self.backends) < 2:
            self.skipTest('needs at least two backends')

    def test_fifo_agrees_every_cycle (self):
        step = Lockstep(elaborate_stream_fifo, self.backends)
        try:
            step.reset('i_reset')
            rng = random.Random(20260912)
            for _ in range(300):
                step.set('i_stream_valid', rng.randint(0, 1))
                step.set('i_stream_data', rng.randint(0, 255))
                step.set('o_stream_ready', rng.randint(0, 1))
                step.eval()
                step.tick(1)
        finally:
            step.close()

    def test_reader_agrees_every_cycle (self):
        """The read port is answered by the bench with the one-cycle
        latency, the same on every backend."""
        step = Lockstep(elaborate_stream_from_memory, self.backends)
        try:
            step.reset('i_reset')
            rng = random.Random(20260913)
            step.set('i_count', 12)
            addr = 0
            for cycle in range(200):
                step.set('rd_data', (addr * 37 + 11) & 0xff)
                step.set('i_start', 1 if cycle in (2, 90) else 0)
                step.set('o_stream_ready', rng.randint(0, 1))
                step.eval()
                addr = step.get('rd_addr')
                step.tick(1)
        finally:
            step.close()


if __name__ == '__main__':
    unittest.main()
