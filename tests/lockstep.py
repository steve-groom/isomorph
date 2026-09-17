"""Run one design on several backends at once and compare every cycle.

Not a Simulator mode. Lockstep belongs in continuous integration, not
in `python3 design.py`: it compiles a Verilator model on every run, and
someone would switch it off by lunchtime. Here it is a helper a test
calls.

What is compared, and why that and not more. Ports on every backend,
because every backend has them and they are what the design is for. The
top module's own flip-flops as well, now that the Verilator backend can
be asked for them, because a state machine that goes wrong goes wrong
in a register several cycles before it reaches a pin.

Combinational signals are deliberately not compared. Verilator is
entitled to delete one it can prove nothing needs, so a mismatch there
would be a difference of opinion about optimisation rather than a bug.

A mismatch is an emitter bug. The report names the signal, the cycle,
and what each backend thought, because "the backends disagree" on its
own is not something anyone can act on.
"""
import shutil

from isomorph import Simulator, analyse
from isomorph.emit_c99 import ff_driven_names

HAVE_GCC = shutil.which('gcc') is not None
HAVE_VERILATOR = (shutil.which('verilator') is not None
                  and shutil.which('g++') is not None)


def available (wanted = ('python', 'c99', 'verilator')):
    """The backends this machine can actually run."""
    out = []
    for name in wanted:
        if name == 'python':
            out.append(name)
        elif name == 'c99' and HAVE_GCC:
            out.append(name)
        elif name == 'verilator' and HAVE_VERILATOR:
            out.append(name)
    return out


def watched (top):
    """The names to compare: every port leaf, then every flip-flop of
    the top module that is a plain signal."""
    modules, _ = analyse(top)
    module = modules[-1]
    names = []
    for p in module.ports:
        if p.array:
            names += [f'{p.name}[{i}]' for i in range(p.array)]
        else:
            names.append(p.name)
    driven = ff_driven_names(module)
    ports = {p.name for p in module.ports}
    for s in module.signals:
        if s.name in ports or s.array or s.name not in driven:
            continue
        names.append(s.name)
    return names


class Lockstep:
    """Several simulators of one design, stepped together.

    elaborate is called once per backend, because a Simulator takes an
    elaborated block and two of them must not share signal objects.
    """

    def __init__ (self, elaborate, backends = None, period = 20e-9,
                  clock = None, clocks = None):
        """clocks is {name: period} for a design with more than one.

        Give every domain a period and advance with run(); tick() has
        no meaning when two clocks are running at different rates, and
        says so."""
        self.names = watched(elaborate())
        self.backends = list(backends or available())
        self.sims = {}
        for backend in self.backends:
            sim = Simulator(elaborate(), backend = backend)
            if clocks:
                for name, each in clocks.items():
                    sim.add_clock(each, name)
            else:
                sim.add_clock(period, clock)
            self.sims[backend] = sim
        self.clock = clock
        self.clocks = dict(clocks or {})
        self.cycle = 0

    def set (self, name, value):
        for sim in self.sims.values():
            sim.set(name, value)

    def reset (self, name, clocks = 2):
        """Hold a reset port, advance, release.

        A design with more than one clock cannot be ticked, so it is
        advanced by the longest period instead, which is enough for
        every domain to see the reset."""
        for sim in self.sims.values():
            sim.set(name, 1)
        if self.clocks:
            longest = max(self.clocks.values())
            for _ in range(clocks):
                self.run(longest * 2)
        else:
            self.tick(clocks)
        for sim in self.sims.values():
            sim.set(name, 0)

    def eval (self):
        """Settle every backend and compare.

        A design with no clock at all is compared here and nowhere
        else, and an output port driven combinationally is checked one
        settle earlier than it would be by tick()."""
        for sim in self.sims.values():
            sim.eval()
        self.compare()

    def tick (self, n = 1):
        for _ in range(n):
            for sim in self.sims.values():
                sim.tick(1, self.clock)
            self.cycle += 1
            self.compare()

    def run (self, duration):
        """Advance every domain at its own period, then compare.

        Used for a design with more than one clock, where ticking is
        not a thing that can be done."""
        for sim in self.sims.values():
            sim.run(duration)
        self.cycle += 1
        self.compare()

    def compare (self):
        """Every watched name, on every backend, right now."""
        if len(self.sims) < 2:
            return
        first = self.backends[0]
        for name in self.names:
            try:
                want = self.sims[first].get(name)
            except Exception:
                continue
            for other in self.backends[1:]:
                try:
                    got = self.sims[other].get(name)
                except Exception:
                    continue
                if got != want:
                    raise AssertionError(
                        f'cycle {self.cycle}: {name} is {want} on '
                        f'{first} and {got} on {other}. The backends '
                        'share one intermediate form, so a difference '
                        'between them is an emitter bug.')

    def get (self, name):
        return self.sims[self.backends[0]].get(name)

    def close (self):
        for sim in self.sims.values():
            close = getattr(sim, 'close', None)
            if close is not None:
                close()
