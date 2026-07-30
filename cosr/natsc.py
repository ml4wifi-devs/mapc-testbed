"""A minimal NATS client: publish, subscribe, and request/reply over one TCP connection.

The wire protocol is line-oriented text with counted payloads, so a complete client fits in a
few hundred lines of standard library. That matters here because the transmitter images carry an
old Python and no package manager, which rules out an installed client library.

Delivery semantics are the ones NATS actually provides: a publish is fire-and-forget. Anything
that must not be applied twice carries its own identifier and is made idempotent by the
receiver; anything that must be confirmed uses `request`, which either returns a reply or raises
on its deadline. There is no silent middle ground.

One reader thread owns the socket and dispatches to subscription callbacks and to waiting
requests. Publishing is safe from any thread.
"""
import os
import socket
import threading
import time

CRLF = b"\r\n"
DEFAULT_PORT = 4222
CONNECT_TIMEOUT = 5.0
REQUEST_TIMEOUT = 5.0
MAX_CONTROL_LINE = 4096
MAX_PAYLOAD = 1 << 20


class NatsError(Exception):
    pass


class NatsTimeout(NatsError):
    """A request produced no reply before its deadline."""


class NatsClosed(NatsError):
    """The connection is not usable."""


def _json():
    import json
    return json


class Client(object):

    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, token=None,
                 name="cosr", connect_timeout=CONNECT_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.token = token
        self.name = name
        self.connect_timeout = connect_timeout

        self._sock = None
        self._buf = b""
        self._send_lock = threading.Lock()
        self._reader = None
        self._closing = False

        self._sid = 0
        self._subs = {}            # sid -> (subject, callback)
        self._pending = {}         # inbox suffix -> [Event, message or None]
        # Unique per client instance, so two clients on one host cannot receive each other's
        # replies even while sharing a subject namespace.
        self._inbox_prefix = "_INBOX." + _hexlify(os.urandom(8))
        self._inbox_n = 0
        self._pong = threading.Event()
        self._last_error = None
        self.connected_at = None

    # ------------------------------------------------------------------ connect

    def connect(self):
        try:
            return self._connect()
        except Exception:
            # A rejected or half-finished handshake must not leave the socket open: agents
            # reconnect in a loop, so a leak here costs a descriptor per attempt.
            self.close()
            raise

    def _connect(self):
        s = socket.create_connection((self.host, self.port), self.connect_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(self.connect_timeout)
        self._sock = s
        self._buf = b""
        self._closing = False

        info = self._read_control_line()
        if not info.startswith("INFO "):
            raise NatsError("expected INFO from the server, got %r" % info[:64])

        opts = {"verbose": False, "pedantic": False, "tls_required": False,
                "name": self.name, "lang": "python", "version": "cosr"}
        if self.token:
            opts["auth_token"] = self.token
        self._send_raw(b"CONNECT " + _json().dumps(opts).encode("utf-8") + CRLF)

        # PING/PONG round trip: confirms the server accepted the connection instead of
        # discovering an auth rejection later, on the first publish that matters.
        self._send_raw(b"PING" + CRLF)
        line = self._read_control_line()
        while line.startswith("+OK") or line.startswith("INFO "):
            line = self._read_control_line()
        if line.startswith("-ERR"):
            raise NatsError("server rejected the connection: %s" % line)
        if not line.startswith("PONG"):
            raise NatsError("expected PONG, got %r" % line[:64])

        s.settimeout(0.5)
        self.connected_at = time.time()
        self._subscribe_raw(self._inbox_prefix + ".*", self._on_inbox)
        self._reader = threading.Thread(target=self._read_loop)
        self._reader.daemon = True
        self._reader.start()
        return self

    def close(self):
        self._closing = True
        s = self._sock
        self._sock = None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except (OSError, socket.error):
                pass
            try:
                s.close()
            except (OSError, socket.error):
                pass
        for entry in list(self._pending.values()):
            entry[0].set()

    def is_connected(self):
        return self._sock is not None and not self._closing

    # ------------------------------------------------------------------ io

    def _send_raw(self, data):
        with self._send_lock:
            s = self._sock
            if s is None:
                raise NatsClosed("connection is closed")
            try:
                s.sendall(data)
            except (OSError, socket.error) as e:
                self._last_error = str(e)
                raise NatsClosed("send failed: %s" % e)

    def _recv_some(self):
        s = self._sock
        if s is None:
            raise NatsClosed("connection is closed")
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            return False
        except (OSError, socket.error) as e:
            raise NatsClosed("recv failed: %s" % e)
        if not chunk:
            raise NatsClosed("server closed the connection")
        self._buf += chunk
        return True

    def _read_control_line(self):
        """Blocking read of one CRLF-terminated control line, used only during connect."""
        while True:
            i = self._buf.find(CRLF)
            if i >= 0:
                line = self._buf[:i]
                self._buf = self._buf[i + 2:]
                return line.decode("utf-8", "replace")
            if len(self._buf) > MAX_CONTROL_LINE:
                raise NatsError("control line exceeded %d bytes" % MAX_CONTROL_LINE)
            self._recv_some()

    def _read_loop(self):
        while not self._closing:
            try:
                progressed = self._recv_some()
            except NatsClosed as e:
                self._last_error = str(e)
                break
            if progressed:
                try:
                    self._drain()
                except NatsError as e:
                    self._last_error = str(e)
                    break
        # The reader is the only place a dropped connection is noticed. Leaving the socket in
        # place would leave `is_connected` answering yes for ever, so anything waiting to
        # re-dial waits for a condition that can no longer occur.
        self._drop_socket()
        for entry in list(self._pending.values()):
            entry[0].set()

    def _drop_socket(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except (OSError, socket.error):
                pass

    def _drain(self):
        """Consume every complete protocol message currently buffered."""
        while True:
            i = self._buf.find(CRLF)
            if i < 0:
                if len(self._buf) > MAX_CONTROL_LINE + MAX_PAYLOAD:
                    raise NatsError("buffer overflow with no complete message")
                return
            line = self._buf[:i].decode("utf-8", "replace")
            rest_at = i + 2

            if line.startswith("MSG "):
                parts = line.split()
                if len(parts) not in (4, 5):
                    raise NatsError("malformed MSG: %r" % line[:64])
                subject, sid = parts[1], parts[2]
                reply = parts[3] if len(parts) == 5 else None
                try:
                    nbytes = int(parts[-1])
                except ValueError:
                    raise NatsError("malformed MSG length: %r" % line[:64])
                if nbytes > MAX_PAYLOAD:
                    raise NatsError("payload of %d bytes exceeds the limit" % nbytes)
                end = rest_at + nbytes
                if len(self._buf) < end + 2:
                    return                      # payload still arriving
                payload = self._buf[rest_at:end]
                self._buf = self._buf[end + 2:]
                self._dispatch(subject, sid, reply, payload)
                continue

            self._buf = self._buf[rest_at:]
            if line.startswith("PING"):
                try:
                    self._send_raw(b"PONG" + CRLF)
                except NatsClosed:
                    return
            elif line.startswith("PONG"):
                self._pong.set()
            elif line.startswith("-ERR"):
                self._last_error = line
            # +OK and INFO need no action

    def _dispatch(self, subject, sid, reply, payload):
        entry = self._subs.get(sid)
        if entry is None:
            return
        _subject, callback = entry
        try:
            callback(subject, payload, reply)
        except Exception as e:                  # a subscriber must not kill the reader
            self._last_error = "subscription callback failed: %s" % e

    # ------------------------------------------------------------------ pub/sub

    def publish(self, subject, payload, reply=None):
        if not isinstance(payload, bytes):
            payload = payload.encode("utf-8")
        head = "PUB %s %s %d" % (subject, reply, len(payload)) if reply \
            else "PUB %s %d" % (subject, len(payload))
        self._send_raw(head.encode("utf-8") + CRLF + payload + CRLF)

    def publish_json(self, subject, obj, reply=None):
        self.publish(subject, _json().dumps(obj).encode("utf-8"), reply)

    def _subscribe_raw(self, subject, callback):
        self._sid += 1
        sid = str(self._sid)
        self._subs[sid] = (subject, callback)
        self._send_raw(("SUB %s %s" % (subject, sid)).encode("utf-8") + CRLF)
        return sid

    def subscribe(self, subject, callback):
        """Register `callback(subject, payload_bytes, reply_subject)`."""
        return self._subscribe_raw(subject, callback)

    def subscribe_json(self, subject, callback):
        """As `subscribe`, but the payload is decoded; undecodable messages are skipped."""
        def wrapper(subj, payload, reply):
            try:
                obj = _json().loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return
            callback(subj, obj, reply)
        return self._subscribe_raw(subject, wrapper)

    def unsubscribe(self, sid):
        self._subs.pop(sid, None)
        try:
            self._send_raw(("UNSUB %s" % sid).encode("utf-8") + CRLF)
        except NatsClosed:
            pass

    # ------------------------------------------------------------------ request

    def _on_inbox(self, subject, payload, _reply):
        suffix = subject.rsplit(".", 1)[-1]
        entry = self._pending.get(suffix)
        if entry is None:
            return                              # a reply that arrived after its deadline
        entry[1] = payload
        entry[0].set()

    def request(self, subject, payload, timeout=REQUEST_TIMEOUT):
        """Publish and wait for one reply. Raises NatsTimeout if none arrives in time.

        The pending entry is removed on timeout, so a late reply is discarded rather than
        delivered to whichever request happens to be waiting next.
        """
        self._inbox_n += 1
        suffix = str(self._inbox_n)
        inbox = self._inbox_prefix + "." + suffix
        event = threading.Event()
        entry = [event, None]
        self._pending[suffix] = entry
        try:
            self.publish(subject, payload, reply=inbox)
            if not event.wait(timeout):
                raise NatsTimeout("no reply on %s within %.2fs" % (subject, timeout))
            if entry[1] is None:
                raise NatsClosed("connection lost while awaiting a reply on %s: %s"
                                 % (subject, self._last_error))
            return entry[1]
        finally:
            self._pending.pop(suffix, None)

    def request_json(self, subject, obj, timeout=REQUEST_TIMEOUT):
        raw = self.request(subject, _json().dumps(obj).encode("utf-8"), timeout)
        return _json().loads(raw.decode("utf-8"))

    def ping(self, timeout=REQUEST_TIMEOUT):
        """Round trip to the server. False if it does not answer within the deadline.

        Also serves as a flush: the server processes messages in order, so a PONG proves every
        earlier publish on this connection has been received.
        """
        self._pong.clear()
        try:
            self._send_raw(b"PING" + CRLF)
        except NatsClosed:
            return False
        return self._pong.wait(timeout)


def _hexlify(b):
    return "".join("%02x" % x for x in bytearray(b))


def connect(host="127.0.0.1", port=DEFAULT_PORT, token=None, name="cosr",
            retries=0, backoff=0.5):
    """Connect, optionally retrying. Raises the last error if every attempt fails."""
    last = None
    for attempt in range(retries + 1):
        c = Client(host, port, token, name)
        try:
            return c.connect()
        except (NatsError, OSError, socket.error) as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise NatsError("could not connect to nats://%s:%d: %s" % (host, port, last))
