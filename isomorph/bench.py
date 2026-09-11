"""Bench helpers for streams: a source that feeds a list, a sink that
collects one, and the step that drives them.

Ports only. Every helper reads and writes top-level ports through
Simulator.set and get, so a bench written with them runs unchanged on
the Verilator backend, which is the one whose result counts.

This is not a transaction layer and is not going to become one; see
DATAFLOW.md. A source presents a word and holds it until the design
takes it, a sink raises ready and keeps what arrives, and step() puts
the two either side of a clock edge in the right order: drive the
inputs, settle, look at the handshakes that this edge will complete,
take the edge. A stall pattern is a probability with a seed, or a
function of the cycle, so the run that broke a block can be run again.
"""
import random
from types import SimpleNamespace

from .execute import SimError


class Stall:
    """When to hold back.

    None never stalls. A number is the probability of stalling on any
    cycle, drawn from a generator seeded so the pattern repeats. A
    callable is asked with the cycle number and answers True to stall,
    which is how a bench writes 'every third cycle' or 'cycles 10 to
    20' when that is the pattern that matters."""

    def __init__ (self, stall = None, seed = 0):
        if stall is None:
            self.decide = None
        elif callable(stall):
            self.decide = stall
        else:
            chance = float(stall)
            if not 0.0 <= chance < 1.0:
                raise SimError(f'stall = {stall!r}: a probability is 0 '
                               'up to but not including 1')
            rng = random.Random(seed)
            self.decide = lambda cycle: rng.random() < chance

    def __call__ (self, cycle):
        if self.decide is None:
            return False
        return bool(self.decide(cycle))


def port_name (sim, port):
    """The port a bundle is on, as the prefix of its leaf names."""
    if isinstance(port, str):
        return port
    if isinstance(port, SimpleNamespace):
        name = sim.elaborated.port_names.get(id(port))
        if name is None:
            raise SimError('that bundle is not a port of the top block')
        return name
    raise SimError(f'a stream port is a name or a bundle, not {port!r}')


class StreamSource:
    """Feeds `words` into the stream at `port`, one per accepted beat.

    Once a word is presented it stays presented until the design takes
    it, whatever the stall pattern says, because that is the rule of
    the handshake and a source that broke it would be testing nothing.
    The stall decides only whether to present the next word."""

    def __init__ (self, sim, port, words, stall = None, seed = 0):
        self.sim = sim
        self.port = port_name(sim, port)
        self.pending = [int(w) for w in words]
        self.sent = []
        self.stall = Stall(stall, seed)
        self.cycle = 0
        self.presenting = False

    def drive (self):
        if (not self.presenting and self.pending
                and not self.stall(self.cycle)):
            self.presenting = True
        self.sim.set(f'{self.port}_valid', 1 if self.presenting else 0)
        if self.presenting:
            self.sim.set(f'{self.port}_data', self.pending[0])

    def observe (self):
        if self.presenting and self.sim.get(f'{self.port}_ready'):
            self.sent.append(self.pending.pop(0))
            self.presenting = False
        self.cycle += 1

    @property
    def done (self):
        return not self.pending


class StreamSink:
    """Takes beats from the stream at `port` and keeps their data."""

    def __init__ (self, sim, port, stall = None, seed = 0):
        self.sim = sim
        self.port = port_name(sim, port)
        self.received = []
        self.stall = Stall(stall, seed)
        self.cycle = 0
        self.ready = False

    def drive (self):
        self.ready = not self.stall(self.cycle)
        self.sim.set(f'{self.port}_ready', 1 if self.ready else 0)

    def observe (self):
        if self.ready and self.sim.get(f'{self.port}_valid'):
            self.received.append(int(self.sim.get(f'{self.port}_data')))
        self.cycle += 1


class Pulse:
    """A one-cycle strobe on `port`, on the cycle it is asked for.

    A start input is usually one of these: fire() before a step and it
    is high for that step and low after."""

    def __init__ (self, sim, port):
        self.sim = sim
        self.port = port_name(sim, port)
        self.armed = False

    def fire (self):
        self.armed = True

    def drive (self):
        self.sim.set(self.port, 1 if self.armed else 0)
        self.armed = False

    def observe (self):
        return


def step (sim, drivers, cycles = 1, clock = None, edge = True):
    """Drive, settle, observe, take the edge; `cycles` times over.

    Every driver's drive() runs before the settle, so the design sees
    all the inputs of the cycle at once. Every observe() runs after
    it, so a driver sees the handshake this edge is about to complete
    and nothing that only exists after it.

    edge = False settles and observes without a clock edge, for a
    block that has no clock: a combinational join, a decoder. The
    stall patterns still count in steps."""
    for _ in range(int(cycles)):
        for driver in drivers:
            driver.drive()
        sim.eval()
        for driver in drivers:
            driver.observe()
        if edge:
            sim.tick(1, clock)


def run_until (sim, drivers, cond, limit, clock = None, edge = True):
    """step() until cond() holds. The limit is not optional.

    A bench that waits without a limit is a bench that hangs the
    continuous integration run, so there is no form of this without
    one. Returns the number of cycles it took."""
    limit = int(limit)
    for taken in range(limit):
        if cond():
            return taken
        step(sim, drivers, 1, clock, edge)
    if cond():
        return limit
    raise SimError(f'run_until: still not true after {limit} cycles')
