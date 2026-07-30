"""The resident process on a testbed node.

One source file serves both roles. A receiver captures continuously and answers questions about
which frames arrived; a transmitter fires commanded shots and reports what the gate did with
each one. Both announce themselves on connect with a hash of their own source, so a controller
talking to a stale deployment finds out immediately instead of collecting subtly different
numbers.

Runs as root: capturing raw frames, writing the transmit trigger, and reading kernel messages all
require it. Constrained to Python 3.4 and the standard library, because that is what the
transmitter images provide.

Usage:
  agent.py --role station --name sta1 --iface mon0 --station-id 1 \
           --ap-macs aa:bb:.. --hub 10.0.0.1 [--token T]
  agent.py --role ap --name ap1 --iface wlan0 --mac aa:bb:.. \
           --hub 10.0.0.1 [--token T]
"""
import hashlib
import os
import socket
import struct
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from cosr import counter as counter_mod
from cosr import natsc
from cosr import proto
from cosr import wire

ETH_P_ALL = 3
SOL_PACKET = 263
PACKET_STATISTICS = 6
CAPTURE_BUF = 4096

# The socket receive buffer has to absorb a burst while the parsing loop is between reads.
# Frames are small and the ambient rate is low, so this is generous rather than tuned.
RCVBUF_BYTES = 4 * 1024 * 1024


def source_hash():
    """Hash of every module that makes up this agent, so a stale deployment is detectable.

    A manifest of names and digests rather than a digest of one file: a mismatch in any part
    changes the result, and the order is fixed so the value is reproducible.
    """
    h = hashlib.sha256()
    for name in sorted(("__init__.py", "agent.py", "counter.py", "natsc.py", "proto.py",
                        "radiotap.py", "wire.py", "nl80211.py")):
        path = os.path.join(_HERE, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            h.update(name.encode("utf-8"))
            h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()[:16]


def make_epoch():
    """Identifies this process instance. A change tells a controller that any counter state it
    was relying on has been lost."""
    rnd = "".join("%02x" % b for b in bytearray(os.urandom(4)))
    return "%s-%d" % (rnd, int(time.time()))


# --------------------------------------------------------------------------- receiver

class StationAgent(object):
    """Captures continuously and answers what arrived for a given round."""

    def __init__(self, name, iface, station_id, ap_macs, stamp_off=wire.STAMP_OFFSET):
        self.name = name
        self.iface = iface
        self.station_id = int(station_id)
        self.epoch = make_epoch()
        self.counter = counter_mod.FrameCounter(ap_macs, station_id, stamp_off,
                                                epoch=self.epoch)
        self._sock = None
        self._stop = threading.Event()
        self._thread = None
        self.frames_seen = 0
        self.started_at = time.time()

    def open(self, sock=None):
        """Bind the capture socket. `sock` substitutes a frame source, which is how the capture
        loop is exercised without a live radio."""
        if sock is not None:
            self._sock = sock
            return self
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
        except (OSError, socket.error):
            pass
        s.bind((self.iface, 0))
        s.settimeout(0.2)
        self._sock = s
        return self

    def start(self):
        self._thread = threading.Thread(target=self._loop)
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _loop(self):
        buf = bytearray(CAPTURE_BUF)
        while not self._stop.is_set():
            try:
                n = self._sock.recv_into(buf, CAPTURE_BUF)
            except socket.timeout:
                continue
            except (OSError, socket.error):
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            self.frames_seen += 1
            self.counter.observe(bytes(buf[:n]))

    def collect_drops(self):
        """Fold the socket's drop count into the counter.

        The kernel resets this counter on read, so exactly one caller may read it; the value is
        accumulated rather than sampled. A non-zero count means the delivery figure is a lower
        bound, which the controller reports rather than hides.
        """
        if self._sock is None:
            return 0
        try:
            raw = self._sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8)
        except (OSError, socket.error):
            return 0
        _packets, drops = struct.unpack("II", raw)
        self.counter.drops += drops
        return drops

    def on_fire(self, msg):
        """A round has been commanded. Returns the report once the round is over, or None if
        this receiver is not taking part."""
        proto.validate_fire(msg)
        mine = (msg.get("stations") or {}).get(self.name)
        if mine is None:
            return None

        # The set of addresses watched is fixed when the agent starts and is not narrowed per
        # round. Narrowing it would judge a frame by whichever set happened to be installed when
        # it arrived, so a frame landing just before the round message would be discarded even
        # though the round wanted it. The round's address list selects what to report on, which
        # the counter already does by keying results per address.
        self.counter.set_run(msg["run"])
        since = int(mine.get("since_ordinal") or 0)
        other_at_start = self.counter.other_frames
        expect = mine.get("expect_per_ap") or {}
        deadline = time.time() + proto.report_deadline_s(msg)

        # Answer as soon as everything expected has arrived; wait out the deadline only when
        # something is missing, which is exactly when the extra time might still help.
        while time.time() < deadline:
            if expect and self._complete(msg, since, expect):
                break
            time.sleep(0.01)

        self.collect_drops()
        counts = self.counter.report(msg["seq0"], msg["n"], since_ordinal=since,
                                     other_since=other_at_start)
        return proto.build_report(msg["run"], msg["batch"], self.name, counts)

    def _complete(self, msg, since, expect):
        rep = self.counter.report(msg["seq0"], msg["n"], since_ordinal=since)
        for mac, want in expect.items():
            got = (rep["per_ap"].get(mac.lower()) or {}).get("rx", 0)
            if got < int(want):
                return False
        return True

    def beacon_rows(self):
        """Beacon observations collected since the last call, and drain them.

        Shaped as `station` rather than `hearer`: relating two of this receiver's own fits
        cancels its clock entirely, so these need no correction for the frame's own duration,
        unlike an observation made by a transmitter.
        """
        rows, self.counter.beacons = self.counter.beacons, []
        return [{"station": self.name, "sender": sender,
                 "station_time": station_time, "t1": t1}
                for sender, station_time, t1 in rows]

    def reset(self):
        """Discard everything accumulated about previous runs.

        Called when a deployment is brought up, because the set of nodes and the addresses being
        watched may have changed since the agent started, and counts gathered under the old
        arrangement should not survive into the new one.
        """
        self.counter = counter_mod.FrameCounter(
            self.counter.ap_macs, self.station_id, self.counter.stamp_off, epoch=self.epoch)
        self.frames_seen = 0
        return {"cleared": "counts"}

    def status(self):
        st = self.counter.report(0, 1)
        return {"role": "station", "name": self.name, "iface": self.iface,
                "station_id": self.station_id, "epoch": self.epoch,
                "uptime_s": round(time.time() - self.started_at, 1),
                "frames_seen": self.frames_seen,
                "other_frames": st["other_frames"],
                "drops": self.counter.drops,
                "freq_mhz": self.counter.observed_freq(),
                "watching": sorted(self.counter.ap_macs),
                "source_hash": source_hash()}


