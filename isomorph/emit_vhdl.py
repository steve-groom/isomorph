"""VHDL-2008 emitter: IR modules to synthesizable VHDL.

Ports and signals are std_logic / std_logic_vector (SPEC 4.2, 11).
numeric_std unsigned/signed appear only inside expressions that need
arithmetic, never as port or signal types. Hierarchy is preserved:
one entity/architecture per IR module, named direct instantiations.

Style (IEEE numeric_std, Xilinx UG901, CNRS C_6, so-logic 2.2):
  lowercase keywords; one port per line; named association; downto;
  architecture rtl; process (all) / rising_edge; no std_logic_arith;
  no buffer ports; end entity / end architecture labelled.
"""
import textwrap
import os
import shutil
import tempfile
import subprocess

from . import ir
from .analyse import ConversionError
from . import reserved


def min_width (value):
    if value >= 0:
        return max(1, value.bit_length())
    return (-value - 1).bit_length() + 1


def emit_vhdl (modules):
    """VHDL-2008 text for the module list, leaves first."""
    if not modules:
        return ''
    for m in modules:
        check_names(m)
    by_name = {m.name: m for m in modules}
    parts = []
    pkg = emit_package(modules)
    if pkg:
        parts.append(pkg)
    for m in modules:
        parts.append(emit_unit(m, by_name, modules[-1].name))
    text = '\n\n'.join(parts)
    return text + ('\n' if not text.endswith('\n') else '')


def write_vhdl (modules, path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok = True)
    with open(path, 'w', encoding = 'ascii', newline = '\n') as f:
        f.write(emit_vhdl(modules))
    return path


def lint_vhdl (path):
    """ghdl -a --std=08. Raises ConversionError on failure."""
    workdir = tempfile.mkdtemp(prefix = 'iso_ghdl_')
    cmd = ['ghdl', '-a', '--std=08', '--workdir=' + workdir, path]
    try:
        result = subprocess.run(cmd, capture_output = True, text = True)
    except FileNotFoundError:
        raise ConversionError('ghdl not found on PATH')
    finally:
        shutil.rmtree(workdir, ignore_errors = True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or 'ghdl failed').rstrip()
        raise ConversionError(f'ghdl -a failed for {path}:\n{message}')
    return result.stderr


def check_names (m):
    names = [p.name for p in m.ports] + [s.name for s in m.signals]
    names += list(m.constants) + list(m.parameters)
    names += [p.name for p in m.processes] + [i.name for i in m.instances]
    names.append(m.name)
    for name in names:
        msg = reserved.clash_message(name.split('[')[0])
        if msg:
            raise ConversionError(msg)


def collect_structs (modules):
    seen = []
    names = set()
    for m in modules:
        for item in list(m.ports) + list(m.signals):
            if item.kind == 'struct' and item.type is not None:
                if item.type.name not in names:
                    names.add(item.type.name)
                    seen.append(item.type)
    return seen


def record_decl_lines (t):
    lines = [f'type {t.name} is record']
    for fname, fwidth in t.fields.items():
        lines.append(f'    {fname} : {sl_type(fwidth)};')
    lines.append(f'end record {t.name};')
    return lines


def emit_package (modules):
    """Array and record types used on ports must be visible at the entity."""
    types = []
    seen = set()
    for m in modules:
        for p in m.ports:
            if p.array:
                tname = array_type_name(p.name, p.width, p.array)
                if tname not in seen:
                    seen.add(tname)
                    types.append(array_type_decl(tname, p.width, p.array))
        for s in m.signals:
            if s.array:
                tname = array_type_name(s.name, s.width, s.array)
                if tname not in seen:
                    seen.add(tname)
                    types.append(array_type_decl(tname, s.width, s.array))
    records = collect_structs(modules)
    if not types and not records:
        return ''
    top = modules[-1].name
    lines = [
        'library ieee;',
        'use ieee.std_logic_1164.all;',
        '',
        f'package {top}_pkg is',
    ]
    for decl in types:
        lines.append('  ' + decl)
    for rec in records:
        for line in record_decl_lines(rec):
            lines.append('  ' + line)
    lines.append(f'end package {top}_pkg;')
    return '\n'.join(lines)


def array_type_name (name, width, count):
    base = name.split('[')[0]
    return f'{base}_t'


def array_type_decl (tname, width, count):
    elem = sl_type(width)
    return (f'type {tname} is array (0 to {count - 1}) of {elem};')


