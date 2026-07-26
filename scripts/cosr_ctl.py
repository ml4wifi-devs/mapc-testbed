#!/usr/bin/env python3
"""Co-SR coordination controller.

Drives a coordinated-spatial-reuse (Co-SR) testbed of AR9271 APs: fires one
firmware-gated, microsecond-aligned transmission from each participating AP and
measures what each station receives. Four commands, one shared timing core:

    shot       fire a coordinated Co-SR transmission; report per-link delivery.
    measure    fire + full model inputs: per-link success + RSSI matrices.
    test-sync  fire staggered shots; report per-AP cross-AP sync (bias/jitter/p90).
    gate       single-AP absolute gate precision (the timing diagnostic).

Two input files (see examples/topo_monitor.json / examples/shot.json):

    topo.json   the testbed -- named nodes (role/ip/iface/mac/ssid/station_id), the
                shared channel, and the cross-AP clock source (`sync`): "monitor" (one
                observer station hears every AP) or "beacon" (APs hear each other, the
                tracker runs on the host).
    shot.json   the experiment -- which APs transmit and to which station each one
                targets (`links`), the common A-MPDU shape, MCS/power, and timing.

A link "apA -> sta1" means apA transmits a gated stream stamped with sta1's
`station_id`; delivery for that link is counted at sta1's own receiver, for frames
whose (source AP, station_id) match. That is what makes station ids meaningful:
different APs can target different stations in the same shot, and each station's
number is its own -- not a shared copy of one broadcast.

Delivery is honest about the denominator: each shot's firmware status is read back
from the AP's kernel log, so the denominator is shots that *actually fired*, not
shots commanded (a LATE/TOOFAR gate miss is reported, never counted as a loss).

Usage:
    cosr_ctl.py shot       topo.json shot.json
    cosr_ctl.py measure    topo.json shot.json
    cosr_ctl.py test-sync  topo.json shot.json
    cosr_ctl.py gate       topo.json shot.json
"""
import sys
import os
import json
import time
import re
import shutil
import subprocess
import threading

# --------------------------------------------------------------------------- defaults

STAMP_OFFSET = 24            # byte offset of the C0 5A id stamp inside each subframe
TXP_DESC_DEFAULT = 40        # WMI descriptor txpower byte (0.5 dB units); real EIRP via iw
LEAD_US_DEFAULT = 150000     # fire this far ahead of the shared instant (covers ssh fan-out)
SHOTS_DEFAULT = 20
GAP_S_DEFAULT = 0.15
FRAME_LEN_DEFAULT = 200
NFRAMES_DEFAULT = 1
OFFSET_REFRESH_EVERY = 1     # re-read the cross-AP offset every shot: the read sits BEFORE the
                             # per-shot TSF read (never between it and the fan-out), so it costs
                             # one cheap multiplexed ssh and keeps offset_ts current -- a stale
                             # offset only grows slope*(now-offset_ts) extrapolation error.

# debugfs globs (one AR9271 per VM, so the wildcards resolve uniquely)
TSF_GLOB = "/sys/kernel/debug/ieee80211/*/netdev:*/tsf"
NODE_GLOB = "/sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx"

_PW = "modwifi"              # set from topo["password"] at load time


# --------------------------------------------------------------------------- ssh

