# Usage

Prereq: firmware + driver flashed and the testbed described in a `topo.json` (see
[`INSTALL.md`](INSTALL.md)). Everything is driven by `run.sh`, which reads the topo and needs no
testbed values baked in. Use your own IPs / interfaces / BSSIDs — the values below are placeholders.

## 0. Config files

**`topo.json` — the physical testbed (static).** Named nodes, the shared channel, the ssh password,
and which node observes the cross-AP clock. `station_id` is the station's identity (0–255).
```json
{
  "channel": 1,
  "password": "modwifi",
  "observer": "sta1",
  "nodes": {
    "apA":  {"role": "ap",      "ip": "10.0.0.11", "iface": "wlan0", "mac": "<apA_bssid>", "ssid": "cosrA"},
    "apB":  {"role": "ap",      "ip": "10.0.0.12", "iface": "wlan0", "mac": "<apB_bssid>", "ssid": "cosrB"},
    "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "wlan0", "station_id": 1},
    "sta2": {"role": "station", "ip": "10.0.0.22", "iface": "wlan0", "station_id": 2}
  }
}
```
AR9271 is 2.4 GHz, so `channel` is 1–13; every node shares it. The `observer` must be a station
that hears every AP's beacons (it may also be a measured receiver — no dedicated clock card
needed). If omitted, the first station is used.

The **first AP in `nodes`** is the timing **reference** (the "root" every other AP is aligned to);
there is no master/slave field and no hand-written graph — who-hears-whom is discovered at runtime.
Keep the first AP in `nodes` and the first AP in a shot's `links` the same node, so the fire
reference matches the clock reference. Interface names (`wlanX`) and MACs reshuffle on every USB
re-enumeration; `./run.sh scan <ip> [ip ...]` reads each node's current iface + MAC and prints
paste-ready node stubs, so you don't hand-read them.

A `"sync"` field picks the cross-AP clock source; the two forms are mutually exclusive and both
end at the same `/tmp/offset.json`:

- **`"monitor"`** — an explicit `"monitors": ["sta1", ...]` list of stations that timestamp the
  APs' beacons. One monitor that hears every AP is the simple case; several monitors each hearing a
  subset also work — an AP heard by two monitors bridges them, composed on the control host, so no
  node need hear every AP. No firmware/driver change, no calibration. Always list the monitors
  explicitly. Example: [`examples/topo_monitor.json`](../examples/topo_monitor.json).
- **`"beacon"`** — over-the-air (OTA) beacon sync: the APs hear *each other's* beacons (driver
  beacon tap, in `driver.diff`) and the host composes the graph. No monitor cards. No calibration:
  the tap reports each beacon's rate and length and the tracker computes the airtime itself.
  Example: [`examples/topo_beacon.json`](../examples/topo_beacon.json).

All three are validated end-to-end on the live testbed (per-AP bias sub-µs; see
[`SYNC.md`](SYNC.md), "Status"). The `observer` is still the `test-sync` measurement
point in every mode. To see which sync method is tighter on your own testbed, `run.sh compare`
fires the same `test-sync` through each and prints the per-AP bias/jitter side by side.

**`shot.json` — the experiment.** The common A-MPDU shape (identical across APs, so comparisons
are valid), common MCS/power (overridable per link), the timing, and `links` — which APs transmit
and which station each targets. `links[0].ap` is the timing reference.
```json
{
  "nframes": 1, "frame_len": 200,
  "mcs": 0, "txpower_mbm": 2000,
  "stagger_us": 0, "lead_us": 150000, "shots": 20, "gap_s": 0.15,
  "links": [
    {"ap": "apA", "station": "sta1"},
    {"ap": "apB", "station": "sta2", "mcs": 7}
  ]
}
```
A link `apA -> sta1` makes apA transmit a gated stream stamped with sta1's `station_id`, and its
delivery is counted **at sta1's own receiver**, for frames whose `(source AP, station_id)` match.
So different APs can target different stations in the same shot, and each station's number is its
own. One AP per link per shot (an AP targeting two stations is two separate shots).

