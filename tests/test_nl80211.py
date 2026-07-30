#!/usr/bin/env python3
"""cosr.nl80211: message construction for the wireless configuration interface.

The encoding is checked here; the exchange itself needs a radio and is exercised on one.
"""
import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import nl80211 as N


class TestAttributes(unittest.TestCase):

    def test_length_and_type_precede_the_payload(self):
        a = N._attr(3, struct.pack("=I", 7))
        length, kind = struct.unpack_from("=HH", a, 0)
        self.assertEqual((length, kind), (8, 3))
        self.assertEqual(struct.unpack_from("=I", a, 4)[0], 7)

    def test_payload_is_padded_to_a_four_byte_boundary(self):
        for n in range(1, 9):
            a = N._attr(1, b"x" * n)
            self.assertEqual(len(a) % 4, 0, "payload of %d bytes was left unaligned" % n)
            self.assertEqual(struct.unpack_from("=H", a, 0)[0], 4 + n,
                             "the declared length must exclude the padding")

    def test_round_trip(self):
        blob = N._attr(1, b"abc") + N._attr(2, struct.pack("=I", 42))
        got = N._parse_attrs(blob)
        self.assertEqual(got[1], b"abc")
        self.assertEqual(struct.unpack_from("=I", got[2], 0)[0], 42)

    def test_truncated_input_is_not_fatal(self):
        self.assertEqual(N._parse_attrs(b"\x08\x00"), {})
        self.assertEqual(N._parse_attrs(b"\xff\xff\x01\x00ab"), {})

    def test_zero_length_attribute_does_not_loop(self):
        self.assertEqual(N._parse_attrs(b"\x00\x00\x01\x00"), {})


class TestSetter(unittest.TestCase):

    def test_unavailable_platform_yields_no_setter(self):
        if hasattr(socket, "AF_NETLINK"):
            self.skipTest("this platform provides the interface")
        self.assertIsNone(N.make_power_setter())

    def test_interface_index_is_read_from_the_kernel(self):
        if not os.path.isdir("/sys/class/net"):
            self.skipTest("no kernel interface listing here")
        names = os.listdir("/sys/class/net")
        if not names:
            self.skipTest("no interfaces")
        self.assertIsInstance(N.if_nametoindex(names[0]), int)

    def test_unknown_interface_raises(self):
        self.assertRaises((IOError, OSError), N.if_nametoindex, "definitely-not-an-interface")

if __name__ == "__main__":
    unittest.main(verbosity=2)
