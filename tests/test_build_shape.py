"""Two builds of one block are two modules, in two files.

A block is a function of its parameters and its port widths, so two
builds that differ in either are different hardware and cannot share
one module. The house idiom takes the width from the port - a multiply
step opens with WIDTH = max(len(i_data_a), len(i_data_b)) - so a block
can be built at two sizes with no parameter anywhere, and the hierarchy
walk used to key on the name alone, drop the second build and wire its
instance to the first build's module.
"""
import unittest

from isomorph import (block, signal, always_comb, always_ff, instances,
    open_port, analyse, emit_sv, IsomorphError)


@block
def sized (i_data, o_result):
    """Width from the port, with a constant of its own."""
    WIDTH = len(i_data)

    @always_comb
    def sized_comb ():
        o_result.next = i_data[WIDTH-1:0]

    return instances()


@block
def bare (i_data, o_data):
    """Nothing of its own: no parameter and no constant."""
    @always_comb
    def bare_comb ():
        o_data.next = i_data

    return instances()


@block
def scaled (i_clock, i_data, o_data, RATE = 50000000):
    """A parameter that stays a parameter: only ever used as a value."""
    tick = signal(32)

    @always_comb
    def scaled_comb ():
        o_data.next = i_data & tick[0]

    @always_ff (i_clock.posedge)
    def scaled_logic ():
        tick.next = RATE

    return instances()


def modules (top):
    return [m.name for m in analyse(top)[0]]


class TestBuildShape(unittest.TestCase):

    def test_two_widths_are_two_modules (self):
        @block
        def pair (i_big, i_small, o_big, o_small):
            wide = sized(i_data = i_big, o_result = o_big)
            thin = sized(i_data = i_small, o_result = o_small)
            return instances()

        names = modules(pair(i_big = signal(16), i_small = signal(4),
                             o_big = signal(16), o_small = signal(4)))
        self.assertIn('sized_WIDTH_16', names)
        self.assertIn('sized_WIDTH_4', names)

    def test_each_instance_binds_its_own_width (self):
        """sized names its width with WIDTH = len(i_data), so the two
        builds are one module and a generic rather than two modules.
        Each instance still gets the width it was given, which is what
        this was written to check."""
        @block
        def pair (i_big, i_small, o_big, o_small):
            wide = sized(i_data = i_big, o_result = o_big)
            thin = sized(i_data = i_small, o_result = o_small)
            return instances()

        text = emit_sv(analyse(
            pair(i_big = signal(16), i_small = signal(4),
                 o_big = signal(16), o_small = signal(4)))[0])
        self.assertEqual(text.count('module sized'), 1)
        self.assertIn('sized wide (', text)
        self.assertIn('sized #(.WIDTH(4)) thin (', text)
        self.assertNotIn('sized_WIDTH', text)

    def test_one_width_keeps_the_plain_name (self):
        @block
        def one (i_a, i_b, o_a, o_b):
            first = sized(i_data = i_a, o_result = o_a)
            second = sized(i_data = i_b, o_result = o_b)
            return instances()

        names = modules(one(i_a = signal(8), i_b = signal(8),
                            o_a = signal(8), o_b = signal(8)))
        self.assertIn('sized', names)
        self.assertNotIn('sized_WIDTH_8', names)

    def test_a_block_with_nothing_of_its_own_is_named_by_its_port (self):
        @block
        def two (i_big, i_small, o_big, o_small):
            a = bare(i_data = i_big, o_data = o_big)
            b = bare(i_data = i_small, o_data = o_small)
            return instances()

        names = modules(two(i_big = signal(8), i_small = signal(4),
                            o_big = signal(8), o_small = signal(4)))
        self.assertIn('bare_i_data_8', names)
        self.assertIn('bare_i_data_4', names)

    def test_a_parameter_that_survives_does_not_split_the_module (self):
        @block
        def board (i_clock, i_data, o_fast, o_slow):
            fast = scaled(i_clock = i_clock, i_data = i_data,
                          o_data = o_fast, RATE = 1000000)
            slow = scaled(i_clock = i_clock, i_data = i_data,
                          o_data = o_slow, RATE = 9600)
            return instances()

        text = emit_sv(analyse(
            board(i_clock = signal(), i_data = signal(),
                  o_fast = signal(), o_slow = signal()))[0])
        # one module, and the difference is an override at the instance
        self.assertEqual(text.count('module scaled'), 1)
        self.assertNotIn('scaled_RATE_9600', text)
        self.assertIn('.RATE(9600)', text)

    def test_an_unused_output_does_not_split_the_module (self):
        """open_port() must not make a second module.

        The stand-in signal open_port() builds is a vector where
        signal() would have called a one-bit signal a bit. Both emit
        as logic and std_logic, so telling them apart would give a
        block two names because the parent declined one of its
        outputs. Found on a design where an edge detector is
        instantiated both ways."""
        @block
        def edges (i_data, o_first, o_second):
            @always_comb
            def edges_comb ():
                o_first.next = i_data
                o_second.next = i_data
            return instances()

        @block
        def both (i_a, i_b, o_a, o_b, o_spare):
            used = edges(i_data = i_a, o_first = o_a, o_second = o_spare)
            part = edges(i_data = i_b, o_first = o_b,
                         o_second = open_port())
            return instances()

        names = modules(both(i_a = signal(), i_b = signal(),
                             o_a = signal(), o_b = signal(),
                             o_spare = signal()))
        self.assertEqual([n for n in names if n.startswith('edges')],
                         ['edges'])

    def test_the_name_is_the_smallest_that_separates (self):
        """Two ports differ; naming after one of them is enough."""
        @block
        def two (i_big, i_small, o_big, o_small):
            a = bare(i_data = i_big, o_data = o_big)
            b = bare(i_data = i_small, o_data = o_small)
            return instances()

        names = modules(two(i_big = signal(8), i_small = signal(4),
                            o_big = signal(8), o_small = signal(4)))
        self.assertNotIn('bare_i_data_8_o_data_8', names)


if __name__ == '__main__':
    unittest.main()
