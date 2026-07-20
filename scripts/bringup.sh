#!/usr/bin/env bash
# Rig bring-up after a VM reboot. Configs live in /tmp on the VMs and die on reboot, so
# keep this on a persistent host and re-run it. It starts stock hostapd on each AP VM
# (single-VIF), puts the monitor VM in monitor mode on a shared channel, and opens
# debugfs so the gated-TX node can be written without root.
#
# Each AP is single-VIF ON PURPOSE: adding a monitor VIF to an AP's phy rebases its TSF
# and corrupts the on-chip gate. The cross-AP offset tracker runs on the dedicated
# monitor VM (off both APs' beacons), never on an AP.
#
# Configure via env (channel shared by all; SSIDs are arbitrary but must be distinct):
#   APS="ip:iface:ssid ip:iface:ssid ..."   one entry per AP VM
#   MON="ip:iface"                          the monitor VM
#   CHAN=1                                   optional, default 1
#   PW=modwifi                               optional, the modwifi image password
# Example:
#   APS="10.0.0.11:wlan0:apA 10.0.0.12:wlan0:apB" MON="10.0.0.20:wlan0" ./bringup.sh
set -u

PW=${PW:-modwifi}
CHAN=${CHAN:-1}
: "${APS:?set APS=\"ip:iface:ssid ...\"}"
: "${MON:?set MON=\"ip:iface\"}"

ssh_vm() {
    sshpass -p "$PW" ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
        -o PreferredAuthentications=password -o ConnectTimeout=10 "modwifi@$1" "$2" 2>/dev/null
}

bring_up_ap() {   # ip iface ssid
    local ip=$1 iface=$2 ssid=$3
    # Bring the link down before (re)starting hostapd: after `pkill hostapd` the iface is
    # left in managed/up state and hostapd's nl80211 mode-set races ("Could not configure
    # driver mode"). Downing it first releases the iface so hostapd can set AP mode cleanly.
    ssh_vm "$ip" "echo $PW | sudo -S pkill hostapd 2>/dev/null; sleep 1; \
        echo $PW | sudo -S ip link set $iface down 2>/dev/null; sleep 1; \
        printf 'interface=%s\ndriver=nl80211\nssid=%s\nhw_mode=g\nchannel=%s\n' \
            $iface $ssid $CHAN > /tmp/hostapd.conf; \
        echo $PW | sudo -S hostapd -B /tmp/hostapd.conf >/tmp/hostapd.log 2>&1; sleep 3; \
        echo $PW | sudo -S chmod -R a+rwX /sys/kernel/debug 2>/dev/null; \
        echo -n '$ip AP: '; pgrep hostapd >/dev/null && echo up || tail -3 /tmp/hostapd.log"
}

bring_up_monitor() {   # ip iface
    local ip=$1 iface=$2
    ssh_vm "$ip" "echo $PW | sudo -S ip link set $iface down; \
        echo $PW | sudo -S iw dev $iface set type monitor; \
        echo $PW | sudo -S ip link set $iface up; \
        echo $PW | sudo -S iw dev $iface set channel $CHAN; \
        echo -n '$ip mon: '; iw dev $iface info 2>/dev/null | grep -o 'type monitor' | head -1"
}

for entry in $APS; do
    IFS=: read -r ip iface ssid <<<"$entry"
    bring_up_ap "$ip" "$iface" "$ssid"
done

IFS=: read -r mon_ip mon_iface <<<"$MON"
bring_up_monitor "$mon_ip" "$mon_iface"