def width_parameter (params, width):
    """The name of the parameter this width came from, or None.

    A width is a plain int by the time it reaches the emitters, so the
    only link back to a parameter is its value. Matching on value alone
    renamed any width that happened to equal any parameter: a block with
    AVMM_TIMEOUT = 16 and a 16-bit bus declared its ports as
    [AVMM_TIMEOUT-1:0], and in VHDL that is a generic which would resize
    them. So the value must match a parameter that is named as a width,
    and it must be the only one, or the width is emitted as a literal.
    A missed substitution costs readability; a wrong one costs silence."""
    if not params or width <= 1:
        return None
    found = [name for name, value in params.items()
             if isinstance(value, int) and not isinstance(value, bool)
             and value == width and 'WIDTH' in name.upper()]
    if len(found) != 1:
        return None
    return found[0]


def sl_bound (params, width):
    name = width_parameter(params, width)
    return f'{name} - 1' if name else str(width - 1)


def sl_type (width, kind = 'vector', typ = None, params = None):
    if kind == 'enum' and typ is not None:
        return typ.name
    if kind == 'struct' and typ is not None:
        return typ.name
    if width == 1:
        return 'std_logic'
    return f'std_logic_vector({sl_bound(params, width)} downto 0)'


def emit_unit (m, by_name, top_name):
    lines = []
    lines += header_lines(m)
    lines.append('library ieee;')
    lines.append('use ieee.std_logic_1164.all;')
    lines.append('use ieee.numeric_std.all;')
    needs_pkg = (any(p.array for p in m.ports) or any(s.array for s in m.signals)
                 or any(p.kind == 'struct' for p in m.ports)
                 or any(s.kind == 'struct' for s in m.signals))
    if needs_pkg:
        lines.append(f'use work.{top_name}_pkg.all;')
    lines.append('')
    lines += entity_lines(m)
    lines.append('')
    lines += architecture_lines(m, by_name)
    return '\n'.join(lines)


def header_lines (m):
    """Block docstring as a comment block. '#' comments travel as --."""
    if not m.header:
        return []
    body = m.header.strip('\n').splitlines()
    lines = []
    for line in body:
        lines.append('-- ' + line if line.strip() else '--')
    lines.append('')
    return lines


def reason_lines (reason, indent, marker):
    """The async-reset reason, wrapped to the 79-column house limit."""
    if not reason:
        return []
    pad = ' ' * indent
    width = 79 - indent - len(marker) - 1
    body = textwrap.wrap('asynchronous reset: ' + reason, width)
    return [f'{pad}{marker} {line}' for line in body]


def as_comment (text):
    if text.startswith('#'):
        return '--' + text[1:]
    return '-- ' + text


def comment_lines (comments, indent):
    pad = ' ' * indent
    return [pad + as_comment(c) for c in (comments or [])]


def with_trailing (line, trailing):
    if trailing:
        return line + '  ' + as_comment(trailing)
    return line


def entity_lines (m):
    lines = [f'entity {m.name} is']
    gens = []
    for name, value in m.parameters.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            gens.append((name, value))
    if gens:
        lines.append('  generic (')
        for i, (name, value) in enumerate(gens):
            semi = ';' if i < len(gens) - 1 else ''
            lines.append(f'    {name} : integer := {value}{semi}')
        lines.append('  );')
    if m.ports:
        lines.append('  port (')
        lines += port_lines(m)
        lines.append('  );')
    # a port's attributes belong in the entity declarative part, which
    # is the only place a port name is visible to declare them against
    seen = set()
    decls = []
    specs = []
    for p in m.ports:
        for key, value in p.attributes.items():
            if key == 'unused':
                continue
            if key not in seen:
                seen.add(key)
                decls.append(f'  attribute {key} : string;')
            lit = '"true"' if value is True else f'"{value}"'
            specs.append(f'  attribute {key} of {p.name} : signal is {lit};')
    lines += decls + specs
    lines.append(f'end entity {m.name};')
    return lines


def port_lines (m):
    ports = m.ports
    dirs = ['in ' if p.direction == 'in' else 'out' for p in ports]
    types = []
    names = []
    for p in ports:
        names.append(p.name)
        if p.array:
            types.append(array_type_name(p.name, p.width, p.array))
        else:
            types.append(sl_type(p.width, p.kind, p.type, m.parameters))
    wn = max(len(n) for n in names)
    wd = max(len(d) for d in dirs)
    out = []
    body = [f'{names[i]:<{wn}} : {dirs[i]:<{wd}} {types[i]}'
            for i in range(len(ports))]
    wb = max((len(b.rstrip()) for i, b in enumerate(body)
              if ports[i].trailing), default = 0)
    for i, p in enumerate(ports):
        semi = ';' if i < len(ports) - 1 else ''
        out += comment_lines(p.comments, 4)
        line = '    ' + body[i].rstrip() + semi
        if p.trailing:
            pad = ' ' * max(1, wb + 4 + 1 + 1 - len(line))
            line = line + pad + as_comment(p.trailing)
        out.append(line)
    return out


