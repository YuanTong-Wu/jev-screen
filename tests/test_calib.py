"""Tests for jevscreen.calib: the sieve file, inclusion probability and the card deck, answers, pins and the free
rerank, rules (render / validate / trial), and keyword learning (background df, noisy seeds, mining, peer preview).

No network and no paid calls: rule trials use a fake Jev client; stores are temp DuckDBs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, config, jev, screen, store  # noqa: E402
from test_screen import StoreCase, humanoid_sieve  # noqa: E402

FACETS = {"category": "identity, access, authorization or approval control (IAM, PAM, zero-trust access, API "
                      "gateways)",
          "target": "AI agents and other non-human identities",
          "mechanism": "checks, approves or blocks an agent's calls to tools, APIs or data before they run"}
FACETS_ZH = {"category": "身份/访问控制", "target": "AI agent", "mechanism": "调用前授权、审批、阻断"}


def sieve_doc(**kw) -> dict:
    return {**calib.new_sieve("企业 AI agent 的身份与权限管控", now="2026-09-27T00:00:00+00:00"), **kw}


# ---------------------------------------------------------------------------------------------------------------
# A synthetic finished run of 30 companies (max_out 12) for the deck

SEC, CN = "sec_filing_text", "cninfo_annual_report"


def company(sid, name, *, p, pe, pp, label=None, n=3, src=SEC, mcap=1e9, core=0.9, adj=0.05, kw=True,
            text=None, l1_pass=True, sha=None):
    if label is None and p is not None:
        label = "explicit" if pe >= pp and p >= 0.5 else "partial" if p >= 0.5 else "insufficient"
    lang = "zh" if src == CN else "en"
    body = text or (f"{name} sells identity software for AI agent access control and approval workflows to large "
                    "enterprises around the world.")
    tag = (f"[annual report excerpts: {screen.source_label(src)} {'10-K' if src == SEC else '年度报告'} filed "
           f"2026-03-01; language {lang}]")
    full = tag + "\n\n" + body
    ck = f"ck:{sid}"
    row = {"security_id": sid, "company_key": ck, "name": name, "country": "United States" if src == SEC else "China",
           "market_cap_usd": mcap, "l1_label": "core", "l1_p_core": core, "l1_p_adjacent": adj, "l1_pass": l1_pass,
           "l2_label": label if p is not None else None, "l2_status": label if p is not None else "failed",
           "l2_evidence": "annual_report" if p is not None else None, "l2_p_pos": p, "l2_p_explicit": pe,
           "l2_p_partial": pp, "l2_reads": n, "l2_p_pos_sd": 0.05, "l2_edge": p is not None and 0.4 <= p < 0.6,
           "l2_read_detail": [{"read": i, "p_pos": p} for i in range(n)], "evidence_sha": sha or f"sha-{sid}",
           "filing_source": src, "filing_form": "10-K" if src == SEC else "年度报告", "filing_date": "2026-03-01",
           "doc_lang": lang, "l2_keyword_hit": kw, "evidence_url": f"https://example.test/{sid}",
           "l1_description": f"{name} makes software. It is listed.", "l1_input_tier": "gray-private"}
    kw_text = body
    inp = {"company_key": ck, "security_id": sid, "evidence": "annual_report", "source_id": src, "lang": lang,
           "text": full, "excerpts": [{"kind": "overview", "text": f"{name} overview sentence. More text."}]
           + ([{"kind": "keywords", "text": kw_text}] if kw else []),
           "keyword_hit": kw, "matched_terms": ["identity"] if kw else [], "evidence_sha": sha or f"sha-{sid}"}
    return row, inp


def synthetic_run(extra_examples=(), target_terms=True, facets=True):
    cs = [
        # the output O (12 rows)
        company("SZSE:309352", "北辰信安", p=0.98, pe=0.56, pp=0.42, src=CN, mcap=5e9,
                text="北辰信安为政府客户提供终端安全管理与身份认证产品。"),
        company("NASDAQ:QSHD", "QuietShield", p=0.97, pe=0.9, pp=0.07, mcap=8e10,
                text="QuietShield sells endpoint protection and identity threat detection to enterprises."),
        company("NYSE:SAF1", "Safe One", p=0.95, pe=0.85, pp=0.1, mcap=3e9),
        company("NYSE:SAF2", "Safe Two", p=0.95, pe=0.85, pp=0.1, mcap=2e9),
        company("NYSE:SAF3", "Safe Three", p=0.95, pe=0.85, pp=0.1, mcap=1e9),
        company("NYSE:RYKN", "Rykon Hosting", p=0.55, pe=0.15, pp=0.40, mcap=1e9),
        company("NYSE:HVBQ", "Hivebay", p=0.58, pe=0.15, pp=0.43, mcap=2e10),
        company("NYSE:BND3", "Bound Three", p=0.56, pe=0.16, pp=0.40, mcap=1.5e9),
        company("SSE:609292", "瀚川数安", p=0.57, pe=0.2, pp=0.37, src=CN, mcap=1e9),
        company("SZSE:309161", "恒远世通", p=0.60, pe=0.2, pp=0.40, src=CN, mcap=8e8),
        company("NYSE:ANS", "Answered Co", p=0.55, pe=0.15, pp=0.40, mcap=3e9),
        company("NASDAQ:SLWZ", "Sailwise Identity", p=0.55, pe=0.15, pp=0.40, mcap=9e9),
        # verified below the output (pool): boundary_out candidates
        company("NYSE:BXQO", "Boxwell Cloud", p=0.77, pe=0.2, pp=0.57, core=0.1, mcap=4e9),
        company("NYSE:LOW", "Low Co", p=0.52, pe=0.1, pp=0.42, core=0.1, mcap=5e9),
        # insufficient with strong L1: gap candidates (keyword hit first)
        company("NASDAQ:MQSF", "Mesoforge", p=0.3, pe=0.1, pp=0.2, core=0.6, adj=0.3, mcap=3e12),
        company("NYSE:GAPX", "Gap Nokw", p=0.3, pe=0.1, pp=0.2, core=0.6, adj=0.3, mcap=5e12, kw=False),
        # pinned 'no' whose evidence changed: recheck
        company("NYSE:PINR", "Pinned Re", p=0.7, pe=0.2, pp=0.5, mcap=1e9, sha="sha-new"),
        # failed L2, L1 failures, contradicted: never carded
        company("NYSE:FAIL", "Failed Co", p=None, pe=None, pp=None, mcap=9e11),
        company("NYSE:CONT", "Contra Co", p=0.05, pe=0.02, pp=0.03, label="contradicted", mcap=9e11),
    ]
    cs += [company(f"NYSE:F{i:02d}", f"Filler {i}", p=0.1, pe=0.05, pp=0.05, core=0.2, adj=0.2, mcap=1e8 + i)
           for i in range(11)]
    assert len(cs) == 30
    rows = {r["company_key"]: r for r, _ in cs}
    inputs = {i["company_key"]: i for _, i in cs}
    out_keys = [f"ck:{s}" for s in ("SZSE:309352", "NASDAQ:QSHD", "NYSE:SAF1", "NYSE:SAF2", "NYSE:SAF3", "NYSE:RYKN",
                                    "NYSE:HVBQ", "NYSE:BND3", "SSE:609292", "SZSE:309161", "NYSE:ANS",
                                    "NASDAQ:SLWZ")]
    ranked = sorted((rows[k] for k in out_keys),
                    key=lambda r: (-screen.score_of(r["l2_label"], r["l1_p_core"], r["market_cap_usd"],
                                                    "annual_report"), -r["market_cap_usd"], r["security_id"]))
    out_rows = [{**r, "rank": i} for i, r in enumerate(ranked, 1)]
    unverified = [rows[f"ck:{s}"] for s in ("NASDAQ:MQSF", "NYSE:GAPX")]
    excluded = [{**rows["ck:NYSE:PINR"], "user_verdict": "no", "verdict_source": "user"}]
    pool = [r for k, r in rows.items() if k not in set(out_keys)]
    result = {"run_id": "scr-test-1", "idea": "企业 AI agent 的身份与权限管控",
              "params": {"max_out": 12, "rank": "label"}, "rows": out_rows, "unverified": unverified,
              "excluded_by_user": excluded,
              "layers": {"l2": {"band": {"items": 9, "ok": 18, "cost_usd": 0.0008}}}}
    sv = sieve_doc(facets=FACETS if facets else None, facets_zh=FACETS_ZH if facets else None,
                   target_terms={"en": ["AI agent"], "zh": ["智能体"]} if target_terms else None,
                   examples=[{"security_id": "NYSE:PINR", "company_key": "ck:NYSE:PINR", "want": "no", "chip": "c",
                              "source": "card", "pin": True, "evidence_sha": "sha-old",
                              "evidence_filing": "SEC|10-K|2025-03-01"},
                             {"security_id": "NYSE:ANS", "company_key": "ck:NYSE:ANS", "want": "unsure",
                              "source": "card", "pin": False, "evidence_sha": "sha-NYSE:ANS"},
                             {"security_id": "NASDAQ:SLWZ", "want": "explicit", "source": "sieve", "pin": False},
                             *extra_examples])
    return result, inputs, pool, sv


class TestInclusionProbability(unittest.TestCase):
    def cand(self, ck, p, n=1, **kw):
        return {"company_key": ck, "security_id": ck, "market_cap_usd": 1e9, "l1_p_core": 0.5,
                "l2_evidence": "annual_report", "l2_label": "partial" if p >= 0.5 else "insufficient",
                "l2_p_pos": p, "l2_p_explicit": p / 2, "l2_p_partial": p / 2, "l2_reads": n, **kw}

    def test_deterministic_cut_and_certain_items(self):
        cands = [self.cand("a", 0.5), self.cand("b", 0.95), self.cand("c", 0.05), self.cand("d", 0.55, n=3)]
        pi = calib.inclusion_probability(cands, max_out=10, seed=7)
        self.assertEqual(pi, calib.inclusion_probability(cands, max_out=10, seed=7))
        self.assertNotEqual(pi["a"], calib.inclusion_probability(cands, max_out=10, seed=8)["a"])
        self.assertAlmostEqual(pi["a"], 0.5, delta=0.05)           # on the cut
        self.assertEqual((pi["b"], pi["c"]), (1.0, 0.0))            # far from the cut: certain
        self.assertAlmostEqual(pi["d"], 0.74, delta=0.04)           # sd 0.133 / sqrt(3)
        self.assertEqual(calib.noise_sd(0.5), 0.133)
        self.assertEqual(calib.noise_sd(0.95), 0.034)

    def test_pins_and_competition(self):
        cands = [self.cand("a", 0.95, l1_p_core=0.9), self.cand("b", 0.95), self.cand("c", 0.1)]
        sv = {"examples": [{"company_key": "a", "want": "no", "source": "card", "pin": True},
                           {"company_key": "c", "want": "partial", "source": "card", "pin": True}]}
        pi = calib.inclusion_probability(cands, max_out=1, sieve=sv, seed=1)
        self.assertEqual(pi["a"], 0.0)                               # a user no is never listed
        self.assertEqual(pi["c"], 0.0)                               # pinned partial (0.5 core) loses to b
        self.assertEqual(pi["b"], 1.0)
        pi = calib.inclusion_probability(cands, max_out=2, sieve=sv, seed=1)
        self.assertEqual(pi["c"], 1.0)
        pi_ev = calib.inclusion_probability(cands, max_out=1, rank="ev", seed=1)
        self.assertEqual(pi_ev["a"], 1.0)


class TestSelectCards(unittest.TestCase):
    def types(self, deck):
        return [(c["type"], c["security_id"]) for c in deck]

    def test_slot_order_caps_and_strata(self):
        result, inputs, pool, sv = synthetic_run()
        deck = calib.select_cards(result, inputs, sv, pool=pool)
        self.assertEqual(self.types(deck), [
            ("recheck", "NYSE:PINR"), ("top_check", "SZSE:309352"), ("scope", "NASDAQ:QSHD"),
            ("boundary_in", "NYSE:RYKN"), ("boundary_in", "NYSE:BND3"), ("boundary_in", "SSE:609292"),
            ("boundary_out", "NYSE:BXQO"), ("gap", "NASDAQ:MQSF")])
        self.assertEqual(deck, calib.select_cards(result, list(inputs.values()), sv, pool=pool))   # deterministic
        self.assertEqual([c["n"] for c in deck], list(range(1, 9)))
        top = deck[1]
        self.assertEqual(top["why_zh"], f"排第{top['rank']}，「直接」「相关」五五开")
        self.assertEqual(top["chips"], ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"])      # v1.1: role chips h-k
        self.assertEqual(deck[2]["gap"], "年报摘录没写到AI agent")
        self.assertTrue(deck[3]["why_zh"].startswith("在名单里，但多读几次可能掉出去（入选 "))
        self.assertEqual(deck[6]["why_zh"], "年报判为相关/明确（合计 77%），但排在 12 名外")
        self.assertEqual(deck[0]["why_zh"], "你上次答过，但年报换了新版本")
        # without target terms: no scope card, four boundary_in cards, at most 2 per stratum (Hivebay is the third
        # SEC one and is skipped); max_cards cuts the deck in slot order
        result, inputs, pool, sv = synthetic_run(target_terms=False, facets=False)
        deck = calib.select_cards(result, inputs, sv, pool=pool)
        self.assertEqual(self.types(deck), [
            ("recheck", "NYSE:PINR"), ("top_check", "SZSE:309352"), ("boundary_in", "NYSE:RYKN"),
            ("boundary_in", "NYSE:BND3"), ("boundary_in", "SSE:609292"), ("boundary_in", "SZSE:309161"),
            ("boundary_out", "NYSE:BXQO"), ("gap", "NASDAQ:MQSF")])
        self.assertEqual(deck[1]["chips"], ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"])   # v1.1: g without facets too
        self.assertIsNone(deck[2]["gap"])
        self.assertEqual(self.types(calib.select_cards(result, inputs, sv, pool=pool, max_cards=3)),
                         self.types(deck)[:3])

    def test_answered_checks_and_changed_sha(self):
        result, inputs, pool, sv = synthetic_run()
        keys = {c["security_id"] for c in calib.select_cards(result, inputs, sv, pool=pool)}
        self.assertFalse({"NYSE:ANS", "NASDAQ:SLWZ", "NYSE:FAIL", "NYSE:CONT"} & keys)   # answered / check / not ok
        # the same unsure answer on an older text: the card comes back (not pinned: a normal slot)
        sv["examples"][1]["evidence_sha"] = "sha-older"
        deck = calib.select_cards(result, inputs, sv, pool=pool)
        self.assertIn(("boundary_in", "NYSE:ANS"), self.types(deck))
        # a pinned company with the same sha is never carded
        sv["examples"][0]["evidence_sha"] = "sha-new"
        self.assertNotIn("NYSE:PINR", {c["security_id"] for c in calib.select_cards(result, inputs, sv, pool=pool)})

    def test_empty_deck_and_boundary_out_needs_the_pool(self):
        result, inputs, pool, sv = synthetic_run()
        deck = calib.select_cards(result, inputs, sv)                 # no pool: nothing below the output is known
        self.assertNotIn("boundary_out", [c["type"] for c in deck])
        empty = {"run_id": "scr-x", "idea": "x", "params": {"max_out": 5}, "rows": [], "unverified": []}
        self.assertEqual(calib.select_cards(empty, {}, None), [])
        d = calib.build_deck(empty, {}, None, now="t")
        self.assertEqual(calib.render_cards_md(d), calib.EMPTY_DECK_ZH + "\n")

    def test_deck_json_and_markdown(self):
        result, inputs, pool, sv = synthetic_run()
        deck = calib.build_deck(result, inputs, sv, pool=pool, now="2026-09-27T00:00:00+00:00")
        self.assertEqual(deck["format"], "jevscreen.cards/1")
        self.assertEqual(deck["deck_id"], "deck-scr-test-1-1")
        self.assertEqual(deck["stabilize"], {"items": 9, "reads_added": 18, "cost_usd": 0.0008})
        self.assertEqual(deck["chips"]["yes"]["a"], "直接做（调用前授权、审批、阻断）")
        self.assertEqual(deck["chips"]["no"]["g"], "只有大类（身份/访问控制），没提AI agent")
        c = deck["cards"][3]
        self.assertEqual(set(c), {"n", "type", "company_key", "security_id", "name", "country", "rank", "pi", "what",
                                  "quote", "verdict", "gap", "why_zh", "evidence_sha", "chips"})
        self.assertEqual(c["what"], {"text": "Rykon Hosting makes software.", "source": "公司简介", "tier": "gray-private"})
        self.assertEqual((c["quote"]["source"], c["quote"]["form"], c["quote"]["filing_date"]),
                         ("SEC", "10-K", "2026-03-01"))
        self.assertIn("identity", c["quote"]["text"])
        self.assertLessEqual(len(c["quote"]["text"]), calib.QUOTE_MAX_CHARS)
        self.assertEqual(c["verdict"]["reads"], [0.55, 0.55, 0.55])
        with tempfile.TemporaryDirectory() as d:
            pj, pm = calib.write_deck(deck, d)
            self.assertEqual(calib.load_deck(d), json.loads(pj.read_text(encoding="utf-8")))
            md = pm.read_text(encoding="utf-8")
        lines = md.splitlines()
        self.assertEqual(lines[0], "校准卡 · 企业 AI agent 的身份与权限管控（8 张，约 1 分钟）")
        self.assertIn("g只有大类", lines[1])
        self.assertIn("[2] 北辰信安 309352 · 现排第", md)
        self.assertIn("  做什么：北辰信安 makes software.（公司简介，仅供个人使用）", md)
        self.assertIn("  年报：「北辰信安为政府客户提供终端安全管理与身份认证产品。」（CNINFO 年度报告，2026 年发布）", md)
        self.assertIn("（SEC 10-K 年报，2026 年发布）", md)
        self.assertIn("  系统：明确符合 56%（相关 42%，读 3 次）", md)
        self.assertIn("  缺口：年报摘录没写到AI agent", md)
        self.assertIn(calib.LICENCE_GRAY_ZH, md)
        with self.assertRaises(ValueError):
            calib.load_deck(Path(tempfile.gettempdir()) / "no-such-deck-dir-xyz")


class TestParseAnswers(unittest.TestCase):
    DECK = {"deck_id": "deck-r-1", "chips": {"no": {k: "" for k in "cdef"}},
            "cards": [{"n": i, "company_key": f"k{i}", "security_id": f"X:{i}", "name": f"C{i}",
                       "verdict": {"label": lab}, "evidence_sha": f"s{i}", "type": "boundary_in"}
                      for i, lab in zip(range(1, 6), ["explicit", "partial", "insufficient", "explicit", "partial"])]}

    def parse(self, text, deck=None, warnings=None):
        return [tuple(a) for a in calib.parse_answers(text, deck or self.DECK, warnings)]

    def test_forms(self):
        self.assertEqual(self.parse("１要ａ，２不要c、3？ 4要"), [(1, "yes", "explicit", "a"), (2, "no", None, "c"),
                                                            (3, "unsure", None, None), (4, "yes", "explicit", None)])
        self.assertEqual(self.parse("1要b；2不要\n3要"), [(1, "yes", "partial", "b"), (2, "no", None, None),
                                                      (3, "yes", "partial", None)])      # plain 要, insufficient
        self.assertEqual(self.parse("1. 要a 2：不要d 3号要b 4) no 5 y"),
                         [(1, "yes", "explicit", "a"), (2, "no", None, "d"), (3, "yes", "partial", "b"),
                          (4, "no", None, None), (5, "yes", "partial", None)])
        self.assertEqual(self.parse("1要a2不要c3?"), [(1, "yes", "explicit", "a"), (2, "no", None, "c"),
                                                   (3, "unsure", None, None)])
        self.assertEqual(self.parse("1b 2e 3YES 4n"), [(1, "yes", "partial", "b"), (2, "no", None, "e"),
                                                      (3, "yes", "partial", None), (4, "no", None, None)])
        self.assertEqual(self.parse("1不要c 其余都要"), [(1, "no", None, "c"), (2, "yes", "partial", None),
                                                    (3, "yes", "partial", None), (4, "yes", "explicit", None),
                                                    (5, "yes", "partial", None)])
        self.assertEqual(self.parse("1要a 2跳过 其余不要f"), [(1, "yes", "explicit", "a"), (3, "no", None, "f"),
                                                          (4, "no", None, "f"), (5, "no", None, "f")])
        warns: list = []
        self.assertEqual(self.parse("1要a 1不要c", warnings=warns), [(1, "no", None, "c")])
        self.assertEqual(warns, ["第 1 张答了两次，用最后一次（1不要c）"])

    def test_errors(self):
        cases = {"9要a": "没有第 9 张卡（这副卡的编号是 1、2、3、4、5）",
                 "1不要a": "第 1 张：a 是「要」的理由，不能配「不要」（不要 用 c–k）",
                 "2要c": "第 2 张：c 是「不要」的理由，不能配「要」（要 用 a 或 b）",
                 "1不要g": "第 1 张：这副卡没有 g（旧版卡组，没有这个理由字母）",   # a v1 deck without g
                 "3?c": "第 3 张：「?」不能带理由字母 c",
                 "其余要c": "其余：c 是「不要」的理由，不能配「要」（要 用 a 或 b）",
                 "hello": "看不懂「hello」：请写成 1要a、2不要c、3? 这样的格式",
                 "1要不要": "第 1 张：「1要不要」同时写了要/不要/?，只能选一个",
                 "4": "第 4 张：「4」没写要还是不要（例如 4要a 或 4不要c）",
                 " ，": "没有读到任何回答：请写成 1要a 2不要c 3? 这样的格式"}
        for text, msg in cases.items():
            with self.subTest(text=text), self.assertRaises(calib.AnswerError) as cm:
                calib.parse_answers(text, self.DECK)
            self.assertEqual(str(cm.exception), msg)
        deck_g = {**self.DECK, "chips": {"no": {k: "" for k in "cdefg"}}}
        self.assertEqual(self.parse("1不要g", deck_g), [(1, "no", None, "g")])

    def test_file_form(self):
        data = {"deck_id": "deck-r-1", "answers": {"1": {"v": "yes", "chip": "b"}, "2": {"v": "no", "chip": "c"},
                                                   "3": {"v": "unsure"}}}
        got = calib.answers_from_file(data, self.DECK)
        self.assertEqual([tuple(a) for a in got], [(1, "yes", "partial", "b"), (2, "no", None, "c"),
                                                   (3, "unsure", None, None)])
        self.assertEqual(calib.answers_json(self.DECK, got)["answers"]["1"], {"v": "yes", "chip": "b",
                                                                             "level": "partial"})
        with self.assertRaises(calib.AnswerError):
            calib.answers_from_file({**data, "deck_id": "deck-other"}, self.DECK)
        with self.assertRaises(calib.AnswerError):
            calib.answers_from_file({"answers": {"1": {"v": "maybe"}}}, self.DECK)


class TestSieveFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = config.Config(home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_answers_history_and_undo(self):
        idea = "企业 AI agent 的身份与权限管控"
        path = calib.sieve_path(self.cfg, idea)
        self.assertEqual(path.parent, self.home / "sieves")
        sv = calib.new_sieve(idea, now="t0")
        deck = TestParseAnswers.DECK
        sv, new = calib.record_answers(sv, deck, calib.parse_answers("1要b 2不要c 3?", deck), now="t1")
        self.assertEqual([(e["security_id"], e["want"], e["chip"], e["pin"]) for e in new],
                         [("X:1", "partial", "b", True), ("X:2", "no", "c", True), ("X:3", "unsure", None, False)])
        self.assertEqual(new[0]["evidence_sha"], "s1")
        self.assertEqual((new[0]["deck_id"], new[0]["card_n"], new[0]["source"]), ("deck-r-1", 1, "card"))
        saved = calib.save_sieve(path, sv, now="t2")
        self.assertEqual(saved["version"], 1)
        loaded = calib.load_sieve(path)
        self.assertEqual(loaded, saved)
        self.assertEqual(calib.pins(loaded)["X:2"]["want"], "no")
        # a later answer for the same company supersedes the earlier one; the old one goes to history
        sv2, _ = calib.record_answers(loaded, deck, calib.parse_answers("2要a", deck), now="t3")
        self.assertEqual([(e["security_id"], e["want"]) for e in sv2["examples"]],
                         [("X:1", "partial"), ("X:3", "unsure"), ("X:2", "explicit")])
        self.assertEqual(sv2["history"][-1]["want"], "no")
        self.assertEqual(len(sv2["decks"]), 1)
        sv3, removed = calib.undo_example(sv2, 1, now="t4")
        self.assertEqual(removed["security_id"], "X:1")
        self.assertEqual(sv3["history"][-1]["undone_at"], "t4")
        with self.assertRaises(calib.AnswerError):
            calib.undo_example(sv3, 9)
        calib.save_sieve(path, sv3)
        self.assertEqual(calib.load_sieve(path)["version"], 2)

    def test_stale_writer_is_refused_and_write_is_atomic(self):
        path = self.home / "sieves" / "k.json"
        sv = calib.save_sieve(path, calib.new_sieve("idea x"))
        stale = dict(sv)
        calib.save_sieve(path, sv)                                     # another writer: version 2 on disk
        with self.assertRaises(calib.SieveStale) as cm:
            calib.save_sieve(path, stale)
        self.assertIn("请重新读取", str(cm.exception))
        before = path.read_bytes()
        with mock.patch.object(calib.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            calib.save_sieve(path, calib.load_sieve(path))
        self.assertEqual(path.read_bytes(), before)                    # the old file is intact
        self.assertEqual([p.name for p in path.parent.iterdir() if ".tmp" in p.name], [])

    def test_validation(self):
        good = sieve_doc(facets=FACETS, facets_zh=FACETS_ZH, target_terms={"en": ["AI agent"]},
                         keywords={"ja": {"add": ["SAML"], "weak": ["認証"], "log": []}},
                         rules=["mention_only", {"id": "homonym", "terms": ["SAML"]}],
                         examples=[{"security_id": "NYSE:HVBQ", "want": "no", "chip": "g", "source": "card",
                                    "pin": True}])
        self.assertEqual(calib.validate_sieve(good), [])
        bad = sieve_doc(version=-1, facets={"category": "x"}, target_terms={"xx": ["a"]}, rules=["nope"],
                        keywords={"ja": {"add": "SAML"}},
                        examples=[{"want": "yes"}, {"security_id": "A", "want": "no", "chip": "a"},
                                  {"security_id": "B", "want": "no", "chip": "g", "source": "other"}])
        errs = calib.validate_sieve(bad)
        for frag in ("version", "facets needs", "target_terms.xx", "unknown rule 'nope'", "keywords.ja.add",
                     "examples[0] needs", "examples[0].want", "examples[1].chip 'a' does not fit",
                     "examples[2].source"):
            self.assertTrue(any(frag in e for e in errs), (frag, errs))
        self.assertFalse(any("needs facets" in e for e in errs))           # v1.1: g has a generic text
        p = self.home / "bad.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            calib.load_sieve(p)
        with self.assertRaises(ValueError):
            calib.save_sieve(self.home / "bad2.json", bad)
        self.assertIsNone(calib.load_sieve(self.home / "missing.json"))

    def test_closest_sieve(self):
        idea = "企业 AI agent 的身份与权限管控：在 agent 调用工具之前做授权"
        calib.save_sieve(calib.sieve_path(self.cfg, idea), calib.new_sieve(idea))
        hit = calib.closest_sieve(self.cfg, "企业 AI agent 的身份与权限管控：在 agent 调用工具前做授权")
        self.assertEqual(hit["idea"], idea)
        self.assertGreaterEqual(hit["similarity"], 0.6)
        self.assertIsNone(calib.closest_sieve(self.cfg, idea))                  # its own sieve is not a hint
        self.assertIsNone(calib.closest_sieve(self.cfg, "humanoid robots"))


class TestPinsAndRerank(StoreCase):
    def entry(self, sid, label, core=0.5, ev="annual_report"):
        return {"company_key": f"k:{sid}", "security_id": sid, "name": sid, "market_cap_usd": 1e9, "l1_p_core": core,
                "l2_label": label, "l2_evidence": ev, "score": screen.score_of(label, core, 1e9, ev)}

    def test_apply_pins(self):
        v = [self.entry("A", "explicit"), self.entry("B", "partial"), self.entry("C", "partial")]
        u = [self.entry("D", "insufficient"), self.entry("E", "insufficient")]
        sv = {"examples": [{"security_id": "B", "want": "no", "chip": "c", "source": "card", "pin": True},
                           {"security_id": "D", "want": "explicit", "chip": "a", "source": "card", "pin": True},
                           {"security_id": "C", "want": "explicit", "source": "card", "pin": True},
                           {"security_id": "E", "want": "unsure", "source": "card", "pin": False},
                           {"security_id": "Z", "want": "partial", "source": "card", "pin": True, "name": "Zed"}]}
        v2, excluded, notes = calib.apply_pins(v, u, sv)
        got = {e["security_id"]: e for e in v2}
        self.assertEqual(set(got), {"A", "C", "D"})
        self.assertEqual((got["D"]["verdict_source"], got["D"]["user_note"], got["D"]["user_verdict"]),
                         ("user", calib.USER_ONLY_NOTE, "explicit"))
        self.assertEqual(got["D"]["score"], screen.score_of("explicit", 0.5, 1e9, "annual_report"))
        self.assertEqual((got["C"]["verdict_source"], got["C"]["score"]),
                         ("evidence+user", screen.score_of("explicit", 0.5, 1e9, "annual_report")))
        self.assertEqual((got["A"]["verdict_source"], got["A"]["user_verdict"]), ("evidence", None))
        self.assertEqual([(e["security_id"], e["verdict_source"], e["user_chip"]) for e in excluded],
                         [("B", "user", "c")])
        self.assertEqual(notes, ["校准：Zed 不在本次结果中，钉选（partial）未生效"])
        v3, u3, ex3, _ = screen.pin_and_rank(v, u, sv, max_out=2)
        self.assertEqual([(e["security_id"], e["backfill"]) for e in v3], [("A", False), ("C", False), ("D", False)])
        self.assertEqual(([e["security_id"] for e in u3], [e["security_id"] for e in ex3]), (["E"], ["B"]))
        # backfill: A enters the top 1 only because the user removed B
        v = [self.entry("B", "explicit", core=0.9), self.entry("A", "partial")]
        v4, _, _, _ = screen.pin_and_rank(v, [], {"examples": sv["examples"][:1]}, max_out=1)
        self.assertEqual([(e["security_id"], e["backfill"]) for e in v4], [("A", True)])
        v5, _, _, _ = screen.pin_and_rank(v, [], None, max_out=1)
        self.assertEqual([(e["security_id"], e["backfill"]) for e in v5], [("B", False), ("A", False)])

    def test_rerank_matches_screen_and_diff(self):
        base = self.run_screen(reads=1, max_out=1)
        self.assertEqual([r["security_id"] for r in base["rows"]], ["NYSE:ROBO"])
        sv = humanoid_sieve(examples=[
            {"security_id": "NYSE:ROBO", "want": "no", "chip": "c", "source": "card", "pin": True},
            {"security_id": "TSE:6000", "want": "partial", "chip": "b", "source": "card", "pin": True}])
        with store.session(self.cfg, read_only=True) as con:
            pool = calib.load_pool(con, base["run_id"], base["params"])
        rob2 = next(p for p in pool if p["security_id"] == "NASDAQ:ROB2")
        self.assertEqual((rob2["l1_pass"], rob2["l2_label"], rob2["l2_evidence"], rob2["l2_reads"]),
                         (True, "partial", "annual_report", 1))
        self.assertEqual(rob2["l1_description"], "Robo Two provides motion control software and robot controllers.")
        self.assertAlmostEqual(rob2["l2_p_pos"], 0.8)
        rr = calib.rerank_result(base, sv, pool=pool)
        again = self.run_screen(reads=1, max_out=1, sieve=sv, out_dir=self.home / "o2")

        def key(rows):
            return [(r["security_id"], r["rank"], r["score"], r["user_verdict"], r["verdict_source"],
                     r["backfill"]) for r in rows]
        self.assertEqual(key(rr["rows"]), key(again["rows"]))
        # the user's yes (ServoJP, L1 p_core 0.35) ranks below max_out 1: still listed, with its real rank
        self.assertEqual(key(rr["rows"]), [("NASDAQ:ROB2", 1, rr["rows"][0]["score"], None, "evidence", True),
                                           ("TSE:6000", 2, rr["rows"][1]["score"], "partial", "user", False)])
        self.assertEqual([r.get("below_cut") for r in rr["rows"]], [None, True])
        self.assertEqual([r.get("below_cut") for r in again["rows"]], [None, True])
        self.assertEqual([r["security_id"] for r in rr["unverified"]], [r["security_id"] for r in again["unverified"]])
        self.assertEqual([r["security_id"] for r in rr["excluded_by_user"]],
                         [r["security_id"] for r in again["excluded_by_user"]])
        # the free diff (Robo Two is not in results.json: it comes from the pool)
        self.assertEqual(calib.render_diff_zh(base, rr),
                         "立即生效（免费）\n  移出 1：RoboCorp #1（你：不要）\n  新进 2：ServoJP 6000（你：要（年报没写，按你的判断）"
                         " · 排第2，在前1名外，照样列出） · Robo Two（递补，未经你确认）")
        rr2 = calib.rerank_result(base, sv)
        # without the pool Robo Two is unknown: the pinned yes (from unverified) is the only listed row
        self.assertEqual([(r["security_id"], r["verdict_source"]) for r in rr2["rows"]], [("TSE:6000", "user")])
        self.assertEqual(calib.render_diff_zh(base, calib.rerank_result(base, None, pool=pool)),
                         "立即生效（免费）\n  名单没有变化")
        # a user yes that the evidence does not support
        sv_yes = humanoid_sieve(examples=[
            {"security_id": "NYSE:ROBO", "want": "no", "chip": "c", "source": "card", "pin": True},
            {"security_id": "TSE:6000", "want": "explicit", "chip": "a", "source": "card", "pin": True}])
        self.assertEqual(calib.render_diff_zh(base, calib.rerank_result(base, sv_yes, pool=pool)),
                         "立即生效（免费）\n  移出 1：RoboCorp #1（你：不要）\n  新进 2：ServoJP 6000（你：要（年报没写，按你的判断）"
                         " · 排第2，在前1名外，照样列出） · Robo Two（递补，未经你确认）")
        wide = {**base, "params": {**base["params"], "max_out": 2}}          # room for the user's yes too
        self.assertEqual(calib.render_diff_zh(base, calib.rerank_result(wide, sv_yes, pool=pool)),
                         "立即生效（免费）\n  移出 1：RoboCorp #1（你：不要）\n"
                         "  新进 2：Robo Two · ServoJP 6000（你：要（年报没写，按你的判断））")

    def test_auto_sieve_hint_for_a_reworded_idea(self):
        idea = "humanoid robots for warehouses"
        calib.save_sieve(calib.sieve_path(self.cfg, idea), calib.new_sieve(idea))
        res = self.run_screen(idea="humanoid robots for warehouse", sieve="auto", reads=1)
        self.assertEqual((res["sieve_hint"]["idea"], "calibration" in res), (idea, False))
        self.assertGreaterEqual(res["sieve_hint"]["similarity"], 0.6)
        res = self.run_screen(idea=idea, sieve="auto", reads=1, out_dir=self.home / "o2")     # its own sieve loads
        self.assertNotIn("sieve_hint", res)
        self.assertIn("calibration", res)
        self.assertNotIn("sieve_hint", self.run_screen(reads=1, out_dir=self.home / "o3"))    # default: no lookup

    def test_deck_on_a_real_run(self):
        res = self.run_screen(reads=1)
        inputs = calib.load_inputs(self.home / "out")
        self.assertEqual(set(inputs), {"isin:US0000000001", "isin:US0000000007", "isin:JP0000000002"})
        with store.session(self.cfg, read_only=True) as con:
            pool = calib.load_pool(con, res["run_id"], res["params"])
        self.assertEqual(calib.build_deck(res, inputs, None, pool=pool)["cards"], [])
        sv = humanoid_sieve(target_terms={"en": ["android"]})
        deck = calib.build_deck(res, inputs, sv, pool=pool)
        self.assertEqual([(c["type"], c["security_id"]) for c in deck["cards"]], [("scope", "NYSE:ROBO")])
        card = deck["cards"][0]
        self.assertEqual(card["what"], {"text": "RoboCorp makes industrial robot arms.", "source": "公司简介",
                                        "tier": "gray-private"})
        self.assertIn("humanoid robots", card["quote"]["text"])
        self.assertEqual(card["quote"]["url"], "https://www.sec.gov/robo.htm")
        self.assertEqual(card["chips"], ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"])
        sv2, _ = calib.record_answers(sv, deck, calib.parse_answers("1要b", deck))
        deck2 = calib.build_deck(res, inputs, sv2, pool=pool)
        # answered: not asked again; the scope slot goes to the next broad row (Robo Two, p̄ 0.8, no 'android')
        self.assertEqual([(c["type"], c["security_id"]) for c in deck2["cards"]], [("scope", "NASDAQ:ROB2")])
        self.assertEqual(deck2["deck_id"], f"deck-{res['run_id']}-2")
        sv3, _ = calib.record_answers(sv2, deck2, calib.parse_answers("1不要g", deck2) if "g" in deck2["chips"]["no"]
                                      else calib.parse_answers("1不要c", deck2))
        self.assertEqual(calib.build_deck(res, inputs, sv3, pool=pool)["cards"], [])


class TestRules(unittest.TestCase):
    def test_build_l2_question_with_rules(self):
        plain = screen.build_l2_question("idea x", "Idea X")
        self.assertEqual(plain, jev.Question(key="evidence", instructions=screen.L2_INSTRUCTIONS.format(
            idea="Idea X (original: idea x)"), criteria=dict(screen.L2_CRITERIA)))
        self.assertEqual(screen.build_l2_question("idea x", "Idea X", rules=[], facets=FACETS), plain)
        q1 = screen.build_l2_question("idea x", rules=["mention_only", {"id": "laundry_list"}], facets=FACETS)
        q2 = screen.build_l2_question("idea x", rules=[{"id": "laundry_list"}, "mention_only", "mention_only"],
                                      facets=FACETS)
        self.assertEqual(q1, q2)
        self.assertEqual(jev.item_key(q1, "Co", "text"), jev.item_key(q2, "Co", "text"))
        self.assertNotEqual(jev.item_key(q1, "Co", "text"), jev.item_key(plain, "Co", "text"))
        # facets: templates; without facets: generic fallbacks, facets-only rules off
        qf = screen.build_l2_question("idea x", rules=["user_not_supplier", "scope_narrow"], facets=FACETS)
        self.assertIn("only builds, sells or uses AI agents and other non-human identities", qf.criteria["insufficient"])
        self.assertIn("Explicit means identity, access", qf.criteria["explicit"])
        qg = screen.build_l2_question("idea x", rules=["user_not_supplier", "scope_narrow"])
        self.assertIn("the technology the idea is about", qg.criteria["insufficient"])
        self.assertEqual(qg.criteria["partial"], screen.L2_CRITERIA["partial"])
        self.assertIn("Explicit means the company offers the specific thing", qg.criteria["explicit"])   # v1.1
        self.assertEqual(screen.build_l2_question("idea x", rules=["scope_broad"]).criteria,
                         dict(screen.L2_CRITERIA))
        qh = screen.build_l2_question("idea x", rules=[{"id": "homonym", "terms": ["SAML", "認証"]}], facets=FACETS)
        self.assertIn("Terms such as 'SAML', '認証' count only", qh.criteria["insufficient"])
        self.assertIn("Words of the idea count only", screen.build_l2_question("idea x", rules=["homonym"])
                      .criteria["insufficient"])
        # scope_narrow and scope_broad are exclusive: the newest wins
        newer_broad = [{"id": "scope_narrow", "adopted_at": "2026-01-01"}, {"id": "scope_broad", "adopted_at": "2026-02-01"}]
        q = screen.build_l2_question("idea x", rules=newer_broad, facets=FACETS)
        self.assertIn("Partial includes the company's own", q.criteria["partial"])
        self.assertNotIn("Explicit means", q.criteria["explicit"])
        q = screen.build_l2_question("idea x", rules=["scope_broad", "scope_narrow"], facets=FACETS)
        self.assertIn("Explicit means", q.criteria["explicit"])
        self.assertNotIn("Partial includes", q.criteria["partial"])
        with self.assertRaises(ValueError):
            calib.render_rules(["no_such_rule"])

    def test_validate_rule_text(self):
        text = calib.render_rules(["user_not_supplier"], FACETS)[0][2]
        names = ["ACCESS CO., LTD.", "Okrin, Inc.", "Bix, Inc.", "Sailwise Identity Holdings, Inc."]
        tickers = ["HVBQ", "ISMS", "API", "OKRN"]
        self.assertEqual(calib.validate_rule_text(text, names=names, tickers=tickers), [])
        homonym = calib.render_rules(["homonym"])[0][2]
        self.assertEqual(calib.validate_rule_text(homonym, tickers=["ISMS"]), [])      # the library's own word
        self.assertEqual(calib.validate_rule_text("Vendors like Okrin are insufficient.", names=names),
                         ["含公司名 Okrin"])
        self.assertEqual(calib.validate_rule_text("Like HVBQ and OKRN.", tickers=tickers),
                         ["含股票代码 HVBQ", "含股票代码 OKRN"])
        quote = "We develop and sell humanoid robots for warehouse picking and assembly work."
        self.assertEqual(calib.validate_rule_text("A company that sell humanoid robots for warehouse picking is ok.",
                                                  quotes=[quote]), ["含卡片原文（We develop and sell …）"])
        self.assertEqual(calib.validate_rule_text("sell humanoid robots for", quotes=[quote]), [])   # < 30 chars

    def test_candidate_rules(self):
        cards = [{"n": 1, "type": "boundary_in", "name": "Hivebay", "security_id": "NYSE:HVBQ",
                  "quote": {"text": "Hivebay offers identity and SSO features.", "matched_terms": ["SSO", "identity"]}},
                 {"n": 2, "type": "boundary_in", "name": "Rykon Hosting", "security_id": "NASDAQ:RYKN",
                  "quote": {"text": "We resell everything.", "matched_terms": []}},
                 {"n": 3, "type": "scope", "name": "QuietShield", "security_id": "NASDAQ:QSHD",
                  "quote": {"text": "Identity threat detection.", "matched_terms": ["identity"]}},
                 {"n": 4, "type": "boundary_in", "name": "Signara", "security_id": "NASDAQ:SGNQ",
                  "quote": {"text": "Agreement management.", "matched_terms": ["agreement"]}}]
        deck = {"deck_id": "deck-r-1", "cards": cards, "chips": {"no": {k: "" for k in "cdefg"}}}
        sv = sieve_doc(facets=FACETS, rules=[{"id": "mention_only"}])
        ans = calib.parse_answers("1不要d 2不要e 3要b 4不要d", deck)
        cand, dropped = calib.candidate_rules(sv, deck, ans)
        self.assertEqual([(c["id"], c.get("terms")) for c in cand],
                         [("laundry_list", None), ("scope_broad", None), ("homonym", ["SSO", "identity", "agreement"])])
        self.assertEqual(cand[2]["from"], "card 1 @deck-r-1; card 4 @deck-r-1")
        self.assertEqual(dropped, [])
        # exclusivity: the later card wins (answers are in card order); an adopted rule is not proposed again;
        # no facets: g / scope_broad off
        cand, dropped = calib.candidate_rules(sv, deck, calib.parse_answers("3要b 1不要g 2不要f", deck))
        self.assertEqual([c["id"] for c in cand], ["scope_broad"])
        self.assertEqual([d["id"] for d in dropped], ["scope_narrow", "mention_only"])
        self.assertEqual(dropped[0]["why_zh"], "和后面的回答（scope_broad）冲突，用后面的")
        deck_rev = {**deck, "cards": [{**c, "n": 5 - c["n"]} for c in cards]}     # the scope card is now card 2
        cand, _ = calib.candidate_rules(sv, deck_rev, calib.parse_answers("2要b 4不要g", deck_rev))
        self.assertEqual([c["id"] for c in cand], ["scope_narrow"])
        cand, _ = calib.candidate_rules(sieve_doc(), deck, calib.parse_answers("3要b 1不要g 2不要c", deck))
        self.assertEqual([c["id"] for c in cand], ["scope_narrow", "user_not_supplier"])   # v1.1: g generic
        # a rule text that would carry a company name is dropped
        leaky = sieve_doc(facets={**FACETS, "target": "Hivebay style CRM agents"})
        cand, dropped = calib.candidate_rules(leaky, deck, calib.parse_answers("1不要c", deck))
        self.assertEqual(cand, [])
        self.assertEqual(dropped[0]["id"], "user_not_supplier")
        self.assertIn("含公司名 Hivebay", dropped[0]["why_zh"])


MARKERS = {"user_not_supplier": "A company that only builds", "laundry_list": "A service provider",
           "mention_only": "A single mention"}


class TrialJev:
    """Fake L2 client for trial_rules: p_pos per company from `effects` given the rules present in the question."""

    def __init__(self, base, effects, cost=0.001):
        self.base, self.effects, self.cost = base, effects, cost
        self.spent_usd, self.calls = 0.0, []

    def estimate(self, items, question):
        return {"est_cost_usd": self.cost}

    def classify(self, items, question):
        rules = {rid for rid, m in MARKERS.items() if m in question.criteria["insufficient"]}
        self.calls.append((question.read, [it.item_id for it in items], sorted(rules)))
        self.spent_usd += self.cost
        out = []
        for it in items:
            p = self.base[it.item_id]
            for rid in sorted(rules):
                p = self.effects.get(rid, {}).get(it.item_id, p)
            out.append({"item_id": it.item_id, "label": "partial" if p >= 0.5 else "insufficient",
                        "probs": {"explicit": p / 2, "partial": p / 2, "contradicted": 0.0, "insufficient": 1 - p},
                        "request_id": "r", "status": "ok", "error": None, "cached": False})
        return out


class TestTrialRules(unittest.TestCase):
    BASE = {"HUBS": 0.7, "RXT": 0.6, "BOX": 0.77, "SAIL": 0.9, "A1": 0.95, "A2": 0.95, "A3": 0.95, "A4": 0.95,
            "LOWKW": 0.95}

    def setup(self):
        sv = sieve_doc(facets=FACETS, examples=[
            {"company_key": "HUBS", "want": "no", "chip": "c", "source": "card", "pin": True},
            {"company_key": "RXT", "want": "no", "chip": "e", "source": "card", "pin": True},
            {"company_key": "BOX", "want": "partial", "chip": "b", "source": "card", "pin": True},
            {"security_id": "X:SAIL", "want": "explicit", "source": "sieve", "pin": False},
            {"company_key": "A1", "want": "unsure", "source": "card", "pin": False}])
        base_reads = {k: {"security_id": f"X:{k}", "name": k.title(), "p_pos": p, "l2_label": "partial",
                          "keyword_hit": k != "LOWKW", "market_cap_usd": 1e9} for k, p in self.BASE.items()}
        inputs = {k: {"company_key": k, "text": f"[annual report excerpts: SEC 10-K filed 2026-01-01; language en]"
                                                f"\n\n{k} text"} for k in self.BASE}
        return sv, base_reads, inputs

    def run_trial(self, cands, effects, **kw):
        sv, base_reads, inputs = self.setup()
        client = TrialJev(self.BASE, effects)
        out = calib.trial_rules([{"id": c, "version": 1} for c in cands], sv, base_reads, client, inputs=inputs,
                                idea="idea x", now="t", **kw)
        return out, client, sv

    def test_adopts_when_agreement_rises(self):
        out, client, sv = self.run_trial(["user_not_supplier"], {"user_not_supplier": {"HUBS": 0.3}})
        self.assertEqual((out["evaluation"], out["anchors"]), (4, 4))    # A1 (unsure) is an anchor, LOWKW is not
        self.assertEqual([c["id"] for c in out["adopted"]], ["user_not_supplier"])
        t = out["trials"][0]
        self.assertEqual((t["agree_before"], t["agree_after"], t["items"], t["adopted"]), (2, 3, 16, True))
        self.assertEqual([c[0] for c in client.calls], [0, 1])
        self.assertEqual(out["adopted"][0]["trial"], {"items": 16, "agree_before": 2, "agree_after": 3,
                                                      "cost_usd": 0.002})
        self.assertEqual(out["cost_usd"], 0.002)
        sv2 = calib.adopt_rules(sv, out)
        self.assertEqual([r["id"] for r in sv2["rules"]], ["user_not_supplier"])
        self.assertIn("一致 2→3", calib.render_trial_zh(out))
        self.assertIn(calib.IN_SAMPLE_NOTE_ZH, calib.render_trial_zh(out))

    def test_rejections(self):
        out, _, sv = self.run_trial(["laundry_list"], {"laundry_list": {"RXT": 0.3, "BOX": 0.4}})
        self.assertEqual(out["adopted"], [])
        self.assertEqual(out["rejected"], [{"id": "laundry_list", "why_zh": "这条规则会误伤 Box，未采用", "at": "t"}])
        out, _, _ = self.run_trial(["laundry_list"], {"laundry_list": {"RXT": 0.3, "SAIL": 0.2}})
        self.assertEqual(out["rejected"][0]["why_zh"], "这条规则会误伤 Sail，未采用")        # should_pass check
        out, _, _ = self.run_trial(["mention_only"], {"mention_only": {"HUBS": 0.3, "A1": 0.2, "A2": 0.2,
                                                                       "A3": 0.2}})
        self.assertEqual(out["rejected"][0]["why_zh"], "会让 3 家高分公司掉出（A1、A2、A3），未采用")
        out, _, _ = self.run_trial(["mention_only"], {"mention_only": {"HUBS": 0.3, "A1": 0.2, "A2": 0.2}})
        self.assertEqual([c["id"] for c in out["adopted"]], ["mention_only"])                # 2 anchors: allowed
        out, _, _ = self.run_trial(["mention_only"], {})
        self.assertEqual(out["rejected"][0]["why_zh"], "和你的回答一致度没有提高（2→2），未采用")
        sv2 = calib.adopt_rules(sv, out)
        self.assertEqual([r["id"] for r in sv2["rejected_rules"]], ["mention_only"])

    def test_falls_back_to_single_rules_and_honours_the_budget(self):
        effects = {"user_not_supplier": {"HUBS": 0.3}, "laundry_list": {"RXT": 0.3, "BOX": 0.4}}
        out, client, _ = self.run_trial(["user_not_supplier", "laundry_list"], effects)
        self.assertEqual([t["rules"] for t in out["trials"]],
                         [["user_not_supplier", "laundry_list"], ["user_not_supplier"], ["laundry_list"]])
        self.assertEqual([c["id"] for c in out["adopted"]], ["user_not_supplier"])
        self.assertEqual([r["id"] for r in out["rejected"]], ["laundry_list"])
        self.assertEqual([c[2] for c in client.calls][:2], [["laundry_list", "user_not_supplier"]] * 2)
        # both together pass: one trial only
        out, client, _ = self.run_trial(["user_not_supplier", "laundry_list"],
                                        {"user_not_supplier": {"HUBS": 0.3}, "laundry_list": {"RXT": 0.3}})
        self.assertEqual(len(out["trials"]), 1)
        self.assertEqual([c["id"] for c in out["adopted"]], ["user_not_supplier", "laundry_list"])
        # budget: the first trial would cost 0.002 > 0.0015 -> nothing is sent
        out, client, _ = self.run_trial(["user_not_supplier", "laundry_list"], effects, budget_usd=0.0015)
        self.assertEqual((out["status"], out["trials"], client.calls), ("budget", [], []))
        self.assertEqual(out["untried"], ["user_not_supplier", "laundry_list"])
        self.assertIn("没试（预算不够）", calib.render_trial_zh(out))
        # after the combined trial (0.002 + BOX's re-reads 0.002 + the control 0.002, v1.1) the budget allows one
        # more: the rest stays untried
        out, client, _ = self.run_trial(["user_not_supplier", "laundry_list"], effects, budget_usd=0.0085)
        self.assertEqual(len(out["trials"]), 2)
        self.assertEqual(out["untried"], ["laundry_list"])
        out, client, _ = self.run_trial(["user_not_supplier", "laundry_list"], effects, max_trials=1)
        self.assertEqual(len(out["trials"]), 1)


# ---------------------------------------------------------------------------------------------------------------
# Keywords

YES_JA = [
    "KUMOGA株式会社は、クラウドの認証基盤「KUMOGA One」を提供しております。シングルサインオン（SAML）と多要素認証に対応し、"
    "ワンタイムパスワードも提供しております。Security Assertion Markup Language に準拠しております。",
    "当社は、クラウド型の企業向けの認証サービスとして、シングルサインオンとSAML連携、多要素認証を提供しております。KUMOGA One と連携し、"
    "ワンタイムパスワードにも対応しております。",
    "当社の統合認証製品は、シングルサインオンとSAMLに対応しております。",
]
NOISE_JA = "当社は、情報セキュリティマネジメントシステム（ISMS）の認証を取得しております。"
CLOUD_JA = "当社は、クラウドサービスを提供しております。"
FILLER_JA = "当社は、食品を製造し、全国のスーパーマーケットに販売しております。"


def ja_corpus():
    docs = [(f"Y{i}", t) for i, t in enumerate(YES_JA)]
    docs += [(f"N{i}", NOISE_JA) for i in range(8)]
    docs += [(f"C{i}", CLOUD_JA) for i in range(20)]
    docs += [(f"F{i}", FILLER_JA) for i in range(29)]
    return docs


REL = ["Y0", "Y1", "Y2"] + [f"F{i}" for i in range(9)]     # L1 core / adjacent: 12 of 60 (base rate 20%)


class TestKeywords(unittest.TestCase):
    def test_doc_freq_lift_and_noisy_seeds(self):
        df = calib.doc_freq(ja_corpus(), ["認証", "シングルサインオン", "多要素認証", "IDaaS", "ｓａｍｌ"])
        self.assertEqual((df["n"], calib.df_count(df, "認証"), calib.df_count(df, "シングルサインオン")), (60, 11, 3))
        self.assertEqual(calib.df_count(df, "ｓａｍｌ"), 3)            # full-width / NFKC matching as in excerpts
        self.assertAlmostEqual(calib.lift_of(df, "シングルサインオン", REL), 5.0)
        self.assertIsNone(calib.lift_of(df, "IDaaS", REL))
        noisy = calib.noisy_terms(None, None, ["認証", "多要素認証", "IDaaS"], "ja", df=df)
        self.assertEqual((noisy["weak"], noisy["absent"], noisy["source_id"]), (["認証"], ["IDaaS"], "edinet_yuho"))
        self.assertEqual(noisy["stats"]["認証"], {"df": 11, "share": 0.1833})
        self.assertEqual(noisy["stats"]["多要素認証"], {"df": 2, "share": 0.0333})    # under 5%: stays normal
        edge = calib.noisy_terms(None, None, ["シングルサインオン"], "ja", df=df)
        self.assertEqual(edge["weak"], ["シングルサインオン"])                         # 3 / 60 = exactly 5%: weak
        self.assertEqual(calib.keyword_log_zh("ja", ["シングルサインオン", "SAML"], noisy),
                         "日文关键词 +シングルサインオン +SAML；「認証」降权（18.3% 的日本年报都有）；IDaaS 年报里没出现")
        self.assertEqual(calib.keyword_log_zh("zh", []), "中文关键词：没有变化")

    def test_mine_terms(self):
        corpus = ja_corpus()
        no_docs = ["当社は、ワンタイムパスワードの発行サービスを提供しております。"]
        got = calib.mine_terms("ja", YES_JA, ["認証"], no_docs, ["KUMOGA株式会社"],
                               lambda terms: calib.doc_freq(corpus, terms), REL)
        self.assertEqual(got["add"], ["SAML", "シングルサインオン"])    # 多要素認証: 2 of 3 yes companies (v1.1)
        why = {r["term"]: r["why"] for r in got["rejected"]}
        self.assertEqual(why["多要素認証"], "yes_docs")
        self.assertEqual(why["KUMOGA"], "yes_docs")                     # 2 of 3 (v1.1); the name gate below
        named = calib.mine_terms("ja", YES_JA, ["認証"], [], ["シングルサインオン株式会社"],
                                 lambda terms: calib.doc_freq(corpus, terms), REL)
        self.assertEqual({r["term"]: r["why"] for r in named["rejected"]}["シングルサインオン"], "name")
        self.assertEqual(why["ワンタイムパスワード"], "yes_docs")          # 2 of 3 (v1.1); the no-answer gate below
        refused = calib.mine_terms("ja", YES_JA, ["認証"], ["当社は、シングルサインオンの代理店です。"], [],
                                   lambda terms: calib.doc_freq(corpus, terms), REL)
        self.assertEqual({r["term"]: r["why"] for r in refused["rejected"]}["シングルサインオン"], "no_answer")
        self.assertEqual(why["クラウド"], "yes_docs")                     # 2 of 3 (v1.1); the df gate below
        common = corpus + [(f"S{i}", "当社はシングルサインオンを利用しております。") for i in range(10)]
        dfr = calib.mine_terms("ja", YES_JA, ["認証"], [], [], lambda terms: calib.doc_freq(common, terms), REL)
        self.assertTrue({r["term"]: r["why"] for r in dfr["rejected"]}["シングルサインオン"].startswith("df "))
        self.assertNotIn("Markup", why)                                  # not an all-caps acronym: never a candidate
        self.assertEqual(got["log"][0], {"term": "SAML", "action": "add", "source": "mined", "df": 3, "lift": 5.0,
                                         "yes_docs": 3, "gate": calib.MINE_GATE_VERSION})
        # no L1-relevant companies: no lift, nothing is added; en is not mined in v1
        none = calib.mine_terms("ja", YES_JA, ["認証"], [], [], lambda terms: calib.doc_freq(corpus, terms), [])
        self.assertEqual(none["add"], [])
        self.assertTrue({r["why"] for r in none["rejected"]} >= {"lift None < 3.0"})
        self.assertEqual(calib.mine_terms("en", ["x"], ["y"], [], [], {}, [])["add"], [])

    def test_merge_keywords(self):
        sv = sieve_doc(keywords={"ja": {"add": ["SAML"], "weak": [], "log": []}})
        sv2 = calib.merge_keywords(sv, "ja", add=["シングルサインオン", "SAML", "認証"], weak=["認証"],
                                   log=[{"term": "認証", "action": "weak"}], now="t")
        self.assertEqual(sv2["keywords"]["ja"], {"add": ["SAML", "シングルサインオン"], "weak": ["認証"],
                                                 "log": [{"term": "認証", "action": "weak", "at": "t"}]})
        self.assertEqual(calib.validate_sieve(sv2), [])
        self.assertEqual(sv["keywords"]["ja"]["add"], ["SAML"])          # not mutated

    def test_background_df_is_cached_per_source(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cfg = config.Config(home=home)
            texts = {"a": YES_JA[0], "b": NOISE_JA, "c": FILLER_JA}
            cols = ("doc_id", "security_id", "company_key", "source_id", "text_path", "fetched_at")
            for k, t in texts.items():
                (home / f"{k}.txt").write_text(t, encoding="utf-8")
            with store.session(cfg) as con:
                store.upsert_many(con, "documents", cols, [
                    (f"edinet_yuho:{k}", None, f"isin:{k}", "edinet_yuho", str(home / f"{k}.txt"),
                     dt.datetime(2026, 9, 1)) for k in texts])
                store.upsert_many(con, "documents", cols, [
                    ("sec_filing_text:x", None, "isin:x", "sec_filing_text", str(home / "a.txt"),
                     dt.datetime(2026, 9, 1))])
            with store.session(cfg, read_only=True) as con:
                df = calib.background_df(cfg, con, "edinet_yuho", ["認証", "SAML"])
            self.assertEqual((df["n"], calib.df_count(df, "認証"), calib.df_count(df, "SAML")), (3, 2, 1))
            self.assertEqual(sorted(df["doc_companies"]), ["isin:a", "isin:b", "isin:c"])
            cache = home / "calib" / "df-edinet_yuho.json"
            self.assertTrue(cache.exists())
            (home / "b.txt").unlink()                                   # cached terms are not recounted
            with store.session(cfg, read_only=True) as con:
                self.assertEqual(calib.df_count(calib.background_df(cfg, con, "edinet_yuho", ["認証"]), "認証"), 2)
                # a new term reads the texts again (b.txt is gone: it counts as empty)
                self.assertEqual(calib.df_count(calib.background_df(cfg, con, "edinet_yuho", ["当社"]), "当社"), 1)
                noisy = calib.noisy_terms(cfg, con, ["認証"], "ja")
            self.assertEqual(noisy["weak"], ["認証"])
            with store.session(cfg) as con:          # a new document: counted alone (incremental), not a rebuild
                store.upsert_many(con, "documents", cols, [("edinet_yuho:d", None, "isin:d", "edinet_yuho",
                                                            str(home / "a.txt"), dt.datetime(2026, 9, 2))])
            with store.session(cfg, read_only=True) as con:
                df = calib.background_df(cfg, con, "edinet_yuho", ["認証"])
            self.assertEqual((df["n"], calib.df_count(df, "認証")), (4, 3))
            with store.session(cfg) as con:          # a removed document rebuilds (b.txt is gone: counts as empty)
                con.execute("DELETE FROM documents WHERE doc_id = 'edinet_yuho:d'")
            with store.session(cfg, read_only=True) as con:
                df = calib.background_df(cfg, con, "edinet_yuho", ["認証"])
            self.assertEqual((df["n"], calib.df_count(df, "認証")), (3, 1))


class TestPeerPreview(unittest.TestCase):
    OVERVIEW = "当社グループは、企業向けソフトウェアの開発・販売を主な事業としております。" * 9

    def entry(self, d: Path, key, name, para, lang_text=None):
        p = d / f"{key}.txt"
        p.write_text(lang_text or "\n\n".join([self.OVERVIEW, para]), encoding="utf-8")
        c = {"company_key": key, "security_id": f"TSE:{key}", "name": name,
             "desc": {"plain": f"{name} makes software.", "text": f"[tradingview_profile] {name} makes software.",
                      "tier": "gray-private"}}
        doc = {"doc_id": key, "source_id": "edinet_yuho", "form": "有価証券報告書", "filing_date": "2026-06-20",
               "url": None, "text_path": str(p)}
        return c, doc

    def test_lists_changes_and_protects_yes_companies(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            entries = [
                self.entry(d, "9417", "SecuEx", "当社は、情報セキュリティマネジメントシステムの認証を取得し、品質管理体制の強化を継続しております。"),
                self.entry(d, "9326", "DigiArc", "当社の製品は、クラウドサービス向けのシングルサインオンに対応し、企業のお客様に提供しております。"),
                self.entry(d, "9475", "KUMOGA", "当社は、シングルサインオンと多要素認証を組み合わせた認証基盤を開発し、企業のお客様に提供しております。"),
                self.entry(d, "EN1", "English Co", "", lang_text="Item 1. Business\n\nEnglish Co sells software "
                                                                 "for identity management and single sign-on to "
                                                                 "enterprises across the world.")]
            old = {"terms": ["認証"], "weak": []}
            new = {"terms": ["認証", "シングルサインオン"], "weak": ["認証"]}
            prev = calib.peer_preview(entries, "ja", old, new, today=dt.date(2026, 9, 27))
            got = {x["name"]: (x["hit_before"], x["hit_after"]) for x in prev["changes"]}
            self.assertEqual(got, {"SecuEx": (True, False), "DigiArc": (False, True)})
            self.assertFalse(prev["rejected"])
            self.assertEqual(calib.render_peer_preview_zh(prev, "ja"),
                             "日文关键词影响预览：2 家公司的摘录会变\n  SecuEx 9417：失去关键词段\n  DigiArc 9326：新增关键词段")
            prev = calib.peer_preview(entries, "ja", old, new, protected=["TSE:9417"], today=dt.date(2026, 9, 27))
            self.assertTrue(prev["rejected"])
            self.assertEqual(prev["why_zh"], "会让 SecuEx 失去关键词段落（你答了要），未采用")


class TestCardText(unittest.TestCase):
    def test_first_sentence_skips_abbreviations(self):
        self.assertEqual(calib._first_sentence("[tradingview_profile] Acme Cloud Technology, Inc. operates a "
                                               "multicloud platform. It is listed.", 160),
                         "Acme Cloud Technology, Inc. operates a multicloud platform.")
        self.assertEqual(calib._first_sentence("Acme Co. Ltd. makes things. More.", 160), "Acme Co. Ltd. makes things.")
        self.assertEqual(calib._first_sentence("RoboCorp makes robot arms. It is big.", 160), "RoboCorp makes robot arms.")
        self.assertEqual(calib._first_sentence("公司主营终端安全。其他业务。", 160), "公司主营终端安全。")



# ---------------------------------------------------------------------------------------------------------------
# Review fixes (2026-09-27): each class pins one confirmed finding

class AdditiveJev(TrialJev):
    """TrialJev whose rule effects add up: p = base + the deltas of every rule in the question (so each rule alone
    can pass while the combination knocks out a yes company)."""

    def classify(self, items, question):
        rules = {rid for rid, m in MARKERS.items() if m in question.criteria["insufficient"]}
        self.calls.append((question.read, [it.item_id for it in items], sorted(rules)))
        self.spent_usd += self.cost
        out = []
        for it in items:
            p = self.base[it.item_id] + sum(self.effects.get(r, {}).get(it.item_id, 0.0) for r in rules)
            out.append({"item_id": it.item_id, "label": "partial" if p >= 0.5 else "insufficient",
                        "probs": {"explicit": p / 2, "partial": p / 2, "contradicted": 0.0, "insufficient": 1 - p},
                        "request_id": "r", "status": "ok", "error": None, "cached": False})
        return out


class GapJev(TrialJev):
    """TrialJev that returns no answer (status `status`) for the companies in `missing`, without raising: what
    JevClient does when the budget runs out mid-trial or a packet fails after its retries."""

    def __init__(self, base, effects, missing, status):
        super().__init__(base, effects)
        self.missing, self.status = set(missing), status

    def classify(self, items, question):
        out = super().classify(items, question)
        for x in out:
            if x["item_id"] in self.missing:
                x.update(status=self.status, label=None, probs={})
        return out


class TestTrialGateFixes(unittest.TestCase):
    BASE = TestTrialRules.BASE
    setup = TestTrialRules.setup

    def run_with(self, client, cands, **kw):
        sv, base_reads, inputs = self.setup()
        out = calib.trial_rules([{"id": c, "version": 1} for c in cands], sv, base_reads, client, inputs=inputs,
                                idea="idea x", now="t", **kw)
        return out, sv

    def test_a_rejected_combination_is_never_adopted_through_its_singles(self):
        # each rule alone fixes one 'no' and costs SAIL (should_pass, 0.9) 0.25; together SAIL drops to 0.4
        effects = {"user_not_supplier": {"HUBS": -0.4, "SAIL": -0.25}, "laundry_list": {"RXT": -0.3, "SAIL": -0.25}}
        out, sv = self.run_with(AdditiveJev(self.BASE, effects), ["user_not_supplier", "laundry_list"])
        self.assertEqual([(t["rules"], t["adopted"]) for t in out["trials"]],
                         [(["user_not_supplier", "laundry_list"], False), (["user_not_supplier"], True),
                          (["laundry_list"], True)])
        self.assertEqual([c["id"] for c in out["adopted"]], ["laundry_list"])       # the best single only (tie: id)
        self.assertEqual(out["rejected"], [{"id": "user_not_supplier", "at": "t", "why_zh":
                                            "单独试可以，但和 laundry_list 一起试不行（这条规则会误伤 Sail）；"
                                            "这次只采用 laundry_list"}])
        self.assertEqual([r["id"] for r in calib.adopt_rules(sv, out)["rules"]], ["laundry_list"])
        text = calib.render_trial_zh(out)
        self.assertIn("试 user_not_supplier：一致 2→3（$0.0020）· 单独试可以，但和 laundry_list 一起试不行", text)
        self.assertIn("试 laundry_list：一致 2→3（$0.0020）· 采用", text)

    def test_three_candidates_try_the_union_of_the_passing_singles(self):
        effects = {"user_not_supplier": {"HUBS": -0.4}, "laundry_list": {"RXT": -0.3}, "mention_only": {"BOX": -0.5}}
        out, _ = self.run_with(AdditiveJev(self.BASE, effects), ["user_not_supplier", "laundry_list", "mention_only"])
        self.assertEqual([(t["rules"], t["adopted"]) for t in out["trials"]],
                         [(["user_not_supplier", "laundry_list", "mention_only"], False),
                          (["user_not_supplier"], True), (["laundry_list"], True), (["mention_only"], False),
                          (["user_not_supplier", "laundry_list"], True)])
        self.assertEqual([(c["id"], c["trial"]["agree_after"]) for c in out["adopted"]],
                         [("user_not_supplier", 4), ("laundry_list", 4)])          # adopted as the tested set
        self.assertEqual([r["id"] for r in out["rejected"]], ["mention_only"])
        # no room for the union trial: the best single only, the other one says why
        out, _ = self.run_with(AdditiveJev(self.BASE, effects), ["user_not_supplier", "laundry_list", "mention_only"],
                               max_trials=4)
        self.assertEqual([c["id"] for c in out["adopted"]], ["laundry_list"])
        self.assertEqual({r["id"]: r["why_zh"] for r in out["rejected"]}["user_not_supplier"],
                         "单独试可以，但和 laundry_list 一起没能试：试验次数用完；这次只采用 laundry_list")
        # the estimate covers the union trial too
        sv, base_reads, inputs = self.setup()
        cands = [{"id": c, "version": 1} for c in ("user_not_supplier", "laundry_list", "mention_only")]
        client = TrialJev(self.BASE, {})
        self.assertEqual([calib.estimate_trials(cands[:n], sv, base_reads, client, inputs=inputs, idea="x")["trials"]
                          for n in (1, 2, 3)], [1, 3, 5])

    def test_an_incomplete_trial_adopts_nothing(self):
        effects = {"user_not_supplier": {"HUBS": 0.3, "A1": 0.1, "A2": 0.1, "A3": 0.1, "A4": 0.1}}
        anchors = ["A1", "A2", "A3", "A4"]
        # the budget ran out before the anchors (the rule knocks all of them out): nothing may be adopted
        out, sv = self.run_with(GapJev(self.BASE, effects, anchors, "skipped_budget"), ["user_not_supplier"])
        self.assertEqual((out["status"], out["adopted"], out["rejected"], out["untried"]),
                         ("budget", [], [], ["user_not_supplier"]))
        self.assertEqual(out["trials"][0]["why_zh"], "Jev 没读完（4 家没有结果），结果不完整，未采用")
        self.assertIn("user_not_supplier：没试（预算不够）", calib.render_trial_zh(out))
        # packets that failed after their retries (no exception): incomplete too
        out, _ = self.run_with(GapJev(self.BASE, {"user_not_supplier": {"HUBS": 0.3}}, ["A1"], "failed"),
                               ["user_not_supplier"])
        self.assertEqual((out["status"], out["adopted"], out["untried"]), ("incomplete", [], ["user_not_supplier"]))
        self.assertIn("没试（Jev 中途停了）", calib.render_trial_zh(out))
        # the same trial with every company read is adopted
        out, _ = self.run_with(GapJev(self.BASE, {"user_not_supplier": {"HUBS": 0.3}}, [], "failed"),
                               ["user_not_supplier"])
        self.assertEqual((out["status"], [c["id"] for c in out["adopted"]]), ("ok", ["user_not_supplier"]))

    def test_the_two_reads_are_different_packets(self):
        client = TrialJev(self.BASE, {"user_not_supplier": {"HUBS": 0.3}})
        self.run_with(client, ["user_not_supplier"])
        (r0, ids0, _), (r1, ids1, _) = client.calls
        self.assertEqual((r0, r1), (0, 1))
        self.assertEqual(sorted(ids0), sorted(ids1))
        self.assertNotEqual(ids0, ids1)                       # not a byte-identical (paid duplicate) payload
        for r, ids in ((r0, ids0), (r1, ids1)):               # the band-read order, deterministic
            import hashlib
            self.assertEqual(ids, sorted(ids, key=lambda k: hashlib.sha256(f"{r}:{k}".encode()).hexdigest()))


class TestRuleTextFixes(unittest.TestCase):
    def test_keyword_terms_are_not_tickers(self):
        text = calib.render_rules([{"id": "homonym", "terms": ["SAML", "認証", "SSO"]}], FACETS)[0][2]
        self.assertEqual(calib.validate_rule_text(text, tickers=["SAML"]), ["含股票代码 SAML"])     # OTC:SAML
        self.assertEqual(calib.validate_rule_text(text, tickers=["SAML"], terms=["SAML", "認証"]), [])
        # names are still refused, keyword term or not
        leak = calib.render_rules([{"id": "homonym", "terms": ["Hivebay"]}], FACETS)[0][2]
        self.assertEqual(calib.validate_rule_text(leak, names=["Hivebay, Inc."], terms=["Hivebay"]),
                         ["含公司名 Hivebay"])
        self.assertEqual(calib.keyword_terms({"en": ["SSO"], "ja": ["認証"]},
                                             {"keywords": {"ja": {"add": ["SAML"], "weak": []}}}), ["SSO", "認証", "SAML"])
        self.assertEqual(calib.rule_text_problems([{"id": "homonym", "terms": ["SAML"]}, "mention_only"], FACETS,
                                                  tickers=["SAML"]),
                         [("homonym", "insufficient", ["含股票代码 SAML"])])

    def test_a_homonym_on_an_acronym_card_is_proposed(self):
        cards = [{"n": 1, "type": "boundary_in", "name": "Henn Co", "security_id": "TSE:9475",
                  "quote": {"text": "当社はSAMLとシングルサインオンを提供。", "matched_terms": ["SAML", "シングルサインオン"]}}]
        deck = {"deck_id": "deck-r-1", "cards": cards, "chips": {"no": {k: "" for k in "cdef"}}}
        cand, dropped = calib.candidate_rules(sieve_doc(facets=FACETS), deck, calib.parse_answers("1不要d", deck),
                                              names=["Samsara Luggage, Inc."], tickers=["SAML"])
        self.assertEqual(([(c["id"], c["terms"]) for c in cand], dropped),
                         ([("homonym", ["SAML", "シングルサインオン"])], []))


class TestMiningFixes(unittest.TestCase):
    def test_zh_compounds_inside_a_clause(self):
        got = calib._candidates_in("公司为客户提供身份认证和访问管控服务，AI代理调用需要权限管理平台授权。", "zh")
        self.assertTrue({"身份认证", "访问管控", "权限管理"} <= got)
        self.assertFalse({c for c in got if c[0] in "的和与"})                # no function-word starts
        self.assertIn("統合認証", calib._candidates_in("クラウド型統合認証基盤", "ja"))
        yes = ["公司面向智能体提供零信任身份认证与特权账号管理产品。", "我们的智能体安全平台包括零信任身份认证，以及特权账号管理。"]
        corpus = [("Z0", yes[0]), ("Z1", yes[1])] + [(f"F{i}", "公司生产食品并在全国销售。") for i in range(40)]
        rel = ["Z0", "Z1"] + [f"F{i}" for i in range(8)]
        got = calib.mine_terms("zh", yes, ["智能体"], [], [], lambda t: calib.doc_freq(corpus, t), rel)
        self.assertEqual(got["add"], ["特权账号管理", "零信任身份认证"])


class TestCardFixes(unittest.TestCase):
    def test_names_carry_the_ticker_outside_the_us(self):
        d = calib._display
        self.assertEqual(d({"name": "Beijing Beichen Xinan Software Co., Ltd. Class A", "security_id": "SZSE:309352"}),
                         "Beijing Beichen Xinan Software Co., Ltd. 309352")
        self.assertEqual(d({"name": "KUMOGA K.K.", "security_id": "TSE:9475"}), "KUMOGA K.K. 9475")
        self.assertEqual(d({"name": "恒远世通", "security_id": "SZSE:309161"}), "恒远世通 309161")
        self.assertEqual(d({"name": "Hivebay, Inc.", "security_id": "NYSE:HVBQ"}), "Hivebay, Inc.")
        self.assertEqual(d({"name": "Altavia Inc. Class A", "security_id": "NASDAQ:ALTV"}), "Altavia Inc. Class A")
        row = {"company_key": "k", "security_id": "SZSE:309161", "rank": 20,
               "name": "Hengyuan Shitong Technology Co., Ltd. Class A"}
        self.assertEqual(calib.render_diff_zh({"rows": [row]}, {"rows": [], "excluded_by_user": [row]}),
                         "立即生效（免费）\n  移出 1：Hengyuan Shitong Technology Co., Ltd. 309161 #20（你：不要）")

    def profile_card(self, **kw):
        c = {"company_key": "k:QORN", "security_id": "SET:QORN", "name": "Qorin Public Company Limited", "rank": None,
             "l2_label": "partial", "l2_evidence": "profile", "l2_p_pos": 0.85, "l2_p_explicit": 0.2,
             "l2_p_partial": 0.65, "l1_description": "Qorin provides offline-to-online solutions. More.",
             "l1_input_tier": "gray-private", **kw}
        inp = {"company_key": "k:QORN", "evidence": "profile", "text": "[company profile]\nQorin provides "
               "offline-to-online solutions. More.", "excerpts": [], "evidence_sha": "s"}
        return calib.make_card(5, "boundary_out", c, inp, None, pi=0.1, max_out=40)

    def test_a_profile_card_says_there_is_no_annual_report(self):
        card = self.profile_card()
        self.assertEqual(card["why_zh"], "简介判为相关/明确（合计 85%），但排在 40 名外")
        md = calib.render_cards_md({"idea": "x", "chips": {"no": {}}, "cards": [card]})
        self.assertIn("  年报：（没有年报文本，系统只读了公司简介）", md)
        self.assertEqual(md.count("Qorin provides offline-to-online solutions."), 1)        # not printed twice
        self.assertNotIn("简介：「", md)
        other = self.profile_card(l1_description="Qorin is a Thai company.")
        self.assertIn("  简介：「Qorin provides offline-to-online solutions.」",
                      calib.render_cards_md({"idea": "x", "chips": {"no": {}}, "cards": [other]}))

    def test_the_source_note_names_the_form_and_the_release_year(self):
        card = {"n": 1, "type": "boundary_in", "name": "北辰信安", "security_id": "SZSE:309352", "rank": 1,
                "what": {"text": "x", "source": "公司简介", "tier": None},
                "quote": {"text": "q", "source": "CNINFO", "form": "annual_report_summary", "filing_date": "2026-04-30"},
                "verdict": {"label": "explicit", "p_explicit": 0.56, "p_partial": 0.42, "reads": [0.98]},
                "why_zh": "w"}
        md = calib.render_cards_md({"idea": "x", "chips": {"no": {}}, "cards": [card]})
        self.assertIn("  年报：「q」（CNINFO 年报摘要，2026 年发布）", md)
        for src, form, zh in (("SEC", "10-K", "SEC 10-K 年报"), ("EDINET", "有価証券報告書", "EDINET 有价证券报告书（年报）"),
                              ("DART", "사업보고서", "DART 事业报告（年报）")):
            card["quote"].update(source=src, form=form)
            self.assertIn(f"（{zh}，2026 年发布）", calib.render_cards_md({"idea": "x", "chips": {"no": {}},
                                                                          "cards": [card]}))

    def test_recheck_only_when_the_filing_changed(self):
        result, inputs, pool, sv = synthetic_run()
        types = {c["security_id"]: c for c in calib.select_cards(result, inputs, sv, pool=pool)}
        self.assertEqual(types["NYSE:PINR"]["why_zh"], "你上次答过，但年报换了新版本")      # 2025 filing -> 2026
        # the same filing, only the excerpt changed (a keyword change): no recheck card
        sv["examples"][0]["evidence_filing"] = calib.filing_key("SEC", "10-K", "2026-03-01")
        self.assertNotIn("NYSE:PINR", {c["security_id"] for c in calib.select_cards(result, inputs, sv, pool=pool)})
        # an answer recorded without its filing: rechecked, without claiming a new filing
        del sv["examples"][0]["evidence_filing"]
        card = next(c for c in calib.select_cards(result, inputs, sv, pool=pool) if c["security_id"] == "NYSE:PINR")
        self.assertEqual((card["type"], card["why_zh"]), ("recheck", "你上次答过，但系统读的年报摘录变了"))
        # answers record the filing of their card
        deck = calib.build_deck(result, inputs, sv, pool=pool, now="t")
        n = next(c["n"] for c in deck["cards"] if c["security_id"] == "NYSE:RYKN")
        _, new = calib.record_answers(sv, deck, calib.parse_answers(f"{n}不要e", deck), now="t")
        self.assertEqual(new[0]["evidence_filing"], "SEC|10-K|2026-03-01")


class TestAnswerFormFixes(unittest.TestCase):
    DECK = TestParseAnswers.DECK

    def parse(self, text):
        return [tuple(a) for a in calib.parse_answers(text, self.DECK)]

    def test_spaced_chips_and_full_stops(self):
        for text in ("1不要 c", "1 不要 c", "1 no c", "1. 不要 c", "1不要c。"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), [(1, "no", None, "c")])
        self.assertEqual(self.parse("1 要 a"), [(1, "yes", "explicit", "a")])
        self.assertEqual(self.parse("1要b。2不要e"), [(1, "yes", "partial", "b"), (2, "no", None, "e")])
        self.assertEqual(self.parse("1要b 2 不要 e。"), [(1, "yes", "partial", "b"), (2, "no", None, "e")])
        self.assertEqual(self.parse("1要a 2不要c 3? …"), [(1, "yes", "explicit", "a"), (2, "no", None, "c"),
                                                        (3, "unsure", None, None)])     # the legend, copied
        with self.assertRaises(calib.AnswerError) as cm:
            calib.parse_answers("3 其余都要", self.DECK)
        self.assertEqual(str(cm.exception), "第 3 张：「3」没写要还是不要（例如 3要a 或 3不要c）")
        self.assertEqual(calib.want_zh("no", "e"), "不要e")
        self.assertEqual([calib.want_zh(w) for w in ("explicit", "partial", "unsure")], ["要a", "要b", "?"])


if __name__ == "__main__":
    unittest.main()
