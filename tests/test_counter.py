#!/usr/bin/env python3
"""cosr.counter: continuous station-side accounting.

The tests that matter most are the ones asserting delivery cannot be INFLATED -- every other
failure mode here understates, which is visible; inflation reads as a perfect link.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import counter as C
from cosr import radiotap as R
from cosr import wire as W

AP1 = "24:ec:99:95:22:2f"
AP2 = "24:ec:99:a7:3e:74"


def frame(sa=AP1, sid=1, seq=0, idx=0, signal=-56, freq=2412):
    """A parsed-frame dict shaped exactly like cosr.radiotap.parse() returns."""
    return {"radiotap_len": 24, "tsft": 1000 + seq, "signal_dbm": signal, "freq_mhz": freq,
            "mcs": None, "fc_type": R.FTYPE_DATA, "fc_subtype": 0,
            "da": "ff:ff:ff:ff:ff:ff", "sa": sa, "bssid": "ff:ff:ff:ff:ff:ff",
            "hdrlen": 24, "stamp": (sid, seq, idx)}


def counter(**kw):
    kw.setdefault("ap_macs", [AP1, AP2])
    kw.setdefault("station_id", 1)
    c = C.FrameCounter(**kw)
    c.set_run("run-1")
    return c


class TestBasicCounting(unittest.TestCase):

    def test_counts_our_frames(self):
        c = counter()
        for s in range(5):
            self.assertTrue(c.observe_parsed(frame(seq=s)))
        r = c.report(0, 5)
        self.assertEqual(r["per_ap"][AP1]["rx"], 5)
        self.assertEqual(r["per_ap"][AP1]["coverage"], C.COVERAGE_FULL)

    def test_ampdu_subframes_counted_separately(self):
        c = counter()
        for s in range(3):
            for i in range(5):
                c.observe_parsed(frame(seq=s, idx=i))
        r = c.report(0, 3)
        self.assertEqual(r["per_ap"][AP1]["rx"], 15)
        self.assertEqual(r["per_ap"][AP1]["idx_hist"], {0: 3, 1: 3, 2: 3, 3: 3, 4: 3})

    def test_duplicate_seq_idx_does_not_inflate(self):
        """A monitor can report the same frame twice; the unique count must not move."""
        c = counter()
        for _ in range(10):
            c.observe_parsed(frame(seq=7, idx=0))
        self.assertEqual(c.report(7, 1)["per_ap"][AP1]["rx"], 1)

    def test_per_ap_separation(self):
        c = counter()
        c.observe_parsed(frame(sa=AP1, seq=0))
        c.observe_parsed(frame(sa=AP2, seq=0))
        r = c.report(0, 1)
        self.assertEqual(r["per_ap"][AP1]["rx"], 1)
        self.assertEqual(r["per_ap"][AP2]["rx"], 1)

    def test_frames_outside_the_range_are_not_counted(self):
        c = counter()
        for s in range(10):
            c.observe_parsed(frame(seq=s))
        self.assertEqual(c.report(0, 5)["per_ap"][AP1]["rx"], 5)
        self.assertEqual(c.report(5, 5)["per_ap"][AP1]["rx"], 5)


class TestNotOurs(unittest.TestCase):
    """Everything that must NOT count as delivered, but must still prove the capture is alive."""

    def test_other_station_id_is_not_delivery(self):
        c = counter()
        self.assertFalse(c.observe_parsed(frame(sid=2, seq=0)))
        self.assertEqual(c.report(0, 1)["per_ap"][AP1]["rx"], 0)
        self.assertEqual(c.report(0, 1)["other_frames"], 1)

    def test_foreign_ap_is_not_delivery(self):
        c = counter()
        self.assertFalse(c.observe_parsed(frame(sa="aa:bb:cc:dd:ee:ff", seq=0)))
        self.assertEqual(c.report(0, 1)["other_frames"], 1)

    def test_unstamped_frame_is_not_delivery(self):
        c = counter()
        f = frame()
        f["stamp"] = None
        self.assertFalse(c.observe_parsed(f))
        self.assertEqual(c.report(0, 1)["other_frames"], 1)

    def test_other_frames_is_the_liveness_signal(self):
        """Ambient traffic proves 'alive but heard none of ours' -- a REAL zero, distinct from
        a dead capture. This is what replaces tcpdump's stderr summary."""
        c = counter()
        for _ in range(30):
            c.observe_parsed(frame(sa="aa:bb:cc:dd:ee:ff"))
        r = c.report(0, 5)
        self.assertEqual(r["per_ap"][AP1]["rx"], 0)
        self.assertGreater(r["other_frames"], 0)


