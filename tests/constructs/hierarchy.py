from types import SimpleNamespace

from isomorph import (block, signal, signals, enum, always_ff,
    always_comb, assign, concat, replicate, bits, struct,
    attr, open_port, instances)

'''Two-level design: parent instantiates add2 twice. The SV must keep
both modules and the named instances; nothing is flattened.
'''

@block
def add2 (
        i_a,
        i_b,
        o_sum,

        WIDTH
    ):
    # WIDTH-bit add, carry dropped by an explicit slice.

    @always_comb
    def sum_comb ():
        o_sum.next = (i_a + i_b)[WIDTH-1:0]

    return instances()


@block
def parent (
        i_x,
        i_y,
        bus,
        o_z,
        WIDTH
    ):
    mid = signal(WIDTH)

    # first adder: i_x + i_y
    inst_a = add2 (
        i_a = i_x,
        i_b = i_y,
        o_sum = mid,
        WIDTH = WIDTH
    )
    # second adder: mid + bus.data
    inst_b = add2 (
        i_a = mid,
        i_b = bus.data,
        o_sum = o_z,
        WIDTH = WIDTH
    )
    assign(bus.ack, lambda: o_z[0])
    return instances()


def elaborate_parent ():
    WIDTH = 6
    bus = SimpleNamespace(
        data = signal(WIDTH),
        ack = signal(WIDTH)
    )
    return parent (
        WIDTH = 6,
        i_x = signal(WIDTH),
        i_y = signal(WIDTH),
        bus = bus,
        o_z = signal(WIDTH)
    )


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    from isomorph import convert
    convert(elaborate_parent())
