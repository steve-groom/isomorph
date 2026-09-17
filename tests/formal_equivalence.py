"""The two emitted languages proved equivalent, not just simulated.

This was written off as impossible here
until the ghdl plugin for yosys was installed: with it, yosys reads the
emitted VHDL and the emitted SystemVerilog into one session and
`equiv_simple` proves them the same netlist. That is stronger than the
cycle comparison in `vhdl_lockstep.py`, which only covers the stimulus
it was given, because it covers every input including the don't-cares.

The one piece of local knowledge is where GHDL keeps its compiled
libraries. The Ubuntu plugin is built for the gcc backend and the
installed GHDL is llvm, so the prefix has to be found rather than
assumed; find_prefix does that by looking for the directory that has
ieee/v08 under it.
"""
import glob
import os
import shutil
import subprocess
import tempfile

from isomorph import analyse, write_sv_files, write_vhdl_files

HAVE_YOSYS = shutil.which('yosys') is not None


def find_prefix ():
    """Where GHDL's compiled libraries are, or None.

    The plugin wants a prefix that has ieee/v08 directly under it,
    which is not the prefix ghdl itself reports.
    """
    for candidate in sorted(glob.glob('/usr/lib/ghdl/*/vhdl')
                            + glob.glob('/usr/lib/ghdl/*')
                            + glob.glob('/usr/local/lib/ghdl/*/vhdl')):
        if os.path.isdir(os.path.join(candidate, 'ieee', 'v08')):
            return candidate
    return None


PREFIX = find_prefix()
HAVE_PLUGIN = False
if HAVE_YOSYS and PREFIX:
    probe = subprocess.run(
        ['yosys', '-m', 'ghdl', '-p', 'help ghdl'],
        capture_output = True, text = True,
        env = dict(os.environ, GHDL_PREFIX = PREFIX))
    HAVE_PLUGIN = probe.returncode == 0


def script (sv_files, vhdl_files, top, flatten = True):
    """The yosys script, with the two escalations it needs.

    memory_map turns an inferred RAM into flops on both sides.
    Without it yosys 0.33 hits an internal assertion when two designs
    bring in a memory of the same name, which every one of ours does
    because the names match by construction.

    equiv_simple proves what it can see locally; equiv_induct proves
    the rest by temporal induction, which is what a design with state
    needs. Running only the first leaves a sequential design looking
    unproven when it is merely undecided.
    """
    prep = (f'prep -top {top}' + (' -flatten' if flatten else '')
            + '; memory_map; opt -full')
    lines = []
    for path in sv_files:
        lines.append(f'read_verilog -sv {path}')
    lines += [prep, 'design -stash gold', '']
    lines.append('ghdl --std=08 ' + ' '.join(vhdl_files) + f' -e {top}')
    lines += [prep, 'design -stash gate', '']
    lines += [
        f'design -copy-from gold -as gold {top}',
        f'design -copy-from gate -as gate {top}',
        'equiv_make gold gate equiv',
        'prep -top equiv',
        'equiv_simple -seq 5',
        'equiv_induct -seq 5',
        'equiv_status -assert',
    ]
    return '\n'.join(lines) + '\n'


def unprovable (modules):
    """Why this design cannot go through yosys, or None.

    Two limits, neither of them isomorph's. GHDL's synthesis front end
    ignores a don't-care choice in a matching case: `ghdl --synth` on
    a design with `when "1---"` says 'choice with meta-value is
    ignored' and drops the arm, while `ghdl -a` accepts the same file
    and the simulator runs it correctly. So a design with a match on
    bits() cannot be compared this way, and more to the point a yosys
    flow must not synthesise its VHDL. Vendor tools take case? as
    written; this is measured against GHDL 4.1.0.

    And yosys 0.33's Verilog front end does not parse an unpacked
    array port, which is what an array of signals emits.
    """
    for m in modules:
        for p in m.ports:
            if p.array:
                return ('yosys does not parse an unpacked array port, '
                        f'and {m.name}.{p.name} is one')
    return None


def uses_matching_case (text):
    return 'case?' in text


def prove (elaborate, top = None, flatten = True):
    """Emit both languages and prove them one netlist. Returns the log."""
    modules, _ = analyse(elaborate())
    why = unprovable(modules)
    if why:
        raise NotImplementedError(why)
    top = top or modules[-1].name
    directory = tempfile.mkdtemp(prefix = 'iso_formal_')
    try:
        sv = [p for p in write_sv_files(modules, directory, listing = False)
              if p.endswith('.sv')]
        vhdl = [p for p in
                write_vhdl_files(modules, directory, listing = False)
                if p.endswith('.vhd')]
        for path in vhdl:
            with open(path) as handle:
                if uses_matching_case(handle.read()):
                    raise NotImplementedError(
                        "GHDL's synthesis front end ignores a don't-care "
                        'choice in a matching case, so it would compare '
                        'a netlist with the arms dropped. The emitted '
                        'VHDL is correct and ghdl -a accepts it; see '
                        'unprovable().')
        path = os.path.join(directory, 'equiv.ys')
        with open(path, 'w', encoding = 'ascii') as handle:
            handle.write(script(sv, vhdl, top, flatten))
        done = subprocess.run(
            ['yosys', '-m', 'ghdl', '-s', path],
            capture_output = True, text = True, cwd = directory,
            env = dict(os.environ, GHDL_PREFIX = PREFIX or ''))
        log = (done.stdout or '') + (done.stderr or '')
        if done.returncode != 0:
            raise AssertionError(
                f'yosys could not prove {top} equivalent:\n'
                + '\n'.join(log.splitlines()[-25:]))
        return log
    finally:
        shutil.rmtree(directory, ignore_errors = True)
