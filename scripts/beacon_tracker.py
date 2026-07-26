#!/usr/bin/env python3
"""Cross-AP TSF offset tracker for OTA beacon (AP-hears-AP) sync mode.

Runs on the CONTROL HOST, not on a testbed node. It removes the single-observer
bottleneck of the monitor tracker: instead of one monitor that must hear every AP,
each AP hears the neighbours in its range and the host composes those pairwise clock
relations into one global map.

Each AP with the driver beacon tap exposes a `cosr_beacons` debugfs node whose lines
are `<sender_mac> t1 t2 rate_idx len`, where t1 is the sender AP's TSF taken from the
beacon body (its TX clock), t2 is the hearing AP's own RX TSF (mactime, stamped at the
END of the PPDU), and rate_idx/len describe the received beacon so its on-air duration
can be removed. This tracker polls every AP's node over ssh, fits per directed edge
(hearer X hears sender Y)

    Y_tsf = a + b * X_tsf

and composes the edges from the reference AP (first AP in topo) to every AP by
breadth-first search, writing the SAME /tmp/offset.json the monitor tracker writes so
the controller consumes either source identically:

    { "ref_tsf": <int>, "mon_tsft": 0, "offsets": { "<mac>": [offset, slope], ... } }

Airtime, computed not tuned: t2 is stamped at the END of the beacon PPDU
(RX_FLAG_MACTIME_END) while t1 is the sender's TX clock, so a raw (t2, t1) pair spans
the whole beacon's on-air time. Rather than carry that as a per-testbed constant, the
driver tap now reports each beacon's PHY rate index and length, so the exact PPDU
duration is computed here from the 802.11 timing formula and subtracted from t2
(t2 -> RX-START). What remains is only the ~ns AP-to-AP propagation, negligible at µs
sync, so no airtime knob and no calibration: the edges are D-free like the monitor ones.
(Propagation truly is ns at a few metres; the ~0.5 ms that used to need calibrating was
the beacon's transmit time, not propagation.)

Usage:  beacon_tracker.py <topo.json> [--interval S] [--window N] [--out PATH]
"""
import sys
import json
import time
import math
import subprocess

WINDOW = 16                 # samples kept per edge for the sliding fit
INTERVAL = 1.0              # seconds between polling rounds
OUT_PATH = "/tmp/offset.json"
MAX_GAP_US = 5_000_000      # a t2 jump past this (or backwards) = hostapd restart: reset the edge

NODE_GLOB = "/sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_beacons"

# mac80211 2.4 GHz (band g) bitrate table, in Mbps, indexed by rx_status->rate_idx:
# 0-3 = CCK (1/2/5.5/11), 4-11 = OFDM (6/9/12/18/24/36/48/54). Stable ABI, so the host
# can map the driver's rate_idx without the driver having to encode the rate itself.
RATE_MBPS = [1.0, 2.0, 5.5, 11.0, 6.0, 9.0, 12.0, 18.0, 24.0, 36.0, 48.0, 54.0]

# Fixed AR9271 offset between where the chip samples the *transmitted* beacon Timestamp
# and where it stamps the *received* mactime (RX_FLAG_MACTIME_END). Measured on this
# hardware: the length-dependent PPDU airtime below fully tracks beacon-length changes
# (bias held at ~61 µs across 45- and 72-byte beacons), leaving only this constant. It is
# a silicon property, not a per-testbed value, so it is folded in here rather than tuned.
# Measured for CCK beacons (all AR9271/hostapd beacons on 2.4 GHz go out at 1 Mbps CCK);
# an OFDM-beacon testbed would want its own measurement (could not be forced on this
# hostapd to check). See docs/SYNC.md.
AR9271_TS_OFFSET_US = 61.0


def ppdu_airtime_us(rate_idx, length):
    """Effective RX-END -> TX-instant correction (µs) for a `length`-byte MPDU at
    PHY rate `rate_idx`: the 802.11 PPDU on-air time minus the fixed AR9271 timestamp
    offset (AR9271_TS_OFFSET_US), so subtracting it from t2 lands on the sender's TX
    instant to within ~ns propagation.

    CCK (idx<4): long preamble+header = 192 µs, then length*8/rate bits of PSDU.
    OFDM (idx>=4): 20 µs (L-STF+L-LTF+L-SIG) + 4 µs * ceil((16 SERVICE + 8*length + 6
    TAIL) / N_dbps), N_dbps = rate*4 bits per 4 µs symbol. Standard 802.11 timing.
    """
    if not (0 <= rate_idx < len(RATE_MBPS)):
        return 0.0
    rate = RATE_MBPS[rate_idx]
    if rate_idx < 4:
        air = 192.0 + math.ceil(length * 8 / rate)
    else:
        ndbps = rate * 4
        air = 20.0 + 4.0 * math.ceil((16 + 8 * length + 6) / ndbps)
    return max(0.0, air - AR9271_TS_OFFSET_US)


