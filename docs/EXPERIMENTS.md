# Experiments

A runbook for producing the two quantities a coordinated-spatial-reuse (Co-SR) model consumes —
an RSSI matrix and per-link success under a given configuration — and iterating: give a Co-SR
configuration, get the measurement, give the next.

Prereq: testbed up and the tracker running (`./run.sh up`; see [`INSTALL.md`](INSTALL.md),
[`USAGE.md`](USAGE.md)).

## The interface

`measure` in [`../scripts/cosr_ctl.py`](../scripts/cosr_ctl.py) is the config → measurement call,
for any number of APs and a variable number of stations. You describe the testbed once in `topo.json`
and each experiment in a `shot.json` (USAGE §0); a `link` names an AP and the station it transmits
to. Because the target station's `station_id` is stamped into the frame and delivery is counted at
that station's own receiver, the result is a spatially-resolved per-(AP, station) matrix.

```bash
./run.sh measure topo.json cfg.json   # one config -> one measurement (JSON)
```
```python
from cosr_ctl import load, measure   # testbed stays up between calls
for cfg_path in my_configs:
    m = measure(load("topo.json", cfg_path))
    model.update(m)                   # feed RSSI + per-link success back into the model
```

Returns (see USAGE §2 for the full field list):
```
success_prob:        { "<ap>-><sta>": {delivery, rx, sent, fired, station_id,
                                       mcs_cmd, mcs_seen} }
rssi_ap_to_sta_dbm:  { "<ap>-><sta>": dbm }         # per-frame DATA RSSI at that station
rssi_ap_to_ap_dbm:   { "<ap mac>": { "<peer>": dbm } }
warnings:            [ ... ]                          # present only if attention is needed
```

## Read these before trusting a number

- `delivery` is the raw fraction `rx / fired` at the receiver. For a channel or collision success
  probability, divide a concurrent link's delivery by its isolated-link baseline (E2) at the same
  MCS/power — that is the interference-free, rate-matched reference. Use a reliable receiver so raw
  delivery on a clean isolated link reads ~1.0.
- Keep the tracker alive. Cross-AP alignment is µs-clean only with a fresh offset; a dead
  tracker freezes it and the sync silently drifts. `./run.sh status` shows it.
- Check `warnings` every call. A commanded HT MCS the AP's rate table lacks downgrades silently
  to legacy; `measure` compares `mcs_cmd` vs `mcs_seen` and warns. Gate misses (LATE/TOOFAR) are
  also warned and excluded from the denominator — a warned row is not a channel loss, but a
  mislabeled MCS row is; fix the rate before recording it.

## Recipes

Each is: edit `shot.json`, call `measure`, read the fields. `topo.json` stays fixed; only the shot
knobs below change.

### E0 — Gate sanity (do this first)
Confirm the timing gate before trusting any measurement:
```bash
./run.sh gate topo.json       # single AP (first link), absolute precision
```
Expect residual **median < 1 µs**, no late fires. If not, stop and fix.

### E1 — Topology (the model's matrices)
One shot with both APs; the RSSI fields are the model's interference/pathloss inputs.
```json
{ "nframes":1, "frame_len":200, "mcs":0, "txpower_mbm":2000,
  "stagger_us":600, "lead_us":150000, "shots":30, "gap_s":0.15,
  "links":[ {"ap":"apA","station":"sta1"}, {"ap":"apB","station":"sta2"} ] }
```
Read `rssi_ap_to_ap_dbm` (interference matrix) and `rssi_ap_to_sta_dbm` (AP→station).

### E2 — Isolated-link baseline (the exact normalizer)
Run each AP alone (one link), at the exact `mcs`/`txpower_mbm` you will use concurrently.
Record `success_prob.<link>.delivery`. This is the interference-free, rate-matched reference; a
concurrent delivery divided by it is the channel success probability.

### E3 — Co-SR core: two APs, concurrent, different stations
Both fire at one shared instant (`stagger_us: 0`) to different stations:
```json
"stagger_us": 0, "shots": 30
```
Read each `success_prob.<link>.delivery` and `warnings`. At full overlap one AP wins the
capture effect at each receiver; divide by the E2 baseline for the true per-link loss. Sweep
`txpower_mbm`/`mcs` per link to move a link across the capture threshold.

### E4 — Overlap sweep (collision → clean)
Fix everything; sweep `stagger_us` (0, 100, 300, 600, 1000 µs). Plot `delivery` vs stagger:
full overlap at 0 (capture effect), both recover as they separate past one frame time.
```bash
for s in 0 100 300 600 1000; do
  jq ".stagger_us=$s" shot.json > /tmp/s.json
  ./run.sh measure topo.json /tmp/s.json | jq '{stagger:.stagger_us, sp:.success_prob, w:.warnings}'
done
```

### E5 — Aggregated payload (throughput proxy)
Raise `nframes` (≤ 5) and/or `frame_len` (≤ 300) — one true A-MPDU per shot. Delivered bytes ≈
`rx × frame_len`. `nframes`/`frame_len` are common to all APs by construction (a per-link override
is rejected), so the aggregate is identical and comparisons are valid. Report the counted fraction.

### E6 — Scale out: N APs, per-station matrix
Add nodes to `topo.json` and links to `shot.json` (the fire fans out to all APs in parallel — no
need to inflate `lead_us`, the writes are threaded). One `measure` returns
`success_prob["<ap>-><sta>"]` and `rssi_ap_to_sta_dbm` for the whole topology. Each station is a
card in monitor mode on the shared channel (`run.sh up` sets this). Distinct physical cards give
distinct per-location numbers.

### E7 — Cross-AP sync check
```bash
./run.sh test-sync topo.json shot.json   # per-AP bias/jitter/median/p90 of the emission-time error
```
Fire all participating APs with the shot's `stagger_us`; the observer times each AP's first
subframe and reports each AP's error vs the reference. Median/p90 are the figures to trust.
