# Architecture

The transmit-release decision lives in the radio firmware, not on the host. Everything above it
decides what to transmit and when, gets that decision out in time, and counts what arrived. None
of it sits on a microsecond path.

## One round

A round is **one published instruction and one reply per node**. Nodes subscribe; nothing is
polled, and nothing is copied back after the fact.

```
 CONTROLLER                                    broker (token auth)
 ──────────                                         ▲
  topo.json + experiment.json ── resolve ──► plan         │  nodes dial OUT: no listening
                   │                                │  ports, no per-node config
  clockd: live clock graph ◄── cosr.beacon ─────────┤
                   │  shared instant -> per-AP target
                   ▼
  publish ONE cosr.fire  ─── every node subscribes ───┐
  cosr.status.* ◄── per-AP per-shot outcome vector    │
  cosr.report.* ◄── per-receiver counts, auto-pushed  │
        ┌───────────────────────────────────────────-─┘
        ▼                                       ▼
 TRANSMITTER (root)                      RECEIVER (root)
  one lock over the radio's               continuous raw capture
  command interface                       (run, source, seq) -> subframes seen
  set power via netlink                   + level, channel, drop counter
  N gated writes, no network in the loop  + frames that are not ours (liveness)
  read each outcome from the kernel log   -> publish when the expected count
  publish the outcome vector                 arrives or the deadline passes
  forward beacon observations continuously
```

The instruction carries everything both roles need: the run and batch identity, the base sequence
number, the shot count and spacing, each transmitter's own target time, the rate, aggregate shape
and power, and the set of sources each receiver should expect. Receivers derive their own reply
deadline from it. A small request/reply layer exists only for status and control.

## Stage 1: Controller

`cosr/topo.py` resolves the two config files into a plan, refusing anything that could only fail
later against the hardware. `cosr/clockd.py` keeps a live graph relating every transmitter's clock
and turns one shared instant into a per-transmitter target (see [`SYNC.md`](SYNC.md)).
`cosr/session.py` builds the instruction, awaits the replies, and combines them.

The controller never reads a clock from a radio on the fire path. Doing so costs a slow round trip
on the command interface, and issuing one immediately before a gated write is what wedges a card.

## Stage 2: Node program

`cosr/agent.py` is one source file serving both roles, running on the Python the node images
provide and using only the standard library.

A **transmitter** serialises every use of the radio's command interface behind one lock, sets
power through netlink, then performs one blocking write per shot. Each write returns when its gate
fires, so nothing on the network is in the per-shot loop. The outcome of each shot is read from
the kernel log and attributed to that shot, and the whole vector is published at the end.

A **receiver** captures continuously from a raw socket, parses radiotap itself, and counts our
frames by `(run, source, sequence, subframe)`. It also counts the frames that are not ours: with
ambient traffic on any real channel, that separates "capture alive, saw none of ours", which is
a real zero, from "capture dead", which is no zero at all.

## Stage 3: Driver

`driver.diff` adds a debugfs node that repacks the instruction into a WMI command, and a read-only
ring recording every beacon heard with the sender's clock, the local receive time, and the level.
That ring is what relates the clocks and what supplies the levels between transmitters, so neither
needs a monitor interface on an AP, which would rebase the very clock the design depends on.

## Stage 4: Firmware gate

`firmware.diff` adds the handler that does the actual work:

- parse the target, rate, aggregate shape and stamp parameters;
- build the frames on-chip, stamping each with an identity and sequence;
- drain the target queue **up front**, because stopping DMA can block for a frame's airtime;
- gate: busy-poll the on-chip clock, coarse with interrupts on, then fine with interrupts off,
  carrier sense disabled and backoff zero, so the radio does not defer to a peer;
- at the target, hand the frames to the transmit queue.

Gated frames are never retransmitted, so a delivery figure is a first-attempt figure.

## Stage 5: Counting

Only the shots that **every** participant transmitted are counted. Per-transmitter counts are the
wrong denominator: if one fired 20 and another 17, three of those shots had no coordination at
all, and counting them mixes single-transmitter results into a coordinated figure.

Delivery is reported at both subframe and aggregate granularity, because subframes of one A-MPDU
share a preamble and are not independent trials.

## Wire format

17-byte little-endian prefix, then the 802.11 header template; the body is synthesised on-chip:

```
offset  size  field         meaning
  0      8    target_tsf    absolute target time, LE (0 => fire immediately)
  8      1    rate          HAL rate code; 0 => firmware default; HT/MCS = 0x80|mcs
  9      1    txpower       0..63; accepted but clamped by the RF (real power via netlink)
 10      1    nframes       A-MPDU subframes; 0/1 => single frame
 11      1    stamp_off     byte offset of the 6-byte identity stamp inside each subframe
 12      1    station_id    logical destination, written into the stamp
 13      2    seq           base sequence number, LE, written into the stamp
 15      2    frame_len     full length of each synthesised subframe, LE
 17      N    header        24-byte 802.11 header template
```

The stamp written into each subframe at `stamp_off` is
`C0 5A <station_id> <seq_lo> <seq_hi> <subframe_idx>`. Delivery is counted by unique
`(source, station_id, seq, subframe_idx)`, so a duplicate can never inflate it. The stamp is read
at a known offset and its marker verified, never searched for: a search can match at an odd
position and yield well-formed nonsense.

Gate outcomes: `0` fired, `1` no buffer, `2` the instant had already passed, `3` the instant was
too far ahead, `4` the radio's own clock stopped or was reset while the gate was waiting.

The last one exists because the gate is a busy-wait on that clock, part of it with interrupts
disabled. If the clock is reset, which a MAC reset or a restart of the access point does,
the target instant becomes unreachable, and a wait with no way to give up would hang the radio
with interrupts off, taking it off the USB bus until it is physically re-attached. The wait
detects the clock moving backwards or ceasing to advance, restores what it changed, returns the
frame to its pool, and reports this outcome.

## Where to change things

| you want to… | change |
|---|---|
| a round's rate, power, size, stagger, lead | `experiment.json` |
| a node, the channel, the observer, the clock source | `topo.json` |
| how the shared instant maps to each transmitter | `cosr/clockgraph.py`, `cosr/clockd.py` |
| what a health check covers | `Session.doctor` in `cosr/session.py` |
| the gate timing, carrier sense, interrupt behaviour | `ath_cosr_gated_tx()` in `if_ath.c` |
| frame synthesis, the stamp, no-retransmit | `cosr_build_frame()` in `attacks.c` |
| the A-MPDU layout | `cosr_build_ampdu()` in `if_owl.c` |
| the wire format | `wmi.h` in both trees, the driver's debugfs write, and `cosr/wire.py` |

A firmware or driver change means a rebuild and a reflash, which needs USB re-enumeration
([`USAGE.md`](USAGE.md) notes 4–5). Everything in `cosr/` is plain source pushed by `cosr up`.
