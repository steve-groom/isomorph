"""SystemVerilog emitter: IR modules to IEEE 1800-2017 synthesizable SV.

The output keeps the input's structure: one module per IR module (leaves
first), named processes, named functions, named instances, comments on
the same items. Section 4 of SPEC.txt is the reference."""
import os
import subprocess

from . import ir
from .analyse import ConversionError


def min_width (value):
    if value >= 0:
        return max(1, value.bit_length())
    return (-value - 1).bit_length() + 1


def emit_sv (modules):
    """SystemVerilog text for the module list, leaves first."""
    chunks = []
    preamble = struct_typedefs(modules)
    if preamble:
        chunks.append(preamble)
    chunks += [emit_module(m) for m in modules]
    text = '\n\n'.join(chunks)
    return text + ('\n' if not text.endswith('\n') else '')


def write_sv (modules, path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok = True)
    with open(path, 'w', encoding = 'ascii', newline = '\n') as f:
        f.write(emit_sv(modules))
    return path


def lint_sv (path, top = None):
    """Run verilator --lint-only -Wall. Raises ConversionError on failure.

    -Wno-DECLFILENAME: one file holds every module (SPEC 7), so only the
    top name matches the filename."""
    cmd = ['verilator', '--lint-only', '-Wall', '--sv', '-Wno-DECLFILENAME']
    if top:
        cmd += ['--top-module', top]
    cmd.append(path)
    try:
        result = subprocess.run(cmd, capture_output = True, text = True)
    except FileNotFoundError:
        raise ConversionError('verilator not found on PATH')
    if result.returncode != 0:
        message = (result.stderr or result.stdout or 'verilator failed').rstrip()
        raise ConversionError(f'verilator lint failed for {path}:\n{message}')
    return result.stderr


def emit_module (m):
    lines = []
    lines += header_lines(m)
    lines.append(f'module {m.name} (')
    if m.ports:
        lines += port_lines(m)
    lines.append(');')
    lines.append('')
    body = []
    body += parameter_lines(m)
    body += localparam_lines(m)
    body += enum_lines(m)
    body += signal_lines(m)
    body += function_lines(m)
    body += item_lines(m)
    # indent body, keep blank lines
    for line in body:
        if line == '':
            lines.append('')
        else:
            lines.append(line)
    if lines[-1] != '':
        lines.append('')
    lines.append('endmodule')
    return '\n'.join(lines)


def header_lines (m):
    """Block docstring as a /* */ comment. '#' comments travel as //."""
    if not m.header:
        return []
    body = m.header.strip('\n').splitlines()
    if len(body) == 1:
        return [f'/* {body[0].strip()} */', '']
    lines = ['/*']
    for line in body:
        lines.append(' * ' + line if line.strip() else ' *')
    lines.append(' */')
    lines.append('')
    return lines


def as_comment (text):
    if text.startswith('#'):
        return '//' + text[1:]
    return '// ' + text


def comment_lines (comments, indent):
    pad = ' ' * indent
    return [pad + as_comment(c) for c in (comments or [])]


def with_trailing (line, trailing):
    if trailing:
        return line + '  ' + as_comment(trailing)
    return line


def param_hi (params, width):
    """Keep WIDTH-1 in the HDL when the width is that parameter."""
    if not params or width <= 1:
        return None
    for name, value in params.items():
        if (isinstance(value, int) and not isinstance(value, bool)
                and value == width):
            return f'{name}-1'
    return None


def packed_type (width, kind = 'vector', typ = None, params = None):
    if kind == 'enum' and typ is not None:
        return typ.name
    if kind == 'struct' and typ is not None:
        return typ.name
    if width == 1:
        return 'logic'
    hi = param_hi(params, width)
    if hi is None:
        hi = str(width - 1)
    return f'logic [{hi}:0]'


def collect_structs (modules):
    seen = []
    names = set()
    for m in modules:
        items = list(m.ports) + list(m.signals)
        for item in items:
            if item.kind == 'struct' and item.type is not None:
                if item.type.name not in names:
                    names.add(item.type.name)
                    seen.append(item.type)
    return seen


