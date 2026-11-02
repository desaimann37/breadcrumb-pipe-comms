"""Modulation and coding schemes, and the packet-error model.

Two radio profiles are modelled because the architecture depends on the split:

``WIFI_MCS``
    An OFDM ladder in the style of 802.11n/ac.  This carries video.  It needs a
    healthy SNR and dies quickly once the margin is gone.

``CONTROL_MCS``
    A narrowband, direct-sequence spread-spectrum link with heavy FEC.  It
    carries drive commands, telemetry and the emergency stop.  The processing
    gain buys roughly 20 dB, so it keeps working about 30 dB past the point
    where video has stopped.

That asymmetry is the core safety argument of the design: the operator never
loses the ability to stop the crawler just because the picture froze.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Mcs:
    """One rung of a modulation/coding ladder."""

    name: str
    bits_per_symbol: float      # modulation order, in bits
    code_rate: float            # FEC rate
    required_snr_db: float      # SNR for ~1e-2 PER at the reference bandwidth
    processing_gain_db: float = 0.0

    @property
    def spectral_efficiency(self) -> float:
        """Net information bits per second per Hz."""
        return self.bits_per_symbol * self.code_rate

    def phy_rate_bps(self, bandwidth_hz: float, overhead: float = 0.28) -> float:
        """Usable PHY rate after preamble, guard interval and MAC overhead."""
        gross = bandwidth_hz * self.spectral_efficiency
        if self.processing_gain_db > 0:
            gross /= 10 ** (self.processing_gain_db / 10.0)
        return max(gross * (1.0 - overhead), 0.0)


# Reference ladder, broadly 802.11-like with a 28% airtime overhead.
WIFI_MCS: List[Mcs] = [
    Mcs("BPSK 1/2", 1, 1 / 2, 2.0),
    Mcs("QPSK 1/2", 2, 1 / 2, 5.0),
    Mcs("QPSK 3/4", 2, 3 / 4, 8.0),
    Mcs("16-QAM 1/2", 4, 1 / 2, 11.0),
    Mcs("16-QAM 3/4", 4, 3 / 4, 15.0),
    Mcs("64-QAM 2/3", 6, 2 / 3, 19.0),
    Mcs("64-QAM 3/4", 6, 3 / 4, 21.0),
    Mcs("256-QAM 3/4", 8, 3 / 4, 27.0),
]

# Control/telemetry link: BPSK plus 20 dB of spreading gain and rate-1/3 FEC.
CONTROL_MCS = Mcs("DSSS BPSK 1/3 (PG 20 dB)", 1, 1 / 3, -16.0, processing_gain_db=20.0)


def packet_error_rate(snr_db: float, mcs: Mcs) -> float:
    """Smooth PER curve around the MCS threshold.

    A logistic in the link margin: ~95% PER at zero margin, 50% at +1.5 dB,
    5% at +3 dB, and below 0.1% past +5 dB.  Sharp enough to behave like a real
    waterfall curve without pretending to be a specific chipset.
    """
    margin = snr_db - mcs.required_snr_db
    x = 2.0 * (margin - 1.5)
    if x > 30:
        return 0.0
    if x < -30:
        return 1.0
    return 1.0 / (1.0 + math.exp(x))


def select_mcs(snr_db: float, ladder: Optional[List[Mcs]] = None,
               target_per: float = 0.05) -> Optional[Mcs]:
    """Highest rung whose PER stays under ``target_per`` at this SNR."""
    ladder = ladder or WIFI_MCS
    best = None
    for m in ladder:
        if packet_error_rate(snr_db, m) <= target_per:
            best = m
        else:
            break
    return best


def effective_rate_bps(snr_db: float, bandwidth_hz: float,
                       ladder: Optional[List[Mcs]] = None,
                       retries: int = 3) -> tuple:
    """Goodput of a single hop, after ARQ retries.

    Returns ``(rate_bps, mcs, per)``.  Retransmissions cost airtime, so the
    goodput is derated by the expected number of transmissions per packet.
    """
    mcs = select_mcs(snr_db, ladder)
    if mcs is None:
        return 0.0, None, 1.0
    per = packet_error_rate(snr_db, mcs)
    delivered = 1.0 - per ** (retries + 1)
    expected_tx = sum(per ** k for k in range(retries + 1))
    raw = mcs.phy_rate_bps(bandwidth_hz)
    return raw * delivered / max(expected_tx, 1e-9), mcs, per


def shannon_limit_bps(snr_db: float, bandwidth_hz: float) -> float:
    """Capacity bound, shown next to the achieved rate to keep us honest."""
    snr_lin = 10 ** (snr_db / 10.0)
    return bandwidth_hz * math.log2(1.0 + max(snr_lin, 0.0))
