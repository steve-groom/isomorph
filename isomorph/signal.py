"""Elaboration-time objects: signals, types and the process decorators.

Nothing here simulates. A Signal knows its width, kind and name; the
process decorators record a Python function for the analyser to read as
an AST; assign() records an expression the same way. Section 3 of
SPEC.txt is the reference."""
import sys


class IsomorphError(Exception):
    pass


def _caller_line ():
    # The source line of the declaration that created an object, for
    # comments and declaration order: the first frame outside this file.
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    return frame.f_lineno if frame is not None else 0


class Edge:
    def __init__ (self, signal, polarity):
        self.signal = signal
        self.polarity = polarity


class EnumMember:
    def __init__ (self, enum_type, name, value):
        self.type = enum_type
        self.name = name
        self.value = value

    def __repr__ (self):
        return f'{self.type.name}.{self.name}'


class EnumType:
    """state = enum('RESET', 'FETCH', encoding = 'auto'); IDLE = 0 keyword
    members give explicit values."""

    def __init__ (self, names, explicit, encoding):
        self.name = None                # set from the variable name
        self.encoding = encoding
        self.line = _caller_line()
        self.members = []
        value = 0
        for name in names:
            self.members.append(EnumMember(self, name, value))
            value += 1
        for name, given in explicit.items():
            self.members.append(EnumMember(self, name, given))
        for member in self.members:
            setattr(self, member.name, member)
        self._apply_encoding()

    def _apply_encoding (self):
        """SPEC 5.4: auto keeps sequential values and no vendor
        attribute; the named encodings set member values."""
        n = len(self.members)
        enc = self.encoding
        known = ('auto', 'sequential', 'one_hot', 'gray', 'johnson')
        if enc not in known:
            raise IsomorphError(
                f'enum encoding {enc!r} is not one of {", ".join(known)}')
        if enc == 'one_hot':
            self.width = max(1, n)
            for index, member in enumerate(self.members):
                member.value = 1 << index
            return
        if enc == 'gray':
            self.width = max(1, (n - 1).bit_length()) if n else 1
            for index, member in enumerate(self.members):
                member.value = index ^ (index >> 1)
            return
        if enc == 'johnson':
            width = max(1, (n + 1) // 2)
            self.width = width
            value = 0
            mask = (1 << width) - 1
            for member in self.members:
                member.value = value
                msb = (value >> (width - 1)) & 1
                value = ((value << 1) | (msb ^ 1)) & mask
            return
        self.width = max(1, max(m.value for m in self.members).bit_length())

    def __iter__ (self):
        return iter(self.members)


def enum (*names, encoding = 'auto', **explicit):
    return EnumType(names, explicit, encoding)


class StructType:
    def __init__ (self, name, fields):
        self.name = name
        self.fields = dict(fields)      # field -> width
        self.width = sum(self.fields.values())


def struct (name, **fields):
    return StructType(name, fields)


class InterfaceType:
    def __init__ (self, name, roles):
        self.name = name
        self.roles = roles              # role -> {member: 'in'|'out'}


def interface (name, **roles):
    return InterfaceType(name, roles)


class Signal:
    """signal(), signal(W), signal(enum_type), signal(struct_type).
    The name is filled in at elaboration from the variable that holds
    it.

    There is no declared power-on value. Isomorph emits none, by
    design, so a register holds whatever it comes up with until
    something writes it, and a simulator that quietly started it
    somewhere else would be telling you about a design you are not
    going to get. A value you need at power-on is assigned in the reset
    branch, or the state machine walks to it from wherever it starts.

    Arithmetic on a signal is modular, as it is in SystemVerilog: a
    value assigned to a signal is the low bits of what was computed.
    There is no other kind of signal, and no flag to ask for one,
    because the width rules already make you say where a carry goes:
    (count + 1)[7:0] is the only way to write it."""

    def __init__ (self, width = 1, kind = 'vector', type = None):
        self.width = width
        self.kind = kind                # 'bit', 'vector', 'enum', 'struct'
        self.type = type
        self.name = None
        self.attributes = {}
        self.parent = None              # bundle or array that owns it
        self.line = _caller_line()

    def __len__ (self):
        return self.width

    @property
    def posedge (self):
        return Edge(self, 'pos')

    @property
    def negedge (self):
        return Edge(self, 'neg')

    def __repr__ (self):
        return f'signal({self.name or "?"}[{self.width}])'


def signal (width = 1):
    if isinstance(width, EnumType):
        return Signal(width.width, kind = 'enum', type = width)
    if isinstance(width, StructType):
        return Signal(width.width, kind = 'struct', type = width)
    if not isinstance(width, int) or width < 1:
        raise IsomorphError('signal width must be a positive int, '
                            f'not {width!r}')
    return Signal(width, 'bit' if width == 1 else 'vector')


class SignalArray(list):
    """signals(N, W): N signals of W bits, one array port or memory."""

    def __init__ (self, count, width):
        super().__init__(Signal(width, kind = 'bit' if width == 1
                                else 'vector') for _ in range(count))
        self.width = width
        self.name = None
        self.attributes = {}
        self.line = _caller_line()
        for element in self:
            element.parent = self


def signals (count, width = 1, style = None):
    array = SignalArray(count, width)
    if style:
        array.attributes['ram_style'] = style
    return array


class Bundle:
    """An interface port of one role: members are signals."""

    def __init__ (self, kind, role):
        self.kind = kind
        self.role = role
        self.name = None
        for member, direction in kind.roles[role].items():
            member_signal = Signal(1) if direction in ('in', 'out') else None
            setattr(self, member, member_signal)


def interfaces (count, kind, role):
    return [Bundle(kind, role) for _ in range(count)]


class OpenPort:
    def __init__ (self, width = 1):
        self.width = width


def open_port (width = 1):
    return OpenPort(width)


def attr (target, **attributes):
    target.attributes.update(attributes)
    # Remember where it was written so a comment on the attr() line
    # reaches the signal it attributes, rather than being dropped.
    lines = getattr(target, 'attr_lines', None)
    if lines is None:
        try:
            target.attr_lines = [_caller_line()]
        except AttributeError:
            pass
    else:
        lines.append(_caller_line())
    return target


class Vector:
    """vector(W): a local value inside a converted function."""

    def __init__ (self, width):
        self.width = width


def vector (width):
    return Vector(width)


def concat (*parts):
    raise IsomorphError('concat() is read from the AST, not executed')


def replicate (value, count):
    raise IsomorphError('replicate() is read from the AST, not executed')


def bits (pattern):
    raise IsomorphError('bits() is read from the AST, not executed')


class Process:
    def __init__ (self, kind, func, clock = None, polarity = 'pos'):
        self.kind = kind                # 'comb' or 'ff'
        self.func = func
        self.clock = clock
        self.polarity = polarity
        self.reset = None               # asynchronous reset signal, or None
        self.reset_polarity = 'pos'
        self.reason = None              # why this flop needs one
        self.name = func.__name__
        self.attributes = {}
        self.line = func.__code__.co_firstlineno


def always_comb (func):
    return Process('comb', func)


def always_ff (*edges):
    if len(edges) != 1 or not isinstance(edges[0], Edge):
        raise IsomorphError(
            'always_ff takes one clock edge. A flop with an asynchronous '
            'reset is always_ff_async_reset(clock, reset, reason = ...), '
            'which is for reset synchronisers and dual-clock FIFO '
            'pointers, not for general logic.')
    edge = edges[0]

    def decorate (func):
        return Process('ff', func, edge.signal, edge.polarity)
    return decorate


def always_ff_async_reset (*edges, reason = None):
    """A flop with an asynchronous reset. Deliberately long to type.

    General logic uses a synchronous reset: it needs no extra routing,
    it does not create a recovery/removal timing check, and it cannot
    glitch a register out of a state the rest of the design believes
    it is in. This construct exists for the two cases that cannot:
    a reset synchroniser, which has no running clock to sample the
    release on, and a pointer or flag crossing in a dual-clock FIFO.

    Every use is a severe warning at conversion, naming the process,
    the reset and the reason, and the reason is emitted as a comment
    above the process in both languages.
    """
    if len(edges) != 2 or not all(isinstance(e, Edge) for e in edges):
        raise IsomorphError(
            'always_ff_async_reset takes two edges: the clock first, then '
            'the asynchronous reset, e.g. always_ff_async_reset('
            'i_clock.posedge, i_areset.posedge, reason = "...")')
    clock, reset = edges
    if clock.signal is reset.signal:
        raise IsomorphError(
            'always_ff_async_reset: the clock and the asynchronous reset '
            'are the same signal')
    if not isinstance(reason, str) or not reason.strip():
        raise IsomorphError(
            'always_ff_async_reset needs reason = "..." saying why this '
            'flop cannot take a synchronous reset. It is emitted as a '
            'comment in the HDL and printed at conversion, so write it '
            'for the next reader.')

    def decorate (func):
        p = Process('ff', func, clock.signal, clock.polarity)
        p.reset = reset.signal
        p.reset_polarity = reset.polarity
        p.reason = reason.strip()
        return p
    return decorate


class Assign:
    def __init__ (self, target, expression):
        self.target = target
        self.expression = expression    # a lambda, read as an AST
        self.name = None
        self.line = expression.__code__.co_firstlineno


# Blocks being elaborated, innermost last; assign() registers with the
# innermost since an assign statement is not bound to a local name.
elaboration_stack = []


def assign (target, expression):
    if not callable(expression):
        raise IsomorphError('assign(target, lambda: expression)')
    if not elaboration_stack:
        raise IsomorphError('assign() is used inside a block')
    a = Assign(target, expression)
    elaboration_stack[-1].append(a)
    return a


class Instances:
    """What instances() returns: a snapshot of the block's locals."""

    def __init__ (self, frame_locals):
        self.locals = dict(frame_locals)


def instances ():
    return Instances(sys._getframe(1).f_locals)
