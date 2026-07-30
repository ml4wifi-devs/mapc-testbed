"""Read each node's wireless interface and address, ready to paste into topo.json.

Interface names and addresses reshuffle whenever a card is re-enumerated, so building or
correcting a testbed description otherwise means reading them off every node by hand.
"""
import subprocess

SSH_OPTS = ["-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ConnectTimeout=8"]


def _ssh(ip, user, password, command):
    argv = (["sshpass", "-p", password, "ssh"] + SSH_OPTS
            + ["%s@%s" % (user, ip), command])
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _err = p.communicate()
    if not isinstance(out, str):
        out = out.decode("utf-8", "replace")
    return out


def wireless_interfaces(ip, user, password):
    """[(interface, address), ...] for one node, usually a single entry.

    The address is read from sysfs rather than from `iw`, which can report a stale one after the
    address has been changed.
    """
    listing = _ssh(ip, user, password, "iw dev 2>/dev/null | awk '/Interface/{print $2}'")
    out = []
    for name in listing.split():
        mac = _ssh(ip, user, password,
                   "cat /sys/class/net/%s/address 2>/dev/null" % name).strip()
        out.append((name, mac.lower()))
    return out


def scan(ips, user, password):
    """[(ip, interface, address), ...]; interface is None for a node that did not answer."""
    rows = []
    for ip in ips:
        found = wireless_interfaces(ip, user, password)
        if not found:
            rows.append((ip, None, None))
        for iface, mac in found:
            rows.append((ip, iface, mac))
    return rows


def report(rows, out):
    width = max([len(r[0]) for r in rows] + [2])
    out("%-*s  %-8s  %s" % (width, "ip", "iface", "address"))
    out("%-*s  %-8s  %s" % (width, "-" * width, "-" * 8, "-" * 17))
    for ip, iface, mac in rows:
        out("%-*s  %-8s  %s" % (width, ip, iface or "-",
                                mac or "unreachable, or no wireless interface"))
    out("")
    out("# paste into topo.json, then set role, ssid and station_id:")
    n = 0
    for ip, iface, mac in rows:
        if iface is None:
            continue
        n += 1
        out('"node%d": {"role": "ap|station", "ip": "%s", "iface": "%s", "mac": "%s"},'
            % (n, ip, iface, mac))
