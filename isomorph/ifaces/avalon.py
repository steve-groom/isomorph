"""Avalon-MM pin factory. SimpleNamespace, flattened by elaborate."""
from types import SimpleNamespace

from ..signal import signal


def avalon_mm (WIDTHA = 32, WIDTHD = 32, pipelined = True, lock = True):
    """Master/slave pins: address, data, byteenable, read, write,
    waitrequest, readdatavalid, lock.

    byteenable exists only when the data bus is wider than a byte, as in
    the MyHDL library. pipelined = False drops readdatavalid, for a
    master or slave that completes a read on the clock that clears
    waitrequest. lock = False drops lock, for a master that issues no
    locked sequences. Leaving a pin on a port that nothing drives or
    reads is not free: it is an unused port, and the direction of an
    unused member is inferred as an input, which is wrong for a master's
    outputs."""
    bus = SimpleNamespace(
        address = signal(WIDTHA),
        readdata = signal(WIDTHD),
        writedata = signal(WIDTHD),
        read = signal(),
        write = signal(),
        waitrequest = signal(),
    )
    if WIDTHD > 8:
        bus.byteenable = signal(WIDTHD // 8)
    if pipelined:
        bus.readdatavalid = signal()
    if lock:
        bus.lock = signal()
    return bus
