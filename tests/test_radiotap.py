#!/usr/bin/env python3
"""cosr.radiotap against real captures, with an independent parser as the ground truth.

The fixtures were recorded from a raw capture socket; the `.expect.json` beside each one was
produced by a different implementation, NOT by this parser, so a shared misconception cannot
make the test pass.
"""
import json
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import radiotap as R

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
STAMP_OFF = 24          # where the stamp sits after the 802.11 header


def read_pcap(path):
    """Frames from a little-endian pcap. Test-local: the agent reads a socket, not a file."""
    with open(path, "rb") as fh:
        g = fh.read(24)
        magic, _vmaj, _vmin, _tz, _sf, _snap, network = struct.unpack("<IHHiIII", g)
        assert magic == 0xa1b2c3d4, "fixture is not a little-endian pcap"
        assert network == 127, "fixture link type is not IEEE802_11_RADIOTAP"
        out = []
        while True:
            h = fh.read(16)
            if len(h) < 16:
                break
            _ts, _tu, il, _ol = struct.unpack("<IIII", h)
            out.append(fh.read(il))
        return out


def load_fixture(name):
    frames = read_pcap(os.path.join(FIXTURES, name + ".pcap"))
    with open(os.path.join(FIXTURES, name + ".expect.json")) as fh:
        expect = json.load(fh)["frames"]
    return frames, expect


class TestAgainstTshark(unittest.TestCase):
    """Every decoded field must match tshark's, frame for frame."""

    def _check(self, name):
        frames, expect = load_fixture(name)
        self.assertEqual(len(frames), len(expect), "%s: frame count" % name)
        for i, (buf, exp) in enumerate(zip(frames, expect)):
            got = R.parse(buf, stamp_off=STAMP_OFF)
            self.assertIsNotNone(got, "%s frame %d failed to parse" % (name, i + 1))
            for key, ekey in (("radiotap_len", "radiotap_len"), ("tsft", "mactime"),
                              ("signal_dbm", "signal_dbm"), ("freq_mhz", "freq_mhz"),
                              ("fc_type", "fc_type"), ("fc_subtype", "fc_subtype"),
                              ("sa", "sa")):
                self.assertEqual(got[key], exp[ekey],
                                 "%s frame %d field %s: ours=%r tshark=%r"
                                 % (name, i + 1, key, got[key], exp[ekey]))

    def test_beacons(self):
        self._check("beacons")

    def test_stamped_single(self):
        self._check("stamped_single")

    def test_stamped_ampdu(self):
        self._check("stamped_ampdu")

    def test_variants(self):
        self._check("variants")

    def test_mixed(self):
        """60 consecutive frames of whatever was on the air -- mgmt, ctrl, data, foreign."""
        self._check("mixed")

    def test_signed_signal_is_negative(self):
        """A signal byte must be read as SIGNED: -53 unpacked as unsigned becomes 203."""
        _frames, expect = load_fixture("beacons")
        sigs = [e["signal_dbm"] for e in expect if e["signal_dbm"] is not None]
        self.assertTrue(sigs)
        self.assertTrue(all(-100 < s < 0 for s in sigs), sigs)


