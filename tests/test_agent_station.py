#!/usr/bin/env python3
"""The receiver agent and the message contract it answers.

Frames come from the captures recorded off the rig, fed through a substitute frame source, so the
agent's real capture loop and counting path are exercised without a radio.
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import agent as A
from cosr import proto
from cosr import wire

AP1 = "24:ec:99:95:22:2f"
AP2 = "24:ec:99:a7:3e:74"


class FakeSource(object):
    """Hands out recorded frames, then blocks like an idle radio."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.i = 0
        self.closed = False
        self.stats_reads = 0
        self.drops = 0

    def recv_into(self, buf, nbytes):
        if self.i >= len(self.frames):
            raise socket.timeout()
        f = self.frames[self.i]
        self.i += 1
        n = min(len(f), nbytes)
        buf[:n] = f[:n]
        return n

    def getsockopt(self, level, opt, size):
        import struct
        self.stats_reads += 1
        d = self.drops
        self.drops = 0                      # the kernel resets on read; mimic that
        return struct.pack("II", self.i, d)

    def close(self):
        self.closed = True


def fixture_frames(name):
    from tests.test_radiotap import read_pcap
    return read_pcap(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "fixtures", name + ".pcap"))


def fire_msg(run="r1", batch=1, seq0=0, n=4, station="sta1", expect=None,
             since=0, nframes=5, lead_us=1000, spacing_us=1000):
    return proto.build_fire(
        run=run, batch=batch, seq0=seq0, n=n, spacing_us=spacing_us, stagger_us=0,
        lead_us=lead_us, nframes=nframes, frame_len=300, stamp_off=wire.STAMP_OFFSET,
        channel_freq_mhz=2412,
        aps={"ap1": {"mac": AP1, "target": 12345, "rate": 0x84, "txp": 40,
                     "station_id": 1}},
        stations={station: {"ap_macs": [AP1], "station_id": 1,
                            "since_ordinal": since,
                            "expect_per_ap": expect if expect is not None else {}}})


def station(name="sta1", frames=None, station_id=1, macs=None):
    impl = A.StationAgent(name, "mon0", station_id, macs or [AP1, AP2])
    impl.open(sock=FakeSource(frames or []))
    return impl


class TestCaptureLoop(unittest.TestCase):

    def test_counts_recorded_frames(self):
        frames = fixture_frames("stamped_ampdu")
        impl = station(frames=frames)
        impl.start()
        deadline = time.time() + 5
        while impl.frames_seen < len(frames) and time.time() < deadline:
            time.sleep(0.02)
        impl.stop()
        self.assertEqual(impl.frames_seen, len(frames))

    def test_survives_a_source_error(self):
        class Broken(FakeSource):
            def recv_into(self, buf, nbytes):
                raise socket.error("device went away")
        impl = A.StationAgent("sta1", "mon0", 1, [AP1])
        impl.open(sock=Broken([]))
        impl.start()
        time.sleep(0.3)
        impl.stop()          # must not have died in a way that blocks shutdown

    def test_stop_closes_the_source(self):
        src = FakeSource([])
        impl = A.StationAgent("sta1", "mon0", 1, [AP1])
        impl.open(sock=src)
        impl.start()
        impl.stop()
        self.assertTrue(src.closed)


class TestDrops(unittest.TestCase):

    def test_drops_accumulate_across_reads(self):
        """The kernel counter resets when read, so it has to be accumulated, not sampled."""
        src = FakeSource([])
        impl = A.StationAgent("sta1", "mon0", 1, [AP1])
        impl.open(sock=src)
        src.drops = 3
        impl.collect_drops()
        src.drops = 4
        impl.collect_drops()
        self.assertEqual(impl.counter.drops, 7)

    def test_zero_drops_stays_zero(self):
        impl = station()
        impl.collect_drops()
        self.assertEqual(impl.counter.drops, 0)


