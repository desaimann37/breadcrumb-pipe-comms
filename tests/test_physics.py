"""Checks on the waveguide model.

These are the assertions that would catch a sign error or a bad unit before it
turned into a wrong recommendation in the report.  The important one is
``test_matches_exact_te11_formula_for_metal``: for a bare metal pipe there is a
closed-form answer, and the grazing-ray approximation has to land on it.
"""

import math
import unittest

from pipecomm.materials import (
    AIR, BIOFILM, CEMENT_MORTAR, CONCRETE, DUCTILE_IRON, PVC, SCALE, STEEL,
    VCP, WASTEWATER,
)
from pipecomm.waveguide import (
    SegmentSpec, air_segment, bend_loss_db, blockage_loss_db,
    coherence_bandwidth_hz, cutoff_frequency, evanescent_attenuation_db_per_m,
    hybrid_bounce_loss_db, joint_loss_db, modal_delay_spread_s,
    reflection_coefficient, roughness_factor, segment_loss,
    te11_metal_attenuation_db_per_m,
)

IN = 0.0254


class TestCutoff(unittest.TestCase):
    def test_known_values(self):
        # TE11 cutoff = 1.8412 c / (2 pi a). The argument is the RADIUS, so a
        # 6 in pipe (3 in radius) cuts off at 1153 MHz and a 12 in at 576 MHz.
        self.assertAlmostEqual(cutoff_frequency(3 * IN) / 1e6, 1152.9, delta=1.0)
        self.assertAlmostEqual(cutoff_frequency(6 * IN) / 1e6, 576.4, delta=1.0)
        self.assertAlmostEqual(cutoff_frequency(9 * IN) / 1e6, 384.3, delta=1.0)

    def test_inverse_with_radius(self):
        self.assertGreater(cutoff_frequency(0.1), cutoff_frequency(0.2))

    def test_dielectric_fill_lowers_cutoff(self):
        self.assertLess(cutoff_frequency(0.3, eps_r_fill=4.0), cutoff_frequency(0.3))

    def test_evanescent_below_cutoff(self):
        fc = cutoff_frequency(4 * IN)
        self.assertGreater(evanescent_attenuation_db_per_m(0.5 * fc, fc), 10.0)
        self.assertEqual(evanescent_attenuation_db_per_m(2 * fc, fc), 0.0)


class TestReflection(unittest.TestCase):
    def test_passive(self):
        """A passive wall can never reflect more than it receives."""
        for mat in (DUCTILE_IRON, CONCRETE, VCP, PVC, WASTEWATER, BIOFILM):
            for cos_phi in (0.01, 0.1, 0.4, 0.9):
                for pol in ("TE", "TM"):
                    g = reflection_coefficient(2.4e9, cos_phi, mat, polarisation=pol)
                    self.assertLessEqual(abs(g), 1.0 + 1e-9, f"{mat.name} {pol} {cos_phi}")

    def test_metal_better_than_wet_clay(self):
        g_metal = abs(reflection_coefficient(2.4e9, 0.2, DUCTILE_IRON))
        g_clay = abs(reflection_coefficient(2.4e9, 0.2, VCP))
        self.assertGreater(g_metal, g_clay)

    def test_grazing_is_better_than_normal(self):
        grazing = abs(reflection_coefficient(2.4e9, 0.02, CONCRETE))
        steep = abs(reflection_coefficient(2.4e9, 0.9, CONCRETE))
        self.assertGreater(grazing, steep)

    def test_tm_is_the_lossy_polarisation_for_dielectrics(self):
        """Brewster-like null: TM reflects far worse off concrete than TE."""
        te = abs(reflection_coefficient(2.4e9, 0.12, CONCRETE, polarisation="TE"))
        tm = abs(reflection_coefficient(2.4e9, 0.12, CONCRETE, polarisation="TM"))
        self.assertGreater(te, tm)

    def test_metal_reflects_both_polarisations(self):
        for pol in ("TE", "TM"):
            g = abs(reflection_coefficient(2.4e9, 0.12, STEEL, polarisation=pol))
            self.assertGreater(g, 0.95, pol)

    def test_hybrid_loss_sits_between_the_two(self):
        hyb = hybrid_bounce_loss_db(2.4e9, 0.12, CONCRETE)
        te = -20 * math.log10(abs(reflection_coefficient(
            2.4e9, 0.12, CONCRETE, polarisation="TE")))
        tm = -20 * math.log10(abs(reflection_coefficient(
            2.4e9, 0.12, CONCRETE, polarisation="TM")))
        self.assertTrue(te < hyb < tm, (te, hyb, tm))

    def test_coating_costs_loss(self):
        bare = hybrid_bounce_loss_db(2.4e9, 0.12, DUCTILE_IRON)
        coated = hybrid_bounce_loss_db(2.4e9, 0.12, DUCTILE_IRON, BIOFILM, 0.002)
        self.assertGreater(coated, bare)

    def test_roughness_matters_less_at_grazing(self):
        at_grazing = roughness_factor(2.4e9, 0.05, 1e-3)
        at_steep = roughness_factor(2.4e9, 0.8, 1e-3)
        self.assertGreater(at_grazing, at_steep)


