"""Tests for jevscreen.review (scope design §6): sentence ids, agent decks, parsing the AI's answers, the
agent layer (file, precedence, leakage), escalations and held answers. Pure / temp files only, synthetic data."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, config, ops, review, scope, screen  # noqa: E402

TAG = "[annual report excerpts: SEC 10-K filed 2026-02-01; language en]"


def text_of(i: int, extra: str = "") -> str:
    return (f"{TAG}\n\nCompany {i} makes payment terminals for card acquirers in Asia. It sells them through "
            f"banks. {extra}\n\n[...]\n\nThe segment grew in the year.")


def synthetic(n_rows: int = 15, below: int = 10, gaps: int = 4, max_out: int = 12):
    """(result, inputs, pool): n_rows listed (the top max_out ranked), `below` verified companies below the cut in
    the pool only, `gaps` insufficient annual-report companies with a strong L1."""
    rows, pool, inputs = [], [], {}
    for i in range(1, n_rows + below + gaps + 1):
        ck = f"ck{i}"
        gap = i > n_rows + below
        label = "insufficient" if gap else ("explicit" if i % 3 else "partial")
        r = {"company_key": ck, "security_id": f"X:{i:04d}", "name": f"Co {i} Ltd", "country": "Japan",
             "market_cap_usd": 1e10 - i * 1e8, "l1_p_core": 0.8, "l1_p_adjacent": 0.15, "l1_pass": True,
             "l2_label": label, "l2_status": label, "l2_evidence": "annual_report",
             "l2_p_explicit": 0.9 if label == "explicit" else 0.3, "l2_p_pos": 0.95 if not gap else 0.2,
             "evidence_sha": screen.evidence_sha(text_of(i)), "user_verdict": None}
        r["score"] = screen.score_of(label, 0.8, r["market_cap_usd"], "annual_report")
        inputs[ck] = {"company_key": ck, "security_id": r["security_id"], "evidence": "annual_report",
                      "source_id": "sec_filing_text", "lang": "en", "text": text_of(i),
                      "evidence_sha": r["evidence_sha"], "keyword_hit": True}
        pool.append({k: v for k, v in r.items() if k != "user_verdict"})
        if i <= n_rows:
            rows.append(r)
    rows.sort(key=lambda r: -r["score"])
    for k, r in enumerate(rows, 1):
        r["rank"] = k if k <= max_out else None
    result = {"run_id": "scr-20260927120000-abcdef", "idea": "东南亚的数字支付", "idea_en": "Digital payments in "
              "Southeast Asia", "params": {"max_out": max_out, "rank": "label"},
              "rows": [r for r in rows if r["rank"]], "unverified": [], "terms_by_lang": {"en": ["payment"]},
              "questions": {"l2": {"criteria": dict(screen.L2_CRITERIA)}}, "funnel": {"l2_verified": n_rows + below}}
    return result, inputs, pool


SV = {"format": calib.SIEVE_FORMAT, "version": 1, "idea": "东南亚的数字支付", "idea_key": "k",
      "facets": {"category": "digital payments", "target": "Southeast Asia"},
      "facets_zh": {"category": "数字支付", "target": "东南亚"}, "examples": [], "rules": [], "history": []}


class Sentences(unittest.TestCase):
    def test_deterministic_and_resolvable(self):
        t = text_of(1, "Mr. Tanaka said so. 1. First item here.")
        a, b = review.sentences(t), review.sentences(t)
        self.assertEqual(a, b)
        self.assertEqual([i for i, _ in a], list(range(1, len(a) + 1)))
        self.assertNotIn(TAG, " ".join(s for _, s in a))                      # no tag line
        self.assertEqual(a[0][1], "Company 1 makes payment terminals for card acquirers in Asia.")
        self.assertEqual(review.quote_of(t, [1, 2]), " ".join(s for i, s in a if i in (1, 2)))
        zh = "[company profile; no annual report text available]\n\n公司主要提供移动支付服务。业务覆盖东南亚！另有其他。"
        self.assertEqual([s for _, s in review.sentences(zh)], ["公司主要提供移动支付服务。", "业务覆盖东南亚！", "另有其他。"])

    def test_long_pieces_are_split(self):
        long = "word, " * 100 + "end."
        parts = [s for _, s in review.sentences(long)]
        self.assertTrue(all(len(p) <= review.SENT_MAX for p in parts))
        self.assertEqual(re.sub(r"\s+", "", "".join(parts)), re.sub(r"\s+", "", long))


class Decks(unittest.TestCase):
    def setUp(self):
        self.result, self.inputs, self.pool = synthetic()

    def deck(self, part="A", **kw):
        return review.build_deck(self.result, self.inputs, kw.pop("sieve", SV), kw.pop("agent", {}), part=part,
                                 pool=self.pool, human_lang="zh", **kw)

    def test_part_a_groups_caps_and_content(self):
        held = [{"sid": "s1", "kind": "role.hardware", "family": "role", "value": "hardware",
                 "v": ["ck11", "ck12", "ck3"]}]
        d = self.deck(held=held)
        groups = [it["group"] for it in d["items"]]
        self.assertLessEqual(len(d["items"]), review.DECK_A_MAX)
        self.assertEqual(groups.count("held"), 3)
        self.assertEqual(groups.count("top"), 9)                                  # ck3 is held, not top twice
        self.assertLessEqual(groups.count("below_cut") + groups.count("gap"), review.DECK_A_EDGE)
        h = next(it for it in d["items"] if it["company_key"] == "ck11")
        self.assertEqual((h["held_sid"], h["held_kind"]), ("s1", "role.hardware"))
        self.assertEqual(d["held_questions"][0]["sid"], "s1")
        it = d["items"][0]
        joined = " ".join(s for _, s in it["evidence"]["sentences"])
        self.assertTrue(joined.startswith("Company"))
        self.assertEqual(it["evidence_sha"], self.inputs[it["company_key"]]["evidence_sha"])
        self.assertTrue(d["blocking"])
        self.assertEqual(d["deck_id"], "adeck-scr-20260927120000-abcdef-A")
        self.assertIn("personal use", d["licence_note"].lower())
        self.assertIn("jevscreen judge --deck adeck-", d["record_command"])
        self.assertEqual(json.dumps(d, sort_keys=True), json.dumps(self.deck(held=held), sort_keys=True))
        with tempfile.TemporaryDirectory() as tmp:
            body = json.dumps(d, ensure_ascii=False)
            self.assertNotIn(tmp, body)
            self.assertNotIn(str(Path.home()), body)
            self.assertEqual(ops.scan_secrets(config.Config(home=Path(tmp)), body), [])

    def test_left_out_decided_and_already_answered(self):
        sv = {**SV, "examples": [{"security_id": "X:0001", "company_key": "ck1", "want": "partial", "source": "card",
                                  "pin": True}]}
        agent = {"ck2": {"evidence_sha": self.inputs["ck2"]["evidence_sha"], "v": "yes", "state": "applied"},
                 "ck4": {"evidence_sha": "old", "v": "no", "state": "applied"}}
        keys = [it["company_key"] for it in self.deck(sieve=sv, agent=agent)["items"]]
        self.assertNotIn("ck1", keys)
        self.assertNotIn("ck2", keys)
        self.assertIn("ck4", keys)                               # a verdict on older evidence: asked again

    def test_part_b_and_followup(self):
        d = self.deck("B", removed_so_far=0)
        groups = [it["group"] for it in d["items"]]
        self.assertFalse(d["blocking"])
        self.assertEqual(groups.count("top"), 2)                  # ranks 11..12
        self.assertEqual(groups.count("below_cut"), 5)           # max(5, 0 + 3)
        self.assertEqual(groups.count("gap"), 3)
        d = self.deck("B", removed_so_far=6)
        self.assertEqual([it["group"] for it in d["items"]].count("below_cut"), 9)
        d = self.deck("B", removed_so_far=30)
        self.assertLessEqual([it["group"] for it in d["items"]].count("below_cut"), review.BELOW_CUT_MAX)
        f = self.deck("F1", followup_keys=["ck13", "ck14"])
        self.assertEqual([it["group"] for it in f["items"]], ["followup", "followup"])
        self.assertEqual(review.parse_deck_id(f["deck_id"]), ("scr-20260927120000-abcdef", "F1"))


class Answers(unittest.TestCase):
    def setUp(self):
        r, i, p = synthetic()
        self.deck = review.build_deck(r, i, SV, {}, part="A", pool=p, human_lang="zh",
                                      held=[{"sid": "s1", "kind": "role.hardware", "family": "role",
                                             "value": "hardware", "v": ["ck1"]}])

    def ans(self, answers, **kw):
        return review.parse_answers(self.deck, {"format": review.ANSWERS_FORMAT, "deck_id": self.deck["deck_id"],
                                                "answers": answers, **kw}, SV)

    def test_good_file_and_fallbacks(self):
        got = self.ans({"1": {"v": "no", "chip": "k", "quote_ids": [1], "why": "卖终端给收单机构", "in_group": True,
                              "short": "支付终端"},
                        "2": {"v": "yes", "level": "explicit", "quote_ids": [1, 2], "why": "它自己提供支付服务",
                              "quote_tr": "它为收单机构制造支付终端"},
                        "3": {"v": "unsure", "unsure_kind": "thin", "why": "摘录太短"}})
        self.assertEqual(sorted(got), [1, 2, 3])
        self.assertEqual((got[1]["chip"], got[1]["in_group"], got[1]["short_zh"]), ("k", True, "支付终端"))
        self.assertEqual(got[1]["why_en"], calib.NO_CHIP_TEXT_EN["k"])           # the other language: chip words
        self.assertEqual(got[2]["why_en"], "clearly fits")
        self.assertEqual(got[2]["quote_tr"], "它为收单机构制造支付终端")              # evidence en, human zh
        self.assertEqual(got[3]["unsure_kind"], "thin")

    def test_errors(self):
        for bad, frag in (({"1": {"v": "maybe"}}, "v must be"), ({"99": {"v": "yes", "level": "explicit"}}, "no item"),
                          ({"2": {"v": "no", "chip": "z", "quote_ids": [1]}}, "chips"),
                          ({"2": {"v": "no", "chip": "a", "quote_ids": [1]}}, "chips"),
                          ({"2": {"v": "yes", "quote_ids": [1]}}, "level")):
            with self.assertRaises(review.AgentError) as cm:
                self.ans(bad)
            self.assertIn(frag, cm.exception.text_en)
            self.assertTrue(cm.exception.text_zh)
        with self.assertRaises(review.AgentError):
            review.parse_answers(self.deck, {"deck_id": "adeck-x-A", "answers": {}}, SV)

    def test_bad_quote_ids_downgrade_to_unsure(self):
        got = self.ans({"2": {"v": "no", "chip": "h", "quote_ids": [99], "why": "x"},
                        "3": {"v": "yes", "level": "partial", "quote_ids": [], "why": "y"}})
        self.assertEqual((got[2]["v"], got[2]["quote_bad"], got[2]["was"]), ("unsure", True, "no"))
        self.assertEqual(got[3]["v"], "unsure")
        self.assertNotIn(4, self.ans({"2": {"v": "unsure"}}))                    # missing = not reviewed


    def test_unsure_keeps_valid_quote_ids(self):
        got = self.ans({"2": {"v": "unsure", "unsure_kind": "meaning", "quote_ids": [1], "why": "意思不清楚"},
                        "3": {"v": "unsure", "quote_ids": [99]}})
        self.assertEqual(got[2]["quote_ids"], [1])
        self.assertEqual(got[3]["quote_ids"], [])

    def test_why_and_short_in_the_wrong_language_fall_back(self):
        got = self.ans({"1": {"v": "no", "chip": "k", "quote_ids": [1], "why": "sells terminals", "in_group": True,
                              "short": "terminal maker"},
                        "2": {"v": "yes", "level": "explicit", "quote_ids": [1], "why": "它为 Visa 提供支付服务"}})
        self.assertEqual(got[1]["why_zh"], review.chip_words(SV)["k"]["zh"])        # English on a zh deck
        self.assertIsNone(got[1]["short_zh"])
        self.assertEqual(got[2]["why_zh"], "它为 Visa 提供支付服务")                    # Latin names inside are fine
        r, i, p = synthetic()
        en = review.build_deck(r, i, SV, {}, part="A", pool=p, human_lang="en",
                               held=[{"sid": "s1", "kind": "role.hardware", "family": "role", "value": "hardware",
                                      "v": ["ck1"]}])
        got = review.parse_answers(en, {"format": review.ANSWERS_FORMAT, "deck_id": en["deck_id"], "answers": {
            "1": {"v": "no", "chip": "k", "quote_ids": [1], "why": "卖终端", "in_group": True, "short": "支付终端"},
            "2": {"v": "unsure", "why": "意思不清楚"}}}, SV)
        self.assertEqual(got[1]["why_en"], review.chip_words(SV)["k"]["en"])
        self.assertIsNone(got[1]["short_en"])
        self.assertEqual(got[2]["why_en"], "not sure")

class AgentFile(unittest.TestCase):
    def test_history_and_version_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(home=Path(tmp))
            doc = review.load_agent(cfg, "idea")
            self.assertEqual(doc["version"], 0)
            doc = review.save_agent(cfg, "idea", review.put_verdicts(doc, [{"company_key": "a", "v": "yes"}]))
            doc2 = review.save_agent(cfg, "idea", review.put_verdicts(doc, [{"company_key": "a", "v": "no"}]))
            self.assertEqual(review.agent_verdicts(cfg, "idea")["a"]["v"], "no")
            self.assertEqual(doc2["history"][0]["v"], "yes")
            with self.assertRaises(review.AgentStale):
                review.save_agent(cfg, "idea", doc)                          # read at version 1, disk is at 2
            self.assertTrue(review.agent_path(cfg, "idea").name.endswith(".agent.json"))
            self.assertIsNone(calib.closest_sieve(cfg, "idea"))                  # never taken for a sieve


class Layer(unittest.TestCase):
    rows = [{"company_key": f"c{i}", "security_id": f"X:{i}", "evidence_sha": f"s{i}", "score": 5 - i,
             "market_cap_usd": 1e9, "l2_label": "explicit", "l1_p_core": 0.9, "name": f"C{i}"} for i in range(1, 5)]

    def test_precedence_and_no_leakage(self):
        agent = {"c1": {"evidence_sha": "s1", "v": "no", "chip": "h", "state": "applied", "why_zh": "买方"},
                 "c2": {"evidence_sha": "s2", "v": "yes", "state": "applied", "level": "explicit"},
                 "c3": {"evidence_sha": "stale", "v": "no", "chip": "h", "state": "applied"},
                 "c4": {"evidence_sha": "s4", "v": "no", "chip": "h", "state": "applied"}}
        sv = {"examples": [{"security_id": "X:4", "company_key": "c4", "want": "explicit", "source": "card",
                            "pin": True}]}
        v2, _u, _ex, _n = screen.pin_and_rank([dict(r) for r in self.rows], [], sv, 4, agent=agent, out=(out := {}))
        self.assertEqual([e["company_key"] for e in out["excluded_by_agent"]], ["c1"])
        self.assertEqual(out["excluded_by_agent"][0]["verdict_source"], "agent")
        self.assertIsNone(out["excluded_by_agent"][0].get("user_verdict"))
        by = {e["company_key"]: e for e in v2}
        self.assertEqual(by["c2"]["verdict_source"], "evidence+agent")
        self.assertEqual(by["c2"]["score"], 3)                                   # no re-score
        self.assertIsNone(by["c2"]["user_verdict"])
        self.assertNotIn("agent_verdict", by["c3"])                              # stale sha: ignored
        self.assertEqual(by["c4"]["user_verdict"], "explicit")                   # a human pin wins
        # the sieve's consumers never see the agent layer
        self.assertEqual(set(calib.pins(sv)), {"c4", "X:4"})
        self.assertEqual(calib.named_keys(sv), {"c4", "X:4"})
        diff = calib.render_diff_zh({"rows": [dict(r, rank=i) for i, r in enumerate(self.rows, 1)]},
                                    {"rows": [dict(e, rank=i) for i, e in enumerate(v2, 1)],
                                     "excluded_by_agent": out["excluded_by_agent"]})
        self.assertIn("你的 AI：不要（买方）", diff)
        self.assertNotIn("你：不要", diff)


class Escalations(unittest.TestCase):
    top = {"group": "top", "rank": 3, "in_top": True, "l2_label": "explicit", "p_explicit": 0.9,
           "mentions_idea": True}

    def test_reasons(self):
        C = review.classify
        self.assertEqual(C({"v": "unsure", "unsure_kind": "meaning"}, self.top), ("escalated", "E1"))
        self.assertEqual(C({"v": "unsure", "unsure_kind": "thin"}, self.top), ("applied", None))   # a badge only
        self.assertEqual(C({"v": "unsure", "unsure_kind": "meaning"}, {**self.top, "mentions_idea": False}),
                         ("applied", None))
        self.assertEqual(C({"v": "no", "chip": "h"}, self.top), ("escalated", "E2"))
        self.assertEqual(C({"v": "no", "chip": "h"}, {**self.top, "l2_label": "partial"}), ("applied", None))
        self.assertEqual(C({"v": "yes", "quote_ids": [1]}, {"group": "gap", "would_list": True}), ("escalated", "E3"))
        self.assertEqual(C({"v": "yes", "quote_ids": [1]}, {"group": "gap", "would_list": False}), ("applied", None))
        self.assertEqual(C({"v": "yes"}, self.top, {"want": "no"}), ("escalated", "E4"))

    def test_templates_and_translation(self):
        v = {"company_key": "c", "security_id": "X:1", "name": "Pay Co", "name_zh": "支付公司", "v": "no",
             "quote_ids": [1], "why_zh": "卖终端", "why_en": "sells terminals", "quote_tr": "它卖终端", "ctx": self.top}
        texts = {}
        for code in ("E1", "E2", "E3", "E4"):
            e = review.escalation_item({**v, "escalation": code}, "c1", text=text_of(1), lang_ev="en", sieve=SV,
                                       human={"want": "explicit"})
            texts[code] = e["question_zh"]
            self.assertIsNone(re.search(r"[一-鿿]", e["question_en"]), e["question_en"])
            self.assertEqual(e["tokens"], {"yes": "c1=yes", "no": "c1=no", "unsure": "c1=?"})
            self.assertTrue(e["in_relayed_top"])
        self.assertEqual(len(set(texts.values())), 4)
        self.assertIn("（译文：它卖终端）", texts["E2"])
        self.assertIn("摘录：「Company 1 makes payment terminals", texts["E2"])

    def test_escalation_quote_is_never_empty(self):
        # what the parse path gives for an unsure answer: no quote ids, the default reason
        v = {"company_key": "c", "security_id": "X:1", "name": "Pay Co", "name_zh": "支付公司", "v": "unsure",
             "unsure_kind": "meaning", "quote_ids": [], "why_zh": "拿不准", "why_en": "not sure", "ctx": self.top,
             "escalation": "E1"}
        e = review.escalation_item(v, "c1", text=text_of(1), lang_ev="en", sieve=SV)
        self.assertNotIn("「」", e["question_zh"])
        self.assertNotIn('""', e["question_en"])
        self.assertIn("「Company 1 makes payment terminals", e["question_zh"])     # the item's first sentences
        self.assertNotIn("拿不准——拿不准", e["question_zh"])
        self.assertNotIn("not sure. Excerpt", e["question_en"])
        # bad quote ids of a yes: the AI's call is named, not 'not sure'
        bad = {**v, "quote_bad": True, "was": "yes", "why_zh": "它自己做支付", "why_en": "it runs payments"}
        e = review.escalation_item(bad, "c2", text=text_of(1), lang_ev="en", sieve=SV)
        self.assertIn("它自己做支付", e["question_zh"])
        self.assertNotIn("拿不准", e["question_zh"])
        self.assertIn("it runs payments", e["question_en"])
        # no text at all: no empty excerpt clause
        e = review.escalation_item(v, "c3", text=None, lang_ev=None, sieve=SV)
        self.assertNotIn("「", e["question_zh"])
        self.assertNotIn("Excerpt", e["question_en"])

    def test_held_answers(self):
        doc = {"verdicts": [
            {"company_key": "a", "v": "no", "chip": "k", "state": "held", "held_kind": "role.hardware",
             "ctx": {"l2_label": "partial"}},
            {"company_key": "b", "v": "no", "chip": "h", "state": "held", "held_kind": "role.hardware",
             "ctx": {"l2_label": "explicit", "p_explicit": 0.95, "mentions_idea": True}},
            {"company_key": "c", "v": "yes", "state": "held", "held_kind": "role.buyer", "ctx": {}}]}
        yes = {v["company_key"]: v["state"] for v in review.resolve_held(doc, "role.hardware", "yes")["verdicts"]}
        self.assertEqual(yes, {"a": "not_applied", "b": "escalated", "c": "held"})
        uns = {v["company_key"]: v["state"] for v in review.resolve_held(doc, "role.hardware", "unsure")["verdicts"]}
        self.assertEqual(uns, {"a": "applied", "b": "escalated", "c": "held"})
        no = {v["company_key"]: v["state"] for v in review.resolve_held(doc, "role.hardware", "no")["verdicts"]}
        self.assertEqual(no["a"], "applied")


if __name__ == "__main__":
    unittest.main()