## 1. Bring the testbed up

Testbed state lives in `/tmp` and dies on reboot. `run.sh up` starts stock hostapd on each AP, puts
every station in monitor mode on the shared channel, opens debugfs, and launches the cross-AP
offset tracker on the observer.
```bash
./run.sh up     topo.json    # topo.json is the first arg (defaults to ./topo.json)
./run.sh status topo.json    # APs up? gated-TX node present? monitors + tracker live + offset fresh?
```
The tracker (`offset_tracker.py`, deployed automatically) hears every AP's beacons and writes
`/tmp/offset.json` on the observer — `{ref_tsf, mon_tsft, offsets:{<mac>:[offset,slope]}}`, the
reference AP at `[0,0]`. Keep it running — a stale offset (a dead tracker) makes the two clocks
drift apart and the cross-AP alignment degrade silently. `status` shows it.

In the scaling modes `up` instead starts the tracker on the **control host** and `offset.json` lives
*there*, not on a node: `beacon_tracker.py` for `sync: "beacon"`, or `monitor_tracker.py` (fed by
`offset_tracker.py --raw` on each listed monitor) for multi-monitor. `status`/`doctor` label it
`(host)`; don't look for `offset.json` on the observer in those modes.

## 2. Basic commands

```bash
./run.sh shot      topo.json [shot.json]   # fire; per-link RAW delivery (lightweight; measure normalizes)
./run.sh measure   topo.json [shot.json]   # fire + model inputs: per-link success + RSSI matrices
./run.sh test-sync topo.json [shot.json]   # fire staggered; per-AP cross-AP sync (bias/jitter/p90)
```
(`topo.json` defaults to `./topo.json`, `shot.json` to `./shot.json`.) Each reads the reference AP's TSF, maps the one shared
instant into every AP's own TSF via `/tmp/offset.json`, fans the gated triggers out in parallel,
then parses each station's capture. `shot` and `measure` are separate — no auto-dispatch.

*The denominator.* Every shot's firmware outcome is read back from the AP's kernel log
(`cosr_gated_tx: status=`), so `delivery = rx / shots-that-actually-FIRED`. A gate miss
(`LATE`/`TOOFAR`) is surfaced in `warnings`, never counted as a channel loss.

**`measure` result** (2 APs, different stations):
```json
{
  "success_prob": {
    "apA->sta1": {"station_id":1,"fired":28,"sent":28,"rx":28,"delivery":1.0,
                  "mcs_cmd":0,"mcs_seen":0},
    "apB->sta2": {"station_id":2,"fired":28,"sent":28,"rx":26,"delivery":0.929,
                  "mcs_cmd":0,"mcs_seen":0}
  },
  "rssi_ap_to_sta_dbm": {"apA->sta1": -34.4, "apB->sta2": -32.8},
  "rssi_ap_to_ap_dbm":  {"<apA>": {"<apB>": -58.1}, "<apB>": {"<apA>": -40.5}},
  "warnings": ["apA: 2/30 shots did not fire (late=0 toofar=2 nobf=0) -- not counted as loss"]
}
```
- `delivery` — raw fraction at the station: `rx / fired`, counted from the unique stamped
  subframe ids the receiver captured. A dead capture yields `delivery: null`, not `0`.
- `rssi_ap_to_sta_dbm` — mean per-frame data RSSI, not a beacon proxy.
- `rssi_ap_to_ap_dbm` — AP↔AP interference matrix (transient `mon0` VIF per AP, post-fire; note 1).
- `mcs_seen` vs `mcs_cmd` + `warnings` — a commanded HT rate the AP's table lacks downgrades
  silently to legacy; the row is then mislabeled — fix the rate before recording it.

