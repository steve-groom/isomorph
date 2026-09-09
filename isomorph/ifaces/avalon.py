"""Avalon-MM pin factory. SimpleNamespace, flattened by elaborate."""
from types import SimpleNamespace

from ..signal import signal


def avalon_mm (WIDTHA = 32, WIDTHD = 32):
    """Master/slave pins: address, data, byteenable, read, write,
    waitrequest, readdatavalid, lock. byteenable exists only when the
    data bus is wider than a byte, as in the MyHDL library."""
    bus = SimpleNamespace(
        address = signal(WIDTHA),
        readdata = signal(WIDTHD),
        writedata = signal(WIDTHD),
        read = signal(),
        write = signal(),
        waitrequest = signal(),
        readdatavalid = signal(),
        lock = signal(),
    )
    if WIDTHD > 8:
        bus.byteenable = signal(WIDTHD // 8)
    return bus
