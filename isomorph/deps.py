"""External tools isomorph uses, and whether this machine has them.

Installing compilers is the machine owner's decision and needs root,
so the report prints the exact command for the platform it detects
and stops there. --install runs that command, and asks first unless
told not to.

    python3 -m isomorph doctor
    python3 -m isomorph doctor --install
"""
import os
import platform
import re
import shutil
import subprocess
import sys

MIN_PYTHON = (3, 11)


class Tool:
    def __init__ (self, name, command, args, enables, required = False,
                  packages = None, versioned = True):
        self.name = name
        self.command = command
        self.args = args
        self.enables = enables
        self.required = required
        self.packages = packages or {}
        # a plugin has no version of its own: the command is the host
        # tool and its banner is not the answer to the question asked
        self.versioned = versioned


TOOLS = [
    Tool('gcc', 'gcc', ['--version'],
         'C99 simulator backend, compiling emitted C99',
         packages = {'debian': 'build-essential', 'fedora': 'gcc',
                     'arch': 'base-devel',
                     'darwin': 'xcode-select --install'}),
    Tool('g++', 'g++', ['--version'],
         'Verilator simulator backend',
         packages = {'debian': 'build-essential', 'fedora': 'gcc-c++',
                     'arch': 'base-devel',
                     'darwin': 'xcode-select --install'}),
    Tool('make', 'make', ['--version'],
         'building the Verilator model',
         packages = {'debian': 'build-essential', 'fedora': 'make',
                     'arch': 'base-devel',
                     'darwin': 'xcode-select --install'}),
    Tool('verilator', 'verilator', ['--version'],
         '--lint of SystemVerilog, Verilator simulator backend',
         packages = {'debian': 'verilator', 'fedora': 'verilator',
                     'arch': 'verilator', 'darwin': 'verilator'}),
    Tool('ghdl', 'ghdl', ['--version'],
         '--lint of VHDL, and the VHDL equivalence check',
         packages = {'debian': 'ghdl', 'fedora': 'ghdl',
                     'arch': 'ghdl', 'darwin': 'ghdl'}),
    Tool('yosys', 'yosys', ['--version'],
         'proving the two emitted languages one netlist',
         packages = {'debian': 'yosys', 'fedora': 'yosys',
                     'arch': 'yosys', 'darwin': 'yosys'}),
    Tool('yosys ghdl plugin', 'yosys', ['-m', 'ghdl', '-p', 'help ghdl'],
         'reading the emitted VHDL into yosys, for that proof',
         versioned = False,
         packages = {'debian': 'yosys-plugin-ghdl',
                     'fedora': 'yosys-ghdl', 'arch': 'ghdl-yosys-plugin',
                     'darwin': 'oss-cad-suite'}),
]


def distro ():
    """debian, fedora, arch, darwin or None."""
    if sys.platform == 'darwin':
        return 'darwin'
    try:
        with open('/etc/os-release', encoding = 'utf-8') as f:
            fields = dict(
                line.rstrip('\n').split('=', 1)
                for line in f if '=' in line)
    except OSError:
        return None
    ident = fields.get('ID', '').strip('"').lower()
    like = fields.get('ID_LIKE', '').strip('"').lower().split()
    for candidate in [ident] + like:
        if candidate in ('debian', 'ubuntu'):
            return 'debian'
        if candidate in ('fedora', 'rhel', 'centos'):
            return 'fedora'
        if candidate in ('arch', 'archlinux', 'manjaro'):
            return 'arch'
    return None


def installer (system = None):
    """(command prefix, needs sudo) for the detected platform."""
    system = system or distro()
    return {
        'debian': (['apt-get', 'install', '-y'], True),
        'fedora': (['dnf', 'install', '-y'], True),
        'arch': (['pacman', '-S', '--needed', '--noconfirm'], True),
        'darwin': (['brew', 'install'], False),
    }.get(system, (None, False))


# a machine with no pip cannot install anything, isomorph included
PIP_PACKAGES = {'debian': 'python3-pip', 'fedora': 'python3-pip',
                'arch': 'python-pip'}


def pip_command (system = None):
    """How to get pip itself, on a machine that has none."""
    system = system or distro()
    package = PIP_PACKAGES.get(system)
    prefix, needs_sudo = installer(system)
    if package is None or prefix is None:
        return None
    words = (['sudo'] if needs_sudo else []) + prefix + [package]
    return ' '.join(words)


