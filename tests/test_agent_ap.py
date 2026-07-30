#!/usr/bin/env python3
"""The transmitter agent: batched firing, per-shot outcomes, and the guards around the radio.

The trigger node, the clock and the kernel ring are substituted, so the firing logic and every
failure path are exercised without a radio.
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import agent as A
from cosr import natsc
from cosr import proto
from cosr import wire

MAC = "24:ec:99:95:22:2f"


class FakeKmsg(object):
    """Returns the outcome the radio would have printed for each trigger write."""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.i = 0
        self.overruns = 0
        self.reads = 0

    def read_new(self, bufsize=8192):
        self.reads += 1
        if self.i < len(self.outcomes):
            v = self.outcomes[self.i]
            self.i += 1
            if v is None:
                return "some other kernel message\n"
            return "cosr_gated_tx: status=%d target_lo=1 send_tsf=0 delta_us=0\n" % v
        return "cosr_gated_tx: status=0 target_lo=1 send_tsf=0 delta_us=0\n"

    def close(self):
        pass


def ap_agent(outcomes=None, power_setter=None):
    node = tempfile.NamedTemporaryFile(delete=False, suffix=".trigger")
    node.close()
    impl = A.ApAgent("ap1", "wlan0", MAC, node=node.name,
                     kmsg=FakeKmsg(outcomes),
                     power_setter=power_setter or (lambda iface, mbm: mbm))
    impl._paths = (node.name,)
    return impl


def cleanup(impl):
    for p in getattr(impl, "_paths", ()):
        try:
            os.unlink(p)
        except OSError:
            pass


def fire_msg(n=4, spacing_us=15000, seq0=0, batch=1, run="r1", target=5000000,
             nframes=1, txpower_mbm=None):
    ap = {"mac": MAC, "target": target, "rate": wire.rate_for_mcs(4), "txp": 40,
          "station_id": 1}
    if txpower_mbm is not None:
        ap["txpower_mbm"] = txpower_mbm
    return proto.build_fire(
        run=run, batch=batch, seq0=seq0, n=n, spacing_us=spacing_us, stagger_us=0,
        lead_us=150000, nframes=nframes, frame_len=200, stamp_off=wire.STAMP_OFFSET,
        channel_freq_mhz=2412, aps={"ap1": ap}, stations={})


class TestFiring(unittest.TestCase):

    def tearDown(self):
        cleanup(getattr(self, "impl", None) or ap_agent())

    def test_reports_one_outcome_per_shot(self):
        self.impl = ap_agent([0, 0, 0, 0])
        st = self.impl.on_fire(fire_msg(n=4, spacing_us=0))
        proto.validate_status(st)
        self.assertEqual(proto.decode_shots(st), {0: 0, 1: 0, 2: 0, 3: 0})
        self.assertIsNone(st["error"])

    def test_mixed_outcomes_are_reported_individually(self):
        """Totals cannot express which shots coincided, so outcomes stay per shot."""
        self.impl = ap_agent([0, 2, 0, 3])
        st = self.impl.on_fire(fire_msg(n=4, spacing_us=0))
        self.assertEqual(proto.decode_shots(st), {0: A.FIRED, 1: A.LATE,
                                                  2: A.FIRED, 3: A.TOOFAR})

    def test_targets_advance_by_the_spacing(self):
        self.impl = ap_agent([0, 0, 0])
        written = []
        orig = self.impl._write_trigger

        def spy(payload):
            written.append(payload)
            return orig(payload)
        self.impl._write_trigger = spy
        self.impl.on_fire(fire_msg(n=3, spacing_us=15000, target=5000000))
        targets = [sum(p[i] << (8 * i) for i in range(8)) for p in written]
        self.assertEqual(targets, [5000000, 5015000, 5030000])

    def test_sequence_numbers_follow_the_commanded_range(self):
        self.impl = ap_agent([0, 0, 0])
        st = self.impl.on_fire(fire_msg(n=3, spacing_us=0, seq0=1000))
        self.assertEqual(sorted(proto.decode_shots(st)), [1000, 1001, 1002])

    def test_payload_matches_the_wire_format(self):
        self.impl = ap_agent([0])
        written = []
        self.impl._write_trigger = lambda p: (written.append(p), 0)[1]
        self.impl.on_fire(fire_msg(n=1, spacing_us=0, target=777, nframes=1))
        expect = wire.blob(777, wire.rate_for_mcs(4), 40, 1, wire.STAMP_OFFSET, 1, 0, 200, MAC)
        self.assertEqual(written[0], expect)

    def test_not_addressed_to_this_transmitter(self):
        self.impl = ap_agent([0])
        msg = fire_msg()
        msg["aps"] = {"ap9": msg["aps"]["ap1"]}
        self.assertIsNone(self.impl.on_fire(msg))


class TestRepeatedInstruction(unittest.TestCase):
    """A repeated round must not put more shots on air."""

    def tearDown(self):
        cleanup(self.impl)

    def test_same_batch_returns_the_cached_outcome(self):
        self.impl = ap_agent([0, 0])
        first = self.impl.on_fire(fire_msg(n=2, spacing_us=0, batch=7))
        writes = self.impl.shots_fired
        second = self.impl.on_fire(fire_msg(n=2, spacing_us=0, batch=7))
        self.assertEqual(proto.decode_shots(first), proto.decode_shots(second))
        self.assertEqual(self.impl.shots_fired, writes, "the batch was transmitted twice")

    def test_a_new_batch_does_fire(self):
        self.impl = ap_agent([0, 0, 0, 0])
        self.impl.on_fire(fire_msg(n=2, spacing_us=0, batch=1))
        self.impl.on_fire(fire_msg(n=2, spacing_us=0, batch=2))
        self.assertEqual(self.impl.shots_fired, 4)

    def test_in_progress_is_reported_not_refired(self):
        """A repeat arriving while the batch is still firing must not start a second one."""
        self.impl = ap_agent([0] * 4)
        self.impl._batches[("r1", 9)] = None
        st = self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=9, run="r1"))
        self.assertEqual(st["error"], "IN_PROGRESS")
        self.assertEqual(self.impl.shots_fired, 0)


class TestFailurePaths(unittest.TestCase):

    def tearDown(self):
        cleanup(self.impl)

    def test_absent_outcome_is_unknown_not_a_failure_to_fire(self):
        """No printed outcome does not establish that nothing was transmitted."""
        self.impl = ap_agent([None])
        st = self.impl.on_fire(fire_msg(n=1, spacing_us=0))
        self.assertEqual(proto.decode_shots(st), {0: A.UNKNOWN_OUTCOME})

    def test_write_failure_is_unknown_and_stops_the_batch(self):
        self.impl = ap_agent([0] * 4)
        self.impl._node = "/nonexistent/path/trigger"
        st = self.impl.on_fire(fire_msg(n=4, spacing_us=0))
        shots = proto.decode_shots(st)
        self.assertEqual(shots[0], A.UNKNOWN_OUTCOME)
        self.assertEqual(st["error"], "WEDGED")
        self.assertEqual(len(shots), 1, "firing stops once the radio is unusable")

    def test_a_wedged_radio_refuses_later_rounds(self):
        self.impl = ap_agent([0])
        self.impl.wedged = "clock unusable"
        st = self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=3))
        self.assertEqual(st["error"], "WEDGED")
        self.assertEqual(self.impl.shots_fired, 0)

    def test_power_failure_is_recorded_but_does_not_abort(self):
        def bad(iface, mbm):
            raise RuntimeError("regulatory domain refused it")
        self.impl = ap_agent([0, 0], power_setter=bad)
        st = self.impl.on_fire(fire_msg(n=2, spacing_us=0, txpower_mbm=2000))
        self.assertIn("power_error", st)
        self.assertEqual(len(proto.decode_shots(st)), 2)

    def test_power_is_asserted_every_round(self):
        calls = []
        self.impl = ap_agent([0] * 6, power_setter=lambda i, m: calls.append(m))
        self.impl.on_fire(fire_msg(n=1, spacing_us=0, batch=1, txpower_mbm=2000))
        self.impl.on_fire(fire_msg(n=1, spacing_us=0, batch=2, txpower_mbm=2000))
        self.assertEqual(calls, [2000, 2000],
                         "an interface restart reverts it, so it cannot be remembered")

    def test_version_mismatch_is_refused(self):
        self.impl = ap_agent([0])
        msg = fire_msg()
        msg["v"] = proto.VERSION + 1
        self.assertRaises(proto.ProtocolError, self.impl.on_fire, msg)


class TestSchedule(unittest.TestCase):

    def tearDown(self):
        cleanup(self.impl)

    def test_a_lagging_loop_skips_rather_than_firing_late(self):
        """Attempting a shot whose instant has passed only consumes another slot."""
        self.impl = ap_agent([0] * 6)
        orig = self.impl._write_trigger

        def slow(payload):
            time.sleep(0.05)
            return orig(payload)
        self.impl._write_trigger = slow
        st = self.impl.on_fire(fire_msg(n=5, spacing_us=1000))
        shots = proto.decode_shots(st)
        self.assertEqual(len(shots), 5)
        self.assertIn(A.SKIPPED, shots.values())
        self.assertGreater(st["skipped"], 0)

    def test_no_skipping_when_the_loop_keeps_up(self):
        self.impl = ap_agent([0] * 5)
        st = self.impl.on_fire(fire_msg(n=5, spacing_us=100000))
        self.assertEqual(st["skipped"], 0)
        self.assertNotIn(A.SKIPPED, proto.decode_shots(st).values())


class TestSerialisation(unittest.TestCase):
    """Everything reaching the radio's command interface is serialised: overlapping commands are
    how a card's clock is left permanently unusable."""

    def tearDown(self):
        cleanup(self.impl)

