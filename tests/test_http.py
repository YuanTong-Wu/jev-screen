"""Tests for the polite http.Client. No network: the urllib opener is replaced by a fake."""
from __future__ import annotations

import contextlib
import io
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import http as jhttp  # noqa: E402
from jevscreen.http import Blocked, Client, RequestTimeout  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OKUZAN = (FIXTURES / "tv_profile_TSE-9901.html").read_text(encoding="utf-8")
URL = "https://www.tradingview.com/symbols/TSE-9901/"


class _Body(io.BytesIO):
    """BytesIO that records the sizes passed to read()."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.sizes: list[int | None] = []

    def read(self, n: int | None = -1) -> bytes:  # type: ignore[override]
        self.sizes.append(n)
        return super().read(n)


class _Resp:
    def __init__(self, status: int, body: bytes, headers: dict):
        self.status, self.headers, self._b = status, headers, _Body(body)

    def read(self, n: int | None = -1) -> bytes:
        return self._b.read(n)


class Opener:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status, self.body, self.headers, self.calls, self.last_fp = status, body, headers or {}, 0, None

    def open(self, req, timeout=None):
        self.calls += 1
        if self.status >= 400:
            self.last_fp = _Body(self.body)
            raise urllib.error.HTTPError(req.full_url, self.status, "err", self.headers, self.last_fp)
        return _Resp(self.status, self.body, self.headers)


def client(opener: Opener, **kw) -> Client:
    c = Client(user_agent="test", min_interval_s=0.0, **kw)
    c._opener = opener
    return c


class ChallengeDetection(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("jevscreen.http.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_recaptcha_script_and_captcha_word_are_not_challenges(self):
        page = OKUZAN.replace("<head>", '<head><script src="https://www.google.com/recaptcha/api.js"></script>', 1)
        page = page.replace("Okuzan Motor Corporation", "CAPTCHA-protected Okuzan Motor Corporation", 1)
        self.assertIn("recaptcha", page)
        r = client(Opener(200, page.encode(), {"Content-Type": "text/html"})).get(URL)
        self.assertEqual(r.status, 200)

    def test_cloudflare_title_is_challenge(self):
        body = (FIXTURES / "profile_challenge.html").read_bytes()
        with self.assertRaises(Blocked) as cm:
            client(Opener(200, body)).get(URL)
        self.assertEqual(cm.exception.reason, "challenge_marker")

    def test_cf_mitigated_header_on_200(self):
        with self.assertRaises(Blocked) as cm:
            client(Opener(200, b"{}", {"Content-Type": "application/json", "cf-mitigated": "challenge"})).get(URL)
        self.assertEqual(cm.exception.reason, "cf_mitigated_challenge")

    def test_503_challenge_is_blocked_without_retry(self):
        body = b" " * 5000 + b"<html><head><title>Just a moment...</title></head></html>"
        op = Opener(503, body, {"CF-Mitigated": "challenge", "Content-Type": "text/html"})
        with self.assertRaises(Blocked):
            client(op).get(URL)
        self.assertEqual(op.calls, 1)

    def test_cloudflare_503_html_not_retried(self):
        op = Opener(503, b"<html>busy</html>", {"Server": "cloudflare", "Content-Type": "text/html"})
        with self.assertRaises(Blocked) as cm:
            client(op).get(URL)
        self.assertEqual((cm.exception.reason, op.calls), ("cloudflare_503", 1))

    def test_plain_503_is_retried(self):
        op = Opener(503, b'{"error":"busy"}', {"Content-Type": "application/json"})
        self.assertEqual(client(op).get(URL).status, 503)
        self.assertEqual(op.calls, 3)

    def test_json_mentioning_captcha_is_not_a_challenge(self):
        body = b'{"data":[{"s":"X:Y","d":["Captcha Corp just a moment"]}]}'
        r = client(Opener(200, body, {"Content-Type": "application/json"})).post_json(URL, {})
        self.assertEqual(r.status, 200)


class Caps(unittest.TestCase):
    def test_truncated_flag(self):
        r = client(Opener(200, b"x" * 101)).get(URL, max_bytes=100)
        self.assertEqual((len(r.body), r.truncated), (100, True))
        r = client(Opener(200, b"x" * 100)).get(URL, max_bytes=100)
        self.assertEqual((len(r.body), r.truncated), (100, False))

    def test_error_body_read_is_bounded(self):
        op = Opener(404, b"x" * 5_000_000)
        r = client(op).get(URL)
        self.assertEqual(r.status, 404)
        self.assertTrue(all(n is not None and 0 < n <= 64 * 1024 + 1 for n in op.last_fp.sizes))
        self.assertTrue(r.truncated)

    def test_per_request_cap_does_not_change_client(self):
        c = client(Opener(200, b"x" * 10))
        c.get(URL, max_bytes=5)
        self.assertIsNone(c.max_bytes)


# =========================================================================== total wall-clock deadline


class _Trickle:
    """A streamed 200 body that never ends: every read sleeps `gap` s and returns `size` bytes. read1 (like a real
    socket) returns after one 'system call'. Records reads and close()."""

    def __init__(self, gap: float = 0.02, size: int = 1, headers: dict | None = None, first_delay: float = 0.0):
        self.status, self.headers = 200, headers or {"Content-Type": "application/octet-stream"}
        self.gap, self.size, self.first_delay = gap, size, first_delay
        self.reads, self.closed = 0, False

    def read1(self, n: int = -1) -> bytes:
        time.sleep(self.first_delay if self.reads == 0 and self.first_delay else self.gap)
        self.reads += 1
        return b"x" * min(self.size, n if n and n > 0 else self.size)

    read = read1

    def close(self) -> None:
        self.closed = True


class _PlainTrickle(_Trickle):
    """Same, but only read(n) (no read1 on the class): the get_until loop path of test fakes."""
    read1 = None  # type: ignore[assignment]

    def read(self, n: int = -1) -> bytes:  # type: ignore[override]
        return _Trickle.read1(self, n)


class TrickleOpener:
    def __init__(self, make, open_delay: float = 0.0):
        self.make, self.open_delay, self.calls, self.responses = make, open_delay, 0, []

    def open(self, req, timeout=None):
        self.calls += 1
        self.timeout = timeout
        if self.open_delay:
            time.sleep(self.open_delay)
        r = self.make()
        self.responses.append(r)
        return r


class DeadlineWithFakes(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(Client, "_backoff")       # no retry sleeps (time.sleep itself stays real)
        self.backoff = patcher.start()
        self.addCleanup(patcher.stop)

    def test_trickling_body_hits_deadline_without_retry(self):
        op = TrickleOpener(lambda: _Trickle(gap=0.02))
        c = client(op, deadline_s=0.3)
        t = time.monotonic()
        with self.assertRaises(RequestTimeout) as cm:
            c.get(URL)
        took = time.monotonic() - t
        e = cm.exception
        self.assertIsInstance(e, TimeoutError)
        self.assertNotIsInstance(e, Blocked)
        self.assertGreater(e.received, 0)
        self.assertEqual((e.status, e.deadline_s), (200, 0.3))
        self.assertEqual(op.calls, 1)                            # partial data: never retried
        self.assertTrue(op.responses[0].closed)                   # connection closed
        self.assertLess(took, 1.5)
        self.assertNotIn(URL, str(e))                             # URLs may carry keys: not in the message
        self.assertFalse(c.halt.is_set())                         # not a block

    def test_nothing_received_is_retried_once(self):
        op = TrickleOpener(lambda: _Trickle(gap=0.02), open_delay=0.25)   # headers late: deadline gone at once
        c = client(op, deadline_s=0.2)
        with self.assertRaises(RequestTimeout) as cm:
            c.get(URL)
        self.assertEqual(cm.exception.received, 0)
        self.assertEqual(op.calls, 2)                             # one retry only (max_retries is 2)
        self.assertEqual(self.backoff.call_count, 1)
        self.assertTrue(all(r.closed for r in op.responses))
        self.assertLessEqual(op.timeout, 0.2)                     # open's socket timeout lowered to the deadline

    def test_nothing_received_retry_then_success(self):
        state = {"n": 0}

        def make():
            state["n"] += 1
            if state["n"] == 1:
                time.sleep(0.25)                                  # first attempt: headers after the deadline
                return _Trickle()
            return _Resp(200, b"ok", {})
        op = TrickleOpener(make)
        r = client(op, deadline_s=0.2).get(URL)
        self.assertEqual((r.status, r.body, op.calls), (200, b"ok", 2))

    def test_post_is_never_retried(self):
        op = TrickleOpener(lambda: _Trickle(), open_delay=0.25)
        with self.assertRaises(RequestTimeout):
            client(op, deadline_s=0.2).post_json(URL, {})
        self.assertEqual(op.calls, 1)

    def test_per_call_override_and_no_deadline(self):
        op = TrickleOpener(lambda: _Trickle(gap=0.02))
        c = client(op)                                            # default 120 s
        self.assertEqual(c.deadline_s, jhttp.DEFAULT_DEADLINE_S)
        with self.assertRaises(RequestTimeout) as cm:
            c.get(URL, deadline_s=0.2)
        self.assertEqual(cm.exception.deadline_s, 0.2)
        # a finite trickle completes when no deadline applies
        body = b"y" * 20
        op2 = TrickleOpener(lambda: _FiniteTrickle(body))
        r = client(op2, deadline_s=None).get(URL)
        self.assertEqual((r.body, r.truncated), (body, False))

    def test_get_until_respects_deadline(self):
        for cls in (_Trickle, _PlainTrickle):
            with self.subTest(cls=cls.__name__):
                op = TrickleOpener(lambda: cls(gap=0.02, size=10))
                c = client(op, deadline_s=0.3)
                with self.assertRaises(RequestTimeout) as cm:
                    c.get_until(URL, lambda buf: False, max_bytes=10_000_000)
                self.assertGreater(cm.exception.received, 0)
                self.assertEqual(op.calls, 1)
                self.assertTrue(op.responses[0].closed)

    def test_get_until_nothing_received_retried_once(self):
        op = TrickleOpener(lambda: _Trickle(), open_delay=0.25)
        with self.assertRaises(RequestTimeout):
            client(op, deadline_s=0.2).get_until(URL, lambda buf: False)
        self.assertEqual(op.calls, 2)

    def test_get_until_stop_before_deadline_is_fine(self):
        op = TrickleOpener(lambda: _Trickle(gap=0.01, size=10))
        r = client(op, deadline_s=5).get_until(URL, lambda buf: len(buf) >= 30)
        self.assertTrue(r.stopped_early)
        self.assertEqual(len(r.body), 30)

    def test_blocked_still_wins(self):
        op = Opener(429, b"")
        with self.assertRaises(Blocked):
            client(op, deadline_s=0.1).get(URL)


class _FiniteTrickle(_Trickle):
    def __init__(self, body: bytes):
        super().__init__(gap=0.005)
        self._left = body

    def read1(self, n: int = -1) -> bytes:
        time.sleep(self.gap)
        out, self._left = self._left[:1], self._left[1:]
        return out

    read = read1


class _LocalServer:
    """A local HTTP/1.1 server on 127.0.0.1 (no outside network). mode: 'full' (whole body), 'stall' (headers +
    `prefix` bytes, then silence), 'trickle' (headers + one byte every `gap` s)."""

    def __init__(self, mode: str, prefix: bytes = b"", length: int = 1_000_000, gap: float = 0.05,
                 body: bytes = b"", status: str = "200 OK", ctype: str = "application/zip"):
        self.mode, self.prefix, self.length, self.gap, self.body = mode, prefix, length, gap, body
        self.status, self.ctype = status, ctype
        self.stop = threading.Event()
        self.connections = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/doc.zip"

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            self.conns.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            if self.mode == "raw":                          # no status line at all, just the page
                conn.sendall(self.body)
                return
            length = len(self.body) if self.mode == "full" else self.length
            conn.sendall(b"HTTP/1.1 " + self.status.encode() + b"\r\nContent-Type: " + self.ctype.encode() + b"\r\n"
                         b"Content-Length: " + str(length).encode() + b"\r\nConnection: close\r\n\r\n")
            if self.mode == "full":
                conn.sendall(self.body)
                return
            if self.prefix:
                conn.sendall(self.prefix)
            if self.mode == "stall":
                self.stop.wait(30)
                return
            while not self.stop.is_set():
                conn.sendall(b"z")
                time.sleep(self.gap)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def close(self) -> None:
        self.stop.set()
        for c in self.conns:
            with contextlib.suppress(OSError):
                c.close()
        self.sock.close()
        self.thread.join(2)


def real_client(timeout_s: float = 30.0, **kw) -> Client:
    c = Client(user_agent="test", min_interval_s=0.0, timeout_s=timeout_s, **kw)
    c._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), jhttp._NoRedirect())
    return c


class RawRefusalWithLocalSocket(unittest.TestCase):
    """A refusal page sent without an HTTP status line (seen live on doc.twse.com.tw) is a block, not a network
    error: the first one halts the client, with no retry."""

    def serve(self, *a, **kw) -> _LocalServer:
        srv = _LocalServer(*a, **kw)
        self.addCleanup(srv.close)
        return srv

    def test_raw_forbidden_page_is_blocked_without_retry(self):
        page = (b"<HTML><HEAD><TITLE> Forbidden</TITLE></HEAD><BODY> <H1>Forbidden</H1> "
                b"The requested URL is not allowed. </BODY></HTML>\r\n")
        srv = self.serve("raw", body=page)
        client = real_client(max_retries=2)
        with self.assertRaises(Blocked) as cm:
            client.get(srv.url)
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(srv.connections, 1)
        self.assertTrue(client.halt.is_set())

    def test_other_raw_garbage_is_not_a_block(self):
        srv = self.serve("raw", body=b"garbage without a status line\r\n")
        with self.assertRaises(Exception) as cm:
            real_client(max_retries=0).get(srv.url)
        self.assertNotIsInstance(cm.exception, Blocked)


class DeadlineWithLocalSocket(unittest.TestCase):
    """Real sockets: the per-operation timeout (30 s here) alone would never end these downloads."""

    def setUp(self) -> None:
        patcher = mock.patch.object(Client, "_backoff")
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, *a, **kw) -> _LocalServer:
        srv = _LocalServer(*a, **kw)
        self.addCleanup(srv.close)
        return srv

    def test_stall_after_partial_body(self):
        srv = self.serve("stall", prefix=b"PK\x03\x04" + b"a" * 1000)
        t = time.monotonic()
        with self.assertRaises(RequestTimeout) as cm:
            real_client().get(srv.url, deadline_s=0.6)
        took = time.monotonic() - t
        self.assertEqual(cm.exception.received, 1004)
        self.assertEqual(cm.exception.status, 200)
        self.assertLess(took, 3.0)                              # socket timeout lowered to the time left
        self.assertGreaterEqual(took, 0.55)
        self.assertEqual(srv.connections, 1)                     # partial data: no retry

    def test_stall_before_any_body_byte_is_retried_once(self):
        srv = self.serve("stall")
        with self.assertRaises(RequestTimeout) as cm:
            real_client().get(srv.url, deadline_s=0.4)
        self.assertEqual(cm.exception.received, 0)
        time.sleep(0.1)
        self.assertEqual(srv.connections, 2)

    def test_trickle(self):
        srv = self.serve("trickle", gap=0.03)
        t = time.monotonic()
        with self.assertRaises(RequestTimeout) as cm:
            real_client().get(srv.url, deadline_s=0.6)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertGreater(cm.exception.received, 0)
        self.assertEqual(srv.connections, 1)

    def test_get_until_trickle(self):
        srv = self.serve("trickle", gap=0.03)
        with self.assertRaises(RequestTimeout):
            real_client().get_until(srv.url, lambda buf: False, deadline_s=0.5)
        self.assertEqual(srv.connections, 1)

    def test_fast_body_is_read_whole_and_capped(self):
        body = bytes(range(256)) * 2000                           # 512 000 bytes: several chunks
        srv = self.serve("full", body=body)
        r = real_client().get(srv.url, deadline_s=5)
        self.assertEqual((r.status, r.body, r.truncated), (200, body, False))
        srv2 = self.serve("full", body=body)
        r = real_client().get(srv2.url, deadline_s=5, max_bytes=100_000)
        self.assertEqual((len(r.body), r.truncated), (100_000, True))


    # ---- a stalled 403/429 body: Blocked at once (one connection, halt set), never a timeout or a retry

    def test_stalled_403_429_body_is_blocked_at_once(self):
        for status in ("403 Forbidden", "429 Too Many Requests"):
            for deadline_s, timeout_s in ((1.0, 5.0), (5.0, 0.5)):
                with self.subTest(status=status, deadline_s=deadline_s, timeout_s=timeout_s):
                    srv = self.serve("stall", prefix=b"<html>", status=status, ctype="text/html")
                    c = real_client(timeout_s=timeout_s)
                    t = time.monotonic()
                    with self.assertRaises(Blocked) as cm:
                        c.get(srv.url, deadline_s=deadline_s)
                    self.assertLess(time.monotonic() - t, 0.5)          # the body is never read
                    self.assertEqual(cm.exception.status, int(status[:3]))
                    self.assertTrue(c.halt.is_set())
                    time.sleep(0.1)
                    self.assertEqual(srv.connections, 1)
                    with self.assertRaises(Blocked):
                        real_client(timeout_s=timeout_s).get_until(srv.url, lambda buf: False,
                                                                    deadline_s=deadline_s)

    # ---- the socket timeout (not the deadline) is the binding limit: same no-retry rule

    def test_socket_stall_after_partial_body_is_not_retried(self):
        srv = self.serve("stall", prefix=b"a" * 100)
        c = real_client(timeout_s=0.5)
        with self.assertRaises(RequestTimeout) as cm:
            c.get(srv.url, deadline_s=5)
        e = cm.exception
        self.assertEqual((e.kind, e.received, e.status, e.timeout_s), ("stall", 100, 200, 0.5))
        self.assertTrue(str(e).startswith("stall:"))
        self.assertNotIn(srv.url, str(e))
        time.sleep(0.1)
        self.assertEqual(srv.connections, 1)
        self.assertFalse(c.halt.is_set())

    def test_socket_stall_on_post_is_not_retried(self):
        srv = self.serve("stall")
        with self.assertRaises(RequestTimeout) as cm:
            real_client(timeout_s=0.5).post_json(srv.url, {"q": 1}, deadline_s=5)
        self.assertEqual((cm.exception.kind, cm.exception.received), ("stall", 0))
        time.sleep(0.1)
        self.assertEqual(srv.connections, 1)

    def test_socket_stall_before_any_body_byte_get_retried_once(self):
        srv = self.serve("stall")
        with self.assertRaises(RequestTimeout) as cm:
            real_client(timeout_s=0.3).get(srv.url, deadline_s=5)
        self.assertEqual((cm.exception.kind, cm.exception.received), ("stall", 0))
        time.sleep(0.1)
        self.assertEqual(srv.connections, 2)

    def test_socket_stall_without_deadline(self):
        srv = self.serve("stall", prefix=b"a" * 10)
        with self.assertRaises(RequestTimeout) as cm:
            real_client(timeout_s=0.3).get(srv.url, deadline_s=0)       # <= 0: no deadline, stall still ends it
        self.assertEqual((cm.exception.kind, cm.exception.received), ("stall", 10))
        time.sleep(0.1)
        self.assertEqual(srv.connections, 1)


if __name__ == "__main__":
    unittest.main()
