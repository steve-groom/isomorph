"""C99 cycle-accurate smoke from the IR (SPEC 6.1).

One struct per module, one function per process, uint64_t fields
masked to width. tick() settles comb, runs clocked processes into
*_nxt, commits, settles comb again. Not the design of record.
"""
import os
import shutil

from . import ir
from .analyse import ConversionError


SETTLE_LIMIT = 64


def mask_expr (w):
    if w >= 64:
        return '~0ULL'
    if w <= 0:
        return '0ULL'
    return f'((1ULL << {w}) - 1ULL)'


def check_width (w, where):
    """The C99 backend stores every value in a uint64_t.

    That is the ceiling, and it is the only place the three backends
    do not cover the same designs. A wider signal converts to
    SystemVerilog and VHDL and runs on Verilator; it is the C99 smoke
    build that cannot hold it."""
    if w > 64:
        raise ConversionError(
            f'C99 smoke: {where} is {w} bits, and a C99 field is a '
            'uint64_t (SPEC 6.1). Split the signal, or leave the C99 '
            'backend out and run this design with --run verilator, '
            'which has no width limit and is the design of record '
            'anyway.')


def emit_c99 (modules):
    """Return (header_text, source_text)."""
    if not modules:
        return '', ''
    for m in modules:
        for p in m.ports:
            check_width(p.width, f'port {p.name}')
        for s in m.signals:
            check_width(s.width, f'signal {s.name}')
    top = modules[-1].name
    header = emit_header(modules, top)
    source = emit_source(modules, top)
    return header, source


def write_c99 (modules, c_path):
    header, source = emit_c99(modules)
    base, _ = os.path.splitext(c_path)
    h_path = base + '.h'
    vcd_path = base + '_vcd.c'
    directory = os.path.dirname(os.path.abspath(c_path))
    if directory:
        os.makedirs(directory, exist_ok = True)
    with open(h_path, 'w', encoding = 'ascii', newline = '\n') as f:
        f.write(header)
    with open(c_path, 'w', encoding = 'ascii', newline = '\n') as f:
        f.write(source)
    with open(vcd_path, 'w', encoding = 'ascii', newline = '\n') as f:
        f.write(emit_c99_vcd(modules))
    copy_runtime(directory)
    return c_path, h_path


RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'runtime')


def copy_runtime (directory):
    """iso_vcd.[ch] and iso_log.[ch] next to the emitted sources, so the
    output directory compiles on a machine with no isomorph installed."""
    copied = []
    if not os.path.isdir(RUNTIME_DIR):
        return copied
    for name in sorted(os.listdir(RUNTIME_DIR)):
        if not name.endswith(('.c', '.h')):
            continue
        target = os.path.join(directory, name)
        shutil.copyfile(os.path.join(RUNTIME_DIR, name), target)
        copied.append(target)
    return copied


def emit_header (modules, top):
    by_name = {m.name: m for m in modules}
    guard = top.upper() + '_H'
    lines = [
        f'#ifndef {guard}',
        f'#define {guard}',
        '',
        '#include <stdint.h>',
        '',
    ]
    for m in modules:
        lines += struct_lines(m)
        lines.append(f'void {m.name}_init({m.name} *s);')
        lines.append(f'int {m.name}_eval({m.name} *s);')
        lines.append(f'int {m.name}_clock({m.name} *s);')
        lines.append(f'int {m.name}_tick({m.name} *s);')
        for clk in hierarchy_clocks(m, by_name):
            tag = clock_id(clk)
            lines.append(f'int {m.name}_clock_{tag}({m.name} *s);')
            lines.append(f'int {m.name}_tick_{tag}({m.name} *s);')
            lines.append(f'void {m.name}_edge_{tag}({m.name} *s);')
        lines.append(f'int {m.name}_settle({m.name} *s);')
        lines.append('')
    lines.append('typedef struct IsoVcd IsoVcd;')
    lines.append('typedef struct IsoLog IsoLog;')
    for m in modules:
        lines.append(f'void {m.name}_vcd_defs(IsoVcd *v);')
        lines.append(f'void {m.name}_vcd_dump(IsoVcd *v, const {m.name} *s);')
        lines.append(f'void {m.name}_log_dump(IsoLog *l, const {m.name} *s, '
                     f'const char *prefix);')
    lines.append('')
    lines.append(f'#endif /* {guard} */')
    lines.append('')
    return '\n'.join(lines)


def struct_lines (m):
    ff = ff_driven_names(m)
    lines = [f'typedef struct {m.name} {{']
    for p in m.ports:
        lines += field_lines(p.name, p.width, p.array, p.name in ff)
    for s in m.signals:
        lines += field_lines(s.name, s.width, s.array, s.name in ff)
    for inst in m.instances:
        lines.append(f'    {inst.module} {inst.name};')
    lines.append(f'}} {m.name};')
    lines.append('')
    return lines


def field_lines (name, width, array, has_nxt):
    check_width(width, name)
    if array:
        decl = f'    uint64_t {c_id(name)}[{array}];'
        lines = [decl]
        if has_nxt:
            lines.append(f'    uint64_t {c_id(name)}_nxt[{array}];')
        return lines
    lines = [f'    uint64_t {c_id(name)};']
    if has_nxt:
        lines.append(f'    uint64_t {c_id(name)}_nxt;')
    return lines


