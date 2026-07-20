# Architecture

This document explains the full pipeline and code responsible for each part. There are two subsystems:

1. **The fire pipeline** — one Co-SR shot, host → air → counted result.
2. **The timing subsystem** — cross-AP clock alignment, running continuously, that lets a
   single shared instant be expressed in each AP's own TSF.

The core idea: the TX release gate lives *on the AR9271 chip*, where it busy-polls the
on-chip TSF and keys the frame out at µs precision. Host-side timing cannot do this — USB
scheduling jitter (±2 ms) sits after any host decision. Beacons relate the APs' free-running
TSFs over the air, so one target instant maps to each AP; each frame is NOACK + single-try and
carries a unique id stamp.

## Pipeline overview (one shot)

```
              cosr_ctl.py (controller host)
                   │
   read ref TSF    │        ┌── /tmp/offset.json ◄── offset_tracker.py (monitor VM, off beacons)
                   ▼        ▼
   Tm = now+lead ; per AP: target = shared instant mapped into THAT AP's TSF
                   │
   blob() builds 17B LE prefix + 24B header template
                   │  ssh: printf '\xNN..' > .../cosr_gated_tx   (parallel threads, one per AP)
                   ▼
   DRIVER  write_file_cosr_gated_tx()          (htc_drv_debug.c)
                   │  repack prefix → struct wmi_cosr_gated_tx_cmd ; WMI_COSR_GATED_TX_CMDID
                   ▼
   FIRMWARE  ath_cosr_gated_tx()               (if_ath.c)
       ├── parse target/rate/nframes/frame_len/stamp
       ├── build frame(s):  cosr_build_frame() / cosr_build_ampdu()   (attacks.c / if_owl.c)
       ├── drain VO QCU up front
       ├── GATE: busy-poll on-chip TSF  (coarse IRQ-on → fine IRQ-off, CS-off, backoff=0)
       └── at target TSF:  ath_tgt_txqaddbuf(VO queue)  ── RF emission ──►
                   ▼
   MONITOR VM   tcpdump → cosr_shot.pcap
                   ▼
   cosr_ctl.parse_frames()  → count unique (station_id, seq, idx) stamps
                   ▼
   result dict: per-AP (or per-(AP,station)) delivery, cross-AP sync_error, rssi
```

## Stage 1 — Host controller (`scripts/cosr_ctl.py`)

`cosr_shot(spec)` is the single-observer entry point; `measure()` wraps it for the model interface
and dispatches to `measure_multi()` when APs carry `stations` (per-(AP,station) matrices, any number
of APs). The shared timing loop `_do_shots()` is the one proven fire path, reused by both.
Responsibilities by helper:

| function | responsibility |
|---|---|
| `cosr_shot()` | orchestrate a single-observer shot: start capture → `_do_shots` → parse → result |
| `measure_multi()` | any-N-AP, variable-stations-per-AP: capture per station node → per-(AP,station) matrices |
| `_do_shots()` | the timing-critical fire loop shared by every capture topology |
| `read_tsf(ip)` | read an AP's TSF (debugfs `netdev:*/tsf`); the reference AP's is the timing base |
| `_read_offsets()` | read JSON `/tmp/offset.json` → per-AP `(offset, slope)` (retries on transient empty) |
| `blob()` | build the WMI wire — the 17-byte LE prefix + 24-byte 802.11 header template |
| `write_node()` | ssh `printf '\xNN..' > cosr_gated_tx` — the actual per-AP trigger |
| `set_txpower()` | set TX power via `iw` (the WMI `txpower` byte is ignored by AR9271) |
| `cap_start` / `cap_pull` | start / retrieve the monitor capture on the monitor VM |
| `parse_frames()` | tshark full-hex → every `C0 5A` stamp = `{sa, tsft, station_id, seq, idx}` |
| `rssi_at_monitor()` | mean radiotap signal (dBm) per AP MAC, from beacons |