class TestBeaconRows(unittest.TestCase):

    def tearDown(self):
        cleanup(self.impl)

    def test_parses_rows(self):
        self.impl = ap_agent()
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write("24:ec:99:a7:3e:74 9309798784 9299255158 0 124\n")
        f.write("bad line\n")
        f.write("24:ec:99:a7:3e:74 9309798999 9299255999 0 124 -58\n")
        f.close()
        self.impl._beacons = f.name
        rows = self.impl.beacon_rows()
        os.unlink(f.name)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sender"], "24:ec:99:a7:3e:74")
        self.assertEqual(rows[0]["hearer"], MAC)
        self.assertNotIn("rssi", rows[0])
        self.assertEqual(rows[1]["rssi"], -58, "an appended signal column is carried through")

    def test_absent_source_is_not_an_error(self):
        self.impl = ap_agent()
        self.impl._beacons = None
        self.assertEqual(self.impl.beacon_rows(), [])


class TestStatus(unittest.TestCase):

    def tearDown(self):
        cleanup(self.impl)

    def test_reports_identity(self):
        self.impl = ap_agent()
        st = self.impl.status()
        self.assertEqual(st["role"], "ap")
        self.assertEqual(st["mac"], MAC)
        self.assertIsNone(st["wedged"])
        self.assertEqual(len(st["source_hash"]), 16)

