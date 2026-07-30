"""Maintains the relation between every AP's TSF clock, and the anchor that turns it into a
commandable instant.

Observations arrive continuously from whichever nodes can see beacons. An AP that hears a peer
contributes a direct edge between the two clocks; a station that hears several APs contributes
edges between them with its own clock cancelled. Both feed one graph, so a deployment can use
either source, or both, without changing anything here.

Firing needs two separate things, and conflating them is the classic way to get a confidently
wrong answer:

  * the *relation* between clocks, which is what determines whether transmissions coincide. Its
    accuracy is bounded by how well each slope is estimated and how far the instant is
    extrapolated beyond the samples, so the usable lifetime of a fit is derived from the fit's
    own uncertainty rather than assumed.
  * the *absolute* choice of instant, which only has to land inside the window the gate will
    accept. Choosing it slightly early or late delays every AP by the same amount and does not
    pull them apart, so a coarse anchor is sufficient and no clock needs to be interrogated on
    the critical path.
"""
import time

from . import clockgraph as cg

WINDOW_US_DEFAULT = 20000000       # sample span kept per series
# Tolerated error in the clock relation from extrapolating to the commanded instant. Kept
# comfortably below the precision the gate itself achieves, so it never becomes the dominant
# term, but not so tight that ordinary variation in when observations arrive makes a round
# impossible to command.
BUDGET_US_DEFAULT = 1.0
MIN_SAMPLES = 3                    # below this a slope has no measurable uncertainty
ANCHOR_MAX_AGE_S_DEFAULT = 5.0     # beyond this the anchor is too old to place an instant


class ClockNotReady(RuntimeError):
    """Not enough has been observed to relate the participating clocks."""


class ClockTooUncertain(RuntimeError):
    """The relation is known, but not precisely enough for the requested instant."""


class _Series(object):
    """Samples for one directed observation, newest-bounded by a time span."""

    def __init__(self, window_us):
        self.window_us = window_us
        self.samples = []
        self.last_x = None
        self.last_y = None

    def add(self, x, y):
        if self.last_x is not None:
            if x == self.last_x:
                return                      # the same observation read twice
            if (self.last_x - x) > cg.MAX_GAP_US or (x - self.last_x) > cg.MAX_GAP_US:
                self.samples = []           # a clock restarted; earlier samples are unrelated
            elif x < self.last_x:
                # Already seen, re-delivered. The ring the observations come from has no
                # per-reader cursor, so the same rows arrive again whenever a publish is
                # retried. Discarding the window for that would throw away the whole fit
                # instead of one redundant sample.
                return
            elif abs((y - self.last_y) - (x - self.last_x)) > cg.MAX_SKEW_US:
                # Both clocks advance at nearly the same rate, so between two consecutive
                # observations they must advance by nearly the same amount. A large disagreement
                # means one of them was reset rather than that it is running fast. Keeping the
                # samples from both sides would fit a line straight through the step and yield a
                # confident, badly wrong relation.
                self.samples = []
        self.last_x = x
        self.last_y = y
        self.samples.append((x, y))
        cutoff = x - self.window_us
        if self.samples[0][0] < cutoff:
            self.samples = [s for s in self.samples if s[0] >= cutoff]

    def fit(self):
        if len(self.samples) < MIN_SAMPLES:
            return None
        return cg.fit_with_stderr(self.samples)

    def span_us(self):
        if len(self.samples) < 2:
            return 0
        return self.samples[-1][0] - self.samples[0][0]