def clock_id (name):
    """A clock name as part of a C function name.

    An array element is a legal clock and a legal signal, and
    pll0_i_clocks[0] is neither a legal C identifier nor part of one,
    so the brackets become an underscore."""
    return name.replace('[', '_').replace(']', '')


def c_id (name):
    if '[' in name and name.endswith(']'):
        base, rest = name.split('[', 1)
        return f'{base}[{rest[:-1]}]'
    return name


def ff_driven_names (m):
    names = set()
    for p in m.processes:
        if p.kind == 'ff':
            names |= assigned_roots(p.body)
    return names


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


def has_asserts (modules):
    """Does anything in this design carry an assert?

    The helpers below cost nothing at run time and a -Werror build
    refuses an unused static function, so a design with no invariants
    does not get the machinery."""

    def walk (body):
        for s in body:
            if isinstance(s, ir.Assert):
                return True
            for part in (getattr(s, 'body', None) or []):
                if walk([part]):
                    return True
            for _, branch in (getattr(s, 'branches', None) or []):
                if walk(branch):
                    return True
            for _, arm in (getattr(s, 'arms', None) or []):
                if walk(arm):
                    return True
        return False

    for m in modules:
        for p in m.processes:
            if walk(p.body):
                return True
        for f in m.functions:
            if walk(f.body):
                return True
    return False


def emit_source (modules, top):
    by_name = {m.name: m for m in modules}
    lines = [
        f'#include "{top}.h"',
        '#include <string.h>',
        '#include <stdio.h>',
        '',
        f'#define ISO_MASK(w) ((unsigned)(w) >= 64u ? ~0ULL : '
        f'((1ULL << (w)) - 1ULL))',
        f'#define ISO_SETTLE {SETTLE_LIMIT}',
        '',
    ]
    if has_asserts(modules):
        lines += [
            '/* An assert that fires records itself here rather than',
            '   stopping, so the whole settle finishes and the caller',
            '   raises once with the first message. This used to be',
            '   emitted as a comment, which meant a design could carry',
            '   an invariant only two of the three backends checked. */',
            'static int iso_assert_hit;',
            'static char iso_assert_text[256];',
            '',
            'static void iso_assert_fail (const char *text)',
            '{',
            '    if (!iso_assert_hit) {',
            '        iso_assert_hit = 1;',
            '        snprintf(iso_assert_text, sizeof iso_assert_text,',
            '                 "%s", text);',
            '    }',
            '}',
            '',
            'int iso_assert_failed (void) { return iso_assert_hit; }',
            'const char *iso_assert_message (void)',
            '{',
            '    return iso_assert_text;',
            '}',
            'void iso_assert_clear (void) { iso_assert_hit = 0; }',
            '',
        ]
    for m in modules:
        lines.append(f'static void {m.name}_posedge({m.name} *s);')
        for clk in module_clocks(m):
            lines.append(f'static void {m.name}_posedge_{clk}({m.name} *s);')
        lines.append(f'static void {m.name}_commit({m.name} *s);')
        lines.append(f'static int {m.name}_live_eq(const {m.name} *a, '
                     f'const {m.name} *b);')
    lines.append('')
    seen_macros = set()
    for m in modules:
        lines += enum_defines(m, seen_macros)
        lines += const_defines(m, seen_macros)
        lines += function_lines(m)
        lines += process_fn_lines(m)
        lines += eval_tick_lines(m, by_name)
    return '\n'.join(lines)


def enum_defines (m, seen):
    lines = []
    for name, enum_type in m.enums.items():
        for member in enum_type.members:
            if member.name in seen:
                continue
            seen.add(member.name)
            lines.append(f'#define {member.name} {int(member.value)}ULL')
        if lines:
            lines.append('')
    return lines


def const_defines (m, seen):
    lines = []
    for name, value in m.constants.items():
        if name in seen:
            continue
        seen.add(name)
        lines.append(f'#define {name} {int(value)}ULL')
    for name, value in m.parameters.items():
        if isinstance(value, int) and not isinstance(value, bool):
            if name in seen:
                continue
            seen.add(name)
            lines.append(f'#define {name} {int(value)}ULL')
    if lines:
        lines.append('')
    return lines


def function_lines (m):
    ctx = Ctx(m, ff_driven_names(m))
    lines = []
    for f in m.functions:
        check_width(f.width, f'function {f.name}')
        args = ', '.join(f'uint64_t {n}' for n, w, _ in f.params)
        lines.append(f'static uint64_t {f.name}({args})')
        lines.append('{')
        for name, width in f.locals.items():
            check_width(width, f'local {name}')
            lines.append(f'    uint64_t {name} = 0;')
        ctx.locals = set(f.locals) | set(n for n, _, _ in f.params)
        ctx.ff_write = False
        lines += stmt_lines(ctx, f.body, 4)
        ctx.locals = set()
        lines.append('}')
        lines.append('')
    return lines