def struct_typedefs (modules):
    types = collect_structs(modules)
    if not types:
        return ''
    lines = []
    for t in types:
        lines.append('typedef struct packed {')
        for fname, fwidth in t.fields.items():
            lines.append(f'    {packed_type(fwidth)} {fname};')
        lines.append(f'}} {t.name};')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def port_lines (m):
    ports = m.ports
    dirs = ['input ' if p.direction == 'in' else 'output' for p in ports]
    types = [packed_type(p.width, p.kind, p.type, m.parameters)
             for p in ports]
    names = [f'{p.name} [{p.array}]' if p.array else p.name for p in ports]
    wd = max(len(d) for d in dirs)
    wt = max(len(t) for t in types)
    out = []
    for i, p in enumerate(ports):
        comma = ',' if i < len(ports) - 1 else ''
        out.append(f'    {dirs[i]:<{wd}} {types[i]:<{wt}} {names[i]}{comma}')
    return out


def parameter_lines (m):
    lines = []
    for name, value in m.parameters.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            lines.append(f'    parameter {name} = {value};')
    if lines:
        lines.append('')
    return lines


def localparam_lines (m):
    lines = []
    for name, value in m.constants.items():
        if isinstance(value, bool):
            lines.append(f'    localparam {name} = {int(value)};')
        else:
            # Unsized so WIDTH-1 in an index is integer arithmetic.
            lines.append(f'    localparam {name} = {int(value)};')
    if lines:
        lines.append('')
    return lines


def enum_lines (m):
    lines = []
    for name, enum_type in m.enums.items():
        width = enum_type.width
        packed = 'logic' if width == 1 else f'logic [{width - 1}:0]'
        lines.append(f'    typedef enum {packed} {{')
        members = list(enum_type.members)
        for i, member in enumerate(members):
            comma = ',' if i < len(members) - 1 else ''
            lit = sv_const(member.value, width)
            lines.append(f'        {member.name} = {lit}{comma}')
        lines.append(f'    }} {name};')
        lines.append('')
    return lines


def signal_lines (m):
    lines = []
    for s in m.signals:
        lines += comment_lines(s.comments, 4)
        packed = packed_type(s.width, s.kind, s.type, m.parameters)
        name = s.name
        if s.array:
            name = f'{name} [{s.array}]'
        prefix = ''
        if s.attributes:
            parts = []
            for key, value in s.attributes.items():
                if value is True:
                    parts.append(key)
                elif isinstance(value, str):
                    parts.append(f'{key} = "{value}"')
                else:
                    parts.append(f'{key} = {value}')
            prefix = f'    (* {", ".join(parts)} *)\n'
        line = prefix + f'    {packed} {name};'
        lines.append(with_trailing(line, s.trailing))
    if lines:
        lines.append('')
    return lines


def function_lines (m):
    lines = []
    for f in m.functions:
        ret = packed_type(f.width)
        lines.append(f'    function automatic {ret} {f.name};')
        for name, width, signed in f.params:
            packed = packed_type(width)
            if signed:
                packed = packed.replace('logic', 'logic signed', 1)
            lines.append(f'        input {packed} {name};')
        for name, width in f.locals.items():
            lines.append(f'        {packed_type(width)} {name};')
        lines.append('        begin')
        lines += stmt_lines(f.body, 12, nonblocking = False)
        lines.append('        end')
        lines.append('    endfunction')
        lines.append('')
    return lines


def item_lines (m):
    items = []
    for inst in m.instances:
        items.append((inst.line, 0, inst.name, 'instance', inst))
    for a in m.assigns:
        items.append((a.line, 1, '', 'assign', a))
    for p in m.processes:
        items.append((p.line, 2, p.name, 'process', p))
    items.sort()
    lines = []
    for _, _, _, kind, item in items:
        if kind == 'instance':
            lines += instance_lines(item)
        elif kind == 'assign':
            lines += assign_lines(item)
        else:
            lines += process_lines(item)
        lines.append('')
    return lines


def instance_lines (inst):
    lines = comment_lines(inst.comments, 4)
    lines.append(f'    {inst.module} {inst.name} (')
    formals = list(inst.ports.items())
    for i, (formal, actual) in enumerate(formals):
        comma = ',' if i < len(formals) - 1 else ''
        mapped = sv_expr(actual) if actual is not None else ''
        lines.append(f'        .{formal}({mapped}){comma}')
    lines.append('    );')
    return lines


def assign_lines (a):
    lines = comment_lines(a.comments, 4)
    line = f'    assign {sv_expr(a.target)} = {sv_expr(a.value)};'
    lines.append(with_trailing(line, a.trailing))
    return lines


