"""
The ports the blocks in this package speak, and nothing else speaks.

Not interfaces, which is why they are not in isomorph.ifaces. A
ready/valid/data stream is the handshake AXI4-Stream and Avalon-ST
share rather than either of them - avalon_st is the real thing, with
packets, empty and channels - and a ram port is the shape a vendor
block RAM is inferred from rather than a bus anyone publishes. Both
are conventions this library adopted, so they live with it.
"""
from types import SimpleNamespace

from ..signal import signal


def stream (width = 8):
    """valid, ready and data: one beat when both are high."""
    return SimpleNamespace(
        valid = signal(),
        ready = signal(),
        data = signal(width),
    )


def ram_read (WIDTHA = 8, WIDTHD = 8):
    """addr in, data out on the clock after, with no enable: the data
    register is loaded on every edge, which is what a vendor maps onto
    the RAM's own output register."""
    return SimpleNamespace(
        addr = signal(WIDTHA),
        data = signal(WIDTHD),
    )


def ram_write (WIDTHA = 8, WIDTHD = 8):
    """addr and data written on the clock that we is high."""
    return SimpleNamespace(
        addr = signal(WIDTHA),
        data = signal(WIDTHD),
        we = signal(),
    )
