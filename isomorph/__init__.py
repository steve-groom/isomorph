"""Isomorph: structured Python RTL to SystemVerilog and VHDL."""
import os
import sys

from .signal import (signal, signals, vector, enum, struct, interface,
    interfaces, attr, open_port, concat, replicate, bits, always_comb,
    always_ff, assign, instances, IsomorphError)
from .elaborate import block, Elaborated
from .analyse import analyse, ConversionError
from .dump import dump
from .emit_sv import emit_sv, write_sv, lint_sv
from .emit_vhdl import emit_vhdl, write_vhdl, lint_vhdl
from .emit_c99 import emit_c99, write_c99
from .sidecar import sidecar, write_sidecar
from .execute import SimError
from .sim import Simulator

__all__ = ['block', 'signal', 'signals', 'vector', 'enum', 'struct',
           'interface', 'interfaces', 'attr', 'open_port', 'concat',
           'replicate', 'bits', 'always_comb', 'always_ff', 'assign',
           'instances', 'convert', 'emit_sv', 'emit_vhdl', 'emit_c99',
           'IsomorphError', 'ConversionError', 'SimError', 'Simulator',
           'main']


def _argv_has (flag):
    return flag in sys.argv


def _argv_value (flag, default = None):
    if flag in sys.argv:
        index = sys.argv.index(flag)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return default


def convert (top, dump_ir = None, sv = None, vhdl = None, c99 = None):
    """Analyse a block. Dump the IR with dump_ir / --dump.

    Default: write build/<top>.sv and print the path.
    sv / --sv: SystemVerilog (path or default).
    vhdl / --vhdl: VHDL-2008 (path or default).
    c99 / --c99: cycle-accurate C99 smoke (.c and .h).
    --lint runs verilator and/or ghdl.
    """
    if not isinstance(top, Elaborated):
        raise IsomorphError('convert() takes an elaborated block instance')
    modules, warnings = analyse(top)
    dumping = dump_ir or _argv_has('--dump')
    if dumping:
        print(dump(modules, warnings))
    elif warnings:
        for w in warnings:
            if 'severe:' in w:
                text = w.replace('severe:', '', 1).strip()
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
    elif sv is True or _argv_has('--sv'):
        want_sv = True
    elif (not dumping and vhdl is None and not _argv_has('--vhdl')
          and c99 is None and not _argv_has('--c99')):
        want_sv = True

    if vhdl is False:
        want_vhdl = False
    elif isinstance(vhdl, str):
        want_vhdl = True
        vhdl_path = vhdl
    elif vhdl is True or _argv_has('--vhdl'):
        want_vhdl = True

    if c99 is False:
        want_c99 = False
    elif isinstance(c99, str):
        want_c99 = True
        c99_path = c99
    elif c99 is True or _argv_has('--c99'):
        want_c99 = True

    outdir = _argv_value('-o', 'build')
    top_name = modules[-1].name
    wrote = []
    if want_sv:
        if sv_path is None:
            os.makedirs(outdir, exist_ok = True)
            sv_path = os.path.join(outdir, top_name + '.sv')
        write_sv(modules, sv_path)
        wrote.append(sv_path)
        if _argv_has('--lint'):
            lint_sv(sv_path, top = top_name)
    if want_vhdl:
        if vhdl_path is None:
            os.makedirs(outdir, exist_ok = True)
            vhdl_path = os.path.join(outdir, top_name + '.vhd')
        write_vhdl(modules, vhdl_path)
        wrote.append(vhdl_path)
        if _argv_has('--lint'):
            lint_vhdl(vhdl_path)
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
    if wrote:
        first = os.path.abspath(wrote[0])
        json_path = os.path.join(os.path.dirname(first),
                                 top_name + '.iso.json')
        write_sidecar(modules, json_path)
        wrote.append(json_path)
    for path in wrote:
        print('wrote', os.path.abspath(path))
    return modules


from .entry import main            # noqa: E402  (needs convert)