def _ssh_base(ip, multiplex=True):
    """Base ssh argv. Password auth only (pubkey disabled on the VM image).

    multiplex keeps a persistent ControlMaster: the controller makes many ssh calls
    per shot and, without multiplexing, sshd refuses the rapid reconnects and returns
    empty output (which would break the TSF reads).
    """
    args = ["sshpass", "-p", _PW, "ssh",
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
    raw = ssh(ip, "cat " + TSF_GLOB).strip()
    try:
        v = int(raw, 16)
    except ValueError:
        raise RuntimeError("no/ambiguous TSF debugfs node on %s (got %r) -- is it in AP mode "
                           "with one card and debugfs open (run.sh up)?" % (ip, raw[:40]))
    # a wedged card returns 0xfffffffb... (WMI get_tsf error): valid hex but nonsense. A real
    # TSF (us) is < 2^48, so firing off this would put every target absurdly far -> refuse it.
    if not (0 < v < (1 << 48)):
        raise RuntimeError("implausible TSF 0x%x on %s -- the card's WMI clock is wedged "
                           "(re-enumerate the dongle); refusing to fire off a garbage clock" % (v, ip))
    return v


# --------------------------------------------------------------------------- WMI wire

def blob(target, rate, txp, nframes, stamp_off, station_id, seq, frame_len, mac):
    """Build the little-endian WMI gated-TX payload.

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
    """Set the AP's real TX power via iw (see docs: the WMI descriptor byte is clamped
    to the calibrated power table on AR9271, so real EIRP is set here)."""
    ssh(ip, "echo %s | sudo -S iw dev %s set txpower fixed %d 2>/dev/null" % (_PW, iface, mbm))


# --------------------------------------------------------- fire-status accounting

_STATUS_NAME = {0: "fired", 1: "nobf", 2: "late", 3: "toofar"}


def status_clear(ip):
    """Clear the AP's kernel log so the next run's fire-status lines stand alone."""
    ssh(ip, "echo %s | sudo -S dmesg -C" % _PW)


def status_counts(ip):
    """Count firmware fire outcomes from the AP's kernel log.

    The driver printks `cosr_gated_tx: status=N ...` once per gated write (driver.diff),
    N per COSR_GATED_TX_STATUS: 0 fired, 1 no-buf, 2 late, 3 too-far. Returns a dict
    {fired, late, toofar, nobf}; `fired` is the honest delivery denominator.
    """
    text = ssh(ip, "echo %s | sudo -S dmesg 2>/dev/null | grep 'cosr_gated_tx: status='" % _PW)
    counts = {"fired": 0, "late": 0, "toofar": 0, "nobf": 0}
    for m in re.finditer(r"cosr_gated_tx: status=(\d+)", text):
        name = _STATUS_NAME.get(int(m.group(1)))
        if name:
            counts[name] += 1
    return counts


# --------------------------------------------------------- monitor capture + parse


def cap_start(ip, iface, path, secs):
    """Start a detached tcpdump on a receiver card: our stamped data frames.

    The whole `sudo nohup tcpdump` is backgrounded at the ssh-command level (& then
    echo) -- the pattern that survives the channel close.
    """
    # rm the old capture first (root-owned from the previous run): if this tcpdump fails
    # to start, cap_pull must find nothing, never a stale pcap that inflates delivery.
    cmd = ("echo %s | sudo -S rm -f %s; "
           "echo %s | sudo -S nohup timeout %d tcpdump -i %s -s0 -w %s "
           "'type data' >/dev/null 2>&1 & echo started"
           % (_PW, path, _PW, secs, iface, path))
    ssh_fresh(ip, cmd)


def cap_pull(ip, remote_path, local_path):
    """scp a capture back to the controller."""
    argv = ["sshpass", "-p", _PW, "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "modwifi@" + ip + ":" + remote_path, local_path]
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
    """Return {sa, tsft, signal, station_id, seq, idx} for every stamped frame.

    `signal` is the per-frame radiotap RSSI (dBm) of the stamped DATA frame itself --
    real AP->receiver power, not a beacon proxy -- or None if radiotap carries none.
    """
    fields = subprocess.run(
        ["tshark", "-r", pcap, "-T", "fields",
         "-e", "frame.number", "-e", "radiotap.mactime",
         "-e", "wlan.sa", "-e", "radiotap.dbm_antsignal"],
        capture_output=True, text=True).stdout
    meta = {}
    for line in fields.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            sig = None
            if len(parts) >= 4 and parts[3]:
                # multi-antenna frames report a comma/space list; take the first
                first = re.split(r'[ ,]', parts[3].strip())[0]
                try:
                    sig = int(first)
                except ValueError:
                    sig = None
            meta[int(parts[0])] = (int(parts[1]), parts[2].lower(), sig)

    frames = []
    for frame_no, hexstr in enumerate(_frame_hexes(pcap), 1):
        i = hexstr.find("c05a")
        # need the full 6-byte stamp (12 hex chars) after the marker; a c05a that matches within
        # the last 6 bytes of a frame would otherwise slice past the end -> int('',16) crash.
        if i < 0 or i + 12 > len(hexstr) or frame_no not in meta:
            continue
        tsft, sa, sig = meta[frame_no]
        frames.append({
            "sa": sa,
            "tsft": tsft,
            "signal": sig,
            "station_id": int(hexstr[i + 4:i + 6], 16),
            "seq": int(hexstr[i + 6:i + 8], 16) | (int(hexstr[i + 8:i + 10], 16) << 8),
            "idx": int(hexstr[i + 10:i + 12], 16),
        })
    return frames


def observed_mcs(pcap, macs):
    """Per SA MAC: (most-common radiotap MCS index or None, captured-frame count).

    A commanded HT rate the AP's rate table does not carry falls back to the min
    (legacy) rate silently in firmware; a legacy frame has no radiotap MCS field, so a
    (None, count>0) result means "frames on air but not HT" -- a downgrade to catch.
    """
    from collections import Counter
    out = subprocess.run(
        ["tshark", "-r", pcap, "-Y", "wlan.fc.type==2", "-T", "fields",
         "-e", "wlan.sa", "-e", "radiotap.mcs.index"],
        capture_output=True, text=True).stdout
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


def rssi_ap_to_ap(aps):
    """RSSI matrix {ap_mac: {peer_mac: dbm}} -- each AP's view of its neighbours.

    Each AP hears its peers' beacons via a transient `mon0` monitor VIF on its own
    radio (ath9k_htc runs AP + monitor VIFs at once). The VIF shares the AP's channel;
    it is removed after the sniff. Off the hot path (post-fire), so the momentary TSF
    rebase a monitor VIF causes on an AP cannot perturb a gated shot.
    """
    matrix = {}
    macs = [ap["mac"] for ap in aps]
    for ap in aps:
        peers = [m for m in macs if m != ap["mac"]]
        if not peers:
            continue
        ssh(ap["ip"],
            "echo %s | sudo -S iw dev mon0 del 2>/dev/null; "
            "echo %s | sudo -S iw dev %s interface add mon0 type monitor 2>/dev/null; "
            "echo %s | sudo -S ip link set mon0 up 2>/dev/null" % (_PW, _PW, ap["iface"], _PW))
        text = ssh(ap["ip"],
                   "echo %s | sudo -S timeout 6 tcpdump -i mon0 -e -n type mgt subtype beacon "
                   "2>/dev/null" % _PW)
        matrix[ap["mac"]] = _beacon_signal(text, peers)
        ssh(ap["ip"], "echo %s | sudo -S iw dev mon0 del 2>/dev/null" % _PW)
    return matrix


# --------------------------------------------------------- cross-AP timing core

def _read_offsets(clock_ip):
    """Read (ref_tsf, {mac: (offset, slope)}) from the tracker's /tmp/offset.json.

    clock_ip None => beacon sync mode: beacon_tracker.py runs on this control host and
    writes the file locally, so read it here. Otherwise monitor mode: read it from the
    observer over ssh. Either source has the identical JSON shape.

    Retries a few times: a transient empty read would leave a non-reference AP without
    a clock relation and fire it into the wrong TSF. The reference AP is present as
    (0, 0.0) and maps to itself.
    """
    for _ in range(5):
        if clock_ip is None:
            try:
                raw = open("/tmp/offset.json").read().strip()
            except OSError:
                raw = ""
        else:
            raw = ssh(clock_ip, "cat /tmp/offset.json").strip()
        try:
            d = json.loads(raw)
            offs = {m.lower(): (int(o), float(s)) for m, (o, s) in d["offsets"].items()}
            if offs:
                return int(d["ref_tsf"]), offs
        except (ValueError, KeyError, TypeError):
            pass
        time.sleep(0.3)
    if clock_ip is None:
        raise RuntimeError("offset.json empty/unparsable locally (beacon_tracker.py on this host) "
                           "-- is it running, is the driver beacon tap installed on the APs, and "
                           "are the APs hearing each other? (./run.sh doctor)")
    raise RuntimeError("offset.json empty/unparsable on %s -- is offset_tracker.py running there "
                       "and hearing every AP's beacons? (./run.sh doctor)" % clock_ip)


def _map_target(shared_instant, offset, slope, offset_ts, stagger, ap_index):
    """Express the one shared instant (reference clock) in an AP's own TSF."""
    return int(round(shared_instant
                     + offset
                     + slope * (shared_instant - offset_ts)
                     + stagger * ap_index))