class TestNoInflation(unittest.TestCase):
    """The failure modes that would make delivery look better than reality."""

    def test_new_run_does_not_inherit_counts(self):
        """A fresh experiment against a still-running agent must start from zero, even though
        the seq numbers restart at 0 too."""
        c = counter()
        for s in range(5):
            c.observe_parsed(frame(seq=s))
        self.assertEqual(c.report(0, 5)["per_ap"][AP1]["rx"], 5)

        c.set_run("run-2")
        self.assertEqual(c.report(0, 5)["per_ap"][AP1]["rx"], 0,
                         "run-1 frames must not be counted for run-2")

    def test_old_runs_are_evicted(self):
        c = counter(run_retain=2)
        c.observe_parsed(frame(seq=0))
        c.set_run("run-2")
        c.set_run("run-3")
        self.assertEqual(c.report(0, 1, run_id="run-1")["per_ap"][AP1]["rx"], 0)

    def test_since_ordinal_excludes_the_previous_round(self):
        """Same seq range fired twice in one run: the second report must see only new frames."""
        c = counter()
        c.observe_parsed(frame(seq=0))
        first = c.report(0, 1)
        self.assertEqual(first["per_ap"][AP1]["rx"], 1)

        # nothing new arrives; a report gated on the previous ordinal must show zero
        again = c.report(0, 1, since_ordinal=first["ordinal"])
        self.assertEqual(again["per_ap"][AP1]["rx"], 0)

        c.observe_parsed(frame(seq=0, idx=1))
        third = c.report(0, 1, since_ordinal=first["ordinal"])
        self.assertEqual(third["per_ap"][AP1]["rx"], 1)

    def test_ordinal_is_monotone(self):
        c = counter()
        seen = []
        for s in range(10):
            c.observe_parsed(frame(seq=s))
            seen.append(c.report(0, 10)["ordinal"])
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 10)

    def test_retention_window_is_bounded(self):
        self.assertRaises(ValueError, C.FrameCounter, [AP1], 1, W.STAMP_OFFSET, 40000)


class TestCoverage(unittest.TestCase):
    """A forgotten range must be refused, never answered with a low count."""

    def test_full_when_nothing_evicted(self):
        c = counter(seq_retain=16)
        for s in range(10):
            c.observe_parsed(frame(seq=s))
        self.assertEqual(c.report(0, 10)["per_ap"][AP1]["coverage"], C.COVERAGE_FULL)

    def test_evicted_when_range_entirely_forgotten(self):
        c = counter(seq_retain=8)
        for s in range(40):
            c.observe_parsed(frame(seq=s))
        r = c.report(0, 4)
        self.assertEqual(r["per_ap"][AP1]["coverage"], C.COVERAGE_EVICTED)
        self.assertEqual(r["per_ap"][AP1]["rx"], 0, "and the count is 0 -- hence 'evicted'")

    def test_partial_when_range_straddles_the_watermark(self):
        c = counter(seq_retain=8)
        for s in range(20):
            c.observe_parsed(frame(seq=s))
        found = None
        for lo in range(0, 20):
            if c.report(lo, 8)["per_ap"][AP1]["coverage"] == C.COVERAGE_PARTIAL:
                found = lo
                break
        self.assertIsNotNone(found, "some range must straddle the eviction watermark")

    def test_recent_range_stays_full_after_eviction(self):
        c = counter(seq_retain=8)
        for s in range(40):
            c.observe_parsed(frame(seq=s))
        r = c.report(36, 4)
        self.assertEqual(r["per_ap"][AP1]["coverage"], C.COVERAGE_FULL)
        self.assertEqual(r["per_ap"][AP1]["rx"], 4)


class TestRssi(unittest.TestCase):

    def test_ampdu_zero_signal_subframes_are_excluded(self):
        """The A-MPDU artefact: only the last subframe carries PHY stats. Averaging the zeros
        shifts the mean by tens of dB."""
        c = counter()
        for s in range(3):
            for i in range(5):
                c.observe_parsed(frame(seq=s, idx=i, signal=(-56 if i == 4 else 0)))
        self.assertAlmostEqual(c.report(0, 3)["per_ap"][AP1]["rssi_dbm"], -56.0, places=1)

    def test_rssi_none_when_no_usable_signal(self):
        c = counter()
        c.observe_parsed(frame(seq=0, signal=0))
        self.assertIsNone(c.report(0, 1)["per_ap"][AP1]["rssi_dbm"])

    def test_rssi_averages_real_values(self):
        c = counter()
        c.observe_parsed(frame(seq=0, signal=-50))
        c.observe_parsed(frame(seq=1, signal=-60))
        self.assertAlmostEqual(c.report(0, 2)["per_ap"][AP1]["rssi_dbm"], -55.0, places=1)