**Timing-critical path (per shot):** `read_tsf(reference)` → `Tm = now + lead` → per AP compute its
target → fire every AP's `write_node` **in parallel threads**. Threading matters: serial writes
add ~1 ssh RTT per AP, which for a follower pushes its target into the past (LATE). Everything
non-critical — `set_txpower`, capture start, offset refresh (every 10 shots) — is kept out of
this path. The offset is cached and drift-extrapolated to the fire instant rather than re-read
per shot.

## Stage 2 — Driver (`driver.diff`: `htc_drv_debug.c`, `wmi.h`, `mac80211/debugfs_netdev.c`)

The driver is a passthrough — no timing, no frame building. It adds one debugfs file
(`cosr_gated_tx`) plus a one-line mac80211 change exposing the `tsf` debugfs node on AP
interfaces (stock mac80211 exposes it only for IBSS/mesh; Stage 1 `read_tsf` needs it on an AP).
`write_file_cosr_gated_tx()`:

- reads the 17-byte prefix + header template from userspace,
- unpacks each scalar into `struct wmi_cosr_gated_tx_cmd`: `target_tsf` passed through as raw LE
  bytes (no endian assumption on either side), `frame_len` → `cpu_to_be16` (firmware does
  `ntohs`), trailing bytes copied into `data[]` as the header template,
- issues `WMI_COSR_GATED_TX_CMDID` over WMI/HTC and prints the response `status`.

`wmi.h` adds the command id and the `cmd`/`resp` structs so host and firmware agree on the wire.
Note: the response's `send_tsf` is never filled by firmware, so the driver's `delta_us` print is
meaningless — read only `status`.

## Stage 3 — Firmware handler (`firmware.diff`: `if_ath.c :: ath_cosr_gated_tx`)

The heart of the system. Sequence:

1. **Parse** the target TSF by assembling bytes with constant shifts (the firmware is
   freestanding — no libgcc `__ashldi3` for a variable 64-bit shift; the target CPU is
   big-endian, so the LE bytes are combined via a union). Also read rate, txpower, nframes,
   stamp fields, `frame_len`, `datalen`.
2. **Reject** bad targets: `COSR_GTX_LATE` (now ≥ target), `COSR_GTX_TOOFAR`
   (target − now > `COSR_LEAD_MAX_US` = 600 ms).
3. **Build** the frame(s): `nframes > 1` → `cosr_build_ampdu()` (freed by `cosr_free_ampdu`);
   else `cosr_build_frame()` (freed by `attack_free_packet`). The completion ISR calls `bf_comp`.
4. **Drain the VO QCU up front** — `ah_stopTxDma` + `ath_tx_draintxq` so our frame is the sole
   descriptor at the target. Done here, not in the fine window, because `ah_stopTxDma` can
   block up to a frame airtime (~500 µs) — inside the 2 ms final approach that overran the target.
5. **The gate** — two-phase busy-poll on `ah_getTsf64`:
   - **coarse wait, IRQ on**, until `COSR_FINE_US` (2 ms) before target — beacons, USB/HTC
     servicing, and the watchdog all keep running, so the lead can be hundreds of ms;
   - **IRQ off** for the final approach so no ISR delays the spin exit and fires late;
   - **CS off + backoff = 0** (`AR_DIAG_SW |= FORCE_RX_CLEAR|IGNORE_VIRT_CS`, `AR_DLCL_IFS(q)=0`
     for all queues) so the DCU cannot defer the frame — required for overlap, since the APs
     must fire without deferring to each other; saved and restored around the shot;
   - `while (getTsf64 < target);` → exit exactly at the target.
6. **Fire** — `ath_tgt_txqaddbuf(sc, VO_txq, bf, bf->bf_lastds)`, the AP's real data-TX path
   (does `setTxDP(ours)` + `startTxDma`). This is the only path proven to emit in AP mode; the
   VO (`WME_AC_VO`) queue has the highest DCU arbitration priority and is idle in practice.
7. **Restore** — spin to `target + 300 µs` (frame keys out first), then restore DIAG/IFS/IRQ.
8. **Reply** with `status`.