def _do_shots(aps, clock_ip, lead, stagger, shots, gap):
    """The timing-critical fire loop, shared by every command.

    Reads per-AP (offset, slope) from the clock tracker (monitor: on the observer over
    ssh; beacon: the local file clock_ip=None), maps the one shared instant
    (reference = aps[0]) into each AP's own TSF, and fans the gated trigger out in
    parallel. This is the proven microsecond path -- one implementation so no caller can
    perturb it. Captures/status are handled by the caller around this.

    Returns `commanded`: a list over shots, each a {ap_mac: target_tsf} of what was
    actually commanded that shot -- so the gate diagnostic can pair a frame's monitor
    tsft with its own commanded target (seq = shot index).
    """
    ref_ip = aps[0]["ip"]
    # a single-AP shot is all reference clock -- no cross-AP offset is consulted, so skip
    # the tracker read entirely (one fewer read per shot, and no tracker needed).
    need_offsets = len(aps) > 1
    offset_ts, offsets = _read_offsets(clock_ip) if need_offsets else (0, {})
    commanded = []
    for shot in range(shots):
        if need_offsets and shot % OFFSET_REFRESH_EVERY == 0:
            offset_ts, offsets = _read_offsets(clock_ip)

        shared_instant = read_tsf(ref_ip) + lead
        threads = []
        shot_targets = {}
        for ap_index, ap in enumerate(aps):
            mac = ap["mac"].lower()
            if ap_index == 0:
                offset, slope = 0, 0.0          # reference AP maps to itself
            elif mac in offsets:
                offset, slope = offsets[mac]
            elif clock_ip is None:
                raise RuntimeError("no TSF offset for AP %s (%s) -- it is not in the beacon "
                                   "hearing graph: it hears no peer AP, or its component is "
                                   "disconnected from the reference. Check AP<->AP range or add "
                                   "a bridge (SYNC.md 'What has to be true')."
                                   % (ap.get("name", "?"), mac))
            else:
                raise RuntimeError("no TSF offset for AP %s (%s) -- the observer is not "
                                   "hearing its beacons; check topo observer / range"
                                   % (ap.get("name", "?"), mac))
            target = _map_target(shared_instant, offset, slope, offset_ts,
                                 stagger, ap_index)
            shot_targets[mac] = target
            payload = blob(target, ap["rate"], ap["txp"], ap["nframes"], STAMP_OFFSET,
                           ap["station_id"], shot, ap["frame_len"], ap["mac"])
            t = threading.Thread(target=write_node, args=(ap["ip"], ap["node"], payload))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        commanded.append(shot_targets)
        time.sleep(gap)
    return commanded


