"""main(): the command line every design file gets from one line."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from isomorph import IsomorphError
from isomorph.entry import BACKENDS, parse

DESIGN = textwrap.dedent('''\
    from isomorph import (block, signal, always_ff, instances, main)


    @block
    def dff (i_clock, i_d, o_q):
        @always_ff (i_clock.posedge)
        def ff ():
            o_q.next = i_d
        return instances()


    def elaborate_dff ():
        return dff(i_clock = signal(), i_d = signal(), o_q = signal())


    def test_dff (sim):
        sim.add_clock(10e-9)
        for value in (1, 0, 1, 1, 0):
            sim.set('i_d', value)
            sim.tick(1)
            assert sim.get('o_q') == value, 'dff did not follow i_d'


    if (__name__ == '__main__'):
        import sys
        sys.exit(main(elaborate_dff, test_dff))
''')


class ParseTests (unittest.TestCase):
    def test_no_arguments_runs (self):
        opts = parse([])
        self.assertTrue(opts.run)
        self.assertEqual(opts.backend, 'python')
        self.assertFalse(opts.writing)

    def test_backend_choice (self):
        for backend in BACKENDS:
            self.assertEqual(parse(['--run', backend]).backend, backend)

    def test_bad_backend_is_named (self):
        with self.assertRaises(IsomorphError) as caught:
            parse(['--run', 'icarus'])
        self.assertIn('verilator', str(caught.exception))

    def test_unknown_option_shows_usage (self):
        with self.assertRaises(IsomorphError) as caught:
            parse(['--nonsense'])
        self.assertIn('usage:', str(caught.exception))

    def test_writing_does_not_imply_running (self):
        opts = parse(['--sv'])
        self.assertTrue(opts.writing)
        self.assertFalse(opts.run)

    def test_write_and_run_together (self):
        opts = parse(['--sv', '--run', 'c99'])
        self.assertTrue(opts.writing)
        self.assertTrue(opts.run)
        self.assertEqual(opts.backend, 'c99')

    def test_paths_and_traces (self):
        opts = parse(['--sv', 'out.sv', '-o', 'gen', '--vcd', 'w.vcd',
                      '--traces', 'a,b'])
        self.assertEqual(opts.sv, 'out.sv')
        self.assertEqual(opts.outdir, 'gen')
        self.assertEqual(opts.vcd, 'w.vcd')
        self.assertEqual(opts.traces, ['a', 'b'])

    def test_flags_needing_a_value (self):
        for args in (['-o'], ['--traces']):
            with self.assertRaises(IsomorphError):
                parse(args)

    def test_vcd_takes_a_name_or_stands_alone (self):
        self.assertIs(parse(['--vcd']).vcd, True)
        self.assertEqual(parse(['--vcd', 'w.vcd']).vcd, 'w.vcd')


class DesignFileTests (unittest.TestCase):
    def setUp (self):
        self.dir = tempfile.mkdtemp(prefix = 'iso_entry_')
        self.path = os.path.join(self.dir, 'dff.py')
        with open(self.path, 'w') as f:
            f.write(DESIGN)

    def tearDown (self):
        shutil.rmtree(self.dir, ignore_errors = True)

    def run_design (self, *args):
        return subprocess.run([sys.executable, self.path, *args],
                              capture_output = True, text = True,
                              cwd = self.dir)

    def test_default_runs_the_test_bench (self):
        result = self.run_design()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('python backend', result.stdout)

    def test_help_exits_zero_and_lists_options (self):
        result = self.run_design('--help')
        self.assertEqual(result.returncode, 0)
        for flag in ('--run', '--sv', '--vhdl', '--c99', '--lint', '--vcd'):
            self.assertIn(flag, result.stdout)

    def test_unknown_option_exits_two (self):
        result = self.run_design('--nonsense')
        self.assertEqual(result.returncode, 2)
        self.assertIn('usage:', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_writing_does_not_run_the_bench (self):
        result = self.run_design('--sv')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('.sv', result.stdout)
        self.assertNotIn('backend', result.stdout)
        self.assertTrue(os.path.isfile(
            os.path.join(self.dir, 'build', 'dff', 'dff.sv')))

    def test_output_directory (self):
        result = self.run_design('--sv', '-o', 'gen')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(
            os.path.join(self.dir, 'gen', 'dff', 'dff.sv')))

    def test_vcd (self):
        result = self.run_design('--vcd', 'wave.vcd')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'wave.vcd')))

    def test_named_vcd_is_never_removed (self):
        for _ in range(3):
            self.run_design('--vcd', 'wave.vcd')
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'wave.vcd')))

    def test_bare_vcd_keeps_a_short_history (self):
        """A waveform is worth most next to the one before it, so a
        bare --vcd stamps the time and keeps the last two."""
        import time
        build = os.path.join(self.dir, 'build')
        for run in range(3):
            result = self.run_design('--vcd')
            self.assertEqual(result.returncode, 0, result.stderr)
            waves = sorted(f for f in os.listdir(build)
                           if f.endswith('.vcd'))
            self.assertEqual(len(waves), min(run + 1, 2),
                             f'after {run + 1} runs: {waves}')
            for name in waves:
                self.assertTrue(name.startswith('dff_'), name)
            time.sleep(1.05)        # the stamp is to the second

    def test_rotate_keeps_the_newest (self):
        from isomorph.entry import rotate_vcd
        build = os.path.join(self.dir, 'rot')
        made = []
        for _ in range(4):
            path = rotate_vcd(build, 'thing')
            with open(path, 'w') as handle:
                handle.write('$enddefinitions $end\n')
            made.append(path)
            os.utime(path, (len(made), len(made)))
        left = sorted(os.listdir(build))
        self.assertEqual(len(left), 2, left)
        self.assertIn(os.path.basename(made[-1]), left)

    def test_lint_leaves_no_droppings (self):
        result = self.run_design('--sv', '--vhdl', '--lint')
        self.assertEqual(result.returncode, 0, result.stderr)
        left = sorted(os.listdir(os.path.join(self.dir, 'build',
                                               'dff')))
        self.assertNotIn('dff.o', left)
        self.assertEqual([f for f in left if f.endswith('.cf')], [])

    def test_c99_output_is_self_contained (self):
        result = self.run_design('--c99')
        self.assertEqual(result.returncode, 0, result.stderr)
        build = os.path.join(self.dir, 'build')
        for name in ('iso_vcd.h', 'iso_vcd.c', 'iso_log.h', 'iso_log.c'):
            self.assertTrue(os.path.isfile(os.path.join(build, name)), name)
        if shutil.which('gcc'):
            compiled = subprocess.run(
                ['gcc', '-std=c99', '-Wall', '-Werror', '-c',
                 'dff.c', 'dff_vcd.c', 'iso_vcd.c', 'iso_log.c'],
                cwd = build, capture_output = True, text = True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)

    @unittest.skipUnless(shutil.which('gcc'), 'gcc not installed')
    def test_run_on_c99 (self):
        result = self.run_design('--run', 'c99')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('c99 backend', result.stdout)

    @unittest.skipUnless(shutil.which('verilator'), 'verilator not installed')
    def test_run_on_verilator (self):
        result = self.run_design('--run', 'verilator')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verilator backend', result.stdout)


if __name__ == '__main__':
    unittest.main()
