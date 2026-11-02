"""Adaptive video ladder and the latency budget the operator feels.

The inspection is only useful if the operator can see defects, so the ladder is
annotated with what each rung is actually good for.  "Usable" is not a single
number: 240p at 4 fps is useless for grading a crack but perfectly adequate for
driving the crawler back out of the pipe, and that distinction is what lets the
system degrade instead of failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Protocol overhead on top of the encoded bitrate: RTP/UDP headers, FEC parity
# and retransmission slack.
TRANSPORT_OVERHEAD = 1.18


@dataclass(frozen=True)
class VideoTier:
    """One rung of the adaptive bitrate ladder."""

    name: str
    width: int
    height: int
    fps: float
    bitrate_bps: float
    usable_for: str

    @property
    def required_bps(self) -> float:
        return self.bitrate_bps * TRANSPORT_OVERHEAD

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "bitrate_kbps": round(self.bitrate_bps / 1e3),
            "required_kbps": round(self.required_bps / 1e3),
            "usable_for": self.usable_for,
        }


LADDER: List[VideoTier] = [
    VideoTier("1080p30", 1920, 1080, 30, 5.0e6, "defect grading, NASSCO coding"),
    VideoTier("1080p20", 1920, 1080, 20, 3.0e6, "defect grading"),
    VideoTier("720p30", 1280, 720, 30, 1.8e6, "defect grading, smooth driving"),
    VideoTier("720p20", 1280, 720, 20, 1.1e6, "crack detection"),
    VideoTier("480p20", 854, 480, 20, 650e3, "joint and lateral survey"),
    VideoTier("480p12", 854, 480, 12, 400e3, "general condition"),
    VideoTier("360p15", 640, 360, 15, 280e3, "navigation, obstacle spotting"),
    VideoTier("360p8", 640, 360, 8, 160e3, "navigation"),
    VideoTier("240p8", 426, 240, 8, 90e3, "driving only"),
    VideoTier("240p4", 426, 240, 4, 50e3, "driving only, degraded"),
    VideoTier("stills-2s", 1280, 720, 0.5, 25e3, "still survey, no live driving"),
    VideoTier("stills-10s", 854, 480, 0.1, 6e3, "proof of life"),
]

NO_VIDEO = VideoTier("no video", 0, 0, 0, 0.0, "telemetry only")


@dataclass
class VideoState:
    """What the operator is receiving right now."""

    tier: VideoTier
    offered_bps: float
    delivered_bps: float
    loss_percent: float
    glass_to_glass_ms: float
    freeze: bool
    headroom_db: float
    fec_overhead: float

    def to_dict(self) -> dict:
        d = self.tier.to_dict()
        d.update({
            "offered_kbps": round(self.offered_bps / 1e3),
            "delivered_kbps": round(self.delivered_bps / 1e3),
            "loss_percent": round(self.loss_percent, 2),
            "glass_to_glass_ms": round(self.glass_to_glass_ms, 0),
            "freeze": self.freeze,
            "fec_overhead": round(self.fec_overhead, 2),
        })
        return d


def fec_overhead_for(per: float) -> float:
    """Forward error correction needed to survive a given packet error rate."""
    per = max(min(per, 0.9), 0.0)
    return 1.0 + min(2.5 * per, 0.6)


def select_tier(available_bps: float, per: float = 0.0,
                control_reserve_bps: float = 32e3,
                safety: float = 0.85) -> VideoTier:
    """Highest ladder rung that fits the budget, with the control slice reserved.

    Control always wins: the slice that carries drive commands and the stop is
    taken out of the budget before video gets to bid for anything.
    """
    budget = (available_bps - control_reserve_bps) * safety
    budget /= fec_overhead_for(per)
    if budget <= 0:
        return NO_VIDEO
    for tier in LADDER:
        if tier.required_bps <= budget:
            return tier
    return NO_VIDEO


def glass_to_glass_ms(tier: VideoTier, network_ms: float, jitter_ms: float,
                      capture_encode_ms: float = 22.0,
                      decode_ms: float = 9.0) -> float:
    """End-to-end delay the operator perceives, camera photons to screen."""
    if tier.fps <= 0:
        return float("inf")
    frame_interval = 1000.0 / tier.fps
    jitter_buffer = max(2.0 * jitter_ms, 15.0)
    return frame_interval + capture_encode_ms + network_ms + jitter_buffer + decode_ms


def build_video_state(available_bps: float, per: float, network_ms: float,
                      jitter_ms: float, control_reserve_bps: float = 32e3,
                      margin_db: float = 0.0) -> VideoState:
    tier = select_tier(available_bps, per, control_reserve_bps)
    fec = fec_overhead_for(per)
    offered = tier.required_bps * fec
    residual_loss = max(0.0, per ** 4) * 100.0
    delivered = offered * (1.0 - residual_loss / 100.0)
    freeze = tier is NO_VIDEO
    return VideoState(
        tier=tier,
        offered_bps=offered,
        delivered_bps=delivered,
        loss_percent=residual_loss,
        glass_to_glass_ms=glass_to_glass_ms(tier, network_ms, jitter_ms),
        freeze=freeze,
        headroom_db=margin_db,
        fec_overhead=fec,
    )


def ladder_dict() -> List[dict]:
    return [t.to_dict() for t in LADDER]
