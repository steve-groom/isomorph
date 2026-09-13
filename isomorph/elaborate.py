"""Run a block once and collect what it declared: ports, signals,
constants, enumerations, functions, processes, continuous assignments
and child instances. Section 3.2, 3.8 and 4.1 of SPEC.txt."""
import functools
import hashlib
import inspect
import sys
from types import FunctionType, SimpleNamespace

from .signal import (Signal, SignalArray, EnumType, StructType,
    Process, Assign, Instances, OpenPort, IsomorphError, array_view,
    elaboration_stack, made_stack, note_made)
from .blackbox import Blackbox


# past this a module name is a digest of its parameters instead
NAME_LIMIT = 60


def _name_part (value):
    """One parameter value, as part of an identifier.

    A number is itself. A string is not: a file path has slashes and
    dots in it, and putting one in a module name makes an identifier no
    language accepts and a filename that is a directory away from where
    it was meant to go. A short digest keeps two different images in
    two different modules without pretending the name is readable.
    """
    if isinstance(value, bool) or isinstance(value, int):
        return str(value)
    text = str(value)
    if text.isidentifier():
        return text
    digest = hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]
    return digest


def _join_name (block_name, parts):
    """A block name with the values that tell this build from another.

    Single underscores: VHDL forbids consecutive ones, and SPEC 4.5
    keeps the same identifier in SystemVerilog, VHDL and C99. Four
    thirty-two bit constants spell out to seventy-three characters,
    which puts a VHDL instantiation past the column the house style
    keeps to and makes a file name nobody can read, so past the limit
    the parts become a digest of themselves: still one module per
    distinct build, and short.
    """
    if not parts:
        return block_name
    tail = '_'.join(f'{n}_{_name_part(v)}' for n, v in parts)
    name = f'{block_name}_{tail}'
    if len(name) <= NAME_LIMIT:
        return name
    digest = hashlib.sha1(tail.encode('utf-8')).hexdigest()[:8]
    return f'{block_name}_p{digest}'


def _kind_key (value):
    """What a port's kind changes about the declared type, or None.

    An enumeration or a struct is declared as its named type, so two
    builds given different ones are two modules. Nothing else counts:
    a one-bit port is `logic` and `std_logic` whether it was made by
    signal() and called a bit or by open_port(), which builds its
    stand-in as a vector. Splitting a module over that would give a
    block two names because the parent did not use one of its
    outputs.
    """
    if value.kind in ('enum', 'struct'):
        return (value.kind, getattr(value.type, 'name', None))
    return None


def _shape_part (name, value):
    """One port's contribution to a block's shape.

    The widths a module would be built with, under the names the port
    map uses, so two builds can be compared without emitting either."""
    if isinstance(value, Signal):
        return ((name, value.width, _kind_key(value)),)
    if isinstance(value, SignalArray):
        return ((name, value.width, ('array', len(value))),)
    if isinstance(value, SimpleNamespace):
        out = []
        for member, element in vars(value).items():
            if isinstance(element, Signal):
                out += list(_shape_part(f'{name}_{member}', element))
        return tuple(out)
    if isinstance(value, (list, tuple)):
        out = []
        for index, element in enumerate(value):
            out += list(_shape_part(f'{name}[{index}]', element))
        return tuple(out)
    return ((name, None, None),)


def _separating (order, values):
    """The fewest of these that tell every build apart, or None.

    `values` maps a name to its value per build. One name is tried
    before two, so a block built at two widths is called after the
    width and not after everything that followed from it.
    """
    names = sorted(values)
    for name in names:
        if len({values[name][i] for i in range(len(order))}) == len(order):
            return [name]
    if len({tuple(values[n][i] for n in names)
            for i in range(len(order))}) == len(order):
        return names
    return None


