import unittest
from types import SimpleNamespace

from isomorph import (block, signal, signals, enum, always_comb, always_ff,
    assign, concat, replicate, instances, analyse, IsomorphError)


@block
def leaf (i_a, i_b, o_sum, WIDTH = 8):
    (t0, t1) = [signal(WIDTH) for _ in range(2)]

    @always_comb
    def sum_comb ():
        t0.next = i_a
        t1.next = i_b
        o_sum.next = (t0 + t1)[WIDTH-1:0]

    return instances()


@block
def parent (i_clock, i_x, bus, o_y):
    mid = signal(8)
    regs = signals(4, 8)
    state = enum('IDLE', 'RUN')
    fsm = signal(state)

    inst_leaf = leaf (
        i_a = i_x,
        i_b = bus.data,
        o_sum = mid
    )
    inst_wide = leaf (
        i_a = i_x,
        i_b = bus.data,
        o_sum = o_y,
        WIDTH = 8
    )

    assign(bus.ack, lambda: mid[0])

    @always_ff (i_clock.posedge)
    def fsm_logic ():
        if (fsm == state.IDLE):
            fsm.next = state.RUN
            regs[0].next = mid

    return instances()


def elaborate_parent ():
    clock = signal()
    x = signal(8)
    bus = SimpleNamespace(data = signal(8), ack = signal())
    y = signal(8)
    return parent(i_clock = clock, i_x = x, bus = bus, o_y = y)


class ElaborateTests(unittest.TestCase):
    def test_names_and_kinds (self):
        top = elaborate_parent()
        self.assertEqual(set(top.signals), {'mid', 'fsm'})
        self.assertEqual(top.signals['mid'].width, 8)
        self.assertEqual(list(top.arrays), ['regs'])
        self.assertEqual(top.arrays['regs'][2].name, 'regs[2]')
        self.assertEqual(list(top.enums), ['state'])
        self.assertEqual(top.enums['state'].width, 1)
        self.assertEqual([p.name for p in top.processes], ['fsm_logic'])
        self.assertEqual(len(top.assigns), 1)
        self.assertEqual(list(top.instances), ['inst_leaf', 'inst_wide'])

    def test_tuple_unpack_names_signals (self):
        top = elaborate_parent()
        child = top.instances['inst_leaf']
        self.assertEqual(sorted(child.signals), ['t0', 't1'])

    def test_bundle_port_names (self):
        top = elaborate_parent()
        bus = top.ports['bus']
        self.assertEqual(top.port_names[id(bus.data)], 'bus_data')
        self.assertEqual(top.port_names[id(bus.ack)], 'bus_ack')
        # the caller's own signal is not renamed
        self.assertIsNone(bus.data.name)

    def test_parameter_suffix_and_walk (self):
        top = elaborate_parent()
        names = [m.module_name for m in top.walk()]
        self.assertEqual(names, ['leaf', 'parent'])   # same WIDTH: one module
        self.assertEqual(top.instances['inst_wide'].parameters, {'WIDTH': 8})

    def test_port_directions (self):
        modules, warnings = analyse(elaborate_parent())
        parent_ir = modules[-1]
        directions = {p.name: p.direction for p in parent_ir.ports}
        self.assertEqual(directions['i_x'], 'in')
        self.assertEqual(directions['bus_data'], 'in')
        self.assertEqual(directions['bus_ack'], 'out')
        self.assertEqual(directions['o_y'], 'out')
        leaf_ir = modules[0]
        self.assertEqual({p.name: p.direction for p in leaf_ir.ports},
                         {'i_a': 'in', 'i_b': 'in', 'o_sum': 'out'})

    def test_instance_port_map (self):
        modules, _ = analyse(elaborate_parent())
        inst = modules[-1].instances[0]
        self.assertEqual(inst.module, 'leaf')
        self.assertEqual(inst.ports['i_b'].value, 'bus_data')
        self.assertEqual(inst.ports['o_sum'].value, 'mid')

    def test_block_must_return_instances (self):
        @block
        def bad (i_a):
            return None
        with self.assertRaises(IsomorphError):
            bad(i_a = signal())

    def test_one_hot_member_values (self):
        state = enum('IDLE', 'RUN', 'DONE', encoding = 'one_hot')
        self.assertEqual(state.width, 3)
        # one bit per state with bit 0 inverted, so the first state
        # is all zeros: what Quartus and Efinity both build
        self.assertEqual([m.value for m in state.members], [0, 3, 5])

    def test_gray_member_values (self):
        state = enum('IDLE', 'RUN', 'DONE', encoding = 'gray')
        self.assertEqual(state.width, 2)
        self.assertEqual([m.value for m in state.members], [0, 1, 3])

    def test_johnson_member_values (self):
        state = enum('A', 'B', 'C', 'D', encoding = 'johnson')
        self.assertEqual(state.width, 2)
        self.assertEqual([m.value for m in state.members], [0, 1, 3, 2])

    def test_bad_encoding (self):
        with self.assertRaises(IsomorphError):
            enum('IDLE', encoding = 'rainbow')

    def test_signal_width_check (self):
        with self.assertRaises(IsomorphError):
            signal(0)


class FloatParameterTests (unittest.TestCase):
    """A clock rate is written 50e6 and divided down to the integer
    that reaches the hardware. No HDL has a real generic, so the
    float is elaboration only, like a string parameter."""

    def build (self, rate):
        @block
        def divider (i_clock, o_tick, SYSTEM_CLOCK_HZ = 50e6,
                     TICK_HZ = 1e6):
            CYCLES = round(SYSTEM_CLOCK_HZ / TICK_HZ)
            WIDTHC = max(1, (CYCLES - 1).bit_length())
            count = signal(WIDTHC)

            @always_ff (i_clock.posedge)
            def count_logic ():
                o_tick.next = False
                if (count == (CYCLES - 1)):
                    count.next = 0
                    o_tick.next = True
                else:
                    count.next = (count + 1)[WIDTHC-1:0]

            return instances()

        return divider(i_clock = signal(), o_tick = signal(),
                       SYSTEM_CLOCK_HZ = rate)

    def test_a_float_parameter_elaborates (self):
        modules, _ = analyse(self.build(50e6))
        self.assertEqual(modules[-1].constants['CYCLES'], 50)

    def test_it_is_not_emitted_as_a_generic (self):
        from isomorph import emit_sv, emit_vhdl
        modules, _ = analyse(self.build(50e6))
        self.assertNotIn('SYSTEM_CLOCK_HZ', emit_sv(modules))
        self.assertNotIn('SYSTEM_CLOCK_HZ', emit_vhdl(modules))

    def test_two_rates_are_two_modules_with_readable_names (self):
        modules, _ = analyse(self.build(80e6))
        self.assertEqual(modules[-1].constants['CYCLES'], 80)
        for m in modules:
            self.assertNotIn('.', m.name)


if __name__ == '__main__':
    unittest.main()
