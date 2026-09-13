"""Forward register slice: valid and data registered, ready passed
through.

One flop per data bit and one for valid, and a beat every cycle. The
ready path from o_stream back to i_stream is combinational, so this
cuts the forward path only. A stall that has to be registered as well
is stream_fifo with DEPTH = 2, which is the skid buffer.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main)
from isomorph.lib.ports import stream


@block
def stream_pipe (
        i_clock,
        i_reset,

        i_stream,
        o_stream
    ):

    @always_comb
    def pipe_comb ():
        # room while the register is empty or being drained
        i_stream.ready.next = (not o_stream.valid) | o_stream.ready

    @always_ff (i_clock.posedge)
    def pipe_logic ():
        if (i_reset):
            o_stream.valid.next = False
        else:
            if (i_stream.ready):
                o_stream.valid.next = i_stream.valid
                o_stream.data.next = i_stream.data

    return instances()


#------------------------------------------------------------------------------
def elaborate_stream_pipe (WIDTHD = 8):
    """The one place the ports are built."""
    return stream_pipe (
        i_clock = signal(),
        i_reset = signal(),

        i_stream = stream(WIDTHD),
        o_stream = stream(WIDTHD)
    )


#------------------------------------------------------------------------------
def test_stream_pipe (sim, WIDTHD = 8):
    """Everything in comes out once, in order, under stalls on both
    sides; and with no stalls a beat goes through every cycle."""
    from isomorph.bench import StreamSource, StreamSink, run_until
    from isomorph.proto import Stream

    sim.add_clock(20e-9)
    sim.add_check(Stream(prefix = 'i_stream', reset = 'i_reset'))
    sim.add_check(Stream(prefix = 'o_stream', reset = 'i_reset'))
    sim.reset('i_reset')

    words = [(n * 37 + 5) & ((1 << WIDTHD) - 1) for n in range(40)]
    source = StreamSource(sim, 'i_stream', words, stall = 0.3, seed = 1)
    sink = StreamSink(sim, 'o_stream', stall = 0.3, seed = 2)
    run_until(sim, [source, sink],
              lambda: len(sink.received) == len(words), limit = 400)
    assert sink.received == words, sink.received

    source = StreamSource(sim, 'i_stream', words)
    sink = StreamSink(sim, 'o_stream')
    taken = run_until(sim, [source, sink],
                      lambda: len(sink.received) == len(words),
                      limit = 100)
    assert sink.received == words, sink.received
    assert taken <= len(words) + 1, f'{taken} cycles for {len(words)}'


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_stream_pipe, test_stream_pipe))
