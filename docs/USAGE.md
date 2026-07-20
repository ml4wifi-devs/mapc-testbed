# Usage

Prereq: firmware + driver flashed and the rig up (see [`INSTALL.md`](INSTALL.md)). Use your own
IPs / interfaces / BSSIDs throughout — the values below are placeholders.

## 1. Bring the rig up (after any VM reboot)

Rig state lives in `/tmp` and dies on reboot. `bringup.sh` starts stock hostapd on each AP, puts
the monitor in monitor mode on the shared channel, and opens debugfs. Pass APs + monitor via env:
```bash
APS="10.0.0.11:wlan0:apA 10.0.0.12:wlan0:apB" MON="10.0.0.20:wlan0" scripts/bringup.sh
```
Start the **cross-AP offset tracker** on the monitor VM, off every AP's beacons. The first mac is
the timing reference (`aps[0]`); list all participating AP BSSIDs after it:
```bash
# on the monitor VM (2 APs, or N -- add every AP's BSSID):
python3 offset_tracker.py wlan0 <ref_bssid> <ap2_bssid> [<ap3_bssid> ...]   # -> /tmp/offset.json
```
`/tmp/offset.json` is JSON: `{ref_tsf, mon_tsft, offsets:{<mac>:[offset,slope]}}`, the reference at
`[0,0]`. The observer need not be a dedicated card — any monitor that hears all the APs' beacons
works, including one that also serves as a receiver.

## 2. Fire a shot — one call, one JSON spec

`aps[0]` is the timing reference; `aps` may list any number of APs (raise `lead_us` so the serial
trigger fan-out still lands ahead of the shared instant). The example below is two APs measured at
the one shared `monitor`; for a per-station success/RSSI matrix across receivers, give each AP a
`stations` list and call `measure` — see [`EXPERIMENTS.md`](EXPERIMENTS.md).
```bash
python3 scripts/cosr_ctl.py spec.json     # spec on argv or stdin; prints a result dict
```
```json
{
  "monitor": {"ip": "10.0.0.20", "iface": "wlan0"},
  "aps": [
    {"name":"ref","ip":"10.0.0.11","iface":"wlan0","mac":"<ref_bssid>",
     "station_id":1,"rate":128,"txpower_mbm":2000,"nframes":1,"frame_len":200},
    {"name":"follower", "ip":"10.0.0.12","iface":"wlan0","mac":"<follower_bssid>",
     "station_id":2,"rate":128,"txpower_mbm":2000,"nframes":1,"frame_len":200}
  ],
  "lead_us": 500000, "stagger_us": 500, "shots": 12, "gap_s": 0.3, "ap_rssi": true
}
```
`cosr_ctl.py` reads the reference AP's TSF, maps the one shared instant into each AP's TSF via
`/tmp/offset.json`, fans the gated triggers out in parallel (threads), then parses the monitor
capture and returns, per station: subframes sent/received + delivery, cross-AP sync error
(median/max µs), duplicate count, and RSSI.

Fields:
- `rate` — HAL rateCode, `0x80|mcs` (so `128` = MCS0).
- `txpower_mbm` — real TX power via `iw` in mBm (`2000` = 20 dBm); the WMI txpower byte is
  ignored by the AR9271 RF (note 2) but is sent capped ≤ 40 (= 20 dBm) so the firmware's hot
  default never fires. TX power tracks this monotonically at the monitor.
- `nframes` — A-MPDU subframes in one HT PPDU (`1` = single frame). Bounded by the firmware's
  `POOL_ID_ATTACKS` pool (5 bufs × 300 B → `nframes ≤ 5`, `frame_len ≤ 300`); larger needs a
  firmware pool bump + reflash.
- `stagger_us` — offsets each follower for an overlap/stagger sweep (`0` = deliberate collision).
- `ap_rssi` — optional; `true` adds an AP↔AP RSSI matrix (`rssi_ap_to_ap_dbm`) to the result.
  Off by default because it briefly adds a `mon0` VIF on each AP radio (note 1); it does so
  post-fire and removes it before the next shot (verified: the gate stays clean).

Real result from the spec above (2 APs, single frame, stagger 500, `ap_rssi:true`):
```json
{
  "per_ap": {
    "ref":      {"mac":"…33","station_id":1,"subframes_sent":12,"subframes_rx":8,"delivery":0.667},
    "follower": {"mac":"…74","station_id":2,"subframes_sent":12,"subframes_rx":7,"delivery":0.583}
  },
  "dups": 0,
  "sync_error": {"paired": 7, "median_us": 1, "max_us": 510},
  "rssi_at_monitor_dbm": {"…33": -51.3, "…74": -55.7},
  "rssi_ap_to_ap_dbm": {"…33": {"…74": -55.3}, "…74": {"…33": -55.3}},
  "shots": 12, "stagger_us": 500
}
```
(`sync_error.median_us` 1 µs is the real cross-AP alignment; the occasional large `max_us` is a
single mis-paired straggler, and delivery ~0.6 reflects whole-PPDU capture loss at one monitor —
see §7, not a gate fault.)

