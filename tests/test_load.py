#!/usr/bin/env python3
"""Unit tests for cosr_ctl.load() -- the pure config resolver.

load() turns a topo.json + shot.json into a validated fire plan with no hardware or ssh, so its
dozen validation branches (bad channel/sync, unknown/mis-roled nodes, duplicate AP, per-link A-MPDU
override, missing keys) and its resolution logic (reference = first link's AP, per-link station and
MCS, clock source per sync mode) are all checkable here. Everything downstream of load() is ssh/pcap
I/O and is exercised live, not in unit tests.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cosr_ctl as cc

BASE_NODES = {
    "apA":  {"role": "ap",      "ip": "10.0.0.11", "iface": "wlan0", "mac": "02:00:00:00:00:11", "ssid": "cosrA"},
    "apB":  {"role": "ap",      "ip": "10.0.0.12", "iface": "wlan0", "mac": "02:00:00:00:00:12", "ssid": "cosrB"},
    "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "wlan0", "station_id": 1},
    "sta2": {"role": "station", "ip": "10.0.0.22", "iface": "wlan0", "station_id": 2},
}


def _plan(topo, shot):
    """Write topo/shot to temp files, run load(), return the plan (files cleaned up after)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(topo, tf)
        topo_path = tf.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as sf:
        json.dump(shot, sf)
        shot_path = sf.name
    try:
        return cc.load(topo_path, shot_path)
    finally:
        os.unlink(topo_path)
        os.unlink(shot_path)


def topo(**over):
    t = {"channel": 1, "password": "modwifi", "observer": "sta1", "nodes": dict(BASE_NODES)}
    t.update(over)
    return t


def shot(**over):
    s = {"nframes": 1, "frame_len": 200, "mcs": 0, "txpower_mbm": 2000,
         "links": [{"ap": "apA", "station": "sta1"}, {"ap": "apB", "station": "sta2"}]}
    s.update(over)
    return s


class TestLoadValid(unittest.TestCase):
    def test_reference_is_first_link_ap(self):
        p = _plan(topo(), shot())
        self.assertEqual(p["aps"][0]["name"], "apA")         # links[0].ap is the timing reference
        self.assertEqual(p["aps"][1]["name"], "apB")

    def test_station_and_id_resolved_per_link(self):
        p = _plan(topo(), shot())
        self.assertEqual(p["aps"][0]["station"]["name"], "sta1")
        self.assertEqual(p["aps"][0]["station_id"], 1)
        self.assertEqual(p["aps"][1]["station_id"], 2)

    def test_per_link_mcs_overrides_shot_default(self):
        p = _plan(topo(), shot(mcs=0, links=[{"ap": "apA", "station": "sta1"},
                                             {"ap": "apB", "station": "sta2", "mcs": 7}]))
        self.assertEqual(p["aps"][0]["mcs"], 0)
        self.assertEqual(p["aps"][0]["rate"], 0x80)          # HT rateCode = 0x80 | mcs
        self.assertEqual(p["aps"][1]["mcs"], 7)
        self.assertEqual(p["aps"][1]["rate"], 0x87)

    def test_station_defaults_to_observer(self):
        p = _plan(topo(), shot(links=[{"ap": "apA"}]))       # no station on the link
        self.assertEqual(p["aps"][0]["station"]["name"], "sta1")

    def test_cap_nodes_are_distinct_receivers(self):
        p = _plan(topo(), shot())
        self.assertEqual(len(p["cap_nodes"]), 2)             # sta1 + sta2
        # same station targeted twice would collapse to one capture node
        p2 = _plan(topo(), shot(links=[{"ap": "apA", "station": "sta1"},
                                       {"ap": "apB", "station": "sta1"}]))
        self.assertEqual(len(p2["cap_nodes"]), 1)

    def test_defaults_filled(self):
        p = _plan(topo(), {"links": [{"ap": "apA", "station": "sta1"}]})
        self.assertEqual(p["lead_us"], cc.LEAD_US_DEFAULT)
        self.assertEqual(p["shots"], cc.SHOTS_DEFAULT)
        self.assertEqual(p["stagger_us"], 0)


class TestClockSource(unittest.TestCase):
    def test_monitor_single_observer_reads_over_ssh(self):
        p = _plan(topo(sync="monitor"), shot())
        self.assertEqual(p["clock_ip"], BASE_NODES["sta1"]["ip"])   # tracker on the observer VM

    def test_beacon_reads_local_file(self):
        p = _plan(topo(sync="beacon"), shot())
        self.assertIsNone(p["clock_ip"])                     # tracker on the control host

    def test_multimonitor_reads_local_file(self):
        p = _plan(topo(sync="monitor", monitors=["sta1", "sta2"]), shot())
        self.assertIsNone(p["clock_ip"])                     # composed on the control host


class TestLoadErrors(unittest.TestCase):
    def _err(self, topo_d, shot_d, needle):
        with self.assertRaises(RuntimeError) as cm:
            _plan(topo_d, shot_d)
        self.assertIn(needle, str(cm.exception))

    def test_bad_channel(self):
        self._err(topo(channel=14), shot(), "2.4 GHz")

    def test_bad_sync(self):
        self._err(topo(sync="ptp"), shot(), "sync")

    def test_no_nodes(self):
        self._err({"channel": 1}, shot(), "nodes")

    def test_unknown_link_ap(self):
        self._err(topo(), shot(links=[{"ap": "apZ", "station": "sta1"}]), "no node named")

    def test_wrong_role(self):
        self._err(topo(), shot(links=[{"ap": "sta1", "station": "sta2"}]), "role")

    def test_duplicate_ap(self):
        self._err(topo(), shot(links=[{"ap": "apA", "station": "sta1"},
                                      {"ap": "apA", "station": "sta2"}]), "two links")

    def test_ampdu_shape_on_link_rejected(self):
        self._err(topo(), shot(links=[{"ap": "apA", "station": "sta1", "nframes": 3}]), "common")

    def test_missing_required_ap_key(self):
        nodes = dict(BASE_NODES)
        nodes["apA"] = {"role": "ap", "ip": "10.0.0.11", "iface": "wlan0"}   # no mac
        self._err(topo(nodes=nodes), shot(), "missing")

    def test_empty_links(self):
        self._err(topo(), shot(links=[]), "no links")

    def test_monitors_must_be_list(self):
        self._err(topo(sync="monitor", monitors="sta1"), shot(), "non-empty list")


if __name__ == "__main__":
    unittest.main(verbosity=2)
