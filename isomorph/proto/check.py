"""Protocol checkers on Simulator: traps illegal bus use (IFACE_PLAN)."""
from ..execute import SimError


class ProtocolError(SimError):
    def __init__ (self, check, rule, message, cycle):
        self.check = check
        self.rule = rule
        self.cycle = cycle
        super().__init__(
            f'{check} {rule} at cycle {cycle}: {message}')


_ABSENT = object()


class Check:
    """One protocol monitor. sample(sim, when) with when='edge'|'comb'."""

    name = 'check'

    def __init__ (self, prefix = '', pins = None, strict = True):
        self.prefix = prefix
        self.pins = dict(pins or {})
        self.strict = strict
        self.violations = []

    def pin_name (self, key):
        if key in self.pins:
            return self.pins[key]
        if self.prefix:
            return f'{self.prefix}_{key}'
        return key

    def get (self, sim, key, default = _ABSENT):
        name = self.pin_name(key)
        if name is None:
            return None if default is _ABSENT else default
        try:
            return int(sim.get(name))
        except SimError:
            if default is _ABSENT:
                raise
            return default

    def has (self, sim, key):
        name = self.pin_name(key)
        if name is None:
            return False
        try:
            sim.get(name)
            return True
        except SimError:
            return False

    def sample (self, sim, when):
        return

    def fail (self, sim, rule, message):
        cycle = getattr(sim, 'cycle', 0)
        event = {
            'kind': 'protocol',
            'check': self.name,
            'rule': rule,
            'cycle': cycle,
            'message': message,
        }
        self.violations.append(event)
        events = getattr(sim, '_proto_events', None)
        if events is not None:
            events.append(event)
        if self.strict:
            raise ProtocolError(self.name, rule, message, cycle)
