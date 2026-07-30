#!/usr/bin/env python3
"""cosr.topo.resolve(): turning the two description files into a validated plan.

Every refusal is part of the interface -- it is what tells someone their description is wrong
rather than letting it fail later against the hardware -- so the messages are pinned here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import topo as T
from cosr import wire as W

BASE_NODES = {
    "apA":  {"role": "ap", "ip": "10.0.0.11", "iface": "wlan0",
             "mac": "02:00:00:00:00:11", "ssid": "cosrA"},
    "apB":  {"role": "ap", "ip": "10.0.0.12", "iface": "wlan0",
             "mac": "02:00:00:00:00:12", "ssid": "cosrB"},
    "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "wlan0", "station_id": 1},
    "sta2": {"role": "station", "ip": "10.0.0.22", "iface": "wlan0", "station_id": 2},
}


def topo(**over):
    t = {"channel": 1, "password": "modwifi", "observer": "sta1", "nodes": dict(BASE_NODES)}
    t.update(over)
    return t


def link(ap, station=None, mcs=0, txpower_dbm=20):
    out = {"ap": ap, "mcs": mcs, "txpower_dbm": txpower_dbm}
    if station is not None:
        out["station"] = station
    return out


def shot(**over):
    s = {"nframes": 1, "frame_len": 200,
         "links": [link("apA", "sta1"), link("apB", "sta2")]}
    s.update(over)
    return s


class TestResolveValid(unittest.TestCase):

    def test_reference_is_first_link_ap(self):
        p = T.resolve(topo(), shot())
        self.assertEqual(p["aps"][0]["name"], "apA")
        self.assertEqual(p["aps"][1]["name"], "apB")
        self.assertEqual(p["reference_mac"], "02:00:00:00:00:11")

    def test_station_and_id_resolved_per_link(self):
        p = T.resolve(topo(), shot())
        self.assertEqual(p["aps"][0]["station"]["name"], "sta1")
        self.assertEqual(p["aps"][0]["station_id"], 1)
        self.assertEqual(p["aps"][1]["station_id"], 2)

    def test_each_link_carries_its_own_rate(self):
        p = T.resolve(topo(), shot(links=[link("apA", "sta1", mcs=0),
                                          link("apB", "sta2", mcs=7)]))
        self.assertEqual(p["aps"][0]["mcs"], 0)
        self.assertEqual(p["aps"][0]["rate"], 0x80)
        self.assertEqual(p["aps"][1]["mcs"], 7)
        self.assertEqual(p["aps"][1]["rate"], 0x87)

    def test_station_defaults_to_observer(self):
        p = T.resolve(topo(), shot(links=[link("apA")]))
        self.assertEqual(p["aps"][0]["station"]["name"], "sta1")

    def test_defaults_filled(self):
        p = T.resolve(topo(), {"links": [{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}]})
        self.assertEqual(p["lead_us"], T.LEAD_US_DEFAULT)
        self.assertEqual(p["repeats"], T.REPEATS_DEFAULT)
        self.assertEqual(p["spacing_us"], T.SPACING_US_DEFAULT)
        self.assertEqual(p["stagger_us"], 0)

    def test_macs_are_lowercased(self):
        nodes = dict(BASE_NODES)
        nodes["apA"] = dict(nodes["apA"], mac="02:00:00:00:00:AA")
        p = T.resolve(topo(nodes=nodes), shot())
        self.assertEqual(p["aps"][0]["mac"], "02:00:00:00:00:aa")

    def test_spacing_us_overridable(self):
        self.assertEqual(T.resolve(topo(), shot(spacing_us=8000))["spacing_us"], 8000)

    def test_channel_frequency_is_derived(self):
        """The receiver reports a frequency, not a channel number, so the plan must carry one
        to compare against or the drift check silently never fires."""
        self.assertEqual(T.resolve(topo(channel=1), shot())["channel_freq_mhz"], 2412)
        self.assertEqual(T.resolve(topo(channel=6), shot())["channel_freq_mhz"], 2437)
        self.assertEqual(T.resolve(topo(channel=13), shot())["channel_freq_mhz"], 2472)

    def test_channel_frequency_matches_real_capture(self):
        """Channel 1 in a topo must equal what a card on channel 1 actually reports."""
        from tests.test_radiotap import load_fixture
        from cosr import radiotap as R
        frames, _ = load_fixture("beacons")
        seen = set(R.parse(b)["freq_mhz"] for b in frames)
        self.assertEqual(seen, set([T.channel_to_freq_mhz(1)]))


class TestCredentials(unittest.TestCase):
    """Carried per node in the plan, never in a module-level global: a global would let a helper
    running before resolution use one role's password against the other's node, and would make
    two descriptions resolved in one process contaminate each other."""

    def _login(self, plan, ip):
        for n in plan["nodes"]:
            if n["ip"] == ip:
                return (n["user"], n["password"])
        self.fail("no node at %s" % ip)

    def test_per_role_credentials(self):
        p = T.resolve(topo(ap_user="modwifi", ap_password="appw",
                           monitor_user="pi", monitor_password="stapw"), shot())
        self.assertEqual(self._login(p, "10.0.0.11"), ("modwifi", "appw"))
        self.assertEqual(self._login(p, "10.0.0.21"), ("pi", "stapw"))

    def test_flat_password_fallback(self):
        p = T.resolve(topo(password="shared"), shot())
        self.assertEqual(self._login(p, "10.0.0.11"), ("modwifi", "shared"))
        self.assertEqual(self._login(p, "10.0.0.21"), ("modwifi", "shared"))

    def test_image_default_fallback(self):
        t = {"channel": 1, "observer": "sta1", "nodes": dict(BASE_NODES)}
        p = T.resolve(t, shot())
        self.assertEqual(self._login(p, "10.0.0.11"), (T.DEFAULT_USER, T.DEFAULT_PW))

    def test_two_topologies_do_not_contaminate(self):
        a = T.resolve(topo(password="one"), shot())
        b = T.resolve(topo(password="two"), shot())
        self.assertEqual(self._login(a, "10.0.0.11")[1], "one")
        self.assertEqual(self._login(b, "10.0.0.11")[1], "two")

    def test_all_nodes_listed_for_deployment(self):
        p = T.resolve(topo(), shot())
        self.assertEqual(sorted(n["name"] for n in p["nodes"]),
                         ["apA", "apB", "sta1", "sta2"])
        roles = dict((n["name"], n["role"]) for n in p["nodes"])
        self.assertEqual(roles["apA"], "ap")
        self.assertEqual(roles["sta1"], "station")


class TestSyncModes(unittest.TestCase):
    """Either source of observations resolves, and neither adds anything to the plan beyond
    naming which one the deployment relies on."""

    def test_the_declared_source_is_recorded(self):
        """`doctor` compares this against the observations actually arriving, so a deployment
        that names one source and runs on the other is caught rather than quietly accepted."""
        self.assertEqual(T.resolve(topo(sync="beacon"), shot())["sync"], "beacon")
        self.assertEqual(
            T.resolve(topo(sync="monitor", monitors=["sta1"]), shot())["sync"], "monitor")

    def test_an_unrecognised_source_is_refused(self):
        self.assertRaises(RuntimeError, T.resolve, topo(sync="gps"), shot())

    def test_beacon_is_the_default(self):
        """The mode that needs no hardware beyond the transmitters themselves."""
        t = topo()
        t.pop("sync", None)
        self.assertEqual(T.resolve(t, shot())["sync"], "beacon")

    def test_beacon_mode(self):
        p = T.resolve(topo(sync="beacon"), shot())
        self.assertEqual(p["sync"], "beacon")
        self.assertEqual(p["monitors"], [])

    def test_monitor_mode_lists_monitors(self):
        p = T.resolve(topo(sync="monitor", monitors=["sta1", "sta2"]), shot())
        self.assertEqual(p["sync"], "monitor")
        self.assertEqual(p["monitors"], ["sta1", "sta2"])

    def test_monitor_mode_requires_the_list(self):
        """Naming a source with no publishers is a description that cannot hold."""
        try:
            T.resolve(topo(sync="monitor"), shot())
            self.fail("monitor mode with no monitors must not resolve")
        except RuntimeError as e:
            self.assertIn("no 'monitors' are listed", str(e))


class TestPoolBounds(unittest.TestCase):
    """An over-large aggregate is refused here rather than surfacing as a no-buffer result at
    transmit time."""

    def test_documented_shapes_accepted(self):
        for n, l in ((1, 200), (5, 300), (10, 150), (15, 100)):
            T.resolve(topo(), shot(nframes=n, frame_len=l))

    def test_oversized_shape_rejected(self):
        with self.assertRaises(ValueError) as cm:
            T.resolve(topo(), shot(nframes=6, frame_len=300))
        self.assertIn("POOL_ID_ATTACKS", str(cm.exception))



class TestResolveErrors(unittest.TestCase):

    def _err(self, topo_d, shot_d, needle):
        with self.assertRaises(RuntimeError) as cm:
            T.resolve(topo_d, shot_d)
        self.assertIn(needle, str(cm.exception))

    def test_bad_channel(self):
        self._err(topo(channel=14), shot(), "2.4 GHz")

    def test_bad_sync(self):
        self._err(topo(sync="ptp"), shot(), "sync")

    def test_no_nodes(self):
        self._err({"channel": 1}, shot(), "nodes")

    def test_unknown_link_ap(self):
        self._err(topo(), shot(links=[{"ap": "apZ", "station": "sta1", "mcs": 0, "txpower_dbm": 20}]), "no node named")

    def test_wrong_role(self):
        self._err(topo(), shot(links=[{"ap": "sta1", "station": "sta2"}]), "role")

    def test_duplicate_ap(self):
        self._err(topo(), shot(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20},
                                      {"ap": "apA", "station": "sta2", "mcs": 0, "txpower_dbm": 20}]), "two links")

    def test_ampdu_shape_on_link_rejected(self):
        self._err(topo(), shot(links=[{"ap": "apA", "station": "sta1", "nframes": 3}]), "common")

    def test_missing_required_ap_key(self):
        nodes = dict(BASE_NODES)
        nodes["apA"] = {"role": "ap", "ip": "10.0.0.11", "iface": "wlan0"}
        self._err(topo(nodes=nodes), shot(), "missing")

    def test_missing_required_station_key(self):
        nodes = dict(BASE_NODES)
        nodes["sta1"] = {"role": "station", "ip": "10.0.0.21", "iface": "wlan0"}
        self._err(topo(nodes=nodes), shot(), "missing")

    def test_empty_links(self):
        self._err(topo(), shot(links=[]), "no links")

    def test_no_links_key(self):
        self._err(topo(), {"nframes": 1}, "no links")

    def test_monitors_must_be_list(self):
        self._err(topo(sync="monitor", monitors="sta1"), shot(), "non-empty list")

    def test_monitors_must_be_nonempty(self):
        self._err(topo(sync="monitor", monitors=[]), shot(), "no 'monitors' are listed")

    def test_unknown_monitor_name(self):
        self._err(topo(sync="monitor", monitors=["staZ"]), shot(), "no node named")

    def test_link_without_station_and_no_observer(self):
        t = topo()
        del t["observer"]
        self._err(t, shot(links=[{"ap": "apA"}]), "no observer")


class TestClockParticipants(unittest.TestCase):
    """The clock plane spans the whole deployment, not just the transmitters in one shot."""

    def test_all_transmitters_are_listed(self):
        p = T.resolve(topo(), shot(links=[{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}]))
        self.assertEqual(p["ap_macs"], ["02:00:00:00:00:11"])
        self.assertEqual(sorted(p["all_ap_macs"]),
                         ["02:00:00:00:00:11", "02:00:00:00:00:12"])

    def test_matches_ap_macs_when_all_transmit(self):
        p = T.resolve(topo(), shot())
        self.assertEqual(sorted(p["ap_macs"]), sorted(p["all_ap_macs"]))


class TestLeadBounds(unittest.TestCase):
    """The gate refuses an instant beyond its own ceiling, so the plan refuses one first."""

    def test_a_lead_past_the_gate_ceiling_is_refused(self):
        try:
            T.resolve(topo(), shot(lead_us=W.MAX_LEAD_US + 1))
            self.fail("a lead the gate cannot accept must not reach the hardware")
        except RuntimeError as e:
            self.assertIn("too far away", str(e))

    def test_the_ceiling_itself_is_allowed(self):
        self.assertEqual(T.resolve(topo(), shot(lead_us=W.MAX_LEAD_US))["lead_us"],
                         W.MAX_LEAD_US)

    def test_a_nonsensical_lead_is_refused(self):
        self.assertRaises(RuntimeError, T.resolve, topo(), shot(lead_us=0))

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPerLinkSettingsAreMandatory(unittest.TestCase):
    """Rate and power are what an experiment varies between links, so a common default would
    let a link that was never configured look identical to one that was."""

    def test_a_link_without_a_rate_is_refused(self):
        try:
            T.resolve(topo(), shot(links=[{"ap": "apA", "station": "sta1",
                                           "txpower_dbm": 20}]))
            self.fail("a link with no rate must not resolve")
        except RuntimeError as e:
            self.assertIn("no mcs", str(e))

    def test_a_link_without_a_power_is_refused(self):
        try:
            T.resolve(topo(), shot(links=[{"ap": "apA", "station": "sta1", "mcs": 0}]))
            self.fail("a link with no power must not resolve")
        except RuntimeError as e:
            self.assertIn("no txpower_dbm", str(e))

    def test_a_fractional_power_is_refused(self):
        """The radio discards such a request in silence and stays at the previous power, so the
        round would report OK at a level it was never given."""
        for dbm in (13.5, 9.5, 12.55):
            try:
                T.resolve(topo(), shot(links=[link("apA", "sta1", txpower_dbm=dbm)]))
                self.fail("%r dBm must not resolve" % dbm)
            except RuntimeError as e:
                self.assertIn("whole number of dBm", str(e))

    def test_a_power_outside_the_radio_range_is_refused(self):
        for dbm in (21, 25, -1):
            try:
                T.resolve(topo(), shot(links=[link("apA", "sta1", txpower_dbm=dbm)]))
                self.fail("%d dBm must not resolve" % dbm)
            except RuntimeError as e:
                self.assertIn("covers 0..20", str(e))

    def test_a_power_that_is_not_a_number_is_refused(self):
        for bad in ("20", None, True):
            self.assertRaises(RuntimeError, T.resolve, topo(),
                              shot(links=[link("apA", "sta1", txpower_dbm=bad)]))

    def test_a_whole_dbm_power_resolves_and_carries_both_units(self):
        for dbm in (0, 5, 13, 20):
            p = T.resolve(topo(), shot(links=[link("apA", "sta1", txpower_dbm=dbm)]))
            self.assertEqual(p["aps"][0]["txpower_dbm"], dbm)
            self.assertEqual(p["aps"][0]["txpower_mbm"], dbm * 100)

    def test_an_mcs_outside_the_radio_range_is_refused(self):
        """Single-stream 20 MHz 802.11n has MCS 0..7; anything else is not sent as asked."""
        for mcs in (8, 15, -1):
            try:
                T.resolve(topo(), shot(links=[link("apA", "sta1", mcs=mcs)]))
                self.fail("mcs %d must not resolve" % mcs)
            except RuntimeError as e:
                self.assertIn("0..7", str(e))

    def test_a_fractional_mcs_is_refused(self):
        self.assertRaises(RuntimeError, T.resolve, topo(),
                          shot(links=[link("apA", "sta1", mcs=3.5)]))

    def test_a_top_level_value_does_not_stand_in_for_a_link(self):
        """Naming them once at the top level is exactly the ambiguity this removes."""
        self.assertRaises(RuntimeError, T.resolve, topo(),
                          shot(mcs=7, txpower_dbm=20,
                               links=[{"ap": "apA", "station": "sta1"}]))
