"""Tests for sources.mops. No network: every client is a fake.

All fixtures are SYNTHETIC and built inline (fictional companies 9901 / 9902 / 9903 / 9904 / 9905, made-up file
names and texts), shaped after the pages measured live on 2026-09-27 (doc.twse.com.tw t57sb01 listing / step-9
pages in Big5, the mops.twse.com.tw t05st03 JSON envelope). No real MOPS / TWSE page or report is stored here.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import guard, provenance  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Response  # noqa: E402
from jevscreen.sources import edinet, mops  # noqa: E402
from test_edinet import SyncBase  # noqa: E402

AS_OF = dt.date(2026, 9, 27)          # ROC 115
AR_DESC = "股東會年報(尚未適用永續揭露準則)"


def listing_html(code: str, rows: list[tuple[str, str, str, str, str]], name: str = "測試公司") -> bytes:
    """A hand-made t57sb01 step=1 page: rows of (資料年度, 資料細節說明, file name, size, 上傳日期)."""
    trs = "".join(
        f"<tr> <td align='center'>{code}</td><td align='center'>{year} 年</td> <td align='center'>股東會相關資料</td> "
        f"<td align='center'>&nbsp;</td> <td align='center'>常會</td> <td align='center'>{desc}</td>"
        f"<td align='center'>&nbsp;</td><td><a href='javascript:readfile2(\"F\",\"{code}\",\"{fn}\");'>{fn}</a></td>"
        f"<td align='right'> {size}</td> <td align='cetern'>{up}</td> <td align='cetern'>無</td> </tr>"
        for year, desc, fn, size, up in rows)
    page = ("<html><head><title>電子資料查詢作業</title><meta http-equiv=\"Content-Type\" content=\"text/html;"
            "charset=big5\"></head><body><center><h2><font color=\"blue\">電子資料查詢作業</font></h2>"
            f"<form action='/server-java/t57sb01' method='post' name='fm2'> 公司名稱：{name}<br>"
            "<table border='5'><tr><th>證券代號</th><th>資料年度</th><th>資料類型</th><th>結案類型</th><th>股東會性質</th>"
            "<th>資料細節說明</th><th>備註</th><th>電子檔案</th><th>檔案大小</th><th>上傳日期</th><th>更(補)正</th></tr>"
            f"{trs}</table></form></body></html>")
    return page.encode("big5")


NO_DATA = ("<html><head><title>電子資料查詢作業</title><meta http-equiv=\"Content-Type\" content=\"text/html;charset=big5\">"
           "</head><body><center><h2>電子資料查詢作業</h2><br><h4 align='center'><font color='red'>查無所需資料</font>"
           "</h4></body></html>").encode("big5")


def file_page(path: str) -> bytes:
    return ("<html><head><title>電子資料查詢作業</title></head><body><center><h2>電子資料查詢作業</h2>"
            f"電子檔案：<a href='{path}'>{path.rsplit('/', 1)[-1]}</a><p>請點選連結直接開啟或按右鍵另存新檔"
            "</body></html>").encode("big5")


def basic_json(main: str | None, code: int = 200, hidden: bool = False) -> bytes:
    result = {"companyName": {"isHidden": False, "value": "測試電子股份有限公司"},
              "industryCategory": {"isHidden": False, "value": "半導體業"},
              "companyEnglishAbbreviation": {"isHidden": False, "value": "TEST"}}
    if main is not None:
        result["mainBusiness"] = {"isHidden": hidden, "value": main}
    return json.dumps({"code": code, "message": "查詢成功", "result": result}, ensure_ascii=False).encode("utf-8")


FAKE_PDF = b"%PDF-1.7\n% synthetic stand-in; pdf_to_section is patched in these tests\n"
SECTION = ("(一)業務範圍\n1.所營業務之主要內容：本公司從事測試探針與精密量測設備之研發、製造與銷售。" * 4 +
           "\n2.營業比重：探針 70%，設備 30%。\n(二)產業概況\n半導體測試需求隨先進封裝持續成長。")


def list_url(code: str, year: int) -> str:
    return mops.LIST_URL.format(code=code, year=year)


class FakeClient:
    """Stands in for http.Client (get + request). GET pages keyed by URL; POSTs keyed by 'POST <url>#<filename or
    companyId>'. Values: bytes (200) | int status | (status, body, headers) | Exception | callable(url)."""

    def __init__(self, pages: dict[str, Any] | None = None, default: Any = 404) -> None:
        self.pages, self.default = dict(pages or {}), default
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.min_interval_s, self.requests_made = 0.0, 0

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def request(self, method: str, url: str, *, data: bytes | None = None, headers=None, **kw) -> Response:
        key = url
        if method == "POST":
            body = (data or b"").decode("utf-8")
            if body.startswith("{"):
                key = f"POST {url}#{json.loads(body)['companyId']}"
            else:
                key = f"POST {url}#{parse_qs(body)['filename'][0]}"
        self.calls.append(key)
        self.kwargs.append(dict(kw, headers=headers))
        self.requests_made += 1
        item = self.pages.get(key, self.default)
        if callable(item) and not isinstance(item, type):
            return item(url)
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        if isinstance(item, int):
            return Response(url, item, b"", {}, 0.0)
        if isinstance(item, tuple):
            return Response(url, item[0], item[1], item[2], 0.0)
        return Response(url, 200, item, {}, 0.0)


def company_pages(code: str, *, year: int = 115, fy: int = 114, meeting: str = "20260617",
                  up: str = "115/05/29 09:02:26") -> dict[str, Any]:
    ar = f"{fy + 1911}_{code}_{meeting}F04.pdf"
    tmp = f"/pdf/{ar[:-4]}_20260927_010203.pdf"
    return {
        list_url(code, year): listing_html(code, [
            (str(year), "開會通知", f"{year + 1911}_{code}_{meeting}F01.pdf", "537,700", "115/05/15 10:16:53"),
            (str(fy), AR_DESC, ar, "2,398,290", up),
            (str(fy), "英文版-" + AR_DESC, f"{fy + 1911}_{code}_{meeting}FE4.pdf", "2,291,013", up),
            (str(year), "年報前十大股東相互間關係表", f"{year + 1911}_{code}_{meeting}F17.pdf", "85,142", up)]),
        f"POST {mops.FILE_URL}#{ar}": file_page(tmp),
        mops.DOC_ORIGIN + tmp: FAKE_PDF,
    }


# =========================================================================== pure functions


