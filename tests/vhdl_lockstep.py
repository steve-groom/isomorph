"""The emitted VHDL, run against the emitted SystemVerilog's answers.

The two outputs have to be proved equivalent, and there is a
practical form of that for when a formal tool is not to hand: the same stimulus
through GHDL and through the reference, compared every cycle. Formal
is not to hand here - eqy is not installed and yosys has no ghdl
plugin, so neither language can be read into the same netlist - so
this is the form that runs.

How it works. The design is run on a backend that is already trusted
against Verilator, recording every input applied and every output seen
each cycle. A self-checking VHDL testbench is generated that applies
the same inputs and asserts the same outputs, and ghdl runs it. A
divergence is an emitter bug and the assertion names the port and the
cycle.

The clock is real here, unlike in the cycle model: the testbench
drives it low, applies the inputs, lets the combinational logic
settle, takes the edge, and checks. That is tick() written out in
time, which is why the two agree cycle for cycle.

The run is at assert-level failure rather than error, so a design's
own assertions do not stop it, and the reason is worth writing down.
VHDL has nine-valued logic and a register starts at 'U'; the Python
model, the C99 one and Verilator all start it at zero. Isomorph emits
no power-on value on purpose, so at time zero an
invariant over a register that nothing has written yet evaluates to
'U' in VHDL, and an assertion on 'U' fails. stream_from_memory does
exactly that: its 'a read landed with no room' fires at @0ms under
ghdl and on none of the other three. That is not an emitter bug and
not a design bug, it is the one place the four backends genuinely
disagree, and the check this harness is for is the cycle-by-cycle
output comparison below, which the testbench does itself. Verilator
is where a design's assertions are checked.
"""
import os
import shutil
import subprocess
import tempfile

from isomorph import Simulator, analyse, write_vhdl_files

HAVE_GHDL = shutil.which('ghdl') is not None

SETTLE_NS = 5
EDGE_NS = 5


def bits (value, width):
    value = int(value) & ((1 << width) - 1)
    return ''.join('1' if (value >> n) & 1 else '0'
                   for n in range(width - 1, -1, -1))


def literal (value, width):
    if width == 1:
        return "'1'" if int(value) & 1 else "'0'"
    return '"' + bits(value, width) + '"'


def usable (top):
    """Why this design cannot be checked this way, or None."""
    if any(p.array for p in top.ports):
        return 'an array port is not driven by this testbench yet'
    clocks = {p.clock for p in top.processes
              if p.kind == 'ff' and p.clock}
    if len(clocks) > 1:
        return 'more than one clock needs a testbench per domain'
    return None


def record (elaborate, stimulus, cycles, backend = 'python'):
    """Run the design, keeping what went in and what came out."""
    sim = Simulator(elaborate(), backend = backend)
    top = sim.top
    clocks = {p.clock for p in top.processes
              if p.kind == 'ff' and p.clock}
    clock = sorted(clocks)[0] if clocks else None
    inputs = [p for p in top.ports
              if p.direction == 'in' and p.name != clock]
    outputs = [p for p in top.ports if p.direction == 'out']
    sim.add_clock(20e-9)
    vectors = []
    for cycle in range(cycles):
        applied = stimulus(cycle)
        for name, value in applied.items():
            sim.set(name, value)
        sim.eval()
        if clock:
            sim.tick(1)
        else:
            sim.eval()
        vectors.append((
            {p.name: int(applied.get(p.name, sim.get(p.name)))
             for p in inputs},
            {p.name: int(sim.get(p.name)) for p in outputs}))
    sim.close()
    return top, clock, inputs, outputs, vectors


