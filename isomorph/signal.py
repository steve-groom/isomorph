"""Elaboration-time objects: signals, types and the process decorators.

Nothing here simulates. A Signal knows its width, kind and name; the
process decorators record a Python function for the analyser to read as
an AST; assign() records an expression the same way. Section 3 of
SPEC.txt is the reference."""
import ast
import dis
import sys
from types import SimpleNamespace


class IsomorphError(Exception):
    pass


def _caller_frame ():
    # The first frame outside this file: whoever wrote the declaration.
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    return frame


def _caller_line ():
    # The source line of the declaration that created an object, for
    # comments and declaration order: the first frame outside this file.
    frame = _caller_frame()
    return frame.f_lineno if frame is not None else 0


_sources = {}


def _source_of (path):
    """The text and AST of a file that called us, parsed once."""
    if path not in _sources:
        try:
            with open(path, encoding = 'utf-8') as handle:
                text = handle.read()
            _sources[path] = (text, ast.parse(text))
        except (OSError, SyntaxError, ValueError):
            _sources[path] = (None, None)
    return _sources[path]


def _call_node (frame):
    """The ast.Call for the call being executed in `frame`.

    Python 3.11 carries a column range per instruction, so the call
    site is found exactly rather than by looking for the only call on
    the line. Two signal() calls on one line are told apart.
    """
    text, tree = _source_of(frame.f_code.co_filename)
    if tree is None:
        return None, None
    position = None
    for instruction in dis.get_instructions(frame.f_code):
        if instruction.offset == frame.f_lasti:
            position = instruction.positions
            break
    if position is None or position.lineno is None:
        return None, None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and node.lineno == position.lineno
                and node.col_offset == position.col_offset):
            return node, text
    return None, None


def _written_as (index = 0, keyword = None):
    """The source text of one argument of the call being elaborated.

    A width written as an expression over the block's parameters is
    kept as that expression, so signal(WIDTH - 1) can reach the HDL as
    [WIDTH-2:0] rather than as the [6:0] one elaboration happened to
    produce. SPEC 3.6 already says a slice bound is emitted as written;
    this is the same rule for a declaration.

    None when there is nothing worth carrying: no source to read, or a
    plain number, which says no more than the width already does.
    """
    frame = _caller_frame()
    if frame is None:
        return None
    node, text = _call_node(frame)
    if node is None:
        return None
    argument = None
    if index < len(node.args) and not isinstance(node.args[index],
                                                 ast.Starred):
        argument = node.args[index]
    if argument is None and keyword is not None:
        for item in node.keywords:
            if item.arg == keyword:
                argument = item.value
    if argument is None or isinstance(argument, ast.Constant):
        return None
    written = ast.get_source_segment(text, argument)
    if written is None:
        return None
    return ' '.join(written.split())


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

    # a member reads as its value, so sim.get(fsm) == 4 still holds,
    # and prints as its name, which is what a waveform shows
    def __eq__ (self, other):
        if isinstance(other, EnumMember):
            # by name and value, not by identity: each elaboration
            # builds its own EnumType, so two runs of one design would
            # otherwise disagree about their own states
            return self.name == other.name and self.value == other.value
        if isinstance(other, int) and not isinstance(other, bool):
            return self.value == other
        return NotImplemented

    def __hash__ (self):
        return hash(self.value)

    def __int__ (self):
        return self.value

    def __index__ (self):
        return self.value


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
            # One bit per state, with bit 0 inverted: the first state
            # is all zeros and every other state carries bit 0 as well
            # as its own.
            #
            # The first state declared encodes as zero in every
            # encoding here, and the reason is reset. A full reset
            # clears every flop, and on an ASIC that is all a reset
            # does, so a cleared machine has to sit in a state the
            # design named rather than an illegal one. sequential,
            # gray and johnson give the first member zero already;
            # one_hot was the only one that did not.
            #
            # It keeps what one-hot is for. Bit k is set in state k and
            # nowhere else, so every state but the first still decodes
            # on one bit, and the first decodes on bit 0 being low,
            # which is also one bit. Both fitters here build exactly
            # this, so the netlist agrees with the source (SPEC 5.4).
            self.width = max(1, n)
            for index, member in enumerate(self.members):
                member.value = 0 if index == 0 else (1 << index) | 1
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

    # how the width was written, when that was an expression over the
    # block's parameters rather than a number (ROADMAP item 9)
    width_expr = None

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
    made = Signal(width, 'bit' if width == 1 else 'vector')
    made.width_expr = _written_as(0, 'width')
    return made


class SignalArray(list):
    """signals(N, W): N signals of W bits, one array port or memory."""

    init = None                 # contents at power-on, from preload()
    init_base = None            # and the base they are written in

    def __init__ (self, count, width):
        super().__init__(Signal(width, kind = 'bit' if width == 1
                                else 'vector') for _ in range(count))
        self.width = width
        self.count = count
        self.count_expr = None
        self.name = None
        self.attributes = {}
        self.line = _caller_line()
        for element in self:
            element.parent = self


