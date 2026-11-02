# Demo script — 6 minutes

Run `python run.py` before you start talking. Browser opens on the dashboard.

Keyboard: `a` auto, `s` stop, `d` drop relay, `r` return, **space = E-stop**.

---

## 0:00 — The hook (30 s)

> "Pipe inspection stops at a thousand feet, because that is how much tether
> you can drag. Garney wants two miles. The instinct is to find a radio that
> punches through dirt. That instinct is wrong, and I can show you why in
> about ten seconds."

Scenario selector → **12 in force main**. Carrier dropdown → **433 MHz**.

Point at the red card in *Why this pipe behaves the way it does*:

> "433 megahertz in a twelve-inch pipe is below the waveguide cutoff. Not
> weak — impossible. The field is evanescent. A kilowatt would not help.
> That one number kills most of the obvious answers, including every
> long-range sub-gigahertz radio anyone will suggest to you."

Switch carrier back to **2.437 GHz**. Link comes alive.

> "Above cutoff, the same pipe is a waveguide. It wants to carry the signal.
> The job is not to beat the pipe, it is to use it."

---

## 1:00 — The architecture, moving (90 s)

Scenario → **24 in water transmission main**. Press **`a`** (auto). Sim speed
slider → **40x**.

> "This is a two-mile transmission main. The crawler drives in. Watch the
> link arc above the pipe — green squares are video coming back, amber are
> commands going out. Those are real packets on real hops; when a hop gets
> bad you will see them die mid-flight."

Wait for the first relay drop (~930 m).

> "There. The link margin ran out, so the crawler dropped a relay node — a
> battery-powered puck — and kept going. It carries a magazine of them. On the
> way back out it picks every one of them up. Nothing is left in the pipe."

Point at the hop table as it grows.

> "Every hop here is a real link budget: path loss through the actual pipe
> geometry, modulation chosen from SNR, packet error rate, latency. Not an
> animation."

---

## 2:30 — The safety argument (90 s)

Relay policy → **Stretch**. Relay magazine slider → **2**.

> "Now I am going to starve it: two relays only, and a policy that runs every
> hop to the edge. Watch the video."

The ladder walks down — 1080p → 720p → 480p — then the **VIDEO LINK DOWN**
overlay appears.

> "Video is gone. Now look at the control tile."

Point at **Control link: UP**.

> "Still up. That is deliberate. Video is OFDM and dies at about 2 dB SNR.
> Control is spread spectrum with twenty dB of processing gain and keeps
> working about thirty dB further down. Losing the picture must never mean
> losing the robot."

Press **space**.

> "Emergency stop. Acknowledged across the chain in..." *(read the number off
> the panel)* "...milliseconds. And if the chain ever does go quiet, the
> crawler does not wait to be told — it stops itself after 600 milliseconds on
> its own watchdog."

---

## 4:00 — The evidence (90 s)

> "A demo that only ever works is not evidence. So the same model runs
> headless and tells you where this fails."

Switch scenario → **8 in clay sewer**. Let it run a few seconds.

> "Eight-inch clay sewer, wet. Hops collapse to metres. And notice the model
> says so about itself —" *(point at the Model validity card)* "— the bore is
> 1.6 wavelengths across, below where this approximation is valid. The honest
> answer is 'this needs measurement', and the tool says that rather than
> inventing a number."

Switch to a terminal:

```bash
python cli.py compare
```

> "Every pipe, every carrier. Which ones are practical, which are impossible,
> and why. That is the deliverable Garney actually asked for — decision data.
> It also found something we did not expect: the best carrier depends on the
> pipe material. Metal prefers a lower frequency, concrete prefers a higher
> one. There is no single right answer."

---

## 5:30 — Close (30 s)

> "The model is validated against the closed-form waveguide result for metal
> pipe — it agrees within about fifteen percent. The tests are in the repo and
> one of them caught a real modelling error that was making biofilm look
> beneficial.
>
> What we do not have is measurements. So the $500 does not buy a robot — it
> buys four ESP32 nodes, twenty feet of duct and a tape measure, to answer one
> question: does a real pipe attenuate the way this says it does? Four weeks,
> five hundred dollars, and the answer decides everything after it."

---

## If asked...

**"Is the physics real or did you make it up?"**
> Grazing-ray model for an oversized waveguide, with both polarisations
> averaged, which is the standard hollow-dielectric-guide treatment. For metal
> pipe there is an exact answer and we match it within 14–17% across a 4x
> diameter and 6x frequency range. `tests/test_physics.py`.

**"Why not just use more power?"**
> Below cutoff, power does nothing at all. Above cutoff you are fighting
> 40–500 dB per kilometre; twenty more dB buys a couple of hundred metres and
> regulatory limits cap it anyway. Relays beat power by orders of magnitude.

**"What is the riskiest part?"**
> Not the radio. The mechanism that drops and recovers a node in a wet pipe.
> It is Stage 3 on the roadmap and it is the one we would most want funded.

**"What about battery?"**
> That was the surprise. Cutting the tether cuts shore power. Six hours of
> driving at 60 watts is about 360 Wh plus reserve, so a 620 Wh pack — the
> heaviest single item on the crawler. The untethered requirement is as much
> an energy problem as a radio problem.

**"Could you do it with one radio instead of relays?"**
> No. Show them `python cli.py plan --scenario interceptor_36`.
