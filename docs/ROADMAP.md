# R&D roadmap: from this prototype to a two-mile field system

Garney's own framing is the right one: *"Success is not only reaching 2 miles.
Success is producing a tested architecture and credible evidence for the next
development stage."* This is that next-stage plan.

## Where the project stands now

| | Status |
|---|---|
| Physics model | Built, and validated against the exact modal result for metal guides |
| Architecture | Defined, with the failure boundaries identified |
| Simulation | Complete: live operator demo plus headless evidence generation |
| Hardware | Firmware written, BOM costed at $493, **not yet built or measured** |
| Field data | **None.** This is the gap that matters. |

The honest statement is: we have a defensible model and no measurements. The
roadmap is therefore organised around closing that gap as early and as cheaply
as possible, because every later decision depends on it.

---

## Stage 1 — Anchor the model (4 weeks, $500)

**Goal:** replace the single biggest guess — in-pipe attenuation — with a
measurement.

- Build four relay pucks and the camera platform (`BOM.md`).
- Run Phase 1 and 2 of `TEST_PLAN.md` on the bench rig.
- Feed measurements back through `serial_bridge.py calibrate` and re-run
  `cli.py run --all`.

**Exit criterion:** `out/RESULTS.md` regenerated with measured attenuation and
measured fade margin for at least two pipe diameters.

**Decision gate:** if measured attenuation is within ~3x of model, continue. If
it is 10x worse, stop and re-scope to large-diameter metal pipe only.

---

## Stage 2 — Real pipe, real conditions (6 weeks, ~$2k)

**Goal:** find out what a buried main does that a duct in a workshop does not.

- Phase 4 field test on a Garney site: 100 m minimum in each of metal and
  concrete pipe, dry and with standing water.
- Quantify the discrete features the model guesses at: joints, bends,
  manholes, laterals.
- Add a 5.8 GHz node variant and run the carrier comparison in real pipe. The
  model says dielectric pipes roughly double their hop length at 5.8 GHz; that
  claim is worth $1,500 to settle.

**Exit criterion:** a measured attenuation table by pipe material, diameter and
water condition. This table, not the simulator, is the thing Garney keeps.

---

## Stage 3 — The mechanism (10 weeks, ~$15k)

**Goal:** close the largest unfunded risk, which is mechanical, not electrical.

- Design and build a magazine that drops a relay puck and picks it back up,
  in a wet pipe, under the crawler's own power.
- Target: >90% recovery over 50 cycles (Phase 5).
- Fallback if recovery proves unreliable: expendable nodes at roughly $25 each
  — 20 nodes per two-mile run is $500 of consumables per inspection, which is
  a cost question rather than a feasibility question.

**Exit criterion:** 50-cycle drop-and-recover test passed, wet and dry.

---

## Stage 4 — Integration onto a real crawler (12 weeks, ~$40k)

**Goal:** stop being a science project.

- Package the radio, control and watchdog as a **module** that bolts onto the
  inspection crawler Garney already operates, rather than a bespoke robot.
  The communication package is the reusable deliverable; the crawler is not.
- Integrate the 620 Wh pack and validate the six-hour endurance figure under
  load. Energy, not radio, is what the untethered requirement really costs.
- Harden the safety chain: watchdog, battery-reserve-to-home, and a physical
  retrieval path for a stranded crawler.

**Exit criterion:** a 1,000 ft untethered inspection, start to finish, with
video and recovered nodes — i.e. matching today's tethered limit without a
tether.

---

## Stage 5 — Two miles (16 weeks, ~$60k)

**Goal:** the headline.

- Scale the magazine to 20+ nodes and validate the control-latency ceiling in
  the field (model says ~35 hops).
- Dual-radio relays on alternating channels if video quality at depth proves
  insufficient.
- Full two-mile run on a large-diameter metal main — which the model says is
  where this is achievable, and the decision table says which pipe to pick.

**Exit criterion:** 2 miles, continuous video, every node recovered.

---

## What the model says about where to point this

The decision table (`out/RESULTS.md`) is blunt about it:

| Pipe | Two miles feasible? |
|---|---|
| 24 in+ metal main, dewatered | **Yes** — about 5 relays, full HD video |
| 72 in dry tunnel | **Yes** — about 3 relays |
| 36 in concrete interceptor, live flow | Workable at 5.8 GHz (~15 relays), impractical at 2.4 GHz |
| 12 in force main with scale | Impractical: ~28 relays at best |
| 8 in clay sewer | **No.** Below the model's validated range, and directionally hopeless |

**So the two-mile target should be pursued on large-diameter metal
transmission mains first.** That is not a limitation to apologise for; it is
exactly the asset class where multiple access points are hardest to arrange
and where the 1,000 ft tether limit hurts most.

## Total to a two-mile demonstration

Roughly **48 weeks and $118k** from here, with a hard go/no-go at the end of
Stage 1 that costs $500 and four weeks to reach. That early gate is the most
valuable thing in this plan.
