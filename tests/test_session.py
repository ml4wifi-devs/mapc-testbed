#!/usr/bin/env python3
"""cosr.session: assembling a round and interpreting the replies.

The message bus is substituted by a loopback that hands published rounds to fake participants,
so the whole controller path -- clock, instruction, collection, accounting -- runs without a
testbed.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import (accounting, agent as agent_mod, clockd as clockd_mod,
                  clockgraph as cg, proto, session as S, topo as T, wire)

AP1 = "02:00:00:00:00:11"
AP2 = "02:00:00:00:00:12"

NODES = {
    "apA":  {"role": "ap", "ip": "10.0.0.11", "iface": "wlan0", "mac": AP1},
    "apB":  {"role": "ap", "ip": "10.0.0.12", "iface": "wlan0", "mac": AP2},
    "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "wlan0", "station_id": 1},
    "sta2": {"role": "station", "ip": "10.0.0.22", "iface": "wlan0", "station_id": 2},
}


def make_plan(links=None, nframes=1, mcs=4, txpower_dbm=20):
    shot = {"nframes": nframes, "frame_len": 200,
            "repeats": 4, "spacing_us": 15000, "lead_us": 150000,
            "links": links or [
                {"ap": "apA", "station": "sta1", "mcs": mcs, "txpower_dbm": txpower_dbm},
                {"ap": "apB", "station": "sta2", "mcs": mcs, "txpower_dbm": txpower_dbm}]}
    return T.resolve({"channel": 1, "observer": "sta1", "nodes": NODES}, shot)


class FakeBus(object):
    """Delivers a published round straight to fake participants and routes their replies back."""

    def __init__(self):
        self.subs = {}
        self.published = []
        self.session = None
        self.responders = {}
        self.deliver = True

    def subscribe_json(self, subject, cb):
        self.subs[subject] = cb
        return subject

    def _match(self, subject):
        for pat, cb in self.subs.items():
            if pat == subject:
                return cb
            if pat.endswith(".*") and subject.startswith(pat[:-1]):
                return cb
        return None

    def publish_json(self, subject, obj, reply=None):
        self.published.append((subject, obj))
        if subject == proto.SUBJ_FIRE and self.deliver:
            for name, fn in sorted(self.responders.items()):
                out = fn(obj)
                if out is None:
                    continue
                target = out[0]
                cb = self._match(target)
                if cb:
                    cb(target, out[1], None)

    def request_json(self, subject, obj, timeout=None):
        for (role, name), h in list(self.rpc.items()) if hasattr(self, "rpc") else []:
            if subject == proto.subj_rpc(role, name):
                return h
        return {"ok": True, "status": {"source_hash": agent_mod.source_hash(),
                                       "name": subject}}

    def ping(self, timeout=None):
        return True

    def close(self):
        pass

    def is_connected(self):
        return True


def fake_ap(name, mac, fired="all"):
    def respond(msg):
        mine = msg["aps"].get(name)
        if mine is None:
            return None
        seqs = [wire.SeqAllocator.wire(msg["seq0"] + i) for i in range(msg["n"])]
        if fired == "all":
            shots = dict((s, accounting.FIRED) for s in seqs)
        elif fired == "none":
            shots = dict((s, accounting.TOOFAR) for s in seqs)
        else:
            shots = {}
            for i, s in enumerate(seqs):
                shots[s] = accounting.FIRED if i in fired else accounting.NOBF
        st = proto.build_status(msg["run"], msg["batch"], name, "e1", shots)
        return (proto.subj_status(name), st)
    return respond


def fake_station(name, mac_rx):
    """mac_rx maps an address to how many subframes of each fired shot it received."""
    def respond(msg):
        mine = msg["stations"].get(name)
        if mine is None:
            return None
        seqs = [wire.SeqAllocator.wire(msg["seq0"] + i) for i in range(msg["n"])]
        per_ap = {}
        for mac in mine["ap_macs"]:
            k = mac_rx.get(mac, 0)
            per_seq = dict((s, k) for s in seqs) if k else {}
            per_ap[mac] = {
                "rx": k * len(seqs), "coverage": "full",
                "idx_hist": dict((i, len(seqs)) for i in range(k)),
                "seqs_seen": list(per_seq), "per_seq": per_seq,
                "rssi_dbm": -55.0 if k else None,
            }
        counts = {"epoch": "e1", "ordinal": 10, "other_frames": 30, "other_frames_round": 30, "drops": 0,
                  "freq_mhz": 2412, "per_ap": per_ap}
        return (proto.subj_report(name), proto.build_report(msg["run"], msg["batch"],
                                                            name, counts))
    return respond


def ready_clock(plan):
    """A clock graph good enough to place an instant."""
    cd = clockd_mod.ClockD(plan["reference_mac"])
    # A reception is stamped at the end of the frame, so the receive time must trail the
    # transmit instant by exactly the duration the ingest corrects for. Any other value makes
    # the two directions of the same link disagree about the offset between the clocks.
    air = cg.ppdu_airtime_us(0, 124)
    for i in range(30):
        t = 1000000000 + i * 100000
        cd.observe_peer_beacon(AP1, AP2, t1=int(t * 1.000002 + 1e6),
                               t2=int(t + air), rate_idx=0, length=124)
        cd.observe_peer_beacon(AP2, AP1, t1=int(t),
                               t2=int(t * 1.000002 + 1e6 + air), rate_idx=0, length=124)
    return cd


def make_session(plan=None, aps=None, stations=None):
    plan = plan or make_plan()
    bus = FakeBus()
    sess = S.Session(plan, client=bus, clockd=ready_clock(plan))
    sess.connect()
    bus.responders = {}
    for name, fn in (aps or {}).items():
        bus.responders[name] = fn
    for name, fn in (stations or {}).items():
        bus.responders[name] = fn
    return sess, bus


class TestRound(unittest.TestCase):

    def _both_deliver(self, **kw):
        plan = kw.pop("plan", None) or make_plan()
        return make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})

    def test_full_delivery(self):
        sess, _bus = self._both_deliver()
        r = sess.run()
        self.assertEqual(r["measured_links"], 2)
        for e in r["links"].values():
            self.assertEqual(e["status"], accounting.OK)
            self.assertEqual(e["delivery"], 1.0)

    def test_one_message_carries_the_whole_round(self):
        sess, bus = self._both_deliver()
        sess.run()
        fires = [p for p in bus.published if p[0] == proto.SUBJ_FIRE]
        self.assertEqual(len(fires), 1, "a round must cost exactly one instruction")

    def test_sequence_ranges_do_not_repeat_between_rounds(self):
        sess, bus = self._both_deliver()
        a = sess.run()
        b = sess.run()
        self.assertNotEqual(a["seq0"], b["seq0"])
        self.assertGreaterEqual(b["seq0"], a["seq0"] + a["repeats"])

    def test_batch_identifier_advances(self):
        sess, _bus = self._both_deliver()
        self.assertEqual(sess.run()["batch"] + 1, sess.run()["batch"])

    def test_partial_fire_shrinks_the_denominator(self):
        sess, _bus = make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2, fired=[0, 1])},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        r = sess.run()
        self.assertEqual(r["links"]["apA->sta1"]["joint_shots"], 2)
        self.assertEqual(r["links"]["apA->sta1"]["sent"], 2)
        self.assertEqual(r["links"]["apA->sta1"]["delivery"], 1.0)

    def test_nothing_fired_is_not_a_loss(self):
        sess, _bus = make_session(
            aps={"apA": fake_ap("apA", AP1, fired="none"),
                 "apB": fake_ap("apB", AP2, fired="none")},
            stations={"sta1": fake_station("sta1", {AP1: 0}),
                      "sta2": fake_station("sta2", {AP2: 0})})
        r = sess.run()
        for e in r["links"].values():
            self.assertEqual(e["status"], accounting.NOT_FIRED)
            self.assertIsNone(e["delivery"])
        self.assertEqual(r["measured_links"], 0)

    def test_a_silent_participant_is_named(self):
        sess, _bus = make_session(
            aps={"apA": fake_ap("apA", AP1)},               # apB never answers
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        with self.assertRaises(S.NodeMissing) as cm:
            sess.run()
        self.assertIn("apB", str(cm.exception))

    def test_round_reports_its_own_duration(self):
        sess, _bus = self._both_deliver()
        self.assertGreaterEqual(sess.run()["seconds"], 0.0)


class TestPerLinkSettings(unittest.TestCase):
    """Modulation and power are chosen per link; the aggregate shape and timing are shared."""

    def test_per_link_mcs_reaches_the_instruction(self):
        plan = make_plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0,
                                 "txpower_dbm": 20},
                                {"ap": "apB", "station": "sta2", "mcs": 7,
                                 "txpower_dbm": 20}])
        sess, bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        sess.run()
        fire = [p[1] for p in bus.published if p[0] == proto.SUBJ_FIRE][0]
        self.assertEqual(fire["aps"]["apA"]["rate"], wire.rate_for_mcs(0))
        self.assertEqual(fire["aps"]["apB"]["rate"], wire.rate_for_mcs(7))

    def test_per_link_power_reaches_the_instruction(self):
        plan = make_plan(links=[{"ap": "apA", "station": "sta1", "mcs": 0,
                                 "txpower_dbm": 20},
                                {"ap": "apB", "station": "sta2", "mcs": 0,
                                 "txpower_dbm": 11}])
        sess, bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        sess.run()
        fire = [p[1] for p in bus.published if p[0] == proto.SUBJ_FIRE][0]
        self.assertEqual(fire["aps"]["apA"]["txpower_mbm"], 2000)
        self.assertEqual(fire["aps"]["apB"]["txpower_mbm"], 1100)

    def test_round_overrides_beat_the_configured_values(self):
        """A search over settings varies them per link every round."""
        sess, bus = make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        sess.run(mcs_by_ap={"apA": 1, "apB": 6},
                  power_by_ap={"apA": 1400, "apB": 1700})
        fire = [p[1] for p in bus.published if p[0] == proto.SUBJ_FIRE][0]
        self.assertEqual(fire["aps"]["apA"]["rate"], wire.rate_for_mcs(1))
        self.assertEqual(fire["aps"]["apB"]["rate"], wire.rate_for_mcs(6))
        self.assertEqual(fire["aps"]["apA"]["txpower_mbm"], 1400)
        self.assertEqual(fire["aps"]["apB"]["txpower_mbm"], 1700)

    def test_aggregate_shape_and_timing_are_shared(self):
        plan = make_plan(nframes=5)
        plan["frame_len"] = 300
        sess, bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 5}),
                      "sta2": fake_station("sta2", {AP2: 5})})
        sess.run()
        fire = [p[1] for p in bus.published if p[0] == proto.SUBJ_FIRE][0]
        self.assertEqual(fire["nframes"], 5)
        self.assertEqual(fire["frame_len"], 300)
        self.assertIn("spacing_us", fire)
        self.assertIn("lead_us", fire)
        for entry in fire["aps"].values():
            self.assertNotIn("nframes", entry, "the aggregate shape is not per link")
            self.assertNotIn("frame_len", entry)

    def test_per_link_shape_is_refused_at_configuration_time(self):
        with self.assertRaises(RuntimeError) as cm:
            make_plan(links=[{"ap": "apA", "station": "sta1", "nframes": 3}])
        self.assertIn("common", str(cm.exception))


class TestFreshnessBarrier(unittest.TestCase):

    def test_previous_round_ordinal_is_carried_forward(self):
        sess, bus = make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        sess.run()
        sess.run()
        fires = [p[1] for p in bus.published if p[0] == proto.SUBJ_FIRE]
        self.assertEqual(fires[0]["stations"]["sta1"]["since_ordinal"], 0)
        self.assertEqual(fires[1]["stations"]["sta1"]["since_ordinal"], 10,
                         "the second round must exclude what the first already counted")


class TestClockGate(unittest.TestCase):

    def test_a_round_is_refused_before_the_clocks_relate(self):
        plan = make_plan()
        bus = FakeBus()
        sess = S.Session(plan, client=bus, clockd=clockd_mod.ClockD(plan["reference_mac"]))
        sess.connect()
        self.assertRaises(clockd_mod.ClockNotReady, sess.run)

    def test_beacon_observations_feed_the_clock(self):
        plan = make_plan()
        bus = FakeBus()
        sess = S.Session(plan, client=bus, clockd=clockd_mod.ClockD(plan["reference_mac"]))
        sess.connect()
        rows = []
        for i in range(30):
            t = 1000000000 + i * 100000
            rows.append({"hearer": AP1, "sender": AP2, "t1": int(t * 1.000002 + 1e6),
                         "t2": int(t + 1200), "rate_idx": 0, "len": 124})
        bus.subs[proto.SUBJ_BEACON](proto.SUBJ_BEACON,
                                    {"v": proto.VERSION, "node": "apA", "rows": rows}, None)
        self.assertEqual(sess.beacon_rows, 30)
        self.assertIn(AP2, sess.clockd.status()["reachable"])


def timing_station(name, tsft_by_ap):
    """A receiver that reports arrival times, which is what the timing measurements pair on."""
    def respond(msg):
        mine = msg["stations"].get(name)
        if mine is None:
            return None
        seqs = [wire.SeqAllocator.wire(msg["seq0"] + i) for i in range(msg["n"])]
        per_ap = {}
        for mac in mine["ap_macs"]:
            fn = tsft_by_ap.get(mac)
            tsft = {}
            if fn is not None:
                for i, s in enumerate(seqs):
                    v = fn(i)
                    if v is not None:
                        tsft[s] = v
            per_ap[mac] = {"rx": len(tsft), "coverage": "full",
                           "idx_hist": {0: len(tsft)},
                           "seqs_seen": sorted(tsft), "per_seq": dict((s, 1) for s in tsft),
                           "tsft_by_seq": tsft, "rssi_dbm": -55.0}
        counts = {"epoch": "e1", "ordinal": 5, "other_frames": 30, "other_frames_round": 30, "drops": 0,
                  "freq_mhz": 2412, "per_ap": per_ap}
        return (proto.subj_report(name), proto.build_report(msg["run"], msg["batch"],
                                                            name, counts))
    return respond


def timing_plan(**over):
    nodes = dict(NODES)
    shot = {"nframes": 1, "frame_len": 200, "mcs": 0, "repeats": 10, "spacing_us": 15000,
            "lead_us": 150000,
            "links": [{"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 20}, {"ap": "apB", "station": "sta1", "mcs": 0, "txpower_dbm": 20}]}
    shot.update(over)
    return T.resolve({"channel": 1, "observer": "sta1", "nodes": nodes}, shot)


class TestSyncMeasurement(unittest.TestCase):

    def _session(self, offset_fn):
        plan = timing_plan()
        return make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": timing_station("sta1", {
                AP1: lambda i: 1000000 + i * 15000,
                AP2: lambda i: 1000000 + i * 15000 + offset_fn(i)})})

    def test_matched_stagger_gives_no_error(self):
        sess, _bus = self._session(lambda i: 50000)
        r = sess.sync(repeats=10, stagger_us=50000)
        s = r["per_ap"]["apB"]
        self.assertEqual(s["paired"], 10)
        self.assertAlmostEqual(s["bias_us"], 0.0, places=6)
        self.assertAlmostEqual(s["jitter_us"], 0.0, places=6)

    def test_a_constant_displacement_shows_as_bias(self):
        sess, _bus = self._session(lambda i: 50000 + 7)
        self.assertAlmostEqual(sess.sync(repeats=10, stagger_us=50000)["per_ap"]["apB"]["bias_us"],
                               7.0, places=6)

    def test_scatter_shows_as_spread(self):
        sess, _bus = self._session(lambda i: 50000 + (3 if i % 2 else -3))
        s = sess.sync(repeats=10, stagger_us=50000)["per_ap"]["apB"]
        self.assertAlmostEqual(s["bias_us"], 0.0, places=6)
        self.assertAlmostEqual(s["jitter_us"], 3.0, places=6)

    def test_every_transmitter_addresses_the_observer(self):
        """One receiver must see them all, or its own clock difference enters the answer."""
        sess, bus = self._session(lambda i: 50000)
        sess.sync(repeats=10, stagger_us=50000)
        fire = [p[1] for p in bus.published if p[0] == proto.SUBJ_FIRE][0]
        ids = set(e["station_id"] for e in fire["aps"].values())
        self.assertEqual(ids, set([1]))
        self.assertEqual(set(fire["stations"]), set(["sta1"]))

    def test_a_single_frame_per_shot_is_forced(self):
        """Later subframes share the opening one's arrival stamp and carry no timing."""
        plan = timing_plan(nframes=5, frame_len=300)
        sess, bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": timing_station("sta1", {
                AP1: lambda i: 1000 + i, AP2: lambda i: 1050 + i})})
        sess.sync(repeats=5, stagger_us=50)
        fire = [p[1] for p in bus.published if p[0] == proto.SUBJ_FIRE][0]
        self.assertEqual(fire["nframes"], 1)


    def test_refused_with_one_transmitter(self):
        plan = T.resolve({"channel": 1, "observer": "sta1", "nodes": dict(NODES)},
                         {"nframes": 1, "frame_len": 200, "repeats": 4,
                          "links": [{"ap": "apA", "station": "sta1", "mcs": 0,
                                     "txpower_dbm": 20}]})
        sess, _bus = make_session(plan, aps={"apA": fake_ap("apA", AP1)},
                                  stations={"sta1": timing_station("sta1", {AP1: lambda i: i})})
        with self.assertRaises(RuntimeError) as cm:
            sess.sync()
        self.assertIn("two transmitters", str(cm.exception))


