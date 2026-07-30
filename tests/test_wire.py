#!/usr/bin/env python3
"""cosr.wire: the firmware payload contract, and run-scoped sequence numbers.

The golden vector below is the whole point of this file. The WMI payload is sliced at fixed
offsets by the driver and the firmware synthesises frames from it, so a reordered or resized
field is not a crash -- it is a silently different experiment. Every result already recorded in
results/ was produced by the byte layout pinned here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
from cosr import wire as W

AP_MAC = "24:ec:99:95:22:2f"


class TestGoldenBlob(unittest.TestCase):
    """Byte-for-byte against a captured-by-hand vector, then against the shipped original."""

    # target=0x1122334455667788, rate=0x84 (HT MCS4), txp=40, nframes=5, stamp_off=24,
    # station_id=7, seq=1001 (0x03e9), frame_len=300 (0x012c), mac=24:ec:99:95:22:2f
    GOLDEN = (
        "8877665544332211"          # target_tsf, little-endian
        "84"                        # rate 0x80|4
        "28"                        # txpower 40
        "05"                        # nframes
        "18"                        # stamp_off 24
        "07"                        # station_id
        "e903"                      # seq 1001 LE
        "2c01"                      # frame_len 300 LE
        "08000000"                  # FC data + duration
        "ffffffffffff"              # addr1 broadcast
        "24ec9995222f"              # addr2 = AP
        "ffffffffffff"              # addr3 broadcast
        "0000"                      # sequence control
    )

    def test_golden_vector(self):
        got = W.blob(0x1122334455667788, 0x84, 40, 5, 24, 7, 1001, 300, AP_MAC)
        self.assertEqual("".join("%02x" % b for b in got), self.GOLDEN)

    def test_total_length_and_template_split(self):
        got = W.blob(0, 0x80, 40, 1, 24, 1, 0, 200, AP_MAC)
        self.assertEqual(len(got), 41, "17 fixed bytes + a 24-byte header template")
        self.assertEqual(len(got) - W.HEADER_BYTES, 24)

    def test_addr1_stays_broadcast(self):
        """Reception must not depend on a monitor honouring a unicast address filter."""
        tmpl = W.hdr_template(AP_MAC)
        self.assertEqual(tmpl[4:10], b"\xff" * 6)
        self.assertEqual(tmpl[16:22], b"\xff" * 6)

    def test_addr2_is_the_ap(self):
        self.assertEqual(W.hdr_template(AP_MAC)[10:16],
                         bytes([0x24, 0xec, 0x99, 0x95, 0x22, 0x2f]))

    def test_bad_mac_rejected(self):
        self.assertRaises(ValueError, W.hdr_template, "24:ec:99:95:22")
        self.assertRaises(ValueError, W.hdr_template, "nonsense")


class TestValidateBlob(unittest.TestCase):

    def test_accepts_the_normal_payload(self):
        W.validate_blob(W.blob(0, 0x80, 40, 1, 24, 1, 0, 200, AP_MAC), 24)

    def test_rejects_short_payload(self):
        self.assertRaises(ValueError, W.validate_blob, b"\x00" * 16)

    def test_rejects_oversized_template(self):
        payload = b"\x00" * (W.HEADER_BYTES + W.MAX_TEMPLATE + 1)
        self.assertRaises(ValueError, W.validate_blob, payload,
                          W.MAX_TEMPLATE + 1)

    def test_rejects_stamp_off_disagreeing_with_template(self):
        """A QoS header is 26 bytes; stamp_off=24 would then land inside the header and the
        receiver would read address bytes as a stamp."""
        payload = W.blob(0, 0x80, 40, 1, 26, 1, 0, 200, AP_MAC)   # 24-byte template
        self.assertRaises(ValueError, W.validate_blob, payload, 26)


class TestValidateShape(unittest.TestCase):
    """The buffer-pool bound nothing checked before."""

    def test_documented_shapes_fit(self):
        for n, l in ((1, 200), (5, 300), (10, 150), (15, 100)):
            W.validate_shape(n, l)

    def test_pool_overflow_rejected(self):
        for n, l in ((6, 300), (5, 400), (16, 100), (20, 150)):
            self.assertRaises(ValueError, W.validate_shape, n, l)

    def test_exactly_the_pool_is_allowed(self):
        W.validate_shape(5, 300)
        self.assertEqual(5 * 300, W.POOL_BYTES)

    def test_frame_too_short_for_stamp(self):
        self.assertRaises(ValueError, W.validate_shape, 1, 20)

    def test_nframes_zero_counts_as_one(self):
        W.validate_shape(0, 1500)


class TestSeqAllocator(unittest.TestCase):

    def test_monotone_and_non_overlapping(self):
        a = W.SeqAllocator()
        self.assertEqual(a.alloc(5), 0)
        self.assertEqual(a.alloc(5), 5)
        self.assertEqual(a.alloc(1), 10)

    def test_retry_cannot_reuse_an_aborted_range(self):
        """R3: a retry must get fresh numbers, or `fired` doubles while unique rx does not."""
        a = W.SeqAllocator()
        first = a.alloc(20)                 # batch aborts after a few shots
        retry = a.alloc(20)
        self.assertGreaterEqual(retry, first + 20)
        self.assertEqual(set(range(first, first + 20)) & set(range(retry, retry + 20)), set())

    def test_never_repeats_over_a_long_run(self):
        a = W.SeqAllocator()
        seen = set()
        for _ in range(20000):
            s = a.alloc(1)
            self.assertNotIn(s, seen)
            seen.add(s)

    def test_wire_folds_to_16_bits(self):
        self.assertEqual(W.SeqAllocator.wire(0), 0)
        self.assertEqual(W.SeqAllocator.wire(65535), 65535)
        self.assertEqual(W.SeqAllocator.wire(65536), 0)
        self.assertEqual(W.SeqAllocator.wire(65537), 1)
        self.assertEqual(W.SeqAllocator.wire(70000), 70000 - 65536)

    def test_counter_survives_the_wire_wrap(self):
        """The run counter is unbounded; only its projection onto the wire wraps."""
        a = W.SeqAllocator(start=65530)
        vals = [a.alloc(1) for _ in range(10)]
        self.assertEqual(vals, list(range(65530, 65540)))
        self.assertEqual([W.SeqAllocator.wire(v) for v in vals[-4:]], [0, 1, 2, 3])

    def test_retention_window_must_not_alias_a_wrap(self):
        W.SeqAllocator.check_retention(4096)
        W.SeqAllocator.check_retention(32767 - 1)
        self.assertRaises(ValueError, W.SeqAllocator.check_retention, 32768)
        self.assertRaises(ValueError, W.SeqAllocator.check_retention, 100000)

    def test_alloc_rejects_zero(self):
        self.assertRaises(ValueError, W.SeqAllocator().alloc, 0)


class TestStampCodec(unittest.TestCase):

    def test_round_trip_with_the_parser(self):
        from cosr import radiotap as R
        for sid, seq, idx in ((1, 0, 0), (7, 1001, 4), (255, 65535, 14)):
            stamp = W.encode_stamp(sid, seq, idx)
            # place it where the firmware would: radiotap_len + stamp_off
            buf = b"\x00" * 8 + b"\x00" * 24 + stamp
            self.assertEqual(R.read_stamp(buf, 8, 24), (sid, seq, idx))

    def test_encodes_seq_little_endian(self):
        self.assertEqual(W.encode_stamp(7, 1001, 4), b"\xc0\x5a\x07\xe9\x03\x04")

    def test_matches_real_captured_frames(self):
        """The encoder must reproduce the stamps actually seen on air."""
        from cosr import radiotap as R
        from tests.test_radiotap import load_fixture
        frames, _ = load_fixture("stamped_ampdu")
        for buf in frames:
            f = R.parse(buf, stamp_off=W.STAMP_OFFSET)
            sid, seq, idx = f["stamp"]
            expect = W.encode_stamp(sid, seq, idx)
            at = f["radiotap_len"] + W.STAMP_OFFSET
            self.assertEqual(bytes(buf[at:at + W.STAMP_LEN]), expect)

if __name__ == "__main__":
    unittest.main(verbosity=2)
