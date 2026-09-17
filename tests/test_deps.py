"""python3 -m isomorph doctor: what this machine has, and the exact
command to get what it does not. Nothing is ever installed for you."""
import io
import subprocess
import sys
import unittest
from unittest import mock

from isomorph import deps


class CheckTests (unittest.TestCase):
    def test_rows_are_complete (self):
        rows = deps.check()
        keys = {'name', 'found', 'path', 'version', 'enables', 'required',
                'package'}
        for row in rows:
            self.assertEqual(keys, set(row), row['name'])

    def test_python_is_required_and_present (self):
        python = [r for r in deps.check() if r['name'] == 'python3'][0]
        self.assertTrue(python['required'])
        self.assertTrue(python['found'])

    def test_every_external_tool_is_optional (self):
        for row in deps.check():
            if row['name'] != 'python3':
                self.assertFalse(row['required'], row['name'])

    def test_distro_is_known_or_none (self):
        self.assertIn(deps.distro(),
                      ('debian', 'fedora', 'arch', 'darwin', None))


class AdviceTests (unittest.TestCase):
    def rows (self, missing):
        out = []
        for row in deps.check():
            row = dict(row)
            if row['name'] in missing:
                row['found'] = False
                row['path'] = None
            out.append(row)
        return out

    def test_command_lists_missing_packages_once (self):
        rows = self.rows({'gcc', 'g++', 'make'})
        command = deps.install_command(rows, system = 'debian')
        self.assertEqual(command, 'sudo apt-get install -y build-essential')

    def test_command_per_platform (self):
        rows = self.rows({'verilator'})
        self.assertEqual(deps.install_command(rows, system = 'fedora'),
                         'sudo dnf install -y verilator')
        self.assertEqual(deps.install_command(rows, system = 'arch'),
                         'sudo pacman -S --needed --noconfirm verilator')

    def test_nothing_missing_gives_no_command (self):
        rows = self.rows(set())
        for row in rows:
            row['found'] = True
        self.assertIsNone(deps.install_command(rows, system = 'debian'))

    def test_report_names_what_is_missing_and_stays_calm (self):
        text, ok = deps.report(self.rows({'ghdl', 'verilator'}))
        self.assertTrue(ok, 'no external tool is required')
        self.assertIn('ghdl', text)
        self.assertIn('verilator', text)
        self.assertIn('still converts', text)

    def test_missing_python_is_not_ok (self):
        rows = self.rows(set())
        for row in rows:
            if row['name'] == 'python3':
                row['found'] = False
        _, ok = deps.report(rows)
        self.assertFalse(ok)


def without (name):
    """The rows this machine has, with one tool taken away."""
    rows = deps.check()
    for row in rows:
        if row['name'] == name:
            row['found'] = False
            row['package'] = 'verilator'
    return rows


class PipCommandTests (unittest.TestCase):
    def test_every_platform_we_advise_on_can_get_pip (self):
        for system in ('debian', 'fedora', 'arch'):
            self.assertIn('pip', deps.pip_command(system))

    def test_an_unknown_platform_gets_no_advice (self):
        self.assertIsNone(deps.pip_command('plan9'))


class InstallTests (unittest.TestCase):
    def test_nothing_missing_installs_nothing (self):
        rows = [r for r in deps.check() if r['found']]
        out = io.StringIO()
        with mock.patch('subprocess.call') as call:
            self.assertEqual(deps.install(rows, stream = out), 0)
        call.assert_not_called()

    def test_no_terminal_to_ask_at_installs_nothing (self):
        out = io.StringIO()
        with mock.patch('sys.stdin.isatty', return_value = False), \
                mock.patch('subprocess.call') as call:
            code = deps.install(without('verilator'), stream = out)
        self.assertEqual(code, 1)
        call.assert_not_called()
        self.assertIn('Nothing was installed', out.getvalue())

    def test_no_at_the_prompt_installs_nothing (self):
        out = io.StringIO()
        with mock.patch('sys.stdin.isatty', return_value = True), \
                mock.patch('builtins.input', return_value = 'n'), \
                mock.patch('subprocess.call') as call:
            code = deps.install(without('verilator'), stream = out)
        self.assertEqual(code, 0)
        call.assert_not_called()

    def test_yes_at_the_prompt_runs_the_command (self):
        out = io.StringIO()
        with mock.patch('sys.stdin.isatty', return_value = True), \
                mock.patch('builtins.input', return_value = 'y'), \
                mock.patch('subprocess.call', return_value = 0) as call:
            code = deps.install(without('verilator'), stream = out)
        self.assertEqual(code, 0)
        call.assert_called_once()
        self.assertIn('verilator', call.call_args[0][0])

    def test_yes_up_front_never_asks (self):
        out = io.StringIO()
        with mock.patch('builtins.input') as asked, \
                mock.patch('subprocess.call', return_value = 0) as call:
            deps.install(without('verilator'), ask = False, stream = out)
        asked.assert_not_called()
        call.assert_called_once()

    def test_a_failed_install_is_the_exit_code (self):
        out = io.StringIO()
        with mock.patch('subprocess.call', return_value = 100):
            code = deps.install(without('verilator'), ask = False,
                                stream = out)
        self.assertEqual(code, 100)


class DoctorCommandTests (unittest.TestCase):
    def test_doctor_runs (self):
        result = subprocess.run([sys.executable, '-m', 'isomorph', 'doctor'],
                                capture_output = True, text = True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('python3', result.stdout)
        self.assertIn('verilator', result.stdout)

    def test_an_unknown_option_is_refused (self):
        result = subprocess.run(
            [sys.executable, '-m', 'isomorph', 'doctor', '--nonsense'],
            capture_output = True, text = True)
        self.assertEqual(result.returncode, 2)

    def test_usage_mentions_installing (self):
        result = subprocess.run([sys.executable, '-m', 'isomorph', '--help'],
                                capture_output = True, text = True)
        self.assertIn('--install', result.stdout)

    def test_usage_mentions_doctor (self):
        result = subprocess.run([sys.executable, '-m', 'isomorph', '--help'],
                                capture_output = True, text = True)
        self.assertIn('doctor', result.stdout)


if __name__ == '__main__':
    unittest.main()