class TestOnFire(unittest.TestCase):

    def _fed(self, fixture="stamped_ampdu", run="r1", **kw):
        """A receiver mid-run: the round was declared, then its frames arrived.

        That order is what happens on the rig -- the round message is published a lead time
        before anything is transmitted -- and it matters, because a frame that arrives while no
        round is declared belongs to no round and is deliberately not counted.
        """
        impl = station(**kw)
        impl.counter.set_run(run)
        for buf in fixture_frames(fixture):
            impl.counter.observe(buf)
        return impl

    def test_reports_what_arrived(self):
        impl = self._fed()
        msg = fire_msg(n=4)
        rep = impl.on_fire(msg)
        proto.validate_report(rep)
        self.assertEqual(rep["run"], "r1")
        self.assertEqual(rep["batch"], 1)
        self.assertEqual(rep["station"], "sta1")
        self.assertEqual(rep["per_ap"][AP1]["rx"], 15)
        self.assertEqual(sorted(rep["per_ap"][AP1]["idx_hist"]), [0, 1, 2, 3, 4])

    def test_ignores_a_round_it_is_not_in(self):
        impl = self._fed()
        self.assertIsNone(impl.on_fire(fire_msg(station="someone-else")))

    def test_answers_early_once_everything_expected_has_arrived(self):
        """Waiting out the deadline when the data is already in would set a floor on round time."""
        impl = self._fed()
        msg = fire_msg(n=4, expect={AP1: 15}, lead_us=3000000, spacing_us=0)
        t0 = time.time()
        rep = impl.on_fire(msg)
        elapsed = time.time() - t0
        self.assertEqual(rep["per_ap"][AP1]["rx"], 15)
        self.assertLess(elapsed, 1.0, "took %.2fs despite the data already being present" % elapsed)

    def test_waits_out_the_deadline_when_short(self):
        impl = self._fed()
        msg = fire_msg(n=4, expect={AP1: 999}, lead_us=0, spacing_us=0)
        t0 = time.time()
        rep = impl.on_fire(msg)
        self.assertGreaterEqual(time.time() - t0, proto.report_deadline_s(msg))
        self.assertEqual(rep["per_ap"][AP1]["rx"], 15, "reports what it has, not nothing")

    def test_switches_run_namespace(self):
        impl = self._fed(run="r1")
        self.assertEqual(impl.on_fire(fire_msg(run="r1"))["per_ap"][AP1]["rx"], 15)
        rep2 = impl.on_fire(fire_msg(run="r2"))
        self.assertEqual(rep2["per_ap"][AP1]["rx"], 0,
                         "a new run must not inherit the previous run's frames")

    def test_frames_arriving_before_a_round_are_not_counted(self):
        """Nothing can be attributed to a round that has not been declared."""
        impl = station()
        for buf in fixture_frames("stamped_ampdu"):
            impl.counter.observe(buf)
        self.assertEqual(impl.on_fire(fire_msg())["per_ap"][AP1]["rx"], 0)

    def test_counts_frames_that_arrive_after_the_round_is_declared(self):
        """The real sequence: the round message lands first, then frames stream in while the
        agent is already waiting for them."""
        impl = station()
        frames = fixture_frames("stamped_ampdu")
        msg = fire_msg(n=4, expect={AP1: 15}, lead_us=2000000, spacing_us=0)

        result = {}

        def wait():
            result["rep"] = impl.on_fire(msg)

        t = threading.Thread(target=wait)
        t.start()
        time.sleep(0.1)                     # the agent is now inside its wait
        for buf in frames:
            impl.counter.observe(buf)
        t.join(5.0)
        self.assertIn("rep", result)
        self.assertEqual(result["rep"]["per_ap"][AP1]["rx"], 15)

    def test_since_ordinal_excludes_earlier_frames(self):
        impl = self._fed()
        first = impl.on_fire(fire_msg())
        again = impl.on_fire(fire_msg(since=first["ordinal"]))
        self.assertEqual(again["per_ap"][AP1]["rx"], 0)

    def test_watch_set_is_fixed_at_startup(self):
        """A round must not narrow what is being watched: a frame arriving just before the round
        message would then be judged against the previous set and discarded."""
        impl = self._fed(macs=[AP1, AP2])
        rep = impl.on_fire(fire_msg())            # the round names only AP1
        self.assertEqual(impl.counter.ap_macs, set([AP1, AP2]))
        self.assertEqual(rep["per_ap"][AP1]["rx"], 15)
        self.assertIn(AP2, rep["per_ap"], "results stay keyed per address")
        self.assertEqual(rep["per_ap"][AP2]["rx"], 0)

    def test_unwatched_address_is_never_counted(self):
        impl = self._fed(macs=[AP2])
        self.assertEqual(impl.on_fire(fire_msg())["per_ap"][AP2]["rx"], 0)
        self.assertNotIn(AP1, impl.counter.ap_macs)

    def test_report_carries_the_fields_the_accounting_requires(self):
        from cosr import accounting
        impl = self._fed()
        rep = impl.on_fire(fire_msg())
        self.assertEqual(accounting._missing_fields(rep, AP1), [])

    def test_rejects_a_version_mismatch(self):
        impl = self._fed()
        msg = fire_msg()
        msg["v"] = proto.VERSION + 1
        self.assertRaises(proto.ProtocolError, impl.on_fire, msg)

    def test_rejects_a_malformed_round(self):
        impl = self._fed()
        msg = fire_msg()
        del msg["seq0"]
        self.assertRaises(proto.ProtocolError, impl.on_fire, msg)


