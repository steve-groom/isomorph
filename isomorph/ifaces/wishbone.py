"""Wishbone B4 classic pin factory."""
from types import SimpleNamespace

from ..signal import signal


def wishbone (width_a = 32, width_d = 32):
    sel = max(1, width_d // 8)
    return SimpleNamespace(
        cyc = signal(),
        stb = signal(),
        we = signal(),
        adr = signal(width_a),
        dat_w = signal(width_d),
        dat_r = signal(width_d),
        sel = signal(sel),
        ack = signal(),
        err = signal(),
        rty = signal(),
    )
