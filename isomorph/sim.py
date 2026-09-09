"""Simulator API over the Python executor and compiled C99 (SPEC 6)."""
import json
import os
import subprocess
import tempfile
from ctypes import POINTER, Structure, c_int, c_uint64, CDLL, byref

from .analyse import analyse
from .elaborate import Elaborated
from .emit_c99 import ff_driven_names, module_clocks, write_c99
from .execute import Executor, SimError, split_index
from .signal import IsomorphError, Signal
from .vcd import VcdWriter


RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'runtime')


class Simulator:
    """set / get / eval / tick / posedge, VCD and ndjson.

    backend: 'python' (IR interpreter), 'c99' (gcc -shared) or
    'verilator' (the emitted .sv under Verilator, the reference).
    add_clock(period) stores VCD display time only. Not STA.
    """

    def __init__ (self, top, backend = 'python', workdir = None):
        if not isinstance(top, Elaborated):
            raise IsomorphError('Simulator() takes an elaborated block')
        self.elaborated = top
        self.modules, self.warnings = analyse(top)
        self.backend = backend
        self.by_name = {m.name: m for m in self.modules}
        self.top = self.modules[-1]
        self.period_ns = 10
        self.timescale = '1 ns'
        self.time = 0
        self.cycle = 0
        self.clock_name = _default_clock(self.modules)
        self.clocks = {}
        self.vcd = None
        self.traces = None
        self._vcd_wires = []
        self.log_fp = None
        self.log_last = {}
        self._python = None
        self._c99 = None
        self._native = None
        self._checks = []
        self._proto_events = []
        if backend == 'python':
            self._python = Executor(self.modules)
        elif backend == 'c99':
            self._c99 = C99Backend(self.modules, workdir)
            self._native = self._c99
        elif backend == 'verilator':
            from .verilator import VerilatorBackend
            self._native = VerilatorBackend(self.modules, workdir)
        else:
            raise SimError(f"unknown simulator backend {backend!r}; "
                           "use 'python', 'c99' or 'verilator'")

    def add_clock (self, period, clock = None):
        ns = max(1, int(round(float(period) / 1e-9)))
        name = self._name(clock) if clock is not None else self.clock_name
        if name is not None:
            self.clocks[name] = ns
        if clock is None or name == self.clock_name:
            self.period_ns = ns
            if name is not None:
                self.clock_name = name

    def _period (self, clock):
        if clock and clock in self.clocks:
            return self.clocks[clock]
        return self.period_ns

    def set (self, key, value):
        name = self._name(key)
        if self._python is not None:
            self._python.set(name, value)
        else:
            self._native.set(name, value)

    def get (self, key):
        name = self._name(key)
        if self._python is not None:
            return self._python.get(name)
        return self._native.get(name)

    def add_check (self, check):
        """Protocol monitor. Sampled after comb settle, before the edge."""
        self._checks.append(check)

    def reset (self, port = None, ticks = 2, active = 1):
        """Drive a reset port, tick, then release. Not magic hardware."""
        name = None
        if port is not None:
            name = self._name(port)
        else:
            for p in self.top.ports:
                leaf = p.name.lower()
                if 'reset' in leaf or 'sreset' in leaf:
                    name = p.name
                    break
        if name is None:
            raise SimError('reset() needs a reset port')
        self.set(name, active)
        self.tick(int(ticks))
        self.set(name, 0 if active else 1)

    def eval (self):
        if self._python is not None:
            self._python.eval()
        else:
            self._native.eval()
        self._dump()
        self._run_checks('comb')

    def posedge (self, clock = None):
        self._run_checks('edge')
        clk = self._name(clock) if clock is not None else None
        if self._python is not None:
            self._python.posedge(clk)
        else:
            self._native.clock(clk)
        self.cycle += 1
        self.time += self._period(clk or self.clock_name)
        self._dump()

    def tick (self, n = 1, clock = None):
        n = int(n)
        clk = self._name(clock) if clock is not None else None
        for _ in range(n):
            if self._python is not None:
                self._python.eval()
                self._run_checks('edge')
                self._python.posedge(clk)
            else:
                self._native.eval()
                self._run_checks('edge')
                self._native.clock(clk)
            self.cycle += 1
            self.time += self._period(clk or self.clock_name)
            self._dump()

    def run (self, duration = None, ticks = None):
        """Advance wall-display time, firing each add_clock domain.

        duration is seconds. ticks=N is sugar for tick(N) on the
        default clock. Simultaneous edges share one NBA commit on
        the Python backend.
        """
        if ticks is not None:
            self.tick(ticks)
            return
        if duration is None:
            raise SimError('run() needs duration= seconds or ticks=')
        end = self.time + max(1, int(round(float(duration) / 1e-9)))
        clocks = dict(self.clocks)
        if not clocks:
            if self.clock_name:
                clocks = {self.clock_name: self.period_ns}
            else:
                raise SimError('run() needs add_clock()')
        next_t = {c: self.time + p for c, p in clocks.items()}
        while True:
            due = [(t, c) for c, t in next_t.items() if t <= end]
            if not due:
                self.time = end
                break
            t = min(edge for edge, _ in due)
            group = [c for c, te in next_t.items() if te == t]
            self._fire(group, t)
            for c in group:
                next_t[c] += clocks[c]

    def _fire (self, group, t):
        if self._python is not None:
            self._python.eval()
            self._run_checks('edge')
            if len(group) == 1:
                self._python.posedge(group[0])
            else:
                for c in group:
                    self._python.posedge(c, commit = False)
                self._python.commit()
        else:
            self._native.eval()
            self._run_checks('edge')
            for c in group:
                self._native.clock(c)
        self.cycle += 1
        self.time = t
        self._dump()

    def write_events (self, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok = True)
        with open(path, 'w', encoding = 'ascii', newline = '\n') as f:
            json.dump(self.events(), f, indent = 2)
            f.write('\n')
        return path

    def write_vcd (self, path, traces = None):
        self.traces = traces
        self.vcd = VcdWriter(path, timescale = self.timescale)
        self._vcd_wires = []
        self.vcd.push_scope(self.top.name)
        self._define_scope(self.top, '')
        self.vcd.pop_scope()
        self.vcd.finish_defs()
        self._vcd_emit()
        self.vcd.end_dumpvars()

    def write_log (self, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok = True)
        self.log_fp = open(path, 'w', encoding = 'ascii', newline = '\n')
        self.log_last = {}
        self._log_emit()

    def events (self):
        out = []
        if self._python is not None:
            out.extend(self._python.events)
        out.extend(self._proto_events)
        return out

    def _run_checks (self, when):
        for check in self._checks:
            check.sample(self, when)

    def close (self):
        if self.vcd is not None:
            self.vcd.close()
            self.vcd = None
        if self.log_fp is not None:
            self.log_fp.close()
            self.log_fp = None

    def _name (self, key):
        if isinstance(key, str):
            return key
        if isinstance(key, Signal):
            name = self.elaborated.port_names.get(id(key), key.name)
            if name is None:
                raise SimError('signal has no name')
            return name
        raise SimError(f'cannot use {key!r} as a signal name')

    def _define_scope (self, module, prefix):
        for p in module.ports:
            self._define_field(prefix, p.name, p.width, p.array)
        for s in module.signals:
            self._define_field(prefix, s.name, s.width, s.array)
        for inst in module.instances:
            self.vcd.push_scope(inst.name)
            self._define_scope(self.by_name[inst.module],
                               f'{prefix}{inst.name}.')
            self.vcd.pop_scope()

    def _define_field (self, prefix, name, width, array):
        if array:
            for i in range(array):
                self._define_one(prefix, f'{name}[{i}]', width)
        else:
            self._define_one(prefix, name, width)

    def _define_one (self, prefix, name, width):
        hier = prefix + name
        if not _wanted(hier, self.traces):
            return
        ident = self.vcd.wire(name, width)
        self._vcd_wires.append((ident, hier, width))

    def _dump (self):
        if self.vcd is not None:
            self.vcd.time(self.time)
            self._vcd_emit()
        if self.log_fp is not None:
            self._log_emit()

    def _vcd_emit (self):
        values = {hier: value for hier, width, value in self._live()}
        for ident, hier, width in self._vcd_wires:
            if hier in values:
                self.vcd.change(ident, values[hier], width)

    def _log_emit (self):
        ch = {}
        for hier, width, value in self._live():
            if not _wanted(hier, self.traces):
                continue
            if self.log_last.get(hier) == value:
                continue
            ch[hier] = int(value)
            self.log_last[hier] = int(value)
        rec = {'t': self.time, 'cycle': self.cycle, 'ch': ch}
        if self.clock_name:
            rec['clock'] = self.clock_name
        self.log_fp.write(json.dumps(rec) + '\n')
        self.log_fp.flush()

    def _live (self):
        if self._python is not None:
            return self._python.store.walk_live()
        if self._c99 is not None:
            return walk_c99(self.top, self.by_name, self._c99.state, '')
        # verilator: top-level ports only
        return [(name, width, self._native.get(name))
                for name, _, width, _ in self._native.slots]