class TestStatus(unittest.TestCase):

    def test_reports_identity_and_health(self):
        impl = station(frames=fixture_frames("beacons"))
        for buf in fixture_frames("beacons"):
            impl.counter.observe(buf)
        st = impl.status()
        self.assertEqual(st["role"], "station")
        self.assertEqual(st["name"], "sta1")
        self.assertEqual(st["freq_mhz"], 2412)
        self.assertGreater(st["other_frames"], 0)
        self.assertEqual(len(st["source_hash"]), 16)
        self.assertIn("epoch", st)

    def test_epoch_changes_between_instances(self):
        self.assertNotEqual(station().epoch, station().epoch)


class TestSourceHash(unittest.TestCase):

    def test_is_stable_and_short(self):
        self.assertEqual(A.source_hash(), A.source_hash())
        self.assertEqual(len(A.source_hash()), 16)

    def test_covers_more_than_one_module(self):
        """A digest of a single file would miss a change in any of the others."""
        import hashlib
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "cosr", "agent.py")
        with open(path, "rb") as fh:
            one = hashlib.sha256(fh.read()).hexdigest()[:16]
        self.assertNotEqual(A.source_hash(), one)


class TestProtoContract(unittest.TestCase):

    def test_fire_round_trips(self):
        msg = fire_msg()
        proto.validate_fire(msg)
        self.assertEqual(msg["v"], proto.VERSION)

    def test_fire_rejects_missing_ap_fields(self):
        msg = fire_msg()
        del msg["aps"]["ap1"]["target"]
        self.assertRaises(proto.ProtocolError, proto.validate_fire, msg)

    def test_fire_rejects_missing_station_fields(self):
        msg = fire_msg()
        del msg["stations"]["sta1"]["ap_macs"]
        self.assertRaises(proto.ProtocolError, proto.validate_fire, msg)

    def test_fire_rejects_zero_shots(self):
        self.assertRaises(proto.ProtocolError, fire_msg, n=0)

    def test_deadline_covers_lead_and_the_whole_sequence(self):
        msg = fire_msg(n=20, lead_us=150000, spacing_us=20000)
        d = proto.report_deadline_s(msg)
        self.assertGreater(d, (150000 + 20 * 20000) / 1e6)
        self.assertLess(d, 2.0)

    def test_status_shots_survive_json(self):
        """Sequence numbers are dict keys, which JSON turns into strings."""
        import json
        msg = proto.build_status("r", 1, "ap1", "e1", {0: 0, 1: 2, 2: 3})
        again = json.loads(json.dumps(msg))
        proto.validate_status(again)
        self.assertEqual(proto.decode_shots(again), {0: 0, 1: 2, 2: 3})

    def test_status_carries_an_error(self):
        msg = proto.build_status("r", 1, "ap1", "e1", {}, error="WEDGED")
        self.assertEqual(msg["error"], "WEDGED")

    def test_hello_shape(self):
        h = proto.build_hello("station", "sta1", "e1", "abc", "3.4.0")
        proto.validate_hello(h)

    def test_subjects_are_namespaced_per_node(self):
        self.assertNotEqual(proto.subj_report("sta1"), proto.subj_report("sta2"))
        self.assertNotEqual(proto.subj_status("ap1"), proto.subj_status("ap2"))
        self.assertTrue(proto.subj_rpc("station", "sta1").endswith("station.sta1"))


class TestReset(unittest.TestCase):

    def test_reset_discards_counts(self):
        impl = station()
        impl.counter.set_run("r1")
        for buf in fixture_frames("stamped_ampdu"):
            impl.counter.observe(buf)
        self.assertGreater(impl.counter.ordinal, 0)
        impl.reset()
        self.assertEqual(impl.counter.ordinal, 0)
        self.assertEqual(impl.on_fire(fire_msg(run="r1"))["per_ap"][AP1]["rx"], 0)

    def test_reset_keeps_identity(self):
        impl = station()
        epoch = impl.epoch
        impl.reset()
        self.assertEqual(impl.epoch, epoch, "the process is the same one")
        self.assertEqual(impl.counter.station_id, impl.station_id)

if __name__ == "__main__":
    unittest.main(verbosity=2)
