"""GPIO and interrupt pin factories."""
from types import SimpleNamespace

from ..signal import signal


def gpio (WIDTH = 4):
    """Tristate GPIO bank: o, oe, i, all WIDTH bits."""
    return SimpleNamespace(
        o = signal(WIDTH),
        oe = signal(WIDTH),
        i = signal(WIDTH),
    )


def irq ():
    """One interrupt request line."""
    return SimpleNamespace(
        i = signal(),
    )
