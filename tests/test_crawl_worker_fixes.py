"""Regression tests for the worker-pool review fixes (no network anywhere: fake openers / fake clients only).

- http.Client sets halt in the same step that observes a block (before e.close(), before raising Blocked)
- a retry cut short by the halt returns / re-raises the attempt that was already sent (never Halted); backoff ends
- the window limiter: at most max_per_window starts in any rolling window_s, whatever the number of threads
- the cooldown marker is written by the worker that saw the 429, even while the main thread is stuck in a mid-run
  flush waiting for the store lock
- an interrupt right after a result was collected cannot make the drain spin: the crawl still ends, flushed
- crawl() refuses to start within the cooldown (no request) unless after_block=True
- a client that does not declare thread_safe gets one worker
- the CLI crawl client: 4 starts per rolling second, timeout <= 15 s
"""
from __future__ import annotations

import contextlib
import io
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_crawl_workers as tw  # noqa: E402
import test_tradingview_profiles as tp  # noqa: E402
from jevscreen import cli, guard, store  # noqa: E402
from jevscreen.http import Blocked, Client, Halted  # noqa: E402
from jevscreen.sources import tradingview_profiles as tvp  # noqa: E402

URL = "https://www.tradingview.com/symbols/A-ONE/"


class _SlowCloseHTTPError(urllib.error.HTTPError):
    """HTTPError whose close() takes a while and records whether the client's halt was already set by then."""

    def __init__(self, client: Client, status: int):
        super().__init__(URL, status, "err", {}, io.BytesIO(b"slow down"))
        self.client, self.halt_seen_in_close = client, None

    def close(self):  # noqa: D401
        if self.halt_seen_in_close is None:
            self.halt_seen_in_close = self.client.halt.is_set()
        super().close()


class ClientHalt(unittest.TestCase):
    def test_halt_is_set_when_the_block_is_observed(self):
        for method in ("get_until", "get"):
            for status in (429, 403):
                with self.subTest(method=method, status=status):
                    c = Client(user_agent="test", min_interval_s=0.0)
                    err = _SlowCloseHTTPError(c, status)
                    c._opener = mock.Mock(open=mock.Mock(side_effect=err))
                    call = (lambda: c.get_until(URL, tvp.ProductDetector())) if method == "get_until" \
                        else (lambda: c.get(URL))
                    with self.assertRaises(Blocked):
                        call()
                    self.assertTrue(c.halt.is_set())
                    if method == "get_until":                 # get_until closes the error before raising
                        self.assertIs(err.halt_seen_in_close, True)
        # a challenge detected in the streamed body and a challenge header also halt the client
        for script in ({URL: (200, tp.CHALLENGE_HTML.encode())},
                       {URL: (200, b"<html>", {"cf-mitigated": "challenge"})}):
            c = Client(user_agent="test", min_interval_s=0.0)
            c._opener = tp.FakeOpener(script)
            with self.assertRaises(Blocked):
                c.get_until(URL, tvp.ProductDetector())
            self.assertTrue(c.halt.is_set())
            with self.assertRaises(Halted):                   # and nothing more is sent
                c.get_until(URL, tvp.ProductDetector())
            self.assertEqual(len(c._opener.calls), 1)

    def test_halted_retry_returns_the_sent_attempt(self):
        c = Client(user_agent="test", min_interval_s=0.0)
        c._opener = tp.FakeOpener({URL: (503, b'{"e":1}', {"Content-Type": "application/json"})})
        slept = []

        def sleep(s):                                         # the halt arrives during the first backoff
            slept.append(s)
            c.halt.set()
        with mock.patch("jevscreen.http.time.sleep", side_effect=sleep):
            r = c.get_until(URL, tvp.ProductDetector())
        self.assertEqual((r.status, len(c._opener.calls), c.requests_made), (503, 1, 1))   # not Halted, no retry
        self.assertEqual(slept, [0.25])                       # the backoff ended at the first slice
        c.halt.clear()
        with mock.patch("jevscreen.http.time.sleep", side_effect=sleep):
            self.assertEqual(c.get(URL).status, 503)
        # a network error on the sent attempt is re-raised (the caller records 'error'), never Halted
        c = Client(user_agent="test", min_interval_s=0.0)
        c._opener = mock.Mock(open=mock.Mock(side_effect=urllib.error.URLError("reset")))
        with mock.patch("jevscreen.http.time.sleep", side_effect=lambda s: c.halt.set()):
            with self.assertRaises(urllib.error.URLError):
                c.get_until(URL, tvp.ProductDetector())
        self.assertEqual(c._opener.open.call_count, 1)
        # nothing sent at all: Halted
        with self.assertRaises(Halted):
            c.get(URL)

    def test_window_limiter_caps_starts_per_rolling_window(self):
        c = Client(user_agent="test", min_interval_s=0.02, max_per_window=4, window_s=0.4)
        slots, lock = [], threading.Lock()

        def caller():
            for _ in range(4):
                t = c._wait("h")
                with lock:
                    slots.append(t)
        threads = [threading.Thread(target=caller) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(slots), 16)
        self.assertLessEqual(tw.max_in_window(slots, 0.4 - 1e-6), 4)
        self.assertGreaterEqual(min(tw.gaps(slots)), 0.02 - 1e-6)

    def test_crawl_client_and_apply_rate(self):
        cfg = mock.Mock(user_agent="test", timeout_s=60.0)
        c = cli.make_crawl_client(cfg)
        self.assertEqual((c.max_per_window, c.window_s, c.timeout_s), (4, 1.0, 15.0))
        self.assertEqual(cli.make_crawl_client(mock.Mock(user_agent="t", timeout_s=5.0)).timeout_s, 5.0)
        plain = Client(user_agent="test", min_interval_s=0.0)
        tvp.apply_rate(plain, None)
        self.assertEqual((plain.max_per_window, plain.window_s), (4, 1.0))
        slower = Client(user_agent="test", min_interval_s=0.0, max_per_window=2, window_s=0.1)
        tvp.apply_rate(slower, None)
        self.assertEqual((slower.max_per_window, slower.window_s), (2, 1.0))   # lower cap kept, window never shorter