class TestSyncRequiresSeparation(unittest.TestCase):
    """With no separation the transmissions overlap at the receiver and neither is decoded, so
    the measurement has almost nothing to pair and would report noise."""

    def test_zero_separation_is_refused(self):
        plan = timing_plan()
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": timing_station("sta1", {AP1: lambda i: i, AP2: lambda i: i})})
        with self.assertRaises(RuntimeError) as cm:
            sess.sync(repeats=10, stagger_us=0)
        self.assertIn("separated in time", str(cm.exception))

    def test_separation_is_subtracted_from_the_result(self):
        for stagger in (20000, 50000, 100000):
            plan = timing_plan()
            sess, _bus = make_session(
                plan,
                aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
                stations={"sta1": timing_station("sta1", {
                    AP1: lambda i: 1000000 + i * 15000,
                    AP2: (lambda st: (lambda i: 1000000 + i * 15000 + st))(stagger)})})
            s = sess.sync(repeats=10, stagger_us=stagger)["per_ap"]["apB"]
            self.assertAlmostEqual(s["bias_us"], 0.0, places=6,
                                   msg="separation of %d leaked into the answer" % stagger)


def drifting_station(name, base_fn, per_batch=None):
    """A receiver whose reported arrival times may depend on which round it is, so that a
    relation which holds once and not again is distinguishable from one that holds."""
    def respond(msg):
        extra = (per_batch or (lambda b: 0))(msg["batch"])
        return timing_station(name, {
            AP1: lambda i: 1000000 + i * 15000,
            AP2: lambda i: 1000000 + i * 15000 + base_fn(i) + extra})(msg)
    return respond


