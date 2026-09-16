"""Read process, assign and function bodies as Python ASTs and produce
the typed IR of ir.py, applying the width rules (4.2), the assignment
rule (4.3) and the checks (4.7) of SPEC.txt."""
import ast
import inspect
import textwrap
import sys
import tokenize
from types import SimpleNamespace, FunctionType

from . import ir
from .widths import checked_expression
from .blackbox import Blackbox
from .signal import (Signal, SignalArray, EnumType, EnumMember, const,
    sign_extend, StructType, Process, Assign, Vector, IsomorphError, concat,
    replicate, ones, zeroes, bits, vector, Hexed, Rtl)
from . import reserved


class ConversionError(IsomorphError):
    def __init__ (self, message, file = None, line = None, source = None):
        where = f'{file}:{line}: ' if file else ''
        detail = f'\n    {source.strip()}' if source else ''
        super().__init__(where + message + detail)


def min_width (value):
    if value >= 0:
        return max(1, value.bit_length())
    return (-value - 1).bit_length() + 1


class Scope:
    """Name resolution for one process or function."""

    def __init__ (self, func, elaborated):
        self.func = func
        self.elaborated = elaborated
        self.names = dict(func.__globals__)
        if func.__code__.co_freevars and func.__closure__:
            for name, cell in zip(func.__code__.co_freevars, func.__closure__):
                try:
                    self.names[name] = cell.cell_contents
                except ValueError:
                    pass
        self.locals = {}                    # loop indices, function locals


