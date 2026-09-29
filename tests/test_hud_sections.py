"""The confirmed list and the to-confirm section on the HUD page (ported from feat/no-padding, page side only): rows
that carry shortlist_tier ('high' / 'confirm') and the promotion marker (main_via) render as two sections in the page,
its text and the results panel; the amber grains and the final sieve count the main list only; a row without a
tier on a page with the sections is to confirm; a result whose rows carry no tier keeps the single list. Invented
issuers only."""
from __future__ import annotations

import copy
import re
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)
from jevscreen import page, page_hud, page_sand, screen  # noqa: E402
from test_page import _novice_result, dom_text  # noqa: E402

CJK = re.compile(r"[㐀-鿿]")
LOCAL = {"SZSE:399101": "青澜科技", "SSE:699102": "百峰铝业", "SZSE:399103": "禾源控制", "SZSE:399104": "岚川储能",
         "SZSE:399105": "北屿热管理", "SSE:699106": "澄江新能"}


def split_result(tiers: bool = True) -> dict:
    """Three confirmed rows (the third promoted by your AI's review) and three to confirm, one of them moved out by
    your AI. The page settles a result that carries a shortlist (shortlist.settle), so the labels decide the tiers:
    high = L1 core + L2 explicit, your AI's applied explicit yes promotes, its applied no demotes."""
    r = copy.deepcopy(_novice_result())
    rows = r["rows"]
    rows[0].update(l2_label="explicit")
    # the second confirmed row's excerpt names the idea (a high row whose excerpt does not is to confirm: in_main)
    rows[1].update(l1_label="core", l2_label="explicit",
                   evidence_excerpt="公司为储能电站液冷温控系统提供铝制液冷板和管路，是核心供应商。")
    rows[2].pop("user_verdict")
    rows[2].pop("verdict_source")
    rows[2].update(l2_label="partial", evidence_excerpt="公司为储能系统提供液冷温控阀组。")
    if tiers:                                  # your AI's applied explicit yes, citing a sentence: promoted
        rows[2].update(agent_verdict="yes", agent_level="explicit", agent_state="applied", agent_quote_ids=[1])
    for i, (sid, name) in enumerate([("SZSE:399104", "Lanchuan Storage"), ("SZSE:399105", "Beiyu Thermal"),
                                     ("SSE:699106", "Chengjiang Energy")], start=4):
        rows.append({"rank": i, "security_id": sid, "company_key": f"k{i}", "name": f"{name} Co. Ltd. Class A",
                     "country": "China", "market_cap_usd": 1.1e9, "l1_label": "adjacent", "l2_label": "partial",
                     "l2_evidence": "profile", "evidence_excerpt": "公司产品包括储能温控设备。"})
    if tiers:
        for row, tier in zip(rows, ("high", "high", "high", "confirm", "confirm", None)):
            if tier:
                row["shortlist_tier"] = tier
        rows[2]["main_via"] = "agent"
        rows[3].update(l1_label="core", l2_label="explicit", main_via="agent_no", agent_verdict="no",
                       agent_state="applied", agent_why_zh="原文只写了电池", agent_why_en="the text only names batteries")
        r["shortlist"] = {"by": "l1l2"}
    return r


def data_of(lang: str = "zh", tiers: bool = True) -> dict:
    return page.build_page_data(split_result(tiers), None, lang=lang, local_names=LOCAL)


