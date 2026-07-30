"""Station-side frame accounting: what arrived, and how well bounded that count is.

Runs inside the station agent, fed every frame the capture socket delivers. Capture is
continuous, so a round costs one question and one answer rather than starting and stopping a
capture around every round.

Counting across rounds admits one failure mode, and it is the only one here that makes delivery
look BETTER than reality: if a sequence number still resident in the map is reused, frames that
were never sent get counted. Three mechanisms stop that, and each is pinned by a test:

  * run namespacing -- a new run_id evicts old runs, so a fresh experiment against a
    still-running agent cannot inherit counts;
  * an explicit seq range per report, with `coverage` reporting full/partial/evicted so a
    partially-forgotten range is refused rather than answered with a low count;
  * a monotone `ordinal` that the caller echoes back as `since_ordinal`, giving an exact
    "arrived after the previous round" barrier at no extra cost.

Python 3.4 compatible: deployed to nodes.
"""
from . import radiotap
from . import wire

COVERAGE_FULL = "full"
COVERAGE_PARTIAL = "partial"
COVERAGE_EVICTED = "evicted"

SEQ_RETAIN_DEFAULT = 4096      # distinct seq buckets kept per (run, AP); << 32768, see wire
RUN_RETAIN_DEFAULT = 2         # current run plus one, so a late report still resolves

# Beacon observations held for the controller to collect. Bounded because a receiver runs
# continuously and the controller polls at its own pace.
BEACON_KEEP = 4096


class _Bucket(object):
    """One AP's observations within one run."""

    def __init__(self):
        self.seqs = {}             # wire seq -> {idx: (ordinal, signal_dbm, receive time)}
        self.order = []            # the same keys in arrival order, oldest first
        self.evicted_max = None    # highest seq forgotten so far
        self.rx_frames = 0
        self.mcs = {}              # on-air modulation index -> frames carrying it


