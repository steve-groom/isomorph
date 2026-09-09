"""AXI4-Lite pin factory. No ID, no burst."""
from types import SimpleNamespace

from ..signal import signal


def axi4_lite (width_a = 32, width_d = 32):
    strb = max(1, width_d // 8)
    return SimpleNamespace(
        awvalid = signal(),
        awready = signal(),
        awaddr = signal(width_a),
        awprot = signal(3),
        wvalid = signal(),
        wready = signal(),
        wdata = signal(width_d),
        wstrb = signal(strb),
        bvalid = signal(),
        bready = signal(),
        bresp = signal(2),
        arvalid = signal(),
        arready = signal(),
        araddr = signal(width_a),
        arprot = signal(3),
        rvalid = signal(),
        rready = signal(),
        rdata = signal(width_d),
        rresp = signal(2),
    )