def process_fn_lines (m):
    ctx = Ctx(m, ff_driven_names(m))
    lines = []
    for p in m.processes:
        ctx.ff_write = (p.kind == 'ff')
        lines.append(f'static void {m.name}_{p.name}({m.name} *s)')
        lines.append('{')
        lines += stmt_lines(ctx, p.body, 4)
        lines.append('}')
        lines.append('')
        if p.kind == 'ff' and p.reset:
            lines += async_reset_fn_lines(m, p, ctx)
    return lines


def async_reset_fn_lines (m, p, ctx):
    """The reset branch on its own, writing the live value and the
    pending one, called from comb_once while the reset is held. A cycle
    simulator has no asynchronous event; this is the closest it gets."""
    branch = p.body[0].branches[0][1]
    saved = ctx.ff_write
    ctx.ff_write = False
    lines = [f'static void {m.name}_{p.name}_areset({m.name} *s)', '{']
    lines += stmt_lines(ctx, branch, 4)
    ctx.ff_write = saved
    for name in sorted(assigned_roots(branch)):
        if '[' in name:
            continue
        cid = c_id(name)
        sig = next((x for x in m.signals if x.name == name), None)
        port = next((x for x in m.ports if x.name == name), None)
        arr = (sig.array if sig else (port.array if port else 0))
        if arr:
            lines.append(f'    memcpy(s->{cid}_nxt, s->{cid}, '
                         f'sizeof(s->{cid}));')
        else:
            lines.append(f'    s->{cid}_nxt = s->{cid};')
    lines.append('}')
    lines.append('')
    return lines


def eval_tick_lines (m, by_name):
    ff = ff_driven_names(m)
    lines = []
    lines.append(f'void {m.name}_init({m.name} *s)')
    lines.append('{')
    lines.append(f'    memset(s, 0, sizeof(*s));')
    for inst in m.instances:
        lines.append(f'    {inst.module}_init(&s->{inst.name});')
    lines.append('}')
    lines.append('')
    lines.append(f'static void {m.name}_comb_once({m.name} *s)')
    lines.append('{')
    for inst in m.instances:
        lines += instance_copy_in(m, inst, by_name)
        lines.append(f'    {inst.module}_eval(&s->{inst.name});')
        lines += instance_copy_out(m, inst, by_name)
    for a in m.assigns:
        tgt = c_expr(Ctx(m, ff), a.target, lhs = True)
        val = c_expr(Ctx(m, ff), a.value)
        w = a.target.width
        lines.append(f'    {tgt} = ({val}) & {mask_expr(w)};')
    for p in m.processes:
        if p.kind == 'comb':
            lines.append(f'    {m.name}_{p.name}(s);')
    for p in m.processes:
        if p.kind == 'ff' and p.reset:
            test = f's->{c_id(p.reset)}' if p.reset_polarity == 'pos' \
                else f'!s->{c_id(p.reset)}'
            lines.append(f'    if ({test}) {{')
            lines.append(f'        {m.name}_{p.name}_areset(s);')
            lines.append('    }')
    lines.append('}')
    lines.append('')
    lines += live_eq_lines(m, by_name)
    lines.append(f'int {m.name}_eval({m.name} *s)')
    lines.append('{')
    lines.append(f'    {m.name} prev;')
    lines.append('    int n;')
    lines.append('    for (n = 0; n < ISO_SETTLE; n++) {')
    lines.append('        prev = *s;')
    lines.append(f'        {m.name}_comb_once(s);')
    lines.append(f'        if ({m.name}_live_eq(&prev, s)) {{')
    lines.append('            return 0;')
    lines.append('        }')
    lines.append('    }')
    lines.append('    return 1;')
    lines.append('}')
    lines.append('')
    lines += posedge_lines(m, by_name, clock = None)
    for clk in hierarchy_clocks(m, by_name):
        lines += posedge_lines(m, by_name, clock = clk)

    lines.append(f'static void {m.name}_commit({m.name} *s)')
    lines.append('{')
    for name in sorted(ff):
        cid = c_id(name)
        if '[' in name:
            continue
        sig = next((x for x in m.signals if x.name == name), None)
        port = next((x for x in m.ports if x.name == name), None)
        arr = (sig.array if sig else (port.array if port else 0))
        if arr:
            lines.append(f'    memcpy(s->{cid}, s->{cid}_nxt, '
                         f'sizeof(s->{cid}));')
        else:
            lines.append(f'    s->{cid} = s->{cid}_nxt;')
    for inst in m.instances:
        child = by_name.get(inst.module)
        if child is not None and any(p.kind == 'ff' for p in child.processes):
            lines.append(f'    {inst.module}_commit(&s->{inst.name});')
    lines.append('}')
    lines.append('')
    lines.append(f'int {m.name}_clock({m.name} *s)')
    lines.append('{')
    lines.append(f'    {m.name}_posedge(s);')
    lines.append(f'    {m.name}_commit(s);')
    lines.append(f'    return {m.name}_eval(s);')
    lines.append('}')
    lines.append('')
    lines.append(f'int {m.name}_tick({m.name} *s)')
    lines.append('{')
    lines.append(f'    if ({m.name}_eval(s)) {{')
    lines.append('        return 1;')
    lines.append('    }')
    lines.append(f'    return {m.name}_clock(s);')
    lines.append('}')
    lines.append('')
    # An edge on its own, with no commit and no settle after it.
    # Two clocks that land on the same femtosecond have to take their
    # edges before either one's new values are visible, or the second
    # domain samples the first domain's post-edge value and a
    # clock-domain crossing simulates as something no hardware does.
    lines.append('/* one clock edge, no commit: for coincident edges */')
    for clk in hierarchy_clocks(m, by_name):
        tag = clock_id(clk)
        lines.append(f'void {m.name}_edge_{tag}({m.name} *s)')
        lines.append('{')
        lines.append(f'    {m.name}_posedge_{tag}(s);')
        lines.append('}')
        lines.append('')
    lines.append(f'int {m.name}_settle({m.name} *s)')
    lines.append('{')
    lines.append(f'    {m.name}_commit(s);')
    lines.append(f'    return {m.name}_eval(s);')
    lines.append('}')
    lines.append('')

    for clk in hierarchy_clocks(m, by_name):
        tag = clock_id(clk)
        lines.append(f'int {m.name}_clock_{tag}({m.name} *s)')
        lines.append('{')
        lines.append(f'    {m.name}_posedge_{tag}(s);')
        lines.append(f'    {m.name}_commit(s);')
        lines.append(f'    return {m.name}_eval(s);')
        lines.append('}')
        lines.append('')
        lines.append(f'int {m.name}_tick_{tag}({m.name} *s)')
        lines.append('{')
        lines.append(f'    if ({m.name}_eval(s)) {{')
        lines.append('        return 1;')
        lines.append('    }')
        lines.append(f'    return {m.name}_clock_{tag}(s);')
        lines.append('}')
        lines.append('')
    return lines


