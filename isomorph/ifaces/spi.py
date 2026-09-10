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


def part_spi ():
    """Part-specific SPI: mosi plus a second data line mosd."""
    return SimpleNamespace(
        ss_n = signal(),
        sclk = signal(),
        mosi = signal(),
        mosd = signal(),
        miso = signal(),
    )


def part_spi ():
    """Part-specific SPI: mosi plus a second data line mosd."""
    return SimpleNamespace(
        ss_n = signal(),
        sclk = signal(),
        mosi = signal(),
        mosd = signal(),
        miso = signal(),
    )


def adc121s021 ():
    """ADC121S021 serial ADC, read-only three-wire."""
    return SimpleNamespace(
        ss_n = signal(),
        sclk = signal(),
        miso = signal(),
    )


def bga7204 (SELECT = 1):
    """BGA7204 serial control: SELECT chip selects (a bit when 1)."""
    return SimpleNamespace(
        ss = signal(SELECT) if SELECT > 1 else signal(),
        clk = signal(),
        ser_out = signal(),
        ser_in = signal(),
        ps_config = signal(),
        spi_config = signal(),
    )
