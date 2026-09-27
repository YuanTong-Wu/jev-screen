"""Tests for jevscreen.page (the result page), `jevscreen page`, the page hooks after screen / cards / answer, and
calib.answer_tokens (the page's answer tokens round-trip through parse_answers).

No network and no paid calls: the screen runs on test_screen's seeded store with FakeJev. Invented companies only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, page, quickstart  # noqa: E402
from test_cli_calib import IDEA, CliCase  # noqa: E402
from test_screen import StoreCase  # noqa: E402

FAKE_KEY = "sk-or-v1-" + "fake" * 12          # a test value (release_check recognises 'fake')


def data_of(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    assert m, "no data block"
    return json.loads(m.group(1))


def deck3(with_g: bool) -> dict:
    chips = list(calib.YES_CHIPS + calib.NO_CHIPS[:4]) + (["g"] if with_g else [])
    no = dict(calib.NO_CHIP_TEXT)
    if with_g:
        no["g"] = "只有大类（身份管理），没提AI代理"
    cards = [{"n": n, "type": "boundary_in", "company_key": f"isin:XX000000000{n}", "security_id": f"NYSE:QQ{n}",
              "name": f"Quillon {n}", "rank": n, "pi": 0.5, "what": {"text": "Makes things.", "source": "公司简介"},
              "quote": {"text": "We make things.", "source": "SEC", "form": "10-K", "filing_date": "2026-01-01",
                        "url": "https://www.sec.gov/x.htm"},
              "verdict": {"label": "explicit" if n == 1 else "partial", "p_pos": 0.6, "edge": n == 2},
              "why_zh": "在名单里，但多读几次可能掉出去（入选 50%）", "chips": chips} for n in (1, 2, 3)]
    return {"format": calib.CARDS_FORMAT, "deck_id": "deck-scr-x-1", "run_id": "scr-x", "idea": "q",
            "chips": {"yes": {"a": "直接做", "b": "相关，算大类"}, "no": no}, "cards": cards}


class TestAnswerTokens(unittest.TestCase):
    def test_every_token_round_trips_alone_and_combined(self):
        for with_g in (False, True):
            deck = deck3(with_g)
            toks = calib.answer_tokens(deck)
            self.assertEqual(set(toks), {1, 2, 3})
            self.assertEqual("g" in toks[1], with_g)
            expect = {"yes": ("yes", None), "a": ("yes", "a"), "b": ("yes", "b"), "no": ("no", None),
                      "?": ("unsure", None), **{c: ("no", c) for c in calib.NO_CHIPS}}
            for n, t in toks.items():
                for choice, token in t.items():
                    self.assertRegex(token, r"^\d{1,2}(yes|no|\?|[a-k])$")
                    [a] = calib.parse_answers(token, deck)
                    self.assertEqual((a.n, a.verdict, a.chip), (n, *expect[choice]), token)
            line = " ".join([toks[1]["a"], toks[2]["no"], toks[3]["?"]])
            got = [(a.n, a.verdict, a.chip) for a in calib.parse_answers(line, deck)]
            self.assertEqual(got, [(1, "yes", "a"), (2, "no", None), (3, "unsure", None)])
            cmd = f'jevscreen answer "{line}" --deck {deck["deck_id"]}'
            self.assertEqual(shlex.split(cmd), ["jevscreen", "answer", line, "--deck", "deck-scr-x-1"])

    def test_english_errors(self):
        deck = deck3(False)
        with self.assertRaisesRegex(calib.AnswerError, "There is no card 9"):
            calib.parse_answers("9a", deck, lang="en")
        with self.assertRaisesRegex(calib.AnswerError, "no g"):
            calib.parse_answers("1g", deck, lang="en")
        with self.assertRaisesRegex(calib.AnswerError, "没有第 9 张卡"):
            calib.parse_answers("9a", deck)

    def test_tables_have_the_same_keys(self):
        self.assertEqual(set(calib.WHY_EN), set(calib.WHY_ZH))
        self.assertEqual(set(calib.NO_CHIP_TEXT_EN), set(calib.NO_CHIP_TEXT))
        self.assertEqual(set(calib.ANSWER_ERRORS["zh"]), set(calib.ANSWER_ERRORS["en"]))
        self.assertEqual(set(page.STRINGS["zh"]), set(page.STRINGS["en"]))
        self.assertEqual(set(quickstart.STRINGS["zh"]), set(quickstart.STRINGS["en"]))

    def test_idea_en_problems(self):
        self.assertEqual(calib.idea_en_problems("AI agents that manage identity and access", "AI 代理身份管理"), [])
        self.assertTrue(calib.idea_en_problems("", "x"))
        self.assertTrue(calib.idea_en_problems("a\nb", "x"))
        self.assertTrue(calib.idea_en_problems("x" * 401, "x"))
        self.assertTrue(calib.idea_en_problems("人形机器人的减速器供应商", "人形机器人"))
        probs = calib.idea_en_problems("Suppliers like Quillon Robotics for humanoid robots", "人形机器人",
                                       ["Quillon Robotics Inc"], ["QRBT"])
        self.assertTrue(any("Quillon" in p for p in probs), probs)
        # a name the idea itself contains is allowed
        self.assertEqual(calib.idea_en_problems("Quillon Robotics peers", "Quillon Robotics 同类", ["Quillon Robotics"],
                                                []), [])


class PageCase(CliCase):
    def screen_page(self, *extra):
        rid, od = self.screen_with_deck(*extra)
        return rid, od, (od / "page.html").read_text(encoding="utf-8")


class TestPage(PageCase):
    def test_screen_writes_the_page_after_the_cards(self):
        rid, od, html = self.screen_page()
        d = data_of(html)
        deck = calib.load_deck(od)
        self.assertEqual((d["format"], d["run_id"], d["deck_id"], d["idea"]),
                         (page.PAGE_FORMAT, rid, deck["deck_id"], IDEA))
        self.assertEqual([r["name"] for r in d["rows"]], ["RoboCorp", "Robo Two"])
        robo = d["rows"][0]
        self.assertEqual((robo["verdict"], robo["evidence"], robo["ticker"]), ("explicit", "annual_report", "ROBO"))
        self.assertEqual(robo["quote"]["url"], "https://www.sec.gov/robo.htm")
        self.assertIn("humanoid robots", robo["quote"]["text"].lower())
        self.assertTrue(robo["one_line"])                        # the description's first sentence (store lookup)
        self.assertEqual(len(d["cards"]), len(deck["cards"]))
        self.assertEqual(d["cards"][0]["tokens"], {k: v for k, v in calib.answer_tokens(deck)[1].items()})
        self.assertTrue(page.stable_path(self.cfg, IDEA).exists())
        self.assertEqual(d["gaps"][0]["id"], "no_description")
        for bad in ("<link", "@import", "url(", 'src="http', "<iframe", "<img"):
            self.assertNotIn(bad, html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn('name="referrer" content="no-referrer"', html)
        self.assertNotIn(str(self.home), html)                     # no absolute home path
        self.assertLess(len(html.encode("utf-8")), page.MAX_PAGE_BYTES)

    def test_no_page_flag_and_deterministic_rebuild(self):
        self.write_sieve(target_terms={"en": ["android"]})
        code, _out, err = self.main(["screen", IDEA, "--reads", "1", "--no-page"])
        self.assertEqual(code, 0, err)
        _rid, _p, od = self.last_run()
        self.assertFalse((od / "page.html").exists())
        code, out, err = self.main(["page", "latest", "--json"])
        self.assertEqual(code, 0, err)
        first = (od / "page.html").read_text(encoding="utf-8")
        code, out, err = self.main(["page", str(od), "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual((od / "page.html").read_text(encoding="utf-8"), first)
        self.assertEqual(json.loads(out)["status"], "ok")

    def test_cards_rebuild_carries_the_new_deck_and_answer_accepts_it(self):
        rid, od, _html = self.screen_page()
        old = calib.load_deck(od)["deck_id"]
        # a recorded answer bumps the deck number of the next rebuild
        code, out, err = self.main(["answer", "1a", "--run", rid, "--no-apply"])
        self.assertEqual(code, 0, err)
        code, out, err = self.main(["cards", rid])
        self.assertEqual(code, 0, err)
        d = data_of((od / "page.html").read_text(encoding="utf-8"))
        new = calib.load_deck(od)["deck_id"]
        self.assertNotEqual(new, old)
        self.assertEqual(d["deck_id"], new)
        tok = d["cards"][0]["tokens"]["no"]
        code, out, err = self.main(["answer", tok, "--deck", d["deck_id"], "--no-apply"])
        self.assertEqual(code, 0, err)

    def test_page_failure_only_warns(self):
        self.write_sieve(target_terms={"en": ["android"]})
        with mock.patch.object(page, "render_page", side_effect=RuntimeError("boom")):
            code, _out, err = self.main(["screen", IDEA, "--reads", "1"])
        self.assertEqual(code, 0)
        self.assertIn("result page not written", err)

    def test_a_planted_key_blocks_the_page(self):
        rid, od, _html = self.screen_page()
        res = json.loads((od / "results.json").read_text())
        res["rows"][0]["name"] = f"Leaky {FAKE_KEY}"
        (od / "page.html").unlink()
        warned = []
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_KEY}):
            path, data = page.write_page(self.cfg, od, res, None, warn=warned.append)
        self.assertIsNone(path)
        self.assertFalse((od / "page.html").exists())
        self.assertIn("openrouter", warned[0])
        self.assertNotIn(FAKE_KEY, warned[0])


class TestPageEscaping(unittest.TestCase):
    RESULT = {"run_id": "scr-1", "idea": "robots", "status": "ok", "params": {"min_mcap_usd": 1e9, "max_out": 40},
              "funnel": {"universe": 3, "described": 2, "l1_pass": 1}, "cost_usd": 0.01, "timing": {"total_s": 12},
              "rows": [{"rank": 1, "security_id": "NYSE:EVIL", "company_key": "k1",
                        "name": "</script><script>alert(1)</script>", "country": "United States",
                        "l2_label": "explicit", "l2_evidence": "annual_report", "filing_source": "sec_filing_text",
                        "evidence_excerpt": "line sep   & <b>bold</b> -->", "evidence_url": "javascript:alert(1)"}],
              "unverified": [], "gaps": {}}

    def test_script_breakout_and_links(self):
        data = page.build_page_data(self.RESULT, None, lang="en")
        html = page.render_page(data)
        self.assertEqual(html.count("</script>"), 2)               # the data block's and the code's own
        self.assertNotIn("<script>alert", html)
        self.assertNotIn(" ", html)
        back = data_of(html)
        self.assertEqual(back["rows"][0]["name"], "</script><script>alert(1)</script>")
        self.assertIsNone(back["rows"][0]["quote"]["url"])         # javascript: is never a link
        self.assertEqual(page._safe_url("https://a.example/x?y=1"), "https://a.example/x?y=1")
        for bad in ("javascript:alert(1)", "data:text/html,x", "http://a b", "//x.example", None, 5):
            self.assertIsNone(page._safe_url(bad))

    def test_row_badges_and_limits(self):
        r = dict(self.RESULT["rows"][0], l2_evidence="profile", l2_edge=True, l2_read_note="只读了一次",
                 backfill=True, l2_doc_stale=True)
        self.assertEqual(page._badges(r, None), ["profile_only", "stale", "edge", "read_once", "backfill"])
        many = dict(self.RESULT, rows=[dict(self.RESULT["rows"][0], rank=i) for i in range(1, 60)])
        self.assertEqual(len(page.build_page_data(many, None)["rows"]), page.MAX_ROWS)

    FACETS = {"category": "identity management", "target": "AI agents", "mechanism": "agent identity software"}
    PLACEHOLDERS = ("the target", "the category", "the idea itself")

    def test_english_card_texts_carry_the_decks_facets(self):
        """Review R8: the English chips / why texts used to read 'Only the broad category (the category)...'."""
        sv = calib.new_sieve("q")
        sv["facets"] = dict(self.FACETS)
        self.assertEqual(calib.build_deck({"run_id": "scr-x", "idea": "q"}, {}, sv)["facets_en"], self.FACETS)
        deck = deck3(True)
        deck["facets_en"] = dict(self.FACETS)
        deck["cards"][0]["type"] = "scope"
        data = page.build_page_data(self.RESULT, deck, lang="en")
        en = {k: v["en"] for k, v in data["chips"].items()}
        self.assertEqual(en["g"], "Only the broad category (identity management), no mention of AI agents")
        self.assertEqual(en["a"], "Does it directly (agent identity software)")
        self.assertIn("AI agents", data["cards"][0]["why_en"])
        deck.pop("facets_en")                          # a deck written before: no placeholder words either
        data = page.build_page_data(self.RESULT, deck, lang="en")
        texts = [v["en"] for v in data["chips"].values()] + [c["why_en"] for c in data["cards"]]
        for t in texts:
            for ph in self.PLACEHOLDERS:
                self.assertNotIn(ph, t)
        self.assertEqual(data["chips"]["a"]["en"], "Does it directly")

    def test_a_v11_deck_carries_every_role_chip_in_the_data(self):
        """The `cards` printout offers every 'no' chip of a v1.1 deck (c..k); the page data keeps them all."""
        deck = deck3(False)
        deck["chips"] = calib.deck_chips(None)
        for c in deck["cards"]:
            c["chips"] = list(calib.YES_CHIPS + calib.NO_CHIPS)
        self.assertEqual(set(deck["chips"]["no"]), set("cdefghijk"))
        for lang in ("en", "zh"):
            data = page.build_page_data(self.RESULT, deck, lang=lang)
            self.assertTrue(set("cdefghijk") <= set(data["chips"]), lang)
            self.assertEqual(data["cards"][0]["tokens"]["j"], "1j")

    def drive(self, html, drive):
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "h.js"
            harness.write_text(DOM_SHIM + f"\nconst BLOB={json.dumps(blob)};\n" + "(function(){" + js + "})();\n"
                               + drive, encoding="utf-8")
            r = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_one_page_has_no_card_buttons_and_no_answer_bar(self):
        """Owner decision 2026-09-27 (ONE PAGE): cards are a CLI tool for the user's AI, never a human step on the
        page: no card, no answer button, no answer bar, no 'jevscreen answer' line; one language, no toggle. The
        card data stays in the data block (its plain-word forms and translations serve the `cards` printout)."""
        deck = deck3(False)
        for lang, words in (("zh", ("要", "不要", "不确定", "复制给你的 AI：")), ("en", ("Yes", "No", "Not sure", "Paste this to your AI:"))):
            html = page.render_page(page.build_page_data(self.RESULT, deck, lang=lang))
            probe = ("const btn=all.filter(n=>n.tagName==='button').map(n=>n._text);"
                     "const ta=all.filter(n=>n.tagName==='textarea').length;"
                     "console.log(JSON.stringify({title:document.title,buttons:btn,ta:ta}));")
            out = self.drive(html, probe)
            self.assertEqual(out["ta"], 0, lang)
            for w in words:
                self.assertNotIn(w, out["buttons"], lang)
            self.assertNotIn("中文" if lang == "zh" else "English", out["buttons"])
            self.assertIn("筛选结果" if lang == "zh" else "Screen results", out["title"])
            self.assertNotIn("jevscreen answer", dom_text(self, html)["text"])
            self.assertEqual(len(data_of(html)["cards"]), 3)



def _novice_result() -> dict:
    """A Chinese idea with invented A-share issuers: one excerpt about the idea, one about something else, one row
    the user said yes to, a description that only repeats the name, several reads, and unverified rows."""
    idea = "储能电站液冷温控系统的核心供应商"
    rows = [
        {"rank": 1, "security_id": "SZSE:399101", "company_key": "k1", "name": "Qinglan Thermal Tech Co. Ltd. Class A",
         "country": "China", "market_cap_usd": 2.1e9, "l1_label": "core", "l1_p_core": 0.72, "l2_label": "partial",
         "l2_evidence": "annual_report", "filing_source": "cninfo_annual_report", "filing_form": "annual_report",
         "filing_date": "2026-04-01", "evidence_url": "http://static.cninfo.com.cn/x.PDF", "l2_p_pos": 0.964,
         "evidence_excerpt": "公司为电化学储能系统提供液冷温控解决方案，产品用于储能电站。",
         "l2_read_detail": [{"p_pos": 0.9}, {"p_pos": 0.3}, {"p_pos": 0.8}]},
        {"rank": 2, "security_id": "SSE:699102", "company_key": "k2", "name": "Baifeng Aluminium Co. Ltd. Class A",
         "country": "China", "market_cap_usd": 1.5e9, "l1_label": "adjacent", "l1_p_core": 0.2,
         "l2_label": "partial", "l2_evidence": "annual_report", "filing_source": "cninfo_annual_report",
         "l2_p_pos": 0.99, "evidence_excerpt": "铝压延加工业是将电解铝通过熔铸、轧制等工艺生产铝材的过程。公司是核心供应商。",
         "l2_read_detail": [{"p_pos": 0.99}]},
        {"rank": 3, "security_id": "SZSE:399103", "company_key": "k3", "name": "Heyuan Controls Co. Ltd. Class A",
         "country": "China", "market_cap_usd": 1.2e9, "l2_label": "insufficient", "l2_evidence": "annual_report",
         "user_verdict": "partial", "user_chip": "b", "verdict_source": "user",
         "evidence_excerpt": "公司专注于阀门的研发与销售。"},
    ]
    unv = [{"security_id": f"NYSE:UV{i}", "company_key": f"u{i}", "name": f"Unverio {i} Inc.", "country": "United States",
            "l2_status": "insufficient" if i < 4 else "contradicted"} for i in range(5)]
    return {"run_id": "scr-n", "idea": idea, "idea_en": "Main suppliers of liquid-cooling thermal management "
            "systems for battery energy storage power stations.", "status": "ok",
            "params": {"min_mcap_usd": 1e9, "max_out": 40}, "funnel": {"universe": 9, "described": 8, "l1_pass": 8},
            "terms_by_lang": {"en": ["Main suppliers", "liquid-cooling", "thermal"], "zh": ["储能电站液冷温控系统"]},
            "calibration": {"keywords_weak": {"zh": ["核心供应商"]}},
            "cost_usd": 0.01, "timing": {"total_s": 30}, "rows": rows, "unverified": unv, "gaps": {}}


LOCAL = {"SZSE:399101": "青澜科技", "SSE:699102": "百峰铝业"}


def dom_text(test, html, extra_js="", search=""):
    """Run the page's script in node with the DOM stand-in and return the rendered text (every text node joined
    with newlines) plus a summary of the top table."""
    drive = (r"""
