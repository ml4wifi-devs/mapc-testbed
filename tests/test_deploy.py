#!/usr/bin/env python3
"""cosr.deploy: getting the node program onto nodes and keeping it in step.

The ssh transport is replaced by a recorder, so the ordering guarantees and the failure
handling are exercised without any node.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cosr import deploy as D
from cosr import topo as T

AP1 = "02:00:00:00:00:11"
NODES = {
    "apA":  {"role": "ap", "ip": "10.0.0.11", "iface": "wlan0", "mac": AP1},
    "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "mon0", "station_id": 1},
    "spare": {"role": "station", "ip": "10.0.0.99", "iface": "mon0", "station_id": 9},
}


def plan():
    return T.resolve({"channel": 1, "observer": "sta1", "nodes": NODES},
                     {"nframes": 1, "frame_len": 200, "mcs": 0,
                      "links": [{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}]})


class Recorder(object):
    """Stands in for ssh/scp: records argv and replays scripted results."""

    def __init__(self):
        self.calls = []
        self.running = False
        self.hash = D.expected_hash()
        self.fail_on = None
        self.banner = "** WARNING: connection is not using a post-quantum key exchange\n"

    def __call__(self, argv, stdin=None):
        line = " ".join(argv)
        self.sent = getattr(self, "sent", []) 
        if stdin is not None:
            self.sent.append(stdin)
        self.calls.append(line)
        # ssh always emits a banner on stderr; nothing may treat it as command output.
        if self.fail_on and self.fail_on in line:
            return 1, "", "refused"
        if "source_hash" in line:
            return 0, self.hash + "\n", self.banner
        if "pgrep" in line:
            return (0, "1234\n", self.banner) if self.running else (0, "", self.banner)
        if "pkill" in line:
            assert "[c]osr" in line, "a self-matching pattern would kill the carrying shell"
            if getattr(self, "ignores_term", False) and "-9" not in line:
                return 0, "", self.banner       # a process that ignores the polite request
            self.running = False
            return 0, "", self.banner
        if "agent.py --role" in line:
            self.running = True
            return 0, "", self.banner
        return 0, "", self.banner

    def index_of(self, needle):
        for i, c in enumerate(self.calls):
            if needle in c:
                return i
        raise AssertionError("never ran anything matching %r:\n%s"
                             % (needle, "\n".join(self.calls)))


class TestDeploy(unittest.TestCase):

    def _dep(self):
        rec = Recorder()
        return D.Deployer(plan(), hub="10.0.0.1", token="t", runner=rec), rec

    def test_every_node_in_the_testbed_gets_an_agent(self):
        """Not just the ones this round's links name.

        The clock plane counts every AP as a participant so that a round using one transmitter
        can still place its instant, and which station a round targets changes without
        redeploying. An AP left without an agent publishes no observations and then shows up as
        a clock the graph cannot reach, which reads as broken hardware rather than as a node
        nobody deployed to.
        """
        dep, _rec = self._dep()
        self.assertEqual(sorted(n["name"] for n in dep.nodes()), ["apA", "spare", "sta1"])

    def test_the_old_agent_is_gone_before_the_new_one_starts(self):
        """Starting first and hoping the old one exits leaves two agents answering for one
        node, one of them stale."""
        dep, rec = self._dep()
        rec.running = True
        dep.up()
        self.assertLess(rec.index_of("pkill"), rec.index_of("agent.py --role"))

    def test_the_copy_lands_before_the_agent_is_started(self):
        dep, rec = self._dep()
        dep.up()
        self.assertLess(rec.index_of("cat >"), rec.index_of("agent.py --role"))

    def test_every_module_is_sent(self):
        """The archive must carry the whole node program; a missing module leaves a node that
        imports something the controller never sent."""
        import io as _io
        import tarfile as _tf
        dep, rec = self._dep()
        dep.up()
        tar = _tf.open(fileobj=_io.BytesIO(rec.sent[0]), mode="r")
        self.assertEqual(sorted(tar.getnames()), sorted(D.MODULES))

    def test_the_transfer_does_not_silence_its_own_input(self):
        """`ssh -n` points stdin at /dev/null, which would discard the bytes being sent."""
        dep, rec = self._dep()
        dep.up()
        for call in rec.calls:
            if "cat >" in call:
                self.assertNotIn(" -n ", call)

    def test_a_partial_copy_is_refused_rather_than_started(self):
        """A node running code the controller does not have produces ordinary-looking numbers,
        so the mismatch has to stop the deployment."""
        dep, rec = self._dep()
        rec.hash = "deadbeefdeadbeef"
        try:
            dep.up()
            self.fail("a mismatched build must not be accepted")
        except D.DeployError as e:
            self.assertIn("did not land completely", str(e))
        self.assertNotIn(True, [("agent.py --role" in c) for c in rec.calls])

    def test_an_agent_that_will_not_stay_up_is_an_error(self):
        dep, rec = self._dep()
        real = rec.__call__

        def runner(argv, stdin=None):
            line = " ".join(argv)
            if "agent.py --role" in line:
                rec.calls.append(line)
                return 0, "", ""      # accepted, but the process is not there afterwards
            return real(argv, stdin)

        dep._run = runner
        self.assertRaises(D.DeployError, dep.up)

    def test_a_transmitter_is_told_its_address_and_a_receiver_its_identity(self):
        dep, _rec = self._dep()
        by_name = dict((n["name"], n) for n in dep.nodes())
        ap = " ".join(dep._argv_for(by_name["apA"]))
        sta = " ".join(dep._argv_for(by_name["sta1"]))
        self.assertIn("--mac " + AP1, ap)
        self.assertIn("--station-id 1", sta)
        self.assertIn("--ap-macs " + AP1, sta)

    def test_the_token_is_not_left_out_of_the_command(self):
        dep, _rec = self._dep()
        argv = dep._argv_for(dep.nodes()[0])
        self.assertIn("--token", argv)

    def test_a_password_with_a_quote_survives_the_shell(self):
        p = plan()
        for n in p["nodes"]:
            n["password"] = "pa'ss"
        rec = Recorder()
        dep = D.Deployer(p, hub="10.0.0.1", runner=rec)
        dep.up()
        self.assertTrue(any("'pa'\\''ss'" in c for c in rec.calls),
                        "the password must reach sudo intact")


class TestStreamsAreKeptApart(unittest.TestCase):
    """ssh writes banners and warnings to stderr, and they are not command output."""

    def test_a_banner_does_not_look_like_a_running_process(self):
        """Folding stderr into stdout makes every liveness check see text and answer yes, so a
        node is never reported idle and its previous agent is never replaced."""
        rec = Recorder()
        rec.banner = ("** WARNING: connection is not using a post-quantum key exchange\n"
                      "** This session may be vulnerable to attacks.\n")
        rec.running = False
        dep = D.Deployer(plan(), hub="10.0.0.1", runner=rec)
        dep.stop(dep.nodes()[0])          # must not raise: nothing is actually running

    def test_a_banner_does_not_look_like_an_installed_build(self):
        rec = Recorder()
        rec.hash = ""
        dep = D.Deployer(plan(), hub="10.0.0.1", runner=rec)
        self.assertEqual(dep.verify(dep.nodes()[0]), "",
                         "a node with nothing installed must report nothing, not a banner")

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestStopInsists(unittest.TestCase):
    """Two agents answering for one node is worse than killing one outright."""

    def test_a_process_that_ignores_the_first_signal_is_killed(self):
        rec = Recorder()
        rec.running = True
        rec.ignores_term = True
        dep = D.Deployer(plan(), hub="10.0.0.1", runner=rec)
        dep.stop(dep.nodes()[0])
        self.assertTrue(any("pkill -9" in c for c in rec.calls),
                        "stop must escalate rather than give up")