def _wanted (hier, traces):
    if traces is None:
        return True
    for t in traces:
        if hier == t or hier.endswith('.' + t) or hier.split('.')[-1] == t:
            return True
    return False


def _default_clock (modules):
    clocks = []
    seen = set()
    for m in modules:
        for p in m.processes:
            if p.kind == 'ff' and p.clock and p.clock not in seen:
                seen.add(p.clock)
                clocks.append(p.clock)
    if len(clocks) == 1:
        return clocks[0]
    return clocks[0] if clocks else None


class C99Backend:
    def __init__ (self, modules, workdir):
        self.modules = modules
        self.top = modules[-1]
        self.by_name = {m.name: m for m in modules}
        if workdir is None:
            workdir = tempfile.mkdtemp(prefix = 'iso_sim_')
        self.workdir = workdir
        os.makedirs(workdir, exist_ok = True)
        c_path = os.path.join(workdir, self.top.name + '.c')
        write_c99(modules, c_path)
        so_path = os.path.join(workdir, self.top.name + '.so')
        cmd = ['gcc', '-std=c99', '-O2', '-fPIC', '-shared', '-Wall',
               '-o', so_path, c_path]
        build = subprocess.run(cmd, capture_output = True, text = True)
        if build.returncode != 0:
            raise SimError('gcc failed:\n' + build.stderr)
        self.lib = CDLL(so_path)
        self.ctype = make_ctype(self.top, self.by_name)
        self.state = self.ctype()
        name = self.top.name
        self._init = getattr(self.lib, name + '_init')
        self._eval = getattr(self.lib, name + '_eval')
        self._clock = getattr(self.lib, name + '_clock')
        self._tick = getattr(self.lib, name + '_tick')
        self._init.argtypes = [POINTER(self.ctype)]
        self._eval.argtypes = [POINTER(self.ctype)]
        self._clock.argtypes = [POINTER(self.ctype)]
        self._tick.argtypes = [POINTER(self.ctype)]
        self._eval.restype = c_int
        self._clock.restype = c_int
        self._tick.restype = c_int
        self._clock_by = {}
        for clk in module_clocks(self.top):
            fn = getattr(self.lib, f'{name}_clock_{clk}', None)
            if fn is not None:
                fn.argtypes = [POINTER(self.ctype)]
                fn.restype = c_int
                self._clock_by[clk] = fn
        self._init(byref(self.state))

    def eval (self):
        if self._eval(byref(self.state)) != 0:
            raise SimError(f'combinational loop in {self.top.name}')

    def clock (self, clock = None):
        if clock and clock in self._clock_by:
            r = self._clock_by[clock](byref(self.state))
        else:
            r = self._clock(byref(self.state))
        if r != 0:
            raise SimError(f'combinational loop in {self.top.name}')

    def tick (self):
        if self._tick(byref(self.state)) != 0:
            raise SimError(f'combinational loop in {self.top.name}')

    def get (self, path):
        obj, base, idx = self._resolve(path)
        val = getattr(obj, base)
        if idx is not None:
            return int(val[idx])
        if isinstance(val, (list, tuple)) or hasattr(val, '__len__') and not isinstance(val, int):
            try:
                return [int(val[i]) for i in range(len(val))]
            except TypeError:
                pass
        return int(val)

    def set (self, path, value):
        obj, base, idx = self._resolve(path)
        field = getattr(obj, base)
        if idx is not None:
            field[idx] = int(value)
            return
        setattr(obj, base, int(value))

    def _resolve (self, path):
        parts = path.split('.')
        obj = self.state
        for p in parts[:-1]:
            obj = getattr(obj, p)
        base, idx = split_index(parts[-1])
        if not hasattr(obj, base):
            raise SimError(f'unknown signal {path}')
        return obj, base, idx


