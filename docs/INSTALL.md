# Installation

The deliverables are two patches, `firmware.diff` and `driver.diff`, applied to the
[modwifi](https://github.com/vanhoefm/modwifi) upstreams, plus a small controller and node
program in `cosr/`. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pieces fit together.

## 0. Hardware you need

The transmit side is not hardware-agnostic; the receive side very nearly is.

| Role | Requirement |
|---|---|
| **AP (transmitter)** | **AR9271 only** (`ath9k_htc`). The gate is patched open-source AR9271 firmware, so another chipset needs a port, not a rebuild. One card per node. |
| **Station (delivery counting)** | **Any** card that supports monitor mode and whose radiotap carries `dbm_antsignal`. This is the common case. |
| **Station (`monitor` clock source)** | Additionally needs radiotap **TSFT**. Widely available. |
| **Station (coincidence measurement)** | Needs radiotap **TSFT**, and its stamping has to be steady enough to resolve the effect being measured. This is not a property you have to know in advance: it is measured, and `doctor` reports it every run. See [`SYNC.md`](SYNC.md), "Can this receiver time anything?". |
| **Controller** | Any POSIX host with `python3` (3.6+), `ssh`, `sshpass`, and `nats-server`. Nothing else: no packet-capture tooling and no libraries beyond the standard library. |
| **Nodes** | `python3` (3.4+), `iw`, `hostapd` on the APs. Nothing is installed by this project; the node program is plain source and uses only the standard library. |

At least one AP and one station are needed to measure delivery. Measuring how closely
transmitters coincide needs at least two APs and one timing-capable receiver.

## 1. What the diffs change

**Firmware**: `vanhoefm/modwifi-ath9k-htc` (base `781b8da`):
```bash
git clone https://github.com/vanhoefm/modwifi-ath9k-htc.git
git -C modwifi-ath9k-htc checkout 781b8da
git -C modwifi-ath9k-htc apply firmware.diff
```
Changed files:
- `target_firmware/wlan/include/wmi.h`: command id + `WMI_COSR_GATED_TX_CMD/RESP`.
- `target_firmware/wlan/if_ath.c`: the `ath_cosr_gated_tx` gate handler + dispatch entry.
- `target_firmware/wlan/attacks.c`: `cosr_build_frame`: on-chip subframe synthesis + id stamp.
- `target_firmware/wlan/if_owl.c`: `cosr_build_ampdu`/`cosr_free_ampdu`: hand-built HT A-MPDU;
  de-`static` of `ath_tgt_txqaddbuf` so the handler reuses the real data-TX path.
- `target_firmware/wlan/attacks.h`: declarations.

**Driver**: modwifi backports `ath9k_htc`, shipped upstream as a tarball (not a git repo):
```
modwifi-20150118.tar.gz   md5 291bd2ab8c900a2ff22e26f6becf6048   (2015-01-18)
tar xzf modwifi-20150118.tar.gz            # -> drivers/
cd drivers && git init && git add -A && git commit -m base
git apply ../driver.diff
```
Changed files:
- `drivers/net/wireless/ath/ath9k/wmi.h`: command id + `wmi_cosr_gated_tx_cmd/resp`.
- `drivers/net/wireless/ath/ath9k/htc_drv_debug.c`: debugfs write `cosr_gated_tx` (fires the
  gate) and read-only `cosr_beacons` (every beacon heard, with the sender's TSF, the local
  receive time, and the level it arrived at).
- `drivers/net/wireless/ath/ath9k/htc_drv_txrx.c`: feeds each heard beacon into that ring.
- `net/mac80211/debugfs_netdev.c`: exposes the `tsf` file on **AP** interfaces (stock mac80211
  exposes it only for IBSS/mesh; one line, in `add_ap_files`). Nothing in the tooling reads it:
  the controller never interrogates a radio's clock, for the reasons in [`SYNC.md`](SYNC.md).
  It is kept because being able to read a transmitter's clock by hand is worth having when
  something looks wrong, and it can be dropped if you would rather not patch mac80211.

Stock **hostapd** is used unchanged: the APs run a short generated config that `cosr up`
writes from `topo.json`; no hostapd source changes are needed.

> **Reproducibility.** The firmware `.fw` is byte-reproducible from `firmware.diff` on a clean
> checkout. Kernel modules are not byte-reproducible (they embed build paths and timestamps) but
> rebuild to functionally identical code.

## 2. Build and flash

Each AP node needs `openssh-server` (so the controller can reach it) and `hostapd` (AP mode):
```bash
sudo apt-get update
sudo apt-get install -y openssh-server hostapd
```

**Firmware**: two ways to get the Co-SR `htc_9271.fw` onto a node:
- *Build it* (source of truth): take the firmware tree from §1. Building needs the Xtensa
  big-endian toolchain; set it up per the
  [modwifi build docs](https://github.com/vanhoefm/modwifi), build `target_firmware`
  (`make -C target_firmware` → `htc_9271.fw`), then install:
  ```bash
  sudo cp /lib/firmware/htc_9271.fw /lib/firmware/htc_9271.fw.stock   # back up stock first
  sudo cp <built>/htc_9271.fw /lib/firmware/htc_9271.fw
  ```
- *Copy the prebuilt `.fw`* to further nodes: build once, then copy the resulting `htc_9271.fw`
  into each node's `/lib/firmware/` (same backup-then-copy).

**Driver**: builds on the node against its own kernel:
```bash
cd ~/modwifi/drivers
patch -p1 < /path/to/driver.diff
make defconfig-ath9k-debug               # as upstream; the tarball ships no .config
make
sudo make install                        # -> /lib/modules/$(uname -r)/updates + depmod
sudo reboot
```

**Load the new firmware and driver.** The AR9271 downloads firmware into its RAM only on a USB
re-enumeration: `rmmod`/`modprobe` keeps the firmware already resident, and a new driver against
old firmware gives WMI timeouts (`-110`). So:
```bash
sudo rmmod ath9k_htc     # drop the old module so the Co-SR one binds on reconnect
```
then re-enumerate the dongle: unplug and replug it, or detach and re-attach it from outside the
machine if the node is virtualised. On reconnect the Co-SR driver autoloads and downloads the
Co-SR firmware. Verify:
```bash
dmesg | grep -i 'Transferred FW'
```

> Re-enumeration is a physical action. Plan for it: a card whose firmware failed to initialise
> drops off the bus, and no command on the node itself can bring it back.

## 3. The message broker

Nodes dial out to a broker on the controller, so no node listens on any port and no node needs
per-node configuration. Install [`nats-server`](https://nats.io) (a single binary) on the
controller and run it with a token:
```bash
nats-server -a 0.0.0.0 -p 4222 --auth <your-token>
```
Put the same token in `topo.json` as `"token"`. Bind it to the interface facing the testbed.

## 4. Install the controller

On the controller only; the nodes need nothing installed:
```bash
pip install -e .          # provides the `cosr` command
```
The package has no dependencies. `ssh`, `sshpass` and `nats-server` are external programs.

## 5. First run

Describe the testbed in a `topo.json` (copy `examples/topo_beacon.json`). `cosr scan <ip>...`
reads each node's wireless interface and address for you. Then:

```bash
cosr up     topo.json experiment.json    # radios up, node program placed and started
cosr status topo.json experiment.json    # everything running, on the same build
cosr doctor topo.json experiment.json    # the health verdict, read this before measuring
cosr run    topo.json experiment.json    # one round
```

`up` is idempotent and safe to repeat; it stops any previous node program before starting the
new one, and refuses to start a node whose copy of the program does not match the controller's.

## 6. Deployment model

The system has two independent planes, and the requirements follow from them.

- **Control plane.** The controller reaches each node over an IP management network. This path is
  not real-time: an instruction carries an absolute target time and the on-chip gate absorbs any
  delivery jitter. Keep it off the experimental channel so control traffic does not perturb the
  measurement.
- **Timing plane.** Entirely over the air, via the beacon timestamps. No wired synchronization.

Further requirements:

- **The transmitters' clocks must be relatable over the air.** Their free-running counters are
  related through beacons. Either the APs hear each other (`sync: "beacon"`) or receivers hear
  the APs (`sync: "monitor"`); all that matters is that the graph reaches the reference. See
  [`SYNC.md`](SYNC.md).
- **A transmitting AP must be idle on the voice queue.** Each shot drains that queue so the gated
  frame is the sole descriptor at the target instant; competing voice-class traffic on that AP is
  dropped. Use dedicated APs during coordinated shots.
- **Size the lead to the control network.** The shared instant is placed `lead_us` ahead of now.
  It must cover the time for the instruction to reach every node, or a node finds its instant
  already past and the gate refuses the shot; and it must stay inside the window the gate accepts.
  The 400 ms default suits a wireless management network; a wired one can use less, which also
  slightly improves the clock relation. `doctor` names this failure explicitly when it happens.

The node program is started under `sudo` with the credentials from `topo.json`, and runs as root
because capturing raw frames, writing the transmit trigger and reading kernel messages all
require it. `up` tightens the two debugfs entries it uses to `0640` rather than widening them.
