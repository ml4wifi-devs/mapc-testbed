#!/usr/bin/env bash
# Co-SR testbed driver -- general purpose. Reads a topo.json describing the testbed and
# drives the whole pipeline. No testbed values are baked in here; everything comes from the
# topo file (see examples/topo.json / examples/shot.json).
#
#   ./run.sh up        <topo.json>              bring up APs + station monitors + the clock tracker
#   ./run.sh status    <topo.json>              APs up? gated-TX node present? monitors + tracker live?
#   ./run.sh doctor    <topo.json>              probe every node -> PASS/FAIL table (find the 0-RX cause)
#   ./run.sh reset     <topo.json>              fast fix: re-monitor stations, bounce dead APs, restart tracker
#   ./run.sh shot      <topo.json> [shot.json]  fire a coordinated Co-SR shot -> per-link delivery
#   ./run.sh measure   <topo.json> [shot.json]  fire + model inputs (success + RSSI matrices)
#   ./run.sh test-sync <topo.json> [shot.json]  fire staggered shots -> per-AP sync (bias/jitter/p90)
#   ./run.sh gate      <topo.json> [shot.json]  single-AP absolute gate precision (first link's AP)
#   ./run.sh compare   <topo.json> [shot.json]  OTA vs multi-monitor sync accuracy, side by side
#   ./run.sh down      <topo.json>              tear everything down
#   ./run.sh scan      <ip> [ip ...]            read each node's wireless iface + MAC for topo.json
#
# topo.json is the first arg (defaults to ./topo.json); shot.json is the second, for the fire
# commands (defaults to ./shot.json). AR9271 is 2.4 GHz, so `channel` in topo must be 1..13 (all
# nodes share it). Cold start:  ./run.sh up t.json && ./run.sh status t.json
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$HERE/scripts"

cmd="${1:-status}"; shift || true

# `scan` is the pre-topo helper: it takes node IPs, not a topo file, and prints each node's
# wireless iface + MAC ready for topo.json. Handle it before any topo parsing.
if [ "$cmd" = scan ]; then
    exec python3 "$SCRIPTS/scan_nodes.py" "$@"
fi

# topo file = first positional arg (every command needs it), default ./topo.json
TOPO="${1:-$HERE/topo.json}"; shift || true
[ -f "$TOPO" ] || { echo "no topo file: $TOPO" >&2; exit 1; }

# ---- read topo.json into shell records via python (no jq dependency) ----------------
# emits pipe-delimited lines: meta|channel|password|sync ; ap|name|ip|iface|ssid|mac ;
# station|name|ip|iface|station_id ; observer|name|ip|iface
_topo() { python3 "$SCRIPTS/topo_env.py" "$TOPO"; }
TOPO_LINES="$(_topo)" || { echo "failed to parse $TOPO" >&2; exit 1; }

CHANNEL=""; PW=""; SYNC="monitor"; OBS_IP=""; OBS_IF=""; OBS_NAME=""
AP_LINES=""; STA_LINES=""; AP_MACS=""; MON_LINES=""
while IFS='|' read -r kind a b c d e; do
    case "$kind" in
        meta)     CHANNEL="$a"; PW="$b"; SYNC="${c:-monitor}" ;;
        ap)       AP_LINES+="$a|$b|$c|$d|$e"$'\n'; AP_MACS+="$e " ;;
        station)  STA_LINES+="$a|$b|$c|$d|$e"$'\n' ;;
        observer) OBS_NAME="$a"; OBS_IP="$b"; OBS_IF="$c" ;;
        monitor)  MON_LINES+="$a|$b|$c"$'\n' ;;   # clock monitors for multi-monitor sync
    esac
done <<< "$TOPO_LINES"
# multi-monitor sync = monitor mode with an explicit monitors list; else single-observer
MULTIMON=0; [ "$SYNC" = monitor ] && [ -n "$(echo -n "$MON_LINES" | tr -d '[:space:]')" ] && MULTIMON=1

SSHOPT="-o StrictHostKeyChecking=no -o PubkeyAuthentication=no -o PreferredAuthentications=password -o ConnectTimeout=10"
# -n: read stdin from /dev/null so ssh never swallows the `while read` here-string lines
# it is iterating over (our sudo passwords are piped by the remote command, not ssh stdin).
sh_()   { sshpass -p "$PW" ssh -n $SSHOPT "modwifi@$1" "$2" 2>/dev/null; }
scp_()  { sshpass -p "$PW" scp $SSHOPT "$1" "modwifi@$2:$3" 2>/dev/null; }

