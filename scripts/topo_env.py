#!/usr/bin/env python3
"""Emit a topo.json as pipe-delimited shell records (so run.sh needs no jq).

Lines:
    meta|<channel>|<password>|<sync>
    ap|<name>|<ip>|<iface>|<ssid>|<mac>
    station|<name>|<ip>|<iface>|<station_id>
    observer|<name>|<ip>|<iface>
    monitor|<name>|<ip>|<iface>          (one per topo "monitors" entry, multi-monitor sync)

APs are emitted in node order; the first AP is the timing reference. The observer is
topo["observer"] (must be a station node); if absent, the first station is used. The
monitor lines are the clock monitors for multi-monitor sync (topo "monitors": [names]).

Usage:  python3 topo_env.py topo.json
"""
import sys
import json

topo = json.load(open(sys.argv[1]))
nodes = topo["nodes"]
print("meta|%s|%s|%s" % (topo.get("channel", 1), topo.get("password", "modwifi"),
                         topo.get("sync", "monitor")))

stations = []
first_ap = None
for name, n in nodes.items():
    if n.get("role") == "ap":
        print("ap|%s|%s|%s|%s|%s" % (name, n["ip"], n["iface"],
                                     n.get("ssid", name), n["mac"]))
    elif n.get("role") == "station":
        stations.append(name)
        print("station|%s|%s|%s|%s" % (name, n["ip"], n["iface"], n.get("station_id", "")))

obs = topo.get("observer") or (stations[0] if stations else None)
if obs:
    o = nodes[obs]
    print("observer|%s|%s|%s" % (obs, o["ip"], o["iface"]))

for mname in topo.get("monitors", []):
    if mname in nodes and nodes[mname].get("role") == "station":
        m = nodes[mname]
        print("monitor|%s|%s|%s" % (mname, m["ip"], m["iface"]))
