"""SPI pin factories.

A chip select is active low, so whatever drives it holds it high
while idle. That is done in a reset branch, not by the
declaration: isomorph emits no power-on values, so a pin that has
to be high before the first clock has to be driven high.
"""
from types import SimpleNamespace

from ..signal import signal


def spi ():
    """Generic four-wire SPI, single cs_n."""
    return SimpleNamespace(
        cs_n = signal(),
        sclk = signal(),
        mosi = signal(),
        miso = signal(),
    )


def spim ():
    """SPI master pins."""
    return SimpleNamespace(
        ss_n = signal(),
        sclk = signal(),
        mosi = signal(),
        miso = signal(),
    )


def spis ():
    """SPI slave pins."""
    return SimpleNamespace(
        ss_n = signal(),
        sclk = signal(),
        mosi = signal(),
        miso = signal(),
    )


def spis_tri ():
    """SPI slave with a tristate miso (o, oe, i)."""
    return SimpleNamespace(
        ss_n = signal(),
        sclk = signal(),
        mosi = signal(),
        miso_o = signal(),
        miso_oe = signal(),
        miso_i = signal(),
    )
