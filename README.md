# Isomorph

RTL in Python, converted to SystemVerilog, VHDL-2008 and C99 without
being flattened, renamed or rescheduled.

The thesis: the file Quartus compiled is the file you stepped, peeked,
constrained and equivalence-checked, and every name in the fit report
is one you typed in the Python. A `@block` becomes a module, a process
becomes a process, a signal keeps its name and its width. There is no
scheduler inserting hardware nobody asked for.

## Install

One command on a machine with nothing on it:

    curl -fsSL https://raw.githubusercontent.com/steve-groom/isomorph/main/install.sh | sh

That fetches this tree, pip-installs the package into the
site-packages of the active python — where MyHDL would go — and then
offers to install the optional tools that switch on linting and the
other two simulators. Python 3.11 or newer is all it assumes; if the
machine has no pip it offers to fetch that too, and it handles the
distributions that mark their python externally managed. Nothing is
installed with root without being asked. It is the
[install.sh](install.sh) in this repository, so read it first if you
would rather.

If you already have pip and want the package alone:

    pip install git+https://github.com/steve-groom/isomorph
    isomorph doctor --install

A tarball for a machine with no network, a tools folder of your own,
and the full story, is in [INSTALL.txt](INSTALL.txt).

## A design

```python
from isomorph import (block, signal, always_ff, always_comb,
    instances, main)

@block
def an edge detector (i_clock, i_data, o_leading_edge, o_trailing_edge):

    sample_flop = signal()

    @always_ff (i_clock.posedge)
    def synch_logic ():
        sample_flop.next = i_data

    @always_comb
    def edges_comb ():
        o_leading_edge.next = (i_data & (not sample_flop))
        o_trailing_edge.next = ((not i_data) & sample_flop)

    return instances()

def test_an edge detector (sim):
    sim.add_clock(20e-9)
    sim.set('i_data', 1)
    sim.posedge()
    assert sim.get('o_leading_edge') == 0

if (__name__ == '__main__'):
    import sys
    design = an edge detector(i_clock = signal(), i_data = signal(),
                          o_leading_edge = signal(),
                          o_trailing_edge = signal())
    sys.exit(main(design, test_an edge detector))
```

That file is its own command line:

    python3 an edge detector.py                  run the bench
    python3 an edge detector.py --run verilator  run it under Verilator
    python3 an edge detector.py --sv --vhdl      write the HDL
    python3 an edge detector.py --sv --lint      write it and check it
    python3 an edge detector.py --vcd wave.vcd   record a waveform

## Three simulators, one bench

| backend     | what it is                          | use it for        |
|-------------|-------------------------------------|-------------------|
| `python`    | the IR interpreted                  | `print()` and pdb |
| `c99`       | the emitted C99 through gcc         | speed             |
| `verilator` | the emitted SystemVerilog           | the reference     |

If the Verilator run disagrees with the other two, the other two are
wrong. The same bench drives all three, and the lockstep suite runs
designs on every backend and compares them cycle by cycle. Yosys, with
the GHDL plugin, reads both emitted languages into one netlist and
proves them equivalent rather than merely agreeing on the stimulus a
bench happened to apply.

## Reading

- [INSTALL.txt](INSTALL.txt) — installing, packaging, external tools.

A guide to writing isomorph is being written. Until then the
`examples/` directory and the blocks under `isomorph/lib/` are the
worked ones, and `python3 design.py --help` lists what a design file
can do.

## Licence

MIT. See [LICENSE](LICENSE).
