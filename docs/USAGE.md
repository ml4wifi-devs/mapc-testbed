# Usage

Two JSON files describe everything: `topo.json` is the testbed, `experiment.json` is the experiment.
Every command takes them in that order.

## 0. Config files

**`topo.json`, the physical testbed (static).** Named nodes, the shared channel, the logins, the
broker token, and which node the timing measurements observe from.
```json
{
  "channel": 1,
  "token": "change-me",
  "ap_user": "modwifi",
  "ap_password": "modwifi",
  "monitor_user": "pi",
  "monitor_password": "raspberry",
  "sync": "beacon",
  "observer": "sta1",
  "nodes": {
    "apA":  {"role": "ap",      "ip": "10.0.0.11", "iface": "wlan0", "mac": "<apA_bssid>", "ssid": "cosrA"},
    "apB":  {"role": "ap",      "ip": "10.0.0.12", "iface": "wlan0", "mac": "<apB_bssid>", "ssid": "cosrB"},
    "sta1": {"role": "station", "ip": "10.0.0.21", "iface": "wlan0", "station_id": 1},
    "sta2": {"role": "station", "ip": "10.0.0.22", "iface": "wlan0", "station_id": 2}
  }
}
```

| field | meaning |
|---|---|
| `channel` | 2.4 GHz channel 1–13; every node shares it. |
| `token` | Shared secret for the broker. Must match what `nats-server` was started with. |
| `hub` | Optional. The address nodes dial to reach the broker. Omitted, the controller uses the address that faces the testbed. |
| `sync` | `"beacon"` (the APs hear each other) or `"monitor"` (receivers hear the APs). See [`SYNC.md`](SYNC.md). |
| `monitors` | With `sync: "monitor"`, the stations that contribute observations. Always list them explicitly. |
| `observer` | Where a coincidence measurement is taken. Defaults to the first link's receiver. |
| `station_id` | A station's identity, 0–255, stamped into the frames addressed to it. |

Credentials: APs use `ap_user`/`ap_password`, every `role: "station"` node uses
`monitor_user`/`monitor_password`. Each falls back to a flat `user`/`password`, so a testbed with
one login everywhere can set just `"password"`. The same login is used for both ssh and `sudo`.

The **first link's transmitter** is the timing reference that every other one is related to, and
the first link's receiver is the default `observer`. There is no master/slave field and no
hand-written graph: who-hears-whom is discovered at runtime. Keep the reference the same across
an experiment: a session holds one clock graph, anchored to the reference it started with, so
changing it mid-run is refused rather than silently re-anchored.

Interface names and addresses reshuffle on every USB re-enumeration. `cosr scan <ip> [ip ...]`
reads each node's current interface and address and prints paste-ready node stubs.

**`experiment.json`, the experiment.** The aggregate shape and the timing are common to every link, so
comparisons between links are valid. Rate and power are stated per link.
```json
{
  "nframes": 5, "frame_len": 300,
  "repeats": 20, "spacing_us": 15000,
  "stagger_us": 0, "lead_us": 400000,

  "links": [
    {"ap": "apA", "station": "sta1", "mcs": 7, "txpower_dbm": 14},
    {"ap": "apB", "station": "sta2", "mcs": 2, "txpower_dbm": 8}
  ]
}
```

| field | meaning |
|---|---|
| `nframes`, `frame_len` | The aggregate: one frame, or a true A-MPDU of `nframes` subframes. Common to every link. `nframes * frame_len` must fit the transmit pool (1500 bytes), so 5×300, 10×150 and 15×100 are the useful shapes. |
| `mcs`, `txpower_dbm` | **Stated on every link, with no common default.** These are what an experiment varies between links, the way one link is moved across the capture threshold while another is held fixed, so a link that omits either is refused rather than silently inheriting a value. `mcs` is 0..7 and `txpower_dbm` a whole number of dBm, 0..20; see §4 note 2. |
| `repeats` | How many shots the round contains. Statistics come from raising this, not from enlarging the aggregate. |
| `spacing_us` | Time between shots in a batch. Establish it with `calibrate`. |
| `stagger_us` | Deliberate separation between transmitters, per link position. `0` fires them together. |
| `lead_us` | How far ahead the shared instant is placed. See [`INSTALL.md`](INSTALL.md) §6. |