class TestMapping(unittest.TestCase):
    def test_codes(self):
        secs = [{"security_id": s} for s in ("TWSE:9901", "TPEX:9902", "TWSE:01001T", "TWSE:0050", "TPEX:990201",
                                             "TSE:7203", "TWSE:9901")]
        rep: dict = {}
        got = mops.map_securities_to_mops(secs, report=rep)
        self.assertEqual(got, [("TWSE:9901", "9901", "symbol"), ("TPEX:9902", "9902", "symbol")])
        self.assertEqual((rep["considered"], rep["non_tw"], rep["unmatched"]), (5, 1, 3))
        self.assertEqual(rep["non_company_code"], ["TWSE:01001T", "TWSE:0050", "TPEX:990201"])


class TestListing(unittest.TestCase):
    def test_rows_and_pick(self):
        text, _ = edinet.decode_bytes(company_pages("9901")[list_url("9901", 115)], prefer=("cp950", "big5"))
        rows, state = mops.parse_listing(text)
        self.assertEqual((state, len(rows)), ("rows", 4))
        self.assertEqual(rows[1]["data_year"], 114)
        self.assertEqual(rows[1]["size"], 2398290)
        self.assertEqual(rows[1]["uploaded"], dt.datetime(2026, 5, 29, 9, 2, 26))
        best = mops.pick_annual_report(rows, "9901")
        self.assertEqual(best["filename"], "2025_9901_20260617F04.pdf")     # not FE4 (English), not F17 ('年報前十大')
        self.assertEqual(best["meeting_date"], dt.date(2026, 6, 17))
        self.assertIsNone(mops.pick_annual_report(rows, "9999"))             # another company's file never matches

    def test_newest_fiscal_year_then_upload_wins(self):
        rows = [{"filename": "2024_9901_20250603F04.pdf", "desc": "股東會年報", "data_year": 113,
                 "uploaded": dt.datetime(2025, 5, 1)},
                {"filename": "2025_9901_20260604F04.pdf", "desc": "股東會年報", "data_year": 114,
                 "uploaded": dt.datetime(2026, 5, 1)},
                {"filename": "2025_9901_20260605F04.pdf", "desc": "股東會年報(適用永續揭露準則)", "data_year": 114,
                 "uploaded": dt.datetime(2026, 5, 20)}]
        self.assertEqual(mops.pick_annual_report(rows)["filename"], "2025_9901_20260605F04.pdf")

    def test_states(self):
        self.assertEqual(mops.parse_listing(NO_DATA.decode("big5")), ([], "no_data"))
        self.assertEqual(mops.parse_listing("<html><title>Service Unavailable</title></html>"), ([], "unverified"))

    def test_roc_datetime_and_file_page(self):
        self.assertEqual(mops._roc_datetime("115/05/21 19:02:58"), dt.datetime(2026, 5, 21, 19, 2, 58))
        self.assertEqual(mops._roc_datetime("115/05/21"), dt.datetime(2026, 5, 21))
        self.assertIsNone(mops._roc_datetime("115/13/01"))
        self.assertIsNone(mops._roc_datetime(""))
        self.assertEqual(mops.parse_file_page(file_page("/pdf/2025_9901_20260617F04_20260927_010203.pdf").decode("big5")),
                         "/pdf/2025_9901_20260617F04_20260927_010203.pdf")
        self.assertIsNone(mops.parse_file_page("<html>請稍候......</html>"))

    def test_refusal_marker(self):
        self.assertEqual(mops.refusal_marker("<html>FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED!</html>"),
                         "FOR SECURITY REASONS")
        self.assertEqual(mops.refusal_marker("<p>查詢過於頻繁，請稍後再試</p>"), "查詢過於頻繁")
        self.assertIsNone(mops.refusal_marker(NO_DATA.decode("big5")))
        # an F5 BIG-IP ASM rejection page (the WAF in front of TWSE hosts) is a refusal too
        self.assertEqual(mops.refusal_marker(WAF_PAGE.decode("ascii")), "The requested URL was rejected")
        self.assertEqual(mops.refusal_marker("<html><title>Request Rejected</title></html>"), "<title>Request Rejected")


# hand-made stand-in for an F5 BIG-IP ASM rejection page: HTTP 200, no MOPS content
WAF_PAGE = (b"<html><head><title>Request Rejected</title></head><body>The requested URL was rejected. Please consult "
            b"with your administrator.<br><br>Your support ID is: 1234567890123456789</body></html>")


BODY = ("本公司主要從事測試探針卡之研發、設計、製造及銷售，產品應用於晶圓測試與先進封裝，客戶涵蓋晶圓代工與封裝測試廠。"
        "隨著人工智慧與高效能運算需求成長，公司持續投入高頻與高針數探針卡之開發，並拓展光電與射頻測試之新應用領域。")
B2 = BODY + "\n" + BODY      # two body lines re-flow into two paragraphs


