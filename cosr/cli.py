"""Command line for a Co-SR testbed.

Every command takes the testbed description first and the experiment second, so the same pair of
files drives bringing the testbed up, checking it, and measuring on it. Nothing about a
particular deployment is built in here.

  cosr up        <topo> [exp]   start the radios, place the node program, connect everything
  cosr status    <topo> [exp]   what is running, on which build, and whether the clocks relate
  cosr doctor    <topo> [exp]   check every way the clocks can diverge -> a verdict
  cosr run       <topo> [exp]   fire one coordinated round -> delivery, status and levels
  cosr calibrate <topo> [exp]   find the shortest spacing this hardware actually delivers at
  cosr reset     <topo> [exp]   clear what the nodes have accumulated, without redeploying
  cosr down      <topo> [exp]   stop the node program and the radios
  cosr scan      <ip> [ip ...]  read each node's wireless interface and address
"""
import json
import socket
import sys

from . import deploy as deploy_mod
from . import natsc
from . import proto
from . import scan as scan_mod
from . import session as session_mod
from . import topo as topo_mod

USAGE = __doc__


class CliError(RuntimeError):
    pass


def local_address(target):
    """The address of this host that a node would reach it on.

    Asking the routing table rather than resolving the hostname: a controller commonly has
    several addresses and only one of them faces the testbed.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def load_plan(topo_path, exp_path):
    with open(topo_path) as fh:
        topo = json.load(fh)
    with open(exp_path) as fh:
        experiment = json.load(fh)
    return topo_mod.resolve(topo, experiment)


def hub_for(plan):
    if plan.get("hub"):
        return plan["hub"]
    first = plan["nodes"][0]["ip"] if plan["nodes"] else "127.0.0.1"
    return local_address(first)


def _deployer(plan):
    return deploy_mod.Deployer(plan, hub=hub_for(plan), token=plan.get("token"))


def _session(plan, need_clock=True):
    """Connect, and by default wait until a round could actually be commanded.

    `need_clock=False` is for the commands that exist to recover a testbed rather than measure on
    one. Requiring a usable clock plane there would make them refuse in exactly the state they
    are meant to repair -- a radio that has stopped hearing its peers has no path to the
    reference, which is the condition `reset` is for.
    """
    s = session_mod.Session(plan, hub=hub_for(plan), token=plan.get("token")).connect()
    s.wait_for_agents()
    if need_clock:
        s.wait_for_clock()
    return s


# ------------------------------------------------------------------ commands

def cmd_up(plan, out):
    dep = _deployer(plan)
    ssids = dict((n["name"], n["ssid"] or n["name"]) for n in plan["nodes"] if n["role"] == "ap")
    out("bringing up radios on channel %s" % plan["channel"])
    dep.radios(plan["channel"], ssids)
    out("placing the node program (build %s)" % deploy_mod.expected_hash())
    for name, digest in sorted(dep.up().items()):
        out("  %-8s %s" % (name, digest))
    sess = _session(plan)
    try:
        st = sess.clockd.status()
        out("clocks related: %d edges, %d participants" % (len(st["edges"]),
                                                           len(st["reachable"])))
    finally:
        sess.close()
    return 0


def cmd_down(plan, out):
    dep = _deployer(plan)
    dep.down()
    for node in dep.nodes():
        if node["role"] == "ap":
            dep._sudo(node, "pkill hostapd 2>/dev/null; true", check=False)
    out("stopped")
    return 0


def cmd_status(plan, out):
    dep = _deployer(plan)
    want = deploy_mod.expected_hash()
    out("controller build %s" % want)
    for node in dep.nodes():
        got = dep.verify(node)
        live = bool((dep._ssh(node, "pgrep -f '[c]osr/agent.py' || true",
                              check=False) or "").strip())
        out("  %-8s %-9s installed=%s%s"
            % (node["name"], "running" if live else "STOPPED", got or "nothing",
               "" if got == want else "  (does not match this controller)"))
    return 0


def cmd_doctor(plan, out):
    sess = _session(plan)
    try:
        rep = sess.doctor()
    finally:
        sess.close()
    out("verdict: %s" % rep["verdict"])
    for stage in rep["stages"]:
        out("  %-17s %-13s %s" % (stage["stage"], stage["verdict"], stage["detail"]))
    return 0 if rep["verdict"] == "PASS" else 1


def cmd_reset(plan, out):
    """Discard what every node has accumulated, without replacing the program it is running.

    Bringing the deployment up does this too, by restarting each agent, but that also re-copies
    the program and takes far longer. This is the quick way to clear counts gathered under a
    different arrangement of nodes, and to clear a radio that reported its command interface
    had failed.
    """
    sess = _session(plan, need_clock=False)
    try:
        for role, name in sess.participants():
            resp = sess.client.request_json(proto.subj_rpc(role, name), {"op": "reset"},
                                            timeout=5.0)
            out("  %-8s %s" % (name, "reset" if resp.get("ok") else resp.get("error")))
    finally:
        sess.close()
    return 0


def cmd_run(plan, out):
    sess = _session(plan)
    try:
        res = sess.run()
    finally:
        sess.close()
    _print_links(res, out)
    for pair, dbm in sorted(res["rssi_ap_to_ap_dbm"].items()):
        out("  between transmitters %-18s %s dBm" % (pair, dbm))
    return 0


SPACING_SWEEP = (5000, 10000, 15000, 25000, 50000, 75000, 100000)


def cmd_calibrate(plan, out, repeats=3, margin=0.05, floor=0.5):
    """Find the shortest spacing between shots this hardware actually delivers at.

    Swept against frames that arrived, never against what the gate accepted: a shot the gate
    accepts has been scheduled, not transmitted, and the buffer pool holds one aggregate at a
    time. Sweeping acceptance alone therefore reports a spacing far shorter than anything that
    works, and nothing downstream would reveal the difference.
    """
    sess = _session(plan)
    out("shape: %d frame(s) of %d bytes, %d repeats per spacing"
        % (plan["nframes"], plan["frame_len"], repeats))
    if len(plan["aps"]) > 1:
        # Concurrent transmitters collide by design, so delivery here reflects the overlap far
        # more than the spacing, and the sweep would report that no spacing works.
        out("note: this shot has %d links firing together, so what is measured is mostly their "
            "overlap. Calibrate spacing on a shot with a single link."
            % len(plan["aps"]))
    out("  %-12s %-10s %s" % ("spacing", "worst", "verdict"))
    swept = []
    try:
        for spacing in SPACING_SWEEP:
            worst, why = None, None
            for _ in range(repeats):
                res = sess.run(spacing_us=spacing)
                got = [e["delivery"] for e in res["links"].values()
                       if e["delivery"] is not None]
                if not got:
                    # A missing number is never a bad number. Say which link withheld one and
                    # why, so a sweep that found nothing is distinguishable from one that found
                    # nothing works.
                    worst = None
                    why = "; ".join("%s %s(%s)" % (k, e["status"], e["reason"])
                                    for k, e in sorted(res["links"].items()))
                    break
                worst = min(got) if worst is None else min(worst, min(got))
            swept.append([spacing, worst, why])
    finally:
        sess.close()

    # Judged against the best this link achieved, not against a fixed fraction. A passive
    # receiver does not capture everything even on an idle channel, so its ceiling is a property
    # of the receiver; comparing to an absolute would call every spacing bad on a perfectly
    # usable link.
    unmeasured = [(s, w, why) for s, w, why in swept if w is None and why]
    seen = [w for _s, w, _why in swept if w is not None]
    best = max(seen) if seen else None
    if best is None:
        out("nothing was delivered at any spacing; check `doctor` before reading anything here.")
        return 1
    for row in swept:
        row[2] = row[1] is not None and row[1] >= best - margin
    out("  best delivery seen: %.2f" % best)
    for spacing, worst, ok in swept:
        out("  %-12s %-10s %s" % ("%d us" % spacing,
                                  "n/a" if worst is None else "%.2f" % worst,
                                  "as good as it gets" if ok else "loses more"))
    for spacing, worst, why in unmeasured:
        out("  %d us was not measured: %s" % (spacing, why))
    if best < floor:
        out("the best any spacing achieved was %.2f, so this link is losing frames for a reason "
            "unrelated to spacing. Fix that first; a spacing chosen here would be meaningless."
            % best)
        return 1

    # The shortest spacing from which every longer one is also clean. Taking the first clean
    # result on its own would accept a spacing that happens to survive one batch while a longer
    # one fails, which is noise rather than a threshold, and would then be used for every
    # subsequent measurement.
    chosen = None
    for spacing, _worst, ok in reversed(swept):
        if not ok:
            break
        chosen = spacing
    if chosen is None:
        out("no spacing held the best result from there upwards, so the sweep found no "
            "threshold -- delivery is varying for some other reason. Check `doctor`.")
        return 1
    out("shortest spacing that reached that, and stayed there above: %d us" % chosen)
    return 0


def _print_links(res, out):
    out("%.3f s, %.1f Mb/s over %d measured link(s)"
        % (res["seconds"], res["throughput_mbps"], res["measured_links"]))
    for key in sorted(res["links"]):
        e = res["links"][key]
        out("  %-16s %-12s delivery=%-6s rx=%s/%s  rssi=%s"
            % (key, e["status"], e["delivery"], e["rx"], e["sent"], e["rssi_dbm"]))
        if e.get("reason"):
            out("      %s" % e["reason"])
    for w in res["warnings"]:
        out("  ! %s" % w)


COMMANDS = {
    "up": cmd_up, "down": cmd_down, "status": cmd_status, "doctor": cmd_doctor,
    "run": cmd_run, "calibrate": cmd_calibrate, "reset": cmd_reset,
}


def cmd_scan(argv, out):
    """Takes addresses rather than the two description files: it runs before one exists."""
    user, password, ips = "modwifi", "modwifi", []
    i = 0
    while i < len(argv):
        if argv[i] == "--user":
            user = argv[i + 1]
            i += 2
        elif argv[i] == "--password":
            password = argv[i + 1]
            i += 2
        else:
            ips.append(argv[i])
            i += 1
    if not ips:
        raise CliError("usage: cosr scan <ip> [ip ...] [--user USER] [--password PW]")
    scan_mod.report(scan_mod.scan(ips, user, password), out)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    cmd = argv.pop(0)

    def out(line):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    if cmd == "scan":
        try:
            return cmd_scan(argv, out)
        except CliError as e:
            sys.stderr.write("%s\n" % e)
            return 2
    if cmd not in COMMANDS:
        sys.stderr.write("unknown command %r\n\n%s" % (cmd, USAGE))
        return 2
    topo_path = argv.pop(0) if argv else "topo.json"
    exp_path = argv.pop(0) if argv else "experiment.json"

    try:
        plan = load_plan(topo_path, exp_path)
        return COMMANDS[cmd](plan, out)
    except (deploy_mod.DeployError, session_mod.NodeMissing, natsc.NatsError,
            proto.ProtocolError, RuntimeError, OSError, ValueError) as e:
        sys.stderr.write("%s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