## Stage 3b — Frame builders

**`attacks.c :: cosr_build_frame()`** — one subframe, body synthesized on-chip (the full frame
cannot be shipped: `WMI_CMD_MAX_LEN` is 100 B):

- allocate a `frame_len` buffer from `POOL_ID_ATTACKS`,
- copy the 24-byte 802.11 header template, pad the body with `0x88`,
- write the 6-byte id stamp `C0 5A <station_id> <seq_lo> <seq_hi> <subframe_idx>` at `stamp_off`,
- set the descriptor: rate (resolved from HAL rateCode via `cosr_rix_for_ratecode`; `0` → min
  rate), txpower (accepted, ignored by AR9271 RF), `series[0].Tries = 1`, and
  `HAL_TXDESC_NOACK` → the hardware sends once and never waits for an ACK ⇒ **no
  retransmission** (a retry would fire ungated, outside the shared instant).

**`if_owl.c :: cosr_build_ampdu()`** — N subframes laid out as HT PPDU:

- calls `cosr_build_frame()` per subframe (idx = 0..N−1, so each subframe is uniquely stamped),
- lays them out with delimiter/padding accounting + `ds_link` chaining +
  `set11nAggrFirst/Middle/Last` + `bf_al`/`bf_ndelim` — the same HW aggregation primitives as
  `ath_tgt_tx_form_aggr`, but **without** the TID/BAW/seqno/Block-Ack scheduler. No negotiated
  aggregation and no retransmission; a passive monitor deaggregates the PPDU by walking
  delimiters and still counts each subframe's stamp.

**`if_owl.c`** also de-`static`s `ath_tgt_txqaddbuf` (so `if_ath.c` can reuse the real TX path)
and adds `cosr_free_ampdu`, which walks the `bf_next` chain to return every subframe to the pool.

## Stage 4 — Air → monitor

The frame(s) key out overlapping (stagger 0) or separated (stagger > 0). The monitor VM in monitor mode
captures all data frames with radiotap; each frame carries a `tsft` (PHY-RXSTART on one common
clock).

## Stage 5 — Count (`cosr_ctl.parse_frames`)

Parsed with tshark full-hex: tcpdump `-x` mis-dissects the `C0 5A` stamp as an LLC header
and drops it. Per frame, find `c05a`, extract `(station_id, seq, idx)` plus `sa` and `tsft`.
Delivery = number of unique `(sa, station_id, seq, idx)`; duplicates counted separately. This
is robust under Co-SR collisions — a lost frame is simply an absent id, not a miscount.

---

## Timing subsystem — cross-AP clock alignment

This is why AP mode is required. It runs continuously, entirely off the fire critical path.

