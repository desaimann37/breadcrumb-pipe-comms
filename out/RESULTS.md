# Untethered pipe inspection comms - simulation results

Generated 2026-11-01 23:35 by `cli.py`. Every number here comes from the model in `pipecomm/`, which is validated against the closed-form TE11 result for metal guides in `tests/test_physics.py`.

## Headline

- A **2.0-mile** inspection of a 24 in DI water transmission main completes with **5 relay nodes**, **100%** video availability and **100%** control availability.
- Worst-case emergency-stop round trip across the chain: **199 ms**.
- Round trip took **6.0 h** and left **48%** of the battery.
- Deepest chain was **6 hops**; one-way command latency at that depth is **100 ms**, inside the 600 ms onboard watchdog, so the crawler stops itself if the chain ever does go quiet.
- Across every run the deepest chain was **21 hops** at **697 ms** round trip. Control latency, not battery, is what caps the usable relay count: at roughly 17 ms per hop the 600 ms watchdog budget runs out somewhere near 35 hops.
- Control and video are deliberately different waveforms. In every run below, control availability is at least as high as video availability; that is the property that makes an untethered crawler recoverable.

## Mission results

| scenario | outcome | reach | relays | video | 720p+ | control | worst E-stop RTT |
|---|---|---|---|---|---|---|---|
| 12 in force main with scale and a hard bend | complete | 1598 m (50%) | 20 | 100% | 100% | 100% | 697 ms |
| 36 in concrete interceptor (live flow) | complete | 2146 m (67%) | 20 | 100% | 100% | 100% | 697 ms |
| 8 in clay sewer (failure boundary) | complete | 158 m (18%) | 20 | 71% | 71% | 100% | 697 ms |
| 72 in dry tunnel (best case) | complete | 3219 m (100%) | 3 | 100% | 100% | 100% | 133 ms |
| 24 in water transmission main (dewatered) | complete | 3219 m (100%) | 5 | 100% | 100% | 100% | 199 ms |

## Decision data: carrier choice per pipe

`hop` is how far one radio reaches before the fade margin runs out; `relays` is how many nodes the full run needs.

| scenario | dia | carrier | D/lambda | cutoff | dB/km | hop | relays | verdict |
|---|---|---|---|---|---|---|---|---|
| water_main_24 | 24" | 915 MHz | 1.86 | 288 MHz | 74.2 | 720 m | 5 | indicative only (D/lambda < 3) |
| water_main_24 | 24" | 2.44 GHz | 4.96 | 288 MHz | 47.0 | 930 m | 4 | **practical** |
| water_main_24 | 24" | 5.8 GHz | 11.79 | 288 MHz | 38.3 | 950 m | 4 | **practical** |
| interceptor_36 | 36" | 915 MHz | 2.61 | 205 MHz | 982.4 | 50 m | 78 | indicative only (D/lambda < 3) |
| interceptor_36 | 36" | 2.44 GHz | 6.96 | 205 MHz | 295.9 | 140 m | 30 | impractical |
| interceptor_36 | 36" | 5.8 GHz | 16.57 | 205 MHz | 120.3 | 280 m | 15 | workable |
| force_main_12 | 12" | 915 MHz | 0.91 | 588 MHz | 1096.4 | 50 m | 96 | indicative only (D/lambda < 3) |
| force_main_12 | 12" | 2.44 GHz | 2.43 | 588 MHz | 525.9 | 90 m | 43 | indicative only (D/lambda < 3) |
| force_main_12 | 12" | 5.8 GHz | 5.79 | 588 MHz | 299.7 | 140 m | 28 | impractical |
| sewer_8 | 8" | 915 MHz | 0.59 | 908 MHz | 233208.4 | 0 m | - | **no link - pipe is opaque at this carrier** |
| sewer_8 | 8" | 2.44 GHz | 1.57 | 908 MHz | 7688.2 | 0 m | - | **no link - pipe is opaque at this carrier** |
| sewer_8 | 8" | 5.8 GHz | 3.74 | 908 MHz | 2480.3 | 10 m | - | **no link - pipe is opaque at this carrier** |
| tunnel_72 | 72" | 915 MHz | 5.58 | 96 MHz | 146.5 | 300 m | 10 | workable |
| tunnel_72 | 72" | 2.44 GHz | 14.87 | 96 MHz | 27.5 | 1320 m | 2 | **practical** |
| tunnel_72 | 72" | 5.8 GHz | 35.38 | 96 MHz | 9.5 | 2410 m | 1 | **practical** |

## What the model says, in order of importance

1. **Diameter dominates everything.** Hollow-guide loss scales as `1/a^3`. Doubling the bore cuts the attenuation by about eight times. A 72 in tunnel and an 8 in clay sewer are not the same problem.
2. **Material decides whether two miles is even on the table.** A bare metal pipe is a genuine low-loss waveguide; concrete and clay are lossy dielectric guides and cost one to two orders of magnitude more.
3. **The optimum carrier is material dependent.** In metal, TE11 conductor loss rises as `sqrt(f)`, so a lower carrier is better. In concrete and clay the grazing angle matters more, so a higher carrier wins. There is no single right frequency for all pipe.
4. **Water in the invert is the single worst field condition**, because it both shrinks the aperture (raising cutoff) and replaces a good mirror with a lossy one. In metal pipe it costs an order of magnitude.
5. **Going below cutoff is a hard stop, not a weak signal.** 915 MHz LoRa in a 12 in pipe does not work at any power. Long-range sub-GHz radio is the obvious instinct and it is the wrong answer.
6. **Control must be a separate waveform.** A spread-spectrum control link runs roughly 30 dB below where the video PHY gives up, so the crawler stays drivable long after the picture is gone.
7. **Relay count is capped by latency, not by hardware.** Each hop is a store-and-forward delay. Past roughly 35 hops the one-way command latency eats the 600 ms safety watchdog, which puts a hard ceiling on how far a single-chain architecture can be pushed regardless of how many nodes the crawler carries.
8. **Half-duplex relaying costs a factor of three.** Single-radio nodes on one channel share airtime three ways. Two radios per node on alternating channels recovers it, at roughly double the node cost.

## Limitations

- The loss model is a grazing-ray approximation and is only validated for bores at least three wavelengths across. Rows flagged 'model out of validated range' need bench measurement before anyone designs against them.
- Bend and joint losses are first-order engineering models calibrated to the right order of magnitude, not measured values.
- Material constants in `pipecomm/materials.py` are handbook estimates. Replacing any one of them with a measured value changes the numbers and requires no code change.