def _tell_apart (block_name, shapes):
    """One name per distinct module built from one block.

    A block elaborated twice with different parameters or different
    port widths is two modules in two files, and they cannot share a
    name. The name is built from what the author wrote, and only as
    much of it as it takes to tell the modules apart. Three rungs, in
    the order a reader would look:

    the parameters that differ from their defaults, because those are
    written at the instantiation; then the block's own constants that
    differ, which is where the house idiom puts the width when it
    takes it from len(i_data) and there is no parameter at all; then
    the ports themselves, which is all that is left for a block that
    computes nothing and simply passes a bus through.

    Nothing is invented. Every part of every name is a name the author
    typed and a value the design really built, never an ordinal: the
    cells_0 of SPEC 4.5 is what this exists to avoid.
    """
    order = list(shapes)
    names = {s: shapes[s][0]._built_name for s in order}
    if len(set(names.values())) == len(order):
        return names

    nodes = [shapes[s][0] for s in order]
    base = [[(n, v) for n, v in node.parameters.items()
             if _differs_from_default(node, n, v)] for node in nodes]

    def build (extra):
        out = {}
        for index, shape in enumerate(order):
            out[shape] = _join_name(block_name, base[index] + extra[index])
        return out

    # the block's own constants: WIDTH = len(i_data) and its kin
    constants = {}
    for name in set().union(*(set(node.constants) for node in nodes)):
        column = [node.constants.get(name) for node in nodes]
        if (len({repr(v) for v in column}) > 1
                and all(isinstance(v, int) and not isinstance(v, bool)
                        for v in column)):
            constants[name] = column
    chosen = _separating(order, constants)
    if chosen:
        picked = build([[(n, constants[n][i]) for n in chosen]
                        for i in range(len(order))])
        if len(set(picked.values())) == len(order):
            return picked

    # the ports, for a block that computes nothing of its own
    ports = {}
    columns = [dict((part[0], part[1]) for part in shape[2])
               for shape in order]
    for name in set().union(*(set(c) for c in columns)):
        column = [c.get(name) for c in columns]
        if len({repr(v) for v in column}) > 1:
            ports[name] = column
    chosen = _separating(order, ports)
    if chosen:
        picked = build([[(n, ports[n][i]) for n in chosen]
                        for i in range(len(order))])
        if len(set(picked.values())) == len(order):
            return picked

    raise IsomorphError(
        f'{block_name} is built {len(order)} times and nothing tells '
        'the builds apart: ' + _shape_difference(order)
        + '. They are different modules and cannot share one name. '
        'Give the block a parameter or a constant that differs '
        'between them, so each module is named by something you '
        'wrote. Nothing is invented here.')


def _describe_port (part):
    """One port, as the error message should read it."""
    if part is None:
        return 'absent'
    width, kind = part
    if kind is not None and kind[0] in ('enum', 'struct'):
        return f'{kind[0]} {kind[1]}'
    if kind is not None and kind[0] == 'array':
        return f'an array of {kind[1]} by {width} bits'
    return f'{width} bits'


def _shape_difference (order):
    """The ports that are not the same in every build."""
    out = []
    first = dict((part[0], part[1:]) for part in order[0][2])
    for shape in order[1:]:
        for name, *rest in shape[2]:
            if first.get(name) != tuple(rest):
                out.append(f'{name} is {_describe_port(first.get(name))} '
                           f'in one and {_describe_port(tuple(rest))} in '
                           'another')
    return ', '.join(sorted(set(out))) or 'the ports differ'


def _differs_from_default (node, name, value):
    default = inspect.signature(node.func).parameters[name].default
    return default is not value and default != value