def _cap_secs(shots, gap, lead):
    """Capture window that outlasts the whole fire loop (ssh latency + gap + lead tail)."""
    return int(shots * (gap + 0.5)) + int(lead / 1e6) + 6


# --------------------------------------------------------------------------- config

def _rate_for(mcs):
    """HAL rateCode for an MCS index (HT frames): 0x80 | mcs."""
    return 0x80 | (int(mcs) & 0x7f)


def load(topo_path, shot_path):
    """Load + validate topo and shot; resolve link names into a fire plan.

    Returns a dict:
      { "channel", "observer": {name, ip, iface},
        "aps": [ {name, ip, iface, mac, node(filled later), rate, txp, txpower_mbm,
                  nframes, frame_len, station_id, mcs,
                  station: {name, ip, iface, station_id}} ],   # aps[0] = timing reference
        "cap_nodes": { (ip,iface): {ip, iface, name} },        # distinct receivers to capture
        "stagger_us", "lead_us", "shots", "gap_s" }
    """
    global _PW
    with open(topo_path) as fh:
        topo = json.load(fh)
    with open(shot_path) as fh:
        shot = json.load(fh)

    _PW = topo.get("password", _PW)
    channel = topo.get("channel", 1)
    if not (isinstance(channel, int) and 1 <= channel <= 13):
        raise RuntimeError("topo channel %r invalid -- AR9271 is 2.4 GHz, so 1..13" % channel)
    sync = topo.get("sync", "monitor")
    if sync not in ("monitor", "beacon"):
        raise RuntimeError("topo sync %r invalid -- 'monitor' (one observer hears every AP) "
                           "or 'beacon' (APs hear each other, tracker on the host)" % sync)
    if "nodes" not in topo:
        raise RuntimeError("topo has no 'nodes'")
    nodes = topo["nodes"]

    # required keys per role, validated where a node is first resolved (clear error, not KeyError)
    _REQUIRED = {"ap": ("ip", "iface", "mac"), "station": ("ip", "iface", "station_id")}

    def node(name, role=None):
        if name not in nodes:
            raise RuntimeError("topo has no node named %r (have: %s)"
                               % (name, ", ".join(sorted(nodes))))
        n = nodes[name]
        if role is not None:
            if n.get("role") != role:
                raise RuntimeError("node %r has role %r but is used as an %s"
                                   % (name, n.get("role"), role))
            missing = [k for k in _REQUIRED[role] if k not in n]
            if missing:
                raise RuntimeError("%s node %r is missing %s"
                                   % (role, name, ", ".join(missing)))
        return n

    links = shot["links"]
    if not links:
        raise RuntimeError("shot has no links")

    # A-MPDU shape is common to the whole shot (comparing APs only makes sense when the
    # aggregate is identical); reject a per-link override to make that explicit.
    nframes = shot.get("nframes", NFRAMES_DEFAULT)
    frame_len = shot.get("frame_len", FRAME_LEN_DEFAULT)
    for lk in links:
        for k in ("nframes", "frame_len"):
            if k in lk:
                raise RuntimeError("A-MPDU shape is common: put %r at the shot top level, "
                                   "not on a link" % k)

    def station_for(link, ap_name):
        """Resolve a link's target station node, defaulting to the observer."""
        sname = link.get("station") or topo.get("observer")
        if not sname:
            raise RuntimeError("link %s has no station and topo has no observer to "
                               "fall back to" % ap_name)
        s = node(sname, "station")
        return {"name": sname, "ip": s["ip"], "iface": s["iface"],
                "station_id": int(s["station_id"])}

    aps = []
    seen_ap = set()
    for lk in links:
        ap_name = lk["ap"]
        if ap_name in seen_ap:
            raise RuntimeError("AP %r appears in two links -- one station per AP per shot"
                               % ap_name)
        seen_ap.add(ap_name)
        a = node(ap_name, "ap")
        sta = station_for(lk, ap_name)
        mcs = lk.get("mcs", shot.get("mcs", 0))
        aps.append({
            "name": ap_name, "ip": a["ip"], "iface": a["iface"], "mac": a["mac"],
            "rate": _rate_for(mcs), "mcs": int(mcs),
            "txp": TXP_DESC_DEFAULT,
            "txpower_mbm": lk.get("txpower_mbm", shot.get("txpower_mbm")),
            "nframes": nframes, "frame_len": frame_len,
            "station_id": sta["station_id"], "station": sta,
        })

    # observer = the station that measures cross-AP timing (test-sync capture point, and
    # in monitor sync also the clock source). topo observer, else the first link's station.
    obs_name = topo.get("observer") or aps[0]["station"]["name"]
    obs = node(obs_name, "station")
    observer = {"name": obs_name, "ip": obs["ip"], "iface": obs["iface"]}
    # clock source for the fire loop. Two families put the tracker on THIS host and write
    # offset.json locally (clock_ip=None): beacon sync, and multi-monitor sync (a "monitors"
    # list). A single-observer monitor sync keeps the tracker on the observer -> read via ssh.
    monitors = topo.get("monitors")
    if monitors is not None:
        if not isinstance(monitors, list) or not monitors:
            raise RuntimeError("topo 'monitors' must be a non-empty list of station names")
        for mn in monitors:
            node(mn, "station")
    multi_monitor = sync == "monitor" and bool(monitors)
    clock_ip = None if (sync == "beacon" or multi_monitor) else obs["ip"]

    # distinct receiver cards to capture on (target stations)
    cap_nodes = {}
    for ap in aps:
        s = ap["station"]
        cap_nodes.setdefault((s["ip"], s["iface"]),
                             {"ip": s["ip"], "iface": s["iface"], "name": s["name"]})

    return {
        "channel": channel, "sync": sync, "clock_ip": clock_ip,
        "observer": observer, "aps": aps, "cap_nodes": cap_nodes,
        "stagger_us": shot.get("stagger_us", 0),
        "lead_us": shot.get("lead_us", LEAD_US_DEFAULT),
        "shots": shot.get("shots", SHOTS_DEFAULT),
        "gap_s": shot.get("gap_s", GAP_S_DEFAULT),
    }


