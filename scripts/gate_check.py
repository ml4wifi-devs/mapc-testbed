#!/usr/bin/env python3
"""Single-AP gate precision: how tightly the frame keys out at the commanded target TSF.

Pairs each frame's monitor radiotap tsft (from a pcap) with its commanded target T
(fire_cosr.sh's /tmp/fire_cosr.log, "seq T") by the seq in the C0 5A id stamp. tsft and
T live in different clock domains (monitor vs AP TSF): a constant offset plus slow drift
(~4.5 us/s). We DETREND (subtract a per-seq linear fit of tsft - T) and flag residual
excursions -- a late fire (e.g. ah_stopTxDma blocking) spikes off the ramp.

Reads the pcap via tshark full-hex (tcpdump -x mis-dissects the C0 5A stamp as LLC).

Usage:  python3 gate_check.py <capture.pcap> <master_mac> [/tmp/fire_cosr.log]
"""
import sys
import re
import subprocess

pcap = sys.argv[1]
MAC = sys.argv[2].lower()
logfile = sys.argv[3] if len(sys.argv) > 3 else "/tmp/fire_cosr.log"


def commanded_targets(logfile):
    """seq -> commanded target TSF, from fire_cosr.sh's log."""
    targets = {}
    for line in open(logfile):
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            targets[int(parts[0])] = int(parts[1])
    return targets


def frame_meta(pcap):
    """frame_number -> (radiotap_mactime, source_mac)."""
    out = subprocess.run(
        ["tshark", "-r", pcap, "-T", "fields",
         "-e", "frame.number", "-e", "radiotap.mactime", "-e", "wlan.sa"],
        capture_output=True, text=True).stdout
    meta = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            meta[int(parts[0])] = (int(parts[1]), parts[2].lower())
    return meta


def frame_hexes(pcap):
    """Yield each frame's contiguous hex (radiotap+mac+body)."""
    dump = subprocess.run(["tshark", "-r", pcap, "-x"],
                          capture_output=True, text=True).stdout
    cur = []
    for line in dump.splitlines():
        m = re.match(r'^[0-9a-fA-F]{4,}\s+((?:[0-9a-fA-F]{2}\s+){1,16})', line)
        if m:
            cur.append(re.sub(r'\s', '', m.group(1)))
        elif cur:
            yield "".join(cur)
            cur = []
    if cur:
        yield "".join(cur)


targets = commanded_targets(logfile)
meta = frame_meta(pcap)
tsft = {}   # seq -> monitor tsft for this AP's frames
for frame_no, hexstr in enumerate(frame_hexes(pcap), 1):
    i = hexstr.find("c05a")
    if i < 0 or frame_no not in meta:
        continue
    mactime, sa = meta[frame_no]
    if sa != MAC:
        continue
    seq = int(hexstr[i + 6:i + 8], 16) | (int(hexstr[i + 8:i + 10], 16) << 8)
    tsft[seq] = mactime

seqs = sorted(set(targets) & set(tsft))
print("commanded=%d captured=%d paired=%d" % (len(targets), len(tsft), len(seqs)))
if len(seqs) < 2:
    sys.exit()

# detrend: fit diff = tsft - T against seq, subtract the linear ramp (clock offset + drift)
diff = [(s, tsft[s] - targets[s]) for s in seqs]
n = len(diff)
mean_x = sum(s for s, _ in diff) / n
mean_y = sum(y for _, y in diff) / n
denom = sum((s - mean_x) ** 2 for s, _ in diff)
slope = sum((s - mean_x) * (y - mean_y) for s, y in diff) / denom if denom else 0
residual = sorted(((s, (y - mean_y) - slope * (s - mean_x)) for s, y in diff),
                  key=lambda r: r[0])

abs_res = sorted(abs(r) for _, r in residual)
print("drift slope=%.2f us/shot; residual median|.|=%.2f max|.|=%.1f us" % (
    slope, abs_res[len(abs_res) // 2], abs_res[-1]))
outliers = [(s, round(r, 1)) for s, r in residual if abs(r) > 50]
print("OUTLIERS >50us (late fires):", outliers if outliers else "NONE  -> gate clean")
print("residuals us:", [(s, round(r)) for s, r in residual])
