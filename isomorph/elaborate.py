"""Run a block once and collect what it declared: ports, signals,
constants, enumerations, functions, processes, continuous assignments
and child instances. Section 3.2, 3.8 and 4.1 of SPEC.txt."""
import functools
import inspect
import sys
from types import FunctionType, SimpleNamespace

from .signal import (Signal, SignalArray, Bundle, EnumType, StructType,
    Process, Assign, Instances, OpenPort, IsomorphError, elaboration_stack)


class Elaborated:
    """One elaborated block: the result of calling a @block function."""

    def __init__ (self, func, arguments, frame_locals, assigns):
        self.func = func
        self.block_name = func.__name__
        self.arguments = arguments          # bound arguments, in order
        self.ports = {}                     # name -> Signal | Bundle | list
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
        self.line = 0                       # the instantiating call's line
        self.port_names = {}                # id(leaf signal) -> port name
        self._sort(arguments, frame_locals)
        self.assigns = list(assigns)

    def _sort (self, arguments, frame_locals):
        for name, value in arguments.items():
            if isinstance(value, (Signal, SignalArray, Bundle, SimpleNamespace,
                                  OpenPort)) or (isinstance(value, list) and
                                  value and isinstance(value[0], (Signal, Bundle))):
                self.ports[name] = value
                self.port_names.update(_port_names(name, value))
            elif isinstance(value, (int, bool, EnumType, StructType, str)):
                self.parameters[name] = value
            else:
                raise IsomorphError(f'{self.block_name}: argument {name} is '
                                    f'neither a port nor a parameter: {value!r}')
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
            elif isinstance(value, EnumType):
                value.name = name
                self.enums[name] = value
            elif isinstance(value, Process):
                self.processes.append(value)
            elif isinstance(value, Elaborated):
                value.instance_name = name
                self.instances[name] = value
            elif isinstance(value, bool) or isinstance(value, int):
                self.constants[name] = value
            elif isinstance(value, FunctionType) and not isinstance(value, Process):
                self.functions[name] = value
            elif isinstance(value, (tuple, list)):
                for index, element in enumerate(value):
                    if isinstance(element, Signal) and element.name is None:
                        element.name = f'{name}_{index}'
                        self.signals[element.name] = element
                    elif isinstance(element, Elaborated):
                        iname = f'{name}_{index}'
                        element.instance_name = iname
                        self.instances[iname] = element
            elif isinstance(value, Instances):
                pass
        self.processes.sort(key = lambda p: p.func.__code__.co_firstlineno)
        self._name_shared_types()

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
        """Block name, suffixed by structural parameters that differ from
        the defaults (4.1)."""
        defaults = {name: p.default for name, p in
                    inspect.signature(self.func).parameters.items()}
        suffix = [f'{n}_{v}' for n, v in self.parameters.items()
                  if defaults.get(n) is not v and defaults.get(n) != v]
        # Single underscore: VHDL forbids consecutive underscores, and
        # SPEC 4.5 keeps the same identifier in SV, VHDL and C99.
        return self.block_name + (('_' + '_'.join(suffix)) if suffix else '')

    def walk (self):
        """Every distinct elaborated block below and including this one,
        leaves first."""
        seen = {}
        def visit (node):
            for child in node.instances.values():
                visit(child)
            seen.setdefault(node.module_name, node)
        visit(self)
        return list(seen.values())


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
    elif isinstance(value, (Bundle, SimpleNamespace)):
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
    if isinstance(value, (Bundle, SimpleNamespace)):
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
        try:
            result = func(*bound.args, **bound.kwargs)
        finally:
            assigns = elaboration_stack.pop()
        if not isinstance(result, Instances):
            raise IsomorphError(f'{func.__name__} must end with '
                                'return instances()')
        elaborated = Elaborated(func, dict(bound.arguments), result.locals,
                                assigns)
        elaborated.open_ports = open_ports
        elaborated.line = sys._getframe(1).f_lineno
        return elaborated
    elaborate.block = func
    elaborate.is_block = True
    return elaborate