class TestDoctor(unittest.TestCase):
    """One verdict covering every way the clocks can diverge."""

    def _stage(self, report, name):
        for s in report["stages"]:
            if s["stage"] == name:
                return s
        self.fail("no %s check was run; stages were %s"
                  % (name, [s["stage"] for s in report["stages"]]))

    def _healthy(self, base_fn=None, per_batch=None, aps=None):
        plan = timing_plan()
        return make_session(
            plan,
            aps=aps or {"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": drifting_station("sta1", base_fn or (lambda i: 50000),
                                               per_batch)})

    def test_a_healthy_rig_passes_every_check(self):
        sess, _bus = self._healthy()
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertEqual(r["verdict"], "PASS", r["stages"])
        self.assertEqual(r["failed"], [])
        self.assertEqual(r["skipped"], [])

    def test_a_refused_gate_is_named_as_an_anchor_problem(self):
        """The gate rejecting an instant is the only direct evidence about the relation between
        the controller and the transmitters."""
        sess, _bus = self._healthy(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2, fired="none")})
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertEqual(self._stage(r, "fire")["verdict"], "FAIL")
        self.assertIn("too far ahead", str(self._stage(r, "fire")["numbers"]))
        self.assertEqual(r["verdict"], "FAIL")

    def test_progressive_divergence_is_separated_from_spread(self):
        sess, _bus = self._healthy(base_fn=lambda i: 50000 + 2 * i)
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertEqual(self._stage(r, "divergence")["verdict"], "FAIL")
        self.assertAlmostEqual(self._stage(r, "divergence")["numbers"]["worst_us_per_shot"],
                               2.0, places=3)

    def test_divergence_is_per_shot_even_when_only_some_shots_pair(self):
        """A rate is per shot, so shots that were never heard still have to count as elapsed.

        Only shots both transmitters were heard on can be compared. Numbering those 0, 1, 2 ...
        would measure the separation per *paired* shot while reporting it as per shot, so a link
        heard half the time would read at twice its real rate -- and the thinner the pairing, the
        further off it reads, which is exactly when the number is worth having.
        """
        plan = timing_plan()
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": timing_station(
                "sta1", {AP1: lambda i: 1000000 + i * 15000,
                         # heard on every other shot, drifting 2 us for each shot that passes
                         AP2: lambda i: (1000000 + i * 15000 + 50000 + 2 * i
                                         if i % 2 == 0 else None)})})
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertAlmostEqual(self._stage(r, "divergence")["numbers"]["worst_us_per_shot"],
                               2.0, places=3)

    def test_a_relation_that_does_not_hold_twice_fails_stability(self):
        sess, _bus = self._healthy(per_batch=lambda b: 0 if b <= 1 else 30)
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertEqual(self._stage(r, "coincidence")["verdict"], "PASS",
                         "a single round on its own looks fine, which is the point")
        self.assertEqual(self._stage(r, "stability")["verdict"], "FAIL")
        self.assertAlmostEqual(self._stage(r, "stability")["numbers"]["bias_shift_us"],
                               30.0, places=3)

    def test_a_constant_displacement_fails_coincidence(self):
        sess, _bus = self._healthy(base_fn=lambda i: 50000 + 40)
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertEqual(self._stage(r, "coincidence")["verdict"], "FAIL")

    def test_a_receiver_reporting_no_arrival_times_is_not_reported_as_healthy(self):
        """Whether the transmitters coincide is then simply unknown, which must not read the
        same as knowing they do. Established by asking the receiver, not by a claim about it."""
        plan = timing_plan()
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": timing_station("sta1", {AP1: lambda i: None,
                                                      AP2: lambda i: None})})
        r = sess.doctor(repeats=4, settle_s=0)
        self.assertEqual(self._stage(r, "coincidence")["verdict"], "FAIL")
        self.assertIn("no receive timestamps", self._stage(r, "coincidence")["detail"])
        self.assertNotEqual(r["verdict"], "PASS")

    def test_an_unreachable_clock_stops_the_check(self):
        plan = timing_plan()
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": drifting_station("sta1", lambda i: 50000)})
        sess.clockd = clockd_mod.ClockD(plan["reference_mac"])
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertEqual(self._stage(r, "clock_graph")["verdict"], "FAIL")

    def test_a_momentary_gap_in_observations_is_not_a_loose_relation(self):
        """The projected error moves with how recently observations arrived, so one reading over
        the line is as likely to be a brief gap as a relation that is genuinely too loose."""
        plan = timing_plan()
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": drifting_station("sta1", lambda i: 50000)})
        real = sess.clockd
        readings = [{"worst_slope_stderr": 1e-5, "anchor_age_s": 0.0, "edges": [],
                     "reachable": [real.reference, AP2], "error": None,
                     "sample_span_us": {}, "ref_tsf": 1, "reference": real.reference}]

        class Recovering(object):
            reference = real.reference
            budget_us = real.budget_us

            def status(self):
                return readings.pop(0) if readings else dict(
                    readings_after, reachable=[real.reference, AP2])

            def __getattr__(self, name):
                return getattr(real, name)

        readings_after = {"worst_slope_stderr": 1e-9, "anchor_age_s": 0.0, "edges": [],
                          "error": None, "sample_span_us": {}, "ref_tsf": 1,
                          "reference": real.reference}
        sess.clockd = Recovering()
        r = sess.doctor(repeats=10, settle_s=0, clock_wait_s=2.0)
        self.assertEqual(self._stage(r, "extrapolation")["verdict"], "PASS")

    def test_every_divergence_axis_is_covered(self):
        """Each check exists because a different relation can fail without the others noticing."""
        sess, _bus = self._healthy()
        got = set(s["stage"] for s in sess.doctor(repeats=10, settle_s=0)["stages"])
        self.assertEqual(got, set(["agents", "clock_graph", "extrapolation",
                                   "path_consistency", "fire", "coincidence",
                                   "divergence", "stability"]))

    def test_an_uncorroborated_route_is_not_a_pass(self):
        """One observation of a pair is a relation nothing checks. It may well be right, which
        is exactly why it must not read the same as one that has been confirmed."""
        plan = timing_plan()
        cd = clockd_mod.ClockD(plan["reference_mac"])
        air = cg.ppdu_airtime_us(0, 124)
        for i in range(30):
            t = 1000000000 + i * 100000
            cd.observe_peer_beacon(AP1, AP2, t1=int(t * 1.000002 + 1e6),
                                   t2=int(t + air), rate_idx=0, length=124)
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": drifting_station("sta1", lambda i: 50000)})
        sess.clockd = cd
        r = sess.doctor(repeats=10, settle_s=0)
        self.assertEqual(self._stage(r, "path_consistency")["verdict"], "SKIP")
        self.assertEqual(r["verdict"], "INCONCLUSIVE")

    def test_a_rig_too_broken_to_fire_is_reported_not_raised(self):
        """The health check is run because the rig is suspect; failing to command a round is a
        finding, not a reason to abandon the report."""
        plan = timing_plan()
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": drifting_station("sta1", lambda i: 50000)})

        class TooUncertain(object):
            reference = sess.clockd.reference

            def status(self):
                return sess.clockd.status()
            def path_disagreement(self):
                return sess.clockd.path_disagreement()
            budget_us = 0.5
            def shared_instant(self, lead_us):
                raise clockd_mod.ClockTooUncertain("the fit is too loose to extrapolate")
            def targets(self, *a, **kw):
                raise clockd_mod.ClockTooUncertain("the fit is too loose to extrapolate")

        real = sess.clockd
        sess.clockd = TooUncertain()
        sess.clockd.status = real.status
        sess.clockd.path_disagreement = real.path_disagreement
        r = sess.doctor(repeats=10, settle_s=0, clock_wait_s=0.1)
        self.assertEqual(self._stage(r, "fire")["verdict"], "FAIL")
        self.assertIn("too loose", self._stage(r, "fire")["detail"])
        self.assertEqual(self._stage(r, "coincidence")["verdict"], "SKIP")
        self.assertEqual(r["verdict"], "FAIL")

    def test_a_late_batch_and_a_misplaced_one_are_told_apart(self):
        """The two sides of the gate window have different causes and different repairs, so the
        report must not merge them into one count of refusals."""
        from cosr.agent import LATE, TOOFAR

        def gate(code):
            def respond(msg):
                if "apA" not in msg["aps"]:
                    return None
                seqs = [wire.SeqAllocator.wire(msg["seq0"] + i) for i in range(msg["n"])]
                shots = dict((s, code if i < len(seqs) // 2 else accounting.FIRED)
                             for i, s in enumerate(seqs))
                return (proto.subj_status("apA"),
                        proto.build_status(msg["run"], msg["batch"], "apA", "e1", shots))
            return respond

        for code, expect in ((LATE, "already passed"), (TOOFAR, "too far ahead")):
            sess, _bus = make_session(
                timing_plan(),
                aps={"apA": gate(code), "apB": fake_ap("apB", AP2)},
                stations={"sta1": drifting_station("sta1", lambda i: 50000)})
            r = sess.doctor(repeats=10, settle_s=0)
            stage = self._stage(r, "fire")
            self.assertEqual(stage["verdict"], "FAIL")
            self.assertIn(expect, stage["detail"])

    def test_isolated_refusals_do_not_condemn_the_rig(self):
        """A refused shot is excluded from the accounting rather than counted as a loss, so it
        costs sample size and nothing else."""
        def one_bad(msg):
            if "apA" not in msg["aps"]:
                return None
            seqs = [wire.SeqAllocator.wire(msg["seq0"] + i) for i in range(msg["n"])]
            shots = dict((s, accounting.FIRED) for s in seqs)
            shots[seqs[0]] = 2
            return (proto.subj_status("apA"),
                    proto.build_status(msg["run"], msg["batch"], "apA", "e1", shots))

        sess, _bus = make_session(
            timing_plan(),
            aps={"apA": one_bad, "apB": fake_ap("apB", AP2)},
            stations={"sta1": drifting_station("sta1", lambda i: 50000)})
        r = sess.doctor(repeats=40, settle_s=0)
        self.assertEqual(self._stage(r, "fire")["verdict"], "PASS")
        self.assertGreater(self._stage(r, "fire")["numbers"]["refused_fraction"], 0)


class TestRunIdentity(unittest.TestCase):

    def test_sessions_created_together_do_not_share_an_identity(self):
        """Batch numbers restart per session, and an agent suppresses a repeat of a (run, batch)
        it has already carried out. A shared identifier would have the second session's opening
        rounds answered from cache with nothing transmitted."""
        plan = make_plan()
        ids = set(S.Session(plan, client=FakeBus()).run_id for _ in range(50))
        self.assertEqual(len(ids), 50)

    def test_an_explicit_identity_is_respected(self):
        self.assertEqual(S.Session(make_plan(), client=FakeBus(), run_id="fixed").run_id,
                         "fixed")


class TestPeerLevels(unittest.TestCase):
    """The level between transmitters comes from the beacons they already report hearing."""

    def _sess(self):
        sess, bus = make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        return sess, bus

    def _feed(self, sess, rows):
        sess._on_beacon(proto.SUBJ_BEACON, {"rows": rows}, None)

    def _row(self, hearer, sender, rssi=None, t=1000000):
        r = {"hearer": hearer, "sender": sender, "t1": t, "t2": t + 1000,
             "rate_idx": 0, "len": 124}
        if rssi is not None:
            r["rssi"] = rssi
        return r

    def test_levels_are_reported_by_node_name(self):
        sess, _bus = self._sess()
        for i in range(4):
            self._feed(sess, [self._row(AP1, AP2, -55 - i, t=1000000 + i * 100000)])
        self.assertEqual(sess.peer_levels(), {("apA", "apB"): -56.5})

    def test_direction_is_preserved(self):
        """What one hears from the other need not equal the reverse, so they are separate."""
        sess, _bus = self._sess()
        self._feed(sess, [self._row(AP1, AP2, -50), self._row(AP2, AP1, -70, t=1100000)])
        got = sess.peer_levels()
        self.assertEqual(got[("apA", "apB")], -50.0)
        self.assertEqual(got[("apB", "apA")], -70.0)

    def test_a_driver_that_reports_no_level_still_relates_the_clocks(self):
        """The level is a byproduct: the clock plane must not depend on it being there."""
        sess, _bus = self._sess()
        self._feed(sess, [self._row(AP1, AP2)])
        self.assertEqual(sess.peer_levels(), {})
        self.assertEqual(sess.beacon_rows, 1)

    def test_an_absent_reading_is_not_averaged_in(self):
        """Zero is not a level any radio can report, so it means no reading was taken."""
        sess, _bus = self._sess()
        self._feed(sess, [self._row(AP1, AP2, -60), self._row(AP1, AP2, 0, t=1100000)])
        self.assertEqual(sess.peer_levels(), {("apA", "apB"): -60.0})

    def test_only_recent_beacons_count(self):
        sess, _bus = self._sess()
        sess.levels = S._PeerLevels(keep=2)
        for i, v in enumerate((-90, -50, -50)):
            self._feed(sess, [self._row(AP1, AP2, v, t=1000000 + i * 100000)])
        self.assertEqual(sess.peer_levels(), {("apA", "apB"): -50.0})

    def test_addresses_outside_the_deployment_are_ignored(self):
        sess, _bus = self._sess()
        self._feed(sess, [self._row("02:00:00:00:00:99", AP1, -40)])
        self.assertEqual(sess.peer_levels(), {})


class TestMeasure(unittest.TestCase):
    """A round and the model inputs derived from it, in one exchange."""

    def _sess(self):
        return make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})

    def test_link_levels_accompany_delivery(self):
        sess, _bus = self._sess()
        r = sess.run()
        self.assertEqual(r["rssi_ap_to_sta_dbm"]["apA->sta1"], -55.0)
        self.assertEqual(r["links"]["apA->sta1"]["delivery"], 1.0)

    def test_transmitter_levels_need_no_extra_round(self):
        """Obtaining these any other way means a monitor interface on a transmitter, which
        rebases the very clock the coordination depends on."""
        sess, bus = self._sess()
        sess._on_beacon(proto.SUBJ_BEACON,
                        {"rows": [{"hearer": AP1, "sender": AP2, "t1": 1000, "t2": 2000,
                                   "rate_idx": 0, "len": 124, "rssi": -62}]}, None)
        before = len(bus.published)
        r = sess.run()
        self.assertEqual(r["rssi_ap_to_ap_dbm"]["apA->apB"], -62.0)
        fires = [p for p in bus.published[before:] if p[0] == proto.SUBJ_FIRE]
        self.assertEqual(len(fires), 1, "the levels must not cost a round of their own")

    def test_an_unheard_pair_is_absent_rather_than_zero(self):
        sess, _bus = self._sess()
        self.assertEqual(sess.run()["rssi_ap_to_ap_dbm"], {})


