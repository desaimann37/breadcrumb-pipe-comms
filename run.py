#!/usr/bin/env python3
"""Launch the BREADCRUMB operator console.

Simulation (no hardware):

    python run.py                          # http://127.0.0.1:8765
    python run.py --scenario interceptor_36

Live hardware - the same console, driven by a real crawler:

    python run.py --live --serial COM5
    python run.py --live --serial COM5 --camera http://192.168.4.1:81/stream
    python run.py --live --udp                      # base node over Wi-Fi
    python run.py --live --emulate                  # rehearse with no robot
    python run.py --live --replay out/field1.log    # review a past run

In live mode the crawler's position comes from wheel odometry, every hop's
SNR comes from measured RSSI, and the link-up indicator is telemetry
freshness rather than a model. The pipe context stays modelled on purpose, so
the console shows measured against predicted side by side.
"""

import argparse
import sys
import threading
import webbrowser

import server.app as app
from server.app import RUNNER, serve


def build_live_runner(args):
    """Create a hardware-backed runner from the chosen transport."""
    bridge = app.load_bridge()
    transport = bridge.make_transport(args)
    runner = app.LiveRunner(
        transport,
        scenario_key=args.scenario or "water_main_24",
        camera_url=args.camera,
        noise_floor_dbm=args.noise_floor,
        log_path=args.log,
    )
    app.set_runner(runner)
    return runner


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Untethered pipe inspection comms console",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scenario", default=None,
                    help="pipe model to show context against")
    ap.add_argument("--no-browser", action="store_true")

    live = ap.add_argument_group("live hardware")
    live.add_argument("--live", action="store_true",
                      help="drive a real node chain instead of the simulator")
    live.add_argument("--serial", help="base node serial port, e.g. COM5")
    live.add_argument("--baud", type=int, default=921600)
    live.add_argument("--udp", action="store_true",
                      help="base node over Wi-Fi instead of USB")
    live.add_argument("--udp-port", type=int, default=9870)
    live.add_argument("--udp-peer", default=None)
    live.add_argument("--udp-peer-port", type=int, default=9871)
    live.add_argument("--replay", help="replay a captured log")
    live.add_argument("--replay-speed", type=float, default=1.0)
    live.add_argument("--emulate", action="store_true",
                      help="no hardware: simulator pretending to be the chain")
    live.add_argument("--emulate-speed", type=float, default=4.0,
                      help="emulated crawler speed multiplier (default 4x)")
    live.add_argument("--camera", default=None,
                      help="MJPEG stream URL from the camera node")
    live.add_argument("--noise-floor", type=float, default=None,
                      help="measured receiver noise floor in dBm "
                           "(transmitters off, in the pipe)")
    live.add_argument("--log", default=None,
                      help="append every received line to this file")
    args = ap.parse_args()

    if args.live:
        runner = build_live_runner(args)
        mode = f"LIVE  [{runner.mission.transport_name}]"
    else:
        runner = RUNNER
        if args.scenario:
            runner.command("reset", {"scenario": args.scenario})
        mode = "SIMULATION"

    httpd = serve(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 70)
    print("  BREADCRUMB - untethered comms for long-distance pipe inspection")
    print("=" * 70)
    print(f"  Mode      : {mode}")
    print(f"  Dashboard : {url}")
    print(f"  Pipe model: {runner.mission.scenario.title}")
    if args.live:
        print(f"  Camera    : {args.camera or 'simulated (pass --camera URL for real video)'}")
        print("  Operator heartbeat is running: close this and the crawler stops.")
    print("  Press Ctrl+C to stop.")
    print("=" * 70)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        runner.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