def process_lines (p):
    lines = comment_lines(p.comments, 4)
    if p.attributes:
        parts = []
        for key, value in p.attributes.items():
            if value is True:
                parts.append(key)
            elif isinstance(value, str):
                parts.append(f'{key} = "{value}"')
            else:
                parts.append(f'{key} = {value}')
        lines.append(f'    (* {", ".join(parts)} *)')
    if p.kind == 'ff':
        edge = 'posedge' if p.polarity == 'pos' else 'negedge'
        head = f'    always_ff @({edge} {p.clock}) begin : {p.name}'
        nb = True
    else:
        head = f'    always_comb begin : {p.name}'
        nb = False
    lines.append(head)
    lines += stmt_lines(p.body, 8, nb)
    lines.append('    end')
    return lines


def stmt_lines (body, indent, nonblocking):
    lines = []
    pad = ' ' * indent
    for s in body:
        lines += comment_lines(getattr(s, 'comments', []), indent)
        trailing = getattr(s, 'trailing', None)
        if isinstance(s, ir.Assign):
            op = '<=' if nonblocking else '='
            line = f'{pad}{sv_expr(s.target)} {op} {sv_expr(s.value)};'
            lines.append(with_trailing(line, trailing))
        elif isinstance(s, ir.If):
            lines += if_lines(s, indent, nonblocking)
        elif isinstance(s, ir.For):
            lines += for_lines(s, indent, nonblocking)
        elif isinstance(s, ir.Match):
            lines += match_lines(s, indent, nonblocking)
        elif isinstance(s, ir.Assert):
            lines.append('`ifndef SYNTHESIS')
            line = f'{pad}assert ({cond_text(s.cond)});'
            lines.append(with_trailing(line, trailing))
            lines.append('`endif')
        elif isinstance(s, ir.Return):
            line = f'{pad}return {sv_expr(s.value)};'
            lines.append(with_trailing(line, trailing))
        else:
            lines.append(f'{pad}// <{type(s).__name__}>')
    return lines


def if_lines (node, indent, nonblocking):
    # A pruned constant-if is an If with one (None, body) branch: unwrap.
    if len(node.branches) == 1 and node.branches[0][0] is None:
        return stmt_lines(node.branches[0][1], indent, nonblocking)
    pad = ' ' * indent
    ncond = sum(1 for c, _ in node.branches if c is not None)
    if node.unique:
        first_kw = 'unique if'
    elif ncond > 1:
        first_kw = 'priority if'
    else:
        first_kw = 'if'
    lines = []
    for i, (cond, body) in enumerate(node.branches):
        if cond is None:
            head = f'{pad}end else begin'
        elif i == 0:
            head = f'{pad}{first_kw} ({cond_text(cond)}) begin'
        else:
            head = f'{pad}end else if ({cond_text(cond)}) begin'
        if i == 0:
            head = with_trailing(head, node.trailing)
        lines.append(head)
        lines += stmt_lines(body, indent + 4, nonblocking)
    lines.append(f'{pad}end')
    return lines


def for_lines (node, indent, nonblocking):
    pad = ' ' * indent
    head = (f'{pad}for (int {node.var} = {node.start}; '
            f'{node.var} < {node.stop}; {node.var}++) begin')
    lines = [with_trailing(head, node.trailing)]
    lines += stmt_lines(node.body, indent + 4, nonblocking)
    lines.append(f'{pad}end')
    return lines


def match_lines (node, indent, nonblocking):
    pad = ' ' * indent
    has_bits = any(p is not None and p.op == 'bits' for p, _ in node.arms)
    if has_bits:
        kind = 'unique casez' if node.unique else 'casez'
    else:
        kind = 'unique case' if node.unique else 'case'
    lines = [f'{pad}{kind} ({cond_text(node.subject)})']
    inner = indent + 4
    ipad = ' ' * inner
    for pat, body in node.arms:
        if pat is None:
            label = 'default'
        elif pat.op == 'bits':
            label = f"{pat.width}'b{pat.value}"
        else:
            label = sv_expr(pat)
        lines.append(f'{ipad}{label}: begin')
        lines += stmt_lines(body, inner + 4, nonblocking)
        lines.append(f'{ipad}end')
    lines.append(f'{pad}endcase')
    return lines


def sv_const (value, width, signed = False):
    value = int(value)
    if signed:
        return f"{width}'sd{value}"
    if width == 1:
        return "1'b1" if value else "1'b0"
    return f"{width}'d{value}"