# ---- bring-up primitives ------------------------------------------------------------
bring_up_ap() {   # ip iface ssid
    local ip="$1" iface="$2" ssid="$3"
    # Down the link before (re)starting hostapd: after `pkill hostapd` the iface is left
    # managed/up and hostapd's nl80211 mode-set races. Downing it first lets hostapd set
    # AP mode cleanly. Then open debugfs so the gated-TX node is writable without root.
    sh_ "$ip" "echo $PW | sudo -S pkill hostapd 2>/dev/null; sleep 1; \
        echo $PW | sudo -S ip link set $iface down 2>/dev/null; sleep 1; \
        printf 'interface=%s\ndriver=nl80211\nssid=%s\nhw_mode=g\nchannel=%s\n' \
            '$iface' '$ssid' '$CHANNEL' > /tmp/hostapd.conf; \
        echo $PW | sudo -S hostapd -B /tmp/hostapd.conf >/tmp/hostapd.log 2>&1; sleep 3; \
        echo $PW | sudo -S chmod -R a+rwX /sys/kernel/debug 2>/dev/null; \
        echo -n '  AP $ip: '; pgrep hostapd >/dev/null && echo up || tail -2 /tmp/hostapd.log"
}

bring_up_monitor() {   # ip iface
    local ip="$1" iface="$2"
    sh_ "$ip" "echo $PW | sudo -S ip link set $iface down; \
        echo $PW | sudo -S iw dev $iface set type monitor; \
        echo $PW | sudo -S ip link set $iface up; \
        echo $PW | sudo -S iw dev $iface set channel $CHANNEL; \
        echo -n '  mon $ip: '; iw dev $iface info 2>/dev/null | grep -o 'type monitor' | head -1 || echo FAILED"
}

start_tracker() {
    # deploy + launch the cross-AP clock tracker on the observer (reference = first AP).
    scp_ "$SCRIPTS/offset_tracker.py" "$OBS_IP" /tmp/offset_tracker.py
    local want got
    want=$(wc -c < "$SCRIPTS/offset_tracker.py" | tr -d '[:space:]')
    got=$(sh_ "$OBS_IP" "wc -c < /tmp/offset_tracker.py 2>/dev/null" | tr -d '[:space:]')
    [ "$got" = "$want" ] || { echo "  tracker deploy FAILED to $OBS_IP ($got/$want B)"; return 1; }
    # '[o]ffset...' bracket trick: matches the running tracker but NOT this pkill's own
    # shell (whose cmdline contains the literal pattern) -- a plain -f self-kills the shell.
    # SIGKILL (-9) so a tracker stuck in a tcpdump read dies for sure, and reap its orphaned
    # tcpdump child too, else old captures pile up on the monitor across restarts.
    sh_ "$OBS_IP" "echo $PW | sudo -S pkill -9 -f '[o]ffset_tracker.py' 2>/dev/null; echo $PW | sudo -S pkill -x tcpdump 2>/dev/null; echo -n ''"
    sleep 2
    sh_ "$OBS_IP" "echo $PW | sudo -S nohup setsid python3 /tmp/offset_tracker.py $OBS_IF $AP_MACS \
                   >/tmp/tracker.log 2>&1 & echo -n ''"
    sleep 5
    echo -n "  tracker($OBS_NAME): "; sh_ "$OBS_IP" "cat /tmp/offset.json || echo MISSING"
}

start_beacon_tracker() {
    # beacon sync: the clock tracker runs HERE on the control host, polling every AP's
    # cosr_beacons node over ssh and composing one /tmp/offset.json locally. The '[b]...'
    # bracket keeps this pkill from matching its own shell's cmdline. Both host trackers own
    # the SAME /tmp/offset.json, so kill BOTH types first -- a stray monitor_tracker (e.g. left
    # by an earlier `compare` or a sync-mode switch) would otherwise keep overwriting the file
    # and silently corrupt every follower AP's target (garbage offset -> TOOFAR misfires).
    pkill -9 -f '[b]eacon_tracker.py' 2>/dev/null
    pkill -9 -f '[m]onitor_tracker.py' 2>/dev/null
    rm -f /tmp/offset.json
    sleep 1
    nohup python3 "$SCRIPTS/beacon_tracker.py" "$TOPO" >/tmp/beacon_tracker.log 2>&1 &
    sleep 6
    echo -n "  beacon-tracker(host): "; cat /tmp/offset.json 2>/dev/null || echo "MISSING (see /tmp/beacon_tracker.log)"
}

