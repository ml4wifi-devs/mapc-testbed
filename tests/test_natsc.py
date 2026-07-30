#!/usr/bin/env python3
"""cosr.natsc against a real nats-server on loopback.

Driving a genuine server rather than a mock is the point: the protocol details that break in
practice are payload framing across TCP segment boundaries, replies arriving after a deadline,
and connection loss mid-wait. A mock would reproduce my own assumptions instead of the server's
behaviour. The whole module is skipped when no server binary is present.
"""
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cosr import natsc

SERVER = shutil.which("nats-server") or shutil.which("gnatsd")
TOKEN = "cosr-test-token"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(SERVER, "nats-server not installed")
class NatsCase(unittest.TestCase):
    """One server for the whole class; each test gets fresh clients."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [SERVER, "-a", "127.0.0.1", "-p", str(cls.port), "--auth", TOKEN],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", cls.port), 0.2)
                s.close()
                break
            except (OSError, socket.error):
                time.sleep(0.05)
        else:
            cls.proc.terminate()
            raise AssertionError("nats-server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def setUp(self):
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()

    def client(self, token=TOKEN, name="t"):
        c = natsc.connect("127.0.0.1", self.port, token=token, name=name)
        self.clients.append(c)
        return c


class TestConnect(NatsCase):

    def test_connects_and_pings(self):
        c = self.client()
        self.assertTrue(c.is_connected())
        self.assertTrue(c.ping(timeout=2.0))

    def test_wrong_token_is_rejected_at_connect(self):
        """Authentication must fail immediately, not on the first publish that matters."""
        with self.assertRaises(natsc.NatsError):
            natsc.connect("127.0.0.1", self.port, token="wrong")

    def test_no_server_raises(self):
        with self.assertRaises(natsc.NatsError):
            natsc.connect("127.0.0.1", free_port(), token=TOKEN)

    def test_close_is_idempotent(self):
        c = self.client()
        c.close()
        c.close()
        self.assertFalse(c.is_connected())

    def test_publish_after_close_raises(self):
        c = self.client()
        c.close()
        self.assertRaises(natsc.NatsClosed, c.publish, "x", b"y")


class TestPubSub(NatsCase):

    def _collector(self):
        got = []
        ev = threading.Event()

        def cb(subject, payload, reply):
            got.append((subject, payload, reply))
            ev.set()
        return got, ev, cb

    def test_publish_and_receive(self):
        sub, pub = self.client(name="sub"), self.client(name="pub")
        got, ev, cb = self._collector()
        sub.subscribe("cosr.test", cb)
        self.assertTrue(sub.ping(timeout=2.0))          # ensure SUB is registered
        pub.publish("cosr.test", b"hello")
        self.assertTrue(ev.wait(2.0))
        self.assertEqual(got[0][0], "cosr.test")
        self.assertEqual(got[0][1], b"hello")

    def test_json_round_trip(self):
        sub, pub = self.client(name="sub"), self.client(name="pub")
        got = []
        ev = threading.Event()

        def cb(subject, obj, reply):
            got.append(obj)
            ev.set()
        sub.subscribe_json("cosr.j", cb)
        sub.ping(timeout=2.0)
        pub.publish_json("cosr.j", {"a": 1, "b": [1, 2, 3]})
        self.assertTrue(ev.wait(2.0))
        self.assertEqual(got[0], {"a": 1, "b": [1, 2, 3]})

    def test_wildcard_fanout_reaches_every_subscriber(self):
        """One publish serving all participants is what keeps a round to a single message."""
        subs = [self.client(name="s%d" % i) for i in range(3)]
        events = []
        for s in subs:
            got, ev, cb = self._collector()
            s.subscribe("cosr.fire", cb)
            s.ping(timeout=2.0)
            events.append((got, ev))
        self.client(name="pub").publish("cosr.fire", b"go")
        for got, ev in events:
            self.assertTrue(ev.wait(2.0))
            self.assertEqual(got[0][1], b"go")

    def test_payload_with_crlf_inside(self):
        """Payloads are counted, not delimited, so protocol bytes inside them are safe."""
        sub, pub = self.client(name="sub"), self.client(name="pub")
        got, ev, cb = self._collector()
        sub.subscribe("cosr.bin", cb)
        sub.ping(timeout=2.0)
        payload = b"MSG x 1 5\r\nPING\r\n\x00\xff"
        pub.publish("cosr.bin", payload)
        self.assertTrue(ev.wait(2.0))
        self.assertEqual(got[0][1], payload)

    def test_large_payload_spans_segments(self):
        sub, pub = self.client(name="sub"), self.client(name="pub")
        got, ev, cb = self._collector()
        sub.subscribe("cosr.big", cb)
        sub.ping(timeout=2.0)
        payload = os.urandom(200000)
        pub.publish("cosr.big", payload)
        self.assertTrue(ev.wait(5.0))
        self.assertEqual(got[0][1], payload)

    def test_many_messages_keep_order_and_count(self):
        sub, pub = self.client(name="sub"), self.client(name="pub")
        got = []
        done = threading.Event()

        def cb(subject, payload, reply):
            got.append(int(payload))
            if len(got) == 500:
                done.set()
        sub.subscribe("cosr.seq", cb)
        sub.ping(timeout=2.0)
        for i in range(500):
            pub.publish("cosr.seq", str(i).encode())
        self.assertTrue(done.wait(10.0), "received %d of 500" % len(got))
        self.assertEqual(got, list(range(500)))

    def test_unsubscribe_stops_delivery(self):
        sub, pub = self.client(name="sub"), self.client(name="pub")
        got, ev, cb = self._collector()
        sid = sub.subscribe("cosr.un", cb)
        sub.ping(timeout=2.0)
        sub.unsubscribe(sid)
        sub.ping(timeout=2.0)
        pub.publish("cosr.un", b"x")
        self.assertFalse(ev.wait(0.5))

    def test_a_failing_callback_does_not_kill_the_reader(self):
        sub, pub = self.client(name="sub"), self.client(name="pub")
        ok, ev, cb = self._collector()

        def boom(subject, payload, reply):
            raise ValueError("deliberate")
        sub.subscribe("cosr.boom", boom)
        sub.subscribe("cosr.fine", cb)
        sub.ping(timeout=2.0)
        pub.publish("cosr.boom", b"x")
        pub.publish("cosr.fine", b"y")
        self.assertTrue(ev.wait(2.0), "the reader stopped after a subscriber raised")


class TestRequestReply(NatsCase):

    def _responder(self, client, subject, handler):
        def cb(subj, payload, reply):
            if reply:
                client.publish(reply, handler(payload))
        client.subscribe(subject, cb)
        client.ping(timeout=2.0)

    def test_request_returns_the_reply(self):
        srv, cli = self.client(name="srv"), self.client(name="cli")
        self._responder(srv, "cosr.echo", lambda p: b"re:" + p)
        self.assertEqual(cli.request("cosr.echo", b"hi", timeout=2.0), b"re:hi")

    def test_request_json(self):
        srv, cli = self.client(name="srv"), self.client(name="cli")
        import json

        def handler(payload):
            obj = json.loads(payload.decode())
            return json.dumps({"got": obj["n"] * 2}).encode()
        self._responder(srv, "cosr.dbl", handler)
        self.assertEqual(cli.request_json("cosr.dbl", {"n": 21}, timeout=2.0), {"got": 42})

    def test_timeout_when_nobody_answers(self):
        cli = self.client(name="cli")
        t0 = time.time()
        self.assertRaises(natsc.NatsTimeout, cli.request, "cosr.void", b"x", 0.3)
        self.assertLess(time.time() - t0, 2.0)

    def test_late_reply_is_discarded_not_mispaired(self):
        """A reply arriving after its deadline must not be handed to the next request."""
        srv, cli = self.client(name="srv"), self.client(name="cli")

        def slow(subj, payload, reply):
            time.sleep(0.6)
            if reply:
                srv.publish(reply, b"late:" + payload)
        srv.subscribe("cosr.slow", slow)
        srv.ping(timeout=2.0)

        self.assertRaises(natsc.NatsTimeout, cli.request, "cosr.slow", b"first", 0.2)

        self._responder(srv, "cosr.fast", lambda p: b"fast:" + p)
        self.assertEqual(cli.request("cosr.fast", b"second", timeout=2.0), b"fast:second")
        time.sleep(0.6)

    def test_concurrent_requests_get_their_own_replies(self):
        srv, cli = self.client(name="srv"), self.client(name="cli")
        self._responder(srv, "cosr.tag", lambda p: b"r-" + p)

        results = {}

        def ask(n):
            results[n] = cli.request("cosr.tag", str(n).encode(), timeout=5.0)
        threads = [threading.Thread(target=ask, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 10)
        for n, r in results.items():
            self.assertEqual(r, b"r-" + str(n).encode())

    def test_request_raises_when_the_connection_drops(self):
        cli = self.client(name="cli")
        result = {}

        def ask():
            try:
                cli.request("cosr.never", b"x", timeout=5.0)
            except natsc.NatsError as e:
                result["err"] = type(e).__name__
        t = threading.Thread(target=ask)
        t.start()
        time.sleep(0.3)
        cli.close()
        t.join(5.0)
        self.assertIn("err", result, "a dropped connection must not hang the caller")

    def test_two_clients_do_not_cross_replies(self):
        """Inbox prefixes are per client, so replies cannot be delivered to the wrong one."""
        srv = self.client(name="srv")
        a, b = self.client(name="a"), self.client(name="b")
        self._responder(srv, "cosr.who", lambda p: b"for:" + p)
        self.assertEqual(a.request("cosr.who", b"a", timeout=2.0), b"for:a")
        self.assertEqual(b.request("cosr.who", b"b", timeout=2.0), b"for:b")
        self.assertNotEqual(a._inbox_prefix, b._inbox_prefix)

class TestDisconnectIsObservable(unittest.TestCase):
    """Nothing can re-dial a connection whose loss is never recorded."""

    def test_a_dropped_connection_stops_reporting_as_connected(self):
        """`is_connected` is what a reconnect loop waits on. If the reader never clears the
        socket it answers yes for ever, and the loop waits on a condition that cannot occur."""
        port = free_port()
        proc = subprocess.Popen(
            [SERVER, "-a", "127.0.0.1", "-p", str(port), "--auth", TOKEN],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except (OSError, socket.error):
                time.sleep(0.05)
        try:
            c = natsc.connect("127.0.0.1", port, token=TOKEN, name="drop")
            self.assertTrue(c.is_connected())
            proc.terminate()
            proc.wait()
            for _ in range(100):
                if not c.is_connected():
                    break
                time.sleep(0.05)
            self.assertFalse(c.is_connected(),
                             "the broker is gone; this must not still report connected")
        finally:
            proc.poll() is None and proc.terminate()


if __name__ == "__main__":
    unittest.main(verbosity=2)

