#!/usr/bin/env python3
"""cosr.accounting: joint-shot denominators and explicit per-link status.

Two properties matter here. A shot that not every participant fired must not enter the
denominator, and a link that could not be measured must never present a delivery figure.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import accounting as A
from cosr import topo as T
from cosr import wire as W

AP1 = "02:00:00:00:00:11"
AP2 = "02:00:00:00:00:12"

NODES = {
    "apA":  {"role": "ap", "ip": "10.0.0.11", "iface": "wlan0", "mac": AP1},
    "apB":  {"role": "ap", "ip": "10.0.0.12", "iface": "wlan0", "mac": AP2},
    "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "wlan0", "station_id": 1},
    "sta2": {"role": "station", "ip": "10.0.0.22", "iface": "wlan0", "station_id": 2},
}


def plan(nframes=1, frame_len=200, links=None, channel_freq=None):
    t = {"channel": 1, "observer": "sta1", "nodes": NODES}
    s = {"nframes": nframes, "frame_len": frame_len, "mcs": 4,
         "links": links or [{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20},
                            {"ap": "apB", "station": "sta2", "mcs": 0, "txpower_dbm": 20}]}
    p = T.resolve(t, s)
    if channel_freq:
        p["channel_freq_mhz"] = channel_freq
    return p


def status(fired, misses=None, error=None):
    """Per-shot outcomes: `fired` seqs plus optional {seq: code} misses."""
    shots = dict((s, A.FIRED) for s in fired)
    shots.update(misses or {})
    return {"shots": shots, "epoch": "e1", "error": error}


def report(mac_rx, other=5, drops=0, coverage="full", freq=2412, idx_hist=None, seqs=None):
    per_ap = {}
    for mac, rx in mac_rx.items():
        seen = seqs if seqs is not None else list(range(rx))
        n_each = (rx // len(seen)) if seen else 0
        per_ap[mac] = {"rx": rx, "coverage": coverage,
                       "idx_hist": idx_hist if idx_hist is not None else ({0: rx} if rx else {}),
                       "seqs_seen": seen,
                       "per_seq": dict((s, n_each) for s in seen),
                       "rssi_dbm": -56.0 if rx else None}
    return {"run": "r", "epoch": "e1", "ordinal": 100, "other_frames": other,
            "other_frames_round": other, "drops": drops, "freq_mhz": freq, "per_ap": per_ap}


class TestJointSeqs(unittest.TestCase):

    def test_all_fired(self):
        st = {"apA": status([0, 1, 2]), "apB": status([0, 1, 2])}
        self.assertEqual(A.joint_seqs(st), set([0, 1, 2]))

    def test_intersection_only(self):
        st = {"apA": status([0, 1, 2, 3]), "apB": status([0, 2])}
        self.assertEqual(A.joint_seqs(st), set([0, 2]))

    def test_disjoint_gives_empty(self):
        st = {"apA": status([0, 1]), "apB": status([2, 3])}
        self.assertEqual(A.joint_seqs(st), set())

    def test_missing_ap_gives_empty(self):
        st = {"apA": status([0, 1])}
        self.assertEqual(A.joint_seqs(st, ["apA", "apB"]), set())

    def test_non_fired_codes_excluded(self):
        st = {"apA": status([0], misses={1: A.LATE, 2: A.TOOFAR, 3: A.NOBF}),
              "apB": status([0, 1, 2, 3])}
        self.assertEqual(A.joint_seqs(st), set([0]))

    def test_no_participants(self):
        self.assertEqual(A.joint_seqs({}), set())


class TestDenominator(unittest.TestCase):
    """A shot only one AP fired is a different experiment and must be excluded."""

    def test_partial_fire_shrinks_the_denominator(self):
        p = plan()
        st = {"apA": status([0, 1, 2, 3, 4]),
              "apB": status([0, 1, 2], misses={3: A.TOOFAR, 4: A.TOOFAR})}
        rep = {"sta1": report({AP1: 3}, seqs=[0, 1, 2]),
               "sta2": report({AP2: 3}, seqs=[0, 1, 2])}
        links, warns = A.link_results(p, st, rep, 0, 5)
        a = links["apA->sta1"]
        self.assertEqual(a["joint_shots"], 3)
        self.assertEqual(a["sent"], 3, "not 5 -- two shots were not concurrent")
        self.assertEqual(a["delivery"], 1.0)
        self.assertEqual(a["status"], A.OK)

    def test_frames_from_non_concurrent_shots_are_excluded(self):
        """A receiver answers about the whole commanded range. Counting frames from shots that
        were not concurrent, while dividing by the concurrent subset, is not a fraction at all --
        it can exceed one."""
        p = plan(nframes=5, frame_len=300)
        st = {"apA": status([0, 1, 2, 3]),
              "apB": status([0], misses={1: A.NOBF, 2: A.NOBF, 3: A.NOBF})}
        # the receiver saw every shot apA sent, but only shot 0 was concurrent
        rep = {"sta1": report({AP1: 20}, seqs=[0, 1, 2, 3],
                              idx_hist={0: 4, 1: 4, 2: 4, 3: 4, 4: 4}),
               "sta2": report({AP2: 5}, seqs=[0], idx_hist={0: 1, 1: 1, 2: 1, 3: 1, 4: 1})}
        e = A.link_results(p, st, rep, 0, 4)[0]["apA->sta1"]
        self.assertEqual(e["joint_shots"], 1)
        self.assertEqual(e["sent"], 5)
        self.assertEqual(e["rx"], 5, "only the concurrent shot's subframes count")
        self.assertEqual(e["rx_all_seqs"], 20, "the wider count stays as a diagnostic")
        self.assertEqual(e["delivery"], 1.0)
        self.assertLessEqual(e["delivery"], 1.0)

    def test_delivery_never_exceeds_one(self):
        p = plan(nframes=5, frame_len=300)
        for fired_b in ([0], [0, 1], [0, 1, 2], [0, 1, 2, 3]):
            st = {"apA": status([0, 1, 2, 3]), "apB": status(fired_b)}
            rep = {"sta1": report({AP1: 20}, seqs=[0, 1, 2, 3],
                                  idx_hist={0: 4, 1: 4, 2: 4, 3: 4, 4: 4}),
                   "sta2": report({AP2: 20}, seqs=[0, 1, 2, 3],
                                  idx_hist={0: 4, 1: 4, 2: 4, 3: 4, 4: 4})}
            e = A.link_results(p, st, rep, 0, 4)[0]["apA->sta1"]
            self.assertLessEqual(e["delivery"], 1.0,
                                 "delivery %s with %d concurrent shots"
                                 % (e["delivery"], len(fired_b)))

    def test_skipped_shots_are_named_not_lumped_as_unknown(self):
        p = plan()
        st = {"apA": status([0], misses={1: 90, 2: 90}), "apB": status([0, 1, 2])}
        rep = {"sta1": report({AP1: 1}, seqs=[0]), "sta2": report({AP2: 1}, seqs=[0])}
        links, warns = A.link_results(p, st, rep, 0, 3)
        self.assertEqual(links["apA->sta1"]["gate_misses"], {"skipped": 2})

    def test_own_fired_count_is_kept_as_a_diagnostic(self):
        p = plan()
        st = {"apA": status([0, 1, 2, 3, 4]), "apB": status([0, 1, 2])}
        rep = {"sta1": report({AP1: 3}, seqs=[0, 1, 2]),
               "sta2": report({AP2: 3}, seqs=[0, 1, 2])}
        links, _ = A.link_results(p, st, rep, 0, 5)
        self.assertEqual(links["apA->sta1"]["fired"], 5)
        self.assertEqual(links["apA->sta1"]["joint_shots"], 3)

    def test_no_joint_shot_is_not_a_loss(self):
        p = plan()
        st = {"apA": status([0, 1]), "apB": status([], misses={0: A.TOOFAR, 1: A.TOOFAR})}
        rep = {"sta1": report({AP1: 0}), "sta2": report({AP2: 0})}
        links, warns = A.link_results(p, st, rep, 0, 2)
        for e in links.values():
            self.assertEqual(e["status"], A.NOT_FIRED)
            self.assertIsNone(e["delivery"])
        self.assertTrue(any("not a loss" in w for w in warns))

    def test_gate_miss_reason_is_named(self):
        p = plan()
        st = {"apA": status([], misses={0: A.TOOFAR}), "apB": status([], misses={0: A.TOOFAR})}
        links, _ = A.link_results(p, st, {"sta1": report({AP1: 0})}, 0, 1)
        self.assertEqual(links["apA->sta1"]["reason"], "toofar")


class TestSubframeVsPpdu(unittest.TestCase):
    """Subframes of one aggregate share a preamble, so the two figures differ meaningfully."""

    def test_both_figures_reported(self):
        p = plan(nframes=5, frame_len=300)
        st = {"apA": status([0, 1, 2]), "apB": status([0, 1, 2])}
        rep = {"sta1": report({AP1: 15}, idx_hist={0: 3, 1: 3, 2: 3, 3: 3, 4: 3},
                              seqs=[0, 1, 2]),
               "sta2": report({AP2: 15}, idx_hist={0: 3, 1: 3, 2: 3, 3: 3, 4: 3},
                              seqs=[0, 1, 2])}
        links, _ = A.link_results(p, st, rep, 0, 3)
        e = links["apA->sta1"]
        self.assertEqual(e["sent"], 15)
        self.assertEqual(e["delivery"], 1.0)
        self.assertEqual(e["ppdu_sent"], 3)
        self.assertEqual(e["delivery_ppdu"], 1.0)

    def test_whole_ppdu_lost_shows_in_both(self):
        p = plan(nframes=5, frame_len=300)
        st = {"apA": status([0, 1, 2]), "apB": status([0, 1, 2])}
        rep = {"sta1": report({AP1: 10}, idx_hist={0: 2, 1: 2, 2: 2, 3: 2, 4: 2},
                              seqs=[0, 1]),
               "sta2": report({AP2: 10}, idx_hist={0: 2, 1: 2, 2: 2, 3: 2, 4: 2},
                              seqs=[0, 1])}
        links, _ = A.link_results(p, st, rep, 0, 3)
        e = links["apA->sta1"]
        self.assertAlmostEqual(e["delivery"], 10 / 15.0, places=4)
        self.assertAlmostEqual(e["delivery_ppdu"], 2 / 3.0, places=4)

    def test_deagg_failure_is_not_a_delivery_figure(self):
        """idx 0 only would read as a clean 80% loss; it is a receiver limitation."""
        p = plan(nframes=5, frame_len=300)
        st = {"apA": status([0, 1, 2]), "apB": status([0, 1, 2])}
        rep = {"sta1": report({AP1: 3}, idx_hist={0: 3}, seqs=[0, 1, 2]),
               "sta2": report({AP2: 3}, idx_hist={0: 3}, seqs=[0, 1, 2])}
        links, warns = A.link_results(p, st, rep, 0, 3)
        e = links["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "deagg_unsupported")
        self.assertIsNone(e["delivery"])
        self.assertTrue(any("deaggregating" in w for w in warns))


class TestUnmeasurable(unittest.TestCase):
    """Every path that must refuse to produce a delivery figure."""

    def _one_link_plan(self):
        return plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}])

    def test_missing_station_report(self):
        p = self._one_link_plan()
        links, warns = A.link_results(p, {"apA": status([0])}, {}, 0, 1)
        e = links["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "no_report_from_station")
        self.assertIsNone(e["delivery"])
        self.assertTrue(any("not zero" in w for w in warns))

    def test_silent_capture_is_not_zero_delivery(self):
        p = self._one_link_plan()
        rep = {"sta1": report({AP1: 0}, other=0)}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "capture_silent")
        self.assertIsNone(e["delivery"])

    def test_zero_with_ambient_traffic_is_a_real_zero(self):
        """Traffic proves the card was listening, so nothing of ours is a genuine result."""
        p = self._one_link_plan()
        rep = {"sta1": report({AP1: 0}, other=30)}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.OK)
        self.assertEqual(e["delivery"], 0.0)

    def test_partial_coverage_refused(self):
        p = self._one_link_plan()
        rep = {"sta1": report({AP1: 1}, coverage="partial")}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "coverage_partial")

    def test_evicted_coverage_refused(self):
        p = self._one_link_plan()
        rep = {"sta1": report({AP1: 0}, coverage="evicted")}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "coverage_evicted")

    def test_wrong_channel_refused(self):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}], channel_freq=2412)
        rep = {"sta1": report({AP1: 1}, freq=2437)}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "wrong_channel")

    def test_matching_channel_accepted(self):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}], channel_freq=2412)
        rep = {"sta1": report({AP1: 1}, freq=2412)}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.OK)

    def test_channel_guard_is_active_on_a_plain_resolved_plan(self):
        """The expected frequency must come from resolve() itself. If a caller has to remember
        to set it, the guard is off by default -- which is the same as not having it."""
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}])      # no channel_freq passed in
        self.assertEqual(p["channel_freq_mhz"], 2412)
        rep = {"sta1": report({AP1: 1}, freq=2437)}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "wrong_channel")

    def test_missing_ap_status(self):
        p = self._one_link_plan()
        e = A.link_results(p, {}, {"sta1": report({AP1: 1})}, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.UNKNOWN)
        self.assertEqual(e["reason"], "no_status_from_ap")

    def test_wedged_card_halts_rather_than_scoring(self):
        p = self._one_link_plan()
        st = {"apA": status([], error=A.WEDGED)}
        e = A.link_results(p, st, {"sta1": report({AP1: 0})}, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.WEDGED)
        self.assertIsNone(e["delivery"])

    def test_write_error_is_unknown_not_not_fired(self):
        """A failed trigger write does not prove the frame was not transmitted."""
        p = self._one_link_plan()
        st = {"apA": status([], error="write_failed")}
        e = A.link_results(p, st, {"sta1": report({AP1: 0})}, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.UNKNOWN)
        self.assertIsNone(e["delivery"])

    def test_station_not_watching_this_ap(self):
        p = self._one_link_plan()
        rep = {"sta1": report({AP2: 5})}
        e = A.link_results(p, {"apA": status([0])}, rep, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "station_not_watching_this_ap")

    def test_report_missing_coverage_is_refused(self):
        """Absent must not read as 'full'. Defaulting it turns the guard off silently."""
        p = self._one_link_plan()
        rep = report({AP1: 1})
        del rep["per_ap"][AP1]["coverage"]
        links, warns = A.link_results(p, {"apA": status([0])}, {"sta1": rep}, 0, 1)
        e = links["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "malformed_report")
        self.assertIsNone(e["delivery"])
        self.assertTrue(any("coverage" in w for w in warns))

    def test_report_missing_drops_is_refused(self):
        """Absent must not read as 'no drops': the count could be silently understated."""
        p = self._one_link_plan()
        rep = report({AP1: 1})
        del rep["drops"]
        e = A.link_results(p, {"apA": status([0])}, {"sta1": rep}, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "malformed_report")

    def test_every_required_report_field_is_enforced(self):
        p = self._one_link_plan()
        for field in A._REPORT_FIELDS:
            rep = report({AP1: 1})
            del rep[field]
            e = A.link_results(p, {"apA": status([0])}, {"sta1": rep}, 0, 1)[0]["apA->sta1"]
            self.assertEqual(e["status"], A.NOT_COUNTED,
                             "a report without %r was accepted" % field)

    def test_every_required_per_ap_field_is_enforced(self):
        p = self._one_link_plan()
        for field in A._PER_AP_FIELDS:
            rep = report({AP1: 1})
            del rep["per_ap"][AP1][field]
            e = A.link_results(p, {"apA": status([0])}, {"sta1": rep}, 0, 1)[0]["apA->sta1"]
            self.assertEqual(e["status"], A.NOT_COUNTED,
                             "a report without per_ap.%r was accepted" % field)

    def test_a_complete_report_still_passes(self):
        p = self._one_link_plan()
        e = A.link_results(p, {"apA": status([0])},
                           {"sta1": report({AP1: 1})}, 0, 1)[0]["apA->sta1"]
        self.assertEqual(e["status"], A.OK)

    def test_counter_produces_every_field_the_accounting_requires(self):
        """The agent's real report must satisfy the contract, or every round would refuse."""
        from cosr import counter as C
        c = C.FrameCounter([AP1], 1, epoch="e1")
        c.set_run("r")
        rep = c.report(0, 1)
        self.assertEqual(A._missing_fields(rep, AP1), [])

    def test_drops_downgrade_to_a_lower_bound(self):
        p = self._one_link_plan()
        rep = {"sta1": report({AP1: 1}, drops=3)}
        links, warns = A.link_results(p, {"apA": status([0])}, rep, 0, 1)
        self.assertEqual(links["apA->sta1"]["status"], A.OK)
        self.assertEqual(links["apA->sta1"]["drops"], 3)
        self.assertTrue(any("lower bound" in w for w in warns))