class TestChannel(unittest.TestCase):

    def test_observed_freq_from_radiotap(self):
        c = counter()
        for _ in range(5):
            c.observe_parsed(frame(freq=2412))
        self.assertEqual(c.report(0, 1)["freq_mhz"], 2412)

    def test_drifted_card_is_visible(self):
        c = counter()
        c.freqs = {2437: 50}
        self.assertEqual(c.observed_freq(), 2437)

    def test_none_before_any_frame(self):
        self.assertIsNone(counter().observed_freq())


class TestDeaggGuard(unittest.TestCase):

    def test_only_first_subframe_is_flagged(self):
        """idx 0 only, for a 5-subframe aggregate, would read as a uniform 80% channel loss."""
        self.assertFalse(C.deagg_ok({0: 4}, nframes=5))

    def test_all_subframes_is_fine(self):
        self.assertTrue(C.deagg_ok({0: 3, 1: 3, 2: 3, 3: 3, 4: 3}, nframes=5))

    def test_single_frame_shots_are_exempt(self):
        self.assertTrue(C.deagg_ok({0: 5}, nframes=1))

    def test_empty_is_not_a_deagg_failure(self):
        self.assertTrue(C.deagg_ok({}, nframes=5))


class TestAgainstRealCapture(unittest.TestCase):
    """Drive the counter with the frames actually recorded off the rig."""

    def _feed(self, fixture, station_id):
        from tests.test_radiotap import load_fixture
        frames, _ = load_fixture(fixture)
        c = C.FrameCounter([AP1, AP2], station_id, epoch="e1")
        c.set_run("rig")
        for buf in frames:
            c.observe(buf)
        return c

    def test_single_frame_capture(self):
        c = self._feed("stamped_single", 1)
        r = c.report(0, 5)
        self.assertEqual(r["per_ap"][AP1]["rx"], 5)
        self.assertEqual(r["per_ap"][AP1]["idx_hist"], {0: 5})
        self.assertEqual(r["freq_mhz"], 2412)

    def test_ampdu_capture_all_subframes(self):
        c = self._feed("stamped_ampdu", 1)
        r = c.report(0, 4)
        self.assertEqual(r["per_ap"][AP1]["rx"], 15, "3 fired shots x 5 subframes")
        self.assertEqual(sorted(r["per_ap"][AP1]["idx_hist"]), [0, 1, 2, 3, 4])
        self.assertTrue(C.deagg_ok(r["per_ap"][AP1]["idx_hist"], 5))
        self.assertAlmostEqual(r["per_ap"][AP1]["rssi_dbm"], -56.0, places=1)

    def test_beacons_are_all_other_frames(self):
        c = self._feed("beacons", 1)
        r = c.report(0, 100)
        self.assertEqual(r["per_ap"][AP1]["rx"], 0)
        self.assertEqual(r["other_frames"], 40)

    def test_wrong_station_id_counts_nothing(self):
        c = self._feed("stamped_single", 99)
        self.assertEqual(c.report(0, 5)["per_ap"][AP1]["rx"], 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEvictionFollowsArrival(unittest.TestCase):
    """The wire sequence field folds at 16 bits, so numeric order stops meaning age."""

    def test_the_oldest_arrival_is_forgotten_first(self):
        c = C.FrameCounter([AP1], 1, seq_retain=3)
        c.set_run("r")
        for seq in (10, 11, 12, 13):
            c.observe_parsed(frame(seq=seq))
        b = c._runs["r"][AP1]
        self.assertEqual(sorted(b.seqs), [11, 12, 13])

    def test_a_wrapped_sequence_does_not_evict_the_newest(self):
        """After the fold the smallest key is the newest entry; evicting by value there throws
        away fresh data and leaves the forgotten range describing something else."""
        c = C.FrameCounter([AP1], 1, seq_retain=3)
        c.set_run("r")
        for seq in (65534, 65535, 0, 1):      # the wrap happens mid-run
            c.observe_parsed(frame(seq=seq))
        b = c._runs["r"][AP1]
        self.assertEqual(sorted(b.seqs), [0, 1, 65535],
                         "the entries that arrived last must survive")
        self.assertEqual(b.evicted_max, 65534)