def architecture_lines (m, by_name):
    ctx = Context(m, by_name)
    lines = [f'architecture rtl of {m.name} is']
    lines += constant_lines(m)
    lines += enum_decl_lines(m)
    lines += signal_lines(m)
    lines += attribute_lines(m)
    lines += function_decl_lines(ctx)
    lines.append('  function to_sl (value : boolean) return std_logic is')
    lines.append('  begin')
    lines.append("    if value then")
    lines.append("      return '1';")
    lines.append('    end if;')
    lines.append("    return '0';")
    lines.append('  end function to_sl;')
    lines.append('')
    lines.append('begin')
    body = item_lines(ctx)
    if body:
        lines += body
    else:
        lines.append('  null;')
    lines.append('end architecture rtl;')
    return lines


def constant_lines (m):
    lines = []
    for name, value in m.constants.items():
        if isinstance(value, bool):
            value = int(value)
        value = int(value)
        if abs(value) > 2 ** 31 - 1:
            w = max(32, min_width(value if value >= 0 else -value))
            bits = format(value & ((1 << w) - 1), f'0{w}b')
            lines.append(
                f'  constant {name} : std_logic_vector({w - 1} downto 0) '
                f':= "{bits}";')
        else:
            lines.append(f'  constant {name} : integer := {value};')
    if lines:
        lines.append('')
    return lines


def enum_decl_lines (m):
    lines = []
    for name, enum_type in m.enums.items():
        members = ', '.join(x.name for x in enum_type.members)
        lines.append(f'  type {name} is ({members});')
    if lines:
        lines.append('')
    return lines


def signal_lines (m):
    lines = []
    for s in m.signals:
        lines += comment_lines(s.comments, 2)
        if s.array:
            typ = array_type_name(s.name, s.width, s.array)
        else:
            typ = sl_type(s.width, s.kind, s.type, m.parameters)
        line = f'  signal {s.name} : {typ};'
        lines.append(with_trailing(line, s.trailing))
    if lines:
        lines.append('')
    return lines


def attribute_lines (m):
    """Vendor attributes on signals (SPEC 5.4, 5.12).

    An attribute the entity already declared for a port is visible
    here, and redeclaring it is an error."""
    decls = []
    specs = []
    seen = {key for p in m.ports for key in p.attributes
            if key != 'unused'}
    for s in m.signals:
        for key, value in s.attributes.items():
            if key == 'unused':
                continue
            if key not in seen:
                seen.add(key)
                decls.append(f'  attribute {key} : string;')
            if value is True:
                lit = '"true"'
            else:
                lit = f'"{value}"'
            specs.append(
                f'  attribute {key} of {s.name} : signal is {lit};')
    if not specs:
        return []
    return decls + specs + ['']


def function_decl_lines (ctx):
    lines = []
    for f in ctx.m.functions:
        ret = sl_type(f.width)
        params = []
        for name, width, signed in f.params:
            params.append(f'{name} : {sl_type(width)}')
        plist = '; '.join(params)
        lines.append(f'  function {f.name} ({plist}) return {ret} is')
        for name, width in f.locals.items():
            lines.append(f'    variable {name} : {sl_type(width)};')
        lines.append('  begin')
        saved = ctx.var_map
        ctx.var_map = {n: n for n in f.locals}
        ctx.in_function = True
        lines += stmt_lines(ctx, f.body, 4)
        ctx.var_map = saved
        ctx.in_function = False
        lines.append(f'  end function {f.name};')
        lines.append('')
    return lines


