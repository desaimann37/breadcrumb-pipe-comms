"""Hardware-in-the-loop mission: the same console, driven by a real robot.

``LiveMission`` has the identical snapshot contract as :class:`Mission`, so the
dashboard, the event log and the hop table all work unchanged. The difference
is where the numbers come from:

====================  ==========================  ==========================
Quantity              Simulated                   Live
====================  ==========================  ==========================
Crawler position      integrated from commands    wheel odometry
Per-hop SNR           modelled path loss          measured RSSI from each node
Link up/down          modelled packet error       telemetry freshness
Battery               modelled draw               ADC on the pack
Pipe context          model                       model (still predicted)
Video quality         chosen from modelled rate   chosen from measured rate
====================  ==========================  ==========================

Everything downstream of SNR - modulation choice, packet error rate, the video
ladder, the control margin - runs through exactly the same code as the
simulator. That is deliberate: it means a field test is a direct comparison of
measured against predicted, with no second implementation to disagree about.

This module performs no I/O. It consumes lines of text and emits lines of
text, which makes it testable without a robot attached; ``hardware/bridge/
live_bridge.py`` supplies the serial, UDP or replay transport.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .constants import MILE_M
from .link import RadioConfig, path_loss, pipe_context
from .mcs import CONTROL_MCS, WIFI_MCS, effective_rate_bps, packet_error_rate
from .mission import Sample
from .scenarios import DEFAULT_SCENARIO, build
from .video import build_video_state

# A node is considered present only while its telemetry is fresh.
HOP_TIMEOUT_S = 2.0
CONTROL_TIMEOUT_S = 0.6        # matches the firmware watchdog
TELEM_RATE_HZ = 10.0

FLAG_ESTOP = 1 << 0
FLAG_WATCHDOG = 1 << 1
FLAG_LIGHTS = 1 << 2
FLAG_LOWBATT = 1 << 3


@dataclass
class HopObs:
    """What the chain reported about one hop."""

    index: int
    node_id: int
    rssi_dbm: float
    last_seen: float
    from_m: float = 0.0
    to_m: float = 0.0
    packets: int = 0
    lost: int = 0

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.last_seen


@dataclass
class LiveNode:
    """A relay the operator dropped, with the odometry reading at drop time."""

    index: int
    node_id: int
    position_m: float
    deployed_at_s: float
    battery_wh: float = 26.0
    note: str = "dropped"
    recovered: bool = False
    rssi_dbm: Optional[float] = None

    @property
    def alive(self) -> bool:
        return not self.recovered

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "node_id": self.node_id,
            "position_m": round(self.position_m, 1),
            "deployed_at_s": round(self.deployed_at_s, 1),
            "battery_wh": round(self.battery_wh, 1),
            "note": self.note,
            "alive": self.alive,
            "rssi_dbm": self.rssi_dbm,
        }


class LiveMission:
    """Drive and observe a real crawler, in the simulator's data shapes."""

    POLICIES = {"live": ("Live", 0.0, "Relays are dropped by the operator.")}

    def __init__(self, scenario_key: str = DEFAULT_SCENARIO,
                 radio: Optional[RadioConfig] = None,
                 noise_floor_dbm: Optional[float] = None,
                 battery_full_v: float = 50.4,
                 battery_empty_v: float = 33.0,
                 camera_url: Optional[str] = None,
                 send: Optional[Callable[[str], None]] = None):
        self.scenario = build(scenario_key)
        self.profile = self.scenario.profile
        self.radio = radio or self.scenario.radio
        self.camera_url = camera_url
        self._send = send or (lambda line: None)

        # The receiver noise floor. Default is the modelled thermal floor for
        # the channel; override it with a measurement taken in the pipe with
        # all transmitters off, which is the honest way to do this.
        self.noise_floor_dbm = (noise_floor_dbm
                                if noise_floor_dbm is not None
                                else self.radio.noise_dbm())
        self.measured_noise_floor = noise_floor_dbm is not None

        self.t_s = 0.0
        self.t0 = time.monotonic()
        self.hops: Dict[int, HopObs] = {}
        self.nodes: List[LiveNode] = []
        self.node_counter = 0
        self.magazine = 16

        self.position_m = 0.0
        self.odo_zero_mm: Optional[int] = None
        self.speed_mps = 0.0
        self.commanded_mps = 0.0
        self.cruise_speed = 0.24
        self.battery_v = 0.0
        self.battery_pct = 0.0
        self.flags = 0
        self.lights_on = True
        self.distance_travelled_m = 0.0
        self._last_position = 0.0

        # A genuinely measured command round trip: time from sending a stop
        # to the crawler's own telemetry reporting the flag set. No clock
        # synchronisation needed, which is why this is the number to quote.
        self._pending: Optional[Tuple[str, int, bool, float]] = None
        self.command_rtt_ms: Optional[float] = None
        self.last_telem: Optional[float] = None
        self.last_rssi: Optional[float] = None
        self.link_rtt_ms: Optional[float] = None
        self.frames_rx = 0
        self.frames_expected = 0
        self._last_seq: Optional[int] = None

        self.events: Deque[dict] = deque(maxlen=400)
        self.event_id = 0
        self.samples: List[Sample] = []
        self._last_sample_t = -1e9
        self._last_sample_pos = -1e9

        # (hop length, measured path loss) pairs, for the in-field calibration.
        self.loss_observations: List[Tuple[float, float]] = []

        self.connected = False
        self.transport_name = "not connected"
        self.log("info", f"Live mode armed against {self.scenario.title}")
        self.log("info", "Waiting for telemetry from the base node")

    # -- logging ----------------------------------------------------------

    def log(self, level: str, text: str) -> None:
        self.event_id += 1
        self.events.append({
            "id": self.event_id,
            "t_s": round(self.t_s, 1),
            "level": level,
            "text": text,
            "position_m": round(self.position_m, 1),
        })

    # -- ingest -----------------------------------------------------------

    def ingest(self, line: str) -> None:
        """Consume one line from the base node. Never raises on bad input."""
        line = (line or "").strip()
        if not line:
            return
        parts = line.split(",")
        kind = parts[0]
        try:
            if kind == "TLM":
                self._ingest_telemetry(parts)
            elif kind == "HOP":
                self._ingest_hop(parts)
            elif kind == "RX":
                self._ingest_rx(parts)
            elif kind == "DROP":
                self._ingest_drop(parts)
            elif kind == "EVT":
                self._ingest_event(parts)
            elif kind == "BOOT":
                self.connected = True
                self.log("radio", f"Node booted: {','.join(parts[1:])}")
            elif kind == "CAM":
                self.log("info", f"Camera: {','.join(parts[1:])}")
        except (ValueError, IndexError):
            # A corrupt serial line is normal at 921600 baud over a long
            # cable. Count it and move on rather than killing the console.
            self.log("warn", f"unparsed: {line[:60]}")

    def _ingest_telemetry(self, p: List[str]) -> None:
        # TLM,t_ms,src,hop,rssi,seq,odo_mm,batt_mv,speed_mms,flags
        t_ms = int(p[1])
        hop = int(p[3])
        rssi = float(p[4])
        seq = int(p[5])
        odo_mm = int(p[6])
        batt_mv = int(p[7])
        speed_mms = int(p[8])
        flags = int(p[9]) if len(p) > 9 else 0

        now = time.monotonic()
        self.connected = True
        self.last_telem = now
        self.last_rssi = rssi

        if self.odo_zero_mm is None:
            self.odo_zero_mm = odo_mm
            self.log("info", f"Odometry zeroed at {odo_mm} mm")
        self.position_m = max(0.0, (odo_mm - self.odo_zero_mm) / 1000.0)
        self.distance_travelled_m += abs(self.position_m - self._last_position)
        self._last_position = self.position_m

        self.speed_mps = speed_mms / 1000.0
        self.battery_v = batt_mv / 1000.0
        self.battery_pct = self._battery_pct(self.battery_v)

        if flags != self.flags:
            self._flag_events(self.flags, flags)
            self.flags = flags
        self._settle_pending(flags)
        self.lights_on = bool(flags & FLAG_LIGHTS)

        # Packet delivery: sequence numbers are contiguous at the source.
        self.frames_rx += 1
        if self._last_seq is not None and seq > self._last_seq:
            self.frames_expected += seq - self._last_seq
        else:
            self.frames_expected += 1
        self._last_seq = seq

        # One-way delay: the crawler stamps t_ms at origin. Clocks are not
        # synchronised, so only the *change* is meaningful; track the minimum
        # observed offset and report the excess over it as latency.
        host_ms = (now - self.t0) * 1000.0
        offset = host_ms - t_ms
        if not hasattr(self, "_min_offset") or offset < self._min_offset:
            self._min_offset = offset
        self.link_rtt_ms = max(0.0, (offset - self._min_offset) * 2.0)

        # The crawler always terminates the chain, so its hop index is the
        # number of relays between it and the base. Using the frame's hop
        # counter here would double-count the last link.
        self._touch_hop(index=len([n for n in self.nodes if n.alive]),
                        node_id=int(p[2]), rssi=rssi)

    def _ingest_hop(self, p: List[str]) -> None:
        """A relay reporting the RSSI it hears from the node nearer the base.

        Relay k in drop order terminates hop k-1, so the chain index comes
        from the order the operator dropped the nodes, which the console
        knows, rather than from a hop counter that every relay overwrites.
        """
        # HOP,t_ms,relay_id,rssi_up,hop_index
        node_id = int(p[2])
        rssi = float(p[3])
        alive = sorted((n for n in self.nodes if n.alive),
                       key=lambda n: n.deployed_at_s)
        index = None
        for ordinal, node in enumerate(alive, start=1):
            if node.node_id == node_id:
                index = ordinal - 1
                node.rssi_dbm = rssi
                break
        if index is None:
            index = max(int(p[4]) - 1, 0) if len(p) > 4 else 0
        self._touch_hop(index=index, node_id=node_id, rssi=rssi)

    def _ingest_rx(self, p: List[str]) -> None:
        # RX,t_ms,type,src,hop,rssi,seq
        self.connected = True
        self._touch_hop(index=max(int(p[4]), 0), node_id=int(p[3]),
                        rssi=float(p[5]))

    def _ingest_drop(self, p: List[str]) -> None:
        """The crawler reporting that its magazine released a puck.

        DROP,<t_ms>,<node_id>,<odo_mm>

        The drop position comes from the robot's own odometry at the instant
        the actuator fired, not from whatever the console happened to be
        showing. That difference matters: the hop endpoints feed straight into
        the measured-against-modelled comparison.
        """
        node_id = int(p[2])
        odo_mm = int(p[3])
        if self.odo_zero_mm is None:
            self.odo_zero_mm = odo_mm
        position = max(0.0, (odo_mm - self.odo_zero_mm) / 1000.0)
        if any(n.node_id == node_id and n.alive for n in self.nodes):
            return
        self.node_counter += 1
        self.nodes.append(LiveNode(index=self.node_counter, node_id=node_id,
                                   position_m=position, deployed_at_s=self.t_s,
                                   rssi_dbm=self.last_rssi, note="reported by crawler"))
        self.log("node", f"Relay #{node_id} released at {position:.1f} m "
                         f"(crawler odometry)")

    def _ingest_event(self, p: List[str]) -> None:
        text = ",".join(p[2:]) if len(p) > 2 else ",".join(p[1:])
        level = "alarm" if ("ESTOP" in text or "WATCHDOG" in text) else "info"
        self.log(level, f"node: {text}")

    def _touch_hop(self, index: int, node_id: int, rssi: float) -> None:
        obs = self.hops.get(index)
        if obs is None:
            obs = HopObs(index=index, node_id=node_id, rssi_dbm=rssi,
                         last_seen=time.monotonic())
            self.hops[index] = obs
            self.log("node", f"Hop {index + 1} appeared (node {node_id}, "
                             f"{rssi:.0f} dBm)")
        else:
            # Light smoothing: raw RSSI is noisy enough to make the hop table
            # unreadable, and every downstream decision is slow anyway.
            obs.rssi_dbm = 0.7 * obs.rssi_dbm + 0.3 * rssi
            obs.node_id = node_id
            obs.last_seen = time.monotonic()
        obs.packets += 1

    def _expect(self, label: str, flag: int, want_set: bool) -> None:
        self._pending = (label, flag, want_set, time.monotonic())

    def _settle_pending(self, flags: int) -> None:
        if self._pending is None:
            return
        label, flag, want_set, sent_at = self._pending
        if bool(flags & flag) == want_set:
            self.command_rtt_ms = (time.monotonic() - sent_at) * 1000.0
            self._pending = None
            self.log("alarm" if flag == FLAG_ESTOP else "info",
                     f"{label} confirmed by the crawler in "
                     f"{self.command_rtt_ms:.0f} ms over "
                     f"{len(self._live_hops())} hops")
        elif time.monotonic() - sent_at > 3.0:
            self._pending = None
            self.log("alarm", f"{label} NOT confirmed within 3 s - "
                              "check the chain before driving")

    def _flag_events(self, old: int, new: int) -> None:
        def changed(bit, on_text, off_text):
            if (new & bit) and not (old & bit):
                self.log("alarm", on_text)
            elif (old & bit) and not (new & bit):
                self.log("info", off_text)
        changed(FLAG_ESTOP, "Crawler reports EMERGENCY STOP latched",
                "Emergency stop cleared on the crawler")
        changed(FLAG_WATCHDOG,
                "Crawler WATCHDOG tripped: no command frame, stopped itself",
                "Crawler watchdog cleared, link restored")
        changed(FLAG_LOWBATT, "Crawler battery low", "Crawler battery recovered")

    def _battery_pct(self, volts: float) -> float:
        span = max(self.battery_full_v - self.battery_empty_v, 1e-6)
        return max(0.0, min(100.0, 100.0 * (volts - self.battery_empty_v) / span))

    # Set as attributes so they can be tuned per pack without a constructor
    # change; defaults match a 12S lithium pack.
    battery_full_v = 50.4
    battery_empty_v = 33.0

    # -- link evaluation --------------------------------------------------

    def _live_hops(self) -> List[HopObs]:
        """Hops whose telemetry is still fresh, ordered base to crawler."""
        fresh = [h for h in self.hops.values() if h.age_s < HOP_TIMEOUT_S]
        for stale in [k for k, h in self.hops.items() if h.age_s > 30.0]:
            del self.hops[stale]
        return sorted(fresh, key=lambda h: h.index)

    def _hop_endpoints(self, count: int) -> List[float]:
        """Positions along the pipe for the base, each relay, and the crawler."""
        drops = sorted(n.position_m for n in self.nodes if n.alive)
        points = [0.0] + drops + [self.position_m]
        # If the chain reports more hops than we have recorded drops, space the
        # unknown ones evenly; they are still real hops, we just do not know
        # where the nodes are.
        while len(points) - 1 < count:
            lo, hi = points[-2], points[-1]
            points.insert(len(points) - 1, (lo + hi) / 2.0)
        while len(points) - 1 > count and len(points) > 2:
            points.pop(-2)
        return points

    def chain_dict(self) -> dict:
        """Measured link budget in the same shape ChainResult produces."""
        obs = self._live_hops()
        points = self._hop_endpoints(len(obs))
        hops = []
        goodputs = []
        margins = []

        for i, o in enumerate(obs):
            from_m = points[i] if i < len(points) else 0.0
            to_m = points[i + 1] if i + 1 < len(points) else self.position_m
            o.from_m, o.to_m = from_m, to_m

            snr = min(o.rssi_dbm - self.noise_floor_dbm, self.radio.snr_ceiling_db)
            rate, mcs, per = effective_rate_bps(snr, self.radio.bandwidth_hz, WIFI_MCS)
            margin = snr - WIFI_MCS[0].required_snr_db
            measured_loss = self.radio.tx_power_dbm - o.rssi_dbm

            ctrl_snr = min(
                o.rssi_dbm - (self.noise_floor_dbm
                              - 10 * math.log10(self.radio.bandwidth_hz
                                                / self.radio.control_bandwidth_hz)),
                self.radio.snr_ceiling_db)
            ctrl_per = packet_error_rate(ctrl_snr, CONTROL_MCS)

            # What the model predicted for this same span, so the operator can
            # see measured against predicted while standing at the manhole.
            # Must be the *full* budget - spreading, coupling and
            # implementation loss included - or it is not comparable with
            # tx_power minus RSSI.
            try:
                modelled, _ = path_loss(self.profile, self.radio, from_m, to_m)
            except Exception:
                modelled = 0.0

            hops.append({
                "index": i,
                "from_m": round(from_m, 1),
                "to_m": round(to_m, 1),
                "distance_m": round(abs(to_m - from_m), 1),
                "path_loss_db": round(measured_loss, 1),
                "rx_dbm": round(o.rssi_dbm, 1),
                "snr_db": round(snr, 1),
                "margin_db": round(margin, 1),
                "mcs": mcs.name if mcs else "no link",
                "per": round(per, 4),
                "rate_mbps": round(rate / 1e6, 3),
                "shannon_mbps": 0.0,
                "control_snr_db": round(ctrl_snr, 1),
                "control_margin_db": round(ctrl_snr - CONTROL_MCS.required_snr_db, 1),
                "control_ok": ctrl_per < 0.2,
                "below_cutoff": False,
                "delay_spread_ns": 0.0,
                "feature_db": 0.0,
                "distributed_db": round(measured_loss, 1),
                "worst_feature": None,
                "latency_ms": round(self.link_rtt_ms / max(len(obs), 1) / 2.0, 2)
                if self.link_rtt_ms else 0.0,
                "measured": True,
                "modelled_loss_db": round(modelled, 1),
                "model_error_db": round(measured_loss - modelled, 1),
                "node_id": o.node_id,
                "age_ms": round(o.age_s * 1000),
            })
            goodputs.append(rate)
            margins.append(margin)

        bottleneck = min(goodputs) if goodputs else 0.0
        delivered = (self.frames_rx / self.frames_expected) if self.frames_expected else 0.0
        return {
            "hops": hops,
            "video_goodput_mbps": round(bottleneck / self.radio.airtime_divisor / 1e6, 3),
            "network_latency_ms": round(self.link_rtt_ms or 0.0, 1),
            # Prefer the measured command round trip; fall back to the
            # timestamp-derived estimate until a command has been confirmed.
            "control_rtt_ms": round(self.command_rtt_ms
                                    if self.command_rtt_ms is not None
                                    else (self.link_rtt_ms or 0.0), 1),
            "control_ok": self.control_ok,
            "weakest_hop": margins.index(min(margins)) if margins else 0,
            "min_margin_db": round(min(margins), 1) if margins else 0.0,
            "total_path_loss_db": round(sum(h["path_loss_db"] for h in hops), 1),
            "end_to_end_per": round(max(0.0, 1.0 - delivered), 4),
        }

    @property
    def control_ok(self) -> bool:
        """Measured, not modelled: is telemetry still arriving?"""
        if self.last_telem is None:
            return False
        return (time.monotonic() - self.last_telem) < CONTROL_TIMEOUT_S

    # -- calibration ------------------------------------------------------

    def calibration(self) -> dict:
        """Fit dB/m from what the chain has actually measured so far.

        This is the deliverable of a field test: the measured attenuation of
        this pipe, next to what the model predicted for it.
        """
        pts = [(d, l) for d, l in self.loss_observations if d >= 2.0]
        modelled = self.profile.db_per_m_at(self.radio.frequency_hz,
                                            self.position_m) * 1000.0
        out = {
            "samples": len(pts),
            "modelled_db_per_km": round(modelled, 1),
            "measured_db_per_km": None,
            "ratio": None,
            "r_squared": None,
            "noise_floor_dbm": round(self.noise_floor_dbm, 1),
            "noise_floor_measured": self.measured_noise_floor,
        }
        if len(pts) < 20:
            return out
        n = len(pts)
        mx = sum(d for d, _ in pts) / n
        my = sum(l for _, l in pts) / n
        sxx = sum((d - mx) ** 2 for d, _ in pts)
        if sxx <= 0:
            return out
        slope = sum((d - mx) * (l - my) for d, l in pts) / sxx
        intercept = my - slope * mx
        ss_tot = sum((l - my) ** 2 for _, l in pts)
        ss_res = sum((l - (intercept + slope * d)) ** 2 for d, l in pts)
        out["measured_db_per_km"] = round(slope * 1000.0, 1)
        out["r_squared"] = round(1.0 - ss_res / ss_tot, 3) if ss_tot > 0 else 0.0
        if modelled > 0:
            out["ratio"] = round(slope * 1000.0 / modelled, 2)
        return out

    # -- commands ---------------------------------------------------------

    def command(self, name: str, payload: Optional[dict] = None) -> dict:
        """Translate a console command into wire lines for the base node."""
        payload = payload or {}
        if name == "drive":
            self.commanded_mps = float(payload.get("speed_mps", self.cruise_speed))
            self._send(f"C,{int(self.commanded_mps * 1000)}")
            return {"ok": True, "sent": "drive"}
        if name == "stop":
            self.commanded_mps = 0.0
            self._send("C,0")
            self.log("info", "Operator stop")
            return {"ok": True}
        if name == "return":
            self.commanded_mps = -self.cruise_speed
            self._send(f"C,{int(-self.cruise_speed * 1000)}")
            self.log("info", "Returning")
            return {"ok": True}
        if name == "estop":
            t0 = time.monotonic()
            self._send("E")
            self.commanded_mps = 0.0
            self._expect("EMERGENCY STOP", FLAG_ESTOP, True)
            self.log("alarm", "EMERGENCY STOP sent to the chain, "
                              "awaiting confirmation from the crawler")
            return {"ok": True,
                    "rtt_ms": self.command_rtt_ms or 0.0,
                    "hops": len(self._live_hops()),
                    "awaiting_confirmation": True,
                    "send_ms": round((time.monotonic() - t0) * 1000, 2)}
        if name == "clear_estop":
            self._send("R")
            self._expect("Re-arm", FLAG_ESTOP, False)
            self.log("info", "Re-arm sent")
            return {"ok": True}
        if name == "lights":
            # Also the cheapest honest way to measure the chain round trip
            # without stopping the robot: toggle a flag and time the echo.
            self.lights_on = bool(payload.get("on", not self.lights_on))
            self._send(f"L,{1 if self.lights_on else 0}")
            self._expect("Lights", FLAG_LIGHTS, self.lights_on)
            return {"ok": True, "lights_on": self.lights_on}
        if name == "deploy":
            return self._deploy()
        if name == "set_speed":
            self.cruise_speed = max(0.02, min(0.35, float(payload.get("mps", 0.24))))
            if abs(self.commanded_mps) > 1e-6:
                sign = 1 if self.commanded_mps > 0 else -1
                self._send(f"C,{int(sign * self.cruise_speed * 1000)}")
            return {"ok": True, "cruise_speed": self.cruise_speed}
        if name == "set_magazine":
            self.magazine = max(0, min(48, int(payload.get("count", 16))))
            return {"ok": True, "magazine": self.magazine}
        if name == "auto":
            self.log("warn", "Autonomous mode is disabled in live mode: "
                             "drive manually and drop relays by hand")
            return {"ok": False, "error": "manual control only in live mode"}
        if name in ("set_radio", "set_policy", "reset"):
            # Radio settings are compiled into the firmware; changing them on
            # the console would be a lie.
            return {"ok": False,
                    "error": f"{name} is simulation-only; reflash the nodes to change the radio"}
        return {"ok": False, "error": f"unknown command {name!r}"}

    def _deploy(self) -> dict:
        """Ask the crawler to release a puck.

        The node is not recorded here. The crawler reports the drop with the
        odometry reading taken when the actuator fired, and that DROP line is
        what creates the relay. Recording it optimistically on the console
        would mean inventing a position if the actuator jammed.
        """
        remaining = self.magazine - len([n for n in self.nodes if n.alive])
        if remaining <= 0:
            self.log("warn", "Magazine empty")
            return {"ok": False, "error": "magazine empty"}
        self._send("D")
        self.log("node", f"Release commanded at about {self.position_m:.1f} m, "
                         "waiting for the crawler to confirm")
        return {"ok": True, "commanded": True, "awaiting_confirmation": True}

    # -- stepping ---------------------------------------------------------

    def step(self, dt: float = 0.05) -> None:
        self.t_s = time.monotonic() - self.t0
        if self.last_telem is not None:
            age = time.monotonic() - self.last_telem
            if age > CONTROL_TIMEOUT_S and self.connected:
                if not getattr(self, "_loss_logged", False):
                    self.log("alarm",
                             f"Telemetry lost {age:.1f} s ago. The crawler "
                             "watchdog stops it after 600 ms without a command.")
                    self._loss_logged = True
            elif age < CONTROL_TIMEOUT_S:
                if getattr(self, "_loss_logged", False):
                    self.log("info", "Telemetry restored")
                self._loss_logged = False
        self._record()

    def _record(self) -> None:
        moved = abs(self.position_m - self._last_sample_pos)
        aged = self.t_s - self._last_sample_t
        if moved < 2.0 and aged < 2.0:
            return
        self._last_sample_pos = self.position_m
        self._last_sample_t = self.t_s
        chain = self.chain_dict()
        # Record one calibration point per sample, not per snapshot, so the
        # fit is not dominated by however often the browser happens to poll.
        for h in chain["hops"]:
            if h["distance_m"] > 1.0:
                self.loss_observations.append((h["distance_m"], h["path_loss_db"]))
        if len(self.loss_observations) > 5000:
            del self.loss_observations[:-5000]
        video = self._video(chain)
        self.samples.append(Sample(
            t_s=self.t_s,
            position_m=self.position_m,
            hops=len(chain["hops"]),
            min_margin_db=chain["min_margin_db"],
            goodput_bps=chain["video_goodput_mbps"] * 1e6,
            latency_ms=video["glass_to_glass_ms"],
            control_rtt_ms=chain["control_rtt_ms"],
            video_tier=video["name"],
            battery_pct=self.battery_pct,
            control_ok=chain["control_ok"],
        ))

    def _video(self, chain: dict) -> dict:
        per = max((h["per"] for h in chain["hops"]), default=1.0)
        state = build_video_state(
            chain["video_goodput_mbps"] * 1e6, per,
            chain["network_latency_ms"], 0.25 * chain["network_latency_ms"],
            control_reserve_bps=48e3, margin_db=chain["min_margin_db"])
        return state.to_dict()

    # -- snapshot ---------------------------------------------------------

    def history(self) -> List[dict]:
        return [s.to_dict() for s in self.samples]

    def snapshot(self) -> dict:
        chain = self.chain_dict()
        alive = [n for n in self.nodes if n.alive]
        if not self.connected:
            phase = "idle"
        elif not self.control_ok:
            phase = "aborted"
        elif self.speed_mps > 0.01:
            phase = "outbound"
        elif self.speed_mps < -0.01:
            phase = "returning"
        else:
            phase = "idle"

        return {
            "t_s": round(self.t_s, 2),
            "phase": phase,
            "auto": False,
            "cruise_speed": round(self.cruise_speed, 3),
            "time_scale": 1.0,
            "paused": False,
            "scenario": {
                "key": self.scenario.key,
                "title": self.scenario.title + "  [LIVE HARDWARE]",
                "difficulty": self.scenario.difficulty,
                "why": "Telemetry is measured. Pipe context is still modelled - "
                       "the point of the run is to compare the two.",
            },
            "crawler": {
                "position_m": round(self.position_m, 2),
                "speed_mps": round(self.speed_mps, 3),
                "commanded_speed_mps": round(self.commanded_mps, 3),
                "battery_wh": round(self.battery_v, 1),
                "battery_pct": round(self.battery_pct, 1),
                "distance_travelled_m": round(self.distance_travelled_m, 1),
                "estop": bool(self.flags & FLAG_ESTOP),
                "watchdog_tripped": bool(self.flags & FLAG_WATCHDOG),
                "nodes_remaining": max(0, self.magazine - len(alive)),
                "lights_on": self.lights_on,
                "can_return": self.battery_pct > 20,
                "return_energy_wh": 0.0,
            },
            "nodes": [n.to_dict() for n in alive],
            "spent_nodes": sum(1 for n in self.nodes if n.recovered),
            "chain": chain,
            "video": self._video(chain),
            "context": pipe_context(self.profile, self.radio, self.position_m),
            "radio": self.radio.to_dict(),
            "policy": {
                "key": "live",
                "label": "Live",
                "why": "Relays are dropped by the operator.",
                "deploy_margin_db": 0.0,
                "magazine": self.magazine,
            },
            "events": list(self.events)[-14:],
            "samples": len(self.samples),
            "live": {
                "mode": "live",
                "connected": self.connected,
                "transport": self.transport_name,
                "telemetry_age_ms": round(
                    (time.monotonic() - self.last_telem) * 1000)
                if self.last_telem else None,
                "last_rssi_dbm": self.last_rssi,
                "command_rtt_ms": round(self.command_rtt_ms, 1)
                if self.command_rtt_ms is not None else None,
                "awaiting_confirmation": self._pending[0] if self._pending else None,
                "frames_rx": self.frames_rx,
                "delivery_pct": round(
                    100.0 * self.frames_rx / self.frames_expected, 1)
                if self.frames_expected else 0.0,
                "camera_url": self.camera_url,
                "calibration": self.calibration(),
            },
        }
