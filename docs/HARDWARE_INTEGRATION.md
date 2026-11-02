# Running this on a real robot

What to flash, what to wire, what to run, and in what order.

The console does not change. `python run.py --live ...` serves the same
dashboard, but position comes from wheel odometry, every hop's SNR comes from
measured RSSI, and the link-up light is telemetry freshness rather than a
model. Everything downstream of SNR — modulation choice, packet error rate,
the video ladder, the control margin — runs through the same code as the
simulator, so a field test is a direct measured-against-predicted comparison.

---

## 1. Files, and where each one goes

| File | Goes where | What it is |
|---|---|---|
| `hardware/esp32_node/esp32_node.ino` | **Every** ESP32 node | One sketch, three roles. Set `NODE_ROLE` + `NODE_ID` per device. |
| `hardware/esp32_cam/esp32_cam.ino` | The camera board | MJPEG stream on `:81/stream`, plus a quality ladder over HTTP. |
| `hardware/bridge/live_bridge.py` | Laptop | Serial / UDP / replay / emulated transports, and the operator heartbeat. |
| `pipecomm/live.py` | Laptop | Turns the node chain's telemetry into the dashboard's data shapes. |
| `run.py --live` | Laptop | Launches the console against hardware instead of the simulator. |
| `hardware/bridge/serial_bridge.py` | Laptop | The separate RSSI-vs-distance survey tool for Phase 1. |

### Role assignment

Edit two lines at the top of `esp32_node.ino` before each flash:

```c
#define NODE_ROLE  ROLE_BASE      // ROLE_BASE | ROLE_RELAY | ROLE_CRAWLER
#define NODE_ID    0              // 0 = base, 1..31 = relays, 99 = crawler
```

| Board | ROLE | NODE_ID | Sits |
|---|---|---|---|
| 1 | `ROLE_BASE` | `0` | At the manhole, USB to the laptop |
| 2 | `ROLE_RELAY` | `1` | Relay puck |
| 3 | `ROLE_RELAY` | `2` | Relay puck |
| 4 | `ROLE_CRAWLER` | `99` | On the robot |
| 5 | `ESP32-CAM` | — | On the robot, `esp32_cam.ino` |

`WIFI_CHANNEL` must match on every node. Mismatched channels is the single
most common bring-up failure and it looks exactly like "no radio range".

---

## 2. Signals on the crawler node

This is the wiring that makes the robot move. Pin choices work around two
ESP32 constraints that bite people: **ADC2 cannot be read while Wi-Fi is on**
(so battery sense must be on ADC1, GPIO 32–39), and **GPIO 34–39 are
input-only with no internal pull-ups** (so the encoder inputs need external
resistors).

| Signal | GPIO | Dir | Connects to | Notes |
|---|---|---|---|---|
| `MOTOR_ENABLE` | 23 | out | Driver STBY / enable | **The stop line.** Low = motors dead. |
| `PIN_PWMA` | 25 | out (PWM) | Left motor PWM | 20 kHz, 10-bit |
| `PIN_AIN1` | 26 | out | Left direction A | |
| `PIN_AIN2` | 27 | out | Left direction B | |
| `PIN_PWMB` | 19 | out (PWM) | Right motor PWM | |
| `PIN_BIN1` | 18 | out | Right direction A | |
| `PIN_BIN2` | 17 | out | Right direction B | |
| `PIN_ENC_L` | 34 | in | Left encoder A | **External 10k pull-up to 3V3** |
| `PIN_ENC_R` | 35 | in | Right encoder A | **External 10k pull-up to 3V3** |
| `PIN_BATT_ADC` | 36 | in | Battery divider tap | ADC1 only. Set `BATT_DIVIDER`. |
| `PIN_LIGHTS` | 21 | out | LED ring MOSFET gate | |
| `PIN_MAGAZINE` | 16 | out | Relay release servo/solenoid | 250 ms stroke |
| `HW_WATCHDOG_PIN` | 22 | out | External monostable | See §3 |
| `PIN_LED` | 2 | out | Status LED | On-board |

Ground the ESP32, the motor driver and the battery sense divider to a
**single point**. A motor driver sharing a noisy ground with an ADC is the
other classic cause of "the battery reading jumps around".

### Battery sense divider

```
  Pack + ──[ R1 ]──┬──[ R2 ]── GND
                   │
                GPIO 36
```

