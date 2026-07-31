"""Drives measurement rounds against a running testbed.

A round is one published instruction and one reply per participant. Everything else -- relating
the clocks, choosing the instant, deciding which shots count -- happens here, so the nodes stay
simple and stateless between rounds.

The session is built once and reused. Rebuilding it per round would discard the clock graph,
which needs a span of observations before it can place an instant at all.
"""
import itertools
import os
import threading
import time

from . import accounting
from . import agent as agent_mod
from . import clockd as clockd_mod
from . import natsc
from . import proto
from . import radiotap
from . import wire

COLLECT_MARGIN_S = 2.0      # allowance on top of a round's own duration before giving up
COLLIDE_GUARD_US = 2000

_RUN_SEQ = itertools.count(1)

# How long a level between transmitters stays reportable without being heard again. Beacons
# arrive many times a second, so anything older than this means the pair is no longer being
# heard rather than that it is quiet.
MAX_LEVEL_AGE_S = 30.0


class NodeMissing(RuntimeError):
    """A participant did not answer within the round's deadline."""


class _PeerLevels(object):
    """Received level between transmitters, averaged over recent beacons.

    Every transmitter already reports the beacons it hears from the others, so the level between
    any two of them is a byproduct of relating their clocks and needs no separate measurement.
    Obtaining it any other way means putting a transmitter into monitor mode, which rebases the
    clock the whole design depends on.

    Levels are directional: what A hears from B need not equal what B hears from A.
    """

    def __init__(self, keep=32, max_age_s=MAX_LEVEL_AGE_S, clock=None):
        self.keep = keep
        self.max_age_s = max_age_s
        self._clock = clock or time.monotonic
        self._seen = {}

    def add(self, hearer, sender, rssi):
        if radiotap.usable_signal(rssi) is None:
            return
        q = self._seen.setdefault((hearer.lower(), sender.lower()), [])
        q.append((self._clock(), int(rssi)))
        if len(q) > self.keep:
            del q[:len(q) - self.keep]

    def levels(self):
        """Only pairs heard recently. A transmitter that has been switched off, moved or has
        stopped beaconing would otherwise keep reporting the last level it was heard at, for as
        long as the session lasts."""
        cutoff = self._clock() - self.max_age_s
        out = {}
        for key, q in self._seen.items():
            fresh = [v for t, v in q if t >= cutoff]
            if fresh:
                out[key] = round(sum(fresh) / float(len(fresh)), 1)
        return out


