#!/usr/bin/env python3
"""Co-SR testbed controller: one call to run a gated Co-SR shot and get the numbers.

Give a spec (which APs transmit, to which station id, at which MCS/txpower, how many
A-MPDU subframes, a shared fire instant + stagger). For each shot, cosr_shot():

  - sets each AP's real TX power once via `iw` (the WMI txpower field is ignored on
    AR9271), and its MCS/frame_len/nframes per-shot in the gated WMI command;
  - reads the master AP's TSF, maps the ONE shared instant into every AP's own TSF via
    the observer's per-AP cross-AP offset (offset_tracker.py -> /tmp/offset.json);
  - fans the gated-TX trigger out to every AP IN PARALLEL (threads), so the frame keys
    out on-chip at the target TSF (VO queue, no backoff/CS).

It returns a dict: per-station received-vs-sent unique subframes + delivery, per-AP
subframe totals, the AP<->monitor RSSI, and the measured cross-AP sync error.

Frames are stamped C0 5A <station_id> <seq_lo> <seq_hi> <subframe_idx>; the monitor
capture is counted by unique (station_id, seq, idx), which is robust under Co-SR
collisions (a lost frame is simply an absent id).

Usage:  python3 cosr_ctl.py spec.json      (spec on argv or stdin; prints result JSON)
"""
import sys
import json
import time
import re
import subprocess
import threading

PW = "modwifi"

# debugfs globs (one AR9271 per VM, so the wildcards resolve uniquely)
TSF_GLOB = "/sys/kernel/debug/ieee80211/*/netdev:*/tsf"
NODE_GLOB = "/sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx"

# WMI wire (little-endian): 17-byte fixed prefix + 24-byte 802.11 header template.
STAMP_OFFSET_DEFAULT = 24
FRAME_LEN_DEFAULT = 200
RATE_DEFAULT = 128           # HAL rateCode 0x80|mcs -> MCS0
# WMI descriptor txpower is in 0.5 dB units and is ignored on AR9271 (real EIRP comes from
# `iw set txpower`, see set_txpower); still send an explicit, regulatory-sane value so the
# firmware's hot default (63 = 31.5 dBm) never fires and the field is right on other chips.
TXP_DEFAULT = 40             # 40 * 0.5 dB = 20 dBm (100 mW)
TXP_MAX = 40

# shot defaults
LEAD_US_DEFAULT = 500000
SHOTS_DEFAULT = 15
GAP_S_DEFAULT = 0.3
OFFSET_REFRESH_EVERY = 10     # re-read /tmp/offset.json every N shots (off the critical path)


# --------------------------------------------------------------------------- ssh

def _ssh_base(ip, multiplex=True):
    """Base ssh argv. Password auth only (pubkey disabled on the VMs).

    multiplex=True keeps a persistent ControlMaster: the controller makes many ssh
    calls per shot, and without multiplexing sshd refuses the rapid reconnects and
    returns empty output (which breaks the TSF reads).
    """
    args = ["sshpass", "-p", PW, "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ConnectTimeout=8"]
    if multiplex:
        args += ["-o", "ControlMaster=auto",
                 "-o", "ControlPath=/tmp/cm_%s" % ip,
                 "-o", "ControlPersist=120"]
    return args


def ssh(ip, cmd):
    """Run a command over the multiplexed connection; return stdout."""
    argv = _ssh_base(ip, multiplex=True) + ["modwifi@" + ip, cmd]
    return subprocess.run(argv, capture_output=True, text=True).stdout


def ssh_fresh(ip, cmd):
    """Run on a dedicated (non-multiplexed) connection.

    Backgrounded (&) jobs launched over a ControlMaster session get killed when the
    channel closes, so a detached tcpdump must go over its own connection.
    """
    argv = _ssh_base(ip, multiplex=False) + ["modwifi@" + ip, cmd]
    return subprocess.run(argv, capture_output=True, text=True).stdout


