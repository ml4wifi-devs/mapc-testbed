#!/usr/bin/env python3
"""cosr.clockd: the live clock graph and the checks that gate firing on it.

Synthetic clocks with known parameters, so a recovered relation can be compared against ground
truth rather than against itself.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import clockd as CD
from cosr import clockgraph as cg

REF = "02:00:00:00:00:01"
AP2 = "02:00:00:00:00:02"
AP3 = "02:00:00:00:00:03"
STA = "02:00:00:00:00:aa"

TRUE = {REF: (0.0, 1.0), AP2: (1.0e6, 1.000002), AP3: (-2.5e6, 0.999995)}
MON = (7.5e5, 1.0000011)

BEACON_RATE_IDX = 0
BEACON_LEN = 124
AIRTIME = cg.ppdu_airtime_us(BEACON_RATE_IDX, BEACON_LEN)


def at(mac, t_ref):
    a, b = TRUE[mac]
    return a + b * t_ref


def mon_at(t_ref):
    return MON[0] + MON[1] * t_ref


class FakeClock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def peer_stream(d, hearer, sender, t0=1000000000, n=20, step=100000):
    """`sender` beacons; `hearer` receives. Both times are that node's own clock."""
    for i in range(n):
        t_ref = t0 + i * step
        d.observe_peer_beacon(hearer, sender,
                              t1=int(at(sender, t_ref)),
                              t2=int(at(hearer, t_ref) + AIRTIME),
                              rate_idx=BEACON_RATE_IDX, length=BEACON_LEN)


def station_stream(d, station, senders, t0=1000000000, n=20, step=100000):
    for i in range(n):
        t_ref = t0 + i * step
        for sender in senders:
            d.observe_station_beacon(station, sender,
                                     station_time=int(mon_at(t_ref)),
                                     t1=int(at(sender, t_ref)))


