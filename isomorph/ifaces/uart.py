"""UART pin factory. Lines idle high."""
from types import SimpleNamespace

from ..signal import signal


def uart ():
    return SimpleNamespace(
        txd = signal(),
        oe = signal(),
        rxd = signal(),
    )