def testbench (top, clock, inputs, outputs, vectors):
    """A VHDL testbench that applies the vectors and checks them."""
    name = f'tb_{top.name}'
    lines = [
        'library ieee;',
        'use ieee.std_logic_1164.all;',
        '',
        f'entity {name} is',
        f'end entity {name};',
        '',
        f'architecture sim of {name} is',
    ]
    for port in top.ports:
        lines.append(f'  signal {port.name} : '
                     + ('std_logic' if port.width == 1 else
                        f'std_logic_vector({port.width - 1} downto 0)')
                     + ';')
    lines += [
        'begin',
        f'  dut : entity work.{top.name}',
        '    port map (',
    ]
    maps = [f'      {p.name} => {p.name}' for p in top.ports]
    lines.append(',\n'.join(maps))
    lines += [
        '    );',
        '',
        '  stimulus : process',
        '    variable failures : natural := 0;',
        '    variable skipped  : natural := 0;',
        '  begin',
    ]
    if clock:
        lines.append(f"    {clock} <= '0';")
    for index, (applied, expected) in enumerate(vectors):
        lines.append(f'    -- cycle {index}')
        for port in inputs:
            lines.append(f'    {port.name} <= '
                         f'{literal(applied[port.name], port.width)};')
        lines.append(f'    wait for {SETTLE_NS} ns;')
        if clock:
            lines.append(f"    {clock} <= '1';")
            lines.append(f'    wait for {EDGE_NS} ns;')
            lines.append(f"    {clock} <= '0';")
            lines.append(f'    wait for {EDGE_NS} ns;')
        for port in outputs:
            if port.name not in expected:
                continue
            want = literal(expected[port.name], port.width)
            # only where VHDL has committed to a value. It starts a
            # register at 'U' and isomorph emits no power-on value on
            # purpose, so an output reading state nothing has written
            # yet is undefined here and zero on the other three.
            # That state is undefined, so there is nothing to
            # compare; the skips are counted and reported, because a
            # run that matched only because everything was unknown has
            # said nothing and should not look like a pass.
            lines.append(f'    if (is_x({port.name})) then')
            lines.append('      skipped := skipped + 1;')
            lines.append(f'    elsif ({port.name} /= {want}) then')
            lines.append(
                f'      report "cycle {index}: {port.name} is not '
                f'{expected[port.name]}" severity error;')
            lines.append('      failures := failures + 1;')
            lines.append('    end if;')
    lines += [
        '    if (failures = 0) then',
        '      report "vhdl matched every cycle, " '
        '& integer\'image(skipped) & " checks skipped as undefined" '
        'severity note;',
        '    else',
        '      report "the emitted VHDL and the reference disagree" '
        'severity failure;',
        '    end if;',
        '    wait;',
        '  end process stimulus;',
        '',
        'end architecture sim;',
    ]
    return '\n'.join(lines) + '\n'


def run (elaborate, stimulus, cycles = 40, backend = 'python',
         settle = 0):
    """Record on `backend`, then prove GHDL agrees. Returns the output.

    settle drops the first few cycles from the comparison. Isomorph
    emits no power-on value on purpose, so a VHDL register holds 'U'
    until something writes it while the cycle model starts it at zero;
    the two only have anything to say to each other once a reset has
    been through. A design that resets needs settle to cover the
    reset, and one that does not needs enough cycles for every
    register to have been written. It is a number the bench states
    rather than a window the harness guesses at.
    """
    # what the harness cannot drive is said before anything is run,
    # so the reason is the reason and not whatever fell over first
    modules, _ = analyse(elaborate())
    why = usable(modules[-1])
    if why:
        raise NotImplementedError(why)
    top, clock, inputs, outputs, vectors = record(
        elaborate, stimulus, cycles, backend)
    vectors = [(applied, expected if index >= settle else {})
               for index, (applied, expected) in enumerate(vectors)]
    directory = tempfile.mkdtemp(prefix = 'iso_vhdl_eq_')
    try:
        written = write_vhdl_files(modules, directory, listing = False)
        tb_path = os.path.join(directory, f'tb_{top.name}.vhd')
        with open(tb_path, 'w', encoding = 'ascii') as handle:
            handle.write(testbench(top, clock, inputs, outputs, vectors))
        sources = [p for p in written if p.endswith('.vhd')] + [tb_path]
        for step in (['ghdl', '-a', '--std=08'] + sources,
                     ['ghdl', '-e', '--std=08', f'tb_{top.name}'],
                     ['ghdl', '-r', '--std=08', f'tb_{top.name}',
                      '--assert-level=failure']):
            done = subprocess.run(step, cwd = directory,
                                  capture_output = True, text = True)
            if done.returncode != 0:
                raise AssertionError(
                    ' '.join(step[:3]) + ' failed:\n'
                    + (done.stdout or '') + (done.stderr or ''))
        return done.stdout + done.stderr
    finally:
        shutil.rmtree(directory, ignore_errors = True)
