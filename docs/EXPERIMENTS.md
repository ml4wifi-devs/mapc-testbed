# Experiments

A runbook for producing the two quantities a coordinated-spatial-reuse model consumes, signal
levels and per-link success under a given configuration, and for iterating on them: give a
configuration, get the measurement, give the next.

Prerequisite: the testbed is up and healthy (`cosr up`, then `cosr doctor`; see
[`INSTALL.md`](INSTALL.md) and [`USAGE.md`](USAGE.md)).

## The interface

`run` is the configuration → measurement call, for any number of transmitters and receivers.
The testbed is described once in `topo.json` and each experiment in an `experiment.json`; a link names a
transmitter and the receiver it transmits to. Because the receiver's identity is stamped into the
frames and delivery is counted at that receiver, the result is a spatially resolved matrix.

```bash
cosr run topo.json experiment.json
```
```python
import json
from cosr import cli, session, topo

testbed = json.load(open("topo.json"))
plan = topo.resolve(testbed, json.load(open("experiment.json")))
sess = session.Session(plan, hub=cli.hub_for(plan), token=plan["token"]).connect()
sess.wait_for_agents()
sess.wait_for_clock()
try:
    for cfg in my_configs:
        sess.use_plan(topo.resolve(testbed, cfg))   # one session for the whole run
        model.update(sess.run())
finally:
    sess.close()
```

Build the session **once**. The relation between the node clocks belongs to the deployment, and
it needs a span of observations before an instant can be placed at all; rebuilding the session
each round throws that span away and leaves the opening rounds of every run unusable. `use_plan`
changes which links take part and keeps the rest.

Returns, per link (see [`USAGE.md`](USAGE.md) §3 for every field):

```
links:               { "<ap>-><sta>": {status, delivery, delivery_ppdu, rx, sent,
                                       fired, joint_shots, rssi_dbm, mcs_cmd, mcs_seen, ...} }
rssi_ap_to_sta_dbm:  { "<ap>-><sta>": dbm }
rssi_ap_to_ap_dbm:   { "<ap>-><ap>":  dbm }
warnings:            [ ... ]
```

## Read these before trusting a number

- **Check `status` on every link, every round.** A missing number is never reported as zero; it
  carries a reason. `NOT_FIRED` means nothing was transmitted and is not a loss. `NOT_COUNTED`
  means the receiver could not bound the count. Only `OK` is a measurement.
- **`delivery` is raw.** For a channel or collision success probability, divide a concurrent
  link's delivery by its isolated baseline (E2) at the same rate and power.
- **Use the right denominator.** `delivery` is already computed over the shots every participant
  transmitted. If you recompute anything yourself, use `joint_shots`, not `fired`.
- **Check `warnings`.** A commanded rate the transmitter's table lacks downgrades silently to
  legacy. Each round compares what was commanded against what reached the air and says so,
  wherever the receiver can see it.
- **Run `doctor` before a long run**, and stop on a non-zero exit. It cannot see a node failing
  midway, which is what the per-round statuses are for.

## Recipes

Each is: edit `experiment.json`, call `run`, read the fields. `topo.json` stays fixed.

### E0: Health first
```bash
cosr doctor topo.json experiment.json
```
Everything must pass. An `INCONCLUSIVE` means a check did not run. Find out why before
proceeding, because an unrun check is not a passed one.

### E1: Spacing for your hardware
Use a shot with a **single link**: concurrent transmitters collide by design, so a multi-link
sweep measures their overlap rather than the spacing.
```bash
cosr calibrate topo.json one-link.json
```
Sweeps the interval between shots and reports the shortest one that reaches the link's best
delivery **and holds it at every longer interval too**. Judged on frames that arrived, never on shots the gate
accepted: acceptance means scheduled, not transmitted, and the transmit pool holds one aggregate
at a time.

The reference is the link's own best, not an absolute fraction, because a passive receiver does
not capture everything even on an idle channel. Read the printed table, not just the conclusion:
if the numbers jump around with no threshold in them, something other than spacing is moving the
link and no spacing will fix it. Raise the repeat count on a noisy link.

Put the result in `experiment.json` as `spacing_us`. It depends on the aggregate shape, so redo it if
you change `nframes` or `frame_len`.

### E2: Isolated-link baseline (the normaliser)
Run each transmitter alone, at the exact rate and power you will use concurrently. Record
`delivery`. This is the interference-free, rate-matched reference; a concurrent delivery divided
by it is the channel success probability.

### E3: The coordinated case
Both transmitters fire at one shared instant (`stagger_us: 0`), to different receivers:
```json
{ "nframes": 1, "frame_len": 200,
  "stagger_us": 0, "lead_us": 400000, "spacing_us": 15000, "repeats": 30,
  "links": [ {"ap": "apA", "station": "sta1", "mcs": 0, "txpower_dbm": 14},
             {"ap": "apB", "station": "sta2", "mcs": 0, "txpower_dbm": 14} ] }
```
At full overlap one transmitter wins the capture effect at each receiver, so a low delivery here
is the phenomenon under study, not a fault. Divide by the E2 baseline for the per-link loss, and
sweep per-link `txpower_dbm` and `mcs` to move a link across the capture threshold.

### E4: Overlap sweep (collision → clean)
Fix everything else and sweep `stagger_us` (0, 100, 300, 600, 1000 µs). Delivery is lowest at full
overlap and recovers as the transmissions separate past one frame time.

### E5: Aggregated payload
Raise `nframes` and `frame_len` for one true A-MPDU per shot. The transmit pool bounds
`nframes * frame_len` to 1500 bytes, so 5×300, 10×150 and 15×100 are the useful shapes; anything
larger is refused when the plan is resolved rather than failing on the hardware. Read
`delivery_ppdu` alongside `delivery`: subframes of one aggregate share a preamble and are not
independent trials.

### E6: Scale out
Add nodes to `topo.json` and links to `experiment.json`. One instruction reaches every node, so the
round does not lengthen with the number of transmitters, and `lead_us` does not need to grow with
it. One round returns the whole matrix.

### E7: The levels between transmitters
`rssi_ap_to_ap_dbm` is populated from the beacons the transmitters already report hearing, so it
needs no round of its own and stays current. A pair that is absent has not been heard, which is
not the same as being out of range. It is directional: what one hears from another need not equal
the reverse.

### E8: Is transmit power actually an axis?
Before using power as an experimental variable, establish that the levels you intend to command
are distinguishable at the receiver. Fire one link, sweep `txpower_dbm` over the levels you plan
to use, and read `rssi_ap_to_sta_dbm` at each:

```python
for dbm in (20, 17, 14, 11, 8, 5):
    sess.use_plan(topo.resolve(testbed, experiment_with_power(dbm)))
    print(dbm, sess.run()["rssi_ap_to_sta_dbm"]["apA->sta1"])
```

Sweep in both directions, at every rate you intend to use, and measure at short range so fading
does not swamp the steps.

**Start the axis at 14 dBm, not at 20.** Above 14 dBm the output saturates, so the top requests
land on one level and the response stops scaling with what is commanded; at and below 14 dBm the
steps are faithful and the same at every rate. Power is stated in whole dBm, 0..20: the radio
honours no finer granularity and silently discards a request it cannot represent, leaving the
previous power in force. `topo.resolve` refuses anything else.
