"""A longitudinal model of a real pipe run.

A ``PipeProfile`` is a sequence of uniform sections (diameter, material, water
depth) plus a list of point features (joints, bends, offsets, debris, manholes).
Path loss between any two points is the integral of the distributed attenuation
plus the sum of the features in between, which is exactly the breakdown a field
engineer wants to see when a link fails.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .materials import AIR, Material
from .waveguide import (
    SegmentLoss,
    SegmentSpec,
    blockage_loss_db,
    bend_loss_db,
    coherence_bandwidth_hz,
    cutoff_frequency,
    joint_loss_db,
    modal_delay_spread_s,
    segment_loss,
)

FEATURE_KINDS = ("joint", "bend", "offset", "debris", "manhole", "lateral", "sag")


@dataclass
class PipeFeature:
    """A point discontinuity that costs the link some dB."""

    position_m: float
    kind: str
    label: str = ""
    loss_db: float = 0.0
    meta: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "position_m": round(self.position_m, 2),
            "kind": self.kind,
            "label": self.label,
            "loss_db": round(self.loss_db, 2),
            "meta": {k: round(v, 4) for k, v in self.meta.items()},
        }


@dataclass
class PipeSection:
    """A stretch of pipe with uniform geometry and condition."""

    start_m: float
    end_m: float
    spec: SegmentSpec
    label: str = ""

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m


@dataclass
class LossBreakdown:
    """Why a link costs what it costs."""

    total_db: float
    distributed_db: float
    feature_db: float
    features: List[PipeFeature]
    worst_feature: Optional[PipeFeature]
    below_cutoff: bool
    min_cutoff_hz: float
    delay_spread_ns: float
    coherence_bw_hz: float


class PipeProfile:
    """The pipe under inspection: geometry, condition, and discontinuities."""

    def __init__(self, name: str, length_m: float, sections: List[PipeSection],
                 features: List[PipeFeature], description: str = "",
                 burial_depth_m: float = 3.0):
        self.name = name
        self.length_m = length_m
        self.sections = sorted(sections, key=lambda s: s.start_m)
        self.features = sorted(features, key=lambda f: f.position_m)
        self.description = description
        self.burial_depth_m = burial_depth_m
        self._loss_cache: Dict[Tuple[int, int], SegmentLoss] = {}

    # -- geometry ---------------------------------------------------------

    def section_at(self, x_m: float) -> PipeSection:
        x = min(max(x_m, 0.0), self.length_m)
        for s in self.sections:
            if s.start_m <= x <= s.end_m:
                return s
        return self.sections[-1]

    def diameter_at(self, x_m: float) -> float:
        return self.section_at(x_m).spec.diameter_m

    def fill_at(self, x_m: float) -> float:
        return self.section_at(x_m).spec.fill_fraction

    def section_loss(self, f_hz: float, section: PipeSection) -> SegmentLoss:
        key = (id(section), int(f_hz))
        cached = self._loss_cache.get(key)
        if cached is None:
            cached = segment_loss(f_hz, section.spec)
            self._loss_cache[key] = cached
        return cached

    def db_per_m_at(self, f_hz: float, x_m: float) -> float:
        return self.section_loss(f_hz, self.section_at(x_m)).db_per_m

    def cutoff_at(self, f_hz: float, x_m: float) -> float:
        return self.section_loss(f_hz, self.section_at(x_m)).cutoff_hz

    # -- path loss --------------------------------------------------------

    def loss_between(self, f_hz: float, x0_m: float, x1_m: float) -> LossBreakdown:
        """Total in-pipe path loss between two points, with a breakdown."""
        lo, hi = (x0_m, x1_m) if x0_m <= x1_m else (x1_m, x0_m)
        lo = max(lo, 0.0)
        hi = min(hi, self.length_m)

        distributed = 0.0
        below_cutoff = False
        min_cutoff = 0.0
        span = max(hi - lo, 0.0)

        for s in self.sections:
            a = max(lo, s.start_m)
            b = min(hi, s.end_m)
            if b <= a:
                continue
            sl = self.section_loss(f_hz, s)
            distributed += sl.db_per_m * (b - a)
            min_cutoff = max(min_cutoff, sl.cutoff_hz)
            if sl.below_cutoff:
                below_cutoff = True

        crossed = [ft for ft in self.features if lo < ft.position_m <= hi and ft.kind != "manhole"]
        # A manhole between the two ends dumps energy up the shaft.
        crossed += [ft for ft in self.features if lo < ft.position_m <= hi and ft.kind == "manhole"]
        feature_db = sum(ft.loss_db for ft in crossed)
        worst = max(crossed, key=lambda ft: ft.loss_db, default=None)

        tau = modal_delay_spread_s(f_hz, min_cutoff, span)
        return LossBreakdown(
            total_db=distributed + feature_db,
            distributed_db=distributed,
            feature_db=feature_db,
            features=crossed,
            worst_feature=worst,
            below_cutoff=below_cutoff,
            min_cutoff_hz=min_cutoff,
            delay_spread_ns=tau * 1e9,
            coherence_bw_hz=coherence_bandwidth_hz(tau),
        )

    # -- serialisation ----------------------------------------------------

    def to_dict(self, f_hz: float) -> dict:
        sections = []
        for s in self.sections:
            sl = self.section_loss(f_hz, s)
            sections.append({
                "start_m": round(s.start_m, 1),
                "end_m": round(s.end_m, 1),
                "label": s.label,
                "diameter_m": round(s.spec.diameter_m, 4),
                "diameter_in": round(s.spec.diameter_m / 0.0254, 1),
                "wall": s.spec.wall.name,
                "fill_fraction": round(s.spec.fill_fraction, 3),
                "fill": s.spec.fill_material.name,
                "db_per_km": round(sl.db_per_m * 1000.0, 2),
                "cutoff_mhz": round(sl.cutoff_hz / 1e6, 1),
                "below_cutoff": sl.below_cutoff,
                "water_bounce_fraction": round(sl.water_bounce_fraction, 3),
                "d_over_lambda": round(sl.d_over_lambda, 2),
                "model_valid": sl.model_valid,
            })
        return {
            "name": self.name,
            "description": self.description,
            "length_m": self.length_m,
            "burial_depth_m": self.burial_depth_m,
            "sections": sections,
            "features": [f.to_dict() for f in self.features],
        }


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def make_joints(length_m: float, spacing_m: float, pipe_radius_m: float,
                gap_m: float = 0.006, offset_m: float = 0.004,
                start_m: float = 0.0) -> List[PipeFeature]:
    """One joint every ``spacing_m``, each with a gap and a small offset."""
    out: List[PipeFeature] = []
    x = start_m + spacing_m
    while x < length_m:
        loss = joint_loss_db(gap_m, offset_m, pipe_radius_m)
        out.append(PipeFeature(x, "joint", "pipe joint", loss,
                               {"gap_m": gap_m, "offset_m": offset_m}))
        x += spacing_m
    return out


def repetitive_loss_db_per_m(spacing_m: float, loss_per_item_db: float) -> float:
    """Smear a regularly repeating feature into the distributed attenuation.

    A two-mile run holds well over a thousand joints and a hundred service
    laterals.  Listing each one is noise; what matters is the dB per kilometre
    they add, which is what this returns.
    """
    if spacing_m <= 0:
        return 0.0
    return loss_per_item_db / spacing_m


def make_bend(position_m: float, angle_deg: float, bend_radius_m: float,
              pipe_radius_m: float, f_hz: float, f_c: float,
              label: str = "") -> PipeFeature:
    loss = bend_loss_db(angle_deg, bend_radius_m, pipe_radius_m, f_hz, f_c)
    return PipeFeature(position_m, "bend", label or f"{angle_deg:g} deg bend", loss,
                       {"angle_deg": angle_deg, "radius_m": bend_radius_m})


def make_debris(position_m: float, blocked_fraction: float, label: str = "") -> PipeFeature:
    return PipeFeature(position_m, "debris", label or "debris / grit",
                       blockage_loss_db(blocked_fraction),
                       {"blocked_fraction": blocked_fraction})


def make_offset(position_m: float, offset_m: float, pipe_radius_m: float,
                label: str = "") -> PipeFeature:
    return PipeFeature(position_m, "offset", label or "offset joint",
                       joint_loss_db(0.012, offset_m, pipe_radius_m),
                       {"offset_m": offset_m})


def make_manhole(position_m: float, loss_db: float = 9.0, label: str = "") -> PipeFeature:
    """A manhole is a hole in the waveguide: energy escapes up the shaft.

    It is also the one place a surface relay can be dropped in, so the planner
    treats manholes as preferred repeater sites.
    """
    return PipeFeature(position_m, "manhole", label or "manhole", loss_db, {})


def make_lateral(position_m: float, loss_db: float = 1.6, label: str = "") -> PipeFeature:
    return PipeFeature(position_m, "lateral", label or "service lateral", loss_db, {})