class Analyser:
    def __init__ (self, elaborated, child_directions = None,
                  child_domains = None):
        self.e = elaborated
        self.file = inspect.getsourcefile(elaborated.func)
        self.driven = {}                    # signal name -> driver name
        self.read = set()
        self.functions = {}                 # name -> ir.Function
        self.warnings = []
        self.child_directions = child_directions or {}
        self.child_domains = child_domains or {}
        self.child_clocks = (child_domains or {}).get('__clocks__', {})
        self.child_sync = (child_domains or {}).get('__sync__', {})
        self.domains = {}
        self.port_domains = {}
        self.port_clocks = set()
        self.sync_inputs = set()
        self.crossings = []
        # how each signal's width was written, for a cast that has to
        # follow the generic rather than fold (PARAMETERS.md stage 3)
        self.width_exprs = {}
        for name, sig in list(getattr(elaborated, 'signals', {}).items()) \
                + list(getattr(elaborated, 'arrays', {}).items()):
            written = getattr(sig, 'width_expr', None)
            if written:
                self.width_exprs[name] = written
        self.comments = source_comments(elaborated.func)
        self.file_lines = file_lines(self.file)
        self.consumed = set()           # comment lines already placed

    def name_of (self, sig):
        """A signal's name inside this block: its port name here if it
        is a port, else its own name."""
        return self.e.port_names.get(id(sig), sig.name)

    def comments_before (self, line, previous):
        """Full-line comments between the previous item and this line.

        A comment is placed once. Suites are analysed before the items
        around them, so a comment that closes a suite is claimed there
        and does not reappear above whatever follows."""
        out = []
        for l in range(previous + 1, line):
            if l in self.consumed:
                continue
            c = self.comments.get(l)
            if c is not None and c[0]:
                out.append(c[1])
                self.consumed.add(l)
        return out

    def leading_comments (self, line):
        """The comment block written directly above an item.

        A signal declaration is one of a sequence, so what belongs to it
        is whatever lies between it and the one before. A process or an
        instance is not, so what belongs to it is the run of full-line
        comments immediately above, however long, stopping at the first
        line that is neither a comment nor a decorator. A fixed lookback
        window drops the top of a longer block, which is exactly where
        the section bar lives."""
        start = line
        while start > 1:
            above = start - 1
            if above in self.consumed:
                break
            if self.file_line(above).lstrip().startswith('@'):
                start = above
                continue
            c = self.comments.get(above)
            if (c is None) or (not c[0]):
                break
            start = above
        return self.comments_before(line, start - 1)

    def file_line (self, line):
        """One line of the file that defines this block, 1 based."""
        if 1 <= line <= len(self.file_lines):
            return self.file_lines[line - 1]
        return ''

    def suite_tail (self, nodes):
        """Comments after the last statement of a suite, indented at
        least as far as it. ast ends a suite at its last statement, so
        without this they would drift onto the next item."""
        if not nodes:
            return []
        indent = nodes[0].col_offset
        out = []
        line = nodes[-1].end_lineno + 1
        while line - 1 < len(self.source_lines):
            text = self.source_lines[line - 1]
            stripped = text.strip()
            if not stripped:
                line += 1
                continue
            if not stripped.startswith('#'):
                break
            if len(text) - len(text.lstrip()) < indent:
                break
            absolute = self.line_base + line
            if absolute not in self.consumed:
                self.consumed.add(absolute)
                out.append(stripped)
            line += 1
        return out

    def trailing (self, line):
        c = self.comments.get(line)
        return c[1] if c is not None and not c[0] else None

    # ---- entry points -----------------------------------------------------
    def module (self):
        e = self.e
        for child in e.instances.values():
            self.instance_drivers(child)
        processes = [self.process(p) for p in e.processes]
        assigns = [self.cont_assign(a) for a in e.assigns]
        ports = []
        arg_lines = signature_lines(e.func)
        for name, value in e.ports.items():
            leaves = self._port_leaves(name, value)
            line = arg_lines.get(name)
            if leaves and line is not None:
                # a bundle expands to several ports; the comment written
                # against the argument belongs to the group, so it goes
                # on the first of them
                leaves[0].comments = self.comments_before(
                    line, arg_lines.get('__previous__' + name, line - 1))
                leaves[0].trailing = self.trailing(line)
            ports.extend(leaves)
        signals = []
        for name, s in e.signals.items():
            attrs = dict(s.attributes)
            if s.kind == 'enum' and s.type is not None:
                enc = getattr(s.type, 'encoding', 'auto')
                if enc and enc != 'auto':
                    spelt = VENDOR_ENCODING.get(enc, enc)
                    attrs.setdefault('fsm_encoding', spelt)
                    attrs.setdefault('syn_encoding', spelt)
            signals.append(ir.Sig(name, s.width, s.kind, None,
                                  s.type, attrs, 0, s.line,
                                  width_expr = s.width_expr))
        for name, a in e.arrays.items():
            signals.append(ir.Sig(name, a.width, 'vector', a.init,
                                  None, dict(a.attributes), len(a),
                                  a.line,
                                  width_expr = getattr(a, 'width_expr',
                                                       None),
                                  array_expr = getattr(a, 'count_expr',
                                                       None),
                                  init_base = getattr(a, 'init_base',
                                                      None)))
        signals.sort(key = lambda s: s.line)
        previous = e.func.__code__.co_firstlineno
        by_name = {}
        for name, value in list(e.signals.items()) + list(e.arrays.items()):
            by_name[name] = value
        for s in signals:
            s.comments = self.comments_before(s.line, previous)
            s.trailing = self.trailing(s.line)
            previous = s.line
            source = by_name.get(s.name)
            for line in getattr(source, 'attr_lines', ()) or ():
                s.comments += self.comments_before(line, line - 1)
                extra = self.trailing(line)
                if extra:
                    s.comments.append(extra)
                previous = max(previous, line)
        for p in processes:
            p.comments = self.leading_comments(p.line)
        instances = [self.instance(i) for i in e.instances.values()]
        mod = ir.Module(e.module_name, e.block_name, dict(e.parameters),
                         ports, signals, dict(e.constants), dict(e.enums),
                         list(self.functions.values()), processes, assigns,
                         instances, self.file, header_comment(e.func))
        # a parameter is written in the signature beside the ports and
        # carries its comment the same way one does
        for name in e.parameters:
            line = arg_lines.get(name)
            if line is None:
                continue
            before = self.comments_before(
                line, arg_lines.get('__previous__' + name, line - 1))
            after = self.trailing(line)
            if before or after:
                mod.parameter_comments[name] = (before, after)
        mod.constant_exprs = constant_expressions(e.func, e.constants)
        mod.constant_bases = constant_bases(e.func, e.constants)
        mod.parameter_bases = parameter_bases(e.func, e.parameters)
        written = array_expressions(e.func, e.arrays)
        for sig in mod.signals:
            if sig.array and sig.name in written:
                sig.array_expr = written[sig.name]
        self.warnings += promote_port_widths(mod)
        self.check_names(mod)
        self.check_instance_arrays(mod)
        self.check_unused(mod)
        self.check_comb_loops(mod)
        self.domains = self.signal_domains(mod)
        self.sync_inputs = self.sampled_inputs(mod)
        self.crossings = self.check_crossings(mod, self.domains)
        self.port_domains = {
            p.name: self.domains.get(p.name, set())
            for p in mod.ports if p.direction == 'out'}
        self.port_clocks = self.clock_ports(mod)
        return mod

    def clock_ports (self, mod):
        """The ports of this module that carry a clock.

        Its own flops name some; an instance below may name others
        through a port that does nothing here but pass a clock down,
        which is what a per-domain reset synchroniser looks like.
        """
        names = {p.clock for p in mod.processes
                 if p.kind == 'ff' and p.clock}
        for inst in mod.instances:
            for formal in self.child_clocks.get(inst.module, set()):
                actual = inst.ports.get(formal)
                if actual is not None and actual.op == 'ref':
                    names.add(str(actual.value).split('[')[0])
        ports = {p.name for p in mod.ports}
        return {name for name in names if name in ports}

    def signal_domains (self, mod):
        """Which clock drives each signal, as a set of clock names.

        A flop stamps its own clock on everything it writes. A comb
        process and a continuous assignment carry whatever their
        inputs carry, worked out by going round until nothing changes,
        because one comb process may feed another. An input port
        carries nothing: what drives it happened in the parent, so
        nothing here can say, and guessing would invent crossings.
        """
        dom = {}

        def add (name, clocks):
            if not name or not clocks:
                return False
            before = dom.get(name, set())
            if clocks <= before:
                return False
            dom[name] = before | clocks
            return True

        from .emit_c99 import alias_source, clocked_by

        # a pin the designer says is launched by one of this design's
        # clocks is in that clock's domain: the loop that makes it so
        # leaves the device and comes back, so nothing here can see it
        for port in mod.ports:
            told = (port.attributes or {}).get('synchronous_to')
            if told is not None:
                add(port.name, {alias_source(mod, self.name_of(told))})

        for p in mod.processes:
            if p.kind == 'ff' and p.clock:
                # the clock may be a name this module gave the real one
                # on its way past, and a rename is not a domain of its
                # own, so a flop stamps what its clock resolves back to
                for name in body_writes(p.body):
                    add(name, {clocked_by(mod, p)})
        # an instance hands back its child's domains, renamed to what
        # this module calls those clocks
        for inst in mod.instances:
            child = self.child_domains.get(inst.module, {})
            for formal, clocks in child.items():
                actual = inst.ports.get(formal)
                if actual is None or actual.op != 'ref':
                    continue
                outer = set()
                for clock in clocks:
                    carried = inst.ports.get(clock)
                    if carried is not None and carried.op == 'ref':
                        # resolve before dropping the index: the element
                        # of an array gathered from a pin is a rename of
                        # that pin, and the array is not a domain
                        name = alias_source(mod, str(carried.value))
                        outer.add(name.split('[')[0])
                add(str(actual.value).split('[')[0], outer)
        changed = True
        rounds = 0
        while changed and rounds < 64:
            changed = False
            rounds += 1
            for p in mod.processes:
                if p.kind != 'comb':
                    continue
                carried = set()
                for name in body_reads(p.body):
                    carried |= dom.get(name, set())
                for name in body_writes(p.body):
                    changed = add(name, carried) or changed
            for a in mod.assigns:
                carried = set()
                for name in expr_refs(a.value):
                    carried |= dom.get(name, set())
                changed = add(root_name(a.target), carried) or changed
        return dom

    def check_crossings (self, mod, dom):
        """A flop that samples a signal another clock drives.

        Two flops per bit resolve metastability one bit at a time;
        they do not make a bus atomic, so a multi-bit value sampled
        straight across can be read as one the source never held, and
        that is an error. One bit is a severe warning: still wrong,
        but a static signal crossing is the one case where a designer
        may know something the converter cannot.

        A crossing is deliberate when the flop it lands in carries the
        synchroniser attribute the house style puts on such a chain,
        which is what a synchroniser chain does. Those are collected rather
        than complained about, because they are the paths a timing
        constraint has to name.
        """
        # the attribute goes on the flop that resolves the
        # metastability, which is the destination, and that may be a
        # port as easily as a signal
        marked = {s.name for s in mod.signals
                  if SYNC_ATTRIBUTE in (s.attributes or {})}
        marked |= {p.name for p in mod.ports
                   if SYNC_ATTRIBUTE in (p.attributes or {})}
        width = {s.name: s.width for s in mod.signals}
        width.update({p.name: p.width for p in mod.ports})
        found = []
        for p in mod.processes:
            if p.kind != 'ff' or not p.clock:
                continue
            targets = body_writes(p.body)
            synchroniser = bool(targets) and targets <= marked
            for name in sorted(body_reads(p.body)):
                other = dom.get(name, set()) - {p.clock}
                if not other:
                    continue
                for source in sorted(other):
                    found.append({'signal': name, 'from': source,
                                  'to': p.clock, 'process': p.name,
                                  'width': width.get(name, 1),
                                  'synchronised': synchroniser})
                if synchroniser:
                    continue
                self.report_crossing(mod, p, name, other, width)
        # a crossing also happens where a value is handed to a child
        # running on another clock, which is what every synchroniser
        # instance in a dual-clock design is
        found += self.instance_crossings(mod, dom, width)
        return found

    def sampled_inputs (self, mod):
        """Input ports whose value lands in a flop marked as a
        synchroniser, so a parent may hand them another clock's
        signal on purpose."""
        marked = {s.name for s in mod.signals
                  if SYNC_ATTRIBUTE in (s.attributes or {})}
        marked |= {p.name for p in mod.ports
                   if SYNC_ATTRIBUTE in (p.attributes or {})}
        inputs = {p.name for p in mod.ports if p.direction == 'in'}
        out = set()
        for p in mod.processes:
            if p.kind != 'ff' or not p.clock:
                continue
            targets = body_writes(p.body)
            if not targets or not targets <= marked:
                continue
            out |= body_reads(p.body) & inputs
        for inst in mod.instances:
            for formal in self.child_sync.get(inst.module, set()):
                actual = inst.ports.get(formal)
                if actual is not None and actual.op == 'ref':
                    name = str(actual.value).split('[')[0]
                    if name in inputs:
                        out.add(name)
        return out

    def instance_crossings (self, mod, dom, width):
        """A child on one clock handed a value another clock drives."""
        from .emit_c99 import alias_source

        found = []
        for inst in mod.instances:
            formals = self.child_clocks.get(inst.module, set())
            here = set()
            for formal in formals:
                actual = inst.ports.get(formal)
                if actual is not None and actual.op == 'ref':
                    name = alias_source(mod, str(actual.value))
                    here.add(name.split('[')[0])
            if not here:
                continue
            directions = self.child_directions.get(inst.module, {})
            accepted = self.child_sync.get(inst.module, set())
            for formal, actual in inst.ports.items():
                if formal in formals or actual is None:
                    continue
                if actual.op != 'ref' or directions.get(formal) != 'in':
                    continue
                name = str(actual.value).split('[')[0]
                other = dom.get(name, set()) - here
                if not other:
                    continue
                synchronised = formal in accepted
                for source in sorted(other):
                    found.append({'signal': name, 'from': source,
                                  'to': sorted(here)[0],
                                  'process': inst.name,
                                  'width': width.get(name, 1),
                                  'synchronised': synchronised})
                if synchronised:
                    continue
                self.report_instance_crossing(mod, inst, formal, name,
                                              other, width)
        return found

    def report_crossing (self, mod, p, name, other, width):
        listed = ', '.join(sorted(other))
        bits = width.get(name, 1)
        if bits > 1:
            raise ConversionError(
                f'{p.name} runs on {p.clock} and samples {name}, '
                f'{bits} bits wide, which {listed} drives. Two flops '
                'resolve one bit at a time and do not make a bus '
                'atomic, so the destination can read a value the source '
                'never held. Cross it through a synchroniser one bit at '
                'a time, Gray code it, or hold it still with a '
                'handshake and cross only the flag.',
                self.file, p.line)
        self.warnings.append(
            f'severe: crossing: {p.name} runs on {p.clock} and samples '
            f'{name}, which {listed} drives, with no synchroniser. A '
            f'flop that samples another clock carries {SYNC_ATTRIBUTE}; '
            'see the house style.')

    def report_instance_crossing (self, mod, inst, formal, name, other,
                                  width):
        listed = ', '.join(sorted(other))
        bits = width.get(name, 1)
        if bits > 1:
            raise ConversionError(
                f'{inst.name}.{formal} is given {name}, {bits} bits '
                f'wide, which {listed} drives, and {inst.name} runs on '
                'another clock. Two flops resolve one bit at a time and '
                'do not make a bus atomic. Gray code it, or hold it '
                'still with a handshake and cross only the flag.',
                self.file, inst.line)
        self.warnings.append(
            f'severe: crossing: {inst.name}.{formal} is given {name}, '
            f'which {listed} drives, and {inst.name} runs on another '
            f'clock with no flop marked {SYNC_ATTRIBUTE} behind that '
            'port.')

    def check_instance_arrays (self, mod):
        """A loop over instances is a generate, or it is an error.

        A list of children used to become cells_0, cells_1 and so on:
        names nobody typed, which is the MyHDL behaviour this project
        was a reaction to and which SPEC 4.5 forbids. They are
        cells[0], cells[1] now, and the emitters write them back as
        one labelled generate, so the only name invented is the
        instance label inside the loop body.

        That only works if the array is regular: the same child every
        time, the same parameters, and every port either the same wire
        for all of them or element k of one array. Anything else is an
        error telling the author to name each instance, which gives
        them better names than a loop was ever going to.
        """
        groups = {}
        for inst in mod.instances:
            if inst.array:
                groups.setdefault(inst.array, []).append(inst)
        # a genvar is declared at module level, which is the only
        # spelling Quartus takes, so two arrays may not share one
        spent = set()
        for name, members in groups.items():
            members.sort(key = lambda i: i.index)
            head = members[0]
            for other in members[1:]:
                if other.module != head.module:
                    raise ConversionError(
                        f'{name} holds a {head.module} and a '
                        f'{other.module}. An array of instances is one '
                        'child repeated, so that it can be emitted as a '
                        'generate rather than as instances with invented '
                        'names. Give each of these its own name.',
                        self.file, head.line)
                if other.params != head.params:
                    raise ConversionError(
                        f'{name} builds {head.module} with two different '
                        'parameter sets, so its members are not the same '
                        'module. Give each of these its own name.',
                        self.file, head.line)
            shape = {}
            for formal in head.ports:
                values = [i.ports.get(formal) for i in members]
                texts = [None if v is None else v.value for v in values]
                if len(set(texts)) == 1:
                    shape[formal] = ('same', values[0])
                    continue
                base = indexed_base(texts)
                if base is None:
                    listed = ', '.join('open' if t is None else t
                                       for t in texts)
                    raise ConversionError(
                        f'{name}[k].{formal} is connected to {listed}. '
                        'Every member of an array of instances takes '
                        'either the same wire or element k of one array, '
                        'because the array is emitted as a single '
                        'generate. This one is not that shape, so give '
                        'each instance its own name and connect them '
                        'one at a time.',
                        self.file, head.line)
                shape[formal] = ('index', base)
            head.shape = shape
            head.count = len(members)
            head.var = genvar_name(mod, spent)
            spent.add(head.var)

    def _port_leaves (self, name, value):
        out = []
        if isinstance(value, Signal):
            pname = self.name_of(value)
            direction = 'out' if pname in self.driven else 'in'
            out.append(ir.Port(pname, value.width, direction, value.kind,
                               value.type, 0, [], None,
                               dict(value.attributes),
                               getattr(value, 'clock_period_ns', None),
                               value.width_expr))
        elif isinstance(value, SignalArray):
            pname = self.name_of(value)
            # an instance drives a whole array under its own name and
            # a process drives one entry under an indexed one, so an
            # array port is an output if either says so. Looking only
            # for the entries made a board's array output an input,
            # and the instance below it was assigning to it
            driven = (pname in self.driven
                      or any(self.name_of(s) in self.driven
                             for s in value))
            direction = 'out' if driven else 'in'
            out.append(ir.Port(pname, value.width, direction, 'vector', None,
                               len(value),
                               width_expr = getattr(value, 'width_expr',
                                                    None)))
        elif isinstance(value, SimpleNamespace):
            for member, element in vars(value).items():
                if isinstance(element, Signal):
                    out += self._port_leaves(None, element)
        return out

    def instance_ports (self, child):
        """formal leaf name -> actual signal (or None for open)."""
        ports = {}
        for formal, actual in child.ports.items():
            if formal in getattr(child, 'open_ports', ()):
                ports[formal] = None
            elif isinstance(actual, Signal):
                ports[formal] = actual
            elif isinstance(actual, SimpleNamespace):
                for member, element in vars(actual).items():
                    if isinstance(element, Signal):
                        ports[f'{formal}_{member}'] = element
            elif isinstance(actual, SignalArray):
                ports[formal] = actual
            else:
                ports[formal] = None
        return ports

    def instance_drivers (self, child):
        directions = self.child_directions.get(child.module_name, {})
        for formal, actual in self.instance_ports(child).items():
            if actual is not None and directions.get(formal) == 'out':
                self.current = child.instance_name
                self.drive(self.name_of(actual), None, None)

    def instance (self, child):
        ports = {}
        for formal, actual in self.instance_ports(child).items():
            if actual is None:
                ports[formal] = None
                continue
            aname = self.name_of(actual)
            if aname is None:
                raise ConversionError(
                    f'{child.instance_name}.{formal} is connected to '
                    'something this block has no name for. A slice of an '
                    'array, or a list built from one, is a wire here only '
                    'if it is a whole array: pass the array itself, or '
                    'give the child one element at a time. Connecting '
                    'part of an array is ROADMAP item 11.',
                    self.file, child.line)
            ports[formal] = ir.Expr('ref', actual.width, value = aname)
            self.read.add(aname.split('[')[0])
        # a block bakes its parameters into a specialised module, so
        # an override would say nothing. A blackbox is the other way
        # round: the parameters are how the vendor's part is
        # configured and the only place they can be said is here
        params = dict(getattr(child, 'parameters', {})) \
            if isinstance(child, Blackbox) else {}
        guard = getattr(child, 'guard', None)
        outputs = []
        if guard is not None:
            directions = self.child_directions.get(child.module_name, {})
            for formal, actual in ports.items():
                if actual is not None and directions.get(formal) == 'out':
                    outputs.append((actual.value, actual.width))
        return ir.Instance(child.instance_name, child.module_name, ports,
                           child.line, self.leading_comments(child.line),
                           params = params,
                           array = child.array_name,
                           index = child.array_index,
                           count = child.array_count,
                           guard = guard.text if guard else None,
                           guard_on = bool(guard.value) if guard else True,
                           guard_outputs = outputs)

    # ---- bodies ------------------------------------------------------------
    def _tree (self, func):
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
        node = tree.body[0]
        self.line_base = func.__code__.co_firstlineno - 1
        self.source_lines = source.splitlines()
        return node

    def check_names (self, m):
        """SPEC 4.5: reserved words and VHDL case-insensitive uniqueness.

        Checks the names that will appear in HDL, not Python bundle
        parameter names (bus -> bus_data / bus_ack).
        """
        claimed = {}

        def claim (name, role, line):
            if not name:
                return
            base = name.split('[')[0]
            msg = reserved.clash_message(base)
            if msg:
                raise ConversionError(msg, self.file, line)
            key = base.lower()
            prev = claimed.get(key)
            if prev is not None and (prev[0] != base or prev[1] != role):
                raise ConversionError(
                    f'{base} ({role}) collides with {prev[0]} ({prev[1]}) '
                    f'in VHDL; VHDL names are case-insensitive and the '
                    f'same identifiers are emitted to SystemVerilog. '
                    f'Rename one of them (SPEC 4.5)',
                    self.file, line or prev[2])
            claimed[key] = (base, role, line)

        claim(m.name, 'module', self.e.line)
        for p in m.ports:
            claim(p.name, 'port', None)
        for s in m.signals:
            claim(s.name, 'signal', s.line)
        # an enum member is a name in the module, not inside its own
        # type, in both languages: two state machines that both have a
        # RESET are a duplicate declaration, not two scopes
        member_of = {}
        for name, enum_type in m.enums.items():
            claim(name, 'type', getattr(enum_type, 'line', None))
            for member in enum_type.members:
                owner = member_of.get(member.name)
                if owner is not None and owner != name:
                    raise ConversionError(
                        f'{member.name} is a member of both {owner} and '
                        f'{name}. An enumeration member is a name in the '
                        f'module, so the two collide; give one of them a '
                        f'prefix (SPEC 4.5)',
                        self.file, getattr(enum_type, 'line', None))
                member_of[member.name] = name
                claim(member.name, 'enumeration',
                      getattr(enum_type, 'line', None))
        for name in m.constants:
            claim(name, 'constant', None)
        for name in m.parameters:
            claim(name, 'parameter', None)
        for proc in m.processes:
            claim(proc.name, 'process', proc.line)
        for inst in m.instances:
            claim(inst.name, 'instance', inst.line)
        for fn in m.functions:
            claim(fn.name, 'function', None)

    def error (self, node, message):
        line = self.line_base + getattr(node, 'lineno', 0)
        src = ''
        line = getattr(node, 'lineno', 0)
        if line and (line - 1) < len(self.source_lines):
            src = self.source_lines[node.lineno - 1]
        raise ConversionError(message, self.file, line, src)

    def process (self, proc):
        node = self._tree(proc.func)
        scope = Scope(proc.func, self.e)
        self.current = proc.name
        self.kind = proc.kind
        self.assigned_here = []
        body = self.block_body(node.body, scope, node.lineno)
        if proc.clock:
            self.read.add(self.name_of(proc.clock))
        if proc.kind == 'comb':
            self.check_complete(body, proc.name, proc)
            self.check_rbw(body, set(self.assigned_here), proc)
        reset = None
        if getattr(proc, 'reset', None) is not None:
            reset = self.name_of(proc.reset)
            self.read.add(reset)
            self.check_async_reset(body, reset, proc)
            # the construct will not build without a reason, so every
            # one of these is a circuit whose author has already said
            # why it is right. Announcing it is useful; calling it
            # severe on every run only teaches the reader to skip the
            # word where it means a latch
            self.warnings.append(
                proc.name + ': asynchronous reset on ' + reset
                + ' (' + proc.reason + ')')
        return ir.Process(proc.name, proc.kind,
                          self.name_of(proc.clock) if proc.clock else None,
                          proc.polarity, body, dict(proc.attributes),
                          proc.line, [], reset,
                          getattr(proc, 'reset_polarity', 'pos'),
                          getattr(proc, 'reason', None))

    def check_async_reset (self, body, reset, proc):
        """The body of an asynchronous-reset flop is exactly one if/else
        on the reset. Both languages emit that shape, VHDL as
        `if reset then ... elsif rising_edge(clock) then ...`, so there
        is nowhere to put anything else."""
        shape = (f'{proc.name}: the body of always_ff_async_reset is one '
                 f'if ({reset}): ... else: ... and nothing else')
        if len(body) != 1 or not isinstance(body[0], ir.If):
            raise ConversionError(shape, self.file, proc.line)
        node = body[0]
        if len(node.branches) != 2 or node.branches[-1][0] is not None:
            raise ConversionError(shape + '; no elif', self.file, proc.line)
        cond = node.branches[0][0]
        names = set()
        stack = [cond]
        while stack:
            e = stack.pop()
            if e is None:
                continue
            if getattr(e, 'op', None) in ('ref', 'bit'):
                if isinstance(e.value, str):
                    names.add(e.value)
            stack.extend(getattr(e, 'args', []) or [])
        if reset not in names:
            raise ConversionError(
                f'{proc.name}: the first condition must test {reset}, the '
                'asynchronous reset', self.file, proc.line)

    def else_line (self, node):
        """Line of the `else:` keyword. Comments under it belong to the
        else branch; comments above it stay with the branch that ends
        there. ast records no line for `else`, so read it back from the
        source between the two suites."""
        start = node.body[-1].end_lineno
        stop = node.orelse[0].lineno
        for line in range(start + 1, stop):
            index = line - 1
            if index < len(self.source_lines):
                if self.source_lines[index].strip().startswith('else'):
                    return line
        return stop - 1

    def block_body (self, nodes, scope, opener_line, tail = True):
        """Statements of one suite with their comments attached."""
        out = []
        previous = self.line_base + opener_line
        for node in nodes:
            s = self.statement(node, scope)
            line = self.line_base + node.lineno
            if s is not None:
                s.comments = self.comments_before(line, previous)
                s.trailing = self.trailing(line)
                out.append(s)
            previous = self.line_base + getattr(node, 'end_lineno',
                                                node.lineno)
        if tail:
            for text in self.suite_tail(nodes):
                out.append(ir.Comment(text, previous))
        return out

    def cont_assign (self, a):
        source = textwrap.dedent(inspect.getsource(a.expression))
        # the source line holds assign(target, lambda: expr); take the lambda
        tree = ast.parse(source.strip())
        lam = next(n for n in ast.walk(tree) if isinstance(n, ast.Lambda))
        self.line_base = a.expression.__code__.co_firstlineno - 1
        self.source_lines = source.splitlines()
        scope = Scope(a.expression, self.e)
        self.current = f'assign {a.target.name}'
        self.kind = 'assign'
        tname = self.name_of(a.target)
        target = ir.Expr('ref', a.target.width, value = tname)
        self.drive(tname, target, lam)
        value = self.expression(lam.body, scope)
        value = self.fit(value, target.width, lam,
                         self.target_width_expr(target))
        return ir.ContAssign(target, value, a.line,
                             self.leading_comments(a.line),
                             self.trailing(a.line))

    # ---- statements -------------------------------------------------------
    def statement (self, node, scope):
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                self.error(node, 'one target per assignment')
            return self.assignment(node.targets[0], node.value, node, scope)
        if isinstance(node, ast.If):
            constant = self.constant_or_none(node.test, scope)
            if constant is not None:
                # elaboration-time condition inside a process: prune
                chosen = node.body if constant else node.orelse
                stmts = self.block_body(chosen, scope, node.lineno)
                return ir.If([(None, stmts)],
                             self.line_base + node.lineno) if stmts else None
            branches = []
            headers = [[]]              # comments above each elif / else
            current = node
            while True:
                cond = self.expression(current.test, scope)
                if cond.width != 1:
                    self.warnings.append(f'{self.current}: condition of width '
                                         f'{cond.width} at line '
                                         f'{self.line_base + current.lineno}')
                branches.append((cond, self.block_body(current.body, scope,
                                                        current.lineno)))
                body_end = self.line_base + current.body[-1].end_lineno
                if (len(current.orelse) == 1
                        and isinstance(current.orelse[0], ast.If)):
                    current = current.orelse[0]
                    headers.append(self.comments_before(
                        self.line_base + current.lineno, body_end))
                    continue
                if current.orelse:
                    keyword = self.else_line(current)
                    headers.append(self.comments_before(
                        self.line_base + keyword, body_end))
                    branches.append((None, self.block_body(
                        current.orelse, scope, keyword)))
                break
            node_if = ir.If(branches, self.line_base + node.lineno)
            node_if.branch_comments = headers
            return node_if
        if isinstance(node, ast.For):
            return self.for_loop(node, scope)
        if isinstance(node, ast.Match):
            return self.match_stmt(node, scope)
        if isinstance(node, ast.Assert):
            return ir.Assert(self.expression(node.test, scope),
                             self.assert_message(node),
                             self.line_base + node.lineno)
        if isinstance(node, ast.Return):
            return ir.Return(self.expression(node.value, scope),
                             self.line_base + node.lineno)
        if isinstance(node, ast.Pass):
            return None
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return None                     # docstring
        if isinstance(node, ast.AugAssign):
            self.error(node, 'augmented assignment is not hardware; write '
                       'x.next = x + 1')
        self.error(node, f'statement {type(node).__name__} is not supported '
                   'in a process')

    def for_loop (self, node, scope):
        call = node.iter
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == 'range' and 1 <= len(call.args) <= 2):
            self.error(node, 'for loops iterate range() with constant bounds')
        bounds = [self.constant(a, scope, node) for a in call.args]
        start, stop = (0, bounds[0]) if len(bounds) == 1 else bounds
        if not isinstance(node.target, ast.Name):
            self.error(node, 'loop index must be a name')
        scope.locals[node.target.id] = ('index', start, stop)
        body = self.block_body(node.body, scope, node.lineno)
        del scope.locals[node.target.id]
        written = [self.bound_expression(a, scope) for a in call.args]
        start_expr, stop_expr = ((None, written[0]) if len(written) == 1
                                 else (written[0], written[1]))
        return ir.For(node.target.id, start, stop, body,
                      start_expr, stop_expr,
                      self.line_base + node.lineno)

    def match_stmt (self, node, scope):
        subject = self.expression(node.subject, scope)
        arms = []
        for case in node.cases:
            pat = self.match_pattern(case.pattern, subject, scope, node)
            body = self.block_body(case.body, scope, case.pattern.lineno)
            arms.append((pat, body))
        unique = self.match_is_unique(subject, [p for p, _ in arms], node)
        return ir.Match(subject, arms, unique, self.line_base + node.lineno)

    def match_pattern (self, pat, subject, scope, stmt):
        if isinstance(pat, ast.MatchAs) and pat.name is None:
            return None
        if isinstance(pat, ast.MatchClass):
            target = (scope.names.get(pat.cls.id)
                      if isinstance(pat.cls, ast.Name) else None)
            if (target is bits and len(pat.patterns) == 1
                    and isinstance(pat.patterns[0], ast.MatchValue)
                    and isinstance(pat.patterns[0].value, ast.Constant)
                    and isinstance(pat.patterns[0].value.value, str)):
                pattern = pat.patterns[0].value.value
                if any(ch not in '01?' for ch in pattern):
                    self.error(stmt, "bits() pattern holds 0, 1 and ?")
                if len(pattern) != subject.width:
                    self.error(stmt, f"bits({pattern!r}) width {len(pattern)} "
                               f"does not match {subject.width}")
                return ir.Expr('bits', len(pattern), value = pattern)
            self.error(stmt, 'unsupported match class pattern')
        if isinstance(pat, ast.MatchValue):
            return self.expression(pat.value, scope)
        self.error(stmt, 'unsupported match pattern')

    def match_is_unique (self, subject, patterns, stmt):
        concretes = [p for p in patterns if p is not None]
        has_default = any(p is None for p in patterns)
        if concretes and all(p.op == 'enum' for p in concretes):
            names = {p.value.name for p in concretes}
            members = {m.name for m in concretes[0].value.type.members}
            missing = members - names
            if missing and not has_default:
                self.error(stmt, 'match missing enum members: '
                           + ', '.join(sorted(missing)))
            return names == members
        if concretes and all(p.op == 'bits' for p in concretes):
            values = [p.value for p in concretes]
            if len(values) != len(set(values)):
                return False
            if any('?' in p.value for p in concretes):
                return False
            return True
        return False

    def assignment (self, target, value, node, scope):
        tgt = self.target(target, scope, node)
        val = self.expression(value, scope)
        val = self.fit(val, tgt.width, node, self.target_width_expr(tgt))
        return ir.Assign(tgt, val, self.line_base + node.lineno)

    def field_expr (self, sig, fname, stmt):
        if sig.type is None or fname not in sig.type.fields:
            self.error(stmt, f'{self.name_of(sig)} has no field {fname}')
        width = sig.type.fields[fname]
        lo = struct_field_lo(sig.type, fname)
        name = self.name_of(sig)
        return ir.Expr('field', width, args = [
            ir.Expr('ref', sig.width, value = name),
            ir.Expr('const', min_width(max(lo, 0)), value = lo)],
            value = fname)

    def drive_signal (self, sig, stmt):
        name = self.name_of(sig)
        self.drive(name, None, stmt)
        self.assigned_here.append(name)
        return ir.Expr('ref', sig.width, value = name)

    def target (self, node, scope, stmt):
        """x.next, x.next[i], x.next[hi:lo], x.field.next, x.next.field,
        bundle.m.next, local = ..."""
        if isinstance(node, ast.Attribute) and node.attr == 'next':
            if isinstance(node.value, ast.Subscript):
                arr = self.resolve(node.value.value, scope, stmt)
                if isinstance(arr, SignalArray):
                    name = self.name_of(arr)
                    self.drive(name, None, stmt)
                    self.assigned_here.append(name)
                    idx = self.bound_expression(node.value.slice, scope)
                    return ir.Expr('bit', arr.width, args = [
                        ir.Expr('ref', arr.width, value = name), idx])
            base = self.resolve(node.value, scope, stmt)
            if isinstance(base, Signal):
                return self.drive_signal(base, stmt)
            if isinstance(base, tuple) and base[0] == 'field':
                _, sig, fname = base
                self.drive_signal(sig, stmt)
                return self.field_expr(sig, fname, stmt)
            self.error(stmt, '.next on a non-signal: '
                       + ast.unparse(node.value))
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == 'next'):
            base = self.resolve(node.value.value, scope, stmt)
            if isinstance(base, Signal) and base.kind == 'struct':
                self.drive_signal(base, stmt)
                return self.field_expr(base, node.attr, stmt)
        if isinstance(node, ast.Subscript):
            inner = self.target(node.value, scope, stmt)
            if isinstance(node.slice, ast.Slice):
                # the same part-select a read gets (3.6): a byte lane
                # of a word is written where it is read from, and the
                # base is a loop variable or a signal either way
                part = self.part_select(node.slice, inner, scope, stmt)
                if part is not None:
                    return part
                hi, lo, hi_e, lo_e = self.slice_bounds(node.slice, inner,
                                                       scope, stmt)
                return ir.Expr('slice', hi - lo, args = [inner, hi_e, lo_e],
                               value = (hi, lo))
            index = self.bound_expression(node.slice, scope)
            return ir.Expr('bit', 1, args = [inner, index])
        if isinstance(node, ast.Name):
            local = scope.locals.get(node.id)
            if local is not None and local[0] == 'vector':
                return ir.Expr('ref', local[1], value = node.id)
            self.error(stmt, f'assignment to plain name {node.id}: hardware '
                       'is assigned with .next; a function local needs '
                       'vector(W)')
        self.error(stmt, f'unsupported assignment target {ast.unparse(node)}')

    def drive (self, name, target, node):
        owner = self.driven.get(name)
        if owner is not None and owner != self.current:
            message = f'{name} is driven by both {owner} and {self.current}'
            if node is None:
                raise ConversionError(message, self.file)
            self.error(node, message)
        self.driven[name] = self.current

    def target_width_expr (self, target):
        """How the target's width was written, if it was written.

        A cast that folds the width is a cast that stops following the
        generic: 34'(...) in a module whose product_c is [WIDTH+1:0]
        is right at one width and wrong at every other, so overriding
        the generic gives a design Verilator refuses. The expression
        is checked here, where the module's own names are known, and
        each emitter spells it its own way (PARAMETERS.md stage 3).
        """
        if target.op != 'ref':
            return None
        written = self.width_exprs.get(target.value)
        if not written:
            return None
        scope = {**self.e.parameters, **self.e.constants}
        if checked_expression(written, target.width, scope) is None:
            return None
        return written

    def fit (self, value, width, node, width_expr = None):
        """The assignment rule (4.3): widen explicitly, never truncate."""
        if value.op == 'const' and value.value is not None:
            if value.signed and value.value < 0:
                if min_width(value.value) > width:
                    self.error(node, f'constant {value.value} does not fit '
                               f'in {width} bits')
                value.width = width
                return value
            if min_width(value.value) > width:
                self.error(node, f'constant {value.value} does not fit in '
                           f'{width} bits')
            value.width = width
            return value
        if value.op == 'ref':
            number = self.number(value)
            if number is not None:
                if min_width(number) > width:
                    self.error(node, f'{value.value} is {number}, which does '
                               f'not fit in {width} bits')
                # A constant is an unsized integer in Verilog and takes
                # its width from the context, which was enough while a
                # localparam was a small number the tool could fold.
                # It is not enough once the localparam carries its own
                # expression: that is self-determined at integer width
                # and the assignment reads as a truncation, which is
                # what Verilator called WIDTHTRUNC on byte_count <=
                # BYTES_A. Say the width here instead of relying on
                # the tool's context rules, which is what SPEC 4.2
                # asks for everywhere else. VHDL already said it.
                #
                # A hexed() parameter is a vector of a stated width in
                # both languages, so it says its own width already and
                # an extend to the size it is would only be noise.
                if not (isinstance(number, Hexed) and number.bits):
                    return ir.Expr('extend', width, value.signed, [value])
        if value.int_tree:
            # integer arithmetic has no width of its own either, so it
            # is given the one it is read at rather than the one its
            # operands happened to need and a cast on top of that:
            # to_unsigned(WIDTHA - 1, 8), not a resize of a to_unsigned
            #
            # Its width is the operands' plus one, which is what an add
            # of two unknowns needs and more than WIDTH - 1 does. The
            # value is known here, so it is the one that decides.
            folded = self.folded(value)
            if value.width <= width:
                value.width = width
                return value
            if folded is not None:
                if min_width(folded) > width:
                    self.error(node, f'{folded} does not fit in '
                               f'{width} bits')
                value.width = width
                return value
        if value.width > width:
            self.error(node, f'result truncated: {value.width}-bit value '
                       f'assigned to {width} bits; slice it explicitly')
        if value.width < width:
            # a conditional takes its width from the context it lands
            # in, and both tools push that context into the arms: a
            # 13-bit target around (c ? a12 : b12) is nine WIDTHEXPAND
            # warnings, one per arm, against a cast that is right. Widen
            # the arms instead, where the tool is looking
            if value.op == 'ifexp':
                cond, a, b = value.args
                a = self.fit(a, width, node, width_expr)
                b = self.fit(b, width, node, width_expr)
                return ir.Expr('ifexp', width, a.signed and b.signed,
                               [cond, a, b])
            return ir.Expr('extend', width, value.signed, [value],
                           width_expr = width_expr)
        return value

    # ---- expressions ------------------------------------------------------
    def resolve (self, node, scope, stmt):
        """The elaboration object a name/attribute denotes, or None."""
        if isinstance(node, ast.Name):
            if node.id in scope.locals:
                return ('local', node.id) + tuple(scope.locals[node.id])
            return scope.names.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value, scope, stmt)
            if isinstance(base, SimpleNamespace):
                return getattr(base, node.attr, None)
            if isinstance(base, EnumType):
                return getattr(base, node.attr)
            if (isinstance(base, Signal) and base.kind == 'struct'
                    and base.type is not None
                    and node.attr in base.type.fields):
                return ('field', base, node.attr)
            return None
        if isinstance(node, ast.Subscript):
            base = self.resolve(node.value, scope, stmt)
            if isinstance(base, (SignalArray, list)):
                index = self.constant_or_none(node.slice, scope)
                if index is not None and not isinstance(node.slice, ast.Slice):
                    return base[index]
                return base
            return None
        return None

    def constant_or_none (self, node, scope):
        try:
            names = {k: v for k, v in scope.names.items()
                     if isinstance(v, (int, bool))
                     and not isinstance(v, (Signal, Rtl))}
            for k, v in scope.locals.items():
                if v[0] == 'const':
                    names[k] = v[1]
            code = compile(ast.Expression(body = node), '<const>', 'eval')
            value = eval(code, {'__builtins__': {'len': len, 'max': max,
                                                 'min': min,
                                                 'int': int}}, names)
        except Exception:
            return None
        if isinstance(value, bool):
            return int(value)
        return value if isinstance(value, int) else None

    def constant (self, node, scope, stmt):
        value = self.constant_or_none(node, scope)
        if value is None:
            self.error(stmt, f'{ast.unparse(node)} is not a constant')
        return value

    def bound_expression (self, node, scope):
        """Slice/bit bounds are elaboration arithmetic, not hardware + -."""
        saved = self.kind
        self.kind = 'comb'
        try:
            return self.expression(node, scope)
        finally:
            self.kind = saved

    def slice_bounds (self, sl, base, scope, stmt):
        # Verilog numbering: sig[hi:lo] inclusive, ast.Slice(lower = hi,
        # upper = lo). Returns the exclusive (hi + 1, lo) pair for widths.
        width = base.width
        if sl.lower is None or sl.upper is None:
            self.error(stmt, 'open slices do not exist: write sig[hi:0] or '
                       'sig[len(sig)-1:lo]')
        hi = self.constant(sl.lower, scope, stmt)
        lo = self.constant(sl.upper, scope, stmt)
        if hi < lo:
            self.error(stmt, f'reversed slice [{hi}:{lo}]')
        if not 0 <= lo <= hi < width:
            self.error(stmt, f'slice [{hi}:{lo}] outside bits {width - 1}:0')
        return (hi + 1, lo,
                self.bound_expression(sl.lower, scope),
                self.bound_expression(sl.upper, scope))

    def expression (self, node, scope):
        e = self._expression(node, scope)
        e.line = self.line_base + getattr(node, 'lineno', 0)
        return e

    def is_sized (self, node, scope):
        """True if this argument said how wide it is.

        True and False are one bit and say so. const(value, width)
        says so in as many words, and warning that it took the width
        it was given would be nonsense.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return scope.names.get(node.func.id) is const
        return False

    def literal_base (self, node):
        """How the number was written, so it can be written that way
        again. Hex in the Python is hex in the HDL: the same argument
        as names travelling and comments travelling."""
        line = getattr(node, 'lineno', 0)
        if not line or line - 1 >= len(self.source_lines):
            return None
        text = self.source_lines[line - 1][
            getattr(node, 'col_offset', 0):
            getattr(node, 'end_col_offset', 0)].strip().lower()
        if text.startswith('0x'):
            return 'hex'
        if text.startswith('0b'):
            return 'bin'
        if text.startswith('0o'):
            return 'oct'
        return None

    def _expression (self, node, scope):
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return ir.Expr('const', 1, value = int(v))
            if isinstance(v, int):
                return ir.Expr('const', min_width(v), v < 0, value = v,
                               base = self.literal_base(node))
            self.error(node, f'unsupported constant {v!r}')
        if isinstance(node, (ast.Name, ast.Attribute)):
            obj = self.resolve(node, scope, node)
            if isinstance(obj, Signal):
                name = self.name_of(obj)
                self.read.add(name)
                return ir.Expr('ref', obj.width, kind_signed(obj),
                               value = name)
            if isinstance(obj, tuple) and obj[0] == 'field':
                _, sig, fname = obj
                self.read.add(self.name_of(sig))
                return self.field_expr(sig, fname, node)
            if isinstance(obj, EnumMember):
                return ir.Expr('enum', obj.type.width, value = obj)
            if isinstance(obj, bool):
                return ir.Expr('const', 1, value = int(obj))
            if isinstance(obj, int):
                if isinstance(node, ast.Name) and (node.id in self.e.constants
                        or node.id in self.e.parameters):
                    width = min_width(obj)
                    if (isinstance(obj, Hexed) and obj.bits):
                        width = obj.bits
                    return ir.Expr('ref', width, obj < 0, value = node.id)
                return ir.Expr('const', min_width(obj), obj < 0, value = obj)
            if isinstance(obj, tuple) and obj[0] == 'local':
                _, name, kind, *rest = obj
                if kind == 'index':
                    return ir.Expr('ref', max(1, rest[1].bit_length()),
                                   value = name)
                if kind == 'vector':
                    return ir.Expr('ref', rest[0], value = name)
                if kind == 'param':
                    return ir.Expr('ref', rest[0], rest[1], value = name)
                if kind == 'const':
                    return ir.Expr('const', min_width(rest[0]),
                                   value = rest[0])
            self.error(node, f'unknown name {ast.unparse(node)}')
        if isinstance(node, ast.Subscript):
            return self.subscript(node, scope)
        if isinstance(node, ast.UnaryOp):
            operand = self.expression(node.operand, scope)
            if isinstance(node.op, ast.Not):
                if operand.width != 1:
                    self.error(node, 'not applies to 1-bit values; use ~ '
                               'for a vector')
                return ir.Expr('not', 1, args = [operand])
            if isinstance(node.op, ast.Invert):
                return ir.Expr('unop', operand.width, operand.signed,
                               [operand], '~')
            if isinstance(node.op, ast.USub):
                if operand.op == 'const':
                    v = -operand.value
                    return ir.Expr('const', min_width(v), v < 0, value = v)
                return ir.Expr('unop', operand.width, True, [operand], '-')
            self.error(node, 'unary operator not supported')
        if isinstance(node, ast.BinOp):
            return self.binop(node, scope)
        if isinstance(node, ast.BoolOp):
            self.error(node, 'and/or are not hardware operators; use & and |')
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                self.error(node, 'chained comparison')
            left = self.expression(node.left, scope)
            right = self.expression(node.comparators[0], scope)
            op = {ast.Eq: '==', ast.NotEq: '!=', ast.Lt: '<', ast.LtE: '<=',
                  ast.Gt: '>', ast.GtE: '>='}.get(type(node.ops[0]))
            if op is None:
                self.error(node, 'comparison operator not supported')
            left, right = self.context(left, right, node)
            if left.width != right.width:
                wide = max(left.width, right.width)
                if left.width < wide:
                    left = ir.Expr('extend', wide, left.signed, [left])
                if right.width < wide:
                    right = ir.Expr('extend', wide, right.signed, [right])
            return ir.Expr('cmp', 1, args = [left, right], value = op)
        if isinstance(node, ast.IfExp):
            cond = self.expression(node.test, scope)
            a = self.expression(node.body, scope)
            b = self.expression(node.orelse, scope)
            a, b = self.context(a, b, node)
            return ir.Expr('ifexp', max(a.width, b.width),
                           a.signed and b.signed,
                           [cond, a, b])
        if isinstance(node, ast.Call):
            return self.call(node, scope)
        self.error(node, f'expression {type(node).__name__} not supported')

    def number (self, e):
        """The value behind an expression that is a plain number: a
        literal, a block constant, or a parameter. None if it is not
        one.

        All three are unsized and take the width of the place they are
        used. Padding a parameter out to a width its value happened to
        need would make two builds of a block differ in nothing but the
        padding, and that is two modules in the emitted HDL where the
        source says one."""
        if e.op == 'const' and e.value is not None:
            return e.value
        if e.op == 'ref':
            value = self.e.parameters.get(
                e.value, self.e.constants.get(e.value))
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    def context (self, a, b, node):
        """Give an unsized number the width of its partner (4.2)."""
        a_number = self.number(a)
        b_number = self.number(b)
        if a_number is not None and b_number is not None:
            # two numbers: neither has a width of its own, so they take
            # the wider of what they need and nothing is padded
            width = max(a.width, b.width)
            a.width = width
            b.width = width
            # a named one is an integer in both languages, so the
            # literal beside it is integer arithmetic too and a size
            # on it is the width Verilator argues about: WIDTHA - 4'd1
            # is a SUB wanting 32 bits on the right
            if a.op == 'ref' or b.op == 'ref':
                for side in (a, b):
                    if side.op == 'const':
                        side.unsized = True
        elif a_number is not None:
            if min_width(a_number) > b.width:
                self.error(node, f'constant {a_number} wider than '
                           f'{b.width}-bit '
                           'operand')
            a.width = b.width
        elif b_number is not None:
            if min_width(b_number) > a.width:
                self.error(node, f'constant {b_number} wider than '
                           f'{a.width}-bit '
                           'operand')
            b.width = a.width
        return a, b

    def binop (self, node, scope):
        op = {ast.BitAnd: '&', ast.BitOr: '|', ast.BitXor: '^', ast.Add: '+',
              ast.Sub: '-', ast.LShift: '<<', ast.RShift: '>>',
              ast.Mult: '*'}.get(type(node.op))
        if op is None:
            self.error(node, 'operator not supported in hardware')
        left = self.expression(node.left, scope)
        right = self.expression(node.right, scope)
        if op in ('<<', '>>'):
            return ir.Expr('binop', left.width, left.signed, [left, right], op)
        if left.op == 'const' and right.op == 'const':
            v = {'&': left.value & right.value, '|': left.value | right.value,
                 '^': left.value ^ right.value, '+': left.value + right.value,
                 '-': left.value - right.value,
                 '*': left.value * right.value}[op]
            return ir.Expr('const', min_width(v), v < 0, value = v)
        # * is a scale (part-select index): a constant need not fit in
        # the other operand. & | ^ + - share a width, so context applies.
        if op != '*':
            left, right = self.context(left, right, node)
        signed = left.signed and right.signed
        self.check_signedness(op, left, right, node)
        if op in ('&', '|', '^'):
            if (left.width != right.width
                    and 1 not in (left.width, right.width)):
                self.warnings.append(f'{self.current}: {op} on '
                                     f'{left.width} and '
                                     f'{right.width} bits at line '
                                     f'{self.line_base + node.lineno}')
            return ir.Expr('binop', max(left.width, right.width), signed,
                           [left, right], op)
        if op in ('+', '-'):
            out = ir.Expr('binop', max(left.width, right.width) + 1, signed,
                          [left, right], op)
        else:
            out = ir.Expr('binop', left.width + right.width, signed,
                          [left, right], op)
        out.int_tree = self.integer_tree(left) and self.integer_tree(right)
        return out

    def integer_tree (self, e):
        """Nothing but named constants and literals, so both languages
        do it at integer width and neither gives the result a size."""
        if e.int_tree:
            return True
        return e.op in ('const', 'ref') and self.number(e) is not None

    def folded (self, e):
        """What an expression of named constants and literals comes to,
        or None where some part of it is not one."""
        number = self.number(e)
        if number is not None:
            return number
        if e.op != 'binop' or e.value not in ('+', '-', '*'):
            return None
        left = self.folded(e.args[0])
        right = self.folded(e.args[1])
        if left is None or right is None:
            return None
        if e.value == '+':
            return left + right
        if e.value == '-':
            return left - right
        return left * right

    SIGNED_OPS = ('+', '-', '*', '<', '<=', '>', '>=')

    def check_signedness (self, op, left, right, node):
        """One operand signed and the other not.

        Signedness lives at the use site, which is the right model:
        a vector is unsigned until .signed() where it is used. It also
        makes it easy to sign one side of a subtract or a magnitude
        compare and not the other, and the three backends have
        disagreed about exactly that before. A one-bit operand or a
        constant is not worth a warning; two vectors are.
        """
        if op not in self.SIGNED_OPS:
            return
        if left.signed == right.signed:
            return
        if left.width == 1 or right.width == 1:
            return
        if 'const' in (left.op, right.op):
            return
        odd = 'left' if left.signed else 'right'
        self.warnings.append(
            f'{self.current}: {op} with only its {odd} operand signed, '
            f'at line {self.line_base + node.lineno}. Sign both or '
            'neither: a mixed compare is the one that is wrong in one '
            'language and right in another.')

    def assert_message (self, node):
        """The text after the comma in an assert, or None.

        It has to be a plain string. The message is emitted into the
        SystemVerilog and the VHDL as a literal, so it cannot be built
        at run time from anything the hardware knows, and an f-string
        or a concatenation here would look like it would work."""
        if node.msg is None:
            return None
        if (isinstance(node.msg, ast.Constant)
                and isinstance(node.msg.value, str)):
            text = node.msg.value
            if '"' in text or '\\' in text or '\n' in text:
                self.error(node, 'assert message must be plain text with '
                           'no quote, backslash or newline: it is emitted '
                           'as a literal in two languages')
            return text
        self.error(node, 'assert message must be a plain string, not '
                   + ast.unparse(node.msg) + '. It becomes a literal in '
                   'the emitted HDL, so nothing about it can be worked '
                   'out while the design is running.')
        return None

    def subscript (self, node, scope):
        base_obj = self.resolve(node.value, scope, node)
        if (isinstance(base_obj, (SignalArray, list))
                and not isinstance(node.slice, ast.Slice)):
            index = self.constant_or_none(node.slice, scope)
            if index is not None:
                # sample[STAGES-1] names an element of the array the
                # HDL declares, so it is indexed there rather than
                # resolved to the element this build happens to mean
                written = self.bound_expression(node.slice, scope)
                if written is not None and written.op != 'const':
                    name = (self.name_of(base_obj)
                            if isinstance(base_obj, SignalArray)
                            else ast.unparse(node.value))
                    self.read.add(name)
                    # the element it resolves to, for whatever still
                    # has to know which one: the asynchronous reset
                    # of a process is named, not indexed
                    element = self.name_of(base_obj[index])
                    return ir.Expr('bit', base_obj.width, args = [
                        ir.Expr('ref', base_obj.width, value = name),
                        written], value = element)
                element = base_obj[index]
                name = self.name_of(element)
                self.read.add(name)
                return ir.Expr('ref', element.width, value = name)
            idx = self.expression(node.slice, scope)
            name = (self.name_of(base_obj) if isinstance(base_obj, SignalArray)
                    else ast.unparse(node.value))
            self.read.add(name)
            return ir.Expr('bit', base_obj.width, args = [
                ir.Expr('ref', base_obj.width, value = name), idx])
        base = self.expression(node.value, scope)
        if isinstance(node.slice, ast.Slice):
            sl = node.slice
            part = self.part_select(sl, base, scope, node)
            if part is not None:
                return part
            hi, lo, hi_e, lo_e = self.slice_bounds(sl, base, scope, node)
            return ir.Expr('slice', hi - lo, args = [base, hi_e, lo_e],
                           value = (hi, lo))
        index = self.constant_or_none(node.slice, scope)
        if index is not None:
            if not 0 <= index < base.width:
                self.error(node, f'bit {index} outside {base.width} bits')
            if base.width == 1:
                # the only bit of a one-bit signal is the signal, and
                # neither language will select a bit of a scalar
                return base
            return ir.Expr('bit', 1, args = [base, self.bound_expression(
                node.slice, scope)])
        if base.width == 1:
            return base
        idx = self.expression(node.slice, scope)
        return ir.Expr('bit', 1, args = [base, idx])

    def part_select (self, sl, base, scope, node):
        """sig[b*W + W : b*W] with a signal base -> part-select (3.6)."""
        if sl.upper is None or sl.lower is None:
            return None
        hi_node, lo_node = sl.lower, sl.upper
        if self.constant_or_none(lo_node, scope) is not None:
            return None
        # hi must be lo + (W-1) with W constant: sig[base + W-1:base]
        if (isinstance(hi_node, ast.BinOp) and isinstance(hi_node.op, ast.Add)
                and ast.dump(hi_node.left) == ast.dump(lo_node)):
            offset = self.constant_or_none(hi_node.right, scope)
            if offset is not None:
                index = self.expression(lo_node, scope)
                return ir.Expr('part', offset + 1, args = [base, index],
                               value = offset + 1)
        self.error(node, 'a slice with a variable bound must be '
                   'sig[base + W-1:base] with constant W, or '
                   'sig.part(base, W)')

    def call (self, node, scope):
        func = node.func
        if isinstance(func, ast.Attribute):
            base = self.resolve(func.value, scope, node)
            if func.attr == 'signed':
                inner = self.expression(func.value, scope)
                return ir.Expr('signed', inner.width, True, [inner])
            if func.attr == 'part' and isinstance(base, Signal):
                index = self.expression(node.args[0], scope)
                width = self.constant(node.args[1], scope, node)
                name = self.name_of(base)
                self.read.add(name)
                return ir.Expr('part', width, args = [
                    ir.Expr('ref', base.width, value = name), index],
                   value = width)
            if func.attr == 'part_down' and isinstance(base, Signal):
                index = self.expression(node.args[0], scope)
                width = self.constant(node.args[1], scope, node)
                name = self.name_of(base)
                self.read.add(name)
                return ir.Expr('part_down', width, args = [
                    ir.Expr('ref', base.width, value = name), index],
                    value = width)
            self.error(node, f'method {func.attr} not supported')
        target = (scope.names.get(func.id)
                  if isinstance(func, ast.Name) else None)
        if target is concat:
            parts = [self.expression(a, scope) for a in node.args]
            for a, p in zip(node.args, parts):
                if p.op != 'const' or self.is_sized(a, scope):
                    continue
                self.warnings.append(f'{self.current}: unsized constant '
                                     f'{p.value} in concat takes '
                                     f'{p.width} bits')
            return ir.Expr('concat', sum(p.width for p in parts), args = parts)
        if target is sign_extend:
            inner = self.expression(node.args[0], scope)
            width = self.constant(node.args[1], scope, node)
            if width < inner.width:
                self.error(node, f'sign_extend(): {inner.width} bits do '
                           f'not fit in {width}. The second argument is '
                           'the width of the answer, not how many bits '
                           'to add.')
            if width == inner.width:
                return inner
            # the width of the answer as it was written, so
            # sign_extend(x, WIDTH + 1) follows the generic
            return ir.Expr('extend', width, True, [inner],
                           width_expr = ast.unparse(node.args[1]))
        if target is ones or target is zeroes:
            count = self.constant(node.args[0], scope, node)
            times = self.bound_expression(node.args[0], scope)
            bit = ir.Expr('const', 1, value = 1 if target is ones else 0)
            return ir.Expr('replicate', count, args = [bit, times],
                           value = count)
        if target is replicate:
            inner = self.expression(node.args[0], scope)
            count = self.constant(node.args[1], scope, node)
            # the count as written, so replicate(True, STAGES) follows
            # the generic instead of freezing at this build's STAGES
            times = self.bound_expression(node.args[1], scope)
            return ir.Expr('replicate', inner.width * count,
                           args = [inner, times], value = count)
        if target is len:
            obj = self.resolve(node.args[0], scope, node)
            if obj is None or not hasattr(obj, 'width'):
                self.error(node, 'len() of a non-signal')
            return ir.Expr('const', min_width(obj.width), value = obj.width)
        if target is bits:
            pattern = node.args[0].value
            return ir.Expr('bits', len(pattern), value = pattern)
        if target is const:
            if len(node.args) != 2:
                self.error(node, 'const(value, width): a number and the '
                           'width to carry it in')
            value = self.constant(node.args[0], scope, node)
            width = self.constant(node.args[1], scope, node)
            if width < 1:
                self.error(node, f'const(): width {width} is not positive')
            if min_width(value) > width:
                self.error(node, f'const(): {value} does not fit in '
                           f'{width} bits')
            # a negative value is the pattern it makes in that width,
            # which is what the docstring promises and what the
            # emitters can write. const(-1, 8) is eight ones
            return ir.Expr('const', width,
                           value = value & ((1 << width) - 1),
                           base = self.literal_base(node.args[0]))
        if target is vector:
            self.error(node, 'vector(W) declares a function local: '
                       'name = vector(W)')
        if isinstance(target, FunctionType):
            if self.kind == 'ff':
                self.error(node, f'{func.id}() in a clocked process: call it '
                           'from a comb process and select the result here')
            return self.function_call(func.id, target, node, scope)
        text = ast.unparse(func)
        # the block body is read from the AST, so a name that was never
        # imported does not raise where Python would: it arrives here
        # as a call to nothing, and saying so saves reading the line
        if isinstance(func, ast.Name) and func.id in house_names():
            self.error(node, f'{text}() is an isomorph name that this '
                       'file does not import. Add it to the '
                       'from isomorph import (...) line.')
        self.error(node, f'call to {text} is not supported')

    def function_call (self, name, func, node, scope):
        args = [self.expression(a, scope) for a in node.args]
        f = self.function(name, func, args)
        return ir.Expr('call', f.width, f.signed, args, name)

    def function (self, name, func, args):
        key = (name, tuple((a.width, a.signed) for a in args))
        if key in self.functions:
            return self.functions[key]
        node = self._tree(func)
        saved = (self.line_base, self.source_lines, self.current)
        fscope = Scope(func, self.e)
        params = []
        names = [a.arg for a in node.args.args]
        if len(names) != len(args):
            raise ConversionError(f'{name}: expected {len(names)} arguments')
        for pname, a in zip(names, args):
            fscope.locals[pname] = ('param', a.width, a.signed)
            params.append((pname, a.width, a.signed))
        self.current = name
        locals_ = {}
        rest = []
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign)
                    and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and fscope.names.get(stmt.value.func.id) is vector):
                width = self.constant(stmt.value.args[0], fscope, stmt)
                lname = stmt.targets[0].id
                fscope.locals[lname] = ('vector', width)
                locals_[lname] = width
                continue
            rest.append(stmt)
        body = self.block_body(rest, fscope, node.lineno)
        returns = [s for s in body if isinstance(s, ir.Return)]
        if not returns:
            raise ConversionError(f'{name}: a function returns one value')
        f = ir.Function(name, params, locals_, body, returns[-1].value.width,
                        returns[-1].value.signed)
        self.functions[key] = f
        self.line_base, self.source_lines, self.current = saved
        return f

    # ---- checks -----------------------------------------------------------
    def check_complete (self, body, name, proc):
        """Every output of a combinational process assigned on every path
        (house rule 5). A miss is an inferred latch: severe warning (4.7)."""
        def assigned(stmts):
            names = set()
            for s in stmts:
                if isinstance(s, ir.Assign):
                    names.add(root(s.target))
                elif isinstance(s, ir.If):
                    sets = [assigned(b) for _, b in s.branches]
                    if s.branches and s.branches[-1][0] is None:
                        names |= set.intersection(*sets) if sets else set()
                elif isinstance(s, ir.Match):
                    sets = [assigned(b) for _, b in s.arms]
                    if match_covers_all(s) and sets:
                        names |= set.intersection(*sets)
                elif isinstance(s, ir.For):
                    names |= assigned(s.body)
            return names
        def all_targets(stmts):
            names = set()
            for s in stmts:
                if isinstance(s, ir.Assign):
                    names.add(root(s.target))
                elif isinstance(s, ir.If):
                    for _, b in s.branches:
                        names |= all_targets(b)
                elif isinstance(s, ir.Match):
                    for _, b in s.arms:
                        names |= all_targets(b)
                elif isinstance(s, ir.For):
                    names |= all_targets(s.body)
            return names
        missing = all_targets(body) - assigned(body)
        if missing:
            self.warnings.append(
                'severe: ' + name + ': latch: not assigned on every path: '
                + ', '.join(sorted(missing))
                + ' (inferred latch)')

    def check_rbw (self, body, driven, proc):
        """No read of a combinational signal this process drives before
        that signal is assigned on this path (SPEC 4.7)."""
        driven = {n.split('[')[0] for n in driven}

        def refs (e):
            names = set()
            if e is None:
                return names
            if e.op == 'ref':
                names.add(str(e.value).split('[')[0])
            for arg in e.args:
                names |= refs(arg)
            return names

        def walk (stmts, assigned):
            for s in stmts:
                if isinstance(s, ir.Assign):
                    seen = refs(s.value)
                    if s.target.op in ('bit', 'slice', 'part', 'part_down'):
                        for arg in s.target.args[1:]:
                            seen |= refs(arg)
                    bad = (seen & driven) - assigned
                    tgt = root(s.target).split('[')[0]
                    if bad and bad <= {tgt}:
                        assigned.add(tgt)
                        continue
                    if bad:
                        raise ConversionError(
                            f'{proc.name}: {", ".join(sorted(bad))} read '
                            f'before assigned in this process',
                            self.file, s.line)
                    assigned.add(tgt)
                elif isinstance(s, ir.If):
                    if len(s.branches) == 1 and s.branches[0][0] is None:
                        walk(s.branches[0][1], assigned)
                        continue
                    after = []
                    for cond, b in s.branches:
                        if cond is not None:
                            bad = (refs(cond) & driven) - assigned
                            if bad:
                                raise ConversionError(
                                    f'{proc.name}: {", ".join(sorted(bad))} '
                                    f'read before assigned in this process',
                                    self.file, s.line)
                        branch_set = set(assigned)
                        walk(b, branch_set)
                        after.append(branch_set)
                    if s.branches and s.branches[-1][0] is None and after:
                        assigned |= set.intersection(*after)
                elif isinstance(s, ir.Match):
                    bad = (refs(s.subject) & driven) - assigned
                    if bad:
                        raise ConversionError(
                            f'{proc.name}: {", ".join(sorted(bad))} read '
                            f'before assigned in this process',
                            self.file, s.line)
                    after = []
                    for _, b in s.arms:
                        branch_set = set(assigned)
                        walk(b, branch_set)
                        after.append(branch_set)
                    if match_covers_all(s) and after:
                        assigned |= set.intersection(*after)
                elif isinstance(s, ir.For):
                    walk(s.body, assigned)
                elif isinstance(s, ir.Assert):
                    bad = (refs(s.cond) & driven) - assigned
                    if bad:
                        raise ConversionError(
                            f'{proc.name}: {", ".join(sorted(bad))} read '
                            f'before assigned in this process',
                            self.file, s.line)
                elif isinstance(s, ir.Return):
                    bad = (refs(s.value) & driven) - assigned
                    if bad:
                        raise ConversionError(
                            f'{proc.name}: {", ".join(sorted(bad))} read '
                            f'before assigned in this process',
                            self.file, s.line)
        walk(body, set())

    def check_comb_loops (self, mod):
        """Combinational cycles, ring oscillators included.

        A statement in a comb process runs in order, the way a blocking
        assignment does, so reading a signal the same process assigned
        further up is not a loop: it is the house idiom of a legal base
        value and then stacked overrides. Only a read that reaches back
        past every assignment in this process depends on the outside
        world, and only those edges can close a ring.

        So `o.next = ~o` is a loop and is reported, `o.next = 0` then
        `o.next = o | x` is not, and a cycle through two processes
        still is. Reading a signal before this process drives it is a
        separate check, check_rbw."""
        deps = {}

        def add_dep (target, reads):
            t = str(target).split('[')[0]
            deps.setdefault(t, set())
            for r in reads:
                n = str(r).split('[')[0]
                if n:
                    deps[t].add(n)

        def walk (stmts, cond_reads, settled):
            for s in stmts:
                if isinstance(s, ir.Assign):
                    reads = (expr_refs(s.value) | cond_reads) - settled
                    add_dep(root(s.target), reads)
                    settled.add(str(root(s.target)).split('[')[0])
                elif isinstance(s, ir.If):
                    for cond, body in s.branches:
                        extra = set(cond_reads)
                        if cond is not None:
                            extra |= expr_refs(cond)
                        walk(body, extra, settled)
                elif isinstance(s, ir.Match):
                    extra = cond_reads | expr_refs(s.subject)
                    for _, body in s.arms:
                        walk(body, extra, settled)
                elif isinstance(s, ir.For):
                    walk(s.body, cond_reads, settled)

        # a continuous assignment is its own scope and has no order to
        # hide behind: assign(o, lambda: o | x) really is a loop
        for a in mod.assigns:
            add_dep(root(a.target), expr_refs(a.value))
        for p in mod.processes:
            if p.kind == 'comb':
                walk(p.body, set(), set())

        graph = {}
        for write, reads in deps.items():
            for r in reads:
                graph.setdefault(r, set()).add(write)
        for cycle in _find_cycles(graph):
            self.warnings.append(
                'severe: combinational loop: '
                + ' -> '.join(cycle)
                + ' (no simulator settles this, and no fitter can '
                'build it)')

    def check_unused (self, mod):
        """Every name that is not connected at both ends (SPEC 4.7).

        There are three ways a name can be wrong and this used to
        catch one of them. It asked whether a name was read or driven,
        so a signal read by something and driven by nothing - a
        floating input, the worst of the three, because it simulates as
        zero and builds as whatever the fitter leaves - counted as used
        and was never mentioned. So did a signal driven by something
        and read by nothing, which is logic that reaches no pin.

        An input port is driven from outside and an output port is read
        from outside, so neither is judged on the half that happens
        somewhere else.
        """
        read = {name.split('[')[0] for name in self.read}
        driven = {name.split('[')[0] for name in self.driven}

        for p in mod.ports:
            if p.attributes.get('unused'):
                continue
            base = p.name.split('[')[0]
            if base not in read and base not in driven:
                self.warnings.append(f'unused port {p.name}')
            elif p.direction == 'out' and base not in driven:
                self.warnings.append(
                    f'undriven output {p.name}: nothing in here drives '
                    'it, so it leaves the block floating')

        for s in mod.signals:
            if s.attributes.get('unused'):
                continue
            base = s.name.split('[')[0]
            if base not in read and base not in driven:
                self.warnings.append(f'unused signal {s.name}')
            elif base not in driven and s.init is None:
                # a memory filled by preload() is driven by that: the
                # contents are a declaration initialiser in the HDL and
                # what a configured block RAM comes up holding
                self.warnings.append(
                    f'undriven signal {s.name}: something reads it and '
                    'nothing drives it')
            elif base not in read:
                self.warnings.append(
                    f'unread signal {s.name}: something drives it and '
                    'nothing reads it')


def indexed_base (texts):
    """The array every member indexes by its own position, or None.

    ['d[0]', 'd[1]', 'd[2]'] is the array d. Anything else, including
    an order that is not 0, 1, 2, is not one."""
    base = None
    for index, text in enumerate(texts):
        if not text or not text.endswith(f'[{index}]'):
            return None
        here = text[:text.rindex('[')]
        if base is None:
            base = here
        elif here != base:
            return None
    return base


def genvar_name (mod, spent = ()):
    """A loop index no other name in the module has taken.

    The genvar is declared at module level rather than inside the for,
    because that is the only spelling Quartus Prime Standard parses
    (25.1std, measured 2026-09-12; it rejects `for (genvar k = 0...)`
    outright). So it shares a namespace with everything else here, and
    with the other arrays."""
    taken = {p.name for p in mod.ports} | {s.name for s in mod.signals}
    taken |= set(mod.constants) | set(mod.parameters) | set(mod.enums)
    taken |= {p.name for p in mod.processes} | set(spent)
    for kind in mod.enums.values():
        taken |= {member.name for member in kind.members}
    for name in ['k'] + [f'k{n}' for n in range(100)]:
        if name not in taken:
            return name
    return 'k'


def root (expr):
    while expr.op in ('slice', 'bit', 'part', 'part_down', 'field'):
        expr = expr.args[0]
    return expr.value


# The attribute that says a flop is there to resolve metastability.
# The house style's synchroniser block writes five of them, one per
# vendor; this is the one that means synchroniser rather than merely
# keep, and it is what marks a crossing as deliberate.
SYNC_ATTRIBUTE = 'async_reg'


def body_reads (body):
    """Every signal read anywhere in these statements."""
    names = set()
    for s in body or []:
        if isinstance(s, ir.Assign):
            names |= expr_refs(s.value)
            # an indexed target reads its index
            target = s.target
            while target.op in ('slice', 'bit', 'part', 'part_down',
                                'field'):
                for arg in target.args[1:]:
                    names |= expr_refs(arg)
                target = target.args[0]
        elif isinstance(s, ir.If):
            for cond, inner in s.branches:
                names |= expr_refs(cond)
                names |= body_reads(inner)
        elif isinstance(s, ir.Match):
            names |= expr_refs(s.subject)
            for _, inner in s.arms:
                names |= body_reads(inner)
        elif isinstance(s, ir.For):
            names |= body_reads(s.body)
        elif isinstance(s, ir.Assert):
            names |= expr_refs(s.cond)
        elif isinstance(s, ir.Return):
            names |= expr_refs(s.value)
    return names


def body_writes (body):
    """Every signal assigned anywhere in these statements."""
    names = set()
    for s in body or []:
        if isinstance(s, ir.Assign):
            names.add(root_name(s.target))
        elif isinstance(s, ir.If):
            for _, inner in s.branches:
                names |= body_writes(inner)
        elif isinstance(s, ir.Match):
            for _, inner in s.arms:
                names |= body_writes(inner)
        elif isinstance(s, ir.For):
            names |= body_writes(s.body)
    return names


def root_name (expr):
    while expr.op in ('slice', 'bit', 'part', 'part_down', 'field'):
        expr = expr.args[0]
    return str(expr.value).split('[')[0] if expr.op == 'ref' else ''


def expr_refs (e):
    names = set()
    if e is None:
        return names
    if e.op == 'ref':
        names.add(str(e.value).split('[')[0])
    for arg in getattr(e, 'args', []) or []:
        names |= expr_refs(arg)
    return names


def _find_cycles (graph):
    """graph: src -> dests. Return simple cycles as name lists, cap 8."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    stack = []
    found = []

    def dfs (n):
        if len(found) >= 8:
            return
        color[n] = GRAY
        stack.append(n)
        for m in graph.get(n, ()):
            c = color.get(m, WHITE)
            if c == WHITE:
                dfs(m)
            elif c == GRAY:
                i = stack.index(m)
                found.append(stack[i:] + [m])
        stack.pop()
        color[n] = BLACK

    for n in list(graph):
        if color.get(n, WHITE) == WHITE:
            dfs(n)
    return found


