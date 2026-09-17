"""The stream bench helpers."""
import unittest

from isomorph import Simulator, SimError, signal
from isomorph.bench import (Stall, StreamSource, StreamSink, Pulse, step,
                            run_until, port_name)
from isomorph.lib import stream
from isomorph.lib import stream_pipe


def make_sim (backend = 'python'):
    top = stream_pipe(i_clock = signal(), i_reset = signal(),
                      i_stream = stream(8), o_stream = stream(8))
    sim = Simulator(top, backend = backend)
    sim.add_clock(20e-9)
    sim.reset('i_reset')
    return top, sim


class StallTests (unittest.TestCase):
    def test_none_never_stalls (self):
        stall = Stall(None)
        self.assertFalse(any(stall(c) for c in range(50)))

    def test_a_probability_repeats_with_its_seed (self):
        one = Stall(0.5, seed = 7)
        two = Stall(0.5, seed = 7)
        first = [one(c) for c in range(50)]
        again = [two(c) for c in range(50)]
        self.assertEqual(first, again)
        self.assertTrue(any(first))
        self.assertFalse(all(first))

    def test_a_callable_is_asked_the_cycle (self):
        stall = Stall(lambda cycle: cycle % 3 == 0)
        self.assertEqual([stall(c) for c in range(6)],
                         [True, False, False, True, False, False])

    def test_a_certainty_is_refused (self):
        with self.assertRaises(SimError):
            Stall(1.0)


class SourceSinkTests (unittest.TestCase):
    def test_words_go_through_in_order (self):
        _, sim = make_sim()
        words = list(range(20))
        source = StreamSource(sim, 'i_stream', words)
        sink = StreamSink(sim, 'o_stream')
        taken = run_until(sim, [source, sink],
                          lambda: len(sink.received) == 20, limit = 100)
        self.assertEqual(sink.received, words)
        self.assertEqual(source.sent, words)
        self.assertTrue(source.done)
        self.assertLessEqual(taken, 21)

    def test_a_presented_word_is_held_until_taken (self):
        """The stall decides whether to offer the next word, never
        whether to keep offering this one."""
        _, sim = make_sim()
        # the first word goes into the empty slice; the second is
        # offered on the next cycle and held by the stalled sink; the
        # stall then says stop, and the offer stands anyway
        source = StreamSource(sim, 'i_stream', [5, 6],
                              stall = lambda cycle: cycle >= 2)
        sink = StreamSink(sim, 'o_stream', stall = lambda cycle: True)
        step(sim, [source, sink], 5)
        self.assertEqual(source.sent, [5])
        self.assertEqual(sim.get('i_stream_valid'), 1)
        self.assertEqual(sim.get('i_stream_data'), 6)

    def test_a_bundle_names_its_port (self):
        top, sim = make_sim()
        self.assertEqual(port_name(sim, top.ports['i_stream']), 'i_stream')
        source = StreamSource(sim, top.ports['o_stream'], [])
        self.assertEqual(source.port, 'o_stream')

    def test_a_bundle_that_is_not_a_port_is_refused (self):
        _, sim = make_sim()
        with self.assertRaises(SimError):
            StreamSource(sim, stream(8), [])

    def test_a_pulse_lasts_one_step (self):
        _, sim = make_sim()
        pulse = Pulse(sim, 'i_reset')
        pulse.fire()
        pulse.drive()
        self.assertEqual(sim.get('i_reset'), 1)
        pulse.drive()
        self.assertEqual(sim.get('i_reset'), 0)

    def test_run_until_needs_its_limit (self):
        _, sim = make_sim()
        sink = StreamSink(sim, 'o_stream')
        with self.assertRaises(SimError) as ctx:
            run_until(sim, [sink], lambda: False, limit = 7)
        self.assertIn('7 cycles', str(ctx.exception))

    def test_step_without_an_edge_takes_no_cycle (self):
        _, sim = make_sim()
        before = sim.cycle
        step(sim, [], 3, edge = False)
        self.assertEqual(sim.cycle, before)
        step(sim, [], 3)
        self.assertEqual(sim.cycle, before + 3)


if __name__ == '__main__':
    unittest.main()