class Context:
    def __init__ (self, module, by_name):
        self.m = module
        self.by_name = by_name
        self.var_map = {}
        self.in_function = False
        self.int_names = set()
        self.slv_consts = set()
        for name, value in module.constants.items():
            if isinstance(value, bool):
                self.int_names.add(name)
            elif abs(int(value)) > 2 ** 31 - 1:
                self.slv_consts.add(name)
            else:
                self.int_names.add(name)
        for name, value in module.parameters.items():
            if isinstance(value, int) and not isinstance(value, bool):
                self.int_names.add(name)
        self.enum_types = set(module.enums)
        self.sig_width = {}
        self.sig_kind = {}
        for p in module.ports:
            self.sig_width[p.name] = p.width
            self.sig_kind[p.name] = p.kind
        for s in module.signals:
            self.sig_width[s.name] = s.width
            self.sig_kind[s.name] = s.kind


def item_lines (ctx):
    items = []
    for inst in ctx.m.instances:
        items.append((inst.line, 0, inst.name, 'instance', inst))
    for a in ctx.m.assigns:
        items.append((a.line, 1, '', 'assign', a))
    for p in ctx.m.processes:
        items.append((p.line, 2, p.name, 'process', p))
    items.sort()
    lines = []
    for _, _, _, kind, item in items:
        if kind == 'instance':
            lines += instance_lines(ctx, item)
        elif kind == 'assign':
            lines += assign_lines(ctx, item)
        else:
            lines += process_lines(ctx, item)
        lines.append('')
    return lines


def instance_lines (ctx, inst):
    lines = comment_lines(inst.comments, 2)
    lines.append(f'  {inst.name} : entity work.{inst.module}')
    child = ctx.by_name.get(inst.module)
    if child is not None:
        gens = [(n, v) for n, v in child.parameters.items()
                if isinstance(v, int) and not isinstance(v, bool)]
        if gens:
            lines.append('    generic map (')
            for i, (n, v) in enumerate(gens):
                comma = ',' if i < len(gens) - 1 else ''
                lines.append(f'      {n} => {v}{comma}')
            lines.append('    )')
    lines.append('    port map (')
    formals = list(inst.ports.items())
    for i, (formal, actual) in enumerate(formals):
        comma = ',' if i < len(formals) - 1 else ''
        mapped = 'open' if actual is None else vhdl_expr(ctx, actual)
        lines.append(f'      {formal} => {mapped}{comma}')
    lines.append('    );')
    return lines


def assign_lines (ctx, a):
    lines = comment_lines(a.comments, 2)
    tgt = vhdl_expr(ctx, a.target)
    if a.value.op == 'ifexp':
        cond, x, y = a.value.args
        line = (f'  {tgt} <= {vhdl_expr(ctx, x)} when '
                f'{vhdl_condition(ctx, cond)} else {vhdl_expr(ctx, y)};')
    else:
        line = f'  {tgt} <= {vhdl_expr(ctx, a.value)};'
    lines.append(with_trailing(line, a.trailing))
    return lines


def assigned_roots (body):
    names = set()
    for s in body:
        if isinstance(s, ir.Assign):
            names.add(root_name(s.target))
        elif isinstance(s, ir.If):
            for _, b in s.branches:
                names |= assigned_roots(b)
        elif isinstance(s, ir.Match):
            for _, b in s.arms:
                names |= assigned_roots(b)
        elif isinstance(s, ir.For):
            names |= assigned_roots(s.body)
    return names


def root_name (expr):
    while expr.op in ('slice', 'bit', 'part', 'part_down', 'field'):
        expr = expr.args[0]
    return expr.value


def process_lines (ctx, p):
    lines = comment_lines(p.comments, 2)
    if p.kind == 'ff':
        edge = 'rising_edge' if p.polarity == 'pos' else 'falling_edge'
        saved = ctx.var_map
        ctx.var_map = {}
        ctx.in_function = False
        if p.reset:
            # if reset then ... elsif rising_edge(clock) then ...
            lines += reason_lines(p.reason, 2, '--')
            lines.append(f'  {p.name} : process ({p.clock}, {p.reset}) is')
            lines.append('  begin')
            level = "'1'" if p.reset_polarity == 'pos' else "'0'"
            node = p.body[0]
            lines.append(f'    if {p.reset} = {level} then')
            lines += stmt_lines(ctx, node.branches[0][1], 6)
            lines.append(f'    elsif {edge}({p.clock}) then')
            lines += stmt_lines(ctx, node.branches[1][1], 6)
        else:
            lines.append(f'  {p.name} : process ({p.clock}) is')
            lines.append('  begin')
            lines.append(f'    if {edge}({p.clock}) then')
            lines += stmt_lines(ctx, p.body, 6)
        ctx.var_map = saved
        lines.append('    end if;')
        lines.append(f'  end process {p.name};')
        return lines
    # Combinational: variables so later reads see earlier writes (4.4 / 6).
    driven = [n for n in assigned_roots(p.body) if n in ctx.sig_width]
    lines.append(f'  {p.name} : process (all) is')
    var_map = {}
    for name in driven:
        vname = name + '_v'
        var_map[name] = vname
        width = ctx.sig_width[name]
        kind = ctx.sig_kind.get(name, 'vector')
        typ = sl_type(width, kind, None, ctx.m.parameters)
        if kind in ('enum', 'struct'):
            found = None
            for s in ctx.m.signals:
                if s.name == name and s.type is not None:
                    found = s.type
                    break
            if found is None:
                for port in ctx.m.ports:
                    if port.name == name and port.type is not None:
                        found = port.type
                        break
            if found is not None:
                typ = sl_type(width, kind, found, ctx.m.parameters)
        lines.append(f'    variable {vname} : {typ};')
    lines.append('  begin')
    for name in driven:
        lines.append(f'    {var_map[name]} := {name};')
    saved = ctx.var_map
    ctx.var_map = var_map
    ctx.in_function = False
    lines += stmt_lines(ctx, p.body, 4)
    ctx.var_map = saved
    for name in driven:
        lines.append(f'    {name} <= {var_map[name]};')
    lines.append(f'  end process {p.name};')
    return lines


