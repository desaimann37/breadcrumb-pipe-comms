"""Checks on the hardware-in-the-loop path.

The contract that matters is that ``LiveMission`` and ``Mission`` produce the
same snapshot shape, so the operator console cannot tell which one it is
talking to. If that ever drifts, the dashboard silently loses a panel during a
field test, which is the worst possible time to find out.
"""

import time
import unittest

from pipecomm.live import FLAG_ESTOP, FLAG_LIGHTS, LiveMission
from pipecomm.mission import Mission


def telem(t_ms=0, src=99, hop=0, rssi=-50, seq=1, odo_mm=0,
          batt_mv=48000, speed_mms=0, flags=0):
    return (f"TLM,{t_ms},{src},{hop},{rssi},{seq},{odo_mm},"
            f"{batt_mv},{speed_mms},{flags}")


class Recorder:
    """Captures the wire lines a command would send."""

    def __init__(self):
        self.sent = []

    def __call__(self, line):
        self.sent.append(line)


class TestSnapshotContract(unittest.TestCase):
    def test_live_snapshot_matches_simulated_shape(self):
        """The dashboard must not be able to tell the two apart."""
        sim = Mission("water_main_24")
        sim.step(0.1)
        live = LiveMission("water_main_24")
        live.ingest(telem())
        live.step(0.1)

        sim_keys = set(sim.snapshot())
        live_keys = set(live.snapshot())
        missing = sim_keys - live_keys
        self.assertFalse(missing, f"live snapshot is missing {sorted(missing)}")
        # Live adds the block that advertises itself, plus the two fields the
        # simulated runner bolts on at the server layer and that live mode
        # answers for itself (it always runs at 1x and cannot be paused).
        self.assertEqual(live_keys - sim_keys, {"live", "time_scale", "paused"})

    def test_nested_shapes_match(self):
        sim = Mission("water_main_24")
        sim.step(0.1)
        live = LiveMission("water_main_24")
        live.ingest(telem(rssi=-60))
        live.step(0.1)
        s, l = sim.snapshot(), live.snapshot()
        for block in ("crawler", "chain", "video", "context", "radio", "policy"):
            missing = set(s[block]) - set(l[block])
            self.assertFalse(missing, f"{block} is missing {sorted(missing)}")

    def test_hop_shape_matches(self):
        sim = Mission("water_main_24")
        sim.step(0.1)
        live = LiveMission("water_main_24")
        live.ingest(telem(rssi=-60))
        live.step(0.1)
        sim_hop = sim.snapshot()["chain"]["hops"][0]
        live_hop = live.snapshot()["chain"]["hops"][0]
        missing = set(sim_hop) - set(live_hop)
        self.assertFalse(missing, f"live hop is missing {sorted(missing)}")


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.m = LiveMission("water_main_24")

    def test_bad_lines_never_raise(self):
        for junk in ("", "   ", "TLM", "TLM,a,b,c", "\x00\xff garbage",
                     "HOP,1", "DROP", "RX,1,2"):
            self.m.ingest(junk)      # must not raise
        self.assertIsNotNone(self.m.snapshot())

    def test_odometry_is_zeroed_on_first_telemetry(self):
        self.m.ingest(telem(odo_mm=152_000))
        self.assertAlmostEqual(self.m.position_m, 0.0, places=3)
        self.m.ingest(telem(seq=2, odo_mm=162_000))
        self.assertAlmostEqual(self.m.position_m, 10.0, places=3)

    def test_speed_and_battery_are_decoded(self):
        self.m.ingest(telem(speed_mms=250, batt_mv=44000))
        self.assertAlmostEqual(self.m.speed_mps, 0.25, places=3)
        self.assertAlmostEqual(self.m.battery_v, 44.0, places=2)
        self.assertTrue(0 < self.m.battery_pct < 100)

    def test_control_ok_tracks_telemetry_freshness(self):
        """Link state is measured, not modelled."""
        self.assertFalse(self.m.control_ok)
        self.m.ingest(telem())
        self.assertTrue(self.m.control_ok)
        self.m.last_telem = time.monotonic() - 2.0
        self.assertFalse(self.m.control_ok)

    def test_flags_raise_events(self):
        self.m.ingest(telem())
        before = len(self.m.events)
        self.m.ingest(telem(seq=2, flags=FLAG_ESTOP))
        texts = [e["text"] for e in list(self.m.events)[before:]]
        self.assertTrue(any("EMERGENCY STOP" in t for t in texts), texts)

    def test_drop_records_a_relay_at_reported_odometry(self):
        self.m.ingest(telem(odo_mm=0))
        self.m.ingest("DROP,1000,1,845500")
        self.assertEqual(len(self.m.nodes), 1)
        self.assertAlmostEqual(self.m.nodes[0].position_m, 845.5, places=1)

    def test_duplicate_drop_is_ignored(self):
        self.m.ingest(telem())
        self.m.ingest("DROP,1000,1,500000")
        self.m.ingest("DROP,1100,1,500000")
        self.assertEqual(len(self.m.nodes), 1)


