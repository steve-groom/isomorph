"""A clock that only passes through, and ports that are arrays.

Three levels. The counters are at the bottom, one per clock, and every
level above them holds no flip-flop of its own: it decodes, or it just
hands the array on. That is the shape of a board - a peripheral block
in front of its peripherals, a wrapper in front of a shared SPI slave,
a PLL monitor taking every output clock as one array - and it is the
shape three separate bugs hid in.

A named clock stopped at the first module with no flip-flop of its own,
so a whole subtree never saw an edge, and only in a design with more
than one clock, because a single-clock tick() names none and fires
everything. An array port carried nothing at all in the Python
simulator and did not compile in C99. And the C99 commit skipped the
same pass-through modules, so the pending values below them stayed
pending.

Two domains at unrelated rates, so a backend that clocks both on one
edge is caught by the counts rather than by looking correct.
"""
from isomorph import (block, signal, signals, always_ff, always_comb,
    assign, instances, main)

CLOCKS = 2
WIDTH = 8


@block
def counter (i_clock, i_clock_sreset, o_count):
    """The only flip-flop in the design."""
    next_count = signal(WIDTH)

    @always_comb
    def counter_comb ():
        next_count.next = (o_count + 1)[WIDTH-1:0]

    @always_ff (i_clock.posedge)
    def counter_logic ():
        if (i_clock_sreset):
            o_count.next = 0
        else:
            o_count.next = next_count

    return instances()


@block
def bank (i_clocks, i_clock_sreset, o_counts):
    """One counter per clock, and nothing of its own."""
    inst_counter_0 = counter (
        i_clock = i_clocks[0],
        i_clock_sreset = i_clock_sreset,
        o_count = o_counts[0]
    )
    inst_counter_1 = counter (
        i_clock = i_clocks[1],
        i_clock_sreset = i_clock_sreset,
        o_count = o_counts[1]
    )
    return instances()


@block
def passthrough_clocks (i_clock_fast, i_clock_slow, i_clock_sreset,
                        o_counts, o_sum):
    """The bank, one level further out, plus something to look at.

    The two clocks arrive as pins and reach the bank as one array, so
    each of them is a wire away from the flip-flops it clocks. A wire
    is not a domain of its own, and a simulator that knows clocks by
    name has to be told: otherwise the array entry becomes a domain
    nothing drives and the pin that drives it is ignored.
    """
    clocks = signals(CLOCKS)

    assign(clocks[0], lambda: i_clock_fast)
    assign(clocks[1], lambda: i_clock_slow)

    inst_bank = bank (
        i_clocks = clocks,
        i_clock_sreset = i_clock_sreset,
        o_counts = o_counts
    )

    @always_comb
    def sum_comb ():
        o_sum.next = (o_counts[0] + o_counts[1])[WIDTH-1:0]

    return instances()


#------------------------------------------------------------------------------
def elaborate_passthrough_clocks ():
    """The one place the ports are built."""
    return passthrough_clocks (
        i_clock_fast = signal(),
        i_clock_slow = signal(),
        i_clock_sreset = signal(),
        o_counts = signals(CLOCKS, WIDTH),
        o_sum = signal(WIDTH)
    )


#------------------------------------------------------------------------------
# Simulation / Test Bench
FAST_NS = 10
SLOW_NS = 26


def test_passthrough_clocks (sim):
    """Each counter counts its own clock, and neither counts the
    other's."""
    sim.add_clock(FAST_NS * 1e-9, 'i_clock_fast')
    sim.add_clock(SLOW_NS * 1e-9, 'i_clock_slow')

    sim.set('i_clock_sreset', 1)
    sim.run(duration = 4 * SLOW_NS * 1e-9)
    sim.set('i_clock_sreset', 0)
    sim.eval()
    assert sim.get('o_counts[0]') == 0, 'the fast counter did not reset'
    assert sim.get('o_counts[1]') == 0, 'the slow counter did not reset'

    NANOSECONDS = 1000
    sim.run(duration = NANOSECONDS * 1e-9)
    sim.eval()

    fast = sim.get('o_counts[0]')
    slow = sim.get('o_counts[1]')
    assert fast > 0, 'the fast counter never had an edge'
    assert slow > 0, 'the slow counter never had an edge'
    assert fast > slow, \
        f'the fast counter reached {fast} and the slow one {slow}'
    assert sim.get('o_sum') == ((fast + slow) & 0xff), \
        'the sum did not follow the two counts'


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys

    sys.exit(main(elaborate_passthrough_clocks, test_passthrough_clocks))
