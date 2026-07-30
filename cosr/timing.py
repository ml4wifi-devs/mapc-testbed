"""Summarising how closely transmissions coincided, and how precisely a single one landed.

Both measurements compare receive timestamps taken at one receiver, so its own clock cancels and
no cross-clock relation enters the result.

Two defects are reported separately because they have different causes and different remedies: a
constant displacement between transmitters, and the spread around it. A mean of absolute values
would merge them and hide a systematic offset inside apparent noise.
"""

# Errors beyond this are receive-timestamping artefacts rather than transmission error: the
# radio occasionally stamps an aggregate hundreds of microseconds away from its own start, while
# genuine coincidence error is microseconds. They are excluded from the displacement and spread
# and reported as a count, so their presence is visible rather than absorbed.
OUTLIER_US = 50

# Residual beyond this, after removing the linear relation between the two clocks, means the
# transmission did not begin when it was commanded to.
GATE_OUTLIER_US = 50


def stats(errors, paired, positions=None):
    """Signed per-shot errors in microseconds to a summary.

    Displacement, spread and rate of change are computed over the inliers; the middle value and
    the ninetieth percentile of magnitude use every sample, since both are already insensitive
    to a few extreme values.

    Supplying `positions` adds the rate of change across the batch, which separates a fixed
    displacement from one that grows. It uses the same inliers as the rest: fitted over every
    sample, a handful of mis-stamped receptions hundreds of microseconds out would dominate the
    fit entirely and report a rate that the spread plainly contradicts.
    """
    if not errors:
        return {"paired": paired, "bias_us": None, "jitter_us": None,
                "median_us": None, "p90_abs_us": None, "outliers": 0,
                "drift_us_per_shot": None}
    xs = sorted(errors)
    n = len(xs)
    median = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0
    absx = sorted(abs(x) for x in xs)
    p90 = absx[min(n - 1, int(round(0.9 * (n - 1))))]
    inliers = [x for x in xs if abs(x) <= OUTLIER_US]
    bias = jitter = None
    if inliers:
        bias = sum(inliers) / float(len(inliers))
        jitter = (sum((x - bias) ** 2 for x in inliers) / len(inliers)) ** 0.5
    drift = None
    if positions is not None and len(positions) == len(errors):
        kept = [(p, e) for p, e in zip(positions, errors) if abs(e) <= OUTLIER_US]
        if len(kept) >= 3:
            drift = trend([p for p, _ in kept], [e for _, e in kept])
    return {"paired": paired,
            "bias_us": round(bias, 2) if bias is not None else None,
            "jitter_us": round(jitter, 2) if jitter is not None else None,
            "median_us": round(median, 2),
            "p90_abs_us": round(p90, 2),
            "outliers": n - len(inliers),
            "drift_us_per_shot": drift}


def detrend_residual(pairs):
    """`pairs` are (shot index, observed minus commanded) for one transmitter.

    The observation and the command are read from different clocks, so they differ by a constant
    plus a slow drift. Removing the straight line through the samples leaves only the error in
    when each transmission actually began: a clean one is near zero throughout, while one that
    started late stands away from the line.
    """
    n = len(pairs)
    if n < 2:
        return {"drift_us_per_shot": None, "residual_median_us": None,
                "residual_max_us": None, "outliers": []}
    mean_x = sum(s for s, _ in pairs) / float(n)
    mean_y = sum(y for _, y in pairs) / float(n)
    denom = sum((s - mean_x) ** 2 for s, _ in pairs)
    slope = (sum((s - mean_x) * (y - mean_y) for s, y in pairs) / denom) if denom else 0.0
    resid = [(s, (y - mean_y) - slope * (s - mean_x)) for s, y in pairs]
    absr = sorted(abs(r) for _, r in resid)
    return {
        "drift_us_per_shot": round(slope, 2),
        "residual_median_us": round(absr[len(absr) // 2], 2),
        "residual_max_us": round(absr[-1], 2),
        "outliers": [[s, round(r, 1)] for s, r in resid if abs(r) > GATE_OUTLIER_US],
    }


def pairwise_errors(reference_times, other_times, stagger_us, index):
    """Signed error per shot for one transmitter against the reference.

    Only shots both were observed for are compared; anything else would be comparing a
    transmission against a shot that never arrived. The shots themselves are returned so the
    caller can look for a trend across the batch as well as a summary of the spread.
    """
    common = sorted(set(reference_times) & set(other_times))
    errs = [(other_times[s] - reference_times[s]) - stagger_us * index for s in common]
    return errs, common


def trend(positions, errors):
    """Rate of change of the error across a batch, in microseconds per shot.

    A displacement that grows shot by shot means the two transmitters are running at different
    rates relative to their commanded instants, rather than sitting at a fixed offset. The
    distinction matters: a fixed offset stays bounded, whereas divergence grows without limit
    and a summary of the spread alone cannot tell them apart -- both merely widen it.
    """
    n = len(positions)
    if n < 3:
        return None
    mean_x = sum(positions) / float(n)
    mean_y = sum(errors) / float(n)
    denom = sum((x - mean_x) ** 2 for x in positions)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y)
                for x, y in zip(positions, errors)) / denom
    return round(slope, 4)
