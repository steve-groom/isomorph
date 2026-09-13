"""Two inputs into one beat.

A beat leaves when both inputs hold one, carrying concat(b, a): i_a in
the low bits, i_b above it. Combinational, so it has no clock, and it
is the only block here whose ready depends on a valid, which the
handshake allows of a sink and never of a source.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main)
from isomorph.lib.ports import stream


@block
def stream_join (
        i_a,
        i_b,
        o_stream
    ):

    @always_comb
    def join_comb ():
        o_stream.valid.next = i_a.valid & i_b.valid
        o_stream.data.next = concat(i_b.data, i_a.data)
        # each side is taken only when the other is there too
        i_a.ready.next = i_b.valid & o_stream.ready
        i_b.ready.next = i_a.valid & o_stream.ready

    return instances()


#------------------------------------------------------------------------------
def elaborate_stream_join (WIDTHA = 8, WIDTHB = 4):
    """The one place the ports are built."""
    return stream_join (
        i_a = stream(WIDTHA),
        i_b = stream(WIDTHB),
        o_stream = stream(WIDTHA + WIDTHB)
    )


#------------------------------------------------------------------------------
def test_stream_join (sim, WIDTHA = 8, WIDTHB = 4):
    """Pairs come out in order, b above a, under stalls on all three
    sides. There is no clock in the block, so the bench settles and
    looks, step by step, without an edge."""
    from isomorph.bench import StreamSource, StreamSink, run_until

    words_a = [(n * 23 + 1) & ((1 << WIDTHA) - 1) for n in range(30)]
    words_b = [(n * 5 + 2) & ((1 << WIDTHB) - 1) for n in range(30)]
    want = [(b << WIDTHA) | a for a, b in zip(words_a, words_b)]
    source_a = StreamSource(sim, 'i_a', words_a, stall = 0.3, seed = 8)
    source_b = StreamSource(sim, 'i_b', words_b, stall = 0.3, seed = 9)
    sink = StreamSink(sim, 'o_stream', stall = 0.3, seed = 10)
    run_until(sim, [source_a, source_b, sink],
              lambda: len(sink.received) == len(want), limit = 600,
              edge = False)
    assert sink.received == want, sink.received


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_stream_join, test_stream_join))