class TestSectionsData(unittest.TestCase):
    def test_rows_split_into_the_main_list_and_the_to_confirm_section(self):
        d = data_of()
        self.assertEqual(d["shortlist"], {"high": 3, "confirm": 3, "agent": 1})
        main, confirm = page.main_and_confirm(d)
        self.assertEqual([r["rank"] for r in main], [1, 2, 3])
        self.assertEqual([r["rank"] for r in confirm], [4, 5, 6])
        # a row written without a tier gets one when the page settles the result (shortlist.settle)
        self.assertEqual(next(r for r in confirm if r["ticker"] == "699106")["tier"], "confirm")
        self.assertEqual(main[2]["main_via"], "agent")
        self.assertEqual(page.summary_of(d, None)["main"], 3)
        self.assertEqual([t["rank"] for t in page.top_rows(d)], [1, 2, 3])
        self.assertTrue(page.top_rows(d)[2]["checked_by_agent"])

    def test_rows_without_tiers_keep_the_single_list(self):
        d = data_of(tiers=False)
        self.assertIsNone(d["shortlist"])
        self.assertEqual(len(page.main_and_confirm(d)[0]), 6)
        self.assertEqual(page_hud.panel(d)[2], "结果 · 6 家")
        self.assertNotIn("待核对", page.render_text(d, "zh"))

    def test_the_gold_and_the_final_sieve_are_the_main_list_only(self):
        d = data_of()
        self.assertEqual(page.result_facts(d)["funnel"]["listed"], 3)
        d["live"] = {"sand": {"stage": 4, "state": "done", "final": 3}}
        picks = page_sand.picks(d, 10)
        self.assertEqual([p[2] for p in picks], [1, 2, 3])
        html = page_sand.markup(d)
        self.assertIn('data-dots="3"', html)

    def test_panel_head_and_text_in_one_language(self):
        zh, en = data_of("zh"), data_of("en")
        self.assertEqual(page_hud.panel(zh)[2], "结果 · 确认 3 家 · 待核对 3")
        self.assertEqual(page_hud.panel(en)[2], "Results · 3 confirmed · 3 to confirm")
        tz, te = page.render_text(zh, "zh"), page.render_text(en, "en")
        self.assertIn("确认 3 家；另有 3 家待核对。", tz)
        self.assertIn("待核对（3 家，不算入选）", tz)
        self.assertIn("#3 禾源控制", tz)
        self.assertIn("你的 AI 核对", tz)
        self.assertIn("- 岚川储能", tz)                         # to confirm: never numbered with the main list
        self.assertLess(tz.index("#3 禾源控制"), tz.index("待核对（3 家"))
        self.assertIn("To confirm (3; not in the confirmed list)", te)
        self.assertIn("checked by your AI", te)
        hud_en = page_hud.markup({**en, "live": {"sand": {"state": "done"}}})
        self.assertIsNone(CJK.search(hud_en))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestSectionsRendered(unittest.TestCase):
    DRIVE = r"""
function ids(n,out){if(n.tagName==='details'&&String(n.className).indexOf('row')>=0)out.push(n.id);n.children.forEach(c=>ids(c,out));return out;}
const inSec=new Set();all.filter(n=>String(n.className).indexOf('secondary')>=0).forEach(s=>ids(s,[]).forEach(i=>inSec.add(i)));
const rows_=all.filter(n=>n.tagName==='details'&&String(n.className).indexOf('row')>=0);
const bad=rows_.filter(n=>inSec.has(n.id)!==(Number(String(n.id).slice(1))>3)).map(n=>n.id);
if(rows_.length!==6||bad.length){console.error('sections wrong: '+rows_.length+' '+bad.join(','));process.exit(3);}
"""

    def render(self, lang):
        html = page.render_page(data_of(lang))
        out = dom_text(self, html, extra_js=self.DRIVE)   # exits non-zero unless rows 4-6 sit in the to-confirm box
        return html, out

    def test_the_page_draws_both_sections_and_the_marker(self):
        html, out = self.render("zh")
        text = out["text"]
        self.assertIn("确认的公司（3 家）", text)
        self.assertIn("待核对（3 家，不算入选）", text)
        self.assertIn("你的 AI 核对", text)
        self.assertIn("你的 AI 认为不符，移到待核对", text)
        self.assertEqual(out["trs"], 4)                           # the top table: the main list only (+ header)
        self.assertEqual(sorted(out["table_ranks"]), ["#1", "#2", "#3"])   # grouped by verdict
        self.assertNotRegex(text, r"\{[a-z_]+\}")
        self.assertIn(page.CSP, html)

    def test_to_confirm_rows_have_no_pill_but_keep_their_reason_chips(self):
        html, out = self.render("zh")
        self.assertNotIn("confirm_mark", html)
        self.assertNotIn("rank muted tc", html)
        text = out["text"]
        sec = text[text.index("待核对（3 家，不算入选）"):]
        self.assertNotIn("\n待核对\n", sec)                   # no 待核对 pill before a to-confirm row's name
        self.assertIn("你的 AI 认为不符，移到待核对", sec)       # the reason chips stay
        self.assertIn("摘录没提到你的想法", page.STRINGS["zh"]["confirm_note"])
        self.assertIn("the excerpt shown does not mention the idea", page.STRINGS["en"]["confirm_note"])
        self.assertIn(".rank.tc{display:none}", html)             # the plain page: no empty rank cell
        self.assertIn(page_hud.P + " section.secondary .rank{display:block", page_hud.CSS)   # the panel's dot

    def test_english_page_has_no_chinese_words_in_its_sections(self):
        _html, out = self.render("en")
        for line in out["text"].split("\n"):
            if any(w in line for w in ("Confirmed", "To confirm", "checked by your AI", "moved to confirm")):
                self.assertIsNone(CJK.search(line), line)