def module_clocks (m):
    out = []
    seen = set()
    for p in m.processes:
        if p.kind == 'ff' and p.clock and p.clock not in seen:
            seen.add(p.clock)
            out.append(p.clock)
    return out


def hierarchy_clocks (m, by_name):
    """Every clock name visible at this level, including one that only
    reaches a child.

    module_clocks sees a module's own processes. A block whose clock
    does nothing but drive an instance, as a per-domain reset
    synchroniser does, has no process of its own on it, so without this
    it gets no clock function and the simulator either does nothing on
    that edge or clocks everything on it."""
    out = list(module_clocks(m))
    for inst in m.instances:
        child = by_name.get(inst.module)
        if child is None:
            continue
        inner = set(hierarchy_clocks(child, by_name))
        for formal, actual in inst.ports.items():
            if actual is None or getattr(actual, 'op', None) != 'ref':
                continue
            if (formal in inner) and (actual.value not in out):
                out.append(actual.value)
    return out


def map_inst_clock (inst, child, parent_clock):
    """The child's own name for a clock the parent drives, or None.

    Matched by connection and never by name. A child is clocked on the
    parent's edge only when one of its clock ports is really wired to
    that parent clock. Falling back to "the child has a process on a
    port that happens to be called the same thing" clocked every
    a synchroniser chain in a PLL monitor on the parent's i_clock, because a
    synchroniser's own clock port is also called i_clock, and a
    crossing then propagated in one edge instead of two. Verilator got
    it right and the two smoke backends did not.
    """
    if child is None or parent_clock is None:
        return None
    for formal, actual in inst.ports.items():
        if actual is None:
            continue
        if (getattr(actual, 'op', None) == 'ref'
                and actual.value == parent_clock):
            if any(p.kind == 'ff' and p.clock == formal
                   for p in child.processes):
                return formal
    return None


def posedge_lines (m, by_name, clock):
    suffix = '' if clock is None else '_' + clock_id(clock)
    lines = [f'static void {m.name}_posedge{suffix}({m.name} *s)']
    lines.append('{')
    for p in m.processes:
        if p.kind != 'ff':
            continue
        if clock is not None and p.clock != clock:
            continue
        lines.append(f'    {m.name}_{p.name}(s);')
    for inst in m.instances:
        child = by_name.get(inst.module)
        if child is None:
            continue
        if clock is None:
            if any(p.kind == 'ff' for p in child.processes):
                lines.append(f'    {inst.module}_posedge(&s->{inst.name});')
            continue
        mapped = map_inst_clock(inst, child, clock)
        if mapped:
            lines.append(f'    {inst.module}_posedge_{mapped}('
                         f'&s->{inst.name});')
    lines.append('}')
    lines.append('')
    return lines


def live_eq_lines (m, by_name):
    lines = [f'static int {m.name}_live_eq(const {m.name} *a, '
             f'const {m.name} *b)']
    lines.append('{')
    for p in m.ports:
        lines += live_cmp_field(p.name, p.array)
    for s in m.signals:
        lines += live_cmp_field(s.name, s.array)
    for inst in m.instances:
        if inst.module in by_name:
            lines.append(f'    if (!{inst.module}_live_eq('
                         f'&a->{inst.name}, &b->{inst.name})) {{')
            lines.append('        return 0;')
            lines.append('    }')
    lines.append('    return 1;')
    lines.append('}')
    lines.append('')
    return lines