class TestExtract(unittest.TestCase):
    def test_standard_layout(self):
        lines = ["目錄", "肆、營運概況", "一、業務內容", "二、市場及產銷概況 ........ 52",   # TOC rows
                 "肆、營運概況", "一、業務內容", "(一)業務範圍", "1.", "所營業務之主要內容：", BODY, BODY,
                 "(二)產業概況", BODY, "(四)長、短期業務發展計畫", BODY,
                 "二、市場及產銷概況", "(一)市場分析", "主要銷售地區為亞洲。"]
        text, note = mops.extract_business_section(lines)
        self.assertTrue(text.startswith("(一)業務範圍\n1.所營業務之主要內容："), text[:40])  # bare '1.' joined
        self.assertIn("(四)長、短期業務發展計畫", text)
        self.assertNotIn("市場分析", text)
        self.assertEqual(note, "start:一、業務內容;end:market")

    def test_dotted_layout_ends_at_next_item_not_at_a_percentage(self):
        lines = ["5.1 業務內容 ‑ 98", "5.2 技術領導地位 ‑ 100",                     # TOC rows: never a start
                 "5.1業務內容", "5.1.1業務範圍", BODY, "5.7%", "5.1.2 產業概況", BODY,
                 "5.2 技術領導地位", "研發內容。" * 80]
        text, note = mops.extract_business_section(lines)
        self.assertIn("5.1.2 產業概況", text)
        self.assertIn("5.7%", text)
        self.assertNotIn("技術領導地位", text)
        self.assertEqual(note, "start:5.1業務內容;end:next_item")

    def test_toc_candidate_stays_short_and_body_wins(self):
        lines = ["一、業務內容", "(一)業務範圍 45", "(二)產業概況 47", "(三)技術及研發概況 49", "(四)長、短期業務發展計畫 50",
                 "二、市場及產銷概況 51", "一、業務內容", BODY, BODY, "二、市場及產銷概況"]
        text, note = mops.extract_business_section(lines)
        self.assertEqual(text, B2)
        self.assertEqual(note, "start:一、業務內容;end:market")

    def test_next_cn_item_and_next_chapter_end(self):
        text, note = mops.extract_business_section(["一、業務內容", BODY, BODY, "二、市場概況與競爭", "x" * 300])
        self.assertEqual((text, note), (B2, "start:一、業務內容;end:next_item"))
        text, note = mops.extract_business_section(["(一)業務內容", BODY, BODY, "伍、財務概況", "x" * 300])
        self.assertEqual((text, note), (B2, "start:(一)業務內容;end:next_chapter"))

    def test_fallback_chapter_not_found_too_short(self):
        text, note = mops.extract_business_section(["伍、營運概況", BODY, BODY, "陸、財務概況", "x" * 300])
        self.assertEqual((text, note), (B2, "fallback:chapter"))
        self.assertEqual(mops.extract_business_section(["壹、致股東報告書", BODY]), (None, "business_section_not_found"))
        text, note = mops.extract_business_section(["一、業務內容", "短。", "二、市場及產銷概況"])
        self.assertIsNone(text)
        self.assertTrue(note.startswith("section_too_short:"), note)

    def test_integer_share_table_is_not_a_toc(self):
        # a 業務內容 section opening with an integer revenue-share table (short CJK rows ending in a number)
        lines = ["肆、營運概況", "一、業務內容", "(一)業務範圍", "1.營業比重", "產品別 營業比重", "晶圓 85", "光罩 5",
                 "其他 10", "合計 100", "2.主要產品", BODY, "(二)產業概況", BODY, "二、市場及產銷概況", "(一)市場分析"]
        text, note = mops.extract_business_section(lines)
        self.assertIsNotNone(text, note)
        self.assertEqual(note, "start:一、業務內容;end:market")
        self.assertIn("晶圓 85", text)
        self.assertNotIn("市場分析", text)
        # the same table right under the chapter heading does not hide the 營運概況 fallback either
        lines = ["伍、營運概況", "產品別 營業比重", "晶圓 85", "光罩 5", "其他 10", "合計 100", BODY, BODY, "陸、財務概況"]
        text, note = mops.extract_business_section(lines)
        self.assertEqual(note, "fallback:chapter")
        self.assertIn("光罩 5", text)
        # a ROC-year header row and year columns ('年度 111 112') are not page numbers either
        lines = ["一、業務內容", "年度 111", "年度 112", "年度 113", "年度 114", BODY, BODY, "二、市場及產銷概況"]
        self.assertEqual(mops.extract_business_section(lines)[1], "start:一、業務內容;end:market")

    def test_toc_rows_still_detected(self):
        self.assertTrue(mops._toc_like(["(一)業務範圍 45", "(二)產業概況 47", "(三)技術及研發概況 49", "(四)長、短期業務發展計畫 50"]))
        self.assertTrue(mops._toc_like(["業務範圍 ........ 45", "產業概況 ........ 47", "技術及研發概況 .... 49",
                                        "長短期業務發展計畫 … … 50"]))
        self.assertTrue(mops._toc_like(["5.1.1 業務範圍 ‑ 98", "5.1.2 產業概況 ‑ 99", "5.1.3 研發 ‑ 101", "5.2 技術 ‑ 104"]))
        self.assertFalse(mops._toc_like(["晶圓 85", "光罩 5", "其他 10", "合計 100"]))           # no numbering, no leaders
        self.assertFalse(mops._toc_like(["(一)晶圓 85", "(二)光罩 5", "(三)其他 10", "(四)合計 100"]))  # not page order

    def test_toc_only_note_says_so(self):
        # a long TOC block whose body heading is missing: the TOC candidate is skipped and the note says so
        toc = ["一、業務內容"] + [f"({c})業務項目第{c}類說明 ........ {50 + i}" for i, c in enumerate("一二三四五六七八")]
        text, note = mops.extract_business_section(toc + ["壹、致股東報告書", BODY])
        self.assertIsNone(text)
        self.assertIn("toc_skipped:1", note)

    def test_control_characters_dropped(self):
        text, _ = mops.extract_business_section(["一、業務內容", "●\x01" + BODY, BODY, "二、市場及產銷概況"])
        self.assertTrue(text.startswith("●本公司"))

    @unittest.skipUnless(__import__("importlib").util.find_spec("fitz"), "PyMuPDF not installed")
    def test_pdf_round_trip(self):
        import fitz
        doc = fitz.open()
        for chunk in (["肆、營運概況", "一、業務內容", "(一)業務範圍"] + [BODY[i:i + 30] for i in range(0, len(BODY), 30)] * 3,
                      ["二、市場及產銷概況", "主要銷售地區為亞洲。"]):
            page = doc.new_page()
            y = 72
            for ln in chunk:
                page.insert_text((72, y), ln, fontname="china-t", fontsize=10)
                y += 16
        data = doc.tobytes()
        text, note = mops.pdf_to_section(data)
        self.assertIsNotNone(text, note)
        self.assertIn("測試探針卡", text)
        self.assertNotIn("主要銷售地區", text)
        self.assertIn("backend:pymupdf", note)
        text, note = mops.pdf_to_section(_blank_pdf())
        self.assertIsNone(text)
        self.assertTrue(note.startswith("no_text_layer;pages:1"), note)


def _blank_pdf() -> bytes:
    import fitz
    doc = fitz.open()
    doc.new_page()
    return doc.tobytes()


class TestHelpers(unittest.TestCase):
    def test_is_newest_possible(self):
        today = dt.date(2026, 9, 27)
        self.assertTrue(mops.is_newest_possible(("2025_9901_20260617F04.pdf", mops.EXTRACTOR_VERSION), today))
        self.assertFalse(mops.is_newest_possible(("2024_9901_20250617F04.pdf", mops.EXTRACTOR_VERSION), today))
        self.assertFalse(mops.is_newest_possible(("2025_9901_20260617F04.pdf", "mops-ar-v1"), today))  # old
        self.assertFalse(mops.is_newest_possible(("2025_9901_20260617F04.pdf", mops.EXTRACTOR_VERSION + "/partial"),
                                                  today))
        self.assertFalse(mops.is_newest_possible(None, today))

    def test_description_skips_registration_items(self):
        text = ("(一)業務範圍\n1.本公司所營業務\n（1）C306010成衣業。\n（2）F401010國際貿易業。\n"
                "(1)多媒體積體電路\n本公司主要生產羽絨服、機能外套與各式針織成衣，客戶為國際運動及戶外品牌。")
        self.assertEqual(mops.description_from_section(text), "本公司主要生產羽絨服、機能外套與各式針織成衣，客戶為國際運動及戶外品牌。")
        self.assertEqual(mops.description_from_section(None), "")

    def test_radicals_normalised(self):
        text, _ = mops.extract_business_section(["⼀、業務內容", "(⼀)業務範圍", "產品應用於AI⼿機與⼯業設備，⻑期合作。" + BODY, BODY,
                                                 "⼆、市場及產銷概況"])
        self.assertTrue(text.startswith("(一)業務範圍\n產品應用於AI手機與工業設備，長期合作。"), text[:30])


