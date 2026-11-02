# Architecture

## The one-sentence version

A buried pipe is not a barrier to radio, it is an **oversized waveguide**; the
job is therefore not to punch a signal through soil but to keep a signal inside
the pipe, and to drop relay nodes behind the crawler whenever the pipe stops
cooperating.

## Why the obvious approaches do not work

| Instinct | Why it fails |
|---|---|
| "Use a long-range sub-GHz radio (LoRa, 900 MHz)" | Below the pipe's cutoff frequency nothing propagates at all. A 12 in pipe cuts off at 576 MHz; an 8 in pipe at 865 MHz. 915 MHz LoRa in an 8 in sewer is not weak, it is *evanescent*. No amount of transmit power fixes a cutoff. |
| "Punch through the soil with low frequency" | Through-ground propagation at useful bandwidth needs enormous antennas and gives kilobits, not video. It is a good **backup** for emergency signalling near access points, not a primary link. |
| "Just use a longer tether" | The tether is the thing being removed. A 2 mile tether has unmanageable drag, needs a huge drum, and is why inspections stop at 1,000 ft today. |
| "One very powerful radio at each end" | Attenuation in a real pipe is 40–500 dB/km. Twenty more dB of power buys a few hundred metres at best, and regulatory limits cap it anyway. |

## The architecture

```
  access shaft                                                  crawler
       |                                                            |
   [BASE]====R1========R2==========R3==============R4=============[BOT]
       |      \         \           \               \               |
    laptop     dropped relay pucks, recovered on the way out     camera
```

Four pieces:

**1. The pipe as the transmission medium.**
Above cutoff the pipe guides the wave. Loss is set by how often the wave
bounces off the wall and how good a mirror the wall is, which is why diameter
and material matter more than anything else (see `PHYSICS.md`).

**2. Breadcrumb relays.**
The crawler carries a magazine of battery-powered relay pucks and drops one
whenever the link margin back to the last node runs out, or just before an
obstacle the radio cannot see past (a bend, a manhole, a collapsed section).
On the return pass it picks them back up. Nothing is left in the pipe.

**3. Two waveforms, not one.**

| | Video | Control / telemetry |
|---|---|---|
| Waveform | OFDM, 802.11-class | Direct-sequence spread spectrum, 20 dB processing gain |
| Bandwidth | 20 MHz | 5 MHz |
| Usable down to | ~2 dB SNR | ~-16 dB SNR |
| Carries | the picture | drive, status, **emergency stop** |

The control link keeps working roughly **30 dB past the point where video
gives up**. That asymmetry is the safety argument for the whole system: losing
the picture must never mean losing the robot. Both waveforms sit in the same
band, because the control link has to be above the pipe's cutoff too — the
usual trick of putting control on a long-range sub-GHz radio is exactly the
mistake the cutoff table above rules out.

**4. Onboard autonomy as the backstop.**
If no control frame arrives for 600 ms the crawler stops itself. It does not
need to be told. Combined with a battery reserve that is checked against the
*measured* distance home, the failure mode is "robot sitting still in a pipe",
not "robot lost at 1.5 miles".

## Where the bandwidth goes

Video never gets to bid for the whole link. The control slice is reserved
first, then video takes the highest rung of an adaptive ladder that still fits:

```
1080p30 (5.0 Mbps) -> 720p30 (1.8) -> 480p20 (0.65) -> 360p15 (0.28)
   -> 240p8 (0.09) -> stills every 2 s (25 kbps) -> telemetry only
```

Each rung is annotated with what it is good enough *for*. 240p at 8 fps is
useless for grading a crack and perfectly adequate for driving the crawler
back out of the pipe, and that distinction is what lets the system degrade
instead of fail.

## Relay topology and the half-duplex penalty

A single-radio relay cannot receive and transmit at the same time, so a chain
of them shares airtime roughly three ways: end-to-end goodput is the weakest
hop divided by three. Fitting two radios per node on alternating channels lets
non-adjacent hops run simultaneously and recovers that factor of three, at
roughly double the per-node cost. The dashboard toggles between the two so the
trade is visible rather than assumed.

## What bounds the design

1. **Cutoff** bounds the *carrier*: it must be well above `1.841c / (2 pi a)`.
2. **Attenuation** bounds the *hop length*, and therefore the relay count.
3. **Control latency** bounds the *chain depth*. Each hop is a store-and-forward
   delay of roughly 17 ms; past about 35 hops the one-way command latency eats
   the 600 ms watchdog budget. This, not the size of the magazine, is the hard
   ceiling on a single-chain architecture.
4. **Energy** bounds the *mission*. Cutting the tether also cuts shore power: a
   two-mile round trip at 0.3 m/s is about six hours of driving, so the battery
   becomes the heaviest item on the crawler (see `BOM.md`).

## Code map

| Module | Responsibility |
|---|---|
| `pipecomm/waveguide.py` | Cutoff, grazing-ray attenuation, bends, joints, blockages |
| `pipecomm/materials.py` | Every electromagnetic constant, in one place, for replacement by measurement |
| `pipecomm/pipe.py` | A pipe run: sections, features, path loss with a breakdown |
| `pipecomm/mcs.py` | Modulation ladder, packet error model, the control waveform |
| `pipecomm/link.py` | Link budget per hop, and the relay chain end to end |
| `pipecomm/video.py` | Adaptive bitrate ladder and the glass-to-glass latency budget |
| `pipecomm/crawler.py` | Kinematics, energy, magazine, safety watchdog |
| `pipecomm/mission.py` | The live simulation: deployment policy, recovery, events |
| `pipecomm/scenarios.py` | Five representative pipe runs, from easy to impossible |
| `server/app.py` | Stdlib HTTP + server-sent events |
| `web/` | The operator dashboard |
| `cli.py` | Headless runs, relay planning, the decision table and report |
| `hardware/` | ESP32 node firmware and the field-measurement bridge |

The simulation core has no dependency on the server, and the server has no
dependency on the dashboard. `cli.py` and `run.py` are two front ends onto the
same engine, which is what makes the thing testable.