def version_of (tool):
    path = shutil.which(tool.command)
    if path is None:
        return None, None
    try:
        result = subprocess.run([tool.command] + tool.args,
                                capture_output = True, text = True,
                                timeout = 60)
    except (OSError, subprocess.SubprocessError):
        return path, ''
    if not tool.versioned:
        # the question is whether it loads, and the answer is the
        # exit code rather than anything in the banner
        return (path, 'installed') if result.returncode == 0 else (None, None)
    text = (result.stdout or result.stderr or '').strip().splitlines()
    if not text:
        return path, ''
    found = re.search(r'(\d+\.\d+(\.\d+)?)', text[0])
    return path, found.group(1) if found else text[0][:40]


def check ():
    """[{name, found, path, version, enables, required, package}]."""
    system = distro()
    rows = [{
        'name': 'python3',
        'found': sys.version_info[:2] >= MIN_PYTHON,
        'path': sys.executable,
        'version': platform.python_version(),
        'enables': 'everything',
        'required': True,
        'package': None,
    }]
    for tool in TOOLS:
        path, version = version_of(tool)
        rows.append({
            'name': tool.name,
            'found': path is not None,
            'path': path,
            'version': version,
            'enables': tool.enables,
            'required': tool.required,
            'package': tool.packages.get(system),
        })
    return rows


def missing_packages (rows = None, system = None):
    """Package names to install, deduplicated, in order."""
    rows = check() if rows is None else rows
    out = []
    for row in rows:
        if row['found'] or not row['package']:
            continue
        if row['package'] not in out:
            out.append(row['package'])
    return out


def install_command (rows = None, system = None):
    """The exact shell command, or None if nothing is missing or the
    platform is not one we know how to advise on."""
    system = system or distro()
    packages = missing_packages(rows, system)
    if not packages:
        return None
    prefix, needs_sudo = installer(system)
    if prefix is None:
        return None
    words = ([] if not needs_sudo else ['sudo']) + prefix + packages
    return ' '.join(words)


def report (rows = None):
    """(text, ok). ok is False only if something required is missing."""
    rows = check() if rows is None else rows
    width = max(len(r['name']) for r in rows)
    lines = ['isomorph external tools', '']
    ok = True
    for row in rows:
        if row['found']:
            mark = 'yes'
            detail = row['version'] or ''
        else:
            mark = 'NO '
            detail = 'not on PATH'
            if row['required']:
                ok = False
        lines.append(f"  {mark}  {row['name']:<{width}}  {detail:<12}"
                     f"  {row['enables']}")
    missing = [r for r in rows if not r['found']]
    lines.append('')
    if not missing:
        lines.append('  Everything isomorph can use is installed.')
    else:
        names = ', '.join(r['name'] for r in missing)
        lines.append(f'  Missing: {names}.')
        lines.append('  Isomorph still converts to SystemVerilog and VHDL, '
                     'and the')
        lines.append('  Python simulator still runs. The features listed '
                     'above are off.')
        command = install_command(rows)
        if command:
            lines.append('')
            lines.append('  To install them:')
            lines.append('')
            lines.append('    ' + command)
        else:
            lines.append('')
            lines.append('  Install them with this system\'s package '
                         'manager.')
    lines.append('')
    return '\n'.join(lines), ok


def install (rows = None, ask = True, stream = None):
    """Run the package manager for what is missing. An exit code.

    Nothing is installed without a yes, and a run with no terminal
    to ask at prints the command rather than guessing at consent.
    """
    stream = sys.stdout if stream is None else stream
    command = install_command(rows)
    if command is None:
        # the report above has already said so
        return 0
    if ask:
        if not sys.stdin.isatty():
            stream.write('\n  Nothing was installed: no terminal to ask '
                         'at. Run that\n  yourself, or pass --yes.\n')
            return 1
        stream.write('\n  Run it? [y/N] ')
        stream.flush()
        try:
            answer = input()
        except EOFError:
            answer = ''
        if answer.strip().lower() not in ('y', 'yes'):
            stream.write('  Nothing installed.\n')
            return 0
    stream.write(f'  running: {command}\n')
    stream.flush()
    code = subprocess.call(command, shell = True)
    if code == 0:
        stream.write('\n')
        stream.write(report()[0])
    return code


def main (argv = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    assume_yes = '--yes' in argv or '-y' in argv
    wanted = assume_yes or '--install' in argv
    rest = [a for a in argv if a not in ('--install', '--yes', '-y')]
    if rest:
        sys.stderr.write(f'doctor: unknown option {rest[0]}\n')
        return 2
    rows = check()
    text, ok = report(rows)
    sys.stdout.write(text)
    if not wanted:
        return 0 if ok else 1
    return install(rows, ask = not assume_yes) or (0 if ok else 1)
