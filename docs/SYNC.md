# Relating the transmitters' clocks

Coordinated transmission needs one shared instant expressed in each transmitter's own free-running
clock. This document describes how that relation is established, why the two ways of establishing
it are the same mechanism, and how to check that it holds.

## One mechanism, two sources of observation

Everything reduces to a set of directed affine **edges** between clocks. An edge `(X, Y)` means
`Y = a + b·X`. Composing edges outward from the reference clock gives every transmitter a map, and
that map converts one shared instant into the local time each transmitter is commanded with.

Where the edges come from is an input, not a separate design:

- **`sync: "beacon"`**: each AP hears its peers' beacons. A beacon carries the sender's own clock
  in its body, and the hearer stamps a receive time, so one observation gives both halves. The
  frame's own on-air duration is subtracted to land back on the transmit instant, computed from
  the rate and length the driver reports, so nothing needs calibrating per deployment.
- **`sync: "monitor"`**: a receiver hears several APs. Relating two of its own fits cancels the
  receiver's clock entirely, so this path needs no duration correction at all. More receivers
  simply mean more edges.

Both publish the same shape of observation, and one solver consumes whatever is arriving. A
deployment can use either, or both at once, and extra receivers just add more edges.

## What has to be true

- **The graph must reach the reference.** With `beacon`, every AP must be reachable through peers
  it can hear; with `monitor`, through APs that share a receiver. An otherwise isolated AP needs a
  bridge: a peer in range, or a receiver that also hears a connected AP.
- **Observations must keep arriving.** The relation is extrapolated to the commanded instant, and
  the further it is extrapolated the less it is worth. How long a fit stays usable comes from the
  fit's own uncertainty, not from a fixed timeout, and once that budget is spent firing is
  refused outright.
- **The transmitters that matter are the ones that fire together**: interfering neighbours, in
  range of each other, about one hop apart. Composition therefore stays shallow across the pairs
  whose alignment is being measured.

## Why the controller never reads a clock

The obvious design has the controller ask a transmitter for its current clock before each round.
That reading costs a slow round trip on the radio's command interface, and issuing one
immediately before a gated write is what wedges a card. It is also unnecessary.

The same shared instant is mapped through *every* transmitter's own map, so an error in choosing
that instant moves all of them together: the round happens slightly early or late, but the
transmitters do not pull apart. What is left is the error in the *slopes* multiplied by the size
of that choosing error, which in practice is negligible.

So the instant only has to land inside the window the gate will accept, which is coarse. The
reference clock's own readings arrive continuously in the beacon stream, and receiving any frame,
from anyone, is a reading of the hearing node's clock. Even a deployment with a single
transmitter and no peer to hear can still place an instant.

## Checking that it holds

`cosr doctor topo.json experiment.json` reports the state of the clock plane and measures it. The
checks that bear directly on this document:

- **`clock_graph`**: every participating transmitter is reachable from the reference.
- **`extrapolation`**: the relation is precise enough for an instant this far ahead.
- **`path_consistency`**: independent routes through the graph agree. Nothing else can see a
  contradicting observation: a single traversal simply adopts whichever route it took, so a wrong
  edge becomes the answer. Removing each edge in turn and re-solving exposes it. Where only one
  route reaches a clock, that is reported as uncorroborated rather than as agreement.
- **`coincidence`**: how far apart the transmitters actually landed, measured at one receiver so
  that the receiver's own clock cancels.
- **`divergence`**: whether that separation grows across a batch. A fixed offset stays bounded; a
  difference in *rate* accumulates without limit, and a summary of the spread alone cannot tell
  them apart because both merely widen it.
- **`stability`**: whether the same relation is obtained twice, a few seconds apart.

## Can this receiver time anything?

Every coincidence figure is measured at one receiver, so whether that receiver can time at all
matters. This is not something to declare in a config file and hope: it is measured, and
`doctor` reports it every run.