class TestStamp(unittest.TestCase):

    def test_single_frame_stamp(self):
        """nframes=1: idx is always 0 and seq increments per shot."""
        frames, _ = load_fixture("stamped_single")
        stamps = [R.parse(b, stamp_off=STAMP_OFF)["stamp"] for b in frames]
        self.assertTrue(all(s is not None for s in stamps), stamps)
        self.assertTrue(all(s[2] == 0 for s in stamps), "idx must be 0 for nframes=1")
        self.assertEqual(sorted(set(s[0] for s in stamps)), [1], "station_id")
        self.assertEqual(sorted(set(s[1] for s in stamps)), [0, 1, 2, 3, 4])

    def test_ampdu_all_subframes_recovered(self):
        """5x300 A-MPDU: every subframe idx 0..4 is present for each fired shot.

        This is the empirical answer to 'does RX dedup drop subframes 1..4 because they share
        seqctl' -- it does not; all five arrive and all five are counted.
        """
        frames, _ = load_fixture("stamped_ampdu")
        stamps = [R.parse(b, stamp_off=STAMP_OFF)["stamp"] for b in frames]
        self.assertTrue(all(s is not None for s in stamps))
        by_seq = {}
        for sid, seq, idx in stamps:
            by_seq.setdefault(seq, set()).add(idx)
        self.assertTrue(by_seq)
        for seq, idxs in by_seq.items():
            self.assertEqual(idxs, set(range(5)), "seq %d subframes %s" % (seq, sorted(idxs)))

    def test_ampdu_subframes_share_one_mactime(self):
        """One PPDU: the aggregate's subframes all report the same radiotap mactime, which is
        why only idx 0 may be used for timing."""
        frames, _ = load_fixture("stamped_ampdu")
        got = [R.parse(b, stamp_off=STAMP_OFF) for b in frames]
        by_seq = {}
        for f in got:
            by_seq.setdefault(f["stamp"][1], set()).add(f["tsft"])
        for seq, times in by_seq.items():
            self.assertEqual(len(times), 1, "seq %d spans mactimes %s" % (seq, times))

    def test_labelled_variants(self):
        """The four isolation shots: ungated/gated x HT/legacy, labelled by seq 1001..1004."""
        frames, _ = load_fixture("variants")
        seqs = sorted(R.parse(b, stamp_off=STAMP_OFF)["stamp"][1] for b in frames)
        self.assertEqual(seqs, [1001, 1002, 1003, 1004])

    def test_beacons_have_no_stamp(self):
        frames, _ = load_fixture("beacons")
        for b in frames:
            self.assertIsNone(R.parse(b, stamp_off=STAMP_OFF)["stamp"])

    def test_magic_is_verified_not_searched(self):
        """A hex-search for 'c05a' can match at an ODD nibble and yield well-formed garbage.

        Build a frame whose bytes contain 0x?C 0x05 0xA? before the stamp offset, and whose
        stamp offset holds no magic. The old `hexstr.find("c05a")` approach matches; reading a
        fixed offset and checking the magic does not.
        """
        rt = struct.pack("<BBHI", 0, 0, 8, 0)             # minimal radiotap, no fields
        hdr = bytearray(24)
        hdr[0] = 0x08                                     # data frame
        hdr[4:10] = b"\x1c\x05\xa7\x00\x00\x00"           # nibble-shifted 'c05a' inside addr1
        body = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
        buf = rt + bytes(hdr) + body

        hexstr = "".join("%02x" % b for b in buf)
        self.assertIn("c05a", hexstr, "fixture must contain the odd-nibble trap")
        self.assertIsNone(R.read_stamp(buf, 8, STAMP_OFF),
                          "magic must be verified at the known offset, never searched for")

    def test_truncated_stamp_returns_none(self):
        rt = struct.pack("<BBHI", 0, 0, 8, 0)
        buf = rt + bytes(24) + b"\xc0\x5a\x01"            # stamp cut short
        self.assertIsNone(R.read_stamp(buf, 8, STAMP_OFF))


class TestRssiPolicy(unittest.TestCase):
    """The A-MPDU PHY-stats artefact, and the 44.8 dB error it caused."""

    def test_only_last_subframe_carries_phy_stats(self):
        frames, _ = load_fixture("stamped_ampdu")
        got = [R.parse(b, stamp_off=STAMP_OFF) for b in frames]
        for f in got:
            if f["stamp"][2] == 4:
                self.assertNotEqual(f["signal_dbm"], 0, "last subframe must carry a signal")
            else:
                self.assertEqual(f["signal_dbm"], 0,
                                 "subframe idx %d unexpectedly carried PHY stats"
                                 % f["stamp"][2])

    def test_rssi_ignores_subframes_without_phy_stats(self):
        frames, _ = load_fixture("stamped_ampdu")
        got = [R.parse(b, stamp_off=STAMP_OFF) for b in frames]

        naive = sum(f["signal_dbm"] for f in got) / float(len(got))   # counting the absent ones
        self.assertAlmostEqual(naive, -11.2, places=1)

        self.assertAlmostEqual(R.rssi_dbm(got), -56.0, places=1)
        self.assertGreater(abs(naive - R.rssi_dbm(got)), 40.0,
                           "the bug this guards against is a >40 dB error")

    def test_rssi_unaffected_for_single_frames(self):
        frames, expect = load_fixture("stamped_single")
        got = [R.parse(b, stamp_off=STAMP_OFF) for b in frames]
        naive = sum(e["signal_dbm"] for e in expect) / float(len(expect))
        self.assertAlmostEqual(R.rssi_dbm(got), round(naive, 1), places=1)

    def test_rssi_none_when_nothing_usable(self):
        self.assertIsNone(R.rssi_dbm([]))
        self.assertIsNone(R.rssi_dbm([{"signal_dbm": None}, {"signal_dbm": 0}]))

    def test_usable_signal(self):
        self.assertIsNone(R.usable_signal(None))
        self.assertIsNone(R.usable_signal(0))
        self.assertEqual(R.usable_signal(-56), -56)


