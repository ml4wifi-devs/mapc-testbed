# Installation

Everything here runs on a virtual machine (VM): a fresh [modwifi](https://github.com/vanhoefm/modwifi)
image with one AR9271 USB dongle plugged in. The deliverables are two patches (`firmware.diff`, 
`driver.diff`) applied to the modwifi upstreams. Both diffs come from the trees used to build the 
validated firmware/driver. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pieces fit together.

## 1. What the diffs change

**Firmware** — `vanhoefm/modwifi-ath9k-htc` (base `781b8da`):
```bash
git clone https://github.com/vanhoefm/modwifi-ath9k-htc.git
git -C modwifi-ath9k-htc checkout 781b8da
git -C modwifi-ath9k-htc apply firmware.diff
```
Changed files:
- `target_firmware/wlan/include/wmi.h` — command id + `WMI_COSR_GATED_TX_CMD/RESP`.
- `target_firmware/wlan/if_ath.c` — the `ath_cosr_gated_tx` gate handler + dispatch entry.
- `target_firmware/wlan/attacks.c` — `cosr_build_frame`: on-chip subframe synthesis + id stamp.
- `target_firmware/wlan/if_owl.c` — `cosr_build_ampdu`/`cosr_free_ampdu`: hand-built HT A-MPDU;
  de-`static` of `ath_tgt_txqaddbuf` so the handler reuses the real data-TX path.
- `target_firmware/wlan/attacks.h` — declarations.

**Driver** — modwifi backports `ath9k_htc`, shipped upstream as a tarball (not a git repo):
```
modwifi-20150118.tar.gz   md5 291bd2ab8c900a2ff22e26f6becf6048   (2015-01-18)
tar xzf modwifi-20150118.tar.gz            # -> drivers/
cd drivers && git init && git add -A && git commit -m base
git apply ../driver.diff
```
Changed files:
- `drivers/net/wireless/ath/ath9k/wmi.h` — command id + `wmi_cosr_gated_tx_cmd/resp`.
- `drivers/net/wireless/ath/ath9k/htc_drv_debug.c` — debugfs file `cosr_gated_tx`.
- `net/mac80211/debugfs_netdev.c` — exposes the `tsf` debugfs file on **AP** interfaces (stock
  mac80211 only exposes it for IBSS/mesh, but the host scripts read `netdev:*/tsf` to seed the
  gate on an AP; one line, in `add_ap_files`).

Stock **hostapd** is used unchanged — the APs run a 4-line generated config (see
`scripts/bringup.sh`); no hostapd source changes are needed.

> **Reproducibility.** The firmware `.fw` is byte-reproducible from `firmware.diff` on a clean
> checkout (verified md5 `0cdf25a…`). Kernel `.ko`s are not byte-reproducible (they embed build
> paths/timestamps), but rebuild to functionally identical code (identical normalized
> disassembly to the reference modules).

## 2. Build & flash

The modwifi image already carries the driver *source* (`~/modwifi/drivers/`), kernel headers,
`gcc`/`make`, `python3`, `tcpdump`, and `iw`. Two packages it does not ship:
```bash
sudo apt-get update
sudo apt-get install -y openssh-server   # so the host-side scripts can reach this VM over ssh
sudo apt-get install -y hostapd          # AP mode; not preinstalled
```
(No `tshark` is needed on any VM — the tracker uses `tcpdump`; frame parsing runs host-side.)

**Firmware** — two ways to get the Co-SR `htc_9271.fw` onto a card VM:
- *Build it* (source of truth): take the firmware tree from §1 (the `modwifi-ath9k-htc` clone
  with `firmware.diff` applied — the same source also ships inside
  `~/modwifi/modwifi-20150118.tar.gz`). Building needs the Xtensa big-endian toolchain; set it up
  per the [modwifi build docs](https://github.com/vanhoefm/modwifi), build `target_firmware`
  (`make -C target_firmware` → `htc_9271.fw`), then install:
  ```bash
  sudo cp /lib/firmware/htc_9271.fw /lib/firmware/htc_9271.fw.stock   # back up stock first
  sudo cp <built>/htc_9271.fw /lib/firmware/htc_9271.fw
  ```
- *Copy the prebuilt `.fw`* to additional card VMs: build once as above, then `scp` the resulting
  `htc_9271.fw` into each VM's `/lib/firmware/` (same backup-then-copy).

**Driver** — builds on the VM against its own kernel (backports tree, no `git` needed):
```bash
cd ~/modwifi/drivers
patch -p1 < /path/to/driver.diff         # ath9k_htc + the mac80211 tsf-on-AP one-liner (§1)
make                                     # incremental; recompiles the touched modules
sudo make install                        # -> /lib/modules/$(uname -r)/updates + depmod
```

**Load the new firmware + driver.** The AR9271 downloads firmware into its RAM only on a USB
re-enumeration — `rmmod`/`modprobe` keeps the old resident firmware, and a new driver against
old firmware gives WMI timeouts (`-110`). So:
```bash
sudo rmmod ath9k_htc     # drop the old (stock) module so the Co-SR one binds on reconnect
```
then re-enumerate the dongle: unplug/replug it, or disconnect+reconnect it host-side
(VMware: *Removable Devices → Atheros AR9271 → Disconnect*, then reconnect). On reconnect the Co-SR
driver autoloads and downloads the Co-SR firmware. Verify:
```bash
dmesg | grep -i 'Transferred FW'    # Co-SR firmware loaded
```
Then bring the card up as an AP with `scripts/bringup.sh` (see [`USAGE.md`](USAGE.md) — it
generates the hostapd config, never hand-write one) so the per-vif debugfs nodes appear, and
confirm driver + firmware together:
```bash
sudo ls /sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx   # Co-SR driver bound
```
The definitive check is a gated fire returning `status=0` (`scripts/fire_cosr.sh`), not a WMI error.

## 3. Test rig

Bring up your VMs — all should be reachable over the network by IP and the modwifi image with the
flashed firmware + driver:

- **N AP VMs** (≥ 2), each with one AR9271, running as an AP (hostapd). One is the **reference**
  (its clock is the timing reference); the rest are **followers** whose targets are mapped into the
  reference's instant. Each AP is single-VIF (AP only) — see [`USAGE.md`](USAGE.md) note 1.
- **one TSF observer** — any 802.11 card in monitor mode on the same channel as the APs, that
  hears every AP's beacons and runs the offset tracker.
- **station (receiver) cards** — one monitor-mode card per station, on the shared channel,
  each where a receiver would sit. Each AP is assigned a variable number of receivers;
  a station counts its AP's stamped stream and reports per-location delivery and AP→station RSSI
  (`measure_multi`, [`EXPERIMENTS.md`](EXPERIMENTS.md)).

### Deployment model

The system has two independent planes, and the topology requirements follow from them.

- **Control plane (triggers).** The host reaches each AP over an IP management channel (ssh)
  to write the gated-TX command. This path is non-real-time: the trigger carries an absolute
  target TSF and the on-chip gate absorbs any delivery jitter, so it has no latency requirement and
  needs no particular medium. Recommended to keep it off the experimental channel so control
  traffic does not perturb the measurement.
- **Timing plane (alignment).** Purely over the air via the beacon TSF; no wired synchronization.

Requirements:

- **AR9271 radios and a modwifi-compatible kernel.** The gate firmware is built for the AR9271
  (`k2` target) and the driver + the mac80211 tsf-on-AP patch target the modwifi image's kernel.
  Other chipsets or kernels require a port, not just a rebuild.
- **One card per node.** The debugfs path globs (`ieee80211/*/…` in `cosr_ctl.py` and the
  scripts) assume a single card per VM; a second card breaks the wildcard resolution.
- **A common observer is required.** The APs' free-running TSFs are related
  only through a node that hears them all — the observer timestamps every AP's beacon on its own
  clock, and the controller maps one shared instant into each AP's TSF from those offsets. So **one
  single observer must hear the beacons of every participating AP**.
- **A transmitting AP must be idle on the voice (VO) queue.** Each shot drains the VO
  QCU so the gated frame is the sole descriptor at the target instant; any competing VO-class
  traffic on that AP is dropped. Use dedicated APs (no associated clients pushing traffic) during
  coordinated shots.
- **Size the fire lead within two bounds.** The shared instant (`lead_us` ahead of now) must be far
  enough ahead to cover the serial trigger fan-out — roughly one ssh round-trip per AP — or a later
  AP's target lands in the past and the gate rejects it as LATE. Also, it must stay under the
  firmware's ~600 ms ceiling, which also keeps the offset tracker's linear TSF extrapolation valid.
  The 500 ms default suits a handful of APs on a LAN.

Note each AP's IP, interface name (`wlanX`), and BSSID (`iw dev <iface> info`), plus the monitor's
IP and interface — these are the inputs to `bringup.sh`, `offset_tracker.py`, and the spec.
root ssh is disabled on the image, so debugfs is chmod'd (by `bringup.sh`) rather than firing as root.