def struct_field_lo (struct_type, field_name):
    """Low bit of a packed-struct field; first field is the MSB."""
    msb = struct_type.width
    for fname, fwidth in struct_type.fields.items():
        lo = msb - fwidth
        if fname == field_name:
            return lo
        msb = lo
    return 0


def match_covers_all (s):
    """Default arm, or unique enum match listing every member (4.7)."""
    if not s.arms:
        return False
    if s.arms[-1][0] is None:
        return True
    if s.unique:
        concretes = [p for p, _ in s.arms if p is not None]
        return bool(concretes) and all(p.op == 'enum' for p in concretes)
    return False


def kind_signed (signal):
    return False


def signature_lines (func):
    """argument name -> the source line it is written on, absolute.

    Ports carry their role comment in the block signature, which no
    other pass looks at."""
    out = {}
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return out
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                None)
    if node is None:
        return out
    base = func.__code__.co_firstlineno - 1
    args = list(node.args.args) + list(node.args.kwonlyargs)
    previous = base + node.lineno
    for a in args:
        out[a.arg] = base + a.lineno
        out['__previous__' + a.arg] = previous
        previous = base + a.lineno
    return out


def file_lines (path):
    """Every line of a source file, or nothing if it cannot be read."""
    try:
        with tokenize.open(path) as handle:
            return handle.read().splitlines()
    except (OSError, TypeError, ValueError):
        return []