class TestBasic(unittest.TestCase):
    def test_parse(self):
        info, note = mops.parse_basic(json.loads(basic_json("研發、製造及銷售測試探針卡。")))
        self.assertEqual((info["main_business"], info["industry"], note), ("研發、製造及銷售測試探針卡。", "半導體業", "basic"))
        self.assertEqual(mops.parse_basic(json.loads(basic_json("x", hidden=True))), (None, "basic_no_main_business"))
        self.assertEqual(mops.parse_basic(json.loads(basic_json(None, code=500)))[1], "basic_code_500")
        self.assertEqual(mops.parse_basic([])[1], "basic_bad_json")

    def test_fetch_basic(self):
        c = FakeClient({f"POST {mops.BASIC_API_URL}#9901": basic_json("研發測試探針卡。")})
        res = mops.fetch_basic(c, "9901")
        self.assertEqual((res["status"], res["info"]["main_business"]), ("ok", "研發測試探針卡。"))
        self.assertEqual(c.kwargs[0]["rate_key"], mops.API_RATE_KEY)
        c = FakeClient({f"POST {mops.BASIC_API_URL}#9901": b"<html>FOR SECURITY REASONS, THIS PAGE CAN NOT BE "
                                                                   b"ACCESSED!</html>"})
        with self.assertRaises(mops.MopsRefused):
            mops.fetch_basic(c, "9901")
        c = FakeClient({f"POST {mops.BASIC_API_URL}#9901": b"<html>maintenance</html>"})
        res = mops.fetch_basic(c, "9901")
        self.assertEqual((res["status"], res["refused"]), ("error", True))
        self.assertTrue(res["note"].startswith("basic_not_json;bytes:"))

    def test_fetch_basic_json_envelopes(self):
        key = f"POST {mops.BASIC_API_URL}#9901"
        # a throttle / refusal message inside a well-formed JSON envelope stops at once
        for body in (json.dumps({"code": 429, "message": "查詢過於頻繁，請稍後再試"}, ensure_ascii=False).encode("utf-8"),
                     json.dumps({"code": 403, "message": "FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED!"})
                     .encode("utf-8")):
            with self.assertRaises(mops.MopsRefused):
                mops.fetch_basic(FakeClient({key: body}), "9901")
        # any other code != 200: not what was asked for (counted toward REFUSAL_STOP by sync)
        res = mops.fetch_basic(FakeClient({key: basic_json(None, code=500)}), "9901")
        self.assertEqual((res["status"], res["refused"], res["note"]), ("error", True, "basic_code_500"))
        # code 200 without (or with a hidden) mainBusiness: a real answer, just no field
        for body in (basic_json(None), basic_json("x", hidden=True)):
            res = mops.fetch_basic(FakeClient({key: body}), "9901")
            self.assertEqual((res["status"], res["refused"], res["note"]),
                             ("no_basic", False, "basic_no_main_business"))
        # a refusal marker inside a company's own text is not a refusal
        res = mops.fetch_basic(FakeClient({key: basic_json("資訊安全服務：因為安全性考量而設計之防護系統。")}), "9901")
        self.assertEqual(res["status"], "ok")


# =========================================================================== single company


