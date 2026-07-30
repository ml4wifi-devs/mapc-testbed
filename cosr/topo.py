"""topo.json + experiment.json -> a validated plan. Pure: no hardware, no ssh, no I/O beyond
reading the two files.

Every refusal names what is wrong with the description rather than failing later against the
hardware, so the messages are part of the interface and are pinned by tests.

Credentials are returned in the plan rather than held in a module-level global, so a helper
cannot pick up the wrong role's password and two descriptions can be resolved in one process
without interfering. The aggregate shape is checked against what the transmit buffer pool can
hold, because an over-large shape is otherwise only discovered as a no-buffer result at
transmit time.
"""
import json

from . import wire

TXP_DESC_DEFAULT = 40        # WMI descriptor txpower byte; the AR9271 RF clamps it (see nl80211)
# Power is stated in whole dBm, which is the only granularity the radio honours. The kernel
# interface underneath takes hundredths of a dBm, so the plan carries both.
MBM_PER_DBM = 100
MAX_TXPOWER_DBM = 20
MAX_MCS = 7                  # single-stream 20 MHz 802.11n
# How far ahead of now the shared instant is placed. It must cover the time for the instruction
# to reach every node, or a node finds its instant already past and the gate refuses the shot;
# and it must stay inside the window the gate accepts. Extrapolating further also costs a little
# accuracy in the clock relation, so a deployment on a wired control network can reduce it.
LEAD_US_DEFAULT = 400000
REPEATS_DEFAULT = 20
# Inter-shot spacing inside a batch. A trigger write blocks until its gate fires, so the shortest
# workable value is set by the transmit path rather than by the host: below it, later shots find
# their instant already past. The usable floor depends on the aggregate shape and on the radio,
# so it is a starting point -- establish it for a given deployment with `cosr calibrate`.
SPACING_US_DEFAULT = 15000
FRAME_LEN_DEFAULT = 200
NFRAMES_DEFAULT = 1

DEFAULT_USER = "modwifi"
DEFAULT_PW = "modwifi"

_REQUIRED = {"ap": ("ip", "iface", "mac"), "station": ("ip", "iface", "station_id")}


def _is_int(v):
    """A bool is an int in Python, and True would otherwise be read as 1."""
    return isinstance(v, int) and not isinstance(v, bool)


def channel_to_freq_mhz(channel):
    """Centre frequency of a 2.4 GHz channel. Channel 14 is not reachable here by design."""
    return 2407 + 5 * int(channel)


def creds_for_role(topo, role):
    """(user, password) for a node role. APs use ap_*; every station (monitor or target) uses
    monitor_*; each falls back to the flat user/password, then the image default."""
    group = "ap" if role == "ap" else "monitor"
    user = topo.get(group + "_user") or topo.get("user") or DEFAULT_USER
    pw = topo.get(group + "_password") or topo.get("password") or DEFAULT_PW
    return user, pw


def load(topo_path, shot_path):
    """Read and validate both files; return the fire plan."""
    with open(topo_path) as fh:
        topo = json.load(fh)
    with open(shot_path) as fh:
        experiment = json.load(fh)
    return resolve(topo, experiment)


