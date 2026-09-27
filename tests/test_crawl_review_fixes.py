"""Regression tests for the crawl-descriptions review fixes (no network anywhere: fake openers / fake clients only).

- --rate nan is rejected, --rate inf is clamped; apply_rate is NaN-safe
- a block always ends with the BLOCKED line and exit 2, even when the final flush or the journal fails afterwards
- a complete FinancialProduct without a usable description ends the stream and is the terminal 'no_description'
- incremental detection survives long split start tags and quoted '>' in attributes
- the probe byte after the cap never turns a readable page into an error
- a 3xx to a challenge path is a block
- crawl() takes the www.tradingview.com budget lock itself (reentrant for the CLI)
- SIGTERM during the final flush of a normal run is ignored; ordinary exceptions are 'error', not 'interrupted'
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import signal
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_cli_coverage as tcc  # noqa: E402
import test_crawl_stream_rules as tsr  # noqa: E402
import test_tradingview_profiles as tp  # noqa: E402
from jevscreen import cli, guard, store  # noqa: E402
from jevscreen.http import Blocked, Client  # noqa: E402
from jevscreen.sources import tradingview_profiles as tvp  # noqa: E402

KB = 1024


def empty_description_block() -> bytes:
    obj = {"@context": "https://schema.org", "@type": "FinancialProduct", "name": "Toyota", "tickerSymbol": "7203",
           "description": ""}
    return b'<script type="application/ld+json">' + json.dumps(obj).encode() + b"</script>"


class Rate(unittest.TestCase):
    def test_nan_rejected_inf_clamped(self):
        with self.assertRaises(ValueError):
            tvp.clamp_rate(float("nan"))
        self.assertEqual(tvp.clamp_rate(float("inf")), 4.4)
        with self.assertRaises(ValueError), contextlib.redirect_stderr(io.StringIO()):
            cli.clamp_rate(float("nan"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.clamp_rate(float("inf")), 4.4)
        self.assertIn("clamped to 4.4", err.getvalue())

    def test_apply_rate_and_crawl_client_nan_safe(self):
        c = tp.FakeClient()
        c.min_interval_s = float("nan")
        self.assertAlmostEqual(tvp.apply_rate(c, None), 4.4)
        self.assertAlmostEqual(c.min_interval_s, 1 / 4.4)
        c.min_interval_s = float("inf")
        tvp.apply_rate(c, None)
        self.assertAlmostEqual(c.min_interval_s, 1 / 4.4)
        cfg = mock.Mock(user_agent="test", timeout_s=5.0)
        for bad in (float("nan"), 0.0, -1.0, float("inf"), 99.0):
            self.assertAlmostEqual(cli.make_crawl_client(cfg, bad).min_interval_s, 1 / 4.4)


class CliRate(tcc._CliBase):
    def test_rate_nan_and_inf(self):
        seen = []
        crawl = lambda cfg, client, **kw: seen.append(client.min_interval_s) or {"ok": 0}  # noqa: E731
        with mock.patch.dict(sys.modules, {cli.PROFILES: tcc._fake_module(cli.PROFILES, crawl=crawl)}):
            code, _, err = self.run_cli("crawl-descriptions", "--rate", "nan")
            self.assertEqual(code, 1)
            self.assertIn("--rate", err)
            self.assertEqual(seen, [])                                   # no client, no request
            code, _, err = self.run_cli("crawl-descriptions", "--rate", "inf")
            self.assertEqual(code, 0)
            self.assertIn("clamped to 4.4", err)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 1 / 4.4)
        self.assertFalse(math.isnan(seen[0]))


class BlockedAlwaysReported(tcc._CliBase):
    cfg = tsr.CliCrawlRules.cfg
    populate = tsr.CliCrawlRules.populate
    runs = tsr.CliCrawlRules.runs

    def test_store_locked_final_flush_after_429_still_blocked_exit_2(self):
        real = store.session
        fake_ref: dict = {}

        def session(cfg, *a, wait_s=600.0, **k):
            fake = fake_ref.get("fake")
            if wait_s == tvp.FINAL_WAIT_S and fake is not None and tvp.profile_url("A:TWO") in fake.calls:
                raise store.StoreLocked("Could not set lock on file")
            return real(cfg, *a, wait_s=wait_s, **k)

        url = tvp.profile_url("A:TWO")
        self.populate(["A:ONE", "A:TWO", "A:THREE"])
        fake = tp.FakeClient({tvp.profile_url("A:ONE"): tp.page_for("ONE"), url: Blocked(url, 429, "http_429")})
        fake_ref["fake"] = fake
        before = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(cli, "make_crawl_client", return_value=fake), \
                mock.patch.object(store, "session", session):
            code, out, err = self.run_cli("crawl-descriptions", "--workers", "1")
        self.assertEqual(code, 2)
        self.assertEqual(out.strip().splitlines()[-1], f"BLOCKED url={url} status=429 reason=http_429")
        self.assertEqual(sum(ln.startswith("BLOCKED") for ln in out.splitlines()), 1)
        self.assertIn("final write after the block failed: StoreLocked", err)
        self.assertEqual(fake.calls, [tvp.profile_url("A:ONE"), url])
        self.assertEqual(self.runs()[-1][1], "blocked")                  # the CLI closed the 'running' row
        self.assertIsNotNone(guard.recent_block_marker(self.cfg(), "crawl-descriptions"))
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)       # handler restored after the run

    def test_ctrl_c_in_journal_after_403_still_prints_blocked(self):
        url = tvp.profile_url("A:TWO")
        self.populate(["A:ONE", "A:TWO"])
        fake = tp.FakeClient({tvp.profile_url("A:ONE"): tp.page_for("ONE"), url: Blocked(url, 403, "http_403")})
        with mock.patch.object(cli, "make_crawl_client", return_value=fake), \
                mock.patch.object(cli, "_journal_detached", side_effect=KeyboardInterrupt):
            code, out, _ = self.run_cli("crawl-descriptions")
        self.assertEqual(code, 2)
        self.assertEqual(out.strip().splitlines()[-1], f"BLOCKED url={url} status=403 reason=http_403")

    def test_adapter_error_after_block_still_blocked(self):
        def crawl(cfg, client, on_blocked=None, **kw):
            on_blocked(Blocked("https://www.tradingview.com/symbols/A-X/", 429, "http_429"))
            raise RuntimeError("duckdb went away")
        with mock.patch.dict(sys.modules, {cli.PROFILES: tcc._fake_module(cli.PROFILES, crawl=crawl)}):
            code, out, _ = self.run_cli("crawl-descriptions")
        self.assertEqual(code, 2)
        self.assertEqual(out.strip().splitlines()[-1],
                         "BLOCKED url=https://www.tradingview.com/symbols/A-X/ status=429 reason=http_429")


class AdapterFixes(tp.Base):
    def real_client(self, script: dict) -> Client:
        c = Client(user_agent="test", min_interval_s=0.0)
        c._opener = tp.FakeOpener(script)
        return c

    def test_final_flush_failure_after_block_returns_summary(self):
        self.add("A:ONE", 2)
        self.add("A:TWO", 1)
        url = tvp.profile_url("A:TWO")
        client = tp.FakeClient({tvp.profile_url("A:ONE"): tp.page_for("ONE"), url: Blocked(url, 429, "http_429")})
        real, n = store.session, [0]

        def session(cfg, *a, wait_s=600.0, **k):
            n[0] += 1
            if n[0] > 1:
                raise store.StoreLocked("Could not set lock on file")
            return real(cfg, *a, wait_s=wait_s, **k)

        seen = []
        with mock.patch.object(store, "session", session), contextlib.redirect_stderr(io.StringIO()):
            s = tvp.crawl(self.cfg, client, progress_every=0, on_blocked=seen.append)
        self.assertEqual((s["status"], s["blocked_url"], s["blocked_status"]), ("blocked", url, 429))
        self.assertIn("StoreLocked", s["flush_error"])
        self.assertEqual([(e.url, e.status) for e in seen], [(url, 429)])   # told before the final write
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, guard.CMD_CRAWL_DESCRIPTIONS))

    def test_empty_description_on_big_page_is_terminal_and_stops_early(self):
        self.add("TSE:7203", 1)
        page = tsr.synthetic_page(block=empty_description_block(), after=400 * KB)
        client = self.real_client({tvp.profile_url("TSE:7203"): (200, page)})
        with mock.patch("jevscreen.http.time.sleep"):
            s = tvp.crawl(self.cfg, client, progress_every=0)
        self.assertEqual((s["no_description"], s["no_product_within_cap"], s["early_stops"], s["bytes_read"]),
                         (1, 0, 1, 96 * KB))
        self.assertEqual(self.states()["TSE:7203"][0], "no_description")
        self.assertEqual(tvp.queue(self.con), [])                          # terminal: not re-fetched

    def test_empty_description_via_plain_get_is_no_description(self):
        self.add("A:E", 1)
        client = tp.FakeClient({tvp.profile_url("A:E"): tp.page_for("E", "")})
        tvp.crawl(self.cfg, client, progress_every=0)
        self.assertEqual(self.states()["A:E"][0], "no_description")

    def test_cap_probe_error_keeps_the_page(self):
        class Flaky(tp._FakeHTTPResponse):
            def read(self, n=None):
                if self.served >= tvp.MAX_BYTES:
                    raise ConnectionResetError("reset during probe")
                return super().read(n)

        url = tvp.profile_url("A:BIG")
        c = Client(user_agent="test", min_interval_s=0.0)
        c._opener = mock.Mock(open=lambda req, timeout=None: Flaky(200, b"<html>" + b"z" * (300 * KB)))
        r = c.get_until(url, tvp.ProductDetector())
        self.assertEqual((len(r.body), r.truncated), (256 * KB, True))

    def test_challenge_redirect_is_blocked(self):
        url = tvp.profile_url("A:ONE")
        loc = {"Location": "https://www.tradingview.com/cdn-cgi/challenge-platform/h/b/orchestrate"}
        c = self.real_client({url: (302, b"", loc)})
        with self.assertRaises(Blocked) as cm:
            c.get_until(url, tvp.ProductDetector())
        self.assertEqual((cm.exception.status, cm.exception.reason), (302, "challenge_redirect"))

        class Err302:
            calls = 0

            def open(self, req, timeout=None):
                Err302.calls += 1
                raise urllib.error.HTTPError(req.full_url, 302, "Found", loc, io.BytesIO(b""))

        c = Client(user_agent="test", min_interval_s=0.0)
        c._opener = Err302()
        with self.assertRaises(Blocked):
            c.get(url)
        self.assertTrue(c.halt.is_set())                                    # the client halted itself
        c.halt.clear()                                                      # reuse the client for the next check
        with self.assertRaises(Blocked):
            c.get_until(url, tvp.ProductDetector())
        self.assertEqual(Err302.calls, 2)                                   # never retried
        plain = self.real_client({url: (301, b"", {"Location": "https://www.tradingview.com/symbols/A-ONE2/"})})
        self.assertEqual(plain.get_until(url, tvp.ProductDetector()).status, 301)   # ordinary redirect stays soft

    def test_crawl_takes_budget_lock_itself(self):
        self.add("A:ONE", 1)
        client = tp.FakeClient({tvp.profile_url("A:ONE"): tp.page_for("ONE")})
        path = Path(self.cfg.home) / "locks" / "www.tradingview.com.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
                                   "print('locked', flush=True); sys.stdin.read()", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaises(guard.Busy):
                tvp.crawl(self.cfg, client, progress_every=0)
            self.assertEqual(client.calls, [])                              # no request next to the other crawler
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        with guard.budget_lock(self.cfg, "www.tradingview.com"):            # the CLI's own (non-reentrant) hold
            s = tvp.crawl(self.cfg, client, progress_every=0)               # re-entered, not Busy
            with self.assertRaises(guard.Busy):
                with guard.budget_lock(self.cfg, "www.tradingview.com"):
                    pass
        self.assertEqual(s["ok"], 1)

    def test_sigterm_during_final_flush_is_ignored(self):
        for i in range(3):
            self.add(f"A:G{i}", 10 - i)
        real, n = store.session, [0]

        @contextlib.contextmanager
        def session(cfg, *a, **k):
            n[0] += 1
            with real(cfg, *a, **k) as con:
                if n[0] == 2:                                              # the final flush
                    os.kill(os.getpid(), signal.SIGTERM)
                    for _ in range(100_000):                                # let the signal be handled here
                        pass
                yield con

        client = tp.FakeClient({tvp.profile_url(f"A:G{i}"): tp.page_for(f"G{i}") for i in range(3)})
        with cli.sigterm_as_interrupt(), mock.patch.object(store, "session", session):
            s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=50)
        self.assertEqual((s["status"], s["ok"]), ("ok", 3))
        self.assertEqual(len(self.states()), 3)                            # the batch was not rolled back

    def test_ordinary_exception_is_error_not_interrupted(self):
        self.add("A:V", 1)
        client = tp.FakeClient({tvp.profile_url("A:V"): ValueError("bad parse")})
        with self.assertRaises(ValueError):
            tvp.crawl(self.cfg, client, progress_every=0)
        self.assertEqual(self.run_row()[0], "error")


class Detector(unittest.TestCase):
    def feed(self, page: bytes, chunk: int) -> tvp.ProductDetector:
        det = tvp.ProductDetector()
        for end in range(chunk, len(page) + chunk, chunk):
            if det(page[:end]):
                break
        return det

    def test_long_start_tag_split_at_16_kb_boundary(self):
        block = tsr.PRODUCT_BLOCK.replace(b"<script ", b'<script nonce="' + b"n" * 400 + b'" ', 1)
        head = b"<html><script>var pad='"
        pad = 16 * KB - len(head) - len(b"';</script>") - 300               # the start tag straddles 16 KB
        assert len(head) + pad + len(b"';</script>") + block.index(b">") + 1 > 16 * KB
        page = head + b"x" * pad + b"';</script>" + block + b"<div>" + b"y" * (300 * KB) + b"</div>"
        det = self.feed(page, 16 * KB)
        self.assertIsNotNone(det.product)
        for chunk in (7, 100):
            self.assertIsNotNone(self.feed(page[:len(page) - 300 * KB], chunk).product)

    def test_quoted_gt_in_attribute_and_type_parameters(self):
        base = tsr.PRODUCT_BLOCK
        variants = [base.replace(b"<script ", b'<script data-x="a>b" ', 1),
                    base.replace(b'type="application/ld+json"', b'type="application/ld+json; charset=utf-8"', 1)]
        for page in variants:
            with self.subTest(page=page[:80]):
                self.assertIsNotNone(self.feed(page, 7).product)
                self.assertIsNotNone(tvp.find_financial_product(page.decode()))   # parser agrees with detector
        data_type = base.replace(b'type="application/ld+json"', b'data-type="application/ld+json"', 1)
        self.assertIsNone(self.feed(data_type, 7).product)
        self.assertIsNone(tvp.find_financial_product(data_type.decode()))


if __name__ == "__main__":
    unittest.main()