## 3. Command interface (the WMI write payload)

Host → firmware over WMI/HTC, id `WMI_COSR_GATED_TX_CMDID`. The driver exposes it as debugfs
`.../ath9k_htc/cosr_gated_tx`. Write payload — a 17-byte little-endian prefix + header template:
```
[8  target_tsf ]  absolute target TSF, LE (0 => fire immediately, ungated TX-path smoke test)
[1  rate       ]  HAL rateCode; 0 => firmware min-rate default; HT/MCS = 0x80|mcs
[1  txpower    ]  0..63; ACCEPTED BUT IGNORED by AR9271's RF (note 2) -- 0 => 63
[1  nframes    ]  A-MPDU subframes; 0/1 => a single frame
[1  stamp_off  ]  byte offset of the 6-byte id stamp inside each subframe
[1  station_id ]  logical destination id, written into the stamp
[2  seq        ]  base sequence number, LE, written into the stamp
[2  frame_len  ]  full length of each synthesized subframe, LE
[N  header     ]  24-byte 802.11 header template; the body is synthesized on-chip
```
The frame body cannot be shipped over WMI (`WMI_CMD_MAX_LEN` is 100 B), so the firmware
synthesizes each `frame_len` subframe on-chip from the header template, pads the body with `0x88`,
and writes a 6-byte id stamp `C0 5A <station_id> <seq_lo> <seq_hi> <subframe_idx>` at
`stamp_off`. A monitor counts delivered subframes by unique `(station_id, seq, idx)` — robust
under Co-SR collisions. Read a TSF: debugfs `netdev:*/tsf`.

Gate mechanics: reject targets > 600 ms ahead; a two-phase busy-poll (coarse wait with IRQ on so
beacons/USB keep being serviced, then a ~2 ms final approach with IRQ off) fires on the VO QCU
with carrier-sense and backoff disabled, so the frame keys at the target instant regardless of the
other AP. Frames are NOACK + a single try — a gated frame is never retransmitted (a retry
would fire ungated, outside the shared instant). `nframes > 1` builds one true HT A-MPDU (single
PPDU, N delimited subframes) with no Block-Ack; a passive monitor deaggregates it and counts each
subframe by its stamp.

## 4. Diagnostics (per-layer checks)

Capture on the monitor VM (`tcpdump -i <mon_iface> -s0 -w cap.pcap type data`) while firing:
```bash
# single-AP gate precision: fire N gated frames, then detrend commanded-T vs monitor tsft
AP_IP=10.0.0.11 AP_MAC=<ap_bssid> RATE=128 N=40 LEAD=300000 scripts/fire_cosr.sh
python3 scripts/gate_check.py cap.pcap <ap_bssid>          # residual median/max, late-fire flags

# two-AP sync error from any capture (pairs the two APs by seq stamp)
python3 scripts/skew.py cap.pcap <ref_bssid> <follower_bssid> [stagger_us]
```

## 5. Scripts

| file | role |
|---|---|
| `scripts/cosr_ctl.py` | the one call — `cosr_shot`/`measure`/`measure_multi` + CLI: fire N APs, count per-(AP,station) delivery, sync, RSSI |
| `scripts/offset_tracker.py` | runs on the observer VM; per-AP TSF offset from every AP's beacons → `/tmp/offset.json` (JSON) |
| `scripts/bringup.sh` | rebuild the rig after a VM reboot (APs + monitor, via `APS`/`MON` env) |
| `scripts/fire_cosr.sh` | single-AP gated fire (diagnostic; pairs with `gate_check.py`) |
| `scripts/gate_check.py` | single-AP gate precision: detrended commanded-T vs monitor tsft |
| `scripts/skew.py` | two-AP cross-AP sync error from a capture |

