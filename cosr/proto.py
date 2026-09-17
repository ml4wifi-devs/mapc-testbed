"""Subjects and message shapes for the control plane.

A round is one published message and one reply per participant. Everything a transmitter or a
receiver needs for that round travels in the single `FIRE` message, including the deadline by
which a receiver should answer, so neither side has to be asked a follow-up question.

Receivers are told the exact sequence range being fired, which is what lets them answer about a
specific round rather than "whatever arrived recently". Every message carries a version, so an
agent and a controller that disagree about the shape fail loudly on the first round instead of
producing numbers whose meaning differs between the two.
"""

VERSION = 1

SUBJ_FIRE = "cosr.fire"
SUBJ_HELLO = "cosr.hello"
SUBJ_BEACON = "cosr.beacon"


def subj_status(ap_name):
    """Where a transmitter publishes what its shots actually did."""
    return "cosr.status." + ap_name


def subj_report(station_name):
    """Where a receiver publishes what it counted."""
    return "cosr.report." + station_name


def subj_rpc(role, name):
    """Request/reply endpoint of one agent, for health and calibration."""
    return "cosr.rpc.%s.%s" % (role, name)


class ProtocolError(Exception):
    """A message does not match the agreed shape."""


def _require(obj, fields, what):
    missing = [f for f in fields if f not in obj]
    if missing:
        raise ProtocolError("%s is missing %s" % (what, ", ".join(missing)))


_FIRE_FIELDS = ("v", "run", "batch", "seq0", "n", "spacing_us", "stagger_us", "lead_us",
                "nframes", "frame_len", "stamp_off", "channel_freq_mhz", "aps", "stations")
_FIRE_AP_FIELDS = ("mac", "target", "rate", "txp", "station_id")
_FIRE_STATION_FIELDS = ("ap_macs", "station_id", "since_ordinal", "expect_per_ap")


def build_fire(run, batch, seq0, n, spacing_us, stagger_us, lead_us,
               nframes, frame_len, stamp_off, channel_freq_mhz, aps, stations):
    """Assemble the round message.

    `aps` maps a transmitter name to its commanded target and radio settings; `stations` maps a
    receiver name to what it should expect. A receiver's `expect_per_ap` lets it answer as soon
    as everything has arrived rather than always waiting out the deadline.
    """
    msg = {
        "v": VERSION, "run": run, "batch": batch, "seq0": int(seq0), "n": int(n),
        "spacing_us": int(spacing_us), "stagger_us": int(stagger_us), "lead_us": int(lead_us),
        "nframes": int(nframes), "frame_len": int(frame_len), "stamp_off": int(stamp_off),
        "channel_freq_mhz": int(channel_freq_mhz),
        "aps": aps, "stations": stations,
    }
    validate_fire(msg)
    return msg


def validate_fire(msg):
    _require(msg, _FIRE_FIELDS, "fire message")
    if msg["v"] != VERSION:
        raise ProtocolError(
            "fire message is version %r but this agent speaks %r; the controller and the agents "
            "are not the same build" % (msg["v"], VERSION))
    if msg["n"] < 1:
        raise ProtocolError("fire message asks for %r shots" % (msg["n"],))
    for name, ap in (msg["aps"] or {}).items():
        _require(ap, _FIRE_AP_FIELDS, "fire message entry for transmitter %r" % name)
    for name, sta in (msg["stations"] or {}).items():
        _require(sta, _FIRE_STATION_FIELDS, "fire message entry for receiver %r" % name)
    return msg


def report_deadline_s(msg, margin_s=0.1):
    """How long a receiver should wait before answering with what it has.

    Covers the lead, the whole firing sequence, and a margin for the last frame to arrive and be
    counted. Derived from the message so a receiver never needs to be told separately, and so it
    cannot disagree with the controller about when the round ended.
    """
    return (msg["lead_us"] + (msg["n"] - 1) * msg["spacing_us"]) / 1e6 + margin_s


_STATUS_FIELDS = ("v", "run", "batch", "ap", "epoch", "shots", "error")


def build_status(run, batch, ap_name, epoch, shots, error=None, extra=None):
    """What a transmitter reports: the outcome of every shot, keyed by sequence number.

    Per-shot rather than a count, because a Co-SR measurement is only valid for the shots every
    participant fired, and that intersection cannot be recovered from totals.
    """
    msg = {"v": VERSION, "run": run, "batch": batch, "ap": ap_name, "epoch": epoch,
           "shots": dict((str(k), int(v)) for k, v in (shots or {}).items()),
           "error": error}
    if extra:
        msg.update(extra)
    return msg


def validate_status(msg):
    _require(msg, _STATUS_FIELDS, "status message")
    if msg["v"] != VERSION:
        raise ProtocolError("status message is version %r, expected %r" % (msg["v"], VERSION))
    return msg


def decode_shots(msg):
    """Shot outcomes with integer keys, as the accounting expects."""
    out = {}
    for k, v in (msg.get("shots") or {}).items():
        out[int(k)] = int(v)
    return out


_REPORT_ENVELOPE = ("v", "run", "batch", "station")


def build_report(run, batch, station_name, counts):
    """Wrap a receiver's counts with the round they answer."""
    msg = {"v": VERSION, "run": run, "batch": batch, "station": station_name}
    msg.update(counts)
    return msg


def validate_report(msg):
    _require(msg, _REPORT_ENVELOPE, "report message")
    if msg["v"] != VERSION:
        raise ProtocolError("report message is version %r, expected %r" % (msg["v"], VERSION))
    return msg


_HELLO_FIELDS = ("v", "role", "name", "epoch", "source_hash", "python")


def build_hello(role, name, epoch, source_hash, python):
    return {"v": VERSION, "role": role, "name": name, "epoch": epoch,
            "source_hash": source_hash, "python": python}


def validate_hello(msg):
    _require(msg, _HELLO_FIELDS, "hello message")
    return msg
