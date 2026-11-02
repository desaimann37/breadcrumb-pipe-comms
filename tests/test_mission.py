"""Checks on the mission engine: deployment, recovery, safety and energy."""

import unittest

from pipecomm.crawler import Crawler, CrawlerConfig
from pipecomm.link import RadioConfig
from pipecomm.mission import Mission, plan_nodes
from pipecomm.scenarios import build, list_scenarios


def run(mission, seconds, dt=0.5):
    for _ in range(int(seconds / dt)):
        if mission.finished:
            break
        mission.step(dt)


class TestCrawler(unittest.TestCase):
    def setUp(self):
        self.c = Crawler(CrawlerConfig())

    def test_accelerates_to_commanded_speed(self):
        self.c.set_speed(0.3)
        for _ in range(100):
            self.c.step(0.1, True, 1000.0)
        self.assertAlmostEqual(self.c.speed_mps, 0.3, places=2)

    def test_speed_is_clamped(self):
        self.c.set_speed(99.0)
        self.assertLessEqual(self.c.commanded_speed_mps, self.c.config.max_speed_mps)

    def test_cannot_reverse_past_the_entry(self):
        self.c.set_speed(-0.3)
        for _ in range(200):
            self.c.step(0.1, True, 1000.0)
        self.assertGreaterEqual(self.c.position_m, 0.0)

    def test_estop_halts(self):
        self.c.set_speed(0.3)
        for _ in range(60):
            self.c.step(0.1, True, 1000.0)
        self.c.trigger_estop()
        for _ in range(60):
            self.c.step(0.1, True, 1000.0)
        self.assertAlmostEqual(self.c.speed_mps, 0.0, places=3)

    def test_watchdog_stops_the_crawler_when_control_goes_quiet(self):
        """The safety property the whole architecture is built around."""
        self.c.set_speed(0.3)
        for _ in range(60):
            self.c.step(0.1, True, 1000.0)
        self.assertGreater(self.c.speed_mps, 0.1)
        events = []
        for _ in range(40):
            events += self.c.step(0.1, False, 1000.0)
        self.assertTrue(self.c.watchdog_tripped)
        self.assertTrue(any("WATCHDOG" in e for e in events))
        self.assertAlmostEqual(self.c.speed_mps, 0.0, places=3)

    def test_watchdog_rearms_when_control_returns(self):
        self.c.set_speed(0.3)
        for _ in range(40):
            self.c.step(0.1, False, 1000.0)
        self.assertTrue(self.c.watchdog_tripped)
        for _ in range(10):
            self.c.step(0.1, True, 1000.0)
        self.assertFalse(self.c.watchdog_tripped)

    def test_battery_drains_and_stops_the_machine(self):
        self.c.battery_wh = 0.4
        self.c.set_speed(0.3)
        for _ in range(900):
            self.c.step(0.5, True, 5000.0)
        self.assertEqual(self.c.battery_wh, 0.0)
        self.assertAlmostEqual(self.c.speed_mps, 0.0, places=3)

    def test_return_energy_grows_with_distance(self):
        self.c.position_m = 500
        near = self.c.return_energy_needed_wh()
        self.c.position_m = 2500
        self.assertGreater(self.c.return_energy_needed_wh(), near)

    def test_must_turn_back_when_only_the_reserve_is_left(self):
        self.c.position_m = 3000
        self.c.battery_wh = self.c.return_energy_needed_wh() * 1.1
        self.assertTrue(self.c.must_turn_back())
        self.c.battery_wh = self.c.config.battery_wh
        self.assertFalse(self.c.must_turn_back())


class TestPlanner(unittest.TestCase):
    def test_metal_main_needs_few_relays(self):
        sc = build("water_main_24")
        plan = plan_nodes(sc.profile, sc.radio, step_m=10.0)
        self.assertTrue(1 <= len(plan) <= 10, len(plan))

    def test_concrete_sewer_needs_many(self):
        sc = build("interceptor_36")
        plan = plan_nodes(sc.profile, sc.radio, step_m=10.0, max_nodes=200)
        self.assertGreater(len(plan), 10)

    def test_plan_is_ordered_and_inside_the_pipe(self):
        sc = build("water_main_24")
        plan = plan_nodes(sc.profile, sc.radio, step_m=10.0)
        self.assertEqual(plan, sorted(plan))
        for p in plan:
            self.assertTrue(0 < p < sc.profile.length_m)

    def test_tighter_margin_needs_more_relays(self):
        sc = build("water_main_24")
        loose = plan_nodes(sc.profile, sc.radio, margin_db=2.0, step_m=10.0, max_nodes=200)
        tight = plan_nodes(sc.profile, sc.radio, margin_db=18.0, step_m=10.0, max_nodes=200)
        self.assertGreater(len(tight), len(loose))


