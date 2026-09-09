"""IEEE 1364-2001 VCD writer and a small parser (SIM_PLAN S3, S6)."""


class VcdWriter:
    """Dump-on-change VCD. Identifiers are s0, s1, ... in wire order."""

    def __init__ (self, path, timescale = '1 ns', version = 'isomorph'):
        self.path = path
        self.fp = open(path, 'w', encoding = 'ascii', newline = '\n')
        self.n = 0
        self.last = []
        self.valid = []
        self.widths = []
        self.ids = []
        self.names = []
        self.in_dumpvars = False
        self.pending_time = 0
        self.time_written = False
        self.fp.write('$date\n    isomorph\n$end\n')
        self.fp.write(f'$version\n    {version}\n$end\n')
        self.fp.write(f'$timescale\n    {timescale}\n$end\n')

    def push_scope (self, name):
        self.fp.write(f'$scope module {name} $end\n')

    def pop_scope (self):
        self.fp.write('$upscope $end\n')

    def wire (self, name, width):
        width = max(1, int(width))
        ident = f's{self.n}'
        self.ids.append(ident)
        self.names.append(name)
        self.widths.append(width)
        self.last.append(0)
        self.valid.append(False)
        self.fp.write(f'$var wire {width} {ident} {name} $end\n')
        self.n += 1
        return self.n - 1

    def finish_defs (self):
        self.fp.write('$enddefinitions $end\n')
        self.fp.write('$dumpvars\n')
        self.in_dumpvars = True

    def end_dumpvars (self):
        self.fp.write('$end\n')
        self.in_dumpvars = False

    def time (self, t):
        self.pending_time = int(t)
        self.time_written = False

    def change (self, id, value, width = None):
        if id < 0 or id >= self.n:
            return
        value = int(value) & ((1 << 64) - 1)
        if width is None:
            width = self.widths[id]
        if self.valid[id] and self.last[id] == value and not self.in_dumpvars:
            return
        if not self.in_dumpvars and not self.time_written:
            self.fp.write(f'#{self.pending_time}\n')
            self.time_written = True
        self.fp.write(_bits(value, width))
        if width > 1:
            self.fp.write(' ')
        self.fp.write(self.ids[id])
        self.fp.write('\n')
        self.last[id] = value
        self.valid[id] = True

    def close (self):
        if self.fp is not None:
            self.fp.close()
            self.fp = None


def _bits (value, width):
    if width <= 1:
        return '1' if value & 1 else '0'
    bits = []
    for i in range(width - 1, -1, -1):
        bits.append('1' if (value >> i) & 1 else '0')
    return 'b' + ''.join(bits)


def parse_vcd (text):
    """Return (timescale, [{t, values}]) where values is hier-name -> int.

    Hierarchical names are dotted from the $scope stack, including the
    top module.
    """
    timescale = '1 ns'
    scopes = []
    id_to_name = {}
    id_to_width = {}
    samples = []
    current_t = 0
    current = {}
    in_dumpvars = False

    def flush ():
        samples.append({'t': current_t, 'values': dict(current)})
        current.clear()

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if line.startswith('$timescale'):
            body = line[len('$timescale'):].strip()
            if '$end' not in body:
                parts = []
                while i < len(lines) and '$end' not in lines[i]:
                    parts.append(lines[i].strip())
                    i += 1
                if i < len(lines):
                    i += 1
                timescale = ' '.join(parts) or timescale
            else:
                timescale = body.replace('$end', '').strip() or timescale
            continue
        if line.startswith('$scope'):
            parts = line.split()
            # $scope module name $end
            if len(parts) >= 3:
                scopes.append(parts[2])
            continue
        if line.startswith('$upscope'):
            if scopes:
                scopes.pop()
            continue
        if line.startswith('$var'):
            parts = line.split()
            # $var wire W id name $end
            if len(parts) >= 5:
                width = int(parts[2])
                ident = parts[3]
                name = parts[4]
                hier = '.'.join(scopes + [name]) if scopes else name
                id_to_name[ident] = hier
                id_to_width[ident] = width
            continue
        if line.startswith('$dumpvars'):
            in_dumpvars = True
            continue
        if line.startswith('$end'):
            if in_dumpvars:
                in_dumpvars = False
            continue
        if line.startswith('$'):
            if '$end' not in line:
                while i < len(lines) and '$end' not in lines[i]:
                    i += 1
                if i < len(lines):
                    i += 1
            continue
        if line.startswith('#'):
            flush()
            current_t = int(line[1:].strip())
            continue
        ident, value = _parse_change(line, id_to_width)
        if ident is None:
            continue
        name = id_to_name.get(ident)
        if name is not None:
            current[name] = value
    if current:
        samples.append({'t': current_t, 'values': dict(current)})
    return timescale, samples


def _parse_change (line, id_to_width):
    if line.startswith('b') or line.startswith('B'):
        parts = line.split()
        if len(parts) < 2:
            return None, 0
        bits = parts[0][1:]
        ident = parts[1]
        value = int(bits, 2) if bits else 0
        return ident, value
    if line[0] in '01xXzZ':
        value = 1 if line[0] == '1' else 0
        ident = line[1:].strip()
        return ident, value
    return None, 0


def values_at (samples, name_suffix):
    """Running value of a signal whose hierarchical name ends with suffix."""
    out = []
    last = None
    key = None
    for sample in samples:
        if key is None:
            for k in sample['values']:
                if k == name_suffix or k.endswith('.' + name_suffix):
                    key = k
                    break
        if key is not None and key in sample['values']:
            last = sample['values'][key]
        out.append((sample['t'], last))
    return out
