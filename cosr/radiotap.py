"""Radiotap + 802.11 + Co-SR stamp parsing, pure stdlib.

A receiver reads frames straight off an AF_PACKET socket and parses them here, which is what
lets the count be paired with that socket's own drop counters: an undercount then cannot pass
as a real zero.

Two layers, kept apart so each can be pinned by tests:

  parse(buf)        FAITHFUL decode. Reports exactly what radiotap said, including a signal
                    of 0 dBm. It applies no policy.
  rssi_dbm(frames)  POLICY. Turns a set of frames into one RSSI, discarding the A-MPDU
                    subframes that carry no PHY stats (see SIGNAL_NO_PHY_STATS).

Runs on Python 3.4 (the AP image) as well as 3.7+ (the stations): no f-strings, no typing,
stdlib only.
"""
import struct

# ---------------------------------------------------------------- radiotap field table

# Bit -> (alignment, size) from the radiotap spec. Alignment is relative to the START of the
# radiotap header, which is why _walk tracks an absolute offset rather than a relative one.
_FIELDS = {
    0:  (8, 8),    # TSFT (u64, microseconds, the MAC's own clock)
    1:  (1, 1),    # Flags
    2:  (1, 1),    # Rate
    3:  (2, 4),    # Channel: u16 freq + u16 flags
    4:  (1, 2),    # FHSS
    5:  (1, 1),    # dBm antenna signal (SIGNED)
    6:  (1, 1),    # dBm antenna noise (signed)
    7:  (2, 2),    # Lock quality
    8:  (2, 2),    # TX attenuation
    9:  (2, 2),    # dB TX attenuation
    10: (1, 1),    # dBm TX power (signed)
    11: (1, 1),    # Antenna
    12: (1, 1),    # dB antenna signal
    13: (1, 1),    # dB antenna noise
    14: (2, 2),    # RX flags
    15: (2, 2),    # TX flags
    16: (1, 1),    # RTS retries
    17: (1, 1),    # Data retries
    18: (4, 8),    # XChannel
    19: (1, 3),    # MCS: known, flags, index
    20: (4, 8),    # A-MPDU status
    21: (2, 12),   # VHT
    22: (8, 12),   # Timestamp
    23: (2, 12),   # HE
    24: (2, 12),   # HE-MU
    25: (2, 6),    # HE-MU-other-user
    26: (1, 1),    # 0-length PSDU
    27: (2, 4),    # L-SIG
}
_BIT_TSFT = 0
_BIT_CHANNEL = 3
_BIT_SIGNAL = 5
_BIT_MCS = 19
_BIT_NS_RADIOTAP = 29    # namespace switches: everything after them is not in _FIELDS' space
_BIT_NS_VENDOR = 30
_BIT_EXT = 31

FTYPE_MGT = 0
SUBTYPE_BEACON = 8

# Only the last MPDU of an A-MPDU carries the PPDU's PHY stats; every earlier subframe reports a
# signal of exactly 0, and including those biases the mean by tens of decibels, worse the larger
# the aggregate. A receiver can never legitimately report 0 dBm -- that is a milliwatt arriving
# at the antenna -- so 0 unambiguously means "no PHY stats" rather than a level.
SIGNAL_NO_PHY_STATS = 0

# 802.11 frame types
FTYPE_CTL = 1
FTYPE_DATA = 2

STAMP_MAGIC = b"\xc0\x5a"
STAMP_LEN = 6


def _walk(buf):
    """Decode the radiotap header. Returns (radiotap_len, {bit: value}) or None if malformed.

    Stops at a namespace switch or an unknown bit: every field used here (TSFT, channel, signal,
    MCS) lives in the first presence word, and guessing the length of an unknown field would
    silently misalign everything after it.
    """
    if len(buf) < 8:
        return None
    # it_version, it_pad, it_len, then >=1 presence words
    version, _pad, it_len = struct.unpack_from("<BBH", buf, 0)
    if version != 0 or it_len < 8 or it_len > len(buf):
        return None

    words = []
    off = 4
    while True:
        if off + 4 > it_len:
            return None                     # presence words run past the header
        w = struct.unpack_from("<I", buf, off)[0]
        words.append(w)
        off += 4
        if not (w & (1 << _BIT_EXT)):
            break
        if len(words) > 8:
            return None                     # runaway ext chain

    pos = off                               # field data starts after ALL presence words
    present = words[0]
    vals = {}
    for bit in range(0, 29):
        if not (present & (1 << bit)):
            continue
        if bit not in _FIELDS:
            break
        align, size = _FIELDS[bit]
        rem = pos % align
        if rem:
            pos += align - rem
        if pos + size > it_len:
            break                           # truncated header: keep what was decoded
        if bit == _BIT_TSFT:
            vals[bit] = struct.unpack_from("<Q", buf, pos)[0]
        elif bit == _BIT_SIGNAL:
            vals[bit] = struct.unpack_from("<b", buf, pos)[0]      # SIGNED
        elif bit == _BIT_CHANNEL:
            vals[bit] = struct.unpack_from("<H", buf, pos)[0]      # freq MHz
        elif bit == _BIT_MCS:
            known, _flags, index = struct.unpack_from("<BBB", buf, pos)
            vals[bit] = index if (known & 0x02) else None          # bit1 = index known
        pos += size
    if present & ((1 << _BIT_NS_RADIOTAP) | (1 << _BIT_NS_VENDOR)):
        pass                                # namespace switch: already stopped above
    return it_len, vals