def clock (signal, period):
    """Declare a clock's period, for the timing constraints.

    period is in seconds, the same unit add_clock takes, so
    clock(i_clock, period = 20e-9) is fifty megahertz. It is a fact
    about the board rather than about the bench, so it is declared
    here and reaches the emitted .sdc; nothing in the simulators reads
    it, and nothing in the HDL changes because of it.
    """
    if not isinstance(signal, Signal):
        raise IsomorphError('clock() takes a signal and a period')
    seconds = float(period)
    if seconds <= 0:
        raise IsomorphError(
            f'clock(): a period is a positive number of seconds, not '
            f'{period!r}. Fifty megahertz is period = 20e-9.')
    signal.clock_period_ns = seconds * 1e9
    return signal


def array_view (elements):
    """An array port over signals that already exist.

    A slice of a signals() array is an ordinary Python list, and so is
    a list comprehension over one. Both are what a design hands a
    child when it splits an array in half - a reduction tree does it
    at every level - and both arrive here to become a real array port
    rather than a list nothing knows how to name.

    The elements are shared, not copied: the child's port and the
    parent's array are the same wires, which is the whole point.
    """
    elements = list(elements)
    widths = {e.width for e in elements}
    if len(widths) != 1:
        raise IsomorphError(
            'an array port holds one width, and this one was given '
            + ', '.join(str(w) for w in sorted(widths))
            + '. Elements of one array share a width (SPEC 5.15).')
    view = SignalArray.__new__(SignalArray)
    list.__init__(view, elements)
    view.width = elements[0].width
    view.name = None
    view.attributes = {}
    view.line = _caller_line()
    return view


class Hexed (int):
    """A bit pattern that happens to be held in an int.

    A parameter written 0xC0FFEE carries its base in the source and
    the emitters find it there. One a function worked out does not:
    four characters packed into a word arrive as 1430344276, and
    nobody decodes that back to what was typed. A parameter holding
    one of these is a vector in the emitted HDL rather than an
    integer, which is what it is.
    """

    bits = None


def hexed (value, bits = None):
    """Tag a value as a bit pattern `bits` wide, to be emitted as one."""
    out = Hexed(int(value))
    out.bits = bits
    return out


def preload (array, values, base = 'dec'):
    """The contents a memory holds when the device is configured.

    This is not the power-on value that signal() used to take and no
    longer does. That one set a register in two simulators and in no
    emitted HDL, which is a lie. This one is emitted: it becomes a
    declaration initialiser in the SystemVerilog and in the VHDL, which
    is how every FPGA tool loads a block RAM at configuration, and the
    Python and C99 backends preload the same values. All four agree.

    It is for a memory image: a boot ROM, a program, a lookup table. A
    device that has to come up in a particular state still does that in
    a reset branch, because a flip-flop is not configured, it is reset.

        rom = signals(1024, 32)
        preload(rom, [instruction(n) for n in range(1024)])

    `base` is how the words are written in the emitted HDL, and it is
    the author who knows: a program is read in hex and a sine table in
    decimal, and neither is readable as the other. It is 'dec', 'hex'
    or 'bin', and it reaches both languages the same way a constant's
    base already does.

        preload(rom, contents(IMAGE, WORDS), base = 'hex')
    """
    if not isinstance(array, SignalArray):
        raise IsomorphError('preload() takes a signals() array, not '
                            f'{array!r}')
    values = [int(v) for v in values]
    if len(values) != len(array):
        raise IsomorphError(
            f'preload(): {len(values)} values for an array of '
            f'{len(array)}. A memory image is the whole memory, so pad '
            'it or size the array to what you have.')
    limit = 1 << array.width
    for index, value in enumerate(values):
        if not 0 <= value < limit:
            raise IsomorphError(
                f'preload(): {value} at index {index} does not fit in '
                f'{array.width} bits')
    if base not in ('dec', 'hex', 'bin'):
        raise IsomorphError(f"preload(): base is 'dec', 'hex' or 'bin', "
                            f'not {base!r}')
    array.init = values
    array.init_base = base
    return array


def signals (count, width = 1, style = None):
    array = SignalArray(count, width)
    written = _written_as(1, 'width')
    # how many, as written, so signals(STAGES, W) declares STAGES of
    # them in the HDL rather than the number this build happened to
    # elaborate with
    array.count_expr = _written_as(0, 'count')
    array.width_expr = written
    for element in array:
        element.width_expr = written
    if style:
        array.attributes['ram_style'] = style
    return array


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


def ones (width):
    """A run of `width` ones.

    replicate() repeats a pattern, which is what it is for: two of
    0b1011 is 0b10111011. A string of ones is not a pattern, and
    replicate(True, STAGES) says so awkwardly. Both languages have a
    word for this and neither of them counts: SystemVerilog writes
    \'1 and VHDL (others => \'1\'). This is that word, with the width
    said out loud so a reader does not have to find the declaration.
    """
    raise IsomorphError('ones() is read from the AST, not executed')