class TestThroughput(unittest.TestCase):

    def test_sums_only_sound_measurements(self):
        p = plan()
        st = {"apA": status([0]), "apB": status([0])}
        rep = {"sta1": report({AP1: 1}), "sta2": report({AP2: 0}, other=0)}
        links, _ = A.link_results(p, st, rep, 0, 1)
        total, per, measured, unmeasured = A.total_throughput(links, W.rate_for_mcs)
        self.assertEqual(measured, 1)
        self.assertEqual(unmeasured, 1)
        self.assertIsNone(per["apB->sta2"]["delivery"])

    def test_uses_commanded_mcs_rate(self):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 4, "txpower_dbm": 20}])
        rep = {"sta1": report({AP1: 1})}
        links, _ = A.link_results(p, {"apA": status([0])}, rep, 0, 1)

        def rate(mcs):
            return {4: 39.0}[mcs]
        total, per, _m, _u = A.total_throughput(links, rate)
        self.assertAlmostEqual(total, 39.0, places=3)

    def test_zero_delivery_contributes_zero_but_counts_as_measured(self):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}])
        rep = {"sta1": report({AP1: 0}, other=30)}
        links, _ = A.link_results(p, {"apA": status([0])}, rep, 0, 1)
        total, _per, measured, unmeasured = A.total_throughput(links, W.rate_for_mcs)
        self.assertEqual(total, 0.0)
        self.assertEqual((measured, unmeasured), (1, 0))