@mock.patch.object(mops, "pdf_to_section", return_value=(SECTION, "start:一、業務內容;end:market;pages:3;backend:x"))
class TestFetchAnnualReport(unittest.TestCase):
    def test_ok(self, _pdf):
        c = FakeClient(company_pages("9901"))
        seen: list[str] = []
        res = mops.fetch_annual_report(c, "9901", as_of=AS_OF, on_request=seen.append)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["requests"], 3)
        self.assertEqual(res["text"], SECTION)
        self.assertTrue(res["description"].startswith("1.所營業務之主要內容"))
        f = res["filing"]
        self.assertEqual((f["filename"], f["report_date"], f["filing_date"], f["listing_year"]),
                         ("2025_9901_20260617F04.pdf", dt.date(2025, 12, 31), dt.date(2026, 5, 29), 115))
        self.assertEqual(res["pdf_bytes"], len(FAKE_PDF))
        self.assertIn(";file:2025_9901_20260617F04.pdf", res["note"])
        self.assertEqual(seen[0], list_url("9901", 115))
        self.assertTrue(all(k["rate_key"] == mops.DOC_RATE_KEY for k in c.kwargs))
        self.assertEqual(c.kwargs[2]["deadline_s"], mops.PDF_DEADLINE_S)

    def test_previous_meeting_year(self, _pdf):
        pages = company_pages("9901", year=114, fy=113, meeting="20250603", up="114/05/15 15:50:24")
        pages[list_url("9901", 115)] = listing_html("9901", [("115", "開會通知", "2026_9901_20260604F01.pdf", "1", "115/05/04")])
        res = mops.fetch_annual_report(FakeClient(pages), "9901", as_of=AS_OF)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["filing"]["listing_year"], 114)
        self.assertIn("listing_year:114", res["note"])
        self.assertEqual(res["filing"]["report_date"], dt.date(2024, 12, 31))

    def test_no_annual_filing_and_skip(self, _pdf):
        c = FakeClient({list_url("9903", 115): NO_DATA, list_url("9903", 114): NO_DATA})
        res = mops.fetch_annual_report(c, "9903", as_of=AS_OF)
        self.assertEqual((res["status"], res["note"], res["requests"]), ("no_annual_filing", "no_annual_report:115,114", 2))
        c = FakeClient(company_pages("9901"))
        res = mops.fetch_annual_report(c, "9901", as_of=AS_OF, skip=lambda f: True)
        self.assertEqual((res["status"], res["requests"]), ("skipped", 1))

    def test_refusals(self, _pdf):
        c = FakeClient({list_url("9901", 115): b"<html><title>Busy</title>try later</html>"})
        res = mops.fetch_annual_report(c, "9901", as_of=AS_OF)
        self.assertEqual((res["status"], res["refused"]), ("error", True))
        self.assertIn("list_unverified;bytes:", res["note"])
        self.assertIn("title:Busy", res["note"])
        c = FakeClient({list_url("9901", 115): (302, b"", {"Location": "/error.html"})})
        res = mops.fetch_annual_report(c, "9901", as_of=AS_OF)
        self.assertEqual((res["note"], res["refused"]), ("list_http_302;location:/error.html", True))
        c = FakeClient({list_url("9901", 115): "因為安全性考量，您所執行的頁面無法呈現".encode("big5")})
        with self.assertRaises(mops.MopsRefused) as cm:
            mops.fetch_annual_report(c, "9901", as_of=AS_OF)
        self.assertEqual((cm.exception.reason, cm.exception.cooldown), ("access_refused", True))
        pages = company_pages("9901")
        pages[f"POST {mops.FILE_URL}#2025_9901_20260617F04.pdf"] = b"<html>no link</html>"
        res = mops.fetch_annual_report(FakeClient(pages), "9901", as_of=AS_OF)
        self.assertEqual((res["status"], res["refused"]), ("error", True))
        self.assertTrue(res["note"].startswith("file_link_missing"))
        pages = company_pages("9901")
        pages[mops.DOC_ORIGIN + "/pdf/2025_9901_20260617F04_20260927_010203.pdf"] = b"<html>error</html>"
        res = mops.fetch_annual_report(FakeClient(pages), "9901", as_of=AS_OF)
        self.assertEqual((res["status"], res["refused"]), ("error", True))
        self.assertTrue(res["note"].startswith("not_pdf"))

    def test_no_data_page_without_title_is_unverified(self, _pdf):
        untitled = "<html><body><center>查無所需資料</center></body></html>".encode("big5")
        res = mops.fetch_annual_report(FakeClient({list_url("9903", 115): untitled}), "9903", as_of=AS_OF)
        self.assertEqual((res["status"], res["refused"], res["requests"]), ("error", True, 1))
        self.assertTrue(res["note"].startswith("list_unverified;bytes:"), res["note"])
        self.assertNotIn("title:", res["note"])

    def test_waf_rejection_page_raises(self, _pdf):
        for where, pages in (("listing", {list_url("9901", 115): WAF_PAGE}),
                             ("file", {**company_pages("9901"),
                                       f"POST {mops.FILE_URL}#2025_9901_20260617F04.pdf": WAF_PAGE}),
                             ("pdf", {**company_pages("9901"),
                                      mops.DOC_ORIGIN + "/pdf/2025_9901_20260617F04_20260927_010203.pdf": WAF_PAGE})):
            with self.assertRaises(mops.MopsRefused, msg=where) as cm:
                mops.fetch_annual_report(FakeClient(pages), "9901", as_of=AS_OF)
            self.assertEqual((cm.exception.status, cm.exception.cooldown), (where, True))

    def test_blocked_propagates(self, _pdf):
        c = FakeClient({list_url("9901", 115): Blocked(list_url("9901", 115), 429, "http_429")})
        with self.assertRaises(Blocked):
            mops.fetch_annual_report(c, "9901", as_of=AS_OF)

    def test_extract_failed(self, pdf):
        pdf.return_value = (None, "no_text_layer;pages:80;backend:pymupdf")
        res = mops.fetch_annual_report(FakeClient(company_pages("9901")), "9901", as_of=AS_OF)
        self.assertEqual(res["status"], "extract_failed")
        self.assertTrue(res["note"].startswith("ar;no_text_layer"))


