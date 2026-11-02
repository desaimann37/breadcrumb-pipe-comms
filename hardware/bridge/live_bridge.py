#!/usr/bin/env python3
"""Transports between the operator console and a real (or fake) node chain.

Four ways to reach the hardware, all speaking the same line protocol:

    SerialTransport    USB cable to the base node. The normal case.
    UdpTransport       base node on Wi-Fi at the manhole, no cable.
    ReplayTransport    a previously captured log, for post-mission review.
    EmulatedTransport  no hardware at all: the simulator pretending to be a
                       node chain, so the whole ground station - parsing,
                       dashboard, commands, e-stop - can be tested and
                       rehearsed before anyone drives to a site.

Standalone use for bring-up, before involving the dashboard:

    python live_bridge.py monitor --serial COM5
    python live_bridge.py monitor --emulate
    python live_bridge.py send --serial COM5 --line "C,150"

Wire protocol (base node to laptop):

    BOOT,role=<r>,id=<n>,ch=<c>,txpwr=<dBm>
    TLM,<t_ms>,<src>,<hop>,<rssi>,<seq>,<odo_mm>,<batt_mv>,<speed_mms>,<flags>
    HOP,<t_ms>,<relay_id>,<rssi_up>,<hop_index>
    RX,<t_ms>,<type>,<src>,<hop>,<rssi>,<seq>
    EVT,<t_ms>,<text>

Laptop to base node:

    C,<speed_mm_s>   drive        E   emergency stop
    L,<0|1>          lights       R   re-arm after a stop
    P                operator heartbeat (10 Hz, fail-safe - see below)

The heartbeat is deliberately required: the base node only transmits keepalive
frames while the laptop is alive, so unplugging the USB cable stops the robot
rather than leaving it driving.
"""

from __future__ import annotations

import argparse
import os
import queue
import socket
import sys
import threading
import time
from typing import Iterator, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", ".."))

HEARTBEAT_HZ = 10.0


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


