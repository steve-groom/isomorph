"""FPGA configuration port pins."""
from types import SimpleNamespace

from ..signal import signal


def config ():
    return SimpleNamespace(
        creset_n = signal(reset = True),
        cck = signal(),
        cdi0 = signal(),
    )