class CrawlFixes(tp.Base):
    def ids(self, n: int, prefix: str = "W") -> list[str]:
        out = [f"A:{prefix}{i}" for i in range(n)]
        for i, sid in enumerate(out):
            self.add(sid, 100 - i)
        return out

    def test_marker_written_by_worker_during_stalled_flush(self):
        ids = self.ids(6)
        blocked = tvp.profile_url(ids[1])
        client, opener, _ = tw.real_client({blocked: (429, b"slow down")}, latency=(0.3, 0.3))
        stall = {"n": 0, "marker_at": None, "stall_end": None}
        seen = []

        @contextlib.contextmanager
        def open_session(wait_s):
            if wait_s == tvp.FLUSH_WAIT_S and stall["n"] == 0:
                stall["n"] += 1
                # the first mid-run flush waits "for the store lock" for 2 s; the 429 on W1 lands meanwhile
                end = time.monotonic() + 2.0
                while time.monotonic() < end:
                    if stall["marker_at"] is None and \
                            guard.recent_block_marker(self.cfg, guard.CMD_CRAWL_DESCRIPTIONS) is not None:
                        stall["marker_at"] = time.monotonic()
                    time.sleep(0.02)
                stall["stall_end"] = time.monotonic()
                self.assertEqual(seen, [])                      # the main thread has not handled it yet
            with store.session(self.cfg, wait_s=wait_s) as con:
                yield con

        with contextlib.redirect_stderr(io.StringIO()):
            s = tvp._crawl(self.cfg, client, open_session, limit=None, only_missing=True,
                           max_consecutive_errors=3, progress_every=0, flush_every=1, workers=3,
                           on_blocked=seen.append)
        self.assertIsNotNone(stall["marker_at"])                # on disk while the main thread was still stalled
        self.assertLess(stall["marker_at"], stall["stall_end"])
        self.assertLess(stall["marker_at"], opener.raised_at[blocked] + 0.5)
        self.assertEqual((s["status"], s["blocked_url"]), ("blocked", blocked))
        self.assertEqual([e.url for e in seen], [blocked])
        self.assertEqual(sum(h for _, _, h in opener.starts), 0)

    def test_interrupt_after_collecting_a_result_does_not_hang_the_drain(self):
        ids = self.ids(6, "K")
        client = tw.SlowFakeClient({tvp.profile_url(x): tp.page_for(x.split(":")[1]) for x in ids}, 0.2)
        real_wait = tvp.futures_wait
        fired = []

        def wait(fs, timeout=None, return_when=None):
            done, not_done = real_wait(fs, timeout=timeout, return_when=return_when)
            if done and not fired:
                fired.append(1)
                raise KeyboardInterrupt           # after the result is collected, before handle() records it
            return done, not_done

        box: dict = {}

        def run():
            try:
                box["summary"] = tvp.crawl(self.cfg, client, progress_every=0, flush_every=50, workers=3)
            except BaseException as e:  # noqa: BLE001
                box["error"] = e
        with mock.patch.object(tvp, "futures_wait", wait):
            t = threading.Thread(target=run)
            t.start()
            t.join(15)
        self.assertFalse(t.is_alive(), "the drain never finished")
        self.assertIsInstance(box.get("error"), KeyboardInterrupt)
        st = self.states()
        self.assertEqual(sorted(tvp.profile_url(x) for x in st), sorted(client.calls))   # every sent page recorded
        self.assertEqual(self.run_row()[0], "interrupted")

    def test_halted_retry_is_recorded_as_error(self):
        ids = self.ids(6, "R")
        script = {tvp.profile_url(ids[0]): (503, b"busy"), tvp.profile_url(ids[1]): (429, b"slow down")}
        client, opener, _ = tw.real_client(script, latency=(0.3, 0.3))
        with contextlib.redirect_stderr(io.StringIO()):
            t0 = time.monotonic()
            s = tvp.crawl(self.cfg, client, progress_every=0, workers=3)
            elapsed = time.monotonic() - t0
        st = self.states()
        self.assertEqual(st[ids[0]][:2], ("error", 503))      # sent once, its retry cut short by the halt
        self.assertEqual(st[ids[1]][:2], ("blocked", 429))
        self.assertEqual(s["requests"], len(opener.starts))
        self.assertEqual(s["attempted"], len(st))
        self.assertEqual(opener.urls().count(tvp.profile_url(ids[0])), 1)
        self.assertLess(elapsed, 1.9)                         # the 2 s backoff ended at the halt

    def test_cooldown_refused_without_any_request(self):
        self.ids(3, "C")
        guard.mark_blocked(self.cfg, guard.CMD_CRAWL_DESCRIPTIONS, url=URL, status=429, reason="http_429")
        client = tw.SlowFakeClient({}, 0.0)
        client.halt.set()
        with self.assertRaises(tvp.CooldownActive):
            tvp.crawl(self.cfg, client, progress_every=0)
        with self.assertRaises(tvp.CooldownActive):
            tvp.crawl_with_connection(self.cfg, self.con, client, progress_every=0)
        self.assertEqual(client.calls, [])
        self.assertTrue(client.halt.is_set())                 # the in-process record of the block is kept
        s = tvp.crawl(self.cfg, client, progress_every=0, after_block=True)
        self.assertEqual((s["attempted"], len(client.calls)), (3, 3))

    def test_client_without_thread_safe_gets_one_worker(self):
        ids = self.ids(3, "F")
        client = tp.FakeClient({tvp.profile_url(x): tp.page_for(x.split(":")[1]) for x in ids})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = tvp.crawl(self.cfg, client, progress_every=0, workers=3)
        self.assertEqual((s["workers"], s["ok"]), (1, 3))
        self.assertIn("using 1 worker", err.getvalue())
        self.assertTrue(Client.thread_safe)


if __name__ == "__main__":
    unittest.main()
