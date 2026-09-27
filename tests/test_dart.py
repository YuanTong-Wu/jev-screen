"""Tests for sources.dart. No network: every client is a fake.

Fixtures tests/fixtures/dart_* are SYNTHETIC (fictional companies, corp codes 0099000x, stock codes 9999x0), shaped
after the OpenDART specification; they are not captured live responses. tests/fixtures/dart_web_* are SYNTHETIC too
(invented 한빛전자 사업보고서, rcpNo 20260310009990; generator tools/synthetic_fixtures/dart_web.py), shaped like the
DART website's main.do (TOC script, standard form headings) and the viewer.do sections '1. 사업의 개요' and
'2. 주요 제품 및 서비스'.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import guard  # noqa: E402
from jevscreen.http import Blocked, Client, RequestTimeout, Response  # noqa: E402
from jevscreen.sources import dart, edinet  # noqa: E402
from test_edinet import FakeClient, SyncBase  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_KEY = "0123456789abcdefDARTtestKEY00000000000a"   # fake 40-character key
AS_OF = dt.date(2026, 9, 26)
BGN, END = "20240718", "20260926"                      # AS_OF - LOOKBACK_DAYS .. AS_OF


def fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def list_url(corp: str) -> str:
    return dart.LIST_URL.format(corp_code=corp, bgn_de=BGN, end_de=END)


def doc_url(rcept_no: str) -> str:
    return dart.DOC_URL.format(rcept_no=rcept_no)


def zip_of(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, b in members:
            zf.writestr(n, b)
    return buf.getvalue()


def standard_pages() -> dict:
    return {
        dart.CORPCODE_URL: fx("dart_CORPCODE.zip"),
        list_url("00990001"): fx("dart_list_00990001.json"),
        list_url("00990002"): fx("dart_list_00990002.json"),
        list_url("00990006"): fx("dart_status_013.json"),
        doc_url("20260318000101"): fx("dart_20260318000101_document.zip"),
        doc_url("20260320000202"): fx("dart_20260320000202_document.zip"),
    }


def doc_xml(body: str, encoding: str = "utf-8") -> str:
    return f'<?xml version="1.0" encoding="{encoding}"?><DOCUMENT><BODY>{body}</BODY></DOCUMENT>'


# =========================================================================== pure functions


class TestCorpCodes(unittest.TestCase):
    def test_parse_zip(self):
        rows = dart.parse_corp_codes(fx("dart_CORPCODE.zip"))
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0], {"corp_code": "00990001", "corp_name": "테스트전자",
                                   "corp_eng_name": "Test Electronics Co., Ltd.", "stock_code": "999990",
                                   "modify_date": "20260301"})
        self.assertIsNone(next(r for r in rows if r["corp_code"] == "00990003")["stock_code"])   # ' ' -> None
        self.assertEqual(next(r for r in rows if r["corp_code"] == "00990007")["corp_name"], "에이앤비&씨")

    def test_parse_malformed_xml_falls_back_to_regex(self):
        xml = "<result><list><corp_code>00990001</corp_code><corp_name>A & B</corp_name>" \
              "<stock_code>999990</stock_code></list></result>"
        rows = dart.parse_corp_codes(zip_of([("CORPCODE.xml", xml.encode())]))
        self.assertEqual((rows[0]["corp_code"], rows[0]["corp_name"], rows[0]["stock_code"]),
                         ("00990001", "A & B", "999990"))

    def test_error_envelope(self):
        with self.assertRaises(edinet.ApiStop) as cm:
            dart.parse_corp_codes(fx("dart_status_010.json"))
        self.assertEqual((cm.exception.reason, cm.exception.status), ("key_rejected", "010"))
        xml = b'<?xml version="1.0" encoding="UTF-8"?><result><status>020</status><message>limit</message></result>'
        with self.assertRaises(edinet.ApiStop) as cm:
            dart.parse_corp_codes(xml)
        self.assertEqual((cm.exception.reason, cm.exception.cooldown), ("quota_exceeded", True))

    def test_mapping(self):
        rows = dart.parse_corp_codes(fx("dart_CORPCODE.zip"))
        secs = [{"security_id": s} for s in ("KRX:999990", "KRX:999980", "KRX:999970", "KRX:123456", "TSE:9999",
                                             "KOSDAQ:999960")]
        rep: dict = {}
        got = dart.map_securities_to_corp(secs, rows, report=rep)
        self.assertEqual(got, [("KRX:999990", "00990001", "stock_code"), ("KRX:999980", "00990002", "stock_code"),
                               ("KOSDAQ:999960", "00990006", "stock_code")])
        self.assertEqual((rep["considered"], rep["non_kr"], rep["unmatched"]), (5, 1, 1))
        self.assertEqual(rep["ambiguous"][0]["corp_codes"], ["00990004", "00990005"])


class TestStatus(unittest.TestCase):
    def test_raise_for_status(self):
        dart.raise_for_status("000")
        dart.raise_for_status(None)
        for status, reason, cooldown in (("020", "quota_exceeded", True), ("101", "access_refused", True),
                                         ("010", "key_rejected", False), ("011", "key_rejected", False),
                                         ("012", "key_rejected", False), ("901", "key_rejected", False),
                                         ("800", "service_unavailable", False)):
            with self.assertRaises(edinet.ApiStop) as cm:
                dart.raise_for_status(status, "m")
            self.assertEqual((cm.exception.reason, cm.exception.cooldown), (reason, cooldown), status)
        with self.assertRaises(dart.DartStatusError):
            dart.raise_for_status("100", "field error")

    def test_envelope_status(self):
        self.assertEqual(dart.envelope_status(fx("dart_document_status_014.xml")), ("014", "파일이 존재하지 않습니다."))
        self.assertEqual(dart.envelope_status(fx("dart_status_020.json"))[0], "020")
        self.assertEqual(dart.envelope_status(b"garbage"), (None, None))


class TestSelection(unittest.TestCase):
    def test_original_preferred_over_later_correction(self):
        rows = json.loads(fx("dart_list_00990001.json"))["list"]
        got = dart.select_business_report(rows, as_of=AS_OF)
        self.assertEqual((got["rcept_no"], got["period_end"], got["amendment"]),
                         ("20260318000101", dt.date(2025, 12, 31), False))
        self.assertEqual(got["flags"], ["corrected_later"])

    def test_correction_only_and_newest_period(self):
        rows = [{"report_nm": "[기재정정]사업보고서 (2025.12)", "rcept_no": "20260415000101", "rcept_dt": "20260415"},
                {"report_nm": "사업보고서 (2024.12)", "rcept_no": "20250319000101", "rcept_dt": "20250319"},
                {"report_nm": "반기보고서 (2026.06)", "rcept_no": "20260814000101", "rcept_dt": "20260814"}]
        got = dart.select_business_report(rows, as_of=AS_OF)
        self.assertEqual(got["rcept_no"], "20260415000101")              # newest period wins, even corrected
        self.assertEqual(got["flags"], ["amendment:기재정정"])

    def test_fiscal_year_end_and_stale(self):
        rows = [{"report_nm": "사업보고서 (2023.03)", "rcept_no": "20230629000101", "rcept_dt": "20230629"}]
        got = dart.select_business_report(rows, as_of=AS_OF)
        self.assertEqual(got["period_end"], dt.date(2023, 3, 31))
        self.assertEqual(got["flags"], ["stale:3"])
        self.assertIsNone(dart.select_business_report([{"report_nm": "사업보고서 (2025.12)", "rcept_no": "bad"}]))
        self.assertIsNone(dart.select_business_report([]))


class TestExtraction(unittest.TestCase):
    def _main(self, name: str, rcept: str) -> str:
        m = dart.main_document(edinet.read_zip(fx(name)), rcept)
        return edinet.decode_bytes(m[1], prefer=("utf-8", "cp949"))[0]

    def test_utf8_fixture_with_toc_titles(self):
        text, note = dart.extract_overview(self._main("dart_20260318000101_document.zip", "20260318000101"))
        self.assertEqual(note, "overview")
        self.assertIn("전력반도체용 방열기판", text)
        self.assertIn("1. 부문별 매출 현황", text)                         # ATOC="N" caption does not end it
        self.assertIn("방열기판 1,234", text)                              # TE cells separated
        self.assertIn("회사의 현황 & 전망", text)                          # entity unescaped
        self.assertNotIn("주요 제품은 방열기판(매출비중", text)               # '2. 주요 제품' excluded
        self.assertNotIn("1985년 설립", text)                              # 'I. 회사의 개요' excluded
        self.assertNotIn("<", text)

    def test_euc_kr_fixture_without_atoc(self):
        raw = dart.main_document(edinet.read_zip(fx("dart_20260320000202_document.zip")), "20260320000202")[1]
        self.assertEqual(edinet.decode_bytes(raw, prefer=("utf-8", "cp949"))[1], "cp949")
        text, note = dart.extract_overview(self._main("dart_20260320000202_document.zip", "20260320000202"))
        self.assertEqual(note, "overview")                                # 'Ⅱ.' (U+2161) normalised to 'II.'
        self.assertIn("항체-약물 접합체(ADC)", text)
        self.assertNotIn("임상 파이프라인 현황", text)

    def test_main_document_choice(self):
        members = [("x_00760.xml", b"<a>" + b"b" * 100 + b"</a>"), ("x.xml", b"<a/>")]
        self.assertEqual(dart.main_document(members, "x")[0], "x.xml")
        self.assertEqual(dart.main_document(members[:1], "x")[0], "x_00760.xml")
        self.assertIsNone(dart.main_document([("a.txt", b"")], "x"))

    def test_fallbacks(self):
        para = "<P>" + "당사는 가상의 조선 기자재를 제조하는 회사로 선박용 엔진 부품을 공급합니다. " * 3 + "</P>"
        whole = doc_xml(f"<TITLE>II. 사업의 내용</TITLE>{para}<TITLE>III. 재무에 관한 사항</TITLE><P>x</P>")
        text, note = dart.extract_overview(whole)
        self.assertEqual(note, "business_section_no_overview_title")
        self.assertIn("조선 기자재", text)
        loose = doc_xml(f"<TITLE>1. 사업의 개요</TITLE>{para}<TITLE>2. 주요 제품</TITLE><P>y</P>")
        self.assertEqual(dart.extract_overview(loose)[1], "overview_without_business_title")
        self.assertEqual(dart.extract_overview(doc_xml("<P>nothing</P>")), (None, "business_section_not_found"))
        short = doc_xml("<TITLE>II. 사업의 내용</TITLE><TITLE>1. 사업의 개요</TITLE><P>짧음</P>"
                        "<TITLE>2. 주요 제품</TITLE>")
        self.assertTrue(dart.extract_overview(short)[1].startswith("section_too_short:"))
        paren = doc_xml(f"<TITLE>II. 사업의 내용</TITLE><TITLE>1. (금융업) 사업의 개요</TITLE>{para}"
                        "<TITLE>2. 영업의 현황</TITLE><P>z</P>")
        self.assertEqual(dart.extract_overview(paren)[1], "overview")

    def test_cr_entity_is_a_line_break(self):
        self.assertEqual(dart.dart_xml_to_text("<P>당사는 로봇을&cr;제조합니다</P><TD>a&CR;b</TD>"),
                         "당사는 로봇을\n제조합니다\na\nb")

    def test_short_description_korean(self):
        text, _ = dart.extract_overview(self._main("dart_20260318000101_document.zip", "20260318000101"))
        desc = edinet.short_description_cjk(text)
        self.assertTrue(desc.startswith("당사는 전력반도체용 방열기판"))
        self.assertNotIn("가. 업계의 현황", desc)
        self.assertNotIn("※", desc)


# =========================================================================== sync


class DartSyncBase(SyncBase):
    key_env = "JEVSCREEN_OPENDART_API_KEY"
    key = TEST_KEY
    mode = "zip"

    def setUp(self) -> None:
        super().setUp()
        for name, value in (("WEB_MIN_INTERVAL_S", 0.0),              # fake clients: no real 1 s waits
                            ("CORPCODE_MIN_ROWS", 5),                 # the synthetic corpCode fixture has 7 rows
                            ("CORPCODE_MIN_STOCK_CODES", 3)):
            patcher = mock.patch.object(dart, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_standard(self) -> None:
        self.add("KRX:999990", 2e10, "KR7999990001")
        self.add("KRX:999980", 1e9, "KR7999980002")
        self.add("KRX:999970", 5e8, "KR7999970003")      # ambiguous stock code: not queued
        self.add("KRX:999960", 1e8, "KR7999960004")      # no business report (013)
        self.add("TSE:9999", 9e9, "JP3999999991")        # not a KR venue

    def run_sync(self, client, **kw):
        import contextlib
        kw.setdefault("as_of", AS_OF)
        kw.setdefault("mode", self.mode)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            summary = dart.sync(self.cfg, client, **kw)
        return summary, err.getvalue()


class TestDartSync(DartSyncBase):
    """mode 'zip' (document.xml), kept behind sync(mode='zip')."""

    def test_missing_key_raises_before_any_request(self):
        os.environ.pop(self.key_env, None)
        client = FakeClient(standard_pages())
        with self.assertRaises(dart.DartApiKeyMissing) as cm:
            dart.sync(self.cfg, client)
        self.assertIsInstance(cm.exception, KeyError)
        self.assertIn("opendart_api_key", str(cm.exception))
        self.assertIn("JEVSCREEN_OPENDART_API_KEY", str(cm.exception))
        self.assertEqual(client.calls, [])

    def test_full_run(self):
        self.add_standard()
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual((summary["corp_codes"], summary["securities_mapped"], summary["companies_queued"]), (7, 3, 3))
        self.assertEqual(summary["requests"], 1 + 3 + 2)
        self.assertEqual(summary["ok_documents"], 2)
        for u in client.calls:
            self.assertIn("crtfc_key=" + TEST_KEY, u)
        self.assertTrue(all(kw.get("rate_key") == "dart" for kw in client.kwargs))
        self.assertNotIn(doc_url("20260415000101"), client.public_calls())   # the later correction is not used
        st = self.states(dart.SOURCE_ID)
        self.assertEqual(st["KRX:999990"][:2], ("ok", 200))
        self.assertIn("corrected_later", st["KRX:999990"][3])
        self.assertEqual(st["KRX:999980"][0], "ok")
        self.assertIn("enc:cp949", st["KRX:999980"][3])
        self.assertEqual((st["KRX:999960"][0], st["KRX:999960"][3]),
                         ("no_annual_filing", f"no_business_report_since_{BGN}"))
        self.assertNotIn("KRX:999970", st)
        ids = {r[0]: r[1] for r in self.q("SELECT security_id, id_value FROM identifiers "
                                          "WHERE id_type = 'dart_corp_code'")}
        self.assertEqual(ids, {"KRX:999990": "00990001", "KRX:999980": "00990002", "KRX:999960": "00990006"})
        d = self.q("SELECT security_id, company_key, source_id, cik, form, section, accession, filing_date, "
                   "report_date, url, text_path, extractor FROM documents "
                   "WHERE doc_id = 'dart_business_report:00990001:20260318000101:business'")[0]
        self.assertEqual(d[:10], ("KRX:999990", "isin:KR7999990001", "dart_business_report", None, "사업보고서",
                                  "business", "20260318000101", dt.date(2026, 3, 18), dt.date(2025, 12, 31),
                                  dart.VIEWER_URL.format(rcept_no="20260318000101")))
        self.assertTrue(d[10].endswith("docs/dart/00990001/20260318000101-business.txt"))
        self.assertIn("질화규소 방열기판", Path(d[10]).read_text(encoding="utf-8"))
        self.assertEqual(d[11], "dart-v1/xml")
        desc = self.q("SELECT lang, match_method, text FROM descriptions WHERE security_id = 'KRX:999990'")[0]
        self.assertEqual(desc[:2], ("ko", "dart_corp_code"))
        self.assertTrue(desc[2].startswith("당사는 전력반도체용"))
        self.assertEqual(self.last_run()[:2], ("sync-dart", "ok"))
        self.assert_key_nowhere()

    def test_incremental_skip_and_refresh(self):
        self.add_standard()
        self.run_sync(FakeClient(standard_pages()))
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client)
        # corpCode.xml is not downloaded again: the raw copy saved by the first run is reused
        self.assertEqual((summary["skipped_unchanged"], summary["requests"], summary["corpcode_reused"]), (2, 3, 1))
        self.assertFalse(any("document.xml" in u for u in client.calls))
        summary, _ = self.run_sync(FakeClient(standard_pages()), refresh=True)
        self.assertEqual((summary["skipped_unchanged"], summary["ok_documents"]), (0, 2))

    def test_quota_exceeded_stops_like_blocked(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("00990002")] = fx("dart_status_020.json")
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("quota_exceeded", "blocked"))
        self.assertNotIn(list_url("00990006"), client.public_calls())
        st = self.states(dart.SOURCE_ID)
        self.assertEqual(st["KRX:999990"][0], "ok")                       # earlier work flushed
        self.assertEqual((st["KRX:999980"][0], st["KRX:999980"][3]), ("blocked", "quota_exceeded:020"))
        hit = guard.recent_block_marker(self.cfg, "sync-dart")
        self.assertIsNotNone(hit)
        self.assertIn("quota_exceeded", hit["note"])
        self.assertEqual(self.last_run()[1], "blocked")
        self.assert_key_nowhere()

    def test_key_rejected_on_corpcode_stops_before_anything_else(self):
        self.add_standard()
        client = FakeClient({dart.CORPCODE_URL: fx("dart_status_010.json")})
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"], len(client.calls)),
                         ("key_rejected", "error", 1))
        self.assertIsNone(guard.recent_block_marker(self.cfg, "sync-dart"))
        self.assertEqual(self.last_run()[1], "error")

    def test_key_rejected_on_list_stops(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("00990001")] = fx("dart_status_010.json")
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["stopped_reason"], "key_rejected")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.states(dart.SOURCE_ID), {})

    def test_document_status_014_is_an_item_error(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("20260318000101")] = fx("dart_document_status_014.xml")
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual(summary["status"], "ok")
        st = self.states(dart.SOURCE_ID)
        self.assertEqual((st["KRX:999990"][0], st["KRX:999990"][3]), ("error", "document_status_014"))
        self.assertEqual(st["KRX:999980"][0], "ok")

    def test_other_list_status_is_an_item_error(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("00990001")] = json.dumps({"status": "100", "message": "필드의 부적절한 값"}).encode()
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][3], "list_status_100")
        self.assertEqual(summary["status"], "ok")

    def test_blocked_on_document(self):
        self.add_standard()
        pages = standard_pages()

        def blocked(url: str):
            raise Blocked(url, 403, "http_403")
        pages[doc_url("20260318000101")] = blocked
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["blocked_at"]), ("blocked", "00990001"))
        self.assertEqual(len(client.calls), 3)
        hit = guard.recent_block_marker(self.cfg, "sync-dart")
        self.assertIn(doc_url("20260318000101"), hit["note"])
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][:2], ("blocked", 403))
        self.assert_key_nowhere()

    def test_blocked_on_corpcode(self):
        client = FakeClient({dart.CORPCODE_URL: Blocked(dart.CORPCODE_URL, 429, "http_429")})
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("blocked", "blocked"))

    def test_extract_failed(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("20260318000101")] = zip_of([("20260318000101.xml", doc_xml("<P>빈 문서</P>").encode())])
        self.run_sync(FakeClient(pages))
        st = self.states(dart.SOURCE_ID)
        self.assertEqual(st["KRX:999990"][0], "extract_failed")
        self.assertTrue(st["KRX:999990"][3].startswith("business_section_not_found"))
        row = self.q("SELECT text_path, raw_bytes FROM documents WHERE security_id = 'KRX:999990'")[0]
        self.assertIsNone(row[0])
        self.assertGreater(row[1], 0)

    def test_keyboard_interrupt_flushes(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("00990002")] = KeyboardInterrupt()
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("interrupted", "interrupted"))
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][0], "ok")
        self.assertEqual(self.last_run()[1], "interrupted")

    def test_deadlines_passed_to_client(self):
        self.add_standard()
        client = FakeClient(standard_pages())
        self.run_sync(client)
        by_url = {client.public(u): kw for u, kw in zip(client.calls, client.kwargs)}
        self.assertEqual(by_url[dart.CORPCODE_URL]["deadline_s"], dart.CORPCODE_DEADLINE_S)
        self.assertEqual(by_url[list_url("00990001")]["deadline_s"], dart.LIST_DEADLINE_S)
        self.assertEqual(by_url[doc_url("20260318000101")]["deadline_s"], dart.DOC_DEADLINE_S)
        self.assertEqual((dart.LIST_DEADLINE_S, dart.DOC_DEADLINE_S, dart.CORPCODE_DEADLINE_S), (60.0, 300.0, 900.0))

    def test_document_deadline_is_an_item_error_and_run_goes_on(self):
        self.add_standard()
        pages = standard_pages()
        full = edinet.with_key(doc_url("20260318000101"), dart.KEY_PARAM, TEST_KEY)
        pages[doc_url("20260318000101")] = RequestTimeout(full, 300.0, 300.4, 81920, 200)
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["status"], "ok")
        st = self.states(dart.SOURCE_ID)
        self.assertEqual(st["KRX:999990"][:2], ("error", 200))
        self.assertTrue(st["KRX:999990"][3].startswith("deadline"))
        self.assertEqual(st["KRX:999980"][0], "ok")                       # the run went on
        self.assertEqual(st["KRX:999960"][0], "no_annual_filing")
        self.assertEqual(summary["failures_by_note"]["deadline"], 1)
        self.assertEqual(self.last_run()[:2], ("sync-dart", "ok"))
        self.assertIsNone(guard.recent_block_marker(self.cfg, "sync-dart"))   # not a block
        self.assert_key_nowhere()

    def test_consecutive_deadlines_stop_the_run(self):
        self.add_standard()
        pages = standard_pages()
        for r in ("20260318000101", "20260320000202"):
            pages[doc_url(r)] = RequestTimeout("u", 300.0, 300.0, 10)
        with mock.patch.object(edinet, "MAX_CONSECUTIVE_ERRORS", 2):
            summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("consecutive_errors", "stopped_errors"))
        self.assertEqual(summary["failures_by_note"], {"deadline": 2})

    def test_stalled_document_with_real_client(self):
        """End to end through http.Client: the document body trickles (or its headers come too late), the deadline
        ends it, 'error' / 'deadline' is recorded and the next company is processed."""
        pages = {k: v for k, v in standard_pages().items()}
        stalled = doc_url("20260318000101")
        key_re = re.compile(r"[?&]crtfc_key=[^&]*")

        class Body:
            def __init__(self, data: bytes):
                self.status, self.headers, self._b, self.closed = 200, {}, io.BytesIO(data), False

            def read(self, n=-1):
                return self._b.read(n)

            def close(self):
                self.closed = True

        class Trickle(Body):
            def read1(self, n=-1):
                time.sleep(0.02)
                return b"P"

            read = read1

        class Opener:
            def __init__(self, open_delay: float = 0.0):
                self.calls: list[str] = []
                self.open_delay = open_delay

            def open(self, req, timeout=None):
                pub = key_re.sub("", req.full_url)
                self.calls.append(pub)
                if pub == stalled:
                    if self.open_delay:
                        time.sleep(self.open_delay)
                    return Trickle(b"")
                if pub not in pages:
                    raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b""))
                return Body(pages[pub])

        self.add_standard()
        for open_delay, doc_calls, received in ((0.0, 1, True), (0.4, 2, False)):
            with self.subTest(open_delay=open_delay):
                client = Client(user_agent="test", min_interval_s=0.0)
                client._opener = Opener(open_delay)
                with mock.patch.object(dart, "DOC_DEADLINE_S", 0.3), mock.patch.object(Client, "_backoff"), \
                        mock.patch.object(dart, "DEFAULT_MIN_INTERVAL_S", 0.0):
                    t = time.monotonic()
                    summary, _ = self.run_sync(client, refresh=True)
                self.assertLess(time.monotonic() - t, 10)
                self.assertEqual(summary["status"], "ok")
                self.assertEqual(client._opener.calls.count(stalled), doc_calls)   # retried only when nothing came
                st = self.states(dart.SOURCE_ID)
                self.assertEqual(st["KRX:999990"][0], "error")
                self.assertTrue(st["KRX:999990"][3].startswith("deadline;0.3s;received:"))
                self.assertEqual(st["KRX:999990"][3].endswith("received:0"), not received)
                self.assertEqual(st["KRX:999980"][0], "ok")
                self.assert_key_nowhere()

    def test_limit_codes_min_mcap(self):
        self.add_standard()
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client, limit=1)
        self.assertEqual((summary["companies_queued"], summary["requests"]), (1, 3))
        summary, _ = self.run_sync(FakeClient(standard_pages()), codes="999980,KRX:999960")
        self.assertEqual(summary["companies_queued"], 2)
        summary, _ = self.run_sync(FakeClient(standard_pages()), min_mcap_usd=5e9)
        self.assertEqual(summary["lines"], 1)


# =========================================================================== website (mode 'web')

WEB_RCPT = "20260310009990"
WEB_OVERVIEW = {"rcpNo": WEB_RCPT, "dcmNo": "9990488", "eleId": "10", "offset": "89264", "length": "1873",
                "dtd": "dart4.xsd"}
WEB_PRODUCTS = {"rcpNo": WEB_RCPT, "dcmNo": "9990488", "eleId": "11", "offset": "91141", "length": "3310",
                "dtd": "dart4.xsd"}


def main_url(rcept_no: str) -> str:
    return dart.WEB_MAIN_URL.format(rcept_no=rcept_no)


def viewer_url(p: dict) -> str:
    return (f"https://dart.fss.or.kr/report/viewer.do?rcpNo={p['rcpNo']}&dcmNo={p['dcmNo']}&eleId={p['eleId']}"
            f"&offset={p['offset']}&length={p['length']}&dtd={p['dtd']}")


def js_node(var: str, fields: dict, *, decl: bool = True, quote_char: str = '"') -> str:
    q = quote_char
    body = "".join(f"\t{var}['{k}'] = {q}{v}{q};\n" for k, v in fields.items())
    return (f"\tvar {var} = {{}};\n" if decl else "") + body


def toc_page(script: str) -> str:
    return ("<html><head><script>\nfunction initPage() { makeToc(); }\n"
            f"function makeToc() {{\n\tvar treeData = [];\n{script}\n}}\n"
            "function viewDoc(rcpNo, dcmNo) { var node1 = {}; node1['text'] = 'not toc'; }\n"
            "</script></head><body></body></html>")


def rng(ele: int, offset: int, length: int = 1000, rcp: str = "20260320000202") -> dict:
    return {"rcpNo": rcp, "dcmNo": "9990202", "eleId": str(ele), "offset": str(offset), "length": str(length),
            "dtd": "dart4.xsd"}


def texts(found: dict) -> tuple:
    return tuple((found[k] or {}).get("text") for k in ("chapter", "overview", "products"))


class TestWebToc(unittest.TestCase):
    def setUp(self):
        self.nodes = dart.parse_toc(fx("dart_web_main_20260310009990.html").decode("utf-8"))

    def test_fixture_main_page(self):
        self.assertEqual(len(self.nodes), 134)
        found = dart.find_business_sections(self.nodes)
        self.assertEqual(texts(found), ("II. 사업의 내용", "1. 사업의 개요", "2. 주요 제품 및 서비스"))
        self.assertEqual(found["notes"], [])
        self.assertEqual(dart.section_params(found["overview"]), WEB_OVERVIEW)
        self.assertEqual(dart.section_params(found["products"]), WEB_PRODUCTS)
        self.assertEqual(dart.section_url(found["overview"]), viewer_url(WEB_OVERVIEW))
        self.assertEqual(dart.section_url(found["products"]), viewer_url(WEB_PRODUCTS))
        self.assertEqual((found["chapter"]["eleId"], found["chapter"]["offset"], found["chapter"]["length"]),
                         ("9", "89128", "88412"))
        self.assertEqual((found["overview"]["level"], found["chapter"]["level"]), (2, 1))
        self.assertEqual(self.nodes[found["overview"]["parent"]]["text"], "II. 사업의 내용")
        n3 = next(n for n in self.nodes if n["text"] == "2-1. 연결 재무상태표")
        self.assertEqual((n3["level"], self.nodes[n3["parent"]]["level"]), (3, 2))
        self.assertEqual(self.nodes[0]["text"], "사 업 보 고 서")
        self.assertNotIn("not toc", [n["text"] for n in self.nodes])

    def test_field_order_quotes_and_declarations(self):
        ov = {"length": "2000", "offset": "1500", "eleId": "5", "dcmNo": "9990202", "text": "1.사업의 개요",
              "dtd": "dart4.xsd", "rcpNo": "20260320000202"}
        script = (js_node("node1", {"text": "II. 사업의 내용", **rng(4, 1000, 9000)})
                  + js_node("node2", ov, quote_char="'")
                  + "\tnode1['children'].push(node2);\n"
                  + "\tnode2 = {};\n\tnode2.offset = 3600; node2.length = 900; node2.dcmNo = \"9990202\";"
                    " node2.text = \"2. 주요 제품, 서비스 등\"; node2.eleId = \"6\";\n")
        nodes = dart.parse_toc(toc_page(script))
        self.assertEqual([n["level"] for n in nodes], [1, 2, 2])
        found = dart.find_business_sections(nodes)
        self.assertEqual(texts(found), ("II. 사업의 내용", "1.사업의 개요", "2. 주요 제품, 서비스 등"))
        self.assertEqual(dart.section_params(found["overview"])["offset"], "1500")
        p = dart.section_params(found["products"], "20260320000202")
        self.assertEqual((p["rcpNo"], p["offset"], p["length"], p["dtd"]), ("20260320000202", "3600", "900",
                                                                             "dart4.xsd"))

    def test_escapes_and_entities(self):
        script = js_node("node1", {"text": "II. 사업의 \\\"내용\\\" &amp; x", **rng(1, 1)})
        self.assertEqual(dart.parse_toc(toc_page(script))[0]["text"], 'II. 사업의 "내용" & x')

    def test_title_key(self):
        for title in ("1. 사업의 개요", "1.사업의 개요", "가. 사업의 개요", "1. (금융업) 사업의 개요", "(1) 사업의 개요",
                      "1 . 사업의  개요", "① 사업의 개요"):
            self.assertEqual(dart.title_key(title), "사업의개요", title)
        self.assertEqual(dart.title_key("Ⅱ. 사업의 내용"), "사업의내용")         # U+2161 via NFKC
        self.assertEqual(dart.title_key("II.사업의 내용"), "사업의내용")
        self.assertEqual(dart.title_key("2-1. 연결 재무상태표"), "연결재무상태표")
        self.assertEqual(dart.title_key("II 사업의 내용"), "사업의내용")           # numbering without '.'
        self.assertEqual(dart.title_key("1 사업의 개요"), "사업의개요")
        self.assertEqual(dart.title_key("2025년 사업의 개요"), "2025년사업의개요")   # a year is not numbering

    def test_numbering_without_dot_found(self):
        script = (js_node("node1", {"text": "II 사업의 내용", **rng(4, 1000, 50000)})
                  + js_node("node2", {"text": "1 사업의 개요", **rng(5, 1100)})
                  + js_node("node2", {"text": "2 주요 제품 및 서비스", **rng(6, 2200)}))
        found = dart.find_business_sections(dart.parse_toc(toc_page(script)))
        self.assertEqual(texts(found), ("II 사업의 내용", "1 사업의 개요", "2 주요 제품 및 서비스"))
        self.assertEqual(found["notes"], [])

    def test_multiple_overviews_noted(self):
        script = (js_node("node1", {"text": "II. 사업의 내용", **rng(4, 1000, 50000)})
                  + js_node("node2", {"text": "1. (제조서비스업) 사업의 개요", **rng(5, 1100)})
                  + js_node("node3", {"text": "가. 사업의 개요 요약", **rng(6, 1200)})     # nested: not counted
                  + js_node("node2", {"text": "2. (제조서비스업) 주요 제품 및 서비스", **rng(7, 2200)})
                  + js_node("node2", {"text": "5. (금융업) 사업의 개요", **rng(8, 3300)})
                  + js_node("node2", {"text": "6. (금융업) 주요 제품 및 서비스", **rng(9, 4400)}))
        found = dart.find_business_sections(dart.parse_toc(toc_page(script)))
        self.assertEqual(texts(found)[1:], ("1. (제조서비스업) 사업의 개요", "2. (제조서비스업) 주요 제품 및 서비스"))
        self.assertEqual(found["notes"], ["multiple_overview:2", "multiple_products:2"])

    def test_nested_financial_chapter(self):
        script = (js_node("node1", {"text": "I. 회사의 개요", **rng(2, 100)})
                  + js_node("node2", {"text": "1. 사업의 개요 요약", **rng(3, 200)})      # outside the chapter
                  + js_node("node1", {"text": "Ⅱ. 사업의 내용", **rng(4, 1000, 50000)})
                  + js_node("node2", {"text": "1. 금융업", "rcpNo": "20260320000202"})     # heading without range
                  + js_node("node3", {"text": "가. 사업의 개요", **rng(6, 1200)})
                  + js_node("node3", {"text": "나. 주요 제품 및 서비스", **rng(7, 2300)})
                  + js_node("node2", {"text": "2. 영업의 현황", **rng(8, 3400)}))
        found = dart.find_business_sections(dart.parse_toc(toc_page(script)))
        self.assertEqual(texts(found), ("II. 사업의 내용", "가. 사업의 개요", "나. 주요 제품 및 서비스"))  # NFKC

    def test_products_inside_overview_ignored_and_missing_products(self):
        script = (js_node("node1", {"text": "II. 사업의 내용", **rng(4, 1000, 50000)})
                  + js_node("node2", {"text": "1. 사업의 개요", **rng(5, 1100, 5000)})
                  + js_node("node3", {"text": "나. 주요 제품", **rng(6, 3000)})
                  + js_node("node2", {"text": "3. 원재료 및 생산설비", **rng(7, 6200)}))
        found = dart.find_business_sections(dart.parse_toc(toc_page(script)))
        self.assertEqual(texts(found), ("II. 사업의 내용", "1. 사업의 개요", None))

    def test_chapter_fallback_and_no_chapter(self):
        only_chapter = js_node("node1", {"text": "II. 사업의 내용", **rng(4, 1000, 50000)}) + \
            js_node("node1", {"text": "III. 재무에 관한 사항", **rng(5, 51000)})
        found = dart.find_business_sections(dart.parse_toc(toc_page(only_chapter)))
        self.assertEqual(texts(found), ("II. 사업의 내용", None, None))
        no_range = js_node("node1", {"text": "II. 사업의 내용", "rcpNo": "20260320000202"})
        self.assertEqual(texts(dart.find_business_sections(dart.parse_toc(toc_page(no_range)))), (None, None, None))
        loose = js_node("node1", {"text": "1. 사업의 개요", **rng(4, 1000)}) + \
            js_node("node1", {"text": "2. 주요 제품 및 서비스", **rng(5, 2000)})
        found = dart.find_business_sections(dart.parse_toc(toc_page(loose)))
        self.assertEqual(texts(found), (None, "1. 사업의 개요", "2. 주요 제품 및 서비스"))
        self.assertEqual(found["notes"], ["overview_without_business_title"])

    def test_no_toc(self):
        self.assertEqual(dart.parse_toc("<html><body>잘못된 접근입니다.</body></html>"), [])
        self.assertEqual(dart.parse_toc(toc_page("")), [])


class TestWebHtml(unittest.TestCase):
    def test_overview_fixture(self):
        text = dart.section_text(fx("dart_web_section_overview_20260310009990.html").decode("utf-8"), "1. 사업의 개요")
        self.assertTrue(text.startswith("당사는 본사를 거점으로"))
        self.assertTrue(text.endswith("Cobalt Sensing 등(알파벳순)이 있습니다."))
        self.assertIn("글로벌 전자 기업입니다.\n\n사업별로 보면", text)                  # paragraph break kept
        self.assertIn("생산ㆍ판매하고 있습니다.\n또한, 자회사 한빛오디오에서는", text)               # <BR/> is a line break
        self.assertIn("'다. 사업부문별 현황'과 '라. 사업부문별 요약 재무 현황'", text)      # &nbsp; collapsed
        self.assertNotIn("<", text)
        self.assertNotIn("&nbsp;", text)
        self.assertNotIn("\n\n\n", text)
        desc = edinet.short_description_cjk(text)
        self.assertTrue(desc.startswith("당사는 본사를 거점으로"))

    def test_products_fixture_tables(self):
        text = dart.section_text(fx("dart_web_section_products_20260310009990.html").decode("utf-8"),
                                 "2. 주요 제품 및 서비스")
        self.assertTrue(text.startswith("가. 주요 제품 매출\n\n당사는 센서 모듈, 전원 모듈"))
        lines = text.split("\n")
        self.assertIn("부 문 | 주요 제품 | 매출액 | 비중", lines)
        self.assertIn("완제품 부문 | 센서 모듈, 전원 모듈, 산업용 카메라, 제어 보드 등 | 18,796 | 56.3%",
                      lines)                                                          # <BR/> in a cell -> space
        self.assertIn("총 계 | 33,360 | 100.00%", lines)
        self.assertIn("※ 각 부문별 매출액은 부문 등 간 내부거래를 포함하고 있습니다. | [△는 부(-)의 값임]", lines)
        self.assertIn("(단위 : 억원, %)", lines)
        self.assertIn("나. 주요 제품 등의 가격 변동 현황", lines)
        self.assertNotIn("2. 주요 제품 및 서비스", text)

    def test_entities_unescaped_once(self):
        html = "<P>A &amp;lt;b&amp;gt; &lt;c&gt;</P><TABLE><TR><TD>x &amp;amp; y</TD><TD>&lt;z&gt;</TD></TR></TABLE>"
        self.assertEqual(dart.dart_html_to_text(html), "A &lt;b&gt; <c>\n\nx &amp; y | <z>")

    def test_title_line_dropped_only_when_it_matches(self):
        html = "<P class='section-2'>1.사업의 개요</P><P>본문입니다.</P>"
        self.assertEqual(dart.section_text(html, "1. 사업의 개요"), "본문입니다.")
        self.assertEqual(dart.section_text(html, "2. 주요 제품"), "1.사업의 개요\n\n본문입니다.")

    def test_title_broken_by_br_dropped(self):
        html = "<P><BR/></P><P class='section-2'><A name='toc1'>1. 사업의<BR/>개요</A></P><P>본문입니다.</P>"
        self.assertEqual(dart.section_text(html, "1. 사업의 개요"), "본문입니다.")
        plain = "<P>1. 사업의<BR/>개요</P><P>본문입니다.</P>"                     # no section class: lines joined
        self.assertEqual(dart.section_text(plain, "1. 사업의 개요"), "본문입니다.")
        later = "<P>머리말</P><P class='section-2'>1. 사업의 개요</P><P>본문</P>"   # not leading: kept
        self.assertEqual(dart.section_text(later, "1. 사업의 개요"), "머리말\n\n1. 사업의 개요\n\n본문")

    def test_block_tags_inside_cells_separate_text(self):
        html = "<TABLE><TR><TD><P>첫째 줄</P><P>둘째 줄</P></TD><TD>a<DIV>b</DIV></TD></TR></TABLE>"
        self.assertEqual(dart.dart_html_to_text(html), "첫째 줄 둘째 줄 | a b")

    def test_nested_tables_inside_out(self):
        html = ("<TABLE><TR><TD>x</TD><TD><TABLE><TR><TD>i1</TD><TD>i2</TD></TR><TR><TD>i3</TD></TR></TABLE></TD>"
                "<TD>y</TD></TR><TR><TD>r2a</TD><TD>r2b &amp;amp; c</TD></TR></TABLE><P>끝</P>")
        self.assertEqual(dart.dart_html_to_text(html), "x | i1 | i2 i3 | y\nr2a | r2b &amp; c\n\n끝")

    def test_section_verified(self):
        for name, title in (("dart_web_section_overview_20260310009990.html", "1. 사업의 개요"),
                            ("dart_web_section_products_20260310009990.html", "2. 주요 제품 및 서비스")):
            self.assertTrue(dart.section_verified(fx(name).decode("utf-8"), title))
        self.assertTrue(dart.section_verified("<P>2. 주요 제품 및 서비스</P><P>본문</P>", "2. 주요 제품 및 서비스"))
        for page in ("", "<html><body>잠시 후 다시 시도해 주십시오.</body></html>",
                     "<html><head><title>오류</title></head><body><P>요청하신 페이지를 찾을 수 없습니다.</P></body></html>"):
            self.assertFalse(dart.section_verified(page, "1. 사업의 개요"), page)

    def test_pacer(self):
        now = [100.0]
        slept: list[float] = []

        def sleep(s):
            slept.append(s)
            now[0] += s
        pacer = dart.WebPacer(1.0, clock=lambda: now[0], sleep=sleep)
        pacer.wait()
        now[0] += 0.3
        pacer.wait()
        now[0] += 2.5
        pacer.wait()
        self.assertEqual([round(s, 6) for s in slept], [0.7])
        self.assertEqual(dart.WEB_MIN_INTERVAL_S, 1.0)                  # the default used by sync()


def web_main_for(rcept_no: str) -> bytes:
    return fx("dart_web_main_20260310009990.html")


CHAPTER_RANGE = rng(4, 1000, 50000)
CHAPTER_HTML = ("<HTML><BODY><P class='section-1'>II. 사업의 내용</P><P>당사는 가상의 항체-약물 접합체(ADC) 플랫폼을 "
                "개발하는 바이오 기업으로, 국내외 제약사와 기술이전 계약을 맺고 있습니다. 주요 파이프라인은 "
                "고형암 치료제입니다.</P><TABLE><TR><TD>과제</TD><TD>단계</TD></TR><TR><TD>ADC-1</TD><TD>임상 1상</TD>"
                "</TR></TABLE></BODY></HTML>").encode("utf-8")


def web_pages() -> dict:
    pages = {k: v for k, v in standard_pages().items() if "document.xml" not in k}
    pages[main_url("20260318000101")] = fx("dart_web_main_20260310009990.html")
    pages[viewer_url(WEB_OVERVIEW)] = fx("dart_web_section_overview_20260310009990.html")
    pages[viewer_url(WEB_PRODUCTS)] = fx("dart_web_section_products_20260310009990.html")
    pages[main_url("20260320000202")] = toc_page(
        js_node("node1", {"text": "I. 회사의 개요", **rng(3, 100, 800)})
        + js_node("node1", {"text": "II. 사업의 내용", **CHAPTER_RANGE})).encode("utf-8")
    pages[viewer_url(CHAPTER_RANGE)] = CHAPTER_HTML
    return pages


class TestDartWebSync(DartSyncBase):
    mode = "web"

    def test_full_run(self):
        self.add_standard()
        client = FakeClient(web_pages())
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["status"], summary["mode"], summary["ok_documents"]), ("ok", "web", 2))
        self.assertEqual((summary["web_sections"], summary["corpcode_reused"]), (3, 0))
        self.assertEqual(summary["requests"], 1 + 3 + (1 + 2) + (1 + 1))
        pub = client.public_calls()
        self.assertEqual(pub[:5], [dart.CORPCODE_URL, list_url("00990001"), main_url("20260318000101"),
                                   viewer_url(WEB_OVERVIEW), viewer_url(WEB_PRODUCTS)])
        self.assertFalse(any("document.xml" in u for u in pub))
        for url, kw in zip(client.calls, client.kwargs):
            if url.startswith("https://dart.fss.or.kr/"):
                self.assertEqual((kw["rate_key"], kw["deadline_s"]), ("dart-web", dart.WEB_DEADLINE_S))
                self.assertNotIn("crtfc_key", url)
            else:
                self.assertEqual(kw["rate_key"], "dart")
                self.assertIn("crtfc_key=" + TEST_KEY, url)
        st = self.states(dart.SOURCE_ID)
        self.assertEqual(st["KRX:999990"][:2], ("ok", 200))
        self.assertEqual(st["KRX:999990"][3], "web;overview;products;corrected_later")
        self.assertEqual(st["KRX:999980"][3], "web;fallback:chapter")
        self.assertEqual(st["KRX:999960"][0], "no_annual_filing")
        d = self.q("SELECT url, text_path, extractor, raw_sha256, raw_bytes, text_chars FROM documents "
                   "WHERE doc_id = 'dart_business_report:00990001:20260318000101:business'")[0]
        self.assertEqual(d[0], main_url("20260318000101"))
        self.assertEqual(d[2], "dart-web-v1")
        html = fx("dart_web_section_overview_20260310009990.html") + fx("dart_web_section_products_20260310009990.html")
        self.assertEqual((d[3], d[4]), (hashlib.sha256(html).hexdigest(), len(html)))
        text = Path(d[1]).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("당사는 본사를 거점으로"))
        self.assertIn("이 있습니다.\n\n【주요 제품 및 서비스】\n가. 주요 제품 매출\n\n당사는 센서", text)
        self.assertEqual(d[5], len(text))
        desc = self.q("SELECT text, source_url FROM descriptions WHERE security_id = 'KRX:999990'")[0]
        self.assertTrue(desc[0].startswith("당사는 본사를 거점으로"))
        self.assertNotIn("주요 제품 매출", desc[0])                                     # overview only
        self.assertEqual(desc[1], main_url("20260318000101"))
        chap = Path(self.q("SELECT text_path FROM documents WHERE security_id = 'KRX:999980'")[0][0])
        self.assertTrue(chap.read_text(encoding="utf-8").startswith("당사는 가상의 항체-약물 접합체(ADC)"))
        self.assertIn("ADC-1 | 임상 1상", chap.read_text(encoding="utf-8"))
        self.assertEqual(self.last_run()[:2], ("sync-dart", "ok"))
        self.assert_key_nowhere()

    def test_products_capped(self):
        self.add_standard()
        pages = web_pages()
        pages[viewer_url(WEB_PRODUCTS)] = ("<P>2. 주요 제품 및 서비스</P>" + "<P>" + "가" * 9000 + "</P>").encode()
        self.run_sync(FakeClient(pages), codes="999990")
        st = self.states(dart.SOURCE_ID)["KRX:999990"]
        self.assertTrue(st[3].startswith("web;overview;products;products_capped"))
        text = Path(self.q("SELECT text_path FROM documents WHERE security_id = 'KRX:999990'")[0][0]).read_text(
            encoding="utf-8")
        self.assertEqual(text.split("【주요 제품 및 서비스】\n", 1)[1], "가" * dart.PRODUCTS_MAX_CHARS)

    def test_missing_products_and_failed_products(self):
        self.add_standard()
        for item, note in ((500, "web;overview;products_http_500"),
                           (RequestTimeout("u", 60.0, 60.1, 0, 200), "web;overview;products_deadline"),
                           (ConnectionResetError("reset"), "web;overview;products_network:ConnectionResetError")):
            with self.subTest(note=note):
                pages = web_pages()
                pages[viewer_url(WEB_PRODUCTS)] = item
                summary, _ = self.run_sync(FakeClient(pages), codes="999990", refresh=True)
                st = self.states(dart.SOURCE_ID)["KRX:999990"]
                self.assertEqual((st[0], st[3]), ("ok", note + ";corrected_later"))
                text = Path(self.q("SELECT text_path FROM documents WHERE security_id = 'KRX:999990'")[0][0]) \
                    .read_text(encoding="utf-8")
                self.assertNotIn("【주요 제품 및 서비스】", text)
        pages = web_pages()
        page = fx("dart_web_main_20260310009990.html").decode("utf-8").replace(
            "2. 주요 제품 및 서비스", "2. 원재료 현황")
        pages[main_url("20260318000101")] = page.encode("utf-8")
        client = FakeClient(pages)
        self.run_sync(client, codes="999990", refresh=True)
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][3], "web;overview;no_products;corrected_later")
        self.assertNotIn(viewer_url(WEB_PRODUCTS), client.public_calls())

    def test_failed_products_are_redone_next_run_without_refresh(self):
        self.add_standard()
        for item in (503, RequestTimeout("u", 60.0, 60.1, 0, 200), ConnectionResetError("reset"),
                     "<html><body>잠시 후 다시 시도해 주십시오.</body></html>".encode("utf-8")):
            with self.subTest(item=repr(item)[:40]):
                pages = web_pages()
                pages[viewer_url(WEB_PRODUCTS)] = item
                self.run_sync(FakeClient(pages), codes="999990", refresh=True)
                row = self.q("SELECT extractor, extract_note FROM documents WHERE security_id = 'KRX:999990'")[0]
                self.assertEqual(row[0], "dart-web-v1/partial")
                self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][0], "ok")
                client = FakeClient(web_pages())                       # products back; no --refresh
                summary, _ = self.run_sync(client, codes="999990")
                self.assertEqual((summary["skipped_unchanged"], summary["ok_documents"]), (0, 1))
                self.assertIn(main_url("20260318000101"), client.public_calls())
                self.assertIn(viewer_url(WEB_PRODUCTS), client.public_calls())
                row = self.q("SELECT extractor, extract_note FROM documents WHERE security_id = 'KRX:999990'")[0]
                self.assertEqual(row, ("dart-web-v1", "web;overview;products;corrected_later"))
                summary, _ = self.run_sync(FakeClient(web_pages()), codes="999990")
                self.assertEqual(summary["skipped_unchanged"], 1)            # complete now: skipped

    def test_unverified_section_is_an_error_without_document(self):
        self.add_standard()
        pages = web_pages()
        pages[viewer_url(WEB_OVERVIEW)] = "<html><head><title>안내</title></head><body>잠시 후 다시 시도해 주십시오." \
                                          "</body></html>".encode("utf-8")
        client = FakeClient(pages)
        summary, _ = self.run_sync(client, codes="999990")
        st = self.states(dart.SOURCE_ID)["KRX:999990"]
        self.assertEqual(st[0], "error")
        self.assertRegex(st[3], r"^section_unverified;bytes:\d+;sha:[0-9a-f]{12};title:안내$")
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE security_id = 'KRX:999990'")[0][0], 0)
        self.assertNotIn(viewer_url(WEB_PRODUCTS), client.public_calls())
        self.assertEqual(summary["web_sections"], 0)
        client = FakeClient(web_pages())                                 # retried next run without --refresh
        summary, _ = self.run_sync(client, codes="999990")
        self.assertEqual((summary["ok_documents"], self.states(dart.SOURCE_ID)["KRX:999990"][0]), (1, "ok"))

    def test_consecutive_website_refusals_stop_with_cooldown(self):
        self.add_standard()
        redirect = main_url("20260320000202")
        pages = web_pages()
        pages[main_url("20260318000101")] = "<html><head><title>DART</title></head><body>x</body></html>".encode()
        pages[redirect] = lambda url: Response(url, 302, b"", {"Location": "https://dart.fss.or.kr/error.html"}, 0.0)
        with mock.patch.object(dart, "WEB_REFUSAL_STOP", 3):
            summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual(summary["status"], "ok")                       # 2 refusals: item errors only
        st = self.states(dart.SOURCE_ID)
        self.assertEqual(st["KRX:999980"][3], "main_http_302;location:https://dart.fss.or.kr/error.html")
        self.assertIsNone(guard.recent_block_marker(self.cfg, "sync-dart"))
        with mock.patch.object(dart, "WEB_REFUSAL_STOP", 2):
            summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual((summary["stopped_reason"], summary["status"], summary["blocked_at"]),
                         ("access_refused", "blocked", "00990002"))
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999980"][:2], ("blocked", None))
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999980"][3], "access_refused:web:main_http_302")
        self.assertIn(redirect, guard.recent_block_marker(self.cfg, "sync-dart")["note"])
        self.assertEqual(self.last_run()[1], "blocked")

    def test_consecutive_too_short_sections_stop_the_run(self):
        self.add_standard()
        pages = web_pages()
        pages[viewer_url(WEB_OVERVIEW)] = "<P class='section-2'>1. 사업의 개요</P><P>짧음</P>".encode("utf-8")
        pages[viewer_url(CHAPTER_RANGE)] = "<P class='section-1'>II. 사업의 내용</P><P>짧음</P>".encode("utf-8")
        with mock.patch.object(dart, "SHORT_SECTION_STOP", 2):
            summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("consecutive_errors", "stopped_errors"))
        st = self.states(dart.SOURCE_ID)
        self.assertEqual((st["KRX:999990"][0], st["KRX:999980"][0]), ("extract_failed", "extract_failed"))
        self.assertNotIn("KRX:999960", st)
        self.assertIsNone(guard.recent_block_marker(self.cfg, "sync-dart"))

    def test_web_provenance_records_website_rate_key(self):
        self.add_standard()
        self.run_sync(FakeClient(web_pages()))
        reqs = {k: json.loads(r) for k, r in self.q("SELECT kind, request_json FROM snapshots "
                                                     "WHERE source_id = 'dart_business_report'")}
        self.assertEqual((reqs["filing_batch"]["rate_key"], reqs["filing_batch"]["auth"]), ("dart-web", "none"))
        self.assertIn("viewer.do", reqs["filing_batch"]["fetched_via"])
        self.assertEqual((reqs["aux_batch"]["rate_key"], reqs["aux_batch"]["auth"]),
                         ("dart", "query:<opendart-api-key>"))

    def test_web_lock_taken_before_opendart_lock(self):
        import contextlib
        order: list[str] = []

        def lock(cfg, budget, reentrant=False):
            order.append(budget)
            return contextlib.nullcontext()
        with mock.patch.object(dart.guard, "budget_lock", side_effect=lock):
            self.run_sync(FakeClient(web_pages()))
        self.assertEqual(order, ["dart-web", "dart"])

    def test_no_toc_and_http_errors_are_item_errors(self):
        self.add_standard()
        pages = web_pages()
        pages[main_url("20260318000101")] = "<html><body>시스템 점검 중입니다.</body></html>".encode("utf-8")
        pages[main_url("20260320000202")] = 404
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual(summary["status"], "ok")
        st = self.states(dart.SOURCE_ID)
        self.assertEqual(st["KRX:999990"][0], "error")
        self.assertRegex(st["KRX:999990"][3], r"^no_toc;bytes:\d+;sha:[0-9a-f]{12}$")
        self.assertEqual(summary["failures_by_note"].get("no_toc"), 1)
        self.assertEqual((st["KRX:999980"][0], st["KRX:999980"][3]), ("error", "main_http_404"))
        pages = web_pages()
        pages[viewer_url(WEB_OVERVIEW)] = 500
        self.run_sync(FakeClient(pages), codes="999990")
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][3], "section_http_500")

    def test_extract_failed_cases(self):
        self.add_standard()
        pages = web_pages()
        pages[viewer_url(WEB_OVERVIEW)] = "<P>1. 사업의 개요</P><P>짧음</P>".encode("utf-8")
        client = FakeClient(pages)
        self.run_sync(client, codes="999990")
        st = self.states(dart.SOURCE_ID)["KRX:999990"]
        self.assertEqual((st[0], st[3]), ("extract_failed", "section_too_short:2;web;overview;corrected_later"))
        self.assertNotIn(viewer_url(WEB_PRODUCTS), client.public_calls())
        row = self.q("SELECT text_path, extractor FROM documents WHERE security_id = 'KRX:999990'")[0]
        self.assertEqual(row, (None, "dart-web-v1"))
        pages[main_url("20260318000101")] = toc_page(
            js_node("node1", {"text": "I. 회사의 개요", **rng(3, 100)})).encode("utf-8")
        self.run_sync(FakeClient(pages), codes="999990", refresh=True)
        st = self.states(dart.SOURCE_ID)["KRX:999990"]
        self.assertEqual((st[0], st[3]), ("extract_failed", "business_section_not_found;web;corrected_later"))

    def test_blocked_on_main_page_stops_the_run(self):
        self.add_standard()
        pages = web_pages()
        pages[main_url("20260318000101")] = Blocked(main_url("20260318000101"), 403, "http_403")
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"], summary["blocked_at"]),
                         ("blocked", "blocked", "00990001"))
        self.assertEqual(client.public_calls(), [dart.CORPCODE_URL, list_url("00990001"), main_url("20260318000101")])
        hit = guard.recent_block_marker(self.cfg, "sync-dart")
        self.assertIn(main_url("20260318000101"), hit["note"])
        self.assertIn("403", hit["note"])
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][:2], ("blocked", 403))
        self.assertEqual(self.last_run()[1], "blocked")
        self.assert_key_nowhere()

    def test_blocked_on_section_stops_the_run(self):
        self.add_standard()
        pages = web_pages()
        pages[viewer_url(WEB_PRODUCTS)] = Blocked(viewer_url(WEB_PRODUCTS), 429, "http_429")
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["stopped_reason"], "blocked")
        self.assertEqual(client.public_calls()[-1], viewer_url(WEB_PRODUCTS))
        self.assertNotIn(list_url("00990002"), client.public_calls())
        self.assertIn(viewer_url(WEB_PRODUCTS), guard.recent_block_marker(self.cfg, "sync-dart")["note"])
        self.assertEqual(self.states(dart.SOURCE_ID)["KRX:999990"][0], "blocked")

    def test_extractor_bump_reextracts_zip_rows(self):
        self.add_standard()
        pages = standard_pages() | web_pages()
        summary, _ = self.run_sync(FakeClient(pages), mode="zip")
        self.assertEqual(summary["ok_documents"], 2)
        self.assertEqual({r[0] for r in self.q("SELECT extractor FROM documents")}, {"dart-v1/xml"})
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)                                   # web: earlier zip rows re-extracted
        self.assertEqual((summary["skipped_unchanged"], summary["ok_documents"]), (0, 2))
        self.assertIn(main_url("20260318000101"), client.public_calls())
        self.assertEqual({r[0] for r in self.q("SELECT extractor FROM documents")}, {"dart-web-v1"})
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)                                   # now unchanged
        self.assertEqual((summary["skipped_unchanged"], summary["requests"]), (2, 3))
        self.assertFalse(any(u.startswith("https://dart.fss.or.kr/") for u in client.calls))

    def test_web_pacing(self):
        self.add_standard()
        with mock.patch.object(dart, "WEB_MIN_INTERVAL_S", 1.0), mock.patch.object(dart.time, "sleep") as sl:
            self.run_sync(FakeClient(web_pages()))
        waits = [c.args[0] for c in sl.call_args_list if 0.5 < c.args[0] <= 1.0]
        self.assertEqual(len(waits), 4)                     # 5 website requests -> 4 waits (fake client: instant)

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            dart.sync(self.cfg, FakeClient({}), mode="pdf")


class TestCorpCodeReuse(DartSyncBase):
    mode = "web"

    def put(self, days_ago: int, data: bytes, tag: str = "0000000000000000") -> Path:
        day = dart._utc_today() - dt.timedelta(days=days_ago)
        p = self.cfg.raw_dir / dart.SOURCE_ID / day.isoformat() / f"dart_corpcode-{tag}.zip"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_find_fresh_stale_corrupt(self):
        self.assertIsNone(dart.find_cached_corpcode(self.cfg))
        stale = self.put(8, fx("dart_CORPCODE.zip"), "aaaaaaaaaaaaaaaa")
        self.assertIsNone(dart.find_cached_corpcode(self.cfg))
        fresh = self.put(7, fx("dart_CORPCODE.zip"), "bbbbbbbbbbbbbbbb")
        path, data, rows = dart.find_cached_corpcode(self.cfg)
        self.assertEqual((path, len(rows)), (fresh, 7))
        self.put(1, fx("dart_CORPCODE.zip")[:200], "cccccccccccccccc")          # truncated zip: skipped
        self.put(0, b"not a zip", "dddddddddddddddd")
        self.put(0, zip_of([("readme.txt", b"x")]), "eeeeeeeeeeeeeeee")
        self.assertEqual(dart.find_cached_corpcode(self.cfg)[0], fresh)
        newest = self.put(0, fx("dart_CORPCODE.zip"), "ffffffffffffffff")
        self.assertEqual(dart.find_cached_corpcode(self.cfg)[0], newest)
        other = self.cfg.raw_dir / dart.SOURCE_ID / "latest" / "dart_corpcode-1111111111111111.zip"
        other.parent.mkdir(parents=True)
        other.write_bytes(fx("dart_CORPCODE.zip"))                              # not a day folder: ignored
        self.assertEqual(dart.find_cached_corpcode(self.cfg)[0], newest)
        self.assertTrue(stale.exists())

    def test_sync_reuses_fresh_copy(self):
        self.add_standard()
        path = self.put(2, fx("dart_CORPCODE.zip"))
        client = FakeClient(web_pages())
        summary, _ = self.run_sync(client)
        self.assertNotIn(dart.CORPCODE_URL, client.public_calls())
        self.assertEqual((summary["corpcode_reused"], summary["corp_codes"], summary["ok_documents"]), (1, 7, 2))
        self.assertEqual(summary["requests"], 3 + 3 + 2)
        snap = self.q("SELECT raw_path, raw_sha256, note, rows FROM snapshots WHERE kind = 'dart_corpcode'")[0]
        self.assertEqual(snap, (str(path), hashlib.sha256(fx("dart_CORPCODE.zip")).hexdigest(),
                                f"reused_raw:{path}", 7))

    def test_suspect_corpcode_refused_and_nothing_deleted(self):
        self.add_standard()
        self.run_sync(FakeClient(web_pages()), reuse_corpcode=False)
        ids = self.q("SELECT security_id, id_value FROM identifiers WHERE id_type = 'dart_corp_code' ORDER BY 1")
        self.assertEqual(len(ids), 3)
        snaps = self.q("SELECT count(*) FROM snapshots WHERE kind = 'dart_corpcode'")[0][0]
        with mock.patch.object(dart, "CORPCODE_MIN_ROWS", 8):
            self.assertIsNone(dart.find_cached_corpcode(self.cfg))      # the saved 7-row copy is suspect too
            client = FakeClient(web_pages())
            summary, _ = self.run_sync(client)
        self.assertEqual((summary["status"], summary["stopped_reason"]), ("error", "corpcode_error"))
        self.assertEqual(summary["corpcode_problem"], "corpcode_suspect:rows=7;stock_codes=6")
        self.assertEqual(client.public_calls(), [dart.CORPCODE_URL])
        self.assertEqual(self.q("SELECT security_id, id_value FROM identifiers WHERE id_type = 'dart_corp_code' "
                                "ORDER BY 1"), ids)
        self.assertEqual(self.q("SELECT count(*) FROM snapshots WHERE kind = 'dart_corpcode'")[0][0], snaps)
        self.assertIn("corpcode_suspect", self.last_run()[3])
        with mock.patch.object(dart, "CORPCODE_MIN_STOCK_CODES", 7):
            self.assertIsNone(dart.find_cached_corpcode(self.cfg))
        self.assertEqual(dart.CORPCODE_MIN_ROWS, 5)                       # patched in setUp; real defaults:
        with mock.patch.object(dart, "CORPCODE_MIN_ROWS", 50_000), \
                mock.patch.object(dart, "CORPCODE_MIN_STOCK_CODES", 2_000):
            self.assertIsNotNone(dart.corpcode_problem([{"corp_code": "1", "stock_code": "005930"}] * 49_999))

    def test_sync_downloads_when_stale_corrupt_or_disabled(self):
        self.add_standard()
        self.put(9, fx("dart_CORPCODE.zip"))
        self.put(0, b"PK\x03\x04garbage", "9999999999999999")
        client = FakeClient(web_pages())
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["corpcode_reused"], 0)
        by_url = {client.public(u): kw for u, kw in zip(client.calls, client.kwargs)}
        self.assertEqual(by_url[dart.CORPCODE_URL]["deadline_s"], 900.0)
        note = self.q("SELECT note FROM snapshots WHERE kind = 'dart_corpcode'")[0][0]
        self.assertIsNone(note)
        client = FakeClient(web_pages())                          # the download was saved raw: reused next time
        self.assertEqual(self.run_sync(client)[0]["corpcode_reused"], 1)
        client = FakeClient(web_pages())
        self.run_sync(client, reuse_corpcode=False)
        self.assertEqual(client.public_calls()[0], dart.CORPCODE_URL)


if __name__ == "__main__":
    unittest.main()


class TestLicenceDocs(unittest.TestCase):
    def test_web_mode_host_is_named_in_licence_docs(self):
        # the default web mode reads the section text from the DART website, not the OpenDART API
        from urllib.parse import urlparse
        from jevscreen import provenance
        host = urlparse(dart.WEB_SECTION_URL).hostname
        self.assertEqual(host, urlparse(dart.WEB_MAIN_URL).hostname)
        doc = (FIXTURES.parent.parent / "DATA_LICENSES.md").read_text(encoding="utf-8")
        row = next(r for r in doc.splitlines() if r.startswith("| `dart_business_report`"))
        named = re.compile(r"(?<![\w.-])" + re.escape(host))    # not the 'dart.fss.or.kr' inside opendart.fss.or.kr
        self.assertRegex(row, named)
        self.assertIn("https://opendart.fss.or.kr/intro/terms.do", row)
        src = provenance.SOURCES["dart_business_report"]
        self.assertRegex(src.name + " " + src.notes, named)
