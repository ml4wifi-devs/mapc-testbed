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
- **Scales to any topology** — any number of coordinated APs, each with a variable number of
  receiver stations, from one call.
- **One-call control API** returning per-(AP, station) delivery, cross-AP timing error, and RSSI.

## Quick start

1. **Build & flash** the Co-SR firmware + driver on each card VM, and stand up the rig (≥ 2 AP VMs +
   1 monitor VM, all on one channel) — [`docs/INSTALL.md`](docs/INSTALL.md).
2. **Bring the rig up** and start the offset tracker — [`docs/USAGE.md`](docs/USAGE.md) §1.
3. **Fire a shot**: `python3 scripts/cosr_ctl.py spec.json` — [`docs/USAGE.md`](docs/USAGE.md) §2.
4. **Run experiments** (config → RSSI + per-link success, in a loop) —
   [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md).

## How it works

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) covers the full pipeline, the cross-AP timing
subsystem, and the on-the-wire format.

## Repository layout

```
README.md              this file
firmware.diff          Co-SR patch for vanhoefm/modwifi-ath9k-htc (base 781b8da)
driver.diff            Co-SR patch for modwifi backports ath9k_htc (modwifi-20150118.tar.gz)
scripts/               host-side control + measurement (python3 / bash)
  cosr_ctl.py            the one call: cosr_shot(spec) + CLI
  bringup.sh             (re)start APs + monitor after a reboot
  offset_tracker.py      cross-AP TSF offset from beacons (runs on the monitor VM)
  fire_cosr.sh           single-AP gated fire (diagnostic)
  gate_check.py          single-AP gate precision from a capture
  skew.py                two-AP cross-AP sync error from a capture
docs/
  ARCHITECTURE.md        full pipeline: host → driver → firmware gate → air → monitor → count
  INSTALL.md             build & flash the firmware/driver; the test rig; deployment model
  USAGE.md               bring the rig up, fire a shot, the WMI command interface, gotchas
  EXPERIMENTS.md         config → RSSI + per-link success, for driving a coordination model
```

## Built on

[modwifi](https://github.com/vanhoefm/modwifi) (Vanhoef et al.) — the ath9k_htc firmware/driver
this work patches. This repo adds the Co-SR gate + testbed on top; see the diffs.
