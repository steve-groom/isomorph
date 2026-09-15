"""SPI pin factory.

One bundle whichever end of the wire you are: a master and a slave
carry the same four pins and isomorph reads the direction from what
drives what, so there is nothing for separate factories to say. The
select is cs_n, which is what the SPI checker looks for and what the
memory pin factories here already call it.

A chip select is active low, so whatever drives it holds it high
while idle. That is done in a reset branch, not by the declaration:
isomorph emits no power-on values, so a pin that has to be high
before the first clock has to be driven high.
"""
from types import SimpleNamespace

from ..signal import signal


def spi (tristate = False):
    """Four-wire SPI. `tristate` splits miso into o, oe and i, which
    is what a slave sharing the line with others needs."""
    bus = SimpleNamespace(
        cs_n = signal(),
        sclk = signal(),
        mosi = signal(),
    )
    if tristate:
        bus.miso_o = signal()
        bus.miso_oe = signal()
        bus.miso_i = signal()
    else:
        bus.miso = signal()
    return bus