# --------------------------------------------------------- fire + capture orchestration

def _config_aps(aps):
    """Resolve each AP's debugfs node and set its real TX power (once per run)."""
    for ap in aps:
        ap["node"] = ssh(ap["ip"], "ls " + NODE_GLOB).strip()
        if not ap["node"]:
            raise RuntimeError("AP %s (%s): no cosr_gated_tx debugfs node -- is it in AP "
                               "mode with the Co-SR firmware?" % (ap["name"], ap["ip"]))
        if ap.get("txpower_mbm"):
            set_txpower(ap["ip"], ap["iface"], ap["txpower_mbm"])


def _fire_and_capture(plan, capture=True):
    """Config, (optionally) capture on every station, fire, read fire-status, pull pcaps.

    Returns (frames_by_key, status_by_ap, local_pcaps, live_by_key, commanded):
      frames_by_key   {(ip,iface): [parsed frame, ...]}   parsed capture per receiver
      status_by_ap    {ap_name: {fired, late, toofar, nobf}}
      local_pcaps     {(ip,iface): local pcap path}
      live_by_key     {(ip,iface): bool}   did the capture return any frames at all
      commanded       [ {ap_mac: target_tsf}, ... ]   per-shot commanded targets
    """
    aps = plan["aps"]
    _config_aps(aps)
    for ap in aps:
        status_clear(ap["ip"])

    keys = list(plan["cap_nodes"]) if capture else []
    remote = {k: "/tmp/cosr_rx_%d.pcap" % i for i, k in enumerate(keys)}
    local = {k: "/tmp/cosr_rx_local_%d.pcap" % i for i, k in enumerate(keys)}
    secs = _cap_secs(plan["shots"], plan["gap_s"], plan["lead_us"])
    for k in keys:
        try:                              # never let a failed pull read a prior run's pcap
            os.remove(local[k])
        except OSError:
            pass
        n = plan["cap_nodes"][k]
        cap_start(n["ip"], n["iface"], remote[k], secs)
    if keys:
        time.sleep(1)

    try:
        commanded = _do_shots(aps, plan["clock_ip"], plan["lead_us"],
                              plan["stagger_us"], plan["shots"], plan["gap_s"])

        time.sleep(plan["lead_us"] / 1e6 + 3)   # let the last frame key out + tcpdump flush

        status_by_ap = {ap["name"]: status_counts(ap["ip"]) for ap in aps}

        frames_by_key, live_by_key = {}, {}
        for k in keys:
            n = plan["cap_nodes"][k]
            cap_pull(n["ip"], remote[k], local[k])
            # a live capture always carries beacons, so >header-only bytes == tcpdump+scp
            # worked; an empty/missing pcap means the capture died, not a real zero delivery.
            live_by_key[k] = os.path.exists(local[k]) and os.path.getsize(local[k]) > 24
            frames_by_key[k] = parse_frames(local[k]) if live_by_key[k] else []
        return frames_by_key, status_by_ap, local, live_by_key, commanded
    finally:
        # tear the remote tcpdumps down (each has a `timeout`, but a mid-run failure would
        # otherwise leave them capturing until it elapses, holding the card).
        for k in keys:
            n = plan["cap_nodes"][k]
            ssh_fresh(n["ip"], "echo %s | sudo -S pkill -x tcpdump 2>/dev/null; echo -n ''" % _PW)