def live_cmp_field (name, array):
    cid = c_id(name)
    if array:
        return [
            f'    if (memcmp(a->{cid}, b->{cid}, '
            f'sizeof(a->{cid})) != 0) {{',
            '        return 0;',
            '    }',
        ]
    return [
        f'    if (a->{cid} != b->{cid}) {{',
        '        return 0;',
        '    }',
    ]


def instance_copy_in (m, inst, by_name):
    child = by_name.get(inst.module)
    if child is None:
        return []
    directions = {p.name: p.direction for p in child.ports}
    lines = []
    ctx = Ctx(m, set())
    for formal, actual in inst.ports.items():
        if actual is None:
            continue
        if directions.get(formal) == 'out':
            continue
        lines.append(
            f'    s->{inst.name}.{c_id(formal)} = '
            f'{c_expr(ctx, actual)} & {mask_expr(actual.width)};')
    return lines


def instance_copy_out (m, inst, by_name):
    child = by_name.get(inst.module)
    if child is None:
        return []
    directions = {p.name: p.direction for p in child.ports}
    widths = {p.name: p.width for p in child.ports}
    lines = []
    ctx = Ctx(m, set())
    for formal, actual in inst.ports.items():
        if actual is None:
            continue
        if directions.get(formal) != 'out':
            continue
        tgt = c_expr(ctx, actual, lhs = True)
        w = widths.get(formal, actual.width)
        lines.append(
            f'    {tgt} = s->{inst.name}.{c_id(formal)} & {mask_expr(w)};')
    return lines


class Ctx:
    def __init__ (self, module, ff_names):
        self.m = module
        self.ff_names = ff_names
        self.ff_write = False
        self.locals = set()
        self.int_names = set(module.constants)
        for n, v in module.parameters.items():
            if isinstance(v, int) and not isinstance(v, bool):
                self.int_names.add(n)
        self.loop_vars = set()
        self.widths = {}
        self.arrays = set()
        for p in module.ports:
            self.widths[p.name] = p.width
            if p.array:
                self.arrays.add(p.name)
        for s in module.signals:
            self.widths[s.name] = s.width
            if s.array:
                self.arrays.add(s.name)


def stmt_lines (ctx, body, indent):
    lines = []
    pad = ' ' * indent
    for s in body:
        if isinstance(s, ir.Assign):
            lines.append(pad + c_assign(ctx, s))
        elif isinstance(s, ir.If):
            lines += c_if(ctx, s, indent)
        elif isinstance(s, ir.For):
            lines += c_for(ctx, s, indent)
        elif isinstance(s, ir.Match):
            lines += c_match(ctx, s, indent)
        elif isinstance(s, ir.Assert):
            text = (s.message or 'assertion failed').replace('"', "'")
            lines.append(f'{pad}if (!({c_cond(ctx, s.cond)})) {{')
            lines.append(f'{pad}    iso_assert_fail("{text} (line '
                         f'{s.line})");')
            lines.append(f'{pad}}}')
        elif isinstance(s, ir.Return):
            w = s.value.width
            lines.append(f'{pad}return ({c_expr(ctx, s.value)}) & '
                         f'{mask_expr(w)};')
        elif isinstance(s, ir.Comment):
            lines.append(pad + '//' + s.text.lstrip('#'))
        else:
            lines.append(f'{pad}/* <{type(s).__name__}> */;')
    return lines


def c_if (ctx, node, indent):
    if len(node.branches) == 1 and node.branches[0][0] is None:
        return stmt_lines(ctx, node.branches[0][1], indent)
    pad = ' ' * indent
    lines = []
    for i, (cond, body) in enumerate(node.branches):
        if cond is None:
            lines.append(f'{pad}}} else {{')
        elif i == 0:
            lines.append(f'{pad}if ({c_cond(ctx, cond)}) {{')
        else:
            lines.append(f'{pad}}} else if ({c_cond(ctx, cond)}) {{')
        inner = stmt_lines(ctx, body, indent + 4)
        if inner:
            lines += inner
        else:
            lines.append(f'{pad}    ;')
    lines.append(f'{pad}}}')
    return lines


def c_for (ctx, node, indent):
    pad = ' ' * indent
    ctx.loop_vars.add(node.var)
    lines = [f'{pad}{{']
    lines.append(f'{pad}    int {node.var};')
    lines.append(f'{pad}    for ({node.var} = {node.start}; '
                 f'{node.var} < {node.stop}; {node.var}++) {{')
    lines += stmt_lines(ctx, node.body, indent + 8)
    lines.append(f'{pad}    }}')
    lines.append(f'{pad}}}')
    ctx.loop_vars.discard(node.var)
    return lines


def c_match (ctx, node, indent):
    pad = ' ' * indent
    subj = c_expr(ctx, node.subject)
    lines = []
    first = True
    for pat, body in node.arms:
        if pat is None:
            head = f'{pad}}} else {{' if not first else f'{pad}{{'
            if not first:
                lines.append(f'{pad}}} else {{')
            else:
                lines.append(f'{pad}{{')
        elif pat.op == 'bits':
            mask, val = bits_mask(pat.value)
            cond = f'(({subj}) & {mask}ULL) == {val}ULL'
            kw = 'if' if first else '} else if'
            lines.append(f'{pad}{kw} ({cond}) {{')
        else:
            cond = f'({subj}) == ({c_expr(ctx, pat)})'
            kw = 'if' if first else '} else if'
            lines.append(f'{pad}{kw} ({cond}) {{')
        first = False
        inner = stmt_lines(ctx, body, indent + 4)
        if inner:
            lines += inner
        else:
            lines.append(f'{pad}    ;')
    lines.append(f'{pad}}}')
    return lines


