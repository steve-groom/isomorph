"""Avalon pin factories. SimpleNamespace bundles, flattened by elaborate.

The Intel Avalon Interface Specifications define far more signal roles
than any one port uses, and the specification is explicit that none of
them is required: a port carries the roles it actually needs. So every
optional role here is behind a flag, and the defaults are the fundamental
memory-mapped port.

Leaving a role on a port that nothing drives or reads is not free. It is
an unused port, every linter says so, and an unused bundle member's
direction is inferred as an input, which is wrong for a master's
outputs. Ask for what the block uses and nothing else.
"""
import math
from types import SimpleNamespace

from ..signal import signal


def avalon_mm (WIDTHA = 32, WIDTHD = 32, pipelined = True, lock = True,
               writable = True, readable = True, burst = 0,
               beginbursttransfer = False, response = False,
               writeresponsevalid = False, chipselect = False,
               debugaccess = False):
    """Avalon-MM pins.

    Fundamental, always present:

      address       WIDTHA bits, a word offset into the slave's space
      waitrequest   the slave stalls the interconnect with this

    readable, on by default:

      read, readdata

    writable, on by default:

      write, writedata, and byteenable when WIDTHD is over 8 bits

    Optional roles, each off unless asked for:

      pipelined = True            readdatavalid, for a read whose data
                                  comes back some cycles later
      lock = True                 lock, for an atomic read-modify-write
      burst = N                   burstcount, N bits wide, 2 to 32.
                                  The specification requires waitrequest
                                  alongside it, which is always here
      beginbursttransfer = True   asserted for the first cycle of a burst
      response = True             response, 2 bits, the slave's read
                                  status
      writeresponsevalid = True   writeresponsevalid, with response, the
                                  slave's write status
      chipselect = True           the slave ignores everything else while
                                  this is low
      debugaccess = True          the master may write memory a slave
                                  would otherwise protect

    A port with neither read nor write is refused: the specification's
    minimum is readdata for a read-only port, or write and writedata for
    a write-only one.
    """
    if not readable and not writable:
        raise ValueError(
            'avalon_mm: a port needs read or write; the specification\'s '
            'minimum is readdata for a read-only port, or write and '
            'writedata for a write-only one')
    if burst and not 2 <= burst <= 32:
        raise ValueError(
            f'avalon_mm: burst is {burst}; burstcount is 2 to 32 bits '
            '(pass 0 for no bursting)')
    if beginbursttransfer and not burst:
        raise ValueError(
            'avalon_mm: beginbursttransfer marks the first cycle of a '
            'burst, so it needs burst = N as well')
    if writeresponsevalid and not response:
        raise ValueError(
            'avalon_mm: writeresponsevalid reports its status on '
            'response, so it needs response = True as well')

    bus = SimpleNamespace(
        address = signal(WIDTHA),
        waitrequest = signal(),
    )
    if readable:
        bus.read = signal()
        bus.readdata = signal(WIDTHD)
        if pipelined:
            bus.readdatavalid = signal()
    if writable:
        bus.write = signal()
        bus.writedata = signal(WIDTHD)
        if WIDTHD > 8:
            bus.byteenable = signal(WIDTHD // 8)
    if burst:
        bus.burstcount = signal(burst)
        if beginbursttransfer:
            bus.beginbursttransfer = signal()
    if response:
        bus.response = signal(2)
        if writeresponsevalid:
            bus.writeresponsevalid = signal()
    if lock:
        bus.lock = signal()
    if chipselect:
        bus.chipselect = signal()
    if debugaccess:
        bus.debugaccess = signal()
    return bus


def avalon_st (SYMBOL_BITS = 8, SYMBOLS = 1, packets = False,
               ready = True, channels = 0, ERROR_BITS = 0,
               empty = None):
    """Avalon-ST pins. The source drives everything but ready.

    Fundamental:

      valid         the source qualifies this beat
      data          SYMBOLS * SYMBOL_BITS bits

    Optional:

      ready = True      the sink applies backpressure. On by default;
                        pass False for a source the sink can never stall
      packets = True    startofpacket and endofpacket, and empty when a
                        beat can carry fewer than SYMBOLS symbols
      channels = N      channel, wide enough for N channels, 1 to 128
      ERROR_BITS = N    error, N bits, one meaning per bit
      empty             override the empty width; by default it is
                        ceil(log2(SYMBOLS)) and only present with
                        packets and more than one symbol per beat
    """
    if SYMBOL_BITS < 1 or SYMBOLS < 1:
        raise ValueError(
            f'avalon_st: SYMBOL_BITS {SYMBOL_BITS} and SYMBOLS {SYMBOLS} '
            'must both be at least 1')
    if channels and not 1 <= channels <= 128:
        raise ValueError(
            f'avalon_st: channels is {channels}; Avalon-ST carries 1 to '
            '128 channels (pass 0 for a single unnamed stream)')

    bus = SimpleNamespace(
        valid = signal(),
        data = signal(SYMBOLS * SYMBOL_BITS),
    )
    if ready:
        bus.ready = signal()
    if packets:
        bus.startofpacket = signal()
        bus.endofpacket = signal()
        width = empty
        if width is None:
            width = max(0, math.ceil(math.log2(SYMBOLS))) if SYMBOLS > 1 else 0
        if width:
            bus.empty = signal(width)
    if channels:
        bus.channel = signal(max(1, math.ceil(math.log2(channels))))
    if ERROR_BITS:
        bus.error = signal(ERROR_BITS)
    return bus