def stmt_lines (ctx, body, indent):
    lines = []
    pad = ' ' * indent
    op_assign = ':=' if (ctx.var_map or ctx.in_function) else '<='
    # In a comb process, assignments to mapped names use := on the variable.
    for s in body:
        lines += comment_lines(getattr(s, 'comments', []), indent)
        trailing = getattr(s, 'trailing', None)
        if isinstance(s, ir.Assign) and s.value.op == 'ifexp':
            lines += ifexp_assign(ctx, s, indent)
        elif isinstance(s, ir.Assign):
            use_var = root_name(s.target) in ctx.var_map or ctx.in_function
            tok = ':=' if use_var else '<='
            line = (f'{pad}{vhdl_target(ctx, s.target)} {tok} '
                    f'{vhdl_expr(ctx, s.value)};')
            lines.append(with_trailing(line, trailing))
        elif isinstance(s, ir.If):
            lines += if_lines(ctx, s, indent)
        elif isinstance(s, ir.For):
            lines += for_lines(ctx, s, indent)
        elif isinstance(s, ir.Match):
            lines += match_lines(ctx, s, indent)
        elif isinstance(s, ir.Assert):
            lines.append(f'{pad}-- synthesis translate_off')
            cond = vhdl_condition(ctx, s.cond)
            line = f'{pad}assert {cond} report "assert" severity error;'
            lines.append(with_trailing(line, trailing))
            lines.append(f'{pad}-- synthesis translate_on')
        elif isinstance(s, ir.Return):
            line = f'{pad}return {vhdl_expr(ctx, s.value)};'
            lines.append(with_trailing(line, trailing))
        elif isinstance(s, ir.Comment):
            lines.append(pad + as_comment(s.text))
        else:
            lines.append(f'{pad}-- <{type(s).__name__}>')
    return lines


def ifexp_assign (ctx, s, indent):
    """Expand a ? : assignment to if/else (when/else is not an expression)."""
    pad = ' ' * indent
    cond, a, b = s.value.args
    use_var = root_name(s.target) in ctx.var_map or ctx.in_function
    tok = ':=' if use_var else '<='
    tgt = vhdl_target(ctx, s.target)
    lines = [f'{pad}if {vhdl_condition(ctx, cond)} then']
    lines.append(f'{pad}  {tgt} {tok} {vhdl_expr(ctx, a)};')
    lines.append(f'{pad}else')
    lines.append(f'{pad}  {tgt} {tok} {vhdl_expr(ctx, b)};')
    lines.append(f'{pad}end if;')
    return lines


def if_lines (ctx, node, indent):
    if len(node.branches) == 1 and node.branches[0][0] is None:
        return stmt_lines(ctx, node.branches[0][1], indent)
    pad = ' ' * indent
    lines = []
    headers = getattr(node, 'branch_comments', [])
    for i, (cond, body) in enumerate(node.branches):
        lines += comment_lines(headers[i] if i < len(headers) else [], indent)
        if cond is None:
            head = f'{pad}else'
        elif i == 0:
            head = f'{pad}if {vhdl_condition(ctx, cond)} then'
        else:
            head = f'{pad}elsif {vhdl_condition(ctx, cond)} then'
        if i == 0:
            head = with_trailing(head, node.trailing)
        lines.append(head)
        inner = stmt_lines(ctx, body, indent + 2)
        if inner:
            lines += inner
        else:
            lines.append(pad + '  null;')
    lines.append(f'{pad}end if;')
    return lines


