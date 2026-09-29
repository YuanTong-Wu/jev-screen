"""Tests for sources.sec_edgar. No network: every client is a fake (or a real http.Client with a fake opener).

Fixtures tests/fixtures/sec_* are SYNTHETIC (invented issuers QKNW/QCXL/QFRT, CIKs 99901xx; generator
tools/synthetic_fixtures/sec.py), shaped like EDGAR payloads and inline-XBRL annual reports.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Client, Response  # noqa: E402
from jevscreen.sources import sec_edgar as sec  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PICKS = json.loads((FIXTURES / "sec_picks.json").read_text())
TEST_UA = "JevScreen Tester jevtest-ua@example.invalid"   # fake; the real UA is never used in tests


def _gz(name: str) -> bytes:
    return gzip.open(FIXTURES / name).read()


TICKERS_RAW = _gz("sec_company_tickers_exchange.json.gz")
SUBS_RAW = {t: _gz(f"sec_submissions_{t}.json.gz") for t in ("QKNW", "QCXL", "QFRT")}
DOC_RAW = {"QKNW": _gz("sec_doc_QKNW_10-K.htm.gz"), "QCXL": _gz("sec_doc_QCXL_20-F.htm.gz")}
_TEXT_CACHE: dict[tuple[str, str], str] = {}


def fixture_text(t: str, backend: str = "auto") -> str:
    key = (t, backend)
    if key not in _TEXT_CACHE:
        _TEXT_CACHE[key] = sec.html_to_text(DOC_RAW[t], backend=backend)
    return _TEXT_CACHE[key]


def doc_url(t: str) -> str:
    p = PICKS[t]
    return sec.archive_url(p["cik"], p["acc"], p["doc"])


def subs_url(t: str) -> str:
    return sec.SUBMISSIONS_URL.format(cik=PICKS[t]["cik"])


def lxml_available() -> bool:
    try:
        import lxml.html  # noqa: F401
        return True
    except ImportError:
        return False


class FakeSecClient:
    """Stands in for http.Client: URL -> bytes | int status | Exception; records calls and kwargs."""

    def __init__(self, pages: dict[str, object] | None = None, default: object = 404, on_request=None):
        self.pages, self.default, self.on_request = dict(pages or {}), default, on_request
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.min_interval_s, self.requests_made = 0.0, 0

    def get(self, url: str, **kw) -> Response:
        self.calls.append(url)
        self.kwargs.append(kw)
        self.requests_made += 1
        if self.on_request:
            self.on_request(url)
        item = self.pages.get(url, self.default)
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        if isinstance(item, int):
            return Response(url, item, b"", {}, 0.0)
        return Response(url, 200, item, {}, 0.0)


def standard_pages() -> dict[str, object]:
    pages: dict[str, object] = {sec.TICKERS_URL: TICKERS_RAW}
    for t in ("QKNW", "QCXL", "QFRT"):
        pages[subs_url(t)] = SUBS_RAW[t]
    for t in ("QKNW", "QCXL"):
        pages[doc_url(t)] = DOC_RAW[t]
    return pages


# =========================================================================== pure functions


class TestMapping(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = sec.parse_tickers(TICKERS_RAW)

    def test_parse_tickers_fixture(self):
        data = json.loads(TICKERS_RAW)["data"]
        self.assertEqual(len(self.rows), len(data) - sum(1 for r in data if not r[2]))
        self.assertEqual(sum(1 for r in data if not r[2]), 1)                    # the blank-ticker row is skipped
        self.assertIn({"cik": 9990101, "name": "Quillknow Inc.", "ticker": "QKNW", "exchange": "Nasdaq"}, self.rows)

    def test_normalise_ticker(self):
        for s in ("BRK.B", "brk-b", "BRK/B", "BRK B", "BRK..B"):
            self.assertEqual(sec.normalise_ticker(s), "BRK-B")

    def test_fixture_mapping(self):
        secs = [
            {"security_id": "NYSE:HLDG.B"}, {"security_id": "NYSE:HLDG.A"}, {"security_id": "NYSE:BRBL.B"},
            {"security_id": "NYSE:BZAR", "tv_type": "dr"},        # DR line is the US primary for Bazaar
            {"security_id": "NASDAQ:QKNW"}, {"security_id": "NASDAQ:QCXL"},
            {"security_id": "TSE:9901"}, {"security_id": "HKEX:9910"},   # non-US venues ignored
            {"security_id": "NASDAQ:ZZZZNOPE"},
        ]
        report: dict = {}
        got = {sid: (cik, m) for sid, cik, m in sec.map_securities_to_cik(secs, json.loads(TICKERS_RAW),
                                                                          report=report)}
        self.assertEqual(got["NYSE:HLDG.B"], (9990201, "ticker_class_normalised"))
        self.assertEqual(got["NYSE:HLDG.A"], (9990201, "ticker_class_normalised"))
        self.assertEqual(got["NYSE:BRBL.B"], (9990202, "ticker_class_normalised"))
        self.assertEqual(got["NYSE:BZAR"], (9990203, "ticker_exact"))
        self.assertEqual(got["NASDAQ:QKNW"], (9990101, "ticker_exact"))
        self.assertEqual(got["NASDAQ:QCXL"], (9990102, "ticker_exact"))
        self.assertNotIn("TSE:9901", got)
        self.assertEqual(report["non_us"], 2)
        self.assertEqual(report["unmatched"], 1)
        self.assertEqual(report["ambiguous"], [])

    def test_us_venue_aliases_and_explicit_exchange_column(self):
        rows = [{"cik": 1, "name": "A", "ticker": "AAA", "exchange": "NYSE"},
                {"cik": 2, "name": "B", "ticker": "BBB", "exchange": "CBOE"},
                {"cik": 3, "name": "C", "ticker": "CCC", "exchange": "OTC"}]
        secs = [{"security_id": "ARCA:AAA"}, {"security_id": "BATS:BBB"},
                {"security_id": "X:CCC", "exchange": "OTC", "symbol": "CCC"}, {"security_id": "LSE:AAA"}]
        got = sec.map_securities_to_cik(secs, rows)
        self.assertEqual(sorted(got), [("ARCA:AAA", 1, "ticker_exact"), ("BATS:BBB", 2, "ticker_exact"),
                                       ("X:CCC", 3, "ticker_exact")])

    def test_ambiguous_ticker_reported_and_venue_tiebreak(self):
        rows = [{"cik": 10, "name": "X one", "ticker": "DUP", "exchange": "NYSE"},
                {"cik": 11, "name": "X two", "ticker": "DUP", "exchange": "OTC"},
                {"cik": 20, "name": "Y one", "ticker": "TWIN", "exchange": "Nasdaq"},
                {"cik": 21, "name": "Y two", "ticker": "TWIN", "exchange": "Nasdaq"}]
        report: dict = {}
        got = sec.map_securities_to_cik([{"security_id": "OTC:DUP"}, {"security_id": "NASDAQ:TWIN"}], rows,
                                        report=report)
        self.assertEqual(got, [("OTC:DUP", 11, "ticker_exact")])   # same venue family breaks the tie
        self.assertEqual(report["ambiguous"], [{"security_id": "NASDAQ:TWIN", "ticker": "TWIN", "ciks": [20, 21]}])

    def test_one_cik_per_security_and_dedup(self):
        rows = [{"cik": 5, "name": "Q", "ticker": "Q.U", "exchange": "NYSE"},
                {"cik": 6, "name": "Q2", "ticker": "Q-U", "exchange": "NYSE"}]
        got = sec.map_securities_to_cik([{"security_id": "NYSE:Q.U"}, {"security_id": "NYSE:Q.U"}], rows)
        self.assertEqual(got, [("NYSE:Q.U", 5, "ticker_exact")])   # exact beats normalised; duplicates once


class TestLatestFiling(unittest.TestCase):
    def test_fixtures(self):
        for t in ("QKNW", "QCXL", "QFRT"):
            f = sec.latest_annual_filing(json.loads(SUBS_RAW[t]))
            p = PICKS[t]
            self.assertEqual((f["form"], f["accession"], f["primary_document"], f["filing_date"], f["report_date"]),
                             (p["form"], p["acc"], p["doc"], p["date"], p["report"]), t)
            self.assertFalse(f["amendment"])

    def test_forms_filter(self):
        notes: list[str] = []
        self.assertIsNone(sec.latest_annual_filing(json.loads(SUBS_RAW["QCXL"]), ("10-K",), notes=notes))
        self.assertEqual(notes, ["no_annual_filing"])

    @staticmethod
    def _subs(rows, files=()):
        cols = ["form", "accessionNumber", "filingDate", "reportDate", "primaryDocument"]
        return {"filings": {"recent": {c: [r[i] for r in rows] for i, c in enumerate(cols)}, "files": list(files)}}

    def test_amendment_ignored_when_original_exists(self):
        subs = self._subs([("10-K/A", "A-2", "2026-05-01", "2025-12-31", "a.htm"),
                           ("10-Q", "Q-1", "2026-04-30", "2026-03-31", "q.htm"),
                           ("10-K", "O-1", "2026-02-20", "2025-12-31", "k.htm"),
                           ("10-K", "O-0", "2025-02-20", "2024-12-31", "k0.htm")])
        f = sec.latest_annual_filing(subs)
        self.assertEqual((f["accession"], f["form"], f["amendment"]), ("O-1", "10-K", False))

    def test_amendment_used_only_without_original(self):
        subs = self._subs([("10-K/A", "A-2", "2026-05-01", "2025-12-31", "a.htm"),
                           ("10-K/A", "A-1", "2026-03-01", "2025-12-31", "a1.htm")])
        f = sec.latest_annual_filing(subs)
        self.assertEqual((f["accession"], f["form"], f["base_form"], f["amendment"]), ("A-2", "10-K/A", "10-K", True))

    def test_order_independent_and_none_notes(self):
        subs = self._subs([("20-F", "OLD", "2024-04-01", "2023-12-31", "o.htm"),
                           ("20-F", "NEW", "2025-04-01", "2024-12-31", "n.htm")])
        self.assertEqual(sec.latest_annual_filing(subs)["accession"], "NEW")
        notes: list[str] = []
        self.assertIsNone(sec.latest_annual_filing(self._subs([("8-K", "X", "2026-01-01", "", "x.htm")],
                                                              files=[{"name": "CIK1-submissions-001.json"}]),
                                                   notes=notes))
        self.assertEqual(notes, ["no_annual_filing_in_recent"])
        self.assertIsNone(sec.latest_annual_filing({}))


class TestHtmlToText(unittest.TestCase):
    HTML = (b"<?xml version='1.0' encoding='ASCII'?><html><head><title>t</title><style>p{x:1}</style></head><body>"
            b"<div style='display:none'><ix:header><ix:hidden>SECRET-HIDDEN</ix:hidden></ix:header></div>"
            b"<script>var x = 'SCRIPT';</script>"
            b"<table><tr><td><b>ITEM&#160;1.</b></td><td>Business</td></tr></table>"
            b"<p>AT&amp;T &#8220;quoted&#8221;   and\n  wrapped   text.<br/>Next line</p>"
            b"<div style='DISPLAY: none'>hidden too <div>nested</div> still hidden</div><p>Tail&nbsp;para.</p>"
            b"</body></html>")

    def _check(self, text: str):
        self.assertNotIn("SECRET-HIDDEN", text)
        self.assertNotIn("SCRIPT", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("p{x:1}", text)
        self.assertIn("ITEM 1. Business", text)
        self.assertIn("AT&T “quoted” and wrapped text.\nNext line", text)
        self.assertIn("\n\nTail para.", text)

    def test_stdlib_backend(self):
        self._check(sec.html_to_text(self.HTML, backend="stdlib"))

    @unittest.skipUnless(lxml_available(), "lxml not installed")
    def test_lxml_backend_matches_stdlib(self):
        self._check(sec.html_to_text(self.HTML, backend="lxml"))
        self.assertEqual(sec.html_to_text(self.HTML, backend="lxml"), sec.html_to_text(self.HTML, backend="stdlib"))

    def test_stdlib_used_when_lxml_missing(self):
        with mock.patch.dict(sys.modules, {"lxml": None, "lxml.html": None}):
            self.assertEqual(sec.html_backend(), "stdlib")
            self._check(sec.html_to_text(self.HTML))

    def test_fixture_hides_ix_header(self):
        text = fixture_text("QKNW", "stdlib")
        self.assertNotIn("dei:", text)
        self.assertNotIn("us-gaap:", text)
        self.assertIn("ITEM 1A.RISK FACTORS", text)


class TestExtractSection(unittest.TestCase):
    def assert_no_heading(self, section: str, pattern: str):
        self.assertIsNone(re.search(pattern, section, re.I | re.M), pattern)

    def test_egan_10k_item1(self):
        text = fixture_text("QKNW")
        s, note = sec.extract_section(text, "10-K")
        self.assertEqual(note, "ok:end=item1a")
        self.assertTrue(re.match(r"ITEM 1\.\s+BUSINESS", s), s[:60])
        self.assertTrue(sec.MIN_SECTION_CHARS <= len(s) <= sec.MAX_SECTION_CHARS)
        self.assertGreater(len(s), 20_000)
        self.assertIn("customer engagement", s)
        self.assertIn("knowledge", s)
        self.assert_no_heading(s, r"^\s*item\s*1a")
        self.assertNotIn("RISK FACTORS", s)
        # the text right after the section is the Item 1A heading
        after = text[text.index(s) + len(s):]
        self.assertTrue(re.match(r"\s*(?:\d+\s*)?(?:Table of Contents\s*)?ITEM 1A\.RISK FACTORS", after), after[:80])

    def test_nice_20f_item4(self):
        text = fixture_text("QCXL")
        s, note = sec.extract_section(text, "20-F")
        self.assertEqual(note, "ok:end=item4a")
        self.assertTrue(s.startswith("Item 4. Information on the Company"), s[:60])
        self.assertIn("customer experience", s)
        self.assertGreater(len(s), 50_000)
        self.assert_no_heading(s, r"^\s*item\s*4a")
        self.assertNotIn("Unresolved Staff Comments", s)
        self.assertIn("Item 4.A History and Development of the Company", s)   # sub-heading 4.A is NOT Item 4A

    def test_backends_agree_on_fixtures(self):
        for t, form in (("QKNW", "10-K"), ("QCXL", "20-F")):
            a = sec.extract_section(fixture_text(t, "stdlib"), form)
            b = sec.extract_section(fixture_text(t), form)
            self.assertEqual(a, b, t)

    def test_40f_not_supported(self):
        self.assertEqual(sec.extract_section("anything", "40-F"), (None, "form_40F_aif_in_exhibit_not_supported"))
        self.assertEqual(sec.extract_section("anything", "40-F/A"), (None, "form_40F_aif_in_exhibit_not_supported"))
        self.assertEqual(sec.extract_section("anything", "S-1")[0], None)

    @staticmethod
    def body(words: str, n: int = 600) -> str:
        return ("We design widgets for industrial customers. " + words + " ") * (n // 10)

    def test_toc_rejected_and_body_chosen(self):
        toc = "TABLE OF CONTENTS\nPART I\nItem 1. Business 4\nItem 1A. Risk Factors 12\nItem 2. Properties 20\n"
        text = (toc + "\nPART I\n\nItem 1. Business\n\n" + self.body("alpha") + "\n\nItem 1A. Risk Factors\n\n"
                + self.body("risky") + "\n\nItem 2. Properties\n\nWe lease.\n")
        s, note = sec.extract_section(text, "10-K")
        self.assertEqual(note, "ok:end=item1a")
        self.assertTrue(s.startswith("PART I\n\nItem 1. Business") or s.startswith("Item 1. Business"), s[:40])
        self.assertIn("alpha", s)
        self.assertNotIn("risky", s)
        self.assertNotIn("TABLE OF CONTENTS", s)

    def test_toc_only_is_rejected(self):
        toc = "Item 1. Business 4\nItem 1A. Risk Factors 12\nItem 2. Properties 20\n"
        s, note = sec.extract_section(toc, "10-K")
        self.assertIsNone(s)
        self.assertTrue(note.startswith("section_too_short_or_toc_only"), note)

    def test_heading_variants(self):
        for head in ("ITEM 1 — BUSINESS", "Item 1: Business", "item 1.business", "ITEM 1. BUSINESS",
                     "Item 1 - Business.", "PART I, ITEM 1. BUSINESS"):
            for end in ("ITEM 1A – RISK FACTORS", "Item 1A: Risk Factors", "ITEM 1.A. RISK FACTORS"):
                text = "Cover page\n\n" + head + "\n\n" + self.body("beta") + "\n\n" + end + "\n\nrisks...\n"
                s, note = sec.extract_section(sec._normalise_text(text), "10-K")
                self.assertIsNotNone(s, (head, end, note))
                self.assertIn("beta", s)
                self.assertNotIn("risks...", s)

    def test_mid_sentence_reference_is_not_a_heading(self):
        text = ("Intro text that says see Item 1. Business for details and Item 1A. Risk Factors as well. "
                + self.body("gamma") + "\n\nItem 1. Business\n\n" + self.body("delta") + "\n\nItem 1A. Risk Factors\n")
        s, _ = sec.extract_section(text, "10-K")
        self.assertTrue(s.startswith("Item 1. Business"))
        self.assertNotIn("gamma", s)

    def test_combined_items_1_and_2(self):
        text = ("Items 1 and 2. Business and Properties\n\n" + self.body("oil") + "\n\nItem 1A. Risk Factors\n\nx\n")
        s, note = sec.extract_section(text, "10-K")
        self.assertEqual(note, "ok:end=item1a")
        self.assertTrue(s.startswith("Items 1 and 2. Business and Properties"))
        text2 = ("ITEMS 1 & 2. BUSINESS AND PROPERTIES\n\n" + self.body("gas") + "\n\nITEM 3. LEGAL PROCEEDINGS\n")
        s2, note2 = sec.extract_section(text2, "10-K")
        self.assertEqual(note2, "ok:end=item3")
        self.assertIn("gas", s2)

    def test_fallback_end_boundaries(self):
        text = "Item 1. Business\n\n" + self.body("eps") + "\n\nItem 1B. Unresolved Staff Comments\n\nNone.\n"
        self.assertEqual(sec.extract_section(text, "10-K")[1], "ok:end=item1b")
        text = "Item 1. Business\n\n" + self.body("eps") + "\n\nItem 2. Properties\n\nWe lease.\n"
        self.assertEqual(sec.extract_section(text, "10-K")[1], "ok:end=item2")
        text = ("ITEM 4. INFORMATION ON THE COMPANY\n\n" + self.body("f20") +
                "\n\nITEM 5. OPERATING AND FINANCIAL REVIEW AND PROSPECTS\n")
        self.assertEqual(sec.extract_section(text, "20-F")[1], "ok:end=item5")

    def test_20f_item4_named_after_the_filer(self):
        # Israeli filers (CHKP, ALLT) head Item 4 with their own name: 'ITEM 4. INFORMATION ON CHECK POINT',
        # 'ITEM 4: Information on Allot'; the table of contents and in-text cross-references are not the start
        toc = "Item 4.\n\nInformation on Acme Cyber\n\n26\n\nItem 4A.\n\nUnresolved Staff Comments\n\n36\n\n"
        ref = "Details are in \u201cItem 4 \u2013 Information on Acme Cyber\u201d.\n\n"
        for head in ("ITEM 4.\nINFORMATION ON ACME CYBER", "ITEM 4: Information on Acme"):
            text = (toc + ref + head + "\n\nAcme History and Development\n\n" + self.body("f20")
                    + "\n\nITEM 4A.\nUNRESOLVED STAFF COMMENTS\n\nNot applicable.\n\nITEM 5. OPERATING\n")
            s, note = sec.extract_section(text, "20-F")
            self.assertEqual(note, "ok:end=item4a", head)
            self.assertTrue(s.startswith(head.split("\n")[0]), s[:40])
            self.assertIn("History and Development", s)
        # a heading that is not Item 4's own ('Information on' something else, mid-line) is still no start
        self.assertEqual(sec.extract_section("See Item 4 - Information on Acme.\n" + self.body("f20"), "20-F")[1],
                         "start_heading_not_found")

    def test_bounds(self):
        self.assertEqual(sec.extract_section("no headings here", "10-K"), (None, "start_heading_not_found"))
        self.assertEqual(sec.extract_section("Item 1. Business\n" + self.body("x"), "10-K"),
                         (None, "end_heading_not_found"))
        huge = "Item 1. Business\n\n" + ("word " * 130_000) + "\n\nItem 1A. Risk Factors\n"
        s, note = sec.extract_section(huge, "10-K")
        self.assertIsNone(s)
        self.assertTrue(note.startswith("section_too_long"), note)


@unittest.skipUnless(__import__("importlib").util.find_spec("edgar") is not None, "edgartools not installed")
class TestEdgartoolsComparison(unittest.TestCase):
    """Informational only: reports length agreement with edgartools; correctness never depends on it."""

    def test_length_agreement(self):
        import warnings
        try:
            from edgar.documents import ParserConfig, parse_html
        except Exception as e:  # noqa: BLE001 - optional dependency with its own import-time requirements
            self.skipTest(f"edgartools import failed: {e}")
        for t, form, name in (("QKNW", "10-K", "Item 1"), ("QCXL", "20-F", "Item 4")):
            ours, _ = sec.extract_section(fixture_text(t), form)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    theirs = parse_html(DOC_RAW[t].decode("utf-8", "replace"), ParserConfig(form=form)).get_section(name)
                    theirs_len = len(re.sub(r"\s+", " ", theirs.text())) if theirs else None
            except Exception as e:  # noqa: BLE001
                self.skipTest(f"edgartools failed on {t}: {e}")
            ours_len = len(re.sub(r"\s+", " ", ours))
            ratio = (ours_len / theirs_len) if theirs_len else None
            print(f"\n[edgartools comparison] {t} {name}: ours={ours_len} edgartools={theirs_len} "
                  f"ratio={ratio if ratio is None else round(ratio, 3)}", file=sys.stderr)
            self.assertGreater(ours_len, 0)


class TestShortDescription(unittest.TestCase):
    def test_skips_headings_and_boilerplate(self):
        section = ("ITEM 1. BUSINESS\n\nOverview\n\nThis report contains forward-looking statements within the "
                   "meaning of the Private Securities Litigation Reform Act of 1995, which involve risks.\n\n"
                   "Acme Corp makes industrial widgets and sells them to manufacturers worldwide through direct "
                   "sales teams.\n\n12\n\nTable of Contents\n\nWe were founded in 1990 and are headquartered in Ohio, "
                   "where we employ roughly 300 people.")
        d = sec.short_description(section)
        self.assertTrue(d.startswith("Acme Corp makes industrial widgets"), d)
        self.assertIn("founded in 1990", d)
        self.assertNotIn("forward-looking", d)
        self.assertNotIn("Overview", d)
        self.assertNotIn("Table of Contents", d)

    def test_max_chars_and_sentence_cut(self):
        section = "Item 1. Business\n\n" + " ".join(f"Sentence number {i} is here." for i in range(400))
        d = sec.short_description(section, max_chars=500)
        self.assertLessEqual(len(d), 500)
        self.assertTrue(d.endswith("."), d[-20:])
        self.assertEqual(sec.short_description(None), "")
        self.assertEqual(sec.short_description("ITEM 1. BUSINESS\n\nOverview"), "")

    def test_fixtures(self):
        egan = sec.short_description(sec.extract_section(fixture_text("QKNW"), "10-K")[0])
        self.assertTrue(egan.startswith("Quillknow builds knowledge management software"), egan[:80])
        self.assertLessEqual(len(egan), 1500)
        nice = sec.short_description(sec.extract_section(fixture_text("QCXL"), "20-F")[0])
        self.assertIn("Calyx", nice)
        self.assertLessEqual(len(nice), 1500)


# =========================================================================== sync


class SyncBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), min_interval_s=1.0)
        self.env = mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": TEST_UA})
        self.env.start()
        with store.session(self.cfg) as con:
            self.snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="test", request=None,
                                              raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
                                              duration_s=None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def add(self, security_id: str, cap: float | None, isin: str | None = None, tv_type: str = "stock") -> None:
        exchange, symbol = security_id.split(":", 1)
        row = dict(security_id=security_id, exchange=exchange, symbol=symbol, name=symbol, isin=isin, country=None,
                   tv_type=tv_type, tv_subtype="common" if tv_type == "stock" else None, is_primary=True,
                   price_currency=None, fundamental_currency=None, sector=None, industry=None,
                   company_key=store.company_key(isin, security_id), first_seen_snapshot=self.snap,
                   last_seen_snapshot=self.snap, last_seen_at=store.now_utc(), active=True)
        with store.session(self.cfg) as con:
            store.upsert_many(con, "securities", list(row), [list(row.values())])
            store.upsert_many(con, "market_daily", ["security_id", "as_of", "market_cap_usd", "snapshot_id"],
                              [[security_id, dt.date(2026, 9, 26), cap, self.snap]])

    def add_standard(self) -> None:
        self.add("NASDAQ:QCXL", 9e9, "US99902C1027", tv_type="dr")
        self.add("NASDAQ:QFRT", 7e9, "CA99903F1036")
        self.add("NASDAQ:QKNW", 3e8, "US99901K1016")
        self.add("TSE:9901", 2e11, "JP3999010001")   # non-US: never queued

    def q(self, sql: str, params=()) -> list[tuple]:
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            return con.execute(sql, list(params)).fetchall()

    def states(self) -> dict[str, tuple]:
        return {r[0]: r[1:] for r in self.q(
            "SELECT security_id, status, http_status, attempts, note FROM crawl_state WHERE source_id = ?",
            [sec.SOURCE_ID])}

    def last_run(self) -> tuple:
        return self.q("SELECT status, requests, note, finished_at FROM runs ORDER BY started_at DESC LIMIT 1")[0]

    def run_sync(self, client, **kw) -> tuple[dict, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            summary = sec.sync(self.cfg, client, **kw)
        return summary, err.getvalue()


class TestSync(SyncBase):
    def test_requires_user_agent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEVSCREEN_SEC_USER_AGENT", None)
            client = FakeSecClient(standard_pages())
            with self.assertRaises(sec.SecUserAgentMissing):
                sec.sync(self.cfg, client)
            self.assertEqual(client.calls, [])

    def test_full_run(self):
        self.add_standard()
        client = FakeSecClient(standard_pages())
        summary, _ = self.run_sync(client, progress_every=1)
        self.assertEqual(summary["tickers_rows"], len(sec.parse_tickers(TICKERS_RAW)))
        self.assertEqual(summary["securities_mapped"], 3)
        self.assertEqual(summary["ciks_queued"], 3)
        self.assertEqual(summary["ciks_attempted"], 3)
        self.assertEqual(summary["ok_by_form"], {"20-F": 1, "10-K": 1})
        self.assertEqual(summary["not_supported"], 1)
        self.assertEqual(summary["failures_by_note"], {"form_40F_aif_in_exhibit_not_supported": 1})
        self.assertEqual(summary["requests"], 6)
        self.assertIsNone(summary["stopped_reason"])
        self.assertGreater(summary["bytes_downloaded"], len(DOC_RAW["QKNW"]) + len(DOC_RAW["QCXL"]))
        # queue order: market cap desc, tickers first
        self.assertEqual(client.calls, [sec.TICKERS_URL, subs_url("QCXL"), doc_url("QCXL"), subs_url("QFRT"),
                                        subs_url("QKNW"), doc_url("QKNW")])
        for kw in client.kwargs:
            self.assertEqual(kw["rate_key"], "sec.gov")
            self.assertEqual(kw["headers"]["User-Agent"], TEST_UA)
        self.assertGreaterEqual(client.min_interval_s, sec.DEFAULT_MIN_INTERVAL_S)

        ids = dict((r[0], r[1:]) for r in self.q("SELECT security_id, id_type, id_value, method FROM identifiers"))
        self.assertEqual(ids["NASDAQ:QKNW"], ("sec_cik", "9990101", "ticker_exact"))
        self.assertEqual(ids["NASDAQ:QCXL"], ("sec_cik", "9990102", "ticker_exact"))
        self.assertNotIn("TSE:9901", ids)

        st = self.states()
        self.assertEqual(st["NASDAQ:QKNW"][:2], ("ok", 200))
        self.assertEqual(st["NASDAQ:QCXL"][:2], ("ok", 200))
        self.assertEqual(st["NASDAQ:QFRT"][:2], ("form_not_supported", 200))
        self.assertEqual(st["NASDAQ:QFRT"][3], "form_40F_aif_in_exhibit_not_supported")

        docs = self.q("SELECT doc_id, security_id, company_key, cik, form, section, accession, filing_date, "
                      "report_date, url, raw_sha256, raw_bytes, text_path, text_sha256, text_chars, extractor, "
                      "extract_note, snapshot_id FROM documents ORDER BY cik")
        self.assertEqual(len(docs), 2)
        by_cik = {d[3]: d for d in docs}
        e = by_cik["9990101"]
        p = PICKS["QKNW"]
        self.assertEqual(e[0], f"sec_filing_text:9990101:{p['acc']}:item1")
        self.assertEqual(e[1:7], ("NASDAQ:QKNW", "isin:US99901K1016", "9990101", "10-K", "item1", p["acc"]))
        self.assertEqual((e[7], e[8]), (dt.date(2026, 9, 10), dt.date(2026, 6, 30)))
        self.assertEqual(e[9], doc_url("QKNW"))
        self.assertEqual((e[10], e[11]), (store.sha256(DOC_RAW["QKNW"]), len(DOC_RAW["QKNW"])))
        tp = Path(e[12])
        self.assertEqual(tp, self.cfg.home / "docs" / "sec" / "9990101" / f"{p['acc']}-item1.txt")
        body = tp.read_text(encoding="utf-8")
        self.assertEqual((store.sha256(body.encode()), len(body)), (e[13], e[14]))
        self.assertTrue(body.startswith("ITEM 1."))
        self.assertTrue(e[15].startswith("sec_edgar-v2/"))
        self.assertEqual(e[16], "ok:end=item1a")
        n = by_cik["9990102"]
        self.assertEqual((n[4], n[5]), ("20-F", "item4"))
        self.assertTrue(Path(n[12]).name.endswith("-item4.txt"))
        # no full HTML stored anywhere under home
        for f in self.cfg.home.rglob("*"):
            if f.is_file() and f.suffix != ".duckdb":
                self.assertNotIn(b"ix:header", f.read_bytes()[:5_000_000], f)
        tier = self.q("SELECT s.license_tier FROM snapshots n JOIN sources s USING (source_id) WHERE n.snapshot_id=?",
                      [e[17]])[0][0]
        self.assertEqual(tier, "official-private")

        descs = {r[0]: r[1:] for r in self.q("SELECT security_id, source_id, text, match_method, match_score, "
                                              "source_url, snapshot_id FROM descriptions")}
        self.assertTrue(descs["NASDAQ:QKNW"][1].startswith("Quillknow builds"))
        self.assertEqual(descs["NASDAQ:QKNW"][2:5], ("sec_cik", 1.0, doc_url("QKNW")))
        self.assertIsNotNone(descs["NASDAQ:QKNW"][5])
        self.assertNotIn("NASDAQ:QFRT", descs)

        kinds = sorted((r[0], r[1]) for r in self.q("SELECT source_id, kind FROM snapshots WHERE source_id LIKE 'sec%'"))
        self.assertIn(("sec_tickers", "company_tickers_exchange"), kinds)
        self.assertIn(("sec_tickers", "submissions_batch"), kinds)
        self.assertIn(("sec_filing_text", "filing_batch"), kinds)
        self.assertEqual(len(list((self.cfg.raw_dir / "sec_tickers").rglob("submissions-CIK*.json"))), 3)
        run = self.last_run()
        self.assertEqual(run[:2], ("ok", 6))
        self.assertIsNotNone(run[3])

    def test_user_agent_never_recorded(self):
        self.add_standard()
        pages = standard_pages()
        pages[subs_url("QFRT")] = OSError(f"boom while sending {TEST_UA}")   # even an error echoing it is redacted
        summary, output = self.run_sync(FakeSecClient(pages), progress_every=1)
        self.assertIn("[sec_edgar]", output)          # progress was actually logged
        self.assertNotIn(TEST_UA, output)
        self.assertNotIn("jevtest-ua", output)
        self.assertNotIn(TEST_UA, json.dumps(summary, default=str))
        for (rj,) in self.q("SELECT request_json FROM snapshots WHERE source_id LIKE 'sec%'"):
            self.assertNotIn("jevtest-ua", rj)
            self.assertIn(sec.UA_PLACEHOLDER, rj)
        for table in ("snapshots", "crawl_state", "runs", "documents", "descriptions", "identifiers"):
            for row in self.q(f"SELECT * FROM {table}"):
                self.assertNotIn("jevtest-ua", json.dumps(row, default=str), table)
        self.assertIn(sec.UA_PLACEHOLDER, self.states()["NASDAQ:QFRT"][3])
        for f in self.cfg.home.rglob("*"):
            if f.is_file() and f.suffix != ".duckdb":
                self.assertNotIn(b"jevtest-ua", f.read_bytes(), f)

    def test_incremental_skip_and_refresh(self):
        self.add_standard()
        self.run_sync(FakeSecClient(standard_pages()))
        client = FakeSecClient(standard_pages())
        summary, _ = self.run_sync(client)
        self.assertEqual(client.calls, [sec.TICKERS_URL, subs_url("QCXL"), subs_url("QFRT"), subs_url("QKNW")])
        self.assertEqual(summary["skipped_unchanged"], 2)
        self.assertEqual(summary["ok_by_form"], {})
        self.assertEqual(self.states()["NASDAQ:QKNW"][2], 1)        # untouched: attempts stay 1
        client = FakeSecClient(standard_pages())
        summary, _ = self.run_sync(client, refresh=True)
        self.assertIn(doc_url("QKNW"), client.calls)
        self.assertEqual(summary["ok_by_form"], {"20-F": 1, "10-K": 1})
        self.assertEqual(self.states()["NASDAQ:QKNW"][2], 2)
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 2)

    def test_new_accession_is_fetched(self):
        self.add_standard()
        self.run_sync(FakeSecClient(standard_pages()))
        subs = json.loads(SUBS_RAW["QKNW"])
        r = subs["filings"]["recent"]
        i = r["accessionNumber"].index(PICKS["QKNW"]["acc"])
        for k in r:
            r[k] = [r[k][i]] + r[k]
        r["accessionNumber"][0], r["filingDate"][0] = "0001104659-27-000001", "2027-09-10"
        pages = standard_pages()
        pages[subs_url("QKNW")] = json.dumps(subs).encode()
        pages[sec.archive_url(9990101, "0001104659-27-000001", r["primaryDocument"][0])] = DOC_RAW["QKNW"]
        client = FakeSecClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["ok_by_form"], {"10-K": 1})
        self.assertEqual(summary["skipped_unchanged"], 1)
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE cik = '9990101'")[0][0], 2)

    def test_blocked_stops_immediately(self):
        self.add_standard()
        pages = standard_pages()
        pages[subs_url("QFRT")] = Blocked(subs_url("QFRT"), 429, "http_429")
        client = FakeSecClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["stopped_reason"], "blocked")
        self.assertEqual(summary["blocked_at"], PICKS["QFRT"]["cik"])
        self.assertNotIn(subs_url("QKNW"), client.calls)
        st = self.states()
        self.assertEqual(st["NASDAQ:QFRT"][:2], ("blocked", 429))
        self.assertEqual(st["NASDAQ:QCXL"][0], "ok")                 # earlier work was flushed
        self.assertNotIn("NASDAQ:QKNW", st)
        self.assertEqual(self.last_run()[0], "blocked")

    def test_blocked_on_tickers(self):
        self.add_standard()
        client = FakeSecClient({sec.TICKERS_URL: Blocked(sec.TICKERS_URL, 403, "http_403")})
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], client.calls), ("blocked", [sec.TICKERS_URL]))
        self.assertEqual(self.last_run()[0], "blocked")

    def test_consecutive_errors_stop(self):
        for i, t in enumerate(("QQSC", "ORCD", "QGRD", "QMSW", "QAMB", "QKNW", "QCXL")):
            self.add(f"NASDAQ:{t}", 1e12 - i * 1e9)
        client = FakeSecClient({sec.TICKERS_URL: TICKERS_RAW}, default=500)
        summary, _ = self.run_sync(client, batch_size=2)
        self.assertEqual(summary["stopped_reason"], "consecutive_errors")
        self.assertEqual(summary["ciks_attempted"], sec.MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(summary["failures_by_note"], {"submissions_http_500": 5})
        self.assertEqual(len(self.states()), 5)
        self.assertEqual(self.last_run()[0], "stopped_errors")

    def test_keyboard_interrupt_flushes(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("QKNW")] = KeyboardInterrupt()
        summary, _ = self.run_sync(FakeSecClient(pages), batch_size=25)
        self.assertEqual(summary["stopped_reason"], "interrupted")
        st = self.states()
        self.assertEqual(st["NASDAQ:QCXL"][0], "ok")
        self.assertEqual(st["NASDAQ:QFRT"][0], "form_not_supported")
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 1)
        self.assertEqual(self.last_run()[0], "interrupted")

    def test_extract_failed_and_limit(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("QCXL")] = b"<html><body><p>Nothing useful here.</p></body></html>"
        summary, _ = self.run_sync(FakeSecClient(pages), limit=1)
        self.assertEqual((summary["ciks_queued"], summary["ciks_attempted"]), (1, 1))
        st = self.states()
        self.assertEqual(st["NASDAQ:QCXL"][0], "extract_failed")
        self.assertEqual(st["NASDAQ:QCXL"][3], "start_heading_not_found")
        doc = self.q("SELECT text_path, text_chars, extract_note, raw_bytes FROM documents")[0]
        self.assertEqual(doc[:3], (None, None, "start_heading_not_found"))
        self.assertGreater(doc[3], 0)
        self.assertEqual(self.q("SELECT count(*) FROM descriptions")[0][0], 0)

    def test_no_db_connection_held_during_fetch(self):
        self.add_standard()
        # sanity: while this process holds a write session, another process cannot open one
        with store.session(self.cfg):
            self.assertNotEqual(self._probe_write(), 0)
        results: list[tuple[str, int]] = []

        def probe(url: str) -> None:
            results.append((url, self._probe_write()))

        summary, _ = self.run_sync(FakeSecClient(standard_pages(), on_request=probe))
        self.assertEqual(summary["requests"], 6)
        self.assertEqual(len(results), 6)
        for url, code in results:
            self.assertEqual(code, 0, f"DB was locked while fetching {url}")

    def _probe_write(self) -> int:
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from pathlib import Path; from jevscreen import store; from jevscreen.config import Config\n"
                "with store.session(Config(home=Path(sys.argv[2])), wait_s=0) as con:\n"
                "    con.execute('CREATE TABLE IF NOT EXISTS probe (x INTEGER)'); con.execute('INSERT INTO probe VALUES (1)')\n")
        return subprocess.run([sys.executable, "-c", code, str(SRC), str(self.cfg.home)],
                              capture_output=True, timeout=120).returncode


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes):
        self.status, self._body, self.headers = status, body, {}

    def read(self, n: int | None = None) -> bytes:
        return self._body if n is None else self._body[:n]


class FakeOpener:
    def __init__(self, pages: dict[str, bytes]):
        self.pages, self.requests = pages, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        body = self.pages.get(req.full_url)
        if body is None:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b""))
        return _FakeHTTPResponse(200, body)


class TestSyncWithRealClient(SyncBase):
    def test_generic_client_gets_sec_ua_and_shared_limiter(self):
        self.add("NASDAQ:QKNW", 3e8, "US99901K1016")
        opener = FakeOpener({k: v for k, v in standard_pages().items() if isinstance(v, bytes)})
        client = Client(user_agent="Mozilla/5.0 generic", min_interval_s=0.0)
        client._opener = opener
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["ok_by_form"], {"10-K": 1})
        self.assertEqual(len(opener.requests), 3)
        for req in opener.requests:
            self.assertEqual(req.get_header("User-agent"), TEST_UA)
        self.assertEqual(set(client._last), {"sec.gov"})              # www + data share one limiter
        self.assertEqual(client.min_interval_s, sec.DEFAULT_MIN_INTERVAL_S)



# =========================================================================== review fixes


def _subs_rows(rows, files=()):
    cols = ["form", "accessionNumber", "filingDate", "reportDate", "primaryDocument"]
    return {"filings": {"recent": {c: [r[i] for r in rows] for i, c in enumerate(cols)}, "files": list(files)}}


class TestFilingSelectionFixes(unittest.TestCase):
    def test_transition_report_counts_as_annual(self):
        subs = _subs_rows([("10-KT", "T-1", "2026-05-01", "2025-12-31", "t.htm"),
                           ("10-K", "O-1", "2020-02-01", "2019-12-31", "k.htm")])
        f = sec.latest_annual_filing(subs, ("10-K", "20-F"), as_of=dt.date(2026, 9, 26))
        self.assertEqual((f["accession"], f["form"], f["base_form"], f["amendment"], f["flags"]),
                         ("T-1", "10-KT", "10-K", False, []))
        self.assertEqual(sec.section_for_form(f["base_form"]), "item1")

    def test_newer_excluded_form_and_stale_flags(self):
        subs = _subs_rows([("40-F", "F-1", "2026-03-01", "2025-12-31", "f.htm"),
                           ("20-F", "O-1", "2021-04-01", "2020-12-31", "o.htm")])
        f = sec.latest_annual_filing(subs, ("10-K", "20-F"), as_of=dt.date(2026, 9, 26))
        self.assertEqual(f["accession"], "O-1")
        self.assertEqual(f["flags"], ["newer_annual_form_ignored:40-F", "stale:5"])
        fresh = sec.latest_annual_filing(_subs_rows([("10-K", "N", "2026-02-01", "2025-12-31", "n.htm")]),
                                         as_of=dt.date(2026, 9, 26))
        self.assertEqual(fresh["flags"], [])


class TestExtractionFixes(unittest.TestCase):
    body = staticmethod(TestExtractSection.body)

    def test_toc_without_item_word_does_not_win(self):
        toc = ("TABLE OF CONTENTS\nItem 1. Business 3\n1A. Risk Factors 10\n2. Properties 20\n3. Legal Proceedings 21\n"
               "\nForward-looking statements. " + self.body("preamble", 300) + "\n")
        text = (toc + "PART I\nItem 1. Business\n\n" + self.body("realbody") + "\n\nItem 1A. Risk Factors\n\nrisky\n")
        s, note = sec.extract_section(text, "10-K")
        self.assertEqual(note, "ok:end=item1a")
        self.assertNotIn("preamble", s)
        self.assertNotIn("Risk Factors 10", s)
        self.assertIn("realbody", s)

    def test_toc_like_block_after_only_start_is_rejected(self):
        text = ("Item 1. Business 3\n1A.\n2.\n3.\n" + self.body("frontmatter") + "\n\nItem 1A. Risk Factors\n")
        s, note = sec.extract_section(text, "10-K")
        self.assertIsNone(s)
        self.assertTrue(note.startswith("section_too_short_or_toc_only"), note)

    def test_description_of_and_our_business_headings(self):
        for head in ("Item 1. Description of Business", "ITEM 1. OUR BUSINESS", "Item 1 - Description of the Business"):
            text = "PART I\n" + head + "\n\n" + self.body("omega") + "\n\nItem 1A. Risk Factors\n"
            s, note = sec.extract_section(text, "10-K")
            if "the Business" in head:     # 'description of the business' is not accepted (no false starts)
                self.assertIsNone(s)
                continue
            self.assertEqual(note, "ok:end=item1a", head)
            self.assertIn("omega", s)

    def test_20f_item4_subheadings_do_not_end_section(self):
        text = ("ITEM 4. INFORMATION ON THE COMPANY\n\nItem 4A. History and Development of the Company\n\nshort.\n\n"
                "Item 4B. Business Overview\n\n" + self.body("f20") +
                "\n\nITEM 5. OPERATING AND FINANCIAL REVIEW AND PROSPECTS\n")
        s, note = sec.extract_section(text, "20-F")
        self.assertEqual(note, "ok:end=item5")
        self.assertIn("f20", s)
        text2 = ("ITEM 4. INFORMATION ON THE COMPANY\n\n" + self.body("g20") + "\n\nITEM 4A. UNRESOLVED STAFF COMMENTS\n"
                 "\nNone.\n\nITEM 5. OPERATING AND FINANCIAL REVIEW AND PROSPECTS\n")
        self.assertEqual(sec.extract_section(text2, "20-F")[1], "ok:end=item4a")
        text3 = ("ITEM 4. INFORMATION ON THE COMPANY\n\n" + self.body("h20") + "\n\nITEM 4A. Not applicable.\n\n"
                 "ITEM 5. OPERATING AND FINANCIAL REVIEW AND PROSPECTS\n")
        self.assertEqual(sec.extract_section(text3, "20-F")[1], "ok:end=item4a")

    def test_entities_unescaped_once(self):
        html = b"<p>AT&amp;T and Tom&amp;notes, a &amp;lt;b&amp;gt; tag, &amp;para</p>"
        want = "AT&T and Tom&notes, a &lt;b&gt; tag, &para"
        self.assertEqual(sec.html_to_text(html, backend="stdlib"), want)
        if lxml_available():
            self.assertEqual(sec.html_to_text(html, backend="lxml"), want)

    def test_fixtures_unchanged(self):
        e, ne = sec.extract_section(fixture_text("QKNW"), "10-K")
        n, nn = sec.extract_section(fixture_text("QCXL"), "20-F")
        self.assertEqual((ne, len(e)), ("ok:end=item1a", 40740))
        self.assertEqual((nn, len(n)), ("ok:end=item4a", 63306))

    def test_20f_description_from_business_overview(self):
        sect = sec.extract_section(fixture_text("QCXL"), "20-F")[0]
        desc = sec.short_description(sec.description_source(sect, "20-F"))
        self.assertNotIn("founded on March 3, 1994", desc)
        self.assertTrue(desc.startswith("Calyx Listen is a global enterprise software company"), desc[:120])
        self.assertLessEqual(len(desc), 1500)
        egan = sec.extract_section(fixture_text("QKNW"), "10-K")[0]
        self.assertIs(sec.description_source(egan, "10-K"), egan)
        self.assertEqual(sec.description_source("no overview here", "20-F"), "no overview here")


class TestRedaction(unittest.TestCase):
    def test_redact_handles_json_escaped_non_ascii(self):
        from jevscreen.config import redact
        ua = "Jörg Müller jevtest-ua@example.invalid"
        for text in (ua, json.dumps({"n": ua}), json.dumps({"n": ua}, ensure_ascii=False)):
            out = redact(text, [ua], "<ua>")
            self.assertNotIn("jevtest-ua", out)
            self.assertIn("<ua>", out)
        self.assertIsNone(redact(None, [ua]))

    def test_client_repr_hides_user_agent(self):
        self.assertNotIn("jevtest-ua", repr(Client(user_agent=TEST_UA)))


class TestSyncFixes(SyncBase):
    def assert_ua_nowhere(self, needle: str = "jevtest") -> None:
        for table in ("snapshots", "crawl_state", "runs", "documents", "descriptions", "identifiers"):
            for row in self.q(f"SELECT * FROM {table}"):
                dumped = json.dumps(row, default=str, ensure_ascii=False)
                self.assertNotIn(needle, dumped, table)
                self.assertNotIn("JevScreen Tester", dumped, table)
                self.assertNotIn("Tester jev", dumped, table)
        for f in self.cfg.home.rglob("*"):
            if f.is_file() and f.suffix != ".duckdb":
                self.assertNotIn(needle.encode(), f.read_bytes(), f)

    def test_ua_crossing_truncation_boundary_not_stored(self):
        self.add_standard()
        pages = standard_pages()
        pages[subs_url("QFRT")] = OSError("x" * 175 + " UA=" + TEST_UA)
        self.run_sync(FakeSecClient(pages))
        self.assertIn(" UA=<sec-user", self.states()["NASDAQ:QFRT"][3])     # redacted first, then cut
        self.assert_ua_nowhere()

    def test_ua_in_tickers_error_not_stored(self):
        self.add_standard()
        summary, _ = self.run_sync(FakeSecClient({sec.TICKERS_URL: OSError("y" * 190 + " " + TEST_UA)}))
        self.assertEqual(summary["status"], "error")
        self.assert_ua_nowhere()

    def test_non_ascii_ua_not_stored_in_run_note(self):
        ua = "Jörg Müller jevtest-ua@example.invalid"
        self.add_standard()
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": ua}):
            self.run_sync(FakeSecClient({sec.TICKERS_URL: OSError(f"boom {ua}")}))
        self.assertIn(sec.UA_PLACEHOLDER, self.last_run()[2])
        self.assert_ua_nowhere()

    def heavy_filer_pages(self):
        """QKNW whose `recent` block is 1000 424B2 filings; its 10-K sits on the first older page."""
        real = json.loads(SUBS_RAW["QKNW"])["filings"]["recent"]
        i = real["accessionNumber"].index(PICKS["QKNW"]["acc"])
        page = {k: [v[i]] for k, v in real.items()}
        rows = [("424B2", f"0000000000-26-{n:06d}", "2026-09-01", "", f"p{n}.htm") for n in range(1000)]
        subs = _subs_rows(rows, files=[{"name": "CIK0009990101-submissions-001.json", "filingCount": 1},
                                       {"name": "CIK0009990101-submissions-002.json", "filingCount": 1}])
        pages = standard_pages()
        pages[subs_url("QKNW")] = json.dumps(subs).encode()
        pages["https://data.sec.gov/submissions/CIK0009990101-submissions-001.json"] = json.dumps(page).encode()
        return pages

    def test_heavy_filer_searches_older_pages(self):
        self.add("NASDAQ:QKNW", 3e8, "US99901K1016")
        client = FakeSecClient(self.heavy_filer_pages())
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["ok_by_form"], {"10-K": 1})
        self.assertEqual(summary["older_pages"], 1)
        self.assertEqual(client.calls, [sec.TICKERS_URL, subs_url("QKNW"),
                                        "https://data.sec.gov/submissions/CIK0009990101-submissions-001.json",
                                        doc_url("QKNW")])
        for kw in client.kwargs:
            self.assertEqual((kw["rate_key"], kw["headers"]["User-Agent"]), ("sec.gov", TEST_UA))
        note = self.q("SELECT extract_note FROM documents")[0][0]
        self.assertIn("older_page:1", note)
        self.assertEqual(len(list((self.cfg.raw_dir / "sec_tickers").rglob("submissions-CIK0009990101-submissions-001-*.json"))), 1)
        (rj,) = self.q("SELECT request_json FROM snapshots WHERE kind = 'submissions_batch'")[0]
        self.assertIn("submissions-001.json", rj)

    def test_heavy_filer_without_annual_in_pages(self):
        self.add("NASDAQ:QKNW", 3e8, "US99901K1016")
        pages = self.heavy_filer_pages()
        pages["https://data.sec.gov/submissions/CIK0009990101-submissions-001.json"] = json.dumps(
            {"form": ["8-K"], "accessionNumber": ["X"], "filingDate": ["2020-01-01"], "reportDate": [""],
             "primaryDocument": ["x.htm"]}).encode()
        pages["https://data.sec.gov/submissions/CIK0009990101-submissions-002.json"] = 404
        summary, _ = self.run_sync(FakeSecClient(pages))
        self.assertEqual(self.states()["NASDAQ:QKNW"][:2], ("error", 404))
        self.assertEqual(self.states()["NASDAQ:QKNW"][3], "submissions_page_http_404")

    def test_stale_identifier_removed(self):
        self.add_standard()
        self.run_sync(FakeSecClient(standard_pages()))
        tickers = json.loads(TICKERS_RAW)
        tickers["data"] = [r for r in tickers["data"] if r[2] != "QKNW"]
        pages = standard_pages()
        pages[sec.TICKERS_URL] = json.dumps(tickers).encode()
        summary, _ = self.run_sync(FakeSecClient(pages))
        self.assertEqual(summary["identifiers_removed"], 1)
        ids = {r[0] for r in self.q("SELECT security_id FROM identifiers WHERE id_type = 'sec_cik'")}
        self.assertEqual(ids, {"NASDAQ:QCXL", "NASDAQ:QFRT"})

    def test_class_lines_of_one_cik_all_covered(self):
        from jevscreen import coverage
        self.add("NASDAQ:QKNW", 3e8, "US99901K1016")
        self.add("NASDAQ:QKNWB", 1e8, "US99901K9999")        # second class line, different ISIN
        tickers = {"fields": ["cik", "name", "ticker", "exchange"],
                   "data": [[9990101, "Quillknow", "QKNW", "Nasdaq"], [9990101, "Quillknow", "QKNWB", "Nasdaq"]]}
        pages = standard_pages()
        pages[sec.TICKERS_URL] = json.dumps(tickers).encode()
        self.run_sync(FakeSecClient(pages))
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 1)
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            rep = coverage.report(con)
        self.assertEqual(rep["totals"]["sec_document"], 2)

    def test_blocked_writes_cooldown_marker(self):
        from jevscreen import guard
        self.add_standard()
        pages = standard_pages()
        pages[subs_url("QFRT")] = Blocked(subs_url("QFRT"), 429, "http_429")
        self.run_sync(FakeSecClient(pages))
        hit = guard.recent_block_marker(self.cfg, guard.CMD_SYNC_SEC)
        self.assertIsNotNone(hit)
        self.assertIn("http_429", hit["note"])

    def test_block_survives_locked_final_write(self):
        from jevscreen import cli, guard
        self.add_standard()
        pages = standard_pages()
        pages[subs_url("QFRT")] = Blocked(subs_url("QFRT"), 429, "http_429")
        real = store.session
        calls = [0]

        def locked_after_mapping(cfg, **kw):
            calls[0] += 1
            if calls[0] > 2:           # 1: runs row, 2: tickers/mapping; every later write finds the store locked
                raise store.StoreLocked("Could not set lock on file")
            return real(cfg, **kw)

        with mock.patch.object(store, "session", locked_after_mapping), self.assertRaises(store.StoreLocked):
            self.run_sync(FakeSecClient(pages))
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            self.assertIsNone(cli.recent_block(con, "sync-sec"))           # the journal never got 'blocked' ...
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-sec"))   # ... but the marker did
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli._cooldown_refusal("sync-sec", self.cfg, False), cli.EXIT_BLOCKED)

    def test_locked_mid_run_flush_keeps_batch(self):
        self.add_standard()
        real = store.session
        locked = [1]

        def flaky(cfg, *, wait_s=600.0, **kw):
            if wait_s == sec.FLUSH_WAIT_S and locked[0]:
                locked[0] -= 1
                raise store.StoreLocked("Could not set lock on file")
            return real(cfg, wait_s=wait_s, **kw)

        with mock.patch.object(store, "session", flaky):
            summary, err = self.run_sync(FakeSecClient(standard_pages()), batch_size=1)
        self.assertIn("store locked", err)
        self.assertEqual((summary["status"], summary["ok_by_form"]), ("ok", {"20-F": 1, "10-K": 1}))
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 2)
        self.assertEqual(len(self.states()), 3)
        self.assertEqual(self.last_run()[0], "ok")

    def test_store_locked_too_long_stops_run(self):
        self.add_standard()
        real = store.session

        def locked_mid_run(cfg, *, wait_s=600.0, **kw):
            if wait_s == sec.FLUSH_WAIT_S:
                raise store.StoreLocked("Could not set lock on file")
            return real(cfg, wait_s=wait_s, **kw)

        client = FakeSecClient(standard_pages())
        with mock.patch.object(store, "session", locked_mid_run), mock.patch.object(sec, "MAX_LOCKED_FLUSHES", 2):
            summary, _ = self.run_sync(client, batch_size=1)
        self.assertEqual((summary["stopped_reason"], summary["status"], summary["ciks_attempted"]),
                         ("store_locked", "store_locked", 2))
        self.assertNotIn(subs_url("QKNW"), client.calls)
        self.assertEqual(sorted(self.states()), ["NASDAQ:QCXL", "NASDAQ:QFRT"])   # final flush wrote both
        self.assertEqual(self.last_run()[0], "store_locked")

if __name__ == "__main__":
    unittest.main()