# --------------------------------------------------------------------------- transmitter

FIRED = 0
NOBF = 1
LATE = 2
TOOFAR = 3
CLKLOST = 4             # the radio's clock stopped or was reset while the gate was waiting
DRAIN_STUCK = 5         # fired, but the queue never reported draining: not a trustworthy shot
SKIPPED = 90            # the agent chose not to attempt it; never reported as a gate outcome
UNKNOWN_OUTCOME = 91    # the write failed, so whether it transmitted is genuinely unknown

_STATUS_RE = None


def _status_re():
    global _STATUS_RE
    if _STATUS_RE is None:
        import re
        _STATUS_RE = re.compile(r"cosr_gated_tx: status=(\d+)")
    return _STATUS_RE


class KernelLog(object):
    """Incremental reader over the kernel ring.

    Opened once and positioned at the end, so each read yields only what appeared since the last
    one. The transmit path prints one line per trigger and the line is in the ring before the
    write returns, which is what allows a shot's outcome to be attributed to that shot rather
    than inferred from totals. Nothing is ever cleared, so a retry does not erase the evidence
    of what the previous attempt did.
    """

    def __init__(self, path="/dev/kmsg"):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        os.lseek(self.fd, 0, os.SEEK_END)
        self.overruns = 0

    def read_new(self, bufsize=8192):
        import errno
        chunks = []
        while True:
            try:
                rec = os.read(self.fd, bufsize)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                if e.errno == errno.EPIPE:
                    # Records were overwritten before being read. Keep going: what remains is
                    # still valid, and the loss is counted so it can be reported.
                    self.overruns += 1
                    continue
                raise
            if not rec:
                break
            chunks.append(rec)
        return b"".join(chunks).decode("utf-8", "replace")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ApAgent(object):
    """Fires commanded shots and reports what the gate did with each one.

    A single lock serialises everything that reaches the radio's command interface. Overlapping
    commands are the documented way to leave a card's clock permanently unusable, and the fire
    loop, the clock read and the power setting all go through it.
    """

    def __init__(self, name, iface, mac, node=None, kmsg=None,
                 power_setter=None):
        self.name = name
        self.iface = iface
        self.mac = mac.lower()
        self.epoch = make_epoch()
        self.started_at = time.time()
        self._wmi = threading.Lock()
        self._node = node or _glob_one(
            "/sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_gated_tx")
        self._beacons = _glob_one(
            "/sys/kernel/debug/ieee80211/*/ath9k_htc/cosr_beacons")
        self._kmsg = kmsg if kmsg is not None else KernelLog()
        self._power_setter = power_setter
        self._batches = {}          # (run, batch) -> result, or None while in progress
        self._batch_order = []      # insertion order, for bounding the cache
        self.wedged = None
        self.shots_fired = 0

    # -------------------------------------------------------------- radio access

    def set_power(self, mbm):
        """Assert the transmit power. Re-asserted every round rather than remembered: bringing
        the interface down, or restarting the access point, silently reverts it."""
        if mbm is None:
            return None
        with self._wmi:
            setter = self._power_setter
            if setter is None:
                setter = self._power_setter = _default_power_setter()
            return setter(self.iface, mbm)

    def _write_trigger(self, payload):
        """One trigger write. Returns the gate outcome, or UNKNOWN_OUTCOME.

        A failed write does not establish that nothing was transmitted -- the command may have
        reached the radio and been carried out before the failure was reported -- so it is never
        recorded as "did not fire".
        """
        fd = None
        try:
            fd = os.open(self._node, os.O_WRONLY)
            os.lseek(fd, 0, os.SEEK_SET)
            n = os.write(fd, payload)
        except OSError as e:
            self.wedged = "trigger write failed: %s" % e
            return UNKNOWN_OUTCOME
        finally:
            if fd is not None:
                os.close(fd)
        if n != len(payload):
            self.wedged = "trigger write was truncated (%d of %d bytes)" % (n, len(payload))
            return UNKNOWN_OUTCOME
        text = self._kmsg.read_new()
        found = _status_re().findall(text)
        if not found:
            return UNKNOWN_OUTCOME
        return int(found[-1])

    # -------------------------------------------------------------- firing

    def on_fire(self, msg):
        """Fire the commanded batch and return the status message for it."""
        proto.validate_fire(msg)
        mine = (msg.get("aps") or {}).get(self.name)
        if mine is None:
            return None

        batch = msg["batch"]
        # Scoped by run as well as batch. Batch numbers start again with each run, so keying on
        # the number alone makes a fresh run's first rounds collide with a previous run's, and
        # the agent answers from memory without transmitting anything -- which reads downstream
        # as a total loss on a working link.
        key = (msg["run"], batch)
        if key in self._batches:
            # Already carried out. Report what is known rather than transmitting again: a
            # repeated instruction must not double the shots actually put on air.
            done = self._batches[key]
            if done is None:
                return proto.build_status(msg["run"], batch, self.name, self.epoch, {},
                                          error="IN_PROGRESS")
            return done
        self._batches[key] = None
        self._current_key = key
        self._forget_old_batches()

        if self.wedged:
            result = proto.build_status(msg["run"], batch, self.name, self.epoch, {},
                                        error="WEDGED", extra={"detail": self.wedged})
            self._batches[key] = result
            return result

        try:
            result = self._fire_batch(msg, mine)
        except Exception as e:
            result = proto.build_status(msg["run"], batch, self.name, self.epoch, {},
                                        error="%s: %s" % (type(e).__name__, e))
        self._batches[key] = result
        return result

    BATCH_MEMORY = 256

    def _forget_old_batches(self):
        """Bound the record of completed rounds.

        It exists only to absorb a repeated instruction, which arrives within seconds of the
        original, so a long history serves no purpose and would grow without limit across a run.
        """
        self._batch_order.append(self._current_key)
        while len(self._batch_order) > self.BATCH_MEMORY:
            self._batches.pop(self._batch_order.pop(0), None)

    def _fire_batch(self, msg, mine):
        power_error = None
        try:
            self.set_power(mine.get("txpower_mbm"))
        except Exception as e:
            power_error = str(e)

        base = int(mine["target"])
        spacing = int(msg["spacing_us"])
        n = int(msg["n"])
        seq0 = int(msg["seq0"])
        nframes = max(1, int(msg["nframes"]))
        frame_len = int(msg["frame_len"])
        stamp_off = int(msg["stamp_off"])
        rate = int(mine["rate"])
        txp = int(mine["txp"])
        station_id = int(mine["station_id"])

        shots = {}
        skipped = 0
        with self._wmi:
            # A trigger write blocks until its instant arrives, so the return of the first one
            # marks where the schedule actually started. Timing from there rather than from the
            # commanded lead keeps this correct no matter how far ahead the first instant was
            # placed -- transmitters are deliberately offset from one another for some
            # measurements, and that offset is carried in the instant itself.
            t_first = None
            for i in range(n):
                seq = wire.SeqAllocator.wire(seq0 + i)
                # If the loop has fallen a full slot behind, attempting the shot would only
                # produce a late report and consume another slot, so it is skipped instead.
                behind_us = 0.0
                if t_first is not None and spacing > 0:
                    behind_us = (time.monotonic() - t_first) * 1e6 - (i * spacing)
                if spacing > 0 and behind_us > spacing:
                    shots[seq] = SKIPPED
                    skipped += 1
                    continue
                payload = wire.blob(base + i * spacing, rate, txp, nframes, stamp_off,
                                    station_id, seq, frame_len, self.mac)
                outcome = self._write_trigger(payload)
                if t_first is None:
                    t_first = time.monotonic()
                shots[seq] = outcome
                if outcome == FIRED:
                    self.shots_fired += 1
                if self.wedged:
                    break

        extra = {"skipped": skipped, "overruns": self._kmsg.overruns}
        if power_error:
            extra["power_error"] = power_error
        error = "WEDGED" if self.wedged else None
        return proto.build_status(msg["run"], msg["batch"], self.name, self.epoch,
                                  shots, error=error, extra=extra)

    # -------------------------------------------------------------- telemetry

    def beacon_rows(self):
        """Peer beacon observations currently held by the driver.

        The ring has no per-reader cursor: every read returns the newest entries it holds, so
        consecutive reads overlap. Discarding what has already been forwarded is the caller's
        job, and `_beacon_loop` does it. A plain memory read, unlike the clock, so it can be
        polled often enough to keep up with the ring without interfering with anything.
        """
        if not self._beacons:
            return []
        try:
            with open(self._beacons) as fh:
                text = fh.read()
        except (OSError, IOError):
            return []
        rows = []
        for line in text.splitlines():
            f = line.split()
            if len(f) >= 5 and ":" in f[0] and all(f[i].isdigit() for i in (1, 2, 3, 4)):
                row = {"hearer": self.mac, "sender": f[0].lower(), "t1": int(f[1]),
                       "t2": int(f[2]), "rate_idx": int(f[3]), "len": int(f[4])}
                if len(f) >= 6:
                    try:
                        row["rssi"] = int(f[5])
                    except ValueError:
                        pass
                rows.append(row)
        return rows

    def reset(self):
        """Discard the record of completed rounds and clear a previously unusable radio.

        The unusable flag is cleared deliberately: recovering the card is a physical act the
        agent cannot observe, so the only way it can learn that the radio works again is to be
        told that the deployment has been brought up afresh.
        """
        self._batches = {}
        self._batch_order = []
        was = self.wedged
        self.wedged = None
        self.shots_fired = 0
        return {"cleared": "rounds", "was_unusable": was}

    def status(self):
        return {"role": "ap", "name": self.name, "iface": self.iface, "mac": self.mac,
                "epoch": self.epoch,
                "uptime_s": round(time.time() - self.started_at, 1),
                "node": self._node, "beacons": self._beacons,
                "shots_fired": self.shots_fired,
                "kmsg_overruns": self._kmsg.overruns,
                "wedged": self.wedged,
                "source_hash": source_hash()}


