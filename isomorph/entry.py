"""main(): the standard __main__ for a design file.

A design file ends with

    if (__name__ == '__main__'):
        main(elaborate_thing, test_thing)

where elaborate_thing() returns an elaborated block and test_thing(sim)
drives it. That one line gives the file a command line: run the test
bench on any of the three simulators, write SystemVerilog, VHDL or C99,
lint what was written, dump the IR, or print help.
"""
import glob
import os
import sys
import time

from . import convert
from .elaborate import Elaborated
from .execute import SimError
from .sdc import VENDORS
from .signal import IsomorphError
from .sim import Simulator

BACKENDS = ('python', 'c99', 'verilator')

USAGE = """usage: python3 {prog} [options]

With no options the design's test bench runs on the Python simulator.

run
  --run [BACKEND]  run the test bench. BACKEND is one of
                     python     IR interpreter, prints and pdb work
                     c99        emitted C99 through gcc, fast. Every
                                value is a uint64_t, so a design with
                                a wider signal runs on the other two
                     verilator  emitted SystemVerilog, the reference
                   default python
  --vcd [FILE]     write a VCD of the run. Bare, it names the file
                   after the design and the time and keeps the last
                   two, so a run can be compared with the one before
                   it. With a file name, that file, and nothing is
                   removed
  --traces A,B     limit the VCD to these signals

write
  --sv [FILE]      SystemVerilog
  --vhdl [FILE]    VHDL-2008
  --c99 [FILE]     C99 sources (.c, .h, _vcd.c)
  --json [FILE]    the design as JSON, for a tool that wants to read it
                   rather than build it. Nothing in isomorph reads it
  --sdc VENDOR     timing constraints for quartus, vivado or efinity.
                   The dialects differ, so the vendor is named. It is
                   a supplement to whatever already defines the clocks
                   in your flow, and it carries the clock-domain
                   crossings, which need the design analysed to find
  --lint           check what was written with verilator and ghdl
  --dump           print the IR instead of writing
  --allow-severe   carry on with an inferred latch or a combinational
                   loop instead of stopping. Neither can be built, so
                   this is for a refactor in progress and never for a
                   design you intend to keep
  -o DIR           output directory, default build/

  -h, --help       this text
"""


def _usage (prog):
    return USAGE.format(prog = os.path.basename(prog))


class Options:
    def __init__ (self):
        self.run = False
        self.backend = 'python'
        self.vcd = None
        self.traces = None
        self.sv = None
        self.vhdl = None
        self.c99 = None
        self.json = None
        self.sdc = None
        self.lint = False
        self.dump = False
        self.allow_severe = False
        self.outdir = 'build'
        self.help = False

    @property
    def writing (self):
        return (self.sv is not None or self.vhdl is not None
                or self.c99 is not None or self.json is not None
                or self.sdc is not None or self.dump)


def parse (args, prog = 'design.py'):
    """Parse a design file's command line. Raises IsomorphError with
    the usage text on anything it does not recognise."""
    opts = Options()
    rest = list(args)

    def value (flag, allowed = None):
        """Optional value: the next word, if it is not another flag."""
        if rest and not rest[0].startswith('-'):
            if allowed is not None and rest[0] not in allowed:
                raise IsomorphError(
                    f'{flag}: expected one of {", ".join(allowed)}, '
                    f'not {rest[0]!r}\n\n' + _usage(prog))
            return rest.pop(0)
        return True

    while rest:
        arg = rest.pop(0)
        if arg in ('-h', '--help'):
            opts.help = True
        elif arg == '--run':
            opts.run = True
            picked = value('--run', BACKENDS)
            if picked is not True:
                opts.backend = picked
        elif arg in ('--python', '--c99sim', '--verilator'):
            opts.run = True
            opts.backend = {'--python': 'python', '--c99sim': 'c99',
                            '--verilator': 'verilator'}[arg]
        elif arg == '--sv':
            opts.sv = value('--sv')
        elif arg == '--vhdl':
            opts.vhdl = value('--vhdl')
        elif arg == '--c99':
            opts.c99 = value('--c99')
        elif arg == '--json':
            opts.json = value('--json')
        elif arg == '--sdc':
            if not rest or rest[0].startswith('-'):
                raise IsomorphError(
                    '--sdc names a vendor: '
                    + ', '.join(VENDORS) + '\n\n' + _usage(prog))
            picked = rest.pop(0)
            if picked not in VENDORS:
                raise IsomorphError(
                    f'--sdc: {picked!r} is not one of '
                    + ', '.join(VENDORS) + '\n\n' + _usage(prog))
            opts.sdc = picked
        elif arg == '--allow-severe':
            opts.allow_severe = True
        elif arg == '--lint':
            opts.lint = True
        elif arg == '--dump':
            opts.dump = True
        elif arg == '--vcd':
            opts.vcd = value('--vcd')
        elif arg == '--traces':
            if not rest:
                raise IsomorphError('--traces needs a signal list\n\n'
                                    + _usage(prog))
            opts.traces = [t for t in rest.pop(0).split(',') if t]
        elif arg == '-o':
            if not rest:
                raise IsomorphError('-o needs a directory\n\n'
                                    + _usage(prog))
            opts.outdir = rest.pop(0)
        else:
            raise IsomorphError(f'unknown option {arg!r}\n\n' + _usage(prog))

    if not opts.run and not opts.writing:
        opts.run = True
    return opts


