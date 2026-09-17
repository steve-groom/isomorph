"""Avalon-MM and Avalon-ST pin factories against the specification.

The specification is explicit that no signal role is required: a port
carries the roles it needs. So every optional role is behind a flag, and
asking for a role that cannot stand alone is refused rather than
silently emitted.
"""
import unittest

from isomorph.ifaces import avalon_mm, avalon_st


def pins (bus):
    return {name: len(value) for name, value in vars(bus).items()
            if hasattr(value, 'width')}


class MemoryMappedTests (unittest.TestCase):
    def test_fundamental_port (self):
        bus = pins(avalon_mm(WIDTHA = 8, WIDTHD = 32, pipelined = False,
                             lock = False))
        self.assertEqual(bus, {
            'address': 8, 'waitrequest': 1,
            'read': 1, 'readdata': 32,
            'write': 1, 'writedata': 32, 'byteenable': 4,
        })

    def test_byteenable_only_above_a_byte (self):
        narrow = pins(avalon_mm(WIDTHD = 8, lock = False, pipelined = False))
        self.assertNotIn('byteenable', narrow)
        wide = pins(avalon_mm(WIDTHD = 9, lock = False, pipelined = False))
        self.assertEqual(wide['byteenable'], 1)

    def test_read_only_and_write_only (self):
        read_only = pins(avalon_mm(writable = False, lock = False,
                                   pipelined = False))
        self.assertEqual(set(read_only),
                         {'address', 'waitrequest', 'read', 'readdata'})
        write_only = pins(avalon_mm(readable = False, lock = False,
                                    WIDTHD = 32))
        self.assertEqual(set(write_only), {'address', 'waitrequest', 'write',
                                           'writedata', 'byteenable'})

    def test_a_port_must_do_something (self):
        with self.assertRaises(ValueError) as caught:
            avalon_mm(readable = False, writable = False)
        self.assertIn('minimum', str(caught.exception))

    def test_burstcount_width (self):
        bus = pins(avalon_mm(burst = 5, beginbursttransfer = True))
        self.assertEqual(bus['burstcount'], 5)
        self.assertEqual(bus['beginbursttransfer'], 1)

    def test_burstcount_bounds (self):
        for bad in (1, 33):
            with self.assertRaises(ValueError):
                avalon_mm(burst = bad)

    def test_roles_that_cannot_stand_alone (self):
        with self.assertRaises(ValueError) as caught:
            avalon_mm(beginbursttransfer = True)
        self.assertIn('burst', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            avalon_mm(writeresponsevalid = True)
        self.assertIn('response', str(caught.exception))

    def test_response_is_two_bits (self):
        bus = pins(avalon_mm(response = True, writeresponsevalid = True))
        self.assertEqual(bus['response'], 2)
        self.assertEqual(bus['writeresponsevalid'], 1)

    def test_optional_roles_are_off_by_default (self):
        bus = pins(avalon_mm())
        for role in ('burstcount', 'beginbursttransfer', 'response',
                     'writeresponsevalid', 'chipselect', 'debugaccess'):
            self.assertNotIn(role, bus)

    def test_chipselect_and_debugaccess (self):
        bus = pins(avalon_mm(chipselect = True, debugaccess = True))
        self.assertEqual(bus['chipselect'], 1)
        self.assertEqual(bus['debugaccess'], 1)

    def test_readdatavalid_follows_readable (self):
        self.assertIn('readdatavalid', pins(avalon_mm()))
        self.assertNotIn('readdatavalid',
                         pins(avalon_mm(readable = False)))


class StreamingTests (unittest.TestCase):
    def test_fundamental_source (self):
        self.assertEqual(pins(avalon_st()),
                         {'valid': 1, 'data': 8, 'ready': 1})

    def test_data_is_symbols_times_symbol_bits (self):
        self.assertEqual(pins(avalon_st(SYMBOL_BITS = 8,
                                        SYMBOLS = 4))['data'], 32)
        self.assertEqual(pins(avalon_st(SYMBOL_BITS = 12,
                                        SYMBOLS = 2))['data'], 24)

    def test_no_backpressure (self):
        self.assertNotIn('ready', pins(avalon_st(ready = False)))

    def test_packets_bring_empty_only_when_it_can_be_short (self):
        one = pins(avalon_st(SYMBOLS = 1, packets = True))
        self.assertIn('startofpacket', one)
        self.assertIn('endofpacket', one)
        self.assertNotIn('empty', one)
        four = pins(avalon_st(SYMBOLS = 4, packets = True))
        self.assertEqual(four['empty'], 2)
        eight = pins(avalon_st(SYMBOLS = 8, packets = True))
        self.assertEqual(eight['empty'], 3)

    def test_empty_can_be_overridden (self):
        bus = pins(avalon_st(SYMBOLS = 4, packets = True, empty = 3))
        self.assertEqual(bus['empty'], 3)

    def test_channel_width_holds_the_count (self):
        self.assertEqual(pins(avalon_st(channels = 16))['channel'], 4)
        self.assertEqual(pins(avalon_st(channels = 128))['channel'], 7)
        self.assertEqual(pins(avalon_st(channels = 2))['channel'], 1)

    def test_channel_bounds (self):
        with self.assertRaises(ValueError):
            avalon_st(channels = 129)
        self.assertNotIn('channel', pins(avalon_st(channels = 0)))

    def test_error_bits (self):
        self.assertEqual(pins(avalon_st(ERROR_BITS = 3))['error'], 3)
        self.assertNotIn('error', pins(avalon_st()))

    def test_bad_symbol_shape (self):
        for kwargs in ({'SYMBOL_BITS': 0}, {'SYMBOLS': 0}):
            with self.assertRaises(ValueError):
                avalon_st(**kwargs)


if __name__ == '__main__':
    unittest.main()