def bits_mask (pattern):
    mask = 0
    val = 0
    for i, ch in enumerate(reversed(pattern)):
        if ch == '1':
            mask |= 1 << i
            val |= 1 << i
        elif ch == '0':
            mask |= 1 << i
    return mask, val


def c_cond (ctx, e):
    if e.op == 'cmp':
        return c_cmp(ctx, e)
    if e.op == 'not':
        return f'!({c_cond(ctx, e.args[0])})'
    return f'({c_expr(ctx, e)}) != 0ULL'


def c_cmp (ctx, e):
    op = {'==': '==', '!=': '!=', '<': '<', '<=': '<=',
          '>': '>', '>=': '>='}[e.value]
    a = c_expr(ctx, e.args[0])
    b = c_expr(ctx, e.args[1])
    if e.value in ('<', '<=', '>', '>=') and e.args[0].signed:
        return (f'(int64_t)({a}) {op} (int64_t)({b})')
    return f'({a}) {op} ({b})'


def c_expr (ctx, e, lhs = False, index = False):
    if e is None:
        return '0'
    check_width(e.width, 'expression')
    op = e.op
    a = e.args
    if op == 'ref':
        return c_ref(ctx, e.value, lhs)
    if op == 'const':
        if index:
            return str(int(e.value))
        return f'{int(e.value)}ULL'
    if op == 'enum':
        return e.value.name
    if op == 'bit':
        if is_array_element(ctx, e):
            name = c_expr(ctx, a[0], lhs = lhs)
            return f'{name}[{c_index(ctx, a[1])}]'
        return c_bit(ctx, e, lhs)
    if op == 'slice':
        return c_slice(ctx, e, lhs)
    if op == 'part':
        return c_part(ctx, e, lhs)
    if op == 'part_down':
        src = c_expr(ctx, a[0])
        i = c_index(ctx, a[1])
        w = e.value
        return f'((({src}) >> (({i}) - ({w} - 1))) & {mask_expr(w)})'
    if op == 'field':
        src = c_expr(ctx, a[0])
        lo = int(a[1].value) if len(a) > 1 and a[1].op == 'const' else 0
        return f'((({src}) >> {lo}) & {mask_expr(e.width)})'
    if op == 'concat':
        return c_concat(ctx, e)
    if op == 'replicate':
        return c_replicate(ctx, e)
    if op == 'binop':
        if index:
            return (f'({c_expr(ctx, a[0], index = True)} {e.value} '
                    f'{c_expr(ctx, a[1], index = True)})')
        return c_binop(ctx, e)
    if op == 'cmp':
        return f'(({c_cmp(ctx, e)}) ? 1ULL : 0ULL)'
    if op == 'unop':
        w = e.width
        inner = c_expr(ctx, a[0])
        if e.value == '-':
            return f'((0ULL - ({inner})) & {mask_expr(w)})'
        return f'((~({inner})) & {mask_expr(w)})'
    if op == 'not':
        return f'(({c_expr(ctx, a[0])}) ? 0ULL : 1ULL)'
    if op == 'ifexp':
        return (f'(({c_cond(ctx, a[0])}) ? ({c_expr(ctx, a[1])}) : '
                f'({c_expr(ctx, a[2])}))')
    if op == 'signed':
        return c_expr(ctx, a[0])
    if op == 'extend':
        inner = c_expr(ctx, a[0])
        if e.signed:
            w = a[0].width
            sh = 64 - w
            return (
                f'(((uint64_t)((int64_t)(((int64_t)({inner})) '
                f'<< {sh}) >> {sh})) & {mask_expr(e.width)})'
            )
        return f'(({inner}) & {mask_expr(a[0].width)})'
    if op == 'call':
        args = ', '.join(c_expr(ctx, x) for x in a)
        return f'{e.value}({args})'
    if op == 'bits':
        mask, val = bits_mask(e.value)
        return f'{val}ULL'
    return '0ULL'


def c_ref (ctx, name, lhs):
    if name in ctx.locals or name in ctx.loop_vars or name in ctx.int_names:
        return name
    nxt = lhs and ctx.ff_write and root_plain(name) in ctx.ff_names
    cid = c_id(name)
    if '[' in cid and not cid.startswith('s->'):
        # mem[2] stored as field mem
        base = cid.split('[', 1)[0]
        idx = cid.split('[', 1)[1][:-1]
        field = f'{base}_nxt' if nxt else base
        return f's->{field}[{idx}]'
    field = cid + ('_nxt' if nxt else '')
    return f's->{field}'


def root_plain (name):
    return name.split('[')[0]