KEEP_VCDS = 2


def rotate_vcd (outdir, name, keep = KEEP_VCDS):
    """A VCD named for the design and the time, keeping the last few.

    A waveform is worth most next to the one before it: what changed
    between the run that worked and the run that did not. So a bare
    --vcd stamps the time into the name rather than overwriting, and
    removes the oldest so the directory holds `keep` of them and does
    not grow without limit.

    Only files this made are considered. A VCD with a name of your own
    is yours and is never removed."""
    os.makedirs(outdir, exist_ok = True)
    pattern = os.path.join(outdir, f'{name}_*.vcd')
    existing = sorted(glob.glob(pattern), key = os.path.getmtime)
    for stale in existing[:max(0, len(existing) - (keep - 1))]:
        try:
            os.remove(stale)
        except OSError:
            pass
    stamp = time.strftime('%Y%m%d_%H%M%S')
    path = os.path.join(outdir, f'{name}_{stamp}.vcd')
    if os.path.exists(path):
        # a second run inside the same second
        path = os.path.join(outdir, f'{name}_{stamp}_{os.getpid()}.vcd')
    return path


def run_test (elaborate, test, opts, prog):
    top = elaborate()
    if not isinstance(top, Elaborated):
        raise IsomorphError(f'{prog}: the elaborate function must return '
                            'an elaborated block')
    sim = Simulator(top, backend = opts.backend,
                    allow_severe = opts.allow_severe)
    vcd_path = None
    if opts.vcd:
        if opts.vcd is True:
            vcd_path = rotate_vcd(opts.outdir, sim.top.name)
        else:
            vcd_path = opts.vcd
        directory = os.path.dirname(os.path.abspath(vcd_path))
        os.makedirs(directory, exist_ok = True)
        sim.write_vcd(vcd_path, traces = opts.traces)
    started = time.time()
    test(sim)
    elapsed = time.time() - started
    sim.close()
    print(f'{os.path.basename(prog)}: {opts.backend} backend, '
          f'{sim.cycle} cycles, {elapsed:.2f}s, ok')
    if vcd_path:
        print('wrote', os.path.abspath(vcd_path))
    return 0


def main (elaborate, test = None, argv = None, prog = None):
    """Command line for one design file. Returns an exit code."""
    argv = list(sys.argv if argv is None else argv)
    prog = prog or (argv[0] if argv else 'design.py')
    try:
        opts = parse(argv[1:], prog)
    except IsomorphError as error:
        print(error, file = sys.stderr)
        return 2

    if opts.help:
        print(_usage(prog))
        return 0

    try:
        if opts.writing:
            convert(elaborate(),
                    dump_ir = opts.dump or None,
                    sv = opts.sv if opts.sv is not None else False,
                    vhdl = opts.vhdl if opts.vhdl is not None else False,
                    c99 = opts.c99 if opts.c99 is not None else False,
                    json = opts.json,
                    sdc = opts.sdc,
                    allow_severe = opts.allow_severe,
                    lint = opts.lint,
                    outdir = opts.outdir)
        if opts.run:
            if test is None:
                print(f'{os.path.basename(prog)}: no test bench; this file '
                      'passed no test function to main()', file = sys.stderr)
                return 2
            return run_test(elaborate, test, opts, prog)
    except (IsomorphError, SimError) as error:
        print(error, file = sys.stderr)
        return 1
    return 0