class TestBuildAgreement(unittest.TestCase):
    """Agents must match this controller, not merely each other."""

    def test_a_uniformly_stale_deployment_is_refused(self):
        """Comparing agents only to one another accepts the common case: everything on the
        nodes is equally out of date because the last edit was never pushed."""
        sess, bus = make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        bus.request_json = lambda subject, obj, timeout=None: {
            "ok": True, "status": {"source_hash": "0000000000000000"}}
        try:
            sess.wait_for_agents(timeout=1)
            self.fail("a stale deployment must not be accepted")
        except S.NodeMissing as e:
            self.assertIn("different build", str(e))
            self.assertIn(agent_mod.source_hash(), str(e))

    def test_a_matching_deployment_is_accepted(self):
        sess, _bus = make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        got = sess.wait_for_agents(timeout=1)
        self.assertEqual(set(got.values()), set([agent_mod.source_hash()]))


class TestNothingToCheck(unittest.TestCase):
    """A check with nothing to examine must not read as a check that passed."""

    def _stage(self, report, name):
        for s in report["stages"]:
            if s["stage"] == name:
                return s
        self.fail("no %s check was run" % name)

    def _lone(self):
        """One transmitter, hearing no peer: nothing to relate and nothing to compare."""
        plan = T.resolve({"channel": 1, "observer": "sta1", "nodes": dict(NODES)},
                         {"nframes": 1, "frame_len": 200, "repeats": 4,
                          "links": [{"ap": "apA", "station": "sta1", "mcs": 0,
                                     "txpower_dbm": 20}]})
        cd = clockd_mod.ClockD(plan["reference_mac"], participants=plan["all_ap_macs"])
        air = cg.ppdu_airtime_us(0, 124)
        for i in range(5):
            cd.observe_peer_beacon(AP1, "9a:1a:35:e9:70:9c", t1=999 + i,
                                   t2=int(5000000 + i * 100000 + air), rate_idx=0, length=124)
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1)},
            stations={"sta1": fake_station("sta1", {AP1: 1})})
        sess.clockd = cd
        return sess

    def test_no_relation_to_extrapolate_is_not_a_measured_zero(self):
        r = self._lone().doctor(repeats=4, settle_s=0)
        self.assertEqual(self._stage(r, "extrapolation")["verdict"], "NA")

    def test_no_routes_to_compare_is_not_agreement(self):
        r = self._lone().doctor(repeats=4, settle_s=0)
        stage = self._stage(r, "path_consistency")
        self.assertEqual(stage["verdict"], "NA")
        self.assertNotIn("agree to within", stage["detail"])

    def test_the_gate_is_still_checked(self):
        r = self._lone().doctor(repeats=4, settle_s=0)
        self.assertEqual(self._stage(r, "fire")["verdict"], "PASS")


