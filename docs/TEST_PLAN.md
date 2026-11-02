# Test plan

The deliverable Garney asked for is "evidence showing which communication
methods work, where they fail, and why". That means the tests are designed to
**find the failure boundary**, not to produce a successful demo.

Every phase below states what would make it fail, because a test that cannot
fail is not a test.

---

## Phase 0 — Model verification (done, in this repo)

| Check | Where | Result |
|---|---|---|
| Ray model vs closed-form TE11 for metal guides | `tests/test_physics.py::test_matches_exact_te11_formula_for_metal` | agrees within 14–17% over 4x diameter and 6x frequency |
| `1/a^3` diameter scaling | `test_inverse_cube_diameter_scaling` | 7.3x for a 2x diameter change (theory: 8x) |
| Fouling always increases loss | `test_fouling_always_costs` | passes (this test caught a real modelling error — see `PHYSICS.md` §3) |
| Delay spread fits the 802.11 guard interval | `test_delay_spread_fits_ofdm_guard` | 1 km in a 24 in pipe = ~100 ns vs 800 ns guard |
| Control link outlives video | `test_control_outlives_video_on_a_long_hop` | >20 dB of extra margin |
| Full two-mile mission completes and recovers every node | `test_mission.py::test_full_run_completes_and_recovers_its_relays` | passes |

Run: `python -m unittest discover -s tests -v`

**Would fail if:** the attenuation model disagreed with the exact modal result,
or if any scenario could reach the end of a pipe without the physics allowing it.

---

## Phase 1 — Bench rig: does a real pipe attenuate the way the model says?

This is the phase that matters. Everything downstream is built on its answer.

**Setup.** 20 ft of 8 in galvanised spiral duct and 10 ft of 12 in, coupled and
taped, antennas centred on the axis with printed spiders. One node fixed at
one end, one node moved along the bore in 0.5 m steps.

**Procedure.**
```bash
# at the base node
python hardware/bridge/serial_bridge.py capture --port COM5 --out out/bench_8in.csv
# type the tape-measure reading at every mark; Ctrl+C when done
python hardware/bridge/serial_bridge.py calibrate --csv out/bench_8in.csv
```

**Measure:**
1. RSSI vs distance, 0.5 m resolution, both diameters.
2. The same with 25 mm of water in the invert (the single worst field condition).
3. The same at 915 MHz, to confirm the cutoff prediction directly.

**Pass criteria.**
- Fitted dB/m within **3x** of the model for the 12 in duct. (3x, not 10%: the
  model is an approximation and the absolute value is what we are calibrating.)
- `r^2 > 0.7` on the fit, so the measurement itself is trustworthy.
- Adding water measurably increases attenuation.

**The decisive one:** at 915 MHz in the 8 in duct (cutoff 865 MHz, so barely
above it) the link should be dramatically worse than at 2.4 GHz despite the
lower frequency. If 915 MHz works *better*, the cutoff model is wrong and the
whole carrier-selection argument collapses. This is the cheapest possible test
of the project's central claim, and it should be run first.

**Would fail if:** attenuation is flat with distance (antennas are radiating
outside the duct, not guiding), or if 915 MHz outperforms 2.4 GHz in the 8 in
pipe.

---

## Phase 2 — Multi-hop relay chain

**Setup.** Four nodes in the bench rig, chained, with the far node driven by
the robot chassis.

**Measure.**
1. End-to-end latency per hop (the firmware timestamps every frame).
2. Goodput vs hop count, single-radio single-channel.
3. Packet delivery ratio with one hop deliberately attenuated (slide a metal
   plate partly across the bore).
4. **Emergency stop latency**, hop count 1 through 4, 100 trials each.

**Pass criteria.**
- Per-hop forwarding delay under 25 ms.
- E-stop one-way latency under 600 ms at 4 hops (the watchdog budget).
- Goodput degrades roughly as `1/3` of the weakest hop, confirming the
  half-duplex penalty the architecture assumes.

**Would fail if:** per-hop latency is much worse than modelled, which would
lower the 35-hop ceiling and change the whole relay-count analysis.

---

## Phase 3 — Live video over the chain

**Measure.** Glass-to-glass latency (point the camera at a running millisecond
timer on the operator screen and photograph both), achieved bitrate, and the
quality rung the adaptive ladder settles on at each hop count.

**Pass criteria.**
- 480p or better at 4 hops in the 12 in duct.
- Glass-to-glass under 500 ms, which is the usability threshold for driving.
- Quality degrades gracefully: no hard cut from HD to nothing.

---

## Phase 4 — Field test in real pipe

Needs Garney site access; no purchase required.

**Measure.** The Phase 1 drag test in a real buried main, 100 m minimum, plus:
joint loss (RSSI step across a joint), bend loss (before/after a sweep), and
loss at a manhole.

**Pass criteria.** Fitted dB/m within 3x of the model for the matching pipe
type. Discrete features visible as steps in the RSSI trace.

**This is also where the model gets corrected.** Feed the measured `dB/m` and
fade distribution back into `pipecomm/scenarios.py` and re-run `cli.py run
--all`. Every prediction in `out/RESULTS.md` is then anchored to measurement
rather than to handbook constants.

**Would fail if:** real-pipe attenuation is more than ~5x the model. That would
not kill the architecture, but it would multiply the relay count and push the
two-mile goal out of reach for anything but large-diameter metal pipe — which
is itself a finding worth having.

---

## Phase 5 — Deployment and recovery mechanics

The largest unfunded risk, and deliberately last because it is a mechanical
problem, not a radio one.

**Measure.** Drop and recovery success rate over 50 cycles, in a dry pipe and
with 25 mm of water. Does a dropped puck stay upright? Does it stay on the axis?
Does it survive being driven over?

**Pass criteria.** >90% recovery. Below that, the architecture needs rethinking
towards expendable nodes, which changes the cost model entirely.

---

## Honest summary of risk

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Real attenuation far exceeds model | Medium | High | Phase 1 and 4 measure it early and cheaply |
| Deployment/recovery mechanism fails | Medium | High | Phase 5; fallback is expendable nodes |
| Water conditions worse than assumed | High | Medium | Measured in Phase 1 step 2; dewatering is already normal practice for inspection |
| Control latency caps the chain shorter than 35 hops | Low | Medium | Measured in Phase 2; mitigated by cut-through forwarding |
| Regulatory limits on Tx power in-pipe | Low | Low | A buried pipe is an excellent shield; emissions at the surface are negligible |
