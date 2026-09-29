"""Your AI's call is labelled by what it said, never 符合 / fits for a partial yes (bug on a real run, idea "ai安全":
a row your AI answered yes, level partial, showed 「你的 AI 判断：符合——没有保护AI的产品」).

yes + explicit -> 符合 / fits; yes + partial -> 部分相关 / partly related; no -> 不符合 / does not fit;
unsure -> 拿不准 / not sure. The same words on the page (script), --text, the why facts and quickstart's top rows.

Offline: invented companies only (test_no_padding's sections_result).
"""
from __future__ import annotations

import re
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import page, review, shortlist, why  # noqa: E402
from test_no_padding import data_of, sections_result  # noqa: E402
from test_page import dom_text  # noqa: E402
from test_review import SV, synthetic, text_of  # noqa: E402

CJK = re.compile(r"[㐀-鿿]")
WHY = {"zh": "只提到相关业务，没有保护AI的产品", "en": "related work only, no product that protects AI"}
CASES = (("yes", "explicit", "agent_yes"), ("yes", "partial", "agent_partial"), ("no", None, "agent_no"),
         ("unsure", None, "agent_unsure"))


def _result(v, level):
    """3 confirmed + 3 to confirm; the first to-confirm row carries your AI's call (v, level), applied."""
    res = sections_result(3, 3)
    c = next(r for r in res["rows"] if r["shortlist_tier"] != "high")
    c.update(agent_verdict=v, agent_level=level, agent_state="applied", agent_quote_ids=[1],
             agent_why_zh=WHY["zh"], agent_why_en=WHY["en"])
    return res, c["security_id"]


class TheWords(unittest.TestCase):
    def test_the_strings_exist_and_are_pure(self):
        self.assertEqual(page.STRINGS["zh"]["agent_partial"], "部分相关")
        self.assertEqual(page.STRINGS["en"]["agent_partial"], "partly related")
        for k in ("agent_yes", "agent_partial", "agent_no", "agent_unsure"):
            self.assertTrue(CJK.search(page.STRINGS["zh"][k]), k)
            self.assertFalse(CJK.search(page.STRINGS["en"][k]), k)

    def test_the_key_follows_the_level(self):
        for v, level, key in CASES:
            self.assertEqual(page.agent_key({"v": v, "level": level}), key)
        self.assertIsNone(page.agent_key({"v": "maybe"}))
        self.assertIsNone(page.agent_key(None))


