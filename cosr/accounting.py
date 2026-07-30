"""Turn per-AP fire outcomes and per-station counts into per-link results.

A Co-SR measurement is only meaningful for shots where *every* participating AP actually
transmitted. If one AP misses the gate on a shot, that shot was a lower-order transmission --
fewer interferers -- and folding it into the result mixes two different experiments. So the
denominator is the set of shots common to all participants, not each AP's own count.

Every link carries an explicit status. A delivery figure is produced only when the measurement
is sound; otherwise the reason is named, so a plumbing failure can never be read as a channel
result.
"""

# Firmware gate outcomes.
FIRED = 0
NOBF = 1
LATE = 2
TOOFAR = 3
CLKLOST = 4
DRAIN_STUCK = 5

_GATE_REASON = {NOBF: "nobf", LATE: "late", TOOFAR: "toofar", CLKLOST: "clock_lost",
                DRAIN_STUCK: "drain_stuck",
                90: "skipped", 91: "unknown"}

OK = "OK"
NOT_FIRED = "NOT_FIRED"
NOT_COUNTED = "NOT_COUNTED"
UNKNOWN = "UNKNOWN"
WEDGED = "WEDGED"

COVERAGE_FULL = "full"

# A report comes from the agent this controller deployed, so an absent field means the two sides
# disagree about the protocol rather than that the value was zero. Every guard below refuses on a
# missing field: a check phrased "if X is known and looks wrong, refuse" quietly passes
# everything whenever X is never populated, which is indistinguishable from having no check.
_REPORT_FIELDS = ("drops", "other_frames", "other_frames_round", "per_ap", "ordinal",
                  "epoch", "freq_mhz")
_PER_AP_FIELDS = ("rx", "coverage", "idx_hist", "seqs_seen", "per_seq")


def _missing_fields(report, mac):
    missing = [f for f in _REPORT_FIELDS if f not in report]
    per_ap = report.get("per_ap") or {}
    if mac in per_ap:
        missing += ["per_ap.%s.%s" % (mac, f)
                    for f in _PER_AP_FIELDS if f not in per_ap[mac]]
    return missing


def joint_seqs(statuses, participants=None):
    """Shots that every participant fired.

    `statuses` maps an AP name to its per-shot outcome dict {seq: code}. A shot missing from an
    AP's dict counts as not fired: silence is not evidence of transmission.
    """
    names = list(participants) if participants is not None else list(statuses)
    if not names:
        return set()
    common = None
    for name in names:
        st = statuses.get(name) or {}
        fired = set(seq for seq, code in (st.get("shots") or {}).items() if code == FIRED)
        common = fired if common is None else (common & fired)
    return common or set()


def _ap_gate_summary(st):
    """(fired_count, {reason: count}) for one AP's shot outcomes."""
    shots = (st or {}).get("shots") or {}
    fired = 0
    misses = {}
    for code in shots.values():
        if code == FIRED:
            fired += 1
        else:
            r = _GATE_REASON.get(code, "unknown")
            misses[r] = misses.get(r, 0) + 1
    return fired, misses


def _record_mcs(entry, key, rep, per_ap, commanded, warnings):
    """Compare the modulation index that went on air against the one commanded.

    A receiver whose radiotap carries no modulation index makes every frame look legacy. Absent
    and legacy must therefore be told apart: reporting "not honoured" on a receiver that cannot
    see the field would condemn a rate that was in fact used.
    """
    hist = per_ap.get("mcs_hist") or {}
    if hist:
        seen = max(hist.items(), key=lambda kv: kv[1])[0]
        entry["mcs_seen"] = int(seen)
        if int(seen) != int(commanded):
            warnings.append("%s: commanded modulation index %d but %d went on air"
                            % (key, commanded, int(seen)))
        return
    if not rep.get("mcs_capable"):
        entry["mcs_note"] = "this receiver reports no modulation index, so the rate cannot be "\
                            "verified here"
        return
    if per_ap.get("rx"):
        warnings.append("%s: frames arrived without a modulation index while this receiver "
                        "reports one for other traffic, so they were sent at a legacy rate "
                        "rather than the commanded index %d" % (key, commanded))