class ClockD(object):
    """The clock graph, its anchor, and the checks that gate firing on it."""

    def __init__(self, reference_mac, participants=None, window_us=WINDOW_US_DEFAULT,
                 budget_us=BUDGET_US_DEFAULT,
                 anchor_max_age_s=ANCHOR_MAX_AGE_S_DEFAULT, clock=None):
        self.reference = reference_mac.lower()
        # Observations from addresses outside the testbed are discarded. Neighbouring networks
        # are heard constantly, and admitting them would add clocks that are not participating
        # and can form paths between participants that no measurement supports.
        self.participants = (set(m.lower() for m in participants)
                             if participants else None)
        self.window_us = int(window_us)
        self.budget_us = float(budget_us)
        self.anchor_max_age_s = float(anchor_max_age_s)
        self._clock = clock or time.monotonic

        self._peer = {}          # (hearer, sender) -> _Series, both APs
        self._station = {}       # station -> {sender -> _Series}
        self._anchor = None      # (reference tsf, monotonic when observed)

    # ------------------------------------------------------------------ ingest

    def observe_peer_beacon(self, hearer, sender, t1, t2, rate_idx, length):
        """An AP heard a peer's beacon: `t1` is the sender's transmit timestamp, `t2` the
        hearer's receive time, corrected here for the frame's own duration."""
        hearer, sender = hearer.lower(), sender.lower()
        if hearer == sender:
            return
        # A reception is a reading of the hearer's own clock whoever sent the frame, so it can
        # anchor the reference even when no peer is in range -- which is the ordinary case for a
        # deployment with one transmitter, and the state a deployment falls into when the other
        # transmitters go quiet. Only the *edge between two clocks* needs both of them to be
        # participating, so the filter stays on the edge below.
        self._note_reference(hearer, t2, sender, t1)
        if not self._known(hearer) or not self._known(sender):
            return
        x = t2 - cg.ppdu_airtime_us(rate_idx, length)
        key = (hearer, sender)
        s = self._peer.get(key)
        if s is None:
            s = self._peer[key] = _Series(self.window_us)
        s.add(x, t1)

    def observe_station_beacon(self, station, sender, station_time, t1):
        """A station heard an AP's beacon. No duration correction: the station's clock appears on
        both sides of every edge it contributes and cancels out."""
        station, sender = station.lower(), sender.lower()
        if not self._known(sender):
            return
        fits = self._station.get(station)
        if fits is None:
            fits = self._station[station] = {}
        s = fits.get(sender)
        if s is None:
            s = fits[sender] = _Series(self.window_us)
        s.add(station_time, t1)
        self._note_reference(None, None, sender, t1)

    def _known(self, mac):
        return self.participants is None or mac in self.participants

    def _note_reference(self, hearer, t2, sender, t1):
        """Keep the newest reading of the reference clock, whoever supplied it."""
        value = None
        if sender == self.reference:
            value = t1
        elif hearer is not None and hearer == self.reference:
            value = t2
        if value is None:
            return
        if self._anchor is None or value > self._anchor[0]:
            self._anchor = (int(value), self._clock())
            return
        # A lower reading is normally an observation arriving out of order, so the newest is
        # kept. But a clock that has been reset only ever reads lower from then on, and holding
        # the pre-reset value would leave the anchor stale for good -- firing would be refused
        # for ever, with nothing to recover it. Once nothing newer has arrived for as long as an
        # anchor is allowed to live, take whatever is arriving now.
        if (self._clock() - self._anchor[1]) > self.anchor_max_age_s:
            self._anchor = (int(value), self._clock())

    # ------------------------------------------------------------------ solve

    def edges(self):
        """Every clock-to-clock relation currently supported by observations."""
        out = {}
        stderr = {}
        for (hearer, sender), s in self._peer.items():
            f = s.fit()
            if f is None:
                continue
            a, b, se = f
            out[(hearer, sender)] = (a, b)
            stderr[(hearer, sender)] = se
        for station, fits in self._station.items():
            resolved = {}
            worst = 0.0
            for sender, s in fits.items():
                f = s.fit()
                if f is None:
                    continue
                a, b, se = f
                resolved[sender] = (a, b)
                worst = max(worst, se)
            for key, val in cg.monitor_edges(resolved).items():
                out[key] = val
                # An edge built from two fits inherits uncertainty from both.
                stderr[key] = max(stderr.get(key, 0.0), 2.0 * worst)
        return out, stderr

    def solve(self):
        """{mac: (offset, slope, slope_stderr)} anchored at the current reference reading."""
        if self._anchor is None:
            raise ClockNotReady(
                "no reading of the reference clock yet: nothing has reported hearing %s"
                % self.reference)
        edges, stderr = self.edges()
        maps = cg.solve_offsets(edges, self.reference)
        ref_tsf = self._anchor[0]
        offsets = cg.offsets_from_maps(maps, self.reference, ref_tsf)

        # Attribute the worst uncertainty along each path; without per-path bookkeeping the
        # conservative choice is the worst edge in the graph, which cannot understate it.
        worst = max(list(stderr.values()) + [0.0])
        out = {}
        for mac, (off, slope) in offsets.items():
            out[mac] = (off, slope, 0.0 if mac == self.reference else worst)
        return out, ref_tsf

    # ------------------------------------------------------------------ firing

    def projected_error_us(self, lead_us):
        """Expected error in the clock relation at an instant `lead_us` ahead.

        The same quantity `targets` refuses on, exposed so a caller can wait for the fit to
        mature instead of proceeding the moment it first squeaks under the budget.
        """
        offsets, ref_tsf = self.solve()
        dt = (self.anchor_age_s() or 0.0) * 1e6 + lead_us
        worst = max([se for _off, _slope, se in offsets.values()] + [0.0])
        return worst * dt

    def anchor_age_s(self):
        if self._anchor is None:
            return None
        return self._clock() - self._anchor[1]

    def shared_instant(self, lead_us):
        """An instant `lead_us` in the future, expressed in the reference clock."""
        if self._anchor is None:
            raise ClockNotReady("no reference clock reading yet")
        age = self.anchor_age_s()
        if age > self.anchor_max_age_s:
            raise ClockNotReady(
                "the newest reference clock reading is %.1f s old (limit %.1f s); beacons are "
                "not being observed, so an instant placed now could fall outside the window the "
                "gate accepts" % (age, self.anchor_max_age_s))
        return int(self._anchor[0] + age * 1e6 + lead_us)

    def targets(self, macs, shared_instant, stagger_us=0):
        """{mac: target} -- the shared instant in each AP's own clock.

        Refuses rather than guesses: a clock with no path to the reference, or one whose relation
        is not pinned down well enough for this instant, produces an error naming the cause.
        """
        offsets, ref_tsf = self.solve()
        dt = abs(shared_instant - ref_tsf)
        out = {}
        for i, mac in enumerate(macs):
            mac = mac.lower()
            if mac not in offsets:
                raise ClockNotReady(
                    "no path from the reference clock to %s: it neither hears nor is heard by "
                    "any node connected to %s" % (mac, self.reference))
            off, slope, se = offsets[mac]
            err = se * dt
            if err > self.budget_us:
                raise ClockTooUncertain(
                    "%s: extrapolating %.3f s beyond the samples gives %.2f us of expected "
                    "error, above the %.2f us budget. Either the observation window is too "
                    "short or beacons have stopped arriving."
                    % (mac, dt / 1e6, err, self.budget_us))
            out[mac] = cg.target_tsf(shared_instant, off, slope, ref_tsf, stagger_us, i)
        return out

    def path_disagreement(self, at_tsf=None):
        """How far independent routes through the graph disagree about each clock, in µs.

        A single traversal is silent about a bad edge: whichever route it happens to take becomes
        the answer, and a contradicting edge elsewhere is indistinguishable from a correct one
        until transmissions fail to coincide. Removing each edge in turn and re-solving surfaces
        that contradiction directly.

        Per clock: `redundant` is false when some single edge is the only way to reach it, so
        nothing corroborates its map however good the fit looks; `max_us` is the largest
        disagreement among the routes that remain.
        """
        if self._anchor is None:
            raise ClockNotReady("no reference clock reading yet")
        at = self._anchor[0] if at_tsf is None else at_tsf
        edges, _ = self.edges()
        base = cg.solve_offsets(edges, self.reference)
        out = {}
        for mac in base:
            if mac != self.reference:
                out[mac] = {"redundant": True, "max_us": 0.0}
        for drop in list(edges):
            pruned = dict((k, v) for k, v in edges.items() if k != drop)
            maps = cg.solve_offsets(pruned, self.reference)
            for mac, entry in out.items():
                if mac not in maps:
                    entry["redundant"] = False
                    continue
                a, b = maps[mac]
                a0, b0 = base[mac]
                entry["max_us"] = max(entry["max_us"], abs((a + b * at) - (a0 + b0 * at)))
        for entry in out.values():
            if not entry["redundant"]:
                entry["max_us"] = None
        return out

    # ------------------------------------------------------------------ status

    def status(self):
        """A description of the clock plane, for a health report."""
        edges, stderr = self.edges()
        spans = {}
        for key, s in self._peer.items():
            spans["%s<-%s" % key] = s.span_us()
        for station, fits in self._station.items():
            for sender, s in fits.items():
                spans["%s@%s" % (sender, station)] = s.span_us()
        try:
            offsets, ref_tsf = self.solve()
            reachable = sorted(offsets)
            err = None
        except ClockNotReady as e:
            reachable = []
            ref_tsf = None
            err = str(e)
        return {
            "reference": self.reference,
            # Which kind of observation is actually arriving. Both feed one graph, so a
            # deployment can use either or both; this is what lets a caller check that the
            # source it declared is the one contributing.
            "sources": sorted(
                (["beacon"] if any(s.samples for s in self._peer.values()) else [])
                + (["monitor"] if any(s.samples for fits in self._station.values()
                                        for s in fits.values()) else [])),
            "ref_tsf": ref_tsf,
            "anchor_age_s": self.anchor_age_s(),
            "edges": sorted("%s->%s" % k for k in edges),
            "worst_slope_stderr": max(list(stderr.values()) + [0.0]),
            "sample_span_us": spans,
            "reachable": reachable,
            "error": err,
        }