class TestGapNeverConfirmed(unittest.TestCase):
    """A high row whose shown excerpt does not mention the idea (the page would call it 边缘 / gap) is to confirm, in
    the data (shortlist.settle, shown_gap), the text, the panel head, the gold and the rendered page; page data written
    before that rule (a stale tier) is split the same way by the page's own check (in_main / inMain); the human's own
    yes keeps it listed."""
    GAP = "699102"

    @staticmethod
    def gap_result(pin: str | None = None) -> dict:
        r = split_result()
        r["rows"][1]["evidence_excerpt"] = "铝压延加工业是将电解铝通过熔铸、轧制等工艺生产铝材的过程。"
        r["rows"][1]["main_via"] = "agent"          # even your AI's promotion does not outrank the shown text
        if pin == "user":                           # the human's own yes
            r["rows"][1].update(user_verdict="partial", verdict_source="user")
        return r

    def data(self, lang="zh", pin=None):
        return page.build_page_data(self.gap_result(pin), None, lang=lang, local_names=LOCAL)

    def gap_row(self, d):
        return next(r for r in d["rows"] if r["ticker"] == self.GAP)

    def test_a_gap_row_moves_to_confirm_everywhere(self):
        for lang in ("zh", "en"):
            d = self.data(lang)
            row = self.gap_row(d)
            self.assertIs(row["quote"]["mentions"], False)
            self.assertEqual((row["tier"], row["main_via"]), ("confirm", None))
            main, confirm = page.main_and_confirm(d)
            self.assertEqual([r["ticker"] for r in main], ["399101", "399103"])
            self.assertIn(self.GAP, [r["ticker"] for r in confirm])
            self.assertEqual(d["shortlist"]["high"], 2)
            self.assertEqual([t["rank"] for t in page.top_rows(d)], [1, 2])
            d["live"] = {"sand": {"stage": 4, "state": "done", "final": 2}}
            self.assertEqual([p[2] for p in page_sand.picks(d, 10)], [1, 2])     # gold = confirmed rows only
        zh = self.data("zh")
        self.assertEqual(page_hud.panel(zh)[2], "结果 · 确认 2 家 · 待核对 4")
        text = page.render_text(zh, "zh")
        main_part = text[:text.index("待核对（4 家")]
        self.assertNotIn("边缘", main_part)
        self.assertNotIn("未提到", main_part)
        self.assertIn("- 百峰铝业", text[text.index("待核对（4 家"):])

    def test_saved_data_with_a_stale_tier_is_split_the_same_way(self):
        d = self.data()
        row = self.gap_row(d)
        row["tier"] = "high"                                  # page data written before this rule
        self.assertFalse(page.in_main(row))
        self.assertEqual([r["ticker"] for r in page.main_and_confirm(d)[0]], ["399101", "399103"])

    def test_the_human_yes_still_wins(self):
        d = self.data(pin="user")
        self.assertIn(self.GAP, [r["ticker"] for r in page.main_and_confirm(d)[0]])
        self.assertEqual(len(page.main_and_confirm(d)[0]), 3)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_rendered_panel_has_no_gap_verdict_in_the_confirmed_list(self):
        d = self.data()
        row = self.gap_row(d)
        row["tier"] = "high"                                  # the script decides the same way on its own
        drive = r"""
const sec=all.filter(n=>String(n.className).indexOf('secondary')>=0)[0];
function has(n,x){if(n===x)return true;return n.children.some(c=>has(c,x));}
const rows_=all.filter(n=>n.tagName==='details'&&String(n.className).indexOf('row')>=0);
const outside=rows_.filter(n=>!has(sec,n)).map(n=>n.id);
if(outside.join(',')!=='r1,r2'){console.error('confirmed rows wrong: '+outside.join(','));process.exit(3);}
"""
        out = dom_text(self, page.render_page(d), extra_js=drive)
        self.assertEqual(sorted(out["table_ranks"]), ["#1", "#2"])
        self.assertNotIn("边缘", " ".join(out["table_verdicts"]))
        self.assertIn(page.STRINGS["zh"]["main_hint"], out["text"])


