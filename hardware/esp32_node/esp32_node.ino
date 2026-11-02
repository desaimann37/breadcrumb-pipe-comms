/*
 * BREADCRUMB node firmware - ESP32
 * ================================
 *
 * One sketch, three roles. Set NODE_ROLE and NODE_ID below, flash, done.
 *
 *   ROLE_BASE     at the access shaft. Talks to the laptop over USB serial.
 *   ROLE_RELAY    a dropped puck. Forwards frames both ways, reports its RSSI.
 *   ROLE_CRAWLER  on the robot. Drives the motors, reads encoders, and stops
 *                 itself if the chain goes quiet.
 *
 * ESP-NOW rather than normal Wi-Fi association, because ESP-NOW is
 * connectionless: a crawler driving past a relay starts exchanging frames
 * immediately instead of spending seconds on association and DHCP.
 *
 * SAFETY - read this before connecting a motor
 * --------------------------------------------
 * MOTOR_ENABLE (the driver's STBY/enable pin) is the stop line. It is driven
 * LOW by: an emergency stop frame, the link watchdog, a low battery, or the
 * firmware crashing (see HW_WATCHDOG_PIN below). Never bypass it by tying
 * STBY high - the whole safety argument of this project lives on that pin.
 *
 * Build: Arduino IDE with the ESP32 core (2.x or 3.x both work), board
 *        "ESP32 Dev Module". Or:
 *          arduino-cli compile --fqbn esp32:esp32:esp32 esp32_node
 *          arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 esp32_node
 */

#include <esp_now.h>
#include <esp_wifi.h>
#include <WiFi.h>

// ======================================================= configuration ===

#define ROLE_BASE     0
#define ROLE_RELAY    1
#define ROLE_CRAWLER  2

#define NODE_ROLE     ROLE_CRAWLER   // <-- set per device before flashing
#define NODE_ID       99             // 0 = base, 1..31 = relays, 99 = crawler

#define WIFI_CHANNEL  6              // must match on every node
#define TX_POWER      78             // 0.25 dBm steps; 78 = 19.5 dBm (max)

#define WATCHDOG_MS   600            // stop if no command frame for this long
#define TELEM_HZ      10             // crawler telemetry rate
#define HEARTBEAT_HZ  10             // base -> crawler keepalive rate
#define OPERATOR_TIMEOUT_MS 500      // base stops beating if the laptop goes quiet

#define TICKS_PER_METRE   1200.0f    // encoder ticks per metre of travel
#define MAX_SPEED_MMS     350        // matches CrawlerConfig.max_speed_mps
#define BATT_LOW_MV       33000      // 33.0 V on a 48 V pack: stop and report
#define BATT_DIVIDER      21.0f      // (R1+R2)/R2 of the battery sense divider

// ------------------------------------------------------------- pin map ---
//
// Chosen around two ESP32 constraints that bite people:
//   * ADC2 cannot be read while Wi-Fi is on, so battery sense must be ADC1
//     (GPIO 32-39).
//   * GPIO 34-39 are input-only and have NO internal pull-ups. The encoder
//     inputs therefore need external 10k pull-ups to 3V3.
//
#define PIN_PWMA        25   // left motor PWM
#define PIN_AIN1        26   // left direction
#define PIN_AIN2        27
#define PIN_PWMB        19   // right motor PWM
#define PIN_BIN1        18   // right direction
#define PIN_BIN2        17
#define MOTOR_ENABLE    23   // TB6612 STBY / BTS7960 enable - THE STOP LINE
#define PIN_ENC_L       34   // input only, external 10k pull-up required
#define PIN_ENC_R       35   // input only, external 10k pull-up required
#define PIN_BATT_ADC    36   // ADC1_CH0 (VP), via BATT_DIVIDER
#define PIN_LIGHTS      21   // MOSFET gate for the LED ring
#define PIN_MAGAZINE    16   // servo / solenoid that releases one relay puck
#define HW_WATCHDOG_PIN 22   // toggled every loop; feeds an external monostable
#define PIN_LED          2   // on-board status LED

#define PWM_FREQ_HZ   20000  // above audible, below driver slew limits
#define PWM_BITS         10  // 0..1023

// ======================================================== core compat ====
// The ESP32 Arduino core changed both the LEDC API and the ESP-NOW receive
// callback signature at 3.0. Support both so this builds on whatever the
// team has installed.