A link `apA -> sta1` makes apA transmit a stream stamped with sta1's identity, counted **at
sta1's own receiver**, for frames whose source address and stamped identity both match. Different
APs can therefore target different stations in the same round, and each station's number is its
own. One link per AP per round.

## 1. Bring the testbed up

```bash
cosr up     topo.json experiment.json    # radios up, node program placed and started
cosr status topo.json experiment.json    # what is running, and on which build
```

`up` starts the access points, puts the receivers into monitor mode on the shared channel, copies
the node program to every node, and starts it. It stops any previous copy first and refuses to
start a node whose copy does not match the controller's, so a half-updated testbed cannot quietly
produce results that look comparable.

## 2. Commands

```bash
cosr doctor    topo.json experiment.json   # the health verdict; read this before measuring
cosr run       topo.json experiment.json   # one round -> delivery, status and signal levels
cosr calibrate topo.json experiment.json   # the shortest inter-shot spacing that delivers
cosr reset     topo.json experiment.json   # clear node state without redeploying
cosr down      topo.json experiment.json   # stop the node program and the radios
cosr scan      <ip> [ip ...]               # read each node's interface and address
                                           #   [--user USER] [--password PW]
```

**`doctor`** is the one command to trust before an experiment. It checks every way the clocks can
drift apart, and reports a verdict per check:

| check | what it catches |
|---|---|
| `agents` | A node not answering, or running a different build from the controller. |
| `clock_graph` | A transmitter with no path to the reference clock; it cannot be commanded at all. |
| `extrapolation` | The relation is known, but not precisely enough for an instant this far ahead. |
| `path_consistency` | One observation contradicting the rest. Invisible to everything else, because the contradicting edge simply becomes the answer wherever the traversal used it. |
| `fire` | The gate refusing shots, and which side of its window they fell outside: already past (the instruction did not arrive in time), or too far ahead (the relation is wrong). |
| `coincidence` | How far apart the transmitters actually landed. |
| `divergence` | Whether that separation *grows* across a batch, which a summary of the spread cannot distinguish from noise. |
| `stability` | A relation that holds once and not again. |

A verdict is `PASS`, `FAIL`, `SKIP` or `NA`. **A skipped check never counts as a pass**: the
overall verdict becomes `INCONCLUSIVE`. An unrun check tells you nothing about the failure it
looks for, so treating it as a pass would be guessing. `doctor` exits non-zero unless everything
passed, so a script can stop on it.

`doctor` establishes the testbed at one moment. It cannot see a node that fails midway through a
long run; that is what the per-shot gate outcome and the named per-link status in *every* round
are for.

## 3. Results

A round returns one entry per link:

| field | meaning |
|---|---|
| `status` | `OK`, or a named reason the number is missing (below). |
| `delivery` | Subframes received / subframes sent, over the shots every participant transmitted. |
| `delivery_ppdu` | The same at aggregate granularity. Subframes of one A-MPDU share a preamble, so 20 shots of 5 subframes are ~20 independent trials, not 100. |
| `rx`, `sent` | The raw counts behind those fractions. |
| `fired`, `joint_shots` | Shots this AP transmitted, and shots *every* participant transmitted. Only the latter is a valid denominator for a coordinated result. |
| `rssi_dbm` | Mean level of this link's frames at the receiver. |
| `mcs_seen`, `mcs_note` | The rate that reached the air, where the receiver can report it. |
| `drops` | Frames the receiver's capture lost. Non-zero makes the count a lower bound. |
| `idx_hist` | Which subframe positions arrived, for checking aggregate integrity. |

A status is never a silent zero:

| status | meaning |
|---|---|
| `OK` | Measured. |
| `NOT_FIRED` | The gate refused every shot; nothing was transmitted, so this is not a loss. |
| `NOT_COUNTED` | Transmitted, but the receiver cannot bound the count: capture dead, wrong channel, its records evicted, or it restarted mid-round. |
| `UNKNOWN` | The command failed in a way that leaves it genuinely unknown whether the frames went out. |
| `WEDGED` | The radio's command interface failed. It needs attention before any further measurement. |

Every round also carries `rssi_ap_to_sta_dbm` and `rssi_ap_to_ap_dbm`, both keyed
`"<from>-><to>"`. The levels between transmitters come from the beacons those transmitters
already report hearing, so they cost no extra round and stay current between rounds. An absent
pair has not been heard, which is not the same as being out of range, so it is left out instead
of being reported as zero.

