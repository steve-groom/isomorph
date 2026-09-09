"""Ready/valid stream pins (payload is `data`)."""
from types import SimpleNamespace

from ..signal import signal


def stream (width = 8):
    return SimpleNamespace(
        valid = signal(),
        ready = signal(),
        data = signal(width),
    )
