"""SystemVerilog emitter: IR modules to IEEE 1800-2017 synthesizable SV.

The output keeps the input's structure: one module per IR module (leaves
first), named processes, named functions, named instances, comments on
the same items. Section 4 of SPEC.txt is the reference."""
import ast
import re
import textwrap
import dataclasses
import os
import sys
import subprocess

from . import ir
from .analyse import ConversionError, root_name
from .signal import Hexed
from .widths import (checked_expression, constant_expression,
                     render_expression, width_expression)


def min_width (value):
    if value >= 0:
        return max(1, value.bit_length())
    return (-value - 1).bit_length() + 1


BAR_SV = '//' + '-' * 77

# SystemVerilog wraps at 100, which is what the lowRISC guide asks
# for and what OpenTitan and everything derived from it uses. 79 is
# the Python convention, kept for the Python; no HDL guide asks for it
HOUSE_LIMIT = 100


def fit_comment (line, indent, marker):
    """One emitted comment line, kept inside the 79-column house limit.

    The Python marker is one character and the HDL's is two, so a
    comment written right up to column 79 lands one past it. A section
    bar is redrawn to end exactly at 79; any other line is wrapped at a
    word and continues at the same indent. Neither a bar that overhangs
    nor one that stops short is what was written."""
    if len(line) <= HOUSE_LIMIT:
        return [line]
    pad = ' ' * indent
    body = line.strip()
    text = body[len(marker):]
    if text and (set(text) <= set('-')):
        return [pad + marker + '-' * (HOUSE_LIMIT - indent - len(marker))]
    width = max(20, HOUSE_LIMIT - indent - len(marker) - 1)
    pieces = textwrap.wrap(text.strip(), width) or ['']
    return [f'{pad}{marker} {piece}' for piece in pieces]



SV_INT_MAX = 2 ** 31 - 1


