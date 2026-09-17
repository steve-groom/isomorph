"""An entry of a clock array is a domain, and not the array it sits in.

A PLL monitor takes every PLL output as one array and makes a reset
for each, clocked on its own entry. Flattening the index made every
entry one domain named after the array, so a reset released on entry 0
came back as driven by a clock nothing in the design ran on, and
everything it reset looked like a crossing."""
import unittest

from isomorph import (analyse, block, signal, signals, always_ff,
                      always_comb, instances)


@block
def one_reset (i_clock, i_locked, o_sreset):

    @always_ff (i_clock.posedge)
    def reset_logic ():
        o_sreset.next = (not i_locked)

    return instances()


@block
def per_clock_reset (i_clocks, o_sresets, i_locked):
    """One reset per entry, each released on its own clock."""
    resets = [one_reset(i_clock = i_clocks[n], i_locked = i_locked,
                        o_sreset = o_sresets[n]) for n in range(2)]

    return instances()


@block
def counter (i_clock, i_sreset, o_count):

    @always_ff (i_clock.posedge)
    def count_logic ():
        o_count.next = 0 if i_sreset else (o_count + 1)[7:0]

    return instances()


@block
def board (clock_a, clock_b, locked, o_count):
    clocks = signals(2)
    sresets = signals(2)
    sreset_a = signal()

    @always_comb
    def rename ():
        clocks[0].next = clock_a
        clocks[1].next = clock_b
        sreset_a.next = sresets[0]

    inst_monitor = per_clock_reset (
        i_clocks = clocks, o_sresets = sresets, i_locked = locked)

    inst_count = counter (
        i_clock = clock_a, i_sreset = sreset_a, o_count = o_count)

    return instances()


class ArrayClockDomains (unittest.TestCase):

    def setUp (self):
        self.warnings = analyse(board(
            clock_a = signal(), clock_b = signal(), locked = signal(),
            o_count = signal(8)))[1]

    def test_no_crossing_reported (self):
        """sreset_a is released on clock_a, which is what clocks the
        counter, so nothing crosses."""
        crossings = [w for w in self.warnings if 'crossing' in w]
        self.assertEqual(crossings, [])

    def test_the_array_is_not_a_domain (self):
        """The name of the array must never appear as a clock: it is a
        bundle of two, and neither is called that."""
        self.assertFalse([w for w in self.warnings if 'clocks' in w
                          and 'crossing' in w])


if __name__ == '__main__':
    unittest.main()