def hdrlen_80211(buf, off):
    """802.11 MAC header length for the frame at `off`: 24, +6 for a 4-address frame,
    +2 for QoS. Mirrors ieee80211_anyhdrsize(), which is what the firmware uses to place
    the body -- so if this disagrees, the stamp offset disagrees too.

    Returns None for control frames: their headers are 10-16 bytes with a layout that varies
    per subtype, and none of them ever carries a stamp. Reporting 24 there would imply a body
    offset that does not exist.
    """
    if len(buf) < off + 2:
        return None
    fc0 = buf[off]
    fc1 = buf[off + 1]
    ftype = (fc0 >> 2) & 0x3
    subtype = (fc0 >> 4) & 0xF
    if ftype == FTYPE_CTL:
        return None
    n = 24
    if ftype == FTYPE_DATA:
        if (fc1 & 0x03) == 0x03:            # ToDS and FromDS: addr4 present
            n += 6
        if subtype & 0x08:                  # QoS data
            n += 2
    return n


def _mac(buf, off):
    if len(buf) < off + 6:
        return None
    return "%02x:%02x:%02x:%02x:%02x:%02x" % tuple(buf[off:off + 6])


def parse(buf, stamp_off=None):
    """Faithful decode of one captured frame.

    `stamp_off` is the byte offset of the Co-SR stamp inside the MPDU, i.e. the same
    `stamp_off` shipped in the WMI command (the host uses 24 = immediately after a 24-byte
    header). Measured from the MPDU start, NOT from the body -- the firmware writes the stamp
    at that offset into the frame it synthesises.

    Returns None if the frame is not decodable as radiotap + 802.11. Otherwise a dict:
      radiotap_len, tsft, signal_dbm, freq_mhz, mcs, fc_type, fc_subtype,
      sa, da, bssid, hdrlen, stamp -> (station_id, seq, idx) or None
    """
    rt = _walk(buf)
    if rt is None:
        return None
    it_len, vals = rt
    # Only frame-control + duration are required. A control frame (ACK is 14 bytes including
    # FCS, and carries addr1 alone) must still decode: the station agent counts non-matching
    # frames as proof the capture is alive, so silently dropping them would understate that.
    if len(buf) < it_len + 4:
        return None
    hl = hdrlen_80211(buf, it_len)

    fc0 = buf[it_len]
    out = {
        "radiotap_len": it_len,
        "tsft": vals.get(_BIT_TSFT),
        "signal_dbm": vals.get(_BIT_SIGNAL),
        "freq_mhz": vals.get(_BIT_CHANNEL),
        "mcs": vals.get(_BIT_MCS),
        "fc_type": (fc0 >> 2) & 0x3,
        "fc_subtype": (fc0 >> 4) & 0xF,
        "da": _mac(buf, it_len + 4),
        "sa": _mac(buf, it_len + 10),
        "bssid": _mac(buf, it_len + 16),
        "hdrlen": hl,
        "stamp": None,
        "beacon_tsf": None,
    }
    # A beacon carries the sender's own clock in the first eight bytes of its body. Paired with
    # the receive time in this same frame, one observation gives both halves of a relation
    # between the sender's clock and this receiver's.
    if hl is not None and out["fc_type"] == FTYPE_MGT and out["fc_subtype"] == SUBTYPE_BEACON:
        body = it_len + hl
        if len(buf) >= body + 8:
            out["beacon_tsf"] = _le64(buf, body)
    if stamp_off is not None:
        out["stamp"] = read_stamp(buf, it_len, stamp_off)
    return out


def _le64(buf, off):
    v = 0
    for i in range(8):
        v |= buf[off + i] << (8 * i)
    return v


def read_stamp(buf, radiotap_len, stamp_off):
    """(station_id, seq, idx) from the stamp at a KNOWN offset, or None.

    The magic is verified rather than searched for. Searching a hex rendering for the marker can
    match at an odd nibble index -- bytes like 0x?C 0x05 0xA? -- after which every field slice is
    nibble-shifted garbage that still parses as a valid integer. Reading a fixed offset and
    checking the marker cannot do that.
    """
    p = radiotap_len + stamp_off
    if p + STAMP_LEN > len(buf):
        return None
    if buf[p:p + 2] != STAMP_MAGIC:
        return None
    return (buf[p + 2], buf[p + 3] | (buf[p + 4] << 8), buf[p + 5])


def usable_signal(sig):
    """The signal in dBm if it carries real PHY stats, else None. See SIGNAL_NO_PHY_STATS."""
    if sig is None or sig == SIGNAL_NO_PHY_STATS:
        return None
    return sig


def rssi_dbm(frames):
    """Mean RSSI (dBm) over frames that actually carry PHY stats, or None.

    A mean of dBm values. What matters is WHICH frames are included: only the last subframe of
    an aggregate carries PHY stats, so with more than one frame per shot this is effectively one
    sample per aggregate, because that is all the hardware reports.
    """
    sigs = []
    for f in frames:
        s = usable_signal(f.get("signal_dbm") if isinstance(f, dict) else f)
        if s is not None:
            sigs.append(s)
    if not sigs:
        return None
    return round(float(sum(sigs)) / len(sigs), 1)
