# BREADCRUMB

**Untethered wireless video and control for long-distance pipe inspection**
Challenge X use case proposed by Garney · George Mason University

A working simulation of a robotic crawler operating inside a closed pipeline:
how the signal gets out, how the commands get in, and how far that can be
pushed toward a two-mile inspection.

```bash
python run.py
```

No dependencies. Python 3.9+. A browser opens on the operator dashboard.

---

## The idea in one paragraph

A buried pipe is not a barrier to radio — it is an **oversized waveguide**.
Below its cutoff frequency nothing propagates at any power; above it, the pipe
actively guides the signal. So the design problem is not punching through soil,
it is staying inside the pipe and dropping a **relay node behind the crawler**
whenever the pipe stops cooperating — at a bend, a manhole, or simply when the
link margin runs out. The crawler collects every node on the way back out.
Video and control run as two different waveforms, because the control link has
to keep working long after the picture is gone.

## What is in here

| | |
|---|---|
| **Live demo** | `python run.py` — operator dashboard: pipe cutaway, relay chain, animated packets, simulated camera degraded by the actual computed link, hop-by-hop link budget, emergency stop |
| **Physics model** | Grazing-ray attenuation in an oversized guide, validated against the closed-form TE11 result for metal pipe to within 14–17% |
| **Evidence generator** | `python cli.py run --all` — writes `out/RESULTS.md`, a decision table and per-run telemetry CSVs |
| **Tests** | 120 tests, including one that caught a real modelling error |
| **Live hardware mode** | `python run.py --live --serial COM5` drives a real crawler from the same console: odometry, measured RSSI per hop, measured e-stop round trip |
| **Hardware path** | ESP32 firmware, a $493 bill of materials, and a bridge that feeds field measurements back into the model |

## Try these four things

```bash
python run.py
```

1. **Watch cutoff kill a link.** Scenario → *12 in force main*, carrier → *433
   MHz*. Below the 588 MHz cutoff the field is evanescent: no link at any
   power. Switch back to 2.437 GHz and it returns. This is why the instinctive
   answer — a long-range 900 MHz radio — is the wrong one.
2. **Run a two-mile inspection.** Scenario → *24 in water transmission main*,
   press `a`, set sim speed to 40x. It drops 5 relays, holds HD video the whole
   way, and recovers every node.
3. **Starve it.** Set relay policy to *Stretch* and the magazine to 2. The
   video ladder walks down 1080p → 720p → 480p → nothing, while the control
   link stays up. Press **space** for an emergency stop and read the round trip.
4. **Get the evidence.** `python cli.py compare` prints which pipe and which
   carrier are practical, which are impossible, and why.

## Headline results

From `python cli.py run --all` (regenerate any time; full output in `out/RESULTS.md`):

| Scenario | Reach | Relays | Video up | Control up | Worst E-stop RTT |
|---|---|---|---|---|---|
| 24 in metal water main (dewatered) | **100%** of 2 miles | 5 | 100% | 100% | 199 ms |
| 72 in dry tunnel | **100%** of 2 miles | 3 | 100% | 100% | 133 ms |
| 36 in concrete interceptor (live flow) | 67% | 20 (magazine limit) | 100% | 100% | 697 ms |
| 12 in force main with scale | 50% | 20 (magazine limit) | 99.9% | 100% | 697 ms |
| 8 in clay sewer | 18% | 20 (magazine limit) | 71% | 100% | 697 ms |

**Control availability was 100% in every run, including the ones where video
died.** That is the property the architecture is built around.

## What the model found

1. **Diameter dominates.** Hollow-guide loss scales as `1/a³`. Doubling the
   bore cuts attenuation roughly eightfold. A 72 in tunnel and an 8 in sewer
   are not the same problem.
2. **Material decides whether two miles is on the table.** Bare metal pipe is a
   genuine low-loss waveguide; concrete and clay cost one to two orders of
   magnitude more. Cement-mortar lining makes an iron main behave like concrete.
3. **There is no single best carrier.** In metal, TE11 conductor loss rises as
   `√f`, so lower is better. In concrete and clay the grazing angle dominates,
   so higher is better — 5.8 GHz roughly doubles the hop length versus 2.4 GHz.
4. **Below cutoff is a hard stop, not a weak signal.** 915 MHz in a 12 in pipe
   does not work at any power.
5. **Water in the invert is the worst field condition**, costing an order of
   magnitude in metal pipe: it shrinks the aperture (raising cutoff) *and*
   replaces a good mirror with a lossy one.
6. **Control must be a separate waveform.** Spread spectrum keeps working about
   30 dB below where the video PHY gives up.