class TestPeerMode(unittest.TestCase):
    """APs hearing each other."""

    def test_recovers_the_relation(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        offsets, ref_tsf = d.solve()
        self.assertIn(AP2, offsets)
        off, slope, _se = offsets[AP2]
        shared = ref_tsf + 150000
        self.assertAlmostEqual(cg.target_tsf(shared, off, slope, ref_tsf),
                               at(AP2, shared), delta=3.0)

    def test_reference_maps_to_itself(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        off, slope, se = d.solve()[0][REF]
        self.assertEqual((off, slope, se), (0, 0.0, 0.0))

    def test_two_hop_graph(self):
        """AP3 is only heard by AP2, never by the reference."""
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        peer_stream(d, AP2, AP3)
        offsets, ref_tsf = d.solve()
        shared = ref_tsf + 150000
        off, slope, _se = offsets[AP3]
        self.assertAlmostEqual(cg.target_tsf(shared, off, slope, ref_tsf),
                               at(AP3, shared), delta=5.0)

    def test_airtime_correction_matters(self):
        """Omitting the frame's own duration biases the offset by hundreds of microseconds."""
        d = CD.ClockD(REF)
        for i in range(20):
            t_ref = 1000000000 + i * 100000
            d.observe_peer_beacon(REF, AP2, t1=int(at(AP2, t_ref)),
                                  t2=int(at(REF, t_ref) + AIRTIME),
                                  rate_idx=BEACON_RATE_IDX, length=BEACON_LEN)
        good = d.solve()[0][AP2][0]

        # Identical observations; only the correction is withheld (an unrecognised rate index
        # contributes nothing), so the difference is exactly the bias it removes.
        d2 = CD.ClockD(REF)
        for i in range(20):
            t_ref = 1000000000 + i * 100000
            d2.observe_peer_beacon(REF, AP2, t1=int(at(AP2, t_ref)),
                                   t2=int(at(REF, t_ref) + AIRTIME),
                                   rate_idx=99, length=BEACON_LEN)
        bad = d2.solve()[0][AP2][0]
        self.assertAlmostEqual(abs(good - bad), AIRTIME, delta=2.0)
        self.assertGreater(AIRTIME, 100.0, "the bias this removes is not negligible")


class TestStationMode(unittest.TestCase):
    """A station hearing several APs, with its own clock cancelling out."""

    def test_recovers_the_relation(self):
        d = CD.ClockD(REF)
        station_stream(d, STA, [REF, AP2, AP3])
        offsets, ref_tsf = d.solve()
        shared = ref_tsf + 150000
        for mac in (AP2, AP3):
            off, slope, _se = offsets[mac]
            self.assertAlmostEqual(cg.target_tsf(shared, off, slope, ref_tsf),
                                   at(mac, shared), delta=5.0)

    def test_needs_no_airtime_constant(self):
        """The station path is insensitive to the duration correction entirely."""
        d = CD.ClockD(REF)
        station_stream(d, STA, [REF, AP2])
        self.assertIn(AP2, d.solve()[0])

    def test_two_stations_bridge(self):
        d = CD.ClockD(REF)
        station_stream(d, STA, [REF, AP2])
        station_stream(d, "02:00:00:00:00:bb", [AP2, AP3])
        self.assertIn(AP3, d.solve()[0])

    def test_both_sources_combine(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        station_stream(d, STA, [AP2, AP3])
        offsets, _ref = d.solve()
        self.assertIn(AP2, offsets)
        self.assertIn(AP3, offsets)


class TestWindowing(unittest.TestCase):

    def test_window_is_a_duration_not_a_count(self):
        """Doubling the observation rate must not change the slope, only the sample count.

        A count-bounded window would shrink the span as the rate rises and degrade the slope,
        which then multiplies the extrapolation error.
        """
        def slope_at(step, n):
            d = CD.ClockD(REF)
            peer_stream(d, REF, AP2, n=n, step=step)
            return d.solve()[0][AP2][1]

        slow = slope_at(step=1000000, n=20)
        fast = slope_at(step=200000, n=100)
        self.assertAlmostEqual(slow, fast, places=6)

    def test_samples_outside_the_span_are_dropped(self):
        d = CD.ClockD(REF, window_us=1000000)
        peer_stream(d, REF, AP2, n=40, step=100000)
        s = d._peer[(REF, AP2)]
        self.assertLessEqual(s.span_us(), 1000000)
        self.assertGreaterEqual(len(s.samples), CD.MIN_SAMPLES)

    def test_clock_restart_discards_the_window(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2, n=20)
        self.assertGreater(len(d._peer[(REF, AP2)].samples), 5)
        d.observe_peer_beacon(REF, AP2, t1=1, t2=1,
                              rate_idx=BEACON_RATE_IDX, length=BEACON_LEN)
        self.assertEqual(len(d._peer[(REF, AP2)].samples), 1,
                         "a backwards jump means the clock restarted")

    def test_a_large_forward_gap_also_discards(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2, n=20)
        last = d._peer[(REF, AP2)].last_x
        d.observe_peer_beacon(REF, AP2, t1=0,
                             t2=int(last + cg.MAX_GAP_US + 2 * AIRTIME + 10),
                             rate_idx=BEACON_RATE_IDX, length=BEACON_LEN)
        self.assertEqual(len(d._peer[(REF, AP2)].samples), 1)

    def test_repeated_identical_observation_ignored(self):
        d = CD.ClockD(REF)
        for _ in range(10):
            d.observe_peer_beacon(REF, AP2, t1=5, t2=int(1000 + AIRTIME),
                                  rate_idx=BEACON_RATE_IDX, length=BEACON_LEN)
        self.assertEqual(len(d._peer[(REF, AP2)].samples), 1)

    def test_too_few_samples_yields_no_edge(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2, n=2)
        self.assertEqual(d.edges()[0], {})


class TestAnchor(unittest.TestCase):

    def test_instant_is_ahead_of_the_reference(self):
        clk = FakeClock()
        d = CD.ClockD(REF, clock=clk)
        peer_stream(d, REF, AP2)
        _offsets, ref_tsf = d.solve()
        s = d.shared_instant(150000)
        self.assertGreater(s, ref_tsf)
        self.assertLess(s - ref_tsf, 1000000)

    def test_instant_accounts_for_elapsed_time(self):
        clk = FakeClock()
        d = CD.ClockD(REF, clock=clk)
        peer_stream(d, REF, AP2)
        first = d.shared_instant(150000)
        clk.advance(1.0)
        second = d.shared_instant(150000)
        self.assertAlmostEqual(second - first, 1000000, delta=2000)

    def test_stale_anchor_refuses(self):
        clk = FakeClock()
        d = CD.ClockD(REF, clock=clk, anchor_max_age_s=2.0)
        peer_stream(d, REF, AP2)
        clk.advance(10.0)
        self.assertRaises(CD.ClockNotReady, d.shared_instant, 150000)

    def test_no_observations_refuses(self):
        d = CD.ClockD(REF)
        self.assertRaises(CD.ClockNotReady, d.solve)
        self.assertRaises(CD.ClockNotReady, d.shared_instant, 150000)

    def test_anchor_from_a_station_observation(self):
        d = CD.ClockD(REF)
        station_stream(d, STA, [REF, AP2])
        self.assertIsNotNone(d.anchor_age_s())
        self.assertIn(AP2, d.solve()[0])

    def test_anchor_takes_the_newest_reading(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2, t0=1000000000, n=10)
        first = d.solve()[1]
        peer_stream(d, REF, AP2, t0=2000000000, n=10)
        self.assertGreater(d.solve()[1], first)


class TestTargets(unittest.TestCase):

    def test_all_participants_land_on_the_same_instant(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        peer_stream(d, REF, AP3)
        shared = d.shared_instant(150000)
        targets = d.targets([REF, AP2, AP3], shared)
        for mac, target in targets.items():
            a, b = TRUE[mac]
            self.assertAlmostEqual((target - a) / b, shared, delta=5.0)

    def test_stagger_offsets_by_index(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        shared = d.shared_instant(150000)
        plain = d.targets([REF, AP2], shared)
        stag = d.targets([REF, AP2], shared, stagger_us=50000)
        self.assertEqual(stag[REF], plain[REF])
        self.assertEqual(stag[AP2] - plain[AP2], 50000)

    def test_unreachable_clock_refuses(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        with self.assertRaises(CD.ClockNotReady) as cm:
            d.targets([REF, AP2, AP3], d.shared_instant(150000))
        self.assertIn("no path", str(cm.exception))

    def test_uncertain_relation_refuses(self):
        """Extrapolating far beyond noisy samples must refuse rather than fire on a guess."""
        d = CD.ClockD(REF, budget_us=0.001)
        for i in range(20):
            t_ref = 1000000000 + i * 100000
            jitter = 40 if i % 2 else -40
            d.observe_peer_beacon(REF, AP2, t1=int(at(AP2, t_ref)) + jitter,
                                 t2=int(at(REF, t_ref) + AIRTIME),
                                 rate_idx=BEACON_RATE_IDX, length=BEACON_LEN)
        _offsets, ref_tsf = d.solve()
        far = ref_tsf + 500000
        with self.assertRaises(CD.ClockTooUncertain) as cm:
            d.targets([REF, AP2], far)
        self.assertIn("budget", str(cm.exception))

    def test_clean_samples_pass_the_budget(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2, n=40)
        d.targets([REF, AP2], d.shared_instant(150000))


class TestInstantErrorDoesNotMisalign(unittest.TestCase):
    """Choosing the instant slightly wrong delays everyone equally.

    This is what allows the anchor to be coarse and keeps any clock interrogation off the
    critical path. If it were false, the anchor's accuracy would bound synchronisation.
    """

    def test_spread_is_insensitive_to_the_instant(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2, n=40)
        peer_stream(d, REF, AP3, n=40)
        base = d.shared_instant(150000)

        def spread(shared):
            targets = d.targets([REF, AP2, AP3], shared)
            in_ref = []
            for mac, target in targets.items():
                a, b = TRUE[mac]
                in_ref.append((target - a) / b)
            return max(in_ref) - min(in_ref)

        tight = spread(base)
        with_error = spread(base + 100000)          # 100 ms out
        self.assertLess(tight, 5.0)
        self.assertLess(with_error, 5.0)
        self.assertLess(abs(with_error - tight), 5.0)


class TestStatus(unittest.TestCase):

    def test_reports_the_graph(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        st = d.status()
        self.assertEqual(st["reference"], REF)
        self.assertIn(AP2, st["reachable"])
        self.assertTrue(st["edges"])
        self.assertIsNotNone(st["ref_tsf"])
        self.assertIsNone(st["error"])

    def test_reports_not_ready(self):
        st = CD.ClockD(REF).status()
        self.assertEqual(st["reachable"], [])
        self.assertIsNotNone(st["error"])

    def test_reports_sample_spans(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2, n=10, step=100000)
        self.assertTrue(any(v > 0 for v in d.status()["sample_span_us"].values()))


class TestParticipantFilter(unittest.TestCase):
    """Neighbouring networks are heard constantly; admitting them would add clocks that are not
    part of the measurement and can form unsupported paths between the ones that are."""

    FOREIGN = "aa:bb:cc:dd:ee:ff"

    def test_foreign_sender_is_ignored(self):
        d = CD.ClockD(REF, participants=[REF, AP2])
        peer_stream(d, REF, AP2)
        for i in range(20):
            t = 1000000000 + i * 100000
            d.observe_peer_beacon(REF, self.FOREIGN, t1=int(t), t2=int(t + 1200),
                                  rate_idx=0, length=124)
        self.assertEqual(sorted(d.solve()[0]), sorted([REF, AP2]))

    def test_foreign_hearer_is_ignored(self):
        d = CD.ClockD(REF, participants=[REF, AP2])
        for i in range(20):
            t = 1000000000 + i * 100000
            d.observe_peer_beacon(self.FOREIGN, AP2, t1=int(t), t2=int(t + 1200),
                                  rate_idx=0, length=124)
        self.assertNotIn(self.FOREIGN, d.edges()[0])

    def test_foreign_sender_ignored_on_the_station_path(self):
        d = CD.ClockD(REF, participants=[REF, AP2])
        station_stream(d, STA, [REF, AP2])
        for i in range(20):
            t = 1000000000 + i * 100000
            d.observe_station_beacon(STA, self.FOREIGN, station_time=int(mon_at(t)),
                                     t1=int(t))
        self.assertEqual(sorted(d.solve()[0]), sorted([REF, AP2]))

    def test_no_filter_admits_everything(self):
        d = CD.ClockD(REF)
        peer_stream(d, REF, AP2)
        for i in range(20):
            t = 1000000000 + i * 100000
            d.observe_peer_beacon(REF, self.FOREIGN, t1=int(t * 1.000001),
                                  t2=int(t + 1200), rate_idx=0, length=124)
        self.assertIn(self.FOREIGN, d.solve()[0])


class TestPathDisagreement(unittest.TestCase):
    """A contradicting edge is invisible to a single traversal: it simply becomes the answer
    wherever the traversal happened to use it."""

    A = "02:00:00:00:00:01"
    B = "02:00:00:00:00:02"
    C = "02:00:00:00:00:03"

    def _feed(self, cd, hearer, sender, offset_us, slope=1.0, n=30):
        """`offset_us` is the sender's clock minus the hearer's. The receive stamp trails the
        transmit instant by the frame's own duration, which is what the ingest corrects for."""
        air = cg.ppdu_airtime_us(0, 124)
        for i in range(n):
            t = 2000000000 + i * 100000
            cd.observe_peer_beacon(hearer, sender,
                                   t1=int(t * slope + offset_us), t2=int(t + air), rate_idx=0,
                                   length=124)

    def _graph(self, ac_offset):
        cd = CD.ClockD(self.A, participants=[self.A, self.B, self.C])
        self._feed(cd, self.B, self.A, 0)
        self._feed(cd, self.A, self.B, 0)
        self._feed(cd, self.C, self.B, 500000)
        self._feed(cd, self.A, self.C, ac_offset)
        return cd.path_disagreement()

    def test_a_single_route_is_reported_as_uncorroborated(self):
        """Nothing contradicts a lone route, and that is not the same as it being right."""
        cd = CD.ClockD(self.A, participants=[self.A, self.B])
        self._feed(cd, self.B, self.A, 0)
        got = cd.path_disagreement()
        self.assertFalse(got[self.B]["redundant"])
        self.assertIsNone(got[self.B]["max_us"],
                          "no number may be offered for a route nothing corroborates")

    def test_consistent_routes_agree(self):
        got = self._graph(ac_offset=-500000)
        self.assertTrue(got[self.C]["redundant"])
        self.assertLess(got[self.C]["max_us"], 2.0)

    def test_a_contradicting_edge_is_exposed(self):
        """The direct and the indirect route to C disagree by the amount the edge is wrong by."""
        got = self._graph(ac_offset=-500000 + 900)
        self.assertTrue(got[self.C]["redundant"])
        self.assertGreater(got[self.C]["max_us"], 500.0)

    def test_refuses_without_an_anchor(self):
        cd = CD.ClockD(self.A, participants=[self.A, self.B])
        self.assertRaises(CD.ClockNotReady, cd.path_disagreement)


class TestAnchorFromAnyTraffic(unittest.TestCase):
    """The reference reading and the edges between clocks are separate needs."""

    REF = "02:00:00:00:00:01"
    PEER = "02:00:00:00:00:02"
    FOREIGN = "9a:1a:35:e9:70:9c"

    def _cd(self):
        return CD.ClockD(self.REF, participants=[self.REF, self.PEER])

    def test_a_lone_transmitter_can_still_place_an_instant(self):
        """Measuring one link is legitimate, and such a deployment has no peer to hear."""
        cd = self._cd()
        for i in range(5):
            cd.observe_peer_beacon(self.REF, self.FOREIGN, t1=999 + i,
                                   t2=5000000 + i * 100000, rate_idx=0, length=124)
        self.assertIsNotNone(cd.shared_instant(150000))

    def test_outsiders_still_contribute_no_edge(self):
        """Admitting them would add clocks that are not participating and invent paths between
        participants that nothing measured."""
        cd = self._cd()
        for i in range(5):
            cd.observe_peer_beacon(self.REF, self.FOREIGN, t1=999 + i,
                                   t2=5000000 + i * 100000, rate_idx=0, length=124)
        edges, _stderr = cd.edges()
        self.assertEqual(edges, {})
        self.assertEqual(cd.status()["reachable"], [self.REF])

    def test_a_reading_of_another_clock_is_not_taken_as_the_reference(self):
        cd = self._cd()
        for i in range(5):
            cd.observe_peer_beacon(self.PEER, self.FOREIGN, t1=999 + i,
                                   t2=7000000 + i * 100000, rate_idx=0, length=124)
        self.assertRaises(CD.ClockNotReady, cd.shared_instant, 150000)


class TestClockReset(unittest.TestCase):
    """A clock that is reset must not be fitted across, and must not wedge the anchor."""

    def _feed(self, cd, hearer, sender, n, t0, offset, step=100000):
        air = cg.ppdu_airtime_us(0, 124)
        for i in range(n):
            t = t0 + i * step
            cd.observe_peer_beacon(hearer, sender, t1=int(t + offset),
                                   t2=int(t + air), rate_idx=0, length=124)

    def test_a_step_in_the_relation_discards_the_window(self):
        """Fitting a line through a step yields a confident and entirely wrong relation."""
        cd = CD.ClockD(REF, participants=[REF, AP2])
        self._feed(cd, REF, AP2, n=10, t0=1000000000, offset=0)
        before = len(cd._peer[(REF, AP2)].samples)
        self.assertGreaterEqual(before, 5)
        # the sender's clock jumps a second while the hearer's advances normally
        self._feed(cd, REF, AP2, n=1, t0=1000000000 + 10 * 100000, offset=1000000)
        self.assertEqual(len(cd._peer[(REF, AP2)].samples), 1)

    def test_ordinary_drift_is_not_mistaken_for_a_reset(self):
        cd = CD.ClockD(REF, participants=[REF, AP2])
        air = cg.ppdu_airtime_us(0, 124)
        for i in range(20):
            t = 1000000000 + i * 100000
            cd.observe_peer_beacon(REF, AP2, t1=int(t * 1.000002),
                                   t2=int(t + air), rate_idx=0, length=124)
        self.assertGreaterEqual(len(cd._peer[(REF, AP2)].samples), 15)

    def test_a_reference_reset_does_not_wedge_the_anchor_for_ever(self):
        """Keeping only the highest reading would hold a pre-reset value permanently, and every
        later round would be refused with nothing able to recover it."""
        clock = FakeClock()
        cd = CD.ClockD(REF, participants=[REF, AP2], clock=clock)
        cd.observe_peer_beacon(AP2, REF, t1=9000000000, t2=1000, rate_idx=0, length=124)
        self.assertEqual(cd._anchor[0], 9000000000)

        clock.advance(cd.anchor_max_age_s + 1.0)
        cd.observe_peer_beacon(AP2, REF, t1=5000, t2=2000, rate_idx=0, length=124)
        self.assertEqual(cd._anchor[0], 5000, "the anchor must follow a clock that was reset")
        self.assertLess(cd.anchor_age_s(), 1.0)

    def test_an_out_of_order_reading_is_still_ignored(self):
        clock = FakeClock()
        cd = CD.ClockD(REF, participants=[REF, AP2], clock=clock)
        cd.observe_peer_beacon(AP2, REF, t1=9000000000, t2=1000, rate_idx=0, length=124)
        cd.observe_peer_beacon(AP2, REF, t1=8999999000, t2=1100, rate_idx=0, length=124)
        self.assertEqual(cd._anchor[0], 9000000000)

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestObservationSources(unittest.TestCase):
    """Both kinds of observation feed one graph, and which arrived is reportable."""

    def test_a_receiver_relates_two_transmitters_without_hearing_either_itself(self):
        """Relating two of one receiver's own fits cancels its clock, so no correction for the
        frame's own duration applies on this path at all."""
        d = CD.ClockD(REF, participants=[REF, AP2])
        station_stream(d, STA, [REF, AP2])
        offsets, ref_tsf = d.solve()
        shared = ref_tsf + 150000
        off, slope, _se = offsets[AP2]
        self.assertAlmostEqual(cg.target_tsf(shared, off, slope, ref_tsf),
                               at(AP2, shared), delta=5.0)

    def test_the_source_in_use_is_reported(self):
        d = CD.ClockD(REF, participants=[REF, AP2])
        self.assertEqual(d.status()["sources"], [])
        station_stream(d, STA, [REF, AP2])
        self.assertEqual(d.status()["sources"], ["monitor"])

    def test_a_transmitter_source_is_named_separately(self):
        d = CD.ClockD(REF, participants=[REF, AP2])
        peer_stream(d, REF, AP2)
        self.assertEqual(d.status()["sources"], ["beacon"])

    def test_both_at_once(self):
        d = CD.ClockD(REF, participants=[REF, AP2])
        peer_stream(d, REF, AP2)
        station_stream(d, STA, [REF, AP2])
        self.assertEqual(d.status()["sources"], ["beacon", "monitor"])

    def test_a_receiver_can_anchor_the_reference(self):
        """With no transmitter hearing another, the reference reading has to come from a
        receiver or nothing can be commanded at all."""
        d = CD.ClockD(REF, participants=[REF, AP2])
        station_stream(d, STA, [REF, AP2])
        self.assertIsNotNone(d.shared_instant(150000))


class TestRedeliveredObservations(unittest.TestCase):
    """The ring has no per-reader cursor, so already-forwarded rows arrive again."""

    def test_an_older_sample_is_ignored_not_treated_as_a_restart(self):
        cd = CD.ClockD(REF, participants=[REF, AP2])
        peer_stream(cd, REF, AP2, n=20)
        before = len(cd._peer[(REF, AP2)].samples)
        self.assertGreater(before, 5)
        peer_stream(cd, REF, AP2, n=3)          # replay of rows already seen
        self.assertGreaterEqual(len(cd._peer[(REF, AP2)].samples), before - 1,
                                "a redundant sample must not discard the whole fit")

    def test_a_genuine_restart_still_discards_the_window(self):
        cd = CD.ClockD(REF, participants=[REF, AP2])
        peer_stream(cd, REF, AP2, n=20)
        cd.observe_peer_beacon(REF, AP2, t1=1, t2=1, rate_idx=0, length=124)
        self.assertEqual(len(cd._peer[(REF, AP2)].samples), 1)
