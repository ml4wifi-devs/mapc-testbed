#!/usr/bin/env python3
"""Cross-AP TSF offset tracker, run on a monitor VM that hears every AP's beacons.

No AP may carry a monitor VIF (adding one to an AP's phy rebases its TSF and breaks the
on-chip gate), so the offset is measured passively here. The monitor hears every AP's
beacons; each beacon carries the sender AP's own TSF (first 8 body bytes) and is stamped
with the monitor's radiotap tsft -- one common clock for all APs.

Per AP we fit  ap_tsf = a + b * mon_tsft  over a sliding window, then express each AP's
offset from the *reference* AP (the first mac on the command line) as a linear function of
the reference's TSF, so the controller can extrapolate it to the exact fire instant. 
Writes /tmp/offset.json as JSON:

    { "ref_tsf": <int>,            # reference AP's TSF at the latest sample
      "mon_tsft": <int>,           # monitor tsft at that sample
      "offsets": { "<mac>": [offset, slope], ... } }   # ref mac -> [0, 0.0]

where offset = ap_tsf - ref_tsf at time ref_tsf, and slope = d(offset)/d(ref_tsf).

The controller maps the one shared instant (in the reference clock) into AP k's clock as
    target_k = shared + offset_k + slope_k * (shared - ref_tsf).

Usage:  python3 offset_tracker.py <monitor_iface> <ref_mac> <mac2> [mac3 ...] [--window N]
"""
import sys
import os
import re
import json
import subprocess

argv = sys.argv[1:]
WINDOW = 12
if "--window" in argv:
    i = argv.index("--window")
    WINDOW = int(argv[i + 1])
    del argv[i:i + 2]

# --raw PATH: multi-monitor mode. Instead of composing offsets against a reference AP
# (which assumes this monitor hears that AP), dump the per-AP raw fits (a, b) in THIS
# monitor's own tsft clock: ap_tsf = a + b*mon_tsft. The host (monitor_tracker.py) reads
# every monitor's raw fits and composes the AP graph, so a monitor that hears only a
# subset of APs still contributes its edges.
RAW_PATH = None
if "--raw" in argv:
    i = argv.index("--raw")
    RAW_PATH = argv[i + 1]
    del argv[i:i + 2]

MON_IFACE = argv[0]
MACS = [m.lower() for m in argv[1:]]          # MACS[0] is the reference AP (composed mode)
if len(MACS) < 1:
    sys.exit("usage: offset_tracker.py <iface> <ref_mac> [mac2 ...] [--window N] [--raw PATH]")
MAC_REF = MACS[0]

OFFSET_PATH = "/tmp/offset.json"


# This file is scp'd standalone to the monitor VM and run.sh byte-verifies that one file, so it is
# deliberately self-contained: `fit`/`write_offsets` are re-implemented here rather than imported
# from clock_graph.py (which stays on the control host). Do not "dedupe" by importing it.
def fit(samples):
    """Least-squares y = a + b*x over [(x, y), ...]; return (a, b) or None."""
    n = len(samples)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in samples) / n
    mean_y = sum(y for _, y in samples) / n
    denom = sum((x - mean_x) ** 2 for x, _ in samples)
    if denom == 0:
        return None
    b = sum((x - mean_x) * (y - mean_y) for x, y in samples) / denom
    return (mean_y - b * mean_x, b)


def beacon_tsf(hex_first_line):
    """Beacon Timestamp = first 8 body bytes, little-endian u64."""
    body = "".join(hex_first_line.split())
    return sum(int(body[i:i + 2], 16) << (8 * (i // 2)) for i in range(0, 16, 2))


def write_offsets(ref_tsf, mon_tsft, fit_ref, fits):
    """offset_k(ref_tsf) = (a_k-a_r) + (b_k-b_r)*(ref_tsf-a_r)/b_r   [linear, per AP].

    Identical arithmetic to the original two-AP write_offset, applied reference-vs-each-AP.
    Only APs with a valid fit are emitted; the reference maps to itself ([0, 0.0]).
    """
    a_r, b_r = fit_ref
    offsets = {}
    for mac, f in fits.items():
        if f is None:
            continue
        a_k, b_k = f
        slope = (b_k - b_r) / b_r
        offset = (a_k - a_r) + (b_k - b_r) * (ref_tsf - a_r) / b_r
        offsets[mac] = [round(offset), round(slope, 9)]
    # write-then-rename so a concurrent reader (the controller) never catches a half-written
    # file: rename is atomic on the same filesystem, so a read sees either the old or new JSON.
    tmp = OFFSET_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"ref_tsf": ref_tsf, "mon_tsft": mon_tsft, "offsets": offsets}, fh)
        fh.write("\n")
    os.replace(tmp, OFFSET_PATH)


def write_raw(mon_tsft, fits):
    """--raw mode: dump this monitor's per-AP fits (a, b) in its own tsft clock, atomically."""
    out = {m: [round(a, 3), round(b, 12)] for m, f in fits.items() if f for a, b in [f]}
    tmp = RAW_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"mon_tsft": mon_tsft, "fits": out}, fh)
        fh.write("\n")
    os.replace(tmp, RAW_PATH)


def main():
    proc = subprocess.Popen(
        ["tcpdump", "-i", MON_IFACE, "-e", "-n", "-x", "-l", "wlan[0]==0x80"],
        stdout=subprocess.PIPE, stderr=open("/dev/null", "w"))

    hist = {m: [] for m in MACS}             # mac -> [(mon_tsft, ap_tsf), ...]
    tsft = None
    sa = None

    # readline (not `for line in pipe`) so we get each beacon fresh: Python's pipe
    # read-ahead buffers several KB before yielding, which lags the tracker.
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace")

        m = re.search(r'(\d+)us tsft', line)
        if m:
            tsft = int(m.group(1))
        s = re.search(r'SA:([0-9a-fA-F:]{17})', line)
        if s:
            sa = s.group(1).lower()

        first_hex = re.match(r'\s+0x0000:\s+([0-9a-f ]+)', line)
        if not (first_hex and tsft is not None and sa in hist):
            continue

        hist[sa].append((tsft, beacon_tsf(first_hex.group(1))))
        hist[sa][:] = hist[sa][-WINDOW:]

        fits = {mac: fit(hist[mac]) for mac in MACS}
        if RAW_PATH is not None:
            # multi-monitor: emit raw per-AP fits in this monitor's clock, no reference needed
            if any(fits.values()):
                write_raw(tsft, fits)
        else:
            fit_ref = fits.get(MAC_REF)
            if fit_ref and fit_ref[1] != 0:
                ref_tsf = hist[MAC_REF][-1][1]
                write_offsets(ref_tsf, tsft, fit_ref, fits)

        tsft = None
        sa = None


if __name__ == "__main__":
    main()
