"""Standard-library HTTP server that drives the simulation and the dashboard.

No third-party packages on purpose: a demo that needs ``pip install`` on a
conference wifi is a demo that does not happen.  Telemetry goes out over
server-sent events; commands come back as small JSON posts.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from pipecomm import scenarios
from pipecomm.live import LiveMission
from pipecomm.mission import Mission, plan_nodes
from pipecomm.video import ladder_dict

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

TICK_S = 0.05
MAX_SUBSTEP_S = 0.25


class SimRunner:
    """Owns the mission and advances it on a background thread."""

    def __init__(self, scenario_key: str = scenarios.DEFAULT_SCENARIO):
        self.lock = threading.RLock()
        self.mission = Mission(scenario_key)
        self.time_scale = 1.0
        self.paused = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            wall_dt = min(now - last, 0.5)
            last = now
            if not self.paused:
                sim_dt = wall_dt * self.time_scale
                substeps = max(1, int(sim_dt / MAX_SUBSTEP_S) + 1)
                step = sim_dt / substeps
                with self.lock:
                    for _ in range(substeps):
                        self.mission.step(step)
            time.sleep(TICK_S)

    # -- api --------------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            snap = self.mission.snapshot()
        snap["time_scale"] = self.time_scale
        snap["paused"] = self.paused
        return snap

    def command(self, name: str, payload: dict) -> dict:
        if name == "time_scale":
            self.time_scale = max(0.25, min(240.0, float(payload.get("value", 1.0))))
            return {"ok": True, "time_scale": self.time_scale}
        if name == "pause":
            self.paused = bool(payload.get("on", not self.paused))
            return {"ok": True, "paused": self.paused}
        if name == "reset":
            key = payload.get("scenario")
            with self.lock:
                old_radio = self.mission.radio
                policy = self.mission.deploy_policy
                magazine = self.mission.crawler.config.node_magazine
                self.mission = Mission(key or self.mission.scenario.key)
                if key is None:
                    self.mission.radio = old_radio
                self.mission.command("set_policy", {"policy": policy})
                self.mission.command("set_magazine", {"count": magazine})
                self.mission._refresh_link(0.05)
            return {"ok": True, "scenario": self.mission.scenario.key}
        with self.lock:
            return self.mission.command(name, payload)

    def pipe_dict(self) -> dict:
        with self.lock:
            return self.mission.profile.to_dict(self.mission.radio.frequency_hz)

    def history(self) -> list:
        with self.lock:
            return self.mission.history()

    def plan(self, margin_db: Optional[float] = None) -> dict:
        with self.lock:
            profile = self.mission.profile
            radio = self.mission.radio
            margin = margin_db if margin_db is not None else self.mission._deploy_margin_db
        t0 = time.perf_counter()
        positions = plan_nodes(profile, radio, margin, step_m=10.0)
        return {
            "margin_db": margin,
            "positions": [round(p, 1) for p in positions],
            "node_count": len(positions),
            "hops": len(positions) + 1,
            "length_m": profile.length_m,
            "mean_hop_m": round(profile.length_m / (len(positions) + 1), 1),
            "compute_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


class LiveRunner:
    """Same interface as :class:`SimRunner`, backed by real hardware.

    The dashboard cannot tell the difference, which is the point: one console
    for the simulator and for the robot, so a field test is a like-for-like
    comparison instead of two different tools disagreeing.
    """

    def __init__(self, transport, scenario_key: str = scenarios.DEFAULT_SCENARIO,
                 camera_url: Optional[str] = None,
                 noise_floor_dbm: Optional[float] = None,
                 log_path: Optional[str] = None):
        bridge = load_bridge()
        self.lock = threading.RLock()
        self.transport = transport
        self.mission = LiveMission(scenario_key, camera_url=camera_url,
                                   noise_floor_dbm=noise_floor_dbm,
                                   send=self._write)
        self.mission.transport_name = getattr(transport, "name", "unknown")
        self.reader = bridge.BridgeReader(transport, self._on_line, log_path)
        self.time_scale = 1.0
        self.paused = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _write(self, line: str) -> None:
        self.transport.write(line)

    def _on_line(self, line: str) -> None:
        with self.lock:
            self.mission.ingest(line)

    def start(self) -> None:
        self.reader.start()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.reader.stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                self.mission.step(TICK_S)
            time.sleep(TICK_S)

    def snapshot(self) -> dict:
        with self.lock:
            return self.mission.snapshot()

    def command(self, name: str, payload: dict) -> dict:
        if name in ("time_scale", "pause"):
            return {"ok": False, "error": "live hardware runs in real time"}
        with self.lock:
            return self.mission.command(name, payload)

    def pipe_dict(self) -> dict:
        with self.lock:
            return self.mission.profile.to_dict(self.mission.radio.frequency_hz)

    def history(self) -> list:
        with self.lock:
            return self.mission.history()

    def plan(self, margin_db: Optional[float] = None) -> dict:
        with self.lock:
            profile, radio = self.mission.profile, self.mission.radio
        t0 = time.perf_counter()
        positions = plan_nodes(profile, radio, margin_db, step_m=10.0)
        return {
            "margin_db": margin_db if margin_db is not None else radio.fade_margin_db,
            "positions": [round(p, 1) for p in positions],
            "node_count": len(positions),
            "hops": len(positions) + 1,
            "length_m": profile.length_m,
            "mean_hop_m": round(profile.length_m / (len(positions) + 1), 1),
            "compute_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


def load_bridge():
    """Import hardware/bridge/live_bridge.py without making it a package."""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "hardware", "bridge", "live_bridge.py")
    spec = importlib.util.spec_from_file_location("live_bridge", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = SimRunner()


def set_runner(runner) -> None:
    """Swap the engine the HTTP handlers talk to (simulated or live)."""
    global RUNNER
    RUNNER = runner


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pipecomm/1.0"

    def log_message(self, fmt, *args):  # quieter console
        pass

    # -- helpers ----------------------------------------------------------

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            return self._send_file(os.path.join(WEB_DIR, "index.html"))
        if route == "/api/state":
            return self._send_json(RUNNER.snapshot())
        if route == "/api/pipe":
            return self._send_json(RUNNER.pipe_dict())
        if route == "/api/scenarios":
            return self._send_json({"scenarios": scenarios.list_scenarios()})
        if route == "/api/ladder":
            return self._send_json({"ladder": ladder_dict()})
        if route == "/api/history":
            return self._send_json({"history": RUNNER.history()})
        if route == "/api/plan":
            return self._send_json(RUNNER.plan())
        if route == "/api/stream":
            return self._stream()
        if route.startswith("/web/"):
            return self._send_file(os.path.join(WEB_DIR, route[5:]))
        safe = route.lstrip("/").replace("..", "")
        candidate = os.path.join(WEB_DIR, safe)
        if os.path.isfile(candidate):
            return self._send_file(candidate)
        self._send_json({"error": "not found", "path": route}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._send_json({"ok": False, "error": "bad json"}, 400)
        if route == "/api/cmd":
            name = payload.pop("cmd", "")
            t0 = time.perf_counter()
            result = RUNNER.command(name, payload)
            result["server_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            return self._send_json(result)
        self._send_json({"error": "not found"}, 404)

    def _stream(self) -> None:
        """Server-sent events: one telemetry frame every 100 ms."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(RUNNER.snapshot())
                self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return


def serve(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    RUNNER.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
