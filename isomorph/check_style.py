"""Check the house layout rules that a machine can check.

    python3 -m isomorph style [--fix] [path ...]

Reports every violation and exits non-zero if there is one, so it can
go in a hook or a script. --fix repairs the mechanical ones first: bar
width and trailing whitespace. Nothing else is touched, because nothing
else can be repaired without deciding something.

  - no line over 79 characters
  - a section bar ends exactly at column 79
  - no tabs, no trailing whitespace, ASCII only
  - a file ends with exactly one newline
  - no default painted on a register in a clocked process and then
    overridden

These are the layout rules a machine can settle on its own, so it
does. What a block should look like beyond them is a matter for
whoever is writing it.
"""
import ast
import os
import re
import sys

LIMIT = 79
BAR = re.compile(r'^(\s*)#(-+)\s*$')

# Directories that are not house code and are not held to the layout.
# vendor_models holds device models and the scripts that rewrite them;
# myhdl holds frozen MyHDL sources kept as a reference for a migration,
# and reformatting those would lose the thing they are kept for.
SKIP = ('__pycache__', 'vendor_models', 'myhdl', 'build', '.git')


def driven (node):
    """The name a `<something>.next = ...` target assigns, or None."""
    if not isinstance(node, ast.Attribute) or node.attr != 'next':
        return None
    return ast.unparse(node.value)


def assigned_under (nodes):
    """Every register assigned anywhere beneath these statements."""
    found = set()
    for node in nodes:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign):
                for target in inner.targets:
                    name = driven(target)
                    if name is not None:
                        found.add(name)
    return found


def clocked (func):
    return any('always_ff' in ast.unparse(d) for d in func.decorator_list)


def painted (body, process, found):
    """Registers given a value here that something later overrides.

    In a clocked process every assignment is a register write, so a
    value written and then written again in the same clock is the
    first one thrown away: the reader has to carry the whole block in
    their head to know what the register actually takes. Assign it
    once, from its full condition, or in the arms of an if/else. In a
    comb process this is the ordinary way to write a mux and is left
    alone."""
    for index, statement in enumerate(body):
        if isinstance(statement, ast.Assign) and statement.targets:
            name = driven(statement.targets[0])
            if name is not None and name in assigned_under(body[index + 1:]):
                found.append((
                    statement.lineno,
                    f'{process} paints {name} with a default and something '
                    'below overrides it; assign it once'))
    for statement in body:
        for field in ('body', 'orelse', 'finalbody'):
            inner = getattr(statement, field, None)
            if isinstance(inner, list):
                painted(inner, process, found)
        if isinstance(statement, ast.Match):
            for case in statement.cases:
                painted(case.body, process, found)


def defaults_in_clocked (text):
    """The one rule here that needs the syntax tree rather than the
    characters. A file that will not parse is left to Python to
    complain about."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and clocked(node):
            painted(node.body, node.name, found)
    return sorted(found)


def check (path):
    problems = []
    with open(path, 'rb') as handle:
        raw = handle.read()
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError as bad:
        problems.append((0, f'not ASCII at byte {bad.start}'))
        text = raw.decode('utf-8', 'replace')

    lines = text.split('\n')
    for number, line in enumerate(lines, 1):
        if len(line) > LIMIT:
            problems.append((number, f'{len(line)} characters, over {LIMIT}'))
        if '\t' in line:
            problems.append((number, 'tab'))
        if line != line.rstrip():
            problems.append((number, 'trailing whitespace'))
        bar = BAR.match(line)
        if bar and len(line) != LIMIT:
            indent = len(bar.group(1))
            problems.append((
                number,
                f'section bar is {len(line)} characters; at this indent it '
                f'is #{"-" * 4}... to column {LIMIT}, so '
                f'{LIMIT - indent - 1} hyphens'))
    if text and not text.endswith('\n'):
        problems.append((len(lines), 'no newline at end of file'))
    elif text.endswith('\n\n'):
        problems.append((len(lines), 'blank line at end of file'))
    problems += defaults_in_clocked(text)
    return problems


def repair (path):
    """Normalise what can be normalised. Returns True if it changed."""
    with open(path) as handle:
        text = handle.read()
    out = []
    for line in text.split('\n'):
        bar = BAR.match(line)
        if bar:
            indent = bar.group(1)
            line = indent + '#' + '-' * (LIMIT - len(indent) - 1)
        else:
            line = line.rstrip()
        out.append(line)
    fixed = '\n'.join(out)
    if (fixed == text):
        return False
    with open(path, 'w') as handle:
        handle.write(fixed)
    return True


def main (argv):
    argv = list(argv)
    fixing = ('--fix' in argv)
    if fixing:
        argv.remove('--fix')
    targets = argv[1:] or ['.']
    files = []
    for target in targets:
        if os.path.isdir(target):
            for root, dirs, names in os.walk(target):
                dirs[:] = [d for d in dirs if d not in SKIP]
                files += [os.path.join(root, n) for n in sorted(names)
                          if n.endswith('.py')]
        else:
            files.append(target)

    if fixing:
        changed = [p for p in sorted(files) if repair(p)]
        for path in changed:
            print(f'fixed  {path}')

    total = 0
    for path in sorted(files):
        for number, message in check(path):
            print(f'{path}:{number}: {message}')
            total += 1
    if total:
        print(f'\n{total} problem{"s" if total != 1 else ""} '
              f'in {len(files)} files')
        return 1
    print(f'{len(files)} files, house layout clean')
    return 0
