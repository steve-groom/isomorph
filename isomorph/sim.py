"""Simulator API over the Python executor and compiled C99 (SPEC 6)."""
import json
import os
import subprocess
import tempfile
from ctypes import (POINTER, Structure, c_int, c_uint64, c_char_p, CDLL,
                    byref)

from .analyse import analyse
from .elaborate import Elaborated
from .emit_c99 import (ff_driven_names, module_clocks, clock_id,
                       hierarchy_clocks, write_c99, alias_source)
from .execute import Executor, SimError, split_index
from .signal import EnumMember, IsomorphError, Signal
from .vcd import VcdWriter


RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'runtime')


def _returning (value):
    if False:
        yield
    return value


class Cycles(int):
    """How many clocks something took.

    An int, and awaitable, so `n = sim.until(...)` and
    `n = await sim.until(...)` are the same bench written two ways.
    There is no scheduler under the await: the work is done by the
    time it returns, and the await only reads better.
    """

    def __await__ (self):
        return _returning(int(self))


class _Monitor:
    """A plain function at the sample point (ROADMAP 10)."""

    def __init__ (self, fn):
        self.fn = fn

    def sample (self, sim, when):
        if when == 'edge':
            self.fn(sim)


class Probe:
    """Every signal of the top module by name: sim.dut.sample_flop.

    Reading gives the value, assigning drives it. A name the design
    does not have is an error naming it, rather than a typo that
    quietly reads nothing.
    """

    def __init__ (self, sim, names):
        object.__setattr__(self, '_sim', sim)
        object.__setattr__(self, '_names', set(names))

    def __getattr__ (self, name):
        if name not in self._names:
            raise SimError(f'{name} is not a signal of '
                           f'{self._sim.top.name}')
        return self._sim.get(name)

    def __setattr__ (self, name, value):
        if name not in self._names:
            raise SimError(f'{name} is not a signal of '
                           f'{self._sim.top.name}')
        self._sim.set(name, value)

    def __dir__ (self):
        return sorted(self._names)