#if ESP_ARDUINO_VERSION_MAJOR >= 3
  #define LEDC_ATTACH(pin)        ledcAttach(pin, PWM_FREQ_HZ, PWM_BITS)
  #define LEDC_WRITE(pin, duty)   ledcWrite(pin, duty)
  #define ESPNOW_RECV_SIG const esp_now_recv_info_t *info, const uint8_t *data, int len
  #define ESPNOW_PEER_MAC (info->src_addr)
#else
  static int _ledcNext = 0;
  #define LEDC_ATTACH(pin)        do { int ch = _ledcNext++;                 \
                                       ledcSetup(ch, PWM_FREQ_HZ, PWM_BITS); \
                                       ledcAttachPin(pin, ch); } while (0)
  #define LEDC_WRITE(pin, duty)   ledcWrite(_ledcChannelFor(pin), duty)
  #define ESPNOW_RECV_SIG const uint8_t *mac, const uint8_t *data, int len
  #define ESPNOW_PEER_MAC (mac)
  static int _ledcChannelFor(int pin) { return (pin == PIN_PWMA) ? 0 : 1; }
#endif

// ============================================================= protocol ==

enum FrameType : uint8_t {
  FRAME_BEACON = 1,   // ranging beacon, used for the attenuation survey
  FRAME_TELEM  = 2,   // crawler -> base
  FRAME_CMD    = 3,   // base -> crawler: drive speed in payload a
  FRAME_ESTOP  = 4,   // base -> crawler, latching
  FRAME_REARM  = 5,   // base -> crawler, clears a latched stop
  FRAME_LIGHTS = 6,   // base -> crawler, payload a = 0/1
  FRAME_HELLO  = 7,   // relay announcing itself
  FRAME_DEPLOY = 8,   // base -> crawler: release one relay puck
  FRAME_DROP   = 9,   // crawler -> base: puck released, odo_mm says where
};

enum Flags : uint8_t {
  FLAG_ESTOP    = 1 << 0,
  FLAG_WATCHDOG = 1 << 1,
  FLAG_LIGHTS   = 1 << 2,
  FLAG_LOWBATT  = 1 << 3,
};

struct __attribute__((packed)) Frame {
  uint8_t  type;
  uint8_t  src;        // originating node id
  uint8_t  hop;        // incremented by every relay that forwards it
  uint8_t  flags;
  uint32_t seq;        // unique per source, used for duplicate suppression
  uint32_t t_ms;       // originator timestamp, for one-way delay
  int16_t  a;          // command speed (mm/s), or measured speed in telemetry
  int16_t  b;          // battery, in units of 10 mV
  int32_t  odo_mm;     // crawler odometry
  int8_t   rssi_up;    // RSSI the forwarding node heard from upstream
  int8_t   pad[3];
};

// ============================================================== globals ==

static uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static volatile int   g_lastRssi = 0;        // filled by the promiscuous hook
static volatile long  g_ticksL = 0, g_ticksR = 0;

static uint32_t txSeq = 0;
static uint32_t lastTelem = 0, lastBeat = 0, lastBlink = 0;
static uint32_t lastCmdRxMs = 0;             // crawler: last command heard
static uint32_t lastOperatorMs = 0;          // base: last laptop heartbeat
static bool     estopLatched = false;
static bool     watchdogTripped = false;
static bool     lightsOn = true;
static int16_t  commandedMms = 0;
static int16_t  measuredMms = 0;
static uint16_t batteryMv = 0;
static uint8_t  pucksReleased = 0;

// Duplicate suppression. ESP-NOW broadcast flooding would otherwise loop
// frames around a chain forever. 64 entries covers a few seconds of traffic.
#define SEEN_SIZE 64
static uint32_t seenKey[SEEN_SIZE];
static uint8_t  seenIdx = 0;

static bool alreadySeen(uint8_t src, uint32_t seq) {
  uint32_t key = ((uint32_t)src << 24) ^ (seq & 0x00FFFFFF);
  for (uint8_t i = 0; i < SEEN_SIZE; i++) {
    if (seenKey[i] == key) return true;
  }
  seenKey[seenIdx] = key;
  seenIdx = (seenIdx + 1) % SEEN_SIZE;
  return false;
}

// ============================================================ encoders ===

void IRAM_ATTR isrLeft()  { g_ticksL++; }
void IRAM_ATTR isrRight() { g_ticksR++; }

static long totalTicks() { return g_ticksL + g_ticksR; }