```
coincidence  PASS  1.73 us apart, 1.18 us spread (against own instant 45.70 us)
```

Read the two numbers together.

- **spread**: how far apart the transmitters landed, compared at this one receiver. Because
  both are stamped by the same clock, that clock cancels.
- **against own instant**: each transmitter's arrivals compared with the instants it was
  commanded to use, with a constant offset and a slow drift removed. These two readings come
  from *different* clocks, so what remains includes whatever the receiver's clock does relative
  to the transmitter's beyond a straight line.

The example above is a real measurement, and the two figures look inconsistent: 45.70 µs of
residual against only 1.18 µs of spread. If the receiver were mis-stamping individual frames by
45 µs, the spread could not be 1.18 µs. That residual is therefore almost entirely *common*
wander between the two clocks: it moves both transmitters alike and cancels when they are
compared, so it says nothing about whether they coincided.

**How to read the pair:**

| against own instant | spread | meaning |
|---|---|---|
| small | small | everything is well behaved |
| large | small | clock wander that cancels; coincidence is unaffected |
| large | large | per-frame noise, which does not cancel; this receiver cannot time |
| no number at all | n/a | the receiver reports no per-frame time; the check fails and says so |

Only the third row indicts the receiver, and `doctor` says so in those words.

**If the receiver is the problem.** In order of how often it helps:

1. Use a different receiver as the `observer`. The transmit side is fixed to one chipset; the
   receive side is not, and timestamping quality varies far more between drivers than between
   cards.
2. Keep one frame per shot for timing work. Later subframes of an aggregate share the opening
   one's arrival time, so they add noise and no signal.
3. Place the observer roughly equidistant from the transmitters, or subtract the known path
   difference: propagation enters at about 3.3 ns per metre.
4. Check `path_consistency` first: a contradicting observation inflates the displacement, and
   no receiver quality compensates for a wrong clock relation.

Delivery counting needs only a signal level, not a timestamp, so a receiver that cannot time is
still perfectly usable for everything else.

## Each source on its own

Both sources feed one graph, so a deployment that has both gets both. To check that either
carries the clock *alone*, we disabled the other at the controller and fired a coordinated round
on what was left:

| clock source | edges recovered | displacement | spread |
|---|---|---|---|
| `monitor`, receivers hear the transmitters | 1 | −0.67 µs | 1.37 µs |
| `beacon`, transmitters hear each other | 2 | −1.20 µs | 0.83 µs |

Neither is much better than the other on this hardware. Choose on what the deployment can
supply: `beacon` needs the transmitters within range of one another, `monitor` needs a receiver
that hears them and radiotap timestamps you trust.

`doctor` reports which source is actually arriving (`via beacon`, `via monitor`, or both), and
fails if the `sync` field names one that is not.

## Measured behaviour

On a two-transmitter deployment with one timing-capable receiver, we fired a staggered round
from both and stamped each arrival at that receiver:

| quantity | typical | worst seen |
|---|---|---|
| displacement between transmitters | 0.1–1.2 µs | 2.9 µs |
| spread around it | 0.6–1.7 µs | 8.2 µs |
| change in separation across a batch | 0.00–0.07 µs/shot | 0.24 µs/shot |
| agreement between two measurements seconds apart | 0.2–1.4 µs | 2.4 µs |
| disagreement between independent routes | 0.9–2.1 µs | 2.1 µs |

The rate of change is the figure that matters for whether the transmitters *stay* aligned: at
these values the separation accumulates by around a microsecond across a batch of tens of shots,
with no consistent sign, so the transmitters do not diverge. The route disagreement is about twice
the residual error in the constant relating where the radio samples a transmitted timestamp to
where it stamps a reception, which is consistent with the displacement observed directly.

These are properties of the hardware in use, not constants of the design. Measure them on your
own deployment; `doctor`'s thresholds are arguments so you can set them from what you measure.