def source_comments (func):
    """line -> (is_full_line, text) for every comment in the file that
    defines func."""
    comments = {}
    try:
        path = inspect.getsourcefile(func)
        with tokenize.open(path) as f:
            tokens = list(tokenize.generate_tokens(f.readline))
    except (OSError, TypeError):
        try:
            lines = inspect.getsourcelines(func)[0]
            base = func.__code__.co_firstlineno - 1
            import io
            stream = io.StringIO(''.join(lines))
            tokens = list(tokenize.generate_tokens(stream.readline))
            for t in tokens:
                if t.type == tokenize.COMMENT:
                    full = t.line.strip().startswith('#')
                    comments[base + t.start[0]] = (full,
                                                   t.string)
            return comments
        except Exception:
            return comments
    for t in tokens:
        if t.type == tokenize.COMMENT:
            comments[t.start[0]] = (t.line.strip().startswith('#'), t.string)
    return comments


def header_comment (func):
    """The block's docstring, or failing that its module's (SPEC 4.1).

    One block per file is the house style, so a file whose docstring
    describes the design gets that text as the module header."""
    own = inspect.getdoc(func)
    if own:
        return own
    name = getattr(func, '__module__', None)
    module = sys.modules.get(name) if name else None
    text = module.__doc__ if module is not None else None
    return inspect.cleandoc(text) if text else ''


