"""Fixes from novice simulation #3 (2026-09-28, dexterous-hand components), offline:

- the chat and the page report the same numbers: how many companies your AI checked (every deck, not only the
  last one) and how many were removed (the page's removed list);
- a one-line description taken from a profile that names only a side business gives way to the annual report's
  own business sentence when the run read one;
- a short final list (fewer than 10) is said plainly, with the companies that passed the first read but were not
  confirmed (the 待确认 tier) and a free next step, instead of silence;
- a scope default applied from the idea's own words (e.g. buyers left out) is shown visibly on the page and in chat,
  each with a one-click undo line.

No network and no paid calls: the fakes of test_quickstart / test_screen / test_review_flow; every company is
invented.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import page, quickstart as qs, review_cli, short_list, translations  # noqa: E402
from test_page import LOCAL, _novice_result, dom_text  # noqa: E402
from test_quickstart import IDEA as QIDEA  # noqa: E402
import test_review_flow as TRF  # noqa: E402

CJK = re.compile(r"[㐀-鿿]")


# ------------------------------------------------------------------------------------------------ counts

class TestChatAndPageCountTheSame(TRF.FlowCase):
    """Novice #3: the chat said 'I checked 3 ... removed 9' (the last deck's count beside every removal) while the
    page said 'your AI checked 20'."""

    def some_no(self, it):
        if it.get("held_sid") or it["n"] % 3:
            return self.yes_all(it)
        return {"v": "no", "chip": "e", "quote_ids": [1], "why": "不做这个"}

    def judged_twice(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        code, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, self.some_no)))
        self.assertEqual(code, 0)
        second = out.get("review_pending") or out.get("part_b")    # a follow-up deck of new top rows, or part B
        self.assertIsNotNone(second)
        deck_b = json.loads(Path(second["deck_path"]).read_text(encoding="utf-8"))
        code, out_b = review_cli.judge(self.cfg, deck_b["deck_id"],
                                       file=str(self.answers_file(deck_b, self.some_no)))
        self.assertEqual(code, 0)
        return deck, deck_b, out_b

    def test_after_part_b_the_chat_counts_every_check_and_every_removal_as_the_page_does(self):
        deck, deck_b, out = self.judged_twice()
        _d, r = review_cli.run_files(self.cfg, out["run_id"])
        data = page.build_page_data(r, None, lang="zh")
        n, x = page.change_of(r)["n"], len(data["removed"])
        self.assertGreater(n, len(deck_b["items"]))            # every deck, not only the last one
        self.assertGreater(x, 0)
        self.assertIn(f"核对了 {n} 家", out["text_zh"])
        self.assertIn(f"移出 {x} 家", out["text_zh"])
        self.assertIn(f"checked the excerpts of {n} companies", out["text_en"])
        self.assertIn(f"{x} removed", out["text_en"])
        self.assertIn(f"你的 AI 核对了 {n} 家后调整", data["change"]["zh"])        # the page's version line
        self.assertIn(f"（{x} 家）", page.render_text(data, "zh"))                  # the page's removed section

    def test_the_annual_report_and_profile_split_adds_up_to_the_total(self):
        _a, _b, out = self.judged_twice()
        _d, r = review_cli.run_files(self.cfg, out["run_id"])
        n = page.change_of(r)["n"]
        m = re.search(r"核对了 (\d+) 家的摘录（年报 (\d+) 家、简介 (\d+) 家）", out["text_zh"])
        self.assertIsNotNone(m, out["text_zh"])
        self.assertEqual(int(m.group(1)), n)
        self.assertEqual(int(m.group(2)) + int(m.group(3)), n)


class TestStatusCountsFollowTheCurrentVersion(TRF.RelayAndPage):
    def test_a_later_version_keeps_the_chat_and_the_page_counts_equal(self):
        out = self.done_flow(lang="zh")
        deck_id = out["pending"][0]["deck_id"]
        deck = json.loads(Path(self.job()["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        p = self.home / "ans.json"
        from jevscreen import review
        p.write_text(json.dumps({"format": review.ANSWERS_FORMAT, "deck_id": deck_id, "answers": {
            str(it["n"]): {"v": "no", "chip": "e", "quote_ids": [1], "why": "不做这个"} for it in deck["items"]}}),
            encoding="utf-8")
        code, _j = review_cli.judge(self.cfg, deck_id, file=str(p))
        self.assertEqual(code, 0)
        s = qs.status(self.cfg, qs.idea_key(QIDEA))
        data = page.read_page_data(Path(s["page"]))
        x = len(data["removed"])
        self.assertGreater(x, 0, [v.get("state") for v in review.agent_verdicts(self.cfg, QIDEA).values()])
        self.assertIn(f"移出 {x} 家", s["text_zh"])
        self.assertEqual(s["agent_summary"]["removed_total"], x)
        # the human keeps the removed company: a new version with nothing removed; the chat must not repeat
        # the old 'removed 1'
        sid, kept_name = data["removed"][0]["ticker"], data["removed"][0]["name"]
        rid = s["run_id"]
        full = next(r["security_id"] for r in json.loads(
            (Path(self.job()["result"]["output_dir"]) / "results.json").read_text(encoding="utf-8"))
            ["excluded_by_agent"] if sid in r["security_id"])
        code, _d = review_cli.decide(self.cfg, f"keep={full}", rid)
        self.assertEqual(code, 0)
        s = qs.status(self.cfg, qs.idea_key(QIDEA))
        data = page.read_page_data(Path(s["page"]))
        self.assertEqual(len(data["removed"]), x - 1)
        self.assertNotIn(f"移出 {x} 家", s["text_zh"])
        if x > 1:
            self.assertIn(f"移出 {x - 1} 家", s["text_zh"])
        # the JSON an agent reads directly follows the version too (docs/AGENT_API.md: agent_summary)
        summ = s["agent_summary"]
        self.assertEqual(summ["removed_total"], x - 1)
        self.assertIn(f"核对了 {summ['read']} 家", s["text_zh"])
        self.assertNotIn(f"移出 {x} 家", summ["text_zh"])
        self.assertIn(summ["text_zh"], s["text_zh"])            # the same line as the chat's
        if x - 1 == 0:
            self.assertEqual(summ["removed"], [])
            self.assertNotIn(kept_name, summ["text_zh"])


# ------------------------------------------------------------------------------------------------ one line

OVERVIEW = ("作为AI硬件制造平台，公司依托模切、冲压、CNC等全栈式工艺制造能力，为客户提供从核心零部件、功能模组到精品组装的"
            "全方位解决方案。 公司核心产品广泛应用于AI终端、AI服务器、人形机器人等领域。")


def _ar_result():
    res = _novice_result()
    res["rows"][1]["evidence_sha"] = "sha-k2"
    return res


def _pieces(overview=OVERVIEW):
    return {"k2": {"sha": "sha-k2", "text": "x", "pieces": [overview, "机器人业务全面提供电机、减速器、灵巧手。"],
                   "overview": overview}}


class TestOneLineFromTheAnnualReport(unittest.TestCase):
    PROFILE = {"k2": "Baifeng develops, produces, and sells magnetic materials and products."}

    def test_the_annual_reports_business_sentence_replaces_a_profile_line(self):
        d = page.build_page_data(_ar_result(), None, lang="zh", local_names=LOCAL, descriptions=self.PROFILE,
                                 l2_pieces=_pieces())
        row = next(r for r in d["rows"] if r["rank"] == 2)
        self.assertTrue(row["one_line"].startswith("作为AI硬件制造平台"), row["one_line"])
        self.assertNotIn("magnetic", row["one_line"])
        self.assertEqual(row["one_line_source"], "annual_report")
        self.assertIn("magnetic", row["one_line_profile"])       # kept second, labelled as the profile's line
        self.assertIn(page.STRINGS["zh"]["what_profile"], page.render_text(d, "zh"))
        translations.apply(d, {})
        self.assertFalse(row.get("one_line_x"))                 # a Chinese sentence on a Chinese page: no translation
        [top] = [t for t in page.top_rows(d) if t["rank"] == 2]
        self.assertTrue(top["one_line"].startswith("作为AI硬件制造平台"))
        self.assertEqual(top["one_line_source"], "annual_report")
        self.assertIn(page.STRINGS["zh"]["what_ar"], page.render_text(d, "zh"))

    def test_an_english_page_marks_it_for_translation(self):
        d = page.build_page_data(_ar_result(), None, lang="en", descriptions=self.PROFILE, l2_pieces=_pieces())
        translations.apply(d, {})
        row = next(r for r in d["rows"] if r["rank"] == 2)
        self.assertEqual(row["one_line_source"], "annual_report")
        self.assertTrue(row["one_line_x"])                      # the agent translates it (the page stays English)
        self.assertIsNone(CJK.search(page.STRINGS["en"]["what_ar"]))

    def test_a_heading_or_a_stale_text_keeps_the_profile_line(self):
        for pieces, why in ((_pieces("（一）主要业务"), "a bare heading"),
                            ({"k2": {**_pieces()["k2"], "sha": "other"}}, "the text of another document")):
            with self.subTest(why):
                d = page.build_page_data(_ar_result(), None, lang="zh", descriptions=self.PROFILE, l2_pieces=pieces)
                row = next(r for r in d["rows"] if r["rank"] == 2)
                self.assertIn("magnetic", row["one_line"])
                self.assertEqual(row["one_line_source"], "profile")

    def test_leading_numbering_and_headings_are_stripped(self):
        cases = {"（一）主要业务 公司是全球领先的精密减速器制造商，产品用于工业机器人。": "公司是全球领先的精密减速器制造商",
                 "３ 【事業の内容】当社グループは、主に減速装置を生産・販売する精密減速機事業を営んでおります。": "当社グループは",
                 "Item 1. Business Overview We design and sell motion control products for robots worldwide.":
                     "We design and sell motion control products"}
        for text, start in cases.items():
            with self.subTest(text[:12]):
                self.assertTrue((page.business_sentence(text) or "").startswith(start), page.business_sentence(text))

    def test_the_overview_excerpt_is_loaded_from_the_run_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "l2_inputs.jsonl").write_text(json.dumps({
                "company_key": "k2", "evidence_sha": "sha-k2", "text": "x",
                "excerpts": [{"kind": "overview", "text": OVERVIEW}, {"kind": "context", "text": "别的"}]},
                ensure_ascii=False) + "\n", encoding="utf-8")
            got = page.load_l2_pieces(tmp, ["k2"])
        self.assertEqual(got["k2"]["overview"], OVERVIEW)
        self.assertEqual(got["k2"]["pieces"], [OVERVIEW, "别的"])


# real overview sentences (annual reports of 2025) that are about the economy, the industry, the legal set-up or a
# table, not what the company does: never the row's headline (review of novice #3)
NOT_THE_BUSINESS = {
    "SSE:603700": "2025年我国经济总体保持平稳运行，宏观政策持续发力，新质生产力加快培育，出口保持较强韧性，市场运行呈现出一定的修复态势。"
                  "从行业来看，随着民用水表轮换周期的到来，存量市场需求释放展现出较强的持续性。",
    "SZSE:000532": "2025年，中国宏观经济顶压前行、稳中有进、向新向优，产业升级与新质生产力加快培育，发展质效稳步提升。 "
                   "国务院及相关部门围绕资本市场改革、创投生态优化、耐心资本培育等领域密集出台政策。",
    "SSE:600126": "2025年，钢铁行业面临的市场环境非常严峻，在终端需求疲软、供给弹性不足的情况下，钢价易跌难涨，企业生产经营持续承压。 "
                  "2025年，我国数字产业保持高质量发展态势。",
    "SSE:603296": "电子信息产业是指利用电子技术和信息技术从事电子信息产品相关的设备生产、硬件制造、系统集成、软件开发以及应用服务等细分行业的集合。"
                  "涉及的领域主要包括消费、计算机、通信、家用、汽车、电子器件、软件等。",
    "SSE:600601": "PCB作为电子产品核心的电子互连件，既是电子零件的基板，为元器件提供支撑与电气连接，也是融合电子、机械、化工材料等多领域技术的基础"
                  "产品，因而被称为“电子系统产品之母”。PCB产业发展水平，一定程度上成为衡量一个国家或地区电子信息产业发展速度与技术水准的关键指标。",
    "SSE:603580": "全球轻型输送带的市场主要位于欧洲、北美和亚洲，生产商也多集中于欧、美、日等发达国家和地区，其中全球规模较大的三家生产商是Ammega、"
                  "Habasit和Forbo-Siegling，在全球轻型输送带的市场份额占比达30%左右。在日本和亚太市场，日本的阪东化学、三星皮带、NITTA等公司具有较强的竞争力。",
    "SSE:601138": "当前，全球主要云服务商对AI基础设施的资本支出已进入新一轮扩张周期。随着GPU与ASIC等算力方案持续迭代，AI算力需求持续旺盛。",
    "SSE:603797": "权的特许经营区域和特许经营期内，提供污水收集、输送和/或终端污水处理服务。公司所处的污水处理行业，按照证监会《上市公司行业分类指引"
                  "（2012年修订）》的行业划分，公司属于“水利、环境和公共设施管理业-生态保护和环境治理业”（代码：N77）。",
    "NYSE:SPXC": "Some of the statements in this document and any documents incorporated by reference, including any "
                 "statements as to operational and financial projections, constitute \u201cforward-looking statements\u201d "
                 "within the meaning of Section 21E of the Securities Exchange Act of 1934. We caution you.",
    "NASDAQ:CAMT": "Our legal and commercial name is Camtek Ltd. We were incorporated under the laws of the State of Israel in "
                   "1987 and operate under the Companies Law. Our headquarters are located in Ramat Gavriel Industrial Zone.",
    "NASDAQ:SILC": "Our legal and commercial name is Silicom Ltd. We were incorporated under the laws of the State of Israel in "
                   "1987, and we operate under Israeli law and legislation.",
    "NASDAQ:VRNS": "We were incorporated under the laws of the State of Delaware on November 3, 2004 and commenced operations on "
                   "January 1, 2005. Our principal offices are located at 801 Brickell Avenue, Miami, FL 33131.",
    "TSE:6113": "（イ）金属加工機械事業・板金商品：板金加工機械、ソフトウェア及び周辺装置の開発、製造、販売を行っております。",
    "KRX:029780": "영업부문 | 취급업무 | 주요 내용 카드사업 부문 | 개인신판 | 개인고객을 대상으로 한 할부 및 일시불 신용제공 서비스 법인신판 | "
                  "법인고객을 대상으로 한 신용제공 서비스.",
}
NAMES = {"NYSE:SPXC": ["SPX Technologies, Inc."], "NASDAQ:CAMT": ["Camtek Ltd."], "NASDAQ:SILC": ["Silicom Ltd."],
         "NASDAQ:VRNS": ["Varonis Systems, Inc."], "NYSE:FLNG": ["FLEX LNG Ltd."]}


class TestTheOneLineIsAboutTheCompany(unittest.TestCase):
    def test_a_sentence_about_the_economy_the_industry_or_the_legal_setup_is_never_the_headline(self):
        for sid, text in NOT_THE_BUSINESS.items():
            with self.subTest(sid):
                self.assertIsNone(page.business_sentence(text, names=NAMES.get(sid)))

    def test_a_rejected_first_sentence_gives_way_to_the_next_one_about_the_company(self):
        flng = ("FLEX LNG Ltd. is an exempted company incorporated under the Bermuda Companies Act of 1981 (Company No. "
                "EC-34296). We are a growth-oriented owner and commercial operator of fuel efficient, fifth generation "
                "LNG carriers. As of February 27, 2026, we own 13 vessels.")
        self.assertTrue((page.business_sentence(flng, names=NAMES["NYSE:FLNG"]) or "").startswith(
            "We are a growth-oriented owner"), page.business_sentence(flng, names=NAMES["NYSE:FLNG"]))
        econ = ("2025年，中国宏观经济顶压前行、稳中有进。公司主要从事稀有金属钽、铌及合金等的研发、生产、销售和进出口业务。"
                "国务院出台政策。")
        self.assertEqual(page.business_sentence(econ), "公司主要从事稀有金属钽、铌及合金等的研发、生产、销售和进出口业务。")
        two_bad = "2025年，钢铁行业面临的市场环境非常严峻。2025年，我国数字产业保持高质量发展态势。公司从事钢铁生产业务。"
        self.assertIsNone(page.business_sentence(two_bad))                     # one retry, then the profile line

    def test_a_heading_without_a_full_stop_is_stripped(self):
        text = ("（一）公司所从事的主要业务、主要产品及其用途、经营模式，公司产品市场地位、竞争优势与劣势，公司所处的行业地位情况 "
                "公司的经营业务主要包含生物医药研发生产业务、建材生产制造业务和私募股权投资基金管理业务。 生物医药研发生产业务主要包括多肽原料药。")
        self.assertEqual(page.business_sentence(text), "公司的经营业务主要包含生物医药研发生产业务、建材生产制造业务和私募股权投资基金管理业务。")
        glued = text.replace("情况 公司的", "情况公司的")
        self.assertEqual(page.business_sentence(glued), "公司的经营业务主要包含生物医药研发生产业务、建材生产制造业务和私募股权投资基金管理业务。")

    def test_the_company_as_subject_by_its_own_name_or_a_pronoun_is_kept(self):
        cases = [("Teradyne, Inc. was founded in 1960 and is a leading global provider of automated test equipment.",
                  ["Teradyne, Inc."]),
                 ("Shopify provides essential internet infrastructure for commerce.", ["Shopify Inc."]),
                 ("国博电子主要从事有源相控阵T/R组件和射频集成电路相关产品的研发、生产和销售。", ["国博电子"]),
                 ("당사는 의약품, 의약품 원료, 건강보조식품의 제조 및 판매 사업 등을 영위하고 있습니다.", None),
                 ("当社グループは、主に減速装置を生産・販売する精密減速機事業を営んでおります。", None),
                 ("在金融信息技术业务领域，公司主要为银行等金融机构提供软件开发和技术服务。", None),
                 ("The Company designs, manufactures and sells precision motion control products worldwide.", None)]
        for text, names in cases:
            with self.subTest(text[:16]):
                self.assertEqual(page.business_sentence(text, names=names), text)

    def test_the_page_passes_the_row_names(self):
        res = _ar_result()
        name = res["rows"][1]["name"]
        ov = f"{name} develops and sells dexterous-hand reducers for humanoid robots worldwide."
        d = page.build_page_data(res, None, lang="en", descriptions=TestOneLineFromTheAnnualReport.PROFILE,
                                 l2_pieces=_pieces(ov))
        row = next(r for r in d["rows"] if r["rank"] == 2)
        self.assertEqual(row["one_line_source"], "annual_report")
        self.assertEqual(row["one_line"], ov)


# ------------------------------------------------------------------------------------------------ short list

def _job(listed, *, short=None, steps=None):
    res = {"run_id": "scr-x", "summary": {"listed": listed, "annual_report": listed, "profile_only": 0},
           "page": None, "page_opened": True, "cost_usd": 0.2, "seconds": 60, "top": [],
           "next_steps": steps if steps is not None else [{"text_zh": "换一个想法", "text_en": "Another idea"}]}
    if short is not None:
        res["short_list"] = short
    return {"idea": "灵巧手零部件", "result": res}


class TestShortListIsSaidPlainly(unittest.TestCase):
    SHORT = {"listed": 5, "unverified": 40, "examples_zh": ["鸣志电器", "江苏雷利"],
             "examples_en": ["Moons' Electric", "Jiangsu Leili"]}

    def test_a_list_under_ten_says_so_and_points_at_the_to_confirm_tier(self):
        zh = qs.human_text(_job(5, short=self.SHORT), "done", [], "zh", None)
        self.assertIn("只有 5 家", zh)
        self.assertIn("40 家", zh)
        self.assertIn("鸣志电器", zh)
        self.assertIn(page.STRINGS["zh"]["unverified_title"].split("（")[0], zh)   # the page's section, by name
        en = qs.human_text(_job(5, short=self.SHORT), "done", [], "en", None)
        self.assertIn("only 5 companies", en)
        self.assertIn("40", en)
        self.assertIn("Moons' Electric", en)
        self.assertIsNone(CJK.search(en), en)

    def test_ten_or_more_says_nothing_extra(self):
        zh = qs.human_text(_job(12), "done", [], "zh", None)
        self.assertNotIn("不到 10 家", zh)

    def test_without_a_to_confirm_tier_it_still_says_why_and_what_next(self):
        zh = qs.human_text(_job(3, short={"listed": 3, "unverified": 0}), "done", [], "zh", None)
        self.assertIn("只有 3 家", zh)
        self.assertIn("不到 10 家", zh)
        en = qs.human_text(_job(3, short={"listed": 3, "unverified": 0}), "done", [], "en", None)
        self.assertIn("fewer than 10", en)

    def test_the_result_block_offers_a_free_why_step_for_the_to_confirm_tier(self):
        d = page.build_page_data(_novice_result(), None, lang="zh", local_names=LOCAL)
        short = short_list.of(d)
        self.assertEqual((short["listed"], short["unverified"]), (3, 5))
        self.assertEqual(short["examples_en"][:2], ["Unverio 0", "Unverio 1"])
        steps = short_list.steps(short, "scr-n")
        [st] = steps
        self.assertEqual(st["cost_usd"], 0.0)
        self.assertEqual(st["command"], "jevscreen why UV0 --run scr-n --json")
        self.assertIn("5 家", st["text_zh"])
        self.assertIsNone(CJK.search(st["text_en"]))
        many = dict(d, rows=d["rows"] * 4)
        self.assertIsNone(short_list.of(many))


COMPLETE_ZH, COMPLETE_EN = "就这么多", "that is how many"


def _unchecked_result(statuses=("skipped_budget", "skipped_budget", "failed"), status="ok"):
    res = _novice_result()
    res["status"] = status
    for u, st in zip(res["unverified"], statuses):
        u["l2_status"] = st
    return res


class TestAShortListIsCompleteOnlyWhenEveryCompanyWasChecked(unittest.TestCase):
    """Review of novice #3: 'that is how many state it; I did not pad it' was said on a partial or running run, and
    when the to-confirm list held companies the check never read (budget ran out, the check failed)."""
    SHORT = TestShortListIsSaidPlainly.SHORT

    def test_a_partial_or_running_run_does_not_say_the_list_is_complete(self):
        for status in ("partial", "running"):
            for lang, word in (("zh", COMPLETE_ZH), ("en", COMPLETE_EN)):
                with self.subTest(status=status, lang=lang):
                    t = qs.human_text(_job(4, short={**self.SHORT, "listed": 4, "unverified": 12}), status, [], lang,
                                      None)
                    self.assertNotIn(word, t)
                    self.assertIn(short_list.TEXT[lang]["may_grow"], t)
                    if lang == "en":
                        self.assertIsNone(CJK.search(short_list.text(
                            _job(4, short={**self.SHORT, "listed": 4, "unverified": 12})["result"], "en", status)))

    def test_companies_the_check_never_read_are_counted_and_the_list_is_not_called_complete(self):
        d = page.build_page_data(_unchecked_result(), None, lang="zh", local_names=LOCAL)
        short = short_list.of(d)
        self.assertEqual(short["unchecked"], 3)
        self.assertFalse(short["complete"])
        job = _job(3, short=short)
        zh = qs.human_text(job, "done", [], "zh", None)
        self.assertNotIn(COMPLETE_ZH, zh)
        self.assertNotIn("核对时原文没写明", zh)
        self.assertIn("3 家还没核对", zh)
        en = qs.human_text(job, "done", [], "en", None)
        self.assertNotIn(COMPLETE_EN, en)
        self.assertIn("3 were not checked", en)

    def test_a_checked_run_still_says_it_plainly(self):
        d = page.build_page_data(_unchecked_result(statuses=("insufficient", "contradicted", "no_excerpt")), None,
                                 lang="zh", local_names=LOCAL)
        short = short_list.of(d)
        self.assertEqual((short["unchecked"], short["complete"]), (0, True))
        self.assertIn(COMPLETE_ZH, qs.human_text(_job(3, short=short), "done", [], "zh", None))

    def test_the_page_says_the_same(self):
        for res, why in ((_unchecked_result(), "not checked"),
                         (_unchecked_result(statuses=(), status="partial"), "a partial run")):
            for lang, word in (("zh", COMPLETE_ZH), ("en", COMPLETE_EN)):
                with self.subTest(why=why, lang=lang):
                    d = page.build_page_data(res, None, lang=lang, local_names=LOCAL)
                    txt = page.render_text(d, lang)
                    self.assertNotIn(word, txt)
                    self.assertIn(page.STRINGS[lang]["short_note_open"].split("{")[0], txt)
                    if shutil.which("node"):
                        dom = dom_text(self, page.render_page(d))["text"]
                        self.assertNotIn(word, dom)
                        self.assertIn(page.STRINGS[lang]["short_note_open"].split("{")[0], dom)


class TestShortListOnThePage(unittest.TestCase):
    def test_the_page_says_the_list_is_short_and_points_at_the_to_confirm_section(self):
        for lang in ("zh", "en"):
            d = page.build_page_data(_novice_result(), None, lang=lang, local_names=LOCAL)
            want = page.STRINGS[lang]["short_note"].format(n=3, u=5)
            self.assertIn(want, page.render_text(d, lang))
            if lang == "en":
                self.assertIsNone(CJK.search(want))
            if shutil.which("node"):
                self.assertIn(want, dom_text(self, page.render_page(d))["text"])


# ------------------------------------------------------------------------------------------------ scope defaults

DEFAULT = {"sid": "s1", "family": "role", "value": "buyer", "kind": "role.buyer", "because": "核心零部件供应商", "x": 1,
           "security_ids": ["SZSE:002527"], "token": "s1=yes",
           "text_zh": "按你的原话「核心零部件供应商」，AI 读摘录后认为是灵巧手零部件的买方或使用者的 1 家已移出（如 新时达）；"
                      "要留着就说「s1 要」。",
           "text_en": "Going by your own words \"核心零部件供应商\", the AI read the excerpts and removed 1 that buy or use "
                      "dexterous-hand parts (e.g. Step Electric); to keep them, say \"s1 keep\"."}


class TestScopeDefaultsAreVisible(unittest.TestCase):
    def test_the_page_slot_carries_each_default_with_its_undo_line(self):
        rv = {"agent_review": {"state": "done"}, "defaults": [DEFAULT], "questions": [], "escalations": []}
        for lang in ("zh", "en"):
            q = review_cli.page_block(rv, "scr-9", lang)
            [d] = q["defaults"]
            self.assertEqual(d["undo"], 'jevscreen decide "s1=yes" --run scr-9 --via page')
            self.assertEqual(d["text"], DEFAULT[f"text_{lang}"])

    def test_the_page_shows_the_default_with_a_copy_undo_button(self):
        d = page.build_page_data(_novice_result(), None, lang="zh", local_names=LOCAL, extra={
            "questions": review_cli.page_block({"agent_review": {"state": "done"}, "defaults": [DEFAULT]}, "scr-n",
                                               "zh")})
        txt = page.render_text(d, "zh")
        self.assertIn(page.STRINGS["zh"]["sec_defaults"], txt)
        self.assertIn('jevscreen decide "s1=yes" --run scr-n --via page', txt)
        if shutil.which("node"):
            dom = dom_text(self, page.render_page(d))["text"]
            self.assertIn(page.STRINGS["zh"]["sec_defaults"], dom)
            self.assertIn(page.STRINGS["zh"]["undo_default"], dom)
            self.assertIn("新时达", dom)
            self.assertLess(dom.index(page.STRINGS["zh"]["sec_defaults"]), dom.index("#1"))   # above the results

    def test_the_chat_names_it_right_after_the_counts_with_the_undo_reply(self):
        job = _job(12)
        job["review"] = {"run_id": "scr-x", "defaults": [DEFAULT], "questions": [], "escalations": []}
        view = qs.review_view(job, [])
        [vd] = view["scope"]["defaults"]
        self.assertEqual(vd["undo_command"], review_cli.decide_command("scr-x", "s1=yes"))
        zh = qs.human_text(job, "done", [], "zh", None, view=view).splitlines()
        i = next(k for k, ln in enumerate(zh) if "新时达" in ln)
        self.assertLessEqual(i, 2, zh)                            # right after the done line, not at the bottom
        self.assertTrue(zh[i].startswith(qs.STRINGS["zh"]["default_label"]), zh[i])
        self.assertIn("「s1 要」", zh[i])
        en = qs.human_text(job, "done", [], "en", None, view=view).splitlines()
        j = next(k for k, ln in enumerate(en) if "Step Electric" in ln)
        self.assertTrue(en[j].startswith(qs.STRINGS["en"]["default_label"]), en[j])
        self.assertIsNone(CJK.search(qs.STRINGS["en"]["default_label"]))


if __name__ == "__main__":
    unittest.main()
