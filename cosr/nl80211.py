"""Set a radio's transmit power over the kernel's wireless configuration interface.

The same operation the command-line tool performs, issued directly. Starting a process for it
costs tens of milliseconds on the transmitter images, once per round per radio; issuing it here
is cheap enough to re-assert the power every round. That matters because remembering the value
is unsafe: bringing the interface down, or restarting the access point, reverts the power
silently, and a remembered value would then be wrong for every later round with no indication.

Note that success here means the request was accepted, not that the radio reached the requested
level: regulatory limits are applied without reporting anything. The only way to confirm the
level is to observe the change at a receiver.

Python 3.4 and standard library only.
"""
import os
import socket
import struct

NETLINK_GENERIC = 16
GENL_ID_CTRL = 16

CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2

NL80211_CMD_SET_WIPHY = 2
NL80211_ATTR_IFINDEX = 3
NL80211_ATTR_WIPHY_TX_POWER_SETTING = 0x61
NL80211_ATTR_WIPHY_TX_POWER_LEVEL = 0x62

NL80211_TX_POWER_FIXED = 2

NLM_F_REQUEST = 0x01
NLM_F_ACK = 0x04
NLMSG_ERROR = 0x02
NLMSG_DONE = 0x03

_HDR = struct.Struct("=IHHII")        # length, type, flags, sequence, port
_GENL = struct.Struct("=BBH")         # command, version, reserved


class NetlinkError(OSError):
    pass


def _attr(kind, payload):
    """One attribute: length and type, then the payload padded to a four-byte boundary."""
    body = struct.pack("=HH", 4 + len(payload), kind) + payload
    pad = (4 - len(body) % 4) % 4
    return body + b"\x00" * pad


def _parse_attrs(buf):
    out = {}
    i = 0
    while i + 4 <= len(buf):
        length, kind = struct.unpack_from("=HH", buf, i)
        if length < 4 or i + length > len(buf):
            break
        out[kind] = buf[i + 4:i + length]
        i += (length + 3) & ~3
    return out


class Netlink(object):
    """A connection to the kernel's wireless configuration interface."""

    def __init__(self, timeout=2.0):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
        self.sock.settimeout(timeout)
        self.sock.bind((0, 0))
        self.seq = 0
        self._family = None

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _send(self, family, cmd, attrs, version=1, flags=NLM_F_REQUEST | NLM_F_ACK):
        self.seq += 1
        body = _GENL.pack(cmd, version, 0) + b"".join(attrs)
        msg = _HDR.pack(_HDR.size + len(body), family, flags, self.seq, 0) + body
        self.sock.send(msg)
        return self.seq

    def _recv(self, seq):
        """Wait for the reply to `seq`; raise on a reported error."""
        while True:
            data = self.sock.recv(65536)
            i = 0
            while i + _HDR.size <= len(data):
                length, mtype, _flags, mseq, _pid = _HDR.unpack_from(data, i)
                if length < _HDR.size:
                    return None
                payload = data[i + _HDR.size:i + length]
                if mseq == seq:
                    if mtype == NLMSG_ERROR:
                        err = struct.unpack_from("=i", payload, 0)[0]
                        if err == 0:
                            return None          # an error of zero is the acknowledgement
                        raise NetlinkError(-err, os.strerror(-err))
                    if mtype == NLMSG_DONE:
                        return None
                    return payload
                i += (length + 3) & ~3

    def family_id(self, name="nl80211"):
        """Resolve the wireless interface's identifier, once per connection."""
        if self._family is not None:
            return self._family
        seq = self._send(GENL_ID_CTRL, CTRL_CMD_GETFAMILY,
                         [_attr(CTRL_ATTR_FAMILY_NAME, name.encode("ascii") + b"\x00")],
                         flags=NLM_F_REQUEST)
        payload = self._recv(seq)
        if not payload:
            raise NetlinkError("no reply resolving the %r interface" % name)
        attrs = _parse_attrs(payload[_GENL.size:])
        raw = attrs.get(CTRL_ATTR_FAMILY_ID)
        if not raw:
            raise NetlinkError("the kernel does not provide a %r interface" % name)
        self._family = struct.unpack_from("=H", raw, 0)[0]
        return self._family

    def set_tx_power(self, ifindex, mbm):
        """Fix a radio's transmit power, in hundredths of a dBm."""
        seq = self._send(self.family_id(), NL80211_CMD_SET_WIPHY, [
            _attr(NL80211_ATTR_IFINDEX, struct.pack("=I", int(ifindex))),
            _attr(NL80211_ATTR_WIPHY_TX_POWER_SETTING,
                  struct.pack("=I", NL80211_TX_POWER_FIXED)),
            _attr(NL80211_ATTR_WIPHY_TX_POWER_LEVEL, struct.pack("=I", int(mbm))),
        ])
        self._recv(seq)
        return int(mbm)


def if_nametoindex(name):
    """Interface index by name, read from the kernel's own listing."""
    path = "/sys/class/net/%s/ifindex" % name
    with open(path) as fh:
        return int(fh.read().strip())


def make_power_setter():
    """A callable (interface, mbm) that fixes transmit power, or None where unavailable.

    Returning None lets the caller fall back rather than fail: the interface exists only on the
    platform the transmitters run on, and the controller may not be that platform.
    """
    if not hasattr(socket, "AF_NETLINK"):
        return None

    state = {"nl": None}

    def setter(iface, mbm):
        if state["nl"] is None:
            state["nl"] = Netlink()
        return state["nl"].set_tx_power(if_nametoindex(iface), mbm)
    return setter
