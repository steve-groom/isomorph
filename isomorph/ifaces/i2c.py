"""I2C pin factory. Open drain, so every pin is drive-low or release."""
from types import SimpleNamespace

from ..signal import signal


def i2c ():
    """Master or slave pins for a two-wire bus.

    I2C has no push-pull driver. A device either pulls a line to ground
    or lets go, and an external pull-up returns it high, which is what
    lets any device stretch the clock or arbitrate away. So each line is
    three signals: the value to drive, the enable that drives it, and
    what the line actually reads back as.

    Drive low by asserting the enable with the output low. Release by
    clearing the enable. Read the line through the input, never through
    the output, because another device may be holding it down.

    The inputs cross from an unrelated domain and must be synchronised
    before use; a synchroniser chain is the chain for that."""
    return SimpleNamespace(
        sda_o = signal(),
        sda_oe = signal(),
        sda_i = signal(),
        scl_o = signal(),
        scl_oe = signal(),
        scl_i = signal(),
    )