def resolve(topo, experiment):
    """The validation and resolution itself, on already-parsed dicts (what the tests drive)."""
    channel = topo.get("channel", 1)
    if not (isinstance(channel, int) and 1 <= channel <= 13):
        raise RuntimeError("topo channel %r invalid -- AR9271 is 2.4 GHz, so 1..13" % channel)

    sync = topo.get("sync", "beacon")
    if sync not in ("monitor", "beacon"):
        raise RuntimeError("topo sync %r invalid -- 'monitor' (stations observe every AP) "
                           "or 'beacon' (APs hear each other)" % sync)
    if "nodes" not in topo:
        raise RuntimeError("topo has no 'nodes'")
    nodes = topo["nodes"]

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

    links = experiment.get("links") or []
    if not links:
        raise RuntimeError("the experiment has no links")

    # The aggregate shape is common to every link: comparing transmitters only means something when the
    # aggregate is identical, so a per-link override is rejected rather than silently honoured.
    nframes = experiment.get("nframes", NFRAMES_DEFAULT)
    frame_len = experiment.get("frame_len", FRAME_LEN_DEFAULT)
    for lk in links:
        for k in ("nframes", "frame_len"):
            if k in lk:
                raise RuntimeError("the aggregate shape is common to every link: put %r at the top level, "
                                   "not on a link" % k)
    wire.validate_shape(nframes, frame_len)

    def station_for(link, ap_name):
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
        # Stated per link, with no common fallback: rate and power are the dimensions an
        # experiment varies between links, and a default would let a link that was never
        # configured look identical to one that was.
        if "mcs" not in lk:
            raise RuntimeError("link %s->%s has no mcs; every link states its own"
                               % (ap_name, lk.get("station")))
        if "txpower_dbm" not in lk:
            raise RuntimeError("link %s->%s has no txpower_dbm; every link states its own"
                               % (ap_name, lk.get("station")))
        where = "link %s->%s" % (ap_name, lk.get("station"))
        mcs, dbm = lk["mcs"], lk["txpower_dbm"]
        if not _is_int(mcs) or not 0 <= mcs <= MAX_MCS:
            raise RuntimeError("%s mcs must be a whole number 0..%d (this radio is "
                               "single-stream 20 MHz 802.11n), not %r" % (where, MAX_MCS, mcs))
        # A fractional power is discarded by the radio without an error, leaving whatever was set
        # before it in force. Nothing downstream can see that: the shot reports OK at a power it
        # was never given, and a sweep containing one such level repeats its neighbour's result.
        if not _is_int(dbm):
            raise RuntimeError("%s txpower_dbm must be a whole number of dBm, not %r. The radio "
                               "discards a request it cannot represent without reporting "
                               "anything, and stays at the power set before it."
                               % (where, dbm))
        if not 0 <= dbm <= MAX_TXPOWER_DBM:
            raise RuntimeError("%s asks for %d dBm; the radio covers 0..%d."
                               % (where, dbm, MAX_TXPOWER_DBM))
        aps.append({
            "name": ap_name, "ip": a["ip"], "iface": a["iface"], "mac": a["mac"].lower(),
            "rate": wire.rate_for_mcs(mcs), "mcs": mcs,
            "txp": TXP_DESC_DEFAULT,
            "txpower_dbm": dbm, "txpower_mbm": dbm * MBM_PER_DBM,
            "nframes": nframes, "frame_len": frame_len,
            "station_id": sta["station_id"], "station": sta,
        })

    obs_name = topo.get("observer") or aps[0]["station"]["name"]
    obs = node(obs_name, "station")
    observer = {"name": obs_name, "ip": obs["ip"], "iface": obs["iface"],
                "station_id": int(obs["station_id"])}

    monitors = topo.get("monitors")
    if sync == "monitor" and not monitors:
        raise RuntimeError(
            "sync is 'monitor' but no 'monitors' are listed: name the stations whose "
            "observations should relate the clocks, or use sync 'beacon'")
    if monitors is not None:
        if not isinstance(monitors, list) or not monitors:
            raise RuntimeError("topo 'monitors' must be a non-empty list of station names")
        for mn in monitors:
            node(mn, "station")


    all_nodes = []
    for name, n in nodes.items():
        if "ip" not in n:
            continue
        user, pw = creds_for_role(topo, n.get("role"))
        all_nodes.append({"name": name, "role": n.get("role"), "ip": n["ip"],
                          "iface": n.get("iface"), "user": user, "password": pw,
                          "mac": (n.get("mac") or "").lower() or None,
                          "ssid": n.get("ssid"),
                          "station_id": n.get("station_id")})

    spacing_us = int(experiment.get("spacing_us", SPACING_US_DEFAULT))
    if spacing_us <= 0:
        raise RuntimeError(
            "spacing_us must be positive: with no spacing every shot in a batch targets the "
            "same instant, and all but the first are refused as already past")

    lead_us = int(experiment.get("lead_us", LEAD_US_DEFAULT))
    if not 0 < lead_us <= wire.MAX_LEAD_US:
        raise RuntimeError(
            "lead_us must be between 1 and %d: the gate refuses an instant further ahead than "
            "that, so every shot would be rejected as too far away" % wire.MAX_LEAD_US)

    return {
        "channel": channel,
        # Where the agents connect. Left to the caller when absent, since a deployment on one
        # host and one reachable from every node are not always the same address.
        "hub": topo.get("hub"),
        "token": topo.get("token"),
        # Carried explicitly so the receiver's observed radiotap frequency can be compared
        # against it; without a value to compare, a drifted card is indistinguishable from a
        # link that simply delivered nothing.
        "channel_freq_mhz": channel_to_freq_mhz(channel),
        "sync": sync,
        "monitors": list(monitors) if monitors else [],
        "observer": observer, "aps": aps,
        "nodes": all_nodes,
        "ap_macs": [ap["mac"] for ap in aps],
        # Every transmitter in the deployment, not only those in this shot. The clock plane is a
        # property of the testbed: a shot using one transmitter still needs the observations the
        # others provide in order to place an instant at all.
        "all_ap_macs": [n["mac"] for n in all_nodes
                        if n["role"] == "ap" and n["mac"]],
        "reference_mac": aps[0]["mac"],
        "stagger_us": experiment.get("stagger_us", 0),
        "lead_us": lead_us,
        "repeats": experiment.get("repeats", REPEATS_DEFAULT),
        "spacing_us": spacing_us,
        "nframes": nframes, "frame_len": frame_len,
    }

