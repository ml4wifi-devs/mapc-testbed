#!/usr/bin/env python3
"""cosr.clockgraph: the affine clock algebra.

Two jobs. First, recover a known ground truth from synthetic observations, including through a
composition chain deeper than any two-hop deployment can exercise. Second, stay numerically
identical to the implementation currently producing offsets, so the transition cannot move a
single measured number.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from cosr import clockgraph as CG

R = "02:00:00:00:00:01"      # reference
Y = "02:00:00:00:00:02"
Z = "02:00:00:00:00:03"

# node_tsf = a + b*ref_tsf
TRUE = {R: (0.0, 1.0), Y: (1.0e6, 1.000002), Z: (-2.5e6, 0.999995)}


def as_ref(mac, t_ref):
    a, b = TRUE[mac]
    return a + b * t_ref


class TestAffine(unittest.TestCase):

    def test_compose_then_invert_is_identity(self):
        m = (1234.5, 1.000003)
        a, b = CG.compose(CG.invert(m), m)
        self.assertAlmostEqual(a, 0.0, places=6)
        self.assertAlmostEqual(b, 1.0, places=12)

    def test_compose_matches_substitution(self):
        inner, outer = (10.0, 2.0), (3.0, 5.0)
        a, b = CG.compose(outer, inner)
        for r in (0.0, 7.0, 1e6):
            self.assertAlmostEqual(a + b * r, 3.0 + 5.0 * (10.0 + 2.0 * r), places=6)

    def test_invert_rejects_nothing_but_zero_slope(self):
        self.assertRaises(ZeroDivisionError, CG.invert, (1.0, 0.0))


class TestFit(unittest.TestCase):

    def test_recovers_a_line(self):
        pts = [(x, 5.0 + 2.0 * x) for x in range(10)]
        a, b = CG.fit(pts)
        self.assertAlmostEqual(a, 5.0, places=9)
        self.assertAlmostEqual(b, 2.0, places=9)

    def test_underdetermined(self):
        self.assertIsNone(CG.fit([]))
        self.assertIsNone(CG.fit([(1.0, 2.0)]))
        self.assertIsNone(CG.fit([(1.0, 2.0), (1.0, 3.0)]), "zero x-variance")

    def test_an_exact_line_is_still_not_certain(self):
        """Samples landing exactly on a line say the fit is at least as good as the timestamps
        can show, not that it is perfect."""
        pts = [(x, 5.0 + 2.0 * x) for x in range(10)]
        a, b, se = CG.fit_with_stderr(pts)
        self.assertAlmostEqual(b, 2.0, places=9)
        self.assertGreater(se, 0.0)

    def test_stderr_grows_with_scatter(self):
        clean = [(float(x), 2.0 * x) for x in range(20)]
        noisy = [(float(x), 2.0 * x + (1 if x % 2 else -1)) for x in range(20)]
        self.assertLess(CG.fit_with_stderr(clean)[2], CG.fit_with_stderr(noisy)[2])

    def test_stderr_shrinks_with_a_longer_span(self):
        """The same scatter over a wider span constrains the slope better, which is why the
        sample window is a duration rather than a count."""
        short = [(float(x), 2.0 * x + (1 if x % 2 else -1)) for x in range(10)]
        long_ = [(float(x), 2.0 * x + (1 if x % 2 else -1)) for x in range(0, 100, 10)]
        self.assertLess(CG.fit_with_stderr(long_)[2], CG.fit_with_stderr(short)[2])

    def test_two_samples_have_undefined_uncertainty(self):
        a, b, se = CG.fit_with_stderr([(0.0, 0.0), (1.0, 2.0)])
        self.assertEqual(se, float("inf"))

    def test_stderr_none_when_fit_none(self):
        self.assertIsNone(CG.fit_with_stderr([(1.0, 2.0)]))


class TestAirtime(unittest.TestCase):

    def test_cck_long_preamble(self):
        self.assertAlmostEqual(CG.ppdu_airtime_us(0, 45),
                               192.0 + 360 - CG.AR9271_TS_OFFSET_US, places=6)

    def test_cck_scales_with_length(self):
        d = CG.ppdu_airtime_us(0, 72) - CG.ppdu_airtime_us(0, 45)
        self.assertAlmostEqual(d, 216.0, places=6)

    def test_ofdm(self):
        import math
        rate = CG.RATE_MBPS[4]                      # 6 Mb/s
        expect = (20.0 + 4.0 * math.ceil((16 + 8 * 124 + 6) / (rate * 4))
                  - CG.AR9271_TS_OFFSET_US)
        self.assertAlmostEqual(CG.ppdu_airtime_us(4, 124), expect, places=6)

    def test_short_ofdm_frame_clamps_to_zero(self):
        """The timestamp separation can exceed a short frame's own airtime, in which case the
        correction is floored rather than allowed to move the sample backwards."""
        self.assertEqual(CG.ppdu_airtime_us(6, 45), 0.0)

    def test_unknown_rate_index_contributes_nothing(self):
        self.assertEqual(CG.ppdu_airtime_us(99, 100), 0.0)
        self.assertEqual(CG.ppdu_airtime_us(-1, 100), 0.0)

    def test_never_negative(self):
        self.assertGreaterEqual(CG.ppdu_airtime_us(11, 1), 0.0)


class TestSolve(unittest.TestCase):

    def test_direct_edge(self):
        edges = {(R, Y): (TRUE[Y][0], TRUE[Y][1])}
        maps = CG.solve_offsets(edges, R)
        self.assertAlmostEqual(maps[Y][0], TRUE[Y][0], places=3)
        self.assertAlmostEqual(maps[Y][1], TRUE[Y][1], places=12)

    def test_two_hop_composition(self):
        """A clock reachable only through an intermediary. No two-AP deployment can exercise
        this, and getting the composition order wrong is invisible at one hop."""
        y_of_r = TRUE[Y]
        z_of_y = CG.compose(TRUE[Z], CG.invert(TRUE[Y]))
        maps = CG.solve_offsets({(R, Y): y_of_r, (Y, Z): z_of_y}, R)
        self.assertAlmostEqual(maps[Z][0], TRUE[Z][0], places=2)
        self.assertAlmostEqual(maps[Z][1], TRUE[Z][1], places=12)

    def test_reference_maps_to_itself(self):
        self.assertEqual(CG.solve_offsets({}, R), {R: (0.0, 1.0)})

    def test_unreachable_clock_is_absent(self):
        maps = CG.solve_offsets({(Y, Z): (1.0, 1.0)}, R)
        self.assertNotIn(Z, maps)
        self.assertNotIn(Y, maps)

    def test_edges_traverse_backwards(self):
        maps = CG.solve_offsets({(Y, R): CG.invert(TRUE[Y])}, R)
        self.assertAlmostEqual(maps[Y][1], TRUE[Y][1], places=12)


class TestMonitorEdges(unittest.TestCase):

    def test_station_clock_cancels(self):
        """A station relating two APs needs no duration correction: its own clock drops out."""
        mon = (12345.0, 1.0000011)          # station_tsf = f(ref)
        fits = {}
        for mac in (R, Y, Z):
            fits[mac] = CG.compose(TRUE[mac], CG.invert(mon))
        maps = CG.solve_offsets(CG.monitor_edges(fits), R)
        for mac in (Y, Z):
            self.assertAlmostEqual(maps[mac][0], TRUE[mac][0], places=1)
            self.assertAlmostEqual(maps[mac][1], TRUE[mac][1], places=11)

    def test_two_stations_bridge_on_a_common_ap(self):
        m1, m2 = (1000.0, 1.0000005), (-4000.0, 0.9999992)
        f1 = dict((mac, CG.compose(TRUE[mac], CG.invert(m1))) for mac in (R, Y))
        f2 = dict((mac, CG.compose(TRUE[mac], CG.invert(m2))) for mac in (Y, Z))
        edges = {}
        edges.update(CG.monitor_edges(f1))
        edges.update(CG.monitor_edges(f2))
        maps = CG.solve_offsets(edges, R)
        self.assertAlmostEqual(maps[Z][1], TRUE[Z][1], places=10)

    def test_zero_slope_fit_is_skipped(self):
        self.assertEqual(CG.monitor_edges({R: (0.0, 0.0), Y: (1.0, 1.0)}), {})


class TestOffsetsAndTargets(unittest.TestCase):

    def test_target_is_the_shared_instant_in_local_time(self):
        ref_tsf = 1000000000
        offs = CG.offsets_from_maps(CG.solve_offsets(
            {(R, Y): TRUE[Y], (R, Z): TRUE[Z]}, R), R, ref_tsf)
        shared = ref_tsf + 150000
        for mac in (Y, Z):
            off, slope = offs[mac]
            got = CG.target_tsf(shared, off, slope, ref_tsf)
            self.assertAlmostEqual(got, as_ref(mac, shared), delta=2.0)

    def test_reference_targets_itself(self):
        off, slope = CG.offsets_from_maps({R: (0.0, 1.0)}, R, 5000)[R]
        self.assertEqual((off, slope), (0, 0.0))
        self.assertEqual(CG.target_tsf(9999, off, slope, 5000), 9999)

    def test_stagger_shifts_by_index(self):
        base = CG.target_tsf(1000, 0, 0.0, 0, stagger_us=50, index=0)
        third = CG.target_tsf(1000, 0, 0.0, 0, stagger_us=50, index=3)
        self.assertEqual(third - base, 150)

    def test_shared_instant_error_shifts_all_clocks_together(self):
        """An error in the shared instant delays every AP equally; it does not misalign them.

        The residual spread is the slope-estimate error times the error, not the error times the
        clock-rate difference -- which is why the instant only needs to be accurate enough to
        land inside the gate window.
        """
        ref_tsf = 1000000000
        maps = CG.solve_offsets({(R, Y): TRUE[Y], (R, Z): TRUE[Z]}, R)
        offs = CG.offsets_from_maps(maps, R, ref_tsf)
        eps = 100000          # 100 ms of error in choosing the instant
        shared = ref_tsf + 150000

        spread = []
        for s in (shared, shared + eps):
            fire_ref = {}
            for mac in (Y, Z):
                off, slope = offs[mac]
                local = CG.target_tsf(s, off, slope, ref_tsf)
                a, b = TRUE[mac]
                fire_ref[mac] = (local - a) / b       # back into reference time
            spread.append(abs(fire_ref[Y] - fire_ref[Z]))
        self.assertLess(spread[1], 1.0)
        self.assertLess(abs(spread[1] - spread[0]), 1.0,
                        "misalignment must not grow with the instant's error")


class TestUncertaintyFloor(unittest.TestCase):
    """A fit cannot be more certain than the resolution of what it was fitted to."""

    def test_an_exact_fit_still_carries_uncertainty(self):
        samples = [(i * 100000, i * 100000 + 7) for i in range(10)]
        _a, _b, se = CG.fit_with_stderr(samples)
        self.assertGreater(se, 0.0,
                           "zero uncertainty would license extrapolating without limit")

    def test_the_floor_tightens_as_the_samples_span_more_time(self):
        """A longer baseline pins the slope better, so the floor must fall with it."""
        short = CG.fit_with_stderr([(i * 100000, i * 100000) for i in range(10)])[2]
        long = CG.fit_with_stderr([(i * 1000000, i * 1000000) for i in range(10)])[2]
        self.assertLess(long, short)

    def test_real_scatter_still_dominates_when_it_is_larger(self):
        noisy = [(i * 100000, i * 100000 + (500 if i % 2 else -500)) for i in range(10)]
        clean = [(i * 100000, i * 100000) for i in range(10)]
        self.assertGreater(CG.fit_with_stderr(noisy)[2], CG.fit_with_stderr(clean)[2] * 10)

if __name__ == "__main__":
    unittest.main(verbosity=2)
