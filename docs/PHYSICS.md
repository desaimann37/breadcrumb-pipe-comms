# The physics, and how far to trust it

Everything the simulator claims comes from this model. It is written down here
so it can be argued with.

## 1. The pipe is a waveguide

A hollow pipe of radius `a` supports no propagation below its TE11 cutoff:

```
f_c = 1.8412 * c / (2 * pi * a)
```

| Pipe ID | Cutoff |
|---|---|
| 6 in | 1153 MHz |
| 8 in | 865 MHz |
| 12 in | 576 MHz |
| 24 in | 288 MHz |
| 36 in | 192 MHz |
| 72 in | 96 MHz |

Below cutoff the field is evanescent and decays as
`exp(-2*pi*f_c/c * sqrt(1-(f/f_c)^2))`, which is tens of dB per metre. This is
not a weak signal, it is no signal, and more power does not help. It is the
single most important number in the project, and it is why a 915 MHz
long-range radio — the first thing most people reach for — is the wrong
answer in anything smaller than about 16 inches.

## 2. Attenuation above cutoff: the grazing-ray model

Well above cutoff the pipe is *oversized* (many wavelengths across) and
geometric optics is the standard treatment:

1. The guided ray travels at an angle `theta` from the pipe axis, where
   `sin(theta) = f_c / f`. Far above cutoff the ray is almost axial.
2. Crossing the bore, it makes `tan(theta) / D` reflections per metre.
3. Each reflection loses `-10*log10|Gamma|^2` dB.
4. `alpha [dB/m] = bounces per metre * loss per bounce`.

Both factors scale as `1/a`, so attenuation scales as roughly **`1/a^3`**:
doubling the diameter cuts the loss about eightfold. That is the same scaling
as the classical hollow-dielectric-waveguide result
(`alpha ∝ lambda^2 / a^3`), and it is the reason a 72 in tunnel and an 8 in
sewer are not the same engineering problem.

## 3. Polarisation: the detail that decides the answer

A dipole in a round pipe launches the hybrid HE11 mode, which carries **both**
polarisations. They behave very differently at grazing incidence:

- **TE (perpendicular)**: reflection improves as wall permittivity rises.
- **TM (parallel)**: has a Brewster-like null against any dielectric, so it
  reflects badly off concrete, clay, biofilm and water.

Modelling TE alone produces a result that is wrong in a way that would have
been embarrassing in front of a client: it says biofilm and standing water
*improve* a concrete pipe, because a high-permittivity film is a better mirror
at grazing. Averaging the two reflected powers — which is what the hybrid mode
experiences — reproduces the classical hollow-guide loss factor

```
nu(eps) = (eps + 1) / (2 * sqrt(eps - 1))      (Marcatili & Schmeltzer, 1964)
```

and gives the answer crews actually see: fouling, scale, lining and water all
make the pipe worse. `tests/test_physics.py::test_fouling_always_costs` pins
this down so it cannot regress.

## 4. Validation against a known-good answer

For a bare metal pipe there is a closed form (Pozar, *Microwave Engineering*):

```
alpha_c = R_s / (a * eta * sqrt(1-(f_c/f)^2)) * [ (f_c/f)^2 + 1/(p'^2 - 1) ]
```

The ray model has to land on it. It does, within 14–17% across a 4x range of
diameters and a 6x range of frequencies:

| Pipe | Carrier | Ray model | Exact TE11 | Ratio |
|---|---|---|---|---|
| 12 in | 915 MHz | 26.0 dB/km | 30.2 dB/km | 0.86 |
| 12 in | 2.44 GHz | 25.3 | 22.8 | 1.11 |
| 24 in | 2.44 GHz | 11.8 | 10.2 | 1.16 |
| 48 in | 5.8 GHz | 8.6 | 7.6 | 1.13 |

This is the only part of the model with an independent ground truth, which is
why it is the test that matters most.

