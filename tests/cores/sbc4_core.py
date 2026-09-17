import math
import sys

from isomorph import (block, signal, signals, enum, always_ff,
    always_comb, assign, concat, replicate, bits, struct,
    attr, open_port, instances)

'''
The SBC4 core migrated by hand to isomorph (see NOTES.txt for every
change the width rules forced).

0nnn    pfx n ( x -- if(pf){x=(x<<3)|n}else{x y=signed(n),pf=1} )
1000    ldw ( address -- value ), pf=0
1001    stw ( value address -- ), pf=0
1010    dup ( x -- x x ), pf=0
1011    drp ( x -- ), pf=0
1100    swp ( x y -- y x ), pf=0
1101    adc ( x y -- sum carry ), pf=0
1110    axl ( a b -- a&b a^b ), pf=0
1111    jnz ( cond target -- return_address ), pf=0
'''

@block
def sbc4_core (
        i_clock,
        i_clock_sreset,

        i_irq,
        avmm,

        STACK_POINTER_OPS = True   # PFX 4/5 DRP as SPR/SPW; else shifts
    ):

    RESET_ADDRESS = 0x10
    STACK_ADDRESS = 0x3ff
    IRQ_ADDRESS = 0x8

    WIDTHA = len(avmm.address)
    WIDTHD = max(len(avmm.readdata), len(avmm.writedata))
    OPCODES = int(math.ceil(WIDTHD / 4))
    WIDTHS = int(math.ceil(math.log2(OPCODES)))
    WIDTHT = (WIDTHA + WIDTHS)

    state = enum('RESET', 'FETCH', 'EXECUTE', 'DROP')
    fsm = signal(state)

    pc = signal(WIDTHA)
    sp = signal(WIDTHA)
    slot = signal(WIDTHS)
    rt = signal(WIDTHD)
    rn = signal(WIDTHD)
    pfx_flag = signal()
    swp_flag = signal(2)
    drp_flag = signal()
    irq_flag = signal()

    (op_pfx, op_ldw, op_stw, op_dup, op_drp, op_swp,
        op_adc, op_axl, op_jnz) = [signal() for _ in range(9)]
    op_asr = signal()
    is_fetch = signal()
    is_execute = signal()
    op_spr = signal()
    op_spw = signal()
    op_not = signal()
    op_inz = signal()
    op_push = signal()
    op_pop = signal()
    instr = signal(4)
    opcode_word = signal(WIDTHD)
    decode = signal(8)
    opcode_complete = signal()
    imm_opcode = signal()
    rn_zero = signal()
    last_slot = signal()
    next_slot = signal(WIDTHS)
    next_pc = signal(WIDTHA)
    next_sp = signal(WIDTHA)
    addend = signal(WIDTHD)
    adder = signal(WIDTHD + 1)
    alu_result = signal(WIDTHD)
    sp_inc = signal(WIDTHA)
    irq_event = signal()
    next_rt = signal(WIDTHD)
    next_rn = signal(WIDTHD)
    shift_msbit = signal()
    shift_left = signal(WIDTHD)
    shift_right = signal(WIDTHD)

    assign(avmm.writedata, lambda: rn)
    assign(rn_zero, lambda: rn == 0)
    assign(is_fetch, lambda: fsm == state.FETCH)
    assign(is_execute, lambda: fsm == state.EXECUTE)

    @always_comb
    def precalc_comb ():
        last_slot.next = (slot == OPCODES - 1) & (not is_fetch)
        next_slot.next = (slot + 1)[WIDTHS-1:0]
        next_pc.next = (pc + last_slot)[WIDTHA-1:0]
        if (last_slot):
            next_slot.next = 0
        if (is_fetch):
            next_slot.next = slot

    @always_comb
    def decode_comb ():
        instr.next = opcode_word[slot*4 + 3:slot*4]
        decode.next = 0
        decode.next[instr[2:0]] = instr[3]
        op_pfx.next = (not instr[3])
        op_ldw.next = decode[0]
        op_stw.next = decode[1]
        op_dup.next = decode[2]
        op_drp.next = (decode[3] & (not pfx_flag))
        op_swp.next = (decode[4] & (not pfx_flag))
        op_adc.next = decode[5]
        op_axl.next = decode[6]
        op_jnz.next = decode[7]

        op_asr.next = (decode[3] & pfx_flag & (not rt[2]))
        op_spr.next = False
        op_spw.next = False
        if (STACK_POINTER_OPS):
            op_spr.next = (decode[3] & pfx_flag & rt[2] & (not rt[0]))
            op_spw.next = (decode[3] & pfx_flag & rt[2] & rt[0] &
                (not rt[1]))
        else:
            op_asr.next = (decode[3] & pfx_flag &
                (not (rt[2] & rt[1] & rt[0])))
        op_not.next = (decode[3] & pfx_flag & rt[2] & rt[0] & rt[1])
        op_inz.next = (decode[4] & pfx_flag)
        op_push.next = (op_dup | (op_pfx & (not pfx_flag)))
        op_pop.next = (op_drp | op_asr | op_not | op_jnz)

        sp_inc.next = 1
        if (op_push | is_fetch):
            sp_inc.next = replicate(True, WIDTHA)
        next_sp.next = (sp + sp_inc)[WIDTHA-1:0]

        imm_opcode.next = ((op_pfx & pfx_flag) | op_swp | op_adc | op_inz |
            op_spr | op_spw | op_axl)
        opcode_complete.next = (imm_opcode |
              ((avmm.read | avmm.write) & (not avmm.waitrequest)))

        irq_event.next = (i_irq & (not (irq_flag | pfx_flag |
            (swp_flag != 0) | avmm.read)))

    @always_comb
    def alu_comb ():
        addend.next = rt
        if (op_inz):
            addend.next = 0
            if (WIDTHT < WIDTHD):
                addend.next = concat(rt[WIDTHD-1:WIDTHT],
                                     replicate(False, WIDTHT))
        adder.next = (rn + addend + op_inz)[WIDTHD:0]
        shift_msbit.next = (rn[WIDTHD-1] & (not rt[0])) | \
            (rn[0] & rt[0] & rt[1])
        shift_left.next = concat(rn[WIDTHD-2:0], False)
        shift_right.next = concat(shift_msbit, rn[WIDTHD-1:1])
        alu_result.next = shift_left if (rt[1] & (not rt[0])) else shift_right
        if (op_axl | op_not):
            alu_result.next = (rt ^ rn)
        if (op_adc):
            alu_result.next = adder[WIDTHD]
        if (op_spr):
            alu_result.next = sp

    @always_comb
    def fsm_comb ():
        next_rt.next = rn
        if (op_pfx):
            next_rt.next = concat(replicate(instr[2], WIDTHD - 3), instr[2:0])
            if (pfx_flag):
                next_rt.next = concat(rt[WIDTHD-4:0], instr[2:0])
        if (op_ldw):
            next_rt.next = avmm.readdata
        if (op_axl | op_adc | op_asr | op_spr | op_not):
            next_rt.next = alu_result
        if ((op_jnz & (not swp_flag[1]) & is_execute) | is_fetch):
            next_rt.next = concat(next_pc, next_slot)

        next_rn.next = avmm.readdata
        if ((op_adc | op_inz) & is_execute):
            next_rn.next = adder[WIDTHD-1:0]
        if (op_axl & is_execute):
            next_rn.next = (rt & rn)
        if (((op_swp | op_push) & is_execute) | is_fetch):
            next_rn.next = rt

    @always_ff (i_clock.posedge)
    def fsm_logic ():
        if (i_clock_sreset):
            fsm.next = state.RESET
            avmm.read.next = False
            avmm.write.next = False
            slot.next = 0
            pfx_flag.next = False
            swp_flag.next = 0
            drp_flag.next = False
        else:
            #------------------------------------------------------------------
            if (fsm == state.RESET):
                pc.next = RESET_ADDRESS
                sp.next = STACK_ADDRESS
                pfx_flag.next = False
                swp_flag.next = 0
                fsm.next = state.FETCH
            #------------------------------------------------------------------
            if (fsm == state.FETCH):
                opcode_word.next = avmm.readdata
                if (irq_event):
                    avmm.address.next = sp
                    if (avmm.write):
                        if (not avmm.waitrequest):
                            pc.next = IRQ_ADDRESS
                            slot.next = 0
                            avmm.write.next = False
                            irq_flag.next = True
                            rt.next = next_rt
                            rn.next = next_rn
                            sp.next = next_sp
                    else:
                        avmm.write.next = True
                else:
                    avmm.address.next = pc
                    avmm.read.next = True
                    if (avmm.read & (not avmm.waitrequest)):
                        avmm.read.next = False
                        fsm.next = state.EXECUTE
            #------------------------------------------------------------------
            if (fsm == state.EXECUTE):
                if (op_ldw | op_stw):
                    avmm.address.next = rt[WIDTHA-1:0]
                if (op_pfx | op_dup):
                    avmm.address.next = sp
                if (op_pop):
                    avmm.address.next = next_sp
                if (not (avmm.read | avmm.write)):
                    avmm.read.next = (op_ldw | op_pop)
                    avmm.write.next = ((op_pfx & (not pfx_flag)) | op_stw |
                        op_dup)

                drp_flag.next = op_stw
                if (opcode_complete):
                    avmm.read.next = False
                    avmm.write.next = False
                    pfx_flag.next = (op_pfx | op_inz)
                    swp_flag.next = concat((swp_flag[0] & op_swp), op_swp)
                    if (not (op_dup | op_stw | op_inz)):
                        rt.next = next_rt
                    if (op_swp | op_push | op_adc | op_pop | op_inz |
                            op_axl):
                        rn.next = next_rn
                    if (op_push | op_pop):
                        sp.next = next_sp
                    if (op_spw):
                        sp.next = rn[WIDTHA-1:0]

                    if (op_jnz):
                        if (rn_zero & (not swp_flag[1])):
                            fsm.next = state.DROP
                        else:
                            pc.next = rt[WIDTHT-1:WIDTHS]
                            slot.next = rt[WIDTHS-1:0]
                            if (swp_flag[1]):
                                irq_flag.next = False
                            fsm.next = state.FETCH

                    else:
                        if (op_stw | op_inz | op_spw):
                            fsm.next = state.DROP
                        else:
                            slot.next = next_slot
                            pc.next = next_pc
                            if (last_slot):
                                fsm.next = state.FETCH

            #------------------------------------------------------------------
            if (fsm == state.DROP):
                avmm.address.next = next_sp
                if (avmm.read & (not avmm.waitrequest)):
                    avmm.read.next = False
                    drp_flag.next = False
                    pfx_flag.next = False
                    sp.next = next_sp
                    rt.next = next_rt
                    rn.next = next_rn
                    if (not (drp_flag | (op_inz & rn_zero))):
                        slot.next = next_slot
                        pc.next = next_pc
                        if (last_slot):
                            fsm.next = state.FETCH
                        else:
                            fsm.next = state.EXECUTE
                        if (op_inz):
                            pc.next = rt[WIDTHT-1:WIDTHS]
                            slot.next = rt[WIDTHS-1:0]
                            fsm.next = state.FETCH
                else:
                    avmm.read.next = True

    return instances ()

#------------------------------------------------------------------------------
if (__name__ == '__main__'):
    from types import SimpleNamespace
    from isomorph import convert

    clock = signal()
    clock_sreset = signal()
    irq = signal()
    avmm = SimpleNamespace(
        address = signal(29),
        readdata = signal(32),
        writedata = signal(32),
        read = signal(),
        write = signal(),
        waitrequest = signal()
    )

    inst_core = sbc4_core (
        i_clock = clock,
        i_clock_sreset = clock_sreset,

        i_irq = irq,
        avmm = avmm
    )

    convert(inst_core)