class TestChain(unittest.TestCase):
    def setUp(self):
        self.m = LiveMission("water_main_24")

    def test_single_hop_from_telemetry(self):
        self.m.ingest(telem(odo_mm=0))
        self.m.ingest(telem(seq=2, odo_mm=200_000, rssi=-70))
        chain = self.m.chain_dict()
        self.assertEqual(len(chain["hops"]), 1)
        hop = chain["hops"][0]
        self.assertAlmostEqual(hop["distance_m"], 200.0, places=0)
        # Measured path loss is simply Tx power minus RSSI.
        self.assertAlmostEqual(hop["path_loss_db"],
                               self.m.radio.tx_power_dbm - hop["rx_dbm"], places=1)
        self.assertTrue(hop["measured"])

    def test_hop_indices_follow_drop_order(self):
        self.m.ingest(telem(odo_mm=0))
        self.m.ingest("DROP,100,1,800000")
        self.m.ingest("DROP,200,2,1500000")
        self.m.ingest(telem(seq=5, odo_mm=1_800_000, rssi=-60))
        self.m.ingest("HOP,210,1,-80,1")
        self.m.ingest("HOP,210,2,-75,2")
        chain = self.m.chain_dict()
        self.assertEqual(len(chain["hops"]), 3)
        self.assertEqual([h["index"] for h in chain["hops"]], [0, 1, 2])
        self.assertEqual(chain["hops"][0]["node_id"], 1)
        self.assertEqual(chain["hops"][2]["node_id"], 99)
        # Endpoints come from the drop positions, so hop lengths are real.
        self.assertAlmostEqual(chain["hops"][0]["distance_m"], 800.0, places=0)
        self.assertAlmostEqual(chain["hops"][1]["distance_m"], 700.0, places=0)
        self.assertAlmostEqual(chain["hops"][2]["distance_m"], 300.0, places=0)

    def test_stale_hops_drop_out(self):
        self.m.ingest(telem(rssi=-60))
        self.assertEqual(len(self.m.chain_dict()["hops"]), 1)
        for obs in self.m.hops.values():
            obs.last_seen = time.monotonic() - 10.0
        self.assertEqual(len(self.m.chain_dict()["hops"]), 0)

    def test_weak_rssi_degrades_video(self):
        self.m.ingest(telem(rssi=-40))
        strong = self.m.snapshot()["video"]["name"]
        self.m.hops.clear()
        self.m.ingest(telem(seq=9, rssi=-92))
        weak = self.m.snapshot()["video"]["name"]
        self.assertNotEqual(strong, weak)

    def test_measured_against_modelled_is_reported(self):
        self.m.ingest(telem(odo_mm=0))
        self.m.ingest(telem(seq=2, odo_mm=500_000, rssi=-85))
        hop = self.m.chain_dict()["hops"][0]
        self.assertIn("modelled_loss_db", hop)
        self.assertAlmostEqual(hop["model_error_db"],
                               hop["path_loss_db"] - hop["modelled_loss_db"],
                               places=1)


