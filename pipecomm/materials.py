"""Electromagnetic properties of the materials found inside a buried pipe.

Values are engineering estimates at microwave frequencies drawn from the usual
handbook ranges.  They are deliberately centralised here so a field team can
replace any one of them with a measured value without touching the model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import EPS0


@dataclass(frozen=True)
class Material:
    """A homogeneous medium described by its complex permittivity."""

    name: str
    eps_r: float = 1.0           # real relative permittivity
    sigma: float = 0.0           # conductivity, S/m
    loss_tangent: float = 0.0    # dielectric loss tangent (non-conductive part)
    mu_r: float = 1.0            # relative permeability
    roughness_rms_m: float = 0.0 # RMS surface roughness of a wall of this stuff
    note: str = ""

    def eps_complex(self, f_hz: float) -> complex:
        """Complex relative permittivity eps' - j*eps'' at ``f_hz``."""
        eps_imag = self.eps_r * self.loss_tangent + self.sigma / (2 * math.pi * f_hz * EPS0)
        return complex(self.eps_r, -eps_imag)


AIR = Material("Air", eps_r=1.0)

# --- pipe wall materials -------------------------------------------------
DUCTILE_IRON = Material(
    "Ductile iron", eps_r=1.0, sigma=6.0e6, mu_r=60.0, roughness_rms_m=2.0e-4,
    note="Cement-mortar lined in practice; magnetic, so skin depth is small.",
)
STEEL = Material(
    "Welded steel", eps_r=1.0, sigma=1.0e7, mu_r=100.0, roughness_rms_m=1.5e-4,
    note="Best-case guide: continuous conductive wall, few joints.",
)
CONCRETE = Material(
    "Reinforced concrete", eps_r=6.5, sigma=0.03, loss_tangent=0.08,
    roughness_rms_m=1.2e-3,
    note="RCP / cast-in-place. Lossy dielectric wall; rebar adds scattering.",
)
VCP = Material(
    "Vitrified clay", eps_r=5.0, sigma=0.01, loss_tangent=0.04,
    roughness_rms_m=6.0e-4, note="Common legacy sanitary sewer material.",
)
PVC = Material(
    "PVC / HDPE", eps_r=3.0, sigma=1e-4, loss_tangent=0.012,
    roughness_rms_m=5.0e-5,
    note="Low-loss wall but semi-transparent: energy leaks into the soil.",
)

# --- fills and coatings --------------------------------------------------
POTABLE_WATER = Material(
    "Potable water", eps_r=78.0, sigma=0.02, loss_tangent=0.16,
    note="Debye relaxation dominates the loss at 2.4 GHz.",
)
WASTEWATER = Material(
    "Wastewater", eps_r=74.0, sigma=0.25, loss_tangent=0.16,
    note="Higher ionic conductivity than potable water.",
)
BIOFILM = Material(
    "Biofilm / slime", eps_r=55.0, sigma=0.6, loss_tangent=0.20,
    note="Thin high-loss layer on the wetted perimeter; the dominant wall loss.",
)
SCALE = Material(
    "Mineral scale / tuberculation", eps_r=8.0, sigma=0.02, loss_tangent=0.06,
    note="Rough, high-permittivity crust in unlined iron mains.",
)
SOIL_WET = Material(
    "Wet soil", eps_r=20.0, sigma=0.05, loss_tangent=0.1,
    note="What the signal meets if it leaks out of a plastic pipe.",
)

WALL_MATERIALS = {
    "ductile_iron": DUCTILE_IRON,
    "steel": STEEL,
    "concrete": CONCRETE,
    "vcp": VCP,
    "pvc": PVC,
}

FILL_MATERIALS = {
    "none": AIR,
    "potable": POTABLE_WATER,
    "wastewater": WASTEWATER,
}

COATINGS = {
    "none": None,
    "biofilm": BIOFILM,
    "scale": SCALE,
}

CEMENT_MORTAR = Material(
    "Cement mortar lining", eps_r=6.0, sigma=0.02, loss_tangent=0.06,
    roughness_rms_m=5.0e-4,
    note="Standard inside a ductile iron main. It hides the metal from the "
         "wave, so a lined iron pipe propagates more like concrete.",
)
COATINGS["mortar"] = CEMENT_MORTAR