def _link_delivery(plan, frames_by_key, status_by_ap, live_by_key):
    """Per-link delivery, counted at each link's target station.

    A frame counts for link (apA -> staX) only if its source is apA AND its stamped
    station_id is staX's -- so a station overhearing a frame meant for another station
    is not miscounted as delivered. Duplicates (same (station_id,seq,idx)) are dropped.
    Denominator = shots the AP actually FIRED (from the kernel log) x nframes.

    A receiver whose capture came back empty (tcpdump/scp failed) yields delivery=None
    plus a warning, never 0.0 -- a dead capture must not masquerade as a total loss.
    """
    links = {}
    warnings = []
    for ap in plan["aps"]:
        mac = ap["mac"].lower()
        sid = ap["station_id"]
        s = ap["station"]
        key = (s["ip"], s["iface"])
        fired = status_by_ap[ap["name"]]["fired"]
        sent = fired * max(1, ap["nframes"])
        alive = live_by_key.get(key, False)
        frames = frames_by_key.get(key, [])
        seen = {(f["seq"], f["idx"]) for f in frames
                if f["sa"] == mac and f["station_id"] == sid}
        rx = len(seen)
        name = "%s->%s" % (ap["name"], s["name"])
        links[name] = {
            "ap": ap["name"], "station": s["name"], "station_id": sid,
            "fired": fired, "sent": sent, "rx": rx,
            "delivery": (round(rx / sent, 3) if sent else None) if alive else None,
        }
        if not alive:
            warnings.append("%s: no frames captured at %s (tcpdump/scp failed?) -- delivery "
                            "unmeasured, not zero" % (name, s["name"]))
        st = status_by_ap[ap["name"]]
        misses = st["late"] + st["toofar"] + st["nobf"]
        if fired == 0:
            warnings.append("%s: 0 shots fired (late=%d toofar=%d nobf=%d) -- raise lead_us, or a "
                            "stale/dead clock tracker put the target out of range (./run.sh doctor)"
                            % (ap["name"], st["late"], st["toofar"], st["nobf"]))
        elif misses:
            warnings.append("%s: %d/%d shots did not fire (late=%d toofar=%d nobf=%d) -- not "
                            "counted as loss" % (ap["name"], misses, fired + misses,
                                                 st["late"], st["toofar"], st["nobf"]))
    return links, warnings


# --------------------------------------------------------------------------- commands

def shot(plan):
    """Fire a coordinated Co-SR transmission; report per-link delivery + fire status.

    The lightweight command: transmit and confirm it landed. No RSSI / MCS / interference
    matrices (use `measure` for those).
    """
    frames_by_key, status_by_ap, _, live_by_key, _ = _fire_and_capture(plan)
    links, warnings = _link_delivery(plan, frames_by_key, status_by_ap, live_by_key)
    out = {"links": links, "shots": plan["shots"], "stagger_us": plan["stagger_us"],
           "delivery_note": "raw delivery = rx / fired, counted from unique stamped "
                            "subframe ids at the receiver."}
    if warnings:
        out["warnings"] = warnings
    return out


def measure(plan):
    """Fire + full model inputs: per-link success, AP->station RSSI, AP<->AP RSSI, MCS check.

      { "success_prob":       { "<ap>-><sta>": {delivery, rx, sent, fired, station_id,
                                                mcs_cmd, mcs_seen} },
        "rssi_ap_to_sta_dbm":  { "<ap>-><sta>": dbm },   # per-frame DATA RSSI at the station
        "rssi_ap_to_ap_dbm":   { "<ap mac>": { "<peer mac>": dbm } },
        "warnings":            [ ... ] }
    """
    frames_by_key, status_by_ap, local, live_by_key, _ = _fire_and_capture(plan)
    links, warnings = _link_delivery(plan, frames_by_key, status_by_ap, live_by_key)

    success, rssi_a2s = {}, {}
    for ap in plan["aps"]:
        mac = ap["mac"].lower()
        sid = ap["station_id"]
        s = ap["station"]
        key = "%s->%s" % (ap["name"], s["name"])
        entry = dict(links[key])
        del entry["ap"], entry["station"]

        # MCS integrity: commanded vs on-air, from this station's capture
        mcs_seen, nseen = observed_mcs(local[(s["ip"], s["iface"])], [ap["mac"]]).get(mac, (None, 0))
        entry["mcs_cmd"] = ap["mcs"]
        entry["mcs_seen"] = mcs_seen
        if mcs_seen is not None and mcs_seen != ap["mcs"]:
            warnings.append("%s: commanded MCS %d but on-air MCS %d (rate downgraded)"
                            % (key, ap["mcs"], mcs_seen))
        elif mcs_seen is None and nseen > 0:
            warnings.append("%s: on-air frames are non-HT/legacy -- commanded MCS %d not "
                            "honored (rate table lacks it?)" % (key, ap["mcs"]))
        success[key] = entry

        # AP->station RSSI: mean per-frame DATA signal of this AP's stamped frames here
        sigs = [f["signal"] for f in frames_by_key.get((s["ip"], s["iface"]), [])
                if f["sa"] == mac and f["station_id"] == sid and f["signal"] is not None]
        if sigs:
            rssi_a2s[key] = round(sum(sigs) / len(sigs), 1)

    out = {"success_prob": success,
           "rssi_ap_to_sta_dbm": rssi_a2s,
           "rssi_ap_to_ap_dbm": rssi_ap_to_ap(plan["aps"]) if len(plan["aps"]) >= 2 else {}}
    if warnings:
        out["warnings"] = warnings
    return out