def for_lines (ctx, node, indent):
    pad = ' ' * indent
    last = node.stop - 1
    head = f'{pad}for {node.var} in {node.start} to {last} loop'
    lines = [with_trailing(head, node.trailing)]
    # loop index is an integer; treat as int name while in the body
    ctx.int_names.add(node.var)
    lines += stmt_lines(ctx, node.body, indent + 2)
    ctx.int_names.discard(node.var)
    lines.append(f'{pad}end loop;')
    return lines


def match_lines (ctx, node, indent):
    pad = ' ' * indent
    has_bits = any(p is not None and p.op == 'bits' for p, _ in node.arms)
    kw = 'case?' if has_bits else 'case'
    lines = [f'{pad}{kw} {vhdl_expr(ctx, node.subject)} is']
    inner = indent + 2
    ipad = ' ' * inner
    for pat, body in node.arms:
        if pat is None:
            label = 'others'
        elif pat.op == 'bits':
            bits = pat.value.replace('?', '-')
            label = '"' + bits + '"'
        elif pat.op == 'const':
            # a choice must have the subject's type: a std_logic_vector
            # literal of its width, never a character literal
            width = node.subject.width
            value = int(pat.value) & ((1 << width) - 1)
            if width == 1:
                label = f"'{value}'"
            else:
                label = '"' + format(value, '0%db' % width) + '"'
        else:
            label = vhdl_expr(ctx, pat)
        lines.append(f'{ipad}when {label} =>')
        inner_body = stmt_lines(ctx, body, inner + 2)
        if inner_body:
            lines += inner_body
        else:
            lines.append(' ' * (inner + 2) + 'null;')
    lines.append(f'{pad}end {kw};')
    return lines


def vhdl_target (ctx, e):
    return vhdl_expr(ctx, e)


def vhdl_condition (ctx, e):
    """Boolean condition: std_logic compared to '1', or a boolean expr."""
    if e.op == 'cmp':
        return vhdl_cmp_bool(ctx, e)
    if e.op == 'not':
        inner = vhdl_condition(ctx, e.args[0])
        return f'not ({inner})'
    text = vhdl_expr(ctx, e)
    if e.width == 1 and e.op != 'enum':
        return f"{text} = '1'"
    return text


def vhdl_cmp_bool (ctx, e):
    op = e.value
    a, b = e.args
    vhdl_op = {'==': '=', '!=': '/=', '<': '<', '<=': '<=',
               '>': '>', '>=': '>='}[op]
    if op in ('==', '!='):
        return f'{vhdl_expr(ctx, a)} {vhdl_op} {vhdl_expr(ctx, b)}'
    left = as_unsigned(ctx, a)
    right = as_unsigned(ctx, b)
    return f'{left} {vhdl_op} {right}'


def vhdl_bits (value, width, signed = False):
    value = int(value)
    if width == 1:
        return "'1'" if value else "'0'"
    if signed and value < 0:
        value = value & ((1 << width) - 1)
    bits = format(value, f'0{width}b')
    if len(bits) > width:
        bits = bits[-width:]
    return '"' + bits + '"'


def vhdl_id (name):
    """mem[2] (IR array element) -> mem(2)."""
    if '[' in name and name.endswith(']'):
        base, rest = name.split('[', 1)
        return f'{base}({rest[:-1]})'
    return name


def lookup (ctx, name):
    mapped = ctx.var_map.get(name, name)
    return vhdl_id(mapped)