## 4. Implementation notes

Constraints of the AR9271 / `ath9k_htc` platform that are not obvious, and what they force.

1. **A monitor interface on an AP rebases that radio's clock.** Adding one shifts the phy's TSF
   and perturbs the gate, so APs stay single-interface. The levels between APs are read from the
   driver's beacon ring instead, which needs no monitor interface.
2. **The descriptor transmit-power field is clamped by the AR9271 RF.** With the interface power
   fixed and the descriptor byte swept 10/4/1 dBm, the received level stayed flat within 0.2 dB;
   sweeping the interface power 20/8/3 dBm moved it by 14 dB. Real power is therefore set through
   netlink on the interface; the descriptor byte cannot replace it. The interface accepts only
   whole dBm: anything else is discarded without an error and the previous power stays in force,
   which is why `txpower_dbm` is stated in whole dBm and refused otherwise.
3. **The gate response carries no send time.** Only `status` is meaningful:
   `0` fired, `1` no buffer, `2` the instant had already passed, `3` the instant was too far
   ahead, `4` the radio's clock stopped or was reset while the gate was waiting. The last is
   reported instead of hung on, and the shot leaves the denominator instead of counting as a
   loss.
4. **Firmware loads only on USB re-enumeration**, not on `rmmod`/`modprobe`. A new driver against
   old resident firmware gives WMI error `-110`.
5. **A failed firmware initialisation drops the dongle off the USB bus.** It is recovered by
   re-attaching the device from outside the machine, not from the node.
6. **A radio that has stopped receiving must not be "fixed" by restarting the interface.** The
   symptom is the beacon ring no longer advancing while the node answers normally; bringing the
   interface down in that state can stall the node in the driver's USB path. Report it and
   re-attach the device instead.
7. **Carrier sense and backoff are disabled during a shot**, so the APs fire without deferring to
   one another. They are restored once the transmit queue reports it has drained, not after a
   fixed delay: an aggregate at a low rate is milliseconds of airtime, and restoring the
   configuration while the radio is still keying could defer the rest of the frame. Interrupts
   are masked only for the final approach.
8. **The target queue is drained before the gate, not inside the fine window.** Stopping DMA can
   block for up to a frame's airtime, which would overrun the target instant if left until then.
9. **The firmware avoids variable 64-bit shifts**: the target time is assembled from bytes with
   constant shifts. The target CPU is big-endian.
10. **Gated subframes come from a dedicated pool** and are reclaimed after transmission, or the
    pool exhausts after a few shots. It also bounds `nframes * frame_len` to 1500 bytes.
11. **Node state is not persistent.** Access point and monitor configuration live in `/tmp`;
    `up` rebuilds it.

## 5. Scope and limitations

Read these before quoting a number.

- **`delivery` is the raw fraction at the receiver**, with no capture-ceiling normalisation. A
  passive receiver with imperfect capture reads below 1.0 on a clean link. Use a reliable
  receiver, and take a per-rate reference from an isolated single-link baseline.
- **The clock relation must be live.** With current observations the transmitters coincide to
  about a microsecond. If observations stop, the relation is extrapolated further and further and
  the error grows; `doctor` refuses rather than reporting a number in that state.
- **Coincidence measurement needs matched frame shapes.** Transmitters are paired on the first
  subframe's arrival timestamp; a later subframe of an aggregate shares it and carries no
  independent timing. Compare like with like, and prefer one frame per shot.
- **Differential propagation enters the measurement.** Arrival is timed at the receiver, so the
  path difference between transmitters (about 3.3 ns/m) is included. Place the observing receiver
  roughly equidistant, or subtract the known difference.
- **Transmit power saturates above about 14 dBm.** The top requests land on one level, so an axis
  reaching to 20 dBm has fewer distinct points than it looks like; below 14 dBm it scales
  faithfully. Where the ceiling sits varies by deployment and rate; find yours with
  [`EXPERIMENTS.md`](EXPERIMENTS.md) E8 before using power as an experimental axis.
- **Idle-queue operating point.** The gate is characterised on idle, dedicated queues, which is
  the intended regime for coordinated firing. Deterministic release under a concurrently loaded
  queue is out of scope.
