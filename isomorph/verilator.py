"""Verilator backend for Simulator: the emitted SystemVerilog itself.

The Python and C99 backends run isomorph's own model of the design.
This one runs Verilator's, so it is the reference the other two are
checked against (SPEC 6, 8.7). Verilator is a compiler, not a library,
so a generated shim exposes the top module's ports through a small C
interface and ctypes drives it.

Ports only. Internal signals are not public in the generated model, so
get() and set() of a signal that is not a top-level port raise.
"""
import os
import re
import shutil
import subprocess
import tempfile
from ctypes import CDLL, c_int, c_uint64

from .emit_sv import write_sv
from .emit_c99 import hierarchy_clocks, ff_driven_names
from .execute import SimError


def _internal_cases (internals):
    out = []
    for _, index, width, member in internals:
        ctype = _c_type(width)
        if ctype is None:
            out.append(f'    case {index}: return (uint64_t)'
                       f'g_top->rootp->{member}[word];')
        else:
            out.append(f'    case {index}: return (uint64_t)'
                       f'g_top->rootp->{member};')
    return out


def _c_type (width):
    if width <= 8:
        return 'CData'
    if width <= 16:
        return 'SData'
    if width <= 32:
        return 'IData'
    if width <= 64:
        return 'QData'
    return None                         # wide: VlWide, word addressed


def verilator_root ():
    exe = shutil.which('verilator')
    if exe is None:
        raise SimError('verilator not found on PATH')
    out = subprocess.run(['verilator', '--getenv', 'VERILATOR_ROOT'],
                         capture_output = True, text = True)
    root = out.stdout.strip()
    if not root or not os.path.isdir(os.path.join(root, 'include')):
        raise SimError('cannot locate VERILATOR_ROOT')
    return root


def port_slots (top):
    """[(name, index, width, member expression)] for every port leaf.

    An unpacked array port contributes one slot per element, named
    name[i], matching the paths the other backends accept."""
    slots = []
    for p in top.ports:
        if p.array:
            for i in range(p.array):
                slots.append((f'{p.name}[{i}]', len(slots), p.width,
                              f'{p.name}[{i}]'))
        else:
            slots.append((p.name, len(slots), p.width, p.name))
    return slots


def internal_slots (top, first_index):
    """[(name, index, width, member)] for the top's own flip-flops.

    Ports are reachable already. What a bench cannot see on Verilator
    is the state: the counter, the shift register, the state enum. Only
    the top module's own registers, and only reading them, so nothing
    here can write a non-blocking target behind the model's back.

    Arrays are left out. A RAM is not what someone is trying to look at
    when they ask why a state machine is stuck, and one member per
    element is a lot of shim for that.
    """
    ports = {p.name for p in top.ports}
    driven = ff_driven_names(top)
    out = []
    for sig in top.signals:
        if sig.name in ports or sig.array or sig.name not in driven:
            continue
        member = f'{top.name}__DOT__{sig.name}'
        out.append((sig.name, first_index + len(out), sig.width, member))
    return out


def emit_vlt (top, internals):
    """Verilator's own configuration file, so the annotation never
    reaches the SystemVerilog.

    public_flat_rd and nothing else. Plain `public` would stop
    Verilator inlining the module and change what is being simulated;
    public_flat_rw would let a bench write a flip-flop from outside the
    model, which mis-simulates the same way a force does and, on 5.020,
    gets generated clocks wrong.
    """
    lines = ['`verilator_config']
    for name, _, _, _ in internals:
        lines.append(f'public_flat_rd -module "{top.name}" '
                     f'-var "{name}"')
    return '\n'.join(lines) + '\n'


def check_public (header_path, internals, top_name):
    """Every internal we asked for is in the generated model, under
    the name it has in the Python.

    The prefix Verilator adds is mechanical and the leaf is the name
    that was written. If a leaf is missing, something was renamed or
    optimised out, and quietly losing visibility is worse than saying
    so."""
    try:
        with open(header_path, encoding = 'utf-8', errors = 'replace') as f:
            text = f.read()
    except OSError:
        return
    missing = [name for name, _, _, member in internals
               if member not in text]
    if missing:
        raise SimError(
            f'verilator did not keep {", ".join(sorted(missing))} of '
            f'{top_name} readable. Isomorph never renames, so a name '
            'that is not in the model is a name that went missing, and '
            'a bench reading it would get a wrong answer rather than '
            'an error.')