// ESP-NOW does not hand RSSI to the receive callback, so take it from the
// 802.11 header via the promiscuous hook. Without this there is no
// measurement, only a link that either works or does not.
void promiscuous_rx(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;
  const wifi_promiscuous_pkt_t *p = (wifi_promiscuous_pkt_t *)buf;
  g_lastRssi = p->rx_ctrl.rssi;
}

// ============================================================== motors ===

static void motorsWrite(int16_t mms) {
  // Open loop: duty proportional to commanded speed. Closed-loop PID on the
  // encoders is a TODO; for the bench and first field tests the operator is
  // the loop, and telemetry reports the speed actually achieved.
  bool enabled = !estopLatched && !watchdogTripped && batteryMv > BATT_LOW_MV / 10;
  digitalWrite(MOTOR_ENABLE, enabled ? HIGH : LOW);
  if (!enabled) {
    LEDC_WRITE(PIN_PWMA, 0);
    LEDC_WRITE(PIN_PWMB, 0);
    return;
  }
  bool forward = mms >= 0;
  int mag = abs(mms);
  if (mag > MAX_SPEED_MMS) mag = MAX_SPEED_MMS;
  uint32_t duty = (uint32_t)((mag / (float)MAX_SPEED_MMS) * ((1 << PWM_BITS) - 1));

  digitalWrite(PIN_AIN1, forward ? HIGH : LOW);
  digitalWrite(PIN_AIN2, forward ? LOW : HIGH);
  digitalWrite(PIN_BIN1, forward ? HIGH : LOW);
  digitalWrite(PIN_BIN2, forward ? LOW : HIGH);
  LEDC_WRITE(PIN_PWMA, duty);
  LEDC_WRITE(PIN_PWMB, duty);
}

static void emergencyStop(const char *why) {
  estopLatched = true;
  commandedMms = 0;
  digitalWrite(MOTOR_ENABLE, LOW);
  LEDC_WRITE(PIN_PWMA, 0);
  LEDC_WRITE(PIN_PWMB, 0);
  Serial.printf("EVT,%lu,ESTOP %s\n", (unsigned long)millis(), why);
}

static uint16_t readBatteryMv() {
  // ADC1 only: ADC2 is unavailable while Wi-Fi is running.
  uint32_t raw = 0;
  for (int i = 0; i < 8; i++) raw += analogRead(PIN_BATT_ADC);
  float volts = (raw / 8.0f) * (3.3f / 4095.0f) * BATT_DIVIDER;
  return (uint16_t)(volts * 1000.0f);
}

// ============================================================== sending ==

static void sendFrame(uint8_t type, int16_t a = 0, int16_t b = 0,
                      int32_t odo = 0, uint8_t flags = 0) {
  Frame f = {};
  f.type = type;
  f.src = NODE_ID;
  f.hop = 0;
  f.flags = flags;
  f.seq = ++txSeq;
  f.t_ms = millis();
  f.a = a;
  f.b = b;
  f.odo_mm = odo;
  f.rssi_up = (int8_t)g_lastRssi;
  esp_now_send(BROADCAST, (uint8_t *)&f, sizeof(f));
}

// ============================================================ receiving ==