class Transport:
    """A bidirectional line channel to the node chain."""

    name = "none"

    def lines(self) -> Iterator[str]:
        raise NotImplementedError

    def write(self, line: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SerialTransport(Transport):
    """USB serial to the base node."""

    def __init__(self, port: str, baud: int = 921600):
        try:
            import serial  # type: ignore
        except ImportError:
            raise SystemExit(
                "pyserial is not installed.\n"
                "  pip install pyserial\n"
                "or run with --emulate to exercise the whole path without "
                "hardware.")
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.name = f"serial {port} @ {baud}"

    def lines(self) -> Iterator[str]:
        buf = ""
        while True:
            try:
                chunk = self.ser.read(256).decode("utf-8", "replace")
            except Exception:
                return
            if not chunk:
                yield ""          # keeps the caller's loop ticking
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                yield line.strip()

    def write(self, line: str) -> None:
        try:
            self.ser.write((line + "\n").encode("ascii", "ignore"))
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class UdpTransport(Transport):
    """Base node reachable over Wi-Fi instead of a cable.

    Useful when the base sits on a tripod at the manhole and the operator is
    in the truck.  Same lines, carried in datagrams.
    """

    def __init__(self, listen_port: int = 9870, peer: Optional[str] = None,
                 peer_port: int = 9871):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", listen_port))
        self.sock.settimeout(0.2)
        self.peer = (peer, peer_port) if peer else None
        self.name = f"udp :{listen_port} -> {peer or 'auto'}:{peer_port}"

    def lines(self) -> Iterator[str]:
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                yield ""
                continue
            except OSError:
                return
            if self.peer is None:
                self.peer = (addr[0], self.peer[1] if self.peer else 9871)
                self.name = f"udp <- {addr[0]}"
            for line in data.decode("utf-8", "replace").splitlines():
                yield line.strip()

    def write(self, line: str) -> None:
        if self.peer and self.peer[0]:
            try:
                self.sock.sendto((line + "\n").encode(), self.peer)
            except OSError:
                pass

    def close(self) -> None:
        self.sock.close()


class ReplayTransport(Transport):
    """Replay a captured log, at the rate it was recorded."""

    def __init__(self, path: str, speed: float = 1.0):
        with open(path, encoding="utf-8") as fh:
            self.raw = [ln.strip() for ln in fh if ln.strip()]
        self.speed = max(speed, 0.01)
        self.name = f"replay {os.path.basename(path)} @ {speed}x"

    def lines(self) -> Iterator[str]:
        last_ms: Optional[int] = None
        for line in self.raw:
            parts = line.split(",")
            if len(parts) > 1 and parts[0] in ("TLM", "HOP", "RX", "EVT"):
                try:
                    t_ms = int(parts[1])
                    if last_ms is not None and t_ms > last_ms:
                        time.sleep(min((t_ms - last_ms) / 1000.0 / self.speed, 2.0))
                    last_ms = t_ms
                except ValueError:
                    pass
            yield line
        while True:
            time.sleep(0.2)
            yield ""

    def write(self, line: str) -> None:
        pass          # a replay cannot be driven


class EmulatedTransport(Transport):
    """The simulator wearing a node chain's clothes.

    Runs a real :class:`~pipecomm.mission.Mission` and emits the exact lines
    the ESP32 base node would, including per-hop RSSI derived from the
    modelled path loss.  Commands are fed back into the mission, so the
    dashboard really is driving something.

    This exists so the ground station can be rehearsed - including the
    emergency stop - without a robot on the bench.  It is also how the live
    code path is tested in CI.
    """

    def __init__(self, scenario: str = "water_main_24", auto: bool = True,
                 time_scale: float = 4.0):
        from pipecomm.mission import Mission
        self.mission = Mission(scenario)
        self.mission.command("set_magazine", {"count": 12})
        if auto:
            self.mission.start()
        self.time_scale = time_scale
        self.name = f"emulated ({scenario})"
        self._seq = 0
        self._last_event_id = 0
        self._reported_drops = 0
        self._cmds: "queue.Queue[str]" = queue.Queue()
        self._estop = False

    def lines(self) -> Iterator[str]:
        yield "BOOT,role=0,id=0,ch=6,txpwr=19.50"
        last = time.monotonic()
        last_emit = 0.0
        while True:
            now = time.monotonic()
            dt = min(now - last, 0.5)
            last = now

            while not self._cmds.empty():
                self._apply(self._cmds.get_nowait())

            steps = max(1, int(dt * self.time_scale / 0.2))
            for _ in range(steps):
                self.mission.step(dt * self.time_scale / steps)

            if now - last_emit < 1.0 / 10.0:
                time.sleep(0.01)
                yield ""
                continue
            last_emit = now

            m = self.mission
            self._seq += 1
            t_ms = int(m.t_s * 1000)
            chain = m.chain
            hops = chain.hops
            flags = 0
            if m.crawler.estop:
                flags |= 1
            if m.crawler.watchdog_tripped:
                flags |= 2
            if m.crawler.lights_on:
                flags |= 4

            rssi_last = int(round(hops[-1].rx_dbm)) if hops else -110
            batt_mv = int(33000 + 17400 * (m.crawler.battery_pct / 100.0))
            yield (f"TLM,{t_ms},99,{len(hops)},{rssi_last},{self._seq},"
                   f"{int(m.crawler.position_m * 1000)},{batt_mv},"
                   f"{int(m.crawler.speed_mps * 1000)},{flags}")

            # The crawler reports every puck its magazine released, with the
            # odometry reading at the moment the actuator fired.
            while self._reported_drops < len(m.nodes):
                node = m.nodes[self._reported_drops]
                self._reported_drops += 1
                yield f"DROP,{t_ms},{node.index},{int(node.position_m * 1000)}"

            # Each relay reports the RSSI it heard from upstream.
            for i, hop in enumerate(hops[:-1]):
                yield (f"HOP,{t_ms},{i + 1},{int(round(hop.rx_dbm))},{i + 1}")

            for ev in m.events:
                if ev["id"] > self._last_event_id:
                    self._last_event_id = ev["id"]
                    if ev["level"] in ("alarm", "node", "warn"):
                        yield f"EVT,{t_ms},{ev['text']}"

            yield ""

    def write(self, line: str) -> None:
        self._cmds.put(line)

    def _apply(self, line: str) -> None:
        m = self.mission
        if not line:
            return
        head = line[0]
        if head == "E":
            m.command("estop")
        elif head == "R":
            m.command("clear_estop")
        elif head == "C":
            try:
                mms = int(line.split(",")[1])
            except (IndexError, ValueError):
                return
            m.auto = False
            m.command("drive", {"speed_mps": mms / 1000.0})
        elif head == "L":
            m.command("lights", {"on": line.endswith("1")})
        elif head == "D":
            m.command("deploy")


def make_transport(args) -> Transport:
    if getattr(args, "emulate", False):
        return EmulatedTransport(getattr(args, "scenario", None) or "water_main_24",
                                 time_scale=getattr(args, "emulate_speed", 4.0))
    if getattr(args, "replay", None):
        return ReplayTransport(args.replay, getattr(args, "replay_speed", 1.0))
    if getattr(args, "udp", False):
        return UdpTransport(args.udp_port, args.udp_peer, args.udp_peer_port)
    if getattr(args, "serial", None):
        return SerialTransport(args.serial, getattr(args, "baud", 921600))
    raise SystemExit("choose one of --serial PORT, --udp, --replay FILE, --emulate")


# --------------------------------------------------------------------------
# Reader thread, shared by the server and the standalone tools
# --------------------------------------------------------------------------


class BridgeReader:
    """Pumps transport lines into a sink and heartbeats the operator link."""

    def __init__(self, transport: Transport, on_line, log_path: Optional[str] = None):
        self.transport = transport
        self.on_line = on_line
        self.log_file = open(log_path, "a", encoding="utf-8") if log_path else None
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._beater = threading.Thread(target=self._beat_loop, daemon=True)

    def start(self) -> None:
        self._reader.start()
        self._beater.start()

    def stop(self) -> None:
        self._stop.set()
        self.transport.close()
        if self.log_file:
            self.log_file.close()

    def _read_loop(self) -> None:
        for line in self.transport.lines():
            if self._stop.is_set():
                return
            if not line:
                continue
            if self.log_file:
                self.log_file.write(line + "\n")
                self.log_file.flush()
            try:
                self.on_line(line)
            except Exception as exc:         # never let one bad line kill it
                print(f"[bridge] ingest error: {exc}", file=sys.stderr)

    def _beat_loop(self) -> None:
        """Operator heartbeat. If this stops, the robot stops. By design."""
        while not self._stop.is_set():
            self.transport.write("P")
            time.sleep(1.0 / HEARTBEAT_HZ)


# --------------------------------------------------------------------------
# Standalone tools
# --------------------------------------------------------------------------


def monitor(args) -> int:
    transport = make_transport(args)
    print(f"[bridge] {transport.name}")
    print("[bridge] Ctrl+C to stop\n")
    counts = {}
    reader = BridgeReader(transport, lambda ln: _print_line(ln, counts, args.raw),
                          args.log)
    reader.start()
    try:
        while True:
            time.sleep(5)
            if not args.raw:
                summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"[bridge] {summary or 'no traffic'}")
    except KeyboardInterrupt:
        print("\n[bridge] stopped")
    finally:
        reader.stop()
    return 0


