#!/usr/bin/env python3
"""Probe a list of node IPs and print each one's wireless interface + MAC, ready for topo.json.

The AR9271 cards reshuffle their interface name (wlanX) and MAC on every USB re-enumeration, so
building or fixing a topo.json means re-reading them off each VM by hand -- exactly the boring,
error-prone step this automates. Give it the management IPs; it ssh'es to each (password auth, the
node image's default), reads the wireless iface(s) from `iw dev` and the MAC from
/sys/class/net/<iface>/address, and prints both a human table and paste-ready JSON node stubs.

Usage:  scan_nodes.py <ip> [ip ...] [--password PW] [--user USER]
        (via run.sh:  ./run.sh scan <ip> [ip ...])
"""
import subprocess
import sys


def ssh(ip, user, pw, cmd):
    argv = ["sshpass", "-p", pw, "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ConnectTimeout=8",
            "%s@%s" % (user, ip), cmd]
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.stdout


def wireless_ifaces(ip, user, pw):
    """Return [(iface, mac), ...] for every wireless interface on the node (usually one)."""
    # `iw dev` lists wireless interfaces as "\tInterface wlanX"; the MAC is authoritative from
    # sysfs (iw's "addr" line can lag a MAC change, sysfs does not).
    out = ssh(ip, user, pw, "iw dev 2>/dev/null | awk '/Interface/{print $2}'")
    ifaces = []
    for name in out.split():
        mac = ssh(ip, user, pw, "cat /sys/class/net/%s/address 2>/dev/null" % name).strip()
        ifaces.append((name, mac.lower()))
    return ifaces


def main(argv):
    pw, user = "modwifi", "modwifi"
    ips = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--password":
            pw = argv[i + 1]; i += 2
        elif a == "--user":
            user = argv[i + 1]; i += 2
        else:
            ips.append(a); i += 1
    if not ips:
        sys.exit("usage: scan_nodes.py <ip> [ip ...] [--password PW] [--user USER]")

    rows = []          # (ip, iface, mac) -- one per wireless iface found (or a FAIL marker)
    for ip in ips:
        ifaces = wireless_ifaces(ip, user, pw)
        if not ifaces:
            rows.append((ip, None, None))
        else:
            for iface, mac in ifaces:
                rows.append((ip, iface, mac))

    w_ip = max(len(r[0]) for r in rows)
    print("%-*s  %-8s  %s" % (w_ip, "ip", "iface", "mac"))
    print("%-*s  %-8s  %s" % (w_ip, "-" * w_ip, "-" * 8, "-" * 17))
    for ip, iface, mac in rows:
        if iface is None:
            print("%-*s  %-8s  %s" % (w_ip, ip, "-", "UNREACHABLE / no wireless iface"))
        else:
            print("%-*s  %-8s  %s" % (w_ip, ip, iface, mac))

    print("\n# paste-ready topo.json node stubs (fill role/ssid/station_id, rename the keys):")
    n = 0
    for ip, iface, mac in rows:
        if iface is None:
            continue
        n += 1
        print('"node%d": {"role": "ap|station", "ip": "%s", "iface": "%s", "mac": "%s"},'
              % (n, ip, iface, mac))


if __name__ == "__main__":
    main(sys.argv[1:])