`BATT_DIVIDER` in the sketch must equal `(R1+R2)/R2`. For a 48 V pack,
R1 = 200k, R2 = 10k gives 21.0 and keeps the tap under 3.3 V at full charge.
Set `BATT_LOW_MV` to your pack's cut-off; the firmware emergency-stops below it.

---

## 3. The safety chain — do not shortcut this

`MOTOR_ENABLE` goes low on **any** of:

1. An emergency stop frame from the console.
2. The link watchdog: no command frame for `WATCHDOG_MS` (600 ms).
3. Battery below `BATT_LOW_MV`.
4. The firmware hanging.

Case 4 is why `HW_WATCHDOG_PIN` exists. The firmware toggles it every loop;
feed it to a retriggerable monostable (a 555, or a MAX6746 supervisor) whose
output is ANDed into the driver's enable. If the sketch crashes, the toggling
stops, the monostable times out, and the motors are cut without any software
being involved. A software-only watchdog cannot stop a robot whose software
has stopped.

There is also a **fail-safe operator heartbeat**: the base node only transmits
keepalive frames while the laptop is sending `P` at 10 Hz. Unplug the USB
cable, close the console, or let the laptop sleep, and the crawler's watchdog
trips and stops it. That is deliberate — pulling the cable must stop the
robot, not abandon it driving.

Never tie STBY high "just to get it moving for a test". The entire safety
argument of this project lives on that pin.

---

## 4. If you already have an inspection crawler

Most likely case, and the better one: do **not** use our motor control. Use
the crawler node as a pure communications module and let the existing
controller keep driving.

Delete nothing; just change where the commands land. In `esp32_node.ino`,
replace the body of `motorsWrite()` and the `FRAME_CMD` case with a write to
your controller's link:

```c
case FRAME_CMD:
  commandedMms = f.a;
  lastCmdRxMs = millis();
  Serial2.printf("SPEED %d\n", commandedMms);      // or CAN, or Modbus
  break;

case FRAME_ESTOP:
  digitalWrite(MOTOR_ENABLE, LOW);                 // keep the hard line
  Serial2.println("ESTOP");                        // and tell the controller
  break;
```

and feed odometry back from the controller instead of the encoder ISRs. The
four signals you must still provide are:

| You must supply | Why |
|---|---|
| **Odometry**, in mm | Positions every hop. Without it the console cannot place relays or compute path loss against distance. |
| **A hardware stop input** on the controller | The watchdog needs a path to the motors that does not go through your control software. |
| **Battery voltage** | Return-energy margin; the untethered constraint is as much energy as radio. |
| **A relay release actuator** | One digital output and a 250 ms stroke. |

Everything else (video, RSSI, the chain, the dashboard) works untouched.

---

## 5. Bring-up order

Each step is a thing that can fail on its own. Do not skip ahead; a
mis-channelled relay looks identical to a dead radio.

**1. Console with no hardware at all.**
```bash
python run.py --live --emulate --emulate-speed 60
```
The simulator pretends to be a node chain and emits the real wire protocol.
You should see relays drop, hops appear, and the e-stop confirm. Rehearse the
demo here. If this does not work, nothing downstream will.

**2. One node on the bench.** Flash board 4 as `ROLE_CRAWLER`, nothing wired
to the motors yet.
```bash
python hardware/bridge/live_bridge.py monitor --serial COM5 --raw
```
Expect `BOOT,...` then `TLM,...` at 10 Hz. No `TLM` means the sketch is not
running; garbage means the baud rate is wrong (it is 921600).

**3. Base plus crawler, no relays.** Flash board 1 as `ROLE_BASE`.
```bash
python run.py --live --serial COM5
```
The console should show one hop and a live RSSI. Walk the crawler node away
and watch the margin fall. **This is the first real measurement.**

**4. Motors.** Wire the driver. Before connecting a load, confirm with a
meter that `MOTOR_ENABLE` is low at boot and goes high only after a drive
command. Then test the stop *first*, with the wheels off the ground.

**5. Relays.** Flash boards 2 and 3. Drop them with the console; confirm the
hop count grows and `DROP` lines carry sensible odometry.

**6. Camera.** Flash the camera board, note the IP it prints, then:
```bash
python run.py --live --serial COM5 --camera http://192.168.4.1:81/stream
```

