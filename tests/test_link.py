"""Checks on the radio layer: MCS selection, link budget and the relay chain."""

import unittest

from pipecomm.link import (
    RadioConfig, evaluate_chain, evaluate_hop, free_space_loss_db, max_reach_m,
    path_loss,
)
from pipecomm.materials import CONCRETE, DUCTILE_IRON
from pipecomm.mcs import (
    CONTROL_MCS, WIFI_MCS, effective_rate_bps, packet_error_rate, select_mcs,
    shannon_limit_bps,
)
from pipecomm.pipe import PipeProfile, PipeSection, make_manhole
from pipecomm.video import LADDER, NO_VIDEO, select_tier, fec_overhead_for
from pipecomm.waveguide import SegmentSpec

IN = 0.0254


def simple_profile(length=3000.0, dia_in=24, wall=DUCTILE_IRON, features=None,
                   excess=0.045):
    """A uniform test pipe.

    ``excess`` defaults to 45 dB/km, which is what joints, ovality and a
    mortar lining add to a real main. A perfectly bare pipe is so low loss
    that the link saturates the SNR ceiling for kilometres and most of these
    assertions become vacuous.
    """
    spec = SegmentSpec(dia_in * IN, wall, excess_loss_db_per_m=excess)
    return PipeProfile("test", length, [PipeSection(0, length, spec)], features or [])


class TestMcs(unittest.TestCase):
    def test_ladder_is_ordered(self):
        for a, b in zip(WIFI_MCS, WIFI_MCS[1:]):
            self.assertLess(a.required_snr_db, b.required_snr_db)
            self.assertLess(a.spectral_efficiency, b.spectral_efficiency)

    def test_per_is_monotonic_in_snr(self):
        mcs = WIFI_MCS[3]
        last = 1.1
        for snr in range(0, 30, 2):
            per = packet_error_rate(snr, mcs)
            self.assertLessEqual(per, last)
            last = per

    def test_per_bounds(self):
        mcs = WIFI_MCS[0]
        self.assertGreater(packet_error_rate(-20, mcs), 0.99)
        self.assertLess(packet_error_rate(40, mcs), 1e-6)

    def test_select_mcs_climbs_with_snr(self):
        self.assertIsNone(select_mcs(-10))
        low = select_mcs(6)
        high = select_mcs(30)
        self.assertIsNotNone(low)
        self.assertGreater(high.spectral_efficiency, low.spectral_efficiency)

    def test_rate_never_beats_shannon(self):
        for snr in (3, 8, 15, 25, 32):
            rate, _, _ = effective_rate_bps(snr, 20e6)
            self.assertLess(rate, shannon_limit_bps(snr, 20e6))

    def test_control_waveform_works_far_below_video(self):
        """The whole safety argument: control survives where video cannot."""
        snr = -10.0
        self.assertLess(packet_error_rate(snr, CONTROL_MCS), 0.01)
        self.assertGreater(packet_error_rate(snr, WIFI_MCS[0]), 0.9)

    def test_control_rate_is_small_but_usable(self):
        rate = CONTROL_MCS.phy_rate_bps(5e6)
        self.assertTrue(5e3 < rate < 50e3, rate)


class TestVideoLadder(unittest.TestCase):
    def test_ladder_descends(self):
        for a, b in zip(LADDER, LADDER[1:]):
            self.assertGreater(a.bitrate_bps, b.bitrate_bps)

    def test_no_bandwidth_means_no_video(self):
        self.assertIs(select_tier(0.0), NO_VIDEO)
        self.assertIs(select_tier(20e3, control_reserve_bps=48e3), NO_VIDEO)

    def test_more_bandwidth_never_lowers_quality(self):
        last = 0
        for bps in (0.1e6, 0.4e6, 1e6, 3e6, 10e6, 50e6):
            tier = select_tier(bps)
            self.assertGreaterEqual(tier.bitrate_bps, last)
            last = tier.bitrate_bps

    def test_control_slice_is_reserved_first(self):
        """Video must never be allowed to starve the control channel."""
        tier = select_tier(60e3, control_reserve_bps=48e3)
        self.assertLess(tier.bitrate_bps, 25e3)

    def test_fec_grows_with_loss(self):
        self.assertAlmostEqual(fec_overhead_for(0.0), 1.0)
        self.assertGreater(fec_overhead_for(0.3), fec_overhead_for(0.05))


