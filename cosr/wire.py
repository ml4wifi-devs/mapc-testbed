"""The WMI gated-TX payload, the frame stamp, and run-scoped sequence numbers.

The byte layout is a hard contract with the firmware: the driver slices the debugfs write at
fixed offsets and the firmware synthesises each subframe from it, so a field reordered here is
misread rather than rejected. `tests/test_wire.py` pins it against a golden vector.

    offset  size  field
    0       8     target_tsf, little-endian. 0 => fire immediately, ungated.
    8       1     rate        HAL rateCode; 0 => firmware min-rate default, HT = 0x80|mcs
    9       1     txpower     descriptor 0..63; reaches the descriptor but the AR9271 RF
                              clamps it, so real power is set out-of-band (see cosr.nl80211)
    10      1     nframes     A-MPDU subframes; 0 or 1 => a single frame
    11      1     stamp_off   byte offset of the stamp inside each synthesised MPDU
    12      1     station_id  stamped, identifies the intended receiver
    13      2     seq         little-endian, stamped; the shot index within a run
    15      2     frame_len   little-endian here; the driver re-emits it big-endian
    17      N     802.11 header template, N <= 64

Python 3.4 compatible: this module is deployed to the AP agents.
"""

# --- driver/firmware limits. Exceeding any of these is rejected by the driver or silently
# --- corrupts the frame, so they are checked here rather than discovered on the rig.
HEADER_BYTES = 17          # fixed part before the template; driver rejects count < 17
MAX_TEMPLATE = 64          # driver buffer is buff[17 + 64]
MAX_LEAD_US = 600000       # firmware COSR_LEAD_MAX_US; beyond this it returns TOOFAR

# POOL_ID_ATTACKS is 1500 B total, so an aggregate must fit: 5x300, 10x150, 15x100.
# An over-large shape exhausts the transmit pool, which the radio reports only as a no-buffer
# result at transmit time -- too late and too vague to act on.
POOL_BYTES = 1500

STAMP_MAGIC = b"\xc0\x5a"
STAMP_LEN = 6
STAMP_OFFSET = 24          # immediately after a 24-byte (non-QoS, 3-address) header

SEQ_WIRE_BITS = 16
SEQ_WIRE_MODULUS = 1 << SEQ_WIRE_BITS


def hdr_template(ap_mac):
    """The 24-byte 802.11 header the firmware prepends to every synthesised subframe.

    addr1 stays broadcast. Encoding the station id into a unicast destination instead adds
    nothing -- a receiver identifies these frames by addr2 and the stamp marker -- while making
    reception depend on the capture being fully promiscuous, and tying stamp_off to the header
    length.
    """
    mac_bytes = bytes(int(x, 16) for x in ap_mac.split(":"))
    if len(mac_bytes) != 6:
        raise ValueError("bad MAC %r" % (ap_mac,))
    out = bytearray()
    out += bytes([0x08, 0x00, 0x00, 0x00])   # frame control (data) + duration
    out += bytes([0xff] * 6)                 # addr1 destination: broadcast
    out += mac_bytes                         # addr2 source: this AP
    out += bytes([0xff] * 6)                 # addr3 BSSID: broadcast
    out += bytes([0x00, 0x00])               # sequence control
    return bytes(out)


def validate_shape(nframes, frame_len):
    """Raise if an A-MPDU shape cannot fit the firmware buffer pool."""
    n = max(1, int(nframes))
    total = n * int(frame_len)
    if total > POOL_BYTES:
        raise ValueError(
            "A-MPDU shape %dx%d = %d B exceeds the %d B POOL_ID_ATTACKS pool "
            "(use 5x300, 10x150 or 15x100); the firmware would return NOBF"
            % (n, frame_len, total, POOL_BYTES))
    if int(frame_len) < STAMP_OFFSET + STAMP_LEN:
        raise ValueError("frame_len %d is too short to hold the stamp at offset %d"
                         % (frame_len, STAMP_OFFSET))


def blob(target, rate, txp, nframes, stamp_off, station_id, seq, frame_len, mac):
    """Build the gated-transmit command payload."""
    out = bytearray()
    for i in range(8):
        out.append((target >> (8 * i)) & 0xff)
    out += bytes([rate & 0xff, txp & 0xff, nframes & 0xff,
                  stamp_off & 0xff, station_id & 0xff])
    out += bytes([seq & 0xff, (seq >> 8) & 0xff,
                  frame_len & 0xff, (frame_len >> 8) & 0xff])
    out += hdr_template(mac)
    return bytes(out)


def validate_blob(payload, stamp_off=STAMP_OFFSET):
    """Raise if a payload would be rejected by the driver or misparsed by the firmware."""
    n = len(payload)
    if n < HEADER_BYTES:
        raise ValueError("payload %d B is below the driver's %d B minimum" % (n, HEADER_BYTES))
    tmpl = n - HEADER_BYTES
    if tmpl > MAX_TEMPLATE:
        raise ValueError("header template %d B exceeds the driver's %d B buffer"
                         % (tmpl, MAX_TEMPLATE))
    # The firmware derives the body offset from the template's frame control via
    # ieee80211_anyhdrsize(); if stamp_off disagrees with the template length the stamp lands
    # inside the header and the receiver reads address bytes as a stamp.
    if stamp_off != tmpl:
        raise ValueError("stamp_off %d must equal the header template length %d"
                         % (stamp_off, tmpl))


def rate_for_mcs(mcs):
    """HAL rate code for an HT modulation index."""
    return 0x80 | (int(mcs) & 0x7f)


def encode_stamp(station_id, seq, idx):
    """The 6 bytes the firmware writes at stamp_off. Inverse of cosr.radiotap.read_stamp."""
    return STAMP_MAGIC + bytes([station_id & 0xff,
                                seq & 0xff, (seq >> 8) & 0xff,
                                idx & 0xff])


class SeqAllocator:
    """Run-scoped, strictly monotone shot sequence numbers.

    Two failure modes this exists to prevent, both of which produce plausible wrong numbers:

    * **Reuse across a retry.** Re-firing an aborted batch with the same starting seq doubles
      the `fired` denominator while the receiver's unique-(seq,idx) count stays put, so
      delivery reads about half of the truth. Every allocation advances, so a retry can only
      ever get fresh numbers.
    * **Reuse against a resident receiver.** The station keeps a bounded map keyed by
      (run_id, wire seq). If a wire value still resident there is reused, frames that were
      never sent get counted -- the one failure mode that *inflates* delivery to 100%.
      The wire field is 16 bits, so the receiver's retention window must stay far below half
      the modulus; `check_retention` enforces that.
    """

    def __init__(self, start=0):
        self._next = int(start)

    def alloc(self, n=1):
        """Reserve `n` consecutive numbers and return the first."""
        if n < 1:
            raise ValueError("n must be >= 1")
        base = self._next
        self._next += n
        return base

    @staticmethod
    def wire(seq):
        """Fold an unbounded run sequence onto the 16-bit wire field."""
        return int(seq) % SEQ_WIRE_MODULUS

    @staticmethod
    def check_retention(window):
        """Raise if a receiver retention window is large enough to alias a 16-bit wrap."""
        if window >= SEQ_WIRE_MODULUS // 2:
            raise ValueError(
                "retention window %d aliases the %d-value wire sequence space; keep it well "
                "below %d or an old shot's frames will be counted as a new one's"
                % (window, SEQ_WIRE_MODULUS, SEQ_WIRE_MODULUS // 2))