**7. Measure the noise floor**, once, in the pipe with every transmitter off,
and pass it in. Until you do, the console uses the modelled thermal floor and
says so on the "Measured vs model" card:
```bash
python run.py --live --serial COM5 --noise-floor -97
```

---

## 6. Running it

```bash
# normal: base node on USB
python run.py --live --serial COM5

# with real video
python run.py --live --serial COM5 --camera http://192.168.4.1:81/stream

# base node on Wi-Fi at the manhole, operator in the truck
python run.py --live --udp --udp-peer 192.168.4.1

# log everything for later review
python run.py --live --serial COM5 --log out/field_2026-11-02.log

# review a past run at 4x
python run.py --live --replay out/field_2026-11-02.log --replay-speed 4

# no hardware: rehearse the whole thing
python run.py --live --emulate
```

Controls that are simulation-only (carrier, bandwidth, Tx power, relay
policy, sim speed, autonomous mode) are **disabled** in live mode rather than
quietly lying — the radio settings are compiled into the firmware, so
changing them on screen would mean reflashing the nodes.

---

## 7. What the console measures for you

| Panel | Where the number comes from |
|---|---|
| Hop table `rx_dbm` | RSSI from the 802.11 header, per node |
| Hop table `err` | Measured path loss minus the model's prediction for the same span |
| `Measured vs model` card | Least-squares fit of dB/m across the run, against the modelled value |
| `Live link health` card | Telemetry age, frame delivery ratio, last confirmed command round trip |
| Control RTT | **Measured**: time from sending a command to the crawler's own telemetry reporting the flag set. No clock sync needed. |
| Relay positions | The crawler's odometry at the instant its actuator fired |

Toggling the **lights** is the cheapest way to measure a real chain round trip
without stopping the robot — the console times the flag echo.

When the fit has settled, put the measured numbers back into
`pipecomm/scenarios.py` and re-run `python cli.py run --all`. Every prediction
in `out/RESULTS.md` is then anchored to measurement instead of handbook
constants, which is the whole point of going to site.

---

## 8. Wire protocol

Base node → laptop:

```
BOOT,<role>,<id>,<ch>,<txpwr>
TLM,<t_ms>,<src>,<hop>,<rssi>,<seq>,<odo_mm>,<batt_mv>,<speed_mms>,<flags>
HOP,<t_ms>,<relay_id>,<rssi_up>,<hop_index>
DROP,<t_ms>,<node_id>,<odo_mm>
RX,<t_ms>,<type>,<src>,<hop>,<rssi>,<seq>
EVT,<t_ms>,<text>
```

Laptop → base node:

```
C,<speed_mm_s>    drive          E    emergency stop
L,<0|1>           lights         R    re-arm after a stop
D                 release relay  P    operator heartbeat, 10 Hz
```

`flags` bits: `1` e-stop latched, `2` watchdog tripped, `4` lights on,
`8` battery low.

Frame types on the air: `1` beacon, `2` telemetry, `3` command, `4` e-stop,
`5` re-arm, `6` lights, `7` hello, `8` deploy, `9` drop.

Relays flood frames with duplicate suppression on `(src, seq)` and a 31-hop
limit. That is simple and robust for a chain of four; a production system
would use explicit routing, and the hop count is where that would start.

---

## 9. Known gaps

Stated plainly, because these will bite during a field test:

- **Speed control is open loop.** Duty cycle is proportional to the commanded
  speed; the encoders only report what actually happened. A PID loop is a
  TODO and is wanted before anyone trusts the odometry on a wet grade.
- **MJPEG, not H.264.** Roughly 3x the bitrate for the same quality, chosen
  because a lost packet costs one frame instead of everything up to the next
  keyframe. The simulator's ladder assumes H.264, so expect live video to sit
  one or two rungs lower than the simulation predicts at the same bitrate.
- **No encryption or authentication on the air.** Fine in a buried pipe,
  not fine for anything that surfaces. ESP-NOW supports PMK/LMK; turn it on
  before this goes anywhere public.
- **Relay recovery is not implemented in firmware.** The magazine releases;
  picking pucks back up is Stage 3 of the roadmap and is mechanical.
- **Odometry drifts** on wheel slip, which is common in a wet invert. Every
  distance on the console inherits that error. Tape-measure checks at
  manholes are the cheap correction.
