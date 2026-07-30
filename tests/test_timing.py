#!/usr/bin/env python3
"""cosr.timing: summarising coincidence and gate precision.

Numerically identical to the implementation whose figures are already on record, so a change of
plumbing cannot move a published measurement.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from cosr import timing as T


class TestStats(unittest.TestCase):

    def test_displacement_and_spread_are_separate(self):
        """A constant offset and random scatter are different defects with different causes."""
        shifted = T.stats([10.0] * 20, 20)
        self.assertAlmostEqual(shifted["bias_us"], 10.0, places=6)
        self.assertAlmostEqual(shifted["jitter_us"], 0.0, places=6)

        scattered = T.stats([-5.0, 5.0] * 10, 20)
        self.assertAlmostEqual(scattered["bias_us"], 0.0, places=6)
        self.assertAlmostEqual(scattered["jitter_us"], 5.0, places=6)

    def test_extreme_values_are_excluded_and_counted(self):
        errs = [1.0] * 18 + [900.0, -900.0]
        got = T.stats(errs, 20)
        self.assertEqual(got["outliers"], 2)
        self.assertAlmostEqual(got["bias_us"], 1.0, places=6)
        self.assertAlmostEqual(got["jitter_us"], 0.0, places=6)

    def test_robust_figures_stay_at_the_inlier_level(self):
        """Two extremes in twenty do not move the middle or the ninetieth percentile -- that is
        what makes them robust. Their presence is reported as a count instead, so they are
        visible without distorting the summary."""
        errs = [1.0] * 18 + [900.0, -900.0]
        got = T.stats(errs, 20)
        self.assertAlmostEqual(got["median_us"], 1.0, places=6)
        self.assertAlmostEqual(got["p90_abs_us"], 1.0, places=6)
        self.assertEqual(got["outliers"], 2)

    def test_many_extremes_do_reach_the_ninetieth_percentile(self):
        errs = [1.0] * 10 + [900.0] * 10
        self.assertGreater(T.stats(errs, 20)["p90_abs_us"], 100.0)

    def test_drift_ignores_the_same_extremes_the_spread_does(self):
        """Fitted over every sample, a few mis-stamped receptions would dominate the fit and
        report a rate the spread plainly contradicts."""
        n = 30
        errs = [0.0] * n
        errs[0] = -2000.0
        errs[-1] = 2000.0
        got = T.stats(errs, n, positions=range(n))
        self.assertEqual(got["outliers"], 2)
        self.assertAlmostEqual(got["drift_us_per_shot"], 0.0, places=6)

    def test_a_real_drift_survives_the_filter(self):
        n = 30
        errs = [0.3 * i - 4.0 for i in range(n)]
        got = T.stats(errs, n, positions=range(n))
        self.assertAlmostEqual(got["drift_us_per_shot"], 0.3, places=4)

    def test_drift_absent_without_positions(self):
        self.assertIsNone(T.stats([1.0, 2.0, 3.0], 3)["drift_us_per_shot"])

    def test_empty(self):
        got = T.stats([], 0)
        self.assertIsNone(got["bias_us"])
        self.assertEqual(got["outliers"], 0)

    def test_all_extreme(self):
        got = T.stats([900.0, -900.0], 2)
        self.assertIsNone(got["bias_us"])
        self.assertEqual(got["outliers"], 2)

    def test_even_and_odd_middle_value(self):
        self.assertAlmostEqual(T.stats([1.0, 3.0], 2)["median_us"], 2.0, places=6)
        self.assertAlmostEqual(T.stats([1.0, 2.0, 6.0], 3)["median_us"], 2.0, places=6)


class TestDetrend(unittest.TestCase):

    def test_a_constant_offset_and_drift_are_removed(self):
        pairs = [(i, 1000.0 + 3.0 * i) for i in range(20)]
        got = T.detrend_residual(pairs)
        self.assertAlmostEqual(got["drift_us_per_shot"], 3.0, places=6)
        self.assertLess(got["residual_max_us"], 1e-6)
        self.assertEqual(got["outliers"], [])

    def test_a_late_start_stands_out(self):
        pairs = [(i, 1000.0 + 3.0 * i) for i in range(20)]
        pairs[7] = (7, pairs[7][1] + 400.0)
        got = T.detrend_residual(pairs)
        self.assertTrue(got["outliers"])
        self.assertEqual(got["outliers"][0][0], 7)

    def test_too_few_samples(self):
        got = T.detrend_residual([(0, 1.0)])
        self.assertIsNone(got["drift_us_per_shot"])
        self.assertEqual(got["outliers"], [])


class TestPairwise(unittest.TestCase):

    def test_only_shots_seen_from_both_are_compared(self):
        ref = {0: 100, 1: 200, 2: 300}
        other = {0: 150, 2: 350}
        errs, common = T.pairwise_errors(ref, other, stagger_us=50, index=1)
        self.assertEqual(common, [0, 2])
        self.assertEqual(errs, [0, 0], "a matched stagger leaves no error")

    def test_stagger_scales_with_position(self):
        ref = {0: 100}
        other = {0: 100 + 150}
        errs, _ = T.pairwise_errors(ref, other, stagger_us=50, index=3)
        self.assertEqual(errs, [0])

    def test_no_overlap(self):
        errs, common = T.pairwise_errors({0: 1}, {1: 2}, 0, 1)
        self.assertEqual((errs, common), ([], []))

if __name__ == "__main__":
    unittest.main(verbosity=2)