Note what the exact formula says about frequency: the bracket tends to a
constant, so **TE11 loss in metal rises as `sqrt(f)`**. Only the TE0n modes get
better with frequency, and a dipole does not launch those. In dielectric pipes
the grazing angle dominates instead, and higher frequency wins. There is
therefore no single best carrier for all pipe — it depends on the material.

## 5. What makes a real pipe worse than a textbook one

| Effect | Treatment |
|---|---|
| **Water in the invert** | The air space above the water line becomes the guide: a smaller effective aperture raises the cutoff and steepens the rays, and a share of the bounces land on water instead of wall. In a metal pipe this is an order-of-magnitude effect. |
| **Biofilm, scale, mortar lining** | A three-layer (air/film/substrate) grazing reflection via transverse impedance transform. Thin lossy films over a good conductor are very expensive; this is why a cement-mortar-lined ductile iron main behaves much more like concrete than like steel. |
| **Wall roughness** | Ament/Rayleigh specular derating. At grazing incidence a rough wall still looks smooth, so this is a small term — correctly. |
| **Bends** | Mode-conversion loss growing with turn angle and tightness `a/R`. Calibrated so a 90 degree mitre is ~25 dB (effectively a wall) and a 5-diameter sweep is ~2 dB. |
| **Joints** | Dominated by radial misalignment masking the aperture, not by the gap. A good gasketed joint costs hundredths of a dB; over 1,300 joints in two miles that still adds up, so repeated joints are smeared into the distributed loss rather than listed individually. |
| **Debris, roots, offsets** | Aperture blockage, `-20*log10(1 - blocked fraction)`. |
| **Manholes and shafts** | A hole in the waveguide: energy escapes upward. Modelled as a fixed 6–9 dB, and treated by the planner as a preferred relay site. |
| **Modal dispersion** | Delay spread `(L/c)(1/sqrt(1-(fc/f)^2) - 1)`. For 1 km in a 24 in pipe this is ~100 ns, comfortably inside the 800 ns guard interval of an 802.11 OFDM symbol. That is *why* a stock Wi-Fi PHY works inside a pipe at all, and it is asserted in the tests. |
| **Multipath fading** | Not folded into the attenuation. Handled as a required fade margin, plus a slow Ornstein-Uhlenbeck fade per hop in the live simulation (deeper and faster on the moving crawler hop). |
| **Self-interference** | SNR is capped at 30 dB to represent the delay-spread/ISI floor of a heavily overmoded channel. Without this cap the model would promise modulation orders the channel cannot actually support. |

## 6. Limits of validity — read this before quoting a number

- **The ray model needs an oversized guide.** It is validated for bores at
  least **3 wavelengths** across. At 2.4 GHz that means roughly 15 inches and
  up. An 8 in pipe at 2.4 GHz is 1.6 wavelengths across and the model is
  **out of range** — the code flags this (`model_valid`), the dashboard says so,
  and the report marks those rows "indicative only". The direction of the
  answer (an 8 in wet clay sewer is not a two-mile RF candidate) is solid; the
  specific dB/km is not.
- **Material constants are handbook estimates**, not measurements. They live in
  one file, `pipecomm/materials.py`, precisely so a field measurement can
  replace them without touching the model.
- **Bend and joint losses are first-order engineering models** calibrated to
  the right order of magnitude. They have no independent validation.
- **No antenna pattern model.** Coupling into the guide is a flat 3 dB per end.
  A real antenna in a wet pipe will do worse and will detune.
- **The fade model is statistical, not physical.** It produces realistic
  variability, not a prediction of where the nulls will be in your pipe.

## 7. What a single afternoon of measurement would fix

Running the Phase 1 drag test in `TEST_PLAN.md` and feeding the log through
`hardware/bridge/serial_bridge.py calibrate` produces a measured `dB/m` and a
measured fade distribution for the actual pipe. Substituting those two numbers
into the scenario removes the largest source of uncertainty in everything
above. The model is built to be corrected, not believed.
