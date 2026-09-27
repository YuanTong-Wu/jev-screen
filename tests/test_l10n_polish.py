"""Novice-facing wording polish (review of the provider + full-l10n integration): no internal licence codes in the
Chinese cards / why text, the Vercel key step names the credits it needs, Chinese texts carry no stray half-width
space or colon and never show a non-zero cost as ¥0.00, English texts use the right article and plurals, and the
top table keeps a Chinese word on one line at phone width.

No network, no paid calls (FakeJev / RuleJev); fake keys and invented companies only.
"""
from __future__ import annotations

import re
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401

from jevscreen import calib, jev, keys, l10n, page, quickstart as qs  # noqa: E402
from test_cli_calib import CliCase  # noqa: E402
from test_jev_providers import FAKE_TS, FAKE_VC, Base  # noqa: E402

TIER_CODE = re.compile(r"\b(?:gray|official|public)-(?:private|public)\b")
CJK_SPACE = re.compile(r"[一-鿿] [一-鿿]")


class TestNoLicenceCodes(CliCase):
    def test_cards_in_chinese_name_no_licence_code(self):
        rid, od = self.screen_with_deck()
        code, out, err = self.main(["cards", rid, "--lang", "zh"])
        self.assertEqual(code, 0, out + err)
        md = (od / "cards.md").read_text(encoding="utf-8")
        for text in (out, md):
            self.assertIn("仅供个人使用", text)
            self.assertEqual(TIER_CODE.findall(text), [], text)
            self.assertNotIn("CNINFO", text)
        self.assertIn("巨潮资讯", calib.LICENCE_OFFICIAL_ZH)

    def test_why_quote_names_no_licence_code(self):
        rid, _od = self.screen_with_deck()
        for lang in ("zh", "en"):
            code, out, err = self.main(["why", "NASDAQ:ROB2", "--run", rid, "--lang", lang, "--show-text"])
            self.assertEqual(code, 0, out + err)
            self.assertEqual(TIER_CODE.findall(out), [], out)


class TestProviderWording(Base):
    def test_vercel_key_step_names_the_credits(self):
        keys.use_provider(self.cfg, "vercel")
        it = qs.key_item(self.cfg, qs.key_state(self.cfg), False)
        pr = jev.PROVIDERS["vercel"]
        self.assertIn("credits", it["text_en"])
        self.assertIn(pr.credits_url, it["text_en"])
        self.assertIn("credits", it["text_zh"])
        self.assertIn(pr.credits_url, it["text_zh"])

    def test_chinese_key_texts_have_no_stray_space(self):
        keys.use_provider(self.cfg, "typesafe")
        k = qs.key_state(self.cfg)
        for rej, hs in ((False, None), (True, 401)):
            zh = qs.key_item(self.cfg, k, rej, hs)["text_zh"]
            self.assertEqual(CJK_SPACE.findall(zh), [], zh)
            self.assertNotIn("。 ", zh)
        job = {"idea": "纸杯", "failure": {"kind": "ai_unavailable", "provider": "typesafe", "http_status": 402}}
        zh = qs.human_text(job, "failed", [], "zh", None)
        self.assertIn("TypeSafe 官方说账户没有余额", zh)

    def test_english_key_texts_read_well(self):
        keys.use_provider(self.cfg, "openrouter")
        en = qs.key_item(self.cfg, qs.key_state(self.cfg), True, 401)["text_en"]
        self.assertNotIn("a OpenRouter", en)
        self.assertEqual(en.count("Create"), 1, en)
        self.assertIn("OpenRouter rejected the key.", en)

    def test_switch_notice_has_no_nested_brackets(self):
        keys.write_key(self.cfg, "vercel", FAKE_VC)
        out = keys.write_key(self.cfg, "typesafe", FAKE_TS)
        en = out["provider_switch"]["notice_en"]
        self.assertIn("Jev now goes through TypeSafe (official API) instead of Vercel AI Gateway.", en)
        self.assertNotIn(") (", en)


class TestDoneText(unittest.TestCase):
    def job(self, cost: float) -> dict:
        return {"idea": "纸杯", "result": {"run_id": "scr-x", "summary": {"listed": 1}, "page": None,
                                           "page_opened": True, "cost_usd": cost, "seconds": 30, "top": [],
                                           "next_steps": [{"text_zh": "换一个想法", "text_en": "Another idea"}]}}

    def test_chinese_next_steps_use_a_full_width_colon(self):
        zh = qs.human_text(self.job(0.1), "done", [], "zh", None)
        self.assertIn("下一步 1：换一个想法", zh)
        en = qs.human_text(self.job(0.1), "done", [], "en", None)
        self.assertIn("Next 1: Another idea", en)

    def test_a_tiny_cost_is_not_shown_as_zero_yuan(self):
        zh = qs.human_text(self.job(0.00005), "done", [], "zh", None)
        self.assertNotIn("¥0.00", zh)
        self.assertIn("不到 ¥0.01", zh)
        self.assertIn("约 ¥0.72", qs.human_text(self.job(0.1), "done", [], "zh", None))

    def test_the_page_meta_line_has_the_same_rule(self):
        self.assertIn("不到 ¥0.01", page.STRINGS["zh"]["cny_tiny"])
        self.assertIn("D.cost_usd>0&&D.cost_cny<0.005?'cny_tiny':'cny'", page.JS)


class TestEnglishPlurals(unittest.TestCase):
    def test_gap_lines_for_one_company(self):
        result = {"params": {}, "gaps": {"no_description": [{"security_id": "TSE:1"}],
                                         "l2_profile_only": [{"security_id": "TSE:2"}, {"security_id": "NYSE:A"}]}}
        lines = {g["id"]: g["text_en"] for g in page._gap_lines(result, {"NYSE:A": "United States"}, {})}
        self.assertTrue(lines["no_description"].startswith("1 company has no profile"), lines)
        self.assertTrue(lines["jp_profile_only"].startswith("1 Japanese company was checked"), lines)
        self.assertTrue(lines["us_profile_only"].startswith("1 US company was checked"), lines)
        result["gaps"]["no_description"].append({"security_id": "TSE:3"})
        lines = {g["id"]: g["text_en"] for g in page._gap_lines(result, {}, {})}
        self.assertTrue(lines["no_description"].startswith("2 companies have no profile"), lines)

    def test_cards_header_for_one_card(self):
        deck = {"idea": "robots", "cards": [{"n": 1, "security_id": "NYSE:A", "name": "A Inc", "what": {},
                                             "quote": {"source": "公司简介"}, "verdict": {"label": "partial"}}]}
        with unittest.mock.patch.object(page, "_card_why_en", return_value="why"):
            out = calib.render_cards_en(deck)
        self.assertIn("(1 card, about 1 minute)", out)


class TestTopTableAtPhoneWidth(unittest.TestCase):
    def test_verdict_and_evidence_cells_keep_words_whole(self):
        css = page.CSS.replace("\n", "")
        rule = re.search(r"table\.top10 td:nth-child\(3\),table\.top10 td:nth-child\(4\)\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "no keep-all rule for the verdict and evidence cells")
        self.assertIn("word-break:keep-all", rule.group(1))
        self.assertIn("overflow-wrap:normal", rule.group(1))


if __name__ == "__main__":
    unittest.main()
