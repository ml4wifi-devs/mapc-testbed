"""Affine clock algebra for relating AP TSF clocks.

Every way of observing two clocks reduces to the same object: a directed affine edge
(X, Y) -> (a, b) meaning Y_tsf = a + b*X_tsf. Composing edges from a reference clock to every
other clock gives each AP's map, and that map converts one shared instant into the local TSF
each AP must be commanded with.

Two observation sources produce edges:

  * an AP hearing a peer's beacon. The beacon body carries the sender's own TSF at transmit;
    the hearer stamps a receive time at the END of the PPDU, so the aggregate's on-air duration
    is subtracted to land back on the transmit instant. What remains is propagation, which is
    nanoseconds at room scale and therefore below the resolution that matters.
  * a station hearing two APs. Relating the two fits taken in the station's own clock cancels
    that clock, so no duration correction is needed on this path at all.

Because an AP's map is used to extrapolate to an instant in the future, the slope's uncertainty
is what bounds how long a fit stays usable; `fit_with_stderr` reports it so the caller can
derive that bound instead of guessing a timeout.
"""
import math

# mac80211 2.4 GHz bitrate table indexed by the receive descriptor's rate index:
# 0-3 CCK (1/2/5.5/11 Mb/s), 4-11 OFDM (6/9/12/18/24/36/48/54 Mb/s).
RATE_MBPS = [1.0, 2.0, 5.5, 11.0, 6.0, 9.0, 12.0, 18.0, 24.0, 36.0, 48.0, 54.0]

# Constant separation between where an AR9271 samples the timestamp it transmits and where it
# stamps a reception. A silicon property, not a per-deployment value: the length-dependent term
# below accounts for every beacon-length change, leaving this residue fixed. Established for CCK
# beacons, which is what 2.4 GHz beacons use.
AR9271_TS_OFFSET_US = 61.0

# A receive time that jumps backwards, or forwards by more than this, is a clock restart rather
# than drift. The window must be discarded: fitting across a restart produces a confident and
# entirely wrong slope.
MAX_GAP_US = 5000000

# Disagreement between how far two clocks advanced between consecutive observations, beyond which
# one of them is taken to have been reset. Their rates differ by parts per million, so a genuine
# difference over any realistic interval is microseconds: this sits far above that and far below
# the size of a reset.
MAX_SKEW_US = 100000

# TSF is reported in whole microseconds, which bounds how precisely any relation between two
# such clocks can be established regardless of how many samples are taken.
TSF_QUANTISATION_US = 1.0


def ppdu_airtime_us(rate_idx, length):
    """Correction from a PPDU's receive-end stamp back to its transmit instant, in µs.

    CCK: a 192 µs preamble and header, then the payload at the signalled rate.
    OFDM: 20 µs of preamble and signal field, then 4 µs symbols carrying 16 service bits, the
    payload, and 6 tail bits.
    """
    if not (0 <= rate_idx < len(RATE_MBPS)):
        return 0.0
    rate = RATE_MBPS[rate_idx]
    if rate_idx < 4:
        air = 192.0 + math.ceil(length * 8 / rate)
    else:
        ndbps = rate * 4
        air = 20.0 + 4.0 * math.ceil((16 + 8 * length + 6) / ndbps)
    return max(0.0, air - AR9271_TS_OFFSET_US)


def fit(samples):
    """Least-squares y = a + b*x over [(x, y), ...]; (a, b), or None if underdetermined."""
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


def fit_with_stderr(samples):
    """(a, b, slope_stderr) or None.

    The standard error is what bounds useful extrapolation: an instant Δt beyond the samples
    inherits roughly `slope_stderr * Δt` of error. With fewer than three samples the residual
    variance is undefined and the fit is treated as having unbounded uncertainty.
    """
    base = fit(samples)
    if base is None:
        return None
    a, b = base
    n = len(samples)
    mean_x = sum(x for x, _ in samples) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in samples)
    if n < 3 or sxx == 0:
        return (a, b, float("inf"))
    sse = sum((y - (a + b * x)) ** 2 for x, y in samples)
    var_b = (sse / (n - 2)) / sxx
    # Timestamps are whole microseconds, so a slope can never be known better than that
    # quantisation allows. Samples that happen to land exactly on a line otherwise report zero
    # uncertainty, which would license extrapolating arbitrarily far on the strength of a fit
    # that is merely too coarse to show its own error.
    return (a, b, math.sqrt(max(var_b, (TSF_QUANTISATION_US ** 2 / 12.0) / sxx)))


def compose(outer, inner):
    """Apply `inner` then `outer`: v = a2 + b2*(a1 + b1*r)."""
    a2, b2 = outer
    a1, b1 = inner
    return (a2 + b2 * a1, b2 * b1)


def invert(m):
    """y = a + b*x  ->  x = -a/b + (1/b)*y."""
    a, b = m
    return (-a / b, 1.0 / b)


def monitor_edges(fits):
    """AP-to-AP edges from one station's per-AP fits, cancelling the station's own clock.

    fits[i] = (ai, bi) means i_tsf = ai + bi*station. One direction per pair is emitted;
    solve_offsets supplies the reverse, so traversal never depends on dict ordering.
    """
    macs = list(fits)
    edges = {}
    for a, i in enumerate(macs):
        ai, bi = fits[i]
        if bi == 0:
            continue
        for j in macs[a + 1:]:
            aj, bj = fits[j]
            edges[(i, j)] = (aj - bj * ai / bi, bj / bi)
    return edges


def solve_offsets(edges, ref):
    """Compose edges outward from `ref`; return {mac: (A, B)} with mac_tsf = A + B*ref_tsf.

    Each edge is traversable in both directions. A clock with no path to the reference is absent
    from the result rather than guessed at.
    """
    graph = {}
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
            maps[v] = compose(m_uv, maps[u])
            queue.append(v)
    return maps


def offsets_from_maps(maps, ref, ref_tsf):
    """{mac: (offset, slope)} anchored at `ref_tsf`, the form the fire path consumes.

    A commanded target is then `shared + offset + slope*(shared - ref_tsf)`, which is exactly
    `A + B*shared` -- the shared instant expressed in that AP's own clock.
    """
    out = {}
    for mac, (A, B) in maps.items():
        if mac == ref:
            out[mac] = (0, 0.0)
        else:
            out[mac] = (int(round(A + (B - 1.0) * ref_tsf)), round(B - 1.0, 9))
    return out


def target_tsf(shared_instant, offset, slope, ref_tsf, stagger_us=0, index=0):
    """One shared instant expressed in a single AP's clock."""
    return int(round(shared_instant
                     + offset
                     + slope * (shared_instant - ref_tsf)
                     + stagger_us * index))