class Elaborated:
    """One elaborated block: the result of calling a @block function."""

    def __init__ (self, func, arguments, frame_locals, assigns):
        self.func = func
        self.block_name = func.__name__
        self.arguments = arguments          # bound arguments, in order
        self.ports = {}                     # name -> Signal | namespace | list
        self.parameters = {}                # name -> int/bool/EnumType
        self.signals = {}                   # name -> Signal
        self.arrays = {}                    # name -> SignalArray
        self.constants = {}                 # name -> int
        self.enums = {}                     # name -> EnumType
        self.functions = {}                 # name -> FunctionType
        self.processes = []                 # Process objects, source order
        self.assigns = []                   # Assign objects
        self.instances = {}                 # name -> Elaborated
        self.open_ports = set()             # formals connected to open_port()
        self.instance_name = None
        self.array_name = None              # set when built in a loop
        self.array_index = 0
        self.array_count = 0
        self._name_override = None          # set by _resolve_names
        self.line = 0                       # the instantiating call's line
        self.port_names = {}                # id(leaf signal) -> port name
        self._sort(arguments, frame_locals)
        self.assigns = list(assigns)

    def _sort (self, arguments, frame_locals):
        for name, value in list(arguments.items()):
            # a slice of a signals() array, or a list built from one,
            # is an array port like any other
            if (isinstance(value, (list, tuple))
                    and not isinstance(value, SignalArray)
                    and value and all(isinstance(e, Signal)
                                      for e in value)):
                value = array_view(value)
                arguments[name] = value
                self.arguments[name] = value
            if isinstance(value, (Signal, SignalArray, SimpleNamespace,
                                  OpenPort)):
                self.ports[name] = value
                self.port_names.update(_port_names(name, value))
            elif isinstance(value, (int, bool, EnumType, StructType, str)):
                self.parameters[name] = value
            else:
                raise IsomorphError(f'{self.block_name}: argument {name} is '
                                    'neither a port nor a parameter: '
                                    f'{value!r}')
        port_ids = set()
        for value in self.ports.values():
            for element in _leaf_signals(value):
                port_ids.add(id(element))
        for name, value in frame_locals.items():
            if name in arguments:
                continue
            if isinstance(value, Signal):
                if id(value) in port_ids:
                    continue
                if value.name is None:
                    value.name = name
                self.signals[name] = value
            elif isinstance(value, SignalArray):
                value.name = name
                for index, element in enumerate(value):
                    element.name = f'{name}[{index}]'
                self.arrays[name] = value
            elif isinstance(value, SimpleNamespace):
                self._namespace(name, value, port_ids)
            elif isinstance(value, EnumType):
                value.name = name
                self.enums[name] = value
            elif isinstance(value, Process):
                self.processes.append(value)
            elif isinstance(value, (Elaborated, Blackbox)):
                value.instance_name = name
                self.instances[name] = value
            elif isinstance(value, bool) or isinstance(value, int):
                self.constants[name] = value
            elif (isinstance(value, FunctionType)
                    and not isinstance(value, Process)):
                self.functions[name] = value
            elif isinstance(value, (tuple, list)):
                children = [e for e in value
                            if isinstance(e, (Elaborated, Blackbox))]
                for index, element in enumerate(value):
                    if isinstance(element, Signal) and element.name is None:
                        element.name = f'{name}_{index}'
                        self.signals[element.name] = element
                    elif isinstance(element, (Elaborated, Blackbox)):
                        # array[k], not array_k. The index is one the
                        # author wrote; an underscore and a number is a
                        # name invented on their behalf, which SPEC 4.5
                        # says does not happen. The analyser checks the
                        # array is regular enough to emit as a generate
                        # and refuses it otherwise.
                        iname = f'{name}[{element_index(children, element)}]'
                        element.instance_name = iname
                        element.array_name = name
                        element.array_index = element_index(children,
                                                            element)
                        element.array_count = len(children)
                        self.instances[iname] = element
            elif isinstance(value, Instances):
                pass
        self.processes.sort(key = lambda p: p.func.__code__.co_firstlineno)
        self._name_shared_types()

    def _namespace (self, name, value, port_ids):
        """A namespace of hardware is named by its path.

        A bundle used inside a block rather than as a port - one stage
        of a mux chain handing to the next - holds ordinary signals
        that want ordinary names, and without one they are all called
        None and look to the driver check like the same wire. The same
        rule one level up: the namespace pipeline() returns holds the
        instances and the link bundles between them, so chain.gain is
        the instance chain_gain and the stream leaving it is
        chain_gain_out_valid. Nothing is invented; the name is the path
        you would type to reach the thing."""
        for member, element in vars(value).items():
            path = f'{name}_{member}'
            if isinstance(element, Signal):
                if id(element) in port_ids:
                    continue
                if element.name is None:
                    element.name = path
                self.signals[element.name] = element
            elif isinstance(element, SignalArray):
                if id(element) in port_ids:
                    continue
                if element.name is None:
                    element.name = path
                    for index, each in enumerate(element):
                        each.name = f'{path}[{index}]'
                self.arrays[element.name] = element
            elif isinstance(element, (Elaborated, Blackbox)):
                element.instance_name = path
                self.instances[path] = element
            elif isinstance(element, SimpleNamespace):
                self._namespace(path, element, port_ids)

    def _name_shared_types (self):
        """An enum or struct declared at module scope, shared by several
        blocks, is not a local of any of them. Recover its name from the
        block's globals and declare it in every module that uses it, so
        the emitted type is named rather than None."""
        globals_of_block = getattr(self.func, '__globals__', {})
        used = []
        for value in list(self.signals.values()) + list(self.arrays.values()):
            kind = getattr(value, 'type', None)
            if isinstance(kind, EnumType):
                used.append(kind)
        for value in self.parameters.values():
            if isinstance(value, EnumType):
                used.append(value)
        for kind in used:
            if kind.name is None:
                for name, candidate in globals_of_block.items():
                    if candidate is kind:
                        kind.name = name
                        break
            if kind.name is None:
                raise IsomorphError(
                    f'{self.block_name}: an enum used by this block has no '
                    'name. Assign enum(...) to a variable, in the block or '
                    'at module scope, before using it in signal().')
            self.enums.setdefault(kind.name, kind)

    @property
    def module_name (self):
        """What this block is called in the emitted HDL.

        Its own name, unless the design holds two different builds of
        it, in which case both are suffixed to tell them apart. See
        _resolve_names for why it is that way round.
        """
        if self._name_override is not None:
            return self._name_override
        return self._built_name

    @property
    def _built_name (self):
        """Block name, suffixed by structural parameters that differ from
        the defaults (4.1)."""
        return _join_name(self.block_name,
                          [(n, v) for n, v in self.parameters.items()
                           if _differs_from_default(self, n, v)])

    @property
    def shape (self):
        """What makes this build a different module from another build
        of the same block.

        Its parameters and the widths of its ports. A block is an
        ordinary function of those two things, so two builds that
        agree on them elaborate to the same body and are one module;
        two that do not are two modules, in two files, whether or not
        a parameter says so.

        The port half is why this is not just the parameters. The
        house idiom takes a width from the port - a multiply step opens
        with WIDTH = max(len(i_data_a), len(i_data_b)) - so a block
        can be built at two sizes with no parameter anywhere, and
        keying the hierarchy on the name alone dropped the second
        one and wired its instance to the first one's module.
        """
        params = tuple(sorted((n, repr(v))
                              for n, v in self.parameters.items()))
        ports = tuple(part for name, value in self.ports.items()
                      for part in _shape_part(name, value))
        return (self.block_name, params, ports)

    def walk (self):
        """Every distinct elaborated block below and including this one,
        leaves first."""
        self._resolve_names()
        seen = {}
        def visit (node):
            for child in node.instances.values():
                visit(child)
            seen.setdefault(node.shape, node)
        visit(self)
        return list(seen.values())

    def _resolve_names (self):
        """Name a block after itself unless the design builds it twice.

        A parameter that differs from its default is baked into the
        module during elaboration - a width, a counter limit, the
        contents of a memory - so two blocks built from one source with
        different parameters really are two modules, and they cannot
        share a name. That is why the name carries the parameters.

        But it only has to carry them when there is something to tell
        apart. Three uarts at one baud rate are one module instantiated
        three times, which is what an instance name is for, and calling
        that module uart_SYSTEM_CLOCK_50000000 tells nobody
        anything. A design that holds one build of a block gets the
        block's own name; only a design that holds two of them pays for
        the distinction, and then the suffix is doing real work.

        Nothing else changes: the modules are still one per distinct
        build, and an instance still refers to its own by name.
        """
        by_block = {}
        def visit (node):
            for child in node.instances.values():
                visit(child)
            by_block.setdefault(node.block_name, {}) \
                    .setdefault(node.shape, []).append(node)
        visit(self)
        for block, shapes in by_block.items():
            if len(shapes) == 1:
                for nodes in shapes.values():
                    for node in nodes:
                        node._name_override = block
                continue
            for shape, name in _tell_apart(block, shapes).items():
                for node in shapes[shape]:
                    node._name_override = name


