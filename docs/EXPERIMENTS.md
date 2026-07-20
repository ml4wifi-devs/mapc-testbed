# Experiments

This is a runbook for producing the two quantities a coordinated-spatial-reuse model consumes —
an RSSI matrix and per-link success under a given configuration — and iterating: give a Co-SR
configuration, get the measurement, give the next.

Prereq: rig up and the offset tracker running ([`INSTALL.md`](INSTALL.md), [`USAGE.md`](USAGE.md) §1).

## The interface

`measure(spec)` in [`../scripts/cosr_ctl.py`](../scripts/cosr_ctl.py) is the config → measurement
call, for any number of APs. The spec is a normal shot spec (USAGE §2) with two model
conveniences: each AP may give `mcs` (0–7) instead of the raw `rate`, and the RSSI matrices are
always included. Each AP transmits one gated broadcast stream (stamped with its `station_id`).

```bash
python3 scripts/cosr_ctl.py measure cfg.json     # one config -> one measurement (JSON)
```
```python
from cosr_ctl import measure
for cfg in my_configs:            # rig stays up between calls
    m = measure(cfg)
    model.update(m)               # feed RSSI + per-link success back into the model
```

`measure` has two modes, chosen by whether any AP carries a `stations` list:

**Single-observer mode** (no `stations`) — delivery is counted at the one shared `monitor`.
Returns:
```
success_prob:        { <ap>: {station_id, delivery, rx, sent, mcs_cmd, mcs_seen} }
rssi_at_monitor_dbm: { <ap mac>: dbm }              # AP -> monitor
rssi_ap_to_ap_dbm:   { <ap mac>: { <peer>: dbm } }  # AP <-> AP interference
sync_error:          { paired, median_us, max_us }  # timing sanity (matched frame types only)
warnings:            [ ... ]                         # present only if attention is needed
```

**Per-station mode** — give each AP a `stations` list (a variable number per AP; 0 is fine —
that AP still fires and interferes, it is just not measured at a receiver). Each station is a
monitor-mode card that samples its AP's stream at its own location, so delivery and AP→station
RSSI become spatially resolved matrices:
```json
"aps": [
  { "name":"A", "ip":"…","iface":"wlan0","mac":"<A>","station_id":1,"mcs":0,"txpower_mbm":2000,
    "stations": [ {"name":"sA1","ip":"<rx1>","iface":"wlan0"},
                  {"name":"sA2","ip":"<rx2>","iface":"wlan0"} ] },
  { "name":"B", "ip":"…","iface":"wlan0","mac":"<B>","station_id":2,"mcs":0,"txpower_mbm":2000,
    "stations": [ {"name":"sB1","ip":"<rx3>","iface":"wlan0"} ] }
]
```
Returns:
```
success_prob:        { <ap>: { <station>: {station_id, delivery, rx, sent, mcs_cmd, mcs_seen} } }
rssi_ap_to_sta_dbm:  { <ap>: { <station>: dbm } }   # AP -> that station (beacon-based)
rssi_ap_to_ap_dbm:   { <ap mac>: { <peer>: dbm } }  # AP <-> AP interference
sync_error:          { <ap>: {paired, median_us, max_us} } | null   # best-effort, optional
warnings:            [ ... ]
```
No dedicated clock monitor is needed for per-station mode: the TSF observer is `spec["monitor"]`
or, if omitted, the first station card — any monitor that hears every AP's beacons works. Sync is
best-effort (only reported if some one receiver heard ≥ 2 APs) and is not required for the
measurement.

## Read these before trusting a number

- **`delivery` is measured at the monitor and is receiver-limited.** A single monitor drops whole
  PPDUs (~40–60 % even for a strong isolated link — [`USAGE.md`](USAGE.md) §7), so raw `delivery`
  is biased low by the receiver, not only by collision/channel. For a channel/collision success
  probability, **normalize against an isolated-link baseline** (E2 below): the ratio
  `delivery_concurrent / delivery_isolated` cancels the roughly-constant capture loss. Or add a
  better/dedicated receiver.
