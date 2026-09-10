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

This is the layout half of HOUSE_STYLE.md. The rules the converter
enforces, and the ones only a person can, are in that document; these
are the ones a machine can settle on its own, so it does.
"""
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