void onReceive(ESPNOW_RECV_SIG) {
  if (len < (int)sizeof(Frame)) return;
  Frame f;
  memcpy(&f, data, sizeof(f));
  int rssi = g_lastRssi;

  if (f.src == NODE_ID) return;              // our own flood coming back
  if (alreadySeen(f.src, f.seq)) return;     // already forwarded this one

  digitalWrite(PIN_LED, HIGH);

#if NODE_ROLE == ROLE_BASE
  // Everything that reaches the base goes to the laptop as one CSV line.
  // live_bridge.py parses exactly these.
  if (f.type == FRAME_DROP) {
    Serial.printf("DROP,%lu,%d,%ld
",
                  (unsigned long)millis(), f.a, (long)f.odo_mm);
  } else if (f.type == FRAME_TELEM) {
    Serial.printf("TLM,%lu,%u,%u,%d,%lu,%ld,%d,%d,%u\n",
                  (unsigned long)millis(), f.src, f.hop, rssi,
                  (unsigned long)f.seq, (long)f.odo_mm, f.b * 10, f.a, f.flags);
  } else {
    Serial.printf("RX,%lu,%u,%u,%u,%d,%lu\n",
                  (unsigned long)millis(), f.type, f.src, f.hop, rssi,
                  (unsigned long)f.seq);
  }
  // Per-hop RSSI: every relay stamps what it heard from upstream, so the base
  // can reconstruct the link budget of the whole chain, not just the last hop.
  if (f.hop > 0) {
    Serial.printf("HOP,%lu,%u,%d,%u\n",
                  (unsigned long)millis(), f.src, f.rssi_up, f.hop);
  }
#endif

#if NODE_ROLE == ROLE_RELAY
  if (f.hop < 31) {
    f.hop++;
    f.rssi_up = (int8_t)rssi;   // what this relay heard, for the hop report
    esp_now_send(BROADCAST, (uint8_t *)&f, sizeof(f));
  }
#endif

#if NODE_ROLE == ROLE_CRAWLER
  switch (f.type) {
    case FRAME_ESTOP:
      emergencyStop("commanded");
      lastCmdRxMs = millis();
      break;
    case FRAME_REARM:
      estopLatched = false;
      watchdogTripped = false;
      lastCmdRxMs = millis();
      Serial.printf("EVT,%lu,rearmed\n", (unsigned long)millis());
      break;
    case FRAME_CMD:
      commandedMms = f.a;
      lastCmdRxMs = millis();
      if (watchdogTripped) {
        watchdogTripped = false;
        Serial.printf("EVT,%lu,link restored\n", (unsigned long)millis());
      }
      break;
    case FRAME_LIGHTS:
      lightsOn = f.a != 0;
      digitalWrite(PIN_LIGHTS, lightsOn ? HIGH : LOW);
      lastCmdRxMs = millis();
      break;
    case FRAME_DEPLOY: {
      // Fire the actuator, then report where it happened. The odometry
      // reading is taken here, not on the console, so the relay position is
      // measured rather than assumed.
      lastCmdRxMs = millis();
      digitalWrite(PIN_MAGAZINE, HIGH);
      delay(250);                       // actuator stroke
      digitalWrite(PIN_MAGAZINE, LOW);
      pucksReleased++;
      long odoMm = (long)(totalTicks() / (2.0f * TICKS_PER_METRE) * 1000.0f);
      sendFrame(FRAME_DROP, pucksReleased, 0, odoMm);
      Serial.printf("DROP,%lu,%u,%ld
",
                    (unsigned long)millis(), pucksReleased, odoMm);
      break;
    }
    case FRAME_BEACON:
      lastCmdRxMs = millis();   // a beacon also proves the chain is alive
      break;
    default:
      break;
  }
#endif

  digitalWrite(PIN_LED, LOW);
}

// =============================================================== setup ===

void setup() {
  Serial.begin(921600);
  pinMode(PIN_LED, OUTPUT);
  pinMode(HW_WATCHDOG_PIN, OUTPUT);

#if NODE_ROLE == ROLE_CRAWLER
  pinMode(MOTOR_ENABLE, OUTPUT);
  digitalWrite(MOTOR_ENABLE, LOW);        // start stopped, always
  pinMode(PIN_AIN1, OUTPUT); pinMode(PIN_AIN2, OUTPUT);
  pinMode(PIN_BIN1, OUTPUT); pinMode(PIN_BIN2, OUTPUT);
  pinMode(PIN_LIGHTS, OUTPUT); digitalWrite(PIN_LIGHTS, HIGH);
  pinMode(PIN_MAGAZINE, OUTPUT); digitalWrite(PIN_MAGAZINE, LOW);
  LEDC_ATTACH(PIN_PWMA);
  LEDC_ATTACH(PIN_PWMB);
  LEDC_WRITE(PIN_PWMA, 0);
  LEDC_WRITE(PIN_PWMB, 0);

  pinMode(PIN_ENC_L, INPUT);   // external 10k pull-up required on 34/35
  pinMode(PIN_ENC_R, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_ENC_L), isrLeft, RISING);
  attachInterrupt(digitalPinToInterrupt(PIN_ENC_R), isrRight, RISING);

  analogReadResolution(12);
  analogSetPinAttenuation(PIN_BATT_ADC, ADC_11db);
  batteryMv = readBatteryMv();
#endif

  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_max_tx_power(TX_POWER);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(&promiscuous_rx);

  if (esp_now_init() != ESP_OK) {
    Serial.println("EVT,0,esp_now_init failed");
    while (true) { delay(1000); }
  }
  esp_now_register_recv_cb(onReceive);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  int8_t power = 0;
  esp_wifi_get_max_tx_power(&power);
  Serial.printf("BOOT,role=%d,id=%d,ch=%d,txpwr=%.2f\n",
                NODE_ROLE, NODE_ID, WIFI_CHANNEL, power * 0.25f);
}

// ================================================================ loop ===