class OnEverySurface(unittest.TestCase):
    def test_text_view(self):
        for v, level, key in CASES:
            res, _sid = _result(v, level)
            for lang in ("zh", "en"):
                S = page.STRINGS[lang]
                txt = page.render_text(data_of(res, lang), lang)
                line = S["agent_tag"] + ("：" if lang == "zh" else ": ") + S[key]
                self.assertIn(line + ("——" if lang == "zh" else ": ") + WHY[lang], txt, (v, level, lang))
                if key != "agent_yes":
                    self.assertNotIn(S["agent_tag"] + ("：" if lang == "zh" else ": ") + S["agent_yes"], txt)

    def test_page_script(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        for v, level, key in CASES:
            res, _sid = _result(v, level)
            for lang in ("zh", "en"):
                S = page.STRINGS[lang]
                dom = dom_text(self, page.render_page(data_of(res, lang)))["text"]
                self.assertIn(S[key] + ("——" if lang == "zh" else ": ") + WHY[lang], dom, (v, level, lang))
                yes = re.compile(r"(?<!不)" + re.escape(S["agent_yes"] + ("——" if lang == "zh" else ": ") + WHY[lang]))
                self.assertEqual(bool(yes.search(dom)), key == "agent_yes", (v, level, lang))

    def test_top_rows_and_to_confirm_carry_the_level_and_the_words(self):
        for v, level, key in CASES:
            res, sid = _result(v, level)
            for lang in ("zh", "en"):
                d = data_of(res, lang)
                [row] = [r for r in page.top_rows(d, 50, section="all") if r["ticker"] and sid.endswith(r["ticker"])]
                self.assertEqual((row["agent"], row["agent_level"]), (v, level))
                self.assertEqual(row["agent_words_zh"], page.STRINGS["zh"][key])
                self.assertEqual(row["agent_words_en"], page.STRINGS["en"][key])
                if key == "agent_yes":           # a yes, level explicit, moved it into the main list
                    self.assertTrue(any(r["checked_by_agent"] for r in page.top_rows(d)))
                    continue
                [brief] = [r for r in page.to_confirm_brief(d)["rows"] if r["security_id"] == sid]
                self.assertEqual(brief["agent_level"], level)
                self.assertEqual(brief["agent_words_en"], page.STRINGS["en"][key])

    def test_why_facts(self):
        for v, level, key in CASES:
            row = {"agent_verdict": v, "agent_level": level, "agent_state": "applied",
                   "agent_why_zh": WHY["zh"], "agent_why_en": WHY["en"]}
            f = why._agent_fact(row)
            self.assertTrue(f["zh"].startswith("你的 AI 判断：" + page.STRINGS["zh"][key] + "——"), f)
            self.assertTrue(f["en"].startswith("your AI's call: " + page.STRINGS["en"][key] + " ("), f)
            self.assertFalse(CJK.search(f["en"]), f)


class InTheEscalationQuestions(unittest.TestCase):
    """The chat questions name your AI's call with the same words: a partial yes whose quote ids were bad (E1b) is
    部分相关 / partly related, never 符合 / fits; a partial yes never asks 'your AI says it belongs' (E3): only an
    explicit yes promotes a row, so only an explicit yes is escalated as one that would make the list."""
    top = {"group": "top", "rank": 3, "in_top": True, "l2_label": "partial", "p_explicit": 0.5,
           "mentions_idea": True, "section": "main"}

    def _deck(self):
        r, i, p = synthetic()
        return review.build_deck(r, i, SV, {}, part="A", pool=p, human_lang="zh")

    def _parse(self, deck, answer):
        return review.parse_answers(deck, {"format": review.ANSWERS_FORMAT, "deck_id": deck["deck_id"],
                                           "answers": {"2": answer}}, SV)[2]

    def _e1b(self, ans):
        v = {"company_key": "c", "security_id": "X:1", "name": "Pay Co", "name_zh": "支付公司", "ctx": self.top,
             **{k: ans.get(k) for k in ("v", "level", "was", "was_level", "quote_bad", "quote_ids", "why_zh",
                                        "why_en")}}
        state, code = review.classify(ans, self.top)
        self.assertEqual((state, code), ("escalated", "E1"))
        return review.escalation_item({**v, "escalation": code}, "c1", text=text_of(1), lang_ev="en", sieve=SV)

    def test_e1b_partial_yes_with_bad_quote_ids_is_partly_related(self):
        deck = self._deck()
        ans = self._parse(deck, {"v": "yes", "level": "partial", "quote_ids": [99], "why": "只做相关业务"})
        self.assertEqual((ans["v"], ans["quote_bad"], ans["was"], ans.get("was_level")),
                         ("unsure", True, "yes", "partial"))
        e = self._e1b(ans)
        self.assertIn("没指出摘录里哪句能说明", e["question_zh"])           # the E1b template
        self.assertIn("你的 AI 判断" + page.STRINGS["zh"]["agent_partial"], e["question_zh"])
        self.assertNotIn("判断符合", e["question_zh"])
        self.assertIn("partly related", e["question_en"])
        self.assertNotIn("it fits", e["question_en"])
        # the verdict stored for the page keeps the level your AI gave
        vd = review.verdict_of(deck["items"][1], ans, deck=deck, ctx=self.top, state="escalated", code="E1",
                               answers_sha="x", agent_name=None)
        self.assertEqual((vd["was"], vd["was_level"]), ("yes", "partial"))

    def test_e1b_explicit_yes_and_no_keep_their_words(self):
        deck = self._deck()
        for answer, zh, en in (({"v": "yes", "level": "explicit", "quote_ids": [99], "why": "它自己做支付"},
                                "agent_yes", "it fits"),
                               ({"v": "no", "chip": "h", "quote_ids": [99], "why": "只卖终端"},
                                "agent_no", "it does not fit")):
            e = self._e1b(self._parse(deck, answer))
            self.assertIn("你的 AI 判断" + page.STRINGS["zh"][zh] + "，", e["question_zh"])
            self.assertIn("your AI says " + en + " but", e["question_en"])
        # a verdict stored before was_level existed: a yes stays 符合 / fits (agent_key's legacy rule)
        e = review.escalation_item({"security_id": "X:1", "name": "Pay Co", "v": "unsure", "quote_bad": True,
                                    "was": "yes", "ctx": self.top, "escalation": "E1"}, "c1", text=text_of(1),
                                   lang_ev="en", sieve=SV)
        self.assertIn("你的 AI 判断符合", e["question_zh"])

    def test_e3_only_for_an_explicit_yes(self):
        ctx = {"group": "gap", "would_list": True}
        self.assertEqual(review.classify({"v": "yes", "level": "explicit", "quote_ids": [1]}, ctx),
                         ("escalated", "E3"))
        self.assertEqual(review.classify({"v": "yes", "level": "partial", "quote_ids": [1]}, ctx),
                         ("applied", None))


if __name__ == "__main__":
    unittest.main()