class TestMission(unittest.TestCase):
    def test_all_scenarios_build_and_step(self):
        for meta in list_scenarios():
            m = Mission(meta["key"])
            run(m, 20)
            self.assertGreater(m.t_s, 0)
            self.assertIn("chain", m.snapshot())

    def test_drive_moves_the_crawler(self):
        m = Mission("water_main_24")
        m.command("drive", {"speed_mps": 0.3})
        run(m, 120)
        self.assertGreater(m.crawler.position_m, 20)

    def test_relays_are_dropped_as_margin_runs_out(self):
        m = Mission("interceptor_36")
        m.start()
        run(m, 2000)
        self.assertGreater(m.node_counter, 0)
        positions = [n.position_m for n in m.nodes]
        self.assertEqual(positions, sorted(positions))

    def test_magazine_is_respected(self):
        m = Mission("force_main_12")
        m.command("set_magazine", {"count": 3})
        m.start()
        run(m, 6000)
        self.assertLessEqual(len([n for n in m.nodes if not n.recovered]), 3)

    def test_full_run_completes_and_recovers_its_relays(self):
        """End to end: two miles out, two miles back, nodes picked up."""
        m = Mission("water_main_24")
        m.command("set_magazine", {"count": 24})
        m.start()
        m.cruise_speed = 0.3
        run(m, 60 * 60 * 24, dt=0.5)
        self.assertEqual(m.phase, "complete")
        self.assertGreater(max(s.position_m for s in m.samples), 3000)
        self.assertTrue(all(n.recovered for n in m.nodes),
                        "every relay should be collected on the way out")

    def test_control_survives_wherever_video_does(self):
        """Video may drop, but the crawler must stay drivable."""
        m = Mission("force_main_12")
        m.command("set_policy", {"policy": "stretch"})
        m.command("set_magazine", {"count": 2})
        m.start()
        video_lost = False
        for _ in range(12000):
            if m.finished:
                break
            m.step(0.5)
            if m.video.tier.name == "no video":
                video_lost = True
                self.assertTrue(m.chain.control_ok or m.crawler.watchdog_tripped)
        self.assertTrue(video_lost, "expected the stretch policy to lose video")

    def test_estop_is_acknowledged(self):
        m = Mission("water_main_24")
        m.command("drive", {"speed_mps": 0.3})
        run(m, 60)
        res = m.command("estop")
        self.assertTrue(res["ok"])
        self.assertGreater(res["rtt_ms"], 0)
        run(m, 30)
        self.assertAlmostEqual(m.crawler.speed_mps, 0.0, places=3)

    def test_below_cutoff_kills_the_link(self):
        """433 MHz in a 12 in pipe is physically impossible, not merely weak."""
        m = Mission("force_main_12")
        m.command("set_radio", {"frequency_mhz": 433})
        m.command("drive", {"speed_mps": 0.3})
        run(m, 300)
        self.assertTrue(m.context()["below_cutoff"])
        self.assertEqual(m.chain.video_goodput_bps, 0.0)

    def test_policy_changes_the_relay_budget(self):
        counts = {}
        for policy in ("quality", "stretch"):
            m = Mission("water_main_24")
            m.command("set_policy", {"policy": policy})
            m.start()
            m.cruise_speed = 0.3
            run(m, 20000)
            counts[policy] = m.node_counter
        self.assertGreaterEqual(counts["quality"], counts["stretch"])

    def test_snapshot_is_json_shaped(self):
        import json
        m = Mission("interceptor_36")
        run(m, 60)
        payload = json.dumps(m.snapshot())
        self.assertIn("chain", payload)
        self.assertIn("video", payload)
        self.assertIn("context", payload)

    def test_history_exports(self):
        m = Mission("water_main_24")
        m.command("drive", {"speed_mps": 0.3})
        run(m, 600)
        hist = m.history()
        self.assertGreater(len(hist), 3)
        self.assertIn("goodput_mbps", hist[0])


if __name__ == "__main__":
    unittest.main()
