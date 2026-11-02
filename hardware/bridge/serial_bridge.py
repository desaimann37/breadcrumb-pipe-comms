#!/usr/bin/env python3
"""Capture RSSI from the ESP32 nodes and fit it back into the model.

The point of the field test is not to prove the simulator right. It is to
replace one guessed number - the attenuation of this particular pipe - with a
measured one, so every later prediction is anchored to reality.

    # log a drag test: walk the crawler out, tagging distance as you go
    python serial_bridge.py capture --port COM5 --out ../../out/field_run1.csv

    # fit dB/m from the log and print what to put in the scenario
    python serial_bridge.py calibrate --csv ../../out/field_run1.csv --tx-dbm 19.5

    # no hardware? prove the pipeline works end to end
    python serial_bridge.py capture --simulate --samples 300 \
        --out ../../out/field_sim.csv

The capture format is one row per received beacon:

    t_ms, frame_type, src, hop, rssi_dbm, seq, distance_m, note

``distance_m`` is filled in by the operator: type a number into the terminal
whenever the tape measure passes a mark and every row after it carries that
distance. That is deliberately low-tech, because at a manhole in the rain it
is the only thing that reliably works.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import sys
import threading
import time
from typing import List, Optional, Tuple

FIELDS = ["t_ms", "frame_type", "src", "hop", "rssi_dbm", "seq", "distance_m", "note"]


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------


class DistanceTagger:
    """Lets the operator type the current tape-measure reading while logging."""

    def __init__(self, start: float = 0.0):
        self.distance_m = start
        self.note = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = sys.stdin.readline()
            except (ValueError, OSError):
                return
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                self.distance_m = float(line)
                self.note = ""
                print(f"  [distance now {self.distance_m:.1f} m]")
            except ValueError:
                self.note = line
                print(f"  [note: {line}]")


def parse_line(line: str) -> Optional[dict]:
    """Parse one 'RX,...' line from the base node."""
    parts = line.strip().split(",")
    if not parts or parts[0] != "RX" or len(parts) < 6:
        return None
    try:
        return {
            "t_ms": int(parts[1]),
            "frame_type": int(parts[2]),
            "src": int(parts[3]),
            "hop": int(parts[4]),
            "rssi_dbm": int(parts[5]),
            "seq": int(parts[6]) if len(parts) > 6 else 0,
        }
    except ValueError:
        return None


def simulated_lines(samples: int, db_per_m: float = 0.05,
                    tx_dbm: float = 19.5) -> List[str]:
    """Synthetic log so the capture/calibrate path can be tested dry.

    Models a node dragged out to 200 m with a fixed attenuation plus fading,
    which is exactly the shape of the real measurement.
    """
    rng = random.Random(4)
    out = []
    for i in range(samples):
        d = 1.0 + (200.0 * i / max(samples - 1, 1))
        spread = 20 * math.log10(max(d, 1.0)) * 0.0 + 48.0   # capture loss
        rssi = tx_dbm - spread - db_per_m * d + rng.gauss(0, 2.5)
        out.append(f"RX,{int(i * 100)},1,99,0,{int(round(rssi))},{i}")
    return out


def capture(port: Optional[str], out_path: str, baud: int = 921600,
            simulate: bool = False, samples: int = 300,
            start_distance: float = 0.0) -> str:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tagger = DistanceTagger(start_distance)

    if simulate:
        source = iter(simulated_lines(samples))
        step = 200.0 / max(samples - 1, 1)
    else:
        try:
            import serial  # type: ignore
        except ImportError:
            print("pyserial is not installed. Either:\n"
                  "  pip install pyserial\n"
                  "or run with --simulate to exercise the pipeline dry.",
                  file=sys.stderr)
            return ""
        ser = serial.Serial(port, baud, timeout=1)
        print(f"Listening on {port} at {baud} baud.")
        print("Type a number + Enter to set the current distance in metres, "
              "or any text to tag a note. Ctrl+C to stop.")
        tagger.start()
        source = (ser.readline().decode("utf-8", "replace") for _ in iter(int, 1))
        step = 0.0

    rows = []
    try:
        for i, line in enumerate(source):
            rec = parse_line(line)
            if rec is None:
                if line.strip() and not simulate:
                    print(f"  {line.strip()}")
                continue
            if simulate:
                tagger.distance_m = 1.0 + i * step
            rec["distance_m"] = round(tagger.distance_m, 2)
            rec["note"] = tagger.note
            rows.append(rec)
            if not simulate and len(rows) % 20 == 0:
                print(f"  {len(rows)} samples, last RSSI {rec['rssi_dbm']} dBm "
                      f"at {rec['distance_m']:.1f} m")
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        tagger.stop()

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} samples to {out_path}")
    return out_path


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


def linear_fit(xs: List[float], ys: List[float]) -> Tuple[float, float, float]:
    """Least-squares fit y = a + b*x, returning (a, b, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return my, 0.0, 0.0
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return a, b, r2


def calibrate(csv_path: str, tx_dbm: float = 19.5, min_distance: float = 2.0) -> dict:
    """Fit attenuation per metre from a capture and report the fade statistics."""
    xs: List[float] = []
    ys: List[float] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                d = float(row["distance_m"])
                r = float(row["rssi_dbm"])
            except (ValueError, KeyError):
                continue
            if d >= min_distance:
                xs.append(d)
                ys.append(r)

    if len(xs) < 10:
        raise SystemExit(f"only {len(xs)} usable samples in {csv_path}")

    intercept, slope, r2 = linear_fit(xs, ys)
    db_per_m = -slope
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    mean_abs = sum(abs(r) for r in residuals) / len(residuals)
    var = sum(r * r for r in residuals) / len(residuals)
    sigma = math.sqrt(var)
    # 10th percentile of the fade: a practical fade margin to design to.
    ordered = sorted(residuals)
    p10 = ordered[int(0.10 * len(ordered))]

    result = {
        "samples": len(xs),
        "distance_span_m": (min(xs), max(xs)),
        "db_per_m": db_per_m,
        "db_per_km": db_per_m * 1000.0,
        "capture_loss_db": tx_dbm - intercept,
        "r_squared": r2,
        "fade_sigma_db": sigma,
        "fade_mean_abs_db": mean_abs,
        "suggested_fade_margin_db": abs(p10),
    }

    print(f"\nCalibration from {csv_path}")
    print(f"  samples            {result['samples']} between "
          f"{result['distance_span_m'][0]:.1f} and {result['distance_span_m'][1]:.1f} m")
    print(f"  attenuation        {db_per_m:.4f} dB/m  "
          f"({result['db_per_km']:.1f} dB/km)")
    print(f"  fit quality        r^2 = {r2:.3f}")
    print(f"  coupling + spread  {result['capture_loss_db']:.1f} dB at the near end")
    print(f"  fading             sigma {sigma:.1f} dB, "
          f"10th percentile {p10:.1f} dB")
    print("\nPut this in the scenario (pipecomm/scenarios.py):")
    print(f"    excess_loss_db_per_m = {db_per_m:.4f}   # measured, "
          f"{time.strftime('%Y-%m-%d')}")
    print(f"And set RadioConfig(fade_margin_db={abs(p10):.0f}) to match the "
          "fading you actually saw.\n")
    if r2 < 0.5:
        print("WARNING: poor fit. Either the distance tags are wrong or the "
              "pipe is not behaving like a uniform guide (bends, water, a "
              "lateral). Split the run into sections and fit each one.\n")
    return result


# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="log RSSI from the base node")
    c.add_argument("--port", help="serial port, e.g. COM5 or /dev/ttyUSB0")
    c.add_argument("--baud", type=int, default=921600)
    c.add_argument("--out", default="field_run.csv")
    c.add_argument("--simulate", action="store_true",
                   help="generate a synthetic log instead of reading hardware")
    c.add_argument("--samples", type=int, default=300)
    c.add_argument("--start-distance", type=float, default=0.0)

    f = sub.add_parser("calibrate", help="fit dB/m from a capture")
    f.add_argument("--csv", required=True)
    f.add_argument("--tx-dbm", type=float, default=19.5)
    f.add_argument("--min-distance", type=float, default=2.0)

    args = ap.parse_args(argv)
    if args.cmd == "capture":
        if not args.simulate and not args.port:
            ap.error("--port is required unless --simulate is given")
        path = capture(args.port, args.out, args.baud, args.simulate,
                       args.samples, args.start_distance)
        return 0 if path else 1
    calibrate(args.csv, args.tx_dbm, args.min_distance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