class TestReviewV2Rendered(unittest.TestCase):
    """Review deck v2 on the merged page: your AI's applied explicit yes citing a stored-filing sentence the excerpt
    left out (agent_quotes) promotes a row whose excerpt says nothing of the idea; the confirmed list shows that
    sentence and the promotion marker (the panel keeps .badge.via), in the text view and in the rendered page (the
    HUD panel is the same DOM, skinned)."""
    CITED = "公司的液冷温控阀组已批量用于储能电站。"

    def data(self, lang="zh"):
        r = split_result()
        row = r["rows"][2]
        text = "[annual report]\n\n公司专注于阀门的研发与销售。"
        row.update(evidence_excerpt="公司专注于阀门的研发与销售。", evidence_sha=screen.evidence_sha(text),
                   agent_quote_ids=[5], agent_quotes={"5": self.CITED})
        pieces = {row["company_key"]: {"sha": row["evidence_sha"], "text": text,
                                       "pieces": ["公司专注于阀门的研发与销售。"], "overview": None}}
        return page.build_page_data(r, None, lang=lang, local_names=LOCAL, l2_pieces=pieces)

    def test_the_promoted_row_is_confirmed_with_the_cited_sentence(self):
        d = self.data()
        row = next(r for r in d["rows"] if r["ticker"] == "399103")
        self.assertEqual((row["tier"], row["main_via"], row["verdict"]), ("high", "agent", "explicit"))
        self.assertEqual(row["quote"]["text"], self.CITED)
        self.assertIn(row, page.main_and_confirm(d)[0])
        text = page.render_text(d, "zh")
        self.assertIn("#3 禾源控制 399103 · 中国 — 明确 · 你的 AI 核对", text)
        self.assertIn("- 岚川储能 399104 · 中国 — 相关 · 你的 AI 认为不符，移到待核对", text)
        self.assertIn("Results · 3 confirmed · 3 to confirm", page_hud.panel(self.data("en"))[2])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_rendered_page_quotes_it_in_the_confirmed_list(self):
        html = page.render_page(self.data())
        self.assertIn("badge via", html)
        out = dom_text(self, html)
        text = out["text"]
        confirmed = text[:text.index("待核对（3 家，不算入选）")]
        self.assertIn(self.CITED, confirmed)
        self.assertIn("你的 AI 核对", confirmed)
        self.assertIn(page_hud.P + " details.row .badge.via{display:inline", page_hud.CSS)


class TestPanelSkin(unittest.TestCase):
    def test_the_panel_skin_styles_the_sections(self):
        css = page_hud.CSS
        p = page_hud.P
        self.assertIn(p + " section.results>section.secondary{order:2", css)
        self.assertIn(p + " details.row .badge.via{display:inline", css)
        self.assertIsNone(re.search(r"(^|[},])P[ >{]", css, re.M))   # every panel rule is scoped to the panel

    def test_the_panel_head_is_solid_and_the_open_sheet_hides_the_corner_buttons(self):
        css = page_hud.CSS
        head = re.search(re.escape(page_hud.P) + r">\.hud-ph\{display:flex;position:sticky[^}]*\}", css).group(0)
        self.assertIn("background:#0a0a09", head)                # scrolled rows never show through the head
        phone = css[css.index("@media screen and (max-width:719px)"):]
        self.assertRegex(phone, r"html\.hud-on\.hud-full:not\(\.hud-text\) \.hud>\.hud-tv,html\.hud-on\.hud-full:"
                                r"not\(\.hud-text\) \.hud>\.hud-reset\{visibility:hidden")

    def test_the_title_ends_before_the_instrument_corner(self):
        css = page_hud.CSS
        self.assertIn(".hud-title{left:16px;top:14px;max-width:calc(100vw - 46px - var(--hud-iw,184px) - "
                      "var(--hud-pr,0px))", css)
        self.assertNotIn("max-width:calc(100vw - 150px)", css)            # the phone keeps the measured corner
        self.assertIn("--hud-iw", page_hud.JS)

    def test_the_confirmed_word_is_short_and_its_note_says_once_it_is_the_ai_reading(self):
        self.assertEqual(page.STRINGS["zh"]["verdict_main"], "原文写明")
        self.assertEqual(page.STRINGS["en"]["verdict_main"], "Stated")
        self.assertIsNone(CJK.search(page.STRINGS["en"]["main_hint"]))
        self.assertIn(page_hud.P + " section.results>p.mainhint{display:block", page_hud.CSS)
        text = page.render_text(data_of("zh"), "zh")
        self.assertNotIn("原文写明（", text)
        self.assertIn("#2 百峰铝业 699102 · 中国 — 原文写明", text)


if __name__ == "__main__":
    unittest.main()