class TestEndToEndWithCounter(unittest.TestCase):
    """Feed real captured frames through the counter and into the accounting."""

    def test_real_ampdu_capture_yields_full_delivery(self):
        from cosr import counter as C
        from tests.test_radiotap import load_fixture

        real_ap = "24:ec:99:95:22:2f"
        nodes = dict(NODES)
        nodes["apA"] = dict(nodes["apA"], mac=real_ap)
        p = T.resolve({"channel": 1, "observer": "sta1", "nodes": nodes},
                      {"nframes": 5, "frame_len": 300, "mcs": 4,
                       "links": [{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}]})

        c = C.FrameCounter([real_ap], 1, epoch="e1")
        c.set_run("rig")
        for buf in load_fixture("stamped_ampdu")[0]:
            c.observe(buf)

        # the capture holds shots 0, 2 and 3; shot 1 missed the gate
        st = {"apA": status([0, 2, 3], misses={1: A.LATE})}
        links, warns = A.link_results(p, st, {"sta1": c.report(0, 4)}, 0, 4)
        e = links["apA->sta1"]
        self.assertEqual(e["status"], A.OK)
        self.assertEqual(e["joint_shots"], 3)
        self.assertEqual(e["sent"], 15)
        self.assertEqual(e["rx"], 15)
        self.assertEqual(e["delivery"], 1.0)
        self.assertEqual(e["delivery_ppdu"], 1.0)
        self.assertAlmostEqual(e["rssi_dbm"], -56.0, places=1)
        self.assertTrue(any("excluded from the denominator" in w for w in warns))


