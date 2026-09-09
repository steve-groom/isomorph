"""main(): the standard __main__ for a design file.

A design file ends with

    if (__name__ == '__main__'):
        main(elaborate_thing, test_thing)

where elaborate_thing() returns an elaborated block and test_thing(sim)
drives it. That one line gives the file a command line: run the test
bench on any of the three simulators, write SystemVerilog, VHDL or C99,
lint what was written, dump the IR, or print help.
"""
import os
import sys
import time

from . import convert
from .elaborate import Elaborated
from .execute import SimError
from .signal import IsomorphError
from .sim import Simulator

BACKENDS = ('python', 'c99', 'verilator')

USAGE = """usage: python3 {prog} [options]

With no options the design's test bench runs on the Python simulator.

run
  --run [BACKEND]  run the test bench. BACKEND is one of
                     python     IR interpreter, prints and pdb work
                     c99        emitted C99 through gcc, fast
                     verilator  emitted SystemVerilog, the reference
                   default python
  --vcd FILE       write a VCD of the run
  --traces A,B     limit the VCD to these signals

write
  --sv [FILE]      SystemVerilog
  --vhdl [FILE]    VHDL-2008
  --c99 [FILE]     C99 sources (.c, .h, _vcd.c)
  --lint           check what was written with verilator and ghdl
  --dump           print the IR instead of writing
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
        self.lint = False
        self.dump = False
        self.outdir = 'build'
        self.help = False

    @property
    def writing (self):
        return (self.sv is not None or self.vhdl is not None
                or self.c99 is not None or self.dump)


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
        elif arg == '--lint':
            opts.lint = True
        elif arg == '--dump':
            opts.dump = True
        elif arg == '--vcd':
            if not rest:
                raise IsomorphError('--vcd needs a file name\n\n'
                                    + _usage(prog))
            opts.vcd = rest.pop(0)
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


def run_test (elaborate, test, opts, prog):
    top = elaborate()
    if not isinstance(top, Elaborated):
        raise IsomorphError(f'{prog}: the elaborate function must return '
                            'an elaborated block')
    sim = Simulator(top, backend = opts.backend)
    if opts.vcd:
        directory = os.path.dirname(os.path.abspath(opts.vcd))
        os.makedirs(directory, exist_ok = True)
        sim.write_vcd(opts.vcd, traces = opts.traces)
    started = time.time()
    test(sim)
    elapsed = time.time() - started
    sim.close()
    print(f'{os.path.basename(prog)}: {opts.backend} backend, '
          f'{sim.cycle} cycles, {elapsed:.2f}s, ok')
    if opts.vcd:
        print('wrote', os.path.abspath(opts.vcd))
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
            saved = sys.argv
            sys.argv = [prog] + (['--lint'] if opts.lint else []) \
                + ['-o', opts.outdir]
            try:
                convert(elaborate(),
                        dump_ir = opts.dump or None,
                        sv = opts.sv if opts.sv is not None else False,
                        vhdl = opts.vhdl if opts.vhdl is not None else False,
                        c99 = opts.c99 if opts.c99 is not None else False)
            finally:
                sys.argv = saved
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