def link_results(plan, statuses, reports, seq_lo, n_shots):
    """Per-link results for one batch.

    `statuses`  {ap_name: {"shots": {seq: code}, "epoch": str, "error": str|None}}
    `reports`   {station_name: the dict cosr.counter.FrameCounter.report returns}

    Returns (links, warnings). `links` is keyed "<ap>-><station>".
    """
    nframes = max(1, int(plan.get("nframes", 1)))
    ap_names = [ap["name"] for ap in plan["aps"]]
    joint = joint_seqs(statuses, ap_names)

    links = {}
    warnings = []
    for ap in plan["aps"]:
        name = ap["name"]
        sta = ap["station"]
        key = "%s->%s" % (name, sta["name"])
        st = statuses.get(name)
        rep = reports.get(sta["name"])
        fired, misses = _ap_gate_summary(st)

        entry = {
            "ap": name, "station": sta["name"], "station_id": ap["station_id"],
            "mcs_cmd": ap["mcs"], "nframes": nframes,
            "fired": fired, "gate_misses": misses,
            "joint_shots": len(joint),
            "sent": len(joint) * nframes,
            "rx": None, "delivery": None, "delivery_ppdu": None,
            "rssi_dbm": None, "drops": None, "idx_hist": {},
            "mcs_seen": None, "mcs_note": None,
            "status": None, "reason": None,
        }

        # --- transport and card faults first: they invalidate the measurement outright
        if st is None:
            entry["status"] = UNKNOWN
            entry["reason"] = "no_status_from_ap"
            warnings.append("%s: the AP never reported its shot outcomes" % key)
            links[key] = entry
            continue
        if st.get("error") == WEDGED:
            entry["status"] = WEDGED
            entry["reason"] = "wmi_error"
            warnings.append("%s: the AP's WMI interface failed; the card needs attention "
                            "before any further measurement" % key)
            links[key] = entry
            continue
        if st.get("error"):
            entry["status"] = UNKNOWN
            entry["reason"] = str(st["error"])
            warnings.append("%s: %s -- the shots may or may not have gone out" % (key, st["error"]))
            links[key] = entry
            continue

        # The frames went out, but not at the power that was asked for, so the result does not
        # measure the configuration it claims to. Power is the dimension an experiment sweeps,
        # which makes a silently wrong one worse than a missing number.
        if st.get("power_error"):
            entry["status"] = NOT_COUNTED
            entry["reason"] = "power_not_set"
            warnings.append("%s: the transmit power could not be set (%s), so this round did "
                            "not run at the power it states" % (key, st["power_error"]))
            links[key] = entry
            continue

        if not joint:
            entry["status"] = NOT_FIRED
            entry["reason"] = _dominant_reason(misses)
            warnings.append("%s: no shot was fired by every participant (%s); nothing to "
                            "measure, not a loss" % (key, _fmt_misses(misses) or "no outcomes"))
            links[key] = entry
            continue

        # --- receiver side
        if rep is None:
            entry["status"] = NOT_COUNTED
            entry["reason"] = "no_report_from_station"
            warnings.append("%s: the station never reported; delivery unmeasured, not zero" % key)
            links[key] = entry
            continue

        per_ap = (rep.get("per_ap") or {}).get(ap["mac"])
        if per_ap is None:
            entry["status"] = NOT_COUNTED
            entry["reason"] = "station_not_watching_this_ap"
            warnings.append("%s: the station is not counting this AP's address" % key)
            links[key] = entry
            continue

        missing = _missing_fields(rep, ap["mac"])
        if missing:
            entry["status"] = NOT_COUNTED
            entry["reason"] = "malformed_report"
            warnings.append("%s: the station's report is missing %s, so the measurement cannot "
                            "be bounded; the agent and controller disagree about the protocol"
                            % (key, ", ".join(missing)))
            links[key] = entry
            continue

        entry["drops"] = rep["drops"]
        entry["idx_hist"] = per_ap["idx_hist"] or {}
        _record_mcs(entry, key, rep, per_ap, ap["mcs"], warnings)
        coverage = per_ap["coverage"]

        if coverage != COVERAGE_FULL:
            entry["status"] = NOT_COUNTED
            entry["reason"] = "coverage_" + str(coverage)
            warnings.append("%s: the station no longer holds the whole shot range (%s); the "
                            "count would be a lower bound, so it is not reported as delivery"
                            % (key, coverage))
            links[key] = entry
            continue

        expected_freq = plan.get("channel_freq_mhz")
        seen_freq = rep.get("freq_mhz")
        if expected_freq and seen_freq is None:
            # Phrasing this as "if the channel is known and looks wrong" would pass every
            # report from a receiver that never reports one, which is indistinguishable from
            # having no check at all.
            entry["status"] = NOT_COUNTED
            entry["reason"] = "channel_unknown"
            warnings.append("%s: the receiver reports no channel, so a card that drifted off "
                            "the shot's channel would be indistinguishable from a link that "
                            "delivered nothing" % key)
            links[key] = entry
            continue
        if expected_freq and seen_freq != expected_freq:
            entry["status"] = NOT_COUNTED
            entry["reason"] = "wrong_channel"
            warnings.append("%s: the station is on %d MHz but the shot was on %d MHz"
                            % (key, seen_freq, expected_freq))
            links[key] = entry
            continue

        # Count only the shots every participant transmitted. The receiver answers about the
        # whole commanded range, which includes shots that were not concurrent, so counting over
        # that range while dividing by the concurrent subset would not be a fraction at all.
        per_seq = per_ap.get("per_seq") or {}
        rx = 0
        for s in joint:
            rx += int(per_seq.get(str(s), per_seq.get(s, 0)) or 0)
        seqs_seen = set(int(s) for s in per_seq) & joint
        # Assigned only once the count itself has survived every check above: a level measured
        # on a link whose delivery was refused is not a result either, and would otherwise be
        # exported into the matrix a channel model consumes.
        entry["rssi_dbm"] = per_ap.get("rssi_dbm")
        entry["rx"] = rx
        entry["rx_all_seqs"] = int(per_ap["rx"] or 0)

        # Judged on this round alone: the lifetime total is non-zero within moments of
        # starting and stays so even after the capture has died.
        if rx == 0 and not rep["other_frames_round"]:
            # Nothing of ours and nothing at all: the capture cannot be shown to have been
            # listening, so this is not evidence of a lossy channel.
            entry["status"] = NOT_COUNTED
            entry["reason"] = "capture_silent"
            warnings.append("%s: the station observed no traffic whatsoever, so a zero here "
                            "cannot be distinguished from a capture that was not running" % key)
            links[key] = entry
            continue

        from .counter import deagg_ok
        if rx and not deagg_ok(entry["idx_hist"], nframes):
            entry["status"] = NOT_COUNTED
            entry["reason"] = "deagg_unsupported"
            warnings.append("%s: only the first subframe of each aggregate was recovered, which "
                            "would read as a uniform %d/%d loss; the receiver is not "
                            "deaggregating" % (key, nframes - 1, nframes))
            links[key] = entry
            continue

        sent = len(joint) * nframes
        entry["delivery"] = round(float(rx) / sent, 4) if sent else None
        # Reported separately because subframes of one aggregate share a preamble: losing it
        # loses all of them, so they are not independent trials and the two figures answer
        # different questions.
        ppdu_rx = len(seqs_seen & joint)
        entry["ppdu_rx"] = ppdu_rx
        entry["ppdu_sent"] = len(joint)
        entry["delivery_ppdu"] = round(float(ppdu_rx) / len(joint), 4) if joint else None
        entry["status"] = OK

        if entry["drops"]:
            warnings.append("%s: the receiver dropped %d frames, so delivery is a lower bound"
                            % (key, entry["drops"]))
        if misses:
            warnings.append("%s: %s did not fire (%s); those shots are excluded from the "
                            "denominator" % (key, _plural(sum(misses.values())),
                                             _fmt_misses(misses)))
        links[key] = entry

    return links, warnings


def _dominant_reason(misses):
    if not misses:
        return "no_outcomes"
    return max(misses.items(), key=lambda kv: kv[1])[0]


def _fmt_misses(misses):
    return " ".join("%s=%d" % (k, v) for k, v in sorted(misses.items()))


def _plural(n):
    return "%d shot%s" % (n, "" if n == 1 else "s")


def total_throughput(links, rate_for_mcs):
    """Sum of delivery x PHY rate over links with a sound measurement.

    Links without a delivery figure contribute nothing and are counted separately, so a caller
    can tell a genuinely idle round from one that failed to measure.
    """
    total = 0.0
    measured = 0
    unmeasured = 0
    per = {}
    for key, e in links.items():
        if e.get("status") == OK and e.get("delivery") is not None:
            rate = rate_for_mcs(e["mcs_cmd"])
            thr = e["delivery"] * rate
            per[key] = {"delivery": e["delivery"], "rate_mbps": rate,
                        "thr_mbps": round(thr, 3), "rx": e["rx"], "sent": e["sent"]}
            total += thr
            measured += 1
        else:
            per[key] = {"delivery": None, "status": e.get("status"), "reason": e.get("reason")}
            unmeasured += 1
    return round(total, 3), per, measured, unmeasured
