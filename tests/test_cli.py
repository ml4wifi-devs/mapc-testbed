#!/usr/bin/env python3
"""cosr.cli: argument handling and what each command prints.

The session and the deployment are substituted, so the command layer is exercised without a
testbed.
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cosr import cli as C

TOPO = {
    "channel": 1, "observer": "sta1", "hub": "10.0.0.1", "token": "t",
    "nodes": {
        "apA": {"role": "ap", "ip": "10.0.0.11", "iface": "wlan0",
                "mac": "02:00:00:00:00:11", "ssid": "cosrA"},
        "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "mon0", "station_id": 1},
    },
}
EXPERIMENT = {"nframes": 1, "frame_len": 200, "mcs": 0, "repeats": 4,
        "links": [{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}]}


class Out(object):
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)

    def text(self):
        return "\n".join(self.lines)


def write_files(tmp, topo=None, experiment=None):
    tp = os.path.join(tmp, "topo.json")
    sp = os.path.join(tmp, "experiment.json")
    with open(tp, "w") as fh:
        json.dump(topo or TOPO, fh)
    with open(sp, "w") as fh:
        json.dump(experiment or EXPERIMENT, fh)
    return tp, sp


class TestPlanLoading(unittest.TestCase):

    def test_both_descriptions_are_read(self):
        tmp = tempfile.mkdtemp()
        tp, sp = write_files(tmp)
        plan = C.load_plan(tp, sp)
        self.assertEqual(plan["channel"], 1)
        self.assertEqual(plan["hub"], "10.0.0.1")
        self.assertEqual(plan["token"], "t")

    def test_a_stated_hub_is_used_verbatim(self):
        tmp = tempfile.mkdtemp()
        tp, sp = write_files(tmp)
        self.assertEqual(C.hub_for(C.load_plan(tp, sp)), "10.0.0.1")

    def test_without_a_stated_hub_the_facing_address_is_chosen(self):
        """A controller commonly has several addresses and only one of them faces the testbed,
        so the routing table decides rather than the hostname."""
        topo = dict(TOPO)
        topo.pop("hub")
        tmp = tempfile.mkdtemp()
        tp, sp = write_files(tmp, topo=topo)
        got = C.hub_for(C.load_plan(tp, sp))
        self.assertRegex(got, r"^\d+\.\d+\.\d+\.\d+$")


class TestDispatch(unittest.TestCase):

    def test_help_is_not_an_error(self):
        self.assertEqual(C.main(["--help"]), 0)

    def test_an_unknown_command_is_refused(self):
        self.assertEqual(C.main(["frobnicate"]), 2)

    def test_every_documented_command_exists(self):
        for name in ("up", "down", "status", "doctor", "run", "calibrate"):
            self.assertIn(name, C.COMMANDS, "%s is documented but not implemented" % name)

    def test_every_command_is_documented(self):
        for name in C.COMMANDS:
            self.assertIn("cosr %s" % name, C.USAGE,
                          "%s is implemented but not documented" % name)


class FakeSession(object):
    def __init__(self, report=None, shot=None):
        self._report = report
        self._shot = shot
        self.closed = False

    def doctor(self, **kw):
        return self._report

    def run(self, **kw):
        return self._shot

    def close(self):
        self.closed = True


def a_round(delivery=1.0):
    return {"seconds": 0.3, "throughput_mbps": 6.5, "measured_links": 1,
            "links": {"apA->sta1": {"status": "OK", "delivery": delivery, "rx": 4,
                                    "sent": 4, "rssi_dbm": -55.0, "reason": None}},
            "rssi_ap_to_sta_dbm": {"apA->sta1": -55.0},
            "rssi_ap_to_ap_dbm": {"apA->apB": -62.0},
            "warnings": []}


class TestReporting(unittest.TestCase):

    def _plan(self):
        tmp = tempfile.mkdtemp()
        tp, sp = write_files(tmp)
        return C.load_plan(tp, sp)

    def test_a_failing_health_check_sets_a_nonzero_status(self):
        """A script that runs this before an experiment must be able to stop on the result."""
        rep = {"verdict": "FAIL", "stages": [{"stage": "fire", "verdict": "FAIL",
                                              "detail": "refused"}]}
        C._session = lambda plan: FakeSession(report=rep)
        out = Out()
        self.assertEqual(C.cmd_doctor(self._plan(), out), 1)
        self.assertIn("FAIL", out.text())

    def test_an_inconclusive_check_is_not_reported_as_success(self):
        rep = {"verdict": "INCONCLUSIVE", "stages": []}
        C._session = lambda plan: FakeSession(report=rep)
        self.assertEqual(C.cmd_doctor(self._plan(), Out()), 1)

    def test_a_passing_check_succeeds(self):
        rep = {"verdict": "PASS", "stages": []}
        C._session = lambda plan: FakeSession(report=rep)
        self.assertEqual(C.cmd_doctor(self._plan(), Out()), 0)

    def test_the_session_is_closed_even_when_a_command_fails(self):
        sess = FakeSession(report={"verdict": "PASS", "stages": []})
        C._session = lambda plan: sess
        C.cmd_doctor(self._plan(), Out())
        self.assertTrue(sess.closed)

    def test_levels_between_transmitters_come_with_every_round(self):
        """They are read from beacons already being reported, so there is no round to save by
        leaving them out and no second verb that could disagree with the first."""
        C._session = lambda plan: FakeSession(shot=a_round())
        out = Out()
        C.cmd_run(self._plan(), out)
        self.assertIn("-62.0 dBm", out.text())
        self.assertIn("delivery=1.0", out.text())


class TestCalibrate(unittest.TestCase):
    """The spacing sweep must judge frames that arrived, not shots the gate accepted."""

    def _plan(self):
        tmp = tempfile.mkdtemp()
        tp, sp = write_files(tmp)
        return C.load_plan(tp, sp)

    def test_the_shortest_clean_spacing_is_reported(self):
        class Sweep(FakeSession):
            def run(self, spacing_us=None, **kw):
                return a_round(1.0 if spacing_us >= 25000 else 0.4)

        C._session = lambda plan: Sweep()
        out = Out()
        self.assertEqual(C.cmd_calibrate(self._plan(), out, repeats=1), 0)
        self.assertIn("stayed there above: 25000 us", out.text())

    def test_a_lone_clean_result_below_a_failing_one_is_not_the_answer(self):
        """A short spacing that survives one batch while a longer one fails is noise, not a
        threshold, and would then be used for every later measurement."""
        class Fluke(FakeSession):
            def run(self, spacing_us=None, **kw):
                return a_round(0.4 if spacing_us == 10000 else 1.0)

        C._session = lambda plan: Fluke()
        out = Out()
        self.assertEqual(C.cmd_calibrate(self._plan(), out, repeats=1), 0)
        self.assertIn("stayed there above: 15000 us", out.text())
        self.assertNotIn("above: 5000 us", out.text())

    def test_the_worst_of_several_attempts_decides(self):
        """One good batch does not make a spacing usable."""
        state = {"n": 0}

        class Flaky(FakeSession):
            def run(self, spacing_us=None, **kw):
                state["n"] += 1
                return a_round(1.0 if state["n"] % 2 else 0.3)

        C._session = lambda plan: Flaky()
        out = Out()
        self.assertEqual(C.cmd_calibrate(self._plan(), out, repeats=2), 1)

    def test_a_link_that_never_delivers_is_an_error_not_a_number(self):
        """Every spacing being equally bad is not every spacing being equally fine."""
        class Never(FakeSession):
            def run(self, spacing_us=None, **kw):
                return a_round(0.2)

        C._session = lambda plan: Never()
        out = Out()
        self.assertEqual(C.cmd_calibrate(self._plan(), out, repeats=1), 1)
        self.assertIn("unrelated to spacing", out.text())

    def test_the_receivers_own_ceiling_is_the_reference(self):
        """A passive receiver does not capture everything even on an idle channel, so judging
        against an absolute fraction would call every spacing bad on a usable link."""
        class Ceiling(FakeSession):
            def run(self, spacing_us=None, **kw):
                return a_round(0.90 if spacing_us >= 15000 else 0.60)

        C._session = lambda plan: Ceiling()
        out = Out()
        self.assertEqual(C.cmd_calibrate(self._plan(), out, repeats=1), 0)
        self.assertIn("stayed there above: 15000 us", out.text())
        self.assertIn("best delivery seen: 0.90", out.text())

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReset(unittest.TestCase):
    """Clearing node state without replacing the program each node is running."""

    def _plan(self):
        tmp = tempfile.mkdtemp()
        tp, sp = write_files(tmp)
        return C.load_plan(tp, sp)

    def test_every_participant_is_reset(self):
        asked = []

        class Sess(FakeSession):
            client = type("B", (), {"request_json": staticmethod(
                lambda subj, obj, timeout=None: asked.append((subj, obj["op"])) or {"ok": True})})()

            def participants(self):
                return [("ap", "apA"), ("station", "sta1")]

        C._session = lambda plan: Sess()
        out = Out()
        self.assertEqual(C.cmd_reset(self._plan(), out), 0)
        self.assertEqual([op for _s, op in asked], ["reset", "reset"])
        self.assertIn("apA", out.text())

    def test_a_node_that_refuses_is_reported_rather_than_ignored(self):
        class Sess(FakeSession):
            client = type("B", (), {"request_json": staticmethod(
                lambda subj, obj, timeout=None: {"ok": False, "error": "radio busy"})})()

            def participants(self):
                return [("ap", "apA")]

        C._session = lambda plan: Sess()
        out = Out()
        C.cmd_reset(self._plan(), out)
        self.assertIn("radio busy", out.text())


class TestConfigErrorsReachTheUser(unittest.TestCase):
    """The refusals in topo.resolve are the interface for a wrong description, so they must
    arrive as their message rather than as a stack trace."""

    def _write(self, experiment):
        tmp = tempfile.mkdtemp()
        tp, sp = write_files(tmp, experiment=experiment)
        return tp, sp

    def test_a_link_missing_its_rate_is_reported_not_raised(self):
        bad = dict(EXPERIMENT)
        bad["links"] = [{"ap": "apA", "station": "sta1", "txpower_dbm": 20}]
        tp, sp = self._write(bad)
        self.assertEqual(C.main(["run", tp, sp]), 1)

    def test_an_impossible_aggregate_is_reported_not_raised(self):
        bad = dict(EXPERIMENT)
        bad["nframes"], bad["frame_len"] = 20, 300      # 6000 B, well past the pool
        tp, sp = self._write(bad)
        self.assertEqual(C.main(["run", tp, sp]), 1)

    def test_a_missing_file_is_reported_not_raised(self):
        self.assertEqual(C.main(["run", "/nonexistent/topo.json"]), 1)
