#!/usr/bin/env python3
"""Shared affine clock-graph core for the scaling sync modes.

Both scaling methods reduce to the same problem: a set of pairwise affine relations
between AP TSF clocks (edges), composed from a reference AP to every AP. Only the edge
SOURCE differs -- beacon mode (an AP hears a peer's beacons) vs multi-monitor mode (a
monitor hears two APs, so relating them eliminates the monitor's clock). This module is
the math both share; the trackers just build the edge dict and call solve_offsets.

An edge is a directed affine map keyed (X, Y) with value (a, b) meaning Y_tsf = a + b*X_tsf.
A map composes as (a2,b2) o (a1,b1) = (a2 + b2*a1, b2*b1) and inverts as (-a/b, 1/b).
solve_offsets composes the edges from a reference into maps[mac] = (A, B) with
mac_tsf = A + B*ref_tsf, which write_offsets converts to the controller's offset.json
(offset = A + (B-1)*ref_tsf, slope = B-1; reference maps to [0, 0.0]).
"""
import os
import json


def fit(samples):
    """Least-squares y = a + b*x over [(x, y), ...]; return (a, b) or None (<2 pts / flat x)."""
    n = len(samples)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in samples) / n
    mean_y = sum(y for _, y in samples) / n
    denom = sum((x - mean_x) ** 2 for x, _ in samples)
    if denom == 0:
        return None
    b = sum((x - mean_x) * (y - mean_y) for x, y in samples) / denom
    return (mean_y - b * mean_x, b)


def compose(outer, inner):
    """(a2,b2) o (a1,b1): if u=a1+b1*r and v=a2+b2*u then v=(a2+b2*a1)+(b2*b1)*r."""
    a2, b2 = outer
    a1, b1 = inner
    return (a2 + b2 * a1, b2 * b1)


def invert(m):
    """Inverse of an affine map: y=a+b*x  ->  x=-a/b+(1/b)*y."""
    a, b = m
    return (-a / b, 1.0 / b)


def solve_offsets(edges, ref):
    """Compose edges into ref->each-AP maps by BFS; return {mac: (A, B)}  (mac = A + B*ref).

    edges[(X, Y)] = (a, b) is the true map Y = a + b*X. Each edge and its inverse are added
    so the graph is traversable either way even when only one direction was measured. Only
    APs reachable from ref appear in the result (a disconnected AP is silently absent -- the
    caller/doctor surfaces that; firing it raises a clear error).
    """
    graph = {}   # node -> [(neighbour, map neighbour=f(node)), ...]
    for (x, y), (a, b) in edges.items():
        graph.setdefault(x, []).append((y, (a, b)))
        if b != 0:
            graph.setdefault(y, []).append((x, invert((a, b))))

    maps = {ref: (0.0, 1.0)}
    queue = [ref]
    while queue:
        u = queue.pop(0)
        for v, m_uv in graph.get(u, []):
            if v in maps:
                continue
            maps[v] = compose(m_uv, maps[u])         # ref->v = (u->v) o (ref->u)
            queue.append(v)
    return maps


def write_offsets(out_path, ref_tsf, maps, ref):
    """Emit the controller's offset.json atomically.

    offset_k = A_k + (B_k-1)*ref_tsf, slope_k = B_k-1; reference AP is [0, 0.0]. The
    controller maps a shared instant as target_k = shared + offset_k + slope_k*(shared-ref_tsf).
    Write-then-rename so a concurrent reader never sees a half-written file.
    """
    offsets = {}
    for mac, (A, B) in maps.items():
        if mac == ref:
            offsets[mac] = [0, 0.0]
        else:
            offsets[mac] = [round(A + (B - 1.0) * ref_tsf), round(B - 1.0, 9)]
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"ref_tsf": ref_tsf, "mon_tsft": 0, "offsets": offsets}, fh)
        fh.write("\n")
    os.replace(tmp, out_path)
