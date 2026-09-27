"""Streaming crawl rules for crawl-descriptions (no network anywhere: fake openers / fake clients only).

- early stop at the end of the FinancialProduct JSON-LD (synthetic page with the fixture's product block at ~90 KB)
- 256 KB cap without a product -> soft 'no_product_within_cap', not counted toward the consecutive-error stop
- first 429 / 403 / challenge -> whole run stops, BLOCKED line, exit 2, zero further requests, no retry
- rate default and clamp 4.4 req/s (limiter interval ~0.227 s)
- covered-by-SEC set refreshed at every flush: a company covered after the crawl started is skipped
- SIGTERM -> the Ctrl-C path: flush + 'interrupted'
- earlier 'running' rows of the same command are marked 'abandoned' on start
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_cli_coverage as tcc  # noqa: E402
import test_tradingview_profiles as tp  # noqa: E402
from jevscreen import cli, guard, store  # noqa: E402
from jevscreen.http import Blocked, Client, Response  # noqa: E402
from jevscreen.sources import tradingview_profiles as tvp  # noqa: E402

KB = 1024
OKUZAN = tp.OKUZAN_HTML.encode("utf-8")
_start = OKUZAN.index(b'<script type="application/ld+json">')
_end = OKUZAN.index(b"</script>", _start) + len(b"</script>")
PRODUCT_BLOCK = OKUZAN[_start:_end]          # the FinancialProduct JSON-LD script of the (synthetic) fixture
assert b"FinancialProduct" in PRODUCT_BLOCK


def synthetic_page(block_end: int = 90 * KB, after: int = 200 * KB, block: bytes = PRODUCT_BLOCK) -> bytes:
    """HTML whose FinancialProduct script ends exactly at `block_end`, padded before (a big non-JSON-LD script) and
    after (more markup), like the live pages (block ends at 89.1-89.8 KB)."""
    head = b"<!DOCTYPE html><html><head><title>Okuzan</title><script>var pad='"
    mid = b"';</script>"
    pad = block_end - len(head) - len(mid) - len(block)
    assert pad > 0
    page = head + b"x" * pad + mid + block
    assert len(page) == block_end
    return page + b"<div>" + b"y" * after + b"</div></head></html>"


def chunk_end(offset: int, chunk: int = 16 * KB) -> int:
    """Bytes read when the stream stops after the chunk that contains byte `offset - 1`."""
    return -(-offset // chunk) * chunk


class GetUntil(unittest.TestCase):
    def setUp(self) -> None:
        p = mock.patch("jevscreen.http.time.sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def client(self, script: dict) -> tuple[Client, tp.FakeOpener]:
        c = Client(user_agent="test", min_interval_s=0.0)
        c._opener = tp.FakeOpener(script)
        return c, c._opener

    def test_early_stop_right_after_the_product_block(self):
        url = tvp.profile_url("TSE:9901")
        page = synthetic_page()
        c, op = self.client({url: (200, page, {"Content-Type": "text/html"})})
        det = tvp.ProductDetector()
        r = c.get_until(url, det)
        self.assertTrue(r.stopped_early)
        self.assertFalse(r.truncated)
        self.assertEqual(len(r.body), chunk_end(90 * KB))          # 6 chunks of 16 KB = 96 KB, not 290 KB
        self.assertEqual(op.responses[0].served, 96 * KB)          # nothing more was pulled from the socket
        self.assertTrue(op.responses[0].closed)                     # disconnected at once
        self.assertEqual(det.product["tickerSymbol"], "9901")
        # one byte short of the block end: the block is incomplete in 5 chunks, so it must read the 6th
        self.assertFalse(tvp.product_complete(page[:90 * KB - 1]))
        self.assertTrue(tvp.product_complete(page[:90 * KB]))

    def test_block_ending_on_a_chunk_boundary_stops_at_that_chunk(self):
        url = tvp.profile_url("TSE:9901")
        c, op = self.client({url: (200, synthetic_page(block_end=80 * KB))})
        r = c.get_until(url, tvp.ProductDetector())
        self.assertEqual((len(r.body), r.stopped_early, op.responses[0].served), (80 * KB, True, 80 * KB))

    def test_cap_reached_without_product(self):
        url = tvp.profile_url("TSE:9901")
        c, op = self.client({url: (200, b"<html>" + b"z" * (300 * KB))})
        r = c.get_until(url, tvp.ProductDetector())
        self.assertEqual((len(r.body), r.truncated, r.stopped_early), (256 * KB, True, False))
        self.assertEqual(op.responses[0].served, 256 * KB + 1)      # cap + 1 probe byte, never the whole page
        small = self.client({url: (200, b"<html>small</html>")})[0].get_until(url, tvp.ProductDetector())
        self.assertEqual((small.truncated, small.stopped_early), (False, False))

    def test_429_and_403_raise_before_reading_and_never_retry(self):
        url = tvp.profile_url("TSE:9901")
        for status in (429, 403):
            with self.subTest(status=status):
                fp = _CountingBody(b"slow down" * 1000)
                c = Client(user_agent="test", min_interval_s=0.0)
                c._opener = _ErrOpener(status, fp)
                with self.assertRaises(Blocked) as cm:
                    c.get_until(url, tvp.ProductDetector())
                self.assertEqual((cm.exception.status, cm.exception.reason), (status, f"http_{status}"))
                self.assertEqual((c._opener.calls, fp.sizes), (1, []))   # one request, body never read
                self.assertEqual(c.requests_made, 1)

    def test_challenge_signature_in_stream_and_header(self):
        url = tvp.profile_url("TSE:9901")
        c, op = self.client({url: (200, tp.CHALLENGE_HTML.encode() + b" " * (200 * KB))})
        with self.assertRaises(Blocked) as cm:
            c.get_until(url, tvp.ProductDetector())
        self.assertEqual(cm.exception.reason, "challenge_marker")
        self.assertEqual(op.responses[0].served, 16 * KB)           # detected in the first chunk
        self.assertTrue(op.responses[0].closed)
        c, op = self.client({url: (200, synthetic_page(), {"cf-mitigated": "challenge", "Content-Type": "text/html"})})
        with self.assertRaises(Blocked) as cm:
            c.get_until(url, tvp.ProductDetector())
        self.assertEqual((cm.exception.reason, op.responses[0].served), ("cf_mitigated_challenge", 0))

    def test_cloudflare_503_not_retried_plain_503_retried(self):
        url = tvp.profile_url("TSE:9901")
        c, op = self.client({url: (503, b"<html>busy</html>", {"Server": "cloudflare", "Content-Type": "text/html"})})
        with self.assertRaises(Blocked):
            c.get_until(url, tvp.ProductDetector())
        self.assertEqual(len(op.calls), 1)
        c, op = self.client({url: (503, b'{"e":1}', {"Content-Type": "application/json"})})
        self.assertEqual(c.get_until(url, tvp.ProductDetector()).status, 503)
        self.assertEqual(len(op.calls), 3)
        c, op = self.client({url: (404, b"")})
        self.assertEqual(c.get_until(url, tvp.ProductDetector()).status, 404)
        self.assertEqual(len(op.calls), 1)                          # 4xx never retried

    def test_limiter_interval_at_4_4_rps(self):
        url = tvp.profile_url("TSE:9901")
        c = Client(user_agent="test", min_interval_s=0.0)
        tvp.apply_rate(c, None)
        self.assertAlmostEqual(c.min_interval_s, 0.2273, places=4)
        c._opener = tp.FakeOpener({url: (200, tp.page_for("9901").encode())})
        clock = iter([100.0, 100.0, 100.0, 100.05, 100.05, 100.05, 100.05, 100.05])
        with mock.patch("jevscreen.http.time.monotonic", side_effect=lambda: next(clock, 100.3)):
            c.get_until(url, tvp.ProductDetector())
            c.get_until(url, tvp.ProductDetector())
        waits = [call.args[0] for call in self.sleep.call_args_list]
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 1 / 4.4 - 0.05, places=6)   # second request waited out the interval


class _CountingBody(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.sizes: list = []

    def read(self, n=-1):  # type: ignore[override]
        self.sizes.append(n)
        return super().read(n)


class _ErrOpener:
    def __init__(self, status: int, fp):
        self.status, self.fp, self.calls = status, fp, 0

    def open(self, req, timeout=None):
        self.calls += 1
        raise urllib.error.HTTPError(req.full_url, self.status, "err", {}, self.fp)


class CrawlStreaming(tp.Base):
    def real_client(self, script: dict) -> Client:
        c = Client(user_agent="test", min_interval_s=0.0)
        c._opener = tp.FakeOpener(script)
        return c

    def test_crawl_stops_each_page_early_and_reports_meter(self):
        self.add("TSE:9901", 2e11, isin="JP3999010001")
        client = self.real_client({tvp.profile_url("TSE:9901"): (200, synthetic_page())})
        err = io.StringIO()
        with mock.patch("jevscreen.http.time.sleep"), contextlib.redirect_stderr(err):
            s = tvp.crawl(self.cfg, client, progress_every=1)
        self.assertEqual((s["ok"], s["early_stops"], s["bytes_read"], s["mean_kb_per_page"]), (1, 1, 96 * KB, 96.0))
        self.assertTrue(client._opener.responses[0].closed)
        line = err.getvalue().strip().splitlines()[-1]
        self.assertTrue(line.startswith("[tradingview_profile] 1/1 {'ok': 1, "))   # format kept, meter appended
        self.assertTrue(line.endswith("early_stops=1 bytes_read=98304 mean_kb=96.0"))
        self.assertTrue(self.con.execute("SELECT text FROM descriptions").fetchone()[0].startswith("Okuzan Motor"))

    def test_cap_without_product_is_soft_and_not_an_error_streak(self):
        for i in range(5):
            self.add(f"A:C{i}", 10 - i)
        big = b"<html>" + b"z" * (300 * KB)
        client = self.real_client({tvp.profile_url(f"A:C{i}"): (200, big) for i in range(5)})
        with mock.patch("jevscreen.http.time.sleep"):
            s = tvp.crawl(self.cfg, client, progress_every=0, max_consecutive_errors=3)
        self.assertEqual((s["stopped_reason"], s["attempted"], s["no_product_within_cap"], s["error"]),
                         (None, 5, 5, 0))
        self.assertEqual({v[0] for v in self.states().values()}, {"no_product_within_cap"})
        self.assertNotIn("no_description", {v[0] for v in self.states().values()})
        self.assertEqual(len(tvp.queue(self.con)), 5)                    # retried by a later run

    def _blocked_run(self, second: tuple) -> tuple[dict, Client]:
        ids = ["A:ONE", "A:TWO", "A:THREE", "A:FOUR"]
        for i, sid in enumerate(ids):
            self.add(sid, 10 - i)
        client = self.real_client({tvp.profile_url("A:ONE"): (200, tp.page_for("ONE").encode()),
                                   tvp.profile_url("A:TWO"): second,
                                   tvp.profile_url("A:THREE"): (200, tp.page_for("THREE").encode()),
                                   tvp.profile_url("A:FOUR"): (200, tp.page_for("FOUR").encode())})
        with mock.patch("jevscreen.http.time.sleep"):
            return tvp.crawl(self.cfg, client, progress_every=0, workers=1), client

    def test_first_block_stops_whole_run_for_429_403_and_challenge(self):
        cases = {"429": ((429, b"slow down"), 429, "http_429"), "403": ((403, b"no"), 403, "http_403"),
                 "challenge": ((200, tp.CHALLENGE_HTML.encode()), 200, "challenge_marker")}
        for name, (second, status, reason) in cases.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                s, client = self._blocked_run(second)
                self.assertEqual(client._opener.calls, [tvp.profile_url("A:ONE"), tvp.profile_url("A:TWO")])
                self.assertEqual(client.requests_made, 2)                  # no retry, nothing after the block
                self.assertEqual((s["status"], s["blocked_url"], s["blocked_status"], s["blocked_reason"]),
                                 ("blocked", tvp.profile_url("A:TWO"), status, reason))
                self.assertEqual(self.states()["A:ONE"][0], "ok")           # progress flushed
                self.assertEqual(self.states()["A:TWO"][:2], ("blocked", status))
                self.assertIsNotNone(guard.recent_block_marker(self.cfg, guard.CMD_CRAWL_DESCRIPTIONS))

    def _cover_by_sec_text(self, con, sid: str, ck: str) -> None:
        store.upsert_many(con, "descriptions", ["security_id", "source_id", "company_key", "text"],
                          [[sid, "sec_filing_text", ck, "Item 1. Business"]])

    def _cover_by_document(self, con, sid: str, ck: str, cik: str = "320193") -> None:
        store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value", "method"],
                          [[sid, "sec_cik", cik, "ticker"]])
        store.upsert_many(con, "documents", ["doc_id", "company_key", "source_id", "cik", "text_path"],
                          [[f"sec_filing_text:{cik}:acc:item1", ck, "sec_filing_text", cik.zfill(10),
                            "/tmp/x.txt"]])

    def test_sec_covered_companies_dropped_before_crawl(self):
        ck_a = self.add("NYSE:AAA", 5, isin="US0000000AAA")
        ck_b = self.add("NYSE:BBB", 4, isin="US0000000BBB")
        self.add("NYSE:CCC", 3, isin="US0000000CCC")
        ck_d = self.add("NYSE:DDD", 2, isin="US0000000DDD")
        self._cover_by_sec_text(self.con, "NYSE:AAA", ck_a)
        self._cover_by_document(self.con, "NYSE:BBB", ck_b)
        store.upsert_many(self.con, "documents", ["doc_id", "company_key", "source_id", "cik", "text_path"],
                          [["sec_filing_text:9:acc:item1", ck_d, "sec_filing_text", "9", None]])   # no text
        store.upsert_many(self.con, "identifiers", ["security_id", "id_type", "id_value", "method"],
                          [["NYSE:DDD", "sec_cik", "9", "ticker"]])
        self.assertEqual(tvp.queue(self.con), ["NYSE:CCC", "NYSE:DDD"])
        self.assertEqual(tvp.queue(self.con, only_missing=False), ["NYSE:CCC", "NYSE:DDD"])
        self.assertEqual(tvp.queue(self.con, skip_sec_covered=False, only_missing=False),
                         ["NYSE:AAA", "NYSE:BBB", "NYSE:CCC", "NYSE:DDD"])

    def test_covered_set_refreshed_at_each_flush_mid_run(self):
        cks = {sid: self.add(sid, 10 - i, isin=f"US00000000{i}X") for i, sid in
               enumerate(["A:ONE", "A:TWO", "A:THREE", "A:FOUR", "A:FIVE"])}
        pages = {tvp.profile_url(sid): tp.page_for(sid.split(":")[1]) for sid in cks}

        def sync_sec_progress(url: str) -> None:
            # while the crawl fetches A:ONE, sync-sec (another writer) finishes THREE (text) and FOUR (document)
            if url == tvp.profile_url("A:ONE"):
                with store.session(self.cfg, wait_s=5) as con:
                    self._cover_by_sec_text(con, "A:THREE", cks["A:THREE"])
                    self._cover_by_document(con, "A:FOUR", cks["A:FOUR"], cik="0000789019")

        client = tp.ProbeClient(pages, sync_sec_progress)
        s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=1, workers=1)
        self.assertEqual(client.calls, [tvp.profile_url(x) for x in ("A:ONE", "A:TWO", "A:FIVE")])
        self.assertEqual((s["skipped_sec_covered"], s["attempted"], s["remaining"], s["ok"]), (2, 3, 0, 3))
        self.assertGreaterEqual(s["covered_refreshes"], 1)
        self.assertNotIn("A:THREE", self.states())

    def test_sigterm_flushes_and_marks_interrupted(self):
        for i in range(4):
            self.add(f"A:T{i}", 10 - i)
        pages = {tvp.profile_url(f"A:T{i}"): tp.page_for(f"T{i}") for i in range(4)}

        def term(url: str) -> None:
            if url == tvp.profile_url("A:T2"):
                os.kill(os.getpid(), signal.SIGTERM)

        client = tp.ProbeClient(pages, term)
        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(cli.Terminated), cli.sigterm_as_interrupt():
            tvp.crawl(self.cfg, client, progress_every=0, flush_every=50, workers=1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)       # handler restored
        # pending batch flushed on SIGTERM; the page already on the wire (T2, fetched by the worker) is drained and
        # recorded; nothing new (T3) starts
        self.assertEqual(sorted(self.states()), ["A:T0", "A:T1", "A:T2"])
        self.assertNotIn(tvp.profile_url("A:T3"), client.calls)
        self.assertEqual(self.run_row()[0], "interrupted")
        self.assertIsNotNone(self.run_row()[2])


class CliCrawlRules(tcc._CliBase):
    def cfg(self):
        from jevscreen import config
        return config.load()

    def populate(self, ids: list[str]) -> None:
        with store.session(self.cfg(), wait_s=5) as con:
            snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="test", request=None,
                                         raw_path=None, raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
            for i, sid in enumerate(ids):
                ex, sym = sid.split(":")
                row = dict(security_id=sid, exchange=ex, symbol=sym, name=sym, is_primary=True, tv_type="stock",
                           tv_subtype="common", company_key=store.company_key(None, sid), active=True,
                           first_seen_snapshot=snap, last_seen_snapshot=snap)
                store.upsert_many(con, "securities", list(row), [list(row.values())])
                store.upsert_many(con, "market_daily", ["security_id", "as_of", "market_cap_usd", "snapshot_id"],
                                  [[sid, "2026-09-26", 100 - i, snap]])

    def runs(self) -> list[tuple]:
        with store.session(self.cfg(), read_only=True, wait_s=5) as con:
            return con.execute("SELECT command, status, finished_at, note FROM runs ORDER BY started_at").fetchall()

    def test_rate_default_and_clamp_4_4(self):
        self.assertEqual(cli.build_parser().parse_args(["crawl-descriptions"]).rate, 4.4)
        seen = []
        crawl = lambda cfg, client, **kw: seen.append(client.min_interval_s) or {"ok": 0}  # noqa: E731
        with mock.patch.dict(sys.modules, {cli.PROFILES: tcc._fake_module(cli.PROFILES, crawl=crawl)}):
            code, _, err = self.run_cli("crawl-descriptions")
            self.assertEqual((code, err), (0, ""))
            code, _, err = self.run_cli("crawl-descriptions", "--rate", "10")
            self.assertEqual(code, 0)
            self.assertIn("clamped to 4.4", err)
        self.assertAlmostEqual(seen[0], 0.2273, places=4)
        self.assertAlmostEqual(seen[1], 0.2273, places=4)

    def _blocked_cli(self, second: object) -> tuple[int, str, tp.FakeClient]:
        ids = ["A:ONE", "A:TWO", "A:THREE"]
        self.populate(ids)
        fake = tp.FakeClient({tvp.profile_url("A:ONE"): tp.page_for("ONE"), tvp.profile_url("A:TWO"): second,
                              tvp.profile_url("A:THREE"): tp.page_for("THREE")})
        with mock.patch.object(cli, "make_crawl_client", return_value=fake):
            code, out, _ = self.run_cli("crawl-descriptions", "--workers", "1")
        return code, out, fake

    def test_first_429_prints_blocked_line_and_exits_2(self):
        url = tvp.profile_url("A:TWO")
        for status in (429, 403):
            with self.subTest(status=status):
                self.tearDown_home()
                code, out, fake = self._blocked_cli(Blocked(url, status, f"http_{status}"))
                self.assertEqual(code, 2)
                self.assertEqual(fake.calls, [tvp.profile_url("A:ONE"), url])    # zero further requests
                last = out.strip().splitlines()[-1]
                self.assertEqual(last, f"BLOCKED url={url} status={status} reason=http_{status}")
                self.assertEqual(sum(ln.startswith("BLOCKED") for ln in out.splitlines()), 1)
                self.assertEqual(self.runs()[-1][1], "blocked")
                self.assertIsNotNone(guard.recent_block_marker(self.cfg(), "crawl-descriptions"))
                # no automatic resume: a rerun is refused by the cooldown without any request
                with mock.patch.object(cli, "make_crawl_client", side_effect=AssertionError("no client")):
                    self.assertEqual(self.run_cli("crawl-descriptions")[0], 2)

    def test_challenge_prints_blocked_line(self):
        url = tvp.profile_url("A:TWO")
        code, out, fake = self._blocked_cli(Blocked(url, 200, "challenge_marker"))
        self.assertEqual((code, len(fake.calls)), (2, 2))
        self.assertEqual(out.strip().splitlines()[-1], f"BLOCKED url={url} status=200 reason=challenge_marker")

    def tearDown_home(self) -> None:
        """Fresh JEVSCREEN_HOME for each subTest."""
        new = tempfile.TemporaryDirectory()
        self.addCleanup(new.cleanup)
        os.environ["JEVSCREEN_HOME"] = new.name
        from jevscreen import config, consent
        consent.record(config.Config(), consent.GRAY_SOURCES, "yes")     # crawl-descriptions needs it

    def test_sigterm_in_cli_crawl_exits_130_after_flush(self):
        self.populate(["A:S0", "A:S1", "A:S2"])

        def term(url: str) -> None:
            if url == tvp.profile_url("A:S1"):
                os.kill(os.getpid(), signal.SIGTERM)

        fake = tp.ProbeClient({tvp.profile_url(f"A:S{i}"): tp.page_for(f"S{i}") for i in range(3)}, term)
        with mock.patch.object(cli, "make_crawl_client", return_value=fake):
            code, out, _ = self.run_cli("crawl-descriptions", "--workers", "1")
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(out)["signal"], "SIGTERM")
        runs = self.runs()
        self.assertEqual([r[1] for r in runs], ["interrupted"])
        self.assertIsNotNone(runs[0][2])
        with store.session(self.cfg(), read_only=True, wait_s=5) as con:
            # S1 was on the wire when SIGTERM arrived: drained and recorded; S2 never started
            self.assertEqual(sorted(con.execute("SELECT security_id FROM crawl_state").fetchall()),
                             [("A:S0",), ("A:S1",)])
        self.assertNotIn(tvp.profile_url("A:S2"), fake.calls)

    def test_sigterm_in_cli_sync_sec_exits_130(self):
        def sync(cfg, client, **kw):
            with store.session(cfg) as con:
                con.execute("INSERT INTO runs VALUES ('s1', 'sync sec_edgar', ?, NULL, 'running', 0, NULL)",
                            [store.now_utc()])
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(10_000_000):   # the handler raises at the next bytecode
                pass
            raise AssertionError("SIGTERM not delivered")
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": "Test Person test@example.org"}), \
                mock.patch.dict(sys.modules, {cli.SEC_EDGAR: tcc._fake_module(cli.SEC_EDGAR, sync=sync)}):
            code, out, err = self.run_cli("sync-sec")
        self.assertEqual(code, 130)
        self.assertNotIn("test@example.org", out + err)
        self.assertEqual([r[:2] for r in self.runs()], [("sync sec_edgar", "interrupted")])

    def test_abandoned_running_rows_marked_on_start(self):
        with store.session(self.cfg(), wait_s=5) as con:
            con.execute("INSERT INTO runs VALUES ('old', 'crawl tradingview_profile', ?, NULL, 'running', 5, NULL)",
                        [store.now_utc()])
            con.execute("INSERT INTO runs VALUES ('sec', 'sync sec_edgar', ?, NULL, 'running', 5, NULL)",
                        [store.now_utc()])
        crawl = lambda cfg, client, **kw: {"ok": 0}  # noqa: E731
        with mock.patch.dict(sys.modules, {cli.PROFILES: tcc._fake_module(cli.PROFILES, crawl=crawl)}):
            code, _, err = self.run_cli("crawl-descriptions")
        self.assertEqual(code, 0)
        self.assertIn("abandoned", err)
        with store.session(self.cfg(), read_only=True, wait_s=5) as con:
            rows = dict(con.execute("SELECT run_id, status FROM runs WHERE run_id IN ('old', 'sec')").fetchall())
            finished = con.execute("SELECT finished_at FROM runs WHERE run_id = 'old'").fetchone()[0]
        self.assertEqual(rows, {"old": "abandoned", "sec": "running"})   # other command untouched
        self.assertIsNotNone(finished)
        # a live holder of the budget lock means the process is not gone: the command refuses (exit 4), marks nothing
        with store.session(self.cfg(), wait_s=5) as con:
            con.execute("UPDATE runs SET status = 'running', finished_at = NULL WHERE run_id = 'old'")
        with guard.budget_lock(self.cfg(), "www.tradingview.com"):
            self.assertEqual(self.run_cli("crawl-descriptions")[0], cli.EXIT_BUSY)
        with store.session(self.cfg(), read_only=True, wait_s=5) as con:
            self.assertEqual(con.execute("SELECT status FROM runs WHERE run_id = 'old'").fetchone()[0], "running")

    def test_sync_sec_marks_its_own_abandoned_rows(self):
        with store.session(self.cfg(), wait_s=5) as con:
            con.execute("INSERT INTO runs VALUES ('sec', 'sync sec_edgar', ?, NULL, 'running', 5, NULL)",
                        [store.now_utc()])
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": "Test Person test@example.org"}), \
                mock.patch.dict(sys.modules, {cli.SEC_EDGAR: tcc._fake_module(cli.SEC_EDGAR,
                                                                             sync=lambda c, cl, **k: {"status": "ok"})}):
            self.assertEqual(self.run_cli("sync-sec")[0], 0)
        with store.session(self.cfg(), read_only=True, wait_s=5) as con:
            self.assertEqual(con.execute("SELECT status FROM runs WHERE run_id = 'sec'").fetchone()[0], "abandoned")


if __name__ == "__main__":
    unittest.main()
