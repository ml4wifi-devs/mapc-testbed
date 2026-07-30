#!/usr/bin/env python3
"""cosr.scan: reading each node's wireless interface and address.

The ssh transport is replaced, so the parsing and the output shape are exercised without nodes.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cosr import scan as S


class Fake(object):
    """Answers the two commands scan issues, per address."""

    def __init__(self, ifaces, addresses, silent=()):
        self.ifaces = ifaces
        self.addresses = addresses
        self.silent = set(silent)
        self.calls = []

    def __call__(self, ip, user, password, command):
        self.calls.append((ip, command))
        if ip in self.silent:
            return ""
        if "iw dev" in command:
            return "\n".join(self.ifaces.get(ip, [])) + "\n"
        for name in self.ifaces.get(ip, []):
            if "/net/%s/" % name in command:
                return self.addresses.get((ip, name), "") + "\n"
        return ""


class TestScan(unittest.TestCase):

    def setUp(self):
        self._real = S._ssh

    def tearDown(self):
        S._ssh = self._real

    def _install(self, fake):
        S._ssh = fake
        return fake

    def test_one_interface_per_node(self):
        self._install(Fake({"10.0.0.1": ["wlan0"]},
                           {("10.0.0.1", "wlan0"): "AA:BB:CC:DD:EE:FF"}))
        self.assertEqual(S.scan(["10.0.0.1"], "u", "p"),
                         [("10.0.0.1", "wlan0", "aa:bb:cc:dd:ee:ff")])

    def test_the_address_is_lowercased(self):
        """topo.json is compared against addresses that arrive lowercased from the radio."""
        self._install(Fake({"10.0.0.1": ["wlan0"]},
                           {("10.0.0.1", "wlan0"): "AA:BB:CC:DD:EE:FF"}))
        self.assertEqual(S.scan(["10.0.0.1"], "u", "p")[0][2], "aa:bb:cc:dd:ee:ff")

    def test_several_interfaces_are_all_reported(self):
        self._install(Fake({"10.0.0.1": ["wlan0", "wlan1"]},
                           {("10.0.0.1", "wlan0"): "aa:00:00:00:00:01",
                            ("10.0.0.1", "wlan1"): "aa:00:00:00:00:02"}))
        rows = S.scan(["10.0.0.1"], "u", "p")
        self.assertEqual([r[1] for r in rows], ["wlan0", "wlan1"])

    def test_a_node_that_does_not_answer_is_listed_rather_than_dropped(self):
        """Silently omitting it would read as a node with no wireless interface."""
        self._install(Fake({}, {}, silent=["10.0.0.9"]))
        self.assertEqual(S.scan(["10.0.0.9"], "u", "p"), [("10.0.0.9", None, None)])

    def test_the_address_is_read_from_the_kernel_not_from_iw(self):
        """`iw` can report an address that is no longer the one in use."""
        fake = self._install(Fake({"10.0.0.1": ["wlan0"]},
                                  {("10.0.0.1", "wlan0"): "aa:00:00:00:00:01"}))
        S.scan(["10.0.0.1"], "u", "p")
        self.assertTrue(any("/sys/class/net/wlan0/address" in c for _ip, c in fake.calls))


class TestReport(unittest.TestCase):

    def _lines(self, rows):
        out = []
        S.report(rows, out.append)
        return out

    def test_every_node_appears(self):
        lines = self._lines([("10.0.0.1", "wlan0", "aa:00:00:00:00:01"),
                             ("10.0.0.2", "wlan1", "aa:00:00:00:00:02")])
        self.assertTrue(any("10.0.0.1" in l and "wlan0" in l for l in lines))
        self.assertTrue(any("10.0.0.2" in l and "wlan1" in l for l in lines))

    def test_an_unreachable_node_says_so_rather_than_showing_blanks(self):
        lines = self._lines([("10.0.0.9", None, None)])
        self.assertTrue(any("unreachable" in l for l in lines))

    def test_the_stubs_are_valid_json_once_wrapped(self):
        """They are meant to be pasted into topo.json, so they have to parse there."""
        import json
        lines = self._lines([("10.0.0.1", "wlan0", "aa:00:00:00:00:01"),
                             ("10.0.0.2", "wlan1", "aa:00:00:00:00:02")])
        stubs = [l for l in lines if l.startswith('"node')]
        self.assertEqual(len(stubs), 2)
        parsed = json.loads("{" + "".join(stubs).rstrip(",") + "}")
        self.assertEqual(parsed["node1"]["ip"], "10.0.0.1")
        self.assertEqual(parsed["node2"]["mac"], "aa:00:00:00:00:02")

    def test_an_unreachable_node_produces_no_stub(self):
        lines = self._lines([("10.0.0.9", None, None)])
        self.assertEqual([l for l in lines if l.startswith('"node')], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
