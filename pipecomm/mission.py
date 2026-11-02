"""The mission engine: one crawler, a growing chain of relays, a live link.

This is the piece that turns the static link budget into something you can
watch.  Every tick it advances the crawler, re-evaluates every hop including
slow multipath fading, decides whether a relay node has to be dropped, picks a
video quality that fits the surviving bandwidth, and runs the safety watchdog.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

from .crawler import Crawler, CrawlerConfig, RelayNode
from .link import (ChainResult, RadioConfig, evaluate_chain, max_reach_m,
                   pipe_context)
from .pipe import PipeFeature, PipeProfile
from .scenarios import DEFAULT_SCENARIO, Scenario, build
from .video import VideoState, build_video_state, select_tier

PHASES = ("idle", "outbound", "returning", "complete", "aborted")


@dataclass
class Sample:
    """One recorded point of the inspection, for charts and CSV export."""

    t_s: float
    position_m: float
    hops: int
    min_margin_db: float
    goodput_bps: float
    latency_ms: float
    control_rtt_ms: float
    video_tier: str
    battery_pct: float
    control_ok: bool

    def to_dict(self) -> dict:
        return {
            "t_s": round(self.t_s, 1),
            "position_m": round(self.position_m, 1),
            "hops": self.hops,
            "min_margin_db": round(self.min_margin_db, 2),
            "goodput_mbps": round(self.goodput_bps / 1e6, 3),
            "latency_ms": round(self.latency_ms, 1),
            "control_rtt_ms": round(self.control_rtt_ms, 1),
            "video_tier": self.video_tier,
            "battery_pct": round(self.battery_pct, 1),
            "control_ok": self.control_ok,
        }


def plan_nodes(profile: PipeProfile, radio: RadioConfig, margin_db: Optional[float] = None,
               limit_m: Optional[float] = None, step_m: float = 5.0,
               max_nodes: int = 64) -> List[float]:
    """Pre-compute where relays have to go to cover the run.

    This is the answer to the headline question: how many nodes does a two-mile
    inspection need?  It walks the pipe, extending each hop until the margin
    runs out, and records where the next node must be dropped.
    """
    limit = limit_m if limit_m is not None else profile.length_m
    positions: List[float] = []
    anchor = 0.0
    while anchor < limit - 1e-6 and len(positions) < max_nodes:
        reach = max_reach_m(profile, radio, anchor, margin_db, step_m, limit)
        if reach <= anchor + step_m:
            # Cannot even make one step: the next feature is impassable, so the
            # node has to sit right on top of it.
            reach = min(anchor + step_m, limit)
        if reach >= limit - 1e-6:
            break
        positions.append(reach)
        anchor = reach
    return positions


class Mission:
    """A live, steppable inspection run."""

    def __init__(self, scenario_key: str = DEFAULT_SCENARIO,
                 radio: Optional[RadioConfig] = None,
                 crawler_config: Optional[CrawlerConfig] = None,
                 seed: int = 7):
        self.scenario: Scenario = build(scenario_key)
        self.profile: PipeProfile = self.scenario.profile
        self.radio: RadioConfig = radio or self.scenario.radio
        self.crawler = Crawler(crawler_config or CrawlerConfig())
        self.rng = random.Random(seed)

        self.t_s = 0.0
        self.phase = "idle"
        self.auto = False
        self.cruise_speed = self.crawler.config.max_speed_mps * 0.8
        self.nodes: List[RelayNode] = []
        self.node_counter = 0
        self.events: Deque[dict] = deque(maxlen=400)
        self.event_id = 0
        self.samples: List[Sample] = []
        self._last_sample_pos = -1e9
        self._last_sample_t = -1e9
        self._fade: Dict[int, float] = {}
        self._last_tier_name = ""
        self.deploy_policy = "balanced"
        self._deploy_margin_db = self._policy_margin()
        self._stranded_logged = False
        # Deploy decisions run off a filtered margin, not an instantaneous
        # reading: a single 3 dB fade is not a reason to spend a relay node.
        self._margin_ewma: Optional[float] = None
        self._control_bad_s = 0.0
        self._control_good_s = 0.0
        self._triggered_features: set = set()

        self.chain: ChainResult = ChainResult()
        self.video: VideoState = build_video_state(0.0, 1.0, 0.0, 0.0)
        self._refresh_link(0.0)
        self.log("info", f"Scenario loaded: {self.scenario.title}")
        self.log("info",
                 f"{self.profile.name}, {self.profile.length_m:.0f} m "
                 f"({self.profile.length_m / 1609.344:.2f} miles)")

    # Relay budget policy: how much margin to insist on before spending a node.
    # This is the main engineering trade in the system - relays versus picture.
    POLICIES = {
        "quality": ("Quality", 4.0,
                    "Keeps a fat margin. Best picture, most relays."),
        "balanced": ("Balanced", 2.0,
                     "Drops a relay as the fade margin runs out."),
        "stretch": ("Stretch", -5.0,
                    "Runs hops to the edge. Fewest relays, video degrades."),
    }

    def _policy_margin(self) -> float:
        _, offset, _ = self.POLICIES.get(self.deploy_policy, self.POLICIES["balanced"])
        return self.radio.fade_margin_db + offset

    # -- logging ----------------------------------------------------------

    def log(self, level: str, text: str) -> None:
        self.event_id += 1
        self.events.append({
            "id": self.event_id,
            "t_s": round(self.t_s, 1),
            "level": level,
            "text": text,
            "position_m": round(self.crawler.position_m, 1),
        })

    # -- link -------------------------------------------------------------

    def node_positions(self) -> List[float]:
        """Base station, then every live relay, then the crawler."""
        live = sorted(n.position_m for n in self.nodes if n.alive)
        return [0.0] + live + [self.crawler.position_m]

    def _fades(self, n_hops: int, dt: float) -> List[float]:
        """Slow multipath fading per hop, as an Ornstein-Uhlenbeck walk.

        The hop that ends on the crawler fades faster and deeper because the
        crawler is physically moving through the modal interference pattern.
        """
        out: List[float] = []
        for i in range(n_hops):
            moving = (i == n_hops - 1)
            tau = 1.2 if moving else 6.0
            sigma = 2.6 if moving else 0.9
            prev = self._fade.get(i, 0.0)
            a = math.exp(-dt / tau)
            value = prev * a + sigma * math.sqrt(max(1.0 - a * a, 0.0)) * self.rng.gauss(0, 1)
            self._fade[i] = value
            out.append(value)
        for stale in [k for k in self._fade if k >= n_hops]:
            del self._fade[stale]
        return out

    def _refresh_link(self, dt: float) -> None:
        positions = self.node_positions()
        fades = self._fades(max(len(positions) - 1, 0), max(dt, 1e-3))
        self.chain = evaluate_chain(self.profile, self.radio, positions, fades)

        per = max((h.per for h in self.chain.hops), default=1.0)
        jitter = 0.25 * self.chain.network_latency_ms if math.isfinite(
            self.chain.network_latency_ms) else 100.0
        net_ms = self.chain.network_latency_ms if math.isfinite(
            self.chain.network_latency_ms) else 1e6
        self.video = build_video_state(
            self.chain.video_goodput_bps, per, net_ms, jitter,
            control_reserve_bps=48e3, margin_db=self.chain.min_margin_db,
        )
        if self.chain.hops:
            last_margin = self.chain.hops[-1].margin_db
            if self._margin_ewma is None:
                self._margin_ewma = last_margin
            else:
                a = math.exp(-max(dt, 1e-3) / 3.0)
                self._margin_ewma = self._margin_ewma * a + last_margin * (1 - a)

        if self.video.tier.name != self._last_tier_name:
            if self._last_tier_name:
                self.log("video", f"Video {self._last_tier_name} -> {self.video.tier.name}")
            self._last_tier_name = self.video.tier.name

    # -- relay deployment -------------------------------------------------

    def next_heavy_feature(self, lookahead_m: float = 12.0,
                           min_loss_db: float = 5.0) -> Optional[PipeFeature]:
        x = self.crawler.position_m
        for ft in self.profile.features:
            if x < ft.position_m <= x + lookahead_m and ft.loss_db >= min_loss_db:
                return ft
        return None

    def deploy_node(self, note: str = "manual") -> Optional[RelayNode]:
        if self.crawler.nodes_remaining <= 0:
            self.log("warn", "Deploy requested but the magazine is empty")
            return None
        pos = max(self.crawler.position_m - 0.3, 0.0)
        live = [n for n in self.nodes if n.alive]
        if live and abs(pos - max(n.position_m for n in live)) < 5.0:
            return None
        self.node_counter += 1
        node = RelayNode(
            index=self.node_counter,
            position_m=pos,
            deployed_at_s=self.t_s,
            battery_wh=self.crawler.config.node_battery_wh,
            note=note,
        )
        self.nodes.append(node)
        self.crawler.nodes_remaining -= 1
        self._margin_ewma = None
        self.log("node",
                 f"Relay #{node.index} dropped at {pos:.0f} m ({note}); "
                 f"{self.crawler.nodes_remaining} left in magazine")
        self._refresh_link(0.05)
        return node

    def _auto_deploy(self) -> None:
        if self.phase != "outbound" or self.crawler.nodes_remaining <= 0:
            return
        if not self.chain.hops:
            return
        last = self.chain.hops[-1]
        filtered = self._margin_ewma if self._margin_ewma is not None else last.margin_db

        if filtered < self._deploy_margin_db:
            self.deploy_node(f"margin {filtered:.1f} dB")
            return
        feature = self.next_heavy_feature()
        if feature is not None and id(feature) not in self._triggered_features:
            # Drop a node *before* an obstacle the radio cannot see past, and
            # only once per obstacle.
            self._triggered_features.add(id(feature))
            self.deploy_node(f"ahead of {feature.label} ({feature.loss_db:.0f} dB)")
            return
        if not last.control_ok:
            self.deploy_node("control link degraded")

    def _auto_recover(self) -> None:
        if self.phase != "returning":
            return
        live = [n for n in self.nodes if n.alive]
        if not live:
            return
        furthest = max(live, key=lambda n: n.position_m)
        if self.crawler.position_m <= furthest.position_m + 0.5:
            furthest.recovered = True
            self.crawler.nodes_remaining += 1
            self._margin_ewma = None
            self.log("node", f"Relay #{furthest.index} recovered at "
                             f"{furthest.position_m:.0f} m")
            self._refresh_link(0.05)

    # -- auto mission -----------------------------------------------------

    def start(self) -> None:
        self.auto = True
        if self.phase in ("idle", "complete", "aborted"):
            self.phase = "outbound"
            self.log("info", "Autonomous inspection started (outbound)")
        self.crawler.clear_estop()
        self.crawler.set_speed(self.cruise_speed)

    def turn_around(self, reason: str) -> None:
        if self.phase == "returning":
            return
        self.phase = "returning"
        self.log("info", f"Turning back: {reason}")
        self.crawler.set_speed(-self.cruise_speed)

    def _auto_logic(self) -> None:
        if not self.auto:
            return
        c = self.crawler
        if self.phase == "outbound":
            if not (c.estop or c.watchdog_tripped):
                c.set_speed(self.cruise_speed)
            if c.position_m >= self.profile.length_m - 0.2:
                self.turn_around("reached the end of the run")
            elif c.must_turn_back(self.cruise_speed):
                self.turn_around(
                    f"battery reserve reached ({c.battery_wh:.0f} Wh left, "
                    f"{c.return_energy_needed_wh(self.cruise_speed):.0f} Wh needed to get back)")
            elif (c.nodes_remaining == 0 and self.chain.hops
                  and self.chain.hops[-1].margin_db < 1.0):
                if not self._stranded_logged:
                    self.log("warn", "Magazine empty and the last hop is at its limit")
                    self._stranded_logged = True
                self.turn_around("out of relay nodes")
        elif self.phase == "returning":
            if c.battery_wh <= 0.0 and c.position_m > 1.0:
                self.phase = "aborted"
                self.auto = False
                self.log("alarm",
                         f"Crawler stranded at {c.position_m:.0f} m with a flat "
                         "battery: physical recovery required")
                return
            if not (c.estop or c.watchdog_tripped):
                c.set_speed(-self.cruise_speed)
            if c.position_m <= 0.5:
                c.set_speed(0.0)
                self.phase = "complete"
                self.auto = False
                self.log("info",
                         f"Inspection complete. Reached {max((s.position_m for s in self.samples), default=0):.0f} m, "
                         f"{self.node_counter} relays used, "
                         f"{c.battery_pct:.0f}% battery left")

    # -- stepping ---------------------------------------------------------

    def step(self, dt: float = 0.05) -> None:
        self.t_s += dt
        raw_control = self.chain.control_ok and bool(self.chain.hops)
        if raw_control:
            self._control_good_s += dt
            self._control_bad_s = 0.0
        else:
            self._control_bad_s += dt
            self._control_good_s = 0.0
        # Debounce so a momentary fade does not flap the watchdog.
        control_ok = raw_control if self._control_good_s >= 0.4 else (
            self._control_bad_s < 0.25)
        for text in self.crawler.step(dt, control_ok, self.profile.length_m):
            level = "alarm" if "WATCHDOG" in text or "battery" in text else "info"
            self.log(level, text)
            if "Reached end" in text and self.auto:
                self.turn_around("reached the end of the run")

        self._refresh_link(dt)
        self._auto_logic()
        self._auto_deploy()
        self._auto_recover()
        self._drain_nodes(dt)
        self._record(dt)

    def _drain_nodes(self, dt: float) -> None:
        drain = self.crawler.config.node_idle_w * dt / 3600.0
        for node in self.nodes:
            if node.alive:
                was = node.battery_wh
                node.battery_wh = max(0.0, node.battery_wh - drain)
                if was > 0 and node.battery_wh == 0:
                    self.log("alarm", f"Relay #{node.index} battery dead at "
                                      f"{node.position_m:.0f} m")

    def _record(self, dt: float) -> None:
        moved = abs(self.crawler.position_m - self._last_sample_pos)
        aged = self.t_s - self._last_sample_t
        if moved < 5.0 and aged < 2.0:
            return
        self._last_sample_pos = self.crawler.position_m
        self._last_sample_t = self.t_s
        self.samples.append(Sample(
            t_s=self.t_s,
            position_m=self.crawler.position_m,
            hops=len(self.chain.hops),
            min_margin_db=self.chain.min_margin_db,
            goodput_bps=self.chain.video_goodput_bps,
            latency_ms=self.video.glass_to_glass_ms,
            control_rtt_ms=self.chain.control_rtt_ms,
            video_tier=self.video.tier.name,
            battery_pct=self.crawler.battery_pct,
            control_ok=self.chain.control_ok,
        ))

    # -- commands ---------------------------------------------------------

    def command(self, name: str, payload: Optional[dict] = None) -> dict:
        payload = payload or {}
        c = self.crawler
        if name == "drive":
            self.auto = False
            if self.phase in ("idle", "complete"):
                self.phase = "outbound"
            c.clear_estop()
            c.set_speed(float(payload.get("speed_mps", self.cruise_speed)))
            return {"ok": True}
        if name == "stop":
            self.auto = False
            c.set_speed(0.0)
            self.log("info", "Operator stop")
            return {"ok": True}
        if name == "estop":
            c.trigger_estop()
            self.auto = False
            rtt = self.chain.control_rtt_ms
            self.log("alarm",
                     f"EMERGENCY STOP acknowledged in {rtt:.0f} ms over "
                     f"{len(self.chain.hops)} hops")
            return {"ok": True, "rtt_ms": rtt, "hops": len(self.chain.hops)}
        if name == "clear_estop":
            c.clear_estop()
            self.log("info", "Emergency stop cleared")
            return {"ok": True}
        if name == "deploy":
            node = self.deploy_node("operator")
            return {"ok": node is not None}
        if name == "auto":
            self.start()
            return {"ok": True}
        if name == "return":
            self.auto = True
            self.turn_around("operator requested return")
            return {"ok": True}
        if name == "lights":
            c.lights_on = bool(payload.get("on", not c.lights_on))
            return {"ok": True, "lights_on": c.lights_on}
        if name == "set_radio":
            return self._set_radio(payload)
        if name == "set_policy":
            key = str(payload.get("policy", "balanced"))
            if key not in self.POLICIES:
                return {"ok": False, "error": f"unknown policy {key!r}"}
            self.deploy_policy = key
            self._deploy_margin_db = self._policy_margin()
            label, _, why = self.POLICIES[key]
            self.log("info", f"Relay policy: {label} - {why} "
                             f"(drop at {self._deploy_margin_db:.0f} dB margin)")
            return {"ok": True, "policy": key,
                    "deploy_margin_db": self._deploy_margin_db}
        if name == "set_magazine":
            n = max(0, min(48, int(payload.get("count", 16))))
            used = self.node_counter - sum(1 for x in self.nodes if x.recovered)
            self.crawler.config.node_magazine = n
            self.crawler.nodes_remaining = max(0, n - len([x for x in self.nodes if x.alive]))
            self.log("info", f"Relay magazine set to {n} nodes")
            return {"ok": True, "magazine": n}
        if name == "set_speed":
            self.cruise_speed = max(0.02, min(c.config.max_speed_mps,
                                              float(payload.get("mps", 0.24))))
            if self.auto and self.phase == "outbound":
                c.set_speed(self.cruise_speed)
            elif self.auto and self.phase == "returning":
                c.set_speed(-self.cruise_speed)
            return {"ok": True, "cruise_speed": self.cruise_speed}
        return {"ok": False, "error": f"unknown command {name!r}"}

    def _set_radio(self, payload: dict) -> dict:
        r = self.radio
        changed = []
        if "frequency_mhz" in payload:
            r.frequency_hz = float(payload["frequency_mhz"]) * 1e6
            changed.append(f"carrier {r.frequency_hz / 1e6:.0f} MHz")
        if "bandwidth_mhz" in payload:
            r.bandwidth_hz = float(payload["bandwidth_mhz"]) * 1e6
            changed.append(f"bandwidth {r.bandwidth_hz / 1e6:.0f} MHz")
        if "tx_power_dbm" in payload:
            r.tx_power_dbm = float(payload["tx_power_dbm"])
            r.control_tx_power_dbm = r.tx_power_dbm
            changed.append(f"Tx {r.tx_power_dbm:.0f} dBm")
        if "channel_count" in payload:
            r.channel_count = int(payload["channel_count"])
            changed.append(f"{r.channel_count} channels")
        if "radios_per_node" in payload:
            r.radios_per_node = int(payload["radios_per_node"])
            changed.append(f"{r.radios_per_node} radios/node")
        if "fade_margin_db" in payload:
            r.fade_margin_db = float(payload["fade_margin_db"])
            changed.append(f"fade margin {r.fade_margin_db:.0f} dB")
        self._deploy_margin_db = self._policy_margin()
        self.profile._loss_cache.clear()
        self._refresh_link(0.05)
        if changed:
            self.log("radio", "Radio reconfigured: " + ", ".join(changed))
        fc = self.profile.cutoff_at(r.frequency_hz, self.crawler.position_m)
        if r.frequency_hz <= fc:
            self.log("alarm",
                     f"{r.frequency_hz / 1e6:.0f} MHz is below the {fc / 1e6:.0f} MHz "
                     "cutoff of this pipe: nothing propagates at all")
        return {"ok": True, "radio": r.to_dict()}

    def reset(self, scenario_key: Optional[str] = None) -> None:
        key = scenario_key or self.scenario.key
        radio = self.radio if scenario_key is None else None
        self.__init__(key, radio=radio, crawler_config=self.crawler.config,
                      seed=self.rng.randint(1, 10_000))

    @property
    def finished(self) -> bool:
        return self.phase in ("complete", "aborted")

    # -- reporting --------------------------------------------------------

    def context(self) -> dict:
        """What the pipe looks like where the crawler is standing."""
        return pipe_context(self.profile, self.radio, self.crawler.position_m)

    def snapshot(self) -> dict:
        return {
            "t_s": round(self.t_s, 2),
            "phase": self.phase,
            "auto": self.auto,
            "cruise_speed": round(self.cruise_speed, 3),
            "scenario": {
                "key": self.scenario.key,
                "title": self.scenario.title,
                "difficulty": self.scenario.difficulty,
                "why": self.scenario.why,
            },
            "crawler": self.crawler.to_dict(),
            "nodes": [n.to_dict() for n in self.nodes if n.alive],
            "spent_nodes": sum(1 for n in self.nodes if not n.alive),
            "chain": self.chain.to_dict(),
            "video": self.video.to_dict(),
            "context": self.context(),
            "radio": self.radio.to_dict(),
            "policy": {
                "key": self.deploy_policy,
                "label": self.POLICIES[self.deploy_policy][0],
                "why": self.POLICIES[self.deploy_policy][2],
                "deploy_margin_db": round(self._deploy_margin_db, 1),
                "magazine": self.crawler.config.node_magazine,
            },
            "events": list(self.events)[-14:],
            "samples": len(self.samples),
        }

    def history(self) -> List[dict]:
        return [s.to_dict() for s in self.samples]