FATAL_SEVERE = ('latch:', 'combinational loop:')


def fatal_warnings (warnings):
    """The severe warnings that stop the run.

    An inferred latch and a combinational loop are design errors, not
    style. The Python and C99 simulators hold the previous value across
    a missing path, so a design with a latch passes its bench and fails
    in silicon, which is the MyHDL wound this project is a reaction to.
    A loop settles in no simulator and builds in no fitter.

    An asynchronous reset is neither. always_ff_async_reset is the
    construct you have to call out on purpose, it will not compile
    without a reason, and that reason reaches the emitted HDL. It is
    announced as a warning, so that severe still means a design that
    cannot be built when the reader sees it."""
    out = []
    for w in warnings:
        if 'severe:' not in w:
            continue
        body = w.split('severe:', 1)[1]
        if any(mark in body for mark in FATAL_SEVERE):
            out.append(w)
    return out


# What the fitters call the encodings, which is not what SPEC 5.4
# calls them. Measured on Quartus Prime 25.1std and Efinity 2026.1,
# 2026-09-13, on a three-state machine:
#
#   one_hot   rejected by both. "Invalid value" from Quartus, "unknown
#             fsm encoding ignored" from Efinity, and the machine came
#             out binary on Efinity
#   one-hot   Quartus takes it, Efinity does not
#   onehot    both take it, and Efinity really does build it one-hot:
#             three flops for three states rather than two
#
# sequential, gray and johnson are spelt the same everywhere. The
# Python spelling stays one_hot, because that is a Python name; only
# what reaches the HDL changes. AMD Vivado documents one_hot with the
# underscore and is not installed here, so it is untested and is
# ROADMAP item 16.
VENDOR_ENCODING = {'one_hot': 'onehot'}


