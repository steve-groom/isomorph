"""Data through a memory and a pipeline of stream blocks.

A write stream fills a ram_block through stream_to_memory. A start
pulse then has stream_from_memory read it back through a pipeline of
a gain stage, a register slice and a FIFO to the output stream, with
the sink stalling as it likes. Every block in the chain is a named
instance, every wire between them is named by its path, and the
bench runs on all three simulators and lints in both languages.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main)
from isomorph.ifaces import stream, ram_read, ram_write
from isomorph.lib import (ram_block, stream_to_memory, stream_from_memory,
    stream_pipe, stream_fifo, pipeline)


@block
def gain_stage (
        i_stream,
        o_stream,

        GAIN = 3        # the multiplier; the product is truncated
    ):
    """A combinational stage: data times a constant, modulo the width.
    ready and valid pass straight through."""
    WIDTHD = len(o_stream.data)
    product = signal(WIDTHD)

    @always_comb
    def gain_comb ():
        product.next = (i_stream.data * GAIN)[WIDTHD-1:0]
        o_stream.valid.next = i_stream.valid
        o_stream.data.next = product
        i_stream.ready.next = o_stream.ready

    return instances()


@block
def dataflow (
        i_clock,
        i_reset,

        i_write,        # the stream that fills the memory
        i_write_start,  # pulse; take i_count beats from i_write
        i_read_start,   # pulse; read i_count words back out
        i_count,
        o_busy,

        o_read          # the memory contents, through the pipeline
    ):
    WIDTHA = 4
    WIDTHD = len(i_write.data)

    wr = ram_write(WIDTHA, WIDTHD)
    rd = ram_read(WIDTHA, WIDTHD)
    read_out = stream(WIDTHD)
    write_busy = signal()
    read_busy = signal()

    inst_ram = ram_block (
        i_clock = i_clock,

        wr = wr,
        rd = rd,

        DEPTH = 1 << WIDTHA
    )

    inst_writer = stream_to_memory (
        i_clock = i_clock,
        i_reset = i_reset,

        i_start = i_write_start,
        i_count = i_count,
        o_busy = write_busy,

        i_stream = i_write,
        wr = wr
    )

    inst_reader = stream_from_memory (
        i_clock = i_clock,
        i_reset = i_reset,

        i_start = i_read_start,
        i_count = i_count,
        o_busy = read_busy,

        rd = rd,
        o_stream = read_out
    )

    # the pipeline: instances chain_gain, chain_pipe, chain_fifo, and
    # the links chain_gain_out and chain_pipe_out between them
    chain = pipeline(read_out, o_read, [
        ('gain', gain_stage, {'GAIN': 3}),
        ('pipe', stream_pipe),
        ('fifo', stream_fifo, {'DEPTH': 4}),
    ], i_clock, i_reset)

    assign(o_busy, lambda: write_busy | read_busy)

    return instances()


#------------------------------------------------------------------------------
def elaborate_dataflow (WIDTHD = 8):
    """The one place the ports are built."""
    return dataflow (
        i_clock = signal(),
        i_reset = signal(),

        i_write = stream(WIDTHD),
        i_write_start = signal(),
        i_read_start = signal(),
        i_count = signal(5),
        o_busy = signal(),

        o_read = stream(WIDTHD)
    )


#------------------------------------------------------------------------------
def test_dataflow (sim, WIDTHD = 8):
    """Fill the memory from a stalling source, read it back through
    the pipeline into a stalling sink, and the sink holds the image
    times the gain."""
    from isomorph.bench import (StreamSource, StreamSink, Pulse, step,
                                run_until)
    from isomorph.proto import Stream

    sim.add_clock(20e-9)
    sim.add_check(Stream(prefix = 'i_write', reset = 'i_reset'))
    sim.add_check(Stream(prefix = 'o_read', reset = 'i_reset'))
    sim.reset('i_reset')

    COUNT = 14
    mask = (1 << WIDTHD) - 1
    image = [(n * 47 + 13) & mask for n in range(COUNT)]
    source = StreamSource(sim, 'i_write', image, stall = 0.3, seed = 21)
    sink = StreamSink(sim, 'o_read', stall = 0.3, seed = 22)
    write_start = Pulse(sim, 'i_write_start')
    read_start = Pulse(sim, 'i_read_start')
    drivers = [source, sink, write_start, read_start]

    sim.set('i_count', COUNT)
    write_start.fire()
    step(sim, drivers, 1)
    run_until(sim, drivers, lambda: sim.get('o_busy') == 0, limit = 200)
    assert source.done, source.pending

    read_start.fire()
    step(sim, drivers, 1)
    run_until(sim, drivers, lambda: sim.get('o_busy') == 0, limit = 200)
    run_until(sim, drivers, lambda: len(sink.received) == COUNT,
              limit = 50)
    assert sink.received == [(w * 3) & mask for w in image], sink.received


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_dataflow, test_dataflow))
