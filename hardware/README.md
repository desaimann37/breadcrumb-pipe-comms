# Hardware

Two pieces: firmware for the ESP32 nodes, and a bridge that turns what they
measure into corrections to the model.

> **Status: written, not yet built.** Nothing in this directory has been run on
> real hardware. It is the Stage 1 plan from `../docs/ROADMAP.md`, not a
> report of results. The laptop half has been verified end to end against the
> emulated transport.

**For wiring, signals, the safety chain and bring-up order, read
[`../docs/HARDWARE_INTEGRATION.md`](../docs/HARDWARE_INTEGRATION.md).** This
file covers the firmware and the measurement workflow only.

## Two different tools

| Tool | For |
|---|---|
| `python run.py --live ...` | Driving the robot from the operator console, live |
| `bridge/live_bridge.py` | The transports behind `--live`, plus a bring-up monitor |
| `bridge/serial_bridge.py` | The standalone RSSI-vs-distance survey (Phase 1) |

Rehearse everything without hardware first:

```bash
python run.py --live --emulate --emulate-speed 60
```

## Why ESP32 and ESP-NOW

- **Calibrated RSSI** per received frame, which is the measurement the whole
  project needs.
- **ESP-NOW is connectionless.** A node driving past another starts exchanging
  frames immediately instead of spending seconds on association and DHCP. For
  a crawler moving through a chain of relays that matters more than throughput.
- **Under $25 a node** with a camera, so a four-node chain fits the budget.
- The **-U** board variant has an external antenna connector. Use it. A PCB
  antenna pressed against a wet pipe wall detunes badly, and that would corrupt
  exactly the measurement being taken.

## Flashing

`esp32_node/esp32_node.ino`, one sketch, three roles. `esp32_cam/esp32_cam.ino`
goes on the camera board. Edit two lines per node device:

```c
#define NODE_ROLE     ROLE_CRAWLER   // ROLE_BASE | ROLE_RELAY | ROLE_CRAWLER
#define NODE_ID       99             // 0 = base, 1..N = relays, 99 = crawler
```

Arduino IDE with the ESP32 core, board "ESP32 Dev Module". Or:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 esp32_node
arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 esp32_node
```

Every node must share `WIFI_CHANNEL`.

## The one non-obvious bit

ESP-NOW does not hand RSSI to the receive callback. The sketch grabs it from
the 802.11 header through the promiscuous-mode hook:

```c
esp_wifi_set_promiscuous(true);
esp_wifi_set_promiscuous_rx_cb(&promiscuous_rx);
```

Without that there is no measurement, only a link that either works or does not.

## Measuring a pipe

```bash
# log RSSI against distance; type the tape-measure reading as you go
python bridge/serial_bridge.py capture --port COM5 --out ../out/bench_12in.csv

# fit attenuation and fade statistics
python bridge/serial_bridge.py calibrate --csv ../out/bench_12in.csv
```

`calibrate` prints the two numbers to paste into `pipecomm/scenarios.py`:

```
    excess_loss_db_per_m = 0.0496   # measured, 2026-11-01
And set RadioConfig(fade_margin_db=3) to match the fading you actually saw.
```

Re-run `python cli.py run --all` afterwards and every prediction in
`out/RESULTS.md` is anchored to measurement instead of handbook constants.

### No hardware yet?

The whole capture-and-fit path runs dry, which is how it was tested:

```bash
python bridge/serial_bridge.py capture --simulate --samples 400 --out ../out/field_sim.csv
python bridge/serial_bridge.py calibrate --csv ../out/field_sim.csv
```

It should recover approximately the 0.05 dB/m the synthetic log was built with.

## Serial protocol

Base node to laptop, one line per received frame:

```
RX,<t_ms>,<frame_type>,<src_id>,<hop_count>,<rssi_dbm>,<seq>
```

Laptop to base node:

```
C,<speed>     drive command, speed in mm/s
E             emergency stop
```

Frame types: `1` beacon, `2` telemetry, `3` command, `4` emergency stop.

## Safety note

The crawler role implements the 600 ms link-loss watchdog that the simulator
models. Wire `WATCHDOG,stop` to the motor controller enable line, not just to
a serial print. On a real machine that line is the difference between a
crawler that stops in a pipe and a crawler that has to be dug up.