def read_tsf(ip):
    """Read an AP's 64-bit TSF from debugfs."""
    return int(ssh(ip, "cat " + TSF_GLOB).strip(), 16)


# ------------------------------------------------------------------- WMI wire

def blob(target, rate, txp, nframes, stamp_off, station_id, seq, frame_len, mac):
    """Build the little-endian WMI write payload.

    Layout: [8 target_tsf][1 rate][1 txp][1 nframes][1 stamp_off][1 station_id]
            [2 seq][2 frame_len][24-byte 802.11 header template].
    The frame body is synthesized on-chip (WMI_CMD_MAX_LEN is 100 B), so only the
    header template is shipped here.
    """
    out = bytearray()
    for i in range(8):
        out.append((target >> (8 * i)) & 0xff)
    out += bytes([rate & 0xff, txp & 0xff, nframes & 0xff,
                  stamp_off & 0xff, station_id & 0xff])
    out += bytes([seq & 0xff, (seq >> 8) & 0xff,
                  frame_len & 0xff, (frame_len >> 8) & 0xff])
    mac_bytes = bytes(int(x, 16) for x in mac.split(":"))
    # 802.11 header template: FC+dur, addr1=broadcast, addr2=AP mac, addr3=broadcast, seqctl
    out += bytes([0x08, 0x00, 0x00, 0x00])
    out += bytes([0xff] * 6)          # addr1 (dst) broadcast
    out += mac_bytes                  # addr2 (src) = this AP
    out += bytes([0xff] * 6)          # addr3 (bssid) broadcast
    out += bytes([0x00, 0x00])        # seq ctl
    return bytes(out)


def write_node(ip, node, payload):
    """Trigger a gated shot: printf the escaped bytes into the debugfs node."""
    escaped = "".join("\\x%02x" % b for b in payload)
    ssh(ip, "printf '%s' > %s" % (escaped, node))


def set_txpower(ip, iface, mbm):
    """Set the AP's real TX power via iw (the WMI txpower byte is ignored on AR9271)."""
    ssh(ip, "echo %s | sudo -S iw dev %s set txpower fixed %d 2>/dev/null" % (PW, iface, mbm))


# ---------------------------------------------------- monitor capture + parse

def cap_start(mon_ip, iface, path, secs):
    """Start a detached, data-only tcpdump on the monitor.

    The whole `sudo nohup tcpdump` is backgrounded at the ssh-command level (& then
    echo) -- the pattern that survives the channel close. Notes:
      - no `</dev/null`: it would steal sudo's stdin from the `echo|sudo -S` password
        pipe and make sudo prompt (the pipe closes on its own, so ssh does not hang);
      - no pkill of old captures: `pkill -f tcpdump.*<path>` also matches THIS launcher
        shell's own cmdline and kills the parent before tcpdump starts. Old captures
        self-expire via `timeout`.
    Data-only, so it does not disturb the beacon tracker's own tcpdump on the iface.
    """
    cmd = ("echo %s | sudo -S nohup timeout %d tcpdump -i %s -s0 -w %s type data "
           ">/dev/null 2>&1 & echo started" % (PW, secs, iface, path))
    ssh_fresh(mon_ip, cmd)