**`scripts/offset_tracker.py`** (runs on the TSF observer — any monitor-mode card, dedicated or a
station card, that hears every AP's beacons):

- tcpdump every AP's beacons (`wlan[0] == 0x80`),
- each beacon carries the sender AP's own TSF (first 8 body bytes, little-endian) and is
  stamped with the observer's radiotap `tsft` — one common clock for all APs,
- per-AP least-squares fit `ap_tsf = a + b · tsft_obs` over a sliding window,
- writes `/tmp/offset.json` as JSON: `{ref_tsf, mon_tsft, offsets:{<mac>:[offset,slope]}}`, where for
  each AP `offset = ap_tsf − ref_tsf` at the reference AP's current time and
  `slope = d(offset)/d(ref_tsf)`. The reference (`aps[0]`) is present as `[0, 0.0]` (maps to itself).
  The per-AP arithmetic is identical to the original two-AP form, so the microsecond timing carries
  over unchanged; only the wire format generalizes from one follower to N.

**How the controller uses it** (per shot, per AP `k`; reference `k = 0` has `offset = slope = 0`):

```
target_AP_k = round( Tm + offset + slope·(Tm − ts) + stagger·k )
```

Take the one shared instant `Tm` (reference clock), map it into AP `k`'s own TSF via the offset plus
a drift extrapolation to the exact fire instant. `stagger = 0` ⇒ both APs hit the same real
instant ⇒ deliberate on-air collision (capture effect proves overlap). `stagger > 0` ⇒ a
controlled offset for an overlap/stagger sweep.

**Why not a monitor VIF on the AP:** adding `mon0` to an AP's phy rebases that phy's TSF
(multi-VIF) and corrupts the on-chip gate, and also corrupts VO-queue TX. So the tracker must
live on the dedicated monitor VM, off beacons — never on an AP. Every AP stays single-VIF.

**Why AP mode at all:** (1) beacons carry the TSF the tracker needs; (2) the firmware build path
(`cosr_build_frame`) requires a valid vap + node context (`sc_vap[0]` / `sc_sta[0]`), which
hostapd's BSS provides — pure monitor injection has no such context and the build fails. IBSS
also beacons but merges TSF (nodes adopt the max), which fights the free-running independent-
clock model; AP mode gives each AP its own free clock.

---

## Diagnostics (not in the pipeline)

- **`scripts/skew.py`** — two-AP cross-AP sync error from any capture: pairs the two APs by the
  seq in the stamp, reports `(tsft_follower − tsft_reference) − stagger` (median/mean/max/std). This is
  the relative number.
- **`scripts/fire_cosr.sh` + `scripts/gate_check.py`** — single-AP absolute gate precision:
  `fire_cosr.sh` fires N gated frames and logs `seq commanded_T`; `gate_check.py` pairs each with
  the monitor `tsft`, detrends the constant offset + slow drift between the two clock domains, and
  flags residual excursions (a late fire spikes off the ramp). This is the direct evidence for the
  headline single-AP precision.

---

## Wire format (host ↔ driver ↔ firmware)

17-byte little-endian prefix, then the 802.11 header template (body synthesized on-chip):

```
offset  size  field         meaning
  0      8    target_tsf    absolute target TSF, LE (0 => fire immediately, ungated smoke test)
  8      1    rate          HAL rateCode; 0 => firmware min-rate default; HT/MCS = 0x80|mcs
  9      1    txpower       0..63; ACCEPTED BUT IGNORED by AR9271 RF (real power via iw)
 10      1    nframes       A-MPDU subframes; 0/1 => single frame
 11      1    stamp_off     byte offset of the 6-byte id stamp inside each subframe
 12      1    station_id    logical destination id, written into the stamp
 13      2    seq           base sequence number, LE, written into the stamp
 15      2    frame_len     full length of each synthesized subframe, LE
 17      N    header        24-byte 802.11 header template
```

Id stamp written into each synthesized subframe at `stamp_off`:
`C0 5A <station_id> <seq_lo> <seq_hi> <subframe_idx>` (6 bytes). The monitor counts delivery by
unique `(station_id, seq, subframe_idx)`.

Response status codes (`enum COSR_GATED_TX_STATUS`): `0` FIRED, `1` NOBF (no tx buf), `2` LATE
(command arrived after target), `3` TOOFAR (> 600 ms ahead).

---

## Where to change things

| you want to… | change |
|---|---|
| add/adjust a shot's rate, power, size, stagger, lead | `spec.json` fields consumed by `cosr_ctl.py` |
| change how the shared instant maps to each AP | `cosr_shot()` target formula + `offset_tracker.py` |
| change the gate timing / CS / IRQ behaviour | `ath_cosr_gated_tx()` in `if_ath.c` (rebuild + reflash) |
| change frame synthesis / stamp / no-retx | `cosr_build_frame()` in `attacks.c` (rebuild + reflash) |
| change A-MPDU layout | `cosr_build_ampdu()` in `if_owl.c` (rebuild + reflash) |
| change the wire (add a field) | `wmi.h` (both trees) + driver `write_file_cosr_gated_tx()` + `blob()` |

Any firmware/driver change means a rebuild and a reflash, which needs USB re-enumeration
(see [`USAGE.md`](USAGE.md) notes 3–4). Host-only changes (`cosr_ctl.py`, trackers, diagnostics)
need no reflash.
