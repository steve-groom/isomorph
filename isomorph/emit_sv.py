"""SystemVerilog emitter: IR modules to IEEE 1800-2017 synthesizable SV.

The output keeps the input's structure: one module per IR module (leaves
first), named processes, named functions, named instances, comments on
the same items. Section 4 of SPEC.txt is the reference."""
import re
import textwrap
import os
import subprocess

from . import ir
from .analyse import ConversionError


def min_width (value):
    if value >= 0:
        return max(1, value.bit_length())
    return (-value - 1).bit_length() + 1


BAR_SV = '//' + '-' * 77


def emit_sv (modules):
    """SystemVerilog text for the module list, leaves first.

    One file holds every module in the design (SPEC 7), so a bar to
    column 79 goes between them: with each module carrying its own
    header comment there is otherwise nothing to say where one ends
    and the next begins."""
    chunks = []
    preamble = struct_typedefs(modules)
    if preamble:
        chunks.append(preamble)
    chunks += [BAR_SV + '\n' + emit_module(m) for m in modules]
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
    rest = (enum_lines(m) + signal_lines(m) + function_lines(m)
            + item_lines(m))
    body += parameter_lines(m, rest)
    body += localparam_lines(m, rest)
    body += rest
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
    """Block docstring as // lines, the shape the VHDL header already
    has. Every comment isomorph emits is then one form: // here, --
    there. lowRISC's Verilog style guide prefers // over /* */, and it
    is what the overwhelming majority of SystemVerilog in the wild
    uses."""
    if not m.header:
        return []
    lines = []
    for line in m.header.strip('\n').splitlines():
        lines.append('// ' + line if line.strip() else '//')
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
        return '//' + text[1:]
    return '// ' + text


def comment_lines (comments, indent):
    pad = ' ' * indent
    return [pad + as_comment(c) for c in (comments or [])]


def with_trailing (line, trailing):
    if trailing:
        return line + '  ' + as_comment(trailing)
    return line


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


def param_hi (params, width):
    """Keep WIDTH-1 in the HDL when the width really is that parameter."""
    name = width_parameter(params, width)
    return f'{name}-1' if name else None


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
    body = [f'{dirs[i]:<{wd}} {types[i]:<{wt}} {names[i]}'
            for i in range(len(ports))]
    wb = max((len(b.rstrip()) for i, b in enumerate(body)
              if ports[i].trailing), default = 0)
    for i, p in enumerate(ports):
        comma = ',' if i < len(ports) - 1 else ''
        out += comment_lines(p.comments, 4)
        marks = attribute_text(p.attributes)
        if marks:
            out.append('    ' + marks)
        waived = p.attributes.get('unused')
        if waived:
            out.append('    /* verilator lint_off UNUSEDSIGNAL */')
        line = '    ' + body[i].rstrip() + comma
        if p.trailing:
            pad = ' ' * max(1, wb + 5 + 1 - len(line))
            line = line + pad + as_comment(p.trailing)
        out.append(line)
        if waived:
            out.append('    /* verilator lint_on UNUSEDSIGNAL */')
    return out


def used_in (name, body):
    """Does this identifier appear in the rendered module body?

    A parameter that only set an array size or a loop bound is folded
    to a literal by then. Declaring it anyway leaves a parameter that
    looks overridable but changes nothing, and every linter says so.
    Isomorph specialises a module per parameter set (4.1), so the
    declaration is documentation, not an interface."""
    word = re.compile(r'\b' + re.escape(name) + r'\b')
    for line in body:
        code = line.split('//')[0]
        if word.search(code):
            return True
    return False


def parameter_lines (m, body = None):
    lines = []
    for name, value in m.parameters.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if body is not None and not used_in(name, body):
                continue
            lines.append(f'    parameter {name} = {value};')
    if lines:
        lines.append('')
    return lines


def localparam_lines (m, body = None):
    lines = []
    for name, value in m.constants.items():
        if body is not None and not used_in(name, body):
            continue
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


def attribute_text (attributes):
    """(* a, b = "c" *) for the attributes that reach the HDL. `unused`
    is isomorph's own marker for a pin the designer means to ignore, so
    it becomes a lint waiver rather than an attribute."""
    parts = []
    for key, value in attributes.items():
        if key == 'unused':
            continue
        if value is True:
            parts.append(key)
        elif isinstance(value, str):
            parts.append(f'{key} = "{value}"')
        else:
            parts.append(f'{key} = {value}')
    return '(* ' + ', '.join(parts) + ' *)' if parts else ''


def signal_lines (m):
    lines = []
    for s in m.signals:
        lines += comment_lines(s.comments, 4)
        packed = packed_type(s.width, s.kind, s.type, m.parameters)
        name = s.name
        if s.array:
            name = f'{name} [{s.array}]'
        marks = attribute_text(s.attributes)
        prefix = f'    {marks}\n' if marks else ''
        if s.attributes.get('unused'):
            prefix = '    /* verilator lint_off UNUSEDSIGNAL */\n' + prefix
        line = prefix + f'    {packed} {name};'
        lines.append(with_trailing(line, s.trailing))
        if s.attributes.get('unused'):
            lines.append('    /* verilator lint_on UNUSEDSIGNAL */')
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
    formals = list(inst.ports.items())
    # an output the parent does not want is open_port(), which is the
    # designer saying so. Verilator flags every empty connection under
    # -Wall, so the waiver goes with the instance that has one rather
    # than leaving --lint to be turned off
    open_ports = [f for f, a in formals if a is None]
    if open_ports:
        lines.append('    /* verilator lint_off PINCONNECTEMPTY */')
    lines.append(f'    {inst.module} {inst.name} (')
    for i, (formal, actual) in enumerate(formals):
        comma = ',' if i < len(formals) - 1 else ''
        mapped = sv_expr(actual) if actual is not None else ''
        lines.append(f'        .{formal}({mapped}){comma}')
    lines.append('    );')
    if open_ports:
        lines.append('    /* verilator lint_on PINCONNECTEMPTY */')
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
        if p.reset:
            redge = 'posedge' if p.reset_polarity == 'pos' else 'negedge'
            lines += reason_lines(p.reason, 4, '//')
            head = (f'    always_ff @({edge} {p.clock} or {redge} '
                    f'{p.reset}) begin : {p.name}')
        else:
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
        elif isinstance(s, ir.Comment):
            lines.append(pad + as_comment(s.text))
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
    headers = getattr(node, 'branch_comments', [])
    for i, (cond, body) in enumerate(node.branches):
        above = headers[i] if i < len(headers) else []
        if above:
            # a comment written above the keyword keeps that place: close
            # the previous branch first, then the comment, then the keyword
            lines.append(f'{pad}end')
            lines += comment_lines(above, indent)
            head = (f'{pad}else begin' if cond is None
                    else f'{pad}else if ({cond_text(cond)}) begin')
        elif cond is None:
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
        elif pat.op == 'const':
            # a case item is compared against the subject, so it carries
            # the subject's width, not the minimum for its value
            label = f"{node.subject.width}'d{int(pat.value)}"
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
        token = e.value
        if token == '>>' and getattr(a[0], 'signed', False):
            # SystemVerilog >> is logical whatever the operand, so a
            # signed right shift is >>>. VHDL's shift_right(signed(x))
            # is already arithmetic, and the two must agree.
            token = '>>>'
        if index:
            return (f'{sv_expr(a[0], True)} {token} '
                    f'{sv_expr(a[1], True)}')
        return f'({sv_expr(a[0])} {token} {sv_expr(a[1])})'
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