function walk(n,out){if(n._text)out.push(n._text);n.children.forEach(c=>walk(c,out));return out;}
""" + extra_js + r"""
const tbl=all.filter(n=>n.tagName==='table');
const heads=all.filter(n=>n.tagName==='th').map(n=>n._text);
const trs=tbl.length?tbl[0].children.filter(n=>n.tagName==='tr').length:0;
console.log(JSON.stringify({text:walk(app,[]).join('\n'),heads:heads,trs:trs}));
""")
    js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
    win = f"global.window={{location:{{search:{json.dumps(search)}}}}};\n"
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "h.js"
        harness.write_text(DOM_SHIM + win + f"\nconst BLOB={json.dumps(blob)};\n" + "(function(){" + js + "})();\n"
                           + drive, encoding="utf-8")
        r = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=60)
    test.assertEqual(r.returncode, 0, r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestNovicePageFixes(unittest.TestCase):
    def data(self, lang="zh"):
        return page.build_page_data(_novice_result(), deck3(False), lang=lang, local_names=LOCAL,
                                    descriptions={"k1": "Qinglan Thermal Tech. ", "k2": "Baifeng makes aluminium."})

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_rendered_details_use_plain_words_and_no_template_placeholders(self):
        """P0: '读了 {n} 次，{k} 次判为符合' was the label of every row; internal labels core / partial / p̄ leaked."""
        html = page.render_page(self.data("zh"))
        out = dom_text(self, html)
        text = out["text"]
        self.assertNotRegex(text, r"\{[a-z_]+\}")
        self.assertIn("读取次数", text)
        self.assertIn("读了 3 次，2 次判为符合", text)
        self.assertIn("AI 判断符合的把握", text)
        self.assertIn("96%", text)
        for word in ("核心", "相邻", "相关"):
            self.assertIn(word, text)
        lines = set(text.split("\n"))
        for internal in ("core", "adjacent", "partial", "explicit", "0.964", "0.72"):
            self.assertNotIn(internal, lines)
        for internal in ("p̄", "p_core", "初读核心分"):
            self.assertNotIn(internal, text)
        dbg = dom_text(self, html, search="?debug")["text"]
        self.assertIn("初读核心分（调试）", dbg)
        self.assertIn("0.72", dbg.split("\n"))
        en = dom_text(self, page.render_page(self.data("en")))["text"]
        self.assertNotRegex(en, r"\{[a-z_]+\}")
        self.assertIn("Read 3 times, 2 judged a fit", en)
        self.assertIn("How sure the AI is that it fits", en)
        self.assertNotIn("p̄", en)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_top_table_names_and_grouped_unverified_on_the_page(self):
        out = dom_text(self, page.render_page(self.data("zh")))
        self.assertEqual(out["heads"], ["#", "公司", "结论", "证据"])
        self.assertEqual(out["trs"], 1 + 3)
        text = out["text"]
        self.assertIn("青澜科技", text)
        self.assertNotIn("Qinglan Thermal Tech", text)          # an official Chinese name: no English name
        self.assertNotIn("Class A", text)
        self.assertIn("年报摘录未提到（缺口）", text)
        self.assertIn("年报没提到", text)                               # the table's evidence cell
        self.assertIn("4 家：年报/简介说得不够清楚", text)
        self.assertIn("1 家：年报/简介里没找到支持", text)
        self.assertEqual(text.count("年报/简介说得不够清楚"), 1)           # merged, not one line per company

    def test_evidence_that_does_not_mention_the_idea_is_a_gap_and_borderline(self):
        rows = {r["rank"]: r for r in self.data()["rows"]}
        self.assertTrue(rows[1]["quote"]["mentions"])
        self.assertEqual(rows[1]["badges"], [])
        self.assertFalse(rows[1]["edge"])
        # aluminium rolling: none of 储能 / 电站 / 液冷 / 温控 / thermal …; '核心供应商' is weak and generic
        self.assertIs(rows[2]["quote"]["mentions"], False)
        self.assertEqual(rows[2]["badges"][0], "no_mention")
        self.assertTrue(rows[2]["edge"])
        # the user said yes: their call stands, not marked
        self.assertNotIn("no_mention", rows[3]["badges"])
        self.assertEqual(page.summary_of(self.data(), None)["no_mention"], 1)
        terms = page.idea_terms(_novice_result())
        for t in ("储能", "电站", "液冷", "温控", "thermal", "liquid-cooling"):
            self.assertIn(t, terms)
        for t in ("系统", "核心", "心供", "控系", "Main", "suppliers", "核心供应商"):
            self.assertNotIn(t, terms)
        self.assertEqual(page.idea_words_shown(_novice_result(), "zh")[:4], ["储能", "电站", "液冷", "温控"])
        # English: the plural / hyphen forms of the idea's words count
        en = {"idea": "humanoid robots", "rows": [], "terms_by_lang": {"en": ["humanoid robot"]}}
        self.assertTrue(page.mentions_idea("We build humanoid robots for plants.", page.idea_terms(en)))
        self.assertEqual(page.mentions_idea("We roll aluminium sheet.", page.idea_terms(en)), [])
        self.assertIsNone(page.mentions_idea("", page.idea_terms(en)))

    def test_names_one_line_and_top_rows(self):
        d = self.data("zh")
        r1 = d["rows"][0]
        self.assertEqual((r1["name"], r1["name_zh"]), ("Qinglan Thermal Tech Co. Ltd.", "青澜科技"))
        # the description only repeats the name: the excerpt's first sentence says what it does
        self.assertEqual(r1["one_line"], "公司为电化学储能系统提供液冷温控解决方案，产品用于储能电站。")
        self.assertEqual(d["rows"][1]["one_line"], "Baifeng makes aluminium.")
        top = page.top_rows(d, 10)
        self.assertEqual((top[0]["name"], top[0]["name_en"]), ("青澜科技", "Qinglan Thermal Tech Co. Ltd."))
        self.assertEqual(top[1]["excerpt_mentions_idea"], False)
        self.assertEqual(page.top_rows(self.data("en"), 1)[0]["name"], "Qinglan Thermal Tech Co. Ltd.")
        self.assertEqual(page._clean_name("Foo Industries Co., Ltd. Class B"), "Foo Industries Co., Ltd.")
        self.assertEqual(page._clean_name("Classic Brands Inc."), "Classic Brands Inc.")

    def test_plain_text_list_and_noscript(self):
        d = self.data("zh")
        text = page.render_text(d)
        self.assertIn("#1 青澜科技 399101 · 中国 — 相关 · 年报原文", text)
        self.assertIn("做什么：Baifeng makes aluminium.（原文，未翻译）", text)
        self.assertIn("#2 百峰铝业", text)
        self.assertIn("年报摘录未提到（缺口） [边缘：摘录没提到你的想法", text)
        self.assertIn("4 家：年报/简介说得不够清楚", text)
        self.assertNotRegex(text, r"\{[a-z_]+\}")
        html = page.render_page(d)
        m = re.search(r"<noscript>(.*?)</noscript>", html, re.S)
        self.assertIn("#1 青澜科技", m.group(1))
        en = page.render_text(self.data("en"))
        self.assertIn("#1 Qinglan Thermal Tech Co. Ltd. 399101 · China — Related · annual report", en)
        self.assertNotIn("青澜", en)                               # an English page shows English names only
        # untrusted names are escaped inside <noscript>
        evil = page.render_page(page.build_page_data(TestPageEscaping.RESULT, None, lang="en"))
        self.assertEqual(evil.count("</script>"), 2)
        self.assertIn("&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;", evil)

    def test_unverified_groups(self):
        self.assertEqual(self.data()["unverified_groups"],
                         [{"key": "st_insufficient", "n": 4}, {"key": "st_contradicted", "n": 1}])


class TestNoviceReviewFixes(unittest.TestCase):
    """Review of the novice-page fixes: the gap check reads all the text the AI read, the user's call is marked in
    every view, profile gaps say 'profile', form ids are plain words, the named words are the checked ones."""

    CONTEXT = "公司积极拓展储能温控业务，在储能领域已量产系列化液冷机组，交付行业头部客户。"

    def pieces(self, sha=None):
        return {"k2": {"sha": sha, "pieces": [_novice_result()["rows"][1]["evidence_excerpt"],
                                              "公司主要产品为铝板带箔。" * 3, self.CONTEXT]}}

    def data(self, lang="zh", **kw):
        return page.build_page_data(_novice_result(), None, lang=lang, local_names=LOCAL, **kw)

    def test_a_context_piece_that_names_the_idea_is_the_quote_not_a_gap(self):
        rows = {r["rank"]: r for r in self.data(l2_pieces=self.pieces())["rows"]}
        q = rows[2]["quote"]
        self.assertIs(q["mentions"], True)
        self.assertIn("储能温控业务", q["text"])
        self.assertNotIn("no_mention", rows[2]["badges"])
        self.assertFalse(rows[2]["edge"])
        text = page.render_text(self.data(l2_pieces=self.pieces()))
        self.assertNotIn("年报摘录未提到（缺口）", text)
        # the run folder's l2_inputs.jsonl is read by default
        with tempfile.TemporaryDirectory() as tmp:
            res = dict(_novice_result(), output_dir=tmp)
            res["rows"][1]["evidence_sha"] = "aaaa"
            line = {"company_key": "k2", "evidence_sha": "aaaa", "evidence": "annual_report", "text": "[x]\nbody",
                    "excerpts": [{"kind": "overview", "text": res["rows"][1]["evidence_excerpt"]},
                                 {"kind": "context", "text": self.CONTEXT}]}
            (Path(tmp) / "l2_inputs.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
            d = page.build_page_data(res, None)
            self.assertIs(d["rows"][1]["quote"]["mentions"], True)
            self.assertEqual(page.load_l2_pieces(tmp, ["k2"])["k2"]["pieces"][1], self.CONTEXT)
            self.assertEqual(page.load_l2_pieces(Path(tmp) / "missing"), {})
        # another document (evidence_sha differs): not used, still a gap
        res = _novice_result()
        res["rows"][1]["evidence_sha"] = "bbbb"
        d = page.build_page_data(res, None, l2_pieces=self.pieces(sha="cccc"))
        self.assertIs(d["rows"][1]["quote"]["mentions"], False)
        # none of the text the AI read names it: a gap
        d = page.build_page_data(_novice_result(), None, l2_pieces={"k2": {"sha": None, "pieces": ["铝箔。"]}})
        self.assertIn("no_mention", d["rows"][1]["badges"])

    def test_the_quote_window_starts_at_the_sentence(self):
        long = "甲" * 400 + "。公司积极拓展储能温控业务。" + "乙" * 400
        w = page._window(long, long.index("公司"))
        self.assertTrue(w.startswith("…公司积极拓展储能温控业务"), w[:30])
        self.assertLessEqual(len(w), page.QUOTE_CHARS)

    def test_the_users_call_is_marked_in_the_text_list_and_top_rows(self):
        d = self.data("zh")
        text = page.render_text(d)
        line = [ln for ln in text.splitlines() if ln.startswith("#3 ")][0]
        self.assertIn("相关 · 按你的判断（AI 没从原文确认）", line)
        self.assertIn("你的判断]", line)
        self.assertNotIn("年报原文", line)
        en = [ln for ln in page.render_text(self.data("en")).splitlines() if ln.startswith("#3 ")][0]
        self.assertIn("your call (the AI did not confirm it from the text)", en)
        top = page.top_rows(d)
        self.assertEqual((top[2]["user"], top[2]["verdict_from_user"]), (True, True))
        self.assertIs(top[2]["excerpt_mentions_idea"], False)      # the valve excerpt does not name the idea
        self.assertEqual((top[0]["user"], top[0]["verdict_from_user"]), (False, False))
        self.assertEqual(page.summary_of(d, None)["no_mention"], 1)   # the user's row is not counted as a gap

    def profile_result(self):
        res = _novice_result()
        res["rows"].append({"rank": 4, "security_id": "NYSE:ZZQ", "company_key": "k4", "name": "Zeta Pumpworks Inc.",
                            "country": "United States", "l2_label": "partial", "l2_evidence": "profile",
                            "evidence_excerpt": "Zeta Pumpworks makes water pumps for farms."})
        return res

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_top_table_says_profile_for_a_profile_gap_and_user_for_the_users_call(self):
        d = page.build_page_data(self.profile_result(), None, lang="zh", local_names=LOCAL)
        self.assertIs(d["rows"][3]["quote"]["mentions"], False)
        text = dom_text(self, page.render_page(d))["text"]
        self.assertIn("简介没提到", text)
        self.assertEqual(text.count("年报没提到"), 1)                 # only the aluminium row
        self.assertIn("按你的判断（AI 没从原文确认）", text)
        self.assertIn("这一家是按你的判断列入的", text)
        en = dom_text(self, page.render_page(page.build_page_data(self.profile_result(), None, lang="en")))["text"]
        self.assertIn("not in the profile", en)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_form_ids_are_plain_words(self):
        for lang, want in (("zh", "出处：巨潮资讯 · 年报 · 2026年4月1日"),
                           ("en", "Source: CNINFO · annual report · 1 Apr 2026")):
            text = dom_text(self, page.render_page(page.build_page_data(_novice_result(), deck3(False), lang=lang)))[
                "text"]
            self.assertIn(want, text)
            for word in ("annual_report", "filing_form", "出处: filing", "Source: filing", "2026-04-01"):
                self.assertNotIn(word, text, lang)
            # a card's SEC form (the cards are not on the page; their data serves the `cards` printout)
            cards = page.build_page_data(_novice_result(), deck3(False), lang=lang)["cards"]
            self.assertEqual(cards[0]["quote"]["where"], "美国年报 10-K" if lang == "zh" else "SEC 10-K")
            if lang == "zh":
                self.assertNotIn("CNINFO", text)
        self.assertEqual(page.form_words("annual_report_summary"), ("年报摘要", "annual report summary"))
        self.assertEqual(page.form_words("some_internal_id"), (None, None))
        self.assertEqual(page.form_words("10-Q"), ("10-Q", "10-Q"))

    def test_named_missing_words_are_the_checked_ones(self):
        res = {"idea": "thermal management for battery storage", "rows": [],
               "terms_by_lang": {"en": ["thermal", "battery storage", "liquid cooling"]},
               "calibration": {"keywords_weak": {"en": ["thermal"]}}}
        self.assertNotIn("thermal", page.idea_words_shown(res, "en"))
        self.assertNotIn("thermal", page.idea_terms(res))
        zh = {"idea": "储能液冷温控", "rows": [], "calibration": {"keywords_weak": {"zh": ["储能"]}}}
        self.assertNotIn("储能", page.idea_words_shown(zh, "zh"))
        self.assertIn("液冷", page.idea_words_shown(zh, "zh"))

    def test_no_gap_when_no_word_is_in_the_excerpts_script(self):
        # --no-translate: Chinese words only, an English excerpt: nothing to check, not a gap
        self.assertIsNone(page.mentions_idea("Zorvan makes liquid cooling for battery storage.", ["储能液冷温控系统"]))
        # an English idea without Chinese words, a Chinese filing that states it
        en = {"idea": "humanoid robots", "rows": [], "terms_by_lang": {"en": ["humanoid robot"]}}
        self.assertIsNone(page.mentions_idea("公司主要产品为人形机器人及其关节模组。", page.idea_terms(en)))
        res = {"run_id": "scr-h", "idea": "humanoid robots", "status": "ok", "params": {}, "funnel": {},
               "terms_by_lang": {"en": ["humanoid robot"]}, "unverified": [], "gaps": {},
               "rows": [{"rank": 1, "security_id": "SZSE:399201", "company_key": "h1", "name": "Hexa Robot Co.",
                         "l2_label": "explicit", "l2_evidence": "annual_report", "filing_source": "cninfo_annual_report",
                         "evidence_excerpt": "公司主要产品为人形机器人及其关节模组。"}]}
        r = page.build_page_data(res, None)["rows"][0]
        self.assertIsNone(r["quote"]["mentions"])
        self.assertNotIn("no_mention", r["badges"])
        # the same script still checks: an English excerpt without the words is a gap
        self.assertEqual(page.mentions_idea("We roll aluminium sheet.", page.idea_terms(en)), [])


class TestDoneTextMatchesThePage(unittest.TestCase):
    def test_chat_lines_say_gap_borderline_and_your_call(self):
        d = page.build_page_data(_novice_result(), None, lang="zh", local_names=LOCAL)
        job = {"result": {"summary": page.summary_of(d, None), "top": page.top_rows(d), "cost_usd": 0.1,
                          "page": None, "page_opened": True, "seconds": 30, "next_steps": []}}
        zh = quickstart.human_text(job, "done", [], "zh", None).splitlines()
        row = {ln.split(".", 1)[0]: ln for ln in zh if re.match(r"^\d+\. ", ln)}
        self.assertTrue(row["1"].endswith("相关，年报原文"), row["1"])
        self.assertIn("中国", row["1"])
        self.assertTrue(row["2"].endswith("相关，年报摘录未提到（缺口），边缘"), row["2"])
        self.assertTrue(row["3"].endswith("相关，按你的判断（AI 没从原文确认）"), row["3"])
        en = quickstart.human_text(job, "done", [], "en", None)
        self.assertIn("Related, the filing excerpt does not mention it (gap), borderline", en)
        self.assertIn("Related, your call (the AI did not confirm it from the text)", en)


class TestLocalNamesFromTheStore(StoreCase):
    def test_cninfo_short_names_through_identifiers(self):
        from why_seed import seed_why
        from jevscreen import store
        seed_why(self.cfg, self.home)
        with store.session(self.cfg, read_only=True) as con:
            got = page._local_names(con, ["SZSE:309901", "SSE:609902", "NYSE:ROBO"])
        self.assertEqual(got, {"SZSE:309901": "ST示信", "SSE:609902": "*ST甲信"})
        res = {"run_id": "scr-l", "idea": "humanoid robots", "status": "ok", "params": {}, "funnel": {},
               "rows": [{"rank": 1, "security_id": "SZSE:309901", "company_key": "isin:CNE000009901",
                         "name": "Shixin Humanoid Tech Co., Ltd. Class A", "l2_label": "partial"}],
               "unverified": [], "gaps": {}}
        _d, _c, _l, local = page._store_lookups(self.cfg, res)
        self.assertEqual(local, {"SZSE:309901": "ST示信"})
        data = page.build_page_data(res, None, lang="zh", local_names=local)
        self.assertEqual(page.top_rows(data)[0]["name"], "ST示信")
        self.assertEqual(data["rows"][0]["name"], "Shixin Humanoid Tech Co., Ltd.")

    def test_a_and_b_shares_of_one_issuer_keep_their_own_names(self):
        """One org_id, two codes (309901 ST示信 A, 209901 示信B, listed after it): the A line keeps its name."""
        from why_seed import STOCK_LIST, seed_why
        from jevscreen import store
        self.assertEqual(sorted(r["code"] for r in STOCK_LIST["stockList"] if r["orgId"] == "9900099011"),
                         ["209901", "309901"])
        seed_why(self.cfg, self.home)
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO identifiers SELECT 'SZSE:209901', id_type, id_value, method, snapshot_id "
                        "FROM identifiers WHERE security_id = 'SZSE:309901' AND id_type = 'cninfo_orgid'")
            con.execute("INSERT INTO identifiers SELECT 'SZSE:309999', id_type, id_value, method, snapshot_id "
                        "FROM identifiers WHERE security_id = 'SZSE:309901' AND id_type = 'cninfo_orgid'")
        with store.session(self.cfg, read_only=True) as con:
            got = page._local_names(con, ["SZSE:309901", "SZSE:209901", "SZSE:309999"])
        # a code the list does not carry falls back to the issuer's A-share name
        self.assertEqual(got, {"SZSE:309901": "ST示信", "SZSE:209901": "示信B", "SZSE:309999": "ST示信"})


class TestPageTextCommand(PageCase):
    def test_page_text_prints_the_ranked_list(self):
        rid, od, _html = self.screen_page()
        code, out, err = self.main(["page", rid, "--text"])
        self.assertEqual(code, 0, err)
        self.assertIn("#1 RoboCorp", out)
        code, out, err = self.main(["page", rid, "--text", "--json"])
        self.assertEqual(code, 0, err)
        self.assertIn("#1 RoboCorp", json.loads(out)["text"])
        code, out, err = self.main(["page", rid, "--json"])
        self.assertNotIn("text", json.loads(out))


class TestAnswerWords(unittest.TestCase):
    def test_plain_answer_words(self):
        self.assertEqual(calib.answer_words_zh("partial", "b", user_only=True), "要（年报没写，按你的判断）")
        self.assertEqual(calib.answer_words_zh("explicit", "a"), "要（直接做）")
        self.assertEqual(calib.answer_words_zh("partial", None), "要（相关，算大类）")
        self.assertEqual(calib.answer_words_zh("no", "h"), "不要（是买方/客户/用户，不是供应方）")
        self.assertEqual(calib.answer_words_zh("no", None), "不要")
        self.assertEqual(calib.answer_words_zh("unsure"), "不确定")


DOM_SHIM = r"""
class Node{constructor(tag){this.tagName=tag;this.children=[];this.style={};this._text='';this.className='';this.attrs={};
 this.value='';this.onclick=null;this.oninput=null;}
 appendChild(c){this.children.push(c);return c;}
 set textContent(v){this.children=[];this._text=String(v);} get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=v;} focus(){} select(){}}
const all=[];function mk(tag){const n=new Node(tag);all.push(n);return n;}
const app=mk('div');
global.document={documentElement:{},body:{className:''},title:'',createElement:mk,createTextNode:(t)=>{const n=mk('#text');n._text=String(t);return n;},
 getElementById:(id)=>id==='data'?{textContent:BLOB}:app,querySelector:()=>null,execCommand:()=>true};
global.navigator={};
"""

if __name__ == "__main__":
    unittest.main()