def vhdl_expr (ctx, e, index = False):
    if e is None:
        return ''
    op = e.op
    a = e.args
    if op == 'ref':
        name = e.value
        if name in ctx.int_names:
            if index:
                return name
            return f'std_logic_vector(to_unsigned({name}, {e.width}))'
        return lookup(ctx, name)
    if op == 'const':
        if index:
            return str(int(e.value))
        if e.value is not None and str(e.value) in ctx.int_names:
            return str(e.value)
        return vhdl_bits(e.value, e.width, e.signed)
    if op == 'enum':
        return e.value.name
    if op == 'bit':
        return vhdl_bit(ctx, e)
    if op == 'slice':
        return vhdl_slice(ctx, e)
    if op == 'part':
        return vhdl_part(ctx, e)
    if op == 'part_down':
        return vhdl_part_down(ctx, e)
    if op == 'field':
        return f'{vhdl_expr(ctx, a[0])}.{e.value}'
    if op == 'concat':
        text = ' & '.join(as_vector(ctx, x) for x in a)
        return f"std_logic_vector'({text})"
    if op == 'replicate':
        return vhdl_replicate(ctx, e)
    if op == 'binop':
        if index:
            return (f'{vhdl_expr(ctx, a[0], True)} {e.value} '
                    f'{vhdl_expr(ctx, a[1], True)}')
        return vhdl_binop(ctx, e)
    if op == 'cmp':
        return f'to_sl({vhdl_cmp_bool(ctx, e)})'
    if op == 'unop':
        if e.value == '-':
            return (f'std_logic_vector(-signed({vhdl_expr(ctx, a[0])}))')
        return f'(not {vhdl_expr(ctx, a[0])})'
    if op == 'not':
        return f"(not {vhdl_expr(ctx, a[0])})"
    if op == 'ifexp':
        cond = vhdl_condition(ctx, a[0])
        return (f'({vhdl_expr(ctx, a[1])} when {cond} else '
                f'{vhdl_expr(ctx, a[2])})')
    if op == 'signed':
        inner = vhdl_expr(ctx, a[0])
        if a[0].op == 'concat':
            inner = f"std_logic_vector'({inner})"
        return inner
    if op == 'extend':
        if a[0].op == 'ref' and a[0].value in ctx.int_names:
            return f'std_logic_vector(to_unsigned({a[0].value}, {e.width}))'
        inner = vhdl_expr(ctx, a[0])
        if a[0].width == 1:
            zeros = e.width - 1
            if e.signed:
                return f'({zeros - 1} downto 0 => {inner}) & {inner}' if zeros else inner
            if zeros == 1:
                return f"'0' & {inner}"
            return f'({zeros - 1} downto 0 => \'0\') & {inner}'
        fn = 'signed' if e.signed else 'unsigned'
        return (f'std_logic_vector(resize({fn}({inner}), {e.width}))')
    if op == 'call':
        args = ', '.join(vhdl_expr(ctx, x) for x in a)
        return f'{e.value}({args})'
    if op == 'bits':
        return '"' + e.value.replace('?', '-') + '"'
    return f'/* {op} */'


def as_vector (ctx, e):
    """Force a 1-bit value to an slv concatenand."""
    text = vhdl_expr(ctx, e)
    if e.width == 1 and e.op != 'slice':
        # std_logic concatenates with &
        return text
    return text


def as_unsigned (ctx, e):
    if e.op == 'const':
        return f'to_unsigned({int(e.value)}, {e.width})'
    if e.op == 'ref' and e.value in ctx.int_names:
        return f'to_unsigned({e.value}, {e.width})'
    text = vhdl_expr(ctx, e)
    if e.signed or e.op == 'signed':
        return f'signed({text})'
    if e.width == 1:
        return f"unsigned'('0' & {text})"
    return f'unsigned({text})'


def is_int_tree (ctx, e):
    if e.op == 'const':
        return True
    if e.op == 'ref' and e.value in ctx.int_names:
        return True
    if e.op == 'binop' and e.value in ('+', '-', '*'):
        return is_int_tree(ctx, e.args[0]) and is_int_tree(ctx, e.args[1])
    return False


def vhdl_binop (ctx, e):
    op = e.value
    a, b = e.args
    w = e.width
    if is_int_tree(ctx, e):
        text = (f'({vhdl_index(ctx, a)} {op} {vhdl_index(ctx, b)})')
        return f'std_logic_vector(to_unsigned({text}, {w}))'
    if op in ('&', '|', '^'):
        vop = {'&': 'and', '|': 'or', '^': 'xor'}[op]
        return f'({vhdl_expr(ctx, a)} {vop} {vhdl_expr(ctx, b)})'
    if op in ('+', '-'):
        left = f'resize({as_unsigned(ctx, a)}, {w})'
        right = f'resize({as_unsigned(ctx, b)}, {w})'
        return f'std_logic_vector({left} {op} {right})'
    if op == '*':
        left = as_unsigned(ctx, a)
        right = as_unsigned(ctx, b)
        return f'std_logic_vector(resize({left} * {right}, {w}))'
    if op in ('<<', '>>'):
        fn = 'shift_left' if op == '<<' else 'shift_right'
        if b.op == 'const' or (b.op == 'ref' and b.value in ctx.int_names):
            amt = vhdl_expr(ctx, b, index = True)
        else:
            amt = f'to_integer(unsigned({vhdl_expr(ctx, b)}))'
        left = as_unsigned(ctx, a)
        if e.signed or a.signed:
            left = f'signed({vhdl_expr(ctx, a)})'
        return f'std_logic_vector({fn}({left}, {amt}))'
    return f'({vhdl_expr(ctx, a)} {op} {vhdl_expr(ctx, b)})'


