"""install.sh --tar: the one file a colleague is given, and what has
to be inside it for `./install.sh` to work when they unpack it."""
import os
import pathlib
import subprocess
import tarfile
import tempfile
import tomllib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'install.sh'


def version ():
    with open(ROOT / 'pyproject.toml', 'rb') as f:
        return tomllib.load(f)['project']['version']


def archive ():
    """Build it once for the whole class."""
    out = tempfile.mkdtemp()
    result = subprocess.run(['sh', str(SCRIPT), '--tar', '--dest', out],
                            capture_output = True, text = True)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return pathlib.Path(out) / f'isomorph-{version()}.tar.gz'


class ArchiveTests (unittest.TestCase):
    @classmethod
    def setUpClass (cls):
        cls.path = archive()
        with tarfile.open(cls.path) as tar:
            cls.members = tar.getmembers()
        cls.names = [m.name for m in cls.members]

    def test_archive_is_written (self):
        self.assertTrue(self.path.is_file())

    def test_one_top_directory_named_by_version (self):
        top = {name.split('/')[0] for name in self.names}
        self.assertEqual(top, {f'isomorph-{version()}'})

    def test_holds_what_install_needs (self):
        top = f'isomorph-{version()}'
        for wanted in ('install.sh', 'INSTALL.txt', 'pyproject.toml',
                       'README.md', 'LICENSE',
                       'isomorph/__init__.py', 'isomorph/deps.py'):
            self.assertIn(f'{top}/{wanted}', self.names)

    def test_holds_the_c99_runtime (self):
        runtime = [n for n in self.names if n.endswith('.c')
                   or n.endswith('.h')]
        self.assertTrue(runtime, 'the c99 backend has nothing to compile')

    def test_caches_are_left_out (self):
        for name in self.names:
            self.assertNotIn('__pycache__', name)
            self.assertNotIn('.egg-info', name)
            self.assertFalse(name.endswith('.pyc'), name)

    def test_their_install_script_is_executable (self):
        script = [m for m in self.members
                  if m.name.endswith('/install.sh')][0]
        self.assertTrue(script.mode & 0o111, oct(script.mode))


class BootstrapTests (unittest.TestCase):
    """curl | sh: no tree around the script, so it fetches one. The
    mirror is a file:// URL shaped like GitHub's archive path, and
    --pack stops it before anything is installed."""
    @classmethod
    def setUpClass (cls):
        cls.tmp = tempfile.mkdtemp()
        heads = pathlib.Path(cls.tmp) / 'mirror/archive/refs/heads'
        heads.mkdir(parents = True)
        stage = pathlib.Path(cls.tmp) / 'stage'
        stage.mkdir()
        with tarfile.open(archive()) as tar:
            tar.extractall(stage, filter = 'data')
        (stage / f'isomorph-{version()}').rename(stage / 'isomorph-main')
        with tarfile.open(heads / 'main.tar.gz', 'w:gz') as tar:
            tar.add(stage / 'isomorph-main', arcname = 'isomorph-main')
        cls.empty = pathlib.Path(cls.tmp) / 'empty'
        cls.empty.mkdir()

    def run_piped (self, *args):
        env = dict(os.environ,
                   ISOMORPH_REPO = f'file://{self.tmp}/mirror')
        with open(SCRIPT) as script:
            return subprocess.run(['sh', '-s', '--', *args],
                                  cwd = self.empty, stdin = script,
                                  env = env, capture_output = True,
                                  text = True)

    def test_it_fetches_and_does_the_work (self):
        out = pathlib.Path(self.tmp) / 'packed'
        result = self.run_piped('--pack', '--dest', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('fetching', result.stdout)
        self.assertTrue((out / 'isomorph/__init__.py').is_file())

    def test_it_leaves_nothing_behind (self):
        self.run_piped('--pack', '--dest', f'{self.tmp}/packed2')
        self.assertEqual(list(self.empty.iterdir()), [])


class UsageTests (unittest.TestCase):
    def test_help_names_the_two_things_myhdl_does_not_do (self):
        result = subprocess.run(['sh', str(SCRIPT), '--help'],
                                capture_output = True, text = True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--tar', result.stdout)
        self.assertIn('--deps', result.stdout)

    def test_help_names_where_it_can_be_put (self):
        result = subprocess.run(['sh', str(SCRIPT), '--help'],
                                capture_output = True, text = True)
        self.assertIn('--target', result.stdout)
        self.assertIn('MyHDL', result.stdout)

    def test_target_and_editable_are_refused_together (self):
        result = subprocess.run(
            ['sh', str(SCRIPT), '--target', '/tmp/x', '--editable'],
            capture_output = True, text = True)
        self.assertEqual(result.returncode, 2)

    def test_an_unknown_option_is_refused (self):
        result = subprocess.run(['sh', str(SCRIPT), '--nonsense'],
                                capture_output = True, text = True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
