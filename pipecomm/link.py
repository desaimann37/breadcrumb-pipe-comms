"""Link budget for a single hop, and for a chain of relay nodes.

The chain is the architecture.  A single radio cannot cross two miles of pipe,
but a crawler that leaves a trail of small relay nodes behind it can, and the
nodes are recovered on the way back out.  This module turns a pipe profile and
a node layout into the numbers an operator actually sees: SNR per hop, video
goodput, glass-to-glass latency and control round-trip time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from .constants import NOISE_FLOOR_DBM_HZ
from .mcs import (
    CONTROL_MCS,
    WIFI_MCS,
    Mcs,
    effective_rate_bps,
    packet_error_rate,
    shannon_limit_bps,
    select_mcs,
)
from .pipe import LossBreakdown, PipeProfile


@dataclass
class RadioConfig:
    """One radio design point, the thing the project is really evaluating."""

    name: str = "2.4 GHz OFDM + DSSS control"
    frequency_hz: float = 2.437e9
    bandwidth_hz: float = 20e6
    tx_power_dbm: float = 20.0           # 100 mW, inside Part 15 limits
    noise_figure_db: float = 6.0
    antenna_coupling_db: float = 3.0     # per end: monopole to TE11 mode
    implementation_loss_db: float = 3.0
    snr_ceiling_db: float = 30.0         # delay spread / self-interference floor
    fade_margin_db: float = 8.0          # required margin for multipath fading

    # Control / telemetry link, deliberately a separate waveform.
    control_bandwidth_hz: float = 5e6
    control_tx_power_dbm: float = 20.0
    control_frame_bytes: int = 24

    # Relay topology.
    # Default is the cheap build: one radio per node on one channel, which
    # pays the classic factor-of-three half-duplex relaying penalty. Switching
    # to two radios on alternating channels is an upgrade the dashboard can
    # toggle, and it is one of the clearest findings in the study.
    radios_per_node: int = 1
    channel_count: int = 1

    # Packetisation.
    video_packet_bytes: int = 1400
    relay_processing_ms: float = 0.6

    def noise_dbm(self, bandwidth_hz: Optional[float] = None) -> float:
        bw = bandwidth_hz or self.bandwidth_hz
        return NOISE_FLOOR_DBM_HZ + 10.0 * math.log10(bw) + self.noise_figure_db

    @property
    def airtime_divisor(self) -> float:
        """How many hops have to share the same airtime.

        A single-radio, single-channel chain loses a factor of three to
        half-duplex relaying.  Dual-radio nodes on alternating channels get
        most of that back, which is one of the main findings of the study.
        """
        if self.radios_per_node >= 2 and self.channel_count >= 2:
            return max(1.0, 3.0 / min(self.channel_count, 3))
        return 3.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "frequency_mhz": round(self.frequency_hz / 1e6, 1),
            "bandwidth_mhz": round(self.bandwidth_hz / 1e6, 1),
            "tx_power_dbm": self.tx_power_dbm,
            "noise_figure_db": self.noise_figure_db,
            "fade_margin_db": self.fade_margin_db,
            "radios_per_node": self.radios_per_node,
            "channel_count": self.channel_count,
            "airtime_divisor": round(self.airtime_divisor, 2),
            "control_bandwidth_mhz": round(self.control_bandwidth_hz / 1e6, 2),
        }


@dataclass
class HopResult:
    """Everything known about one node-to-node hop."""

    index: int
    from_m: float
    to_m: float
    distance_m: float
    path_loss_db: float
    rx_dbm: float
    snr_db: float
    margin_db: float
    mcs_name: str
    per: float
    rate_bps: float
    shannon_bps: float
    control_snr_db: float
    control_margin_db: float
    control_ok: bool
    below_cutoff: bool
    delay_spread_ns: float
    feature_db: float
    distributed_db: float
    worst_feature: Optional[str]
    latency_ms: float
    control_latency_ms: float

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "from_m": round(self.from_m, 1),
            "to_m": round(self.to_m, 1),
            "distance_m": round(self.distance_m, 1),
            "path_loss_db": round(self.path_loss_db, 1),
            "rx_dbm": round(self.rx_dbm, 1),
            "snr_db": round(self.snr_db, 1),
            "margin_db": round(self.margin_db, 1),
            "mcs": self.mcs_name,
            "per": round(self.per, 4),
            "rate_mbps": round(self.rate_bps / 1e6, 3),
            "shannon_mbps": round(self.shannon_bps / 1e6, 2),
            "control_snr_db": round(self.control_snr_db, 1),
            "control_margin_db": round(self.control_margin_db, 1),
            "control_ok": self.control_ok,
            "below_cutoff": self.below_cutoff,
            "delay_spread_ns": round(self.delay_spread_ns, 1),
            "feature_db": round(self.feature_db, 1),
            "distributed_db": round(self.distributed_db, 1),
            "worst_feature": self.worst_feature,
            "latency_ms": round(self.latency_ms, 2),
        }


@dataclass
class ChainResult:
    """End-to-end performance of the whole relay chain."""

    hops: List[HopResult] = field(default_factory=list)
    video_goodput_bps: float = 0.0
    network_latency_ms: float = 0.0
    control_rtt_ms: float = 0.0
    control_ok: bool = True
    weakest_hop: Optional[int] = None
    min_margin_db: float = 0.0
    total_path_loss_db: float = 0.0
    end_to_end_per: float = 0.0

    def to_dict(self) -> dict:
        return {
            "hops": [h.to_dict() for h in self.hops],
            "video_goodput_mbps": round(self.video_goodput_bps / 1e6, 3),
            "network_latency_ms": round(self.network_latency_ms, 1),
            "control_rtt_ms": round(self.control_rtt_ms, 1),
            "control_ok": self.control_ok,
            "weakest_hop": self.weakest_hop,
            "min_margin_db": round(self.min_margin_db, 1),
            "total_path_loss_db": round(self.total_path_loss_db, 1),
            "end_to_end_per": round(self.end_to_end_per, 4),
        }


def free_space_loss_db(distance_m: float, f_hz: float) -> float:
    """Spreading loss before the energy is captured by the guide."""
    if distance_m <= 0:
        return 0.0
    return 20 * math.log10(distance_m) + 20 * math.log10(f_hz) - 147.55221


def path_loss(profile: PipeProfile, radio: RadioConfig, x_a: float, x_b: float):
    """In-pipe path loss between two points, with the breakdown that explains it.

    Energy spreads freely for the first few pipe diameters, after which the
    guide confines it and only the attenuation per metre applies.  That is why
    a pipe beats free space badly over long runs and loses to it over short ones.
    """
    d = abs(x_b - x_a)
    bd: LossBreakdown = profile.loss_between(radio.frequency_hz, x_a, x_b)
    diameter = profile.diameter_at((x_a + x_b) / 2.0)
    capture_len = min(d, 5.0 * diameter)
    spread = free_space_loss_db(max(capture_len, 0.05), radio.frequency_hz)
    total = (spread + bd.total_db
             + 2 * radio.antenna_coupling_db
             + radio.implementation_loss_db)
    return total, bd


def evaluate_hop(profile: PipeProfile, radio: RadioConfig, index: int,
                 x_a: float, x_b: float, fade_db: float = 0.0) -> HopResult:
    """Full link budget for one hop, video and control alike."""
    loss_db, bd = path_loss(profile, radio, x_a, x_b)
    loss_db += fade_db

    rx = radio.tx_power_dbm - loss_db
    snr = min(rx - radio.noise_dbm(), radio.snr_ceiling_db)

    rate, mcs, per = effective_rate_bps(snr, radio.bandwidth_hz, WIFI_MCS)
    mcs_name = mcs.name if mcs else "no link"
    lowest_req = WIFI_MCS[0].required_snr_db
    margin = snr - lowest_req

    ctrl_rx = radio.control_tx_power_dbm - loss_db
    ctrl_snr = min(ctrl_rx - radio.noise_dbm(radio.control_bandwidth_hz),
                   radio.snr_ceiling_db)
    ctrl_margin = ctrl_snr - CONTROL_MCS.required_snr_db
    ctrl_per = packet_error_rate(ctrl_snr, CONTROL_MCS)
    ctrl_ok = ctrl_per < 0.2 and not bd.below_cutoff

    # Store-and-forward delay for one video packet and one control frame.
    if rate > 0:
        tx_ms = (radio.video_packet_bytes * 8 / rate) * 1000.0
    else:
        tx_ms = float("nan")
    ctrl_rate = CONTROL_MCS.phy_rate_bps(radio.control_bandwidth_hz)
    ctrl_tx_ms = (radio.control_frame_bytes * 8 / ctrl_rate) * 1000.0

    return HopResult(
        index=index,
        from_m=x_a,
        to_m=x_b,
        distance_m=abs(x_b - x_a),
        path_loss_db=loss_db,
        rx_dbm=rx,
        snr_db=snr,
        margin_db=margin,
        mcs_name=mcs_name,
        per=per,
        rate_bps=rate,
        shannon_bps=shannon_limit_bps(snr, radio.bandwidth_hz),
        control_snr_db=ctrl_snr,
        control_margin_db=ctrl_margin,
        control_ok=ctrl_ok,
        below_cutoff=bd.below_cutoff,
        delay_spread_ns=bd.delay_spread_ns,
        feature_db=bd.feature_db,
        distributed_db=bd.distributed_db,
        worst_feature=(bd.worst_feature.label if bd.worst_feature else None),
        latency_ms=(tx_ms + radio.relay_processing_ms) if rate > 0 else float("inf"),
        control_latency_ms=ctrl_tx_ms + radio.relay_processing_ms,
    )


def evaluate_chain(profile: PipeProfile, radio: RadioConfig,
                   positions: List[float],
                   fades: Optional[List[float]] = None) -> ChainResult:
    """Evaluate a relay chain given the node positions along the pipe.

    ``positions[0]`` is the surface/base station at the access point and
    ``positions[-1]`` is the crawler.
    """
    result = ChainResult()
    if len(positions) < 2:
        return result

    hops: List[HopResult] = []
    for i in range(len(positions) - 1):
        fade = fades[i] if fades and i < len(fades) else 0.0
        hops.append(evaluate_hop(profile, radio, i, positions[i], positions[i + 1], fade))
    result.hops = hops

    rates = [h.rate_bps for h in hops]
    bottleneck = min(rates) if rates else 0.0
    result.video_goodput_bps = bottleneck / radio.airtime_divisor

    result.network_latency_ms = sum(
        h.latency_ms for h in hops if math.isfinite(h.latency_ms)
    ) * radio.airtime_divisor if bottleneck > 0 else float("inf")

    result.control_rtt_ms = 2.0 * sum(h.control_latency_ms for h in hops)
    result.control_ok = all(h.control_ok for h in hops)

    margins = [h.margin_db for h in hops]
    result.min_margin_db = min(margins)
    result.weakest_hop = margins.index(result.min_margin_db)
    result.total_path_loss_db = sum(h.path_loss_db for h in hops)

    delivered = 1.0
    for h in hops:
        delivered *= (1.0 - min(h.per ** 4, 1.0))
    result.end_to_end_per = 1.0 - delivered
    return result


def pipe_context(profile: PipeProfile, radio: RadioConfig, x_m: float) -> dict:
    """What the pipe looks like at ``x_m``: geometry, loss, and what is ahead.

    Shared by the simulated and the live mission so the operator console shows
    the same context whether the numbers come from the model or from hardware.
    """
    section = profile.section_at(x_m)
    sl = profile.section_loss(radio.frequency_hz, section)
    ahead = [ft for ft in profile.features if ft.position_m > x_m]
    nxt = ahead[0] if ahead else None
    return {
        "section_label": section.label,
        "diameter_in": round(section.spec.diameter_m / 0.0254, 1),
        "fill_fraction": round(section.spec.fill_fraction, 3),
        "wall": section.spec.wall.name,
        "db_per_km": round(sl.db_per_m * 1000.0, 2),
        "cutoff_mhz": round(sl.cutoff_hz / 1e6, 1),
        "below_cutoff": sl.below_cutoff,
        "bounces_per_m": round(sl.bounces_per_m, 3),
        "ray_angle_deg": round(sl.ray_angle_deg, 2),
        "water_bounce_fraction": round(sl.water_bounce_fraction, 3),
        "d_over_lambda": round(sl.d_over_lambda, 2),
        "model_valid": sl.model_valid,
        "next_feature": nxt.to_dict() if nxt else None,
        "next_feature_distance_m": round(nxt.position_m - x_m, 1) if nxt else None,
    }


def max_reach_m(profile: PipeProfile, radio: RadioConfig, start_m: float,
                target_margin_db: Optional[float] = None,
                step_m: float = 2.0, limit_m: Optional[float] = None) -> float:
    """How far one hop can reach from ``start_m`` before the margin runs out.

    This is the function that sizes the whole mission: it answers "how many
    relay nodes do I need to carry to inspect two miles?".
    """
    target = radio.fade_margin_db if target_margin_db is None else target_margin_db
    limit = limit_m if limit_m is not None else profile.length_m
    x = start_m
    last_good = start_m
    while x < limit:
        x = min(x + step_m, limit)
        hop = evaluate_hop(profile, radio, 0, start_m, x)
        if hop.margin_db < target or hop.rate_bps <= 0:
            return last_good
        last_good = x
    return last_good
