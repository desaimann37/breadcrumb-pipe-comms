"""The crawler and the relay nodes it leaves behind.

The communication architecture only works if the robot carries it, so the
mechanical and energy budget is modelled alongside the radio: how many nodes
fit in the magazine, what they cost in battery, and how far the machine can get
before it has to turn around to recover them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CrawlerConfig:
    """Physical limits of the inspection crawler."""

    max_speed_mps: float = 0.35          # ~1.1 ft/s, typical CCTV crawler
    accel_mps2: float = 0.25
    # Cutting the tether also cuts shore power. A two-mile round trip at
    # 0.3 m/s is about six hours of driving, so the pack is the single
    # heaviest item on the crawler. See docs/BOM.md.
    battery_wh: float = 620.0
    drive_power_w: float = 38.0
    idle_power_w: float = 7.0
    radio_power_w: float = 5.5
    lights_power_w: float = 9.0
    node_magazine: int = 16
    node_battery_wh: float = 26.0
    node_idle_w: float = 1.4             # relay, listening and forwarding
    watchdog_ms: float = 600.0           # autonomous stop if control goes quiet
    reserve_fraction: float = 1.35       # return-energy multiplier before turning back

    def to_dict(self) -> dict:
        return {
            "max_speed_mps": self.max_speed_mps,
            "battery_wh": self.battery_wh,
            "node_magazine": self.node_magazine,
            "node_battery_wh": self.node_battery_wh,
            "watchdog_ms": self.watchdog_ms,
            "reserve_fraction": self.reserve_fraction,
        }


@dataclass
class RelayNode:
    """A dropped repeater: a puck with two radios and a battery."""

    index: int
    position_m: float
    deployed_at_s: float
    battery_wh: float
    note: str = ""
    recovered: bool = False

    @property
    def alive(self) -> bool:
        return self.battery_wh > 0.0 and not self.recovered

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "position_m": round(self.position_m, 1),
            "deployed_at_s": round(self.deployed_at_s, 1),
            "battery_wh": round(self.battery_wh, 1),
            "battery_pct": round(100.0 * self.battery_wh / max(self.battery_wh, 1e-9), 0),
            "note": self.note,
            "alive": self.alive,
        }


class Crawler:
    """Kinematics, energy and the onboard safety watchdog."""

    def __init__(self, config: Optional[CrawlerConfig] = None):
        self.config = config or CrawlerConfig()
        self.position_m = 0.0
        self.speed_mps = 0.0
        self.commanded_speed_mps = 0.0
        self.battery_wh = self.config.battery_wh
        self.distance_travelled_m = 0.0
        self.estop = False
        self.watchdog_tripped = False
        self.time_since_control_s = 0.0
        self.lights_on = True
        self.nodes_remaining = self.config.node_magazine

    # -- commands ---------------------------------------------------------

    def set_speed(self, mps: float) -> None:
        limit = self.config.max_speed_mps
        self.commanded_speed_mps = max(-limit, min(limit, mps))

    def trigger_estop(self) -> None:
        self.estop = True
        self.commanded_speed_mps = 0.0

    def clear_estop(self) -> None:
        self.estop = False
        self.watchdog_tripped = False
        self.time_since_control_s = 0.0

    # -- integration ------------------------------------------------------

    def step(self, dt: float, control_ok: bool, max_position_m: float) -> List[str]:
        """Advance by ``dt`` seconds. Returns any safety events raised."""
        events: List[str] = []
        cfg = self.config

        if control_ok:
            if self.time_since_control_s * 1000.0 >= cfg.watchdog_ms and self.watchdog_tripped:
                events.append("Control link restored; crawler re-armed")
                self.watchdog_tripped = False
            self.time_since_control_s = 0.0
        else:
            self.time_since_control_s += dt
            if (self.time_since_control_s * 1000.0 >= cfg.watchdog_ms
                    and not self.watchdog_tripped):
                self.watchdog_tripped = True
                events.append(
                    f"WATCHDOG: no control frame for {cfg.watchdog_ms:.0f} ms, "
                    "crawler stopped autonomously")

        target = 0.0 if (self.estop or self.watchdog_tripped) else self.commanded_speed_mps
        if self.battery_wh <= 0.0:
            target = 0.0

        dv = cfg.accel_mps2 * dt
        if target > self.speed_mps:
            self.speed_mps = min(self.speed_mps + dv, target)
        elif target < self.speed_mps:
            self.speed_mps = max(self.speed_mps - dv, target)

        moved = self.speed_mps * dt
        new_pos = self.position_m + moved
        if new_pos < 0.0:
            new_pos = 0.0
            self.speed_mps = 0.0
        if new_pos > max_position_m:
            new_pos = max_position_m
            self.speed_mps = 0.0
            events.append("Reached end of pipe run")
        self.distance_travelled_m += abs(new_pos - self.position_m)
        self.position_m = new_pos

        power = cfg.idle_power_w + cfg.radio_power_w
        if self.lights_on:
            power += cfg.lights_power_w
        if abs(self.speed_mps) > 1e-3:
            power += cfg.drive_power_w * (abs(self.speed_mps) / cfg.max_speed_mps)
        drained = power * dt / 3600.0
        if self.battery_wh > 0 and self.battery_wh - drained <= 0:
            events.append("Crawler battery exhausted")
        self.battery_wh = max(0.0, self.battery_wh - drained)
        return events

    # -- derived ----------------------------------------------------------

    @property
    def battery_pct(self) -> float:
        return 100.0 * self.battery_wh / self.config.battery_wh

    def return_energy_needed_wh(self, speed_mps: Optional[float] = None) -> float:
        """Energy to drive back to the access point at ``speed_mps``."""
        cfg = self.config
        v = speed_mps or cfg.max_speed_mps
        if v <= 0:
            return float("inf")
        seconds = self.position_m / v
        power = cfg.idle_power_w + cfg.radio_power_w + cfg.drive_power_w
        return power * seconds / 3600.0

    def can_return(self, speed_mps: Optional[float] = None) -> bool:
        return self.battery_wh >= self.return_energy_needed_wh(speed_mps)

    def must_turn_back(self, speed_mps: Optional[float] = None) -> bool:
        """True once only the reserve is left: time to head for the shaft."""
        needed = self.return_energy_needed_wh(speed_mps)
        return self.battery_wh <= needed * self.config.reserve_fraction

    def to_dict(self) -> dict:
        return {
            "position_m": round(self.position_m, 2),
            "speed_mps": round(self.speed_mps, 3),
            "commanded_speed_mps": round(self.commanded_speed_mps, 3),
            "battery_wh": round(self.battery_wh, 1),
            "battery_pct": round(self.battery_pct, 1),
            "distance_travelled_m": round(self.distance_travelled_m, 1),
            "estop": self.estop,
            "watchdog_tripped": self.watchdog_tripped,
            "nodes_remaining": self.nodes_remaining,
            "lights_on": self.lights_on,
            "can_return": self.can_return(),
            "return_energy_wh": round(self.return_energy_needed_wh(), 1),
        }