void loop() {
  uint32_t now = millis();

  // Feed the external hardware watchdog. If this firmware hangs, the
  // monostable times out and pulls MOTOR_ENABLE low on its own.
  digitalWrite(HW_WATCHDOG_PIN, (now / 10) & 1);

#if NODE_ROLE == ROLE_CRAWLER
  // ---- link watchdog: the safety property the simulator models ----------
  if (lastCmdRxMs && (now - lastCmdRxMs > WATCHDOG_MS) && !watchdogTripped) {
    watchdogTripped = true;
    commandedMms = 0;
    digitalWrite(MOTOR_ENABLE, LOW);
    Serial.printf("EVT,%lu,WATCHDOG no command for %d ms - stopped\n",
                  (unsigned long)now, WATCHDOG_MS);
  }

  // ---- odometry and speed ----------------------------------------------
  static long lastTicks = 0;
  static uint32_t lastSpeedMs = 0;
  if (now - lastSpeedMs >= 100) {
    long ticks = totalTicks();
    float metres = (ticks - lastTicks) / (2.0f * TICKS_PER_METRE);
    measuredMms = (int16_t)(metres * 1000.0f / ((now - lastSpeedMs) / 1000.0f));
    lastTicks = ticks;
    lastSpeedMs = now;
    batteryMv = readBatteryMv();
    if (batteryMv < BATT_LOW_MV && !estopLatched) {
      emergencyStop("battery low");
    }
  }

  motorsWrite(commandedMms);

  // ---- telemetry upstream ----------------------------------------------
  if (now - lastTelem >= (uint32_t)(1000 / TELEM_HZ)) {
    lastTelem = now;
    uint8_t flags = 0;
    if (estopLatched)    flags |= FLAG_ESTOP;
    if (watchdogTripped) flags |= FLAG_WATCHDOG;
    if (lightsOn)        flags |= FLAG_LIGHTS;
    if (batteryMv < BATT_LOW_MV) flags |= FLAG_LOWBATT;
    long odoMm = (long)(totalTicks() / (2.0f * TICKS_PER_METRE) * 1000.0f);
    sendFrame(FRAME_TELEM, measuredMms, (int16_t)(batteryMv / 10), odoMm, flags);
    // Also straight out the USB port, so the crawler can be bench-tested
    // with no chain at all.
    Serial.printf("TLM,%lu,%d,0,%d,%lu,%ld,%u,%d,%u\n",
                  (unsigned long)now, NODE_ID, g_lastRssi,
                  (unsigned long)txSeq, odoMm, batteryMv, measuredMms, flags);
  }
#endif

#if NODE_ROLE == ROLE_BASE
  // ---- commands from the laptop ----------------------------------------
  while (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;
    lastOperatorMs = now;
    char c = line.charAt(0);
    if (c == 'E') {
      sendFrame(FRAME_ESTOP, 0, 0, 0, FLAG_ESTOP);
      Serial.printf("EVT,%lu,estop sent\n", (unsigned long)now);
    } else if (c == 'R') {
      sendFrame(FRAME_REARM);
    } else if (c == 'C') {
      int sp = line.substring(2).toInt();
      sendFrame(FRAME_CMD, (int16_t)sp);
    } else if (c == 'L') {
      sendFrame(FRAME_LIGHTS, line.substring(2).toInt() ? 1 : 0);
    } else if (c == 'D') {
      sendFrame(FRAME_DEPLOY);
    } else if (c == 'P') {
      // heartbeat only; keeps the base beating downstream
    }
  }

  // ---- keepalive downstream --------------------------------------------
  // Deliberately fail-safe: the base only beats while the LAPTOP is alive.
  // Pull the USB cable and the crawler's watchdog trips and stops it, which
  // is the behaviour we want.
  if (now - lastBeat >= (uint32_t)(1000 / HEARTBEAT_HZ)) {
    lastBeat = now;
    if (lastOperatorMs && (now - lastOperatorMs < OPERATOR_TIMEOUT_MS)) {
      sendFrame(FRAME_BEACON);
    }
  }
#endif

#if NODE_ROLE == ROLE_RELAY
  // Announce presence so the operator console can draw the chain even
  // before any traffic flows through this node.
  if (now - lastTelem >= 1000) {
    lastTelem = now;
    sendFrame(FRAME_HELLO);
  }
#endif

  if (now - lastBlink > 500) {
    lastBlink = now;
    digitalWrite(PIN_LED, !digitalRead(PIN_LED));
  }
  delay(1);
}
