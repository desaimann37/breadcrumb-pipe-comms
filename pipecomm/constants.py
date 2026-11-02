"""Physical constants (SI) used across the link model."""

import math

C = 299_792_458.0            # speed of light, m/s
EPS0 = 8.854_187_8128e-12    # vacuum permittivity, F/m
MU0 = 4e-7 * math.pi         # vacuum permeability, H/m
ETA0 = 376.730_313_668       # free-space wave impedance, ohm
K_BOLTZ = 1.380_649e-23      # Boltzmann constant, J/K
T0 = 290.0                   # reference noise temperature, K

# Thermal noise floor in a 1 Hz bandwidth at T0, in dBm.
#   10*log10(k*T0*1000) = -173.98 dBm/Hz
NOISE_FLOOR_DBM_HZ = 10.0 * math.log10(K_BOLTZ * T0 * 1000.0)

# First root of J1'(x): the TE11 cutoff eigenvalue for a circular guide.
TE11_EIGENVALUE = 1.841_183_781_340_659_3

FEET_PER_METER = 3.280_839_895
MILE_M = 1609.344
