#!/usr/bin/env bash
# Single-AP gated fire tool (diagnostic; pairs with gate_check.py).
#
# Fires N gated shots on the master AP. Each shot is one frame synthesized on-chip from
# the given rate/txpower/frame_len and stamped (station_id, seq=shot#, idx=0). Logs
# "seq commanded_T" to /tmp/fire_cosr.log so gate_check.py can pair each frame's monitor
# tsft with its commanded target and measure gate precision.
#
# Required env: AP_IP AP_MAC (the AP to fire from).
# Env overrides: RATE TXP NF SOFF SID FLEN LEAD N SLEEP; UNGATED=1 fires immediately.
#   e.g.  AP_IP=10.0.0.11 AP_MAC=02:00:00:00:00:11 RATE=128 N=40 ./fire_cosr.sh
set -u

: "${AP_IP:?set AP_IP to the AP address}"
: "${AP_MAC:?set AP_MAC to the AP BSSID}"
IP_M=$AP_IP
MAC_M=$AP_MAC

RATE=${RATE:-0}          # HAL rateCode; 0 => firmware default; HT/MCS = 0x80|mcs
TXP=${TXP:-0}            # accepted but ignored by AR9271 (real power via iw)
NF=${NF:-1}              # A-MPDU subframes; 1 => single frame
SOFF=${SOFF:-24}         # id-stamp offset within the frame
SID=${SID:-7}            # station id
FLEN=${FLEN:-200}        # synthesized frame length
LEAD=${LEAD:-300000}     # fire this many us ahead of the current TSF
N=${N:-40}              # number of shots
SLEEP=${SLEEP:-0.25}     # gap between shots

SSH="sshpass -p modwifi ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no -o PreferredAuthentications=password"
# Persistent ControlMaster so per-shot ssh is a cheap multiplex.
$SSH -M -S /tmp/cmC -o ControlPersist=300 modwifi@$IP_M true 2>/dev/null
em() { $SSH -S /tmp/cmC modwifi@$IP_M "$@" 2>/dev/null; }

NODE=$(em 'ls /sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx')
TSF='/sys/kernel/debug/ieee80211/*/netdev:*/tsf'

# Build the WMI write payload as a printf escape string. Layout (little-endian):
#   [8 target][1 rate][1 txp][1 nframes][1 stamp_off][1 station_id]
#   [2 seq][2 frame_len][24-byte 802.11 header template]
blob() {
    local target=$1 rate=$2 txp=$3 nf=$4 soff=$5 sid=$6 seq=$7 flen=$8 mac=$9
    local s="" h i a b c d e f

    for i in 0 1 2 3 4 5 6 7; do                        # 8-byte target TSF, LE
        printf -v h '\\x%02x' $(( (target >> (8*i)) & 0xff )); s+=$h
    done
    printf -v h '\\x%02x\\x%02x\\x%02x\\x%02x\\x%02x' "$rate" "$txp" "$nf" "$soff" "$sid"; s+=$h
    printf -v h '\\x%02x\\x%02x' $(( seq & 0xff )) $(( (seq>>8) & 0xff )); s+=$h
    printf -v h '\\x%02x\\x%02x' $(( flen & 0xff )) $(( (flen>>8) & 0xff )); s+=$h

    IFS=: read a b c d e f <<<"$mac"                    # 802.11 header template
    s+='\x08\x00\x00\x00'                               # FC + duration
    s+='\xff\xff\xff\xff\xff\xff'                       # addr1 (dst) broadcast
    s+="\\x$a\\x$b\\x$c\\x$d\\x$e\\x$f"                 # addr2 (src) = this AP
    s+='\xff\xff\xff\xff\xff\xff'                       # addr3 (bssid) broadcast
    s+='\x00\x00'                                       # seq ctl
    echo "$s"
}

: > /tmp/fire_cosr.log
for i in $(seq 0 $((N-1))); do
    now=$(( $(em "cat $TSF") ))
    if [ "${UNGATED:-0}" = 1 ]; then T=0; else T=$((now + LEAD)); fi
    printf "$(blob $T $RATE $TXP $NF $SOFF $SID $i $FLEN $MAC_M)" | em "cat > $NODE"
    echo "$i $T" >> /tmp/fire_cosr.log
    echo "shot $i now=$now T=$T seq=$i"
    sleep "$SLEEP"
done