class FrameCounter(object):
    """Counts stamped frames per (run, AP, seq, subframe).

    `ap_macs` is the set of accepted source addresses; `station_id` is this station's stamped
    id. A frame counts only when BOTH match, so a station overhearing a frame addressed to a
    different station is never miscounted as delivered.
    """

    def __init__(self, ap_macs, station_id, stamp_off=wire.STAMP_OFFSET,
                 seq_retain=SEQ_RETAIN_DEFAULT, run_retain=RUN_RETAIN_DEFAULT, epoch=""):
        wire.SeqAllocator.check_retention(seq_retain)
        self.ap_macs = set(m.lower() for m in ap_macs)
        self.station_id = int(station_id)
        self.stamp_off = int(stamp_off)
        self.seq_retain = int(seq_retain)
        self.run_retain = int(run_retain)
        self.epoch = epoch

        self._runs = {}            # run_id -> {ap_mac -> _Bucket}
        self._run_order = []       # eviction order, oldest first
        self.run_id = None

        self.ordinal = 0           # monotone count of frames matched as ours, all runs
        self.other_frames = 0      # frames seen that are not ours -- the liveness signal
        # Whether this receiver reports a modulation index at all. A card that reports none
        # makes every frame look legacy, so without this a commanded rate would appear to have
        # been downgraded on a receiver that simply cannot see it.
        self.mcs_capable = False
        # (transmitter, local receive time, that transmitter's own clock), newest last.
        self.beacons = []
        self.freqs = {}            # observed radiotap channel -> count
        self.drops = 0             # accumulated from the socket, added by the agent

    # ------------------------------------------------------------------ runs

    def set_run(self, run_id):
        """Begin (or resume) a run. A new run evicts old ones so counts cannot be inherited."""
        if run_id not in self._runs:
            self._runs[run_id] = {}
            self._run_order.append(run_id)
            while len(self._run_order) > self.run_retain:
                self._runs.pop(self._run_order.pop(0), None)
        self.run_id = run_id

    def _bucket(self, ap_mac):
        if self.run_id is None:
            return None
        aps = self._runs.get(self.run_id)
        if aps is None:
            return None
        b = aps.get(ap_mac)
        if b is None:
            b = aps[ap_mac] = _Bucket()
        return b

    # ------------------------------------------------------------------ ingest

    def observe(self, buf):
        """Feed one raw captured frame. Returns True if it was one of ours."""
        f = radiotap.parse(buf, stamp_off=self.stamp_off)
        if f is None:
            self.other_frames += 1
            return False
        return self.observe_parsed(f)

    def observe_parsed(self, f):
        """Same, for an already-parsed frame."""
        # Recorded for every frame, ours or not: the channel this card is really on is a
        # property of the card, not of this traffic, and a drifted card delivers none of it.
        freq = f.get("freq_mhz")
        if freq is not None:
            self.freqs[freq] = self.freqs.get(freq, 0) + 1
        if f.get("mcs") is not None:
            self.mcs_capable = True

        # A beacon from a watched transmitter carries that transmitter's own clock, and this
        # frame carries the local receive time for it. Both halves of a clock relation, from
        # traffic that is already being captured.
        sa = f.get("sa")
        if (f.get("beacon_tsf") is not None and f.get("tsft") is not None
                and sa in self.ap_macs):
            self.beacons.append((sa, int(f["tsft"]), int(f["beacon_tsf"])))
            if len(self.beacons) > BEACON_KEEP:
                del self.beacons[:len(self.beacons) - BEACON_KEEP]

        stamp = f.get("stamp")
        sa = f.get("sa")
        if stamp is None or sa is None or sa not in self.ap_macs:
            self.other_frames += 1
            return False
        sid, seq, idx = stamp
        if sid != self.station_id:
            # Addressed to a different station. Real traffic, but not ours -- it still proves
            # the capture is alive, so it counts as `other`, never as delivered.
            self.other_frames += 1
            return False
        b = self._bucket(sa)
        if b is None:
            self.other_frames += 1
            return False

        self.ordinal += 1
        b.rx_frames += 1
        by_idx = b.seqs.get(seq)
        if by_idx is None:
            by_idx = b.seqs[seq] = {}
            b.order.append(seq)
            self._evict(b)
        if idx not in by_idx:      # duplicate (seq, idx) never inflates the count
            by_idx[idx] = (self.ordinal, radiotap.usable_signal(f.get("signal_dbm")),
                           f.get("tsft"))
            m = f.get("mcs")
            if m is not None:
                b.mcs[m] = b.mcs.get(m, 0) + 1
        return True

    def _evict(self, b):
        """Forget the entries that arrived earliest.

        Not the numerically smallest key: the wire sequence field is 16 bits and folds
        (cosr.wire.SeqAllocator.wire), so once a run passes 65536 shots the smallest key is the
        newest entry. Evicting by value there discards fresh data and leaves `evicted_max`
        describing a range that was never forgotten, which then decides whether a delivery
        figure is reported at all.
        """
        while len(b.order) > self.seq_retain:
            oldest = b.order.pop(0)
            if b.seqs.pop(oldest, None) is None:
                continue
            if b.evicted_max is None or oldest > b.evicted_max:
                b.evicted_max = oldest

    # ------------------------------------------------------------------ report

    def _coverage(self, b, lo, n):
        if b.evicted_max is None:
            return COVERAGE_FULL
        hi = lo + n - 1
        if lo > b.evicted_max:
            return COVERAGE_FULL
        if hi <= b.evicted_max:
            return COVERAGE_EVICTED
        return COVERAGE_PARTIAL

    def report(self, seq_lo, n, since_ordinal=0, run_id=None, other_since=0):
        """Counts for the wire-seq range [seq_lo, seq_lo+n) of a run.

        `since_ordinal` discards anything matched at or before that point, which is how a round
        excludes the previous round's frames without needing a clock. `coverage` must be checked
        by the caller: a PARTIAL or EVICTED range means the answer is a lower bound and the
        round is unmeasured, not a low delivery.
        """
        run = run_id if run_id is not None else self.run_id
        aps = self._runs.get(run, {})
        seqs = [wire.SeqAllocator.wire(seq_lo + i) for i in range(n)]
        per_ap = {}
        for mac in sorted(self.ap_macs):
            b = aps.get(mac)
            if b is None:
                per_ap[mac] = {"rx": 0, "coverage": COVERAGE_FULL, "idx_hist": {},
                               "mcs_hist": {},
                               "seqs_seen": [], "per_seq": {}, "tsft_by_seq": {},
                               "rssi_dbm": None}
                continue
            rx = 0
            idx_hist = {}
            seen = []
            sigs = []
            per_seq = {}
            tsft = {}
            for s in seqs:
                by_idx = b.seqs.get(s)
                if not by_idx:
                    continue
                fresh = [(i, sig, ts) for i, (o, sig, ts) in by_idx.items()
                         if o > since_ordinal]
                if not fresh:
                    continue
                seen.append(s)
                per_seq[s] = len(fresh)
                for i, sig, ts in fresh:
                    rx += 1
                    idx_hist[i] = idx_hist.get(i, 0) + 1
                    if sig is not None:
                        sigs.append(sig)
                    # Only the opening subframe carries a receive time that reflects when the
                    # transmission actually began; later ones share the aggregate's stamp.
                    if i == 0 and ts is not None:
                        tsft[s] = ts
            per_ap[mac] = {
                "rx": rx,
                "coverage": self._coverage(b, seq_lo, n),
                "idx_hist": idx_hist,
                "seqs_seen": sorted(seen),
                # Kept per shot as well as in total: only the caller knows which shots every
                # participant transmitted, and a figure counted over a wider set than it is
                # divided by is not a fraction of anything.
                "per_seq": per_seq,
                "tsft_by_seq": tsft,
                "rssi_dbm": radiotap.rssi_dbm(sigs),
                "mcs_hist": dict(b.mcs),
            }
        return {
            "run": run,
            "epoch": self.epoch,
            "ordinal": self.ordinal,
            "other_frames": self.other_frames,
            # Frames seen during this round alone. The lifetime total above cannot answer
            # "is the capture alive now?": on any real channel it is non-zero within moments
            # of starting, and then stays non-zero for ever, including after the capture dies.
            "other_frames_round": self.other_frames - int(other_since or 0),
            "drops": self.drops,
            "freq_mhz": self.observed_freq(),
            "mcs_capable": self.mcs_capable,
            "per_ap": per_ap,
        }

    def observed_freq(self):
        """The channel this card is actually on, from radiotap -- no `iw` call needed.

        Replaces re-asserting the channel before every capture: a card that drifted after a
        regulatory reset shows up here as a mismatch instead of as silent zero delivery.
        """
        if not self.freqs:
            return None
        best = None
        for f, c in self.freqs.items():
            if best is None or c > self.freqs[best]:
                best = f
        return best


def deagg_ok(idx_hist, nframes):
    """False when only the first subframe of every aggregate was recovered.

    That pattern means the receiver is not deaggregating, which would read as a uniform
    (nframes-1)/nframes loss -- a plausible-looking channel result with a plumbing cause. The
    caller must report it as unmeasured rather than as delivery.
    """
    if nframes <= 1:
        return True
    if not idx_hist:
        return True
    return any(int(i) > 0 for i in idx_hist)