# --------------------------------------------------------------------------- ssh

def _ssh_base(ip, pw):
    """Multiplexed ssh argv (password auth; pubkey disabled on the node image).

    A polling loop reconnects rapidly; without a ControlMaster sshd refuses the
    reconnects and returns empty output, so the master is kept persistent.
    """
    return ["sshpass", "-p", pw, "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ConnectTimeout=8",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=/tmp/cm_bt_%s" % ip,
            "-o", "ControlPersist=120",
            "modwifi@" + ip]


def read_beacons(ip, pw):
    """Read one AP's cosr_beacons node; return [(sender_mac, t1, t2, rate_idx, len), ...]."""
    out = subprocess.run(_ssh_base(ip, pw) + ["cat " + NODE_GLOB + " 2>/dev/null"],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        f = line.split()
        if len(f) == 5 and ":" in f[0] and all(f[i].isdigit() for i in (1, 2, 3, 4)):
            rows.append((f[0].lower(), int(f[1]), int(f[2]), int(f[3]), int(f[4])))
    return rows


# --------------------------------------------------------------------------- math
# The affine fit / BFS / offset.json emit are shared with the multi-monitor tracker. The
# beacon edges are made D-free here by subtracting each beacon's PPDU airtime from t2, so
# no separate airtime-correction step is needed before composing.
from clock_graph import fit, solve_offsets, write_offsets


def main():
    argv = sys.argv[1:]
    interval, window, out = INTERVAL, WINDOW, OUT_PATH

    def take(flag, cast, cur):
        if flag in argv:
            i = argv.index(flag)
            v = cast(argv[i + 1])
            del argv[i:i + 2]
            return v
        return cur
    interval = take("--interval", float, interval)
    window = take("--window", int, window)
    out = take("--out", str, out)
    if not argv:
        sys.exit("usage: beacon_tracker.py <topo.json> [--interval S] [--window N] [--out PATH]")

    topo = json.load(open(argv[0]))
    pw = topo.get("password", "modwifi")
    aps = [(name, n) for name, n in topo["nodes"].items() if n.get("role") == "ap"]
    if len(aps) < 2:
        sys.exit("beacon_tracker needs >= 2 APs in topo")
    ref = aps[0][1]["mac"].lower()
    # macs we recognise as APs; a foreign beacon from a non-AP BSSID is ignored so it
    # never becomes a phantom graph node.
    ap_macs = {n["mac"].lower() for _, n in aps}

    hist = {}          # (hearer_mac, sender_mac) -> [(t2_start, t1), ...]   (x=t2_start, y=t1)
    last_t2 = {}       # same key -> last raw t2 seen, for the discontinuity guard
    ref_tsf = {"v": None}

    while True:
        for name, n in aps:
            x = n["mac"].lower()
            for sender, t1, t2, rate_idx, length in read_beacons(n["ip"], pw):
                if sender not in ap_macs or sender == x:
                    continue
                key = (x, sender)
                prev = last_t2.get(key)
                if prev is not None and (t2 <= prev or t2 - prev > MAX_GAP_US):
                    hist[key] = []                     # TSF reset/gap: drop the stale window
                if prev is not None and t2 == prev:
                    continue                           # ring re-read overlap: same sample
                last_t2[key] = t2
                # subtract the beacon's own on-air time: t2 (RX-end) -> RX-start, so the pair
                # (t2_start, t1) spans only ~ns propagation -> the edge is D-free, no calibration.
                t2_start = t2 - ppdu_airtime_us(rate_idx, length)
                hist.setdefault(key, []).append((t2_start, t1))
                hist[key][:] = hist[key][-window:]
                # track newest reference-clock value: ref as hearer (t2) or as sender (t1)
                if x == ref:
                    ref_tsf["v"] = t2 if ref_tsf["v"] is None else max(ref_tsf["v"], t2)
                if sender == ref:
                    ref_tsf["v"] = t1 if ref_tsf["v"] is None else max(ref_tsf["v"], t1)

        edges = {k: f for k, v in hist.items() for f in [fit(v)] if f}
        if edges and ref_tsf["v"] is not None:
            maps = solve_offsets(edges, ref)
            if len(maps) > 1:                          # reference reached at least one AP
                write_offsets(out, ref_tsf["v"], maps, ref)
        time.sleep(interval)


if __name__ == "__main__":
    main()