def hexed_width (value):
    """The width a hexed() value said it was, or the word it fills."""
    return value.bits or max(32, ((min_width(value) + 3) // 4) * 4)


def sv_hexed (value):
    """A hexed() value as a sized hex literal, the width it declared.

    Its VHDL twin is a std_logic_vector generic, and the two have to
    read as the same word: x"41424344" there and 32'h41424344 here."""
    width = hexed_width(value)
    return f"{width}'h{int(value) & ((1 << width) - 1):0{(width + 3) // 4}X}"


def sv_param (value):
    """A parameter value as SystemVerilog.

    Vendor IP is configured with strings as often as with numbers -
    OPERATION_MODE = "NORMAL" - so a string goes in quoted and
    anything else through the number rules."""
    if isinstance(value, str):
        return '"' + value.replace('"', '') + '"'
    if isinstance(value, bool):
        return "1'b1" if value else "1'b0"
    if isinstance(value, Hexed):
        return sv_hexed(value)
    return sv_number(value)


SV_BASE = {'hex': 'h', 'bin': 'b', 'oct': 'o'}


def sv_base_number (written, value):
    """A constant in the base its author wrote it in, or None.

    Unsized, for the reason sv_number gives: a localparam that lands
    in an index has to stay integer arithmetic. Only the spelling
    changes, and the value is checked against the digits so a source
    line nobody can read back cannot rename a number.
    """
    if not written:
        return None
    base, digits = written
    letter = SV_BASE.get(base)
    if not letter or not digits:
        return None
    try:
        if int(digits, {'hex': 16, 'bin': 2, 'oct': 8}[base]) != int(value):
            return None
    except ValueError:
        return None
    return f"'{letter}{digits}"


def sv_number (value):
    """A parameter or localparam value.

    Unsized, so WIDTH-1 in an index stays integer arithmetic. A value
    past the end of a signed 32-bit integer cannot be written that
    way: Verilog reads a bare 3735928559 as a negative number, and a
    comparison against it would then go signed. That one is sized and
    written in hex, which is how a word of that size is read anyway."""
    value = int(value)
    if -SV_INT_MAX - 1 <= value <= SV_INT_MAX:
        return str(value)
    width = max(32, min_width(value if value >= 0 else -value))
    width = ((width + 3) // 4) * 4
    return f"{width}'h{value & ((1 << width) - 1):0{width // 4}X}"


PARAM_DEFAULT_SV = re.compile(r'^(\s*parameter \w+) = .*$', re.M)
# a generic's default, of any type. A constant reads `constant NAME :
# type := value`, so the second word before the colon keeps this off
# it: what a constant is set to is part of the body, not an override
PARAM_DEFAULT_VHDL = re.compile(r'^(\s+\w+ : .*?) := .*$', re.M)


def body_key (m, wide = None):
    """What the module is, with its name and parameter values taken out.

    Two builds of one block that agree on this are the same module. A
    parameter isomorph folded into the body - a width, a counter limit,
    a rate turned into a number of clocks - changes it and they differ.
    One that survived as a real parameter does not, and the difference
    belongs at the instance.

    The comparison is the emitted text and not the IR, because that is
    the question being asked. A width that follows a generic is the
    same text at every width while the IR still holds the number it
    elaborated with, so comparing the IR would never merge anything
    that PARAMETERS.md stage 2 made mergeable.

    Both languages have to agree. VHDL still folds the operand
    widening inside arithmetic where SystemVerilog sizes the result
    and lets the operands be self-determined, so a pair whose
    SystemVerilog matches can still be two different entities.
    Merging on the SystemVerilog alone would emit one entity that is
    right at one width and wrong at the other, and would leave the
    two languages with different module lists besides.
    """
    anonymous = dataclasses.replace(m, name = 'iso_body')
    text = PARAM_DEFAULT_SV.sub(r'\1', emit_module(anonymous))
    if not m.blackbox:
        # by name, because the package rebinds emit_vhdl to the
        # function of that name
        from .emit_vhdl import emit_unit
        # which generics have to be a vector is decided across every
        # build of the block, not per build. Deciding it per build
        # gave one of them an integer generic and the other a vector,
        # so their texts never matched and they never merged - and
        # after merging the surviving entity has to carry every value
        # any of them holds anyway
        unit = emit_unit(anonymous, {'iso_body': anonymous}, 'iso_body',
                         {'iso_body': wide or {}})
        text += '\n' + PARAM_DEFAULT_VHDL.sub(r'\1', unit)
    return text


# One block emitted as two modules means some value in the body froze
# at this build's parameters instead of following the generic. That is
# a bug in the translator every time, so it says so rather than
# quietly writing <block>_<PARAM>_<value>.sv
_SPLITS = set()


def clear_splits ():
    _SPLITS.clear()


def report_split (block, group, bodies):
    """Say which line stopped two builds of one block being one."""
    key = (block, tuple(m.name for m in group))
    if key in _SPLITS:
        return
    _SPLITS.add(key)
    names = ', '.join(m.name for m in group)
    detail = ''
    first = bodies[group[0].name].splitlines()
    second = bodies[group[1].name].splitlines()
    for a, b in zip(first, second):
        if a != b:
            detail = (f'; they first differ at {a.strip()!r} '
                      f'against {b.strip()!r}')
            break
    sys.stderr.write(
        f'warning: {block} is emitted as {len(group)} modules '
        f'({names}) because their bodies are not the same text{detail}. '
        'A parameter of the block reached the body as its value rather '
        'than its name.\n')


def merge_builds (modules):
    """One module per distinct body, and what each build must override.

    Returns the modules to emit and a map from the name a build had to
    the name it is emitted under together with the parameter values
    that build needs. A block instantiated twice with a different
    VERSION was two modules with two names for a body that is the same
    text either way; it is one module and one override now.
    """
    from .emit_vhdl import wide_params
    from .widths import mark_varying_widths

    # before the bodies are compared: a width that varies between
    # builds keeps its parameter in the type, which is what makes
    # those bodies the same text and merges them
    mark_varying_widths(modules)
    by_name = wide_params(modules)
    wide = {}
    for m in modules:
        slot = wide.setdefault(m.block, {})
        for name, width in by_name.get(m.name, {}).items():
            slot[name] = max(slot.get(name, 0), width)
    first = {}
    keep = []
    rename = {}
    bodies = {}
    for m in modules:
        bodies[m.name] = body_key(m, wide.get(m.block, {}))
        key = (m.block, bodies[m.name])
        chosen = first.get(key)
        if chosen is None:
            first[key] = m
            keep.append(m)
            rename[m.name] = (m.name, {})
            continue
        override = {name: value for name, value in m.parameters.items()
                    if chosen.parameters.get(name) != value}
        rename[m.name] = (chosen.name, override)

    # merging can leave one build of a block where there were several,
    # and then it is called what it is. The elaborator could not know:
    # it does not have the bodies to compare
    survivors = {}
    for m in keep:
        survivors.setdefault(m.block, []).append(m)
    for block, group in survivors.items():
        if len(group) > 1:
            report_split(block, group, bodies)
    plain = {}
    for block, group in survivors.items():
        if len(group) == 1 and group[0].name != block:
            plain[group[0].name] = block
    if plain:
        keep = [dataclasses.replace(m, name = plain.get(m.name, m.name))
                for m in keep]
        rename = {was: (plain.get(now, now), over)
                  for was, (now, over) in rename.items()}
    return keep, rename


def emit_sv (modules):
    """SystemVerilog text for the module list, leaves first.

    One file holds every module in the design (SPEC 7), so a bar to
    column 79 goes between them: with each module carrying its own
    header comment there is otherwise nothing to say where one ends
    and the next begins."""
    modules, rename = merge_builds(modules)
    modules = [retarget(m, rename) for m in modules]
    chunks = []
    preamble = struct_typedefs(modules)
    if preamble:
        chunks.append(preamble)
    chunks += [BAR_SV + '\n' + emit_module(m) for m in modules]
    text = '\n\n'.join(chunks)
    return text + ('\n' if not text.endswith('\n') else '')


def retarget (m, rename):
    """Point every instance at the module its build was merged into."""
    if not any(i.module in rename and rename[i.module][0] != i.module
               or rename.get(i.module, ('', {}))[1]
               for i in m.instances):
        return m
    out = []
    for inst in m.instances:
        name, override = rename.get(inst.module, (inst.module, {}))
        out.append(dataclasses.replace(inst, module = name,
                                       params = dict(override)))
    return dataclasses.replace(m, instances = out)


def sv_files (modules):
    """One file per module, in dependency order, leaves first.

    What a person writing SystemVerilog by hand would produce, and what
    a vendor tool wants to be handed: a file per module and a list of
    them. Anything the modules share - struct typedefs - goes in a
    header each of them includes.

    Returns [(filename, text)], the header first if there is one.
    """
    modules, rename = merge_builds(modules)
    modules = [retarget(m, rename) for m in modules]
    top = modules[-1].name
    out = []
    shared = struct_typedefs(modules)
    header = f'{top}_types.svh' if shared else None
    if header:
        guard = header.replace('.', '_').upper()
        out.append((header, f'`ifndef {guard}\n`define {guard}\n\n'
                    + shared + f'\n`endif\n'))
    for m in modules:
        text = emit_module(m)
        if header and not m.blackbox:
            text = f'`include "{header}"\n\n' + text
        out.append((m.name + '.sv', text + '\n'))
    return out


def write_sv_files (modules, directory, listing = True):
    """Write one file per module into `directory`, and a .f list of
    them in the order a tool should read them."""
    os.makedirs(directory, exist_ok = True)
    written = []
    for name, text in sv_files(modules):
        path = os.path.join(directory, name)
        with open(path, 'w', encoding = 'ascii', newline = '\n') as f:
            f.write(text)
        written.append(path)
    if listing:
        top = os.path.basename(directory)
        # a blackbox stub is listed on its own. A linter wants it, so
        # that it has ports to check the instance against; the fitter
        # must not have it, because it is given the vendor's real one
        # and two modules of a name is an error there
        stubs = {m.name + '.sv' for m in modules if m.blackbox}
        listing_path = os.path.join(directory, top + '.f')
        with open(listing_path, 'w', encoding = 'ascii',
                  newline = '\n') as f:
            for path in written:
                if os.path.basename(path) not in stubs:
                    f.write(os.path.basename(path) + '\n')
        written.append(listing_path)
        if stubs:
            stub_path = os.path.join(directory, top + '_blackbox.f')
            with open(stub_path, 'w', encoding = 'ascii',
                      newline = '\n') as f:
                f.write('// stubs for lint only. Give the fitter the\n'
                        "// vendor's own file instead of these.\n")
                for name in sorted(stubs):
                    f.write(name + '\n')
            written.append(stub_path)
    return written


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
    top name matches the filename.

    -Wno-UNUSEDSIGNAL and -Wno-PINCONNECTEMPTY: isomorph makes both of
    those checks itself, in check_unused, and says so as a warning
    naming the port or the signal. Asking Verilator for them as well
    bought nothing and cost a pair of lint_off comments around every
    declaration that had been thought about, which is not what anyone
    wants to read. The emitted SystemVerilog carries no pragmas at all
    now; whatever a vendor tool has to say about an unused signal it
    can say without isomorph having pre-empted it.
    """
    cmd = ['verilator', '--lint-only', '-Wall', '--sv', '--assert',
           '-Wno-DECLFILENAME', '-Wno-UNUSEDSIGNAL',
           '-Wno-PINCONNECTEMPTY']
    if top:
        cmd += ['--top-module', top]
    cmd += [path] if isinstance(path, str) else list(path)
    try:
        result = subprocess.run(cmd, capture_output = True, text = True)
    except FileNotFoundError:
        raise ConversionError('verilator not found on PATH')
    if result.returncode != 0:
        message = (result.stderr or result.stdout
                   or 'verilator failed').rstrip()
        raise ConversionError(f'verilator lint failed for {path}:\n{message}')
    return result.stderr



def break_points (line):
    """Every index a newline may be put at.

    Both languages treat a newline as whitespace between two lexical
    elements, so a break is legal before any token and after any open
    bracket or comma. Inside a double-quoted string it is not."""
    out = []
    quoted = False
    for index, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if (ch == ' ') or (ch in '(,'):
            out.append(index + 1)
    return out


def fold_line (line, indent, limit = None):
    """One long line as several, broken only where a newline is
    whitespace to both languages.

    The generated arithmetic of a wide VHDL expression is a nest of
    resize() calls with no whitespace to speak of, so breaking at a
    space alone is not enough and a break after an open bracket or a
    comma is allowed too. If no break inside the limit exists the line
    is emitted long, which is better than one broken in the wrong
    place."""
    limit = HOUSE_LIMIT if limit is None else limit
    pad = ' ' * indent
    out = []
    rest = line
    while len(rest) > limit:
        lead = len(rest) - len(rest.lstrip())
        points = [p for p in break_points(rest) if lead < p <= limit]
        if not points:
            break
        head = rest[:points[-1]].rstrip()
        if not head.strip():
            break
        out.append(head)
        rest = pad + rest[points[-1]:].lstrip()
        # each pass removes at least one character, so this cannot
        # run away; the cap is only there in case it ever could. A
        # memory image is thousands of values and folds into hundreds
        # of lines, which is fine and is what an initialiser looks
        # like.
        if len(out) > 20000:
            break
    out.append(rest)
    return out


def assignment_split (line):
    """Index just past the assignment arrow of a statement, or None.

    Only a real assignment: the text before the arrow, with anything
    bracketed removed, has to be a single name. That keeps a comparison
    inside a condition from being mistaken for one. A VHDL attribute
    specification or type declaration breaks after its `is` instead,
    which is the only place either of them can."""
    stripped = line.lstrip()
    if stripped.startswith('attribute ') or stripped.startswith('type '):
        cut = line.find(' is ')
        return (cut + 4) if (cut >= 0) else None
    depth = 0
    index = 0
    while index < len(line):
        ch = line[index]
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth = max(0, depth - 1)
        elif depth == 0:
            for arrow in (' <= ', ' := ', ' = '):
                if line.startswith(arrow, index):
                    if is_target(line[:index]):
                        return index + len(arrow)
                    return None
        index += 1
    return None


NOT_TARGETS = frozenset((
    'if', 'elsif', 'elif', 'else', 'while', 'for', 'case', 'when',
    'with', 'return', 'assert', 'begin', 'end', 'wait', 'until',
    'report', 'severity'))


def is_target (text):
    """Does this read as the left hand side of an assignment?

    A bracketed condition collapses to its keyword once the brackets
    are removed, so `if ((a) = (b))` looked like an assignment to a
    signal called `if`. A keyword is never a target."""
    out = []
    depth = 0
    for ch in text:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    name = ''.join(out).strip()
    return bool(name) and (name[0].isalpha() or name[0] == '_') \
        and (' ' not in name) and (name.lower() not in NOT_TARGETS)


def wrap_long_lines (lines, marker, step):
    """Break an over-long statement once, after its assignment arrow.

    The 79-column house limit is about what isomorph emits, not only
    about what was written, and a wide name with a concatenation on the
    right of it passes 79 easily. If the expression will not fit on a
    line of its own either, the line is left as it is: a break in the
    wrong place reads worse than a long line."""
    out = []
    for line in lines:
        if (len(line) <= HOUSE_LIMIT) or (marker in line):
            out.append(line)
            continue
        lead = len(line) - len(line.lstrip())
        cut = assignment_split(line)
        if cut is None:
            out += fold_line(line, lead + step)
            continue
        indent = ' ' * (lead + step)
        tail = indent + line[cut:].strip()
        head = line[:cut].rstrip()
        if len(head) <= HOUSE_LIMIT:
            out.append(head)
        else:
            out += fold_line(head, lead + step)
        if len(tail) <= HOUSE_LIMIT:
            out.append(tail)
        else:
            out += fold_line(tail, lead + step + step)
    return out

def blackbox_lines (m):
    """A stub so a linter has something to read.

    Ports and no body, marked black_box, which is the attribute
    Quartus and Synplify both know. It is written to its own file and
    listed separately from the design, because the fitter is given the
    vendor's real one instead and two modules of a name is an error.
    """
    lines = []
    if m.blackbox_source:
        lines += [as_comment(line)
                  for line in fit_comment(m.blackbox_source, 0, '//')]
    lines.append('// A stub. The fitter is given the real one; this is')
    lines.append('// here so that a lint has ports to check against.')
    lines.append('//')
    lines.append('// The lint_off pair is the only pragma isomorph')
    lines.append('// emits, and it is in this file rather than in a')
    lines.append('// design: a module with no body has every output')
    lines.append('// undriven and every input unused, which is what a')
    lines.append('// stub is, and saying so here is better than')
    lines.append('// driving them to nothing and calling it a model.')
    lines.append('/* verilator lint_off UNDRIVEN */')
    lines.append('/* verilator lint_off UNUSEDSIGNAL */')
    lines.append('/* verilator lint_off UNUSEDPARAM */')
    lines.append('(* black_box *)')
    if m.parameters:
        lines.append(f'module {m.name} #(')
        kept = list(m.parameters.items())
        for index, (name, value) in enumerate(kept):
            comma = ',' if index < len(kept) - 1 else ''
            lines.append(f'    parameter {name} = {sv_param(value)}{comma}')
        lines.append(') (')
    else:
        lines.append(f'module {m.name} (')
    lines += port_lines(m)
    lines.append(');')
    lines.append('endmodule')
    lines.append('/* verilator lint_on UNUSEDPARAM */')
    lines.append('/* verilator lint_on UNUSEDSIGNAL */')
    lines.append('/* verilator lint_on UNDRIVEN */')
    return '\n'.join(wrap_long_lines(lines, '//', 4))


def emit_module (m):
    if m.blackbox:
        return blackbox_lines(m)
    lines = []
    lines += header_lines(m)
    ports = port_lines(m) if m.ports else []
    rest = (enum_lines(m) + signal_lines(m) + genvar_lines(m)
            + function_lines(m) + item_lines(m))
    # a localparam that emits its own expression may be the only place
    # a parameter is named, so the parameter list is decided after
    locals_ = localparam_lines(m, rest)
    params = parameter_lines(m, rest + ports + locals_)
    if params:
        lines.append(f'module {m.name} #(')
        lines += params
        lines.append(') (')
    else:
        lines.append(f'module {m.name} (')
    lines += ports
    lines.append(');')
    lines.append('')
    body = []
    body += locals_
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
    return '\n'.join(wrap_long_lines(lines, '//', 4))


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
        if line.strip():
            check_comment(line)
            lines += fit_comment('// ' + line, 0, '//')
        else:
            lines.append('//')
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


# A comment beginning with one of these is not a comment to a Verilog
# tool, it is a directive. Verilator reads `// Verilator harness ...`
# as a pragma and fails the lint on it, whatever the case and whatever
# the spacing; synopsys, synthesis and pragma are the same trick in
# other tools, and translate_off is the one that silently deletes the
# lines after it.
COMMENT_PRAGMAS = ('verilator', 'synopsys', 'synthesis', 'pragma')


def check_comment (text):
    """Refuse a comment a tool would read as an instruction.

    Comments travel into the HDL, which is the point of them here, and
    that makes the first word of one load-bearing. This project says a
    Python name that is a reserved word in an output language is an
    error rather than a silent rename (SPEC 4.5); the same applies to
    a comment that is a directive in an output language, and for the
    same reason: the alternative is rewriting what the author wrote.
    """
    body = text.lstrip('#').strip().lower()
    for word in COMMENT_PRAGMAS:
        if body.startswith(word):
            raise ConversionError(
                f'this comment starts with {word!r}, so a Verilog tool '
                'reads it as a directive rather than as a comment, and '
                f'verilator --lint-only fails on it:\n  {text.strip()}\n'
                'Comments are emitted as they were written, so reword '
                f'it to put {word!r} anywhere but first.')


def as_comment (text):
    check_comment(text)
    if text.startswith('#'):
        return '//' + text[1:]
    return '// ' + text


def comment_lines (comments, indent):
    pad = ' ' * indent
    out = []
    for c in (comments or []):
        out += fit_comment(pad + as_comment(c), indent, '//')
    return out


def with_trailing (line, trailing):
    if trailing:
        return line + '  ' + as_comment(trailing)
    return line


def trailing_lines (line, trailing, indent):
    """A declaration and the comment written after it on the same line.

    Beside it when the two fit inside the house limit, on its own line
    above when they do not. Dropping the comment and letting the line
    run past column 79 are both worse."""
    if not trailing:
        return [line]
    joined = with_trailing(line, trailing)
    if len(joined) <= HOUSE_LIMIT:
        return [joined]
    return comment_lines([trailing], indent) + [line]


def width_parameter (params, width, locals = None):
    """The name of the parameter this width came from, or None.

    A width is a plain int by the time it reaches the emitters, so the
    only link back to a parameter is its value. Matching on value alone
    renamed any width that happened to equal any parameter: a block with
    AVMM_TIMEOUT = 16 and a 16-bit bus declared its ports as
    [AVMM_TIMEOUT-1:0], and in VHDL that is a generic which would resize
    them. So the value must match a parameter that is named as a width,
    and it must be the only one, or the width is emitted as a literal.

    `locals` are the block's localparams. One can never be the answer,
    because a port width cannot name something declared in the body,
    but if one of them has the same value there is no way to tell which
    was meant: an SPI master has SPI_WIDTH = 8 and WIDTHT = 8, and i_speed
    is the second, not the first. So a matching localparam makes the
    width ambiguous and it goes out as a literal.

    A missed substitution costs readability; a wrong one costs silence."""
    if not params or width <= 1:
        return None

    def named (source):
        return [name for name, value in (source or {}).items()
                if isinstance(value, int) and not isinstance(value, bool)
                and value == width and 'WIDTH' in name.upper()]

    found = named(params)
    if len(found) != 1:
        return None
    if named(locals):
        return None
    return found[0]


def param_hi (params, width, locals = None):
    """Keep WIDTH-1 in the HDL when the width really is that parameter."""
    name = width_parameter(params, width, locals)
    return f'{name}-1' if name else None


def packed_type (width, kind = 'vector', typ = None, params = None,
                 locals = None, width_expr = None, scope = None,
                 varying = False):
    if kind == 'enum' and typ is not None:
        return typ.name
    if kind == 'struct' and typ is not None:
        return typ.name
    # one bit is a scalar, unless this width is a parameter that really
    # varies between builds of the block: then it keeps [WIDTH-1:0] so
    # every build is the same text and they merge into one module
    if width == 1 and not varying:
        return 'logic'
    # what the author wrote first, then the width matched back to a
    # parameter by its value, then the number itself
    hi = width_expression(width_expr, width,
                          params if scope is None else scope)
    if hi is None:
        hi = param_hi(params, width, locals)
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
    # a port list is above the localparams and may not name one
    types = [packed_type(p.width, p.kind, p.type, m.parameters,
                         m.constants, p.width_expr, m.parameters,
                         p.varying_width)
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
        out += attribute_lines(p.attributes, 4)
        line = '    ' + body[i].rstrip() + comma
        if p.trailing:
            pad = ' ' * max(1, wb + 5 + 1 - len(line))
            aligned = line + pad + as_comment(p.trailing)
            if len(aligned) <= HOUSE_LIMIT:
                out.append(aligned)
            else:
                # it will not fit beside the port, so it goes above it
                out += comment_lines([p.trailing], 4)
                out.append(line)
        else:
            out.append(line)
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
    """Parameters as an ANSI parameter port list, ahead of the ports.

    A port width may name one, and a name must be declared before it is
    used, so `module m #(parameter WIDTH = 8) (input logic [WIDTH-1:0]
    i_data)` is the only legal order. Declaring them in the body after
    the port list is what MyHDL emitted; Verilator accepts it, Quartus
    and Vivado do not."""
    kept = []
    for name, value in m.parameters.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if body is not None and not used_in(name, body):
                continue
            kept.append((name, value))
    lines = []
    for index, (name, value) in enumerate(kept):
        comma = ',' if index < len(kept) - 1 else ''
        before, after = m.parameter_comments.get(name, ([], None))
        lines += comment_lines(before, 4)
        if isinstance(value, Hexed):
            width = hexed_width(value)
            kind = 'logic' if width == 1 else f'logic [{width - 1}:0]'
            text = f'    parameter {kind} {name} = {sv_hexed(value)}{comma}'
        else:
            spelt = sv_base_number(m.parameter_bases.get(name), value)
            text = (f'    parameter {name} = '
                    f'{spelt or sv_number(value)}{comma}')
        lines += trailing_lines(text, after, 4)
    return lines


def localparam_lines (m, body = None):
    """Constants, each as the author wrote it where that can be read.

    A constant may name a parameter or a constant declared above it,
    so the scope grows as the list is walked and a constant that was
    pruned never enters it: naming one that is not there emits a
    module that does not compile.
    """
    lines = []
    scope = dict(m.parameters)
    for name, value in m.constants.items():
        if body is not None and not used_in(name, body):
            continue
        text = constant_expression(m.constant_exprs.get(name), value, scope)
        if not text:
            text = sv_base_number(m.constant_bases.get(name), value)
        lines.append(f'    localparam {name} = '
                     f'{text if text else sv_number(value)};')
        scope[name] = value
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


def attribute_lines (attributes, indent):
    """(* ... *) broken across lines when it will not fit on one.

    No two vendors spell the same attribute the same way, so a signal
    that has to survive all of them carries five of them, and that is
    longer than a line."""
    text = attribute_text(attributes)
    if not text:
        return []
    pad = ' ' * indent
    if len(pad + text) <= HOUSE_LIMIT:
        return [pad + text]
    parts = [p.strip() for p in text[3:-3].split(', ')]
    lines = []
    current = pad + '(* '
    for index, part in enumerate(parts):
        piece = part + (' *)' if index == len(parts) - 1 else ',')
        if len(current + piece) > HOUSE_LIMIT:
            lines.append(current.rstrip())
            current = pad + '   '
        current = current + piece + ' '
    lines.append(current.rstrip())
    return lines


def signal_lines (m):
    lines = []
    waived = async_reset_nets(m)
    for s in m.signals:
        lines += comment_lines(s.comments, 4)
        packed = packed_type(s.width, s.kind, s.type, m.parameters,
                             m.constants, s.width_expr,
                             {**m.parameters, **m.constants},
                             s.varying_width)
        name = s.name
        if s.array:
            name = f'{name} [{array_size(s, m)}]'
        lines += attribute_lines(s.attributes, 4)
        tail = array_init_sv(s) if s.array and s.init else ''
        waive = s.name in waived
        if waive:
            lines.append('    // asserted with no clock and released '
                         'with one, which is')
            lines.append('    // what a reset synchroniser is')
            lines.append('    /* verilator lint_off SYNCASYNCNET */')
        lines += trailing_lines(f'    {packed} {name}{tail};',
                                s.trailing, 4)
        if waive:
            lines.append('    /* verilator lint_on SYNCASYNCNET */')
    if lines:
        lines.append('')
    return lines


def array_init_sv (s):
    """The contents of a memory, as a declaration initialiser.

    An unpacked array takes an assignment pattern, and that is what
    every FPGA tool reads to load a block RAM at configuration time. It
    is not an initial block: SPEC 5.16 bans those for reset values, and
    this is not a reset value, it is what the device is programmed
    with."""
    if (len(set(s.init)) == 1 and len(s.init) > 1):
        # a thousand copies of the same word says it once
        return (" = '{default: "
                + table_word_sv(s.init[0], s.width, s.init_base) + '}')
    items = ', '.join(table_word_sv(v, s.width, s.init_base)
                      for v in s.init)
    return " = '{" + items + '}'


def table_word_sv (value, width, base = None):
    """One word of a memory image, in the base preload() was given.

    A program is read in hex and a sine table in decimal, and the
    author is the one who knows which. The VHDL says the same word
    the same way.
    """
    value = int(value)
    if value < 0:
        return sv_const(value, width)
    if base == 'bin':
        return f"{width}'b{value:0{width}b}"
    if base == 'hex':
        return f"{width}'h{value:0{-(-width // 4)}X}"
    return f"{width}'d{value}"


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


def array_groups (m):
    """{array name: members in index order} for the instance arrays."""
    groups = {}
    for inst in m.instances:
        if inst.array:
            groups.setdefault(inst.array, []).append(inst)
    for members in groups.values():
        members.sort(key = lambda i: i.index)
    return groups


def item_lines (m):
    items = []
    groups = array_groups(m)
    done = set()
    for inst in m.instances:
        if inst.array:
            if inst.array in done:
                continue
            done.add(inst.array)
            members = groups[inst.array]
            items.append((members[0].line, 0, inst.array, 'array',
                          members[0]))
            continue
        items.append((inst.line, 0, inst.name, 'instance', inst))
    for a in m.assigns:
        items.append((a.line, 1, '', 'assign', a))
    for p in m.processes:
        items.append((p.line, 2, p.name, 'process', p))
    # by where it was written, and no further: two assigns on one
    # source line tie on every part of the key, and sorting would then
    # reach for the items themselves, which do not compare
    items.sort(key = lambda item: item[:4])
    lines = []
    for _, _, _, kind, item in items:
        if kind == 'instance':
            lines += guarded_lines(instance_lines(item), item)
        elif kind == 'array':
            lines += generate_lines(item)
        elif kind == 'assign':
            lines += assign_lines(item)
        else:
            lines += process_lines(item)
        lines.append('')
    return lines


def genvar_lines (m):
    """One genvar per instance array, at module level.

    Quartus Prime Standard will not parse a genvar declared inside the
    loop header, which is the compact IEEE 1800 form every other tool
    takes. Measured on 25.1std, 2026-09-12. So the declaration comes
    out here and the loop only assigns it, which is the Verilog-2001
    spelling and is accepted everywhere."""
    names = []
    for members in array_groups(m).values():
        var = members[0].var
        if var and var not in names:
            names.append(var)
    if not names:
        return []
    return [f'    genvar {name};' for name in names] + ['']


def generate_lines (head):
    """An array of instances as one labelled generate.

    The label is the name the array has in the Python, and the index
    is the one the loop counted with, so cells[2] in the fitter report
    is cells[2] in the source. The instance inside the loop body is
    called inst: both languages need an identifier there and there is
    no name in the Python to take, so it is the same word every time
    rather than a different invention each time.
    """
    lines = comment_lines(head.comments, 4)
    var = head.var or 'k'
    lines.append('    generate')
    lines.append(f'        for ({var} = 0; {var} < {head.count}; '
                 f'{var}++) begin : {head.array}')
    if head.params:
        values = ', '.join(f'.{n}({sv_number(v)})'
                           for n, v in sorted(head.params.items()))
        lines.append(f'            {head.module} #({values}) inst (')
    else:
        lines.append(f'            {head.module} inst (')
    formals = list(head.ports.items())
    for i, (formal, actual) in enumerate(formals):
        comma = ',' if i < len(formals) - 1 else ''
        kind, payload = head.shape.get(formal, ('same', actual))
        if kind == 'index':
            mapped = f'{payload}[{var}]'
        else:
            mapped = sv_expr(payload) if payload is not None else ''
        lines.append(f'                .{formal}({mapped}){comma}')
    lines.append('            );')
    lines.append('        end')
    lines.append('    endgenerate')
    return lines


def guarded_lines (body, inst):
    """An instance built inside a when(), as an if ... generate.

    The label is the instance's own name with a prefix, because both
    languages want one on a generate and the name is the only thing
    here that says which instance it belongs to."""
    if not inst.guard:
        return body
    label = f'g_{inst.name}'
    lines = ['    generate']
    lines.append(f'        if ({inst.guard}) begin : {label}')
    lines += ['        ' + line if line else line for line in body]
    lines.append('        end')
    if inst.guard_outputs:
        # the other arm holds what the instance drove, because a line
        # nothing drives is one the tools call out and the simulators
        # read as zero anyway
        lines.append(f'        else begin : {label}')
        for name, width in inst.guard_outputs:
            lines.append(f"            assign {name} = {width}'b0;")
        lines.append('        end')
    lines.append('    endgenerate')
    return lines


def instance_lines (inst):
    lines = comment_lines(inst.comments, 4)
    formals = list(inst.ports.items())
    if inst.params:
        kept = sorted(inst.params.items())
        values = ', '.join(f'.{n}({sv_param(v)})' for n, v in kept)
        head = f'    {inst.module} #({values}) {inst.name} ('
        if len(head) <= HOUSE_LIMIT:
            lines.append(head)
        else:
            # vendor IP arrives with a dozen parameters and the one
            # line form is unreadable long before it is illegal
            lines.append(f'    {inst.module} #(')
            for index, (n, v) in enumerate(kept):
                comma = ',' if index < len(kept) - 1 else ''
                lines.append(f'        .{n}({sv_param(v)}){comma}')
            lines.append(f'    ) {inst.name} (')
    else:
        lines.append(f'    {inst.module} {inst.name} (')
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
            line = f'{pad}assert ({cond_text(s.cond)})'
            if s.message:
                line += f' else $error("{s.message}");'
            else:
                line += ';'
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
    # a plain if, always. priority is an assertion about the design and
    # a direction to the synthesizer to build the chain in order, and
    # the author wrote neither. unique used to be emitted where the
    # branches were provably exclusive, and Quartus Prime Standard,
    # which fits every number in RESULTS.md, rejects `unique if` as a
    # syntax error (it takes unique before case only; 25.1std, checked
    # 2026-09-12). One emitter, one output, so neither is written.
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
    start = (bound_text(node.start_expr) if node.start_expr is not None
             else node.start)
    stop = (bound_text(node.stop_expr) if node.stop_expr is not None
            else node.stop)
    head = (f'{pad}for (int {node.var} = {start}; '
            f'{node.var} < {stop}; {node.var}++) begin')
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


def sv_const (value, width, signed = False, base = None):
    """A sized literal, in the base it was written in.

    0xDEAD_BEEF came out as 32'd3735928559, which is the same number
    and unreadable. Python's ast keeps the source text of a literal,
    so the base costs nothing to carry and the file the fitter
    compiles says what the Python said."""
    value = int(value)
    if signed:
        # the sign goes outside the literal. 8'sd-1 is not a number to
        # Verilog, which reads the base and then finds no digits;
        # verilator says "Number is missing value digits"
        if value < 0:
            return f"-{width}'sd{-value}"
        return f"{width}'sd{value}"
    if width == 1:
        return "1'b1" if value else "1'b0"
    masked = value & ((1 << width) - 1)
    if base == 'hex':
        return f"{width}'h{masked:0{(width + 3) // 4}X}"
    if base == 'bin':
        return f"{width}'b{masked:0{width}b}"
    if base == 'oct':
        return f"{width}'o{masked:0{(width + 2) // 3}o}"
    return f"{width}'d{value}"


def sv_expr (e, index = False):
    if e is None:
        return ''
    op = e.op
    a = e.args
    if op == 'ref':
        return e.value
    if op == 'const':
        if index or e.unsized:
            return str(int(e.value))
        return sv_const(e.value, e.width, e.signed, e.base)
    if op == 'enum':
        return e.value.name
    if op == 'bit':
        idx_e = a[1]
        # Array index (width > 1) is a word select; a 1-bit node is a
        # bit select. Parameter arithmetic (WIDTH - 1) is emitted as
        # an unsized index; a signal index is width-cast for Verilator.
        if e.width != 1:
            if idx_e.op == 'const':
                # unsized: a sized literal is as wide as the value
                # needs rather than as wide as the array wants, so
                # command[1] came out as command[1'b1] and verilator
                # asked for the two bits a three-deep array indexes with
                return f'{sv_expr(a[0])}[{sv_expr(idx_e, index = True)}]'
            if idx_e.op == 'binop':
                # STAGES-1 and i-1 are elaboration arithmetic: an
                # index, not a vector, and a sized literal in one is
                # the WIDTHEXPAND verilator reports
                return f'{sv_expr(a[0])}[{bound_text(idx_e, False)}]'
            return f'{sv_expr(a[0])}[{sv_expr(idx_e)}]'
        if idx_e.op == 'const':
            idx = sv_expr(idx_e, index = True)
        elif idx_e.op in ('binop', 'ref'):
            idx = bound_text(idx_e, wrap = False)
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
                return f"{e.width}'({unwrapped(sv_expr(base))} >> {lo})"
            if len(a) >= 3 and a[2].op == 'const' and a[2].value == 0:
                size = cast_size(a[1])
                if size is not None:
                    return f"{size}'({unwrapped(sv_expr(base))})"
            return f"{e.width}'({unwrapped(sv_expr(base))})"
        if len(a) >= 3:
            hi = bound_text(a[1], wrap = False)
            lo = bound_text(a[2], wrap = False)
            return f'{sv_expr(base)}[{hi}:{lo}]'
        hi, lo = e.value[0] - 1, e.value[1]
        return f'{sv_expr(base)}[{hi}:{lo}]'
    if op == 'part':
        idx_e = a[1]
        if idx_e.op in ('binop', 'ref', 'const'):
            # a lane number is a count, not a vector
            return (f'{sv_expr(a[0])}[{bound_text(idx_e, wrap = False)} '
                    f'+: {e.value}]')
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
        times = bound_text(a[1]) if len(a) > 1 else str(e.value)
        return '{' + f'{times}{{{sv_expr(a[0])}}}' + '}'
    if op == 'binop':
        token = e.value
        if token == '>>' and getattr(a[0], 'signed', False):
            # SystemVerilog >> is logical whatever the operand, so a
            # signed right shift is >>>. VHDL's shift_right(signed(x))
            # is already arithmetic, and the two must agree.
            token = '>>>'
        if index:
            # a slice bound is written without sized literals, and a
            # nested one has to keep its brackets: (STAGES-1)*WIDTHD
            # is not STAGES-1*WIDTHD
            # a bound is written WIDTH-1, the way every range in
            # either language is written, and not WIDTH - 1
            outer = INDEX_PRECEDENCE.get(token, 0)
            return (f'{sv_index_operand(a[0], outer)}{token}'
                    f'{sv_index_operand(a[1], outer, True)}')
        if token in ('<<', '>>', '>>>'):
            # a shift amount is self-determined, so it is a count and
            # not a vector: x >> 1, the way it would be typed
            return f'({sv_expr(a[0])} {token} {sv_expr(a[1], True)})'
        text = f'({sv_expr(a[0])} {token} {sv_expr(a[1])})'
        if e.int_tree:
            # arithmetic on named constants is integer arithmetic, 32
            # bits wide whatever the numbers are. Beside a vector that
            # is what Verilator calls WIDTHEXPAND, so it is given the
            # width it is being read at, which is what the VHDL says
            # with to_unsigned(x, n)
            return f"{e.width}'{text}"
        return text
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
        # the cast follows the generic where the target's width did
        size = render_expression(e.width_expr, 'sv')
        size = f'({size})' if size else str(e.width)
        if e.signed:
            return f"{size}'($signed({inner}))"
        return f"{size}'({inner})"
    if op == 'call':
        return e.value + '(' + ', '.join(sv_expr(x) for x in a) + ')'
    if op == 'bits':
        return f"{e.width}'b{e.value}"
    return f'/* {op} */'


def cast_size (hi):
    """The width of a [hi:0] slice, as the author wrote the bound.

    A slice of an expression is emitted as a width cast, and folding
    the width there is what stops a module following its own generic:
    34'(...) is right at one width and wrong at every other, so a
    design that overrides the generic is one Verilator refuses. The
    width is one more than the top bit, and the one is folded into
    the bound rather than written beside it.

    Anything but a bare name is parenthesised, because size'(expr)
    binds tighter than arithmetic: WIDTH+2'(x) is WIDTH + (2'(x)),
    which is a different expression and a legal one.

    None when the bound is a plain number, which says no more than
    the width already does.
    """
    if hi.op == 'const':
        return None
    if (hi.op == 'binop' and hi.args[1].op == 'const'
            and hi.args[0].op == 'ref'):
        step = hi.args[1].value
        name = hi.args[0].value
        if hi.value == '-':
            if step == 1:
                return name
            return f'({name}-{step - 1})'
        if hi.value == '+':
            return f'({name}+{step + 1})'
    return f'({bound_text(hi)}+1)'


# * binds tighter than + -, and anything not named here is
# bracketed rather than guessed at
INDEX_PRECEDENCE = {'*': 2, '/': 2, '%': 2, '+': 1, '-': 1}


def sv_index_operand (e, outer = 0, right = False):
    """One side of a bound expression, bracketed only where precedence
    needs it: (STAGES-1)*WIDTHD is not STAGES-1*WIDTHD, and a bound
    nobody has to decode is the point of writing it as the author did."""
    text = sv_expr(e, index = True)
    if e.op == 'unop':
        return f'({text})'
    if e.op != 'binop':
        return text
    inner = INDEX_PRECEDENCE.get(e.value, 0)
    if (outer == 0 or inner == 0 or inner < outer
            or (right and inner == outer)):
        return f'({text})'
    return text


def async_reset_nets (m):
    """Signals this module clocks and also uses as an asynchronous
    reset, which is a reset synchroniser and nothing else.

    Verilator reports SYNCASYNCNET on a net flopped both ways, and it
    is right to: the pattern is a bug everywhere except here, where
    the whole circuit exists to assert with no clock and release with
    one. always_ff_async_reset is the deliberate construct that makes
    it, so the file it emits waives the warning on that one net
    rather than leaving a design that cannot pass its own lint.
    """
    resets = {str(p.reset).split('[')[0]
              for p in m.processes if p.reset}
    clocked = set()
    for p in m.processes:
        if p.kind == 'ff':
            clocked |= {root_name(a.target) for a in walk_assigns(p.body)}
    return resets & clocked


def walk_assigns (body):
    """Every assignment in these statements, however nested."""
    for s in body or []:
        if isinstance(s, ir.Assign):
            yield s
        elif isinstance(s, ir.If):
            for _, inner in s.branches:
                yield from walk_assigns(inner)
        elif isinstance(s, ir.Match):
            for _, inner in s.arms:
                yield from walk_assigns(inner)
        elif isinstance(s, ir.For):
            yield from walk_assigns(s.body)


def array_size (s, m):
    """How many elements, as the author wrote it."""
    scope = {**m.parameters, **m.constants}
    text = constant_expression(s.array_expr, s.array, scope)
    return text or str(s.array)


def bound_text (e, wrap = True):
    """Slice/bit bound: names and arithmetic as written, no sized
    literals.

    Inside [hi:lo] or [i] the brackets and the colon delimit it, and
    [WIDTH-1:0] is how every range in the language is written. A
    replication count has no such delimiter, so {(N-1)*W{x}} keeps
    its brackets or it is a different expression.
    """
    text = sv_expr(e, index = True)
    if wrap and e.op == 'binop':
        return f'({text})'
    return text


def cond_text (e):
    """Condition in if/case/assert: drop one layer of wrapping parens."""
    return unwrapped(sv_expr(e))


def unwrapped (text):
    """One layer of brackets off, where the whole text is inside it.

    size'(expr) brackets what it casts and every binop renders itself
    bracketed, so a cast of one came out WIDTH\'((a + b)). The pair
    that says what the cast applies to is the one that stays.
    """
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
