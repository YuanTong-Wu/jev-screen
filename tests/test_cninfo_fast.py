"""sync-cninfo fast mode (no network anywhere: fake clients, or REAL http.Clients on a fake latency transport).

Rules under test (docs/DATA_RULES.md, CNINFO):
- bulk listing: stock='' date-range query paged on rate_key 'cninfo' (>= 1 s apart), every page saved raw, one
  'list_pass' snapshot; stops on hasMore=false / short page / empty or already-seen page / page guard
- rows grouped by orgId + secCode; a company settled by the window gets exactly the per-company selection; others
  fall back to the per-company query, capped (the rest: 'no_annual_report_in_window', no crawl_state row); an
  incomplete listing lifts the cap
- PDFs on rate_key 'cninfo-static': worker pool, starts >= 0.25 s apart and <= 4 per rolling second, while
  'cninfo' stays >= 1 s
- first 429 on a PDF: halt at once, zero new starts on either key, marker written, in-flight drained + flushed,
  BLOCKED line, exit 2 (CLI); SIGTERM: halt, drain, flush, 'interrupted'
- incremental: already-extracted reports are skipped before any PDF request (the listing still runs)
- throughput with 3 workers and ~0.5 s PDF latency is well above 1 company/s
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import math
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_cninfo as tc  # noqa: E402
from jevscreen import cli, guard, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Client, Response  # noqa: E402
from jevscreen.sources import cninfo  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BULK_PAGE = json.loads((FIXTURES / "cninfo_bulk_listing_page.json").read_bytes())
SINCE = dt.date(2025, 8, 26)                 # 13 months before 2026-09-26 (the measured live window)
T_2026_04_20 = 1776614400000                 # 2026-04-20 00:00 +08:00
EPS = 1e-6                                   # limiter slots are exact monotonic arithmetic
MS_DAY = 86_400_000


def ann(code: str, org: str, title: str, aid: str, ms: int, size: int = 100) -> dict:
    return {"secCode": code, "secName": code, "orgId": org, "announcementId": aid, "announcementTitle": title,
            "announcementTime": ms, "adjunctUrl": f"finalpage/{aid}.PDF", "adjunctSize": size, "adjunctType": "PDF"}


def pdf_url(aid: str) -> str:
    return cninfo.pdf_url(f"finalpage/{aid}.PDF")


def page(rows: list[dict], *, has_more: bool | None = None, total: int | None = None,
         total_pages: int | None = None) -> bytes:
    d: dict = {"announcements": rows, "totalAnnouncement": total if total is not None else len(rows),
               "totalpages": total_pages if total_pages is not None else 0}
    if has_more is not None:
        d["hasMore"] = has_more
    return json.dumps(d, ensure_ascii=False).encode()


def in_window(rows: list[dict], since: dt.date = SINCE) -> list[dict]:
    t0 = dt.datetime.combine(since, dt.time(), cninfo.CN_TZ).timestamp() * 1000
    return [r for r in rows if int(r["announcementTime"]) >= t0]


def full_page(rows: list[dict], **kw) -> bytes:
    """A full page (QUERY_PAGE_SIZE rows): `rows` padded with rows of companies outside the test universe."""
    pad = [summary_rows(1000 + i)[0] for i in range(cninfo.QUERY_PAGE_SIZE - len(rows))]
    return page(rows + pad, **kw)


def code_of_pdf(url: str) -> str:
    """SZSE security id of a synthetic summary PDF url (.../S0000i.PDF -> SZSE:30000i)."""
    return f"SZSE:30{url.rsplit('/', 1)[1][2:6]}"


def paginate(rows: list[dict], size: int = cninfo.QUERY_PAGE_SIZE) -> list[bytes]:
    chunks = [rows[i:i + size] for i in range(0, len(rows), size)] or [[]]
    return [page(c, has_more=i < len(chunks) - 1, total=len(rows), total_pages=len(chunks))
            for i, c in enumerate(chunks)]


def company(i: int) -> tuple[str, str]:
    """Synthetic SZSE company i: (code, orgId)."""
    return f"30{i:04d}", f"99000{i:05d}"


def summary_rows(i: int) -> list[dict]:
    code, org = company(i)
    return [ann(code, org, f"C{i}2025年年度报告摘要", f"S{i:05d}", T_2026_04_20 - i * 1000),
            ann(code, org, f"C{i}2025年年度报告", f"F{i:05d}", T_2026_04_20 - i * 1000, size=900)]


# =========================================================================== fakes


class FastFake:
    """http.Client stand-in without a limiter: GET url -> bytes | int | exception; POST listing (stock='') by pageNum
    from `pages` (the last page repeats beyond the end unless `beyond` is given); POST per-company by code."""

    thread_safe = True

    def __init__(self, gets: dict | None = None, pages: list | None = None, posts: dict | None = None,
                 beyond: bytes | int | None = None, pdf_default: bytes | int = tc.SUMMARY_PDF):
        self.gets, self.pages, self.posts, self.beyond = dict(gets or {}), list(pages or []), dict(posts or {}), beyond
        self.pdf_default = pdf_default
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.forms: list[dict] = []
        self.rate_keys: list[tuple[str, str | None]] = []
        self.min_interval_s, self.requests_made = 1.0, 0
        self.halt = threading.Event()

    @staticmethod
    def _answer(url: str, item) -> Response:
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return Response(url, item, b"", {}, 0.0)
        return Response(url, 200, item, {}, 0.0)

    def get(self, url: str, **kw) -> Response:
        with self.lock:
            self.calls.append(url)
            self.rate_keys.append((url, kw.get("rate_key")))
            self.requests_made += 1
        default = self.pdf_default if url.startswith("http://static.") else 404
        return self._answer(url, self.gets.get(url, default))

    def post_form(self, url: str, form: dict, headers=None, rate_key=None) -> Response:
        with self.lock:
            self.forms.append(dict(form))
            self.rate_keys.append((url, rate_key))
            self.requests_made += 1
            code = form["stock"].split(",")[0]
            self.calls.append(f"LIST p{form['pageNum']}" if not code else f"POST {code}")
        if not code:
            n = int(form["pageNum"])
            if n <= len(self.pages):
                return self._answer(url, self.pages[n - 1])
            return self._answer(url, self.beyond if self.beyond is not None else self.pages[-1])
        return self._answer(url, self.posts.get(code, 404))

    def pdf_calls(self) -> list[str]:
        return [c for c in self.calls if c.startswith("http://static.")]


class _Resp:
    def __init__(self, status: int, body: bytes, headers: dict):
        self.status, self.headers = status, headers
        self._b = io.BytesIO(body)

    def read(self, n: int | None = None) -> bytes:
        return self._b.read() if n is None or n < 0 else self._b.read(n)

    def close(self) -> None:
        pass


class Transport:
    """Opener for REAL http.Clients (both CNINFO clients share it): each open() is a request start, recorded as
    (time, method, url, form, halt already set?); PDFs take `pdf_latency` s, other requests `latency` s."""

    def __init__(self, *, stock_list: bytes, pages: list[bytes], pdfs: dict[str, object] | None = None,
                 posts: dict[str, bytes] | None = None, latency: float = 0.05, pdf_latency: float = 0.5,
                 hook=None):
        self.stock_list, self.pages, self.pdfs, self.posts = stock_list, pages, dict(pdfs or {}), dict(posts or {})
        self.latency, self.pdf_latency, self.hook = latency, pdf_latency, hook
        self.lock = threading.Lock()
        self.starts: list[tuple[float, str, str, dict | None, bool]] = []
        self.raised_at: dict[str, float] = {}
        self.halts: list[threading.Event] = []
        self.active = self.max_active = 0

    def open(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        form = dict(urllib.parse.parse_qsl(req.data.decode(), keep_blank_values=True)) if req.data else None
        is_pdf = url.startswith("http://static.")
        with self.lock:
            self.starts.append((time.monotonic(), method, url, form, any(h.is_set() for h in self.halts)))
            if is_pdf:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
        try:
            if self.hook:
                self.hook(url)
            time.sleep(self.pdf_latency if is_pdf else self.latency)
            if url == cninfo.STOCK_LIST_URL:
                status, body, ctype = 200, self.stock_list, "application/json"
            elif form is not None and not form.get("stock"):
                n = int(form["pageNum"])
                body = self.pages(form) if callable(self.pages) else self.pages[min(n, len(self.pages)) - 1]
                status, ctype = 200, "application/json"
            elif form is not None:
                status, body, ctype = (200, self.posts[form["stock"].split(",")[0]], "application/json") \
                    if form["stock"].split(",")[0] in self.posts else (404, b"", "text/plain")
            else:
                item = self.pdfs.get(url, tc.SUMMARY_PDF)
                status, body, ctype = (item, b"", "text/plain") if isinstance(item, int) else \
                    (200, item, "application/pdf")
            if status >= 400:
                with self.lock:
                    self.raised_at[url] = time.monotonic()
                raise urllib.error.HTTPError(url, status, "err", {"Content-Type": ctype}, io.BytesIO(body))
            return _Resp(status, body, {"Content-Type": ctype})
        finally:
            if is_pdf:
                with self.lock:
                    self.active -= 1

    def pdf_starts(self) -> list[tuple]:
        return [s for s in self.starts if s[2].startswith("http://static.")]

    def www_starts(self) -> list[tuple]:
        return [s for s in self.starts if not s[2].startswith("http://static.")]


def recording(client: Client) -> list[tuple[str, float]]:
    """Wrap client._wait: every limiter slot the client claims, as (rate key, monotonic time)."""
    slots: list[tuple[str, float]] = []
    lock = threading.Lock()
    claim = client._wait

    def wait(host, url=None):
        t = claim(host, url)
        with lock:
            slots.append((host, t))
        return t
    client._wait = wait
    return slots


def real_clients(transport: Transport) -> tuple[Client, Client, list, list]:
    c = Client(user_agent="test", min_interval_s=1.0, timeout_s=10.0)
    c._opener = transport
    pdf = cninfo.static_client_for(c)
    transport.halts.append(c.halt)
    return c, pdf, recording(c), recording(pdf)


def max_in_window(times: list[float], window: float) -> int:
    t = sorted(times)
    best, j = 0, 0
    for i in range(len(t)):
        while t[i] - t[j] >= window:
            j += 1
        best = max(best, i - j + 1)
    return best


def gaps(times: list[float]) -> list[float]:
    t = sorted(times)
    return [b - a for a, b in zip(t, t[1:])]


# =========================================================================== listing, grouping, selection


class TestListing(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def listing(self, fake: FastFake, max_pages: int = cninfo.MAX_LIST_PAGES) -> cninfo.Listing:
        return cninfo.list_window(fake, self.cfg, cninfo.Listing(SINCE, dt.date(2026, 9, 26)), max_pages=max_pages)

    def rows(self, n: int, start: int = 0) -> list[dict]:
        return [r for i in range(start, start + n) for r in summary_rows(i)[:1]]

    def test_form_raw_pages_and_has_more(self):
        pages = paginate(self.rows(75))                          # 30 + 30 + 15
        fake = FastFake(pages=pages)
        lst = self.listing(fake)
        self.assertEqual((lst.pages, len(lst.announcements), lst.stop, lst.complete), (3, 75, "has_more_false", True))
        self.assertEqual(fake.forms[0], {"stock": "", "tabName": "fulltext", "pageSize": "30", "pageNum": "1",
                                         "column": "szse", "category": "category_ndbg_szsh;",
                                         "seDate": "2025-08-26~2026-09-26"})
        self.assertEqual([f["pageNum"] for f in fake.forms], ["1", "2", "3"])
        self.assertTrue(all(k == cninfo.RATE_KEY for _, k in fake.rate_keys))
        self.assertEqual(len(lst.manifest), 3)
        for m, body in zip(lst.manifest, pages):
            self.assertEqual(Path(m["raw_path"]).read_bytes(), body)            # each page saved raw
            self.assertIn("list-p000", Path(m["raw_path"]).name)

    def test_short_page_without_has_more(self):
        rows = self.rows(40)
        pages = [page(rows[:30]), page(rows[30:])]              # no hasMore key at all
        lst = self.listing(FastFake(pages=pages))
        self.assertEqual((lst.pages, len(lst.announcements), lst.stop, lst.complete), (2, 40, "short_page", True))

    def test_repeated_last_page_stops(self):
        rows = self.rows(60)
        pages = [page(rows[:30], has_more=True), page(rows[30:], has_more=True)]   # server keeps saying hasMore
        fake = FastFake(pages=pages)                            # and repeats page 2 for ever
        lst = self.listing(fake)
        self.assertEqual((lst.pages, len(lst.announcements), lst.stop, lst.complete), (3, 60, "duplicate_page", True))
        self.assertEqual(len(fake.forms), 3)

    def test_early_duplicate_page_is_incomplete(self):
        rows = self.rows(30)
        lst = self.listing(FastFake(pages=[page(rows, has_more=True, total=150, total_pages=5)]))
        self.assertEqual((lst.stop, lst.complete), ("duplicate_page", False))

    def test_shifted_page_is_deduplicated(self):
        rows = self.rows(50)
        # a new announcement pushed the listing by 5 rows between page 1 and page 2
        pages = [page(rows[:30], has_more=True), page(rows[25:50], has_more=False)]
        lst = self.listing(FastFake(pages=pages))
        self.assertEqual((len(lst.announcements), len({cninfo._ann_key(a) for a in lst.announcements})), (50, 50))

    def test_empty_page_and_page_guard(self):
        lst = self.listing(FastFake(pages=[page(self.rows(30), has_more=True), page([], has_more=True)]))
        self.assertEqual((lst.stop, lst.complete, len(lst.announcements)), ("empty_page", True, 30))

        class Endless(FastFake):       # no totalAnnouncement / totalpages at all: only the hard guard
            def post_form(self, url, form, headers=None, rate_key=None):
                n = int(form["pageNum"])
                self.calls.append(n)
                rows = [r for i in range(n * 30, n * 30 + 30) for r in summary_rows(i)[:1]]
                return Response(url, 200, json.dumps({"announcements": rows, "hasMore": True}).encode(), {}, 0.0)
        fake = Endless()
        lst = self.listing(fake, max_pages=5)
        self.assertEqual((lst.stop, lst.complete, lst.pages, fake.calls), ("page_limit", False, 5, [1, 2, 3, 4, 5]))
        fake = Endless()
        lst = cninfo.list_window(fake, self.cfg, cninfo.Listing(SINCE, dt.date(2026, 9, 26)))
        # without totals: CNINFO's own 100-page cap ends it (page 101 is never requested), incomplete
        self.assertEqual((len(fake.calls), lst.stop, lst.complete), (cninfo.PAGE_CAP_PAGES, "page_cap", False))

    def test_total_bounds_the_guard(self):
        class Endless(FastFake):
            def post_form(self, url, form, headers=None, rate_key=None):
                n = int(form["pageNum"])
                self.calls.append(n)
                return Response(url, 200, page([r for i in range(n * 30, n * 30 + 30) for r in summary_rows(i)[:1]],
                                               has_more=True, total=100, total_pages=3), {}, 0.0)
        fake = Endless()
        lst = self.listing(fake)
        # the page count is ceil(100 / 30) = 4 (the server's totalpages 3 is floor): guard at 4 + 3
        self.assertEqual((lst.stop, len(fake.calls), lst.expected_pages()), ("page_limit", 7, 4))

    def test_totalpages_is_floor_and_rows_are_counted(self):
        rows = self.rows(41)
        # live semantics: total 41 -> totalpages 1, but there are 2 pages; page 2 comes back empty
        lst = self.listing(FastFake(pages=[page(rows[:30], has_more=True, total=41, total_pages=1),
                                           page([], has_more=False, total=41, total_pages=1)]))
        self.assertEqual((lst.stop, lst.complete, lst.shortfall, len(lst.announcements)),
                         ("empty_page", False, 11, 30))
        # the real last page arrives: complete
        lst = self.listing(FastFake(pages=[page(rows[:30], has_more=True, total=41, total_pages=1),
                                           page(rows[30:], has_more=False, total=41, total_pages=1)]))
        self.assertEqual((lst.stop, lst.complete, lst.shortfall), ("has_more_false", True, None))
        # a row lost at a page boundary (same-day ties re-ordered): hasMore=false but fewer rows than reported
        lst = self.listing(FastFake(pages=[page(rows[:30], has_more=True, total=41),
                                           page(rows[29:40], has_more=False, total=41)]))
        self.assertEqual((lst.complete, lst.shortfall, lst.error), (False, 1, "rows_missing:40_of_41"))
        # a new announcement while paging (rows pushed down by one): nothing lost, complete
        lst = self.listing(FastFake(pages=[page(rows[:30], has_more=True, total=41),
                                           page(rows[29:41], has_more=False, total=42)]))
        self.assertEqual((lst.complete, len(lst.announcements)), (True, 41))

    def test_errors_leave_the_listing_incomplete(self):
        lst = self.listing(FastFake(pages=[page(self.rows(30), has_more=True), 500]))
        self.assertEqual((lst.stop, lst.error, lst.complete, lst.pages), ("error", "list_http_500", False, 1))
        lst = self.listing(FastFake(pages=[b"<html>"]))
        self.assertEqual((lst.error, lst.complete), ("list_bad_json", False))

    def test_fixture_page_groups_by_org_and_code(self):
        lst = self.listing(FastFake(pages=[json.dumps(BULK_PAGE, ensure_ascii=False).encode()]))
        self.assertEqual((lst.stop, lst.complete, lst.total_reported), ("has_more_false", True, 3))
        by_org, by_code = lst.groups()
        self.assertEqual(sorted(by_org), [tc.ORG_309386, tc.ORG_609519])
        self.assertEqual(sorted(by_code), ["309386", "609519"])
        rows = cninfo.company_announcements(by_org, by_code, tc.ORG_309386, ["309386"])
        self.assertEqual([r["announcementId"] for r in rows], ["9225102237", "9225102236"])
        # a row known only by its secCode (orgId missing) still reaches the company, once
        extra = dict(BULK_PAGE["announcements"][1], orgId="", announcementId="X1")
        by_code["309386"].append(extra)
        by_code["200386"] = [dict(extra, secCode="200386", announcementId="X2")]
        rows = cninfo.company_announcements(by_org, by_code, tc.ORG_309386, ["309386", "200386"])
        self.assertEqual(sorted(r["announcementId"] for r in rows), ["9225102236", "9225102237", "X1", "X2"])


class TestSelectionEquivalence(unittest.TestCase):
    def test_fixture_query_selection_is_identical(self):
        full = tc.QUERY["announcements"]
        rows = in_window(full)
        self.assertEqual(len(rows), 2)                           # only the 2025 report + summary are in the window
        self.assertTrue(cninfo.window_settles(rows, SINCE))
        for kind in cninfo.KINDS:
            for as_of in (None, dt.date(2026, 9, 26), dt.date(2027, 6, 1)):
                a = cninfo.select_annual_reports(rows, kind, as_of=as_of)
                b = cninfo.select_annual_reports(full, kind, as_of=as_of)
                self.assertEqual(a, b, (kind, as_of))

    def test_live_title_fixtures_selection_is_identical(self):
        # every live-format case of test_cninfo that the window settles selects the same documents
        cases = [tc.query_609519(), tc.query_609519(include_summary=False)]
        for raw in cases:
            full = json.loads(raw)["announcements"]
            rows = in_window(full)
            self.assertTrue(cninfo.window_settles(rows, SINCE))
            for kind in cninfo.KINDS:
                self.assertEqual(cninfo.select_annual_reports(rows, kind), cninfo.select_annual_reports(full, kind))

    def test_what_the_window_does_not_settle(self):
        full = tc.QUERY["announcements"]
        self.assertFalse(cninfo.window_settles([], SINCE))                       # nothing in the window
        self.assertFalse(cninfo.window_settles(in_window(full, dt.date(2026, 5, 1)), dt.date(2026, 5, 1)))
        stale = [ann("300999", "o", "2024年年度报告（更新后）", "R1", T_2026_04_20 - 200 * MS_DAY)]
        self.assertFalse(cninfo.window_settles(stale, SINCE))                   # only an older year: fall back
        unparsed = [ann("300999", "o", "XX2025年年度报告及摘要", "U1", T_2026_04_20)]
        self.assertTrue(cninfo.window_settles(unparsed, SINCE))                 # the guard sees the same year
        self.assertEqual(cninfo.select_annual_reports(unparsed), [])

    def test_helpers(self):
        self.assertEqual(cninfo.months_before(dt.date(2026, 9, 26), 13), dt.date(2025, 8, 26))
        self.assertEqual(cninfo.months_before(dt.date(2026, 3, 31), 1), dt.date(2026, 2, 28))
        self.assertEqual(cninfo.months_before(dt.date(2026, 1, 15), 13), dt.date(2024, 12, 15))
        self.assertEqual(cninfo.default_since(dt.date(2026, 9, 26)), SINCE)
        self.assertEqual(cninfo.parse_since("2025-09-01"), dt.date(2025, 9, 1))
        with self.assertRaises(ValueError):
            cninfo.parse_since("09/2025")
        self.assertEqual([cninfo.clamp_workers(w) for w in (None, 0, 1, 3, 4, 9, "x")], [3, 1, 1, 3, 4, 4, 3])
        c = Client(user_agent="ua", min_interval_s=1.0)
        s = cninfo.static_client_for(c)
        self.assertIs(s.halt, c.halt)
        self.assertIs(s._opener, c._opener)
        self.assertEqual((s.min_interval_s, s.max_per_window, c.min_interval_s, c.max_per_window), (0.25, 4, 1.0, None))


# =========================================================================== sync (fast mode)


class FastBase(tc.SyncBase):
    def run_fast(self, client, **kw) -> tuple[dict, str]:
        kw.setdefault("since", SINCE)
        kw.setdefault("per_company", False)
        kw.setdefault("progress_every", 0)
        kw.setdefault("list_min_queue", 0)          # tiny test universes: exercise the listing anyway
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            summary = cninfo.sync(self.cfg, client, **kw)
        return summary, err.getvalue()

    def add_companies(self, n: int) -> bytes:
        """n synthetic SZSE companies (market cap descending) and their stock list."""
        rows = []
        for i in range(n):
            code, org = company(i)
            self.add(f"SZSE:{code}", 1e10 - i)
            rows.append({"code": code, "orgId": org, "zwjc": f"C{i}", "category": "A股", "pinyin": "c"})
        return json.dumps({"stockList": rows}, ensure_ascii=False).encode()

    def store_doc(self, i: int, year: int, *, extractor: str = f"{cninfo.EXTRACTOR_VERSION}/pymupdf",
                  form: str = "annual_report_summary", text: bool = True) -> None:
        code, org = company(i)
        row = {c: None for c in cninfo._DOC_COLS}
        row.update(doc_id=f"{cninfo.SOURCE_ID}:{org}:D{i}{year}{form}:business", security_id=f"SZSE:{code}",
                   source_id=cninfo.SOURCE_ID, cik=org, form=form, section="business", accession=f"D{i}{year}",
                   report_date=dt.date(year, 12, 31), url=pdf_url(f"D{i}{year}{form}"),
                   text_path=str(Path(self.tmp.name) / f"D{i}.txt") if text else None, extractor=extractor,
                   fetched_at=store.now_utc())
        with store.session(self.cfg) as con:
            store.upsert_many(con, "documents", cninfo._DOC_COLS, [[row[c] for c in cninfo._DOC_COLS]])

    def docs(self) -> list[tuple]:
        return self.q("SELECT doc_id, security_id, form, url, report_date, filing_date, raw_sha256, text_sha256, "
                      "text_chars, extractor, extract_note FROM documents ORDER BY doc_id")


class TestFastSync(FastBase):
    def standard_fake(self, **kw) -> FastFake:
        c = tc.standard_client()
        rows = [dict(r, secCode="309386", orgId=tc.ORG_309386) for r in tc.QUERY["announcements"]] + \
            [dict(r, secCode="609519", orgId=tc.ORG_609519) for r in json.loads(tc.query_609519())["announcements"]]
        rows = in_window(rows)
        return FastFake(c.gets, paginate(rows), c.posts, **kw)

    def test_same_documents_as_the_per_company_path(self):
        self.add_standard()
        stale_code, stale_org = "000999", "gssz0000999"
        # a company whose newest report is outside the window (stale) is settled by the per-company fallback
        stale_q = json.dumps({"announcements": [
            {**tc.ann("老公司2023年年度报告摘要", "finalpage/2024-04-01/3000000001.PDF", T_2026_04_20 - 750 * MS_DAY),
             "secCode": stale_code, "orgId": stale_org}]}, ensure_ascii=False).encode()
        stock = json.loads(tc.STOCK_LIST_RAW)
        stock["stockList"].append({"code": stale_code, "orgId": stale_org, "zwjc": "老公司", "category": "A股"})
        stock_raw = json.dumps(stock, ensure_ascii=False).encode()
        self.add(f"SZSE:{stale_code}", 5e8)
        stale_pdf = "http://static.cninfo.com.cn/finalpage/2024-04-01/3000000001.PDF"

        def fake(per_company: bool):
            f = self.standard_fake()
            f.gets.update({cninfo.STOCK_LIST_URL: stock_raw, stale_pdf: tc.SUMMARY_PDF})
            f.posts[stale_code] = stale_q
            return f
        f1 = fake(True)
        s1, _ = self.run_fast(f1, per_company=True)
        docs1, states1 = self.docs(), self.states()
        # fresh store, fast mode
        self.tearDown()
        self.setUp()
        self.add_standard()
        self.add(f"SZSE:{stale_code}", 5e8)
        f2 = fake(False)
        s2, _ = self.run_fast(f2)
        self.assertEqual((s1["status"], s2["status"]), ("ok", "ok"))
        norm = lambda rows: [tuple(r) for r in rows]   # noqa: E731
        self.assertEqual(norm(self.docs()), norm(docs1))
        self.assertEqual({k: (v[0], v[1], v[3]) for k, v in self.states().items()},
                         {k: (v[0], v[1], v[3]) for k, v in states1.items()})
        self.assertEqual(set(f2.pdf_calls()), set(f1.pdf_calls()))
        # the fast run queried only the stale company; everything else came from one listing page
        self.assertEqual([c for c in f2.calls if c.startswith(("POST", "LIST"))], ["LIST p1", f"POST {stale_code}"])
        self.assertEqual((s2["list_pages"], s2["fallback_queries"], s2["mode"]), (1, 1, "fast"))
        self.assertTrue(all(k == cninfo.STATIC_RATE_KEY for u, k in f2.rate_keys if u.startswith("http://static.")))
        self.assertTrue(all(k == cninfo.RATE_KEY for u, k in f2.rate_keys if not u.startswith("http://static.")))
        snap = self.q("SELECT kind, rows, status, note FROM snapshots WHERE source_id = ? AND kind = 'list_pass'",
                      [cninfo.SOURCE_ID])
        self.assertEqual(snap, [("list_pass", 4, "ok", "stop=has_more_false;pages=1;rows=4")])
        run = self.last_run()
        self.assertEqual(run[0], "ok")
        self.assertEqual(json.loads(run[2])["list_pages"], 1)

    def test_fallback_cap_and_uncapped_incomplete_listing(self):
        stock = self.add_companies(6)
        rows = summary_rows(0)                                   # only company 0 is in the window
        posts = {company(i)[0]: page(summary_rows(i)) for i in range(1, 6)}
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate(rows), posts)
        s, err = self.run_fast(f, max_fallback=2)
        # 3 of 6 companies left unchecked: more than 5% -> 'partial' with a warning
        self.assertEqual((s["fallback_queries"], s["no_annual_report_in_window"], s["status"]), (2, 3, "partial"))
        self.assertEqual((s["skipped_not_in_listing"], s["skipped_older_year_only"]), (3, 0))
        self.assertIn("WARNING: 3 of 6 companies were not checked", err)
        self.assertEqual(self.last_run()[0], "partial")
        self.assertEqual([c for c in f.calls if c.startswith("POST")], ["POST 300001", "POST 300002"])  # by market cap
        self.assertEqual(set(self.states()), {f"SZSE:{company(i)[0]}" for i in range(3)})   # the rest: no row
        self.assertEqual(s["no_annual_report_in_window_codes"], ["300003", "300004", "300005"])
        self.assertEqual(s["status_ok"], 3)
        # listing cut short by an HTTP error: it proves nothing, not even for company 0 whose rows it saw (a
        # revision or the full report could be on the missed page), so every company is queried, no cap
        posts[company(0)[0]] = page(summary_rows(0))
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, [full_page(rows, has_more=True), 500], posts)
        s, err = self.run_fast(f, max_fallback=2, refresh=True)
        self.assertEqual((s["list_complete"], s["fallback_queries"], s["no_annual_report_in_window"]), (False, 6, 0))
        self.assertEqual(s["status"], "ok")
        self.assertIn("listing unusable", err)
        snap = self.q("SELECT status, note FROM snapshots WHERE kind = 'list_pass' ORDER BY fetched_at DESC LIMIT 1")
        self.assertEqual(snap, [("partial", "stop=error;error=list_http_500;pages=1;rows=0")])

    def test_incremental_skip_before_any_pdf_request(self):
        stock = self.add_companies(5)
        rows = [r for i in range(5) for r in summary_rows(i)]
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate(rows))
        s, _ = self.run_fast(f)
        self.assertEqual((s["status_ok"], len(f.pdf_calls())), (5, 5))
        f2 = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate(rows))
        s2, _ = self.run_fast(f2)
        self.assertEqual(f2.pdf_calls(), [])                      # nothing downloaded again
        self.assertEqual((s2["skipped_unchanged"], s2["list_pages"]), (5, 1))   # the listing still ran
        self.assertIn("LIST p1", f2.calls)
        # a new revision (another adjunctUrl) of one company is fetched; the others stay skipped
        rev = ann(company(2)[0], company(2)[1], "C22025年年度报告摘要（修订后）", "S00002R", T_2026_04_20 + MS_DAY)
        f3 = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate([rev] + rows))
        s3, _ = self.run_fast(f3)
        self.assertEqual((f3.pdf_calls(), s3["skipped_unchanged"]), ([pdf_url("S00002R")], 4))

    def test_first_block_on_listing_stops_before_any_pdf(self):
        stock = self.add_companies(3)
        seen = []
        f = FastFake({cninfo.STOCK_LIST_URL: stock},
                     [full_page(summary_rows(0), has_more=True), Blocked(cninfo.QUERY_URL, 429, "http_429")])
        s, err = self.run_fast(f, on_blocked=seen.append)
        self.assertEqual((s["status"], s["blocked_at"], s["blocked_status"]), ("blocked", "listing", 429))
        self.assertEqual(f.pdf_calls(), [])
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-cninfo"))
        self.assertEqual(sum(ln.startswith("[cninfo] BLOCKED url=") for ln in err.splitlines()), 1)
        self.assertEqual(self.q("SELECT status FROM snapshots WHERE kind = 'list_pass'"), [("partial",)])

    def test_consecutive_pdf_errors_stop(self):
        stock = self.add_companies(9)
        rows = [r for i in range(9) for r in summary_rows(i)[:1]]
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate(rows), pdf_default=404)   # every PDF -> 404
        s, _ = self.run_fast(f, workers=1)
        self.assertEqual((s["stopped_reason"], s["status"]), ("consecutive_errors", "stopped_errors"))
        self.assertEqual(s["status_error"], cninfo.MAX_CONSECUTIVE_ERRORS)


class TestFastWorkers(FastBase):
    """REAL http.Clients on the latency transport."""

    def setup_universe(self, n: int, *, pdfs: dict | None = None, hook=None, pdf_latency: float = 0.5
                       ) -> tuple[Transport, Client, Client, list, list]:
        stock = self.add_companies(n)
        rows = [r for i in range(n) for r in summary_rows(i)[:1]]         # summaries only: one PDF per company
        t = Transport(stock_list=stock, pages=paginate(rows), pdfs=pdfs, pdf_latency=pdf_latency, hook=hook)
        c, pdf, www_slots, pdf_slots = real_clients(t)
        return t, c, pdf, www_slots, pdf_slots

    def test_spacing_window_and_throughput(self):
        n = 24
        t, c, pdf, www_slots, pdf_slots = self.setup_universe(n, pdf_latency=0.5)
        s, err = self.run_fast(c, pdf_client=pdf, workers=3, progress_every=8)
        self.assertEqual((s["status"], s["status_ok"], s["workers"]), ("ok", n, 3))
        static = [tt for k, tt in pdf_slots if k == cninfo.STATIC_RATE_KEY]
        www = [tt for k, tt in www_slots if k == cninfo.RATE_KEY]
        self.assertEqual((len(static), len(www)), (n, 2))                # stock list + one listing page
        self.assertEqual({k for k, _ in www_slots}, {cninfo.RATE_KEY})
        self.assertEqual({k for k, _ in pdf_slots}, {cninfo.STATIC_RATE_KEY})
        self.assertGreaterEqual(min(gaps(static)), cninfo.STATIC_MIN_INTERVAL_S - EPS)
        self.assertLessEqual(max_in_window(static, 1.0), cninfo.STATIC_MAX_PER_WINDOW)
        self.assertGreaterEqual(min(gaps(www)), 1.0 - EPS)
        self.assertGreaterEqual(t.max_active, 2)                          # downloads overlap
        self.assertGreater(s["companies_per_s"], 2.0)                     # well above 1 company/s
        self.assertAlmostEqual(s["mean_pdf_kb"], len(tc.SUMMARY_PDF) / 1024, places=0)
        self.assertEqual((s["pdfs"], s["list_pages"], s["fallback_queries"]), (n, 1, 0))
        self.assertEqual(s["requests"], n + 2)
        lines = [ln for ln in err.splitlines() if ln.startswith(f"[cninfo] ") and "/24 {" in ln]
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[-1], r"^\[cninfo\] 24/24 \{'ok': 24, .*\} companies/s=\d+\.\d\d$")

    def test_fallback_queries_keep_one_second_while_pdfs_run(self):
        n = 8
        stock = self.add_companies(n)
        rows = [r for i in range(0, n, 2) for r in summary_rows(i)[:1]]   # odd companies need their own query
        posts = {company(i)[0]: page(summary_rows(i)[:1]) for i in range(1, n, 2)}
        t = Transport(stock_list=stock, pages=paginate(rows), posts=posts, pdf_latency=0.5)
        c, pdf, www_slots, pdf_slots = real_clients(t)
        s, _ = self.run_fast(c, pdf_client=pdf, workers=3)
        self.assertEqual((s["status_ok"], s["fallback_queries"]), (n, n // 2))
        www = [tt for _, tt in www_slots]
        self.assertEqual(len(www), 2 + n // 2)
        self.assertGreaterEqual(min(gaps(www)), 1.0 - EPS)
        static = [tt for _, tt in pdf_slots]
        self.assertGreaterEqual(min(gaps(static)), cninfo.STATIC_MIN_INTERVAL_S - EPS)
        self.assertLessEqual(max_in_window(static, 1.0), cninfo.STATIC_MAX_PER_WINDOW)

    def test_first_429_on_a_pdf_halts_everything(self):
        n = 14
        blocked_url = pdf_url("S00004")
        t, c, pdf, www_slots, pdf_slots = self.setup_universe(n, pdfs={blocked_url: 429}, pdf_latency=0.4)
        seen = []
        s, err = self.run_fast(c, pdf_client=pdf, workers=3, on_blocked=seen.append)
        self.assertEqual((s["status"], s["stopped_reason"]), ("blocked", "blocked"))
        self.assertEqual((s["blocked_url"], s["blocked_status"], s["blocked_reason"], s["blocked_at"]),
                         (blocked_url, 429, "http_429", "300004"))
        self.assertEqual([(e.url, e.status) for e in seen], [(blocked_url, 429)])
        raised = t.raised_at[blocked_url]
        self.assertEqual([st for st in t.starts if st[0] > raised], [])   # zero new starts on either key
        self.assertEqual(sum(st[4] for st in t.starts), 0)                # none started with halt set
        pdf_urls = [st[2] for st in sorted(t.pdf_starts())]
        self.assertLessEqual(len(pdf_urls), pdf_urls.index(blocked_url) + 3)   # at most workers - 1 after it
        self.assertLess(len(pdf_urls), n)
        marker = guard.recent_block_marker(self.cfg, "sync-cninfo")
        self.assertIsNotNone(marker)
        self.assertIn(blocked_url, marker["note"])
        st = self.states()
        started = {code_of_pdf(u) for u in pdf_urls}
        self.assertEqual(set(st), started)                                # every started PDF recorded (drained)
        self.assertEqual(st["SZSE:300004"][:2], ("blocked", 429))
        self.assertEqual({v[0] for k, v in st.items() if k != "SZSE:300004"}, {"ok"})
        self.assertEqual(self.last_run()[0], "blocked")
        lines = err.splitlines()
        self.assertEqual(sum(ln.startswith("[cninfo] BLOCKED url=") for ln in lines), 1)
        self.assertEqual(lines[-1], f"[cninfo] BLOCKED url={blocked_url} status=429 reason=http_429")
        self.assertIn("draining", err)

    def test_sigterm_drains_in_flight_and_flushes(self):
        n = 12
        trigger = pdf_url("S00003")

        def term(url: str) -> None:
            if url == trigger:
                os.kill(os.getpid(), signal.SIGTERM)
        t, c, pdf, _, _ = self.setup_universe(n, hook=term, pdf_latency=0.4)
        before = signal.getsignal(signal.SIGTERM)
        with cli.sigterm_as_interrupt():
            s, _ = self.run_fast(c, pdf_client=pdf, workers=3)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual((s["status"], s["stopped_reason"]), ("interrupted", "interrupted"))
        self.assertEqual(sum(st[4] for st in t.starts), 0)                # nothing started after the halt
        pdf_urls = {st[2] for st in t.pdf_starts()}
        self.assertIn(trigger, pdf_urls)
        self.assertLess(len(pdf_urls), n)
        st = self.states()
        self.assertEqual(set(st), {code_of_pdf(u) for u in pdf_urls})     # drained + flushed
        self.assertEqual({v[0] for v in st.values()}, {"ok"})
        self.assertEqual(s["abandoned_in_flight"], 0)
        self.assertEqual(self.last_run()[0], "interrupted")


# =========================================================================== CLI


class TestCli(unittest.TestCase):
    def test_parse_new_options(self):
        p = cli.build_parser()
        a = p.parse_args(["sync-cninfo"])
        self.assertEqual((a.workers, a.since, a.per_company), (3, None, False))
        a = p.parse_args(["sync-cninfo", "--workers", "2", "--since", "2025-09-01", "--per-company"])
        self.assertEqual((a.workers, a.since, a.per_company), (2, dt.date(2025, 9, 1), True))
        for bad in (["sync-cninfo", "--since", "2025/09/01"], ["sync-cninfo", "--workers", "x"],
                    ["sync-edinet", "--workers", "2"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                p.parse_args(bad)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_options_reach_an_adapter_that_takes_them(self):
        seen = {}

        def sync(cfg, client, *, limit=None, codes=None, kind="summary", refresh=False, min_mcap_usd=None,
                 on_blocked=None, per_company=False, workers=3, since=None):
            seen.update(per_company=per_company, workers=workers, since=since, interval=client.min_interval_s)
            return {"status": "ok"}
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"JEVSCREEN_HOME": home, "JEVSCREEN_MIN_INTERVAL_S": "1.0"}), \
                mock.patch.dict(sys.modules, {cli.CNINFO: tc_fake_module(sync)}):
            code, _, _ = self.run_cli("sync-cninfo", "--workers", "9", "--since", "2025-09-01")
            self.assertEqual(code, 0)
            self.assertEqual(seen, {"per_company": False, "workers": 4, "since": dt.date(2025, 9, 1),
                                    "interval": 1.0})
            code, _, _ = self.run_cli("sync-cninfo", "--per-company")
            self.assertEqual((code, seen["per_company"], seen["workers"]), (0, True, 3))

    def test_blocked_pdf_exit_2_marker_and_cooldown(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"JEVSCREEN_HOME": home, "JEVSCREEN_MIN_INTERVAL_S": "1.0"}):
            cfg = Config(home=Path(home))
            base = FastBase()
            base.cfg = cfg
            with store.session(cfg) as con:
                base.snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="test", request=None,
                                                  raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
                                                  duration_s=None)
            stock = base.add_companies(6)
            rows = [r for i in range(6) for r in summary_rows(i)[:1]]
            blocked_url = pdf_url("S00002")
            t = Transport(stock_list=stock, pages=paginate(rows), pdfs={blocked_url: 429}, pdf_latency=0.3)
            with mock.patch("urllib.request.build_opener", return_value=t), \
                    mock.patch.object(cninfo, "default_since", return_value=SINCE), \
                    mock.patch.object(cninfo, "LIST_MIN_QUEUE", 0):
                code, out, err = self.run_cli("sync-cninfo")
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)["status"], "blocked")
                self.assertIn(f"[cninfo] BLOCKED url={blocked_url} status=429 reason=http_429", err)
                raised = t.raised_at[blocked_url]
                self.assertEqual([st for st in t.starts if st[0] > raised], [])
                self.assertIsNotNone(guard.recent_block_marker(cfg, "sync-cninfo"))
                n_starts = len(t.starts)
                code, out, _ = self.run_cli("sync-cninfo")          # the user decides when to resume
                self.assertEqual((code, json.loads(out)["status"], len(t.starts)), (2, "cooldown", n_starts))



# =========================================================================== review fixes


class TestWindowAndTitles(unittest.TestCase):
    def test_default_window_settles_in_the_reporting_season(self):
        fy2025 = [ann("300999", "o", "C2025年年度报告摘要", "S1", T_2026_04_20),
                  ann("300999", "o", "C2025年年度报告", "F1", T_2026_04_20)]
        for today in (dt.date(2027, 2, 10), dt.date(2027, 3, 31), dt.date(2027, 4, 20), dt.date(2027, 4, 30)):
            since = cninfo.default_since(today)
            self.assertEqual((since, cninfo.settle_floor(today)), (dt.date(2026, 1, 1), dt.date(2026, 1, 1)), today)
            # a company that has not yet published FY2026 is still settled by the listing
            self.assertTrue(cninfo.window_settles(fy2025, since), today)
        self.assertEqual(cninfo.default_since(dt.date(2027, 5, 1)), dt.date(2026, 4, 1))
        self.assertEqual(cninfo.settle_floor(dt.date(2027, 5, 1)), dt.date(2027, 1, 1))
        self.assertEqual(cninfo.default_since(dt.date(2026, 9, 26)), SINCE)
        self.assertEqual(cninfo.default_since(dt.date(2027, 1, 15)), dt.date(2025, 12, 15))
        # the plain 13-month window would not have settled it (the bug)
        self.assertFalse(cninfo.window_settles(fy2025, cninfo.months_before(dt.date(2027, 3, 31), 13)))

    def test_titles_prefixed_with_the_own_stock_code(self):
        def rows(code: str, *titles: str) -> list[dict]:
            return [ann(code, "o", t, f"A{i}", T_2026_04_20 - i * 400 * MS_DAY) for i, t in enumerate(titles)]
        sel = cninfo.select_annual_reports(rows("603629", "603629：利通电子2025年年度报告", "利通电子2019年年度报告摘要"),
                                           "full", as_of=dt.date(2026, 9, 26))
        self.assertEqual([(d["year"], d["kind"], d["flags"]) for d in sel], [(2025, "full", [])])
        sel = cninfo.select_annual_reports(rows("600909", "600909：华安证券股份有限公司2020年年度报告"), "full")
        self.assertEqual([(d["year"], d["kind"]) for d in sel], [(2020, "full")])
        # another company's report and another code's prefix stay rejected
        self.assertEqual(cninfo.annual_report_scan(rows("601318", "中国平安：平安银行股份有限公司2025年年度报告摘要"))[0], [])
        self.assertEqual(cninfo.annual_report_scan(rows("601318", "000001：平安银行2025年年度报告"))[0], [])
        no_code = [dict(r, secCode=None) for r in rows("603629", "603629：利通电子2025年年度报告")]
        self.assertEqual(cninfo.annual_report_scan(no_code)[0], [])

    def test_pdf_parsing_is_serialised(self):
        active, peak, lock = [0], [0], threading.Lock()

        def slow(data):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return ["text"]
        with mock.patch.object(cninfo, "_pages_pymupdf", side_effect=slow), \
                mock.patch.object(cninfo, "pdf_backends", return_value=["pymupdf"]):
            threads = [threading.Thread(target=cninfo.pdf_pages, args=(b"%PDF",)) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak[0], 1)

    def test_daemon_pool(self):
        pool = cninfo._DaemonPool(3, "t")
        self.assertTrue(all(t.daemon for t in pool._threads))
        done = []
        for i in range(5):
            pool.submit(done.append, i)
        pool.submit(lambda: 1 / 0)          # a failing task does not kill its worker
        pool.submit(done.append, 5)
        pool.shutdown(wait=True)
        self.assertEqual(sorted(done), [0, 1, 2, 3, 4, 5])


class TestFastReviewFixes(FastBase):
    def test_incomplete_listing_is_not_used_even_for_companies_it_saw(self):
        stock = self.add_companies(1)
        code, _ = company(0)
        # page 1 carries company 0's summary only, page 2 (with its full report) fails
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, [full_page(summary_rows(0)[:1], has_more=True), 500],
                     {code: page(summary_rows(0))})
        s, _ = self.run_fast(f, kind="full")
        self.assertEqual((s["list_complete"], s["fallback_queries"], s["status_ok"]), (False, 1, 1))
        self.assertEqual(f.pdf_calls(), [pdf_url("F00000")])                    # the full report, as per company
        self.assertEqual(self.q("SELECT form FROM documents"), [("annual_report",)])

    def test_small_queue_or_codes_skip_the_listing(self):
        stock = self.add_companies(3)
        posts = {company(i)[0]: page(summary_rows(i)) for i in range(3)}
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, [], posts)
        s, err = self.run_fast(f, list_min_queue=None)                         # default LIST_MIN_QUEUE
        self.assertEqual((s["status_ok"], s["list_pages"], s["fallback_queries"]), (3, 0, 3))
        self.assertEqual(s["list_skipped"], f"queue 3 <= {cninfo.LIST_MIN_QUEUE}")
        self.assertFalse(any(c.startswith("LIST") for c in f.calls))
        self.assertTrue(all(k == cninfo.STATIC_RATE_KEY for u, k in f.rate_keys if u.startswith("http://static.")))
        self.assertIn("listing skipped", err)
        self.assertEqual(self.q("SELECT count(*) FROM snapshots WHERE kind = 'list_pass'"), [(0,)])
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, [], posts)
        s, _ = self.run_fast(f, codes=["300001"], refresh=True)                 # list_min_queue=0, but --codes
        self.assertEqual((s["list_skipped"], s["fallback_queries"], s["queued"]), ("--codes given", 1, 1))
        self.assertFalse(any(c.startswith("LIST") for c in f.calls))

    def test_non_pdf_page_on_static_stops_the_run(self):
        stock = self.add_companies(4)
        rows = [r for i in range(4) for r in summary_rows(i)[:1]]
        bad = pdf_url("S00001")
        f = FastFake({cninfo.STOCK_LIST_URL: stock, bad: b"<html><body>\xe8\xae\xbf\xe9\x97\xae\xe8\xbf\x87\xe4\xba\x8e"
                                                         b"\xe9\xa2\x91\xe7\xb9\x81</body></html>"}, paginate(rows))
        seen = []
        s, err = self.run_fast(f, workers=1, on_blocked=seen.append)
        self.assertEqual((s["status"], s["blocked_url"], s["blocked_status"], s["blocked_reason"]),
                         ("blocked", bad, 200, "non_pdf_body"))
        self.assertTrue(f.halt.is_set())
        self.assertEqual(len(seen), 1)
        self.assertEqual(f.pdf_calls(), [pdf_url("S00000"), bad])              # nothing started after it
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-cninfo"))
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE url = ?", [bad]), [(0,)])   # never 'failed'
        self.assertEqual(self.states()["SZSE:300001"][:2], ("blocked", 200))
        self.assertIn(f"[cninfo] BLOCKED url={bad} status=200 reason=non_pdf_body", err)

    def test_non_pdf_bytes_are_an_error_not_a_failed_extraction(self):
        stock = self.add_companies(3)
        rows = [r for i in range(3) for r in summary_rows(i)[:1]]
        bad = pdf_url("S00001")
        f = FastFake({cninfo.STOCK_LIST_URL: stock, bad: b""}, paginate(rows))
        s, _ = self.run_fast(f, workers=1)
        self.assertEqual((s["status"], s["status_ok"], s["status_error"]), ("ok", 2, 1))
        self.assertEqual(self.states()["SZSE:300001"][0::3], ("error", "pdf_not_pdf"))
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE url = ?", [bad]), [(0,)])   # retried later

    def test_beijing_companies_outside_the_cap_when_the_listing_has_none(self):
        stock = json.loads(self.add_companies(3))
        self.add("BJSE:920001", 1e9)
        stock["stockList"].append({"code": "920001", "orgId": "gfbj0920001", "zwjc": "BJ", "category": "A股"})
        bj_rows = [ann("920001", "gfbj0920001", "BJ2025年年度报告摘要", "B00001", T_2026_04_20)]
        posts = {company(i)[0]: page(summary_rows(i)) for i in range(3)}
        posts["920001"] = page(bj_rows)
        f = FastFake({cninfo.STOCK_LIST_URL: json.dumps(stock, ensure_ascii=False).encode()},
                     paginate(summary_rows(0)), posts)
        s, err = self.run_fast(f, max_fallback=1)
        self.assertIn("no Beijing", err)
        self.assertEqual([c for c in f.calls if c.startswith("POST")], ["POST 300001", "POST 920001"])
        self.assertEqual((s["fallback_queries"], s["no_annual_report_in_window"]), (2, 1))
        self.assertEqual(s["no_annual_report_in_window_codes"], ["300002"])

    def test_known_empty_companies_go_last_in_the_fallback_budget(self):
        stock = self.add_companies(3)
        with store.session(self.cfg) as con:
            store.upsert_many(con, "crawl_state", cninfo._STATE_COLS,
                              [[cninfo.SOURCE_ID, "SZSE:300001", "no_annual_report", 200, 1, store.now_utc(),
                                "no_annual_report"]])
        posts = {company(i)[0]: page(summary_rows(i)) for i in range(3)}
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate(summary_rows(0)), posts)
        s, _ = self.run_fast(f, max_fallback=1)
        self.assertEqual([c for c in f.calls if c.startswith("POST")], ["POST 300002"])   # never-found one last
        self.assertEqual(s["no_annual_report_in_window_codes"], ["300001"])

    def test_crash_after_a_block_is_journaled_blocked(self):
        stock = self.add_companies(3)
        rows = [r for i in range(3) for r in summary_rows(i)[:1]]
        f = FastFake({cninfo.STOCK_LIST_URL: stock, pdf_url("S00001"): Blocked(pdf_url("S00001"), 429, "http_429")},
                     paginate(rows))
        real = guard.mark_blocked
        calls = []

        def flaky(*a, **kw):          # the worker's marker write fails once; the main thread writes it again
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk full")
            return real(*a, **kw)
        err = io.StringIO()
        with mock.patch.object(cninfo.guard, "mark_blocked", side_effect=flaky), \
                contextlib.redirect_stderr(err), self.assertRaises(RuntimeError):
            cninfo.sync(self.cfg, f, since=SINCE, workers=1, progress_every=0, list_min_queue=0)
        self.assertEqual(self.last_run()[0], "blocked")
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-cninfo"))
        self.assertIn(f"[cninfo] BLOCKED url={pdf_url('S00001')} status=429 reason=http_429", err.getvalue())

    def test_since_after_the_floor_warns(self):
        stock = self.add_companies(1)
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate(summary_rows(0)))
        _, err = self.run_fast(f, since=dt.datetime.now(cninfo.CN_TZ).date())
        self.assertIn("WARNING: --since", err)

    def test_timing_fields(self):
        stock = self.add_companies(2)
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate([r for i in range(2) for r in summary_rows(i)[:1]]))
        s, _ = self.run_fast(f)
        self.assertEqual(s["fetched_companies"], 2)
        self.assertIsNotNone(s["list_s"])
        self.assertGreaterEqual(s["total_s"], s["elapsed_s"])


# =========================================================================== adaptive windows (page cap 100)

PAGE_CAP_LIVE = json.loads((FIXTURES / "cninfo_bulk_page_cap_2026-09-26.json").read_bytes())
TODAY = dt.date(2026, 9, 26)


def day_ms(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time(), cninfo.CN_TZ).timestamp() * 1000)


def pad_rows(day: dt.date, n: int, start: int = 0, year: int = 2025) -> list[dict]:
    """n rows on `day` of SSE companies outside the test universes (codes 6xxxxx)."""
    return [ann(f"6{(start + k) % 100000:05d}", f"gssh{start + k:07d}", f"P{start + k}{year}年年度报告摘要",
                f"P{day:%Y%m%d}{start + k:06d}", day_ms(day)) for k in range(n)]


def sz_rows(day: dt.date, n: int, start: int = 0, year: int = 2025) -> list[dict]:
    """n rows on `day` of SZSE companies outside the test universes (codes 0xxxxx)."""
    return [ann(f"0{(start + k) % 100000:05d}", f"gssz{start + k:07d}", f"Z{start + k}{year}年年度报告摘要",
                f"Z{day:%Y%m%d}{start + k:06d}", day_ms(day)) for k in range(n)]


def weekdays(a: dt.date, b: dt.date) -> list[dt.date]:
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1) if (a + dt.timedelta(days=i)).weekday() < 5]


def season_counts() -> dict[dt.date, int]:
    """Rows per day of the live 13-month window 2025-08-26~2026-09-26 (11,403 rows): the newest days exactly as
    the live pages 1-100 show them (fixture), the rest a plausible reporting season (March, April peak, the
    2026-04-28 remainder) with the older revisions spread over the 2025 weekdays."""
    counts = {dt.date.fromisoformat(d): n for d, n in PAGE_CAP_LIVE["day_counts"].items() if d != "2026-04-28"}
    for d in weekdays(dt.date(2026, 4, 1), dt.date(2026, 4, 27)):
        counts[d] = 220
    for d in weekdays(dt.date(2026, 3, 1), dt.date(2026, 3, 31)):
        counts[d] = 120
    for d in weekdays(dt.date(2026, 1, 1), dt.date(2026, 2, 28)):
        counts[d] = 10
    counts[dt.date(2026, 4, 28)] = 1300
    rest = PAGE_CAP_LIVE["page1_meta"]["totalAnnouncement"] - sum(counts.values())
    old = weekdays(SINCE, dt.date(2025, 12, 31))
    for i, d in enumerate(old):
        counts[d] = rest // len(old) + (1 if i < rest % len(old) else 0)
    return counts


def season_rows() -> list[dict]:
    rows, k = [], 0
    for day, n in sorted(season_counts().items()):
        rows += pad_rows(day, n, start=k)
        k += n
    return rows


class ListServer:
    """CNINFO's date-range listing as observed live: rows newest first, 30 per page, totalAnnouncement = rows in
    seDate (inclusive, day granularity), at most `cap_pages` pages per query; beyond the cap it serves page 1 again
    (live 2026-09-26: page 101 == page 1 byte for byte) or, with beyond='last', the last capped page.
    plates=True honours the 'plate' filter (sh / sz / bj by secCode, as verified live 2026-09-27), else it is ignored.
    se_date: 'inclusive' (a <= day <= b, as assumed), 'exclusive_end' (a <= day < b) or 'overlap' (a-1 <= day <= b)
    to check that the splitter notices a server that does not work as assumed."""

    PLATE_MARKET = {"sh": "SSE", "sz": "SZSE", "bj": "BJ"}

    def __init__(self, rows: list[dict], *, cap_pages: int | None = None, beyond: str = "first",
                 plates: bool = False, se_date: str = "inclusive"):
        self.by_day: dict[dt.date, list[dict]] = {}
        for r in rows:
            self.by_day.setdefault(cninfo._ms_to_date(r["announcementTime"]), []).append(r)
        for day_rows in self.by_day.values():
            day_rows.sort(key=lambda r: r["announcementId"], reverse=True)
        self.cap_pages = cap_pages
        self.beyond = beyond
        self.plates, self.se_date = plates, se_date
        self.requests: list[tuple[str, int]] = []
        self.plate_requests: list[tuple[str, str, int]] = []

    def in_range(self, d: dt.date, a: dt.date, b: dt.date) -> bool:
        if self.se_date == "exclusive_end":
            return a <= d < b
        if self.se_date == "overlap":
            return a - dt.timedelta(days=1) <= d <= b
        return a <= d <= b

    def __call__(self, form: dict) -> bytes:
        a, b = (dt.date.fromisoformat(x) for x in form["seDate"].split("~"))
        sel = [r for d in sorted(self.by_day, reverse=True) if self.in_range(d, a, b) for r in self.by_day[d]]
        n = int(form["pageNum"])
        if form.get("plate"):
            self.plate_requests.append((form["seDate"], form["plate"], n))
            if self.plates:
                sel = [r for r in sel if cninfo.market_of_code(r["secCode"]) == self.PLATE_MARKET[form["plate"]]]
        self.requests.append((form["seDate"], n))
        cap = cninfo.PAGE_CAP_PAGES if self.cap_pages is None else self.cap_pages
        if n > cap:
            n = 1 if self.beyond == "first" else cap
        chunk = sel[(n - 1) * 30:n * 30]
        return json.dumps({"announcements": chunk, "totalAnnouncement": len(sel), "totalRecordNum": len(sel),
                           "totalpages": len(sel) // 30, "hasMore": n * 30 < len(sel)}, ensure_ascii=False).encode()

    def max_page(self) -> int:
        return max((n for _, n in self.requests), default=0)


class ListFake(FastFake):
    """FastFake whose listing POSTs (stock='') are answered by a ListServer."""

    def __init__(self, server: ListServer, gets: dict | None = None, posts: dict | None = None, **kw):
        super().__init__(gets, [], posts, **kw)
        self.server = server

    def post_form(self, url: str, form: dict, headers=None, rate_key=None) -> Response:
        if form["stock"]:
            return super().post_form(url, form, headers=headers, rate_key=rate_key)
        with self.lock:
            self.forms.append(dict(form))
            self.rate_keys.append((url, rate_key))
            self.requests_made += 1
            self.calls.append(f"LIST {form['seDate']} p{form['pageNum']}")
        return Response(url, 200, self.server(form), {}, 0.0)


class TestAdaptiveWindows(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def range_pass(self, fake, since=SINCE, until=TODAY, **kw) -> cninfo.RangeListing:
        return cninfo.list_range(fake, self.cfg, cninfo.RangeListing(since, until), **kw)

    def test_live_fixture(self):
        live = PAGE_CAP_LIVE
        self.assertEqual((live["page1_meta"]["totalAnnouncement"], live["pages_fetched"]), (11403, 101))
        self.assertEqual(live["page1_sha256"], live["page101_sha256"])          # page 101 is page 1 again
        self.assertEqual((live["rows_pages_1_100"], live["distinct_rows_pages_1_100"]), (3000, 3000))
        self.assertEqual(sum(live["day_counts"].values()), cninfo.PAGE_CAP_ROWS)
        self.assertEqual(cninfo.PAGE_CAP_ROWS, cninfo.PAGE_CAP_PAGES * cninfo.QUERY_PAGE_SIZE)
        self.assertLess(cninfo.WINDOW_MAX_ROWS, cninfo.PAGE_CAP_ROWS)
        counts = season_counts()
        self.assertEqual(sum(counts.values()), 11403)
        self.assertEqual(max(counts.values()), live["day_counts"]["2026-04-29"])   # the live peak day (1,424)

    def test_one_window_hits_the_cap_like_the_live_run(self):
        rows = season_rows()
        # the old behaviour (no cap known): page 101 repeats page 1 -> duplicate_page after 3,000 rows, incomplete
        for beyond in ("first", "last"):                    # live: page 1 again; also a repeated page 100
            server = ListServer(rows, cap_pages=100, beyond=beyond)
            with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 10_000):
                lst = cninfo.list_window(ListFake(server), self.cfg, cninfo.Listing(SINCE, TODAY))
            self.assertEqual((lst.pages, len(lst.announcements), lst.stop, lst.complete, lst.total_first),
                             (101, 3000, "duplicate_page", False, 11403), beyond)
        # now: page 101 is never requested
        server = ListServer(rows)
        lst = cninfo.list_window(ListFake(server), self.cfg, cninfo.Listing(SINCE, TODAY))
        self.assertEqual((lst.pages, len(lst.announcements), lst.stop, lst.complete), (100, 3000, "page_cap", False))
        self.assertEqual(server.max_page(), 100)

    def test_live_year_needs_about_420_requests(self):
        server = ListServer(season_rows())
        fake = ListFake(server)
        rl = self.range_pass(fake)
        self.assertEqual((rl.usable, rl.complete, rl.gaps, rl.days_over_cap), (True, True, [], []))
        self.assertEqual((len(rl.announcements), rl.window_rows, rl.total_reported, rl.shortfall),
                         (11403, 11403, 11403, None))
        self.assertEqual(rl.total_first, 11403)                               # the whole-range probe
        self.assertEqual(rl.pages, len(fake.forms))
        self.assertGreaterEqual(rl.pages, math.ceil(11403 / 30))              # 381 pages at the very least
        self.assertLessEqual(rl.pages, 450)                                   # not 5,000+ per-company queries
        self.assertLessEqual(server.max_page(), cninfo.PAGE_CAP_PAGES)
        self.assertTrue(all(k == cninfo.RATE_KEY for _, k in fake.rate_keys))
        for w in rl.leaves:                                                   # every leaf fits under the margin
            self.assertLessEqual(w.total_first, cninfo.WINDOW_MAX_ROWS, w.se_date)
            self.assertTrue(w.complete, w.se_date)
        for w in rl.windows:
            if w.stop == "split":
                self.assertEqual(w.pages, 1)                                  # a split costs one probe
                self.assertGreater(w.total_first, cninfo.WINDOW_MAX_ROWS)
        # leaves tile the range: consecutive, no overlap, no hole
        spans = sorted((w.since, w.until) for w in rl.leaves)
        self.assertEqual((spans[0][0], spans[-1][1]), (SINCE, TODAY))
        self.assertTrue(all(b[0] == a[1] + dt.timedelta(days=1) for a, b in zip(spans, spans[1:])))
        # every page saved raw under a per-window name
        names = {Path(m["raw_path"]).name.rsplit("-", 1)[0] for m in rl.manifest}
        self.assertIn("list-p0001-20250826-20260926", names)
        self.assertEqual(len(rl.manifest), rl.pages)

    def test_split_down_to_a_single_day(self):
        days = [dt.date(2026, 4, 20) + dt.timedelta(days=i) for i in range(10)]
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 80 if d == days[7] else 5, start=i * 100)]
        server = ListServer(rows)
        rl = self.range_pass(ListFake(server), days[0], days[-1], max_rows=40)
        self.assertEqual((rl.complete, len(rl.announcements), rl.total_reported), (True, 125, 125))
        dense = [w for w in rl.leaves if w.since == w.until == days[7]]
        self.assertEqual(len(dense), 1)                                       # split down to the day itself
        self.assertEqual((dense[0].total_first, dense[0].pages, dense[0].stop), (80, 3, "has_more_false"))
        self.assertTrue(all(w.total_first <= 40 for w in rl.leaves if w.since < w.until))
        self.assertEqual(rl.pages, sum(w.pages for w in rl.windows))

    def test_day_over_cap_is_a_gap_of_that_day_only(self):
        days = [dt.date(2026, 4, 27) + dt.timedelta(days=i) for i in range(5)]
        over = days[2]
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 100 if d == over else 10, start=i * 1000)]
        server = ListServer(rows)
        with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 3):                 # cap 90 rows per query
            rl = self.range_pass(ListFake(server), days[0], days[-1], max_rows=60)
        self.assertLessEqual(server.max_page(), 3)                            # never past the cap
        self.assertEqual((rl.usable, rl.complete, rl.days_over_cap), (True, False, [over.isoformat()]))
        self.assertEqual(rl.gaps, [(over, over, "day_over_cap")])
        # completeness: sum of the leaves' distinct rows vs sum of their totals
        self.assertEqual((rl.total_reported, rl.window_rows, rl.shortfall, len(rl.announcements)), (140, 130, 10, 130))
        self.assertTrue(all(w.complete for w in rl.leaves if w.since != over))
        self.assertIn(f"day_over_cap:{over}", cninfo._listing_note(rl))
        # which companies the gap can affect: newest listed year Y matters from 1 January Y+1
        fy = lambda y: [ann("300001", "o", f"C{y}年年度报告摘要", f"Y{y}", day_ms(dt.date(y + 1, 3, 1)))]  # noqa: E731
        self.assertTrue(rl.gap_affects(fy(2025)))                          # FY2025 rows can be on 2026-04-29
        self.assertTrue(rl.gap_affects([]))                                # nothing listed: could be on that day
        rl.gaps = [(dt.date(2025, 4, 29), dt.date(2025, 4, 29), "day_over_cap")]
        self.assertFalse(rl.gap_affects(fy(2025)))                         # FY2025 cannot be out on 2025-04-29
        self.assertTrue(rl.gap_affects(fy(2024)))

    def test_rows_missing_in_one_window_is_a_gap_of_that_window(self):
        days = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(4)]
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 40, start=i * 1000)]
        server = ListServer(rows)
        real = server.__call__

        def lossy(form):                  # same-day rows re-ordered between two pages: one row seen twice, one lost
            body = json.loads(real(form))
            if form["seDate"] == "2026-04-01~2026-04-02" and form["pageNum"] == "2":
                prev = json.loads(real(dict(form, pageNum="1")))["announcements"]
                body["announcements"][0] = prev[-1]
            return json.dumps(body).encode()
        fake = ListFake(server)
        fake.server = lossy
        rl = self.range_pass(fake, days[0], days[-1], max_rows=90)
        self.assertEqual((rl.usable, rl.complete), (True, False))
        self.assertEqual(rl.gaps, [(days[0], days[1], "rows_missing:79_of_80")])
        self.assertEqual((rl.shortfall, rl.days_over_cap), (1, []))

    def test_http_error_or_page_budget_make_the_pass_unusable(self):
        days = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(4)]
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 40, start=i * 1000)]
        rl = self.range_pass(ListFake(ListServer(rows)), days[0], days[-1], max_rows=90, max_pages=3)
        self.assertEqual((rl.usable, rl.stop, rl.pages), (False, "page_limit", 3))

        class Failing(ListFake):
            def post_form(self, url, form, headers=None, rate_key=None):
                if form["seDate"].startswith("2026-04-01") and form["pageNum"] == "1":
                    return Response(url, 500, b"", {}, 0.0)
                return super().post_form(url, form, headers=headers, rate_key=rate_key)
        rl = self.range_pass(Failing(ListServer(rows)), days[0], days[-1], max_rows=90)
        self.assertEqual((rl.usable, rl.complete, rl.stop, rl.error), (False, False, "error", "list_http_500"))

    # ---- repairs and the split check (review 2026-09-26: a gap in the reporting season affects nearly everyone)

    def over_cap_day(self) -> tuple[list[dt.date], dt.date, list[dict]]:
        days = [dt.date(2026, 4, 27) + dt.timedelta(days=i) for i in range(4)]
        over = days[1]
        rows = [r for i, d in enumerate(days) if d != over for r in pad_rows(d, 10, start=i * 1000)]
        rows += pad_rows(over, 60, start=5000) + sz_rows(over, 50, start=6000)     # 110 rows > cap 90
        return days, over, rows

    def test_day_over_cap_is_repaired_per_exchange(self):
        days, over, rows = self.over_cap_day()
        server = ListServer(rows, plates=True)
        with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 3):                 # cap 90 rows per query
            rl = self.range_pass(ListFake(server), days[0], days[-1], max_rows=60)
        self.assertLessEqual(server.max_page(), 3)
        self.assertEqual((rl.usable, rl.complete, rl.gaps), (True, True, []))
        self.assertEqual((rl.days_over_cap, rl.repairs), ([over.isoformat()], [f"{over}~{over}:plate"]))
        self.assertEqual((len(rl.announcements), rl.total_reported, rl.shortfall), (140, 140, None))
        self.assertEqual({p for _, p, _ in server.plate_requests}, {"sh", "sz", "bj"})
        # sh 60 rows (2 pages) + sz 50 (2 pages) + bj 0 (1 page)
        self.assertEqual(len(server.plate_requests), 5)
        self.assertEqual([w.role for w in rl.windows if w.plate], ["plate:sh", "plate:sz", "plate:bj"])
        self.assertTrue(all(w.role == "window" for w in rl.leaves))
        note = cninfo._listing_note(rl)
        self.assertIn(f"day_over_cap:{over}", note)
        self.assertIn(f"repaired:{over}~{over}:plate", note)
        self.assertIn(f"pages={rl.pages};rows=140", note)
        raw = [Path(m["raw_path"]).name for m in rl.manifest if m.get("plate")]
        self.assertEqual(sorted({n.split("-")[4] for n in raw}), ["bj", "sh", "sz"])          # list-pNNNN-a-b-<plate>
        self.assertTrue(all(n.startswith(f"list-p000") and f"-{over:%Y%m%d}-{over:%Y%m%d}-" in n for n in raw), raw)

    def test_day_over_cap_repair_costs_one_request_when_plate_is_ignored(self):
        days, over, rows = self.over_cap_day()
        server = ListServer(rows, plates=False)
        with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 3):
            rl = self.range_pass(ListFake(server), days[0], days[-1], max_rows=60)
        self.assertEqual(server.plate_requests, [(f"{over}~{over}", "sh", 1)])   # one probe, then given up
        self.assertEqual((rl.usable, rl.complete, rl.gaps, rl.repairs), (True, False, [(over, over, "day_over_cap")], []))
        # a plate that mixes markets, or plates that do not add up to the day, are not accepted either
        for mode, bad in (("mixed", "plate_mixed:sh"), ("short", "plate_totals")):
            server = ListServer(rows, plates=True)
            real = server.__call__

            def serve(form, real=real, mode=mode):
                body = json.loads(real(form))
                if form.get("plate") == "sh" and mode == "mixed" and body["announcements"]:
                    body["announcements"][0] = sz_rows(over, 1, start=9000)[0]
                if form.get("plate") == "bj" and mode == "short":
                    body["totalAnnouncement"] = 0
                if form.get("plate") == "sz" and mode == "short":            # SZSE rows not served under 'sz'
                    body = {"announcements": [], "totalAnnouncement": 0, "hasMore": False}
                return json.dumps(body).encode()
            fake = ListFake(server)
            fake.server = serve
            logs: list[str] = []
            with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 3):
                rl = self.range_pass(fake, days[0], days[-1], max_rows=60, log=logs.append)
            self.assertEqual((rl.usable, rl.gaps), (True, [(over, over, "day_over_cap")]), mode)
            self.assertTrue(any(bad in m for m in logs), (mode, logs))

    def test_transient_rows_missing_is_repaired_by_a_second_pass(self):
        days = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(4)]
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 40, start=i * 1000)]
        server = ListServer(rows)
        real = server.__call__
        lost = []

        def lossy_once(form):             # the first pass of one window loses a row at a page boundary
            body = json.loads(real(form))
            if form["seDate"] == "2026-04-01~2026-04-02" and form["pageNum"] == "2" and not lost:
                prev = json.loads(real(dict(form, pageNum="1")))["announcements"]
                lost.append(body["announcements"][0]["announcementId"])
                body["announcements"][0] = prev[-1]
            return json.dumps(body).encode()
        fake = ListFake(server)
        fake.server = lossy_once
        rl = self.range_pass(fake, days[0], days[-1], max_rows=90)
        self.assertEqual((rl.usable, rl.complete, rl.gaps), (True, True, []))
        self.assertEqual(rl.repairs, ["2026-04-01~2026-04-02:retry"])
        self.assertIn(lost[0], {a["announcementId"] for a in rl.announcements})
        self.assertEqual((len(rl.announcements), rl.window_rows, rl.total_reported, rl.shortfall), (160, 160, 160, None))
        retry = [w for w in rl.windows if w.role == "retry"]
        self.assertEqual([(w.se_date, w.pages) for w in retry], [("2026-04-01~2026-04-02", 3)])
        self.assertEqual(rl.pages, sum(w.pages for w in rl.windows))

    def test_split_totals_must_add_up(self):
        rows = season_rows()
        # as assumed (inclusive by China date): every split agrees, the leaves add up to the whole-range probe
        rl = self.range_pass(ListFake(ListServer(rows)))
        self.assertEqual((rl.usable, rl.split_mismatches), (True, []))
        # an exclusive end loses each split's boundary day; overlapping ends count it twice: both are refused
        for mode, sign in (("exclusive_end", -1), ("overlap", 1)):
            logs: list[str] = []
            rl = self.range_pass(ListFake(ListServer(rows, se_date=mode)), log=logs.append)
            self.assertEqual((rl.usable, rl.complete, rl.stop), (False, False, "split_totals"), mode)
            self.assertTrue(rl.error.startswith("split_totals:"), rl.error)
            self.assertTrue(rl.split_mismatches, mode)
            self.assertTrue(all((c - o) * sign > 0 for _, _, o, c in rl.split_mismatches), (mode, rl.split_mismatches))
            self.assertTrue(any("do not add up" in m for m in logs), mode)
        # rows published while the pass runs (newest day) stay within the allowance
        server = ListServer(rows)
        real = server.__call__
        extra = pad_rows(TODAY, 20, start=90000)

        def growing(form):
            if len(server.requests) == 1:                                   # after the whole-range probe
                server.by_day.setdefault(TODAY, []).extend(extra)
            return real(form)
        fake = ListFake(server)
        fake.server = growing
        rl = self.range_pass(fake)
        self.assertEqual((rl.usable, rl.complete, rl.split_mismatches), (True, True, []))
        self.assertEqual(len(rl.announcements), 11403 + 20)
        self.assertTrue(cninfo.totals_agree(100, 97) and not cninfo.totals_agree(100, 96))
        self.assertTrue(cninfo.totals_agree(100, 130) and not cninfo.totals_agree(100, 131))
        self.assertTrue(cninfo.totals_agree(10_000, 10_100) and not cninfo.totals_agree(10_000, 10_101))

    # ---- review 2026-09-26 (2): retry / plate totals, a blank whole-range probe

    def lossy_first_pass(self, server: ListServer, se_date: str, *, retry=None):
        """Serve `server`, but the first pass of `se_date` loses one row at the page 1/2 boundary (page 2 repeats
        page 1's last row); `retry(form)` may answer the second pass's page 1 instead (None: the server)."""
        real = server.__call__
        state = {"p1": 0, "lost": []}

        def serve(form):
            if form["seDate"] == se_date and not form.get("plate"):
                if form["pageNum"] == "1":
                    state["p1"] += 1
                    if state["p1"] == 2 and retry is not None:
                        out = retry(form)
                        if out is not None:
                            return out
                if form["pageNum"] == "2" and state["p1"] == 1:
                    body = json.loads(real(form))
                    prev = json.loads(real(dict(form, pageNum="1")))["announcements"]
                    state["lost"].append(body["announcements"][0]["announcementId"])
                    body["announcements"][0] = prev[-1]
                    return json.dumps(body).encode()
            return real(form)
        fake = ListFake(server)
        fake.server = serve
        return fake, state

    def test_retry_total_growth_does_not_fail_the_split_check(self):
        # a busy day: rows published while the first pass pages the newest leaf; the retry's page 1 is much newer
        # than the parent's probe, so its total must not enter the split / whole-range checks
        old, new = dt.date(2026, 4, 20), dt.date(2026, 4, 29)
        server = ListServer(pad_rows(old, 100) + sz_rows(new, 100, start=1000))
        leaf = "2026-04-25~2026-04-29"
        added = sz_rows(new, 40, start=5000)

        def publish(form):                                    # 40 new reports before the retry's page 1
            server.by_day[new].extend(added)
            return None
        fake, state = self.lossy_first_pass(server, leaf, retry=publish)
        rl = self.range_pass(fake, old, new, max_rows=150)
        self.assertEqual(state["p1"], 2)                                      # the leaf was paged twice
        self.assertEqual((rl.split_mismatches, rl.error), ([], None))
        self.assertEqual((rl.usable, rl.complete, rl.stop, rl.gaps), (True, True, "done", []))
        self.assertEqual(rl.repairs, [f"{leaf}:retry"])
        self.assertEqual(len(rl.announcements), 240)                          # nothing missing
        self.assertEqual((rl.total_first, rl.total_reported, rl.shortfall), (200, 200, None))
        leaf_w = next(w for w in rl.leaves if w.se_date == leaf)
        self.assertEqual(leaf_w.total_first, 100)                             # the first pass's page-1 total
        self.assertEqual([w.total_first for w in rl.windows if w.role == "retry"], [140])

    def test_retry_with_a_lower_total_is_not_a_repair(self):
        days = [dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(4)]
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 20, start=i * 1000)]
        blank = json.dumps({"announcements": None, "totalAnnouncement": 0, "totalpages": 0, "hasMore": False}).encode()
        # a blank / throttled retry reply (total 0), or a retry reporting fewer rows than the first pass (the lost
        # row is not served): the window stays a gap in a single-window pass
        for mode in ("blank", "lower"):
            server = ListServer(rows)
            se = f"{days[0]}~{days[-1]}"
            fake, state = self.lossy_first_pass(server, se, retry=(lambda f: blank) if mode == "blank" else None)
            if mode == "lower":
                real_serve = fake.server

                def withdraw(form, real_serve=real_serve, state=state, server=server):
                    if state["p1"] >= 1 and form["pageNum"] == "1" and state["lost"]:
                        for d, day_rows in server.by_day.items():
                            server.by_day[d] = [r for r in day_rows if r["announcementId"] not in state["lost"]]
                    return real_serve(form)
                fake.server = withdraw
            logs: list[str] = []
            rl = self.range_pass(fake, days[0], days[-1], log=logs.append)
            self.assertEqual(state["p1"], 2, mode)
            self.assertEqual((rl.usable, rl.complete, rl.repairs), (True, False, []), mode)
            self.assertEqual(rl.gaps, [(days[0], days[-1], "rows_missing:79_of_80")], mode)
            self.assertEqual((len(rl.announcements), rl.total_reported, rl.shortfall), (79, 80, 1), mode)
            self.assertTrue(any("retry_total_dropped" in m for m in logs), (mode, logs))
            self.assertIn(";gap:", cninfo._listing_note(rl))

    def test_day_over_cap_plate_totals_may_not_fall_short(self):
        # rows no honoured plate returns (here: 2 Beijing rows at the day's tail, 'bj' answers nothing) are lost
        # beyond the first pass's cap: the plate repair must not accept a plate total below the day's total
        days, over, rows = self.over_cap_day()
        rows += [ann(f"83000{k}", f"gsbj{k}", f"B{k}2025年年度报告", f"A{over:%Y%m%d}{k:06d}", day_ms(over))
                 for k in range(2)]                                        # ids sort last: beyond page 3
        server = ListServer(rows, plates=True)
        real = server.__call__

        def serve(form):
            if form.get("plate") == "bj":
                server.plate_requests.append((form["seDate"], "bj", int(form["pageNum"])))
                return json.dumps({"announcements": [], "totalAnnouncement": 0, "hasMore": False}).encode()
            return real(form)
        fake = ListFake(server)
        fake.server = serve
        logs: list[str] = []
        with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 3):
            rl = self.range_pass(fake, days[0], days[-1], max_rows=60, log=logs.append)
        self.assertEqual((rl.usable, rl.complete, rl.repairs), (True, False, []))
        self.assertEqual(rl.gaps, [(over, over, "day_over_cap")])
        self.assertTrue(any("plate_totals:110_vs_112" in m for m in logs), logs)
        self.assertEqual(next(w for w in rl.leaves if w.since == over).total_first, 112)

    def test_blank_whole_range_probe_is_asked_again(self):
        days = [dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(4)]
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 20, start=i * 1000)]
        blank = json.dumps({"announcements": None, "totalAnnouncement": 0, "totalpages": 0, "hasMore": False}).encode()
        # a transient blank page 1 of the whole range: asked once more, then read as usual
        server = ListServer(rows)
        answered: list[int] = []

        def once(form):
            answered.append(1)
            return blank if len(answered) == 1 else server(form)
        fake = ListFake(server)
        fake.server = once
        rl = self.range_pass(fake, days[0], days[-1])
        self.assertEqual((rl.usable, rl.complete, rl.stop, len(rl.announcements)), (True, True, "done", 80))
        self.assertEqual((rl.total_first, rl.total_reported, len(rl.leaves)), (80, 80, 1))
        self.assertEqual([w.role for w in rl.windows], ["empty_probe", "window"])
        self.assertEqual(rl.pages, len(answered))
        # the whole range answers nothing twice: not proof that nothing was published, the pass is unusable
        for body in (blank, json.dumps({"announcements": [], "totalAnnouncement": 0, "hasMore": False}).encode()):
            fake = ListFake(ListServer(rows))
            fake.server = lambda form, body=body: body
            logs: list[str] = []
            rl = self.range_pass(fake, days[0], days[-1], log=logs.append)
            self.assertEqual((rl.usable, rl.complete, rl.stop), (False, False, "empty_range"))
            self.assertEqual(rl.error, f"empty_range:{days[0]}~{days[-1]}")
            self.assertEqual(rl.pages, 2)
            self.assertTrue(cninfo._listing_note(rl).startswith("stop=empty_range;error=empty_range:"))
            self.assertTrue(any("answered no rows twice" in m for m in logs), logs)


class TestAdaptiveSync(FastBase):
    def test_settle_vs_fallback_around_a_day_over_cap(self):
        stock = self.add_companies(4)
        since, today, over = dt.date(2025, 1, 1), dt.date(2026, 3, 15), dt.date(2025, 4, 29)
        a_code, a_org = company(0)
        b_code, b_org = company(1)
        rows = pad_rows(over, 70, year=2024)                                         # the day over the cap
        rows += [ann(a_code, a_org, "C02025年年度报告摘要", "A1", day_ms(dt.date(2026, 3, 10)))]  # FY2025: settled
        rows += [ann(b_code, b_org, "C12024年年度报告摘要", "B1", day_ms(dt.date(2025, 3, 20)))]  # FY2024: gap
        rows += [ann(company(3)[0], company(3)[1], "C32025年年度报告摘要", "D1", day_ms(dt.date(2026, 3, 2)))]
        posts = {b_code: page([ann(b_code, b_org, "C12024年年度报告摘要", "B1", day_ms(dt.date(2025, 3, 20)))]),
                 company(2)[0]: page(summary_rows(2)[:1])}
        server = ListServer(rows)
        f = ListFake(server, {cninfo.STOCK_LIST_URL: stock}, posts)
        with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 2), mock.patch.object(cninfo, "WINDOW_MAX_ROWS", 40), \
                mock.patch.object(cninfo, "_today", return_value=today):
            s, err = self.run_fast(f, since=since, max_fallback=0)
        self.assertEqual(s["status"], "ok")
        self.assertLessEqual(server.max_page(), 2)
        self.assertEqual((s["days_over_cap"], s["list_complete"], s["list_usable"]), (["2025-04-29"], False, True))
        # company 0 (FY2025, cannot be on 2025-04-29) and 3 settle from the listing; 1 (FY2024 could have a
        # revision there) and 2 (not listed) are queried although the fallback cap is 0
        self.assertEqual([c for c in f.calls if c.startswith("POST")], [f"POST {b_code}", f"POST {company(2)[0]}"])
        self.assertEqual((s["fallback_queries"], s["gap_queries_planned"], s["no_annual_report_in_window"]), (2, 2, 0))
        self.assertEqual(s["status_ok"], 4)
        self.assertEqual((s["list_rows"], s["list_total_reported"], s["list_shortfall"]), (63, 73, 10))
        self.assertGreater(s["list_windows"], 1)
        self.assertEqual(s["requests"], 1 + s["list_pages"] + 2 + s["pdfs"])
        self.assertIn("days over the cap: 2025-04-29", err)
        snap = self.q("SELECT status, note FROM snapshots WHERE kind = 'list_pass'")
        self.assertEqual(snap[0][0], "partial")
        self.assertIn("day_over_cap:2025-04-29", snap[0][1])
        raw = json.loads(Path(self.q("SELECT raw_path FROM snapshots WHERE kind = 'list_pass'")[0][0]).read_bytes())
        self.assertEqual((raw["days_over_cap"], raw["usable"], raw["rows"]), (["2025-04-29"], True, 63))
        self.assertEqual(json.loads(self.last_run()[2])["days_over_cap"], ["2025-04-29"])

    def test_live_size_listing_settles_the_universe_in_about_420_requests(self):
        stock = self.add_companies(6)
        rows = season_rows()
        for i in range(6):                                                   # the universe inside the 11,403
            code, org = company(i)
            rows[1000 + i] = ann(code, org, f"C{i}2025年年度报告摘要", f"S{i:05d}", rows[5000 + i * 7]["announcementTime"])
        server = ListServer(rows)
        f = ListFake(server, {cninfo.STOCK_LIST_URL: stock})
        with mock.patch.object(cninfo, "_today", return_value=TODAY):
            s, _ = self.run_fast(f, since=SINCE)
        self.assertEqual((s["status"], s["status_ok"], s["fallback_queries"]), ("ok", 6, 0))
        self.assertEqual((s["list_complete"], s["list_rows"], s["days_over_cap"]), (True, 11403, []))
        self.assertLessEqual(s["list_pages"], 450)
        self.assertEqual(s["requests"], 1 + s["list_pages"] + 6)            # stock list + listing + 6 PDFs
        self.assertLessEqual(server.max_page(), cninfo.PAGE_CAP_PAGES)

    def test_listing_windows_keep_one_second_on_cninfo(self):
        stock = self.add_companies(2)
        days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(3)]      # since = settle floor
        rows = [r for i, d in enumerate(days) for r in pad_rows(d, 25, start=i * 1000)]
        rows += [dict(summary_rows(i)[0], announcementTime=day_ms(days[1])) for i in range(2)]
        t = Transport(stock_list=stock, pages=ListServer(rows), pdf_latency=0.05)
        c, pdf, www_slots, _ = real_clients(t)
        with mock.patch.object(cninfo, "WINDOW_MAX_ROWS", 40), \
                mock.patch.object(cninfo, "_today", return_value=days[-1]):
            s, err = self.run_fast(c, pdf_client=pdf, workers=1, since=days[0])
        self.assertEqual((s["status"], s["status_ok"]), ("ok", 2), (s, err))
        www = [tt for k, tt in www_slots if k == cninfo.RATE_KEY]
        self.assertEqual(len(www), 1 + s["list_pages"])
        self.assertGreaterEqual(s["list_pages"], 4)                          # probe + split windows
        self.assertGreaterEqual(min(gaps(www)), 1.0 - EPS)


class TestPreQuerySkip(FastBase):
    def universe(self) -> bytes:
        stock = self.add_companies(6)
        self.store_doc(0, 2025)                                          # newest possible year: current
        self.store_doc(1, 2024)                                          # FY2025 may exist
        self.store_doc(2, 2025, extractor="cninfo-v2/pymupdf")           # older extractor
        self.store_doc(3, 2025, text=False)                              # failed extraction
        self.store_doc(5, 2025, form="annual_report")                    # a full report
        return stock

    def run_at(self, today: dt.date, stock: bytes, **kw) -> tuple[dict, FastFake]:
        posts = {company(i)[0]: page(summary_rows(i)) for i in range(6)}
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, [], posts)
        kw.setdefault("list_min_queue", None)                            # 6 companies: the listing is skipped
        with mock.patch.object(cninfo, "_today", return_value=today):
            s, _ = self.run_fast(f, **kw)
        return s, f

    def posted(self, f: FastFake) -> list[int]:
        return [int(c.split()[1]) - 300000 for c in f.calls if c.startswith("POST")]

    def test_after_30_april(self):
        stock = self.universe()
        s, f = self.run_at(dt.date(2026, 9, 26), stock)
        self.assertEqual(self.posted(f), [1, 2, 3, 4])                   # 0 and 5 hold FY2025: no request
        self.assertEqual((s["skipped_current"], s["fallback_queries"], s["status"]), (2, 4, "ok"))
        self.assertNotIn("SZSE:300000", self.states())                   # no crawl_state row, like skipped_unchanged
        self.assertEqual(s["requests"], 1 + 4 + len(f.pdf_calls()))

    def test_before_30_april(self):
        stock = self.universe()
        # 15 March 2026: FY2024 is the expected year, FY2025 already exists for many: only FY2025 holders skip
        s, f = self.run_at(dt.date(2026, 3, 15), stock)
        self.assertEqual(self.posted(f), [1, 2, 3, 4])
        self.assertEqual(s["skipped_current"], 2)
        # 15 March 2027: FY2026 can exist, nobody holds it: everyone is queried
        s, f = self.run_at(dt.date(2027, 3, 15), stock, refresh=False)
        self.assertEqual((self.posted(f), s["skipped_current"]), ([0, 1, 2, 3, 4, 5], 0))

    def test_refresh_and_kind_full(self):
        stock = self.universe()
        s, f = self.run_at(dt.date(2026, 9, 26), stock, refresh=True)
        self.assertEqual((self.posted(f), s["skipped_current"]), ([0, 1, 2, 3, 4, 5], 0))
        s, f = self.run_at(dt.date(2026, 9, 26), stock, kind="full")
        self.assertEqual(self.posted(f), [0, 1, 2, 3, 4])                # a stored summary is not the full report
        self.assertEqual(s["skipped_current"], 1)

    def test_skip_applies_to_an_unusable_listing_but_not_to_settled_companies(self):
        stock = self.universe()
        rows = [r for i in range(6) for r in summary_rows(i)[:1]]
        # listing cut short (HTTP 500): only companies that might have a newer report are queried
        posts = {company(i)[0]: page(summary_rows(i)) for i in range(6)}
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, [full_page(rows[:1], has_more=True), 500], posts)
        with mock.patch.object(cninfo, "_today", return_value=TODAY):
            s, _ = self.run_fast(f)
        self.assertEqual(s["list_usable"], False)
        self.assertEqual((self.posted(f), s["skipped_current"]), ([1, 2, 3, 4], 2))
        # usable listing: every company settles from its rows (no query at all; a revision would be seen)
        rev = ann(company(0)[0], company(0)[1], "C02025年年度报告摘要（修订后）", "S00000R", T_2026_04_20 + MS_DAY)
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, paginate([rev] + rows))
        with mock.patch.object(cninfo, "_today", return_value=TODAY):
            s, _ = self.run_fast(f)
        self.assertEqual((self.posted(f), s["skipped_current"], s["list_complete"]), ([], 0, True))
        # the current company's revision; company 5 (only a full report stored) gets its summary
        self.assertEqual(sorted(f.pdf_calls()), [pdf_url("S00000R"), pdf_url("S00005")])


class TestSeasonGap(FastBase):
    """A gap in the 2026 reporting season (the review's case): every FY2025 company is affected by it
    (gap_affects: a gap ending on or after 1 January 2026), so an unrepaired gap costs one query per non-current
    company; the repairs keep that at zero."""

    N = 50
    OVER = dt.date(2026, 4, 28)

    def season(self) -> tuple[bytes, list[dict]]:
        stock = self.add_companies(self.N)
        rows = []
        for i in range(self.N):                           # FY2025 summaries: half on the day over the cap
            day = self.OVER if i % 2 == 0 else dt.date(2026, 4, 20) + dt.timedelta(days=i % 5)
            rows.append(dict(summary_rows(i)[0], announcementTime=day_ms(day)))
        rows += pad_rows(self.OVER, 70, start=5000)       # 25 + 70 = 95 rows on 2026-04-28 > cap 90
        rows += pad_rows(dt.date(2026, 3, 31), 20, start=7000)
        return stock, rows

    def run_season(self, server: ListServer, stock: bytes, **kw) -> tuple[dict, ListFake, str]:
        posts = {company(i)[0]: page(summary_rows(i)[:1]) for i in range(self.N)}
        f = ListFake(server, {cninfo.STOCK_LIST_URL: stock}, posts)
        with mock.patch.object(cninfo, "PAGE_CAP_PAGES", 3), mock.patch.object(cninfo, "WINDOW_MAX_ROWS", 60), \
                mock.patch.object(cninfo, "_today", return_value=TODAY):
            s, err = self.run_fast(f, since=dt.date(2026, 1, 1), max_fallback=0, **kw)
        return s, f, err

    def queried(self, f: ListFake) -> int:
        return sum(1 for c in f.calls if c.startswith("POST"))

    def test_repaired_day_over_cap_needs_no_gap_query(self):
        stock, rows = self.season()
        s, f, err = self.run_season(ListServer(rows, plates=True), stock)
        self.assertEqual((s["status"], s["status_ok"]), ("ok", self.N))
        self.assertEqual((s["days_over_cap"], s["list_gaps"], s["list_complete"]), (["2026-04-28"], [], True))
        self.assertEqual(s["list_repairs"], ["2026-04-28~2026-04-28:plate"])
        self.assertEqual((s["gap_queries_planned"], s["fallback_queries"], self.queried(f)), (0, 0, 0))
        self.assertEqual(s["requests"], 1 + s["list_pages"] + self.N)            # stock list + listing + PDFs
        note = self.q("SELECT status, note FROM snapshots WHERE kind = 'list_pass'")[0]
        self.assertEqual(note[0], "ok")
        self.assertIn("repaired:2026-04-28~2026-04-28:plate", note[1])
        self.assertIn("repaired: 2026-04-28~2026-04-28:plate", err)

    def test_unrepaired_gap_costs_one_query_per_non_current_company(self):
        stock, rows = self.season()
        for i in range(20):                               # 20 companies already hold their FY2025 section
            self.store_doc(i, 2025)
        server = ListServer(rows, plates=False)
        s, f, err = self.run_season(server, stock)
        self.assertEqual(len(server.plate_requests), 1)                          # the repair gave up after 1
        self.assertEqual((s["list_gaps"], s["list_usable"], s["list_complete"]),
                         (["2026-04-28~2026-04-28:day_over_cap"], True, False))
        # every FY2025 company is affected by a 2026-04-28 gap; the 20 current ones are skipped without a request
        # (a revision inside the gap is missed: counted), the other 30 are queried although the cap is 0
        self.assertEqual((s["skipped_current"], s["skipped_current_in_gap"]), (20, 20))
        self.assertEqual((s["gap_queries_planned"], s["fallback_queries"], self.queried(f)), (30, 30, 30))
        self.assertEqual(s["status_ok"], 30)
        self.assertIn("20 of them could have a revision in a listing gap", err)

    def test_transient_row_loss_in_april_needs_no_gap_query(self):
        stock, rows = self.season()
        rows = [r for r in rows if cninfo._ms_to_date(r["announcementTime"]) != self.OVER or r["secCode"][0] == "3"]
        server = ListServer(rows)
        real = server.__call__
        lost: list[str] = []

        def lossy_once(form):             # the first page 2 of any window loses one row (unstable same-day order)
            body = json.loads(real(form))
            if form["pageNum"] == "2" and not lost and len(body["announcements"]) > 1:
                prev = json.loads(real(dict(form, pageNum="1")))["announcements"]
                lost.append(body["announcements"][0]["announcementId"])
                body["announcements"][0] = prev[-1]
            return json.dumps(body).encode()
        posts = {company(i)[0]: page(summary_rows(i)[:1]) for i in range(self.N)}
        f = ListFake(server, {cninfo.STOCK_LIST_URL: stock}, posts)
        f.server = lossy_once
        with mock.patch.object(cninfo, "WINDOW_MAX_ROWS", 40), mock.patch.object(cninfo, "_today", return_value=TODAY):
            s, _ = self.run_fast(f, since=dt.date(2026, 1, 1), max_fallback=0)
        self.assertTrue(lost)
        self.assertEqual(len(s["list_repairs"]), 1)
        self.assertTrue(s["list_repairs"][0].endswith(":retry"))
        self.assertEqual((s["list_gaps"], s["list_complete"], s["list_shortfall"]), ([], True, None))
        self.assertEqual((s["gap_queries_planned"], s["fallback_queries"], s["status_ok"]), (0, 0, self.N))

    def test_codes_is_an_explicit_recheck(self):
        stock = self.add_companies(2)
        self.store_doc(0, 2025)                           # current, but named with --codes: queried
        posts = {company(i)[0]: page(summary_rows(i)) for i in range(2)}
        f = FastFake({cninfo.STOCK_LIST_URL: stock}, [], posts)
        with mock.patch.object(cninfo, "_today", return_value=TODAY):
            s, _ = self.run_fast(f, codes=["300000"])
        self.assertEqual([c for c in f.calls if c.startswith("POST")], ["POST 300000"])
        self.assertEqual((s["skipped_current"], s["fallback_queries"], s["list_skipped"]), (0, 1, "--codes given"))


def tc_fake_module(sync):
    import types
    mod = types.ModuleType(cli.CNINFO)
    mod.sync = sync
    return mod


if __name__ == "__main__":
    unittest.main()
