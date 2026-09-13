"""Simple dual-port RAM: one write port, one registered read port.

This is the shape every vendor infers a block RAM from: a signals()
array written under the clock through wr, and read into a register
under the same clock through rd, with no enable on the read. The word
at rd.addr is on rd.data the cycle after. A read of the address being
written that cycle returns the old word, which is what the array
semantics say and what the M9K and the like do in their default mode.
"""
from isomorph import (block, signal, signals, enum, always_ff,
    always_ff_async_reset, always_comb, assign, concat, replicate, bits,
    struct, attr, open_port, instances, main, IsomorphError)
from isomorph.lib.ports import ram_read, ram_write


@block
def ram_block (
        i_clock,

        wr,
        rd,

        DEPTH = 16      # words
    ):
    if (DEPTH > (1 << len(rd.addr))):
        raise IsomorphError(f'ram_block: DEPTH = {DEPTH} does not fit '
                            f'{len(rd.addr)} address bits')
    WIDTHD = len(wr.data)

    mem = signals(DEPTH, WIDTHD)

    @always_ff (i_clock.posedge)
    def ram_logic ():
        if (wr.we):
            mem[wr.addr].next = wr.data
        rd.data.next = mem[rd.addr]

    return instances()


#------------------------------------------------------------------------------
def elaborate_ram_block (WIDTHA = 4, WIDTHD = 8):
    """The one place the ports are built."""
    return ram_block (
        i_clock = signal(),

        wr = ram_write(WIDTHA, WIDTHD),
        rd = ram_read(WIDTHA, WIDTHD),

        DEPTH = 1 << WIDTHA
    )


#------------------------------------------------------------------------------
def test_ram_block (sim, WIDTHA = 4, WIDTHD = 8):
    """Every word reads back the cycle after its address, and a read
    of the word being written sees the old one."""
    sim.add_clock(20e-9)
    mask = (1 << WIDTHD) - 1
    image = [(a * 37 + 11) & mask for a in range(1 << WIDTHA)]

    sim.set('wr_we', 1)
    for a, word in enumerate(image):
        sim.set('wr_addr', a)
        sim.set('wr_data', word)
        sim.tick(1)
    sim.set('wr_we', 0)

    for a, word in enumerate(image):
        sim.set('rd_addr', a)
        sim.tick(1)
        assert sim.get('rd_data') == word, (a, sim.get('rd_data'))

    # read-during-write returns the word from before the write
    sim.set('wr_we', 1)
    sim.set('wr_addr', 3)
    sim.set('wr_data', (~image[3]) & mask)
    sim.set('rd_addr', 3)
    sim.tick(1)
    assert sim.get('rd_data') == image[3]
    sim.set('wr_we', 0)
    sim.tick(1)
    assert sim.get('rd_data') == (~image[3]) & mask


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_ram_block, test_ram_block))
