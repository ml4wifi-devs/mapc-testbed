"""Placing the node program on every node and keeping it in step with this controller.

The node program is pushed as plain source over ssh, so a node needs nothing installed beyond
the Python that ships with its image. After pushing, the node is asked to hash what it now has
and the answer is compared with what was sent: a copy that failed part way leaves a node running
code the controller does not have, and the resulting numbers look ordinary.

Restarting is stop-then-verify-gone-then-start. Starting first and hoping the old process exits
leaves two agents on one node, both answering, one of them stale.
"""
import io
import os
import subprocess
import tarfile

from . import agent as agent_mod

# Everything the node program consists of. The controller-only parts -- relating clocks,
# accounting, driving rounds -- stay here, so a node cannot silently diverge on them.
MODULES = ("__init__.py", "agent.py", "counter.py", "natsc.py",
           "nl80211.py", "proto.py", "radiotap.py", "wire.py")

REMOTE_DIR = "/opt/cosr"
LOG = "/tmp/cosr-agent.log"

SSH_OPTS = ["-o", "StrictHostKeyChecking=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ConnectTimeout=10"]

_HERE = os.path.dirname(os.path.abspath(__file__))


class DeployError(RuntimeError):
    pass


def expected_hash():
    """What every node must report once it is up to date."""
    return agent_mod.source_hash()


