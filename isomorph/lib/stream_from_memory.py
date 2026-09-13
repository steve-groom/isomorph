"""Read a memory at ascending addresses and present it as a stream.

A pulse on i_start with i_count set begins a run: the block reads
i_count words through rd from address zero upwards and offers each on
o_stream, in order, under whatever backpressure the sink applies.
o_busy holds until the last word has left.

The read port answers one cycle after the address, so a word issued
this cycle lands next cycle whether or not the sink is ready then. It
lands in a two-deep stream_fifo, and a credit counter holds the number
of fifo slots not yet spoken for: a read is issued only while a credit
is in hand, a credit is spent on issue and returned when a word leaves
the fifo, or spent in the same cycle one is handed back, which is
what keeps a word a cycle moving once the sink is free. So there is
always room, which the assert says.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main)
from isomorph.lib.ports import stream, ram_read
from isomorph.lib.stream_fifo import stream_fifo


@block
def stream_from_memory (
        i_clock,
        i_reset,

        i_start,        # pulse; begins a run of i_count words
        i_count,        # words to read, one bit wider than rd.addr
        o_busy,

        rd,
        o_stream
    ):
    WIDTHA = len(rd.addr)
    WIDTHD = len(rd.data)

    running = signal()
    count = signal(WIDTHA + 1)
    count_inc = signal(WIDTHA + 1)
    issue = signal()
    last = signal()
    pending = signal()          # a read went out last cycle
    credit = signal(2)          # fifo slots not yet spoken for, 0 to 2
    credit_inc = signal(2)
    credit_dec = signal(2)
    credit_nxt = signal(2)
    pop = signal()
    fill = stream(WIDTHD)       # the read data, into the fifo

    inst_fifo = stream_fifo (
        i_clock = i_clock,
        i_reset = i_reset,

        i_stream = fill,
        o_stream = o_stream,

        DEPTH = 2
    )

    #--------------------------------------------------------------------------
    # the return path: its own process, reading only registers, so
    # that the wire into the fifo does not come out of the process
    # that reads the fifo's outputs. Verilator sees a process as one
    # node, and that shape is a loop to it though no bit goes round
    @always_comb
    def fill_comb ():
        fill.valid.next = pending
        fill.data.next = rd.data

    #--------------------------------------------------------------------------
    # the issue side: addresses out, credits kept
    @always_comb
    def read_comb ():
        count_inc.next = (count + 1)[WIDTHA:0]
        last.next = (count_inc == i_count)
        pop.next = o_stream.valid & o_stream.ready
        # a credit in hand, or one being handed back this cycle
        issue.next = running & ((credit != 0) | pop)
        rd.addr.next = count[WIDTHA-1:0]
        assert (not fill.valid) | fill.ready, 'a read landed with no room'
        credit_inc.next = (credit + 1)[1:0]
        credit_dec.next = (credit - 1)[1:0]
        credit_nxt.next = credit
        if (issue & (not pop)):
            credit_nxt.next = credit_dec
        if (pop & (not issue)):
            credit_nxt.next = credit_inc
        # busy until the last word has left the fifo
        o_busy.next = running | (credit != 2)

    @always_ff (i_clock.posedge)
    def read_logic ():
        if (i_reset):
            running.next = False
            pending.next = False
            credit.next = 2
        else:
            pending.next = issue
            credit.next = credit_nxt
            if (i_start & (not running)):
                running.next = True
                count.next = 0
            if (issue):
                count.next = count_inc
            if (issue & last):
                running.next = False

    return instances()


#------------------------------------------------------------------------------
def elaborate_stream_from_memory (WIDTHA = 4, WIDTHD = 8):
    """The one place the ports are built."""
    return stream_from_memory (
        i_clock = signal(),
        i_reset = signal(),

        i_start = signal(),
        i_count = signal(WIDTHA + 1),
        o_busy = signal(),

        rd = ram_read(WIDTHA, WIDTHD),
        o_stream = stream(WIDTHD)
    )


#------------------------------------------------------------------------------
class ReadModel:
    """A memory behind the read port, with the one-cycle latency: the
    address seen before this edge is the word offered after it."""

    def __init__ (self, sim, image):
        self.sim = sim
        self.image = list(image)
        self.addr = 0

    def drive (self):
        self.sim.set('rd_data', self.image[self.addr])

    def observe (self):
        self.addr = self.sim.get('rd_addr')


def test_stream_from_memory (sim, WIDTHA = 4, WIDTHD = 8):
    """A run delivers words 0 to i_count - 1 in order under stalls,
    then drops busy; a free sink gets a word a cycle."""
    from isomorph.bench import StreamSink, Pulse, run_until, step
    from isomorph.proto import Stream

    sim.add_clock(20e-9)
    sim.add_check(Stream(prefix = 'o_stream', reset = 'i_reset'))
    sim.reset('i_reset')

    image = [(a * 37 + 11) & ((1 << WIDTHD) - 1)
             for a in range(1 << WIDTHA)]
    COUNT = 12
    memory = ReadModel(sim, image)
    start = Pulse(sim, 'i_start')
    sink = StreamSink(sim, 'o_stream', stall = 0.4, seed = 12)
    drivers = [memory, start, sink]

    step(sim, drivers, 3)
    assert sim.get('o_busy') == 0
    assert sink.received == []

    sim.set('i_count', COUNT)
    start.fire()
    step(sim, drivers, 1)
    assert sim.get('o_busy') == 1
    run_until(sim, drivers, lambda: sim.get('o_busy') == 0, limit = 300)
    assert sink.received == image[:COUNT], sink.received

    # a second run, sink never stalling: one word a cycle
    sink = StreamSink(sim, 'o_stream')
    drivers = [memory, start, sink]
    start.fire()
    step(sim, drivers, 1)
    taken = run_until(sim, drivers, lambda: sim.get('o_busy') == 0,
                      limit = 100)
    assert sink.received == image[:COUNT], sink.received
    assert taken <= COUNT + 2, f'{taken} cycles for {COUNT}'


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_stream_from_memory, test_stream_from_memory))