def sv_expr (e, index = False):
    if e is None:
        return ''
    op = e.op
    a = e.args
    if op == 'ref':
        return e.value
    if op == 'const':
        if index:
            return str(int(e.value))
        return sv_const(e.value, e.width, e.signed)
    if op == 'enum':
        return e.value.name
    if op == 'bit':
        idx_e = a[1]
        # Array index (width > 1) is a word select; a 1-bit node is a
        # bit select. Parameter arithmetic (WIDTH - 1) is emitted as
        # an unsized index; a signal index is width-cast for Verilator.
        if e.width != 1:
            return f'{sv_expr(a[0])}[{sv_expr(idx_e)}]'
        if idx_e.op == 'const':
            idx = sv_expr(idx_e, index = True)
        elif idx_e.op in ('binop', 'ref'):
            idx = bound_text(idx_e)
        else:
            idx = sv_expr(idx_e)
            need = max(1, (a[0].width - 1).bit_length())
            if idx_e.width != need:
                idx = f"{need}'({idx})"
        return f'{sv_expr(a[0])}[{idx}]'
    if op == 'slice':
        # Bit-selects attach to a primary (a name, a concat, another
        # select). An expression such as (a + b) cannot be sliced in
        # SV; take the low bits with a width cast, or shift then cast.
        base = a[0]
        if base.op not in ('ref', 'bit', 'slice', 'part', 'part_down',
                           'field', 'concat', 'replicate'):
            lo = e.value[1]
            if lo:
                return f"{e.width}'({sv_expr(base)} >> {lo})"
            # [NAME-1:0] on an expression: NAME'(expr) keeps the parameter.
            if (len(a) >= 3 and a[2].op == 'const' and a[2].value == 0
                    and a[1].op == 'binop' and a[1].value == '-'
                    and a[1].args[0].op == 'ref'
                    and a[1].args[1].op == 'const'
                    and a[1].args[1].value == 1):
                return f"{a[1].args[0].value}'({sv_expr(base)})"
            return f"{e.width}'({sv_expr(base)})"
        if len(a) >= 3:
            hi = bound_text(a[1])
            lo = bound_text(a[2])
            return f'{sv_expr(base)}[{hi}:{lo}]'
        hi, lo = e.value[0] - 1, e.value[1]
        return f'{sv_expr(base)}[{hi}:{lo}]'
    if op == 'part':
        idx_e = a[1]
        idx = sv_expr(idx_e)
        need = max(1, (a[0].width - 1).bit_length())
        if idx_e.width != need:
            idx = f"{need}'({idx})"
        return f'{sv_expr(a[0])}[{idx} +: {e.value}]'
    if op == 'part_down':
        idx_e = a[1]
        idx = sv_expr(idx_e)
        need = max(1, (a[0].width - 1).bit_length())
        if idx_e.width != need:
            idx = f"{need}'({idx})"
        return f'{sv_expr(a[0])}[{idx} -: {e.value}]'
    if op == 'field':
        return f'{sv_expr(a[0])}.{e.value}'
    if op == 'concat':
        return '{' + ', '.join(sv_expr(x) for x in a) + '}'
    if op == 'replicate':
        return '{' + f'{e.value}{{{sv_expr(a[0])}}}' + '}'
    if op == 'binop':
        if index:
            return (f'{sv_expr(a[0], True)} {e.value} '
                    f'{sv_expr(a[1], True)}')
        return f'({sv_expr(a[0])} {e.value} {sv_expr(a[1])})'
    if op == 'cmp':
        return f'({sv_expr(a[0])} {e.value} {sv_expr(a[1])})'
    if op == 'unop':
        return f'({e.value}{sv_expr(a[0])})'
    if op == 'not':
        return f'(!{sv_expr(a[0])})'
    if op == 'ifexp':
        return f'({sv_expr(a[0])} ? {sv_expr(a[1])} : {sv_expr(a[2])})'
    if op == 'signed':
        return f'$signed({sv_expr(a[0])})'
    if op == 'extend':
        inner = sv_expr(a[0])
        if e.signed:
            return f"{e.width}'($signed({inner}))"
        return f"{e.width}'({inner})"
    if op == 'call':
        return e.value + '(' + ', '.join(sv_expr(x) for x in a) + ')'
    if op == 'bits':
        return f"{e.width}'b{e.value}"
    return f'/* {op} */'


def bound_text (e):
    """Slice/bit bound: names and arithmetic as written, no sized literals."""
    if e.op in ('binop',):
        return f'({sv_expr(e, index = True)})'
    return sv_expr(e, index = True)


def cond_text (e):
    """Condition in if/case/assert: drop one layer of wrapping parens."""
    text = sv_expr(e)
    if text.startswith('(') and text.endswith(')'):
        depth = 0
        for i, ch in enumerate(text):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0 and i != len(text) - 1:
                    return text
        return text[1:-1]
    return text
