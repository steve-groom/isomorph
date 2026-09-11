"""Synchronous memory ports: the shape every FPGA block RAM is inferred
from.

A read port is an address in and a word out one clock later, with no
enable: the data register is loaded on every edge and is what the
vendor maps onto the RAM's own output register. A write port is an
address, a word and a strobe. ram_block in isomorph.lib drives one of
each from a signals() array, and stream_from_memory and
stream_to_memory speak to them.
"""
from types import SimpleNamespace

from ..signal import signal


def ram_read (WIDTHA = 8, WIDTHD = 8):
    """addr in, data out on the clock after."""
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