**`test-sync` result** (per non-reference AP, error = measured emission delta − intended stagger):
```json
{ "reference":"apA", "stagger_us":300, "shots":30,
  "per_ap": {"apB": {"paired":28, "bias_us":0.4, "jitter_us":0.8, "median_us":0.0, "p90_abs_us":2.0, "outliers":1}},
  "fired": {"apA":28, "apB":29} }
```
`bias_us` (mean signed error) and `jitter_us` (std) are the two distinct sync defects — a constant
offset vs random spread; `median_us` (signed) and `p90_abs_us` (worst-case magnitude) are robust
summaries over *every* shot. A few shots per run land hundreds of µs off — an AR9271 monitor
RX-mactime artifact, not a real emission error — so `bias_us`/`jitter_us` are computed over the
inliers (|err| ≤ 50 µs) and `outliers` counts the excluded shots; if you compute your own bias/jitter
from a raw capture, drop those outliers too or they dominate the mean/std. With a live tracker the
gate is µs-clean cross-AP (|bias|, jitter, p90 all a few µs). Pairs on the first subframe (idx 0) only.

Fields recap: `mcs` (0–7 → HAL `0x80|mcs`); `txpower_mbm` real power via `iw` (`2000` = 20 dBm);
`nframes`/`frame_len` bounded by the firmware `POOL_ID_ATTACKS` pool (`nframes ≤ 5`,
`frame_len ≤ 300`; larger needs a pool bump + reflash); `stagger_us` (`0` = deliberate collision).

## 3. Command interface (the WMI write payload)

Host → firmware over WMI/HTC, id `WMI_COSR_GATED_TX_CMDID`. The driver exposes it as debugfs
`.../ath9k_htc/cosr_gated_tx`. Write payload — a 17-byte little-endian prefix + header template:
```
[8  target_tsf ]  absolute target TSF, LE (0 => fire immediately, ungated TX-path smoke test)
[1  rate       ]  HAL rateCode; 0 => firmware min-rate default; HT/MCS = 0x80|mcs
[1  txpower    ]  0..63; reaches the TX descriptor but the AR9271 RF clamps it (note 2); 0 => 63
[1  nframes    ]  A-MPDU subframes; 0/1 => a single frame
[1  stamp_off  ]  byte offset of the 6-byte id stamp inside each subframe
[1  station_id ]  logical destination id, written into the stamp
[2  seq        ]  base sequence number, LE, written into the stamp
[2  frame_len  ]  full length of each synthesized subframe, LE
[N  header     ]  24-byte 802.11 header template; the body is synthesized on-chip
```
The frame body cannot be shipped over WMI (`WMI_CMD_MAX_LEN` is 100 B), so the firmware
synthesizes each `frame_len` subframe on-chip from the header template, pads the body with `0x88`,
and writes a 6-byte id stamp `C0 5A <station_id> <seq_lo> <seq_hi> <subframe_idx>` at `stamp_off`.
A monitor counts delivered subframes by unique `(source AP, station_id, seq, idx)` — this holds up under
Co-SR collisions. Read a TSF: debugfs `netdev:*/tsf`.

Gate mechanics: reject targets > 600 ms ahead; a two-phase busy-poll (coarse wait with IRQ on so
beacons/USB keep being serviced, then a ~2 ms final approach with IRQ off) fires on the VO QCU with
carrier-sense and backoff disabled, so the frame keys at the target instant regardless of the other
AP. Frames are NOACK + a single try — a gated frame is never retransmitted (a retry would fire
ungated, outside the shared instant). `nframes > 1` builds one true HT A-MPDU (single PPDU, N
delimited subframes) with no Block-Ack; a passive monitor deaggregates it and counts each subframe
by its stamp.

## 4. Diagnostics (per-layer checks)

```bash
./run.sh gate topo.json [shot.json]   # single-AP absolute gate precision (the first link's AP)
```
`gate` fires the **first link's AP alone** for `shots` shots, pairs each frame's monitor tsft
with its own commanded target, and detrends the AP↔monitor clock ramp. Result:
```json
{ "ap":"apA","station":"sta1","shots":40,"fired":40,"captured":38,"paired":38,
  "drift_us_per_shot":4.5,"residual_median_us":0.66,"residual_max_us":1.8,"outliers":[],"clean":true }
```
Expect **`residual_median_us` < 1** and `outliers: []` (`clean: true`); a late fire spikes off the
ramp into `outliers`. Same command, same two files, same wire as every other command — `gate` is
just `cosr_ctl.py gate` (no separate script). To gate a different AP, make it the first link. Use
it after any firmware/driver change to confirm the µs gate.