class TestAirSegment(unittest.TestCase):
    def test_empty_pipe(self):
        seg = air_segment(0.3, 0.0)
        self.assertAlmostEqual(seg.area_m2, math.pi * 0.09, places=6)
        self.assertAlmostEqual(seg.water_bounce_fraction, 0.0, places=6)

    def test_half_full(self):
        seg = air_segment(0.3, 0.5)
        self.assertAlmostEqual(seg.area_m2, math.pi * 0.09 / 2, places=6)
        self.assertAlmostEqual(seg.chord_len_m, 0.6, places=6)
        self.assertGreater(seg.water_bounce_fraction, 0.3)

    def test_fuller_pipe_shrinks_aperture(self):
        a = air_segment(0.3, 0.2).effective_radius_m
        b = air_segment(0.3, 0.6).effective_radius_m
        self.assertGreater(a, b)


class TestSegmentLoss(unittest.TestCase):
    def test_bigger_pipe_lower_loss(self):
        small = segment_loss(2.4e9, SegmentSpec(8 * IN, DUCTILE_IRON)).db_per_m
        large = segment_loss(2.4e9, SegmentSpec(24 * IN, DUCTILE_IRON)).db_per_m
        self.assertGreater(small, large)

    def test_below_cutoff_flagged_and_huge(self):
        res = segment_loss(433e6, SegmentSpec(8 * IN, DUCTILE_IRON))
        self.assertTrue(res.below_cutoff)
        self.assertGreater(res.db_per_m, 100.0)

    def test_water_is_catastrophic_in_a_metal_pipe(self):
        """Standing water swaps an excellent mirror for a lossy one."""
        dry = segment_loss(2.4e9, SegmentSpec(24 * IN, DUCTILE_IRON)).db_per_m
        wet = segment_loss(2.4e9, SegmentSpec(
            24 * IN, DUCTILE_IRON, WASTEWATER, 0.3)).db_per_m
        self.assertGreater(wet, 10 * dry)

    def test_more_water_always_worse(self):
        for wall in (DUCTILE_IRON, CONCRETE):
            last = 0.0
            for fill in (0.0, 0.1, 0.2, 0.3, 0.5):
                cur = segment_loss(2.4e9, SegmentSpec(
                    24 * IN, wall, WASTEWATER, fill)).db_per_m
                self.assertGreater(cur, last, f"{wall.name} at {fill}")
                last = cur

    def test_water_raises_cutoff(self):
        dry = segment_loss(2.4e9, SegmentSpec(24 * IN, CONCRETE))
        wet = segment_loss(2.4e9, SegmentSpec(24 * IN, CONCRETE, WASTEWATER, 0.5))
        self.assertGreater(wet.cutoff_hz, dry.cutoff_hz)

    def test_fouling_always_costs(self):
        """Biofilm and scale must never look like an improvement.

        A TE-only model gets this wrong: a high-permittivity film reflects
        better at grazing, so fouling appears to help.  Including the TM half
        of the hybrid mode restores the answer field crews actually see.
        """
        for wall in (DUCTILE_IRON, STEEL, CONCRETE, VCP):
            bare = segment_loss(2.4e9, SegmentSpec(24 * IN, wall)).db_per_m
            for coat, thick in ((BIOFILM, 0.002), (BIOFILM, 0.004),
                                (SCALE, 0.004)):
                fouled = segment_loss(2.4e9, SegmentSpec(
                    24 * IN, wall, AIR, 0.0, coat, thick)).db_per_m
                self.assertGreater(fouled, bare, f"{wall.name} + {coat.name}")

    def test_mortar_lining_costs_a_metal_pipe_its_advantage(self):
        """Lining is what makes a potable main behave like concrete.

        Applied to a dielectric wall the lining is nearly the same material as
        the substrate, so it is a no-op; the case that matters is lining over
        metal, where it hides an excellent mirror behind a lossy slab.
        """
        bare = segment_loss(2.4e9, SegmentSpec(24 * IN, DUCTILE_IRON)).db_per_m
        for thickness in (0.003, 0.006, 0.009, 0.012):
            lined = segment_loss(2.4e9, SegmentSpec(
                24 * IN, DUCTILE_IRON, AIR, 0.0, CEMENT_MORTAR, thickness)).db_per_m
            self.assertGreater(lined, bare, f"{thickness * 1000:.0f} mm lining")

    def test_metal_beats_concrete_by_orders_of_magnitude(self):
        metal = segment_loss(2.4e9, SegmentSpec(24 * IN, DUCTILE_IRON)).db_per_m
        conc = segment_loss(2.4e9, SegmentSpec(24 * IN, CONCRETE)).db_per_m
        self.assertGreater(conc / metal, 10.0)

    def test_matches_exact_te11_formula_for_metal(self):
        """The grazing-ray model must agree with the closed-form modal result."""
        for dia_in in (12, 24, 48):
            for f in (915e6, 2.4e9, 5.8e9):
                a = dia_in * IN / 2
                ray = segment_loss(f, SegmentSpec(dia_in * IN, STEEL)).db_per_m
                exact = te11_metal_attenuation_db_per_m(f, a, STEEL.sigma, STEEL.mu_r)
                self.assertTrue(0.7 < ray / exact < 1.4,
                                f"{dia_in} in at {f / 1e9:.2f} GHz: ray "
                                f"{ray * 1000:.2f} vs exact {exact * 1000:.2f} dB/km")

    def test_inverse_cube_diameter_scaling(self):
        """Hollow dielectric guide loss goes as 1/a^3: diameter is everything."""
        small = segment_loss(2.4e9, SegmentSpec(24 * IN, CONCRETE)).db_per_m
        large = segment_loss(2.4e9, SegmentSpec(48 * IN, CONCRETE)).db_per_m
        self.assertTrue(6.0 < small / large < 10.0, small / large)

    def test_optimum_frequency_depends_on_material(self):
        """Metal prefers a low carrier, dielectric pipes prefer a high one."""
        metal_lo = segment_loss(915e6, SegmentSpec(24 * IN, STEEL)).db_per_m
        metal_hi = segment_loss(5.8e9, SegmentSpec(24 * IN, STEEL)).db_per_m
        self.assertLess(metal_lo, metal_hi)
        conc_lo = segment_loss(915e6, SegmentSpec(36 * IN, CONCRETE)).db_per_m
        conc_hi = segment_loss(5.8e9, SegmentSpec(36 * IN, CONCRETE)).db_per_m
        self.assertGreater(conc_lo, conc_hi)

    def test_plausible_magnitude_for_a_dry_metal_main(self):
        db_km = segment_loss(2.4e9, SegmentSpec(24 * IN, DUCTILE_IRON)).db_per_m * 1000
        self.assertTrue(2.0 < db_km < 40.0, db_km)

    def test_plausible_magnitude_for_a_wet_sewer(self):
        spec = SegmentSpec(36 * IN, CONCRETE, WASTEWATER, 0.2, BIOFILM, 0.002)
        db_km = segment_loss(2.4e9, spec).db_per_m * 1000
        self.assertTrue(50.0 < db_km < 1500.0, db_km)