def zeroes (width):
    """A run of `width` zeroes, the counterpart of ones().

    A bare 0 is sized to the width this build happened to have, so a
    register declared signal(WIDTH) is cleared with 4\'d0 and the
    vendor reads a width mismatch the moment WIDTH is anything else.
    This one says the width, so there is nothing to mismatch.
    """
    raise IsomorphError('zeroes() is read from the AST, not executed')


def sign_extend (value, width):
    """value, sign-extended to `width` bits.

    The second argument is the width of the answer. That is the whole
    point of it, and the reason it is not simply replicate() renamed:

        next_rt.next = sign_extend(instr[2:0], WIDTHD)

    against what you had to write before,

        concat(replicate(instr[2], WIDTHD - 3), instr[2:0])

    where the reader has to check that the repeated bit is the slice's
    top one, that the slice is three wide, and that WIDTHD - 3 still
    matches if the slice ever changes. Widen instr[2:0] to instr[3:0]
    and the old line is quietly wrong by a bit; the new one cannot be.

    A one-bit value extended to n bits is n copies of it, so the old
    spelling keeps working under the new name. Extending to the width
    it already has returns it unchanged, and asking for fewer bits
    than it has is an error rather than a silent truncation.
    """
    raise IsomorphError('sign_extend() is read from the AST, not executed')


def bits (pattern):
    raise IsomorphError('bits() is read from the AST, not executed')


def const (value, width):
    """A number that carries its own width.

    A bare number takes the width of wherever it is used, which is
    right nearly everywhere and impossible in the one place it is not:
    a shift has no context to take, so `1 << i_sel` has nothing to
    happen in. const(1, NOPS) is the one-hot that idiom wanted.

    Python already has the bases - 0b1010_1010, 0xDEAD_BEEF, 200, -5,
    underscores and all - so the only thing added here is the width,
    and the base you wrote is the base emitted. A negative value is
    the two's complement pattern in that width, and .signed() at the
    point of use decides how it is read, which is the rule the rest of
    the language follows.
    """
    raise IsomorphError('const() is read from the AST, not executed')


# Processes and child instances made while a block is elaborating, so
# instances() can tell whether every one of them is still reachable.
made_stack = []


def note_made (thing):
    if made_stack:
        made_stack[-1].append(thing)


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
        note_made(self)


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

    reason is compulsory. Every use is announced as a warning at
    conversion, naming the process, the reset and the reason, and the
    reason is emitted as a comment above the process in both
    languages. It is not severe: the construct refuses to build
    without a justification, so every one that converts is a circuit
    whose author has already said why, and reporting that as a fault
    on every run would only teach the reader to skip the word where
    it means a latch.
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


def _reachable (value, kept, depth = 0):
    """Everything a block-local name still holds on to.

    A list of instances is one legitimate way to hold several under
    one name, and a namespace is the other: the one pipeline() returns
    holds instances and the link bundles between them, and a bundle
    holds signals. Those are walked, to a depth no design reaches.
    Anything else with attributes is looked in one level, as before,
    and no further, because a function or a module has attributes too
    and nobody wants them walked."""
    kept.add(id(value))
    if depth > 8:
        return
    if isinstance(value, (list, tuple)):
        for element in value:
            _reachable(element, kept, depth + 1)
    elif isinstance(value, SimpleNamespace):
        for element in vars(value).values():
            _reachable(element, kept, depth + 1)
    elif depth == 0 and hasattr(value, '__dict__'):
        for element in vars(value).values():
            kept.add(id(element))


class Instances:
    """What instances() returns: a snapshot of the block's locals."""

    def __init__ (self, frame_locals):
        self.locals = dict(frame_locals)


def instances ():
    """The block's locals, and a check that none of its hardware was
    lost on the way here.

    instances() reads the frame's locals, so a name rebound to
    something else takes the first object with it and the hardware
    quietly disappears. MyHDL had the same trap and it cost people
    days: writing `inst = child(...)` twice, or reusing a process name
    in a loop, removes the first one and converts without a word."""
    frame_locals = sys._getframe(1).f_locals
    if made_stack:
        kept = set()
        for value in frame_locals.values():
            _reachable(value, kept)
        lost = [t for t in made_stack[-1] if id(t) not in kept]
        if lost:
            what = []
            for thing in lost:
                kind = ('process' if isinstance(thing, Process)
                        else 'instance')
                where = getattr(thing, 'line', 0) or getattr(
                    getattr(thing, 'func', None), '__name__', '?')
                what.append(f'a {kind} made at line {where}')
            raise IsomorphError(
                'this block lost ' + ', '.join(what)
                + '. instances() collects what the block\'s names still '
                'point at, so binding a name twice throws the first one '
                'away and the hardware goes with it. Give each process '
                'and each instance its own name.')
    return Instances(frame_locals)
