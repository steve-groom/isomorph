"""Single-clock FIFO with first word fall through.

Pointers are one bit wider than the index, so full and empty are told
apart by the top bit and the depth is a power of two. The read is
combinational from the array, so the tool builds this from registers
or LUT RAM and the word at the head is on o_stream the cycle it is
written. DEPTH = 2 is the skid buffer: both valid and ready are
registered across it and it still passes a beat every cycle. A deep
FIFO with the registered read a block RAM wants is a later block.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main, IsomorphError)
from isomorph.lib.ports import stream


@block
def stream_fifo (
        i_clock,
        i_reset,

        i_stream,
        o_stream,

        DEPTH = 16      # words, a power of two
    ):
    if (DEPTH < 2 or DEPTH & (DEPTH - 1)):
        raise IsomorphError(f'stream_fifo: DEPTH = {DEPTH} is not a power '
                            'of two of at least 2')
    PTR = DEPTH.bit_length() - 1
    WIDTHD = len(i_stream.data)

    ram = signals(DEPTH, WIDTHD)
    wrptr = signal(PTR + 1)
    rdptr = signal(PTR + 1)
    wrptr_inc = signal(PTR + 1)
    rdptr_inc = signal(PTR + 1)
    wr_index = signal(PTR)
    rd_index = signal(PTR)
    full = signal()
    empty = signal()
    push = signal()
    pop = signal()

    @always_comb
    def fifo_comb ():
        wr_index.next = wrptr[PTR-1:0]
        rd_index.next = rdptr[PTR-1:0]
        # the pointers meet from behind when full, level when empty
        full.next = (wrptr[PTR] != rdptr[PTR]) & (wr_index == rd_index)
        empty.next = (wrptr == rdptr)
        i_stream.ready.next = (not full)
        o_stream.valid.next = (not empty)
        o_stream.data.next = ram[rd_index]
        push.next = i_stream.valid & (not full)
        pop.next = o_stream.ready & (not empty)
        wrptr_inc.next = (wrptr + 1)[PTR:0]
        rdptr_inc.next = (rdptr + 1)[PTR:0]

    @always_ff (i_clock.posedge)
    def ptr_logic ():
        if (i_reset):
            wrptr.next = 0
            rdptr.next = 0
        else:
            if (push):
                wrptr.next = wrptr_inc
            if (pop):
                rdptr.next = rdptr_inc

    @always_ff (i_clock.posedge)
    def ram_logic ():
        if (push):
            ram[wr_index].next = i_stream.data

    return instances()


#------------------------------------------------------------------------------
def elaborate_stream_fifo (WIDTHD = 8, DEPTH = 4):
    """The one place the ports are built."""
    return stream_fifo (
        i_clock = signal(),
        i_reset = signal(),

        i_stream = stream(WIDTHD),
        o_stream = stream(WIDTHD),

        DEPTH = DEPTH
    )


#------------------------------------------------------------------------------
def test_stream_fifo (sim, WIDTHD = 8, DEPTH = 4):
    """Order kept under stalls; fills to DEPTH and no further; a beat
    every cycle once running with no stalls."""
    from isomorph.bench import StreamSource, StreamSink, run_until, step
    from isomorph.proto import Stream

    sim.add_clock(20e-9)
    sim.add_check(Stream(prefix = 'i_stream', reset = 'i_reset'))
    sim.add_check(Stream(prefix = 'o_stream', reset = 'i_reset'))
    sim.reset('i_reset')

    words = [(n * 41 + 3) & ((1 << WIDTHD) - 1) for n in range(50)]
    source = StreamSource(sim, 'i_stream', words, stall = 0.4, seed = 3)
    sink = StreamSink(sim, 'o_stream', stall = 0.4, seed = 4)
    run_until(sim, [source, sink],
              lambda: len(sink.received) == len(words), limit = 600)
    assert sink.received == words, sink.received

    # fill with the sink stalled: DEPTH go in, then it is full. The
    # source offers exactly that many, so it is not left holding a
    # word when the sink below is swapped for one that takes
    source = StreamSource(sim, 'i_stream', words[:DEPTH + 1])
    sink = StreamSink(sim, 'o_stream', stall = lambda cycle: True)
    step(sim, [source, sink], DEPTH + 3)
    assert len(source.sent) == DEPTH, source.sent
    assert sim.get('i_stream_ready') == 0

    # let the last offered word in and drain, and it empties
    sink = StreamSink(sim, 'o_stream')
    step(sim, [source, sink], DEPTH + 2)
    assert sink.received == words[:DEPTH + 1], sink.received
    assert sim.get('o_stream_valid') == 0

    # full rate: a word a cycle, one cycle of latency into the array
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
    sys.exit(main(elaborate_stream_fifo, test_stream_fifo))
