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
from .signal import (Signal, SignalArray, EnumType, EnumMember,
    StructType, Process, Assign, Vector, IsomorphError, concat, replicate,
    bits, vector)
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
    def __init__ (self, elaborated, child_directions = None):
        self.e = elaborated
        self.file = inspect.getsourcefile(elaborated.func)
        self.driven = {}                    # signal name -> driver name
        self.read = set()
        self.functions = {}                 # name -> ir.Function
        self.warnings = []
        self.child_directions = child_directions or {}
        self.comments = source_comments(elaborated.func)
        self.file_lines = file_lines(self.file)
        self.consumed = set()           # comment lines already placed
        self.loop_names = set()         # for-loop indices currently open

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
                    attrs.setdefault('fsm_encoding', enc)
                    attrs.setdefault('syn_encoding', enc)
            signals.append(ir.Sig(name, s.width, s.kind, None,
                                  s.type, attrs, 0, s.line))
        for name, a in e.arrays.items():
            signals.append(ir.Sig(name, a.width, 'vector', a.init,
                                  None, dict(a.attributes), len(a),
                                  a.line))
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
        self.check_names(mod)
        self.check_unused(mod)
        self.check_comb_loops(mod)
        return mod

    def _port_leaves (self, name, value):
        out = []
        if isinstance(value, Signal):
            pname = self.name_of(value)
            direction = 'out' if pname in self.driven else 'in'
            out.append(ir.Port(pname, value.width, direction, value.kind,
                               value.type, 0, [], None,
                               dict(value.attributes)))
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
                               len(value)))
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
            else:
                aname = self.name_of(actual)
                ports[formal] = ir.Expr('ref', actual.width, value = aname)
                self.read.add(aname.split('[')[0])
        return ir.Instance(child.instance_name, child.module_name, ports,
                           child.line, self.leading_comments(child.line))

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
            self.warnings.append(
                'severe: ' + proc.name + ': asynchronous reset on '
                + reset + ' (' + (proc.reason or '') + ')')
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
            if getattr(e, 'op', None) == 'ref':
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
        value = self.fit(value, target.width, lam)
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
        self.loop_names.add(node.target.id)
        body = self.block_body(node.body, scope, node.lineno)
        self.loop_names.discard(node.target.id)
        del scope.locals[node.target.id]
        return ir.For(node.target.id, start, stop, body,
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
        val = self.fit(val, tgt.width, node)
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

    def fit (self, value, width, node):
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
                value.width = width
                return value
        if value.width > width:
            self.error(node, f'result truncated: {value.width}-bit value '
                       f'assigned to {width} bits; slice it explicitly')
        if value.width < width:
            return ir.Expr('extend', width, value.signed, [value])
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
                     and not isinstance(v, Signal)}
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

    def _expression (self, node, scope):
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return ir.Expr('const', 1, value = int(v))
            if isinstance(v, int):
                return ir.Expr('const', min_width(v), v < 0, value = v)
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
                    return ir.Expr('ref', min_width(obj), obj < 0,
                                   value = node.id)
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
                if self.kind == 'ff':
                    self.error(node, '- in a clocked process: negate in a '
                               'comb process and select here')
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
            if self.kind == 'ff' and op in ('<', '<=', '>', '>='):
                self.error(node, f'{op} in a clocked process is a subtractor: '
                           'compare in a comb process and select here')
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

    def is_elaboration (self, e):
        """True if this expression is settled before the netlist exists:
        a literal, a parameter, a constant, an open loop index, or an
        expression built only from those."""
        op = getattr(e, 'op', None)
        if op == 'const':
            return True
        if op == 'ref':
            name = e.value
            return (name in self.e.constants or name in self.e.parameters
                    or name in self.loop_names)
        if op in ('binop', 'unop'):
            return all(self.is_elaboration(a) for a in (e.args or []))
        return False

    def binop (self, node, scope):
        op = {ast.BitAnd: '&', ast.BitOr: '|', ast.BitXor: '^', ast.Add: '+',
              ast.Sub: '-', ast.LShift: '<<', ast.RShift: '>>',
              ast.Mult: '*'}.get(type(node.op))
        if op is None:
            self.error(node, 'operator not supported in hardware')
        left = self.expression(node.left, scope)
        right = self.expression(node.right, scope)
        if self.kind == 'ff' and op in ('+', '-', '*', '<<', '>>'):
            # index arithmetic on parameters, constants and unrolled loop
            # variables is settled before the netlist exists: regs[i-1]
            # in a shift register is wiring, not an adder.
            if not (self.is_elaboration(left) and self.is_elaboration(right)):
                self.error(node, f'{op} in a clocked process: arithmetic '
                           'belongs in a comb process that this one selects '
                           'from')
        if op in ('<<', '>>'):
            if right.op != 'const' and self.kind == 'ff' \
                    and not self.is_elaboration(right):
                self.error(node, f'{op} in a clocked process: a barrel '
                           'shifter belongs in a comb process')
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
            return ir.Expr('binop', max(left.width, right.width) + 1, signed,
                           [left, right], op)
        return ir.Expr('binop', left.width + right.width, signed,
                       [left, right], op)

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
            return ir.Expr('bit', 1, args = [base, self.bound_expression(
                node.slice, scope)])
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
                if (p.op == 'const' and not (isinstance(a, ast.Constant)
                                             and isinstance(a.value, bool))):
                    self.warnings.append(f'{self.current}: unsized constant '
                                         f'{p.value} in concat takes '
                                         f'{p.width} bits')
            return ir.Expr('concat', sum(p.width for p in parts), args = parts)
        if target is replicate:
            inner = self.expression(node.args[0], scope)
            count = self.constant(node.args[1], scope, node)
            return ir.Expr('replicate', inner.width * count, args = [inner],
                           value = count)
        if target is len:
            obj = self.resolve(node.args[0], scope, node)
            if obj is None or not hasattr(obj, 'width'):
                self.error(node, 'len() of a non-signal')
            return ir.Expr('const', min_width(obj.width), value = obj.width)
        if target is bits:
            pattern = node.args[0].value
            return ir.Expr('bits', len(pattern), value = pattern)
        if target is vector:
            self.error(node, 'vector(W) declares a function local: '
                       'name = vector(W)')
        if isinstance(target, FunctionType):
            if self.kind == 'ff':
                self.error(node, f'{func.id}() in a clocked process: call it '
                           'from a comb process and select the result here')
            return self.function_call(func.id, target, node, scope)
        self.error(node, f'call to {ast.unparse(func)} is not supported')

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


