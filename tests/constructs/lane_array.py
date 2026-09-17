from isomorph import (block, signal, signals, enum, always_ff,
    always_comb, assign, concat, replicate, bits, struct,
    attr, open_port, instances, main)

'''An array of instances: one child repeated, emitted as a generate.

Every lane takes the same clock and element k of the two arrays, which
is the one shape a loop over instances is allowed to have. The
emitted SystemVerilog declares a genvar and one generate block, the
VHDL a for ... generate, and the C99 an array of child structs.
'''

LANES = 4


@block
def lane (
        i_clock,
        i_reset,

        i_d,
        o_q,

        WIDTH = 8
    ):
    # one flop, so the array has something to hold

    @always_ff (i_clock.posedge)
    def lane_logic ():
        if (i_reset):
            o_q.next = 0
        else:
            o_q.next = i_d

    return instances()


@block
def lane_array (
        i_clock,
        i_reset,

        i_data,
        o_data
    ):
    cells = []
    for lane_index in range(LANES):
        cells.append(lane (
            i_clock = i_clock,
            i_reset = i_reset,

            i_d = i_data[lane_index],
            o_q = o_data[lane_index],

            WIDTH = 8
        ))

    return instances()


#------------------------------------------------------------------------------
def elaborate_lane_array ():
    """The one place the ports are built."""
    return lane_array (
        i_clock = signal(),
        i_reset = signal(),

        i_data = signals(LANES, 8),
        o_data = signals(LANES, 8)
    )


#------------------------------------------------------------------------------
def test_lane_array (sim):
    """Each lane carries its own element and no other."""
    sim.add_clock(20e-9)
    sim.reset('i_reset')

    for k in range(LANES):
        sim.set(f'i_data[{k}]', 10 + k * 5)
    sim.tick(1)
    for k in range(LANES):
        assert sim.get(f'o_data[{k}]') == 10 + k * 5, k

    # one lane moves and the others hold their own value
    sim.set('i_data[2]', 0xff)
    sim.tick(1)
    assert sim.get('o_data[2]') == 0xff
    assert sim.get('o_data[0]') == 10
    assert sim.get('o_data[3]') == 25


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    import sys
    sys.exit(main(elaborate_lane_array, test_lane_array))