@mock.patch.object(mops, "pdf_to_section", return_value=(SECTION, "start:一、業務內容;end:market;pages:3;backend:x"))
class TestFetchCompany(unittest.TestCase):
    """fetch_company(): the on-demand path keeps the cooldown discipline and the 'mops' budget lock."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), min_interval_s=1.0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ok_without_database(self, _pdf):
        c = FakeClient(company_pages("9901"))
        res = mops.fetch_company(self.cfg, "9901", client=c, as_of=AS_OF)
        self.assertEqual((res["status"], res["text"], len(c.calls)), ("ok", SECTION, 3))
        self.assertEqual(c.min_interval_s, mops.DOC_MIN_INTERVAL_S)             # raised to the polite floor
        self.assertFalse((Path(self.cfg.home) / "jevscreen.duckdb").exists())   # nothing written
        self.assertIsNone(guard.recent_block_marker(self.cfg, mops.COMMAND))

    def test_refuses_within_cooldown(self, _pdf):
        guard.mark_blocked(self.cfg, mops.COMMAND, url=list_url("9902", 115), status=403, reason="http_403")
        c = FakeClient(company_pages("9901"))
        with self.assertRaises(mops.CooldownActive):
            mops.fetch_company(self.cfg, "9901", client=c, as_of=AS_OF)
        self.assertEqual(c.calls, [])                                             # no request at all
        res = mops.fetch_company(self.cfg, "9901", client=c, as_of=AS_OF, after_block=True)
        self.assertEqual(res["status"], "ok")

    def test_block_writes_the_cooldown_marker(self, _pdf):
        c = FakeClient({list_url("9901", 115): Blocked(list_url("9901", 115), 403, "http_403")})
        with self.assertRaises(Blocked):
            mops.fetch_company(self.cfg, "9901", client=c, as_of=AS_OF)
        hit = guard.recent_block_marker(self.cfg, mops.COMMAND)
        self.assertIsNotNone(hit)
        self.assertIn(list_url("9901", 115), hit["note"])
        self.assertIn("http_403", hit["note"])

    def test_refusal_page_writes_the_cooldown_marker(self, _pdf):
        pages = company_pages("9901")
        pages[f"POST {mops.FILE_URL}#2025_9901_20260617F04.pdf"] = WAF_PAGE
        with self.assertRaises(mops.MopsRefused):
            mops.fetch_company(self.cfg, "9901", client=FakeClient(pages), as_of=AS_OF)
        hit = guard.recent_block_marker(self.cfg, mops.COMMAND)
        self.assertIsNotNone(hit)
        self.assertIn("access_refused", hit["note"])
        self.assertIn(mops.FILE_URL, hit["note"])

    def test_busy_while_another_process_holds_the_budget(self, _pdf):
        path = Path(self.cfg.home) / "locks" / f"{mops.BUDGET}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
                                   "print('locked', flush=True); sys.stdin.read()", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        c = FakeClient(company_pages("9901"))
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaises(guard.Busy):
                mops.fetch_company(self.cfg, "9901", client=c, as_of=AS_OF)
            self.assertEqual(c.calls, [])                                         # never beside a sync
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        with guard.budget_lock(self.cfg, mops.BUDGET):                           # this process's own hold: re-entered
            self.assertEqual(mops.fetch_company(self.cfg, "9901", client=c, as_of=AS_OF)["status"], "ok")


# =========================================================================== sync


@mock.patch.object(mops, "pdf_to_section", return_value=(SECTION, "start:一、業務內容;end:market;pages:3;backend:x"))
class TestSync(SyncBase):
    def setUp(self) -> None:
        super().setUp()
        self.add("TWSE:9901", 5e9, "TW0009901000")
        self.add("TPEX:9902", 3e9, "TW0009902000")
        self.add("TPEX:9903", 1e9, "TW0009903000")
        self.add("TWSE:01001T", 2e9, "TW00001001T0")     # REIT: not a company code, never queued
        self.add("TSE:7203", 9e9)                         # not Taiwan

    def pages(self) -> dict[str, Any]:
        pages = {**company_pages("9901"), **company_pages("9902", meeting="20260610")}
        pages[list_url("9903", 115)] = NO_DATA
        pages[list_url("9903", 114)] = NO_DATA
        return pages

    def test_sync_writes_documents_and_is_incremental(self, _pdf):
        c = FakeClient(self.pages())
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", full_pass=True)
        self.assertEqual(s["status"], "ok")
        self.assertEqual((s["companies_queued"], s["ok_documents"], s["status_no_annual_filing"]), (3, 2, 1))
        self.assertEqual(s["non_company_code"], ["TWSE:01001T"])
        self.assertEqual(c.calls[0], list_url("9901", 115))                  # market cap order
        docs = self.q("SELECT doc_id, security_id, form, accession, filing_date, report_date, extractor, text_path, "
                      "snapshot_id, url FROM documents ORDER BY doc_id")
        self.assertEqual([d[0] for d in docs], ["mops_annual_report:9901:2025_9901_20260617F04:business",
                                                "mops_annual_report:9902:2025_9902_20260610F04:business"])
        d = docs[0]
        self.assertEqual((d[1], d[2], d[3], d[4], d[5], d[6]),
                         ("TWSE:9901", "股東會年報", "2025_9901_20260617F04.pdf", dt.date(2026, 5, 29),
                          dt.date(2025, 12, 31), mops.EXTRACTOR_VERSION))
        self.assertEqual(Path(d[7]).read_text(encoding="utf-8"), SECTION)
        self.assertIsNotNone(d[8])
        self.assertEqual(d[9], list_url("9901", 115))
        desc = self.q("SELECT source_id, lang, snapshot_id FROM descriptions WHERE security_id = 'TWSE:9901'")
        self.assertEqual(desc[0][:2], ("mops_annual_report", "zh"))
        self.assertEqual(desc[0][2], d[8])            # a batch with documents: descriptions point to its filing batch
        self.assertEqual(self.q("SELECT kind FROM snapshots WHERE snapshot_id = ?", [d[8]])[0][0], "filing_batch")
        ids = self.q("SELECT security_id, id_value, method FROM identifiers WHERE id_type = 'mops_co_id' ORDER BY 1")
        self.assertEqual(ids, [("TPEX:9902", "9902", "symbol"), ("TPEX:9903", "9903", "symbol"),
                               ("TWSE:9901", "9901", "symbol")])
        self.assertEqual(self.states("mops_annual_report")["TPEX:9903"][:2], ("no_annual_filing", 200))
        self.assertEqual(self.last_run()[:2], ("sync-mops", "ok"))
        self.assertEqual(self.q("SELECT license_tier FROM sources WHERE source_id = 'mops_annual_report'")[0][0],
                         "official-private")
        raws = list((Path(self.cfg.raw_dir) / "mops_annual_report").rglob("list-9901-*"))
        self.assertEqual(len(raws), 1)
        # second run: the stored reports are the newest possible (fiscal 2025 in 2026): no request for them
        c2 = FakeClient(self.pages())
        s2 = mops.sync(self.cfg, c2, as_of=AS_OF, mode="annual", full_pass=True)
        self.assertEqual((s2["skipped_unchanged"], s2["skipped_without_request"], s2["ok_documents"]), (2, 2, 0))
        self.assertEqual(c2.calls, [list_url("9903", 115), list_url("9903", 114)])
        # in 2027 a newer report could exist: listed again (116 has none yet, 115 has the stored file) -> skipped
        pages = self.pages()
        pages[list_url("9901", 116)] = NO_DATA
        pages[list_url("9902", 116)] = NO_DATA
        c3 = FakeClient(pages)
        s3 = mops.sync(self.cfg, c3, as_of=dt.date(2027, 3, 1), codes="9901,9902", mode="annual")
        self.assertEqual((s3["skipped_unchanged"], s3["skipped_without_request"], s3["ok_documents"]), (2, 0, 0))
        self.assertEqual(c3.calls, [list_url("9901", 116), list_url("9901", 115), list_url("9902", 116),
                                    list_url("9902", 115)])
        # --refresh downloads again
        s4 = mops.sync(self.cfg, FakeClient(self.pages()), as_of=AS_OF, codes="9901", refresh=True, mode="annual")
        self.assertEqual(s4["ok_documents"], 1)

    def test_extract_failed_is_listed_again_not_newest_possible(self, pdf):
        pdf.return_value = (None, "section_too_short:0;starts:1;pages:3;backend:x")
        s = mops.sync(self.cfg, FakeClient(self.pages()), as_of=AS_OF, mode="annual", codes="9901")
        self.assertEqual(s["status_extract_failed"], 1)
        # next run: the stored failure is not 'the newest that can exist': the listing is fetched (1 request), and the
        # unchanged file + extractor is skipped without downloading the PDF again
        c = FakeClient(self.pages())
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", codes="9901")
        self.assertEqual((s["skipped_without_request"], s["skipped_unchanged"], c.calls),
                         (0, 1, [list_url("9901", 115)]))
        # an extractor fix (new EXTRACTOR_VERSION) downloads and extracts it again
        pdf.return_value = (SECTION, "start:一、業務內容;end:market;pages:3;backend:x")
        with mock.patch.object(mops, "EXTRACTOR_VERSION", mops.EXTRACTOR_VERSION + "-next"):
            s = mops.sync(self.cfg, FakeClient(self.pages()), as_of=AS_OF, mode="annual", codes="9901")
        self.assertEqual(s["ok_documents"], 1)

    def test_truncated_pdf_is_partial_and_fetched_again(self, _pdf):
        pages = self.pages()
        tmp_pdf = mops.DOC_ORIGIN + "/pdf/2025_9901_20260617F04_20260927_010203.pdf"
        pages[tmp_pdf] = lambda url: Response(url, 200, FAKE_PDF, {}, 0.0, truncated=True)
        s = mops.sync(self.cfg, FakeClient(pages), as_of=AS_OF, mode="annual", codes="9901")
        self.assertEqual(s["ok_documents"], 1)
        extractor, note = self.q("SELECT extractor, extract_note FROM documents "
                                 "WHERE source_id = 'mops_annual_report'")[0]
        self.assertEqual(extractor, mops.EXTRACTOR_VERSION + mops.PARTIAL_SUFFIX)
        self.assertIn(";truncated", note)
        # a partial copy is neither 'newest possible' nor 'unchanged': the next run downloads it again (3 requests)
        c = FakeClient(self.pages())
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", codes="9901")
        self.assertEqual((s["ok_documents"], s["skipped_unchanged"], len(c.calls)), (1, 0, 3))
        self.assertEqual(self.q("SELECT extractor FROM documents WHERE source_id = 'mops_annual_report'")[0][0],
                         mops.EXTRACTOR_VERSION)

    def test_codes_limit_min_mcap(self, _pdf):
        c = FakeClient(self.pages())
        s = mops.sync(self.cfg, c, as_of=AS_OF, codes="9902", mode="annual")
        self.assertEqual((s["companies_queued"], s["ok_documents"]), (1, 1))
        c = FakeClient(self.pages())
        s = mops.sync(self.cfg, c, as_of=AS_OF, min_mcap_usd=4e9, refresh=True, mode="annual", full_pass=True)
        self.assertEqual(s["companies_queued"], 1)
        s = mops.sync(self.cfg, FakeClient(self.pages()), as_of=AS_OF, limit=2, refresh=True, mode="annual",
                      full_pass=True)
        self.assertEqual(s["companies_queued"], 2)

    def test_blocked_stops_with_marker(self, _pdf):
        pages = self.pages()
        pages[list_url("9902", 115)] = Blocked(list_url("9902", 115), 403, "http_403")
        c = FakeClient(pages)
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", full_pass=True)
        self.assertEqual((s["status"], s["stopped_reason"], s["ok_documents"]), ("blocked", "blocked", 1))
        self.assertNotIn(list_url("9903", 115), c.calls)
        hit = guard.recent_block_marker(self.cfg, "sync-mops")
        self.assertIn(list_url("9902", 115), hit["note"])
        self.assertEqual(self.states("mops_annual_report")["TPEX:9902"][0], "blocked")

    def test_consecutive_refusals_stop_the_run(self, _pdf):
        for code in ("9904", "9905"):
            self.add(f"TWSE:{code}", 5e8, None)
        busy = b"<html><title>Busy</title></html>"
        c = FakeClient({list_url(k, 115): busy for k in ("9901", "9902", "9903", "9904", "9905")})
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", full_pass=True)
        self.assertEqual((s["status"], s["stopped_reason"], s["companies_attempted"]), ("blocked", "access_refused", 3))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-mops"))

    def test_security_page_stops_at_once(self, _pdf):
        pages = self.pages()
        pages[list_url("9901", 115)] = b"<html>FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED!</html>"
        c = FakeClient(pages)
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", full_pass=True)
        self.assertEqual((s["status"], s["stopped_reason"], len(c.calls)), ("blocked", "access_refused", 1))

    def test_waf_rejection_page_stops_at_once(self, _pdf):
        pages = self.pages()
        pages[list_url("9901", 115)] = WAF_PAGE        # the first company; the others would answer normally
        c = FakeClient(pages)
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", codes="9901,9902,9903")
        self.assertEqual((s["status"], s["stopped_reason"], len(c.calls)), ("blocked", "access_refused", 1))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-mops"))

    def test_basic_mode(self, _pdf):
        c = FakeClient({f"POST {mops.BASIC_API_URL}#9901": basic_json("研發、製造及銷售測試探針卡。"),
                        f"POST {mops.BASIC_API_URL}#9902": basic_json("x", hidden=True),
                        f"POST {mops.BASIC_API_URL}#9903": basic_json("精密量測設備。")})
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="basic")
        self.assertEqual((s["status"], s["ok_descriptions"], s["status_no_basic"]), ("ok", 2, 1))
        rows = self.q("SELECT security_id, text, source_url, snapshot_id FROM descriptions WHERE source_id = 'mops_basic' "
                      "ORDER BY 1")
        self.assertEqual([r[:2] for r in rows], [("TPEX:9903", "精密量測設備。"), ("TWSE:9901", "研發、製造及銷售測試探針卡。")])
        self.assertTrue(all(r[3] for r in rows))                              # never a NULL snapshot
        kinds = self.q("SELECT DISTINCT s.kind FROM descriptions d JOIN snapshots s USING (snapshot_id) "
                       "WHERE d.source_id = 'mops_basic'")
        self.assertEqual(kinds, [("aux_batch",)])                              # the batch holding the raw JSON
        self.assertEqual(rows[1][2], mops.BASIC_PAGE_URL.format(code="9901"))
        self.assertEqual(self.states("mops_basic")["TPEX:9902"][:3], ("no_basic", 200, 1))
        c2 = FakeClient({f"POST {mops.BASIC_API_URL}#9902": basic_json("補上。")})
        s2 = mops.sync(self.cfg, c2, as_of=AS_OF, mode="basic")
        self.assertEqual((s2["skipped_unchanged"], c2.calls), (2, [f"POST {mops.BASIC_API_URL}#9902"]))

    def test_basic_json_throttle_stops_at_once(self, _pdf):
        throttle = json.dumps({"code": 429, "message": "查詢過於頻繁，請稍後再試"}, ensure_ascii=False).encode("utf-8")
        c = FakeClient(default=throttle)
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="basic")
        self.assertEqual((s["status"], s["stopped_reason"], len(c.calls)), ("blocked", "access_refused", 1))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-mops"))

    def test_basic_error_codes_stop_after_three(self, _pdf):
        for code in ("9904", "9905"):
            self.add(f"TWSE:{code}", 5e8, None)
        c = FakeClient(default=json.dumps({"code": 500, "message": "error"}).encode("utf-8"))
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="basic")
        self.assertEqual((s["status"], s["stopped_reason"], len(c.calls)), ("blocked", "access_refused", 3))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-mops"))
        self.assertEqual(self.q("SELECT count(*) FROM descriptions WHERE source_id = 'mops_basic'")[0][0], 0)

    def test_default_mode_is_basic_and_bulk_annual_needs_opt_in(self, _pdf):
        # doc.twse.com.tw's robots.txt disallows crawlers: no bulk annual crawl unless asked for explicitly
        c = FakeClient(default=basic_json("精密量測設備。"))
        s = mops.sync(self.cfg, c, as_of=AS_OF)
        self.assertEqual((s["mode"], s["ok_descriptions"]), ("basic", 3))
        self.assertTrue(all(k.startswith(f"POST {mops.BASIC_API_URL}#") for k in c.calls))
        c = FakeClient(self.pages())
        for kw in ({}, {"limit": 2}, {"min_mcap_usd": 4e9}):
            with self.assertRaises(ValueError) as cm:
                mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", **kw)
            self.assertIn("full_pass", str(cm.exception))
        self.assertEqual(c.calls, [])
        s = mops.sync(self.cfg, c, as_of=AS_OF, mode="annual", codes="9901")      # a few named companies: fine
        self.assertEqual(s["ok_documents"], 1)

    def test_bad_mode(self, _pdf):
        with self.assertRaises(ValueError):
            mops.sync(self.cfg, FakeClient(), mode="zip")


@mock.patch.object(mops, "pdf_to_section", return_value=(SECTION, "start:一、業務內容;end:market;pages:3;backend:x"))
class TestScreenReadsMops(SyncBase):
    """Layer 2 of a screen reads the stored MOPS business section (not only `jevscreen coverage` counts it)."""

    def test_layer2_reads_mops_documents(self, _pdf):
        from jevscreen import coverage, report, screen, store
        self.add("TWSE:9901", 5e9, "TW0009901000")
        s = mops.sync(self.cfg, FakeClient(company_pages("9901")), as_of=AS_OF, mode="annual", codes="9901")
        self.assertEqual(s["ok_documents"], 1)
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            docs = screen.load_documents(con, min_mcap_usd=0)
            by_source = coverage.report(con)["official_document_by_source"]
        d = docs["isin:TW0009901000"]
        self.assertEqual((d["source_id"], d["form"]), ("mops_annual_report", "股東會年報"))
        self.assertEqual(Path(d["text_path"]).read_text(encoding="utf-8"), SECTION)
        self.assertEqual(by_source["mops_annual_report"], 1)
        self.assertEqual(screen.OFFICIAL_DOC_SOURCES["mops_annual_report"], ("MOPS", "zh"))
        self.assertEqual(screen.NATIVE_ID_TYPES["mops_co_id"], "mops_annual_report")
        self.assertEqual(screen.source_label("mops_annual_report"), "MOPS")
        self.assertEqual(report.SOURCE_LABELS["mops_annual_report"], "MOPS")

    def test_coverage_counts_only_what_the_screen_reads(self, _pdf):
        from jevscreen import coverage, screen
        self.assertLessEqual(set(coverage.OFFICIAL_DOC_SOURCES), set(screen.OFFICIAL_DOC_SOURCES))


class TestProvenanceAndCli(unittest.TestCase):
    def test_sources(self):
        self.assertEqual(provenance.tier_of("mops_annual_report"), provenance.LicenseTier.OFFICIAL_PRIVATE)
        self.assertEqual(provenance.tier_of("mops_basic"), provenance.LicenseTier.OFFICIAL_PRIVATE)

    def test_cli_parser_and_registration(self):
        from jevscreen import cli
        p = cli.build_parser()
        a = p.parse_args(["sync-mops"])
        self.assertEqual((a.limit, a.min_mcap, a.codes, a.refresh, a.mode, a.after_block, a.full_pass),
                         (None, None, None, False, "basic", False, False))
        a = p.parse_args(["sync-mops", "--mode", "annual", "--full-pass"])
        self.assertEqual((a.mode, a.full_pass), ("annual", True))
        a = p.parse_args(["sync-mops", "--codes", "2330, 6223", "--mode", "basic", "--limit", "5"])
        self.assertEqual((a.codes, a.mode, a.limit), (["2330", "6223"], "basic", 5))
        self.assertIs(cli.COMMANDS["sync-mops"], cli.cmd_sync_mops)
        self.assertEqual(cli.CLI_RATE_BUDGETS["sync-mops"], "mops")
        self.assertEqual(cli.OFFICIAL_SYNC["sync-mops"][0], "jevscreen.sources.mops")
        self.assertEqual(cli.OFFICIAL_MIN_INTERVAL_S["sync-mops"], mops.DOC_MIN_INTERVAL_S)


from test_cli_coverage import _CliBase, _fake_module  # noqa: E402


class TestCliRun(_CliBase):
    def test_mode_passed_and_journaled(self):
        from jevscreen import cli, config, store
        seen = {}

        def sync(cfg, client, *, limit=None, codes=None, refresh=False, min_mcap_usd=None, mode="basic",
                 full_pass=False):
            seen.update(limit=limit, codes=codes, refresh=refresh, min_mcap_usd=min_mcap_usd, mode=mode,
                        interval=client.min_interval_s)
            if full_pass:
                seen["full_pass"] = True
            return {"status": "ok", "ok_documents": 0}
        with mock.patch.dict(sys.modules, {cli.MOPS: _fake_module(cli.MOPS, sync=sync)}):
            code, out, _ = self.run_cli("sync-mops", "--codes", "2330", "--mode", "basic", "--min-mcap", "2e8")
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"limit": None, "codes": ["2330"], "refresh": False, "min_mcap_usd": 2e8,
                                "mode": "basic", "interval": 1.5})
        with store.session(config.load(), read_only=True, wait_s=5) as con:
            self.assertEqual(con.execute("SELECT command, status FROM runs").fetchall(), [("sync-mops", "ok")])

    def test_bulk_annual_needs_opt_in(self):
        from jevscreen import cli
        seen: list[dict] = []

        def sync(cfg, client, *, limit=None, codes=None, refresh=False, min_mcap_usd=None, mode="basic",
                 full_pass=False):
            seen.append({"mode": mode, "codes": codes, "full_pass": full_pass, "limit": limit})
            return {"status": "ok", "ok_documents": 0}
        with mock.patch.dict(sys.modules, {cli.MOPS: _fake_module(cli.MOPS, sync=sync)}):
            code, _, err = self.run_cli("sync-mops", "--mode", "annual", "--limit", "900")
            self.assertEqual((code, seen), (cli.EXIT_ERROR, []))           # refused before any run
            self.assertIn("robots.txt", err)
            self.assertIn("--full-pass", err)
            self.assertEqual(self.run_cli("sync-mops")[0], 0)                                   # default: basic
            self.assertEqual(self.run_cli("sync-mops", "--mode", "annual", "--codes", "2330")[0], 0)
            self.assertEqual(self.run_cli("sync-mops", "--mode", "annual", "--full-pass", "--limit", "5")[0], 0)
        self.assertEqual(seen, [{"mode": "basic", "codes": None, "full_pass": False, "limit": None},
                                {"mode": "annual", "codes": ["2330"], "full_pass": False, "limit": None},
                                {"mode": "annual", "codes": None, "full_pass": True, "limit": 5}])


if __name__ == "__main__":
    unittest.main()
