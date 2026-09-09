"""External memory device pins: SDRAM and HyperRAM. Timing numbers
ride along on the bundle as plain attributes for the controller."""
from types import SimpleNamespace

from ..signal import signal


def device_sdram (DATA_WIDTH = 16, ROW_WIDTH = 12, COL_WIDTH = 9,
                  BANK_WIDTH = 2, CAS_LATENCY = 3, REFRESH_ms = 64,
                  tMRD_cycles = 4, tRFC_ns = 60, tRCD_ns = 18, tRP_ns = 18,
                  tWR_ns = 15, tRAS_ns = 42, tRC_ns = 60, tRRD_ns = 12):
    """SDR SDRAM pins with split dq (o, oe, i). Control lines reset
    inactive high."""
    return SimpleNamespace(
        DATA_WIDTH = DATA_WIDTH,
        ROW_WIDTH = ROW_WIDTH,
        COL_WIDTH = COL_WIDTH,
        BANK_WIDTH = BANK_WIDTH,
        CAS_LATENCY = CAS_LATENCY,
        REFRESH_ms = REFRESH_ms,
        tMRD_cycles = tMRD_cycles,
        tRFC_ns = tRFC_ns,
        tRCD_ns = tRCD_ns,
        tRP_ns = tRP_ns,
        tWR_ns = tWR_ns,
        tRAS_ns = tRAS_ns,
        tRC_ns = tRC_ns,
        tRRD_ns = tRRD_ns,

        addr = signal(ROW_WIDTH),
        ba = signal(BANK_WIDTH),
        cas_n = signal(reset = True),
        cke = signal(reset = True),
        cs_n = signal(reset = True),
        dq_o = signal(DATA_WIDTH),
        dq_oe = signal(),
        dq_i = signal(DATA_WIDTH),
        # x4 devices have a single DQM pin; never zero width
        dqm = signal(max(1, DATA_WIDTH // 8)),
        ras_n = signal(reset = True),
        we_n = signal(reset = True),
    )


def hyperram (WIDTH = 16, tACC_ns = 35, tVCS_us = 150, LATENCY_CLOCKS = 7,
              tRWR_ns = 35):
    """HyperRAM pins with split dq and rwds (o, oe, i)."""
    return SimpleNamespace(
        tACC_ns = tACC_ns,
        tVCS_us = tVCS_us,
        LATENCY_CLOCKS = LATENCY_CLOCKS,
        tRWR_ns = tRWR_ns,

        reset_n = signal(reset = True),
        ck = signal(),
        ck_n = signal(reset = True),
        cs_n = signal(reset = True),
        rwds_i = signal(WIDTH // 8),
        rwds_o = signal(WIDTH // 8),
        rwds_oe = signal(),
        dq_i = signal(WIDTH),
        dq_o = signal(WIDTH),
        dq_oe = signal(),
    )