**When a shot returns 0 (or intermittently few) frames RX — `./run.sh doctor`.** "0 frames RX"
is several distinct faults wearing one symptom; `doctor` probes every node and prints a PASS/FAIL
table that names the layer:
```bash
./run.sh doctor topo.json  # per node: reachable? AP beaconing? station in monitor on-channel?
                         #           does each station HEAR each AP? tracker alive + offset fresh?
```
- **`UNREACHABLE`** — the VM is down or the AR9271 dropped off USB; re-enumerate the dongle
  (host-side disconnect/reconnect), not a guest reboot.
- **station `<- apX  SILENT`** — that station cannot hear that AP's *beacons*, so it cannot receive
  its *data* either. This is the usual 0-RX cause (range, wrong channel, card in the wrong mode).
- **station hears beacons but a shot still gives `rx=0`** — capture is healthy; look at the
  gate/data path instead (`fired` count, `mcs_seen` vs `mcs_cmd`, a stale offset → `TOOFAR`).
- **tracker `STALE`/`not-running`** — cross-AP alignment is drifting; restart it.

**Fast fix — `./run.sh reset`.** Re-asserts every station's monitor mode + channel, bounces only
a *dead* AP (a live AP keeps its free-running TSF undisturbed), and restarts the offset tracker —
the common soft faults without a full `up`. A `wedge scan WARN` / `UNREACHABLE` needs a physical
USB re-enumerate first; `reset` fixes the rest. Re-run `doctor` to confirm.

## 5. Files

| file | role |
|---|---|
| `run.sh` | the driver — reads `topo.json`; `up`/`status`/`doctor`/`reset`/`shot`/`measure`/`test-sync`/`gate`/`down` |
| `scripts/cosr_ctl.py` | the controller — `shot`/`measure`/`test-sync`/`gate`: fire N APs, count per-(AP,station) delivery, RSSI, sync, gate precision |
| `scripts/topo_env.py` | emit `topo.json` as shell records (so `run.sh` needs no jq) |
| `scripts/scan_nodes.py` | read each node's wireless iface + MAC from its IP (`run.sh scan <ip>...`), for building `topo.json` |
| `scripts/offset_tracker.py` | single-observer tracker (on the observer VM); `--raw` mode feeds multi-monitor |
| `scripts/clock_graph.py` | shared affine clock-graph core (compose / BFS / `offset.json` emit) for the scaling modes |
| `scripts/beacon_tracker.py` | beacon-sync tracker; on the host, polls each AP's `cosr_beacons`, composes the graph |
| `scripts/monitor_tracker.py` | multi-monitor tracker; on the host, composes each monitor's raw fits into the graph |

All Python is python3 (the observer tracker runs under the modwifi image's python3; the host-side
scaling trackers run on the control host).

## 6. Implementation notes

Non-obvious constraints of the AR9271 / ath9k_htc platform and how the design accommodates them.

1. **A monitor VIF rebases an AP's phy TSF.** Adding a `mon0` interface to an AP's radio shifts
   that phy's TSF and can perturb the on-chip gate, so APs remain single-VIF and the cross-AP
   offset tracker runs on a station node, off beacons. The lone exception is the AP↔AP RSSI
   matrix, which adds `mon0` after firing and removes it before the next shot (verified clean).