7. **Relay count is capped by latency, not hardware.** At ~17 ms per hop, the
   600 ms safety watchdog runs out near 35 hops.
8. **Cutting the tether cuts shore power.** Six hours of driving at ~60 W needs
   a 620 Wh pack — the heaviest item on the crawler. The untethered requirement
   is as much an energy problem as a radio problem.

## Command line

```bash
python run.py                                  # live dashboard
python run.py --scenario interceptor_36        # start on a different pipe

python cli.py run --scenario water_main_24     # one full autonomous inspection
python cli.py run --all --magazine 20          # every scenario + write the report
python cli.py plan --scenario interceptor_36   # relay spacing, without simulating
python cli.py compare                          # the carrier/pipe decision table

python -m unittest discover -s tests           # 120 tests
node tools/uicheck.js                          # dashboard render check (server must be running)
```

## Running it on a real robot

The dashboard does not change. `--live` swaps the simulated engine for a
hardware one with the identical data contract, so a field test is a direct
measured-against-predicted comparison instead of two tools disagreeing.

```bash
python run.py --live --emulate                 # rehearse with no hardware at all
python run.py --live --serial COM5             # base node on USB
python run.py --live --serial COM5 --camera http://192.168.4.1:81/stream
python run.py --live --udp --udp-peer 192.168.4.1   # base node over Wi-Fi
python run.py --live --replay out/field.log    # review a past run
```

| In live mode | Source |
|---|---|
| Crawler position | wheel odometry |
| Per-hop SNR | measured RSSI from each node |
| Link up/down | telemetry freshness, not a model |
| Control round trip | **measured**: command sent to crawler flag echoed back |
| Relay positions | the crawler's odometry when its actuator fired |
| Pipe context | still modelled, on purpose — that is the comparison |

Flash `hardware/esp32_node/esp32_node.ino` to each board with `NODE_ROLE` set
(base / relay / crawler), and `hardware/esp32_cam/esp32_cam.ino` to the camera.
**[`docs/HARDWARE_INTEGRATION.md`](docs/HARDWARE_INTEGRATION.md) has the full
signal table, the safety chain, the bring-up order, and how to bolt this onto
an inspection crawler you already own.**

Start with `--emulate`: the simulator emits the real wire protocol, so the
whole ground station — parsing, dashboard, commands, emergency stop — can be
tested and rehearsed before anyone drives to a site.

## Honesty about limits

- The ray model is validated for bores at least **3 wavelengths** across
  (~15 in at 2.4 GHz). Smaller pipes are flagged `model_valid: false` in the
  telemetry, called out in the dashboard, and marked "indicative only" in the
  report. The direction of those answers is sound; the exact dB/km is not.
- Material constants are handbook estimates, deliberately isolated in
  `pipecomm/materials.py` so one measurement can replace one number.
- Bend and joint losses are first-order models calibrated to the right order
  of magnitude, with no independent validation.
- **No hardware has been built and nothing has been measured.** The firmware,
  the live mode and the BOM are the plan for closing that gap, not a report of
  results. Live mode has been verified end to end against the emulated
  transport, not against a robot.

`docs/PHYSICS.md` has the full list and what each one would take to fix.

## Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The design, and why the obvious approaches fail |
| [`docs/PHYSICS.md`](docs/PHYSICS.md) | The model, its validation, and its limits |
| [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) | Five phases, each with a stated failure condition |
| [`docs/BOM.md`](docs/BOM.md) | $493 of parts and what they prove |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Prototype to two miles: 48 weeks, $118k, with a $500 go/no-go gate at week 4 |
| [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) | Six-minute presentation script |
| [`docs/HARDWARE_INTEGRATION.md`](docs/HARDWARE_INTEGRATION.md) | **Running on a real robot**: signals, safety chain, bring-up, integrating an existing crawler |
| [`hardware/README.md`](hardware/README.md) | Firmware, wiring, and the measurement workflow |

## Layout

```
pipecomm/     simulation core - physics, radio, robot, mission (no I/O)
              live.py = the same contract, fed by real hardware
server/       stdlib HTTP + server-sent events
web/          operator dashboard (no framework, no build step)
cli.py        headless runs, planning, report generation
tests/        120 unit tests
tools/        dashboard render check
hardware/     ESP32 node + camera firmware, transports, measurement bridge
docs/         architecture, physics, test plan, BOM, roadmap, demo script
out/          generated reports and telemetry
```

The simulation core has no dependency on the server, and the server has none on
the dashboard. `cli.py` and `run.py` are two front ends onto the same engine.