class TestClockMaturity(unittest.TestCase):
    """A fresh session starts with no observations, so the fit is worst exactly when it first
    becomes usable."""

    def _sess(self, cd):
        sess, _bus = make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})
        sess.clockd = cd
        return sess

    class Maturing(object):
        """Usable immediately, but only comfortably inside the budget after a few looks."""

        budget_us = 1.0
        reference = AP1

        def __init__(self, good_after):
            self.looks = 0
            self.good_after = good_after

        def shared_instant(self, lead_us):
            return 1000000 + lead_us

        def targets(self, macs, shared, stagger_us=0):
            return dict((m, shared) for m in macs)

        def projected_error_us(self, lead_us):
            self.looks += 1
            return 0.2 if self.looks >= self.good_after else 0.95

        def status(self):
            return {"edges": [], "reachable": [], "error": None}

    def test_it_waits_for_the_fit_to_come_inside_the_budget(self):
        cd = self.Maturing(good_after=3)
        self._sess(cd).wait_for_clock(timeout=10)
        self.assertGreaterEqual(cd.looks, 3, "it must not proceed on the first usable look")

    def test_a_relation_already_comfortable_is_not_waited_on(self):
        cd = self.Maturing(good_after=1)
        t0 = time.time()
        self._sess(cd).wait_for_clock(timeout=10)
        self.assertLess(time.time() - t0, 0.5)

    def test_usable_but_never_comfortable_still_proceeds(self):
        """Refusing here would be stricter than the check that actually gates firing."""
        cd = self.Maturing(good_after=10 ** 6)
        self._sess(cd).wait_for_clock(timeout=1.2)


