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

We present an open, reproducible platform for studying Co-SR on real hardware. A single
host-side call specifies which APs transmit, to which receivers, at what MCS and power, and when.
The firmware fires them in lockstep and the tooling measures delivery, cross-AP timing error, and
signal strength.

## Functionalities

- **Firmware-gated transmission** at microsecond precision — a single AP keys within one OFDM
  symbol of the commanded instant (sub-microsecond typical).
- **Cross-AP alignment** to one shared instant over the air, with configurable stagger for
  controlled on-air overlap (Co-SR / capture-effect studies).
- **Frame aggregation** — a single frame or a true HT A-MPDU (one PPDU of N delimited subframes,
  not a fabricated burst); gated frames are never retransmitted.
- **Per-frame rate and transmit power**, receiver-side delivery counting that stays correct under
  deliberate collision, and AP→station, AP↔monitor, and on-demand AP↔AP RSSI.
- **Any number of coordinated APs**, each transmitting to a chosen station, described in two small
  config files (`topo.json` and `shot.json`).
- **One CLI**, `run.sh`: `up`, `shot`, `measure`, `test-sync`, `gate`, `compare` (which of the two
  cross-AP sync methods is tighter here), and a `doctor`/`reset` pair for the usual testbed faults
  behind an intermittent 0-RX.

## Quick start

1. Build and flash the Co-SR firmware and driver on each card VM, then stand up the testbed (at least
   two AP VMs and one station/observer VM, all on one 2.4 GHz channel) —
   [`docs/INSTALL.md`](docs/INSTALL.md).
2. Describe the testbed in a `topo.json` (copy `examples/topo_monitor.json` or `examples/topo_beacon.json`; `./run.sh scan <ip>...` reads
   each node's live iface + MAC for you) and bring it up with `./run.sh up topo.json` —
   [`docs/USAGE.md`](docs/USAGE.md) §0–1.
3. Fire: `./run.sh shot topo.json shot.json` (also `measure` / `test-sync`). An experiment lives in
   a `shot.json` (copy `examples/shot.json`) — [`docs/USAGE.md`](docs/USAGE.md) §2.
4. Iterate — config in, RSSI and per-link success out, in a loop —
   [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md).

## How it works

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) covers the full pipeline, the cross-AP timing
subsystem, and the on-the-wire format.

## Repository layout

```
README.md              this file
run.sh                 the CLI: up | status | doctor | reset | shot | measure | test-sync | gate | compare | down | scan
examples/
  topo.json              testbed template (nodes, roles, channel) — copy and edit
  shot.json              experiment template (nframes/frame_len/mcs/power, links)
firmware.diff          Co-SR patch for vanhoefm/modwifi-ath9k-htc (base 781b8da)
driver.diff            Co-SR patch for modwifi backports ath9k_htc (modwifi-20150118.tar.gz)
scripts/               host-side control + measurement (python3 / bash)
  cosr_ctl.py            shot / measure / test-sync / gate + CLI (load resolves topo+shot)
  topo_env.py            emit topo.json as shell records (run.sh reads the testbed)
  scan_nodes.py          read each node's wireless iface + MAC from its IP (`run.sh scan`), for topo.json
  offset_tracker.py      single-observer TSF offset from beacons (on the observer VM; --raw feeds multi-monitor)
  clock_graph.py         shared affine clock-graph core (compose / BFS / offset.json) for the scaling sync modes
  beacon_tracker.py      beacon sync: APs hear each other (driver tap); composes on the host
  monitor_tracker.py     multi-monitor sync: several monitors bridged by common APs; composes on the host
docs/
  ARCHITECTURE.md        full pipeline: host → driver → firmware gate → air → monitor → count
  INSTALL.md             build & flash the firmware/driver; the testbed; deployment model
  USAGE.md               bring the testbed up, fire a shot, the WMI command interface, gotchas
  EXPERIMENTS.md         config → RSSI + per-link success, for driving a coordination model
```

## Built on

[modwifi](https://github.com/vanhoefm/modwifi) (Vanhoef et al.) — the ath9k_htc firmware/driver
this work patches. This repo adds the Co-SR gate + testbed on top; see the diffs.