def vhdl_bit (ctx, e):
    base, idx = e.args
    base_t = vhdl_expr(ctx, base)
    if e.width != 1:
        # array word select
        if idx.op == 'const' or (idx.op == 'ref' and idx.value in ctx.int_names):
            return f'{base_t}({vhdl_expr(ctx, idx, True)})'
        return f'{base_t}(to_integer({as_unsigned(ctx, idx)}))'
    if idx.op == 'const':
        return f'{base_t}({int(idx.value)})'
    if idx.op == 'ref' and idx.value in ctx.int_names:
        return f'{base_t}({idx.value})'
    if idx.op == 'binop':
        return f'{base_t}({vhdl_index(ctx, idx)})'
    return f'{base_t}(to_integer({as_unsigned(ctx, idx)}))'


def vhdl_slice (ctx, e):
    base = e.args[0]
    if base.op not in ('ref', 'bit', 'slice', 'part', 'part_down', 'field',
                       'concat', 'replicate'):
        inner = vhdl_expr(ctx, base)
        if e.width == 1:
            # a one-bit value is std_logic here, and a one-element vector
            # will not match it. resize is a function, so it may be
            # indexed; a type conversion may not, hence no
            # std_logic_vector() around it.
            low = e.value[1] if len(e.args) < 3 else 0
            return f'resize(unsigned({inner}), {low + 1})({low})'
        return f'std_logic_vector(resize(unsigned({inner}), {e.width}))'
    if len(e.args) >= 3:
        hi = vhdl_index(ctx, e.args[1])
        lo = vhdl_index(ctx, e.args[2])
    else:
        hi, lo = str(e.value[0] - 1), str(e.value[1])
    if e.width == 1:
        # A one-bit value is std_logic in this type model, and VHDL will
        # not match x(n downto n), a one-element vector, against it.
        return f'{vhdl_expr(ctx, base)}({lo})'
    return f'{vhdl_expr(ctx, base)}({hi} downto {lo})'


def vhdl_part (ctx, e):
    base, idx = e.args
    w = e.value
    i = vhdl_index(ctx, idx)
    return f'{vhdl_expr(ctx, base)}(({i}) + {w - 1} downto ({i}))'


def vhdl_part_down (ctx, e):
    base, idx = e.args
    w = e.value
    i = vhdl_index(ctx, idx)
    return f'{vhdl_expr(ctx, base)}(({i}) downto ({i}) - {w - 1})'


def vhdl_index (ctx, e):
    """Integer index / bound expression."""
    if e.op == 'const':
        return str(int(e.value))
    if e.op == 'ref':
        if e.value in ctx.int_names:
            return e.value
        mapped = lookup(ctx, e.value)
        w = ctx.sig_width.get(e.value, e.width)
        if w == 1:
            return f"to_integer(unsigned'('0' & {mapped}))"
        return f'to_integer(unsigned({mapped}))'
    if e.op == 'binop':
        op = e.value
        if op == '*':
            # scale: integer * integer
            return (f'({vhdl_index(ctx, e.args[0])} * '
                    f'{vhdl_index(ctx, e.args[1])})')
        return (f'({vhdl_index(ctx, e.args[0])} {op} '
                f'{vhdl_index(ctx, e.args[1])})')
    if e.op in ('bit', 'slice', 'part'):
        return f'to_integer(unsigned({vhdl_expr(ctx, e)}))'
    if e.width == 1:
        return f"to_integer(unsigned'('0' & {vhdl_expr(ctx, e)}))"
    return f'to_integer(unsigned({vhdl_expr(ctx, e)}))'


def vhdl_replicate (ctx, e):
    n = e.value
    inner = e.args[0]
    text = vhdl_expr(ctx, inner)
    if inner.width == 1:
        if inner.op == 'const':
            bit = '1' if inner.value else '0'
            if n == 1:
                return f"'{bit}'"
            return '"' + bit * n + '"'
        if n == 1:
            return text
        return ' & '.join([text] * n)
    return ' & '.join([text] * n)