def root (expr):
    while expr.op in ('slice', 'bit', 'part', 'part_down', 'field'):
        expr = expr.args[0]
    return expr.value


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

    An asynchronous reset is severe and is deliberately not fatal.
    always_ff_async_reset is the construct you have to call out on
    purpose, and its warning carries the reason you gave into the
    emitted HDL. Making it stop the run would mean the only way to
    write a reset synchroniser is a flag that also switches off the
    other two checks."""
    out = []
    for w in warnings:
        if 'severe:' not in w:
            continue
        body = w.split('severe:', 1)[1]
        if any(mark in body for mark in FATAL_SEVERE):
            out.append(w)
    return out


def analyse (elaborated, allow_severe = False):
    """IR modules for every distinct block in the hierarchy, leaves first,
    and the warnings gathered.

    A fatal severe warning raises unless allow_severe is set, which is
    what --allow-severe passes for someone mid-refactor."""
    modules, warnings = [], []
    directions = {}
    for node in elaborated.walk():
        a = Analyser(node, directions)
        m = a.module()
        directions[m.name] = {p.name: p.direction for p in m.ports}
        modules.append(m)
        warnings += [f'{node.module_name}: {w}' for w in a.warnings]
    fatal = fatal_warnings(warnings)
    if fatal and not allow_severe:
        listed = '\n  '.join(' '.join(w.split()) for w in fatal)
        raise ConversionError(
            'severe, and this design will not be built:\n  ' + listed
            + '\n\nFix it, or pass --allow-severe to carry on with it '
            'as it is.')
    return modules, warnings
