#!/usr/bin/env python3
"""Multi-monitor cross-AP TSF tracker, run on the control host.

The single-observer tracker (offset_tracker.py without --raw) needs one monitor that
hears EVERY AP. Multi-monitor mode drops that: several monitors each hear a subset of
APs, and an AP heard by two monitors bridges their clock axes -- the same graph the
beacon tracker composes, only the edges come from monitors instead of the APs' own taps.

Each monitor runs offset_tracker.py --raw (deployed by run.sh) and publishes, in its own
tsft clock, a per-AP fit  ap_tsf = a + b*mon_tsft  as /tmp/raw_fits.json. This host reads
every monitor's file over ssh and, for each monitor that hears both AP i and AP j, forms
the direct edge i->j by eliminating that monitor's clock:

    i = ai + bi*mon,  j = aj + bj*mon   =>   j = (aj - bj*ai/bi) + (bj/bi)*i

That edge is airtime-bias-free: the RX-end-vs-TX-start offset D is identical for both APs
at the one monitor (same beacon, same rate) and cancels in the elimination -- so unlike
beacon mode there is no airtime constant to measure. Edges from all monitors compose (via
clock_graph, breadth-first from the reference AP) into the same /tmp/offset.json the other
trackers write, which the controller reads locally.

Usage:  monitor_tracker.py <topo.json> [--interval S] [--out PATH]
"""
import sys
import json
import time
import subprocess

from clock_graph import solve_offsets, write_offsets

INTERVAL = 1.0
OUT_PATH = "/tmp/offset.json"
RAW_REMOTE = "/tmp/raw_fits.json"
TSF_GLOB = "/sys/kernel/debug/ieee80211/*/netdev:*/tsf"


def _ssh(ip, pw, cmd):
    argv = ["sshpass", "-p", pw, "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ConnectTimeout=8",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=/tmp/cm_mt_%s" % ip,
            "-o", "ControlPersist=120",
            "modwifi@" + ip, cmd]
    return subprocess.run(argv, capture_output=True, text=True).stdout


def read_raw_fits(ip, pw):
    """Read a monitor's /tmp/raw_fits.json; return {mac: (a, b)} or {} if absent/stale."""
    raw = _ssh(ip, pw, "cat " + RAW_REMOTE + " 2>/dev/null").strip()
    try:
        d = json.loads(raw)
        return {m.lower(): (float(a), float(b)) for m, (a, b) in d["fits"].items()}
    except (ValueError, KeyError, TypeError):
        return {}


def read_ref_tsf(ip, pw):
    """Reference AP's live TSF (hex) from debugfs, or None.

    Any recent reference-clock value keeps offset.json self-consistent (the controller's
    slope term makes the result independent of the exact ref_tsf), but a current one keeps
    the slope extrapolation short, so read the reference AP directly like the controller does.
    """
    raw = _ssh(ip, pw, "cat " + TSF_GLOB).strip()
    try:
        v = int(raw, 16)
    except ValueError:
        return None
    # a wedged card returns 0xfffffffb... (WMI get_tsf error): parses as hex but is nonsense.
    # A real TSF (microseconds) is well under 2^48 (~8.9 years), so reject anything above it
    # rather than anchor the whole graph on garbage.
    return v if 0 < v < (1 << 48) else None


def monitor_edges(fits):
    """AP->AP edges from one monitor's per-AP fits, by eliminating the monitor's clock.

    fits[i] = (ai, bi) means i_tsf = ai + bi*mon. For each unordered pair with bi != 0:
    edge (i, j) = (aj - bj*ai/bi, bj/bi), i.e. j = a + b*i. Airtime-bias-free (see module
    docstring). Only one direction per pair is emitted -- solve_offsets adds the inverse -- so
    the BFS does not depend on dict-iteration order.
    """
    macs = list(fits)
    edges = {}
    for a, i in enumerate(macs):
        ai, bi = fits[i]
        if bi == 0:
            continue
        for j in macs[a + 1:]:
            aj, bj = fits[j]
            edges[(i, j)] = (aj - bj * ai / bi, bj / bi)
    return edges


def main():
    argv = sys.argv[1:]
    interval, out = INTERVAL, OUT_PATH

    def take(flag, cast, cur):
        if flag in argv:
            k = argv.index(flag)
            v = cast(argv[k + 1])
            del argv[k:k + 2]
            return v
        return cur
    interval = take("--interval", float, interval)
    out = take("--out", str, out)
    if not argv:
        sys.exit("usage: monitor_tracker.py <topo.json> [--interval S] [--out PATH]")

    topo = json.load(open(argv[0]))
    pw = topo.get("password", "modwifi")
    nodes = topo["nodes"]
    aps = [(n, nd) for n, nd in nodes.items() if nd.get("role") == "ap"]
    if len(aps) < 2:
        sys.exit("monitor_tracker needs >= 2 APs in topo")
    ref = aps[0][1]["mac"].lower()
    ref_ip = aps[0][1]["ip"]

    monitors = topo.get("monitors") or ([topo["observer"]] if topo.get("observer") else [])
    monitors = [nodes[m] for m in monitors if m in nodes and nodes[m].get("role") == "station"]
    if not monitors:
        sys.exit("monitor_tracker needs topo 'monitors': [station, ...] (or an 'observer')")

    while True:
        edges = {}
        for mon in monitors:
            edges.update(monitor_edges(read_raw_fits(mon["ip"], pw)))
        ref_tsf = read_ref_tsf(ref_ip, pw)
        if edges and ref_tsf is not None:
            maps = solve_offsets(edges, ref)
            if len(maps) > 1:
                write_offsets(out, ref_tsf, maps, ref)
        time.sleep(interval)


if __name__ == "__main__":
    main()