class TestNoiseAttribution(unittest.TestCase):
    """A scattered result blames the transmitters or the receiver, and they need opposite fixes."""

    def _stage(self, report, name):
        for s in report["stages"]:
            if s["stage"] == name:
                return s
        self.fail("no %s check was run" % name)

    def _run(self, jitter_a, jitter_b):
        """`jitter_x` shifts each transmitter's arrivals about its own commanded instant."""
        plan = timing_plan()
        base = 1000000

        def arrivals(shift):
            return lambda i: base + i * 15000 + shift(i)

        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": timing_station("sta1", {
                AP1: arrivals(jitter_a),
                AP2: lambda i: base + i * 15000 + 50000 + jitter_b(i)})})
        return sess.doctor(repeats=10, settle_s=0)

    def test_one_erratic_transmitter_is_not_blamed_on_the_receiver(self):
        r = self._run(lambda i: 0, lambda i: 20 if i % 2 else -20)
        stage = self._stage(r, "coincidence")
        self.assertEqual(stage["verdict"], "FAIL")
        self.assertNotIn("this receiver's stamping", stage["detail"])

    def test_a_receiver_stamping_badly_is_named_as_such(self):
        """Stamping noise is independent per frame, so it does not cancel in the pairwise
        comparison and looks exactly like the transmitters disagreeing -- unless each is also
        checked against its own commanded instant, where it shows up for all of them at once."""
        stage = self._stage(self._run(lambda i: 10 if i % 2 else -10,
                                      lambda i: 10 if (i // 2) % 2 else -10), "coincidence")
        self.assertEqual(stage["verdict"], "FAIL")
        self.assertIn("this receiver's stamping", stage["detail"])

    def test_a_clean_run_says_nothing_about_either(self):
        stage = self._stage(self._run(lambda i: 0, lambda i: 0), "coincidence")
        self.assertEqual(stage["verdict"], "PASS")
        self.assertIn("against own instant", stage["detail"],
                      "reported every run: on its own it is clock wander that cancels, but "
                      "together with a large spread it means the receiver cannot time")


class TestReplyProvenance(unittest.TestCase):
    """A reply is only evidence about the round it belongs to."""

    def _sess(self):
        return make_session(
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": fake_station("sta1", {AP1: 1}),
                      "sta2": fake_station("sta2", {AP2: 1})})

    def test_a_reply_from_another_run_is_ignored(self):
        """Batch numbers restart at one per session on a shared subject, so a second controller
        collides immediately and would otherwise contribute counts to a round it never
        commanded."""
        sess, _bus = self._sess()
        sess._on_report(proto.subj_report("sta1"),
                        proto.build_report("someone-else", 1, "sta1",
                                           {"epoch": "x", "ordinal": 999, "other_frames": 1, "other_frames_round": 1,
                                            "drops": 0, "freq_mhz": 2412, "per_ap": {}}), None)
        self.assertEqual(sess._reports, {})
        self.assertEqual(sess._since, {})

    def test_a_restarted_receiver_resets_the_barrier(self):
        """Its counter restarts at zero while the barrier would not, so every later frame would
        be filtered out as already seen and delivery would read zero."""
        sess, _bus = self._sess()

        def report(epoch, ordinal):
            return proto.build_report(sess.run_id, 1, "sta1",
                                      {"epoch": epoch, "ordinal": ordinal, "other_frames": 1, "other_frames_round": 1,
                                       "drops": 0, "freq_mhz": 2412, "per_ap": {}})

        sess._on_report(proto.subj_report("sta1"), report("e1", 500), None)
        self.assertEqual(sess._since["sta1"], 500)
        sess._on_report(proto.subj_report("sta1"), report("e2", 3), None)
        self.assertEqual(sess._since["sta1"], 0,
                         "a counter from a new instance is not comparable to the old one")

    def test_the_barrier_still_advances_within_one_instance(self):
        sess, _bus = self._sess()
        for ordinal in (10, 25):
            sess._on_report(proto.subj_report("sta1"),
                            proto.build_report(sess.run_id, 1, "sta1",
                                               {"epoch": "e1", "ordinal": ordinal,
                                                "other_frames": 1, "other_frames_round": 1, "drops": 0,
                                                "freq_mhz": 2412, "per_ap": {}}), None)
        self.assertEqual(sess._since["sta1"], 25)

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDeclaredClockSource(unittest.TestCase):
    """`sync` states which observations a deployment intends to rely on."""

    def _stage(self, report, name):
        for s in report["stages"]:
            if s["stage"] == name:
                return s
        self.fail("no %s check was run" % name)

    def _doctor(self, declared):
        plan = timing_plan()
        plan["sync"] = declared
        sess, _bus = make_session(
            plan,
            aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
            stations={"sta1": drifting_station("sta1", lambda i: 50000)})
        return sess.doctor(repeats=10, settle_s=0)

    def test_a_declared_source_that_is_not_arriving_is_a_failure(self):
        """The clocks may well be related by the other source; that is not what was asked for,
        and silently accepting it means the deployment is not the one described."""
        stage = self._stage(self._doctor("monitor"), "clock_graph")
        self.assertEqual(stage["verdict"], "FAIL")
        self.assertIn("declares", stage["detail"])

    def test_the_source_actually_in_use_is_named(self):
        stage = self._stage(self._doctor("beacon"), "clock_graph")
        self.assertEqual(stage["verdict"], "PASS")
        self.assertIn("via beacon", stage["detail"])


class TestLevelsExpire(unittest.TestCase):
    """A level is evidence that a pair is being heard, not a permanent property of the pair."""

    class Clock(object):
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            return self.t

    def test_a_pair_no_longer_heard_stops_being_reported(self):
        clock = self.Clock()
        levels = S._PeerLevels(clock=clock)
        levels.add("aa", "bb", -60)
        self.assertEqual(levels.levels(), {("aa", "bb"): -60.0})
        clock.t += S.MAX_LEVEL_AGE_S + 1
        self.assertEqual(levels.levels(), {},
                         "a transmitter that has gone away must not keep reporting a level")

    def test_a_pair_still_being_heard_is_kept(self):
        clock = self.Clock()
        levels = S._PeerLevels(clock=clock)
        levels.add("aa", "bb", -60)
        clock.t += S.MAX_LEVEL_AGE_S + 1
        levels.add("aa", "bb", -70)
        self.assertEqual(levels.levels(), {("aa", "bb"): -70.0},
                         "only the readings still inside the window count")


class TestDoctorStagger(unittest.TestCase):
    """The separation between transmitters must stay inside one shot's slot."""

    def test_a_stagger_at_or_above_the_spacing_is_reduced(self):
        """At exactly the spacing, one transmitter's shot lands on the next shot of the other:
        they collide, nothing decodes, and the check would report that nothing paired while the
        clocks were fine."""
        seen = []
        plan = make_plan()
        plan["spacing_us"] = 50000
        sess, _bus = make_session(plan=plan,
                                  aps={"apA": fake_ap("apA", AP1), "apB": fake_ap("apB", AP2)},
                                  stations={"sta1": fake_station("sta1", {AP1: 1}),
                                            "sta2": fake_station("sta2", {AP2: 1})})
        real = sess._timing_round

        def spy(repeats, stagger_us, spacing_us, nframes=1):
            seen.append(stagger_us)
            return real(repeats, stagger_us, spacing_us, nframes)

        sess._timing_round = spy
        sess.doctor(repeats=2, stagger_us=50000, settle_s=0.0)
        self.assertTrue(seen, "the timing round was never attempted")
        for st in seen:
            self.assertLess(st, 50000)
            self.assertGreaterEqual(st, 2000)

    def test_the_last_transmitter_is_kept_inside_the_slot(self):
        """Transmitter i fires i staggers late, so it is the last one that has to fit.

        A step small enough to look harmless on its own still walks the far transmitters into the
        following shot once there are enough of them: they collide there, pair on almost nothing,
        and the check reports them as unheard rather than as spaced too widely to begin with.
        """
        seen = []
        names = ["ap%d" % i for i in range(1, 7)]
        macs = dict((n, "02:00:00:00:00:%02x" % i) for i, n in enumerate(names, start=1))
        nodes = dict((n, {"role": "ap", "ip": "10.0.0.%d" % (30 + i), "iface": "wlan0",
                          "mac": macs[n]}) for i, n in enumerate(names))
        nodes["sta1"] = {"role": "station", "ip": "10.0.0.21", "iface": "wlan0",
                         "station_id": 1}
        shot = {"nframes": 1, "frame_len": 200, "repeats": 4, "spacing_us": 50000,
                "lead_us": 150000,
                "links": [{"ap": n, "station": "sta1", "mcs": 4, "txpower_dbm": 20}
                          for n in names]}
        plan = T.resolve({"channel": 1, "observer": "sta1", "nodes": nodes}, shot)

        # `ready_clock` relates only the two APs of the default fixture, so a plan this size
        # needs every transmitter tied back to the reference or the check stops before it gets
        # anywhere near the stagger.
        ref = plan["reference_mac"]
        cd = clockd_mod.ClockD(ref)
        air = cg.ppdu_airtime_us(0, 124)
        for other in [m for m in macs.values() if m != ref]:
            for i in range(30):
                t = 1000000000 + i * 100000
                cd.observe_peer_beacon(ref, other, t1=int(t * 1.000002 + 1e6),
                                       t2=int(t + air), rate_idx=0, length=124)
                cd.observe_peer_beacon(other, ref, t1=int(t),
                                       t2=int(t * 1.000002 + 1e6 + air), rate_idx=0, length=124)

        bus = FakeBus()
        sess = S.Session(plan, client=bus, clockd=cd)
        sess.connect()
        bus.responders = dict((n, fake_ap(n, macs[n])) for n in names)
        bus.responders["sta1"] = fake_station("sta1", dict((macs[n], 1) for n in names))
        real = sess._timing_round

        def spy(repeats, stagger_us, spacing_us, nframes=1):
            seen.append(stagger_us)
            return real(repeats, stagger_us, spacing_us, nframes)

        sess._timing_round = spy
        sess.doctor(repeats=2, stagger_us=12500, settle_s=0.0)
        self.assertTrue(seen, "the timing round was never attempted")
        for st in seen:
            for k in range(1, len(names)):
                phase = (k * st) % 50000
                self.assertGreaterEqual(
                    min(phase, 50000 - phase), 2000,
                    "transmitters %d apart land on each other at a stagger of %d" % (k, st))

