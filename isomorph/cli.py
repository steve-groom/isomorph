"""python -m isomorph: dump IR, convert to SystemVerilog, lint."""
import runpy
import sys

USAGE = (
    'usage: python -m isomorph dump design.py\n'
    '       python -m isomorph design.py [--sv] [--vhdl] [--c99] '
    '[--lint] [-o dir]\n'
    '       python -m isomorph sim-report log.ndjson '
    '[sidecar.json] [events.json]\n'
    '  default: write build/<top>.sv\n'
    '  --c99    cycle-accurate C99 smoke (.c, .h, _vcd.c)\n'
    '  -o dir   output directory (default build/)'
)


def main (argv = None):
    argv = list(sys.argv if argv is None else argv)
    args = argv[1:]
    if not args or args[0] in ('-h', '--help'):
        stream = sys.stdout if args and args[0] in ('-h', '--help') else sys.stderr
        print(USAGE, file = stream)
        return 0 if args and args[0] in ('-h', '--help') else 2
    if args[0] == 'dump':
        if len(args) < 2:
            print(USAGE, file = sys.stderr)
            return 2
        design = args[1]
        sys.argv = [design, '--dump'] + args[2:]
        runpy.run_path(design, run_name = '__main__')
        return 0
    if args[0] == 'sim-report':
        if len(args) < 2:
            print(USAGE, file = sys.stderr)
            return 2
        from .sim_report import report
        sidecar = None
        events_path = None
        for extra in args[2:]:
            if extra.endswith('.events.json') or extra.endswith('events.json'):
                events_path = extra
            elif sidecar is None:
                sidecar = extra
            else:
                events_path = extra
        result = report(args[1], sidecar, events_path)
        sys.stdout.write(result['text'])
        return 0
    design = None
    flags = []
    for arg in args:
        if design is None and not arg.startswith('-') and arg.endswith('.py'):
            design = arg
        else:
            flags.append(arg)
    if design is None:
        print(USAGE, file = sys.stderr)
        return 2
    sys.argv = [design] + flags
    runpy.run_path(design, run_name = '__main__')
    return 0