def c_bit (ctx, e, lhs):
    base, idx = e.args
    if e.width != 1:
        b = c_expr(ctx, base, lhs = lhs)
        i = c_index(ctx, idx)
        return f'{b}[{i}]'
    if lhs:
        # bit assign: handled as read-modify in c_assign? IR target is bit.
        # Emit a statement-level RMW in c_assign via special case.
        pass
    b = c_expr(ctx, base)
    i = c_index(ctx, idx)
    return f'((({b}) >> ({i})) & 1ULL)'


def c_index (ctx, e):
    if e.op == 'const':
        return str(int(e.value))
    if e.op == 'ref' and e.value in ctx.int_names | ctx.loop_vars:
        return e.value
    if e.op == 'binop':
        return (f'({c_index(ctx, e.args[0])} {e.value} '
                f'{c_index(ctx, e.args[1])})')
    return f'(int)({c_expr(ctx, e)})'


def c_slice (ctx, e, lhs):
    base = e.args[0]
    lo = e.value[1]
    w = e.width
    if len(e.args) >= 3 and e.args[2].op == 'const':
        lo = int(e.args[2].value)
    src = c_expr(ctx, base)
    if lhs:
        return src  # RMW at assign time; rare for slices as full target
    return f'((({src}) >> {lo}) & {mask_expr(w)})'


def c_part (ctx, e, lhs):
    base, idx = e.args
    w = e.value
    src = c_expr(ctx, base)
    i = c_index(ctx, idx)
    return f'((({src}) >> ({i})) & {mask_expr(w)})'


def c_concat (ctx, e):
    parts = []
    shift = 0
    for x in reversed(e.args):
        parts.append(f'((({c_expr(ctx, x)}) & {mask_expr(x.width)}) '
                     f'<< {shift})')
        shift += x.width
    check_width(shift, 'concat')
    return '(' + ' | '.join(parts) + ')'


def c_replicate (ctx, e):
    n = e.value
    inner = e.args[0]
    src = c_expr(ctx, inner)
    w = inner.width
    total = n * w
    check_width(total, 'replicate')
    if w == 1:
        return f'(({src}) & 1ULL ? {mask_expr(n)} : 0ULL)'
    parts = []
    for i in range(n):
        parts.append(f'((({src}) & {mask_expr(w)}) << {i * w})')
    return '(' + ' | '.join(parts) + ')'


def c_signed (text, e):
    """An operand as int64_t, sign extended from its own width.

    signed() only marks an expression; the bits underneath are the same.
    That is enough for + - and the bitwise operators, and not enough for
    * and >>, where sign decides the answer."""
    if not getattr(e, 'signed', False):
        return f'((int64_t)({text}))'
    sh = 64 - e.width
    if sh <= 0:
        return f'((int64_t)({text}))'
    return f'((((int64_t)({text})) << {sh}) >> {sh})'


def c_binop (ctx, e):
    op = e.value
    a, b = e.args
    w = e.width
    left = c_expr(ctx, a)
    right = c_expr(ctx, b)
    if op in ('&', '|', '^'):
        return f'(({left}) {op} ({right})) & {mask_expr(w)}'
    if op in ('+', '-', '*'):
        return (f'((uint64_t)({c_signed(left, a)} {op} '
                f'{c_signed(right, b)})) & {mask_expr(w)}')
    if op == '<<':
        shift = c_expr(ctx, b, index = True)
        return f'(({left}) << ({shift})) & {mask_expr(w)}'
    if op == '>>':
        # arithmetic when the left operand is signed
        return (f'((uint64_t)({c_signed(left, a)} >> '
                f'({c_expr(ctx, b, index = True)}))) & {mask_expr(w)}')
    return f'(({left}) {op} ({right})) & {mask_expr(w)}'


def is_array_element (ctx, e):
    """ir uses op 'bit' for both a bit of a vector and an element of a
    signal array reached by a variable index. They are the same syntax
    in SystemVerilog and VHDL, so only C tells them apart: one is a
    shift and mask, the other is a real array subscript. Elements wider
    than one bit are already distinguishable by width; a signals(N, 1)
    array is not, which is what this answers."""
    if getattr(e, 'op', None) != 'bit' or not e.args:
        return False
    base = e.args[0]
    return (getattr(base, 'op', None) == 'ref'
            and base.value in getattr(ctx, 'arrays', ()))


def c_assign (ctx, s):
    t = s.target
    val = f'({c_expr(ctx, s.value)}) & {mask_expr(t.width)}'
    if t.op == 'bit' and t.width == 1 and not is_array_element(ctx, t):
        base = t.args[0]
        idx = c_index(ctx, t.args[1])
        dst = c_expr(ctx, base, lhs = True)
        return (f'{dst} = ({dst} & ~(1ULL << ({idx}))) | '
                f'(({val} & 1ULL) << ({idx}));')
    if t.op == 'slice':
        lo = t.value[1]
        w = t.width
        dst = c_expr(ctx, t.args[0], lhs = True)
        m = mask_expr(w)
        return (f'{dst} = ({dst} & ~(({m}) << {lo})) | '
                f'(({val} & ({m})) << {lo});')
    if t.op == 'field':
        lo = int(t.args[1].value) if len(t.args) > 1 else 0
        w = t.width
        dst = c_expr(ctx, t.args[0], lhs = True)
        m = mask_expr(w)
        return (f'{dst} = ({dst} & ~(({m}) << {lo})) | '
                f'(({val} & ({m})) << {lo});')
    tgt = c_expr(ctx, t, lhs = True)
    return f'{tgt} = {val};'