class Session(object):

    def __init__(self, plan, hub="127.0.0.1", token=None, run_id=None,
                 client=None, clockd=None):
        self.plan = plan
        self.hub = hub
        self.token = token
        # Batch numbers restart at one per session, and an agent suppresses a repeat of a
        # (run, batch) it has already carried out. Two sessions sharing a run identifier would
        # therefore have the second one's opening rounds answered from cache without anything
        # being transmitted, so the identifier must be unique per session, not per second.
        self.run_id = run_id or ("run-%d-%d-%d" % (int(time.time()), os.getpid(),
                                                   next(_RUN_SEQ)))
        self.client = client
        self.clockd = clockd or clockd_mod.ClockD(plan["reference_mac"],
                                                  participants=plan.get("all_ap_macs") or plan["ap_macs"])
        self.seq = wire.SeqAllocator()
        self.batch = 0

        self._lock = threading.Lock()
        self._status = {}          # batch -> {ap name: message}
        self._reports = {}         # batch -> {station name: message}
        self._since = {}           # station name -> ordinal from its previous report
        self._epochs = {}          # station name -> which instance of it that ordinal came from
        self.agents = {}           # name -> hello message
        self.beacon_rows = 0
        self.levels = _PeerLevels()

    # ------------------------------------------------------------------ lifecycle

    def connect(self):
        if self.client is None:
            self.client = natsc.connect(self.hub, natsc.DEFAULT_PORT, token=self.token,
                                        name="cosr-controller", retries=3)
        self.client.subscribe_json(proto.SUBJ_HELLO, self._on_hello)
        self.client.subscribe_json(proto.SUBJ_BEACON, self._on_beacon)
        self.client.subscribe_json("cosr.status.*", self._on_status)
        self.client.subscribe_json("cosr.report.*", self._on_report)
        self.client.ping(timeout=3.0)
        return self

    def use_plan(self, plan):
        """Point the session at a different set of links.

        Which transmitters and receivers take part changes from round to round, while the
        relation between the clocks is a property of the deployment. Building a session per
        round would discard that relation, and it needs a span of observations before it can
        place an instant at all.
        """
        self.plan = plan
        return self

    def close(self):
        if self.client is not None:
            self.client.close()

    def _on_hello(self, _subject, msg, _reply):
        with self._lock:
            self.agents[msg.get("name")] = msg

    def _on_beacon(self, _subject, msg, _reply):
        for row in msg.get("rows") or []:
            try:
                if "station" in row:
                    # Observed by a receiver: its own clock appears on both sides of every edge
                    # it contributes and cancels, so no duration correction applies.
                    self.clockd.observe_station_beacon(
                        row["station"], row["sender"], row["station_time"], row["t1"])
                else:
                    self.clockd.observe_peer_beacon(
                        row["hearer"], row["sender"], row["t1"], row["t2"],
                        row["rate_idx"], row["len"])
            except (KeyError, TypeError):
                continue
            with self._lock:
                self.beacon_rows += 1
                # Absent when the radio does not report a level with the observation; the
                # clock relation does not depend on it, so it is recorded when present and
                # simply not offered when not.
                if row.get("rssi") is not None:
                    self.levels.add(row["hearer"], row["sender"], row["rssi"])

    def _mine(self, msg):
        """Replies are broadcast, and batch numbers restart at one in every session.

        Without checking whose round a reply belongs to, a second controller -- or this one
        restarted -- contributes shot outcomes and counts to a round it never commanded.
        """
        return msg.get("run") == self.run_id

    def _on_status(self, _subject, msg, _reply):
        if not self._mine(msg):
            return
        try:
            proto.validate_status(msg)
        except proto.ProtocolError:
            return
        with self._lock:
            self._status.setdefault(msg.get("batch"), {})[msg.get("ap") or msg.get("node")] = msg

    def _on_report(self, _subject, msg, _reply):
        if not self._mine(msg):
            return
        try:
            proto.validate_report(msg)
        except proto.ProtocolError:
            return
        name = msg.get("station") or msg.get("node")
        with self._lock:
            self._reports.setdefault(msg.get("batch"), {})[name] = msg
            # The barrier is a counter held by that agent, so it is only comparable to later
            # reports from the same instance of it. A restart resets the counter while this
            # value would not, and every later frame would be filtered out as already seen.
            known = self._epochs.get(name)
            if known is not None and known != msg.get("epoch"):
                self._since[name] = 0
            elif msg.get("ordinal") is not None:
                self._since[name] = msg["ordinal"]
            self._epochs[name] = msg.get("epoch")

    # ------------------------------------------------------------------ readiness

    def participants(self):
        """(role, name) for every node this plan needs."""
        out = [("ap", a["name"]) for a in self.plan["aps"]]
        seen = set()
        for a in self.plan["aps"]:
            n = a["station"]["name"]
            if n not in seen:
                seen.add(n)
                out.append(("station", n))
        return out

    def wait_for_agents(self, timeout=20.0, per_request=2.0):
        """Every participant must answer and be running the same build.

        Each agent is asked directly rather than waited for: an announcement is published once,
        when an agent starts, so anything that depends on having heard it would fail purely
        because the controller was started second.

        A build mismatch is refused rather than tolerated, since two versions of the accounting
        produce numbers that look comparable and are not.
        """
        deadline = time.time() + timeout
        hashes = {}
        missing = []
        for role, name in self.participants():
            got = None
            while time.time() < deadline and got is None:
                try:
                    resp = self.client.request_json(
                        proto.subj_rpc(role, name), {"op": "status"}, timeout=per_request)
                    if resp.get("ok"):
                        got = resp["status"]
                except natsc.NatsError:
                    time.sleep(0.2)
            if got is None:
                missing.append(name)
            else:
                hashes[name] = got.get("source_hash")
                with self._lock:
                    self.agents[name] = got
        if missing:
            raise NodeMissing(
                "no agent answered for: %s. Check that the agent is running there and that it "
                "can reach the message broker." % ", ".join(sorted(missing)))
        distinct = set(hashes.values())
        if len(distinct) > 1:
            raise NodeMissing(
                "agents are running different builds, so their results are not comparable: %s"
                % hashes)
        # Comparing the agents only to each other would accept a deployment that is uniformly
        # out of date.
        want = agent_mod.source_hash()
        stale = sorted(n for n, h in hashes.items() if h and h != want)
        if stale:
            raise NodeMissing(
                "%s are running a different build of the agent than this controller (%s, "
                "expected %s). Deploy again before measuring."
                % (", ".join(stale), sorted(distinct)[0], want))
        return hashes

    def wait_for_clock(self, timeout=30.0, margin=0.5):
        """Block until the clocks can be related well enough to command a round.

        Observations accumulate from nothing when a session starts, so the fit is at its worst
        just as it first becomes usable. Proceeding at that moment leaves the projected error
        sitting on the budget, where it drifts back and forth across the threshold and makes an
        otherwise healthy deployment fail intermittently. Waiting for it to come comfortably
        inside costs a few seconds once per session and nothing thereafter.
        """
        deadline = time.time() + timeout
        last = None
        want = self.clockd.budget_us * margin
        while time.time() < deadline:
            try:
                self.clockd.targets(self.plan["ap_macs"],
                                    self.clockd.shared_instant(self.plan["lead_us"]))
                if self.clockd.projected_error_us(self.plan["lead_us"]) <= want:
                    return self.clockd.status()
                last = "the relation is usable but still close to the budget"
            except (clockd_mod.ClockNotReady, clockd_mod.ClockTooUncertain) as e:
                last = e
            time.sleep(0.5)
        # Usable but not yet comfortable is still usable: refusing here would be stricter than
        # the check that actually gates firing.
        try:
            self.clockd.targets(self.plan["ap_macs"],
                                self.clockd.shared_instant(self.plan["lead_us"]))
            return self.clockd.status()
        except (clockd_mod.ClockNotReady, clockd_mod.ClockTooUncertain) as e:
            raise NodeMissing("the clocks could not be related within %.0fs: %s"
                              % (timeout, e if last is None else last))

    # ------------------------------------------------------------------ rounds

    def run(self, repeats=None, spacing_us=None, stagger_us=None, mcs_by_ap=None,
            power_by_ap=None):
        """Fire one round and return what happened on every link.

        The levels between transmitters come from beacons they already report hearing, so they
        are always included: they cost no extra round and are current whether or not a given
        round happens to need them.
        """
        plan = self.plan
        n = int(repeats if repeats is not None else plan["repeats"])
        spacing = int(spacing_us if spacing_us is not None else plan["spacing_us"])
        stagger = int(stagger_us if stagger_us is not None else plan["stagger_us"])

        shared = self.clockd.shared_instant(plan["lead_us"])
        targets = self.clockd.targets(plan["ap_macs"], shared, stagger_us=stagger)

        seq0 = self.seq.alloc(n)
        self.batch += 1
        batch = self.batch

        aps = {}
        for ap in plan["aps"]:
            mcs = (mcs_by_ap or {}).get(ap["name"], ap["mcs"])
            aps[ap["name"]] = {
                "mac": ap["mac"], "target": targets[ap["mac"]],
                "rate": wire.rate_for_mcs(mcs), "txp": ap["txp"],
                "station_id": ap["station_id"],
                "txpower_mbm": (power_by_ap or {}).get(ap["name"], ap["txpower_mbm"]),
            }

        stations = {}
        for ap in plan["aps"]:
            name = ap["station"]["name"]
            entry = stations.setdefault(name, {
                "ap_macs": plan["ap_macs"], "station_id": ap["station_id"],
                "since_ordinal": self._since.get(name, 0), "expect_per_ap": {}})
            entry["expect_per_ap"][ap["mac"]] = n * plan["nframes"]

        msg = proto.build_fire(
            run=self.run_id, batch=batch, seq0=wire.SeqAllocator.wire(seq0), n=n,
            spacing_us=spacing, stagger_us=stagger, lead_us=plan["lead_us"],
            nframes=plan["nframes"], frame_len=plan["frame_len"],
            stamp_off=wire.STAMP_OFFSET, channel_freq_mhz=plan["channel_freq_mhz"],
            aps=aps, stations=stations)

        t0 = time.time()
        self.client.publish_json(proto.SUBJ_FIRE, msg)
        statuses, reports = self._collect(batch, msg, set(aps), set(stations))
        elapsed = time.time() - t0

        shots_by_ap = {}
        for name, st in statuses.items():
            shots_by_ap[name] = {"shots": proto.decode_shots(st),
                                 "epoch": st.get("epoch"), "error": st.get("error"),
                                 "power_error": (st.get("extra") or {}).get("power_error")}
        links, warnings = accounting.link_results(
            plan, shots_by_ap, reports, wire.SeqAllocator.wire(seq0), n)

        total, per, measured, unmeasured = accounting.total_throughput(
            links, _rate_mbps)
        return {
            "run": self.run_id, "batch": batch, "seq0": wire.SeqAllocator.wire(seq0),
            "repeats": n, "spacing_us": spacing, "stagger_us": stagger,
            "links": links, "per_link": per, "throughput_mbps": total,
            "shots_by_ap": shots_by_ap,
            "measured_links": measured, "unmeasured_links": unmeasured,
            "warnings": warnings, "seconds": round(elapsed, 3),
            "rssi_ap_to_sta_dbm": dict((k, e["rssi_dbm"]) for k, e in links.items()),
            "rssi_ap_to_ap_dbm": dict(("%s->%s" % k, v)
                                      for k, v in self.peer_levels().items()),
        }

    def _collect(self, batch, msg, want_aps, want_stations):
        """Await every participant's reply, or name the ones that never answered."""
        budget = proto.report_deadline_s(msg) + COLLECT_MARGIN_S
        deadline = time.time() + budget
        while time.time() < deadline:
            with self._lock:
                st = dict(self._status.get(batch, {}))
                rp = dict(self._reports.get(batch, {}))
            if want_aps <= set(st) and want_stations <= set(rp):
                return st, rp
            time.sleep(0.02)
        with self._lock:
            st = dict(self._status.get(batch, {}))
            rp = dict(self._reports.get(batch, {}))
        said = [(n, m["error"]) for n, m in list(st.items()) + list(rp.items())
                if m.get("error")]
        if said:
            raise NodeMissing(
                "the round was refused by %s. This was diagnosed there, not a timeout."
                % "; ".join("%s (%s)" % (n, e) for n, e in sorted(said)))
        missing = sorted((want_aps - set(st)) | (want_stations - set(rp)))
        if missing:
            raise NodeMissing(
                "no reply from %s within %.1fs. The round was commanded, so whether it went out "
                "is unknown; results for it are not reported."
                % (", ".join(missing), budget))
        return st, rp



    def _timing_round(self, repeats, stagger_us, spacing_us, nframes=1):
        """Fire a round aimed entirely at the observer and return its receive times per transmitter.

        Every transmitter addresses the observer so that one receiver, and therefore one clock, sees
        all of them: comparing arrival times taken at different receivers would fold their clock
        difference into the answer.

        A single frame per shot, always. Later subframes of an aggregate share the opening one's
        receive stamp, so they carry no independent timing information.
        """
        from . import proto as _proto
        from . import topo as _topo
        plan = self.plan
        obs = plan["observer"]
        n = int(repeats if repeats is not None else plan["repeats"])
        spacing = int(spacing_us if spacing_us is not None else plan["spacing_us"])

        shared = self.clockd.shared_instant(plan["lead_us"])
        targets = self.clockd.targets(plan["ap_macs"], shared, stagger_us=stagger_us)

        seq0 = self.seq.alloc(n)
        self.batch += 1
        batch = self.batch

        aps = {}
        for ap in plan["aps"]:
            aps[ap["name"]] = {
                "mac": ap["mac"], "target": targets[ap["mac"]],
                "rate": wire.rate_for_mcs(ap["mcs"]), "txp": ap["txp"],
                "station_id": obs["station_id"], "txpower_mbm": ap["txpower_mbm"]}
        stations = {obs["name"]: {"ap_macs": plan["ap_macs"],
                                  "station_id": obs["station_id"],
                                  "since_ordinal": self._since.get(obs["name"], 0),
                                  "expect_per_ap": dict((m, n) for m in plan["ap_macs"])}}

        msg = _proto.build_fire(
            run=self.run_id, batch=batch, seq0=wire.SeqAllocator.wire(seq0), n=n,
            spacing_us=spacing, stagger_us=stagger_us, lead_us=plan["lead_us"],
            nframes=nframes, frame_len=plan["frame_len"], stamp_off=wire.STAMP_OFFSET,
            channel_freq_mhz=plan["channel_freq_mhz"], aps=aps, stations=stations)

        self.client.publish_json(_proto.SUBJ_FIRE, msg)
        statuses, reports = self._collect(batch, msg, set(aps), set(stations))
        rep = reports[obs["name"]]
        times = {}
        for ap in plan["aps"]:
            raw = (rep["per_ap"].get(ap["mac"]) or {}).get("tsft_by_seq") or {}
            times[ap["name"]] = dict((int(k), int(v)) for k, v in raw.items())
        fired = {}
        for name, st in statuses.items():
            fired[name] = _proto.decode_shots(st)
        return times, fired, targets, wire.SeqAllocator.wire(seq0), n, spacing

    def sync(self, repeats=None, stagger_us=50000, spacing_us=None):
        """How closely the transmitters coincide, measured at the observer."""
        from . import timing
        from . import topo as _topo
        plan = self.plan
        if len(plan["aps"]) < 2:
            raise RuntimeError("comparing transmission instants needs at least two transmitters")
        if stagger_us <= 0:
            raise RuntimeError(
                "a coincidence measurement needs the transmitters separated in time. With no "
                "separation their transmissions overlap at the receiver and neither can be "
                "decoded, so almost nothing pairs and the result is meaningless. Separate them "
                "by more than one frame's airtime; the separation is subtracted from the "
                "measurement, so it does not affect the answer.")
        times, fired, _targets, _seq0, n, _sp = self._timing_round(
            repeats, stagger_us, spacing_us)
        if not any(times.values()):
            raise RuntimeError(
                "the observer %r returned no receive timestamps, so there is nothing to pair "
                "transmissions on. Its capture reports no per-frame time; use a receiver whose "
                "radiotap carries one." % plan["observer"]["name"])
        ref_name = plan["aps"][0]["name"]
        ref_times = times.get(ref_name, {})
        per_ap = {}
        for i, ap in enumerate(plan["aps"][1:], start=1):
            errs, common = timing.pairwise_errors(ref_times, times.get(ap["name"], {}),
                                                  stagger_us, i)
            # A displacement that grows across the batch means the two are running at different
            # rates, not sitting at a fixed offset. Reported alongside the spread, which widens
            # for either and cannot tell them apart.
            per_ap[ap["name"]] = timing.stats(errs, len(common), positions=range(len(common)))
        return {"reference": ref_name, "stagger_us": stagger_us, "repeats": n,
                "per_ap": per_ap,
                "fired": dict((k, sum(1 for c in v.values() if c == 0))
                              for k, v in fired.items())}

    # ------------------------------------------------------------------ health

    def doctor(self, repeats=20, stagger_us=50000, spacing_us=None, settle_s=2.0,
               bias_us=4.0, jitter_us=5.0, drift_us_per_shot=0.5, path_us=4.0,
               stability_us=4.0, refused_fraction=0.10, clock_wait_s=15.0):
        """Check every way the clocks can diverge, and report a verdict per check.

        Three clock relations decide whether a round means anything, and they fail in different
        ways, so each is checked against the thing that can actually observe it:

          * between transmitters -- decides whether they coincide. Observed at one receiver, so
            the receiver's own clock cancels.
          * between the controller and the transmitters -- decides only *when* the round happens,
            since one instant is mapped for everyone and an error in it moves them together. It
            cannot pull them apart, but it can push the instant outside the window the gate
            accepts, which the gate itself reports per shot.
          * between routes through the clock graph -- invisible to both of the above, because a
            contradicting edge simply becomes the answer wherever the traversal used it.

        Verdicts are PASS, FAIL, SKIP or NA. A skipped check is never absorbed into a pass: the
        overall verdict is INCONCLUSIVE, because an unrun check and a passed one are only the
        same thing if the failure it looks for cannot happen.

        The thresholds carry margin over the spread a working deployment shows, so that a pass
        means something and a failure is worth acting on. They are arguments because the spread
        is a property of the hardware in use: measure it locally and set them from that rather
        than from a figure quoted elsewhere.

        This establishes the rig at one moment. It cannot see a node that fails midway through a
        long run; that is what the per-shot outcome and named per-link status in every round are
        for.
        """
        from . import timing
        from . import topo as _topo

        out = []

        def record(stage, verdict, detail, **numbers):
            out.append({"stage": stage, "verdict": verdict, "detail": detail,
                        "numbers": numbers})

        try:
            hashes = self.wait_for_agents()
            record("agents", "PASS", "%d nodes, build %s"
                   % (len(hashes), agent_mod.source_hash()), agents=sorted(hashes))
        except NodeMissing as e:
            record("agents", "FAIL", str(e))
            return self._verdict(out)

        st = self.clockd.status()
        missing = [m for m in self.plan["ap_macs"] if m.lower() not in st["reachable"]]
        if st["error"] or missing:
            record("clock_graph", "FAIL",
                   st["error"] or ("no path from the reference clock to: %s. Those transmitters "
                                   "cannot be commanded to a shared instant at all."
                                   % ", ".join(missing)),
                   edges=st["edges"], reachable=st["reachable"])
            return self._verdict(out)
        else:
            declared = self.plan.get("sync")
            sources = st.get("sources") or []
            if declared and declared not in sources:
                record("clock_graph", "FAIL",
                       "the deployment declares %r as its clock source but no such observation "
                       "is arriving (only: %s). The clocks may still be related by the other "
                       "source, which is not what was asked for."
                       % (declared, ", ".join(sources) or "none"),
                       edges=st["edges"], sources=sources)
            else:
                record("clock_graph", "PASS",
                       "%d edges, %d clocks, %.1f s old, via %s"
                       % (len(st["edges"]), len(st["reachable"]),
                          st["anchor_age_s"] or 0.0, "+".join(sources) or "none"),
                       edges=st["edges"], anchor_age_s=st["anchor_age_s"],
                       sources=sources, sample_span_us=st["sample_span_us"])

        others = [m for m in st["reachable"] if m != self.clockd.reference]
        dt = (st["anchor_age_s"] or 0.0) * 1e6 + self.plan["lead_us"]
        budget = self.clockd.budget_us
        if not others:
            # With nothing but the reference taking part there is no relation to extrapolate:
            # the reference maps to itself exactly. Reporting a measured zero here would state
            # that a check passed when there was nothing for it to examine.
            record("extrapolation", "NA",
                   "only the reference clock is participating, so no relation is being "
                   "extrapolated to place the instant.")
        else:
            se = st["worst_slope_stderr"]
            err = se * dt
            if err > budget:
                # The figure moves with how recently observations arrived, so a single reading
                # over the line is as likely to be a brief gap in them as a relation that is
                # genuinely too loose. Only a value that stays over is a finding.
                try:
                    self.wait_for_clock(timeout=clock_wait_s)
                    st = self.clockd.status()
                    se = st["worst_slope_stderr"]
                    dt = (st["anchor_age_s"] or 0.0) * 1e6 + self.plan["lead_us"]
                    err = se * dt
                except NodeMissing:
                    pass
            record("extrapolation", "PASS" if err <= budget else "FAIL",
                   "%.2f / %.2f us" % (err, budget) if err <= budget else
                   "placing an instant %.3f s beyond the observations carries %.3f us of "
                   "expected error, above the %.2f us budget. Either observations are not "
                   "arriving often enough, or the instant is being placed too far ahead."
                   % (dt / 1e6, err, budget),
                   projected_error_us=round(err, 4), budget_us=budget, slope_stderr=se)

        # Two observations of the same pair disagree by twice the error in the constant relating
        # where the radio samples a transmitted timestamp to where it stamps a reception, so the
        # threshold sits above that residue and far below any real contradiction.
        try:
            paths = self.clockd.path_disagreement()
        except clockd_mod.ClockNotReady as e:
            paths = None
            record("path_consistency", "SKIP", str(e))
        if paths is not None:
            corroborated = dict((m, v) for m, v in paths.items() if v["redundant"])
            unverified = sorted(m for m, v in paths.items() if not v["redundant"])
        if paths is not None and not paths:
            # No clock but the reference, so there are no routes that could disagree. This is
            # genuinely nothing to check rather than a check that passed.
            record("path_consistency", "NA",
                   "only the reference clock is participating, so there are no routes to "
                   "compare.")
        elif paths:
            worst = max([v["max_us"] for v in corroborated.values()] + [0.0])
            bad = sorted(m for m, v in corroborated.items() if v["max_us"] > path_us)
            if bad:
                record("path_consistency", "FAIL",
                       "routes through the graph disagree by up to %.2f us for: %s. One "
                       "observation contradicts the rest, so the map in use is decided by "
                       "traversal order rather than by measurement."
                       % (worst, ", ".join(bad)),
                       worst_us=round(worst, 3), unverified=unverified)
            elif unverified:
                # Two transmitters that beacon can always hear each other, so a single route is
                # an observation that has not arrived rather than a graph that cannot have one.
                # Nothing contradicts a lone route, and that is not the same as it being right.
                record("path_consistency", "SKIP",
                       "only one route reaches %s, so nothing corroborates the relation being "
                       "used for it. Each pair should be observed in both directions; wait for "
                       "the reverse observation, or add a node that hears both."
                       % ", ".join(unverified),
                       worst_us=round(worst, 3), unverified=unverified)
            else:
                record("path_consistency", "PASS",
                       "%.2f us" % worst, worst_us=round(worst, 3))

        # Whether this receiver can support a timing measurement is established by asking it,
        # not by a claim in the description: either its captures carry a per-frame receive time
        # or they do not, and that is visible in what it returns.
        timing_ok = len(self.plan["aps"]) >= 2
        reason = ("one transmitter alone has nothing to coincide with; include at least two."
                  if not timing_ok else "")

        if not timing_ok:
            record("coincidence", "SKIP", reason)
            record("divergence", "SKIP", "requires a coincidence measurement.")
            record("stability", "SKIP", "requires a coincidence measurement.")
            res = self._guarded(record, lambda: self.run(repeats=repeats, spacing_us=spacing_us),
                                wait_s=clock_wait_s)
            if res is not None:
                self._gate_stage(record,
                                 dict((k, v["shots"]) for k, v in res["shots_by_ap"].items()),
                                 res["repeats"], tolerance=refused_fraction)
            return self._verdict(out)

        spacing_now = int(spacing_us if spacing_us is not None else self.plan["spacing_us"])
        if spacing_now > 0:
            phase = stagger_us % spacing_now
            if min(phase, spacing_now - phase) < COLLIDE_GUARD_US:
                stagger_us = max(COLLIDE_GUARD_US, spacing_now // 4)

        rounds = []
        for i in range(2):
            if i:
                time.sleep(settle_s)
            got = self._guarded(record,
                                lambda: self._timing_round(repeats, stagger_us, spacing_us),
                                wait_s=clock_wait_s)
            if got is None:
                for stage in ("coincidence", "divergence", "stability"):
                    record(stage, "SKIP", "requires a round that was actually commanded.")
                return self._verdict(out)
            rounds.append(got)

        merged = {}
        for _times, fired, _t, _s, _n, _sp in rounds:
            for name, shotmap in fired.items():
                merged.setdefault(name, {}).update(shotmap)
        self._gate_stage(record, merged, sum(r[4] for r in rounds),
                         tolerance=refused_fraction)

        # Each transmitter's own arrival against its own commanded instant, with a constant
        # offset and a slow drift removed because the two readings come from different clocks.
        #
        # What is left is NOT purely how well the receiver stamps: it also contains whatever
        # the receiver's clock does relative to that transmitter's beyond a straight line. That
        # part is common to every transmitter this receiver hears, so it cancels in the
        # comparison between them and does not affect whether they coincided.
        #
        # The discriminating case is when it is large for EVERY transmitter at once *and* the
        # comparison between them is also large: then the noise is per-frame, it does not
        # cancel, and it is the receiver rather than the radios.
        selfnoise = {}
        for times, _f, tgts, seq0, nshots, sp in rounds:
            for ap in self.plan["aps"]:
                got = times.get(ap["name"]) or {}
                pairs = []
                for i in range(nshots):
                    seq = wire.SeqAllocator.wire(seq0 + i)
                    if seq in got:
                        pairs.append((i, got[seq] - (tgts[ap["mac"]] + i * sp)))
                if len(pairs) >= 3:
                    r = timing.detrend_residual(pairs)
                    prev = selfnoise.get(ap["name"])
                    if prev is None or r["residual_median_us"] > prev:
                        selfnoise[ap["name"]] = r["residual_median_us"]

        summaries = []
        for times, _f, _t, _s, _n, _sp in rounds:
            ref_name = self.plan["aps"][0]["name"]
            per_ap = {}
            for i, ap in enumerate(self.plan["aps"][1:], start=1):
                errs, common = timing.pairwise_errors(times.get(ref_name, {}),
                                                      times.get(ap["name"], {}), stagger_us, i)
                per_ap[ap["name"]] = timing.stats(errs, len(common),
                                                  positions=range(len(common)))
            summaries.append(per_ap)

        # No arrival times at all is the receiver; arrival times that do not pair is the link.
        # Reporting both as "nothing paired" would send someone to check the wrong thing.
        if not any(t for _times, _f, _t, _s, _n, _sp in rounds for t in _times.values()):
            record("coincidence", "FAIL",
                   "the observer %r returned no receive timestamps, so there is nothing to pair "
                   "transmissions on. Its captures carry no per-frame time; a coincidence "
                   "measurement needs a receiver whose radiotap provides one."
                   % self.plan["observer"]["name"])
            for stage in ("divergence", "stability"):
                record(stage, "SKIP", "requires a coincidence measurement.")
            return self._verdict(out)

        pairs = [s for s in summaries[0].values() if s["bias_us"] is not None]
        if not pairs:
            record("coincidence", "FAIL",
                   "nothing paired: no shot was received from both transmitters, so how closely "
                   "they coincided cannot be established.", per_ap=summaries[0])
            record("divergence", "SKIP", "requires paired shots.")
            record("stability", "SKIP", "requires paired shots.")
            return self._verdict(out)

        # A steady displacement and scatter about it are different defects -- a mis-estimated
        # relation against a receiver stamping badly -- and a deployment shows very different
        # amounts of each, so they are judged separately rather than against one number.
        worst_bias = max(abs(s["bias_us"]) for s in pairs)
        worst_jit = max(s["jitter_us"] for s in pairs)
        why = []
        if worst_bias > bias_us:
            why.append("a %.2f us displacement between them (limit %.2f)"
                       % (worst_bias, bias_us))
        if worst_jit > jitter_us:
            why.append("%.2f us of scatter around it (limit %.2f)" % (worst_jit, jitter_us))
        noisy = sorted(n for n in selfnoise.values() if n is not None)
        blame_receiver = (len(noisy) >= 2 and noisy[0] > jitter_us / 2.0)
        if why and blame_receiver:
            why.append("and every transmitter is equally inconsistent against its own commanded "
                       "instant (%s). Noise that shows up for all of them AND does not cancel "
                       "between them is per-frame, so it is this receiver's stamping rather "
                       "than the transmitters disagreeing"
                       % ", ".join("%s %.1f us" % (k, v) for k, v in sorted(selfnoise.items())
                                   if v is not None))
        # Reported every run because it is what tells a large spread apart from a receiver
        # that cannot time: on its own a large value means clock wander that cancels, but
        # together with a large spread it means per-frame noise that does not.
        residual = max([v for v in selfnoise.values() if v is not None] or [0.0])
        record("coincidence", "PASS" if not why else "FAIL",
               "%.2f us apart, %.2f us spread (against own instant %.2f us)"
               % (worst_bias, worst_jit, residual) if not why else
               "the transmissions did not coincide: " + "; ".join(why),
               per_ap=summaries[0], bias_threshold_us=bias_us, jitter_threshold_us=jitter_us,
               own_instant_residual_us=residual, self_residual_us=selfnoise)

        drifts = [s["drift_us_per_shot"] for s in pairs if s["drift_us_per_shot"] is not None]
        if not drifts:
            record("divergence", "SKIP", "too few paired shots to establish a rate of change.")
        else:
            worst_drift = max(abs(d) for d in drifts)
            record("divergence", "PASS" if worst_drift <= drift_us_per_shot else "FAIL",
                   "%.3f us/shot" % worst_drift if worst_drift <= drift_us_per_shot else
                   "the separation grows by %.4f us per shot: the transmitters are running at "
                   "different rates rather than sitting at a fixed offset, so the error "
                   "accumulates without limit" % worst_drift,
                   worst_us_per_shot=worst_drift, threshold=drift_us_per_shot)

        walk = None
        for name, first in summaries[0].items():
            second = summaries[1].get(name) or {}
            if first["bias_us"] is None or second.get("bias_us") is None:
                continue
            d = abs(second["bias_us"] - first["bias_us"])
            walk = d if walk is None else max(walk, d)
        if walk is None:
            record("stability", "SKIP",
                   "the second measurement paired nothing, so no comparison is possible.")
        else:
            record("stability", "PASS" if walk <= stability_us else "FAIL",
                   "%.2f us over %.0f s" % (walk, settle_s) if walk <= stability_us else
                   "the offset moved %.2f us between two measurements %.0f s apart, so the "
                   "relation is being re-estimated differently over time rather than holding"
                   % (walk, settle_s),
                   bias_shift_us=round(walk, 3), threshold_us=stability_us)

        return self._verdict(out)

    def _guarded(self, record, fn, wait_s=15.0):
        """Run one round, turning a refusal to command it into a reported failure.

        A health check that raises is useless precisely when it is needed: every reason a round
        cannot be commanded -- an unrelatable clock, a relation too uncertain to extrapolate, a
        participant that stopped answering -- is a finding about the rig, and belongs in the
        report next to the checks that did run.
        """
        try:
            return fn()
        except NodeMissing as e:
            record("fire", "FAIL", "the round could not be commanded: %s" % e)
            return None
        except (clockd_mod.ClockNotReady, clockd_mod.ClockTooUncertain):
            pass
        # Observations arrive over the same network as everything else, so a brief gap in them
        # is ordinary and self-correcting. Only a gap that persists is a finding, so the clock
        # is given a chance to catch up before the round is called impossible.
        try:
            self.wait_for_clock(timeout=wait_s)
            return fn()
        except (clockd_mod.ClockNotReady, clockd_mod.ClockTooUncertain, NodeMissing) as e:
            record("fire", "FAIL",
                   "the round could not be commanded, and the clocks did not recover within "
                   "%.0f s: %s" % (wait_s, e))
            return None

    def _gate_stage(self, record, fired, expected, tolerance=0.05):
        """The gate's own per-shot outcome, which is what observes the controller-to-transmitter
        relation: a shared instant placed outside the window the gate accepts is refused, and the
        refusal names which side of the window it fell on.

        Isolated refusals are counted and reported but do not condemn the rig: a refused shot is
        excluded from the accounting rather than counted as a loss, so it costs sample size and
        nothing else. A sustained rate is the thing that means the relation itself is wrong.
        """
        from .agent import (FIRED, NOBF, LATE, TOOFAR, CLKLOST, DRAIN_STUCK, SKIPPED,
                            UNKNOWN_OUTCOME)
        names = {FIRED: "fired", NOBF: "no buffer", LATE: "instant already passed",
                 TOOFAR: "instant too far ahead", CLKLOST: "radio clock stopped or reset",
                 DRAIN_STUCK: "queue never drained",
                 SKIPPED: "skipped by the agent",
                 UNKNOWN_OUTCOME: "write failed, outcome unknown"}
        tally = {}
        bad = {}
        late = {}
        where = {}
        for ap, shotmap in fired.items():
            counts = {}
            for code in shotmap.values():
                counts[code] = counts.get(code, 0) + 1
            tally[ap] = dict((names.get(c, str(c)), n) for c, n in counts.items())
            off = sorted(s for s, c in shotmap.items() if c in (LATE, TOOFAR))
            if off:
                bad[ap] = len(off)
                late[ap] = sum(1 for c in shotmap.values() if c == LATE)
                # Which shots were refused separates a relation that is wrong throughout from
                # one that only fails at the far end of a batch, where the extrapolation is
                # longest -- different causes, so the positions are reported, not just a count.
                where[ap] = {"refused_seqs": off[:16],
                             "positions": [sorted(shotmap).index(s) for s in off[:16]],
                             "of_shots": len(shotmap)}
        rate = max([float(n) / max(1, len(fired.get(ap) or {})) for ap, n in bad.items()] + [0.0])
        # Which side of the window a shot fell outside names a different cause. Already past
        # means the instruction did not reach the transmitter in time, which is the control
        # link and the lead allowed for it, and shows up bunched at the start of a batch. Too
        # far ahead means the instant was mapped into the wrong place, which is the relation
        # between the clocks. Reporting them together would point at the wrong repair.
        head = sum(late.values()) * 2 >= sum(bad.values())
        if rate > tolerance:
            record("fire", "FAIL",
                   "the gate refused %.0f%% of shots (%s) because the instant had already "
                   "passed by the time the transmitter was reached. The clocks agree; the lead "
                   "allowed between commanding a round and firing it does not cover the delay "
                   "in reaching the nodes. Raise lead_us, or use a quieter control link."
                   % (100.0 * rate, ", ".join("%s: %d" % kv for kv in sorted(bad.items())))
                   if head else
                   "the gate refused %.0f%% of shots (%s) as too far ahead of the transmitters' "
                   "own clocks. The instant was mapped into the wrong place, so the relation "
                   "between the clocks is wrong rather than merely late."
                   % (100.0 * rate, ", ".join("%s: %d" % kv for kv in sorted(bad.items()))),
                   per_ap=tally, refused=where, refused_fraction=round(rate, 4))
        elif bad:
            record("fire", "PASS",
                   "%d shots, %.0f%% refused (below the %.0f%% that would mean the relation "
                   "itself is wrong)" % (expected, 100.0 * rate, 100.0 * tolerance),
                   per_ap=tally, refused=where, refused_fraction=round(rate, 4))
        else:
            record("fire", "PASS", "%d shots" % expected, per_ap=tally,
                   refused_fraction=0.0)

    def _verdict(self, stages):
        """FAIL beats INCONCLUSIVE beats PASS. A skipped check cannot be the reason a rig is
        declared healthy, so it degrades the result rather than being ignored."""
        verdicts = set(s["verdict"] for s in stages)
        if "FAIL" in verdicts:
            overall = "FAIL"
        elif "SKIP" in verdicts:
            overall = "INCONCLUSIVE"
        else:
            overall = "PASS"
        return {"verdict": overall, "stages": stages,
                "failed": [s["stage"] for s in stages if s["verdict"] == "FAIL"],
                "skipped": [s["stage"] for s in stages if s["verdict"] == "SKIP"]}

    def peer_levels(self):
        """{(transmitter, transmitter): dBm} -- how strongly each hears the others, by name.

        Directional, and only for pairs actually heard. A pair that is absent has not been
        observed rather than being out of range, so it is left out instead of reported as zero.
        """
        by_mac = dict((n["mac"], n["name"]) for n in self.plan["nodes"] if n.get("mac"))
        out = {}
        for (hearer, sender), dbm in self.levels.levels().items():
            if hearer in by_mac and sender in by_mac:
                out[(by_mac[hearer], by_mac[sender])] = dbm
        return out

    def status(self):
        with self._lock:
            agents = dict(self.agents)
            rows = self.beacon_rows
        return {"run": self.run_id, "agents": sorted(agents), "beacon_rows": rows,
                "clock": self.clockd.status()}


# Single-stream rates for the modulation indices this radio supports, used to weight delivery
# into a comparable figure. Raw counts travel alongside so any other weighting can be applied
# afterwards.
_HT20_LGI_MBPS = {0: 6.5, 1: 13.0, 2: 19.5, 3: 26.0, 4: 39.0, 5: 52.0, 6: 58.5, 7: 65.0}


def _rate_mbps(mcs):
    return _HT20_LGI_MBPS.get(int(mcs), 0.0)
