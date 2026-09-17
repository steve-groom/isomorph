"""Timing constraints, from the clocks and the crossings.

This writes a supplementary constraint file, not a replacement for the
one a vendor tool generates. The Efinity Interface Designer already
writes create_clock for its PLL outputs and says in the file that any
manual change is lost; Quartus derive_clocks will invent them. What no
vendor tool can know is which signals cross between domains, because
that needs the design analysed, and that is what this is for.

The dialects differ, so the vendor is named rather than guessed:

  quartus   Intel Quartus Prime, .sdc read by TimeQuest
  vivado    AMD Vivado, .xdc
  efinity   Efinix Efinity, .sdc

Only the Quartus output has been run through the tool it is for, and
the header of every file says which one it is.

Two rules about what goes in. A path is constrained only where the
crossing was declared deliberate, because a crossing with no
synchroniser is a bug and cutting its timing would hide it. And a
false path is written per crossing pair rather than set_clock_groups
over everything, because a group says the clocks are unrelated and
only the author knows that; the line to use instead is in the file.
"""
from .analyse import ConversionError

VENDORS = ('quartus', 'vivado', 'efinity')


def clock_ports (top):
    """Ports declared a clock period, in port order."""
    return [(p.name, p.period_ns) for p in top.ports
            if getattr(p, 'period_ns', None)]


def crossing_pairs (top):
    """{(a, b): [crossings]} for the deliberate crossings, a < b."""
    pairs = {}
    for crossing in getattr(top, 'crossings', []) or []:
        if not crossing.get('synchronised'):
            continue
        key = tuple(sorted((crossing['from'], crossing['to'])))
        if key[0] == key[1]:
            continue
        pairs.setdefault(key, []).append(crossing)
    return pairs


def loose_crossings (top):
    return [c for c in (getattr(top, 'crossings', []) or [])
            if not c.get('synchronised')]


def get_clocks (vendor, name):
    if vendor == 'vivado':
        return f'[get_clocks {{{name}}}]'
    return f'[get_clocks {{{name}}}]'


def emit_sdc (modules, vendor):
    """The constraint text for one design."""
    if vendor not in VENDORS:
        raise ConversionError(
            f'{vendor!r} is not a vendor this writes constraints for; '
            'it knows ' + ', '.join(VENDORS) + '. The dialects differ, '
            'so it is named rather than guessed.')
    top = modules[-1]
    suffix = 'xdc' if vendor == 'vivado' else 'sdc'
    lines = [
        f'# {top.name}.{suffix} - written by isomorph for {vendor}.',
        '#',
        '# A supplement, not a replacement. Whatever defines the clocks',
        '# in your flow keeps defining them: an Efinity Interface',
        "# Designer file says so in its own header, and Quartus'",
        '# derive_clocks will invent them. What is here is what needs',
        '# the design analysed to know.',
        '#',
        '# Every name below is the name in the emitted HDL, which is',
        '# the name written in the Python.',
        '',
    ]
    lines += create_clock_lines(top, vendor)
    lines += uncertainty_lines(vendor)
    lines += crossing_lines(top, vendor)
    text = '\n'.join(lines).rstrip('\n')
    return text + '\n'


def create_clock_lines (top, vendor):
    declared = clock_ports(top)
    lines = ['#' + '-' * 74, '# clocks']
    if not declared:
        lines += [
            '#',
            '# No port declared a period, so nothing is created here.',
            '# clock(i_clock, period = 20e-9) in the block declares one,',
            '# and it is only worth doing where nothing else already',
            '# defines the clock.',
            '',
        ]
        return lines
    lines.append('')
    for name, period in declared:
        if vendor == 'vivado':
            lines.append(f'create_clock -name {name} -period '
                         f'{period:.3f} [get_ports {{{name}}}]')
        else:
            lines.append(f'create_clock -name {{{name}}} -period '
                         f'{period:.3f} [get_ports {{{name}}}]')
    lines.append('')
    return lines


def uncertainty_lines (vendor):
    """Quartus wants to be told to work its own uncertainty out.

    Without it every clock transfer is a critical warning saying the
    numbers are optimistic, which is true, and the line that fixes it
    is the same one in every Quartus project ever written. Vivado
    derives it without being asked and Efinity has no equivalent, so
    this is Quartus only.
    """
    if vendor != 'quartus':
        return []
    return ['#' + '-' * 74,
            '# uncertainty',
            '',
            'derive_clock_uncertainty',
            '']


def crossing_lines (top, vendor):
    pairs = crossing_pairs(top)
    loose = loose_crossings(top)
    lines = ['#' + '-' * 74, '# clock domain crossings']
    if not pairs and not loose:
        lines += ['#', '# The design has none.', '']
        return lines
    lines.append('#')
    lines.append('# Found by analysing the design, one line per path:')
    for crossing in sorted(
            (getattr(top, 'crossings', []) or []),
            key = lambda c: (c['module'], c['process'], c['signal'])):
        mark = '' if crossing['synchronised'] else '   NO SYNCHRONISER'
        lines.append(
            f"#   {crossing['module']}.{crossing['process']}: "
            f"{crossing['signal']} [{crossing['width']}] "
            f"{crossing['from']} -> {crossing['to']}{mark}")
    if loose:
        lines.append('#')
        lines.append('# The ones marked above have no flop declared a')
        lines.append('# synchroniser behind them. They are not')
        lines.append('# constrained here: cutting the timing on a')
        lines.append('# crossing that has no synchroniser hides it.')
    lines.append('')
    if not pairs:
        return lines
    for (first, second), members in sorted(pairs.items()):
        lines.append(f'# {first} and {second}: '
                     f'{len(members)} path(s) through a synchroniser')
        for source, target in ((first, second), (second, first)):
            lines.append(
                f'set_false_path -from {get_clocks(vendor, source)} '
                f'-to {get_clocks(vendor, target)}')
        lines.append('')
    lines += [
        '# A false path per pair rather than one set_clock_groups over',
        '# all of them, because a group asserts the clocks are',
        '# unrelated and only you know that. If they are, this is the',
        '# line, and it replaces the pairs above:',
        '#',
    ]
    clocks = sorted({name for pair in pairs for name in pair})
    if vendor == 'vivado':
        groups = ' '.join(f'-group [get_clocks {{{c}}}]' for c in clocks)
    else:
        groups = ' '.join(f'-group {{{c}}}' for c in clocks)
    lines.append(f'#   set_clock_groups -asynchronous {groups}')
    lines.append('')
    return lines


def write_sdc (modules, path, vendor):
    import os
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok = True)
    with open(path, 'w', encoding = 'ascii', newline = '\n') as handle:
        handle.write(emit_sdc(modules, vendor))
    return path