def emit_c99_vcd (modules):
    """Separate compilation unit: VCD and ndjson dump of live fields."""
    if not modules:
        return ''
    top = modules[-1].name
    lines = [
        f'#include "{top}.h"',
        '#include "iso_vcd.h"',
        '#include "iso_log.h"',
        '#include <stdio.h>',
        '',
    ]
    for m in modules:
        lines.append(f'static void {m.name}_vcd_scope(IsoVcd *v);')
        lines.append(f'static void {m.name}_vcd_dump_inner('
                     f'IsoVcd *v, const {m.name} *s, int *idp);')
        lines.append(f'static void {m.name}_log_inner('
                     f'IsoLog *l, const {m.name} *s, const char *pfx);')
    lines.append('')
    for m in modules:
        lines += vcd_scope_lines(m)
        lines += vcd_dump_lines(m)
        lines += log_inner_lines(m)
        lines.append(f'void {m.name}_vcd_defs(IsoVcd *v)')
        lines.append('{')
        lines.append(f'    iso_vcd_push_scope(v, "{m.name}");')
        lines.append(f'    {m.name}_vcd_scope(v);')
        lines.append('    iso_vcd_pop_scope(v);')
        lines.append('}')
        lines.append('')
        lines.append(f'void {m.name}_vcd_dump(IsoVcd *v, const {m.name} *s)')
        lines.append('{')
        lines.append('    int id = 0;')
        lines.append(f'    {m.name}_vcd_dump_inner(v, s, &id);')
        lines.append('}')
        lines.append('')
        lines.append(f'void {m.name}_log_dump(IsoLog *l, const {m.name} *s, '
                     f'const char *prefix)')
        lines.append('{')
        lines.append(f'    {m.name}_log_inner(l, s, prefix ? prefix : "");')
        lines.append('}')
        lines.append('')
    return '\n'.join(lines)


def vcd_scope_lines (m):
    lines = [f'static void {m.name}_vcd_scope(IsoVcd *v)']
    lines.append('{')
    for p in m.ports:
        lines += vcd_wire_field(p.name, p.width, p.array)
    for s in m.signals:
        lines += vcd_wire_field(s.name, s.width, s.array)
    for inst in m.instances:
        lines.append(f'    iso_vcd_push_scope(v, "{inst.name}");')
        lines.append(f'    {inst.module}_vcd_scope(v);')
        lines.append('    iso_vcd_pop_scope(v);')
    lines.append('}')
    lines.append('')
    return lines


def vcd_wire_field (name, width, array):
    if array:
        out = []
        for i in range(array):
            out.append(f'    iso_vcd_wire(v, "{name}[{i}]", {width});')
        return out
    return [f'    iso_vcd_wire(v, "{name}", {width});']


def vcd_dump_lines (m):
    lines = [f'static void {m.name}_vcd_dump_inner('
             f'IsoVcd *v, const {m.name} *s, int *idp)']
    lines.append('{')
    for p in m.ports:
        lines += vcd_dump_field(p.name, p.width, p.array)
    for s in m.signals:
        lines += vcd_dump_field(s.name, s.width, s.array)
    for inst in m.instances:
        lines.append(f'    {inst.module}_vcd_dump_inner('
                     f'v, &s->{inst.name}, idp);')
    lines.append('}')
    lines.append('')
    return lines


def vcd_dump_field (name, width, array):
    cid = c_id(name)
    if array:
        out = []
        for i in range(array):
            out.append(f'    iso_vcd_change(v, (*idp)++, '
                       f's->{cid}[{i}], {width});')
        return out
    return [f'    iso_vcd_change(v, (*idp)++, s->{cid}, {width});']


def log_inner_lines (m):
    lines = [f'static void {m.name}_log_inner('
             f'IsoLog *l, const {m.name} *s, const char *pfx)']
    lines.append('{')
    lines.append('    char name[256];')
    for p in m.ports:
        lines += log_field(p.name, p.array)
    for s in m.signals:
        lines += log_field(s.name, s.array)
    for inst in m.instances:
        lines.append(f'    snprintf(name, sizeof name, "%s%s{inst.name}", '
                     f'pfx, pfx[0] ? "." : "");')
        lines.append(f'    {inst.module}_log_inner(l, &s->{inst.name}, name);')
    lines.append('}')
    lines.append('')
    return lines


def log_field (name, array):
    cid = c_id(name)
    if array:
        out = ['    {', '        int i;',
               f'        for (i = 0; i < {array}; i++) {{',
               f'            snprintf(name, sizeof name, '
               f'"%s%s{name}[%d]", pfx, pfx[0] ? "." : "", i);',
               f'            iso_log_field(l, name, s->{cid}[i]);',
               '        }', '    }']
        return out
    return [
        f'    snprintf(name, sizeof name, "%s%s{name}", '
        f'pfx, pfx[0] ? "." : "");',
        f'    iso_log_field(l, name, s->{cid});',
    ]
