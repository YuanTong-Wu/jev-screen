"""Deck v2 of your AI's review (2026-09-29 diagnosis of the AI review layer): part A reads every listed row (the
whole to-confirm section and the whole main list, not the first 10 of each); every item with a stored official
filing carries a few more numbered sentences of it (`evidence.more`: product / business passages the excerpt cut
off), which an answer may cite; the cited text is kept with the verdict, so the page, the escalation question and
`why` can quote it; the instructions say that a main-list row whose text contradicts a hard part of the idea is a no.
Offline, synthetic data."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_review_flow as TRF  # noqa: E402
from jevscreen import page, review, review_cli, screen, shortlist, store  # noqa: E402

IDEA_EN = "Liquid cooling for grid-scale battery storage"


def row(i: int, tier: str) -> dict:
    return {"rank": i, "company_key": f"k{i}", "security_id": f"NYSE:C{i:02d}", "name": f"Co {i}",
            "country": "United States", "market_cap_usd": 3e9 - i * 1e7, "l1_label": "core", "l1_p_core": 0.8,
            "l2_label": "explicit" if tier == "high" else "partial", "l2_evidence": "annual_report",
            "l2_p_pos": 0.9, "evidence_sha": f"sha{i}", "shortlist_tier": tier}


def sections(n_high: int, n_confirm: int, max_out: int = 40):
    rows = [row(i, "high") for i in range(1, n_high + 1)] + \
        [row(i, "confirm") for i in range(n_high + 1, n_high + n_confirm + 1)]
    res = {"run_id": "scr-20260929120000-abcdef", "idea": IDEA_EN, "idea_en": IDEA_EN,
           "params": {"max_out": max_out}, "rows": rows, "unverified": [],
           "terms_by_lang": {"en": ["liquid cooling", "battery storage"]}}
    inputs = {r["company_key"]: {"text": "[annual report excerpts: SEC 10-K filed 2026-02-01; language en]\n\n"
                                         "It makes cooling units. It sells them to utilities.",
                                 "evidence_sha": r["evidence_sha"], "evidence": "annual_report", "lang": "en",
                                 "source_id": "sec_filing_text"} for r in rows}
    return res, inputs


class DeckCoverage(unittest.TestCase):
    def test_part_a_reads_the_whole_to_confirm_section_and_the_whole_main_list(self):
        res, inputs = sections(17, 23)                         # jp-factory-automation: 17 main rows
        deck = review.build_deck(res, inputs, None, {}, part="A")
        tops = [(it["rank"], it["section"]) for it in deck["items"] if it["group"] == "top"]
        self.assertEqual([r for r, s in tops if s == "to_confirm"], list(range(18, 41)))
        self.assertEqual([r for r, s in tops if s == "main"], list(range(1, 18)))     # past the old cut of 10
        self.assertLessEqual(len(deck["items"]), review.DECK_A_MAX)
        self.assertGreaterEqual(review.DECK_A_MAX, 55)

    def test_part_b_then_holds_no_listed_row(self):
        res, inputs = sections(6, 34)                          # optical: to-confirm rows 11..34 were part B only
        a = review.build_deck(res, inputs, None, {}, part="A")
        self.assertEqual(sum(1 for it in a["items"] if it["group"] == "top"), 40)
        b = review.build_deck(res, inputs, None, {}, part="B",
                              exclude_keys={it["company_key"] for it in a["items"]})
        self.assertEqual([it for it in b["items"] if it["group"] == "top"], [])

    def test_instructions_name_the_more_block_and_the_main_list_rule(self):
        res, inputs = sections(2, 3)
        ins = review.build_deck(res, inputs, None, {}, part="A")["instructions_en"]
        self.assertIn("evidence.more", ins)
        self.assertIn("'main'", ins)
        for word in ("geography", "plan", "chip"):
            self.assertIn(word, ins)


CN_DOC = ("公司主要从事光通信器件的研发、生产和销售。\n"
          "行业方面，据统计全球数据中心液冷市场规模预计持续增长，储能液冷需求旺盛。\n"
          "公司主要产品包括储能液冷机组、冷板和温控系统，产品介绍如下：\n"
          "储能液冷机组 用于储能电站电池柜的液冷温控\n"
          "报告期内公司完成了董事会换届。\n"
          "公司的储能液冷机组已批量供货国内多家储能集成商。")


class MoreSentences(unittest.TestCase):
    def test_picks_the_companys_own_product_passages_that_the_excerpt_did_not_show(self):
        shown = "公司主要从事光通信器件的研发、生产和销售。"
        got = review.more_sentences(CN_DOC, shown=shown, strong=["液冷", "储能"], weak=["温控"], anchors=[],
                                    start=4)
        ids = [i for i, _ in got]
        texts = [s for _, s in got]
        self.assertEqual(ids, list(range(4, 4 + len(got))))                  # numbered after the excerpt
        self.assertTrue(any("储能液冷机组 用于储能电站" in s for s in texts))     # the product table row
        self.assertTrue(any("已批量供货" in s for s in texts))
        self.assertFalse(any("光通信器件" in s for s in texts))                  # already in the excerpt
        self.assertFalse(any("董事会" in s for s in texts))                      # no idea word
        self.assertLessEqual(len(got), review.MORE_MAX)
        # an industry-trend sentence ranks below the company's own product sentences
        trend = next((k for k, s in enumerate(texts) if "市场规模" in s), None)
        own = next(k for k, s in enumerate(texts) if "已批量供货" in s)
        self.assertTrue(trend is None or len(got) == review.MORE_MAX or trend < own)
        cut = review.more_sentences(CN_DOC, shown=shown, strong=["液冷", "储能"], weak=[], anchors=[], start=1,
                                    max_n=2)
        self.assertEqual(len(cut), 2)
        self.assertFalse(any("市场规模" in s for _, s in cut))                   # own product first

    def test_a_latin_anchor_counts_in_a_cjk_filing(self):
        text = "公司产品覆盖100G至400G光模块。\n公司推出1.6T/800G光模块产品，已向海外客户批量出货。"
        got = review.more_sentences(text, shown="公司产品覆盖100G至400G光模块。", strong=[], weak=[],
                                    anchors=["800G"], start=9)
        self.assertEqual([i for i, _ in got], [9])
        self.assertIn("800G", got[0][1])

    def test_nothing_without_terms_or_text(self):
        self.assertEqual(review.more_sentences("", shown="", strong=["x"], weak=[], anchors=[]), [])
        self.assertEqual(review.more_sentences(CN_DOC, shown="", strong=[], weak=[], anchors=[]), [])


def one_item_deck():
    res, inputs = sections(1, 1)
    deck = review.build_deck(res, inputs, None, {}, part="A", human_lang="en")
    docs = {"k2": {"text": CN_DOC, "source_id": "cninfo_annual_report", "form": "annual_report",
                   "filing_date": "2026-04-20", "section": "business_deep"}}
    res["terms_by_lang"] = {"en": ["liquid cooling"], "zh": ["储能液冷", "液冷"]}
    return res, inputs, review.add_more(deck, docs, res)


class AddMore(unittest.TestCase):
    def test_items_with_a_stored_filing_get_numbered_more_sentences(self):
        res, inputs, deck = one_item_deck()
        it = next(i for i in deck["items"] if i["company_key"] == "k2")
        n_ex = len(it["evidence"]["sentences"])
        more = it["evidence"]["more"]
        self.assertEqual(more["sentences"][0][0], n_ex + 1)
        self.assertEqual((more["source"], more["form"], more["filing_date"]), ("CNINFO", "annual_report",
                                                                               "2026-04-20"))
        self.assertTrue(it["review_sha"])
        self.assertNotEqual(it["review_sha"], it["evidence_sha"])
        other = next(i for i in deck["items"] if i["company_key"] == "k1")
        self.assertNotIn("more", other["evidence"])                                 # no stored filing
        self.assertEqual(json.dumps(deck, sort_keys=True), json.dumps(one_item_deck()[2], sort_keys=True))

    def test_an_answer_may_cite_a_more_sentence_and_its_text_travels_with_the_verdict(self):
        res, inputs, deck = one_item_deck()
        it = next(i for i in deck["items"] if i["company_key"] == "k2")
        mid, mtext = next((i, s) for i, s in it["evidence"]["more"]["sentences"] if "已批量供货" in s)
        data = {"format": review.ANSWERS_FORMAT, "deck_id": deck["deck_id"],
                "answers": {str(it["n"]): {"v": "yes", "level": "explicit", "quote_ids": [mid],
                                           "why": "its storage liquid-cooling units ship in volume"}}}
        ans = review.parse_answers(deck, data)[it["n"]]
        self.assertEqual((ans["v"], ans["quote_ids"]), ("yes", [mid]))
        v = review.verdict_of(it, ans, deck=deck, ctx={"section": "to_confirm"}, state="applied", code=None,
                              answers_sha="x", agent_name=None)
        self.assertEqual(v["quotes"], {str(mid): mtext})
        self.assertEqual(v["review_sha"], it["review_sha"])
        self.assertEqual(v["more_source"]["source"], "CNINFO")
        self.assertEqual(review.cited_text(inputs["k2"]["text"], [mid], v["quotes"]), mtext)
        kept, _ex, _n = review.apply_agent([{"company_key": "k2", "evidence_sha": "sha2"}], {"k2": v})
        self.assertEqual(kept[0]["agent_quotes"], {str(mid): mtext})
        self.assertIn("agent_quotes", screen.SCOPE_ROW_KEYS)
        r = dict(row(2, "confirm"), agent_verdict="yes", agent_level="explicit", agent_state="applied",
                 agent_quote_ids=[mid])
        self.assertEqual(shortlist.main_of(r), ("high", "agent"))
        # the escalation question quotes the cited more sentence, not the excerpt's first sentences
        esc = review.escalation_item(dict(v, escalation="E3", state="escalated"), "c1", text=inputs["k2"]["text"],
                                     lang_ev="zh", sieve=None)
        self.assertIn("已批量供货", esc["question_en"])

    def test_page_clear_verdict_uses_the_cited_more_sentence(self):
        text = "[company profile]\n\nIt is a bank."
        sha = screen.evidence_sha(text)
        cited = "It runs digital payments for merchants in Southeast Asia."
        r = {"agent_verdict": "yes", "agent_state": "applied", "agent_level": "explicit", "evidence_sha": sha,
             "agent_quote_ids": [3]}
        l2 = {"text": text, "sha": sha}
        self.assertIsNone(page._cited_direct_match(r, l2, ["Southeast Asia"], ["payments"]))
        r["agent_quotes"] = {"3": cited}
        self.assertEqual(page._cited_direct_match(r, l2, ["Southeast Asia"], ["payments"]), cited)


class PrepareWithStoredFiling(TRF.FlowCase):
    SHORTLIST = True

    def test_prepare_reads_every_listed_row_and_adds_the_stored_filing(self):
        with store.session(self.cfg) as con:
            ck, sid = con.execute("SELECT company_key, security_id FROM securities WHERE security_id = 'SGX:P01'"
                                  ).fetchone()
            path = self.home / "docs" / "p01.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Pay Service 1 is a holding company.\n\nOur digital payments wallet serves merchants in "
                            "Southeast Asia and processed 2 billion transactions.\n\nThe board met four times.",
                            encoding="utf-8")
            con.execute("INSERT INTO documents (doc_id, security_id, company_key, source_id, cik, form, section, "
                        "accession, text_path, filing_date, extractor, fetched_at) VALUES ('sec_filing_text:1:A1:"
                        "item1', ?, ?, 'sec_filing_text', '1', '10-K', 'item1', 'A1', ?, DATE '2026-02-01', 'x', ?)",
                        [sid, ck, str(path), store.now_utc()])
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        listed = [r for r in self.base["rows"] if r.get("rank") is not None]
        read = {it["company_key"] for it in deck["items"]}
        self.assertTrue({r["company_key"] for r in listed} <= read)          # all 20 listed rows, not 10
        it = next(i for i in deck["items"] if i["company_key"] == ck)
        self.assertIn("Southeast Asia", " ".join(s for _, s in it["evidence"]["more"]["sentences"]))
        self.assertNotIn("board", " ".join(s for _, s in it["evidence"]["more"]["sentences"]))


if __name__ == "__main__":
    unittest.main()