class Deployer(object):

    def __init__(self, plan, hub, token=None, remote_dir=REMOTE_DIR, runner=None):
        self.plan = plan
        self.hub = hub
        self.token = token
        self.remote_dir = remote_dir
        self._run = runner or _run

    # ---------------------------------------------------------------- transport

    def _ssh(self, node, command, check=True, stdin=None):
        # `-n` only when nothing is being sent: it redirects stdin from /dev/null, which would
        # discard the very bytes a transfer is trying to deliver.
        argv = (["sshpass", "-p", node["password"], "ssh"]
                + ([] if stdin is not None else ["-n"]) + SSH_OPTS
                + ["%s@%s" % (node["user"], node["ip"]), command])
        rc, out, err = self._run(argv, stdin=stdin)
        if check and rc != 0:
            raise DeployError("%s: command failed (%s): %s"
                              % (node["name"], rc, (err or out).strip()))
        return out

    def _send(self, node, blob, remote):
        """Write bytes to a path on the node over the ssh channel itself.

        Not scp: current versions carry the file over the SFTP subsystem, which authenticates
        separately and does not accept a password supplied the way an ordinary ssh invocation
        does. Writing through a shell keeps every transfer on the one authenticated path.
        """
        self._ssh(node, "cat > %s" % _quote(remote), stdin=blob)

    def _sudo(self, node, command, check=True):
        """`-S` reads the password from stdin and `-p ''` keeps the prompt out of the output,
        so what comes back is the command's own output and nothing else."""
        return self._ssh(node, "echo %s | sudo -S -p '' sh -c %s"
                         % (_quote(node["password"]), _quote(command)), check=check)

    # ---------------------------------------------------------------- lifecycle

    def nodes(self):
        return [n for n in self.plan["nodes"] if n.get("ip")]

    def push(self, node):
        # Delivered as one archive to a path the login user owns, then unpacked as root. A
        # transfer cannot elevate, and writing straight into a root-owned directory fails
        # quietly on some images.
        stage = "/tmp/cosr-modules.tar"
        self._send(node, _archive(), stage)
        self._sudo(node, "mkdir -p %s/cosr && tar xf %s -C %s/cosr && rm -f %s"
                   % (self.remote_dir, stage, self.remote_dir, stage))

        got = self.verify(node)
        want = expected_hash()
        if got != want:
            raise DeployError(
                "%s reports build %s after the copy but this controller is %s. The copy did not "
                "land completely; the node would answer with code that is not what was sent."
                % (node["name"], got or "nothing", want))
        return got

    def verify(self, node):
        """The build the node actually has on disk, independent of what is running."""
        out = self._ssh(
            node,
            "python3 -c \"import sys; sys.path.insert(0, '%s'); "
            "from cosr import agent; print(agent.source_hash())\"" % self.remote_dir,
            check=False)
        out = (out or "").strip().splitlines()
        return out[-1].strip() if out else ""

    def stop(self, node):
        # The bracket keeps the pattern from matching the shell that carries it: the command
        # line of that shell contains the pattern verbatim, so a plain match kills itself and
        # reports success without stopping the agent.
        self._sudo(node, "pkill -f '[c]osr/agent.py' || true", check=False)
        for attempt in range(20):
            out = self._ssh(node, "pgrep -f '[c]osr/agent.py' || true", check=False)
            if not (out or "").strip():
                return True
            # Ask once, then insist. A process that has not gone after several seconds is not
            # going to; leaving it running would mean two agents answering for one node, which
            # is worse than killing it outright.
            if attempt == 8:
                self._sudo(node, "pkill -9 -f '[c]osr/agent.py' || true", check=False)
            _sleep(0.25)
        raise DeployError("%s: the previous agent did not exit even after being killed; two "
                          "agents would answer for it" % node["name"])

    def _argv_for(self, node):
        args = ["--role", node["role"], "--name", node["name"],
                "--iface", node["iface"], "--hub", self.hub]
        if self.token:
            args += ["--token", self.token]
        if node["role"] == "ap":
            args += ["--mac", node["mac"]]
        else:
            args += ["--station-id", str(node["station_id"]),
                     "--ap-macs", ",".join(self.plan["all_ap_macs"])]
        return args

    def start(self, node):
        cmd = ("cd %s && nohup python3 cosr/agent.py %s >> %s 2>&1 &"
               % (self.remote_dir, " ".join(self._argv_for(node)), LOG))
        self._sudo(node, cmd)
        for _ in range(20):
            if (self._ssh(node, "pgrep -f '[c]osr/agent.py' || true",
                          check=False) or "").strip():
                return True
            _sleep(0.25)
        tail = self._ssh(node, "tail -n 20 %s || true" % LOG, check=False)
        raise DeployError("%s: the agent did not stay up. Its output was:\n%s"
                          % (node["name"], tail))

    # ---------------------------------------------------------------- radios

    def bring_up_ap(self, node, channel, ssid):
        """Start the access point and make the transmit trigger writable.

        The interface is brought down first: after stopping a previous instance it is left up
        and managed, and setting access-point mode on a live interface races with that state.
        The rate is pinned to a high-throughput one because a shot commands a modulation index,
        which a basic-rate-only network would silently ignore.
        """
        conf = ("interface=%s\ndriver=nl80211\nssid=%s\nhw_mode=g\nchannel=%s\n"
                "ieee80211n=1\nwmm_enabled=1\nrequire_ht=1\nht_capab=[SHORT-GI-20]\n"
                % (node["iface"], ssid, channel))
        self._sudo(node, "pkill hostapd 2>/dev/null; true", check=False)
        self._sudo(node, "ip link set %s down" % node["iface"], check=False)
        self._send(node, conf.encode("utf-8"), "/tmp/hostapd.conf")
        self._sudo(node, "hostapd -B /tmp/hostapd.conf > /tmp/hostapd.log 2>&1 || true",
                   check=False)
        for _ in range(20):
            if (self._ssh(node, "pgrep hostapd || true", check=False) or "").strip():
                break
            _sleep(0.5)
        else:
            log = self._ssh(node, "tail -n 5 /tmp/hostapd.log || true", check=False)
            raise DeployError("%s: the access point did not start:\n%s" % (node["name"], log))
        # The transmit trigger and the beacon ring live under debugfs. Only these two entries
        # are opened up, and only for the owner's group: making the whole of debugfs
        # world-writable would hand every local user the radio, and is not needed anyway
        # because the node program runs as root.
        self._sudo(node, "chmod 0640 /sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_beacons "
                         "/sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx 2>/dev/null; "
                         "true", check=False)

    def bring_up_monitor(self, node, channel):
        """Put a receiver into monitor mode on the shot's channel."""
        iface = node["iface"]
        self._sudo(node, "ip link set %s down" % iface)
        self._sudo(node, "iw dev %s set type monitor" % iface)
        self._sudo(node, "ip link set %s up" % iface)
        self._sudo(node, "iw dev %s set channel %s" % (iface, channel))

    def radios(self, channel, ssids):
        for node in self.nodes():
            if node["role"] == "ap":
                self.bring_up_ap(node, channel, ssids.get(node["name"], node["name"]))
            else:
                self.bring_up_monitor(node, channel)

    def up(self, nodes=None):
        """Push and restart every node, reporting what happened to each."""
        out = {}
        for node in (nodes if nodes is not None else self.nodes()):
            self.stop(node)
            digest = self.push(node)
            self.start(node)
            out[node["name"]] = digest
        return out

    def down(self, nodes=None):
        for node in (nodes if nodes is not None else self.nodes()):
            self.stop(node)


def _quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _sleep(seconds):
    import time
    time.sleep(seconds)


def _archive():
    """Every node module as one tar, so a deployment is a single transfer."""
    buf = io.BytesIO()
    tar = tarfile.open(fileobj=buf, mode="w")
    for name in MODULES:
        tar.add(os.path.join(_HERE, name), arcname=name)
    tar.close()
    return buf.getvalue()


def _run(argv, stdin=None):
    """(exit status, stdout, stderr), kept apart.

    ssh writes login banners and warnings to stderr. Folding them into stdout makes every
    "is anything running?" check see text and conclude yes, so a node would never be reported
    as idle and the output of a remote command could never be read back cleanly.
    """
    p = subprocess.Popen(argv, stdin=(subprocess.PIPE if stdin is not None else None),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(stdin)
    if not isinstance(out, str):
        out = out.decode("utf-8", "replace")
    if not isinstance(err, str):
        err = err.decode("utf-8", "replace")
    return p.returncode, out, err
