"""Tests for sources.cninfo. No network: every client is a fake (or a real http.Client with a fake opener).

Fixtures tests/fixtures/cninfo_* are SYNTHETIC: invented issuers, codes (009xxx, 309386, 609xxx), orgIds and
announcement ids; the PDFs are text-only PyMuPDF renders (embedded Droid Sans Fallback subset, Apache-2.0).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
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

from jevscreen import guard, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Client, RequestTimeout, Response  # noqa: E402
from jevscreen.sources import cninfo  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STOCK_LIST_RAW = (FIXTURES / "cninfo_stock_list_trimmed.json").read_bytes()
STOCK_LIST = json.loads(STOCK_LIST_RAW)
QUERY_RAW = (FIXTURES / "cninfo_query_309386.json").read_bytes()
QUERY = json.loads(QUERY_RAW)
SUMMARY_PDF = (FIXTURES / "cninfo_309386_2025_summary.pdf").read_bytes()
FULL_PDF = (FIXTURES / "cninfo_309386_2025_full_p1-40.pdf").read_bytes()
SUMMARY_URL = "http://static.cninfo.com.cn/finalpage/2026-04-15/9225102236.PDF"
FULL_URL = "http://static.cninfo.com.cn/finalpage/2026-04-15/9225102237.PDF"
ORG_309386, ORG_609519 = "9900093860", "gssh0609519"
_CACHE: dict = {}


def fixture_section(kind: str, backend: str = "auto") -> tuple[str | None, str, str]:
    key = (kind, backend)
    if key not in _CACHE:
        _CACHE[key] = cninfo.extract_from_pdf(SUMMARY_PDF if kind == "summary" else FULL_PDF, kind, backend=backend)
    return _CACHE[key]


def ann(title: str, adj: str, ms: int, aid: str | None = None, size: int = 100) -> dict:
    return {"announcementTitle": title, "adjunctUrl": adj, "adjunctSize": size, "announcementTime": ms,
            "adjunctType": "PDF", "announcementId": aid or Path(adj).stem}


T_2026_04_01 = 1774972800000   # 2026-04-01 00:00 +08:00


def query_609519(include_summary: bool = True) -> bytes:
    rows = [ann("壬示酒业2025年年度报告", "finalpage/2026-04-01/2000000002.PDF", T_2026_04_01, size=3000)]
    if include_summary:
        rows.append(ann("壬示酒业2025年年度报告摘要", "finalpage/2026-04-01/2000000001.PDF", T_2026_04_01))
    rows.append(ann("壬示酒业2024年年度报告摘要", "finalpage/2025-04-01/1000000001.PDF", T_2026_04_01 - 365 * 86400000))
    return json.dumps({"announcements": rows, "totalAnnouncement": len(rows)}, ensure_ascii=False).encode()


URL_609519_SUMMARY = "http://static.cninfo.com.cn/finalpage/2026-04-01/2000000001.PDF"
URL_609519_FULL = "http://static.cninfo.com.cn/finalpage/2026-04-01/2000000002.PDF"


class FakeCninfoClient:
    """Stands in for http.Client: GET url -> bytes | int | Exception; POST form keyed by the stock code."""

    def __init__(self, gets: dict | None = None, posts: dict | None = None, on_request=None):
        self.gets, self.posts, self.on_request = dict(gets or {}), dict(posts or {}), on_request
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.forms: list[dict] = []
        self.min_interval_s, self.requests_made = 0.0, 0

    def _answer(self, url: str, item) -> Response:
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        if isinstance(item, int):
            return Response(url, item, b"", {}, 0.0)
        return Response(url, 200, item, {}, 0.0)

    def get(self, url: str, **kw) -> Response:
        self.calls.append(url)
        self.kwargs.append(kw)
        self.requests_made += 1
        if self.on_request:
            self.on_request(url)
        return self._answer(url, self.gets.get(url, 404))

    def post_form(self, url: str, form: dict, headers=None, rate_key=None) -> Response:
        code = form["stock"].split(",")[0]
        self.calls.append(f"POST {code}")
        self.kwargs.append({"headers": headers, "rate_key": rate_key})
        self.forms.append(form)
        self.requests_made += 1
        if self.on_request:
            self.on_request(f"POST {code}")
        return self._answer(url, self.posts.get(code, 404))


def standard_client(**kw) -> FakeCninfoClient:
    gets = {cninfo.STOCK_LIST_URL: STOCK_LIST_RAW, SUMMARY_URL: SUMMARY_PDF, FULL_URL: FULL_PDF,
            URL_609519_SUMMARY: SUMMARY_PDF, URL_609519_FULL: FULL_PDF}
    posts = {"309386": QUERY_RAW, "609519": query_609519()}
    gets.update(kw.pop("gets", {}))
    posts.update(kw.pop("posts", {}))
    return FakeCninfoClient(gets, posts, **kw)


# =========================================================================== mapping


class TestMapping(unittest.TestCase):
    def test_parse_stock_list_fixture(self):
        rows = cninfo.parse_stock_list(STOCK_LIST_RAW)
        self.assertEqual(len(rows), len(STOCK_LIST["stockList"]))
        r = next(r for r in rows if r["code"] == "309386")
        self.assertEqual((r["org_id"], r["category"], r["name"]), (ORG_309386, "A股", "辛示科信"))

    def test_market_of_code(self):
        self.assertEqual([cninfo.market_of_code(c) for c in ("609519", "689825", "900929", "009001", "309386",
                                                             "200992", "439047", "920000", "12345")],
                         ["SSE", "SSE", "SSE", "SZSE", "SZSE", "SZSE", "BJ", "BJ", None])

    def test_real_fixture_mapping(self):
        secs = [{"security_id": "SZSE:309386"}, {"security_id": "SSE:609519"}, {"security_id": "SZSE:009001"},
                {"security_id": "SSE:689825"}, {"security_id": "BSE:500325", "isin": "INE002A01018"},
                {"security_id": "TSE:9901"}, {"security_id": "SZSE:009001"}]
        rep: dict = {}
        out = cninfo.map_securities_to_orgid(secs, STOCK_LIST, report=rep)
        self.assertEqual({sid: org for sid, org, _, _ in out},
                         {"SZSE:309386": ORG_309386, "SSE:609519": ORG_609519, "SZSE:009001": "gssz0009001",
                          "SSE:689825": "9920099008"})
        self.assertTrue(all(m == "code_exact" for *_, m in out))
        self.assertEqual((rep["considered"], rep["non_cn"], rep["ambiguous"]), (4, 2, []))  # dup code, same orgId

    def test_b_shares_and_beijing(self):
        rows = {"stockList": [
            {"code": "900929", "orgId": "gssh0900929", "category": "B股", "zwjc": "锦旅B股"},
            {"code": "600754", "orgId": "gssh0600754", "category": "A股", "zwjc": "锦江酒店"},
            {"code": "200992", "orgId": "gssz0200992", "category": "B股", "zwjc": "中鲁B"},
            {"code": "920000", "orgId": "gfbj0832000", "category": "A股", "zwjc": "安徽凤凰"}]}
        secs = [{"security_id": "SSE:900929"}, {"security_id": "SSE:600754"},
                {"security_id": "SZSE:200992"}, {"security_id": "BJSE:920000"},
                {"security_id": "BSE:920000", "isin": "CNE100005YP0"}]
        rep: dict = {}
        out = {sid: org for sid, org, _, _ in cninfo.map_securities_to_orgid(secs, rows, report=rep)}
        self.assertEqual(out, {"SSE:900929": "gssh0900929", "SSE:600754": "gssh0600754",
                               "SZSE:200992": "gssz0200992", "BJSE:920000": "gfbj0832000",
                               "BSE:920000": "gfbj0832000"})

    def test_a_line_never_maps_to_b_entry_and_venue_mismatch(self):
        rows = {"stockList": [{"code": "600001", "orgId": "x1", "category": "B股"},
                              {"code": "000002", "orgId": "x2", "category": "A股"}]}
        rep: dict = {}
        out = cninfo.map_securities_to_orgid([{"security_id": "SSE:600001"}, {"security_id": "SSE:000002"}],
                                             rows, report=rep)
        self.assertEqual(out, [])
        self.assertEqual((rep["unmatched"], rep["venue_mismatch"]), (1, 1))

    def test_ambiguous_code(self):
        rows = {"stockList": [{"code": "600001", "orgId": "a", "category": "A股"},
                              {"code": "600001", "orgId": "b", "category": "A股"}]}
        rep: dict = {}
        self.assertEqual(cninfo.map_securities_to_orgid([{"security_id": "SSE:600001"}], rows, report=rep), [])
        self.assertEqual(rep["ambiguous"], [{"security_id": "SSE:600001", "code": "600001", "org_ids": ["a", "b"]}])


# =========================================================================== announcement selection


class TestSelection(unittest.TestCase):
    AS_OF = dt.date(2026, 9, 26)

    def test_real_fixture_summary_and_full(self):
        docs = cninfo.select_annual_reports(QUERY["announcements"], "summary", as_of=self.AS_OF)
        self.assertEqual([(d["kind"], d["year"], d["url"]) for d in docs],
                         [("summary", 2025, SUMMARY_URL), ("full", 2025, FULL_URL)])
        self.assertEqual(docs[0]["filing_date"], dt.date(2026, 4, 15))      # announcementTime in China time
        self.assertEqual(docs[0]["announcement_id"], "9225102236")
        self.assertEqual(docs[0]["flags"], [])
        full = cninfo.select_annual_reports(QUERY["announcements"], "full", as_of=self.AS_OF)
        self.assertEqual([(d["kind"], d["url"]) for d in full], [("full", FULL_URL)])

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            cninfo.select_annual_reports([], "abstract")

    def test_english_and_h_share_versions_ignored(self):
        rows = [ann("2025年年度报告（英文版）", "a/1.PDF", 3), ann("2025年年度报告摘要(英文版)", "a/2.PDF", 3),
                ann("H股公告-2025年年度报告", "a/3.PDF", 3), ann("2025年年度报告", "a/4.PDF", 1),
                ann("2025 Annual Report", "a/5.PDF", 4)]
        docs = cninfo.select_annual_reports(rows, "summary", as_of=self.AS_OF)
        self.assertEqual([d["adjunct_url"] for d in docs], ["a/4.PDF"])      # no summary -> full only

    def test_latest_revision_wins_and_cancelled_skipped(self):
        rows = [a for a in QUERY["announcements"] if a["announcementTitle"].startswith("2016")]
        docs = cninfo.select_annual_reports(rows, "summary", as_of=self.AS_OF)
        self.assertEqual([d["title"] for d in docs], ["2016年年度报告摘要（更新后）", "2016年年度报告（更新后）"])
        self.assertTrue(all(d["revision"] for d in docs))
        self.assertEqual(docs[0]["flags"], ["stale:9"])
        rows = [ann("2025年年度报告摘要", "a/1.PDF", 100), ann("2025年年度报告摘要（修订版）", "a/2.PDF", 200),
                ann("2025年年度报告摘要（已取消）", "a/3.PDF", 300)]
        self.assertEqual([d["adjunct_url"] for d in cninfo.select_annual_reports(rows, "summary")], ["a/2.PDF"])

    def test_newest_fiscal_year_wins_over_order(self):
        rows = [ann("2023年年度报告摘要", "a/1.PDF", 1), ann("2024年年度报告", "a/2.PDF", 2),
                ann("2024年年度报告摘要", "a/3.PDF", 2)]
        docs = cninfo.select_annual_reports(list(reversed(rows)), "summary", as_of=self.AS_OF)
        self.assertEqual([(d["year"], d["kind"]) for d in docs], [(2024, "summary"), (2024, "full")])

    def test_parse_title(self):
        self.assertEqual(cninfo.parse_title("2025年年度报告摘要")["kind"], "summary")
        self.assertEqual(cninfo.parse_title("壬示酒业2025年年度报告")["kind"], "full")
        self.assertEqual(cninfo.parse_title("2025年度报告全文")["year"], 2025)
        self.assertEqual(cninfo.parse_title("2025年年度报告<em>摘要</em>")["kind"], "summary")
        self.assertTrue(cninfo.parse_title("2025年年度报告摘要（已取消）")["cancelled"])
        # superseded pre-revision copies and annulled copies are dropped like cancelled ones
        for t in ("2025年年度报告（更正前）", "2025年年度报告摘要（修订前）", "2025年年度报告（更新前）",
                  "2025年年度报告（已废止）", "2025年年度报告（作废）"):
            p = cninfo.parse_title(t)
            self.assertEqual((p["cancelled"], p["revision"]), (True, False), t)
        self.assertEqual(cninfo.parse_title("2025年年度报告（更正后）")["revision"], True)
        rows = [ann("2025年年度报告（更正前）", "a/9.PDF", 100), ann("2025年年度报告（更正后）", "a/1.PDF", 100)]
        self.assertEqual([d["adjunct_url"] for d in cninfo.select_annual_reports(rows, "full")], ["a/1.PDF"])
        for t in ("2025年半年度报告", "2025年半年度报告摘要", "关于2025年年度报告的更正公告", "2025年第三季度报告",
                  "2025年年度报告披露提示性公告", "监事会关于2025年年度报告的书面审核意见", "H股公告-2025年年度报告",
                  "", None):
            self.assertIsNone(cninfo.parse_title(t), t)


# =========================================================================== PDF extraction


class TestPdfExtraction(unittest.TestCase):
    def test_summary_fixture(self):
        text, note, backend = fixture_section("summary")
        self.assertEqual((note, backend), ("ok:end=accounting_data", "pymupdf"))
        self.assertTrue(text.startswith("2、报告期主要业务或产品简介\n公司示例维护协同结构"), text[:80])
        self.assertIn("身份认证", text)
        self.assertIn("（3）安全芯片", text)
        self.assertTrue(text.rstrip().endswith("网络数据3.83%。"), text[-80:])
        self.assertNotIn("主要会计数据", text)
        self.assertNotIn("年度报告摘要", text)                              # page-2 header cleaned
        self.assertNotIn("\n2\n", text)                                    # page number cleaned
        self.assertIn("区域计划产品。报告期内，身份认", text)                # wrapped lines re-flowed

    def test_full_fixture(self):
        text, note, _ = fixture_section("full")
        self.assertEqual(note, "ok:end=core_competence")
        self.assertTrue(text.startswith("一、报告期内公司从事的主要业务\n报告期内公司物流营业收入"), text[:80])
        self.assertIn("身份认证", text)
        self.assertNotIn("核心竞争力分析", text)
        self.assertNotIn("年度报告全文", text)                              # running header of pages 12-13
        self.assertNotIn("\n12\n", text)
        self.assertIn("智能终端物流终端生产载体物服务工。", text)
        self.assertTrue(cninfo.MIN_SECTION_CHARS <= len(text) <= cninfo.MAX_SECTION_CHARS)

    def test_pypdf_backend_agrees(self):
        for kind in ("summary", "full"):
            a, na, _ = fixture_section(kind)
            b, nb, backend = fixture_section(kind, "pypdf")
            self.assertEqual((backend, nb), ("pypdf", na))
            self.assertEqual(a.split("\n", 1)[0], b.split("\n", 1)[0])
            self.assertLess(abs(len(a) - len(b)), max(50, len(a) // 20))

    def test_pypdf_used_when_pymupdf_fails(self):
        with mock.patch.object(cninfo, "_pages_pymupdf", side_effect=RuntimeError("broken")):
            text, note, backend = cninfo.extract_from_pdf(SUMMARY_PDF, "summary")
        self.assertEqual((note, backend), ("ok:end=accounting_data", "pypdf"))

    def test_other_rules_as_fallback(self):
        text, note, _ = cninfo.extract_from_pdf(FULL_PDF, "summary")
        self.assertEqual(note, "ok:end=core_competence;rules=full")
        self.assertTrue(text.startswith("一、报告期内公司从事的主要业务"))

    def test_garbage_pdf(self):
        text, note, _ = cninfo.extract_from_pdf(b"%PDF-1.4 not really a pdf", "summary")
        self.assertIsNone(text)
        self.assertTrue(note.startswith(("pdf_parse_failed", "no_text_layer")), note)

    def test_short_description(self):
        text, _, _ = fixture_section("full")
        d = cninfo.short_description(text)
        self.assertLessEqual(len(d), cninfo.SHORT_DESC_CHARS)
        self.assertTrue(d.startswith("报告期内公司物流营业收入"))             # heading line dropped
        self.assertTrue(d.endswith(("。", "；", "！", "？")), d[-20:])
        s, _, _ = fixture_section("summary")
        self.assertEqual(cninfo.short_description(s), s.split("\n", 1)[1])   # short section: all of it
        self.assertEqual(cninfo.short_description(None), "")


class TestCleaning(unittest.TestCase):
    def test_headers_and_page_numbers_removed(self):
        pages = [f"示例科技股份有限公司2025 年年度报告全文\n{i}\n正文第{i}页的内容。\n- {i} -" for i in range(1, 6)]
        self.assertEqual(cninfo.clean_pages(pages), [f"正文第{i}页的内容。" for i in range(1, 6)])

    def test_body_line_repeated_outside_zone_kept(self):
        body = "\n".join(f"第{j}行" for j in range(10))
        pages = [f"页眉公司\n{body}\n单位：元\n{body}\n{i}" for i in range(1, 4)]
        lines = cninfo.clean_pages(pages)
        self.assertEqual(lines.count("单位：元"), 3)
        self.assertNotIn("页眉公司", lines)
        self.assertFalse(any(ln.isdigit() for ln in lines))

    def test_single_page_only_page_number(self):
        self.assertEqual(cninfo.clean_pages(["公司名称\n内容。\n第 1 页 共 1 页"]), ["公司名称", "内容。"])

    def test_real_summary_pages(self):
        pages, _ = cninfo.pdf_pages(SUMMARY_PDF)
        lines = cninfo.clean_pages(pages)
        self.assertEqual(sum(1 for ln in lines if ln == "辛示科信科技股份有限公司2025年年度报告摘要"), 1)  # title kept
        self.assertFalse(any(ln in ("1", "2", "3", "4") for ln in lines))
        self.assertEqual(lines[0], "证券代码：309386")

    def test_normalise_line(self):
        self.assertEqual(cninfo.normalise_line("截至 2025 年12 月31 日，USB Key、OTP 以及"),
                         "截至2025年12月31日，USB Key、OTP以及")


class TestSectionRules(unittest.TestCase):
    FILL = "公司主要从事工业软件的研发与销售，产品覆盖设计、仿真与制造环节，客户包括汽车与航空企业。"

    def lines(self, *parts: str, body: int = 6) -> list[str]:
        out = []
        for p in parts:
            out.append(p)
            if not p.startswith("#"):
                out.extend([self.FILL] * body)
        return [x.lstrip("#") for x in out]

    def test_summary_sse_style_heading(self):
        ls = self.lines("1 公司简介", "2 报告期公司主要业务简介", "3 公司主要会计数据和财务指标")
        text, note = cninfo.extract_business_section(ls, "summary")
        self.assertEqual(note, "ok:end=accounting_data")
        self.assertTrue(text.startswith("2 报告期公司主要业务简介"))

    def test_summary_star_market_n_m_headings(self):
        ls = self.lines("2.1 公司简介", "2.2 报告期公司主要业务简介", "2.3 主要会计数据和财务指标")
        text, note = cninfo.extract_business_section(ls, "summary")
        self.assertEqual(note, "ok:end=accounting_data")
        self.assertTrue(text.startswith("2.2 报告期公司主要业务简介"))
        self.assertNotIn("主要会计数据", text)
        ls = self.lines("2.1 公司简介", "2.2 报告期公司主要业务简介", "2.4 前十名股东情况")
        self.assertEqual(cninfo.extract_business_section(ls, "summary")[1], "ok:end=next_item")
        # decimal amounts are not 'N.M' headings
        self.assertIsNone(cninfo.parse_heading("2.57亿元"))
        self.assertEqual(cninfo.parse_heading("2.2 报告期公司主要业务简介")["level"], "sub")

    def test_summary_next_item_fallback(self):
        ls = self.lines("2、报告期主要业务或产品简介", "3、股本及股东情况")
        self.assertEqual(cninfo.extract_business_section(ls, "summary")[1], "ok:end=next_item")

    def test_full_sse_style_and_end_variants(self):
        ls = self.lines("第三节 管理层讨论与分析", "一、经营情况讨论与分析", "三、报告期内公司从事的业务情况",
                        "四、报告期内核心竞争力分析")
        text, note = cninfo.extract_business_section(ls, "full")
        self.assertEqual(note, "ok:end=core_competence")
        self.assertTrue(text.startswith("三、报告期内公司从事的业务情况"))
        ls = self.lines("二、报告期内公司所从事的主要业务、经营模式、行业情况及研发情况说明", "三、核心技术与研发进展")
        self.assertEqual(cninfo.extract_business_section(ls, "full")[1], "ok:end=core_tech")
        ls = self.lines("一、报告期内公司从事的主要业务", "第四节 公司治理")
        self.assertEqual(cninfo.extract_business_section(ls, "full")[1], "ok:end=next_part")
        ls = self.lines("一、报告期内公司从事的主要业务", "二、主要资产重大变化情况")
        self.assertEqual(cninfo.extract_business_section(ls, "full")[1], "ok:end=major_assets")

    def test_toc_row_and_sentence_are_not_headings(self):
        ls = ["一、报告期内公司从事的主要业务......................11", "三、核心竞争力分析......................13",
              "报告期内公司从事的主要业务未发生重大变化，公司继续专注于主业。"] + \
            self.lines("一、报告期内公司从事的主要业务", "三、核心竞争力分析")
        text, note = cninfo.extract_business_section(ls, "full")
        self.assertEqual(note, "ok:end=core_competence")
        self.assertTrue(text.startswith("一、报告期内公司从事的主要业务\n公司主要从事"))
        self.assertIsNone(cninfo.parse_heading("报告期内公司从事的主要业务未发生重大变化。"))

    def test_bounds_and_failures(self):
        self.assertEqual(cninfo.extract_business_section(self.lines("一、公司信息"), "full"),
                         (None, "start_heading_not_found"))
        self.assertEqual(cninfo.extract_business_section(self.lines("一、报告期内公司从事的主要业务"), "full"),
                         (None, "end_heading_not_found"))
        short = self.lines("一、报告期内公司从事的主要业务", "三、核心竞争力分析", body=1)
        text, note = cninfo.extract_business_section(short, "full")
        self.assertIsNone(text)
        self.assertTrue(note.startswith("section_too_short:"), note)
        with mock.patch.object(cninfo, "MAX_SECTION_CHARS", 250):
            text, note = cninfo.extract_business_section(
                self.lines("一、报告期内公司从事的主要业务", "三、核心竞争力分析"), "full")
        self.assertIsNone(text)
        self.assertTrue(note.startswith("section_too_long:"), note)
        self.assertEqual(cninfo.extract_business_section([], "full"), (None, "empty_text"))

    def test_too_long_named_end_falls_back_to_next_item(self):
        ls = self.lines("一、报告期内公司从事的主要业务", "二、报告期内公司所处行业情况", body=5) + \
            [self.FILL] * 20 + ["三、核心竞争力分析"]
        with mock.patch.object(cninfo, "MAX_SECTION_CHARS", 500):
            text, note = cninfo.extract_business_section(ls, "full")
        self.assertEqual(note, "ok:end=next_item")
        self.assertNotIn("行业情况", text)

    def test_join_paragraphs(self):
        full = "甲" * 40
        out = cninfo.join_paragraphs(["一、标题", full, "乙乙。", full, "要求", "（1）小节", "Point-", "of-Interaction"])
        self.assertEqual(out.split("\n"), ["一、标题", full + "乙乙。", full + "要求", "（1）小节",
                                           "Point-", "of-Interaction"])


# =========================================================================== sync


class SyncBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), min_interval_s=1.0)
        with store.session(self.cfg) as con:
            self.snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="test", request=None,
                                              raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
                                              duration_s=None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, security_id: str, cap: float | None, isin: str | None = None) -> None:
        exchange, symbol = security_id.split(":", 1)
        row = dict(security_id=security_id, exchange=exchange, symbol=symbol, name=symbol, isin=isin, country="China",
                   tv_type="stock", tv_subtype="common", is_primary=True, price_currency=None,
                   fundamental_currency=None, sector=None, industry=None,
                   company_key=store.company_key(isin, security_id), first_seen_snapshot=self.snap,
                   last_seen_snapshot=self.snap, last_seen_at=store.now_utc(), active=True)
        with store.session(self.cfg) as con:
            store.upsert_many(con, "securities", list(row), [list(row.values())])
            store.upsert_many(con, "market_daily", ["security_id", "as_of", "market_cap_usd", "snapshot_id"],
                              [[security_id, dt.date(2026, 9, 26), cap, self.snap]])

    def add_standard(self) -> None:
        self.add("SSE:609519", 2e11, "CNE0000018R8")
        self.add("SZSE:309386", 1e9, "CNE100001Q95")
        self.add("NASDAQ:NICE", 9e9, "US6536561086")      # never queued

    def q(self, sql: str, params=()) -> list[tuple]:
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            return con.execute(sql, list(params)).fetchall()

    def states(self) -> dict[str, tuple]:
        return {r[0]: r[1:] for r in self.q(
            "SELECT security_id, status, http_status, attempts, note FROM crawl_state WHERE source_id = ?",
            [cninfo.SOURCE_ID])}

    def last_run(self) -> tuple:
        return self.q("SELECT status, requests, note, command FROM runs ORDER BY started_at DESC LIMIT 1")[0]

    def run_sync(self, client, **kw) -> tuple[dict, str]:
        kw.setdefault("per_company", True)     # these tests pin the per-company path; fast mode: test_cninfo_fast
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            summary = cninfo.sync(self.cfg, client, **kw)
        return summary, err.getvalue()


class TestSync(SyncBase):
    def test_full_run(self):
        self.add_standard()
        client = standard_client()
        summary, _ = self.run_sync(client, progress_every=1)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual((summary["securities_mapped"], summary["queued"], summary["ok_by_form"]),
                         (2, 2, {"annual_report_summary": 2}))
        self.assertEqual(client.calls, [cninfo.STOCK_LIST_URL, "POST 609519", URL_609519_SUMMARY,
                                        "POST 309386", SUMMARY_URL])       # market cap order
        self.assertTrue(all(k.get("rate_key") == cninfo.RATE_KEY for k in client.kwargs))
        self.assertEqual(client.forms[1], {"stock": f"309386,{ORG_309386}", "tabName": "fulltext",
                                           "pageSize": "30", "pageNum": "1", "column": "szse",
                                           "category": "category_ndbg_szsh;"})
        self.assertEqual(client.kwargs[3]["headers"]["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(self.q("SELECT security_id, id_value FROM identifiers WHERE id_type = 'cninfo_orgid' "
                                "ORDER BY 1"), [("SSE:609519", ORG_609519), ("SZSE:309386", ORG_309386)])
        doc = self.q("SELECT doc_id, security_id, company_key, source_id, cik, form, section, accession, filing_date, "
                     "report_date, url, raw_sha256, raw_bytes, text_path, text_sha256, text_chars, extractor, "
                     "extract_note, snapshot_id FROM documents WHERE cik = ?", [ORG_309386])[0]
        self.assertEqual(doc[:11], (f"cninfo_annual_report:{ORG_309386}:9225102236:business", "SZSE:309386",
                                    "isin:CNE100001Q95", "cninfo_annual_report", ORG_309386, "annual_report_summary",
                                    "business", "9225102236", dt.date(2026, 4, 15), dt.date(2025, 12, 31),
                                    SUMMARY_URL))
        self.assertEqual((doc[11], doc[12]), (store.sha256(SUMMARY_PDF), len(SUMMARY_PDF)))
        tp = Path(doc[13])
        self.assertEqual(tp, self.cfg.home / "docs" / "cninfo" / "309386" / "9225102236-business.txt")
        text = tp.read_text()
        self.assertTrue(text.startswith("2、报告期主要业务或产品简介"))
        self.assertEqual((doc[14], doc[15]), (store.sha256(text.encode()), len(text)))
        self.assertEqual((doc[16], doc[17]), (cninfo.EXTRACTOR_VERSION + "/pymupdf", "ok:end=accounting_data"))
        self.assertTrue(self.q("SELECT count(*) FROM snapshots WHERE snapshot_id = ? AND source_id = ?",
                               [doc[18], cninfo.SOURCE_ID])[0][0])
        desc = self.q("SELECT text, lang, source_url, match_method FROM descriptions WHERE security_id = 'SZSE:309386' "
                      "AND source_id = ?", [cninfo.SOURCE_ID])[0]
        self.assertTrue(desc[0].startswith("公司示例维护协同结构"))
        self.assertIn("身份认证", desc[0])
        self.assertEqual(desc[1:], ("zh", SUMMARY_URL, "cninfo_orgid"))
        st = self.states()
        self.assertEqual({k: v[0] for k, v in st.items()}, {"SSE:609519": "ok", "SZSE:309386": "ok"})
        run = self.last_run()
        self.assertEqual((run[0], run[1], run[3]), ("ok", 5, "sync-cninfo"))
        kinds = {r[0] for r in self.q("SELECT kind FROM snapshots WHERE source_id = ?", [cninfo.SOURCE_ID])}
        self.assertEqual(kinds, {"stock_list", "query_batch", "report_batch"})

    def test_kind_full(self):
        self.add_standard()
        summary, _ = self.run_sync(standard_client(), kind="full", codes=["309386"])
        self.assertEqual(summary["ok_by_form"], {"annual_report": 1})
        doc = self.q("SELECT form, extract_note, url FROM documents")[0]
        self.assertEqual(doc, ("annual_report", "ok:end=core_competence", FULL_URL))

    def test_summary_missing_falls_back_to_full(self):
        self.add_standard()
        client = standard_client(posts={"609519": query_609519(include_summary=False)})
        summary, _ = self.run_sync(client, codes=["SSE:609519"])
        self.assertIn(URL_609519_FULL, client.calls)
        self.assertEqual((summary["ok_by_form"], summary["fallback_full"]), ({"annual_report": 1}, 1))

    def test_summary_extract_failure_falls_back_to_full(self):
        self.add_standard()
        client = standard_client(gets={SUMMARY_URL: b"%PDF-1.4 broken"})
        summary, _ = self.run_sync(client, codes=["309386"])
        self.assertEqual(client.calls[-2:], [SUMMARY_URL, FULL_URL])
        rows = self.q("SELECT form, text_path IS NOT NULL, extract_note FROM documents ORDER BY form")
        self.assertEqual([r[:2] for r in rows], [("annual_report", True), ("annual_report_summary", False)])
        self.assertIn("summary_failed:", rows[0][2])
        self.assertEqual(self.states()["SZSE:309386"][0], "ok")
        # rerun: the ok full report is found through the failed summary -> nothing downloaded
        client2 = standard_client(gets={SUMMARY_URL: b"%PDF-1.4 broken"})
        summary2, _ = self.run_sync(client2, codes=["309386"])
        self.assertEqual(client2.calls, [cninfo.STOCK_LIST_URL, "POST 309386"])
        self.assertEqual(summary2["skipped_unchanged"], 1)

    def test_extract_failed(self):
        self.add_standard()
        client = standard_client(gets={SUMMARY_URL: b"%PDF-x", FULL_URL: b"%PDF-y"})
        summary, _ = self.run_sync(client, codes=["309386"])
        self.assertEqual(summary["status_extract_failed"], 1)
        self.assertEqual(self.states()["SZSE:309386"][0], "extract_failed")
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE text_path IS NULL")[0][0], 2)
        self.assertEqual(self.q("SELECT count(*) FROM descriptions")[0][0], 0)

    def test_no_annual_report(self):
        self.add_standard()
        client = standard_client(posts={"309386": json.dumps({"announcements": None}).encode()})
        self.run_sync(client, codes=["309386"])
        self.assertEqual(self.states()["SZSE:309386"][:2], ("no_annual_report", 200))

    def test_incremental_skip_and_refresh(self):
        self.add_standard()
        self.run_sync(standard_client())
        client = standard_client()
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["skipped_unchanged"], 2)
        self.assertEqual(client.calls, [cninfo.STOCK_LIST_URL, "POST 609519", "POST 309386"])
        client = standard_client()
        summary, _ = self.run_sync(client, refresh=True)
        self.assertEqual(summary["skipped_unchanged"], 0)
        self.assertIn(SUMMARY_URL, client.calls)
        self.assertEqual(self.states()["SZSE:309386"][2], 2)      # attempts counted (skip does not count)

    def test_failed_rows_of_an_older_extractor_are_retried(self):
        self.add_standard()
        client = standard_client(gets={SUMMARY_URL: b"%PDF-x", FULL_URL: b"%PDF-y"})
        self.run_sync(client, codes=["309386"])
        self.assertEqual(self.states()["SZSE:309386"][0], "extract_failed")
        # same extractor version: nothing new to try, no download
        client = standard_client(gets={SUMMARY_URL: b"%PDF-x", FULL_URL: b"%PDF-y"})
        summary, _ = self.run_sync(client, codes=["309386"])
        self.assertEqual((client.calls, summary["skipped_unchanged"]), ([cninfo.STOCK_LIST_URL, "POST 309386"], 1))
        # an improved extractor (new EXTRACTOR_VERSION) retries the failed rows without --refresh
        with mock.patch.object(cninfo, "EXTRACTOR_VERSION", "cninfo-v99"):
            client = standard_client()
            summary, _ = self.run_sync(client, codes=["309386"])
            self.assertIn(SUMMARY_URL, client.calls)
            self.assertEqual(self.states()["SZSE:309386"][0], "ok")
            # ... and successful rows are never fetched again
            client = standard_client()
            summary, _ = self.run_sync(client, codes=["309386"])
            self.assertEqual(client.calls, [cninfo.STOCK_LIST_URL, "POST 309386"])

    def test_filters_limit_and_min_mcap(self):
        self.add_standard()
        summary, _ = self.run_sync(standard_client(), min_mcap_usd=5e9)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(set(self.states()), {"SSE:609519"})
        summary, _ = self.run_sync(standard_client(), limit=0)
        self.assertEqual(summary["queued"], 0)

    def test_blocked_stops_immediately(self):
        self.add_standard()
        seen = []
        client = standard_client(posts={"309386": Blocked(cninfo.QUERY_URL, 429, "http_429")})
        self.add("SZSE:000001", 1e8)
        summary, _ = self.run_sync(client, on_blocked=seen.append)
        self.assertEqual((summary["stopped_reason"], summary["blocked_at"], summary["status"]),
                         ("blocked", "309386", "blocked"))
        self.assertNotIn("POST 000001", client.calls)
        self.assertEqual(len(seen), 1)
        st = self.states()
        self.assertEqual(st["SZSE:309386"][:2], ("blocked", 429))
        self.assertEqual(st["SSE:609519"][0], "ok")                      # earlier work flushed
        self.assertNotIn("SZSE:000001", st)
        self.assertEqual(self.last_run()[0], "blocked")
        marker = guard.recent_block_marker(self.cfg, "sync-cninfo")
        self.assertIsNotNone(marker)
        self.assertIn("http_429", marker["note"])

    def test_blocked_on_pdf_download(self):
        self.add_standard()
        client = standard_client(gets={URL_609519_SUMMARY: Blocked(URL_609519_SUMMARY, 403, "http_403")})
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["stopped_reason"], "blocked")
        self.assertNotIn("POST 309386", client.calls)
        self.assertEqual(self.states()["SSE:609519"][0], "blocked")

    def test_blocked_on_stock_list(self):
        self.add_standard()
        client = FakeCninfoClient({cninfo.STOCK_LIST_URL: Blocked(cninfo.STOCK_LIST_URL, 403, "http_403")})
        summary, _ = self.run_sync(client)
        self.assertEqual((summary["stopped_reason"], client.calls), ("blocked", [cninfo.STOCK_LIST_URL]))
        self.assertEqual(self.last_run()[0], "blocked")
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-cninfo"))

    def test_keyboard_interrupt_flushes(self):
        self.add_standard()
        client = standard_client(gets={SUMMARY_URL: KeyboardInterrupt()})
        summary, _ = self.run_sync(client)
        self.assertEqual(summary["stopped_reason"], "interrupted")
        self.assertEqual(self.states()["SSE:609519"][0], "ok")
        self.assertEqual(self.last_run()[0], "interrupted")

    def test_consecutive_errors_stop(self):
        codes = [r["code"] for r in STOCK_LIST["stockList"] if r["code"][0] in "03"][:7]
        codes = list(dict.fromkeys(codes))
        for i, c in enumerate(codes):
            self.add(f"SZSE:{c}", 1e10 - i)
        client = FakeCninfoClient({cninfo.STOCK_LIST_URL: STOCK_LIST_RAW}, {})   # every query -> 404
        summary, _ = self.run_sync(client, batch_size=2)
        self.assertEqual(summary["stopped_reason"], "consecutive_errors")
        self.assertEqual(summary["attempted"], cninfo.MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(summary["failures_by_note"], {"query_http_404": 5})
        self.assertEqual(self.last_run()[0], "stopped_errors")

    def test_no_db_connection_held_during_fetch(self):
        self.add_standard()
        with store.session(self.cfg):
            self.assertNotEqual(self._probe_write(), 0)       # sanity: a held session blocks another process
        results: list[tuple[str, int]] = []
        summary, _ = self.run_sync(standard_client(on_request=lambda u: results.append((u, self._probe_write()))))
        self.assertEqual(summary["requests"], 5)
        self.assertEqual(len(results), 5)
        for url, code in results:
            self.assertEqual(code, 0, f"DB was locked while fetching {url}")

    def _probe_write(self) -> int:
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from pathlib import Path; from jevscreen import store; from jevscreen.config import Config\n"
                "with store.session(Config(home=Path(sys.argv[2])), wait_s=0) as con:\n"
                "    con.execute('CREATE TABLE IF NOT EXISTS probe (x INTEGER)'); con.execute('INSERT INTO probe VALUES (1)')\n")
        return subprocess.run([sys.executable, "-c", code, str(SRC), str(self.cfg.home)],
                              capture_output=True, timeout=120).returncode


# =========================================================================== live-validation formats (2026-09-26)
# Titles below mirror the announcement title formats returned by the hisAnnouncement query on 2026-09-26. The
# cninfo_<code>_2025_<kind>_pages.txt fixtures are SYNTHETIC page texts (pages joined by '\f') for invented issuers
# (609201-609204 banks, 609205 telecom, 609206 energy, 609207 insurer; unlisted 6092xx codes): they keep the page
# layout of the live cases (headings, section numbers, running headers, page numbers, table cells, line wrapping)
# with every other word replaced by filler and every figure's digits permuted, so each exercises the same
# extraction path as the live report it stands for.

T_DAY = 86_400_000


def fixture_pages(name: str) -> list[str]:
    return (FIXTURES / f"cninfo_{name}_pages.txt").read_text(encoding="utf-8").split("\f")


_PAGES_CACHE: dict = {}


def pages_section(name: str, kind: str) -> tuple[str | None, str]:
    key = (name, kind)
    if key not in _PAGES_CACHE:
        _PAGES_CACHE[key] = cninfo.extract_from_pages(fixture_pages(name), kind)
    return _PAGES_CACHE[key]


class TestRealTitleFormats(unittest.TestCase):
    def check(self, title: str, year: int, kind: str, revision: bool = False) -> None:
        p = cninfo.parse_title(title)
        self.assertIsNotNone(p, title)
        self.assertEqual((p["year"], p["kind"], p["revision"], p["english"], p["cancelled"]),
                         (year, kind, revision, False, False), title)

    def test_company_name_prefix(self):
        self.check("富士康工业互联网股份有限公司2025年年度报告摘要", 2025, "summary")
        self.check("富士康工业互联网股份有限公司2025年年度报告", 2025, "full")
        self.check("富士康工业互联网股份有限公司2023年年度报告（修订后）", 2023, "full", revision=True)
        self.check("中国银行股份有限公司2025年年度报告摘要", 2025, "summary")
        self.check("中国移动：2025年年度报告摘要", 2025, "summary")
        self.check("中国移动：2025年年度报告", 2025, "full")
        self.check("中国平安2025年年度报告", 2025, "full")

    def test_nianbao_and_nian_du_bao_gao(self):
        self.check("中国石油天然气股份有限公司2025年年报", 2025, "full")
        self.check("中国石油天然气股份有限公司2024年度报告", 2024, "full")
        self.check("中国石油天然气股份有限公司2021 年度报告", 2021, "full")
        self.check("建设银行2025年度报告", 2025, "full")
        self.check("建设银行2025年年度报告摘要", 2025, "summary")
        self.check("建设银行2022年年报", 2022, "full")
        self.check("甲示银行2025年度报告摘要", 2025, "summary")
        self.check("农业银行2025年度报告", 2025, "full")
        self.check("2025年报摘要", 2025, "summary")

    def test_results_announcement_and_chinese_numeral_years(self):
        self.check("中国石油天然气股份有限公司二零二四年度业绩公告（年度报告摘要）", 2024, "summary")
        self.check("中国石油天然气股份有限公司二零二零年度业绩公告（年度报告摘要）", 2020, "summary")
        self.check("二〇二五年度业绩报告", 2025, "summary")
        self.check("某某股份二〇二五年年度报告", 2025, "full")
        self.assertEqual(cninfo._year_value("二○二三"), 2023)

    def test_skipped_copies(self):
        for t in ("建设银行H股公告-2025年年度报告", "建设银行H股公告-2021年年报", "H股公告-2025年年度报告",
                  "甲示银行H股公告-2022年度报告", "H股-2015年度报告", "2025 Annual Report",
                  "中国平安：平安银行股份有限公司2025年年度报告摘要", "关于2025年年度报告的更正公告",
                  "2025年半年度报告", "2025年年度报告披露提示性公告", "2025年年度股东大会决议公告"):
            self.assertIsNone(cninfo.parse_title(t), t)
            self.assertIsNone(cninfo.looks_like_annual_report(t), t)
        self.assertTrue(cninfo.parse_title("2025年度报告（英文版）")["english"])
        self.assertTrue(cninfo.parse_title("壬示酒业2025年年度报告（英文版）")["english"])
        for t in ("中国石油天然气股份有限公司2025年年报（更正前）", "2025年年度报告摘要（修订前）"):
            p = cninfo.parse_title(t)
            self.assertEqual((p["cancelled"], p["revision"]), (True, False), t)
        for t in ("2025年年度报告（更正后）", "2025年年度报告（更新后）", "2025年年度报告摘要（修订版）"):
            self.assertTrue(cninfo.parse_title(t)["revision"], t)

    def test_ascii_zero_in_chinese_year(self):
        self.check("二0二五年度业绩公告（年度报告摘要）", 2025, "summary")
        self.assertEqual(cninfo.looks_like_annual_report("XX银行二0二五年度报告全文及摘要"), 2025)

    def test_own_full_name_after_colon_counts_for_the_guard(self):
        t = "中国平安：中国平安保险（集团）股份有限公司2025年年度报告"
        self.assertIsNone(cninfo.parse_title(t))                        # not selected ...
        self.assertEqual(cninfo.looks_like_annual_report(t), 2025)      # ... but never silently an older year
        self.assertEqual(cninfo.looks_like_annual_report("甲示银行：中国甲示银行股份有限公司2025年度报告"), 2025)
        self.assertIsNone(cninfo.looks_like_annual_report("中国平安：平安银行股份有限公司2025年年度报告摘要"))

    def test_results_flash_forecast_and_correction_are_not_annual_reports(self):
        for t in ("2025年度业绩快报", "2025年度业绩预告", "境外监管公告-2025年年度报告", "2025年年度报告更正公告",
                  "XX股份2025年度业绩快报公告"):
            self.assertIsNone(cninfo.parse_title(t), t)
            self.assertIsNone(cninfo.looks_like_annual_report(t), t)

    def test_annual_report_like_but_unparsed(self):
        self.assertIsNone(cninfo.parse_title("富士康工业互联网股份有限公司2025年年度报告及摘要"))
        self.assertEqual(cninfo.looks_like_annual_report("富士康工业互联网股份有限公司2025年年度报告及摘要"), 2025)
        self.assertEqual(cninfo.looks_like_annual_report("XX银行二零二五年度报告全文及摘要"), 2025)


class TestSelectionLiveFormats(unittest.TestCase):
    AS_OF = dt.date(2026, 9, 26)

    def select(self, rows, kind="summary", as_of=None, report=None):
        return cninfo.select_annual_reports(rows, kind, as_of=as_of or self.AS_OF, report=report)

    def test_601138_company_prefix_beats_old_unprefixed_titles(self):
        t = T_2026_04_01
        rows = [ann("富士康工业互联网股份有限公司2025年年度报告", "finalpage/2026-03-11/1225004420.PDF", t),
                ann("富士康工业互联网股份有限公司2025年年度报告摘要", "finalpage/2026-03-11/1225004416.PDF", t),
                ann("富士康工业互联网股份有限公司2024年年度报告", "finalpage/2025-04-30/1223421561.PDF", t - 330 * T_DAY),
                ann("富士康工业互联网股份有限公司2023年年度报告（修订后）", "finalpage/2024-03-28/1219424472.PDF",
                    t - 730 * T_DAY),
                ann("2019年年度报告摘要", "finalpage/2020-03-31/1207432132.PDF", t - 2200 * T_DAY),
                ann("2019年年度报告", "finalpage/2020-03-31/1207432131.PDF", t - 2200 * T_DAY)]
        docs = self.select(rows)
        self.assertEqual([(d["year"], d["kind"], d["announcement_id"], d["flags"]) for d in docs],
                         [(2025, "summary", "1225004416", []), (2025, "full", "1225004420", [])])

    def test_601857_nianbao_and_chinese_numeral_summary(self):
        t = T_2026_04_01
        rows = [ann("中国石油天然气股份有限公司2025年年报", "finalpage/2026-03-30/1225049376.PDF", t),
                ann("中国石油天然气股份有限公司2024年度报告", "finalpage/2025-03-31/1222962164.PDF", t - 365 * T_DAY),
                ann("中国石油天然气股份有限公司二零二四年度业绩公告（年度报告摘要）",
                    "finalpage/2025-03-31/1222962163.PDF", t - 365 * T_DAY),
                ann("2020年度报告", "finalpage/2021-03-26/1209453479.PDF", t - 1800 * T_DAY)]
        docs = self.select(rows)
        self.assertEqual([(d["year"], d["kind"], d["announcement_id"]) for d in docs],
                         [(2025, "full", "1225049376")])          # 2025 has no summary: the full report only
        self.assertEqual([d["announcement_id"] for d in self.select(rows, "full")], ["1225049376"])
        # the 2024 summary written in Chinese numerals is found for 2024
        docs = self.select(rows[1:3], as_of=dt.date(2025, 9, 1))
        self.assertEqual([(d["year"], d["kind"]) for d in docs], [(2024, "summary"), (2024, "full")])

    def test_601939_h_share_copy_skipped(self):
        t = T_2026_04_01
        rows = [ann("建设银行H股公告-2025年年度报告", "finalpage/2026-04-28/1225204147.PDF", t + 27 * T_DAY),
                ann("建设银行2025年年度报告摘要", "finalpage/2026-03-28/1225048162.PDF", t),
                ann("建设银行2025年度报告", "finalpage/2026-03-28/1225046718.PDF", t),
                ann("建设银行2024年年度报告摘要", "finalpage/2025-03-29/1222940502.PDF", t - 365 * T_DAY)]
        for kind, want in (("summary", ["1225048162", "1225046718"]), ("full", ["1225046718"])):
            self.assertEqual([d["announcement_id"] for d in self.select(rows, kind)], want)

    def test_subsidiary_report_filed_by_parent_skipped(self):
        t = T_2026_04_01
        rows = [ann("中国平安2025年年度报告摘要", "finalpage/2026-03-27/1225038326.PDF", t),
                ann("中国平安：平安银行股份有限公司2025年年度报告摘要", "finalpage/2026-03-21/1225023541.PDF",
                    t + 5 * T_DAY)]
        self.assertEqual([d["announcement_id"] for d in self.select(rows)], ["1225038326"])

    def test_revision_supersedes_original_even_when_posted_earlier(self):
        rows = [ann("XX公司2025年年度报告（修订后）", "a/1.PDF", 100), ann("XX公司2025年年度报告", "a/2.PDF", 200)]
        docs = self.select(rows, "full")
        self.assertEqual([(d["adjunct_url"], d["revision"]) for d in docs], [("a/1.PDF", True)])

    def test_newest_year_unparsed_guard(self):
        rows = [ann("XX公司2025年年度报告及摘要", "a/1.PDF", 300), ann("XX公司2024年年度报告", "a/2.PDF", 200),
                ann("XX公司2024年年度报告摘要", "a/3.PDF", 200)]
        rep: dict = {}
        self.assertEqual(self.select(rows, report=rep), [])                 # never the older 2024 report
        self.assertEqual(rep["note"], "newest_year_unparsed:2025")
        self.assertEqual(rep["unparsed_titles"], ["XX公司2025年年度报告及摘要"])
        # an unparsed title of an OLDER year does not block the newest one
        rows = [ann("XX公司2023年年度报告及摘要", "a/1.PDF", 100), ann("XX公司2024年年度报告", "a/2.PDF", 200)]
        rep = {}
        self.assertEqual([d["year"] for d in self.select(rows, "full", report=rep)], [2024])
        self.assertIsNone(rep["note"])
        # H-share and English copies of a newer year never trigger the guard
        rows = [ann("H股公告-2025年年度报告", "a/1.PDF", 300), ann("2025 Annual Report", "a/4.PDF", 300),
                ann("2025年度报告（英文版）", "a/5.PDF", 300), ann("2024年年度报告", "a/2.PDF", 200)]
        self.assertEqual([d["year"] for d in self.select(rows, "full", as_of=dt.date(2026, 4, 1))], [2024])

    def test_staleness_flag(self):
        rows = [ann("2024年年度报告", "a/2.PDF", 200)]
        self.assertEqual(self.select(rows, "full")[0]["flags"], ["stale:1"])      # 2025 reports were due 2026-04-30
        self.assertEqual(self.select(rows, "full", as_of=dt.date(2026, 4, 30))[0]["flags"], [])
        self.assertEqual(self.select(rows, "full", as_of=dt.date(2026, 5, 1))[0]["flags"], ["stale:1"])
        self.assertEqual(self.select([ann("2022年年度报告", "a/3.PDF", 1)], "full")[0]["flags"], ["stale:3"])
        self.assertEqual(cninfo.expected_latest_year(dt.date(2026, 9, 26)), 2025)
        self.assertEqual(cninfo.expected_latest_year(dt.date(2026, 3, 1)), 2024)

    def test_kind_full_uses_summary_when_newest_year_has_no_full_report(self):
        rows = [ann("2025年年度报告摘要", "a/1.PDF", 300), ann("2024年年度报告", "a/2.PDF", 200)]
        docs = self.select(rows, "full")
        self.assertEqual([(d["year"], d["kind"], d["flags"]) for d in docs], [(2025, "summary", ["summary_only"])])


class TestLiveSectionFixtures(unittest.TestCase):
    """Business sections of banks, insurers and A+H issuers (all 'start_heading_not_found' with cninfo-v2)."""

    def test_page_text_fixtures_use_the_unlisted_code_block(self):
        # invented issuers get unlisted 6092xx codes, never a one-digit variant of the live report's code
        codes = sorted({p.name.split("_")[1] for p in FIXTURES.glob("cninfo_*_pages.txt")})
        self.assertTrue(codes and all(c.startswith("6092") for c in codes), codes)

    def test_bank_a_summary_mda_opening_fallback(self):
        text, note = pages_section("609201_2025_summary", "summary")
        self.assertEqual(note, "ok:end=next_item;fallback:mda_opening")
        self.assertTrue(text.startswith("5.1经营情况概览\n2025环例“十四五”平台目标载体。"), text[:60])
        self.assertIn("年末集团总资产21.85例亿元", text)
        self.assertNotIn("5.2展望", text)
        self.assertLessEqual(len(text), cninfo.MDA_OPENING_CHARS)

    def test_bank_a_full_business_review(self):
        text, note = pages_section("609201_2025_full", "full")
        self.assertEqual(note, "ok:end=next_item;family=business_review")
        self.assertTrue(text.startswith("8.3业务综述\n专栏："), text[:60])
        self.assertIn("8.3.1\n公司金融业务", text)
        self.assertIn("示例银行目标样本体系银行", text)                     # last paragraph before 8.4
        self.assertNotIn("8.4风险管理", text)
        self.assertNotIn("8.1经济金融及监管环境", text)
        self.assertNotIn("2025年度报告（A股）", text)                        # running header removed

    def test_bank_b_summary_main_business(self):
        text, note = pages_section("609202_2025_summary", "summary")
        self.assertEqual(note, "ok:end=next_item;family=main_business")
        self.assertTrue(text.startswith("2.2主要业务简介\n乙示银行股份有限公司材一结构演示销售评估队伍银行"),
                        text[:60])
        self.assertIn("本行环客户设备公司金融业务、个人金融业务、资金资管业务效率工具金融服务", text)
        self.assertNotIn("主要会计数据", text)

    def test_bank_b_full_running_header_chapter(self):
        text, note = pages_section("609202_2025_full", "full")
        self.assertEqual(note, "ok:end=running_header;family=business_review")
        self.assertTrue(text.startswith("业务回顾\n本集团评主要业务样本例公司金融业务"), text[:60])
        self.assertIn("数字人民币", text)
        lines = text.split("\n")
        self.assertNotIn("管理层讨论与分析", lines)                          # running headers dropped
        self.assertEqual(lines.count("业务回顾"), 1)
        self.assertNotIn("风险管理架构", text)                               # next chapter
        self.assertNotIn("现金流量表分析", text)                             # previous chapter

    def test_telecom_summary_business_overview(self):
        text, note = pages_section("609205_2025_summary", "summary")
        self.assertEqual(note, "ok:end=financial_review;family=business_review")
        self.assertTrue(text.startswith("业务概览\n2025接，公司队伍批次通信服务、算力服务、智能服务"), text[:60])
        self.assertIn("算力服务", text)
        self.assertNotIn("财务概览", text)

    def test_energy_full_business_review(self):
        text, note = pages_section("609206_2025_full", "full")
        self.assertEqual(note, "ok:end=mda;family=business_review")
        self.assertTrue(text.startswith("业务回顾\n1、市场回顾\n（1）原油市场"), text[:60])
        self.assertIn("（4）天然气销售业务", text)
        self.assertNotIn("经营情况讨论与分析", text)
        self.assertNotIn("董事长", text)

    def test_bank_c_summary_fails_full_business_review(self):
        self.assertEqual(pages_section("609203_2025_summary", "summary"), (None, "start_heading_not_found"))
        text, note = pages_section("609203_2025_full", "full")
        self.assertEqual(note, "ok:end=risk_management;family=business_review")
        self.assertTrue(text.startswith("业务回顾\n专题一：网络机制“五材料规章”"), text[:60])

    def test_insurer_and_bank_d_summaries(self):
        text, note = pages_section("609207_2025_summary", "summary")
        self.assertEqual(note, "ok:end=next_item;family=main_business")
        self.assertTrue(text.startswith("二、报告期主要业务\n流程例点路径结构区域区域综合金融、规模接口服务集团"))
        self.assertNotIn("主要财务数据和股东情况", text)
        text, note = pages_section("609204_2025_summary", "summary")
        self.assertEqual(note, "ok:end=next_item;family=main_business")
        self.assertTrue(text.startswith("2.2主要业务简介\n本行计划能力研发网络数4624演示路径效率队伍银行"))
        self.assertNotIn("财务概要", text)

    def test_existing_extractions_byte_identical(self):
        """sha256 of the extractions of the 309386 fixtures (and the other-kind fallbacks); pinned on the synthetic PDFs."""
        want = {("summary", "summary"): ("ok:end=accounting_data",
                                         "349956110154c936"),
                ("full", "full"): ("ok:end=core_competence", "1c7bce379a788e78"),
                ("full", "summary"): ("ok:end=core_competence;rules=full", "1c7bce379a788e78"),
                ("summary", "full"): ("ok:end=accounting_data;rules=summary", "349956110154c936")}
        for (pdf, kind), (note, digest) in want.items():
            text, got, _ = cninfo.extract_from_pdf(SUMMARY_PDF if pdf == "summary" else FULL_PDF, kind)
            self.assertEqual((got, store.sha256(text.encode())[:16]), (note, digest), (pdf, kind))

    def test_clean_pages_offsets_do_not_change_lines(self):
        pages, _ = cninfo.pdf_pages(FULL_PDF)
        offsets: list[int] = []
        self.assertEqual(cninfo.clean_pages(pages, offsets=offsets), cninfo.clean_pages(pages))
        self.assertEqual(len(offsets), len(pages) + 1)
        self.assertEqual(offsets[0], 0)
        self.assertEqual(offsets, sorted(offsets))


class TestFamilyRules(unittest.TestCase):
    FILL = "本行向公司和个人客户提供存款、贷款、支付结算、财富管理等金融服务，并在境外设有分支机构。"

    def test_numbered_family_end_ignores_sentence_fragments(self):
        ls = ["8.讨论与分析", "8.3业务综述"] + [self.FILL] * 3 + ["49个国家和地区建立了410家境外机构"] + \
            [self.FILL] * 3 + ["8.4风险管理", self.FILL]
        text, note, _ = cninfo.extract_family_section(ls, "full")
        self.assertEqual(note, "ok:end=next_item;family=business_review")
        self.assertIn("49个国家和地区", text)
        self.assertNotIn("风险管理", text)

    def test_toc_span_is_not_a_section(self):
        toc = ["业务回顾", "44", "公司金融业务", "52", "个人金融业务", "60", "风险管理", "92"]
        body = ["业务回顾"] + [self.FILL] * 6 + ["风险管理", self.FILL]
        text, note, _ = cninfo.extract_family_section(toc + body, "full")
        self.assertEqual(note, "ok:end=risk_management;family=business_review")
        self.assertTrue(text.startswith("业务回顾\n本行向公司"))

    def test_too_long_family_section_is_clipped(self):
        ls = ["业务回顾"] + [self.FILL] * 20 + ["风险管理", self.FILL]
        with mock.patch.object(cninfo, "MAX_SECTION_CHARS", 400):
            text, note = cninfo.extract_section(ls, "full")
        self.assertTrue(note.startswith("ok:end=risk_management;family=business_review;clipped:"), note)
        self.assertLessEqual(len(text), 400)
        self.assertTrue(text.endswith("。"))

    def test_mda_opening_is_bounded(self):
        ls = ["第三节 管理层讨论与分析", "一、经营情况讨论与分析"] + [self.FILL] * 300 + ["第四节 公司治理"]
        text, note = cninfo.extract_section(ls, "full")
        self.assertEqual(note, "ok:end=opening_bound;fallback:mda_opening")
        self.assertTrue(text.startswith("第三节 管理层讨论与分析"))
        self.assertLessEqual(len(text), cninfo.MDA_OPENING_CHARS)
        self.assertEqual(cninfo.extract_section(["一、公司信息", self.FILL], "full"),
                         (None, "start_heading_not_found"))


def query_609201(summary_title: str = "甲示银行2025年度报告摘要", extra: list | None = None) -> bytes:
    rows = [ann("甲示银行2025年度报告", "finalpage/2026-03-28/1225047240.PDF", T_2026_04_01, size=9072),
            ann(summary_title, "finalpage/2026-03-28/1225046871.PDF", T_2026_04_01, size=398),
            ann("甲示银行H股公告-2022年度报告", "finalpage/2023-04-27/1216613647.PDF", T_2026_04_01 - 1070 * T_DAY),
            ann("2019年年度报告摘要", "finalpage/2020-03-28/1207419812.PDF", T_2026_04_01 - 2200 * T_DAY)]
    return json.dumps({"announcements": (extra or []) + rows}, ensure_ascii=False).encode()


URL_609201_SUMMARY = "http://static.cninfo.com.cn/finalpage/2026-03-28/1225046871.PDF"
URL_609201_FULL = "http://static.cninfo.com.cn/finalpage/2026-03-28/1225047240.PDF"


def fake_extract(data: bytes, kind: str, *, backend: str = "auto"):
    """extract_from_pdf stand-in: the fake 'PDF' bytes ('%PDF-' + name) name a page-text fixture."""
    name = data.decode().removeprefix("%PDF-")
    if name == "broken":
        return None, "start_heading_not_found", "pymupdf"
    return cninfo.extract_from_pages(fixture_pages(name), kind) + ("pymupdf",)


class TestSyncLiveFormats(SyncBase):
    def setUp(self) -> None:
        super().setUp()
        self.add("SSE:609201", 3e11, "CNE000001P37")

    def run_bank_a(self, gets: dict, query: bytes | None = None):
        client = standard_client(gets=gets, posts={"609201": query or query_609201()})
        with mock.patch.object(cninfo, "extract_from_pdf", side_effect=fake_extract):
            summary, _ = self.run_sync(client, codes=["609201"])
        return summary, client

    def test_summary_fallback_then_full_business_section(self):
        summary, client = self.run_bank_a({URL_609201_SUMMARY: b"%PDF-609201_2025_summary",
                                         URL_609201_FULL: b"%PDF-609201_2025_full"})
        self.assertEqual(client.calls, [cninfo.STOCK_LIST_URL, "POST 609201", URL_609201_SUMMARY, URL_609201_FULL])
        self.assertEqual((summary["ok_by_form"], summary["fallback_full"]), ({"annual_report": 1}, 1))
        rows = self.q("SELECT form, text_path IS NOT NULL, extract_note, extractor FROM documents ORDER BY form")
        self.assertEqual([r[:2] for r in rows], [("annual_report", True), ("annual_report_summary", True)])
        self.assertEqual(rows[0][2], "ok:end=next_item;family=business_review;summary_fallback")
        self.assertEqual(rows[1][2], "ok:end=next_item;fallback:mda_opening")
        self.assertTrue(rows[0][3].startswith(cninfo.EXTRACTOR_VERSION + "/"))
        st = self.states()["SSE:609201"]
        self.assertEqual((st[0], st[3]), ("ok", "ok:end=next_item;family=business_review;summary_fallback"))
        desc = self.q("SELECT text, source_url FROM descriptions WHERE security_id = 'SSE:609201'")[0]
        self.assertEqual(desc[1], URL_609201_FULL)
        self.assertTrue(desc[0].startswith("专栏："), desc[0][:40])
        # rerun: the summary is stored with text -> nothing is downloaded again
        summary, client = self.run_bank_a({})
        self.assertEqual((client.calls[-1], summary["skipped_unchanged"]), ("POST 609201", 1))

    def test_summary_fallback_kept_when_full_fails(self):
        summary, client = self.run_bank_a({URL_609201_SUMMARY: b"%PDF-609201_2025_summary", URL_609201_FULL: b"%PDF-broken"})
        self.assertEqual(summary["ok_by_form"], {"annual_report_summary": 1})
        st = self.states()["SSE:609201"]
        self.assertEqual(st[0], "ok")
        self.assertEqual(st[3], "ok:end=next_item;fallback:mda_opening;full_failed:start_heading_not_found")
        desc = self.q("SELECT source_url FROM descriptions WHERE security_id = 'SSE:609201'")[0]
        self.assertEqual(desc[0], URL_609201_SUMMARY)
        summary, _ = self.run_bank_a({URL_609201_SUMMARY: b"%PDF-609201_2025_summary", URL_609201_FULL: 404},
                                   query=query_609201())
        self.assertEqual(summary["skipped_unchanged"], 1)            # summary text already stored

    def test_summary_fallback_kept_when_full_download_fails(self):
        self.run_bank_a({URL_609201_SUMMARY: b"%PDF-609201_2025_summary", URL_609201_FULL: 404})
        st = self.states()["SSE:609201"]
        self.assertEqual((st[0], st[3]), ("ok", "ok:end=next_item;fallback:mda_opening;full_http_404"))

    def test_summary_fallback_kept_when_full_download_times_out(self):
        timeout = RequestTimeout(URL_609201_FULL, cninfo.PDF_DEADLINE_S, 300.2, 4096, 200)
        summary, client = self.run_bank_a({URL_609201_SUMMARY: b"%PDF-609201_2025_summary", URL_609201_FULL: timeout})
        self.assertEqual(client.kwargs[-1].get("deadline_s"), cninfo.PDF_DEADLINE_S)   # explicit PDF deadline
        st = self.states()["SSE:609201"]
        self.assertEqual((st[0], st[3]), ("ok", "ok:end=next_item;fallback:mda_opening;full_network:RequestTimeout"))
        self.assertEqual(summary["ok_by_form"], {"annual_report_summary": 1})
        rows = self.q("SELECT form, text_path FROM documents")
        self.assertEqual([r[0] for r in rows], ["annual_report_summary"])
        self.assertTrue(Path(rows[0][1]).is_file())                     # the text file is not orphaned
        desc = self.q("SELECT source_url FROM descriptions WHERE security_id = 'SSE:609201'")[0]
        self.assertEqual(desc[0], URL_609201_SUMMARY)

    def test_full_download_network_error_without_kept_summary_records_rows(self):
        summary, _ = self.run_bank_a({URL_609201_SUMMARY: b"%PDF-broken", URL_609201_FULL: ConnectionResetError("reset")})
        st = self.states()["SSE:609201"]
        self.assertEqual(st[0], "error")
        self.assertTrue(st[3].startswith("network: ConnectionResetError"), st[3])
        rows = self.q("SELECT form, text_path, extract_note FROM documents")
        self.assertEqual([(r[0], r[1]) for r in rows], [("annual_report_summary", None)])   # failed summary kept

    def test_kind_full_with_summary_only_is_counted_separately(self):
        query = json.dumps({"announcements": [ann("甲示银行2025年度报告摘要", "finalpage/2026-03-28/1225046871.PDF",
                                                  T_2026_04_01, size=398)]}, ensure_ascii=False).encode()
        client = standard_client(gets={URL_609201_SUMMARY: b"%PDF-609201_2025_summary"}, posts={"609201": query})
        with mock.patch.object(cninfo, "extract_from_pdf", side_effect=fake_extract):
            summary, _ = self.run_sync(client, codes=["609201"], kind="full")
        self.assertEqual((summary["summary_only"], summary["fallback_full"]), (1, 0))
        self.assertEqual(summary["ok_by_form"], {"annual_report_summary": 1})
        self.assertIn(";summary_only", self.states()["SSE:609201"][3])

    def test_newest_year_unparsed_records_note_and_downloads_nothing(self):
        extra = [ann("甲示银行2026年度报告全文及摘要", "finalpage/2027-03-28/1.PDF", T_2026_04_01 + 365 * T_DAY)]
        summary, client = self.run_bank_a({}, query=query_609201(extra=extra))
        self.assertEqual(client.calls, [cninfo.STOCK_LIST_URL, "POST 609201"])
        st = self.states()["SSE:609201"]
        self.assertEqual((st[0], st[3]), ("no_annual_report", "newest_year_unparsed:2026"))
        self.assertEqual(summary["failures_by_note"], {"newest_year_unparsed": 1})

    def test_extractor_version_bumped(self):
        self.assertNotEqual(cninfo.EXTRACTOR_VERSION, "cninfo-v2")      # v2 failures (banks) are retried


# =========================================================================== real http.Client, fake opener


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status, self._body, self.headers = status, body, headers or {"Content-Type": "application/json"}

    def read(self, n: int | None = None) -> bytes:
        return self._body if n is None else self._body[:n]


class FakeOpener:
    def __init__(self, status: int = 200, body: bytes = QUERY_RAW):
        self.status, self.body, self.requests = status, body, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if self.status >= 400:
            raise urllib.error.HTTPError(req.full_url, self.status, "err", {}, io.BytesIO(b""))
        return _FakeHTTPResponse(self.status, self.body)


class TestPostFormWithRealClient(unittest.TestCase):
    def test_form_body_headers_and_limiter(self):
        client = Client(user_agent="jev-screen-test", min_interval_s=0.0)
        client._opener = FakeOpener()
        resp = cninfo.post_form(client, cninfo.QUERY_URL, cninfo.query_form("309386", ORG_309386),
                                headers=cninfo.QUERY_HEADERS, rate_key=cninfo.RATE_KEY)
        self.assertEqual(resp.json()["announcements"][0]["announcementTitle"], "2025年年度报告")
        req = client._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        body = dict(x.split("=", 1) for x in req.data.decode().split("&"))
        self.assertEqual(body["stock"], f"309386%2C{ORG_309386}")
        self.assertEqual(body["category"], "category_ndbg_szsh%3B")
        self.assertEqual(req.get_header("X-requested-with"), "XMLHttpRequest")
        self.assertTrue(req.get_header("Content-type").startswith("application/x-www-form-urlencoded"))
        self.assertEqual(client.requests_made, 1)
        self.assertIn(cninfo.RATE_KEY, client._last)

    def test_form_post_uses_request_with_query_deadline(self):
        client = Client(user_agent="jev-screen-test", min_interval_s=0.0)
        client._opener = FakeOpener()
        with mock.patch.object(Client, "request", autospec=True, side_effect=Client.request) as req:
            cninfo.post_form(client, cninfo.QUERY_URL, {"a": "b"}, headers=cninfo.QUERY_HEADERS,
                             rate_key=cninfo.RATE_KEY)
        kw = req.call_args.kwargs
        self.assertEqual((kw["deadline_s"], kw["data"], kw["rate_key"]), (cninfo.QUERY_DEADLINE_S, b"a=b", "cninfo"))
        self.assertTrue(kw["headers"]["Content-Type"].startswith("application/x-www-form-urlencoded"))

    def test_429_raises_blocked_and_halts(self):
        client = Client(user_agent="jev-screen-test", min_interval_s=0.0)
        client._opener = FakeOpener(status=429)
        with self.assertRaises(Blocked):
            cninfo.post_form(client, cninfo.QUERY_URL, {"a": "b"}, rate_key=cninfo.RATE_KEY)
        self.assertTrue(client.halt.is_set())

    def test_sync_raises_client_interval(self):
        client = standard_client()
        client.min_interval_s = 0.1
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                cninfo.sync(Config(home=Path(tmp)), client)
        self.assertEqual(client.min_interval_s, cninfo.DEFAULT_MIN_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
