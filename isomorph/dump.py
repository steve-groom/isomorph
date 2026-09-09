"""Readable text of the IR: the check that every signal width and every
process was found (order of work, step 2)."""
from . import ir


def dump_module (m):
    out = [f'module {m.name} (block {m.block})']
    if m.parameters:
        out.append('  parameters: ' + ', '.join(f'{k} = {v!r}' for k, v in m.parameters.items()))
    for p in m.ports:
        arr = f' [{p.array}]' if p.array else ''
        out.append(f'  port {p.direction:3} {p.name} [{p.width}]{arr} {p.kind}')
    for name, e in m.enums.items():
        out.append(f'  enum {name} [{e.width}] ' + ', '.join(
            f'{x.name}={x.value}' for x in e.members))
    for name, v in m.constants.items():
        out.append(f'  const {name} = {v}')
    for s in m.signals:
        for c in s.comments:
            out.append(f'  {c}')
        extra = f'    {s.trailing}' if s.trailing else ''
        if s.array:
            extra += f' [{s.array}]'
        if s.wrap:
            extra += ' wrap'
        if s.kind == 'enum':
            extra = f' enum {s.type.name}' + extra
        out.append(f'  signal {s.name} [{s.width}]{extra}')
    for f in m.functions:
        out.append(f'  function {f.name}(' + ', '.join(
            f'{n}[{w}]' for n, w, _ in f.params) + f') -> [{f.width}]')
        out += stmts(f.body, 4)
    for a in m.assigns:
        out.append(f'  assign {ir.render(a.target)} = {ir.render(a.value)}')
    for p in m.processes:
        for c in p.comments:
            out.append(f'  {c}')
        clk = f' @{p.polarity}edge {p.clock}' if p.kind == 'ff' else ''
        out.append(f'  process {p.name} ({p.kind}{clk})')
        out += stmts(p.body, 4)
    for i in m.instances:
        out.append(f'  instance {i.name} : {i.module}')
        for formal, actual in i.ports.items():
            out.append(f'    .{formal} ({ir.render(actual) if actual else "open"})')
    return '\n'.join(out)


def stmts (body, indent):
    pad = ' ' * indent
    out = []
    for s in body:
        for c in getattr(s, 'comments', []):
            out.append(f'{pad}{c}')
        tail = f'    {s.trailing}' if getattr(s, 'trailing', None) else ''
        if isinstance(s, ir.Assign):
            out.append(f'{pad}{ir.render(s.target)} = {ir.render(s.value)}{tail}')
        elif isinstance(s, ir.If):
            first = True
            for cond, b in s.branches:
                if cond is None:
                    out.append(f'{pad}else:' if not first else f'{pad}always:')
                else:
                    tag = 'unique ' if first and s.unique else ''
                    kw = 'if' if first else 'elif'
                    out.append(f'{pad}{tag}{kw} {ir.render(cond)}:')
                first = False
                out += stmts(b, indent + 4)
        elif isinstance(s, ir.For):
            out.append(f'{pad}for {s.var} in {s.start}..{s.stop - 1}:')
            out += stmts(s.body, indent + 4)
        elif isinstance(s, ir.Match):
            tag = ' unique' if s.unique else ''
            out.append(f'{pad}match{tag} {ir.render(s.subject)}:')
            for pat, b in s.arms:
                label = '_' if pat is None else ir.render(pat)
                out.append(f'{pad}    case {label}:')
                out += stmts(b, indent + 8)
        elif isinstance(s, ir.Assert):
            out.append(f'{pad}assert {ir.render(s.cond)}')
        elif isinstance(s, ir.Return):
            out.append(f'{pad}return {ir.render(s.value)}')
        else:
            out.append(f'{pad}<{type(s).__name__}>')
    return out


def dump (modules, warnings):
    text = '\n\n'.join(dump_module(m) for m in modules)
    if warnings:
        text += '\n\nwarnings:\n' + '\n'.join('  ' + w for w in warnings)
    return text