class TestOnAirRate(unittest.TestCase):
    """Whether the commanded modulation index actually reached the air."""

    def _link(self, mcs_hist, mcs_capable, rx=4):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 4, "txpower_dbm": 20}])
        st = {"apA": status([0, 1, 2, 3])}
        rep = report({AP1: rx}, seqs=[0, 1, 2, 3])
        rep["mcs_capable"] = mcs_capable
        rep["per_ap"][AP1]["mcs_hist"] = mcs_hist
        links, warnings = A.link_results(p, st, {"sta1": rep}, 0, 4)
        return links["apA->sta1"], warnings

    def test_the_commanded_rate_is_confirmed(self):
        e, w = self._link({4: 4}, True)
        self.assertEqual(e["mcs_seen"], 4)
        self.assertEqual(w, [])

    def test_a_downgrade_is_reported(self):
        e, w = self._link({0: 4}, True)
        self.assertEqual(e["mcs_seen"], 0)
        self.assertTrue(any("went on air" in x for x in w), w)

    def test_a_receiver_that_cannot_see_the_rate_does_not_condemn_it(self):
        """Reporting a downgrade here would blame the transmitter for the receiver's blindness."""
        e, w = self._link({}, False)
        self.assertIsNone(e["mcs_seen"])
        self.assertIn("cannot be verified", e["mcs_note"])
        self.assertEqual(w, [])

    def test_a_legacy_rate_is_reported_when_the_receiver_can_see_the_field(self):
        e, w = self._link({}, True)
        self.assertIsNone(e["mcs_seen"])
        self.assertTrue(any("legacy rate" in x for x in w), w)

    def test_the_most_frequent_index_wins(self):
        e, _w = self._link({4: 9, 0: 1}, True)
        self.assertEqual(e["mcs_seen"], 4)

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestChannelIsProven(unittest.TestCase):
    """A receiver that cannot say which channel it is on cannot bound the measurement."""

    def _link(self, freq):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}])
        st = {"apA": status([0, 1, 2, 3])}
        rep = report({AP1: 4}, seqs=[0, 1, 2, 3], freq=freq)
        links, warnings = A.link_results(p, st, {"sta1": rep}, 0, 4)
        return links["apA->sta1"], warnings

    def test_a_matching_channel_is_measured(self):
        self.assertEqual(self._link(2412)[0]["status"], A.OK)

    def test_a_different_channel_is_refused(self):
        e, _w = self._link(2437)
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "wrong_channel")

    def test_an_unknown_channel_is_refused_rather_than_assumed_right(self):
        """Phrasing the guard as 'if we know it and it looks wrong' passes every report from a
        receiver that never reports one, which is the same as having no guard."""
        e, w = self._link(None)
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "channel_unknown")
        self.assertIsNone(e["delivery"])
        self.assertTrue(any("reports no channel" in x for x in w), w)