class TestLinkBudget(unittest.TestCase):
    def setUp(self):
        self.profile = simple_profile()
        self.radio = RadioConfig()

    def test_free_space_sanity(self):
        # 2.4 GHz at 100 m is a hair over 80 dB.
        self.assertAlmostEqual(free_space_loss_db(100, 2.4e9), 80.05, delta=0.5)

    def test_loss_grows_with_distance(self):
        a, _ = path_loss(self.profile, self.radio, 0, 100)
        b, _ = path_loss(self.profile, self.radio, 0, 500)
        self.assertGreater(b, a)

    def test_clean_pipe_beats_free_space_at_range(self):
        """The waveguide effect is the entire reason this is feasible."""
        clean = simple_profile(excess=0.0)
        pipe_db, _ = path_loss(clean, self.radio, 0, 1500)
        self.assertLess(pipe_db, free_space_loss_db(1500, self.radio.frequency_hz))

    def test_snr_falls_with_distance(self):
        near = evaluate_hop(self.profile, self.radio, 0, 0, 50)
        far = evaluate_hop(self.profile, self.radio, 0, 0, 900)
        self.assertGreater(near.snr_db, far.snr_db)

    def test_snr_is_capped_by_dispersion_floor(self):
        hop = evaluate_hop(self.profile, self.radio, 0, 0, 1.0)
        self.assertLessEqual(hop.snr_db, self.radio.snr_ceiling_db + 1e-9)

    def test_more_power_more_margin(self):
        base = evaluate_hop(self.profile, self.radio, 0, 0, 700)
        hot = RadioConfig(tx_power_dbm=30.0)
        strong = evaluate_hop(self.profile, hot, 0, 0, 700)
        self.assertGreater(strong.margin_db, base.margin_db)

    def test_feature_loss_is_attributed(self):
        prof = simple_profile(features=[make_manhole(100.0, 9.0)])
        hop = evaluate_hop(prof, self.radio, 0, 0, 200)
        self.assertAlmostEqual(hop.feature_db, 9.0, places=3)
        self.assertEqual(hop.worst_feature, "manhole")

    def test_control_outlives_video_on_a_long_hop(self):
        hop = evaluate_hop(self.profile, self.radio, 0, 0, 1500)
        self.assertGreater(hop.control_margin_db, hop.margin_db + 20)


class TestChain(unittest.TestCase):
    def setUp(self):
        self.profile = simple_profile(length=3000.0)
        self.radio = RadioConfig()

    def test_empty_chain(self):
        res = evaluate_chain(self.profile, self.radio, [0.0])
        self.assertEqual(res.hops, [])
        self.assertEqual(res.video_goodput_bps, 0.0)

    def test_relays_beat_one_long_hop(self):
        """Three short hops must deliver more than one hop of the same span."""
        direct = evaluate_chain(self.profile, self.radio, [0.0, 2400.0])
        relayed = evaluate_chain(self.profile, self.radio, [0.0, 800.0, 1600.0, 2400.0])
        self.assertGreater(relayed.video_goodput_bps, direct.video_goodput_bps)

    def test_bottleneck_is_the_weakest_hop(self):
        res = evaluate_chain(self.profile, self.radio, [0.0, 100.0, 1100.0])
        self.assertEqual(res.weakest_hop, 1)

    def test_latency_grows_with_hops(self):
        short = evaluate_chain(self.profile, self.radio, [0.0, 400.0])
        long = evaluate_chain(self.profile, self.radio, [0.0, 200.0, 400.0, 600.0, 800.0])
        self.assertGreater(long.control_rtt_ms, short.control_rtt_ms)

    def test_dual_radio_recovers_airtime(self):
        """The clearest architectural finding in the study."""
        single = RadioConfig(radios_per_node=1, channel_count=1)
        dual = RadioConfig(radios_per_node=2, channel_count=3)
        self.assertAlmostEqual(single.airtime_divisor, 3.0)
        self.assertAlmostEqual(dual.airtime_divisor, 1.0)
        positions = [0.0, 400.0, 800.0, 1200.0]
        a = evaluate_chain(self.profile, single, positions).video_goodput_bps
        b = evaluate_chain(self.profile, dual, positions).video_goodput_bps
        self.assertAlmostEqual(b / a, 3.0, delta=0.05)

    def test_fade_reduces_margin(self):
        clear = evaluate_chain(self.profile, self.radio, [0.0, 600.0])
        faded = evaluate_chain(self.profile, self.radio, [0.0, 600.0], fades=[10.0])
        self.assertLess(faded.min_margin_db, clear.min_margin_db - 5)


class TestReach(unittest.TestCase):
    def test_reach_shrinks_with_required_margin(self):
        profile = simple_profile()
        radio = RadioConfig()
        loose = max_reach_m(profile, radio, 0.0, target_margin_db=2.0, step_m=10)
        tight = max_reach_m(profile, radio, 0.0, target_margin_db=18.0, step_m=10)
        self.assertGreater(loose, tight)

    def test_concrete_needs_more_relays_than_metal(self):
        radio = RadioConfig()
        metal = max_reach_m(simple_profile(wall=DUCTILE_IRON), radio, 0.0, step_m=10)
        conc = max_reach_m(simple_profile(wall=CONCRETE), radio, 0.0, step_m=10)
        self.assertGreater(metal, 3 * conc)

    def test_a_big_obstacle_stops_the_hop(self):
        prof = simple_profile(features=[make_manhole(120.0, 60.0)])
        reach = max_reach_m(prof, RadioConfig(), 0.0, step_m=5)
        self.assertLess(reach, 125.0)


if __name__ == "__main__":
    unittest.main()