def emit_shim (top, slots, clocks, internals = ()):
    name = top.name
    out = [
        f'#include "V{name}.h"',
        # the root holds the module's own signals, which is where a
        # public_flat_rd flip-flop lives
        f'#include "V{name}___024root.h"',
        '#include "verilated.h"',
        '#include <stdint.h>',
        '#include <cstdio>',
        '#include <cstring>',
        '',
        f'static V{name} *g_top = 0;',
        '',
        '/* Verilator ends the process when an immediate assertion',
        '   fires. That is the right default for a standalone harness',
        '   and the wrong one here: the bench is a Python program, and',
        '   killing the interpreter loses the cycle it was on and every',
        '   other check it was going to make. fatalOnError(false) is',
        '   the supported way to ask for a recorded error instead of an',
        '   abort; gotError() then says one happened. Verilator has',
        '   already printed the message, with the process it fired',
        '   in. */',
        '',
        'extern "C" {',
        '',
        'int iso_assert_failed (void)',
        '{',
        '    return Verilated::threadContextp()->gotError() ? 1 : 0;',
        '}',
        '',
        'void iso_assert_clear (void)',
        '{',
        '    Verilated::threadContextp()->gotError(false);',
        '}',
        '',
        'void iso_init (void)',
        '{',
        '    Verilated::threadContextp()->fatalOnError(false);',
        f'    if (!g_top) g_top = new V{name}();',
        '    g_top->eval();',
        '}',
        '',
        'void iso_final (void)',
        '{',
        '    if (g_top) { g_top->final(); delete g_top; g_top = 0; }',
        '}',
        '',
        'int iso_eval (void)',
        '{',
        '    g_top->eval();',
        '    return 0;',
        '}',
        '',
        'void iso_set (int idx, int word, uint64_t value)',
        '{',
        '    switch (idx) {',
    ]
    for _, index, width, member in slots:
        ctype = _c_type(width)
        if ctype is None:
            out.append(f'    case {index}: g_top->{member}[word] = '
                       '(uint32_t)value; break;')
        else:
            out.append(f'    case {index}: g_top->{member} = '
                       f'({ctype})value; break;')
    out += [
        '    default: break;',
        '    }',
        '}',
        '',
        'uint64_t iso_get (int idx, int word)',
        '{',
        '    switch (idx) {',
    ]
    for _, index, width, member in slots:
        if _c_type(width) is None:
            out.append(f'    case {index}: return (uint64_t)'
                       f'g_top->{member}[word];')
        else:
            out.append(f'    case {index}: return (uint64_t)'
                       f'g_top->{member};')
    out += [
        '    default: break;',
        '    }',
        '    return 0;',
        '}',
        '',
        '/* The top module\'s own flip-flops, read only. The prefix is',
        '   verilator\'s; the leaf is the name written in the Python. */',
        'uint64_t iso_get_internal (int idx, int word)',
        '{',
        '    (void)word;',
        '    switch (idx) {',
    ] + _internal_cases(internals) + [
        '    default: break;',
        '    }',
        '    return 0;',
        '}',
        '',
        'int iso_clock (int idx)',
        '{',
        '    switch (idx) {',
    ]
    by_name = {n: i for n, i, _, _ in slots}
    for clk in clocks:
        if clk in by_name:
            index = by_name[clk]
            out += [
                f'    case {index}:',
                f'        g_top->{clk} = 1; g_top->eval();',
                f'        g_top->{clk} = 0; g_top->eval();',
                '        break;',
            ]
    out += [
        '    default: return 1;',
        '    }',
        '    return 0;',
        '}',
        '',
        '/* Several clocks whose edges land at the same time. They all',
        '   rise together, so a domain that samples another domain sees',
        '   the value it had before this instant, which is what the',
        '   hardware does and what taking them one at a time would',
        '   not. Bit n of the mask is clocks[n]. */',
        'int iso_clock_group (uint64_t mask)',
        '{',
    ]
    grouped = [c for c in clocks if c in by_name]
    if not grouped:
        out.append('    (void)mask;')
    for position, clk in enumerate(grouped):
        out.append(f'    if (mask & (1ULL << {position})) '
                   f'{{ g_top->{clk} = 1; }}')
    out.append('    g_top->eval();')
    for position, clk in enumerate(grouped):
        out.append(f'    if (mask & (1ULL << {position})) '
                   f'{{ g_top->{clk} = 0; }}')
    out += [
        '    g_top->eval();',
        '    return 0;',
        '}',
        '',
        '}',
        '',
    ]
    return '\n'.join(out)