- **Know which mode you are in.** In *single-observer* mode `delivery_A` is P(A's frame reaches
  *the one monitor*) — capture-at-a-point, not P(station _i_ decodes its AP at _i_'s location). For
  a spatially-resolved per-link matrix use per-station mode: one monitor-mode card per station,
  each counting its AP's stream at its own location. Each station is still a single point, and its
  `delivery` is still receiver-limited (normalize against an isolated-link baseline, below) — but
  the matrix now separates locations instead of collapsing them onto one observer.
- **Check `warnings` every call.** A commanded HT MCS that the AP's rate table does not carry
  downgrades silently to a legacy rate in firmware; `measure` compares commanded vs on-air MCS
  (`mcs_cmd` / `mcs_seen`) and warns. A warning means the row is **mislabeled** — fix the rate
  (enable an 11ng/HT rate table on the AP) before recording it. Do not feed a warned row to a model.
- **`sync_error` needs matched frame types** on both APs, and the µs figure includes differential
  AP→observer propagation ([`USAGE.md`](USAGE.md) §7). It is a sanity check here, not a model input.

## Recipes

Each is: edit one JSON config, call `measure`, read the fields. All configs share the `monitor`
block and the per-AP identity (`ip`, `iface`, `mac`); only the knobs below change.

### E0 — Gate sanity (do this first)
Confirm the timing gate before trusting any measurement. Single AP, absolute-precision check:
```bash
AP_IP=<apA_ip> AP_MAC=<apA_bssid> RATE=128 N=40 LEAD=300000 scripts/fire_cosr.sh
python3 scripts/gate_check.py cap.pcap <apA_bssid>
```
Expect residual **median < 1 µs**, no late fires. If not, stop and fix.

### E1 — Topology (the model's matrices)
One config with both APs; the RSSI fields are the model's interference/pathloss inputs.
```json
{ "monitor":{"ip":"…","iface":"wlan0"},
  "aps":[ {"name":"A","ip":"…","iface":"wlan0","mac":"<A>","station_id":1,"txpower_mbm":2000,"mcs":0,"nframes":1,"frame_len":200},
          {"name":"B","ip":"…","iface":"wlan0","mac":"<B>","station_id":2,"txpower_mbm":2000,"mcs":0,"nframes":1,"frame_len":200} ],
  "lead_us":500000, "stagger_us":500, "shots":20, "gap_s":0.3 }
```
Read `rssi_ap_to_ap_dbm` (interference matrix) and `rssi_at_monitor_dbm` (AP→receiver).

### E2 — Isolated-link baseline (the normalizer)
Run each AP **alone** (one AP in `aps`), at the exact `mcs`/`txpower_mbm` you will use
concurrently. Record `success_prob.<ap>.delivery` per AP — this is the interference-free
reference every concurrent number is divided by. **Required** for a meaningful success probability.

### E3 — Co-SR core: two APs, concurrent, different stations
Both fire at one shared instant (`stagger_us: 0`) to different `station_id`s:
```json
"stagger_us": 0, "shots": 30
```
Read each `success_prob.<ap>.delivery`, `dups: 0`, and `warnings`. Divide by the E2 baseline →
per-link spatial-reuse gain or capture loss. Sweep `txpower_mbm`/`mcs` per AP to move a link across
the capture threshold.

### E4 — Overlap sweep (collision → clean)
Fix everything; sweep `stagger_us` (0, 100, 300, 600, 1000 µs). Plot delivery vs stagger: full
overlap at 0 (capture effect), both recover as they separate.
```bash
for s in 0 100 300 600 1000; do
  jq ".stagger_us=$s" cfg.json > /tmp/s.json
  python3 scripts/cosr_ctl.py measure /tmp/s.json | jq '{stagger:.stagger_us, sp:.success_prob, w:.warnings}'
done
```

### E5 — Aggregated payload (throughput proxy)
Raise `nframes` (≤ 5) and/or `frame_len` (≤ 300) on the transmitters — one true A-MPDU per shot.
Delivered bytes ≈ `rx × frame_len`. Use matched `nframes` on both APs for a valid `sync_error`,
and report the counted fraction, not raw throughput (single-monitor whole-PPDU loss).

### E6 — Scale out: N APs, per-station success matrix
Add more entries to `aps` (the fire fans out to all of them; raise `lead_us` so the serial
trigger fan-out — roughly one ssh round-trip per AP — still lands ahead of the shared instant),
and give each AP a `stations` list of its receiver cards (variable per AP, 0 allowed). One call
returns `success_prob[ap][station]` and `rssi_ap_to_sta_dbm[ap][station]` for the whole topology.
The TSF observer is `monitor` (or the first station if omitted) and must hear every AP's beacons.
Each station is a card in monitor mode on the shared channel; run `bringup.sh`-style monitor setup
on each. Distinct physical cards give distinct per-location numbers; listing the same card under
two APs measures one point twice.
