#!/usr/bin/env python3
"""Unit tests for the shared clock-graph core and both edge sources.

A 3-AP scenario with known true clock maps checks: the affine compose/invert; that the OTA
beacon PPDU-airtime formula matches hand-computed 802.11 timing; that beacon edges recover
truth once each beacon's computed airtime is subtracted from t2 (and are wrong if it is not);
that multi-monitor edges recover truth WITHOUT any airtime constant (airtime cancels in the
monitor elimination); and that a two-hop composition (reachable only through an intermediate
AP) recovers the true map. Composition depth > 1 cannot be built on the 4-VM testbed, so it is
checked here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import clock_graph as cg
import beacon_tracker as bt
import monitor_tracker as mt

# true clock maps relative to reference R:  clock = A + B * ref_tsf
R = "02:00:00:00:00:01"
Y = "02:00:00:00:00:02"
Z = "02:00:00:00:00:03"
TRUE = {R: (0.0, 1.0), Y: (1_000_000.0, 1.000002), Z: (-2_500_000.0, 0.999995)}
# a beacon at 1 Mbps CCK, 45-byte MPDU: its PPDU on-air time is what a real RX-END mactime
# folds into t2, and exactly what the tracker computes and subtracts from its rate_idx+len.
BRATE, BLEN = 0, 45
D = bt.ppdu_airtime_us(BRATE, BLEN)     # the airtime the tracker subtracts (TSF us)


def clock(mac, ref_tsf):
    a, b = TRUE[mac]
    return a + b * ref_tsf


# --- beacon edge: x=t2 (hearer RX-end = clock+airtime), y=t1 (sender TX). The tracker
# subtracts the computed PPDU airtime from t2; `subtract=False` models forgetting to. ---
def beacon_edge(hearer, sender, ref_start, n=20, step=102400, subtract=True):
    samples = []
    for i in range(n):
        r = ref_start + i * step
        t2 = clock(hearer, r) + D                              # RX-end includes the airtime
        if subtract:
            t2 -= bt.ppdu_airtime_us(BRATE, BLEN)              # tracker's correction
        samples.append((t2, clock(sender, r)))                # (x=t2_corrected, y=t1)
    return cg.fit(samples)


# --- monitor fit: a monitor with its OWN clock hears APs; ap_tsf = a + b*mon_tsft ---
def monitor_fits(macs, mon_a, mon_b, ref_start, n=20, step=102400):
    """Return {mac: (a, b)} as offset_tracker --raw would emit for a monitor whose clock is
    mon_tsft = mon_a + mon_b*ref_tsf. Each beacon is stamped at RX-end (+D in the mon clock),
    exactly like the real capture, so the test also proves D cancels in monitor_edges."""
    fits = {}
    for m in macs:
        samples = []                                   # (x=mon_tsft, y=ap_tsf)
        for i in range(n):
            r = ref_start + i * step
            mon = mon_a + mon_b * r + D                # monitor RX-end stamp
            samples.append((mon, clock(m, r)))
        fits[m] = cg.fit(samples)
    return fits


class TestAffine(unittest.TestCase):
    def test_compose_inverse_identity(self):
        a, b = cg.compose((1234.5, 1.000003), cg.invert((1234.5, 1.000003)))
        self.assertAlmostEqual(a, 0.0, places=6)
        self.assertAlmostEqual(b, 1.0, places=12)

    def test_compose_matches_manual(self):
        # u = 10 + 2r ; v = 3 + 5u  => v = 53 + 10r
        self.assertEqual(cg.compose((3.0, 5.0), (10.0, 2.0)), (53.0, 10.0))


class TestBeaconAirtime(unittest.TestCase):
    def test_ppdu_airtime_matches_802_11(self):
        # CCK 1 Mbps, 45 B: 192 us preamble+header + 45*8 us PSDU - 61 us AR9271 const
        self.assertEqual(bt.ppdu_airtime_us(0, 45), 192 + 360 - 61)
        # length term tracks exactly: +27 bytes at 1 Mbps = +216 us
        self.assertEqual(bt.ppdu_airtime_us(0, 72) - bt.ppdu_airtime_us(0, 45), 216)
        # OFDM 6 Mbps, 45 B: 20 + 4*ceil((16+360+6)/24) - 61
        import math
        self.assertEqual(bt.ppdu_airtime_us(4, 45), 20 + 4 * math.ceil(382 / 24) - 61)
        self.assertEqual(bt.ppdu_airtime_us(99, 45), 0.0)          # unknown rate_idx -> no correction


class TestBeaconEdges(unittest.TestCase):
    def _chain(self, subtract=True):
        # R hears Y, Y hears Z -- Z reachable only via R->Y->Z (depth-2 composition)
        return {(R, Y): beacon_edge(R, Y, 5_000_000, subtract=subtract),
                (Y, Z): beacon_edge(Y, Z, 5_000_000, subtract=subtract)}

    def test_direct_edge_recovers_truth(self):
        maps = cg.solve_offsets(self._chain(), R)
        self.assertAlmostEqual(maps[Y][0], TRUE[Y][0], places=1)
        self.assertAlmostEqual(maps[Y][1], TRUE[Y][1], places=9)

    def test_two_hop_chain_recovers_truth(self):
        maps = cg.solve_offsets(self._chain(), R)
        self.assertAlmostEqual(maps[Z][0], TRUE[Z][0], places=1)   # reached only via R->Y->Z
        self.assertAlmostEqual(maps[Z][1], TRUE[Z][1], places=9)

    def test_unsubtracted_airtime_biases_offset(self):
        # forgetting to subtract the airtime must leave a visible per-hop offset error ~ D
        maps = cg.solve_offsets(self._chain(subtract=False), R)
        self.assertGreater(abs(maps[Y][0] - TRUE[Y][0]), 100.0)


class TestMonitorEdges(unittest.TestCase):
    def test_single_monitor_hears_all_no_airtime_constant(self):
        # one monitor hears R, Y, Z; monitor_edges must recover truth with NO D correction
        fits = monitor_fits([R, Y, Z], 7_000_000.0, 1.0000015, 5_000_000)
        maps = cg.solve_offsets(mt.monitor_edges(fits), R)
        for mac in (Y, Z):
            self.assertAlmostEqual(maps[mac][0], TRUE[mac][0], places=0)
            self.assertAlmostEqual(maps[mac][1], TRUE[mac][1], places=8)

    def test_two_monitors_bridge_on_common_ap(self):
        # monitor A hears R,Y ; monitor B hears Y,Z. Only Y is common -> it bridges the two
        # monitors' clock axes so Z is reachable from R. Different monitor clocks on purpose.
        fa = monitor_fits([R, Y], 3_000_000.0, 1.0000009, 5_000_000)
        fb = monitor_fits([Y, Z], -9_000_000.0, 0.9999988, 5_000_000)
        edges = {}
        edges.update(mt.monitor_edges(fa))
        edges.update(mt.monitor_edges(fb))
        maps = cg.solve_offsets(edges, R)
        self.assertIn(Z, maps)                                   # bridged R->Y (monA), Y->Z (monB)
        self.assertAlmostEqual(maps[Z][0], TRUE[Z][0], places=0)
        self.assertAlmostEqual(maps[Z][1], TRUE[Z][1], places=8)

    def test_disconnected_ap_absent(self):
        # monitor hears only R and Y; Z is heard by nobody -> absent from the solved maps
        fits = monitor_fits([R, Y], 2_000_000.0, 1.0, 5_000_000)
        maps = cg.solve_offsets(mt.monitor_edges(fits), R)
        self.assertNotIn(Z, maps)


class TestControllerConsumption(unittest.TestCase):
    """offset.json a tracker emits must, through the controller's target formula, hit each
    AP's true clock -- exercised at an instant AWAY from ref_tsf so the slope term matters."""
    def _maps(self):
        fits = monitor_fits([R, Y, Z], 7_000_000.0, 1.0000015, 5_000_000)
        return cg.solve_offsets(mt.monitor_edges(fits), R)

    def test_target_hits_true_clock_with_slope(self):
        maps = self._maps()
        ref_tsf = 6_000_000
        shared = ref_tsf + 50_000_000                 # 50 s later on the reference clock
        for mac, (A, B) in maps.items():
            off = 0 if mac == R else round(A + (B - 1.0) * ref_tsf)
            slope = 0.0 if mac == R else round(B - 1.0, 9)
            target = shared + off + slope * (shared - ref_tsf)
            self.assertAlmostEqual(target, clock(mac, shared), delta=3)
            if mac == Z:
                self.assertGreater(abs(slope * (shared - ref_tsf)), 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