start_monitor_graph_tracker() {
    # multi-monitor sync: each monitor runs offset_tracker.py --raw (per-AP fits in its own
    # clock), and a host-side monitor_tracker.py composes them into one /tmp/offset.json.
    local name ip iface
    while IFS='|' read -r name ip iface; do
        [ -z "$name" ] && continue
        # </dev/null: scp (unlike sh_'s `ssh -n`) would otherwise consume the here-string this
        # loop reads from, so only the first monitor would be deployed.
        scp_ "$SCRIPTS/offset_tracker.py" "$ip" /tmp/offset_tracker.py </dev/null
        sh_ "$ip" "echo $PW | sudo -S pkill -9 -f '[o]ffset_tracker.py' 2>/dev/null; echo $PW | sudo -S pkill -x tcpdump 2>/dev/null; echo -n ''"
    done <<< "$MON_LINES"
    sleep 2
    while IFS='|' read -r name ip iface; do
        [ -z "$name" ] && continue
        sh_ "$ip" "echo $PW | sudo -S nohup setsid python3 /tmp/offset_tracker.py $iface $AP_MACS \
                   --raw /tmp/raw_fits.json >/tmp/tracker.log 2>&1 & echo -n ''"
        echo "  raw-fit tracker on $name ($ip) launched"
    done <<< "$MON_LINES"
    # kill BOTH host tracker types: they share one /tmp/offset.json, so a stray beacon_tracker
    # (from a sync-mode switch or a prior `compare`) would race and corrupt the offsets.
    pkill -9 -f '[m]onitor_tracker.py' 2>/dev/null
    pkill -9 -f '[b]eacon_tracker.py' 2>/dev/null
    rm -f /tmp/offset.json
    sleep 3
    nohup python3 "$SCRIPTS/monitor_tracker.py" "$TOPO" >/tmp/monitor_tracker.log 2>&1 &
    sleep 6
    echo -n "  monitor-graph-tracker(host): "; cat /tmp/offset.json 2>/dev/null || echo "MISSING (see /tmp/monitor_tracker.log)"
}

start_clock_tracker() {   # dispatch on sync family
    if [ "$SYNC" = beacon ]; then start_beacon_tracker
    elif [ "$MULTIMON" = 1 ]; then start_monitor_graph_tracker
    else start_tracker; fi
}

# file-age in seconds for a HOST-local file (BSD stat on darwin, GNU stat on linux)
_host_age() { local m; m=$(stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null); \
    [ -n "$m" ] && echo $(( $(date +%s) - m )) || echo MISSING; }

# every AP must appear in the host-local offset.json; solve_offsets silently drops an AP
# disconnected from the reference, which would then abort the fire -- so name it here instead.
_offset_coverage() {
    local m miss=""
    for m in $AP_MACS; do grep -qi "\"$m\"" /tmp/offset.json 2>/dev/null || miss="$miss $m"; done
    [ -n "$miss" ] && echo "  |  offset.json MISSING APs:$miss (disconnected graph -> they cannot fire)" \
        || echo "  |  offset.json covers all APs"
}

# ---- diagnostics --------------------------------------------------------------------
# `doctor` probes every node and prints a PASS/FAIL table so an intermittent "0 frames
# RX" resolves to a named layer instead of a guess. The decisive check is per-AP beacon
# reachability at each station: a station that is SILENT to the AP targeting it cannot
# receive that AP's data either (the 0-RX cause); one that hears beacons but still gets
# rx=0 points at the gate/HT-data path, not the capture.

_fresh_secs() {   # ip path -> file age in seconds, or MISSING
    sh_ "$1" "now=\$(date +%s); m=\$(stat -c %Y '$2' 2>/dev/null); \
              [ -n \"\$m\" ] && echo \$((now-m)) || echo MISSING"
}