class TestKernelLog(unittest.TestCase):
    """Exercised against the real device where available; the mechanics were confirmed on the
    transmitter image, so this guards the parsing rather than the kernel behaviour."""

    def test_reads_incrementally(self):
        if not os.path.exists("/dev/kmsg") or not os.access("/dev/kmsg", os.R_OK):
            self.skipTest("/dev/kmsg is Linux-only; the mechanics are exercised on the node")
        log = A.KernelLog()
        try:
            first = log.read_new()
            self.assertIsInstance(first, str)
        finally:
            log.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRunScoping(unittest.TestCase):
    """Batch numbers restart with each run, so the record of completed rounds must be scoped by
    run. Keying on the number alone makes a new run's first rounds answer from memory without
    transmitting -- which reads downstream as total loss on a working link."""

    def tearDown(self):
        cleanup(self.impl)

    def test_same_batch_number_in_a_new_run_still_fires(self):
        self.impl = ap_agent([0] * 8)
        first = self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=1, run="run-A"))
        self.assertEqual(self.impl.shots_fired, 4)
        second = self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=1, run="run-B"))
        self.assertEqual(self.impl.shots_fired, 8, "the new run was answered from memory")
        self.assertEqual(second["run"], "run-B")
        self.assertIsNotNone(first)

    def test_repeat_within_one_run_is_still_absorbed(self):
        self.impl = ap_agent([0] * 8)
        self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=1, run="run-A"))
        self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=1, run="run-A"))
        self.assertEqual(self.impl.shots_fired, 4)

    def test_the_reply_describes_the_round_that_was_asked_for(self):
        """A cached reply from another round would report a different number of shots."""
        self.impl = ap_agent([0] * 30)
        self.impl.on_fire(fire_msg(n=20, spacing_us=0, batch=1, run="run-A"))
        st = self.impl.on_fire(fire_msg(n=5, spacing_us=0, batch=1, run="run-B"))
        self.assertEqual(len(proto.decode_shots(st)), 5)

    def test_the_record_is_bounded(self):
        self.impl = ap_agent([0] * 4000)
        for b in range(A.ApAgent.BATCH_MEMORY + 50):
            self.impl.on_fire(fire_msg(n=1, spacing_us=0, batch=b, run="r"))
        self.assertLessEqual(len(self.impl._batches), A.ApAgent.BATCH_MEMORY)


class TestReset(unittest.TestCase):
    """Bringing a deployment up clears state gathered under a previous arrangement."""

    def tearDown(self):
        cleanup(self.impl)

    def test_reset_allows_a_repeated_round_to_fire_again(self):
        self.impl = ap_agent([0] * 8)
        self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=1, run="r"))
        self.impl.reset()
        self.impl.on_fire(fire_msg(n=4, spacing_us=0, batch=1, run="r"))
        self.assertEqual(self.impl.shots_fired, 4, "counters restart from the reset")

    def test_reset_clears_an_unusable_radio(self):
        """Recovering a card is a physical act the agent cannot observe."""
        self.impl = ap_agent([0] * 4)
        self.impl.wedged = "clock unusable"
        out = self.impl.reset()
        self.assertIsNone(self.impl.wedged)
        self.assertEqual(out["was_unusable"], "clock unusable")
        st = self.impl.on_fire(fire_msg(n=2, spacing_us=0, batch=1, run="r"))
        self.assertIsNone(st["error"])