def element_index (children, element):
    for index, child in enumerate(children):
        if child is element:
            return index
    return 0


def _port_names (name, value):
    """Port names for the leaves of a port, keyed by signal identity: the
    parent's signal is not renamed, it is only called this inside here."""
    names = {}
    if isinstance(value, Signal):
        names[id(value)] = name
    elif isinstance(value, SignalArray):
        names[id(value)] = name
        for index, element in enumerate(value):
            names[id(element)] = f'{name}[{index}]'
    elif isinstance(value, SimpleNamespace):
        names[id(value)] = name
        for member, element in vars(value).items():
            if isinstance(element, Signal):
                names[id(element)] = f'{name}_{member}'
    return names


def _leaf_signals (value):
    if isinstance(value, Signal):
        return [value]
    if isinstance(value, (SignalArray, list)):
        out = []
        for element in value:
            out += _leaf_signals(element)
        return out
    if isinstance(value, SimpleNamespace):
        return [e for e in vars(value).values() if isinstance(e, Signal)]
    return []


def block (func):
    """Decorator: calling the block elaborates it and returns an
    Elaborated instance for the parent to hold."""
    signature = inspect.signature(func)

    @functools.wraps(func)
    def elaborate (*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        open_ports = set()
        for name, value in list(bound.arguments.items()):
            if isinstance(value, OpenPort):
                open_ports.add(name)
                bound.arguments[name] = Signal(value.width)
        elaboration_stack.append([])
        made_stack.append([])
        try:
            result = func(*bound.args, **bound.kwargs)
        finally:
            assigns = elaboration_stack.pop()
            made_stack.pop()
        if not isinstance(result, Instances):
            raise IsomorphError(f'{func.__name__} must end with '
                                'return instances()')
        elaborated = Elaborated(func, dict(bound.arguments), result.locals,
                                assigns)
        elaborated.open_ports = open_ports
        elaborated.line = sys._getframe(1).f_lineno
        # the parent is the block that will have to keep hold of this
        note_made(elaborated)
        return elaborated
    elaborate.block = func
    elaborate.is_block = True
    return elaborate
