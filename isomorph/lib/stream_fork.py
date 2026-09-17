"""One beat to two outputs.

Each output takes the beat once, in whichever order the two are ready,
and the input advances when both have. Two flops remember which side
has taken the current beat. Cascade for more than two; an N-way form
waits on arrays of bundle ports.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main)
from isomorph.lib.ports import stream


@block
def stream_fork (
        i_clock,
        i_reset,

        i_stream,
        o_a,
        o_b
    ):

    sent_a = signal()       # o_a has taken the beat now on i_stream
    sent_b = signal()
    take_a = signal()
    take_b = signal()
    done = signal()

    @always_comb
    def fork_comb ():
        o_a.valid.next = i_stream.valid & (not sent_a)
        o_b.valid.next = i_stream.valid & (not sent_b)
        o_a.data.next = i_stream.data
        o_b.data.next = i_stream.data
        take_a.next = o_a.valid & o_a.ready
        take_b.next = o_b.valid & o_b.ready
        # the beat is finished when each side has it, now or already
        done.next = (sent_a | take_a) & (sent_b | take_b)
        i_stream.ready.next = done

    @always_ff (i_clock.posedge)
    def fork_logic ():
        if (i_reset):
            sent_a.next = False
            sent_b.next = False
        else:
            sent_a.next = (sent_a | take_a) & (not done)
            sent_b.next = (sent_b | take_b) & (not done)

    return instances()


#------------------------------------------------------------------------------
def elaborate_stream_fork (WIDTHD = 8):
    """The one place the ports are built."""
    return stream_fork (
        i_clock = signal(),
        i_reset = signal(),

        i_stream = stream(WIDTHD),
        o_a = stream(WIDTHD),
        o_b = stream(WIDTHD)
    )


#------------------------------------------------------------------------------
def test_stream_fork (sim, WIDTHD = 8):
    """Both sides see every word once, in order, whatever the stalls."""
    from isomorph.bench import StreamSource, StreamSink, run_until
    from isomorph.proto import Stream

    sim.add_clock(20e-9)
    sim.add_check(Stream(prefix = 'i_stream', reset = 'i_reset'))
    sim.add_check(Stream(prefix = 'o_a', reset = 'i_reset'))
    sim.add_check(Stream(prefix = 'o_b', reset = 'i_reset'))
    sim.reset('i_reset')

    words = [(n * 29 + 7) & ((1 << WIDTHD) - 1) for n in range(40)]
    source = StreamSource(sim, 'i_stream', words, stall = 0.3, seed = 5)
    sink_a = StreamSink(sim, 'o_a', stall = 0.5, seed = 6)
    sink_b = StreamSink(sim, 'o_b', stall = 0.5, seed = 7)
    run_until(sim, [source, sink_a, sink_b],
              lambda: (len(sink_a.received) == len(words)
                       and len(sink_b.received) == len(words)),
              limit = 800)
    assert sink_a.received == words, sink_a.received
    assert sink_b.received == words, sink_b.received

    # with both sides free it is a wire: a word a cycle
    source = StreamSource(sim, 'i_stream', words)
    sink_a = StreamSink(sim, 'o_a')
    sink_b = StreamSink(sim, 'o_b')
    taken = run_until(sim, [source, sink_a, sink_b],
                      lambda: len(sink_b.received) == len(words),
                      limit = 100)
    assert taken <= len(words), f'{taken} cycles for {len(words)}'


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_stream_fork, test_stream_fork))