All Python is python3 (the tracker runs under the modwifi image's python3 on the monitor VM).

## 6. Implementation notes

Non-obvious constraints of the AR9271 / ath9k_htc platform and how the design accommodates them.
They document why the gate and tooling are built as they are, and matter to anyone extending or
porting the work.

1. **A monitor VIF rebases an AP's phy TSF.** Adding a `mon0` interface to an AP's radio shifts
   that phy's TSF and can perturb the on-chip gate, so APs remain single-VIF and the cross-AP
   offset tracker runs on a dedicated monitor node, off beacons. The lone exception is the
   optional `ap_rssi` measurement (§2), which adds `mon0` after firing and removes it before the
   next shot; this is verified not to disturb the gate.
2. **The descriptor TX-power field has no effect on the AR9271 RF.** The WMI `txpower` byte reaches
   the TX descriptor but does not change emitted power; real power is set via `iw dev <if> set
   txpower fixed <mbm>` (as `cosr_ctl.py` does, tracked monotonically by a monitor RSSI sweep). The
   field is retained for interface stability, capped ≤ 40, and documented as inert. The response's
   `send_tsf` is likewise unused (always 0), so the driver's `delta_us` print carries no meaning —
   only `status` is significant.
3. **Firmware loads only on USB re-enumeration**, not on `rmmod`/`modprobe`; a new driver against
   old resident firmware yields WMI error `-110`. Reflashing therefore requires an unplug/replug or
   a host-side USB reset.
4. **A failed firmware initialization drops the dongle off the USB bus** and is recovered by a
   host-side reconnect rather than a guest reboot; toggling the guest's `.../authorized` node can
   hang the VM.
5. **Carrier sense and backoff are disabled during a shot** (`AR_DIAG_FORCE_RX_CLEAR |
   AR_DIAG_IGNORE_VIRT_CS`, `AR_DLCL_IFS = 0`) so the APs fire without deferring to one another —
   the defining requirement for deliberate overlap — and are restored per shot. Interrupts are
   masked only for the final ~2 ms of the approach, so a beacon ISR cannot delay the spin exit,
   while the coarse wait keeps interrupts enabled so beacons and the watchdog survive a long lead.
6. **The target QCU is drained before the gate, not inside the fine window.** `ah_stopTxDma` can
   block for up to a frame airtime (~500 µs); performing it during the 2 ms final approach overran
   the target instant.
7. **The firmware avoids variable 64-bit shifts** (freestanding, no libgcc `__ashldi3`): the target
   TSF is assembled from bytes with constant shifts. The target CPU is big-endian.
8. **Gated subframes are allocated from `POOL_ID_ATTACKS`** and must be reclaimed through
   `attack_free_packet` / `cosr_free_ampdu`, or the pool is exhausted after a few shots. The pool
   (5 × 300 B) also bounds `nframes` and `frame_len`.
9. **`tcpdump -x` mis-dissects the `C0 5A` stamp as an LLC header and drops it**; captures are
   parsed with tshark full-hex (as `cosr_ctl.py` / `skew.py` / `gate_check.py` do). tcpdump BPF
   filters also miscompile on this radiotap link type, so captures are taken unfiltered and
   filtered in post-processing.
10. **Rig state is non-persistent** — hostapd and monitor configuration live in `/tmp` and are
    rebuilt with `bringup.sh` after a reboot.

## 7. Scope and limitations

What the testbed measures and where its measurements stop — read these before quoting a number.

- **A-MPDU proof.** The AR9271 monitor radiotap does not emit the A-MPDU-status TLV, so one PPDU
  is confirmed two independent ways: (a) TX descriptors — `bf_al` spans all N subframes with
  `IsAggr` on every subframe and `MoreAggr` cleared only on the last; (b) RX — the first N−1
  subframes share one exact PHY-RXSTART MAC timestamp (independent frames cannot), and the last
  subframe arrives in the same sub-µs band while only its radiotap `mactime` reads garbage (a
  monitor artifact for the final subframe, not a separate transmission). Do not read the last
  subframe's per-frame `mactime` as a real time.
- **Delivery.** Under deliberate overlap a single monitor drops whole PPDUs (all-N-or-nothing per
  shot), so per-shot delivery runs ~50–60%. This is a receiver-capture limit, not an aggregation
  or gate fault; do not report it as throughput without a better/second receiver.
- **Sync measurement needs matched frame types.** `sync_error` pairs the two APs on their first
  subframe's PHY-RXSTART timestamp (idx 0 — a later A-MPDU subframe's `mactime` is garbage, see
  above). Compare like with like: both APs single-frame, or both A-MPDU. A legacy/single frame
  and an HT A-MPDU are timestamped at different PPDU reference points, so mixing them adds a fixed
  offset (~the frame time) to the reported error. Matched single-frame sync is clean (median
  ~1–3 µs); the gate itself is proven to < 1 µs single-AP.
- **Differential propagation enters the measured sync.** The observer times each frame at
  PHY-RXSTART, i.e. TX-start plus the AP→observer path delay (~3.3 ns per metre). APs at
  unequal distances from the monitor therefore add an apparent offset to the reported cross-AP
  `sync_error` that is a property of geometry, not of the gate. When quoting a microsecond sync
  figure, keep the observer roughly equidistant from the APs or subtract the known path
  differences; the gate's own precision is bounded better by the single-AP absolute measurement,
  which has no cross-AP path term.
- **Idle-queue operating point.** The gate is characterized on idle/dedicated queues, which is the
  intended regime for coordinated firing. Deterministic release under a concurrently loaded queue
  is outside the current scope; the firmware marks where a just-before-fire re-check would slot in.