class TestCalibration(unittest.TestCase):
    def test_fit_recovers_a_known_attenuation(self):
        """Feed in a synthetic drag test; the fit must find its slope."""
        m = LiveMission("water_main_24")
        db_per_m = 0.060
        fixed = 55.0
        for i in range(120):
            d = 5.0 + i * 5.0
            rssi = m.radio.tx_power_dbm - (fixed + db_per_m * d)
            m.ingest(telem(t_ms=i * 100, seq=i + 1,
                           odo_mm=int(d * 1000), rssi=int(round(rssi))))
            m._record()
        cal = m.calibration()
        self.assertGreater(cal["samples"], 20)
        self.assertAlmostEqual(cal["measured_db_per_km"], db_per_m * 1000,
                               delta=60.0)
        self.assertGreater(cal["r_squared"], 0.95)

    def test_calibration_is_honest_before_it_has_data(self):
        m = LiveMission("water_main_24")
        cal = m.calibration()
        self.assertIsNone(cal["measured_db_per_km"])
        self.assertFalse(cal["noise_floor_measured"])

    def test_measured_noise_floor_is_flagged(self):
        m = LiveMission("water_main_24", noise_floor_dbm=-98.0)
        self.assertTrue(m.calibration()["noise_floor_measured"])
        self.assertEqual(m.noise_floor_dbm, -98.0)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.m = LiveMission("water_main_24", send=self.rec)
        self.m.ingest(telem())

    def test_drive_sends_speed_in_mm_per_second(self):
        self.m.command("drive", {"speed_mps": 0.25})
        self.assertIn("C,250", self.rec.sent)

    def test_stop_sends_zero(self):
        self.m.command("stop")
        self.assertIn("C,0", self.rec.sent)

    def test_return_sends_negative_speed(self):
        self.m.command("set_speed", {"mps": 0.2})
        self.m.command("return")
        self.assertIn("C,-200", self.rec.sent)

    def test_estop_sends_e_and_awaits_confirmation(self):
        res = self.m.command("estop")
        self.assertIn("E", self.rec.sent)
        self.assertTrue(res["awaiting_confirmation"])

    def test_estop_round_trip_is_measured_from_the_crawler_flag(self):
        """The quotable safety number comes from the robot, not a model."""
        self.m.command("estop")
        time.sleep(0.02)
        self.m.ingest(telem(seq=2, flags=FLAG_ESTOP))
        self.assertIsNotNone(self.m.command_rtt_ms)
        self.assertGreater(self.m.command_rtt_ms, 0.0)
        self.assertEqual(self.m.snapshot()["chain"]["control_rtt_ms"],
                         round(self.m.command_rtt_ms, 1))

    def test_unconfirmed_command_is_reported_not_silently_dropped(self):
        self.m.command("estop")
        self.m._pending = ("EMERGENCY STOP", FLAG_ESTOP, True,
                           time.monotonic() - 5.0)
        self.m.ingest(telem(seq=3, flags=0))
        texts = [e["text"] for e in self.m.events]
        self.assertTrue(any("NOT confirmed" in t for t in texts), texts)

    def test_deploy_commands_the_crawler_and_waits(self):
        res = self.m.command("deploy")
        self.assertIn("D", self.rec.sent)
        self.assertTrue(res["awaiting_confirmation"])
        # Nothing is recorded until the crawler says where it happened.
        self.assertEqual(len(self.m.nodes), 0)
        self.m.ingest("DROP,500,1,120000")
        self.assertEqual(len(self.m.nodes), 1)

    def test_deploy_respects_an_empty_magazine(self):
        self.m.command("set_magazine", {"count": 1})
        self.m.ingest("DROP,100,1,1000")
        res = self.m.command("deploy")
        self.assertFalse(res["ok"])

    def test_lights_toggle_measures_round_trip(self):
        self.m.command("lights", {"on": False})
        self.assertIn("L,0", self.rec.sent)
        time.sleep(0.02)
        self.m.ingest(telem(seq=4, flags=0))
        self.assertIsNotNone(self.m.command_rtt_ms)

    def test_simulation_only_commands_are_refused(self):
        for name in ("set_radio", "set_policy", "reset", "auto"):
            res = self.m.command(name, {})
            self.assertFalse(res["ok"], name)
            self.assertIn("error", res)

    def test_unknown_command(self):
        self.assertFalse(self.m.command("fly")["ok"])


class TestPhase(unittest.TestCase):
    def test_phase_reflects_measured_motion(self):
        m = LiveMission("water_main_24")
        self.assertEqual(m.snapshot()["phase"], "idle")
        m.ingest(telem(speed_mms=200))
        self.assertEqual(m.snapshot()["phase"], "outbound")
        m.ingest(telem(seq=2, speed_mms=-200))
        self.assertEqual(m.snapshot()["phase"], "returning")

    def test_lost_telemetry_shows_as_aborted(self):
        m = LiveMission("water_main_24")
        m.ingest(telem(speed_mms=200))
        m.last_telem = time.monotonic() - 5.0
        self.assertEqual(m.snapshot()["phase"], "aborted")


class TestEmulatedTransport(unittest.TestCase):
    """The no-hardware rehearsal path has to actually work."""

    def test_emulator_produces_parsable_lines(self):
        import importlib.util
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "hardware", "bridge", "live_bridge.py")
        spec = importlib.util.spec_from_file_location("live_bridge", path)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)

        transport = bridge.EmulatedTransport("water_main_24", time_scale=60.0)
        mission = LiveMission("water_main_24", send=transport.write)
        got = 0
        for line in transport.lines():
            mission.ingest(line)
            if line.startswith("TLM"):
                got += 1
            if got >= 20:
                break
        mission.step(0.1)
        snap = mission.snapshot()
        self.assertTrue(snap["live"]["connected"])
        self.assertGreater(len(snap["chain"]["hops"]), 0)
        self.assertGreater(mission.position_m, 0.0)


if __name__ == "__main__":
    unittest.main()