class TestMalformed(unittest.TestCase):

    def test_too_short(self):
        for buf in (b"", b"\x00", b"\x00\x00\x08"):
            self.assertIsNone(R.parse(buf))

    def test_bad_version(self):
        self.assertIsNone(R.parse(struct.pack("<BBHI", 1, 0, 8, 0) + bytes(24)))

    def test_it_len_past_buffer(self):
        self.assertIsNone(R.parse(struct.pack("<BBHI", 0, 0, 999, 0) + bytes(24)))

    def test_it_len_too_small(self):
        self.assertIsNone(R.parse(struct.pack("<BBHI", 0, 0, 4, 0) + bytes(24)))

    def test_runaway_ext_chain(self):
        """A presence word with the ext bit set forever must not loop or over-read."""
        buf = struct.pack("<BBH", 0, 0, 8) + struct.pack("<I", 1 << 31)
        self.assertIsNone(R.parse(buf))

    def test_no_80211_header(self):
        self.assertIsNone(R.parse(struct.pack("<BBHI", 0, 0, 8, 0) + b"\x08\x00"))

    def test_control_frame_decodes_without_addresses(self):
        """An ACK is 14 bytes of 802.11 and carries addr1 only. It must still decode -- the
        station agent counts non-matching frames as proof the capture is alive -- but with no
        SA and no hdrlen, since a control header has no body to offset into."""
        frames, expect = load_fixture("mixed")
        ctl = [(b, e) for b, e in zip(frames, expect) if e["fc_type"] == R.FTYPE_CTL]
        self.assertTrue(ctl, "fixture must contain a control frame")
        for buf, exp in ctl:
            got = R.parse(buf, stamp_off=STAMP_OFF)
            self.assertIsNotNone(got)
            self.assertEqual(got["fc_type"], R.FTYPE_CTL)
            self.assertIsNone(got["hdrlen"])
            self.assertIsNone(got["stamp"])
            self.assertEqual(got["sa"], exp["sa"])
            self.assertIsNotNone(got["signal_dbm"], "radiotap still decodes for control")

    def test_missing_fields_are_none_not_zero(self):
        """Absent radiotap fields must be None, so 'unknown' never reads as a real value."""
        got = R.parse(struct.pack("<BBHI", 0, 0, 8, 0) + bytes(24))
        self.assertIsNotNone(got)
        self.assertIsNone(got["tsft"])
        self.assertIsNone(got["signal_dbm"])
        self.assertIsNone(got["freq_mhz"])
        self.assertIsNone(got["mcs"])


class TestHeaderLen(unittest.TestCase):
    """hdrlen must track ieee80211_anyhdrsize(), which is what the firmware uses to place the
    stamp -- a disagreement here means the stamp is read from the wrong offset."""

    def _fc(self, fc0, fc1):
        return R.hdrlen_80211(bytes([fc0, fc1]) + bytes(30), 0)

    def test_plain_data(self):
        self.assertEqual(self._fc(0x08, 0x00), 24)

    def test_qos_data(self):
        self.assertEqual(self._fc(0x88, 0x00), 26)

    def test_four_address(self):
        self.assertEqual(self._fc(0x08, 0x03), 30)

    def test_qos_four_address(self):
        self.assertEqual(self._fc(0x88, 0x03), 32)

    def test_management_is_24(self):
        self.assertEqual(self._fc(0x80, 0x00), 24)

    def test_our_frames_are_plain_data_so_stamp_off_24_is_the_body(self):
        """The host ships stamp_off=24 assuming a 24-byte header. Confirm the real frames the
        firmware emits are plain non-QoS data, so that assumption holds on air."""
        frames, _ = load_fixture("stamped_single")
        for b in frames:
            f = R.parse(b, stamp_off=STAMP_OFF)
            self.assertEqual(f["fc_type"], R.FTYPE_DATA)
            self.assertEqual(f["hdrlen"], 24)

if __name__ == "__main__":
    unittest.main(verbosity=2)