def cap_pull(mon_ip, remote_path, local_path):
    """scp the capture back to the controller."""
    argv = ["sshpass", "-p", PW, "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "modwifi@" + mon_ip + ":" + remote_path, local_path]
    subprocess.run(argv, capture_output=True)


def _frame_hexes(pcap):
    """Yield each frame's contiguous hex (radiotap+mac+body) from tshark -x.

    tcpdump -x mis-dissects the C0 5A stamp as an LLC header and drops it, so the raw
    hex must come from tshark.
    """
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


def parse_frames(pcap):
    """Return a list of {sa, tsft, station_id, seq, idx} for every stamped frame."""
    # per-frame radiotap mactime + source mac, keyed by frame number
    fields = subprocess.run(
        ["tshark", "-r", pcap, "-T", "fields",
         "-e", "frame.number", "-e", "radiotap.mactime", "-e", "wlan.sa"],
        capture_output=True, text=True).stdout
    meta = {}
    for line in fields.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            meta[int(parts[0])] = (int(parts[1]), parts[2].lower())

    frames = []
    for frame_no, hexstr in enumerate(_frame_hexes(pcap), 1):
        i = hexstr.find("c05a")
        if i < 0 or frame_no not in meta:
            continue
        tsft, sa = meta[frame_no]
        frames.append({
            "sa": sa,
            "tsft": tsft,
            "station_id": int(hexstr[i + 4:i + 6], 16),
            "seq": int(hexstr[i + 6:i + 8], 16) | (int(hexstr[i + 8:i + 10], 16) << 8),
            "idx": int(hexstr[i + 10:i + 12], 16),
        })
    return frames


def _beacon_signal(text, macs):
    """Mean radiotap signal (dBm) per SA MAC in `macs`, from tcpdump -e beacon lines."""
    acc = {}
    for line in text.splitlines():
        sig = re.search(r'(-?\d+)dB signal', line)
        sa = re.search(r'SA:([0-9a-fA-F:]{17})', line)
        if sig and sa:
            entry = acc.setdefault(sa.group(1).lower(), [0.0, 0])
            entry[0] += float(sig.group(1))
            entry[1] += 1
    return {m: round(acc[m][0] / acc[m][1], 1) for m in macs if m in acc}


def rssi_at_monitor(mon_ip, iface, macs, secs=4):
    """Mean radiotap signal (dBm) per AP MAC, from beacons seen at the monitor."""
    text = ssh(mon_ip,
               "echo %s | sudo -S timeout %d tcpdump -i %s -e -n type mgt subtype beacon "
               "2>/dev/null" % (PW, secs, iface))
    return _beacon_signal(text, macs)


def rssi_ap_to_ap(aps, secs=6):
    """RSSI matrix {ap_mac: {peer_mac: dbm}}: each AP hears its neighbours' beacons via a
    `mon0` monitor VIF added on its own radio (ath9k_htc runs AP + monitor VIFs at once).
    The VIF shares the AP's channel, so no channel set is needed; removed after capture."""
    matrix = {}
    for ap in aps:
        peers = [a["mac"] for a in aps if a["mac"] != ap["mac"]]
        if not peers:
            continue
        ssh(ap["ip"],
            "echo %s | sudo -S iw dev mon0 del 2>/dev/null; "
            "echo %s | sudo -S iw dev %s interface add mon0 type monitor 2>/dev/null; "
            "echo %s | sudo -S ip link set mon0 up 2>/dev/null" % (PW, PW, ap["iface"], PW))
        text = ssh(ap["ip"],
                   "echo %s | sudo -S timeout %d tcpdump -i mon0 -e -n type mgt subtype beacon "
                   "2>/dev/null" % (PW, secs))
        matrix[ap["mac"]] = _beacon_signal(text, peers)
        ssh(ap["ip"], "echo %s | sudo -S iw dev mon0 del 2>/dev/null" % PW)
    return matrix


# ---------------------------------------------------------------- the one call

def _read_offsets(mon_ip):
    """Read (ref_tsf, {mac: (offset, slope)}) from the tracker's JSON /tmp/offset.json.

    Retries a few times: a transient empty read would give no offsets, which puts a
    slave target in the reference clock and fires TOOFAR. The reference AP (aps[0]) is
    present with (0, 0.0) -> it maps to itself.
    """
    for _ in range(5):
        raw = ssh(mon_ip, "cat /tmp/offset.json").strip()
        try:
            d = json.loads(raw)
            offs = {m.lower(): (int(o), float(s)) for m, (o, s) in d["offsets"].items()}
            if offs:
                return int(d["ref_tsf"]), offs
        except (ValueError, KeyError, TypeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("offset.json empty/unparsable on %s -- is offset_tracker.py running "
                       "and hearing every AP's beacons?" % mon_ip)


def _map_target(shared_instant, offset, slope, offset_ts, stagger, ap_index):
    """Express the one shared instant (reference clock) in an AP's own TSF."""
    return int(round(shared_instant
                     + offset
                     + slope * (shared_instant - offset_ts)
                     + stagger * ap_index))


def _config_aps(aps):
    """Config phase (once per run): resolve each AP's debugfs node + set real TX power."""
    for ap in aps:
        ap["node"] = ssh(ap["ip"], "ls " + NODE_GLOB).strip()
        if "txpower_mbm" in ap:
            set_txpower(ap["ip"], ap["iface"], ap["txpower_mbm"])


def _do_shots(aps, master_ip, offset_mon_ip, lead, stagger, shots, gap):
    """The timing-critical fire loop, shared by every capture topology.

    Reads per-AP (offset, slope) from the observer's tracker, maps the one shared instant
    (reference = aps[0]) into each AP's own TSF, and fans the gated trigger out in parallel.
    This is the proven microsecond path -- kept as a single implementation so no caller can
    perturb it. Captures are started by the caller before this runs.
    """
    master = aps[0]
    offset_ts, offsets = _read_offsets(offset_mon_ip)
    for shot in range(shots):
        if shot % OFFSET_REFRESH_EVERY == 0:
            offset_ts, offsets = _read_offsets(offset_mon_ip)

        shared_instant = read_tsf(master_ip) + lead
        threads = []
        for ap_index, ap in enumerate(aps):
            mac = ap["mac"].lower()
            if ap_index == 0:
                offset, slope = 0, 0.0          # reference AP maps to itself
            elif mac in offsets:
                offset, slope = offsets[mac]
            else:
                # the observer never heard this AP's beacon -> we have no clock relation
                # for it. Defaulting to (0,0) would silently fire it in the reference clock
                # (garbage target); refuse instead so the miss is visible, not chased later.
                raise RuntimeError("no TSF offset for AP %s (%s) -- the observer is not "
                                   "hearing its beacons; check the tracker macs/range"
                                   % (ap.get("name", "?"), mac))
            target = _map_target(shared_instant, offset, slope, offset_ts,
                                 stagger, ap_index)
            payload = blob(target,
                           ap.get("rate", RATE_DEFAULT),
                           min(ap.get("txpower", TXP_DEFAULT), TXP_MAX),
                           ap.get("nframes", 1),
                           ap.get("stamp_off", STAMP_OFFSET_DEFAULT),
                           ap["station_id"],
                           shot,
                           ap.get("frame_len", FRAME_LEN_DEFAULT),
                           ap["mac"])
            t = threading.Thread(target=write_node, args=(ap["ip"], ap["node"], payload))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        time.sleep(gap)


def _cap_secs(shots, gap, lead):
    """Capture window that outlasts the whole fire loop (ssh latency + gap + LEAD tail)."""
    return int(shots * (gap + 1.2)) + int(lead / 1e6) + 8


def cosr_shot(spec):
    mon = spec["monitor"]
    aps = spec["aps"]
    lead = spec.get("lead_us", LEAD_US_DEFAULT)
    stagger = spec.get("stagger_us", 0)
    shots = spec.get("shots", SHOTS_DEFAULT)
    gap = spec.get("gap_s", GAP_S_DEFAULT)
    master = aps[0]                       # aps[0] is the timing reference

    _config_aps(aps)

    pcap_remote = "/tmp/cosr_shot.pcap"
    cap_start(mon["ip"], mon["iface"], pcap_remote, _cap_secs(shots, gap, lead))
    time.sleep(1)

    _do_shots(aps, master["ip"], mon["ip"], lead, stagger, shots, gap)

    time.sleep(lead / 1e6 + 4)            # let the last frame key out + tcpdump flush
    local = "/tmp/cosr_shot.pcap"
    cap_pull(mon["ip"], pcap_remote, local)
    return _summarize(spec, parse_frames(local))


def _summarize(spec, records):
    """Build the result dict from parsed frames."""
    aps = spec["aps"]
    mon = spec["monitor"]
    stagger = spec.get("stagger_us", 0)
    shots = spec.get("shots", SHOTS_DEFAULT)

    # per-AP unique subframes; duplicates counted separately
    unique_by_ap = {}
    seen = set()
    dups = 0
    for r in records:
        key = (r["sa"], r["station_id"], r["seq"], r["idx"])
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        unique_by_ap[r["sa"]] = unique_by_ap.get(r["sa"], 0) + 1

    per_ap = {}
    for ap in aps:
        sent = shots * max(1, ap.get("nframes", 1))
        got = unique_by_ap.get(ap["mac"], 0)
        per_ap[ap["name"]] = {
            "mac": ap["mac"],
            "station_id": ap["station_id"],
            "subframes_sent": sent,
            "subframes_rx": got,
            "delivery": round(got / sent, 3) if sent else 0,
        }

    # cross-AP sync error (only meaningful when frames are separated enough to both decode)
    sync = None
    if len(aps) == 2:
        # Pair on the first subframe (idx 0) only: it carries the valid PHY-RXSTART timestamp
        # of the PPDU. A later A-MPDU subframe's radiotap mactime is a monitor artifact (garbage)
        # and would pollute the sync error when nframes>1 (idx is always 0 for a single frame).
        m_tsft = {r["seq"]: r["tsft"] for r in records if r["sa"] == aps[0]["mac"] and r["idx"] == 0}
        s_tsft = {r["seq"]: r["tsft"] for r in records if r["sa"] == aps[1]["mac"] and r["idx"] == 0}
        common = sorted(set(m_tsft) & set(s_tsft))
        if common:
            err = sorted(abs((s_tsft[c] - m_tsft[c]) - stagger) for c in common)
            sync = {"paired": len(common),
                    "median_us": err[len(err) // 2],
                    "max_us": err[-1]}

    rssi = rssi_at_monitor(mon["ip"], mon["iface"], [ap["mac"] for ap in aps])
    result = {"per_ap": per_ap, "dups": dups, "sync_error": sync,
              "rssi_at_monitor_dbm": rssi, "shots": shots, "stagger_us": stagger}
    # AP<->AP RSSI needs a transient mon0 VIF on each AP radio -- off by default because a
    # monitor VIF on an AP rebases that phy's TSF (README note 1); safe here only because it
    # runs post-fire and is removed before the next shot (verified: gate stays clean). Opt in
    # with "ap_rssi": true in the spec when you want the matrix.
    if len(aps) >= 2 and spec.get("ap_rssi"):
        result["rssi_ap_to_ap_dbm"] = rssi_ap_to_ap(aps)
    return result


def observed_mcs(pcap, macs):
    """Per SA MAC in `macs`: (most-common radiotap MCS index or None, captured-frame count).

    A commanded HT rate that the AP's rate table does not carry falls back to the min
    (legacy) rate *silently* in firmware -- a legacy frame has no radiotap MCS field, so a
    (None, count>0) result means "frames on air but not HT" = a downgrade to catch.
    """
    from collections import Counter
    try:
        out = subprocess.run(
            ["tshark", "-r", pcap, "-Y", "wlan.fc.type==2", "-T", "fields",
             "-e", "wlan.sa", "-e", "radiotap.mcs.index"],
            capture_output=True, text=True).stdout
    except Exception:
        return {m: (None, 0) for m in macs}
    macs = [m.lower() for m in macs]
    idxs = {m: Counter() for m in macs}
    cnt = {m: 0 for m in macs}
    for line in out.splitlines():
        f = line.split("\t")
        if not f:
            continue
        sa = f[0].strip().lower()
        if sa not in idxs:
            continue
        cnt[sa] += 1
        v = f[1].strip() if len(f) > 1 else ""
        if v.isdigit():
            idxs[sa][int(v)] += 1
    return {m: (idxs[m].most_common(1)[0][0] if idxs[m] else None, cnt[m]) for m in macs}


def _station_name(sta):
    """Display name for a station node (explicit `name`, else ip:iface)."""
    return sta.get("name") or "%s:%s" % (sta["ip"], sta["iface"])


def _collect_stations(aps):
    """Map the per-AP `stations` lists to distinct capture nodes.

    Returns (nodes, ap_stations):
      nodes        {node_key: {"ip", "iface"}}          node_key = (ip, iface), captured once
      ap_stations  {ap_name: [(station_name, node_key), ...]}
    A station is a monitor-mode card that samples its AP's broadcast stream at its location;
    two APs may list the same physical card (shared node_key) -- it is captured once.
    """
    nodes = {}
    ap_stations = {}
    for ap in aps:
        lst = []
        for sta in ap.get("stations", []):
            key = (sta["ip"], sta["iface"])
            nodes.setdefault(key, {"ip": sta["ip"], "iface": sta["iface"]})
            lst.append((_station_name(sta), key))
        ap_stations[ap["name"]] = lst
    return nodes, ap_stations


def _best_effort_sync(frames_by_node, aps, stagger):
    """Cross-AP sync error from any one receiver that heard >= 2 APs (idx-0 frames).

    Optional/diagnostic: pairs each non-reference AP against the reference on matching seq,
    using the first-subframe radiotap mactime (a later A-MPDU subframe's mactime is a monitor
    artifact). Returns None if no single node heard two APs.
    """
    ref = aps[0]["mac"].lower()
    for frames in frames_by_node.values():
        by_ap = {}
        for r in frames:
            if r["idx"] == 0:
                by_ap.setdefault(r["sa"], {})[r["seq"]] = r["tsft"]
        if ref not in by_ap or len(by_ap) < 2:
            continue
        out = {}
        for ap in aps[1:]:
            mac = ap["mac"].lower()
            if mac not in by_ap:
                continue
            common = sorted(set(by_ap[ref]) & set(by_ap[mac]))
            if common:
                err = sorted(abs((by_ap[mac][c] - by_ap[ref][c]) - stagger) for c in common)
                out[ap["name"]] = {"paired": len(common),
                                   "median_us": err[len(err) // 2], "max_us": err[-1]}
        if out:
            return out
    return None


def measure_multi(spec):
    """Config -> per-(AP, station) measurement for ANY number of APs, with a variable
    number of stations per AP.

    Each AP transmits ONE gated broadcast stream (stamped with its `station_id`); the
    `stations` under an AP are monitor-mode receiver cards that each sample that stream at
    their own location. For every (AP, station) it reports delivery + on-air MCS + the
    AP->station beacon RSSI. The offset observer (spec["monitor"], or the first station if
    omitted) supplies the cross-AP TSF offsets and need not be a dedicated card.

      { "success_prob": { <ap>: { <station>: {delivery, rx, sent, mcs_cmd, mcs_seen} } },
        "rssi_ap_to_sta_dbm": { <ap>: { <station>: dbm } },   # AP -> station (beacon)
        "rssi_ap_to_ap_dbm":  { <ap mac>: { <peer>: dbm } },  # AP <-> AP interference
        "sync_error": {...} | null,                           # best-effort, matched types
        "warnings": [ ... ] }
    """
    import copy
    spec = copy.deepcopy(spec)
    for ap in spec.get("aps", []):
        if "mcs" in ap:
            ap["rate"] = 0x80 | (ap["mcs"] & 0x7f)

    aps = spec["aps"]
    master = aps[0]
    lead = spec.get("lead_us", LEAD_US_DEFAULT)
    stagger = spec.get("stagger_us", 0)
    shots = spec.get("shots", SHOTS_DEFAULT)
    gap = spec.get("gap_s", GAP_S_DEFAULT)

    nodes, ap_stations = _collect_stations(aps)
    if not nodes:
        raise RuntimeError("measure_multi: no stations listed on any AP")

    # offset observer: an explicit monitor, else the first station card (any node that
    # hears every AP's beacons works -- no dedicated clock monitor required).
    observer = spec.get("monitor")
    if not observer:
        first_key = next(iter(nodes))
        observer = {"ip": nodes[first_key]["ip"], "iface": nodes[first_key]["iface"]}

    _config_aps(aps)

    # start a data capture on every distinct station node, then fire, then pull each back
    keys = list(nodes)
    remote = {k: "/tmp/cosr_sta_%d.pcap" % i for i, k in enumerate(keys)}
    secs = _cap_secs(shots, gap, lead)
    for k in keys:
        cap_start(nodes[k]["ip"], nodes[k]["iface"], remote[k], secs)
    time.sleep(1)

    _do_shots(aps, master["ip"], observer["ip"], lead, stagger, shots, gap)

    time.sleep(lead / 1e6 + 4)
    frames_by_node, mcs_by_node = {}, {}
    macs = [ap["mac"] for ap in aps]
    for i, k in enumerate(keys):
        local = "/tmp/cosr_sta_%d.pcap" % i
        cap_pull(nodes[k]["ip"], remote[k], local)
        frames_by_node[k] = parse_frames(local)
        mcs_by_node[k] = observed_mcs(local, macs)

    # AP->station beacon RSSI: one beacon sniff per node, covering the APs that target it
    node_macs = {k: [] for k in keys}
    for ap in aps:
        for _name, k in ap_stations[ap["name"]]:
            if ap["mac"] not in node_macs[k]:
                node_macs[k].append(ap["mac"])
    rssi_by_node = {k: rssi_at_monitor(nodes[k]["ip"], nodes[k]["iface"], node_macs[k])
                    for k in keys if node_macs[k]}

    # build the per-(AP, station) matrices
    success, rssi_a2s, warnings = {}, {}, []
    for ap in aps:
        name, mac = ap["name"], ap["mac"].lower()
        sent = shots * max(1, ap.get("nframes", 1))
        success[name], rssi_a2s[name] = {}, {}
        for sta_name, k in ap_stations[ap["name"]]:
            seen = set()
            for r in frames_by_node[k]:
                if r["sa"] == mac:
                    seen.add((r["seq"], r["idx"]))
            rx = len(seen)
            entry = {"station_id": ap["station_id"], "delivery": round(rx / sent, 3) if sent else 0,
                     "rx": rx, "sent": sent}
            if "mcs" in ap:
                seen_mcs, nseen = mcs_by_node[k].get(mac, (None, 0))
                entry["mcs_cmd"] = ap["mcs"]
                entry["mcs_seen"] = seen_mcs
                if seen_mcs is not None and seen_mcs != ap["mcs"]:
                    warnings.append("%s@%s: commanded MCS %d but on-air MCS %d (rate downgraded)"
                                    % (name, sta_name, ap["mcs"], seen_mcs))
                elif seen_mcs is None and nseen > 0:
                    warnings.append("%s@%s: on-air frames are non-HT/legacy -- commanded MCS %d "
                                    "not honored" % (name, sta_name, ap["mcs"]))
            success[name][sta_name] = entry
            if k in rssi_by_node and ap["mac"] in rssi_by_node[k]:
                rssi_a2s[name][sta_name] = rssi_by_node[k][ap["mac"]]

    out = {"success_prob": success,
           "rssi_ap_to_sta_dbm": rssi_a2s,
           "rssi_ap_to_ap_dbm": rssi_ap_to_ap(aps) if len(aps) >= 2 and spec.get("ap_rssi", True) else {},
           "sync_error": _best_effort_sync(frames_by_node, aps, stagger)}
    if warnings:
        out["warnings"] = warnings
    return out


def measure(spec):
    """Config -> measurement, for driving a coordination model in a loop.

    Input is a `cosr_shot` spec (see USAGE), with two conveniences for model use:
      - each AP may give `mcs` (0..7) instead of the raw `rate`; converted to HAL 0x80|mcs.
      - the AP<->AP RSSI matrix is always included (ap_rssi defaults on here).
    Each AP is one link (AP -> its `station_id`). Returns only what a model needs:

      { "success_prob": { <ap name>: {station_id, delivery, rx, sent,
                                      mcs_cmd, mcs_seen} },      # mcs_* present when `mcs` given
        "rssi_at_monitor_dbm": { <ap mac>: dbm },        # AP -> receiver (monitor)
        "rssi_ap_to_ap_dbm":   { <ap mac>: { <peer>: dbm } },   # AP <-> AP interference
        "sync_error": {...},                             # timing sanity, matched types only
        "warnings": [ ... ] }                            # present only if something needs attention

    `delivery` is measured at the monitor and is receiver-limited (whole-PPDU capture loss);
    for a channel/collision success probability, normalize against an isolated-AP baseline run,
    or use a receiver at each station. `warnings` flags a commanded MCS that did not go out on
    air (silent rate downgrade). Call once per Co-SR configuration; the rig stays up between calls.

    If any AP carries a `stations` list, this dispatches to measure_multi() -- per-(AP,
    station) matrices for any number of APs and a variable station count per AP.
    """
    if any(ap.get("stations") for ap in spec.get("aps", [])):
        return measure_multi(spec)

    import copy
    spec = copy.deepcopy(spec)                     # don't mutate the caller's config
    for ap in spec.get("aps", []):
        if "mcs" in ap:
            ap["rate"] = 0x80 | (ap["mcs"] & 0x7f)
    spec.setdefault("ap_rssi", True)
    r = cosr_shot(spec)

    # verify the commanded MCS actually went out (an unavailable HT rate downgrades silently
    # to a legacy rate in firmware -- see observed_mcs). cosr_shot leaves the capture here.
    obs = observed_mcs("/tmp/cosr_shot.pcap", [ap["mac"] for ap in spec["aps"]])

    success = {}
    warnings = []
    for ap in spec["aps"]:
        pa = r["per_ap"][ap["name"]]
        entry = {"station_id": pa["station_id"], "delivery": pa["delivery"],
                 "rx": pa["subframes_rx"], "sent": pa["subframes_sent"]}
        if "mcs" in ap:
            seen_mcs, nframes_seen = obs.get(ap["mac"].lower(), (None, 0))
            entry["mcs_cmd"] = ap["mcs"]
            entry["mcs_seen"] = seen_mcs
            if seen_mcs is not None and seen_mcs != ap["mcs"]:
                warnings.append("%s: commanded MCS %d but on-air MCS %d (rate downgraded)"
                                % (ap["name"], ap["mcs"], seen_mcs))
            elif seen_mcs is None and nframes_seen > 0:
                warnings.append("%s: on-air frames are non-HT/legacy -- commanded MCS %d not "
                                "honored (rate table lacks it? try an 11ng/HT AP)" % (ap["name"], ap["mcs"]))
            elif nframes_seen == 0:
                warnings.append("%s: MCS unverified -- no frames captured for this AP" % ap["name"])
        success[ap["name"]] = entry

    out = {"success_prob": success,
           "rssi_at_monitor_dbm": r["rssi_at_monitor_dbm"],
           "rssi_ap_to_ap_dbm": r.get("rssi_ap_to_ap_dbm", {}),
           "sync_error": r["sync_error"]}
    if warnings:
        out["warnings"] = warnings
    return out


if __name__ == "__main__":
    # `cosr_ctl.py measure spec.json` -> trimmed model input (success prob + RSSI);
    # `cosr_ctl.py spec.json`         -> full result dict.
    if len(sys.argv) > 1 and sys.argv[1] == "measure":
        spec = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else json.load(sys.stdin)
        print(json.dumps(measure(spec), indent=2))
    else:
        spec = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
        print(json.dumps(cosr_shot(spec), indent=2))