def make_ctype (module, by_name, cache = None):
    if cache is None:
        cache = {}
    if module.name in cache:
        return cache[module.name]
    ff = ff_driven_names(module)
    fields = []
    for p in module.ports:
        fields += _ctype_fields(p.name, p.array, p.name in ff)
    for s in module.signals:
        fields += _ctype_fields(s.name, s.array, s.name in ff)
    for inst in module.instances:
        child = make_ctype(by_name[inst.module], by_name, cache)
        fields.append((inst.name, child))

    class M (Structure):
        _fields_ = fields

    M.__name__ = module.name
    cache[module.name] = M
    return M


def _ctype_fields (name, array, has_nxt):
    typ = (c_uint64 * array) if array else c_uint64
    out = [(name, typ)]
    if has_nxt:
        out.append((name + '_nxt', typ))
    return out


def walk_c99 (module, by_name, state, prefix):
    for p in module.ports:
        yield from _walk_c_field(state, prefix, p.name, p.width, p.array)
    for s in module.signals:
        yield from _walk_c_field(state, prefix, s.name, s.width, s.array)
    for inst in module.instances:
        child_state = getattr(state, inst.name)
        child = by_name[inst.module]
        yield from walk_c99(child, by_name, child_state,
                            f'{prefix}{inst.name}.')


def _walk_c_field (state, prefix, name, width, array):
    val = getattr(state, name)
    if array:
        for i in range(array):
            yield f'{prefix}{name}[{i}]', width, int(val[i])
    else:
        yield f'{prefix}{name}', width, int(val)