def test_sync(plan):
    """Fire staggered coordinated shots and measure cross-AP timing at the observer.

    Every non-reference AP fires `stagger_us` * index after the reference. At the
    observer (one clock, no cross-clock drift) we pair first-subframe (idx 0) emissions
    by shot seq and report, per AP, the error = (measured emission delta) - (intended
    stagger). Reported as mean / median / std / p90 of the absolute error (µs).

    Only idx 0 carries the valid PHY-RXSTART timestamp; a later A-MPDU subframe's
    radiotap mactime is a monitor artifact, so sync is measured on single-frame or the
    first subframe.

    The error is kept signed: a constant offset (bias) and random spread (jitter) are
    distinct sync defects, so `bias_us` (mean signed) and `jitter_us` (std) are reported
    separately, with `p90_abs_us` for the worst-case magnitude.
    """
    aps = plan["aps"]
    if len(aps) < 2:
        raise RuntimeError("test-sync needs >= 2 APs")
    obs = plan["observer"]

    _config_aps(aps)
    for ap in aps:
        status_clear(ap["ip"])
    remote = "/tmp/cosr_sync.pcap"
    secs = _cap_secs(plan["shots"], plan["gap_s"], plan["lead_us"])
    cap_start(obs["ip"], obs["iface"], remote, secs)
    time.sleep(1)
    _do_shots(aps, plan["clock_ip"], plan["lead_us"], plan["stagger_us"],
              plan["shots"], plan["gap_s"])
    time.sleep(plan["lead_us"] / 1e6 + 3)
    status_by_ap = {ap["name"]: status_counts(ap["ip"]) for ap in aps}
    local = "/tmp/cosr_sync_local.pcap"
    try:                                  # never let a failed pull read a prior run's pcap
        os.remove(local)
    except OSError:
        pass
    cap_pull(obs["ip"], remote, local)
    if not (os.path.exists(local) and os.path.getsize(local) > 24):
        raise RuntimeError("sync capture empty on %s (tcpdump/scp failed?)" % obs["ip"])
    frames = parse_frames(local)

    # per-AP {seq: tsft} on the reference clock, first subframe only
    ref_mac = aps[0]["mac"].lower()
    by_ap = {}
    for f in frames:
        if f["idx"] == 0:
            by_ap.setdefault(f["sa"], {})[f["seq"]] = f["tsft"]
    ref = by_ap.get(ref_mac, {})

    per_ap = {}
    stagger = plan["stagger_us"]
    for i, ap in enumerate(aps[1:], start=1):
        mac = ap["mac"].lower()
        here = by_ap.get(mac, {})
        common = sorted(set(ref) & set(here))
        errs = [(here[s] - ref[s]) - stagger * i for s in common]   # signed
        per_ap[ap["name"]] = _stats(errs, len(common))

    out = {"reference": aps[0]["name"], "stagger_us": stagger,
           "shots": plan["shots"], "per_ap": per_ap,
           "fired": {n: status_by_ap[n]["fired"] for n in status_by_ap}}
    warnings = [w for ap in aps
                for w in ([ "%s: 0 shots fired" % ap["name"]] if status_by_ap[ap["name"]]["fired"] == 0 else [])]
    if warnings:
        out["warnings"] = warnings
    return out


SYNC_OUTLIER_US = 50   # |error| above this is an AR9271 monitor RX-mactime artifact (a mis-stamped
                       # PPDU, hundreds of µs), not a real emission error -- excluded from bias/jitter


def _stats(errs, paired):
    """SIGNED per-shot errors (µs) -> bias (mean), jitter (std), signed median, p90 of |err|.

    A few shots per run land hundreds of µs off from the monitor's RX-mactime artifact while the
    true cross-AP error is µs-level, so the non-robust mean/std would be dominated by them. bias and
    jitter are therefore computed over the inliers (|err| <= SYNC_OUTLIER_US) and `outliers` reports
    how many shots were excluded; median and p90 use every shot (they are already robust).
    """
    if not errs:
        return {"paired": paired, "bias_us": None, "jitter_us": None,
                "median_us": None, "p90_abs_us": None, "outliers": 0}
    xs = sorted(errs)
    n = len(xs)
    median = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    absx = sorted(abs(x) for x in xs)
    p90 = absx[min(n - 1, int(round(0.9 * (n - 1))))]
    inl = [x for x in xs if abs(x) <= SYNC_OUTLIER_US]
    bias = jitter = None
    if inl:
        bias = sum(inl) / len(inl)
        jitter = (sum((x - bias) ** 2 for x in inl) / len(inl)) ** 0.5
    return {"paired": paired,
            "bias_us": round(bias, 2) if bias is not None else None,
            "jitter_us": round(jitter, 2) if jitter is not None else None,
            "median_us": round(median, 2), "p90_abs_us": round(p90, 2), "outliers": n - len(inl)}