2. **The descriptor TX-power field is clamped by the AR9271 RF** (measured: with `iw` fixed at
   20 dBm, sweeping the descriptor byte 10/4/1 dBm left the data-frame RSSI flat at −19.3/−19.4/
   −19.5 dBm; with the byte fixed, sweeping `iw` 20/8/3 dBm moved RSSI to −19.6/−26.7/−34.1).
   Real power is therefore set via `iw dev <if> set txpower fixed <mbm>` — the descriptor byte
   cannot replace it. The response's `send_tsf` is always 0, so the driver's `delta_us` print is
   meaningless; only `status` matters (0 FIRED, 2 LATE, 3 TOOFAR, 1 NOBF).
3. **Firmware loads only on USB re-enumeration**, not on `rmmod`/`modprobe`; a new driver against
   old resident firmware yields WMI error `-110`. Reflashing needs an unplug/replug or USB reset.
4. **A failed firmware initialization drops the dongle off the USB bus**, recovered by a host-side
   reconnect rather than a guest reboot; toggling the guest's `.../authorized` node can hang the VM.
5. **Carrier sense and backoff are disabled during a shot** (`AR_DIAG_FORCE_RX_CLEAR |
   AR_DIAG_IGNORE_VIRT_CS`, `AR_DLCL_IFS = 0`) so the APs fire without deferring to one another,
   restored per shot. Interrupts are masked only for the final ~2 ms of the approach.
6. **The target QCU is drained before the gate, not inside the fine window** — `ah_stopTxDma` can
   block up to a frame airtime (~500 µs) and draining in the 2 ms approach overran the target.
7. **The firmware avoids variable 64-bit shifts** (no libgcc `__ashldi3`): the target TSF is
   assembled from bytes with constant shifts. The target CPU is big-endian.
8. **Gated subframes are allocated from `POOL_ID_ATTACKS`** and reclaimed through
   `attack_free_packet` / `cosr_free_ampdu`, or the pool exhausts after a few shots; it (5 × 300 B)
   also bounds `nframes` and `frame_len`.
9. **`tcpdump -x` mis-dissects the `C0 5A` stamp as an LLC header and drops it**; captures are
   parsed with tshark full-hex. tcpdump BPF filters also miscompile on this radiotap link type, so
   captures use a broad filter and are post-filtered.
10. **`pkill -f <pattern>` self-matches its own launching shell** (the pattern is in the shell's
    cmdline) — it kills the shell before the real target and leaves the target running. `run.sh`
    uses the `'[o]ffset_tracker.py'` bracket trick and `pkill -x tcpdump` to avoid this; getting it
    wrong once left a dead tracker and a frozen offset that quietly wrecked cross-AP sync.
11. **Testbed state is non-persistent** — hostapd/monitor config live in `/tmp`; `run.sh up` rebuilds it.

## 7. Scope and limitations

Read these before quoting a number.

- **`delivery` is the raw fraction `rx / fired` at the receiver** — no capture-ceiling
  normalization. A passive monitor with imperfect capture reads below 1.0 on a clean link; use a
  reliable receiver so raw delivery is trustworthy, and for a per-MCS reference run the isolated
  baseline (E2) at the same MCS.
- **Cross-AP sync depends on a *live* tracker.** With a fresh offset the gate is µs-clean cross-AP
  (`test-sync` median ~1 µs, p90 ~2 µs). A dead/stale tracker freezes the offset while the clocks
  drift, and the error grows without bound — check `status` before trusting a sync number.
- **Sync measurement needs matched frame types.** `test-sync` pairs APs on their first subframe's
  PHY-RXSTART timestamp (idx 0 — a later A-MPDU subframe's `mactime` is a monitor artifact). Both
  APs single-frame, or both A-MPDU. The single-AP gate itself is proven < 1 µs.
- **Differential propagation enters the measured sync.** The observer times each frame at
  PHY-RXSTART (TX-start + AP→observer path delay, ~3.3 ns/m). Keep the observer roughly
  equidistant from the APs, or subtract the known path differences.
- **Idle-queue operating point.** The gate is characterized on idle/dedicated queues (the intended
  regime for coordinated firing). Deterministic release under a concurrently loaded queue is out of
  scope; the firmware marks where a just-before-fire re-check would slot in.
