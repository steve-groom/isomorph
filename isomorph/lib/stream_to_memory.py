"""Write a stream into a memory at ascending addresses.

A pulse on i_start with i_count set begins a run: the block takes
i_count beats from i_stream and writes them through wr at addresses
zero upwards, then drops o_busy. It does not know how deep the memory
is; i_count is the caller's promise. A start while busy is ignored,
and i_count of zero runs until the counter wraps, so give it a count.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main)
from isomorph.lib.ports import stream, ram_write


@block
def stream_to_memory (
        i_clock,
        i_reset,

        i_start,        # pulse; begins a run of i_count beats
        i_count,        # beats to take, one bit wider than wr.addr
        o_busy,

        i_stream,
        wr
    ):
    WIDTHA = len(wr.addr)

    running = signal()
    count = signal(WIDTHA + 1)
    count_inc = signal(WIDTHA + 1)
    take = signal()
    last = signal()

    @always_comb
    def write_comb ():
        i_stream.ready.next = running
        take.next = running & i_stream.valid
        count_inc.next = (count + 1)[WIDTHA:0]
        last.next = (count_inc == i_count)
        wr.we.next = take
        wr.addr.next = count[WIDTHA-1:0]
        wr.data.next = i_stream.data
        o_busy.next = running

    @always_ff (i_clock.posedge)
    def write_logic ():
        if (i_reset):
            running.next = False
        else:
            if (i_start & (not running)):
                running.next = True
                count.next = 0
            if (take):
                count.next = count_inc
            if (take & last):
                running.next = False

    return instances()


#------------------------------------------------------------------------------
def elaborate_stream_to_memory (WIDTHA = 4, WIDTHD = 8):
    """The one place the ports are built."""
    return stream_to_memory (
        i_clock = signal(),
        i_reset = signal(),

        i_start = signal(),
        i_count = signal(WIDTHA + 1),
        o_busy = signal(),

        i_stream = stream(WIDTHD),
        wr = ram_write(WIDTHA, WIDTHD)
    )


#------------------------------------------------------------------------------
class WriteLog:
    """What reached the write port, as (addr, data) per strobe."""

    def __init__ (self, sim):
        self.sim = sim
        self.writes = []

    def drive (self):
        return

    def observe (self):
        if self.sim.get('wr_we'):
            self.writes.append((self.sim.get('wr_addr'),
                                self.sim.get('wr_data')))


def test_stream_to_memory (sim, WIDTHA = 4, WIDTHD = 8):
    """A run takes exactly i_count beats, writes them at 0.., drops
    busy, and takes nothing outside a run."""
    from isomorph.bench import StreamSource, Pulse, run_until, step
    from isomorph.proto import Stream

    sim.add_clock(20e-9)
    sim.add_check(Stream(prefix = 'i_stream', reset = 'i_reset'))
    sim.reset('i_reset')

    COUNT = 10
    words = [(n * 53 + 9) & ((1 << WIDTHD) - 1) for n in range(COUNT + 4)]
    source = StreamSource(sim, 'i_stream', words, stall = 0.3, seed = 11)
    start = Pulse(sim, 'i_start')
    log = WriteLog(sim)
    drivers = [source, start, log]

    # offered but not started: nothing is taken
    step(sim, drivers, 5)
    assert source.sent == [], source.sent
    assert sim.get('o_busy') == 0

    sim.set('i_count', COUNT)
    start.fire()
    step(sim, drivers, 1)
    assert sim.get('o_busy') == 1
    run_until(sim, drivers, lambda: sim.get('o_busy') == 0, limit = 200)
    assert log.writes == list(enumerate(words[:COUNT])), log.writes
    assert source.sent == words[:COUNT], source.sent

    # and it stays idle after the run
    step(sim, drivers, 5)
    assert len(log.writes) == COUNT


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_stream_to_memory, test_stream_to_memory))
