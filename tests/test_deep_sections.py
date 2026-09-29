"""Tests for jevscreen.sources.deep_sections and the adapters' deep mode (CNINFO / EDINET / DART, on demand only).

No network: the adapters run on the fake clients of test_cninfo / test_edinet / test_dart. Every issuer, product
and figure below is invented.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "src"))
sys.path.insert(0, str(TESTS))
import safe_env  # noqa: E402,F401

from jevscreen.sources import cninfo, dart, deep_sections as ds, edinet  # noqa: E402

import test_cninfo as tc  # noqa: E402
import test_dart as td  # noqa: E402
import test_edinet as te  # noqa: E402
from jevscreen import store  # noqa: E402


def plain_view(cfg, ck):
    """(doc_id, evidence text sha) of what a screen reads for one company in the current deep view (none outside
    deep_sections.deep_view): load_documents -> choose_document."""
    from jevscreen import screen
    with store.session(cfg, read_only=True, wait_s=5) as con:
        doc = screen.load_documents(con, company_keys=[ck]).get(ck)
    if not doc:
        return None, None
    return doc["doc_id"], store.sha256(Path(doc["text_path"]).read_bytes())

P = ("示例公司围绕示例光模块和示例连接器开展研发、生产和销售，产品用于示例数据中心和示例通信设备，客户覆盖国内外主要设备厂商。"
     "公司采用以销定产的经营模式，主要原材料为示例芯片和示例光器件，通过直销方式服务示例设备厂商。")
PAGES = [
    "\n".join(["示例科技股份有限公司2025年年度报告", "第三节 管理层讨论与分析", "一、报告期内公司所处行业情况",
               "示例行业在报告期内保持增长，示例数据中心建设带动示例光模块需求上升，行业集中度进一步提高，头部厂商份额扩大。",
               "二、报告期内公司从事的主要业务", P, P.replace("研发", "设计"), P.replace("销售", "交付"),
               "三、核心竞争力分析", "公司在示例光模块的高速率产品上拥有自主设计能力和批量交付经验，核心工艺团队稳定，已取得多项示例专利。"
               "公司与示例设备厂商建立了长期合作关系，产品通过了多家客户的认证。",
               "12"]),
    "\n".join(["示例科技股份有限公司2025年年度报告", "四、主营业务分析", "1、概述",
               "报告期内公司实现营业收入示例金额，同比增长，主要来自示例光模块产品的放量和示例连接器的新客户导入。",
               "2、收入与成本", "（1）营业收入构成", "单位：元", "分行业", "示例通信设备制造", "1,234.00", "100.00%",
               "分产品", "示例光模块", "987.00", "80.00%", "示例连接器", "247.00", "20.00%", "分地区", "境内", "境外",
               "（2）占公司营业收入或营业利润10%以上的行业、产品、地区、销售模式的情况", "示例光模块", "987.00", "35.00%",
               "（3）公司实物销售收入是否大于劳务收入", "是", "3、费用", "销售费用同比增长。", "13"]),
    "\n".join(["示例科技股份有限公司2025年年度报告", "十一、公司未来发展的展望",
               "公司计划在2027年推出示例激光雷达新产品，并进入示例汽车市场，目前处于样品阶段，尚未形成收入。",
               "第四节 公司治理", "一、公司治理的基本状况", "公司按规定建立了治理结构。", "14"]),
]


class TestAssemble(unittest.TestCase):
    def test_dedupe_caps_and_order(self):
        base = "甲段落内容足够长。\n乙段落内容也足够长。"
        app, names = ds.assemble(base, [("core", "甲段落内容足够长。\n新的核心竞争力内容足够长足够长足够长足够长足够长足够长。"),
                                        ("mda", None), ("revenue", "x")])
        self.assertEqual(names, ["core"])
        self.assertTrue(app.startswith("\n\n【核心竞争力分析】\n"))
        self.assertNotIn("甲段落", app)
        big = "\n".join("段落" * 30 + str(i) for i in range(2000))
        app, names = ds.assemble("", [("mda", big), ("revenue", big), ("core", big)])
        self.assertLessEqual(len(app), ds.DEEP_MAX_CHARS + 10)
        for name in names:
            self.assertIn(ds.HEADINGS[name], app)
        block = app.split("\n\n")[1]
        self.assertLessEqual(len(block), ds.BLOCK_CAPS["mda"] + len(ds.HEADINGS["mda"]) + 1)

    def test_pack_short_joins_table_cells(self):
        text = ds.pack_short("分产品\n示例光模块\n987.00\n80.00%\n" + "长" * 50)
        self.assertEqual(text.split("\n")[0], "分产品 | 示例光模块 | 987.00 | 80.00%")
        self.assertEqual(text.split("\n")[1], "长" * 50)


class TestCninfoDeep(unittest.TestCase):
    def test_plain_extraction_is_unchanged_and_deep_adds_blocks(self):
        plain, pnote = cninfo.extract_from_pages(PAGES, "full")
        self.assertIsNotNone(plain)
        self.assertNotIn("deep:", pnote)
        text, note = cninfo.extract_from_pages(PAGES, "full", deep=True)
        self.assertTrue(text.startswith(plain))
        app = text[len(plain):]
        self.assertIn("deep:revenue,core,mda", note)
        self.assertIn("【营业收入构成】", app)
        self.assertIn("示例光模块 | 987.00 | 80.00%", app)             # table cells packed into one line
        self.assertIn("分地区", app)
        self.assertIn("占公司营业收入或营业利润10%以上", app)             # the 10% table belongs to the block
        self.assertNotIn("实物销售收入", app)                             # (3) ends it
        self.assertIn("【核心竞争力分析】", app)
        self.assertIn("【管理层讨论与分析（节选）】", app)
        self.assertIn("行业集中度进一步提高", app)
        self.assertEqual(app.count(P), 0)                                  # the business section is not repeated
        self.assertNotIn("激光雷达", text)                                 # the outlook is left out on purpose
        self.assertLessEqual(len(app), ds.DEEP_MAX_CHARS + 10)

    def test_pdf_fixture_deep(self):
        plain, _n, _b = cninfo.extract_from_pdf(tc.FULL_PDF, "full")
        text, note, _b2 = cninfo.extract_from_pdf(tc.FULL_PDF, "full", deep=True)
        self.assertTrue(text.startswith(plain))
        self.assertGreater(len(text), len(plain))
        self.assertIn(";deep:", note)


class TestCninfoDeepSync(tc.SyncBase):
    def test_deep_text_is_its_own_row_and_a_plain_screen_never_reads_it(self):
        self.add_standard()
        self.run_sync(tc.standard_client(), codes=["309386"])            # plain: the summary, cninfo-v3
        shallow = self.q("SELECT doc_id, text_path, text_sha256, extractor FROM documents WHERE cik = ? "
                         "ORDER BY doc_id", [tc.ORG_309386])
        self.assertTrue(all(r[3].startswith("cninfo-v3/") for r in shallow))
        ck = self.q("SELECT company_key FROM securities WHERE security_id = 'SZSE:309386'")[0][0]
        before = plain_view(self.cfg, ck)
        with self.assertRaises(ValueError):
            cninfo.sync(self.cfg, tc.standard_client(), deep=True)        # never a bulk deep re-crawl
        client = tc.standard_client()
        summary, _ = self.run_sync(client, codes=["309386"], deep=True)
        self.assertEqual((summary["status"], summary["kind"], summary["deep"]), ("ok", "full", True))
        self.assertIn(tc.FULL_URL, client.calls)
        row = self.q("SELECT extractor, extract_note, text_path, doc_id, section FROM documents WHERE url = ? AND "
                     "section = ?", [tc.FULL_URL, ds.DEEP_SECTION])[0]
        self.assertTrue(row[0].startswith(cninfo.DEEP_EXTRACTOR_VERSION + "/"))
        self.assertIn(";deep:", row[1])
        self.assertTrue(row[2].endswith("-business_deep.txt") and row[3].endswith(":business_deep"))
        deep_text = Path(row[2]).read_text(encoding="utf-8")
        # the shallow rows and files are untouched, and a screen without the levers reads exactly what it read
        self.assertEqual(self.q("SELECT doc_id, text_path, text_sha256, extractor FROM documents WHERE cik = ? AND "
                                "section IS DISTINCT FROM ? ORDER BY doc_id", [tc.ORG_309386, ds.DEEP_SECTION]),
                         shallow)
        self.assertEqual(plain_view(self.cfg, ck), before)
        with ds.deep_view([ck]):                                          # the top-N update pass reads the deep text
            self.assertEqual(plain_view(self.cfg, ck)[0], row[3])
        # a plain sync never settles on (nor replaces) the deep row; a second deep fetch has nothing new to read
        client = tc.standard_client()
        self.run_sync(client, codes=["309386"], kind="full")
        self.assertIn(tc.FULL_URL, client.calls)
        self.assertEqual(Path(row[2]).read_text(encoding="utf-8"), deep_text)
        client = tc.standard_client()
        summary, _ = self.run_sync(client, codes=["309386"], deep=True)
        self.assertNotIn(tc.FULL_URL, client.calls)

    def test_failed_deep_extraction_never_hides_the_shallow_row(self):
        self.add_standard()
        self.run_sync(tc.standard_client(), codes=["309386"], kind="full")
        ck = self.q("SELECT company_key FROM securities WHERE security_id = 'SZSE:309386'")[0][0]
        before = plain_view(self.cfg, ck)
        self.assertIsNotNone(before[0])
        with mock.patch.object(cninfo, "extract_from_pdf", return_value=(None, "start_heading_not_found", "x")):
            self.run_sync(tc.standard_client(), codes=["309386"], deep=True)
        self.assertEqual(plain_view(self.cfg, ck), before)
        with ds.deep_view([ck]):
            self.assertEqual(plain_view(self.cfg, ck), before)


class TestEdinetDeep(te.EdinetSyncBase):
    def zip_with_blocks(self) -> bytes:
        business = "当社グループは、架空の精密部品の製造販売を主な事業としております。" * 5
        mda = "(1) 経営成績の状況\n当連結会計年度の架空センサーの販売は増加いたしました。生産、受注及び販売の実績は次のとおりです。" * 3
        seg = "報告セグメントは架空センサー事業と架空部品事業であります。\n架空センサー\n1,234\n架空部品\n567"
        tsv = ("要素ID\t項目名\tコンテキストID\t相対年度\t値\n"
               f'"{edinet.TEXT_BLOCK_ELEMENT}"\t"x"\t"FilingDateInstant"\t"提出日時点"\t"{business}"\n'
               f'"jpcrp_cor:{ds.JA_MDA_SUFFIX}"\t"x"\t"FilingDateInstant"\t"提出日時点"\t"{mda}"\n'
               f'"jpcrp_cor:NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock"\t"x"\t"CurrentYearDuration"'
               f'\t"当期"\t"{seg}"\n')
        return te.zip_of([("XBRL_TO_CSV/jpcrp030000-asr-001_X.csv", b"\xff\xfe" + tsv.encode("utf-16-le"))])

    def test_blocks_only_when_deep(self):
        data = self.zip_with_blocks()
        b, extra, note = edinet.extract_business_blocks(data)
        self.assertNotIn("deep:", note)
        self.assertNotIn("経営成績", extra)
        b2, extra2, note2 = edinet.extract_business_blocks(data, deep=True)
        self.assertEqual(b2, b)
        self.assertIn("deep:ja_mda,ja_segment", note2)
        self.assertIn(ds.HEADINGS["ja_mda"], extra2)
        self.assertIn("架空センサー事業", extra2)

    def test_deep_sync_and_plain_sync_keeps_it(self):
        pages = te.standard_pages()
        pages[te.doc_url("S100T001")] = self.zip_with_blocks()
        self.add_standard()
        self.run_sync(te.FakeClient(pages), codes=["9999"])               # the baseline store: shallow
        shallow = self.q("SELECT doc_id, text_path, text_sha256 FROM documents WHERE security_id = 'TSE:9999'")
        ck = self.q("SELECT company_key FROM securities WHERE security_id = 'TSE:9999'")[0][0]
        before = plain_view(self.cfg, ck)
        events = []
        with self.assertRaises(ValueError):
            edinet.sync(self.cfg, te.FakeClient(pages), deep=True)
        summary, _ = self.run_sync(te.FakeClient(pages), codes=["9999"], deep=True,
                                   on_company=lambda sids, st, note: events.append((tuple(sids), st)))
        self.assertEqual((summary["status"], summary["ok_documents"], summary["deep"]), ("ok", 1, True))
        self.assertIn((("TSE:9999",), "ok"), events)
        ext, path = self.q("SELECT extractor, text_path FROM documents WHERE security_id = 'TSE:9999' AND "
                           "section = ?", [ds.DEEP_SECTION])[0]
        self.assertEqual(ext, f"{edinet.DEEP_EXTRACTOR_VERSION}/csv")
        self.assertIn(ds.HEADINGS["ja_segment"], Path(path).read_text(encoding="utf-8"))
        self.assertEqual(self.q("SELECT doc_id, text_path, text_sha256 FROM documents WHERE security_id = 'TSE:9999' "
                                "AND section IS DISTINCT FROM ?", [ds.DEEP_SECTION]), shallow)
        self.assertEqual(plain_view(self.cfg, ck), before)              # a screen without the levers: unchanged
        for deep in (False, True):                                     # each kind settles on its own row
            client = te.FakeClient(pages)
            summary, _ = self.run_sync(client, codes=["9999"], deep=deep)
            self.assertEqual(summary["skipped_unchanged"], 1)
            self.assertFalse(any("documents/S100T001" in u for u in client.public_calls()))


class TestDartDeep(td.DartSyncBase):
    mode = "web"
    SALES = {"rcpNo": td.WEB_RCPT, "dcmNo": "9990488", "eleId": "13", "offset": "109887", "length": "3347",
             "dtd": "dart4.xsd"}
    MDA = {"rcpNo": td.WEB_RCPT, "dcmNo": "9990488", "eleId": "102", "offset": "1018930", "length": "8138",
           "dtd": "dart4.xsd"}

    def pages(self) -> dict:
        pages = td.web_pages()
        pages[td.viewer_url(self.SALES)] = ("<P class='section-2'>4. 매출 및 수주상황</P><TABLE><TR><TD>가상 센서"
                                            "</TD><TD>1,234</TD></TR><TR><TD>가상 모듈</TD><TD>567</TD></TR></TABLE>"
                                            "<P>당사의 매출은 가상 센서와 가상 모듈로 구성되어 있으며 국내외 고객사에 판매됩니다.</P>"
                                            ).encode("utf-8")
        pages[td.viewer_url(self.MDA)] = ("<P class='section-1'>IV. 이사의 경영진단 및 분석의견</P>"
                                          "<P>당기 가상 센서 부문의 매출이 증가하였으며 신규 고객사향 공급이 시작되었습니다.</P>"
                                          ).encode("utf-8")
        return pages

    def test_deep_web_sync(self):
        self.add_standard()
        with self.assertRaises(ValueError):
            self.run_sync(td.FakeClient(self.pages()), deep=True)          # codes required
        client = td.FakeClient(self.pages())
        summary, _ = self.run_sync(client, codes="999990", deep=True)
        self.assertEqual((summary["status"], summary["deep"]), ("ok", True))
        pub = client.public_calls()
        self.assertIn(td.viewer_url(self.SALES), pub)
        self.assertIn(td.viewer_url(self.MDA), pub)
        ext, path = self.q("SELECT extractor, text_path FROM documents WHERE security_id = 'KRX:999990'")[0]
        self.assertEqual(ext, dart.DEEP_WEB_EXTRACTOR_VERSION)
        self.assertTrue(path.endswith("-business_deep.txt"))
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("【주요 제품 및 서비스】", text)
        self.assertIn(ds.HEADINGS["ko_sales"], text)
        self.assertIn("가상 센서 | 1,234", text)
        self.assertIn(ds.HEADINGS["ko_mda"], text)
        st = self.states(dart.SOURCE_ID)["KRX:999990"]
        self.assertIn("deep:ko_sales,ko_mda", st[3])
        ck = self.q("SELECT company_key FROM securities WHERE security_id = 'KRX:999990'")[0][0]
        self.assertEqual(plain_view(self.cfg, ck), (None, None))          # no shallow row: a plain screen reads none
        client = td.FakeClient(self.pages())
        summary, _ = self.run_sync(client, codes="999990")                 # plain web: its own (shallow) row
        self.assertEqual(summary["skipped_unchanged"], 0)
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE security_id = 'KRX:999990'")[0][0], 2)
        self.assertNotIn(ds.HEADINGS["ko_sales"], Path(self.q(
            "SELECT text_path FROM documents WHERE security_id = 'KRX:999990' AND section = 'business'")[0][0]
        ).read_text(encoding="utf-8"))
        client = td.FakeClient(self.pages())
        summary, _ = self.run_sync(client, codes="999990", deep=True)      # the deep row is current for a deep fetch
        self.assertEqual(summary["skipped_unchanged"], 1)
        self.assert_key_nowhere()

    def test_chapter_fallback_is_deepened_too(self):
        """A TOC without the '사업의 개요' sub-node (chapter fallback): the deep blocks are still fetched and
        appended, so a row labelled deep really holds the deep text."""
        self.add_standard()
        pages = self.pages()
        mda = td.rng(9, 90000, 5000)
        pages[td.main_url("20260320000202")] = td.toc_page(
            td.js_node("node1", {"text": "I. 회사의 개요", **td.rng(3, 100, 800)})
            + td.js_node("node1", {"text": "II. 사업의 내용", **td.CHAPTER_RANGE})
            + td.js_node("node1", {"text": "IV. 이사의 경영진단 및 분석의견", **mda})).encode("utf-8")
        pages[td.viewer_url(mda)] = ("<P class='section-1'>IV. 이사의 경영진단 및 분석의견</P><P>당기 가상 항체 플랫폼의 "
                                     "기술이전 수익이 증가하였으며 신규 제약사와의 공동개발 계약이 체결되었습니다.</P>"
                                     ).encode("utf-8")
        client = td.FakeClient(pages)
        self.run_sync(client, codes="999980", deep=True)
        self.assertIn(td.viewer_url(mda), client.public_calls())
        ext, path, note = self.q("SELECT extractor, text_path, extract_note FROM documents WHERE "
                                 "security_id = 'KRX:999980'")[0]
        self.assertEqual(ext, dart.DEEP_WEB_EXTRACTOR_VERSION)
        self.assertIn("fallback:chapter", note)
        self.assertIn("deep:ko_mda", note)
        self.assertIn(ds.HEADINGS["ko_mda"], Path(path).read_text(encoding="utf-8"))

    def test_failed_deep_block_keeps_the_rest_as_partial(self):
        self.add_standard()
        pages = self.pages()
        pages[td.viewer_url(self.MDA)] = 500
        self.run_sync(td.FakeClient(pages), codes="999990", deep=True)
        ext = self.q("SELECT extractor FROM documents WHERE security_id = 'KRX:999990'")[0][0]
        self.assertEqual(ext, dart.DEEP_WEB_EXTRACTOR_VERSION + dart.PARTIAL_SUFFIX)
        self.assertIn("ko_mda_http_500", self.states(dart.SOURCE_ID)["KRX:999990"][3])


if __name__ == "__main__":
    unittest.main()