class TestClockLostShot(unittest.TestCase):
    """A shot the radio abandoned because its own clock stopped is not a delivery loss."""

    def _link(self, codes):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}])
        st = {"apA": status([s for s, c in codes.items() if c == A.FIRED],
                            misses=dict((s, c) for s, c in codes.items() if c != A.FIRED))}
        rep = report({AP1: 2}, seqs=[s for s, c in codes.items() if c == A.FIRED])
        links, warnings = A.link_results(p, st, {"sta1": rep}, 0, len(codes))
        return links["apA->sta1"], warnings

    def test_it_is_excluded_from_the_denominator(self):
        e, _w = self._link({0: A.FIRED, 1: A.FIRED, 2: A.CLKLOST, 3: A.CLKLOST})
        self.assertEqual(e["joint_shots"], 2, "a shot that never went out is not a trial")
        self.assertEqual(e["delivery"], 1.0)

    def test_it_is_named_rather_than_lumped_in(self):
        e, _w = self._link({0: A.CLKLOST, 1: A.CLKLOST})
        self.assertEqual(e["status"], A.NOT_FIRED)
        self.assertEqual(e["reason"], "clock_lost")
        self.assertEqual(e["gate_misses"], {"clock_lost": 2})


class TestLivenessIsPerRound(unittest.TestCase):
    """A capture that died must not read as a link that delivered nothing."""

    def _link(self, lifetime, this_round):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}])
        rep = report({AP1: 0}, other=lifetime, seqs=[])
        rep["other_frames_round"] = this_round
        links, _w = A.link_results(p, {"apA": status([0, 1])}, {"sta1": rep}, 0, 2)
        return links["apA->sta1"]

    def test_a_dead_capture_is_refused_even_after_a_busy_session(self):
        """The lifetime total is non-zero within moments of starting on any real channel, so
        judging on it passes every dead capture from then on."""
        e = self._link(lifetime=50000, this_round=0)
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertIsNone(e["delivery"])

    def test_a_live_capture_that_heard_none_of_ours_is_a_real_zero(self):
        e = self._link(lifetime=50000, this_round=120)
        self.assertEqual(e["status"], A.OK)
        self.assertEqual(e["delivery"], 0.0)


class TestPowerMustBeApplied(unittest.TestCase):
    """Power is a dimension experiments sweep, so a silently wrong one is worse than none."""

    def _link(self, power_error):
        p = plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 14}])
        st = status([0, 1])
        st["power_error"] = power_error
        links, warnings = A.link_results(p, {"apA": st}, {"sta1": report({AP1: 2}, seqs=[0, 1])},
                                         0, 2)
        return links["apA->sta1"], warnings

    def test_a_failed_power_set_is_not_reported_as_a_measurement(self):
        e, w = self._link("netlink: operation not permitted")
        self.assertEqual(e["status"], A.NOT_COUNTED)
        self.assertEqual(e["reason"], "power_not_set")
        self.assertIsNone(e["delivery"])
        self.assertTrue(any("could not be set" in x for x in w), w)

    def test_a_successful_round_is_unaffected(self):
        e, _w = self._link(None)
        self.assertEqual(e["status"], A.OK)