def house_names ():
    """What `from isomorph import ...` can bring in."""
    from . import __all__ as names
    return set(names)


def array_expressions (func, names):
    """How each array's element count was written, from the block AST.

    signals() reads its own call site, which works for a file on disk
    and not for a block compiled from a string. The block body is an
    AST here either way, and an array whose count folds while the
    loop over it does not is worse than one that folds everywhere:
    the loop would write past the end of it.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, ValueError):
        return {}
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return {}
    out = {}
    for stmt in ast.walk(tree.body[0]):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        call = stmt.value
        if not (isinstance(target, ast.Name) and target.id in names
                and isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == 'signals' and call.args):
            continue
        if isinstance(call.args[0], ast.Constant):
            continue
        out[target.id] = ' '.join(ast.unparse(call.args[0]).split())
    return out


def constant_expressions (func, names):
    """The source text of each constant a block assigns at its top level.

    m.constants is name -> int, so WIDTHH = WIDTH // 2 emitted
    localparam WIDTHH = 16 and said nothing about where sixteen came
    from. A block body reads as an AST the same way a process body
    does, and the expression is the author's own text.

    Only a plain assignment at the top level of the block, and only to
    a name elaboration kept as a constant. A value built in a loop or
    a comprehension has no single expression to carry, and a plain
    number says no more than the value already does.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, ValueError):
        return {}
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return {}
    out = {}
    for stmt in tree.body[0].body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
        else:
            continue
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        name = targets[0].id
        if name not in names or isinstance(stmt.value, ast.Constant):
            continue
        text = ast.get_source_segment(source, stmt.value)
        if text:
            out[name] = ' '.join(text.split())
    return out


