"""Representative pipe runs to test the communication architecture against.

Each scenario is a two-mile (or shorter) run that a Garney crew could plausibly
be asked to inspect.  They are chosen to bracket the problem: one that should
work easily, two that are realistic, and one that is meant to fail so the study
has a documented failure boundary.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Dict, List

from .constants import MILE_M
from .link import RadioConfig
from .materials import (
    BIOFILM,
    CEMENT_MORTAR,
    CONCRETE,
    DUCTILE_IRON,
    SCALE,
    VCP,
    WASTEWATER,
    POTABLE_WATER,
)
from .pipe import (
    PipeFeature,
    PipeProfile,
    PipeSection,
    make_bend,
    make_debris,
    make_manhole,
    make_offset,
    repetitive_loss_db_per_m,
)
from .waveguide import SegmentSpec, cutoff_frequency, joint_loss_db

TWO_MILES = 2.0 * MILE_M  # 3218.7 m

IN = 0.0254


@dataclass
class Scenario:
    """A named test case: a pipe, a radio, and why it is interesting."""

    key: str
    title: str
    difficulty: str
    why: str
    profile: PipeProfile
    radio: RadioConfig

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "difficulty": self.difficulty,
            "why": self.why,
            "pipe": self.profile.to_dict(self.radio.frequency_hz),
            "radio": self.radio.to_dict(),
        }


def _joint_excess(spacing_m: float, gap_m: float, offset_m: float,
                  radius_m: float) -> float:
    return repetitive_loss_db_per_m(spacing_m, joint_loss_db(gap_m, offset_m, radius_m))


# --------------------------------------------------------------------------
# Scenario builders
# --------------------------------------------------------------------------


def water_main_24() -> Scenario:
    """24 in cement-mortar-lined ductile iron water transmission main, dewatered."""
    d = 24 * IN
    a = d / 2
    rng = random.Random(24)
    excess = _joint_excess(5.5, 0.006, 0.003, a) + 0.0004  # joints + ovality
    spec = SegmentSpec(
        diameter_m=d, wall=DUCTILE_IRON, coating=CEMENT_MORTAR,
        coating_thickness_m=0.009, excess_loss_db_per_m=excess,
    )
    # A short dewatered-but-damp stretch in a sag.
    sag = SegmentSpec(
        diameter_m=d, wall=DUCTILE_IRON, coating=CEMENT_MORTAR,
        coating_thickness_m=0.009, fill_material=POTABLE_WATER,
        fill_fraction=0.18, excess_loss_db_per_m=excess,
    )
    sections = [
        PipeSection(0, 1180, spec, "dewatered main"),
        PipeSection(1180, 1310, sag, "low point, 18% residual water"),
        PipeSection(1310, TWO_MILES, spec, "dewatered main"),
    ]
    f = 2.437e9
    fc = cutoff_frequency(a)
    features: List[PipeFeature] = [
        make_bend(420, 22.5, 6.0, a, f, fc, "22.5 deg sweep"),
        make_bend(1512, 45.0, 4.5, a, f, fc, "45 deg sweep"),
        make_bend(2390, 11.25, 9.0, a, f, fc, "11.25 deg sweep"),
        make_offset(905, 0.022, a, "offset joint (22 mm)"),
        make_offset(2104, 0.030, a, "offset joint (30 mm)"),
        make_debris(1255, 0.22, "grit in sag"),
        PipeFeature(760, "lateral", "16 in branch tee", 0.9),
        PipeFeature(1880, "lateral", "air valve riser", 0.7),
        make_manhole(1600, 7.0, "valve vault (access shaft)"),
        make_manhole(3100, 7.0, "valve vault (access shaft)"),
    ]
    for _ in range(6):
        x = rng.uniform(150, TWO_MILES - 100)
        features.append(make_offset(x, rng.uniform(0.006, 0.016), a, "minor offset"))
    profile = PipeProfile(
        "24 in DI water transmission main", TWO_MILES, sections, features,
        "Cement-mortar lined ductile iron, dewatered for inspection, two long "
        "sweeps and one sag holding residual water.",
        burial_depth_m=3.5,
    )
    return Scenario(
        "water_main_24", "24 in water transmission main (dewatered)", "baseline",
        "The use case Garney described: few access points, long run, pipe taken "
        "out of service. This is where two miles is actually plausible.",
        profile, RadioConfig(),
    )


def interceptor_36() -> Scenario:
    """36 in reinforced concrete interceptor sewer, live flow."""
    d = 36 * IN
    a = d / 2
    rng = random.Random(36)
    excess = (_joint_excess(2.44, 0.010, 0.005, a)
              + repetitive_loss_db_per_m(25.0, 0.15)   # service laterals
              + 0.0015)                                # rebar scatter, ovality
    def sec(fill):
        return SegmentSpec(
            diameter_m=d, wall=CONCRETE, coating=BIOFILM, coating_thickness_m=0.002,
            fill_material=WASTEWATER, fill_fraction=fill,
            excess_loss_db_per_m=excess,
        )
    sections = [
        PipeSection(0, 900, sec(0.18), "normal flow, 18% full"),
        PipeSection(900, 1450, sec(0.32), "flat grade, 32% full"),
        PipeSection(1450, 2600, sec(0.20), "normal flow, 20% full"),
        PipeSection(2600, TWO_MILES, sec(0.45), "surcharged reach, 45% full"),
    ]
    f = 2.437e9
    fc = cutoff_frequency(a)
    features: List[PipeFeature] = [
        make_bend(1180, 30.0, 8.0, a, f, fc, "30 deg sweep"),
        make_bend(2740, 22.5, 7.0, a, f, fc, "22.5 deg sweep"),
        make_debris(640, 0.18, "grease and rag build-up"),
        make_debris(2180, 0.30, "root intrusion"),
        make_offset(1495, 0.040, a, "offset joint (40 mm)"),
    ]
    for x in range(300, int(TWO_MILES), 300):
        features.append(make_manhole(float(x), 8.0, "manhole"))
    for _ in range(8):
        features.append(make_offset(rng.uniform(100, TWO_MILES - 100),
                                    rng.uniform(0.008, 0.025), a, "minor offset"))
    profile = PipeProfile(
        "36 in RCP interceptor sewer", TWO_MILES, sections, features,
        "Live interceptor with biofilm on the wetted perimeter and a surcharged "
        "reach near the downstream end.",
        burial_depth_m=6.0,
    )
    return Scenario(
        "interceptor_36", "36 in concrete interceptor (live flow)", "realistic",
        "Large diameter helps, but biofilm and a water surface in the invert "
        "take most of it back. The surcharged reach is the real test.",
        profile, RadioConfig(),
    )


def force_main_12() -> Scenario:
    """12 in unlined ductile iron force main with scale and residual water."""
    d = 12 * IN
    a = d / 2
    rng = random.Random(12)
    excess = _joint_excess(5.5, 0.008, 0.004, a) + 0.0008
    def sec(fill):
        return SegmentSpec(
            diameter_m=d, wall=DUCTILE_IRON, coating=SCALE, coating_thickness_m=0.004,
            fill_material=WASTEWATER, fill_fraction=fill,
            excess_loss_db_per_m=excess,
        )
    sections = [
        PipeSection(0, 700, sec(0.08), "drained, 8% residual"),
        PipeSection(700, 1150, sec(0.25), "low point, 25% residual"),
        PipeSection(1150, 2200, sec(0.10), "drained, 10% residual"),
        PipeSection(2200, TWO_MILES, sec(0.16), "drained, 16% residual"),
    ]
    f = 2.437e9
    fc = cutoff_frequency(a)
    features: List[PipeFeature] = [
        make_bend(330, 45.0, 1.2, a, f, fc, "45 deg fabricated bend"),
        make_bend(980, 90.0, 0.9, a, f, fc, "90 deg bend (tight)"),
        make_bend(2010, 22.5, 2.5, a, f, fc, "22.5 deg bend"),
        make_debris(1490, 0.25, "tuberculation ridge"),
        PipeFeature(1700, "lateral", "air release valve", 1.1),
        make_manhole(1950, 6.0, "access pit"),
    ]
    for _ in range(10):
        features.append(make_offset(rng.uniform(80, TWO_MILES - 80),
                                    rng.uniform(0.004, 0.012), a, "minor offset"))
    profile = PipeProfile(
        "12 in DI force main", TWO_MILES, sections, features,
        "Small bore, unlined and tuberculated, with a tight 90 degree bend that "
        "the radio cannot see past.",
        burial_depth_m=2.5,
    )
    return Scenario(
        "force_main_12", "12 in force main with scale and a hard bend", "hard",
        "Small diameter pushes the cutoff up to 576 MHz and the tight bend is "
        "effectively a wall. Shows where a node has to sit on the bend itself.",
        profile, RadioConfig(),
    )


def sewer_8() -> Scenario:
    """8 in vitrified clay sanitary sewer: the documented failure boundary."""
    d = 8 * IN
    a = d / 2
    rng = random.Random(8)
    excess = (_joint_excess(1.22, 0.010, 0.004, a)
              + repetitive_loss_db_per_m(18.0, 0.2)
              + 0.0015)
    def sec(fill):
        return SegmentSpec(
            diameter_m=d, wall=VCP, coating=BIOFILM, coating_thickness_m=0.0008,
            fill_material=WASTEWATER, fill_fraction=fill,
            excess_loss_db_per_m=excess,
        )
    length = 900.0
    sections = [
        PipeSection(0, 320, sec(0.15), "15% full"),
        PipeSection(320, 560, sec(0.28), "flat grade, 28% full"),
        PipeSection(560, length, sec(0.18), "18% full"),
    ]
    f = 2.437e9
    fc = cutoff_frequency(a)
    features: List[PipeFeature] = [
        make_debris(210, 0.35, "root mass"),
        make_debris(615, 0.28, "grease"),
        make_offset(430, 0.020, a, "offset joint (20 mm)"),
        make_bend(700, 22.5, 0.6, a, f, fc, "bend at manhole"),
    ]
    for x in range(100, int(length), 100):
        features.append(make_manhole(float(x), 8.0, "manhole"))
    for _ in range(6):
        features.append(make_offset(rng.uniform(40, length - 40),
                                    rng.uniform(0.004, 0.010), a, "minor offset"))
    profile = PipeProfile(
        "8 in VCP sanitary sewer", length, sections, features,
        "Small clay sewer, biofilm everywhere, joints every four feet. Included "
        "to document where the architecture stops working.",
        burial_depth_m=3.0,
    )
    return Scenario(
        "sewer_8", "8 in clay sewer (failure boundary)", "fails",
        "Cutoff is 865 MHz and the bore is only 1.6 wavelengths across, so the "
        "pipe is barely a waveguide at all. Hops collapse to tens of metres.",
        profile, RadioConfig(),
    )


def tunnel_72() -> Scenario:
    """72 in dry outfall tunnel: the easy case, to bound the upside."""
    d = 72 * IN
    a = d / 2
    excess = _joint_excess(6.0, 0.012, 0.006, a) + 0.0003
    spec = SegmentSpec(diameter_m=d, wall=CONCRETE, excess_loss_db_per_m=excess)
    sections = [PipeSection(0, TWO_MILES, spec, "dry tunnel")]
    f = 2.437e9
    fc = cutoff_frequency(a)
    features = [
        make_bend(1100, 15.0, 30.0, a, f, fc, "long sweep"),
        make_manhole(1609, 6.0, "drop shaft"),
    ]
    profile = PipeProfile(
        "72 in dry outfall tunnel", TWO_MILES, sections, features,
        "Large dry concrete tunnel: the best case the architecture will ever see.",
        burial_depth_m=12.0,
    )
    return Scenario(
        "tunnel_72", "72 in dry tunnel (best case)", "easy",
        "Shows the upside of the waveguide effect: kilometre-class hops and "
        "full-rate video with a handful of nodes.",
        profile, RadioConfig(),
    )


BUILDERS: Dict[str, Callable[[], Scenario]] = {
    "water_main_24": water_main_24,
    "interceptor_36": interceptor_36,
    "force_main_12": force_main_12,
    "sewer_8": sewer_8,
    "tunnel_72": tunnel_72,
}

DEFAULT_SCENARIO = "water_main_24"


def build(key: str = DEFAULT_SCENARIO) -> Scenario:
    if key not in BUILDERS:
        raise KeyError(f"unknown scenario {key!r}; have {sorted(BUILDERS)}")
    return BUILDERS[key]()


def list_scenarios() -> List[dict]:
    out = []
    for key, fn in BUILDERS.items():
        sc = fn()
        out.append({
            "key": key,
            "title": sc.title,
            "difficulty": sc.difficulty,
            "why": sc.why,
            "length_m": sc.profile.length_m,
            "diameter_in": round(sc.profile.diameter_at(0) / IN, 0),
        })
    return out