GATE_OUTLIER_US = 50   # residual above this = a late fire (e.g. ah_stopTxDma blocking)


def _detrend_residual(pairs):
    """pairs = [(seq, tsft - commanded_target), ...] across shots of ONE AP.

    tsft (monitor clock) and the commanded target (AP clock) live in different domains --
    a constant offset plus slow drift (~µs/shot). Fit that linear ramp against seq and
    subtract it; a clean gate leaves near-zero residual, a late fire spikes off the ramp.
    Returns {captured, drift_us_per_shot, residual_median_us, residual_max_us, outliers}.
    """
    n = len(pairs)
    if n < 2:
        return {"drift_us_per_shot": None, "residual_median_us": None,
                "residual_max_us": None, "outliers": []}
    mean_x = sum(s for s, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    denom = sum((s - mean_x) ** 2 for s, _ in pairs)
    slope = (sum((s - mean_x) * (y - mean_y) for s, y in pairs) / denom) if denom else 0.0
    resid = [(s, (y - mean_y) - slope * (s - mean_x)) for s, y in pairs]
    absr = sorted(abs(r) for _, r in resid)
    return {
        "drift_us_per_shot": round(slope, 2),
        "residual_median_us": round(absr[len(absr) // 2], 2),
        "residual_max_us": round(absr[-1], 2),
        "outliers": [[s, round(r, 1)] for s, r in resid if abs(r) > GATE_OUTLIER_US],
    }


def gate(plan):
    """Single-AP absolute gate precision -- the timing diagnostic.

    Fires the first link's AP `shots` times (no cross-AP timing), captures at that AP's
    station, pairs each frame's monitor tsft with its own commanded target by seq, and
    detrends the AP<->monitor clock ramp. A clean gate leaves a µs-level residual with no
    outliers; a late fire spikes off the ramp. Uses the same wire (`blob`) and parser
    (`parse_frames`) as every other command -- no separate tool, no second wire format.
    """
    ap = plan["aps"][0]
    s = ap["station"]
    sub = dict(plan)
    sub["aps"] = [ap]              # fire this AP alone: single-AP path skips the observer
    sub["cap_nodes"] = {(s["ip"], s["iface"]):
                        {"ip": s["ip"], "iface": s["iface"], "name": s["name"]}}

    frames_by_key, status_by_ap, _local, live_by_key, commanded = _fire_and_capture(sub)
    key = (s["ip"], s["iface"])
    mac = ap["mac"].lower()

    tsft = {f["seq"]: f["tsft"] for f in frames_by_key.get(key, [])
            if f["sa"] == mac and f["idx"] == 0}
    targets = {shot: cmd[mac] for shot, cmd in enumerate(commanded) if mac in cmd}
    paired = sorted(set(tsft) & set(targets))
    pairs = [(seq, tsft[seq] - targets[seq]) for seq in paired]

    out = {"ap": ap["name"], "station": s["name"], "shots": plan["shots"],
           "fired": status_by_ap[ap["name"]]["fired"],
           "captured": len(tsft), "paired": len(paired)}
    out.update(_detrend_residual(pairs))
    out["clean"] = bool(pairs) and not out["outliers"]

    warnings = []
    if not live_by_key.get(key):
        warnings.append("no frames captured at %s (tcpdump/scp failed?)" % s["name"])
    if len(paired) < 2:
        warnings.append("too few paired shots (%d) to measure the gate -- check delivery/offset"
                        % len(paired))
    if warnings:
        out["warnings"] = warnings
    return out


# --------------------------------------------------------------------------- CLI

_COMMANDS = {"shot": shot, "measure": measure, "test-sync": test_sync, "gate": gate}


def _preflight():
    """Fail loudly if a host-side tool is missing -- otherwise a silent tshark/scp failure
    would look like clean data (0 delivery, null RSSI) instead of an error."""
    missing = [t for t in ("tshark", "sshpass", "scp") if not shutil.which(t)]
    if missing:
        sys.exit("missing required tool(s): %s -- install them on the controller host"
                 % ", ".join(missing))


def main(argv):
    if len(argv) < 2 or argv[0] not in _COMMANDS:
        sys.exit("usage: cosr_ctl.py {shot|measure|test-sync|gate} topo.json shot.json")
    _preflight()
    cmd = argv[0]
    topo_path = argv[1]
    shot_path = argv[2] if len(argv) > 2 else "shot.json"
    try:
        plan = load(topo_path, shot_path)
        result = _COMMANDS[cmd](plan)
    except (RuntimeError, FileNotFoundError, ValueError, KeyError) as e:
        sys.exit("%s: %s" % (type(e).__name__, e))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
