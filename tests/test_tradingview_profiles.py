"""Tests for sources.tradingview_profiles. No network: every client is a fake or a real Client with a fake opener."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import os
import subprocess
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Client, Response  # noqa: E402
from jevscreen.sources import tradingview_profiles as tvp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OKUZAN_HTML = (FIXTURES / "tv_profile_TSE-9901.html").read_text(encoding="utf-8")
GRAPH_HTML = (FIXTURES / "profile_graph.html").read_text(encoding="utf-8")
NO_FP_HTML = (FIXTURES / "profile_no_financial_product.html").read_text(encoding="utf-8")
CHALLENGE_HTML = (FIXTURES / "profile_challenge.html").read_text(encoding="utf-8")


def page_for(ticker: str, desc: str = "A company that makes things.") -> str:
    obj = {"@context": "https://schema.org", "@type": "FinancialProduct", "name": f"{ticker} Corp",
           "tickerSymbol": ticker, "description": desc}
    return f'<html><head><script type="application/ld+json">{json.dumps(obj)}</script></head></html>'


class FakeClient:
    """Stands in for http.Client: maps URL -> Response | Exception; records every call."""

    def __init__(self, pages: dict[str, object] | None = None, default: object = None):
        self.pages, self.default = pages or {}, default
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.min_interval_s, self.max_bytes, self.requests_made = 0.0, None, 0

    def get(self, url: str, **kw) -> Response:
        self.calls.append(url)
        self.kwargs.append(kw)
        self.requests_made += 1
        item = self.pages.get(url, self.default)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return Response(url, item, b"", {}, 0.0)
        if item is None:
            return Response(url, 404, b"", {}, 0.0)
        return Response(url, 200, str(item).encode(), {}, 0.0)


class _FakeHTTPResponse:
    """A streamed body: read(n) returns the next n bytes; records bytes served and whether close() was called."""

    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status, self.headers = status, headers or {}
        self._stream = io.BytesIO(body)
        self.served, self.closed = 0, False

    def read(self, n: int | None = None) -> bytes:
        data = self._stream.read() if n is None or n < 0 else self._stream.read(n)
        self.served += len(data)
        return data

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """Replaces urllib's opener inside a REAL http.Client so its Blocked logic is exercised offline."""

    def __init__(self, script: dict[str, tuple]):
        self.script, self.calls, self.responses = script, [], []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        status, body, *rest = self.script.get(url, (404, b""))
        headers = rest[0] if rest else {}
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "err", headers, io.BytesIO(body))
        resp = _FakeHTTPResponse(status, body, headers)
        self.responses.append(resp)
        return resp


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), min_interval_s=1.0)
        self.con = store.connect(self.cfg)
        self.snap = store.record_snapshot(self.con, source_id="tradingview_scanner", kind="test", request=None,
                                          raw_path=None, raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def add(self, security_id: str, cap: float | None, isin: str | None = None, **kw) -> str:
        exchange, symbol = security_id.split(":", 1)
        ck = store.company_key(isin, security_id)
        row = dict(security_id=security_id, exchange=exchange, symbol=symbol, name=symbol, isin=isin,
                   country=None, tv_type=kw.get("tv_type", "stock"), tv_subtype=kw.get("tv_subtype", "common"),
                   is_primary=kw.get("is_primary", True), price_currency=None, fundamental_currency=None,
                   sector=None, industry=None, company_key=ck, first_seen_snapshot=self.snap,
                   last_seen_snapshot=self.snap, last_seen_at=store.now_utc(), active=True)
        store.upsert_many(self.con, "securities", list(row), [list(row.values())])
        store.upsert_many(self.con, "market_daily", ["security_id", "as_of", "market_cap_usd", "snapshot_id"],
                          [[security_id, dt.date(2026, 9, 26), cap, self.snap]])
        return ck

    def states(self) -> dict[str, tuple]:
        rows = self.con.execute("SELECT security_id, status, http_status, attempts, note FROM crawl_state "
                                "WHERE source_id = 'tradingview_profile'").fetchall()
        return {r[0]: r[1:] for r in rows}

    def run_row(self) -> tuple:
        return self.con.execute("SELECT status, requests, finished_at FROM runs ORDER BY started_at DESC").fetchone()


class TestUrlAndExtraction(unittest.TestCase):
    def test_profile_url(self):
        self.assertEqual(tvp.profile_url("TSE:9901"), "https://www.tradingview.com/symbols/TSE-9901/")
        self.assertEqual(tvp.profile_url("NYSE:BRK.B"), "https://www.tradingview.com/symbols/NYSE-BRK.B/")
        self.assertEqual(tvp.profile_url("XETR:A/B C"), "https://www.tradingview.com/symbols/XETR-A%2FB%20C/")
        for bad in ("7203", "TSE:", ":7203", ""):
            with self.assertRaises(ValueError):
                tvp.profile_url(bad)

    def test_fixture_page(self):
        d = tvp.extract_description(OKUZAN_HTML)
        self.assertEqual(d["name"], "Okuzan Motor Corp.")
        self.assertEqual(d["tickerSymbol"], "9901")
        self.assertTrue(d["description"].startswith("Okuzan Motor Corporation (Okuzan) designs, assembles"))
        self.assertIsNone(d["url"])  # absent key stays None
        self.assertEqual(set(d), {"name", "tickerSymbol", "description", "url"})

    def test_graph_variant(self):
        d = tvp.extract_description(GRAPH_HTML)
        self.assertEqual(d["tickerSymbol"], "BRK.B")
        self.assertIsNone(tvp.validate(d, "NYSE:BRK.B", tvp.profile_url("NYSE:BRK.B")))

    def test_missing_financial_product_and_no_js(self):
        self.assertIsNone(tvp.extract_description(NO_FP_HTML))
        self.assertIsNone(tvp.extract_description(""))

    def test_rejects_empty_and_too_long(self):
        self.assertIsNone(tvp.extract_description(page_for("X", "   ")))
        self.assertIsNone(tvp.extract_description(page_for("X", "a" * 20001)))
        self.assertIsNotNone(tvp.extract_description(page_for("X", "a" * 20000)))

    def test_validate(self):
        url = tvp.profile_url("NYSE:BRK.B")
        ok = {"tickerSymbol": "brk.b", "url": "https://www.tradingview.com/symbols/NYSE-BRK.B/"}
        self.assertIsNone(tvp.validate(ok, "NYSE:BRK.B", url))
        self.assertIsNone(tvp.validate({"tickerSymbol": "BRK/B"}, "NYSE:BRK.B", url))
        self.assertIsNone(tvp.validate({"tickerSymbol": "NYSE:BRK.B"}, "NYSE:BRK.B", url))
        self.assertIn("ticker_mismatch", tvp.validate({"tickerSymbol": "7201"}, "TSE:9901", tvp.profile_url("TSE:9901")))
        self.assertEqual(tvp.validate({"tickerSymbol": None}, "TSE:9901", tvp.profile_url("TSE:9901")), "ticker_missing")
        bad_url = {"tickerSymbol": "BRK.B", "url": "https://www.tradingview.com/symbols/NYSE-BRK.A/"}
        self.assertIn("url_path_mismatch", tvp.validate(bad_url, "NYSE:BRK.B", url))
        evil = {"tickerSymbol": "BRK.B", "url": "https://evil.example/symbols/NYSE-BRK.B/"}
        self.assertIn("url_host_mismatch", tvp.validate(evil, "NYSE:BRK.B", url))


class TestRate(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(tvp.MAX_RATE, 4.4)
        self.assertEqual(tvp.clamp_rate(None), 4.4)
        self.assertEqual(tvp.clamp_rate(5.0), 4.4)
        self.assertEqual(tvp.clamp_rate(0.5), 0.5)
        with self.assertRaises(ValueError):
            tvp.clamp_rate(0)

    def test_apply_rate_sets_client(self):
        c = FakeClient()
        self.assertAlmostEqual(tvp.apply_rate(c, 10.0), 4.4)
        self.assertAlmostEqual(c.min_interval_s, 1 / 4.4)     # ~0.227 s between requests
        self.assertAlmostEqual(c.min_interval_s, 0.227, places=3)
        self.assertIsNone(c.max_bytes)  # the client's own cap is never changed
        tvp.apply_rate(c, 0.25)
        self.assertEqual(c.min_interval_s, 4.0)
        tvp.apply_rate(c, None)          # never lowers a slower client
        self.assertEqual(c.min_interval_s, 4.0)


class TestCrawl(Base):
    def test_happy_path_fixture_page(self):
        ck = self.add("TSE:9901", 2e11, isin="JP3999010001")
        client = FakeClient({tvp.profile_url("TSE:9901"): OKUZAN_HTML})
        client.min_interval_s = 0.01  # crawl must clamp this to >= 1/4.4 s
        s = tvp.crawl(self.cfg, client, progress_every=0)
        self.assertEqual((s["ok"], s["attempted"], s["stopped_reason"], s["status"]), (1, 1, None, "ok"))
        self.assertAlmostEqual(client.min_interval_s, 1 / 4.4)
        self.assertEqual(client.kwargs, [{"max_bytes": 256 * 1024}])  # per-request cap, client untouched
        self.assertIsNone(client.max_bytes)
        text, company_key, snap, url = self.con.execute(
            "SELECT text, company_key, snapshot_id, source_url FROM descriptions WHERE security_id='TSE:9901'").fetchone()
        self.assertTrue(text.startswith("Okuzan Motor Corporation"))
        self.assertEqual((company_key, url), (ck, "https://www.tradingview.com/symbols/TSE-9901/"))
        kind, raw_path, rows = self.con.execute(
            "SELECT kind, raw_path, rows FROM snapshots WHERE snapshot_id = ?", [snap]).fetchone()
        self.assertEqual((kind, rows), ("profile_batch", 1))
        line = json.loads(Path(raw_path).read_text().splitlines()[0])
        self.assertEqual(line["product"]["@type"], "FinancialProduct")
        self.assertLess(Path(raw_path).stat().st_size, 10_000)  # only the extracted object, not the page
        self.assertEqual(store.license_tier_for_snapshot(self.con, snap), "gray-private")
        self.assertEqual(self.states()["TSE:9901"][:3], ("ok", 200, 1))
        self.assertEqual(self.run_row()[:2], ("ok", 1))

    def test_queue_order_and_universe_line(self):
        self.add("NYSE:BIG", 3e12)
        self.add("NYSE:MID", 1e9)
        self.add("NYSE:NOCAP", None)
        self.add("NYSE:PREF", 5e12, tv_subtype="preferred")
        self.add("LSE:DUP", 1e8, isin="GB0000000001")
        self.add("NYSE:DUP", 9e8, isin="GB0000000001")
        self.assertEqual(tvp.queue(self.con), ["NYSE:BIG", "NYSE:MID", "NYSE:DUP", "NYSE:NOCAP"])
        self.assertEqual(tvp.queue(self.con, limit=2), ["NYSE:BIG", "NYSE:MID"])

    def test_missing_fp_mismatch_404_statuses(self):
        for sid, cap in (("A:NOFP", 4), ("A:MISM", 3), ("A:GONE", 2), ("A:GOOD", 1)):
            self.add(sid, cap)
        client = FakeClient({tvp.profile_url("A:NOFP"): NO_FP_HTML, tvp.profile_url("A:MISM"): page_for("OTHER"),
                             tvp.profile_url("A:GONE"): 404, tvp.profile_url("A:GOOD"): page_for("GOOD")})
        s = tvp.crawl(self.cfg, client, progress_every=0)
        st = self.states()
        self.assertEqual(st["A:NOFP"][0], "no_description")
        self.assertEqual(st["A:MISM"][0], "mismatch")
        self.assertIn("ticker_mismatch", st["A:MISM"][3])
        self.assertEqual(st["A:GONE"][:2], ("not_found", 404))
        self.assertEqual(st["A:GOOD"][0], "ok")
        self.assertEqual((s["ok"], s["mismatch"], s["not_found"], s["no_description"], s["error"]), (1, 1, 1, 1, 0))
        # the mismatched object is kept in the raw batch for audit, but no description row is written for it
        self.assertEqual(self.con.execute("SELECT count(*) FROM descriptions").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT sum(rows) FROM snapshots WHERE kind='profile_batch'").fetchone()[0], 2)

    def test_blocked_429_real_client_stops_run(self):
        ids = ["A:ONE", "A:TWO", "A:THREE", "A:FOUR"]
        for i, sid in enumerate(ids):
            self.add(sid, 10 - i)
        client = Client(user_agent="test", min_interval_s=0.0)
        opener = FakeOpener({tvp.profile_url("A:ONE"): (200, page_for("ONE").encode()),
                             tvp.profile_url("A:TWO"): (429, b"slow down"),
                             tvp.profile_url("A:THREE"): (200, page_for("THREE").encode())})
        client._opener = opener
        with mock.patch("jevscreen.http.time.sleep") as sleep:
            s = tvp.crawl(self.cfg, client, progress_every=0, workers=1)
        self.assertTrue(sleep.called)  # the 1 s politeness interval was enforced
        self.assertEqual(opener.calls, [tvp.profile_url("A:ONE"), tvp.profile_url("A:TWO")])  # no retry, no more
        self.assertEqual((s["stopped_reason"], s["blocked_at"], s["status"]), ("blocked", "A:TWO", "blocked"))
        st = self.states()
        self.assertEqual(st["A:ONE"][0], "ok")  # batch so far was written
        self.assertEqual(st["A:TWO"][:2], ("blocked", 429))
        self.assertNotIn("A:THREE", st)
        self.assertNotIn("A:FOUR", st)
        self.assertEqual(self.run_row()[:2], ("blocked", 2))
        self.assertEqual(self.con.execute("SELECT count(*) FROM descriptions").fetchone()[0], 1)
        # next run starts again from the blocked item, never from done ones
        self.assertEqual(tvp.queue(self.con), ["A:TWO", "A:THREE", "A:FOUR"])

    def test_challenge_page_is_blocked(self):
        self.add("A:ONE", 2)
        self.add("A:TWO", 1)
        client = Client(user_agent="test", min_interval_s=0.0)
        client._opener = FakeOpener({tvp.profile_url("A:ONE"): (200, CHALLENGE_HTML.encode())})
        with mock.patch("jevscreen.http.time.sleep"):
            s = tvp.crawl(self.cfg, client, progress_every=0, workers=1)
        self.assertEqual(s["stopped_reason"], "blocked")
        self.assertEqual(self.states(), {"A:ONE": ("blocked", 200, 1, "blocked: challenge_marker")})

    def test_blocked_from_fake_client(self):
        self.add("A:ONE", 1)
        url = tvp.profile_url("A:ONE")
        client = FakeClient({url: Blocked(url, 403, "http_403")})
        s = tvp.crawl(self.cfg, client, progress_every=0)
        self.assertEqual((s["stopped_reason"], s["blocked"], s["remaining"]), ("blocked", 1, 0))
        from jevscreen import guard
        self.assertIn("http_403", guard.recent_block_marker(self.cfg, guard.CMD_CRAWL_DESCRIPTIONS)["note"])

    def test_consecutive_errors_stop(self):
        for i in range(6):
            self.add(f"A:E{i}", 10 - i)
        client = FakeClient({tvp.profile_url("A:E0"): 404, tvp.profile_url("A:E1"): 500,
                             tvp.profile_url("A:E2"): OSError("reset"), tvp.profile_url("A:E3"): 301,
                             tvp.profile_url("A:E4"): http.client.IncompleteRead(b"partial")})
        s = tvp.crawl(self.cfg, client, progress_every=0, max_consecutive_errors=3, workers=1)
        self.assertEqual((s["stopped_reason"], s["status"], s["error"], s["redirect"], s["attempted"]),
                         ("consecutive_errors", "stopped_errors", 3, 1, 5))
        self.assertEqual(len(client.calls), 5)
        self.assertNotIn("A:E5", self.states())
        self.assertIn("network", self.states()["A:E2"][3])
        self.assertEqual(self.states()["A:E3"][0], "redirect")    # does not count toward the stop
        self.assertIn("IncompleteRead", self.states()["A:E4"][3])

    def test_mismatch_and_redirect_do_not_stop_and_have_retry_limit(self):
        for i in range(4):
            self.add(f"A:M{i}", 10 - i)
        pages = {tvp.profile_url("A:M0"): page_for("OTHER"), tvp.profile_url("A:M1"): 302,
                 tvp.profile_url("A:M2"): page_for("OTHER"), tvp.profile_url("A:M3"): page_for("M3")}
        for run in range(tvp.MAX_SOFT_ATTEMPTS):
            s = tvp.crawl(self.cfg, FakeClient(pages), progress_every=0)
            self.assertIsNone(s["stopped_reason"])
        self.assertEqual(s["attempted"], 3)                        # M3 finished in run 1
        self.assertEqual(self.states()["A:M1"][:3], ("redirect", 302, 3))
        self.assertEqual(tvp.queue(self.con), [])                  # retry limit reached

    def test_truncated_page_is_not_final(self):
        self.add("A:BIG", 1)
        client = Client(user_agent="test", min_interval_s=0.0)
        client._opener = FakeOpener({tvp.profile_url("A:BIG"): (200, b" " * tvp.MAX_BYTES + OKUZAN_HTML.encode())})
        with mock.patch("jevscreen.http.time.sleep"):
            tvp.crawl(self.cfg, client, progress_every=0)
        self.assertEqual(self.states()["A:BIG"][0], "no_product_within_cap")
        self.assertEqual(tvp.queue(self.con), ["A:BIG"])
        self.assertIsNone(client.max_bytes)

    def test_resume_skips_done_and_retries_errors(self):
        for i in range(4):
            self.add(f"A:S{i}", 10 - i)
        pages = {tvp.profile_url(f"A:S{i}"): page_for(f"S{i}") for i in range(4)}
        pages[tvp.profile_url("A:S1")] = 500
        first = FakeClient(pages)
        tvp.crawl(self.cfg, first, limit=2, progress_every=0, workers=1)
        self.assertEqual(len(first.calls), 2)
        pages[tvp.profile_url("A:S1")] = page_for("S1")
        second = FakeClient(pages)
        s = tvp.crawl(self.cfg, second, progress_every=0, workers=1)
        self.assertEqual(second.calls, [tvp.profile_url(f"A:S{i}") for i in (1, 2, 3)])
        self.assertEqual(s["ok"], 3)
        self.assertEqual(self.states()["A:S1"][:3], ("ok", 200, 2))  # attempts accumulate
        third = FakeClient(pages)
        self.assertEqual(tvp.crawl(self.cfg, third, progress_every=0)["attempted"], 0)
        self.assertEqual(third.calls, [])

    def test_batches_commit_every_n_pages(self):
        for i in range(5):
            self.add(f"A:B{i}", 10 - i)
        client = FakeClient({tvp.profile_url(f"A:B{i}"): page_for(f"B{i}") for i in range(5)})
        s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=2)
        self.assertEqual(s["snapshots"], 3)
        self.assertEqual(self.con.execute("SELECT count(DISTINCT snapshot_id) FROM descriptions").fetchone()[0], 3)

    def test_interrupt_flushes_progress(self):
        for i in range(3):
            self.add(f"A:I{i}", 10 - i)
        pages = {tvp.profile_url("A:I0"): page_for("I0"), tvp.profile_url("A:I1"): KeyboardInterrupt()}
        with self.assertRaises(KeyboardInterrupt):
            tvp.crawl(self.cfg, FakeClient(pages), progress_every=0, workers=1)
        self.assertEqual(self.states(), {"A:I0": ("ok", 200, 1, None)})
        self.assertEqual(self.run_row()[0], "interrupted")

    def test_only_missing_skips_companies_with_fd_description(self):
        ck = self.add("NYSE:HAS", 5, isin="US0000000001")
        self.add("NYSE:NEW", 1)
        store.upsert_many(self.con, "descriptions", ["security_id", "source_id", "company_key", "text"],
                          [["NYSE:HAS", "financedatabase_local", ck, "Yahoo text"]])
        self.assertEqual(tvp.queue(self.con), ["NYSE:NEW"])
        self.assertEqual(tvp.queue(self.con, only_missing=False), ["NYSE:HAS", "NYSE:NEW"])
        client = FakeClient({tvp.profile_url("NYSE:NEW"): page_for("NEW")})
        tvp.crawl(self.cfg, client, progress_every=0)
        self.assertEqual(client.calls, [tvp.profile_url("NYSE:NEW")])


# Runs in a separate process: opens a WRITE session on the store and writes one runs row. Exit 0 = the store was
# free, 3 = StoreLocked. Used to prove the crawler holds no DuckDB connection while it fetches.
PROBE = r"""
import sys
sys.path.insert(0, sys.argv[1])
from jevscreen import store
from jevscreen.config import Config
from pathlib import Path
try:
    with store.session(Config(home=Path(sys.argv[2])), wait_s=float(sys.argv[3])) as con:
        con.execute("INSERT INTO runs VALUES (?, 'probe', now()::TIMESTAMP, NULL, 'ok', 0, NULL)", [sys.argv[4]])
except store.StoreLocked:
    sys.exit(3)
"""
SRC = str(Path(__file__).resolve().parents[1] / "src")


def probe_write(home: Path, run_id: str, wait_s: float = 5.0) -> int:
    return subprocess.run([sys.executable, "-c", PROBE, SRC, str(home), str(wait_s), run_id],
                          capture_output=True, timeout=120).returncode


class ProbeClient(FakeClient):
    """FakeClient whose get() first runs a hook (e.g. writes to the store from another process)."""

    def __init__(self, pages, hook):
        super().__init__(pages)
        self.hook = hook

    def get(self, url: str, **kw) -> Response:
        self.hook(url)
        return super().get(url, **kw)


class TestShortSessions(Base):
    def test_other_process_can_write_while_crawler_fetches(self):
        for i in range(2):
            self.add(f"A:P{i}", 10 - i)
        home = Path(self.tmp.name)
        # negative control: while this process holds a connection, another process cannot write
        self.assertEqual(probe_write(home, "control", wait_s=0.5), 3)
        self.con.close()
        results = []
        client = ProbeClient({tvp.profile_url(f"A:P{i}"): page_for(f"P{i}") for i in range(2)},
                             lambda url: results.append(probe_write(home, f"probe-{len(results)}")))
        s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=1, workers=1)
        self.assertEqual(results, [0, 0])           # never StoreLocked during a fetch
        self.assertEqual((s["ok"], s["status"]), (2, "ok"))
        self.con = store.connect(self.cfg)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs WHERE command = 'probe'").fetchone()[0], 2)
        self.assertEqual(self.con.execute("SELECT count(*) FROM descriptions").fetchone()[0], 2)

    def test_no_session_open_during_fetch(self):
        for i in range(3):
            self.add(f"A:N{i}", 10 - i)
        real, open_now, opened = store.session, [0], [0]

        @contextlib.contextmanager
        def counting(*a, **k):
            with real(*a, **k) as con:
                open_now[0] += 1
                opened[0] += 1
                try:
                    yield con
                finally:
                    open_now[0] -= 1

        seen = []
        client = ProbeClient({tvp.profile_url(f"A:N{i}"): page_for(f"N{i}") for i in range(3)},
                             lambda url: seen.append(open_now[0]))
        with mock.patch.object(store, "session", counting):
            tvp.crawl(self.cfg, client, progress_every=0, flush_every=2, workers=1)
        self.assertEqual(seen, [0, 0, 0])
        self.assertEqual(opened[0], 3)   # queue + one mid-run flush + final flush

    def test_flush_every_n_pages(self):
        for i in range(5):
            self.add(f"A:F{i}", 10 - i)
        committed = []
        client = ProbeClient({tvp.profile_url(f"A:F{i}"): page_for(f"F{i}") for i in range(5)},
                             lambda url: committed.append(len(self.states())))
        s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=2, workers=1)
        self.assertEqual(committed, [0, 0, 2, 2, 4])   # state visible to others after every 2 pages
        self.assertEqual((s["snapshots"], s["flushes"]), (3, 3))
        self.assertEqual(len(self.states()), 5)

    def test_interrupt_after_mid_run_flush_writes_pending_pages(self):
        for i in range(5):
            self.add(f"A:K{i}", 10 - i)
        pages = {tvp.profile_url(f"A:K{i}"): page_for(f"K{i}") for i in range(3)}
        pages[tvp.profile_url("A:K3")] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            tvp.crawl(self.cfg, FakeClient(pages), progress_every=0, flush_every=2, workers=1)
        self.assertEqual(sorted(self.states()), ["A:K0", "A:K1", "A:K2"])
        self.assertEqual(self.run_row()[0], "interrupted")
        self.assertIsNotNone(self.run_row()[2])

    def test_locked_mid_run_flush_keeps_batch(self):
        for i in range(4):
            self.add(f"A:L{i}", 10 - i)
        real, calls = store.session, [0]

        def flaky(*a, **k):
            calls[0] += 1
            if calls[0] == 2:          # the first mid-run flush finds the store locked
                raise store.StoreLocked("Could not set lock on file")
            return real(*a, **k)

        client = FakeClient({tvp.profile_url(f"A:L{i}"): page_for(f"L{i}") for i in range(4)})
        with mock.patch.object(store, "session", flaky), contextlib.redirect_stderr(io.StringIO()) as err:
            s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=2, workers=1)
        self.assertIn("store locked", err.getvalue())
        # pages 1-2 kept after the locked flush; the next attempt waits for another flush_every pages (at 4
        # pending), which is the end of the queue here, so the final flush writes all four at once
        self.assertEqual((s["ok"], s["status"], s["flushes"], s["snapshots"]), (4, "ok", 1, 1))
        self.assertEqual(len(self.states()), 4)

    def test_store_locked_too_long_stops_run_and_flushes(self):
        for i in range(5):
            self.add(f"A:X{i}", 10 - i)
        real = store.session

        def locked_mid_run(cfg, *, wait_s=600.0, **k):
            if wait_s == tvp.FLUSH_WAIT_S:
                raise store.StoreLocked("Could not set lock on file")
            return real(cfg, wait_s=wait_s, **k)

        client = FakeClient({tvp.profile_url(f"A:X{i}"): page_for(f"X{i}") for i in range(5)})
        with mock.patch.object(store, "session", locked_mid_run), mock.patch.object(tvp, "MAX_BATCH", 2), \
                contextlib.redirect_stderr(io.StringIO()):
            s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=1, workers=1)
        self.assertEqual((s["stopped_reason"], s["status"], s["attempted"]), ("store_locked", "store_locked", 2))
        self.assertEqual(sorted(self.states()), ["A:X0", "A:X1"])   # nothing lost: the final flush wrote both
        self.assertEqual(self.run_row()[0], "store_locked")

    def test_locked_flushes_back_off_and_stop(self):
        for i in range(12):
            self.add(f"A:Y{i}", 20 - i)
        real, attempts = store.session, []

        def locked_mid_run(cfg, *, wait_s=600.0, **k):
            if wait_s == tvp.FLUSH_WAIT_S:
                attempts.append(1)
                raise store.StoreLocked("Could not set lock on file")
            return real(cfg, wait_s=wait_s, **k)

        client = FakeClient({tvp.profile_url(f"A:Y{i}"): page_for(f"Y{i}") for i in range(12)})
        with mock.patch.object(store, "session", locked_mid_run), mock.patch.object(tvp, "MAX_LOCKED_FLUSHES", 3), \
                contextlib.redirect_stderr(io.StringIO()):
            s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=2, workers=1)
        # attempts at 2, 4 and 6 pending pages (not one per page), then stop after the third locked attempt
        self.assertEqual((len(attempts), s["attempted"], s["stopped_reason"]), (3, 6, "store_locked"))
        self.assertEqual(len(self.states()), 6)

    def test_crawl_with_connection_backward_compatible(self):
        self.add("A:C0", 1)
        client = FakeClient({tvp.profile_url("A:C0"): page_for("C0")})
        with mock.patch.object(store, "session", side_effect=AssertionError("must use the given connection")):
            s = tvp.crawl_with_connection(self.cfg, self.con, client, progress_every=0, batch_size=1)
        self.assertEqual((s["ok"], s["status"]), (1, "ok"))
        self.assertEqual(self.states()["A:C0"][0], "ok")


if __name__ == "__main__":
    unittest.main()