BASE_PREFIX = {'0x': 'hex', '0b': 'bin', '0o': 'oct'}


def parameter_bases (func, values):
    """How each parameter default was written, and what a value says
    about itself.

    A default written 0xC0FFEE carries its base in the source. One a
    function worked out carries nothing, so hexed() marks the value
    and that is read here too.
    """
    out = {}
    for name, value in values.items():
        if isinstance(value, Hexed):
            out[name] = ('hex', format(int(value), 'X'))
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, ValueError):
        return out
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return out
    args = tree.body[0].args
    named = list(args.args) + list(args.kwonlyargs)
    defaults = ([None] * (len(args.args) - len(args.defaults))
                + list(args.defaults) + list(args.kw_defaults))
    for arg, default in zip(named, defaults):
        if default is None or arg.arg not in values:
            continue
        if not (isinstance(default, ast.Constant)
                and isinstance(default.value, int)
                and not isinstance(default.value, bool)):
            continue
        text = (ast.get_source_segment(source, default) or '').strip()
        base = BASE_PREFIX.get(text[:2].lower())
        if base:
            out[arg.arg] = (base, text[2:].replace('_', ''))
    return out


def constant_bases (func, names):
    """How each plain-number constant was written: 0xdead is dead.

    constant_expressions carries an expression because the names in
    it matter. A bare literal has no names, so it fell through to the
    value and 0xdead reached the HDL as 57005. The base is the rest
    of what the author said about it.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, ValueError):
        return {}
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return {}
    out = {}
    for stmt in tree.body[0].body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not (isinstance(target, ast.Name) and target.id in names
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, int)
                and not isinstance(stmt.value.value, bool)):
            continue
        text = (ast.get_source_segment(source, stmt.value) or '').strip()
        base = BASE_PREFIX.get(text[:2].lower())
        if base:
            out[target.id] = (base, text[2:].replace('_', ''))
    return out


def port_width_name (text):
    """The port whose width this constant names, or None.

    Exactly `len(port)` or `len(bundle.member)`. Anything else names
    no single port: `max(len(i_data_a), len(i_data_b))` is a width
    derived from two of them and gives neither a name, and a generic
    isomorph named for itself would be the cells_0 of SPEC 4.5 in a
    different hat. A block that wants to be reusable says so by
    naming its widths.
    """
    try:
        node = ast.parse(text, mode = 'eval').body
    except (SyntaxError, ValueError):
        return None
    if not (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == 'len'
            and len(node.args) == 1 and not node.keywords):
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Name):
        return arg.id
    if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
        return f'{arg.value.id}_{arg.attr}'
    return None


def ports_measured (text, known):
    """The ports this expression calls len() on, whatever else it does.

    Evidence that the author meant the block to size itself from its
    ports, which is what makes a missed promotion worth saying out
    loud rather than leaving as a module quietly frozen at one width.
    """
    try:
        tree = ast.parse(text, mode = 'eval').body
    except (SyntaxError, ValueError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == 'len' and len(node.args) == 1):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name):
            name = arg.id
        elif (isinstance(arg, ast.Attribute)
                and isinstance(arg.value, ast.Name)):
            name = f'{arg.value.id}_{arg.attr}'
        else:
            continue
        if name in known:
            found.add(name)
    return found


def promote_port_widths (mod):
    """A constant that names a port's width becomes a generic.

    `WIDTHD = len(i_data)` is not just a constant. It is the author
    giving that port's width a name, and a name is the one thing the
    converter cannot invent for itself, so this is the whole hinge of
    PARAMETERS.md stage 2. The port then declares [WIDTHD-1:0], one
    module serves every width, and a VHDL team can be handed the file
    rather than a regenerated blob per size.

    The value has to be the width the port really has, or the name is
    not naming what it looks like it names and the number goes out
    instead. An array port is left alone: its element type carries the
    width in VHDL and that is PARAMETERS.md stage 4.
    """
    by_name = {}
    for port in mod.ports:
        by_name.setdefault(port.name, port)
    warnings = []
    promoted = []
    for name, text in list(mod.constant_exprs.items()):
        wanted = port_width_name(text)
        if wanted is None:
            measured = ports_measured(text, by_name)
            if measured:
                listed = ', '.join(sorted(measured))
                first = sorted(measured)[0]
                warnings.append(
                    f'{name} is measured from {listed} but names no one '
                    f"port's width, so every port stays the width it "
                    'was built with and this module cannot be used at '
                    'another size. Give each width a constant of its '
                    f'own, one that is exactly len({first}), and each '
                    'becomes a generic that the ports and everything '
                    'derived from them follow. An output needs one too, '
                    'or it stays fixed while the inputs move.')
            continue
        port = by_name.get(wanted)
        value = mod.constants.get(name)
        if port is None or port.array or value is None:
            continue
        if value != port.width:
            continue
        promoted.append(name)
        port.width_expr = name
    if not promoted:
        return warnings
    # every other port of the same width that this one names follows,
    # because two ports of one width are one generic and saying it
    # twice would let a build override half of itself
    for name in promoted:
        value = mod.constants[name]
        for port in mod.ports:
            if (not port.array and port.width == value
                    and not port.width_expr):
                port.width_expr = name
    for name in promoted:
        mod.parameters[name] = mod.constants.pop(name)
        mod.constant_exprs.pop(name, None)
    return warnings


def blackbox_module (node):
    """The IR for something the fitter has and isomorph does not.

    Ports with the directions that were declared, and no body. The
    emitters write a stub for it so a linter has something to read,
    and the real one is handed to the tool instead.
    """
    ports = []
    for name, width in node.kind.inputs.items():
        ports.append(ir.Port(name, width, 'in',
                             'bit' if width == 1 else 'vector'))
    for name, width in node.kind.outputs.items():
        ports.append(ir.Port(name, width, 'out',
                             'bit' if width == 1 else 'vector'))
    header = node.kind.source or ''
    return ir.Module(node.module_name, node.block_name,
                     dict(node.kind.params), ports, [], {}, {}, [], [],
                     [], [], '', header, True, node.kind.source)


def analyse (elaborated, allow_severe = False):
    """IR modules for every distinct block in the hierarchy, leaves first,
    and the warnings gathered.

    A fatal severe warning raises unless allow_severe is set, which is
    what --allow-severe passes for someone mid-refactor."""
    modules, warnings = [], []
    directions = {}
    domains = {}
    crossings = []
    for node in elaborated.walk():
        if isinstance(node, Blackbox):
            m = blackbox_module(node)
            directions[m.name] = {p.name: p.direction for p in m.ports}
            domains[m.name] = {}
            modules.append(m)
            continue
        a = Analyser(node, directions, domains)
        m = a.module()
        directions[m.name] = {p.name: p.direction for p in m.ports}
        domains[m.name] = a.port_domains
        domains.setdefault('__clocks__', {})[m.name] = a.port_clocks
        domains.setdefault('__sync__', {})[m.name] = a.sync_inputs
        for crossing in a.crossings:
            crossing = dict(crossing)
            crossing['module'] = m.name
            crossings.append(crossing)
        modules.append(m)
        warnings += [f'{node.module_name}: {w}' for w in a.warnings]
    if modules:
        modules[-1].crossings = crossings
    fatal = fatal_warnings(warnings)
    if fatal and not allow_severe:
        listed = '\n  '.join(' '.join(w.split()) for w in fatal)
        raise ConversionError(
            'severe, and this design will not be built:\n  ' + listed
            + '\n\nFix it, or pass --allow-severe to carry on with it '
            'as it is.')
    return modules, warnings