class VerilatorBackend:
    """Same interface as C99Backend, driving Verilator's model."""

    def __init__ (self, modules, workdir = None, trace = False):
        self.modules = modules
        self.top = modules[-1]
        self.by_name = {m.name: m for m in modules}
        if workdir is None:
            workdir = tempfile.mkdtemp(prefix = 'iso_vl_')
        self.workdir = workdir
        os.makedirs(workdir, exist_ok = True)

        name = self.top.name
        self.slots = port_slots(self.top)
        self.index = {n: i for n, i, _, _ in self.slots}
        self.width = {n: w for n, _, w, _ in self.slots}
        self.clocks = hierarchy_clocks(self.top, self.by_name)
        self.default_clock = self.clocks[0] if self.clocks else None
        # the mask bit each clock owns in iso_clock_group, which is its
        # place in the list the shim was written from
        self.clock_position = {c: i for i, c in enumerate(
            [c for c in self.clocks if c in self.index])}

        self.internals = internal_slots(self.top, len(self.slots))
        self.internal_index = {n: i for n, i, _, _ in self.internals}
        self.internal_width = {n: w for n, _, w, _ in self.internals}

        sv_path = os.path.join(workdir, name + '.sv')
        write_sv(modules, sv_path)
        vlt_path = os.path.join(workdir, name + '.vlt')
        with open(vlt_path, 'w', encoding = 'ascii') as f:
            f.write(emit_vlt(self.top, self.internals))

        mdir = os.path.join(workdir, 'obj_dir')
        # --assert, or the immediate assertions the design carries are
        # compiled away and the one backend that reads the emitted
        # SystemVerilog checks none of them
        cmd = ['verilator', '--cc', '--sv', '--assert', '-Wno-fatal',
               '-Wno-DECLFILENAME', '--top-module', name,
               '--Mdir', mdir, '-CFLAGS', '-fPIC']
        if trace:
            cmd.append('--trace')
        cmd += [vlt_path, sv_path]
        build = subprocess.run(cmd, capture_output = True, text = True)
        if build.returncode != 0:
            raise SimError('verilator failed:\n'
                           + (build.stderr or build.stdout))
        check_public(os.path.join(mdir, f'V{name}___024root.h'),
                     self.internals, name)
        # the shim is written after verilator, because it reads members
        # that only exist once the model has been generated
        shim_path = os.path.join(workdir, 'iso_shim.cpp')
        with open(shim_path, 'w', encoding = 'ascii') as f:
            f.write(emit_shim(self.top, self.slots, self.clocks,
                              self.internals))
        make = subprocess.run(['make', '-C', mdir, '-f', f'V{name}.mk',
                               f'V{name}__ALL.a'],
                              capture_output = True, text = True)
        if make.returncode != 0:
            raise SimError('verilator model build failed:\n'
                           + (make.stderr or make.stdout))

        root = verilator_root()
        include = os.path.join(root, 'include')
        so_path = os.path.join(workdir, name + '_vl.so')
        runtime = ['verilated.cpp', 'verilated_threads.cpp']
        if trace:
            runtime.append('verilated_vcd_c.cpp')
        sources = [os.path.join(include, f) for f in runtime
                   if os.path.isfile(os.path.join(include, f))]
        link = ['g++', '-fPIC', '-shared', '-O2', '-std=c++17',
                '-o', so_path, shim_path] + sources + [
                '-I' + mdir, '-I' + include,
                '-I' + os.path.join(include, 'vltstd'),
                os.path.join(mdir, f'V{name}__ALL.a'), '-pthread']
        result = subprocess.run(link, capture_output = True, text = True)
        if result.returncode != 0:
            raise SimError('shim link failed:\n'
                           + (result.stderr or result.stdout))

        self.lib = CDLL(so_path)
        self.lib.iso_get.argtypes = [c_int, c_int]
        self.lib.iso_get.restype = c_uint64
        self.lib.iso_get_internal.argtypes = [c_int, c_int]
        self.lib.iso_get_internal.restype = c_uint64
        self.lib.iso_set.argtypes = [c_int, c_int, c_uint64]
        self.lib.iso_eval.restype = c_int
        self.lib.iso_clock.argtypes = [c_int]
        self.lib.iso_clock.restype = c_int
        self.lib.iso_clock_group.argtypes = [c_uint64]
        self.lib.iso_clock_group.restype = c_int
        self.lib.iso_assert_failed.restype = c_int
        self.lib.iso_init()

    # -- Simulator interface ------------------------------------------
    def eval (self):
        self.lib.iso_eval()
        self._check_asserts()

    def _check_asserts (self):
        """Raise if an assertion fired during the last evaluation.

        Verilator has already printed which one, where, and the text
        that was written after the comma, so this only has to stop the
        run rather than repeat it."""
        if not self.lib.iso_assert_failed():
            return
        self.lib.iso_assert_clear()
        raise SimError(
            f'an assertion in {self.top.name} failed; verilator printed '
            'it above, with the file, the line and the process')

    def clock (self, clock = None):
        name = clock or self.default_clock
        if name is None or name not in self.index:
            raise SimError('verilator backend needs a clock port, '
                           f'not {name!r}')
        if self.lib.iso_clock(self.index[name]) != 0:
            raise SimError(f'unknown clock {name}')
        self._check_asserts()

    def clock_group (self, clocks):
        """Several clocks whose edges land at the same time.

        All of them rise together and all of them fall together, so a
        flop sampling another domain sees that domain's pre-edge
        value. Clocking them one after another would show it the new
        one, which is the one thing a crossing must never see."""
        names = list(clocks)
        if len(names) == 1:
            return self.clock(names[0])
        mask = 0
        for name in names:
            if name not in self.clock_position:
                raise SimError(
                    f'{name!r} is not a clock of {self.top.name}')
            mask |= 1 << self.clock_position[name]
        if self.lib.iso_clock_group(mask) != 0:
            raise SimError('grouped clock failed')
        self._check_asserts()
        return None

    def tick (self):
        self.eval()
        self.clock()

    def _slot (self, path):
        if path in self.index:
            return self.index[path], self.width[path]
        raise SimError(
            f'{path!r} is not a port of {self.top.name}, nor one of its '
            'flip-flops. The verilator backend reaches the top module: '
            'its ports, and its own registers for reading.')

    def get (self, path):
        if path in self.internal_index:
            return self._read(self.lib.iso_get_internal,
                              self.internal_index[path],
                              self.internal_width[path])
        idx, width = self._slot(path)
        return self._read(self.lib.iso_get, idx, width)

    def _read (self, reader, idx, width):
        if width <= 64:
            return int(reader(idx, 0)) & ((1 << width) - 1)
        value = 0
        for word in range((width + 31) // 32):
            part = int(reader(idx, word)) & 0xffffffff
            value |= part << (32 * word)
        return value & ((1 << width) - 1)

    def set (self, path, value):
        if path in self.internal_index:
            raise SimError(
                f'{path!r} is a flip-flop inside {self.top.name}, and it '
                'can be read but not written. Writing one from outside '
                'the model races the non-blocking assignment that owns '
                'it, so the three backends would stop agreeing. Drive '
                'the port that leads to it instead.')
        idx, width = self._slot(path)
        value = int(value) & ((1 << width) - 1)
        if width <= 64:
            self.lib.iso_set(idx, 0, value)
            return
        for word in range((width + 31) // 32):
            self.lib.iso_set(idx, word, (value >> (32 * word)) & 0xffffffff)

    def close (self):
        if getattr(self, 'lib', None) is not None:
            self.lib.iso_final()
