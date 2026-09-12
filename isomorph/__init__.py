"""Isomorph: structured Python RTL to SystemVerilog and VHDL."""
import os
import sys

from .signal import (signal, signals, vector, enum, struct,
    preload, attr, open_port, concat, replicate, bits, always_comb,
    always_ff, always_ff_async_reset, assign, instances, IsomorphError)
from .elaborate import block, Elaborated
from .blackbox import blackbox, Blackbox
from .analyse import analyse, fatal_warnings, ConversionError
from .dump import dump
from .emit_sv import emit_sv, write_sv, write_sv_files, lint_sv
from .emit_vhdl import (emit_vhdl, write_vhdl, write_vhdl_files,
                        lint_vhdl)
from .emit_c99 import emit_c99, write_c99
from .sidecar import sidecar, write_sidecar
from .execute import SimError
from .sim import Simulator

__all__ = ['block', 'blackbox', 'signal', 'signals', 'vector', 'enum',
           'struct', 'preload', 'attr', 'open_port', 'concat',
           'replicate', 'bits', 'always_comb', 'always_ff',
           'always_ff_async_reset', 'assign',
           'instances', 'convert', 'emit_sv', 'emit_vhdl', 'emit_c99',
           'IsomorphError', 'ConversionError', 'SimError', 'Simulator',
           'main']


def report_lint (text):
    """What the linter said, put in front of you.

    A warning is not a failure to ghdl or to verilator, but a vendor
    tool may well make it an error -- a literal past the end of
    integer is one -- so it is printed rather than dropped."""
    for line in (text or '').splitlines():
        if line.strip():
            print(line)


def convert (top, dump_ir = None, sv = None, vhdl = None, c99 = None,
             allow_severe = False, lint = False, outdir = 'build',
             json = None):
    """Analyse a block. Dump the IR with dump_ir / --dump.

    Default: write build/<top>/ - one file per module and a
    .f list of them - and print the paths.
    sv / --sv: SystemVerilog (path or default).
    vhdl / --vhdl: VHDL-2008 (path or default).
    json / --json: the intermediate form as JSON, for another tool.
    c99 / --c99: cycle-accurate C99 smoke (.c and .h).
    lint runs verilator and ghdl over what was written.
    outdir is where a path that was not given by name goes.

    An inferred latch or a combinational loop stops the conversion.
    allow_severe carries on with it anyway and says what it allowed.

    Every option is an argument. This used to read sys.argv, which
    meant a block imported by a parent behaved differently depending on
    how the process happened to be started, and entry.py had to
    rewrite sys.argv around the call to say what it wanted. Option
    parsing lives in entry.py, which is the only thing that has a
    command line.
    """
    if not isinstance(top, Elaborated):
        raise IsomorphError('convert() takes an elaborated block instance')
    modules, warnings = analyse(top, allow_severe)
    if allow_severe:
        for w in fatal_warnings(warnings):
            text = ' '.join(w.replace('severe:', '', 1).split())
            print('allowed severe:', text, file = sys.stderr)
    dumping = bool(dump_ir)
    if dumping:
        print(dump(modules, warnings))
    elif warnings:
        for w in warnings:
            if 'severe:' in w:
                text = ' '.join(w.replace('severe:', '', 1).split())
                print('severe:', text, file = sys.stderr)
            else:
                print('warning:', w, file = sys.stderr)

    want_sv = False
    want_vhdl = False
    want_c99 = False
    sv_path = None
    vhdl_path = None
    c99_path = None
    if sv is False:
        want_sv = False
    elif isinstance(sv, str):
        want_sv = True
        sv_path = sv
    elif sv is True:
        want_sv = True
    elif not dumping and vhdl is None and c99 is None:
        want_sv = True

    if vhdl is False:
        want_vhdl = False
    elif isinstance(vhdl, str):
        want_vhdl = True
        vhdl_path = vhdl
    elif vhdl is True:
        want_vhdl = True

    if c99 is False:
        want_c99 = False
    elif isinstance(c99, str):
        want_c99 = True
        c99_path = c99
    elif c99 is True:
        want_c99 = True

    top_name = modules[-1].name
    wrote = []
    # a file per module and a list of them, which is what a vendor
    # tool wants handed to it and what a person writing the HDL by
    # hand would produce. Naming a file explicitly still puts the
    # whole design in that one file, which is what a bench wants
    if want_sv:
        if sv_path is None:
            written = write_sv_files(modules,
                                     os.path.join(outdir, top_name))
            wrote += written
            if lint:
                report_lint(
                    lint_sv([p for p in written if p.endswith('.sv')],
                            top = top_name))
        else:
            write_sv(modules, sv_path)
            wrote.append(sv_path)
            if lint:
                report_lint(lint_sv(sv_path, top = top_name))
    if want_vhdl:
        if vhdl_path is None:
            written = write_vhdl_files(modules,
                                       os.path.join(outdir, top_name))
            wrote += written
            if lint:
                report_lint(
                    lint_vhdl([p for p in written if p.endswith('.vhd')]))
        else:
            write_vhdl(modules, vhdl_path)
            wrote.append(vhdl_path)
            if lint:
                report_lint(lint_vhdl(vhdl_path))
    if want_c99:
        if c99_path is None:
            os.makedirs(outdir, exist_ok = True)
            c99_path = os.path.join(outdir, top_name + '.c')
        c_path, h_path = write_c99(modules, c99_path)
        wrote.append(c_path)
        wrote.append(h_path)
        vcd_c = os.path.splitext(c_path)[0] + '_vcd.c'
        if os.path.isfile(vcd_c):
            wrote.append(vcd_c)
    # the sidecar is the intermediate form as JSON, for a tool that
    # wants to read a design rather than build it - a pin planner, a
    # register-map generator. Nothing in isomorph reads it, so it is
    # written when it is asked for and not beside every conversion
    if json is not None:
        json_path = json
        if json_path is True:
            base = os.path.abspath(wrote[0]) if wrote else outdir
            json_path = os.path.join(
                os.path.dirname(base) if wrote else outdir,
                top_name + '.iso.json')
        write_sidecar(modules, json_path)
        wrote.append(json_path)
    for path in wrote:
        print('wrote', os.path.abspath(path))
    return modules


from .entry import main            # noqa: E402  (needs convert)
