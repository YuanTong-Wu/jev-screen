"""Tests for sources.edinet (and the run engine it shares with sources.dart).

No network: every client is a fake. Fixtures tests/fixtures/edinet_* are SYNTHETIC (fictional companies and codes
E999xx / 9999 / 285A), shaped after the EDINET API v2 specification; they are not captured live responses.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import signal
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any, Callable
from unittest import mock
from urllib.parse import quote

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import guard, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, RequestTimeout, Response  # noqa: E402
from jevscreen.sources import edinet  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_KEY = "edinetTESTkey+/=0123456789abcdef"     # fake; contains characters that URL-quoting changes
AS_OF = dt.date(2026, 9, 26)
_KEY_PARAM_RE = re.compile(r"[?&](?:Subscription-Key|crtfc_key)=[^&]*")


def fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


CODELIST = fx("edinet_Edinetcode.zip")
DOC1 = fx("edinet_S100T001_type5.zip")
DOC2 = fx("edinet_S100T002_type5.zip")


def list_url(day: str) -> str:
    return edinet.LIST_URL.format(date=day)


def doc_url(doc_id: str) -> str:
    return edinet.DOC_URL.format(doc_id=doc_id)


def empty_day(day: str) -> bytes:
    return json.dumps({"metadata": {"parameter": {"date": day, "type": "2"}, "resultset": {"count": 0},
                                    "status": "200", "message": "OK"}, "results": []}).encode()


def zip_of(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, b in members:
            zf.writestr(n, b)
    return buf.getvalue()


class FakeClient:
    """Stands in for http.Client. Pages are keyed by the KEY-FREE URL; values: bytes | int status | Exception |
    callable(full_url) -> Response. Unknown EDINET list URLs answer an empty day. Records the full URLs."""

    def __init__(self, pages: dict[str, Any] | None = None, default: Any = 404, on_request: Callable | None = None):
        self.pages, self.default, self.on_request = dict(pages or {}), default, on_request
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.min_interval_s, self.requests_made = 0.0, 0

    @staticmethod
    def public(url: str) -> str:
        return _KEY_PARAM_RE.sub("", url)

    def get(self, url: str, **kw) -> Response:
        self.calls.append(url)
        self.kwargs.append(kw)
        self.requests_made += 1
        if self.on_request:
            self.on_request(url)
        pub = self.public(url)
        item = self.pages.get(pub)
        if item is None:
            m = re.search(r"documents\.json\?date=(\d{4}-\d{2}-\d{2})", pub)
            item = empty_day(m.group(1)) if m else self.default
        if callable(item) and not isinstance(item, type):
            return item(url)
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        if isinstance(item, int):
            return Response(url, item, b"", {}, 0.0)
        return Response(url, 200, item, {}, 0.0)

    def public_calls(self) -> list[str]:
        return [self.public(u) for u in self.calls]


def standard_pages() -> dict[str, Any]:
    return {
        edinet.CODELIST_URL: CODELIST,
        list_url("2026-06-26"): fx("edinet_documents_2026-06-26.json"),
        list_url("2026-03-27"): fx("edinet_documents_2026-03-27.json"),
        list_url("2025-06-27"): fx("edinet_documents_2025-06-27.json"),
        doc_url("S100T001"): DOC1,
        doc_url("S100T002"): DOC2,
    }


# =========================================================================== pure functions


class TestCodeListAndMapping(unittest.TestCase):
    def test_parse_codelist_zip_cp932(self):
        rows = edinet.parse_codelist(CODELIST)
        self.assertEqual(len(rows), 7)
        first = rows[0]
        self.assertEqual((first["edinet_code"], first["sec_code"], first["listed"], first["name"]),
                         ("E99901", "99990", True, "テスト精機株式会社"))
        self.assertEqual(first["name_en"], "Test Seiki Co., Ltd.")
        fund = next(r for r in rows if r["edinet_code"] == "G99907")
        self.assertIsNone(fund["sec_code"])
        self.assertIs(next(r for r in rows if r["edinet_code"] == "E99905")["listed"], False)

    def test_parse_codelist_bare_csv_utf8_and_text(self):
        with zipfile.ZipFile(io.BytesIO(CODELIST)) as zf:
            text = zf.read("EdinetcodeDlInfo.csv").decode("cp932")
        self.assertEqual(len(edinet.parse_codelist(text.encode("utf-8"))), 7)
        self.assertEqual(len(edinet.parse_codelist(text)), 7)
        with self.assertRaises(ValueError):
            edinet.parse_codelist("a,b,c\n1,2,3\n")

    def test_sec_code_for_symbol(self):
        self.assertEqual(edinet.sec_code_for_symbol("7203"), "72030")
        self.assertEqual(edinet.sec_code_for_symbol("285a"), "285A0")
        self.assertEqual(edinet.sec_code_for_symbol("２８５Ａ"), "285A0")      # full-width input
        self.assertEqual(edinet.sec_code_for_symbol("25935"), "25935")
        self.assertIsNone(edinet.sec_code_for_symbol("TOYOTA"))

    def test_mapping(self):
        rows = edinet.parse_codelist(CODELIST)
        secs = [{"security_id": s} for s in ("TSE:9999", "TSE:285A", "TSE:9998", "TSE:9997", "NAG:9996",
                                             "SAPSE:1111", "KRX:999990", "TSE:9999")]
        rep: dict = {}
        got = edinet.map_securities_to_edinet(secs, rows, report=rep)
        self.assertEqual(got, [("TSE:9999", "E99901", "sec_code"), ("TSE:285A", "E99902", "sec_code"),
                               ("NAG:9996", "E99906", "sec_code")])
        self.assertEqual((rep["considered"], rep["non_jp"], rep["unmatched"]), (6, 1, 2))   # 9997 unlisted, 1111
        self.assertEqual(rep["ambiguous"], [{"security_id": "TSE:9998", "sec_code": "99980",
                                             "edinet_codes": ["E99903", "E99904"]}])


class TestListsAndSelection(unittest.TestCase):
    def test_parse_list_and_filter(self):
        rows = edinet.parse_documents_list(fx("edinet_documents_2026-06-26.json"))
        self.assertEqual(len(rows), 6)
        annual = [r["docID"] for r in rows if edinet.is_annual_report(r)]
        # quarterly (140), amendment (130), withdrawn 120 and a 120 on another ordinance/form are excluded
        self.assertEqual(annual, ["S100T001", "S100T0X9"])
        row = next(r for r in rows if r["docID"] == "S100T001")
        self.assertFalse(edinet.is_annual_report(dict(row, disclosureStatus="2")))     # 不開示

    def test_status_handling(self):
        with self.assertRaises(edinet.ApiStop) as cm:
            edinet.parse_documents_list(fx("edinet_status_401.json"))
        self.assertEqual((cm.exception.reason, cm.exception.status, cm.exception.cooldown), ("key_rejected", 401, False))
        with self.assertRaises(edinet.ApiStop) as cm:
            edinet.check_status({"metadata": {"status": "429", "message": "Too Many Requests"}})
        self.assertEqual((cm.exception.reason, cm.exception.cooldown), ("quota_exceeded", True))
        # 404 is no longer read as an empty day here: the caller decides by the retention window
        with self.assertRaises(edinet.EdinetStatusError) as cm404:
            edinet.parse_documents_list(fx("edinet_status_404.json"))
        self.assertEqual(cm404.exception.status, "404")
        self.assertFalse(edinet.outside_retention(AS_OF - dt.timedelta(days=400), AS_OF))
        self.assertTrue(edinet.outside_retention(AS_OF - dt.timedelta(days=3650), AS_OF))
        with self.assertRaises(edinet.EdinetStatusError) as cm2:
            edinet.parse_documents_list({"metadata": {"status": "500", "message": "Internal Server Error"}})
        self.assertEqual(cm2.exception.status, "500")
        edinet.check_status({"results": []})   # no status at all: OK

    def test_pick_latest(self):
        base = {"edinet_code": "E1", "csv_flag": "1"}
        got = edinet.pick_latest([dict(base, doc_id="A", submit_datetime="2025-06-27 15:00", period_end="2025-03-31"),
                                  dict(base, doc_id="B", submit_datetime="2026-06-26 15:00", period_end="2026-03-31"),
                                  dict(base, doc_id="C", submit_datetime="2026-06-26 09:00", period_end="2026-03-31")])
        self.assertEqual(got["E1"]["doc_id"], "B")


class TestExtraction(unittest.TestCase):
    def test_utf16_tsv_fixture(self):
        text, note = edinet.extract_business_section(DOC1)
        self.assertIn("精密測定機器及び産業用センサー", text)
        self.assertNotIn("1946年", text)                                  # 沿革 is another element
        self.assertIn("csv:jpcrp030000-asr-001_E99901", note)
        self.assertIn("enc:utf-16-le", note)

    def test_cp932_html_value_fixture(self):
        text, note = edinet.extract_business_section(DOC2)
        self.assertIn("enc:cp932", note)
        self.assertNotIn("<p>", text)
        self.assertIn("サンプルAI Desk", text)
        self.assertIn("\n\n", text)                                       # paragraphs kept

    def _one(self, csv_bytes: bytes, name: str = "XBRL_TO_CSV/jpcrp030000-asr-001_X.csv"):
        return edinet.extract_business_section(zip_of([(name, csv_bytes)]))

    def test_encodings_and_delimiters(self):
        body = "当社は架空の食品メーカーであり、冷凍食品及び調味料の製造販売を行っております。" * 3
        header = "要素ID\t項目名\tコンテキストID\t値\n"
        row = f'"{edinet.TEXT_BLOCK_ELEMENT}"\t"事業の内容"\t"FilingDateInstant"\t"{body}"\n'
        for enc in ("utf-16-le", "utf-16-be"):                           # BOM-less UTF-16
            text, note = self._one((header + row).encode(enc))
            self.assertEqual(text, body, enc)
            self.assertIn(f"enc:{enc}", note)
        text, note = self._one(b"\xef\xbb\xbf" + (header + row).encode("utf-8"))
        self.assertEqual(text, body)
        comma = f'要素ID,項目名,値\n"{edinet.TEXT_BLOCK_ELEMENT}","事業の内容","{body}"\n'
        self.assertEqual(self._one(comma.encode("utf-8"))[0], body)
        headerless = f'"{edinet.TEXT_BLOCK_ELEMENT}"\t"x"\t"{body}"\n'
        self.assertEqual(self._one(headerless.encode("cp932"))[0], body)

    def test_huge_text_block_and_failures(self):
        body = "事業内容の説明文です。" * 20_000                               # > csv's default field limit
        tsv = f'要素ID\t値\n"{edinet.TEXT_BLOCK_ELEMENT}"\t"{body}"\n'
        text, _ = self._one(b"\xff\xfe" + tsv.encode("utf-16-le"))
        self.assertEqual(len(text), len(body))
        self.assertEqual(self._one("要素ID\t値\n\"jpcrp_cor:Other\"\t\"x\"\n".encode())[1], "element_not_found")
        short = f'要素ID\t値\n"{edinet.TEXT_BLOCK_ELEMENT}"\t"－"\n'
        self.assertTrue(self._one(short.encode())[1].startswith("section_too_short:1"))
        self.assertEqual(edinet.extract_business_section(zip_of([("a.txt", b"x")]))[1], "zip_without_csv")
        self.assertTrue(edinet.extract_business_section(b"PK\x03\x04garbage")[1].startswith("bad_zip"))

    def test_fixture_notes_blocks(self):
        self.assertTrue(edinet.extract_business_section(DOC1)[1].endswith(";blocks:business"))

    BUSINESS = "当社グループは、架空の精密部品の製造販売を主な事業としております。" * 3
    POLICY = ("文中の将来に関する事項は、当連結会計年度末現在において当社グループが判断したものであります。\n"
              "(1) 経営方針\n当社グループは、顧客の課題解決を通じて持続的な成長を目指します。\n"
              "(2) 対処すべき課題\n半導体市場の需要変動に備え、生産体制の柔軟性を高めてまいります。")

    def _tsv(self, rows: list[tuple[str, str]]) -> str:
        return "要素ID\t項目名\tコンテキストID\t相対年度\t値\n" + "".join(
            f'"{el}"\t"x"\t"FilingDateInstant"\t"提出日時点"\t"{val}"\n' for el, val in rows)

    def test_policy_block_appended_utf16(self):
        tsv = self._tsv([(edinet.TEXT_BLOCK_ELEMENT, self.BUSINESS), ("jpcrp_cor:CompanyHistoryTextBlock", "1946年"),
                         (edinet.POLICY_BLOCK_ELEMENT, "【経営方針、経営環境及び対処すべき課題等】\n" + self.POLICY)])
        for data, enc in ((b"\xff\xfe" + tsv.encode("utf-16-le"), "utf-16-le"),
                          (tsv.encode("utf-16-be"), "utf-16-be"), (tsv.encode("cp932"), "cp932")):
            with self.subTest(enc=enc):
                text, note = self._one(data)
                self.assertEqual(text, self.BUSINESS + "\n\n" + edinet.POLICY_HEADING + "\n" + self.POLICY)
                self.assertEqual(text.count("経営方針、経営環境及び対処すべき課題等"), 1)   # own heading not doubled
                self.assertNotIn("1946年", text)
                self.assertIn(f"enc:{enc}", note)
                self.assertTrue(note.endswith(";blocks:business,policy"), note)
                self.assertNotIn("policy_capped", note)
        # the short description still comes from 事業の内容
        self.assertTrue(edinet.short_description_cjk(text).startswith("当社グループは、架空の精密部品"))

    def test_policy_block_html_and_cap(self):
        long_policy = "<p>" + "当社グループは、持続的な成長のため、研究開発投資を継続してまいります。" * 400 + "</p>"
        tsv = self._tsv([(edinet.POLICY_BLOCK_ELEMENT, long_policy), (edinet.TEXT_BLOCK_ELEMENT, self.BUSINESS)])
        text, note = self._one(b"\xff\xfe" + tsv.encode("utf-16-le"))
        head, _, policy = text.partition("\n\n" + edinet.POLICY_HEADING + "\n")
        self.assertEqual(head, self.BUSINESS)
        self.assertNotIn("<p>", policy)
        self.assertLessEqual(len(policy), edinet.POLICY_MAX_CHARS)
        self.assertGreater(len(policy), edinet.POLICY_MAX_CHARS * 0.8)
        self.assertTrue(policy.endswith("。"))
        self.assertIn(";policy_capped", note)
        self.assertTrue(note.endswith(";blocks:business,policy"))

    def test_policy_block_missing_empty_or_in_other_csv(self):
        text, note = self._one(self._tsv([(edinet.TEXT_BLOCK_ELEMENT, self.BUSINESS)]).encode("utf-8"))
        self.assertEqual((text, note.endswith(";blocks:business")), (self.BUSINESS, True))
        text, note = self._one(self._tsv([(edinet.TEXT_BLOCK_ELEMENT, self.BUSINESS),
                                          (edinet.POLICY_BLOCK_ELEMENT, "－")]).encode("utf-8"))
        self.assertEqual((text, note.endswith(";blocks:business")), (self.BUSINESS, True))   # '－' only
        text, note = self._one(self._tsv([(edinet.TEXT_BLOCK_ELEMENT, self.BUSINESS),
                                          (edinet.POLICY_BLOCK_ELEMENT, "【経営方針、経営環境及び対処すべき課題等】")])
                               .encode("utf-8"))
        self.assertEqual((text, note.endswith(";blocks:business")), (self.BUSINESS, True))   # heading only
        z = zip_of([("XBRL_TO_CSV/jpcrp030000-asr-001_X.csv",
                     b"\xff\xfe" + self._tsv([(edinet.TEXT_BLOCK_ELEMENT, self.BUSINESS)]).encode("utf-16-le")),
                    ("XBRL_TO_CSV/jpcrp030000-asr-002_X.csv",
                     b"\xff\xfe" + self._tsv([(edinet.POLICY_BLOCK_ELEMENT, self.POLICY)]).encode("utf-16-le"))])
        text, note = edinet.extract_business_section(z)
        self.assertTrue(text.endswith(self.POLICY))
        self.assertIn(";policy_csv:jpcrp030000-asr-002_X.csv", note)
        # policy alone (no 事業の内容) is not a business section
        self.assertEqual(self._one(self._tsv([(edinet.POLICY_BLOCK_ELEMENT, self.POLICY)]).encode())[1],
                         "element_not_found")

    SHORT_BUSINESS = "当社グループは、架空の精密部品の製造販売を主な事業としております。" * 5   # ~170 chars
    LONG_POLICY = "(1) 中期経営計画\n" + "当社グループは、中期経営計画に基づき海外展開と研究開発投資を加速してまいります。\n\n" * 20

    def test_short_business_with_long_policy_description_from_business_only(self):
        tsv = self._tsv([(edinet.TEXT_BLOCK_ELEMENT, self.SHORT_BUSINESS),
                         (edinet.POLICY_BLOCK_ELEMENT, self.LONG_POLICY)])
        z = zip_of([("XBRL_TO_CSV/jpcrp030000-asr-001_X.csv", b"\xff\xfe" + tsv.encode("utf-16-le"))])
        business, extra, note = edinet.extract_business_blocks(z)
        self.assertEqual(business, self.SHORT_BUSINESS)
        self.assertTrue(extra.startswith("\n\n" + edinet.POLICY_HEADING + "\n"))
        self.assertTrue(note.endswith(";blocks:business,policy"))
        self.assertEqual(edinet.extract_business_section(z), (business + extra, note))
        desc = edinet.short_description_cjk(business)
        self.assertNotIn("中期経営計画", desc)
        self.assertLessEqual(len(desc), len(self.SHORT_BUSINESS))

    def test_too_short_business_fails_even_with_policy(self):
        tiny = "当社は部品を製造しております。"                                  # < MIN_SECTION_CHARS
        tsv = self._tsv([(edinet.TEXT_BLOCK_ELEMENT, tiny), (edinet.POLICY_BLOCK_ELEMENT, self.LONG_POLICY)])
        text, note = self._one(tsv.encode("utf-8"))
        self.assertIsNone(text)
        self.assertTrue(note.startswith(f"section_too_short:{len(tiny)};"), note)
        self.assertTrue(note.endswith(";blocks:business,policy"), note)

    def test_short_description_cjk(self):
        text, _ = edinet.extract_business_section(DOC1)
        desc = edinet.short_description_cjk(text)
        self.assertTrue(desc.startswith("当社グループは、当社、子会社12社"))
        self.assertNotIn("【事業の内容】", desc)                         # heading
        self.assertNotIn("（注）", desc)                                 # note
        self.assertNotIn("[事業系統図]", desc)                           # caption
        self.assertNotIn("次のとおりであります", desc)                    # lead-in to a chart
        long = "当社は架空の事業を営んでおります。" * 100
        cut = edinet.short_description_cjk(long, max_chars=200)
        self.assertLessEqual(len(cut), 200)
        self.assertTrue(cut.endswith("。"))
        self.assertEqual(edinet.short_description_cjk(""), "")

    def test_decode_bytes(self):
        self.assertEqual(edinet.decode_bytes("日本語テキストです".encode("cp932"), prefer=("utf-8", "cp932"))[1],
                         "cp932")
        self.assertEqual(edinet.decode_bytes('<?xml version="1.0" encoding="euc-kr"?><a>한국어</a>'.encode("cp949"))[1],
                         "cp949")


class TestRedactionHelpers(unittest.TestCase):
    def test_key_redactor_and_with_key(self):
        redact = edinet.key_redactor(TEST_KEY, "<k>")
        url = edinet.with_key(list_url("2026-06-26"), edinet.KEY_PARAM, TEST_KEY)
        self.assertNotIn(TEST_KEY, url)                                  # quoted in the URL ...
        self.assertNotIn(TEST_KEY, redact(url))
        self.assertNotIn("edinetTESTkey", redact(url))                   # ... and redacted in that form too
        self.assertEqual(redact(url), list_url("2026-06-26") + "&Subscription-Key=<k>")
        self.assertEqual(redact(f"x {TEST_KEY} y".encode()), b"x <k> y")
        self.assertEqual(redact(json.dumps({"k": TEST_KEY})), json.dumps({"k": "<k>"}))

    def test_sigterm_as_interrupt(self):
        before = signal.getsignal(signal.SIGTERM)
        if before not in (signal.SIG_DFL, None):
            self.skipTest("SIGTERM already handled by the test runner")
        with self.assertRaises(KeyboardInterrupt):
            with edinet.sigterm_as_interrupt():
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(200):                     # the handler runs between bytecodes of the main thread
                    time.sleep(0.01)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


# =========================================================================== sync


class SyncBase(unittest.TestCase):
    key_env = "JEVSCREEN_EDINET_API_KEY"
    key = TEST_KEY

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), min_interval_s=1.0)
        self.env = mock.patch.dict(os.environ, {self.key_env: self.key})
        self.env.start()
        with store.session(self.cfg) as con:
            self.snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="test", request=None,
                                              raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
                                              duration_s=None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def add(self, security_id: str, cap: float | None, isin: str | None = None) -> None:
        exchange, symbol = security_id.split(":", 1)
        row = dict(security_id=security_id, exchange=exchange, symbol=symbol, name=symbol, isin=isin, country=None,
                   tv_type="stock", tv_subtype="common", is_primary=True, price_currency=None,
                   fundamental_currency=None, sector=None, industry=None,
                   company_key=store.company_key(isin, security_id), first_seen_snapshot=self.snap,
                   last_seen_snapshot=self.snap, last_seen_at=store.now_utc(), active=True)
        with store.session(self.cfg) as con:
            store.upsert_many(con, "securities", list(row), [list(row.values())])
            store.upsert_many(con, "market_daily", ["security_id", "as_of", "market_cap_usd", "snapshot_id"],
                              [[security_id, dt.date(2026, 9, 26), cap, self.snap]])

    def q(self, sql: str, params=()) -> list[tuple]:
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            return con.execute(sql, list(params)).fetchall()

    def states(self, source_id: str) -> dict[str, tuple]:
        return {r[0]: r[1:] for r in self.q(
            "SELECT security_id, status, http_status, attempts, note FROM crawl_state WHERE source_id = ?",
            [source_id])}

    def last_run(self) -> tuple:
        return self.q("SELECT command, status, requests, note, finished_at FROM runs "
                      "ORDER BY started_at DESC LIMIT 1")[0]

    def assert_key_nowhere(self) -> None:
        """The key (any form) is in no DB value and no file under home."""
        forms = {self.key, quote(self.key, safe="")}
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            tables = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main' "
                "AND table_type='BASE TABLE'").fetchall()]
            for t in tables:
                dump = json.dumps(con.execute(f"SELECT * FROM {t}").fetchall(), default=str, ensure_ascii=False)
                for f in forms:
                    self.assertNotIn(f, dump, f"key found in table {t}")
        for p in Path(self.cfg.home).rglob("*"):
            if p.is_file() and p.suffix != ".duckdb" and not p.name.endswith(".wal"):
                data = p.read_bytes()
                for f in forms:
                    self.assertNotIn(f.encode(), data, f"key found in {p}")


class EdinetSyncBase(SyncBase):
    def add_standard(self) -> None:
        self.add("TSE:9999", 5e9, "JP3999999991")
        self.add("TSE:285A", 3e8, "JP3999999992")
        self.add("TSE:9998", 2e8, "JP3999999993")        # ambiguous code: not queued
        self.add("NAG:9996", 1e8, "JP3999999996")        # only a withdrawn / other-form 120
        self.add("KRX:999990", 9e9, "KR7999999990")      # not a JP venue

    def run_sync(self, client, **kw) -> tuple[dict, str]:
        kw.setdefault("as_of", AS_OF)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            summary = edinet.sync(self.cfg, client, **kw)
        return summary, err.getvalue()


class TestCodelistReuse(EdinetSyncBase):
    """reuse_codelist_hours (daily pack job): a code list another command fetched shortly before is not
    downloaded again."""

    def seed_codelist(self, hours_ago: float) -> str:
        path, digest = store.save_raw(self.cfg, edinet.SOURCE_ID, "edinet_codelist", CODELIST, suffix="zip")
        with store.session(self.cfg) as con:
            return store.record_snapshot(con, source_id=edinet.SOURCE_ID, kind="edinet_codelist", request=None,
                                         raw_path=path, raw_sha256=digest, raw_bytes=len(CODELIST), rows=7,
                                         duration_s=None,
                                         fetched_at=store.now_utc() - dt.timedelta(hours=hours_ago))

    def test_recent_codelist_is_reused(self):
        self.add_standard()
        snap = self.seed_codelist(hours_ago=1)
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client, reuse_codelist_hours=6)
        self.assertNotIn(edinet.CODELIST_URL, client.public_calls())
        self.assertTrue(summary["codelist_reused"])
        self.assertEqual(summary["codelist_rows"], 7)
        self.assertEqual(self.q("SELECT id_value, snapshot_id FROM identifiers WHERE security_id = 'TSE:9999'"),
                         [("E99901", snap)])
        self.assertEqual(summary["ok_documents"], 2)

    def test_old_codelist_refresh_or_default_fetches(self):
        self.add_standard()
        self.seed_codelist(hours_ago=7)
        client = FakeClient(standard_pages())
        self.run_sync(client, reuse_codelist_hours=6)
        self.assertEqual(client.public_calls().count(edinet.CODELIST_URL), 1)
        self.seed_codelist(hours_ago=0)
        for kw in ({}, {"reuse_codelist_hours": 6, "refresh": True}):
            client = FakeClient(standard_pages())
            self.run_sync(client, **kw)
            self.assertEqual(client.public_calls().count(edinet.CODELIST_URL), 1, kw)

    def test_env_sets_the_default(self):
        self.add_standard()
        self.seed_codelist(hours_ago=1)
        client = FakeClient(standard_pages())
        with mock.patch.dict(os.environ, {"JEVSCREEN_EDINET_REUSE_CODELIST_HOURS": "6"}):
            self.run_sync(client)
        self.assertNotIn(edinet.CODELIST_URL, client.public_calls())

    def test_tampered_cached_codelist_is_fetched_again(self):
        self.add_standard()
        self.seed_codelist(hours_ago=1)
        path = self.q("SELECT raw_path FROM snapshots WHERE kind = 'edinet_codelist'")[0][0]
        Path(path).write_bytes(b"not the file we recorded")
        client = FakeClient(standard_pages())
        self.run_sync(client, reuse_codelist_hours=6)
        self.assertEqual(client.public_calls().count(edinet.CODELIST_URL), 1)


class TestEdinetSync(EdinetSyncBase):
    def test_description_from_business_block_only(self):
        business = "当社グループは、架空の精密部品の製造販売を主な事業としております。" * 5
        policy = "(1) 中期経営計画\n" + "当社グループは、中期経営計画に基づき海外展開を加速してまいります。\n\n" * 20
        tsv = ("要素ID\t項目名\tコンテキストID\t相対年度\t値\n"
               f'"{edinet.TEXT_BLOCK_ELEMENT}"\t"x"\t"FilingDateInstant"\t"提出日時点"\t"{business}"\n'
               f'"{edinet.POLICY_BLOCK_ELEMENT}"\t"x"\t"FilingDateInstant"\t"提出日時点"\t"{policy}"\n')
        pages = standard_pages()
        pages[doc_url("S100T001")] = zip_of([("XBRL_TO_CSV/jpcrp030000-asr-001_X.csv",
                                              b"\xff\xfe" + tsv.encode("utf-16-le"))])
        self.add_standard()
        summary, _ = self.run_sync(FakeClient(pages), codes=["9999"])
        self.assertEqual(summary["ok_documents"], 1)
        path = self.q("SELECT text_path FROM documents WHERE security_id = 'TSE:9999'")[0][0]
        self.assertIn("中期経営計画", Path(path).read_text(encoding="utf-8"))      # the stored section keeps it
        desc = self.q("SELECT text FROM descriptions WHERE security_id = 'TSE:9999'")[0][0]
        self.assertTrue(desc.startswith("当社グループは、架空の精密部品"))
        self.assertNotIn("中期経営計画", desc)

    def test_missing_key_raises_before_any_request(self):
        os.environ.pop(self.key_env, None)
        client = FakeClient(standard_pages())
        with self.assertRaises(edinet.EdinetApiKeyMissing) as cm:
            edinet.sync(self.cfg, client)
        self.assertIsInstance(cm.exception, KeyError)
        self.assertIn("edinet_api_key", str(cm.exception))
        self.assertIn("JEVSCREEN_EDINET_API_KEY", str(cm.exception))
        self.assertEqual(client.calls, [])

    def test_key_file_is_used(self):
        os.environ.pop(self.key_env, None)
        (Path(self.cfg.home) / "edinet_api_key").write_text(self.key + "\n")
        self.add_standard()
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client, codes=["9999"])
        self.assertEqual(summary["status"], "ok")
        self.assertTrue(any(quote(self.key, safe="") in u for u in client.calls[1:]))

    def test_full_run(self):
        self.add_standard()
        client = FakeClient(standard_pages())
        summary, err = self.run_sync(client)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["securities_mapped"], 3)
        self.assertEqual(len(summary["ambiguous"]), 1)
        self.assertEqual(summary["companies_queued"], 3)
        self.assertEqual(summary["list_days_scanned"], edinet.DEFAULT_BACKFILL_DAYS)   # E99906 never found
        self.assertEqual(summary["list_days_fetched"], edinet.DEFAULT_BACKFILL_DAYS)
        self.assertEqual(summary["requests"], 1 + edinet.DEFAULT_BACKFILL_DAYS + 2)
        self.assertEqual(summary["ok_documents"], 2)
        # key sent on every API call, never on the key-free code list
        self.assertNotIn("Subscription-Key", client.calls[0])
        for u in client.calls[1:]:
            self.assertIn("Subscription-Key=" + quote(self.key, safe=""), u)
        self.assertTrue(all(kw.get("rate_key") == "edinet" for kw in client.kwargs))
        # newest day first; the document for S100T001 (not the 130 amendment) was fetched
        pub = client.public_calls()
        self.assertEqual(pub[1], list_url("2026-09-26"))
        self.assertIn(doc_url("S100T001"), pub)
        self.assertNotIn(doc_url("S100T0A1"), pub)
        self.assertNotIn(doc_url("S100S001"), pub)                       # older report of the same company
        st = self.states(edinet.SOURCE_ID)
        self.assertEqual(st["TSE:9999"][:2], ("ok", 200))
        self.assertEqual(st["TSE:285A"][0], "ok")
        self.assertEqual(st["NAG:9996"][0], "no_annual_filing")
        self.assertEqual(st["NAG:9996"][3], "no_yuho_in_last_400_days;other_annual:withdrawn,015/090000")
        self.assertNotIn("TSE:9998", st)
        ids = dict((r[0], r[1:]) for r in self.q("SELECT security_id, id_value, method FROM identifiers "
                                                  "WHERE id_type = 'edinet_code'"))
        self.assertEqual(ids, {"TSE:9999": ("E99901", "sec_code"), "TSE:285A": ("E99902", "sec_code"),
                               "NAG:9996": ("E99906", "sec_code")})
        docs = {r[0]: r[1:] for r in self.q(
            "SELECT doc_id, security_id, company_key, source_id, cik, form, section, accession, filing_date, "
            "report_date, url, raw_sha256, raw_bytes, text_path, text_chars, extractor, snapshot_id FROM documents")}
        d = docs["edinet_yuho:E99901:S100T001:business"]
        self.assertEqual(d[:10], ("TSE:9999", "isin:JP3999999991", "edinet_yuho", None, "有価証券報告書", "business",
                                  "S100T001", dt.date(2026, 6, 26), dt.date(2026, 3, 31), doc_url("S100T001")))
        self.assertEqual(d[11], len(DOC1))
        self.assertTrue(d[12].endswith("docs/edinet/E99901/S100T001-business.txt"))
        self.assertIn("精密測定機器", Path(d[12]).read_text(encoding="utf-8"))
        self.assertEqual(d[14], "edinet-v2/csv")
        self.assertTrue(d[15].startswith("edinet_yuho:filing_batch:"))
        self.assertIn("edinet_yuho:E99902:S100T002:business", docs)
        desc = self.q("SELECT security_id, source_id, lang, match_method, text, source_url FROM descriptions "
                      "WHERE source_id = 'edinet_yuho' ORDER BY security_id")
        self.assertEqual([r[0] for r in desc], ["TSE:285A", "TSE:9999"])
        self.assertEqual(desc[1][1:4], ("edinet_yuho", "ja", "edinet_code"))
        self.assertTrue(desc[1][4].startswith("当社グループは"))
        run = self.last_run()
        self.assertEqual(run[:3], ("sync-edinet", "ok", 1 + edinet.DEFAULT_BACKFILL_DAYS + 2))
        # final lists cached (all but the 2 newest days), with no key inside
        cached = sorted(p.name for p in (Path(self.cfg.home) / "raw" / "edinet" / "lists").glob("*.json"))
        self.assertEqual(len(cached), edinet.DEFAULT_BACKFILL_DAYS - edinet.FINAL_AFTER_DAYS)
        self.assertNotIn("2026-09-26.json", cached)
        self.assertNotIn("2026-09-25.json", cached)
        self.assertIn("2026-09-24.json", cached)
        self.assertIn("lists:", err)
        self.assert_key_nowhere()

    def test_incremental_run_uses_cache_and_skips_unchanged(self):
        self.add_standard()
        self.run_sync(FakeClient(standard_pages()))
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["list_days_fetched"], edinet.FINAL_AFTER_DAYS)   # only the non-final days
        self.assertEqual(summary["list_days_cached"], edinet.DEFAULT_BACKFILL_DAYS - edinet.FINAL_AFTER_DAYS)
        self.assertEqual(summary["skipped_unchanged"], 2)
        self.assertEqual(summary["requests"], 1 + edinet.FINAL_AFTER_DAYS)
        self.assertFalse(any("/documents/S100" in u for u in client.calls))
        # refresh re-downloads the documents (lists still from cache)
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client, refresh=True)
        self.assertEqual((summary["skipped_unchanged"], summary["ok_documents"]), (0, 2))
        self.assertEqual(self.q("SELECT attempts FROM crawl_state WHERE security_id = 'TSE:9999'")[0][0], 2)

    def test_new_filing_is_fetched(self):
        self.add_standard()
        self.run_sync(FakeClient(standard_pages()), codes=["9999"])
        pages = standard_pages()
        newer = json.loads(fx("edinet_documents_2026-06-26.json"))
        row = dict(newer["results"][1], docID="S100U001", submitDateTime="2026-09-26 10:00")
        pages[list_url("2026-09-26")] = json.dumps({"metadata": {"status": "200"}, "results": [row]}).encode()
        pages[doc_url("S100U001")] = DOC1
        client = FakeClient(pages)
        summary, _ = self.run_sync(client, codes=["9999"])
        self.assertEqual(summary["ok_documents"], 1)
        self.assertEqual(summary["list_days_scanned"], 1)                # found on the first (newest) day
        self.assertIn(doc_url("S100U001"), client.public_calls())
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE doc_id LIKE 'edinet_yuho:E99901:%'")[0][0], 2)

    def test_early_stop_and_filters(self):
        self.add_standard()
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client, codes=["TSE:9999", "285A"], limit=1)
        self.assertEqual(summary["companies_queued"], 1)
        self.assertTrue(summary["list_scan_stopped_early"])
        self.assertEqual(summary["list_days_scanned"], (AS_OF - dt.date(2026, 6, 26)).days + 1)
        self.assertEqual(summary["requests"], 1 + summary["list_days_scanned"] + 1)
        summary, _ = self.run_sync(FakeClient(standard_pages()), min_mcap_usd=1e9)
        self.assertEqual(summary["lines"], 1)

    def test_list_key_rejected_in_http_200_stops(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("2026-09-26")] = fx("edinet_status_401.json")
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("key_rejected", "error"))
        self.assertEqual(len(client.calls), 2)                           # code list + the first list day
        self.assertIn("invalid subscription key", summary["stop_message"])
        self.assertIsNone(guard.recent_block_marker(self.cfg, "sync-edinet"))
        self.assertEqual(self.last_run()[1], "error")
        self.assertEqual(self.states(edinet.SOURCE_ID), {})
        self.assert_key_nowhere()

    def test_document_key_rejected_stops(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("S100T001")] = fx("edinet_status_401.json")
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["stopped_reason"], "key_rejected")
        self.assertNotIn(doc_url("S100T002"), client.public_calls())

    def test_document_status_404_is_an_item_error(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("S100T001")] = fx("edinet_status_404.json")
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual(summary["status"], "ok")
        st = self.states(edinet.SOURCE_ID)
        self.assertEqual(st["TSE:9999"][0], "error")
        self.assertEqual(st["TSE:9999"][3], "document_status_404")
        self.assertEqual(st["TSE:285A"][0], "ok")

    def test_blocked_on_document_stops_and_marks_cooldown(self):
        self.add_standard()
        pages = standard_pages()

        def blocked(url: str):
            raise Blocked(url, 429, "http_429")          # the real client puts the full URL (with key) here
        pages[doc_url("S100T001")] = blocked
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("blocked", "blocked"))
        self.assertEqual(summary["blocked_at"], "E99901")
        self.assertNotIn(doc_url("S100T002"), client.public_calls())
        st = self.states(edinet.SOURCE_ID)
        self.assertEqual(st["TSE:9999"][:2], ("blocked", 429))
        self.assertNotIn("TSE:285A", st)
        hit = guard.recent_block_marker(self.cfg, "sync-edinet")
        self.assertIsNotNone(hit)
        self.assertIn(doc_url("S100T001"), hit["note"])
        self.assertEqual(self.last_run()[1], "blocked")
        self.assert_key_nowhere()

    def test_blocked_on_list_and_codelist(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("2026-09-25")] = lambda url: (_ for _ in ()).throw(Blocked(url, 403, "http_403"))
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual((summary["stopped_reason"], summary["blocked_at"]), ("blocked", "list:2026-09-25"))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-edinet"))
        self.assert_key_nowhere()
        client = FakeClient({edinet.CODELIST_URL: Blocked(edinet.CODELIST_URL, 403, "http_403")})
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], len(client.calls)), ("blocked", 1))

    def test_list_errors_stop_after_consecutive_failures(self):
        self.add_standard()
        client = FakeClient({edinet.CODELIST_URL: CODELIST}, default=500)
        client.pages.update({list_url((AS_OF - dt.timedelta(days=i)).isoformat()): 500 for i in range(10)})
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("list_errors", "stopped_errors"))
        self.assertEqual(summary["list_days_failed"], edinet.MAX_CONSECUTIVE_ERRORS)

    def _assert_recent_404_stops(self, answer) -> None:
        """A wrong endpoint / parameter (404 on recent days) must stop the run, not write every company as
        'no_annual_filing' with status ok."""
        self.add_standard()
        client = FakeClient({edinet.CODELIST_URL: CODELIST})
        client.pages.update({list_url((AS_OF - dt.timedelta(days=i)).isoformat()): answer for i in range(10)})
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("list_errors", "stopped_errors"))
        self.assertEqual(summary["list_days_failed"], edinet.MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(self.states(edinet.SOURCE_ID), {})

    def test_list_http_404_inside_retention_is_a_failure(self):
        self._assert_recent_404_stops(404)

    def test_list_metadata_404_inside_retention_is_a_failure(self):
        self._assert_recent_404_stops(fx("edinet_status_404.json"))

    def test_report_withdrawn_on_a_newer_day_is_not_picked(self):
        self.add_standard()
        pages = standard_pages()
        old = json.loads(fx("edinet_documents_2026-06-26.json"))
        t001 = next(r for r in old["results"] if r["docID"] == "S100T001")
        pages[list_url("2026-09-26")] = json.dumps(
            {"metadata": {"status": "200"}, "results": [dict(t001, withdrawalStatus="1")]}).encode()
        client = FakeClient(pages)
        summary, _ = self.run_sync(client, codes=["9999"])
        self.assertNotIn(doc_url("S100T001"), client.public_calls())
        self.assertNotEqual(self.states(edinet.SOURCE_ID)["TSE:9999"][0], "ok")

    def test_isolated_list_failure_is_noted(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("2026-09-20")] = b"not json"
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["list_days_failed"], 1)
        self.assertIn("list_days_failed:1", self.states(edinet.SOURCE_ID)["NAG:9996"][3])

    def test_keyboard_interrupt_flushes(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("S100T002")] = KeyboardInterrupt()
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("interrupted", "interrupted"))
        self.assertEqual(self.states(edinet.SOURCE_ID)["TSE:9999"][0], "ok")
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 1)
        self.assertEqual(self.last_run()[1], "interrupted")

    def test_extract_failed_and_no_csv(self):
        self.add_standard()
        pages = standard_pages()
        pages[doc_url("S100T001")] = zip_of([("XBRL_TO_CSV/jpcrp_x.csv", "要素ID\t値\n\"a\"\t\"b\"\n".encode())])
        day = json.loads(fx("edinet_documents_2026-03-27.json"))
        day["results"][0]["csvFlag"] = "0"
        pages[list_url("2026-03-27")] = json.dumps(day).encode()
        summary, _ = self.run_sync(FakeClient(pages))
        st = self.states(edinet.SOURCE_ID)
        self.assertEqual((st["TSE:9999"][0], st["TSE:9999"][3]), ("extract_failed", "element_not_found"))
        self.assertEqual(st["TSE:285A"][0], "no_csv")
        doc = self.q("SELECT text_path, extract_note, raw_bytes FROM documents")[0]
        self.assertEqual(doc[:2], (None, "element_not_found"))
        self.assertEqual(self.q("SELECT count(*) FROM descriptions")[0][0], 0)

    def test_network_error_and_consecutive_error_stop(self):
        for i in range(7):
            self.add(f"TSE:{9000 + i}", 1e9 - i)
        rows = [["E9%04d" % i, "内国法人・組合", "上場", "", "", "", f"架空{i}", "", "", "", "", f"{9000 + i}0", ""]
                for i in range(7)]
        csv_text = '"ＥＤＩＮＥＴコード","提出者種別","上場区分","a","b","c","提出者名","提出者名（英字）","d","e","f",' \
                   '"証券コード","提出者法人番号"\n' + "".join(",".join(f'"{c}"' for c in r) + "\n" for r in rows)
        results = [{"docID": f"S1{i:06d}", "edinetCode": "E9%04d" % i, "docTypeCode": "120", "ordinanceCode": "010",
                    "formCode": "030000", "withdrawalStatus": "0", "csvFlag": "1",
                    "submitDateTime": "2026-06-26 15:00", "periodEnd": "2026-03-31"} for i in range(7)]
        pages = {edinet.CODELIST_URL: zip_of([("EdinetcodeDlInfo.csv", csv_text.encode("cp932"))]),
                 list_url("2026-09-26"): json.dumps({"metadata": {"status": "200"}, "results": results}).encode()}
        pages.update({doc_url(f"S1{i:06d}"): OSError("connection reset") for i in range(7)})
        summary, _ = self.run_sync(FakeClient(pages), batch_size=2)
        self.assertEqual((summary["stopped_reason"], summary["status"]), ("consecutive_errors", "stopped_errors"))
        self.assertEqual(summary["companies_attempted"], edinet.MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(summary["failures_by_note"], {"network": 5})
        self.assertEqual(len(self.states(edinet.SOURCE_ID)), 5)

    def test_deadlines_passed_to_client(self):
        self.add_standard()
        client = FakeClient(standard_pages())
        self.run_sync(client)
        by_url = {client.public(u): kw for u, kw in zip(client.calls, client.kwargs)}
        self.assertEqual(by_url[edinet.CODELIST_URL]["deadline_s"], edinet.CODELIST_DEADLINE_S)
        self.assertEqual(by_url[list_url("2026-09-26")]["deadline_s"], edinet.LIST_DEADLINE_S)
        self.assertEqual(by_url[doc_url("S100T001")]["deadline_s"], edinet.DOC_DEADLINE_S)
        self.assertEqual((edinet.LIST_DEADLINE_S, edinet.DOC_DEADLINE_S), (60.0, 300.0))

    def test_document_deadline_is_an_item_error_and_run_goes_on(self):
        self.add_standard()
        pages = standard_pages()
        full = edinet.with_key(doc_url("S100T001"), edinet.KEY_PARAM, self.key)
        pages[doc_url("S100T001")] = RequestTimeout(full, 300.0, 300.2, 4096, 200)
        client = FakeClient(pages)
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["status"], "ok")
        st = self.states(edinet.SOURCE_ID)
        self.assertEqual(st["TSE:9999"][:2], ("error", 200))
        self.assertEqual(st["TSE:9999"][3], "deadline;300s;received:4096")
        self.assertEqual(st["TSE:285A"][0], "ok")                         # next company still processed
        self.assertEqual(summary["failures_by_note"], {"deadline": 1, "no_yuho_in_last_400_days": 1})
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE security_id = 'TSE:9999'")[0][0], 0)
        self.assert_key_nowhere()

    def test_list_deadline_is_a_per_day_failure(self):
        self.add_standard()
        pages = standard_pages()
        pages[list_url("2026-09-20")] = RequestTimeout("u", 60.0, 60.1, 0)
        summary, _ = self.run_sync(FakeClient(pages))
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["list_days_failed"], 1)
        self.assertEqual(self.states(edinet.SOURCE_ID)["TSE:9999"][0], "ok")

    def test_v1_rows_are_re_extracted(self):
        self.add_standard()
        self.run_sync(FakeClient(standard_pages()), codes=["9999"])
        with store.session(self.cfg) as con:
            con.execute("UPDATE documents SET extractor = 'edinet-v1/csv'")
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client, codes=["9999"])
        self.assertEqual((summary["skipped_unchanged"], summary["ok_documents"]), (0, 1))
        self.assertIn(doc_url("S100T001"), client.public_calls())         # the zip is downloaded again
        self.assertEqual(self.q("SELECT extractor FROM documents")[0][0], "edinet-v2/csv")
        client = FakeClient(standard_pages())
        summary, _ = self.run_sync(client, codes=["9999"])                # now unchanged
        self.assertEqual(summary["skipped_unchanged"], 1)
        self.assertNotIn(doc_url("S100T001"), client.public_calls())

    def test_stale_identifier_removed(self):
        self.add_standard()
        self.run_sync(FakeClient(standard_pages()), codes=["9999"])
        with zipfile.ZipFile(io.BytesIO(CODELIST)) as zf:
            text = zf.read("EdinetcodeDlInfo.csv").decode("cp932").replace('"99990"', '"99950"')
        pages = standard_pages()
        pages[edinet.CODELIST_URL] = zip_of([("EdinetcodeDlInfo.csv", text.encode("cp932"))])
        summary, _ = self.run_sync(FakeClient(pages), codes=["9999"])
        self.assertEqual(summary["identifiers_removed"], 1)
        self.assertEqual(self.q("SELECT count(*) FROM identifiers WHERE security_id = 'TSE:9999'")[0][0], 0)

    def test_client_min_interval_raised(self):
        self.add_standard()
        client = FakeClient(standard_pages())
        self.run_sync(client, codes=["9999"])
        self.assertEqual(client.min_interval_s, edinet.DEFAULT_MIN_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