def _glob_one(pattern):
    import glob
    hits = glob.glob(pattern)
    return hits[0] if hits else None


def _default_power_setter():
    """Prefer the direct interface; fall back to the command-line tool where it is unavailable.

    Re-asserting an unchanged value costs almost nothing -- the driver short-circuits it -- so
    the power is set every round rather than remembered. A remembered value goes stale the
    moment the interface is brought down or the access point restarts.
    """
    try:
        from . import nl80211
        setter = nl80211.make_power_setter()
        if setter is not None:
            return setter
    except Exception:
        pass
    return _iw_set_power


def _iw_set_power(iface, mbm):
    """Assert transmit power through the wireless tool. Any message on the error stream is
    treated as failure: the value reaching the radio is not otherwise observable."""
    import subprocess
    p = subprocess.Popen(["/sbin/iw", "dev", iface, "set", "txpower", "fixed", str(int(mbm))],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _out, err = p.communicate()
    if p.returncode != 0 or err.strip():
        raise RuntimeError("could not set transmit power to %s: %s"
                           % (mbm, err.decode("utf-8", "replace").strip() or
                              "exit %d" % p.returncode))
    return int(mbm)


# --------------------------------------------------------------------------- wiring

class Agent(object):
    """Connects a role implementation to the message bus."""

    def __init__(self, role, name, hub, token=None, impl=None, beacon_hz=5.0):
        self.role = role
        self.name = name
        self.hub = hub
        self.token = token
        self.impl = impl
        self.beacon_hz = beacon_hz
        self.client = None
        self._stop = threading.Event()
        self._beacon_thread = None

    def _reply_subject(self):
        if self.role == "ap":
            return proto.subj_status(self.name)
        return proto.subj_report(self.name)

    def connect(self, retries=30):
        self.client = natsc.connect(self.hub, natsc.DEFAULT_PORT, token=self.token,
                                    name="cosr-%s-%s" % (self.role, self.name),
                                    retries=retries)
        self.client.subscribe_json(proto.SUBJ_FIRE, self._on_fire)
        self.client.subscribe_json(proto.subj_rpc(self.role, self.name), self._on_rpc)
        self.client.publish_json(proto.SUBJ_HELLO, proto.build_hello(
            self.role, self.name, self.impl.epoch, source_hash(),
            "%d.%d.%d" % sys.version_info[:3]))
        # Started once and left running across reconnects: it reads `self.client` each time
        # round, so it picks up a replacement connection on its own.
        if self.beacon_hz > 0 and self._beacon_thread is None:
            self._beacon_thread = threading.Thread(target=self._beacon_loop)
            self._beacon_thread.daemon = True
            self._beacon_thread.start()
        return self

    def _beacon_loop(self):
        """Forward peer beacon observations so the controller can relate the clocks.

        Polled faster than the driver's ring can wrap; otherwise observations are lost and the
        span of each fit silently shortens, which degrades the extrapolation the fire path
        depends on.
        """
        interval = 1.0 / self.beacon_hz
        seen = set()
        while not self._stop.is_set():
            try:
                rows = self.impl.beacon_rows()
            except Exception:
                rows = []
            fresh = []
            for r in rows:
                key = (r["sender"], r.get("t2", r.get("station_time")))
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(r)
            if len(seen) > 20000:
                seen = set((r["sender"], r.get("t2", r.get("station_time"))) for r in rows)
            if fresh:
                try:
                    self.client.publish_json(proto.SUBJ_BEACON,
                                             {"v": proto.VERSION, "node": self.name,
                                              "rows": fresh})
                except natsc.NatsError:
                    # A publish can fail while the connection is being re-established. Missing
                    # some observations costs a little fit span and is recovered from; giving up
                    # would disable the clock plane for good while the agent went on answering
                    # everything else, so nothing would look wrong.
                    self._forget(seen, fresh)
            self._stop.wait(interval)

    @staticmethod
    def _forget(seen, rows):
        """Un-mark rows that were not delivered, so the next attempt sends them again."""
        for r in rows:
            seen.discard((r["sender"], r.get("t2", r.get("station_time"))))

    def _on_fire(self, _subject, msg, _reply):
        subject = self._reply_subject()
        try:
            out = self.impl.on_fire(msg)
        except proto.ProtocolError as e:
            # Carry the round's identity even though the message failed validation: without it
            # the controller cannot match this to the round it is waiting for, and reports a
            # diagnosed protocol fault as a node that never answered.
            self.client.publish_json(subject, self._error_reply(msg, str(e)))
            return
        except Exception as e:
            self.client.publish_json(subject,
                                     self._error_reply(msg, "%s: %s" % (type(e).__name__, e)))
            return
        if out is not None:
            self.client.publish_json(subject, out)

    def _error_reply(self, msg, text):
        """A failure report the controller can actually accept.

        It has to satisfy the same validation as a normal reply and carry the round's identity,
        or it is discarded on arrival and the round is reported as a node that never answered --
        sending the operator after connectivity when the fault was diagnosed and named here.
        """
        msg = msg or {}
        run, batch = msg.get("run"), msg.get("batch")
        if self.role == "ap":
            out = proto.build_status(run, batch, self.name, self.impl.epoch, {})
        else:
            out = proto.build_report(run, batch, self.name,
                                     {"epoch": self.impl.epoch, "ordinal": 0,
                                      "other_frames": 0, "other_frames_round": 0,
                                      "drops": 0, "freq_mhz": None, "per_ap": {}})
        out["error"] = text
        return out

    def _on_rpc(self, _subject, msg, reply):
        if not reply:
            return
        op = (msg or {}).get("op")
        try:
            if op == "status":
                body = {"ok": True, "status": self.impl.status()}
            elif op == "ping":
                body = {"ok": True, "epoch": self.impl.epoch}
            elif op == "reset":
                body = {"ok": True, "reset": self.impl.reset()}
            else:
                body = {"ok": False, "error": "unknown operation %r" % (op,)}
        except Exception as e:
            body = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        self.client.publish_json(reply, body)

    def run_forever(self, retry_s=2.0):
        """Serve until stopped, re-dialling whenever the connection goes away.

        Exiting on a dropped connection would mean that restarting the broker silently
        decommissions every node in the deployment, recoverable only by deploying again to each
        of them. The identity of this process does not change across a reconnect, so nothing a
        controller has already recorded about it is invalidated by one.
        """
        while not self._stop.is_set():
            if self.client is not None and self.client.is_connected():
                time.sleep(0.5)
                continue
            try:
                self.connect(retries=1)
            except natsc.NatsError:
                time.sleep(retry_s)
        self._stop.set()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    def opt(flag, default=None):
        if flag in argv:
            i = argv.index(flag)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default

    role = opt("--role")
    name = opt("--name")
    iface = opt("--iface")
    hub = opt("--hub")
    token = opt("--token")
    station_id = opt("--station-id")
    ap_macs = opt("--ap-macs", "")
    mac = opt("--mac")
    if not (role and name and iface and hub):
        sys.exit("usage: agent.py --role station|ap --name N --iface I --hub HOST "
                 "[--token T] [--station-id N] [--ap-macs a,b,c] [--mac M]")

    if role == "station":
        macs = [m for m in ap_macs.split(",") if m]
        impl = StationAgent(name, iface, int(station_id or 0), macs).open()
        impl.start()
    elif role == "ap":
        if not mac:
            sys.exit("--mac is required for a transmitter")
        impl = ApAgent(name, iface, mac)
        if not impl._node:
            sys.exit("no transmit trigger available on %s; is the interface running as an "
                     "access point with the coordinated-transmit firmware?" % iface)
    else:
        sys.exit("unknown role %r" % role)

    agent = Agent(role, name, hub, token, impl).connect()
    sys.stdout.write("agent %s/%s up: epoch=%s source=%s\n"
                     % (role, name, impl.epoch, source_hash()))
    sys.stdout.flush()
    try:
        agent.run_forever()
    finally:
        if hasattr(impl, "stop"):
            impl.stop()


if __name__ == "__main__":
    main()
