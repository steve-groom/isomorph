from isomorph import (block, signal, signals, enum, always_ff,
    always_comb, assign, concat, replicate, bits, struct,
    attr, open_port, instances)

'''Mini construct core: part-select, match/bits, replicate, FSM.

Used by tests/test_emit_sv.py. Not a processor.
'''

@block
def mini_core (
        i_clock,
        i_reset,

        i_word,
        i_a,
        i_b,
        o_q,
        o_done,

        WIDTH = 8
    ):
    ZERO = 0

    state = enum('IDLE', 'RUN', 'DONE')
    fsm = signal(state)

    acc = signal(WIDTH)
    sum_w = signal(WIDTH)
    ext = signal(WIDTH)
    slot = signal(2)
    instr = signal(4)
    op_add = signal()
    op_ext = signal()

    assign(sum_w, lambda: (i_a + i_b)[WIDTH-1:0])
    assign(o_done, lambda: fsm == state.DONE)

    @always_comb
    def decode_comb ():
        # nibble from the current slot
        instr.next = i_word[slot*4 + 3:slot*4]
        op_add.next = False
        op_ext.next = False
        match instr:
            case bits('1???'):
                op_add.next = True
            case bits('01??'):
                op_ext.next = True
            case _:
                pass
        ext.next = concat(replicate(i_a[WIDTH-1], WIDTH - 4), i_a[3:0])
        o_q.next = acc
        if (op_add):
            o_q.next = sum_w
        if (op_ext):
            o_q.next = ext

    @always_ff (i_clock.posedge)
    def fsm_logic ():
        if (i_reset):
            fsm.next = state.IDLE
            acc.next = ZERO
            slot.next = 0
        else:
            if (fsm == state.IDLE):
                acc.next = i_a
                fsm.next = state.RUN
            if (fsm == state.RUN):
                acc.next = o_q
                fsm.next = state.DONE
            if (fsm == state.DONE):
                fsm.next = state.IDLE

    return instances()


#------------------------------------------------------------------------------
def elaborate_mini_core ():
    """The one place the ports are built."""
    return mini_core (
        i_clock = signal(),
        i_reset = signal(),

        i_word = signal(16),
        i_a = signal(8),
        i_b = signal(8),
        o_q = signal(8),
        o_done = signal()
    )


#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    from isomorph import convert

    convert(elaborate_mini_core())
