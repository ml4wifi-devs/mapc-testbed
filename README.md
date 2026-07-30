# A Scalable Coordinated Spatial Reuse Testbed on Commodity IEEE 802.11 Hardware

**Coordinated spatial reuse (Co-SR)**, an IEEE 802.11 multi-AP coordination (MAPC) scheme,
lets neighbouring access points (APs) transmit in parallel. Co-SR requires the APs to release
their frames in near-perfect alignment, ideally microsecond-accurate. Commodity Wi-Fi cannot
schedule that from the host: the operating system and USB/bus path add milliseconds of
non-deterministic delay after any timing decision.

This testbed moves the transmit-release decision off the host and into the radio firmware. A
small gate in the **Atheros AR9271** firmware busy-polls the on-chip TSF timer and releases each
frame at a commanded absolute time, reaching microsecond precision on inexpensive USB dongles.
Distributed APs are aligned through the standard beacon TSF observed on a common clock, so one
shared target instant is expressed in each AP's local time.

We present an open platform for studying Co-SR on real hardware. One host-side call specifies which
APs transmit, to which receivers, at what MCS and power, and when. The firmware fires them in
lockstep, and the tooling measures delivery, cross-AP timing error and signal strength.

## Functionalities

- **Firmware-gated transmission** at microsecond precision: a single AP keys within one OFDM
  symbol of the commanded instant (sub-microsecond typical).
- **Cross-AP alignment** to one shared instant over the air, with configurable stagger for
  controlled on-air overlap (Co-SR / capture-effect studies).
- **Frame aggregation**: a single frame or a true HT A-MPDU (one PPDU of N delimited subframes,
  not a fabricated burst); gated frames are never retransmitted.
- **Per-link rate and transmit power**, receiver-side delivery counting that stays correct under
  deliberate collision, and AP→station and AP↔AP signal levels.
- **Any number of coordinated APs**, each transmitting to a chosen station, described in two small
  config files (`topo.json` and `experiment.json`).
- **One round per instruction**: a round is one published message and one reply per node, which
  puts a measurement loop well under a second.
- **One health check**, `doctor`, covering every way the clocks can drift apart. A check that
  could not run degrades the verdict to `INCONCLUSIVE`; one that had nothing to examine is
  reported separately, as `NA`.

## Quick start

1. Build and flash the Co-SR firmware and driver on each AP, then stand up the testbed (at least
   one AP and one station, all on one 2.4 GHz channel). See [`docs/INSTALL.md`](docs/INSTALL.md).
2. Describe the testbed in a `topo.json` (copy `examples/topo_beacon.json`;
   `cosr scan <ip>...` reads each node's live interface and address for you).
3. Bring it up: `cosr up topo.json experiment.json`. See [`docs/USAGE.md`](docs/USAGE.md) §1.
4. Check it: `cosr doctor topo.json experiment.json`. Fix anything it names before measuring.
5. Run it: `cosr run topo.json experiment.json`. The experiment lives in an `experiment.json`
   (copy `examples/experiment.json`). See [`docs/USAGE.md`](docs/USAGE.md) §2.

## How it works

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) covers the full pipeline, the cross-AP timing
subsystem, and the on-the-wire format.

## Repository layout

```
README.md              this file
pyproject.toml         installable package; provides the `cosr` command
examples/
  topo_beacon.json       testbed template: transmitters hear each other
  topo_monitor.json      testbed template: receivers observe the transmitters
  experiment.json        experiment template (aggregate, timing, per-link rate and power)
firmware.diff          Co-SR patch for vanhoefm/modwifi-ath9k-htc (base 781b8da)
driver.diff            Co-SR patch for modwifi backports ath9k_htc (modwifi-20150118.tar.gz)
cosr/                  the controller and the node program (python3, standard library only)
  cli.py                 the commands
  deploy.py              place the node program on each node and start it
  agent.py               the resident node program: both transmitter and receiver roles
  session.py             drives rounds; the health check; results
  clockd.py              the live clock graph and the checks that gate firing on it
  clockgraph.py          affine clock algebra: edges, composition, fits
  counter.py             receiver-side frame accounting
  accounting.py          per-link results and their named statuses
  radiotap.py            radiotap / 802.11 / stamp parsing
  wire.py                the transmit command payload and sequence allocation
  natsc.py               message-bus client
  proto.py               subjects and message shapes
  nl80211.py             transmit power via netlink
  timing.py              coincidence and divergence summaries
  topo.py                topo.json + experiment.json -> a validated plan
  scan.py                read each node's interface and address
tests/                  python3 -m unittest discover tests
docs/
  ARCHITECTURE.md        full pipeline: controller -> node -> firmware gate -> air -> receiver
  INSTALL.md             hardware requirements; build & flash; first run
  USAGE.md               the two config files, every command, and the result schema
  SYNC.md                how the clocks are related, and how to measure that they are
  EXPERIMENTS.md         config -> signal levels + per-link success, in a loop
```

## Built on

[modwifi](https://github.com/vanhoefm/modwifi) (Vanhoef et al.) is the ath9k_htc firmware/driver
this work patches. This repo adds the Co-SR gate + testbed on top; see the diffs.