class TestFeatureLosses(unittest.TestCase):
    def test_tight_bend_worse_than_sweep(self):
        a = 0.3
        tight = bend_loss_db(90, 0.3, a, 2.4e9, 288e6)
        sweep = bend_loss_db(90, 3.0, a, 2.4e9, 288e6)
        self.assertGreater(tight, sweep)
        self.assertGreater(tight, 10.0)

    def test_bend_zero_angle(self):
        self.assertEqual(bend_loss_db(0, 1.0, 0.3, 2.4e9, 288e6), 0.0)

    def test_offset_dominates_gap(self):
        gap_only = joint_loss_db(0.01, 0.0, 0.3)
        offset = joint_loss_db(0.01, 0.05, 0.3)
        self.assertGreater(offset, gap_only)
        self.assertLess(gap_only, 0.05)

    def test_blockage(self):
        self.assertAlmostEqual(blockage_loss_db(0.0), 0.0, places=6)
        self.assertGreater(blockage_loss_db(0.5), 5.0)


class TestDispersion(unittest.TestCase):
    def test_delay_spread_grows_with_length(self):
        a = modal_delay_spread_s(2.4e9, 288e6, 100)
        b = modal_delay_spread_s(2.4e9, 288e6, 1000)
        self.assertGreater(b, a)

    def test_delay_spread_fits_ofdm_guard(self):
        """1 km in a 24 in pipe must stay inside the 800 ns 802.11 guard."""
        tau = modal_delay_spread_s(2.4e9, 288e6, 1000)
        self.assertLess(tau, 800e-9)

    def test_coherence_bandwidth(self):
        tau = modal_delay_spread_s(2.4e9, 576e6, 500)
        self.assertGreater(coherence_bandwidth_hz(tau), 1e6)


if __name__ == "__main__":
    unittest.main()
