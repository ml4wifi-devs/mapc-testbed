#!/usr/bin/env python3
"""Two-AP Co-SR sync error from a single monitor capture.

One monitor = one clock, so there is no cross-clock drift to correct. Reads the pcap via
tshark full-hex (tcpdump -x mis-dissects the C0 5A stamp as an LLC header and drops it),
finds each frame's 6-byte id stamp C0 5A <station_id> <seq_lo> <seq_hi> <idx> and its
radiotap MAC timestamp, pairs the two APs by seq, and reports

    sync_error = (tsft_slave - tsft_master) - stagger

Usage:  python3 skew.py <capture.pcap> <master_mac> <slave_mac> [stagger_us]
"""
import sys
import re
import subprocess

pcap = sys.argv[1]
MAC_MASTER = sys.argv[2].lower()
MAC_SLAVE = sys.argv[3].lower()
STAGGER = int(sys.argv[4]) if len(sys.argv) > 4 else 0


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


meta = frame_meta(pcap)
tsft_master = {}   # seq -> tsft
tsft_slave = {}
for frame_no, hexstr in enumerate(frame_hexes(pcap), 1):
    i = hexstr.find("c05a")
    if i < 0 or frame_no not in meta:
        continue
    seq = int(hexstr[i + 6:i + 8], 16) | (int(hexstr[i + 8:i + 10], 16) << 8)
    mactime, sa = meta[frame_no]
    if sa == MAC_MASTER:
        tsft_master[seq] = mactime
    elif sa == MAC_SLAVE:
        tsft_slave[seq] = mactime

common = sorted(set(tsft_master) & set(tsft_slave))
print("master=%d slave=%d paired=%d" % (len(tsft_master), len(tsft_slave), len(common)))
if not common:
    sys.exit()

errors = [(tsft_slave[s] - tsft_master[s]) - STAGGER for s in common]
abs_err = sorted(abs(e) for e in errors)
n = len(abs_err)
mean = sum(errors) / n
std = (sum((e - mean) ** 2 for e in errors) / n) ** 0.5
measured = sum(tsft_slave[s] - tsft_master[s] for s in common) / n

print("intended stagger=%dus; measured (S-M) mean=%.1f us" % (STAGGER, measured))
print("SYNC ERROR us: median|e|=%.2f mean|e|=%.2f max|e|=%.1f std=%.2f" % (
    abs_err[n // 2], sum(abs_err) / n, abs_err[-1], std))
within = lambda t: sum(1 for x in abs_err if x < t)
print("within 1us:%d 4us:%d 10us:%d (of %d)" % (within(1), within(4), within(10), n))
print("per-seq sync_err us:", [(s, (tsft_slave[s] - tsft_master[s]) - STAGGER) for s in common])
