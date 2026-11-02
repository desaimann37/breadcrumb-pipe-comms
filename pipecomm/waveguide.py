"""The pipe treated as an oversized (overmoded) circular waveguide.

Why this model
--------------
A buried pipe is not an obstacle to radio: it is a *waveguide*.  Below the TE11
cutoff frequency nothing propagates at all (the field decays exponentially);
well above cutoff the pipe behaves like a very oversized guide whose theoretical
loss is tiny.  That is the whole basis for untethered in-pipe communication, and
it is why picking the carrier frequency is the single most important decision in
the architecture.

The loss is computed with a grazing-ray (geometric optics) model, which is the
standard way to handle a guide many wavelengths across:

1.  The dominant ray travels at an angle theta from the pipe axis, where
    sin(theta) = f_c / f.  Well above cutoff the ray is nearly axial.
2.  A ray bouncing across the bore makes tan(theta)/D reflections per metre of
    axial travel.  A wider pipe, or a higher frequency, means fewer bounces.
3.  Each bounce loses -20*log10|Gamma| dB, where Gamma is the Fresnel
    reflection coefficient at near-grazing incidence, computed through any
    biofilm/scale layer onto the wall substrate and derated for roughness.
4.  Attenuation (dB/m) = bounces per metre x loss per bounce.

Partially full pipes are handled by replacing the bore with the air segment
above the water line: the cutoff rises (smaller effective aperture) and a share
of the bounces land on water, which is a far worse mirror than any pipe wall.
That one effect is usually the difference between a working link and a dead one.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass
from typing import Optional

from .constants import C, ETA0, TE11_EIGENVALUE
from .materials import AIR, Material

NEPER_TO_DB = 8.685_889_638

# The grazing-ray treatment assumes the bore is several wavelengths across.
# Below this the pipe is only mildly oversized, few modes propagate, and the
# numbers should be treated as indicative until a bench measurement backs them.
MIN_D_OVER_LAMBDA = 3.0


def cutoff_frequency(radius_m: float, eps_r_fill: float = 1.0) -> float:
    """TE11 cutoff of a circular guide of ``radius_m`` (Hz)."""
    if radius_m <= 0:
        return float("inf")
    return TE11_EIGENVALUE * C / (2 * math.pi * radius_m * math.sqrt(eps_r_fill))


def evanescent_attenuation_db_per_m(f_hz: float, f_c: float) -> float:
    """Attenuation below cutoff: the field decays, it does not propagate."""
    if f_hz >= f_c:
        return 0.0
    alpha_np = (2 * math.pi * f_c / C) * math.sqrt(1.0 - (f_hz / f_c) ** 2)
    return NEPER_TO_DB * alpha_np


def modal_delay_spread_s(f_hz: float, f_c: float, length_m: float) -> float:
    """Delay spread from modal dispersion over ``length_m``.

    Axial group velocity is c*sqrt(1-(fc/f)^2).  The spread between axial and
    dominant-mode energy sets the guard interval an OFDM waveform must carry.
    """
    if f_hz <= f_c or length_m <= 0:
        return 0.0
    beta = math.sqrt(1.0 - (f_c / f_hz) ** 2)
    return (length_m / C) * (1.0 / beta - 1.0)


def coherence_bandwidth_hz(delay_spread_s: float) -> float:
    """Rule-of-thumb coherence bandwidth, 1/(5*tau)."""
    if delay_spread_s <= 0:
        return float("inf")
    return 1.0 / (5.0 * delay_spread_s)


def _cos_theta_t(eps_c: complex, mu_r: float, sin_phi_i: float) -> complex:
    """Cosine of the refraction angle inside a medium (complex Snell)."""
    n2 = eps_c * mu_r
    return cmath.sqrt(1.0 - (sin_phi_i ** 2) / n2)


def _wave_impedance(eps_c: complex, mu_r: float, sin_phi_i: float,
                    polarisation: str) -> complex:
    """Transverse wave impedance inside a medium, per polarisation."""
    eta = ETA0 * cmath.sqrt(complex(mu_r, 0.0) / eps_c)
    cos_t = _cos_theta_t(eps_c, mu_r, sin_phi_i)
    return eta / cos_t if polarisation == "TE" else eta * cos_t


def reflection_coefficient(
    f_hz: float,
    cos_phi_i: float,
    substrate: Material,
    coating: Optional[Material] = None,
    coating_thickness_m: float = 0.0,
    polarisation: str = "TE",
) -> complex:
    """Fresnel reflection off a wall, optionally through a coating layer.

    ``cos_phi_i`` is the cosine of the incidence angle from the wall normal.
    Near-grazing incidence means it is close to zero, which is exactly the
    regime an oversized waveguide operates in.  ``polarisation`` selects TE
    (perpendicular) or TM (parallel); the mode a pipe actually carries needs
    both, see :func:`hybrid_bounce_loss_db`.
    """
    cos_phi_i = min(max(cos_phi_i, 1e-9), 1.0)
    sin_phi_i = math.sqrt(max(0.0, 1.0 - cos_phi_i ** 2))

    z_air = ETA0 / cos_phi_i if polarisation == "TE" else ETA0 * cos_phi_i
    eps_sub = substrate.eps_complex(f_hz)
    z_sub = _wave_impedance(eps_sub, substrate.mu_r, sin_phi_i, polarisation)

    if coating is not None and coating_thickness_m > 0:
        eps_c = coating.eps_complex(f_hz)
        cos_t = _cos_theta_t(eps_c, coating.mu_r, sin_phi_i)
        z_c = _wave_impedance(eps_c, coating.mu_r, sin_phi_i, polarisation)
        k0 = 2 * math.pi * f_hz / C
        beta_n = k0 * cmath.sqrt(eps_c * coating.mu_r) * cos_t
        phase = beta_n * coating_thickness_m
        if phase.imag < -30:
            z_in = z_c
        else:
            t = cmath.tan(phase)
            z_in = z_c * (z_sub + 1j * z_c * t) / (z_c + 1j * z_sub * t)
    else:
        z_in = z_sub

    return (z_in - z_air) / (z_in + z_air)


def hybrid_bounce_loss_db(f_hz: float, cos_phi_i: float, substrate: Material,
                          coating: Optional[Material] = None,
                          coating_thickness_m: float = 0.0) -> float:
    """Loss per wall bounce for the hybrid mode a round pipe actually carries.

    A dipole in a circular pipe launches HE11, which is not purely TE or TM: it
    carries both polarisations.  Averaging the two reflected powers reproduces
    the classical hollow-dielectric-waveguide result, where the attenuation
    scales with ``(eps + 1) / (2 * sqrt(eps - 1))`` (Marcatili & Schmeltzer,
    1964) rather than with the TE term alone.

    The distinction is not academic.  TE alone says a lossy dielectric film
    *improves* a concrete wall, because a high-permittivity layer reflects
    better at grazing.  Including TM, which has a Brewster-like null against
    any dielectric, gives the answer field crews actually see: biofilm, scale
    and standing water all make the pipe worse, and metal beats concrete by
    orders of magnitude.
    """
    g_te = reflection_coefficient(f_hz, cos_phi_i, substrate, coating,
                                  coating_thickness_m, "TE")
    g_tm = reflection_coefficient(f_hz, cos_phi_i, substrate, coating,
                                  coating_thickness_m, "TM")
    power = 0.5 * (min(abs(g_te), 1.0) ** 2 + min(abs(g_tm), 1.0) ** 2)
    power = min(max(power, 1e-12), 1.0)
    return -10.0 * math.log10(power)


def roughness_factor(f_hz: float, cos_phi_i: float, rms_m: float) -> float:
    """Ament/Rayleigh specular derating for a rough wall (amplitude ratio).

    At grazing incidence a rough wall still looks smooth, which is another
    reason the oversized-guide regime is forgiving.
    """
    if rms_m <= 0:
        return 1.0
    k = 2 * math.pi * f_hz / C
    g = (2.0 * k * rms_m * cos_phi_i) ** 2
    return math.exp(-0.5 * min(g, 60.0))


@dataclass
class AirSegment:
    """Geometry of the air space above the water line."""

    area_m2: float
    arc_len_m: float
    chord_len_m: float
    effective_radius_m: float
    water_bounce_fraction: float


def air_segment(radius_m: float, fill_fraction: float) -> AirSegment:
    """Air space above a water line filling ``fill_fraction`` of the depth."""
    a = radius_m
    fill = min(max(fill_fraction, 0.0), 0.999)
    t = (2.0 * a * fill - a) / a
    t = min(max(t, -1.0), 1.0)
    root = math.sqrt(max(0.0, 1.0 - t * t))
    arc_angle = math.pi - 2.0 * math.asin(t)
    area = a * a * (arc_angle / 2.0 - t * root)
    arc_len = a * arc_angle
    chord = 2.0 * a * root
    r_eff = math.sqrt(max(area, 1e-12) / math.pi)
    denom = arc_len + chord
    water_frac = (chord / denom) if denom > 0 else 0.0
    return AirSegment(area, arc_len, chord, r_eff, water_frac)


@dataclass
class SegmentSpec:
    """Everything the loss model needs about a uniform stretch of pipe."""

    diameter_m: float
    wall: Material
    fill_material: Material = AIR
    fill_fraction: float = 0.0
    coating: Optional[Material] = None
    coating_thickness_m: float = 0.0
    excess_loss_db_per_m: float = 0.0
    leak_loss_db_per_m: float = 0.0


@dataclass
class SegmentLoss:
    """Attenuation of a segment plus the diagnostics that explain it."""

    db_per_m: float
    cutoff_hz: float
    effective_radius_m: float
    bounces_per_m: float
    wall_loss_db_per_bounce: float
    water_loss_db_per_bounce: float
    water_bounce_fraction: float
    below_cutoff: bool
    ray_angle_deg: float
    d_over_lambda: float = 0.0
    model_valid: bool = True


def segment_loss(f_hz: float, seg: SegmentSpec) -> SegmentLoss:
    """Attenuation in dB/m for a uniform stretch of pipe at ``f_hz``."""
    a = seg.diameter_m / 2.0
    wet = seg.fill_fraction > 0.001 and seg.fill_material is not AIR
    geom = air_segment(a, seg.fill_fraction if wet else 0.0)
    a_eff = geom.effective_radius_m if wet else a

    f_c = cutoff_frequency(a_eff)
    if f_hz <= f_c * 1.0001:
        return SegmentLoss(
            db_per_m=max(evanescent_attenuation_db_per_m(f_hz, f_c), 1e3),
            cutoff_hz=f_c,
            effective_radius_m=a_eff,
            bounces_per_m=0.0,
            wall_loss_db_per_bounce=float("inf"),
            water_loss_db_per_bounce=float("inf"),
            water_bounce_fraction=geom.water_bounce_fraction if wet else 0.0,
            below_cutoff=True,
            ray_angle_deg=90.0,
            d_over_lambda=2.0 * a_eff * f_hz / C,
            model_valid=False,
        )

    d_over_lambda = 2.0 * a_eff * f_hz / C
    ratio = f_c / f_hz
    theta = math.asin(min(ratio, 1.0))
    bounces = math.tan(theta) / (2.0 * a_eff)
    cos_phi_i = ratio

    def bounce_loss(sub: Material, coat, coat_t) -> float:
        db = hybrid_bounce_loss_db(f_hz, cos_phi_i, sub, coat, coat_t)
        rough = roughness_factor(f_hz, cos_phi_i, sub.roughness_rms_m)
        return db - 20.0 * math.log10(max(rough, 1e-6))

    wall_db = bounce_loss(seg.wall, seg.coating, seg.coating_thickness_m)
    water_db = bounce_loss(seg.fill_material, None, 0.0) if wet else 0.0

    wfrac = geom.water_bounce_fraction if wet else 0.0
    per_bounce = (1.0 - wfrac) * wall_db + wfrac * water_db
    db_per_m = bounces * per_bounce + seg.excess_loss_db_per_m + seg.leak_loss_db_per_m

    return SegmentLoss(
        db_per_m=db_per_m,
        cutoff_hz=f_c,
        effective_radius_m=a_eff,
        bounces_per_m=bounces,
        wall_loss_db_per_bounce=wall_db,
        water_loss_db_per_bounce=water_db,
        water_bounce_fraction=wfrac,
        below_cutoff=False,
        ray_angle_deg=math.degrees(theta),
        d_over_lambda=d_over_lambda,
        model_valid=d_over_lambda >= MIN_D_OVER_LAMBDA,
    )


def bend_loss_db(angle_deg: float, bend_radius_m: float, pipe_radius_m: float,
                 f_hz: float, f_c: float) -> float:
    """Loss at a bend from mode conversion in an oversized guide.

    Loss grows with turn angle and with tightness a/R.  A mitred elbow (R ~ a)
    is effectively opaque; a long-radius sweep costs a few dB.  The coefficient
    is calibrated so a 90 degree mitre is about 25 dB and a 5D sweep about 2 dB.
    """
    if angle_deg <= 0:
        return 0.0
    R = max(bend_radius_m, pipe_radius_m)
    tightness = (pipe_radius_m / R) ** 1.5
    f_norm = max(1.0, f_hz / max(f_c, 1.0))
    return 8.686 * 0.45 * math.radians(angle_deg) * tightness * min(f_norm, 12.0)


def joint_loss_db(gap_m: float, offset_m: float, pipe_radius_m: float) -> float:
    """Loss at a pipe joint from the gap and any radial misalignment.

    A gasketed joint is nearly invisible to a grazing wave: the gap interrupts
    the wall over a few millimetres of axial length, and almost no bounces land
    there.  Radial misalignment is the term that matters, because it masks part
    of the aperture.  A well-laid joint costs hundredths of a dB; a dropped or
    offset joint costs a dB or more, and they add up over a two-mile run.
    """
    d = max(pipe_radius_m * 2.0, 1e-6)
    overlap = max(1.0 - offset_m / d, 0.02)
    gap_term = 0.35 * (gap_m / d)
    return -20.0 * math.log10(overlap) + gap_term


def blockage_loss_db(blocked_area_fraction: float) -> float:
    """Aperture blockage by debris, a protruding tap, or a grit-filled sag."""
    open_frac = max(1.0 - blocked_area_fraction, 0.01)
    return -20.0 * math.log10(open_frac)


def te11_metal_attenuation_db_per_m(f_hz: float, radius_m: float,
                                    conductivity: float, mu_r: float = 1.0) -> float:
    """Exact TE11 conductor loss for a circular metal guide, for validation.

    This is the closed-form textbook result (Pozar, *Microwave Engineering*):

        alpha_c = R_s / (a * eta * sqrt(1-(fc/f)^2)) * [ (fc/f)^2 + 1/(p'^2-1) ]

    The grazing-ray model in :func:`segment_loss` has to agree with it for a
    bare metal pipe, and :mod:`tests.test_physics` asserts that it does.  Note
    what it says about frequency: the bracket tends to a constant, so TE11 loss
    rises as sqrt(f).  Only the TE0n modes get better with frequency, and a
    dipole in a pipe does not launch those.
    """
    f_c = cutoff_frequency(radius_m)
    if f_hz <= f_c:
        return evanescent_attenuation_db_per_m(f_hz, f_c)
    r_s = math.sqrt(math.pi * f_hz * 4e-7 * math.pi * mu_r / conductivity)
    ratio2 = (f_c / f_hz) ** 2
    bracket = ratio2 + 1.0 / (TE11_EIGENVALUE ** 2 - 1.0)
    alpha_np = r_s / (radius_m * ETA0 * math.sqrt(1.0 - ratio2)) * bracket
    return NEPER_TO_DB * alpha_np
