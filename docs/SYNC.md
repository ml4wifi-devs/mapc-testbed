# Scaling cross-AP sync past a single observer

The default sync (`sync: "monitor"` with one observer) needs one node that hears *every*
participating AP's beacons. `offset_tracker.py` sits on that observer, fits `ap_tsf = a + b·mon_tsft`
per AP against the observer's clock, and writes `/tmp/offset.json`. In a spread-out network, needing
one node to hear all APs is the ceiling. Two modes remove it — both selected by the `sync` field in
`topo.json`, mutually exclusive:

- **`sync: "beacon"`** — over-the-air (OTA) beacon sync: the APs hear *each other's* beacons directly
  (a driver tap), no monitor cards.
- **`sync: "monitor"` + a `monitors` list** — *several* monitors each hear a subset of APs; an AP
  heard by two monitors bridges them, no firmware/driver change.

Both reduce to the *same* problem: a set of pairwise affine relations between AP TSF clocks (edges),
composed from a reference AP to every AP. `scripts/clock_graph.py` is that shared core (affine
compose `(a2,b2) ∘ (a1,b1) = (a2 + b2·a1, b2·b1)`, its inverse `(-a/b, 1/b)`, breadth-first
composition from the reference, and the `offset.json` emit). The two modes differ only in where the
edges come from, and both write the same `/tmp/offset.json` the controller already consumes, so
nothing downstream changes.

## Method A — OTA beacon sync (`sync: "beacon"`)

A beacon carries the sender's own TSF in its body (the Timestamp field). When AP *X* hears AP *Y*'s
beacon, the hardware also stamps arrival in *X*'s TSF — so one frame gives a pair `(t1 = Y's TSF,
t2 = X's TSF at reception)`. Fit `t1 = a + b·t2` over a sliding window: that is an edge `X→Y`.
Compose edges from a reference AP to reach every AP. With redundant edges (an AP heard by several
peers) a global least-squares would beat a single spanning-tree path, but the spanning tree is
enough for a connected graph (the redundant-edge solve is a later robustness win, not built here).

This is one-way, not PTP: a two-way exchange only cancels propagation delay, which is nanoseconds
between co-located APs — not worth a handshake. Hence *OTA beacon sync*, not PTP.

**The airtime correction (computed, not tuned).** `t1` is the beacon's Timestamp (near TX start);
`t2` is `rx_status->mactime` with `RX_FLAG_MACTIME_END`, i.e. the *end* of the received PPDU. The
two differ by one beacon's on-air time (~0.5 ms — *not* propagation, which really is nanoseconds), so
it must be removed. The driver tap reports each beacon's PHY rate index and length, and
`beacon_tracker.py` computes the exact 802.11 PPDU duration and subtracts it from `t2`. What the
formula does not capture is one fixed **AR9271 silicon constant** — the offset between where the chip
samples the transmitted Timestamp and where it stamps the received mactime (≈61 µs, length-independent,
so folded in as `AR9271_TS_OFFSET_US`). It was measured for CCK beacons (every AR9271/hostapd 2.4 GHz
beacon goes out at 1 Mbps CCK); an OFDM-beacon testbed would want its own. No `airtime_us` knob, no
per-testbed calibration.

The driver tap is ~40 read-only lines (`htc_drv_txrx.c` + `htc_drv_debug.c`, in `driver.diff`): in
`ath9k_rx_prepare`, for each received beacon it pushes `(sender BSSID, t1, t2, rate_idx, len)` into a
ring buffer exposed as the debugfs node `cosr_beacons`. It sits off the TX/gate path, so it cannot
disturb timing (confirmed: the gate stays clean on an AP with the tap active). `beacon_tracker.py`
runs on the control host, polls every AP's `cosr_beacons` over ssh, and composes the graph.

## Method B — multiple monitors, bridged by common APs (`sync: "monitor"` + `monitors`)

Keep dedicated monitor stations, but drop the requirement that *one* hears everything:

```json
"sync": "monitor",
"monitors": ["sta1", "sta2"]
```

