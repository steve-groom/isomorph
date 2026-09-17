"""The three backends compared cycle by cycle (roadmap 3).

They share one intermediate form, so a difference between them is an
emitter bug rather than a design one, and this is where it gets caught.
Ports and the top module's own flip-flops, every cycle, on whichever
backends this machine can build.

This runs in continuous integration and nowhere else: it compiles a
Verilator model per design, which is not something to put in the way of
`python3 design.py`.
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lockstep import Lockstep, available          # noqa: E402

CONSTRUCTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'constructs')
sys.path.insert(0, CONSTRUCTS)


class LockstepTests (unittest.TestCase):
    def setUp (self):
        self.backends = available()
        if len(self.backends) < 2:
            self.skipTest('needs at least two backends')

    def test_mini_core_agrees_every_cycle (self):
        from mini_core import elaborate_mini_core

        step = Lockstep(elaborate_mini_core, self.backends)
        try:
            step.reset('i_reset')
            random.seed(20260910)
            for _ in range(200):
                step.set('i_word', random.randint(0, 0xffff))
                step.set('i_a', random.randint(0, 0xff))
                step.set('i_b', random.randint(0, 0xff))
                step.eval()
                step.tick(1)
        finally:
            step.close()

    def test_hierarchy_agrees_on_every_settle (self):
        """A design with no clock is compared on the settle.

        Two instances of the same child, so this also checks that the
        three backends agree about a hierarchy rather than only about
        one flat module."""
        from hierarchy import elaborate_parent

        step = Lockstep(elaborate_parent, self.backends)
        try:
            random.seed(20260911)
            for _ in range(200):
                step.set('i_x', random.randint(0, 0x3f))
                step.set('i_y', random.randint(0, 0x3f))
                step.set('bus_data', random.randint(0, 0x3f))
                step.eval()
        finally:
            step.close()

    def test_a_planted_difference_is_caught (self):
        """The comparison has teeth.

        Nothing can make one backend genuinely disagree on demand, so
        this pokes one simulator directly and checks the next compare
        reports it, with the name and the cycle."""
        from mini_core import elaborate_mini_core

        step = Lockstep(elaborate_mini_core, self.backends)
        try:
            step.reset('i_reset')
            step.set('i_a', 3)
            step.tick(1)
            other = step.backends[1]
            step.sims[other].set('i_a', 200)
            with self.assertRaises(AssertionError) as caught:
                step.compare()
            self.assertIn('i_a', str(caught.exception))
        finally:
            step.close()


if __name__ == '__main__':
    unittest.main()


CROSSING = """from isomorph import (block, signal, always_ff, always_comb,
                      concat, instances, main)


@block
def two_flop (i_clock, i_data, o_data):
    chain = signal(2)

    @always_ff (i_clock.posedge)
    def chain_logic ():
        chain.next = concat(chain[0], i_data)

    @always_comb
    def chain_comb ():
        o_data.next = chain[1]

    return instances()


@block
def domains (i_clock, i_other, i_d, o_far):
    near = signal()

    @always_ff (i_clock.posedge)
    def near_logic ():
        near.next = i_d

    # the child's own clock port is also called i_clock, and it is
    # wired to the other domain
    inst_cross = two_flop(i_clock = i_other, i_data = near,
                          o_data = o_far)

    return instances()


def elaborate_domains ():
    return domains(i_clock = signal(), i_other = signal(),
                   i_d = signal(), o_far = signal())
"""


class ClockNameCollisionTests (unittest.TestCase):
    """A child is clocked by what it is wired to, not by a name.

    A synchroniser's own clock port is called i_clock, and so is the
    parent's clock. The two smoke backends used to fall back on that
    name when no connection matched, so the parent clocked every child
    on its own edge and a crossing propagated in one edge instead of
    two. Verilator was right and they were not, which is exactly what
    running them together is for.
    """

    def setUp (self):
        self.backends = available()
        if len(self.backends) < 2:
            self.skipTest('needs at least two backends')

    def test_the_crossing_takes_two_edges_on_every_backend (self):
        import itertools
        import linecache

        name = f'<crossing{next(itertools.count())}>'
        linecache.cache[name] = (len(CROSSING), None,
                                 CROSSING.splitlines(True), name)
        namespace = {}
        exec(compile(CROSSING, name, 'exec'), namespace)

        step = Lockstep(namespace['elaborate_domains'], self.backends,
                        clocks = {'i_clock': 20e-9, 'i_other': 27e-9})
        try:
            step.set('i_d', 1)
            step.eval()
            for _ in range(40):
                step.run(10e-9)
            self.assertEqual(step.get('o_far'), 1)
        finally:
            step.close()


class PassThroughTests (unittest.TestCase):
    """A clock that only passes through, and ports that are arrays.

    Three bugs shared this shape, and a board found all three at once.
    A named clock stopped at the first module with no flip-flop of its
    own, so a whole subtree never had an edge - and only ever in a
    design with more than one clock, because a single-clock tick()
    names none and fires everything. An array port carried nothing in
    the Python simulator and did not compile in C99. And the C99
    commit skipped the same pass-through modules, leaving the pending
    values below them pending. And the clocks arrive here as pins and
    reach the counters as one array, so each is a wire away from what
    it clocks: a wire is not a domain of its own, and taking it for
    one made the pin that drives it be ignored.

    Two domains at unrelated rates, so a backend that clocks both on
    one edge is caught by the counts rather than by looking right.
    """

    def setUp (self):
        self.backends = available()
        if len(self.backends) < 2:
            self.skipTest('needs at least two backends')

    def test_each_counter_counts_its_own_clock (self):
        from passthrough_clocks import elaborate_passthrough_clocks

        step = Lockstep(elaborate_passthrough_clocks, self.backends,
                        clocks = {'i_clock_fast': 10e-9,
                                  'i_clock_slow': 26e-9})
        try:
            step.reset('i_clock_sreset')
            for _ in range(60):
                step.run(20e-9)
            fast = step.get('o_counts[0]')
            slow = step.get('o_counts[1]')
            self.assertGreater(fast, 0, 'the fast counter never ran')
            self.assertGreater(slow, 0, 'the slow counter never ran')
            self.assertGreater(fast, slow, 'the two clocks ran as one')
        finally:
            step.close()