def _print_line(line: str, counts: dict, raw: bool) -> None:
    kind = line.split(",", 1)[0]
    counts[kind] = counts.get(kind, 0) + 1
    if raw:
        print(line)
    elif kind in ("BOOT", "EVT"):
        print(f"  {line}")
    elif kind == "TLM" and counts[kind] % 10 == 1:
        p = line.split(",")
        if len(p) >= 10:
            print(f"  pos {int(p[6]) / 1000:7.2f} m   rssi {p[4]:>4} dBm   "
                  f"hops {p[3]}   batt {int(p[7]) / 1000:5.1f} V   "
                  f"speed {int(p[8]) / 1000:5.2f} m/s   flags {p[9]}")


def send(args) -> int:
    transport = make_transport(args)
    transport.write(args.line)
    print(f"sent: {args.line}")
    time.sleep(0.3)
    transport.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_transport_args(p):
        p.add_argument("--serial", help="serial port, e.g. COM5 or /dev/ttyUSB0")
        p.add_argument("--baud", type=int, default=921600)
        p.add_argument("--udp", action="store_true")
        p.add_argument("--udp-port", type=int, default=9870)
        p.add_argument("--udp-peer", default=None)
        p.add_argument("--udp-peer-port", type=int, default=9871)
        p.add_argument("--replay", help="captured log to replay")
        p.add_argument("--replay-speed", type=float, default=1.0)
        p.add_argument("--emulate", action="store_true",
                       help="no hardware: simulator pretending to be the chain")
        p.add_argument("--emulate-speed", type=float, default=4.0,
                       help="how fast the emulated crawler drives, x real time")
        p.add_argument("--scenario", default="water_main_24")

    m = sub.add_parser("monitor", help="watch the chain, for bring-up")
    add_transport_args(m)
    m.add_argument("--raw", action="store_true", help="print every line")
    m.add_argument("--log", default=None, help="append raw lines to this file")

    s = sub.add_parser("send", help="send one line to the base node")
    add_transport_args(s)
    s.add_argument("--line", required=True, help='e.g. "C,150" or "E"')

    args = ap.parse_args(argv)
    return monitor(args) if args.cmd == "monitor" else send(args)


if __name__ == "__main__":
    sys.exit(main())