Each monitor runs `offset_tracker.py --raw` (launched by `run.sh`), publishing per-AP fits
`ap_tsf = a + b·mon_tsft` in its own clock as `/tmp/raw_fits.json`. The host tracker
`scripts/monitor_tracker.py` reads every monitor's fits and, for each monitor hearing both AP *i* and
AP *j*, forms the direct edge *i→j* by eliminating that monitor's clock:

```
i = ai + bi·mon,  j = aj + bj·mon   =>   j = (aj − bj·ai/bi) + (bj/bi)·i
```

An AP heard by two monitors appears in both edge sets and bridges their clock axes; the edges compose
(same `clock_graph` BFS) into one global map. **No airtime constant here:** the same monitor stamps
both APs with the same beacon, so the RX-end-vs-TX-start airtime is identical and cancels in the
elimination. (This is also why the single-observer tracker was always accurate — it is the
one-monitor case of this method.)

**Choosing:** Method A needs the driver tap but no monitor cards; Method B needs monitor cards but no
firmware/driver change. Neither needs manual calibration. Use A when cards are scarce and the APs are
in range of each other, B when spare radios are available. `run.sh compare` measures which is tighter
on your testbed.

## Comparing the two methods (`run.sh compare`)

Which is more accurate *here* is an operational question: fire the APs at one shared TSF and measure
how tightly they land. `run.sh compare topo.json [shot.json]` brings up each clock source in turn (OTA beacon,
then multi-monitor), fires the **same** staggered `test-sync` through each (so only the clock source
differs), and prints the per-AP bias / jitter / p90 side by side with a verdict (lower `|bias| +
jitter` wins). It needs the OTA tap on the APs (method A) and a `monitors` list in `topo.json`
(method B); it restores the topo's configured sync source when done.

## What has to be true

- The graph must be connected to the reference. Method A: every AP reachable from the reference
  through peers it can hear. Method B: through APs that share a monitor. An otherwise-isolated AP
  needs a bridge (a peer in range, or a monitor that also hears a connected AP).
- The APs that need the tightest alignment are the ones that transmit together — interfering
  neighbours, in range of each other, about one hop apart — so composition depth stays shallow for
  exactly the pairs that matter.

## Status

The graph math both methods share — affine compose/inverse, breadth-first composition, the beacon
airtime correction, the multi-monitor clock-elimination edge, a two-monitor bridge on a common AP,
and a two-hop chain recovering an AP's true clock through an intermediate — is covered by
`tests/test_clock_graph.py`, along with the controller's `target = shared + offset + slope·(shared −
ref_tsf)` at an instant away from `ref_tsf` (so the slope term is exercised).

Both scaling modes were run end-to-end on the two-AP testbed and compared against the single-observer
path (one monitor hearing both APs — the pre-existing baseline). Firing a staggered shot from both
APs at one shared target and stamping each at the observer gives, for the second AP relative to the
reference:

| method | bias | jitter | p90 \|err\| | notes |
|---|---|---|---|---|
| single-observer (baseline) | 0.16 µs | 1.53 µs | 2 µs | airtime-free |
| Method A — OTA beacon | 0.19–0.76 µs | ~1 µs | 2–3 µs | airtime computed from rate+len; one AR9271 chip constant |
| Method B — multi-monitor | 1 µs (median) | ~1.8 µs | 3 µs | airtime-free; heavier outlier tail |

`run.sh compare` on this testbed put OTA (mean |bias|+jitter 1.66 µs) slightly ahead of multi-monitor
(2.61 µs): OTA measures each AP pair directly, while multi-monitor composes through an eliminated
monitor clock and carries that extra noise. Method B's median/p90 match the others, but a few shots
per run land hundreds of µs off (spanning-tree composition noise); the redundant-edge global
least-squares is the intended fix.

**Hardware notes.** Composition depth greater than one hop is unit-tested only (the four-VM testbed
has just two APs). The AR9271 dongles remain fragile — a card wedges its WMI clock (`get_tsf` →
`0xfffffffb…`, `WMI_… failed with -110`) if the VM's USB passthrough can't service the gated-TX
command in time; that is a per-VM property, not per-card, and is not cleared by a USB reconnect or a
guest reboot (relocate the AP role to a healthy VM — see INSTALL.md). A wedged VM still works as a
pure RX monitor, since the radiotap path is independent of the WMI clock.