class Simulator:
    """set / get / eval / tick / posedge, VCD and ndjson.

    backend: 'python' (IR interpreter), 'c99' (gcc -shared) or
    'verilator' (the emitted .sv under Verilator, the reference).
    add_clock(period) stores VCD display time only. Not STA.
    """

    def __init__ (self, top, backend = 'python', workdir = None,
                  allow_severe = False):
        if not isinstance(top, Elaborated):
            raise IsomorphError('Simulator() takes an elaborated block')
        self.elaborated = top
        # a latch or a loop stops here too: a simulator that runs a
        # design the fitter would refuse is the gap this project exists
        # to close
        self.modules, self.warnings = analyse(top, allow_severe)
        from .emit_c99 import refuse_blackbox
        refuse_blackbox(self.modules, f'the {backend} backend')
        self.backend = backend
        self.by_name = {m.name: m for m in self.modules}
        self.top = self.modules[-1]
        self.period_ns = 10
        self.timescale = '1 ns'
        self.time = 0
        self.cycle = 0
        # every clock the design has, whether or not add_clock has been
        # called for it. The guard in tick() is about the design, not
        # about what the bench has got round to registering.
        self.domains = hierarchy_clocks(self.top, self.by_name)
        self.clock_name = _default_clock(self.domains, self.modules)
        self.clocks = {}
        self._next_edge = {}        # per clock, when its next edge is due
        self.vcd = None
        self.traces = None
        self._vcd_wires = []
        # the clock wave is drawn rather than sampled; see _falls()
        self._vcd_clocks = []       # (wire, the clock it shows)
        self._clock_value = {}      # clock -> what the wave is now
        self._clock_fall = {}       # clock -> when it goes low again
        self._fired_now = []        # clocks that took this edge
        self.log_fp = None
        self.log_last = {}
        self._python = None
        self._c99 = None
        self._native = None
        self._checks = []
        self._proto_events = []
        self._enum_of = {}
        self._enum_paths(self.top, '')
        self.dut = Probe(self, [s.name for s in
                                list(self.top.signals) + list(self.top.ports)])
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

    def _enum_paths (self, module, prefix):
        for s in list(module.signals) + list(module.ports):
            if s.kind == 'enum' and s.type is not None:
                self._enum_of[prefix + s.name] = s.type
        for inst in module.instances:
            child = self.by_name.get(inst.module)
            if child is not None and child is not module:
                self._enum_paths(child, prefix + inst.name + '.')

    def add_clock (self, period, clock = None):
        ns = max(1, int(round(float(period) / 1e-9)))
        name = self._name(clock) if clock is not None else self.clock_name
        if name is not None:
            if self.clocks.get(name) != ns:
                # a new or changed period restarts this clock's phase
                self._next_edge.pop(name, None)
            self.clocks[name] = ns
        if clock is None or name == self.clock_name:
            self.period_ns = ns
            if name is not None:
                self.clock_name = name

    def _period (self, clock):
        if clock and clock in self.clocks:
            return self.clocks[clock]
        return self.period_ns

    def set (self, key, value, settle = True):
        name = self._name(key)
        if name in self.clocks or name == self.clock_name:
            raise SimError(
                f'{name!r} is a clock, and a clock is not an input. Give '
                'it a period with add_clock() and advance it with tick(), '
                'posedge() or run(). Driving it by hand makes the three '
                'backends disagree, because Verilator sees a pin move '
                'and the other two do not.')
        if isinstance(value, EnumMember):
            value = value.value
        if self._python is not None:
            self._python.set(name, value)
        else:
            self._native.set(name, value)
        # forgetting eval() is a stale read, which is the worst kind of
        # bug a bench can have. settle = False drives several inputs
        # before letting any of them take effect
        if settle:
            self.eval()

    def get (self, key):
        name = self._name(key)
        if self._python is not None:
            value = self._python.get(name)
        else:
            value = self._native.get(name)
        kind = self._enum_of.get(name)
        if kind is not None:
            for member in kind.members:
                if member.value == value:
                    return member
        return value

    def add_check (self, check):
        """Protocol monitor. Sampled after comb settle, before the edge."""
        self._checks.append(check)

    def add_monitor (self, fn):
        """fn(sim) at the same point a check is sampled."""
        self._checks.append(_Monitor(fn))
        return fn

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

    def posedge (self, clock = None, repeat = 1):
        """One rising edge, or several at the same instant.

        Pass a list of clock names for coincident edges: they all take
        their edge before any of them commits, so a flop sampling
        another domain sees that domain's pre-edge value. That is what
        the hardware does, and taking them one after another is not.

        repeat takes that many edges. The answer is how many, which a
        bench may also await; there is no settle in front of an edge
        here, which is the difference from tick().
        """
        repeat = int(repeat)
        if repeat != 1:
            for _ in range(repeat):
                self.posedge(clock)
            return Cycles(repeat)
        if isinstance(clock, (list, tuple, set)):
            names = [self._name(c) for c in clock]
            if not names:
                raise SimError('posedge([]) has no clock to take')
            self._run_checks('edge')
            if self._python is not None:
                for name in names:
                    self._python.posedge(name, commit = False)
                self._python.commit()
            else:
                self._native.clock_group(names)
            self.cycle += 1
            self.time += self._period(names[0])
            self._fired_now = list(names)
            self._dump()
            return Cycles(1)
        self._run_checks('edge')
        clk = self._name(clock) if clock is not None else None
        if self._python is not None:
            self._python.posedge(clk)
        else:
            self._native.clock(clk)
        self.cycle += 1
        self.time += self._period(clk or self.clock_name)
        self._fired_now = [clk or self.clock_name]
        self._dump()
        return Cycles(1)

    def tick (self, n = 1, clock = None, sample = None):
        """One clock period: settle, then take the edge.

        sample names signals to read as they were before the edge,
        which is where a bus is worth looking at, and returns them in
        the order asked for. Without it the answer is the number of
        clocks taken, which a bench may also await.

        A design with more than one clock has to say which one. Ticking
        every domain on an unnamed clock is how a multi-clock design
        quietly passes: the crossing never sees the two rates it was
        written for. Use run(duration) to advance them all at their own
        periods.
        """
        n = int(n)
        clk = self._name(clock) if clock is not None else None
        if clk is None and len(self.domains) > 1:
            named = ', '.join(sorted(self.domains))
            raise SimError(
                f'{self.top.name} has more than one clock ({named}), so '
                "tick() has to name one: tick(n, 'i_wr_clock'). To "
                'advance them all at their own periods, use '
                'run(duration).')
        wanted = list(sample) if sample is not None else None
        taken = None
        for _ in range(n):
            if self._python is not None:
                self._python.eval()
                if wanted is not None and taken is None:
                    taken = [self.get(s) for s in wanted]
                self._run_checks('edge')
                self._python.posedge(clk)
            else:
                self._native.eval()
                if wanted is not None and taken is None:
                    taken = [self.get(s) for s in wanted]
                self._run_checks('edge')
                self._native.clock(clk)
            self.cycle += 1
            self.time += self._period(clk or self.clock_name)
            self._fired_now = [clk or self.clock_name]
            self._dump()
        return taken if wanted is not None else Cycles(n)

    def until (self, condition, limit, clock = None):
        """Clock until condition() is true, and no longer than limit.

        The limit is required. A bench that waits forever is a bench
        that hangs continuous integration.
        """
        limit = int(limit)
        for taken in range(limit + 1):
            self.eval()
            if condition():
                return Cycles(taken)
            if taken == limit:
                break
            self.tick(1, clock)
        raise SimError(f'waited {limit} clocks in {self.top.name} and the '
                       'condition never came true')

    tick_until = until

    def run (self, duration = None, ticks = None):
        """Advance wall-display time, firing each add_clock domain.

        duration is seconds. ticks=N is sugar for tick(N) on the
        default clock. Edges that land at the same instant take theirs
        together, before any of them commits.

        Each clock keeps its phase across calls, so run(a) then run(b)
        is run(a + b). Restarting every clock at the moment of the call
        meant a run shorter than a period fired nothing at all, and two
        short runs fired nothing twice.
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
        for c, p in clocks.items():
            self._next_edge.setdefault(c, self.time + p)
        next_t = {c: self._next_edge[c] for c in clocks}
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
                self._next_edge[c] = next_t[c]

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
            self._native.clock_group(group)
        self.cycle += 1
        self.time = t
        self._fired_now = list(group)
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
            if self._clock_fall:
                self._falls(max(self._clock_fall.values()) + 1)
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

    def _define_scope (self, module, prefix, clock_of = None):
        """clock_of maps a name in this module to the design's clock it
        carries, so a child's i_clock is drawn as the parent's."""
        if clock_of is None:
            clock_of = {name: name for name in self.domains}
        for p in module.ports:
            self._define_field(prefix, p.name, p.width, p.array,
                               clock_of.get(p.name))
        for s in module.signals:
            self._define_field(prefix, s.name, s.width, s.array,
                               clock_of.get(s.name))
        for inst in module.instances:
            child = self.by_name[inst.module]
            inner = {}
            for formal, actual in inst.ports.items():
                if actual is None or getattr(actual, 'op', None) != 'ref':
                    continue
                outer = alias_source(module, actual.value)
                if outer in clock_of:
                    inner[formal] = clock_of[outer]
                elif actual.value in clock_of:
                    inner[formal] = clock_of[actual.value]
            self.vcd.push_scope(inst.name)
            self._define_scope(child, f'{prefix}{inst.name}.', inner)
            self.vcd.pop_scope()

    def _define_field (self, prefix, name, width, array, clock = None):
        if array:
            for i in range(array):
                self._define_one(prefix, f'{name}[{i}]', width)
        else:
            self._define_one(prefix, name, width, clock)

    def _define_one (self, prefix, name, width, clock = None):
        hier = prefix + name
        if not _wanted(hier, self.traces):
            return
        ident = self.vcd.wire(name, width)
        self._vcd_wires.append((ident, hier, width))
        if clock is not None:
            self._vcd_clocks.append((ident, clock))

    def _dump (self):
        if self.vcd is not None:
            self._falls(self.time)
            for c in self._fired_now:
                self._clock_value[c] = 1
                half = self._period(c) // 2
                if half >= 1:
                    self._clock_fall[c] = self.time + half
            self.vcd.time(self.time)
            self._vcd_emit()
        if self.log_fp is not None:
            self._log_emit()
        self._fired_now = []

    def _falls (self, until):
        """Take each clock low again half a period after its edge.

        The model has no clock net. tick() runs the clocked processes
        and commits them; it never toggles a pin, and set() on a clock
        is refused, because a pin that moved on Verilator and not on
        the other two is how the three backends stop agreeing.

        A waveform still needs the wave, and nothing has to be
        invented to draw one: the simulator knows the instant of every
        edge it took and the period add_clock gave that clock. So the
        trace is high at each edge and low half a period later. Every
        rising edge in the file is a cycle the model really ran, which
        is the only claim it makes.
        """
        while True:
            due = [(t, c) for c, t in self._clock_fall.items() if t < until]
            if not due:
                return
            t, c = min(due)
            del self._clock_fall[c]
            self._clock_value[c] = 0
            self.vcd.time(t)
            for ident, clock in self._vcd_clocks:
                if clock == c:
                    self.vcd.change(ident, 0, 1)

    def _vcd_emit (self):
        values = {hier: value for hier, width, value in self._live()}
        drawn = dict(self._vcd_clocks)
        for ident, hier, width in self._vcd_wires:
            if ident in drawn:
                self.vcd.change(ident,
                                self._clock_value.get(drawn[ident], 0), 1)
            elif hier in values:
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


def _default_clock (domains, modules):
    """The clock a bench means when it names none.

    It has to be a clock of the top block. This used to walk every
    module in the hierarchy, leaves first, and take the first flop's
    clock it found, which in any design with a clocked child is the
    child's port name: a cpu_core whose own clock is i_clock0 took
    i_clock from the a multiply step inside it. Nothing raised, because a
    period registered under a name no top-level clock has still falls
    back to the default period, so add_clock kept working and only
    something that had to match the name up with a real clock - the
    VCD's clock trace - could tell.
    """
    if domains:
        return domains[0]
    for m in modules:
        for p in m.processes:
            if p.kind == 'ff' and p.clock:
                return p.clock
    return None


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
        self.widths = {p.name: p.width for p in self.top.ports}
        self.widths.update({sig.name: sig.width for sig in self.top.signals})
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
        self._edge_by = {}
        for clk in hierarchy_clocks(self.top, self.by_name):
            fn = getattr(self.lib, f'{name}_clock_{clock_id(clk)}', None)
            if fn is not None:
                fn.argtypes = [POINTER(self.ctype)]
                fn.restype = c_int
                self._clock_by[clk] = fn
            edge = getattr(self.lib, f'{name}_edge_{clock_id(clk)}', None)
            if edge is not None:
                edge.argtypes = [POINTER(self.ctype)]
                edge.restype = None
                self._edge_by[clk] = edge
        self._assert_failed = getattr(self.lib, 'iso_assert_failed', None)
        self._assert_message = getattr(self.lib, 'iso_assert_message', None)
        self._assert_clear = getattr(self.lib, 'iso_assert_clear', None)
        if self._assert_failed is not None:
            self._assert_failed.restype = c_int
            self._assert_message.restype = c_char_p
        self._settle = getattr(self.lib, name + '_settle', None)
        if self._settle is not None:
            self._settle.argtypes = [POINTER(self.ctype)]
            self._settle.restype = c_int
        self._init(byref(self.state))

    def eval (self):
        if self._eval(byref(self.state)) != 0:
            raise SimError(f'combinational loop in {self.top.name}')
        self._check_asserts()

    def _check_asserts (self):
        """Raise if an assert fired anywhere in the last settle."""
        if self._assert_failed is None or not self._assert_failed():
            return
        text = self._assert_message()
        self._assert_clear()
        raise SimError(text.decode('ascii', 'replace')
                       if isinstance(text, bytes) else str(text))

    def clock (self, clock = None):
        if clock is None:
            r = self._clock(byref(self.state))
        elif clock in self._clock_by:
            r = self._clock_by[clock](byref(self.state))
        else:
            known = ', '.join(sorted(self._clock_by)) or 'none'
            raise SimError(
                f'{clock!r} is not a clock of {self.top.name}; it has '
                f'{known}. Clocking every domain on an unknown name is '
                'how a multi-clock design quietly passes.')
        if r != 0:
            raise SimError(f'combinational loop in {self.top.name}')
        self._check_asserts()

    def clock_group (self, clocks):
        """Several clocks whose edges land at the same time.

        Every domain takes its edge before any of them commits, so a
        flop that samples another domain sees the value it had before
        this instant. Doing them one at a time would let the second
        domain read the first domain's new value, which is the one
        thing a clock-domain crossing must never see.
        """
        names = list(clocks)
        if len(names) == 1:
            return self.clock(names[0])
        if self._settle is None or not all(n in self._edge_by
                                           for n in names):
            raise SimError(
                'this C99 model has no grouped edge; rebuild it')
        for name in names:
            self._edge_by[name](byref(self.state))
        if self._settle(byref(self.state)) != 0:
            raise SimError(f'combinational loop in {self.top.name}')
        self._check_asserts()
        return None

    def tick (self):
        if self._tick(byref(self.state)) != 0:
            raise SimError(f'combinational loop in {self.top.name}')

    def get (self, path):
        obj, base, idx = self._resolve(path)
        val = getattr(obj, base)
        if idx is not None:
            return int(val[idx])
        if (isinstance(val, (list, tuple))
                or (hasattr(val, '__len__') and not isinstance(val, int))):
            try:
                return [int(val[i]) for i in range(len(val))]
            except TypeError:
                pass
        return int(val)

    def set (self, path, value):
        """A value wider than the signal keeps its low bits.

        A C struct field takes whatever fits in its own type, so
        setting 2 on a one-bit port stored 2 here and 0 on the other
        two backends, which is a disagreement about the bench rather
        than about the design and just as confusing."""
        obj, base, idx = self._resolve(path)
        field = getattr(obj, base)
        width = self.widths.get(path.split('.')[-1].split('[')[0])
        value = int(value)
        if width:
            value &= (1 << width) - 1
        if idx is not None:
            field[idx] = value
            return
        setattr(obj, base, value)

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
        out.append((name + '__nxt', typ))
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