class TestPowerSetter(unittest.TestCase):
    """Choosing how to reach the radio, and what a failure there means."""

    def tearDown(self):
        cleanup(self.impl)

    def test_a_setter_is_resolved_when_none_is_given(self):
        self.impl = A.ApAgent("ap1", "wlan0", MAC, node="/dev/null",
                              kmsg=FakeKmsg([0]))
        self.assertIsNone(self.impl._power_setter)
        try:
            self.impl.set_power(2000)
        except Exception:
            pass                     # no radio here; only the resolution matters
        self.assertIsNotNone(self.impl._power_setter)

    def test_none_power_is_not_asserted(self):
        calls = []
        self.impl = ap_agent([0], power_setter=lambda i, m: calls.append(m))
        self.assertIsNone(self.impl.set_power(None))
        self.assertEqual(calls, [])


class TestBeaconForwarding(unittest.TestCase):
    """The stream of observations is what lets the clocks be related at all."""

    class Bus(object):
        def __init__(self, fail_times=0):
            self.published = []
            self.fail_times = fail_times

        def publish_json(self, subject, obj, reply=None):
            if self.fail_times > 0:
                self.fail_times -= 1
                raise natsc.NatsError("connection is being re-established")
            self.published.append(obj)

    class Rows(object):
        def __init__(self, batches):
            self.batches = list(batches)

        def beacon_rows(self):
            return self.batches.pop(0) if self.batches else []

    def _row(self, t2):
        return {"hearer": "aa", "sender": "bb", "t1": t2 + 10, "t2": t2,
                "rate_idx": 0, "len": 124}

    def _agent(self, bus, batches):
        a = A.Agent("ap", "apA", "127.0.0.1", impl=self.Rows(batches))
        a.client = bus
        return a

    def _pump(self, agent, rounds):
        """Run the loop body a fixed number of times instead of forever."""
        calls = [0]
        real_wait = agent._stop.wait

        def wait(_interval):
            calls[0] += 1
            if calls[0] >= rounds:
                agent._stop.set()
            return real_wait(0)

        agent._stop.wait = wait
        agent._beacon_loop()

    def test_observations_are_forwarded(self):
        bus = self.Bus()
        agent = self._agent(bus, [[self._row(100)], [self._row(200)]])
        self._pump(agent, 2)
        sent = [r["t2"] for m in bus.published for r in m["rows"]]
        self.assertEqual(sent, [100, 200])

    def test_the_same_observation_is_not_sent_twice(self):
        bus = self.Bus()
        agent = self._agent(bus, [[self._row(100)], [self._row(100)]])
        self._pump(agent, 2)
        self.assertEqual(len(bus.published), 1)

    def test_a_failed_publish_does_not_end_the_stream(self):
        """Stopping for good would disable the clock plane while the agent went on answering
        everything else, so nothing downstream would look wrong."""
        bus = self.Bus(fail_times=1)
        agent = self._agent(bus, [[self._row(100)], [self._row(200)]])
        self._pump(agent, 2)
        self.assertTrue(bus.published, "the stream must resume after a failed publish")

    def test_an_undelivered_observation_is_retried(self):
        bus = self.Bus(fail_times=1)
        agent = self._agent(bus, [[self._row(100)], [self._row(100)]])
        self._pump(agent, 2)
        sent = [r["t2"] for m in bus.published for r in m["rows"]]
        self.assertEqual(sent, [100], "a row that never went out must not be treated as sent")


class TestErrorRepliesAreAcceptable(unittest.TestCase):
    """A diagnosed fault must reach the controller, not be discarded on arrival."""

    def test_a_refusal_is_shaped_like_a_reply(self):
        """It has to pass the same validation as a normal status, or the round is reported as
        a node that never answered and the operator is sent after connectivity instead."""
        impl = ap_agent()
        try:
            agent = A.Agent("ap", "ap1", "127.0.0.1", impl=impl)
            body = agent._error_reply({"run": "r1", "batch": 7}, "bad shape")
            proto.validate_status(body)          # raises if the controller would drop it
            self.assertEqual(body["run"], "r1")
            self.assertEqual(body["batch"], 7)
            self.assertEqual(body["error"], "bad shape")
        finally:
            cleanup(impl)