_reach() {   # ip -> 0 if the VM answers ssh, 1 otherwise
    [ "$(sh_ "$1" 'echo ok')" = ok ]
}

doctor_ap() {   # name ip iface
    local name="$1" ip="$2" iface="$3"
    echo "AP $name ($ip)"
    if ! _reach "$ip"; then
        echo '  UNREACHABLE .. FAIL (VM down / card dropped off USB -> re-enumerate the dongle)'
        return
    fi
    sh_ "$ip" "
        pgrep hostapd >/dev/null && echo '  hostapd ....... PASS' || echo '  hostapd ....... FAIL (down)'
        ls /sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx >/dev/null 2>&1 \
            && echo '  gated node .... PASS' || echo '  gated node .... FAIL (wrong/old firmware?)'
        # this ath9k_htc iw build prints no channel line; fall back to the hostapd config we set
        ch=\$(iw dev $iface info 2>/dev/null | awk '/channel/{print \$2}')
        [ -z \"\$ch\" ] && ch=\$(grep -oE 'channel=[0-9]+' /tmp/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2)
        [ \"\$ch\" = '$CHANNEL' ] && echo \"  channel ....... PASS (\$ch)\" \
            || echo \"  channel ....... FAIL (\$ch, want $CHANNEL)\"
        w=\$(echo $PW | sudo -S dmesg 2>/dev/null | tail -60 \
             | grep -iE 'failed to|timeout|-110\b|usb disconnect|hw reset' | tail -1)
        [ -z \"\$w\" ] && echo '  wedge scan .... clean' \
            || echo \"  wedge scan .... WARN (re-enumerate USB): \$w\""
    # beacon sync needs the driver beacon tap (cosr_beacons node) AND the AP must actually
    # hear peer APs -- a present-but-empty tap is an isolated AP (the beacon-mode 0-fire cause).
    if [ "$SYNC" = beacon ]; then
        if ! sh_ "$ip" "ls /sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_beacons >/dev/null 2>&1 && echo y" | grep -q y; then
            echo '  beacon tap .... FAIL (no cosr_beacons node -> roll driver.diff to this AP)'
        else
            local bc peers m; bc=$(sh_ "$ip" "cat /sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_beacons 2>/dev/null")
            peers=0
            for m in $AP_MACS; do echo "$bc" | grep -qi "$m" && peers=$((peers+1)); done
            [ "$peers" -gt 0 ] && echo "  beacon tap .... PASS (hears $peers peer AP)" \
                || echo '  beacon tap .... WARN (node present but hears NO peer AP -> isolated: check AP<->AP range/channel)'
        fi
    fi
}

doctor_station() {   # name ip iface
    local name="$1" ip="$2" iface="$3"
    echo "station $name ($ip)"
    if ! _reach "$ip"; then
        echo '  UNREACHABLE .. FAIL (VM down / card dropped off USB -> re-enumerate the dongle)'
        return
    fi
    sh_ "$ip" "
        iw dev $iface info 2>/dev/null | grep -q 'type monitor' \
            && echo '  monitor mode .. PASS' || echo '  monitor mode .. FAIL (not monitor)'
        echo $PW | sudo -S timeout 3 tcpdump -i $iface -e -n -c 80 type mgt subtype beacon \
            2>/dev/null > /tmp/doc_bc.txt
        tot=\$(wc -l < /tmp/doc_bc.txt)
        [ \"\$tot\" -gt 0 ] && echo \"  hears beacons . PASS (\$tot in 3s)\" \
            || echo '  hears beacons . FAIL (0 -- RX dead / wrong channel / wedged card)'
        # no channel line on this iw build; hearing the APs' co-channel beacons IS the proof
        ch=\$(iw dev $iface info 2>/dev/null | awk '/channel/{print \$2}')
        if [ -n \"\$ch\" ]; then
            [ \"\$ch\" = '$CHANNEL' ] && echo \"  channel ....... PASS (\$ch)\" \
                || echo \"  channel ....... FAIL (\$ch, want $CHANNEL)\"
        elif [ \"\$tot\" -gt 0 ]; then echo '  channel ....... PASS (co-channel: hears AP beacons)'
        else echo '  channel ....... UNKNOWN (0 beacons -- cannot confirm)'; fi"
    # per-AP reachability: can this station hear each AP's beacons? (the 0-RX tell)
    local bc; bc=$(sh_ "$ip" "cat /tmp/doc_bc.txt 2>/dev/null")
    while IFS='|' read -r apn api apif apssid apmac; do
        [ -z "$apn" ] && continue
        local c; c=$(grep -c "SA:$apmac" <<< "$bc")
        [ "$c" -gt 0 ] && echo "  <- $apn ...... hears ($c)" \
            || echo "  <- $apn ...... SILENT (cannot hear this AP -> its data cannot arrive)"
    done <<< "$AP_LINES"
}

# ---- commands -----------------------------------------------------------------------
case "$cmd" in

up)
    echo "channel $CHANNEL"
    while IFS='|' read -r name ip iface ssid mac; do
        [ -n "$name" ] && bring_up_ap "$ip" "$iface" "$ssid"
    done <<< "$AP_LINES"
    while IFS='|' read -r name ip iface sid; do
        [ -n "$name" ] && bring_up_monitor "$ip" "$iface"
    done <<< "$STA_LINES"
    start_clock_tracker
    ;;

status)
    while IFS='|' read -r name ip iface ssid mac; do
        [ -z "$name" ] && continue
        echo -n "AP $name ($ip): "
        sh_ "$ip" "pgrep hostapd >/dev/null && echo -n 'hostapd up ' || echo -n 'DOWN '; \
                   ls /sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx >/dev/null 2>&1 \
                   && echo node_ok || echo NO_NODE"
    done <<< "$AP_LINES"
    while IFS='|' read -r name ip iface sid; do
        [ -z "$name" ] && continue
        echo -n "station $name ($ip): "
        sh_ "$ip" "iw dev $iface info 2>/dev/null | grep -o 'type monitor' | head -1 || echo not-monitor"
    done <<< "$STA_LINES"
    if [ "$SYNC" = beacon ]; then
        echo -n "beacon-tracker (host): "
        pgrep -f '[b]eacon_tracker.py' >/dev/null && echo -n 'running  ' || echo -n 'NOT running  '
        echo -n 'offset.json: '; cat /tmp/offset.json 2>/dev/null || echo MISSING
    elif [ "$MULTIMON" = 1 ]; then
        echo -n "monitor-graph-tracker (host): "
        pgrep -f '[m]onitor_tracker.py' >/dev/null && echo -n 'running  ' || echo -n 'NOT running  '
        echo -n 'offset.json: '; cat /tmp/offset.json 2>/dev/null || echo MISSING
    else
        echo -n "tracker ($OBS_NAME): "
        sh_ "$OBS_IP" "pgrep -f '[o]ffset_tracker.py' >/dev/null && echo -n 'running  ' || echo -n 'NOT running  '; \
                       echo -n 'offset.json: '; cat /tmp/offset.json 2>/dev/null || echo MISSING"
    fi
    ;;

shot|measure|test-sync|gate)
    # all four go through the one controller + the two config files -- no bespoke path.
    # `gate` fires the first link's AP alone (single-AP absolute precision).
    SHOT="${1:-$HERE/shot.json}"
    python3 "$SCRIPTS/cosr_ctl.py" "$cmd" "$TOPO" "$SHOT"
    ;;

compare)
    # Head-to-head accuracy of the two AP-sync methods on ONE testbed: bring up each clock
    # source in turn (OTA beacon tracker, then the multi-monitor graph tracker), fire the
    # SAME staggered test-sync through each, and print the per-AP bias/jitter/p90 side by
    # side -- lower is better. Both write the same /tmp/offset.json the controller reads, so
    # only the clock source differs. Needs the OTA tap on the APs (method A) and a "monitors"
    # list in topo (method B). See docs/SYNC.md "Comparing the two methods".
    if [ -z "$(echo -n "$MON_LINES" | tr -d '[:space:]')" ]; then
        echo "compare needs a 'monitors': [...] list in $TOPO for the multi-monitor leg" >&2
        exit 1
    fi
    SHOT="${1:-$HERE/shot.json}"
    tmpo=$(mktemp); tmpm=$(mktemp)
    echo "=== method A: OTA beacon sync ==="
    start_beacon_tracker
    python3 "$SCRIPTS/cosr_ctl.py" test-sync "$TOPO" "$SHOT" | tee "$tmpo"
    pkill -9 -f '[b]eacon_tracker.py' 2>/dev/null
    echo "=== method B: multi-monitor sync ==="
    start_monitor_graph_tracker
    python3 "$SCRIPTS/cosr_ctl.py" test-sync "$TOPO" "$SHOT" | tee "$tmpm"
    pkill -9 -f '[m]onitor_tracker.py' 2>/dev/null
    echo ""
    echo "=== comparison (per non-reference AP: |bias| + jitter, lower = better) ==="
    python3 - "$tmpo" "$tmpm" <<'PY'
import json, sys
def load(p):
    try: return json.load(open(p))
    except Exception: return {}
a, b = load(sys.argv[1]), load(sys.argv[2])
pa, pb = a.get("per_ap", {}), b.get("per_ap", {})
print("%-10s %20s %20s" % ("AP", "OTA beacon", "multi-monitor"))
def fmt(d):
    if not d or d.get("bias_us") is None: return "n/a"
    return "bias %+.2f jit %.2f p90 %s" % (d["bias_us"], d["jitter_us"], d["p90_abs_us"])
for ap in sorted(set(pa) | set(pb)):
    print("%-10s %20s %20s" % (ap, fmt(pa.get(ap)), fmt(pb.get(ap))))
def score(p):
    v = [abs(d["bias_us"]) + d["jitter_us"] for d in p.values() if d.get("bias_us") is not None]
    return sum(v) / len(v) if v else None
sa, sb = score(pa), score(pb)
if sa is not None and sb is not None:
    print("\nmean(|bias|+jitter): OTA %.2f us   multi-monitor %.2f us  -> %s more accurate here"
          % (sa, sb, "OTA" if sa < sb else "multi-monitor"))
PY
    rm -f "$tmpo" "$tmpm"
    # both legs left their tracker killed; restore the topo's configured clock source so a
    # following `shot`/`measure` reads the right offset.json (not a stale compare tracker).
    echo "=== restoring configured sync source ($SYNC) ==="
    start_clock_tracker
    ;;

down)
    while IFS='|' read -r name ip iface ssid mac; do
        [ -z "$name" ] && continue
        sh_ "$ip" "echo $PW | sudo -S pkill hostapd; echo '  $name ($ip) hostapd killed'"
    done <<< "$AP_LINES"
    while IFS='|' read -r name ip iface sid; do
        [ -z "$name" ] && continue
        sh_ "$ip" "echo $PW | sudo -S pkill -x tcpdump 2>/dev/null; echo '  $name ($ip) capture killed'"
    done <<< "$STA_LINES"
    # clock tracker: single-observer runs on the observer, beacon + multi-monitor on this
    # host -- tear down whichever is present (harmless if the others were never started).
    pkill -9 -f '[b]eacon_tracker.py' 2>/dev/null && echo '  host beacon-tracker killed'
    pkill -9 -f '[m]onitor_tracker.py' 2>/dev/null && echo '  host monitor-graph-tracker killed'
    rm -f /tmp/offset.json
    while IFS='|' read -r name ip iface; do
        [ -z "$name" ] && continue
        sh_ "$ip" "echo $PW | sudo -S pkill -9 -f '[o]ffset_tracker.py'; rm -f /tmp/raw_fits.json; echo '  raw-fit tracker on $name killed'"
    done <<< "$MON_LINES"
    sh_ "$OBS_IP" "echo $PW | sudo -S pkill -9 -f '[o]ffset_tracker.py'; rm -f /tmp/offset.json; echo '  observer tracker killed'"
    ;;

doctor)
    echo "channel $CHANNEL  (PASS/FAIL per node; SILENT link = the 0-RX cause)"
    while IFS='|' read -r name ip iface ssid mac; do
        [ -n "$name" ] && doctor_ap "$name" "$ip" "$iface"
    done <<< "$AP_LINES"
    while IFS='|' read -r name ip iface sid; do
        [ -n "$name" ] && doctor_station "$name" "$ip" "$iface"
    done <<< "$STA_LINES"
    if [ "$SYNC" = beacon ]; then
        echo -n "beacon-tracker (host): "
        pgrep -f '[b]eacon_tracker.py' >/dev/null && echo -n 'PASS proc' || echo -n 'FAIL not-running'
        age=$(_host_age /tmp/offset.json)
        # beacon mode polls over ssh (~1-2 s cadence), so allow a looser freshness bound
        if [ "$age" = "MISSING" ]; then echo "  |  offset.json MISSING"
        elif [ "$age" -le 15 ] 2>/dev/null; then echo "  |  offset.json fresh (${age}s)"; _offset_coverage
        else echo "  |  offset.json STALE (${age}s -> restart: ./run.sh reset)"; fi
    elif [ "$MULTIMON" = 1 ]; then
        echo -n "monitor-graph-tracker (host): "
        pgrep -f '[m]onitor_tracker.py' >/dev/null && echo -n 'PASS proc' || echo -n 'FAIL not-running'
        age=$(_host_age /tmp/offset.json)
        if [ "$age" = "MISSING" ]; then echo "  |  offset.json MISSING"
        elif [ "$age" -le 15 ] 2>/dev/null; then echo "  |  offset.json fresh (${age}s)"; _offset_coverage
        else echo "  |  offset.json STALE (${age}s -> restart: ./run.sh reset)"; fi
        # each clock monitor must be publishing fresh raw fits, else its edges are missing
        while IFS='|' read -r name ip iface; do
            [ -z "$name" ] && continue
            fa=$(_fresh_secs "$ip" /tmp/raw_fits.json)
            if [ "$fa" = "MISSING" ]; then echo "  |  monitor $name: raw_fits MISSING (offset_tracker --raw not running / hears no AP)"
            elif [ "$fa" -le 15 ] 2>/dev/null; then echo "  |  monitor $name: raw fits fresh (${fa}s)"
            else echo "  |  monitor $name: raw fits STALE (${fa}s)"; fi
        done <<< "$MON_LINES"
    else
        echo -n "tracker ($OBS_NAME): "
        if ! _reach "$OBS_IP"; then
            echo "FAIL observer $OBS_IP UNREACHABLE"
        else
            sh_ "$OBS_IP" "pgrep -f '[o]ffset_tracker.py' >/dev/null && echo -n 'PASS proc' || echo -n 'FAIL not-running'"
            age=$(_fresh_secs "$OBS_IP" /tmp/offset.json)
            if [ "$age" = "MISSING" ]; then echo "  |  offset.json MISSING"
            elif [ "$age" -le 10 ] 2>/dev/null; then echo "  |  offset.json fresh (${age}s)"
            else echo "  |  offset.json STALE (${age}s -> restart tracker: ./run.sh reset)"; fi
        fi
    fi
    echo "---"
    echo "reads: any FAIL/SILENT above names the culprit. Soft faults (mode/channel/tracker)"
    echo "  -> ./run.sh reset. A 'wedge scan WARN' needs a physical USB re-enumerate."
    ;;

reset)
    # fast fix for the common soft faults, WITHOUT the full `up` (which downs the APs and
    # rebases their TSF): re-assert every station's monitor mode + channel, bounce only a
    # dead AP (a live AP keeps its clock), and restart the offset tracker.
    echo "reset: channel $CHANNEL"
    while IFS='|' read -r name ip iface sid; do
        [ -n "$name" ] && bring_up_monitor "$ip" "$iface"
    done <<< "$STA_LINES"
    while IFS='|' read -r name ip iface ssid mac; do
        [ -z "$name" ] && continue
        # A live AP must be BOTH hostapd-up AND actually in AP mode: a stale hostapd left
        # after a USB reconnect leaves the iface in `type managed` (WMI half-broken), and
        # checking only `pgrep hostapd` would wrongly leave it "undisturbed" and skip the
        # real bringup. Require `iw ... type AP` so a managed-mode card gets re-brought-up.
        if sh_ "$ip" "pgrep hostapd >/dev/null && iw dev $iface info 2>/dev/null | grep -q 'type AP' && echo up" | grep -q up; then
            echo "  AP $name ($ip): hostapd up + AP mode (left undisturbed)"
        else
            bring_up_ap "$ip" "$iface" "$ssid"
        fi
    done <<< "$AP_LINES"
    start_clock_tracker
    echo "reset done -- re-run ./run.sh doctor to confirm."
    ;;

*)
    echo "usage: $0 {up|status|doctor|reset|down} <topo.json>"
    echo "       $0 {shot|measure|test-sync|gate|compare} <topo.json> [shot.json]"
    echo "       $0 scan <ip> [ip ...]"
    exit 1 ;;
esac
