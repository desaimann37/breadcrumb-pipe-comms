# Bill of materials and funding request

Challenge X offers up to **$500** per team. This is what it buys and what it
proves.

## Guiding decision

The $500 does not buy a two-mile system. It buys the **one measurement the
whole design rests on**: how fast does 2.4 GHz actually attenuate inside a real
pipe, and how deep are the fades? Every number in the simulator downstream of
that is either validated or corrected by it. So the budget goes to a
four-node relay chain with a camera and a repeatable way to measure distance,
not to a polished robot.

## Phase 1 build — $493

| # | Item | Qty | Unit | Total | Why this part |
|---|---|---|---|---|---|
| 1 | ESP32-WROOM-32U dev board (external antenna) | 5 | $9 | $45 | The **-U** variant matters: a PCB antenna against a wet pipe wall detunes badly. 5th board is a spare. |
| 2 | 2.4 GHz dipole, RP-SMA + IPEX pigtail | 5 | $6 | $30 | Known gain, repeatable orientation, can be centred in the bore. |
| 3 | XIAO ESP32-S3 Sense (camera + mic) | 1 | $16 | $16 | Video source. Does H.264/MJPEG at 720p onboard. |
| 4 | 18650 cells (protected) | 8 | $5 | $40 | 2 per relay node. |
| 5 | 18650 holders + BMS | 4 | $4 | $16 | |
| 6 | Buck converters 5 V 3 A | 5 | $3 | $15 | |
| 7 | IP67 enclosure, 100 x 68 x 50 mm | 4 | $9 | $36 | Relay pucks. Sewers are wet even when "dewatered". |
| 8 | Tracked robot chassis + motor driver | 1 | $75 | $75 | Carries the camera node. Not a production crawler; a platform that moves at a known speed. |
| 9 | 8 in galvanised spiral duct, 5 ft sections | 4 | $17 | $68 | **The bench rig.** 20 ft of known-geometry metal pipe to validate the model against the exact TE11 formula before going near a real site. |
| 10 | 12 in spiral duct, 5 ft sections | 2 | $26 | $52 | Second diameter, so the `1/a^3` scaling can actually be tested rather than assumed. |
| 11 | Duct couplers, foil tape, end caps | 1 | $22 | $22 | |
| 12 | 100 ft fibreglass tape measure | 1 | $14 | $14 | Distance truth. Unglamorous and indispensable. |
| 13 | Centring spiders (3D printed) + hardware | 1 | $18 | $18 | Holds antennas on the pipe axis so results are repeatable. |
| 14 | Micro servo (relay release actuator) | 1 | $5 | $5 | Fires `PIN_MAGAZINE`; 250 ms stroke. |
| 15 | MAX6746 watchdog supervisor + passives | 2 | $3 | $6 | Cuts `MOTOR_ENABLE` if the firmware hangs. A software watchdog cannot stop a robot whose software has stopped. |
| 16 | Connectors, silicone wire, heatshrink, SD cards | 1 | $35 | $35 | |
| | **Total** | | | **$493** | |

Everything here is a commodity part with a receipt, which is what the funding
rules ask for.

## What each line item is for

- **Items 1–2, 6–7, 13** make four identical relay pucks. Flash
  `hardware/esp32_node/esp32_node.ino` with `NODE_ROLE` set per device.
- **Items 9–11** are the bench rig. 20 ft of 8 in duct plus 10 ft of 12 in duct
  lets us measure attenuation at two diameters in a controlled setting, which
  is the only way to check the `1/a^3` claim without site access.
- **Items 8, 3** make a moving camera platform, so the demo is a robot driving
  and streaming, not two boxes on a bench.
- **Item 12** is the independent variable. Without accurate distance tags the
  RSSI log is uninterpretable.

## Deliberately not bought

| Not buying | Why |
|---|---|
| A real inspection crawler ($15k+) | Out of scope. The communication module is the deliverable; it mounts on whatever platform Garney already runs. |
| Pipe larger than 12 in | Too expensive per foot. Large-diameter data comes from site access to real pipe, which costs nothing but scheduling. |
| Commercial radios / SDRs | ESP32 gives calibrated RSSI, ESP-NOW and a camera for under $25 a node. An SDR would give better data and blow the budget on one device. |
| Custom PCBs | Dev boards in enclosures are faster and reworkable, and nothing here is production. |

## Energy budget — the result that surprised us

Cutting the tether cuts shore power, and that turns out to drive the mechanical
design more than the radio does:

| Load | Power |
|---|---|
| Drive motors (0.3 m/s) | 38 W |
| Lights | 9 W |
| Radio (crawler node) | 5.5 W |
| Electronics idle | 7 W |
| **Total while driving** | **~60 W** |

A two-mile round trip at 0.3 m/s is 6,437 m of driving, about **6 hours**, so
roughly **360 Wh** plus reserve. The simulator carries a **620 Wh** pack
(48 V, 13 Ah) and finishes the round trip with 48% remaining — but that pack is
around 3.5 kg of cells, and it is the single heaviest item on the machine.

Relay pucks are easy by comparison: 1.4 W idle, so two 18650s (26 Wh) run a
node for about 18 hours, comfortably longer than any single shift.

## If more funding became available

In priority order, because this is also the answer to "what would you do with
a real budget":

1. **$1,500 — a 5.8 GHz option.** The decision table says dielectric pipes
   (concrete, clay) roughly double their hop length at 5.8 GHz versus 2.4 GHz.
   That is the single highest-leverage change available and it needs hardware
   to confirm.
2. **$3,000 — dual-radio relay nodes.** Recovers the factor-of-three
   half-duplex penalty. Doubles node cost, triples video throughput.
3. **$2,000 — a vector network analyser rental for one week.** Turns every
   handbook material constant in `materials.py` into a measured one.
4. **$8,000 — a deployment and recovery mechanism** that works in a wet pipe
   under its own power. This is a mechanical problem, not a radio problem, and
   it is the largest unfunded risk in the architecture.
