"""Tests for jevscreen.scope (scope design §3-§5): the split rule, the questions' wording, idea-wording
defaults, recording the human's answers and their enforcement in the ranking step. Pure: no store, no network,
synthetic rows and labels shaped like the accuracy baseline's groups."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, scope, screen  # noqa: E402

CJK = re.compile(r"[㐀-鿿]")

SEA = {"format": calib.SIEVE_FORMAT, "version": 3, "idea": "东南亚的数字支付", "idea_key": "k",
       "facets": {"category": "digital payments", "target": "Southeast Asia"},
       "facets_zh": {"category": "数字支付", "target": "东南亚"}, "examples": [], "rules": [], "history": []}
TECH = {"format": calib.SIEVE_FORMAT, "version": 1, "idea": "AI agent identity", "idea_key": "t",
        "facets": {"category": "identity and access management", "target": "AI agents"},
        "facets_zh": {"category": "身份与权限管理", "target": "AI 智能体"}, "examples": [], "rules": [], "history": []}
PLAIN = {"format": calib.SIEVE_FORMAT, "version": 1, "idea": "谐波减速器", "idea_key": "p", "examples": [], "rules": [],
         "history": []}


def rows_and_labels(spec: list[tuple[str, float, str]], family: str = "role", evidence: str = "annual_report"
                    ) -> tuple[list[dict], dict]:
    """rows ranked 1..n and a facets map from [(label, p, evidence?)]."""
    rows, fmap = [], {}
    for i, (label, p, *ev) in enumerate(spec, 1):
        ck = f"ck{i}"
        rows.append({"company_key": ck, "security_id": f"X:{i:04d}", "name": f"Co {i} Ltd", "rank": i,
                     "user_verdict": None, "l2_evidence": ev[0] if ev else evidence, "evidence_sha": f"sha{i}",
                     "l2_label": "explicit", "score": 10 - i * 0.01, "market_cap_usd": 1e9, "l1_p_core": 0.9})
        fmap[ck] = {family: {"label": label, "p": p, "n": 1, "evidence_sha": f"sha{i}"}}
    return rows, fmap


def forty(v_label: str, v: int, keep: int, *, p: float = 0.9, family: str = "role", keep_label: str = "supplier"):
    spec = [(v_label, p)] * v + [(keep_label, 0.9)] * keep
    spec += [("unclear", 0.8)] * (40 - len(spec))
    return rows_and_labels(spec, family)


class Splits(unittest.TestCase):
    def test_thresholds(self):
        rows, fmap = forty("hardware", 9, 25)
        s = scope.find_splits(rows, fmap, SEA, top_n=40)
        self.assertEqual([(x["family"], x["value"], x["chip"], len(x["V"])) for x in s], [("role", "hardware", "k", 9)])
        rows, fmap = forty("buyer", 3, 30)
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40), [])       # 3 buyers: the agent handles them
        rows, fmap = forty("hardware", 30, 5)
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40), [])       # the K side fails (5 < 8)

    def test_hysteresis_and_neutral(self):
        rows, fmap = forty("hardware", 9, 25, p=0.65)                           # counts to enforce, not to ask
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40), [])
        rows, fmap = forty("unclear", 20, 20)                                    # unclear is neutral: no split
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40), [])

    def test_general_only_ignores_profile_rows(self):
        spec = [("general_only", 0.9, "profile")] * 6 + [("general_only", 0.9)] * 3 + [("specific", 0.9)] * 20
        rows, fmap = rows_and_labels(spec, "scope")
        self.assertEqual(scope.find_splits(rows, fmap, TECH, top_n=40), [])      # 3 annual-report rows only
        spec = [("general_only", 0.9)] * 5 + [("specific", 0.9)] * 20
        rows, fmap = rows_and_labels(spec, "scope")
        s = scope.find_splits(rows, fmap, TECH, top_n=40, n_verified=60)
        self.assertEqual([(x["value"], x["effect"]) for x in s], [("general_only", "demote")])
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40), [])       # a geography idea reads no scope

    def test_order_cap_and_overlap(self):
        spec = [("hardware", 0.9)] * 6 + [("buyer", 0.9)] * 5 + [("upstream", 0.9)] * 4 + [("supplier", 0.9)] * 20
        rows, fmap = rows_and_labels(spec)
        got = [x["value"] for x in scope.find_splits(rows, fmap, PLAIN, top_n=40, cap=3)]
        self.assertEqual(got, ["hardware", "buyer", "upstream"])                 # by rows_changed
        self.assertEqual(len(scope.find_splits(rows, fmap, PLAIN, top_n=40)), 2)  # the final cap
        # geo labels on the same rows as role: the geo split overlaps the role split's side V fully -> dropped
        rows, fmap = forty("hardware", 9, 25)
        for i in range(1, 10):
            fmap[f"ck{i}"]["geo"] = {"label": "outside_only", "p": 0.9, "evidence_sha": f"sha{i}"}
        for i in range(10, 35):
            fmap[f"ck{i}"]["geo"] = {"label": "in_target", "p": 0.9, "evidence_sha": f"sha{i}"}
        got = [(x["family"], x["value"]) for x in scope.find_splits(rows, fmap, SEA, top_n=40, cap=3)]
        self.assertEqual(got, [("role", "hardware")])

    def test_human_pin_reduces_the_side(self):
        rows, fmap = forty("hardware", 5, 25)
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40)[0]["rows_changed"], 5)
        rows[0]["user_verdict"] = "explicit"
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40)[0]["rows_changed"], 4)   # the pin is not asked
        rows, fmap = forty("hardware", 4, 25)
        rows[0]["user_verdict"] = "explicit"
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40), [])

    def test_decided_values_are_not_asked_again_until_the_facets_change(self):
        rows, fmap = forty("hardware", 9, 25)
        fsha = scope.facets_sha(SEA)
        for answer, source in (("skipped", "human"), ("no", "idea_wording"), ("unsure", "human")):
            sv = scope.record(SEA, [{"sid": "s1", "family": "role", "value": "hardware", "answer": answer,
                                     "source": source, "facets_sha": fsha}])
            self.assertEqual(scope.find_splits(rows, fmap, sv, top_n=40), [], answer)
        sv2 = {**sv, "facets_zh": {"category": "电子支付", "target": "东南亚"}}         # facets changed
        self.assertEqual(len(scope.find_splits(rows, fmap, sv2, top_n=40)), 1)
        self.assertEqual(scope.enforced(sv2, scope.facets_sha(sv2)), [])

    def test_baseline_shaped(self):
        # sea-payments: a geo default (东南亚) and role.hardware (POS / smart-card makers)
        rows, fmap = forty("hardware", 9, 25)
        d, notes = scope.implied_defaults("东南亚的数字支付", [{"key": "geo.outside_only", "because": "东南亚"}], SEA)
        self.assertEqual(d, [{"family": "geo", "value": "outside_only", "because": "东南亚"}])
        self.assertEqual([x["value"] for x in scope.find_splits(rows, fmap, SEA, top_n=40)], ["hardware"])
        # reducer: an upstream default; 2 buyers / holdings give no question
        spec = [("buyer", 0.9)] * 2 + [("holding", 0.9)] * 2 + [("supplier", 0.9)] * 30
        rows, fmap = rows_and_labels(spec)
        self.assertEqual(scope.find_splits(rows, fmap, PLAIN, top_n=40), [])
        d, _ = scope.implied_defaults("谐波减速器", [{"key": "role.upstream", "because": "减速器"}], PLAIN)
        self.assertEqual([x["value"] for x in d], ["upstream"])
        # ai-agent identity: role.target_only (builds agents, no IAM for them)
        rows, fmap = forty("target_only", 6, 25)
        self.assertEqual([x["value"] for x in scope.find_splits(rows, fmap, TECH, top_n=40)], ["target_only"])
        self.assertEqual(scope.find_splits(rows, fmap, SEA, top_n=40), [])     # target_only: technology ideas only


class Wording(unittest.TestCase):
    def q(self, sv, value="hardware", agent=None, n=9, names_zh=None):
        rows, fmap = forty(value, n, 25)
        splits = scope.with_p(scope.find_splits(rows, fmap, sv, top_n=40), fmap)
        return scope.questions(splits, sv, n=40, names_zh=names_zh, agent=agent)

    def test_question_texts(self):
        [q] = self.q(SEA)
        self.assertTrue(q["question_zh"].startswith("AI 读摘录后认为，名单前 40 家里有 9 家"))
        self.assertIn("其中 9 家在你看到的前 10", q["question_zh"])
        self.assertIn("「数字支付」", q["question_zh"])
        self.assertTrue(q["question_zh"].endswith("这类公司要不要留在名单里？"))
        self.assertNotIn("digital payments", q["question_zh"])
        self.assertTrue(q["question_en"].startswith("Reading the excerpts, the AI thinks 9 of the top 40 (9 in the "
                                                    "top 10 you saw)"))
        self.assertIsNone(CJK.search(q["question_en"] + q["effect_en"]))
        self.assertTrue(q["question_en"].endswith("Keep this kind of company on the list?"))
        self.assertEqual(q["tokens"], {"yes": f"{q['sid']}=yes", "no": f"{q['sid']}=no", "unsure": f"{q['sid']}=?"})
        self.assertEqual((len(q["examples_v"]), len(q["examples_k"])), (3, 2))
        self.assertEqual(set(scope.Q_ZH), set(scope.Q_EN))
        self.assertEqual(set(scope.KIND_ZH), set(scope.KIND_EN))

    def test_zh_without_facets_zh_is_generic_chinese(self):
        sv = {**SEA, "facets_zh": None}
        [q] = self.q(sv)
        self.assertNotIn("digital", q["question_zh"])
        self.assertNotIn("Southeast", q["question_zh"] + q["effect_zh"])
        self.assertIn("这类产品或服务", q["question_zh"])

    def test_examples_by_mean_p_skip_out_of_group_and_show_descriptors(self):
        rows, fmap = forty("hardware", 9, 25)
        for i, p in zip(range(1, 10), (0.71, 0.95, 0.8, 0.99, 0.72, 0.73, 0.74, 0.75, 0.76)):
            fmap[f"ck{i}"]["role"]["p"] = p
        agent = {"ck4": {"in_group": False}, "ck2": {"short_zh": "卡片终端", "short_en": "card terminals"}}
        splits = scope.with_p(scope.find_splits(rows, fmap, SEA, top_n=40), fmap)
        [q] = scope.questions(splits, SEA, n=40, agent=agent, names_zh={"X:0002": "终端二号"})
        self.assertEqual(q["examples_v"], ["X:0002", "X:0003", "X:0009"])
        self.assertIn("终端二号（卡片终端）", q["question_zh"])
        self.assertIn("Co 2 (card terminals)", q["question_en"])
        self.assertEqual(len(q["side_v"]), 9)                                    # every side-V name is listed

    def test_sids_are_new_numbers(self):
        sv = scope.record(SEA, [{"sid": "s4", "family": "role", "value": "buyer", "answer": "no", "source": "human",
                                 "facets_sha": "other"}])
        [q] = self.q(sv)
        self.assertEqual(q["sid"], "s5")


class Answers(unittest.TestCase):
    def test_implied_no_rules(self):
        idea = "东南亚的数字支付"
        d, notes = scope.implied_defaults(idea, [{"key": "geo.outside_only", "because": "欧洲"}], SEA)
        self.assertEqual(d, [])
        self.assertIn("words of the idea", notes[0])
        d, notes = scope.implied_defaults(idea, [{"key": "scope.general_only", "because": "东南亚"}], TECH)
        self.assertEqual(d, [])
        many = [{"key": "geo.outside_only", "because": "东南亚"}, {"key": "role.hardware", "because": "支付"},
                {"key": "role.buyer", "because": "数字"}]
        d, notes = scope.implied_defaults(idea, many, SEA)
        self.assertEqual(len(d), 2)
        self.assertTrue(any("at most 2" in n for n in notes))
        fsha = scope.facets_sha(SEA)
        entries = scope.default_entries(d, SEA, fsha=fsha, run_id="scr-1")
        self.assertEqual([(e["sid"], e["source"], e["answer"]) for e in entries],
                         [("s1", "idea_wording", "no"), ("s2", "idea_wording", "no")])
        sv = scope.record(SEA, entries)
        self.assertEqual(calib.validate_sieve(sv), [])
        self.assertEqual(len(scope.enforced(sv, fsha)), 2)
        self.assertEqual(scope.default_entries(d, sv, fsha=fsha, run_id="scr-2"), [])     # never re-added
        # the undo token s1=yes is a newer human answer: the default stops applying, the old one goes to history
        undo = scope.answer_entry({"sid": "s1", "family": "geo", "value": "outside_only"}, "yes", fsha=fsha,
                                  run_id="scr-2", via="chat", raw="s1=yes")
        sv2 = scope.record(sv, [undo])
        self.assertEqual([e["value"] for e in scope.enforced(sv2, fsha)], ["hardware"])
        self.assertEqual(sv2["history"][-1]["source"], "idea_wording")
        self.assertEqual(scope.default_entries(d, sv2, fsha=fsha, run_id="scr-3"), [])

    def test_recording_never_touches_rules_or_the_l2_question(self):
        sv = {**SEA, "rules": ["mention_only"]}
        q_before = screen.question_sha(screen.build_l2_question("x", None, sv["rules"], sv["facets"]))
        fsha = scope.facets_sha(sv)
        for ans in ("no", "yes", "unsure", "skipped"):
            sv = scope.record(sv, [scope.answer_entry({"sid": "s1", "family": "role", "value": "hardware"}, ans,
                                                      fsha=fsha, run_id="r", via="page", raw=f"s1={ans}")])
            self.assertEqual(calib.validate_sieve(sv), [])
        self.assertEqual(sv["rules"], ["mention_only"])
        self.assertEqual(len(sv["scope_answers"]), 1)
        self.assertEqual(len([h for h in sv["history"] if h.get("sid") == "s1"]), 3)
        q_after = screen.question_sha(screen.build_l2_question("x", None, sv["rules"], sv["facets"]))
        self.assertEqual(q_before, q_after)

    def test_validate_rejects_unknown_values(self):
        bad = {**SEA, "scope_answers": [{"sid": "s1", "family": "role", "value": "nope", "answer": "no"}]}
        self.assertTrue(any("unknown family/value" in e for e in calib.validate_sieve(bad)))
        bad = {**SEA, "scope_answers": [{"sid": "s1", "family": "role", "value": "buyer", "answer": "maybe"}]}
        self.assertTrue(any("answer must be" in e for e in calib.validate_sieve(bad)))
        bad = {**SEA, "examples": [{"security_id": "X:1", "want": "no", "source": "card", "pin": True, "via": "x"}]}
        self.assertTrue(any(".via must be" in e for e in calib.validate_sieve(bad)))
        ok = {**SEA, "examples": [{"security_id": "X:1", "want": "no", "source": "card", "pin": True,
                                   "via": "override_agent"}]}
        self.assertEqual(calib.validate_sieve(ok), [])


class Enforcement(unittest.TestCase):
    def setUp(self):
        self.fsha = scope.facets_sha(SEA)

    def sieve(self, value="hardware", answer="no", sv=SEA, family="role", source="human"):
        return scope.record(sv, [{"sid": "s1", "family": family, "value": value, "answer": answer,
                                  "source": source, "facets_sha": scope.facets_sha(sv),
                                  "effect": scope.effect_of(family, value)}])

    def test_remove_at_enforce_threshold_and_backfill(self):
        spec = [("hardware", 0.62), ("supplier", 0.9), ("hardware", 0.55), ("supplier", 0.9)]
        rows, fmap = rows_and_labels(spec)
        v2, _u, _ex, _n = screen.pin_and_rank(rows, [], self.sieve(), 2, facets=fmap, out=(out := {}))
        self.assertEqual([e["company_key"] for e in out["excluded_by_scope"]], ["ck1"])
        self.assertEqual(out["excluded_by_scope"][0]["verdict_source"], "scope")
        self.assertEqual(out["excluded_by_scope"][0]["scope_p"], 0.62)
        self.assertEqual([e["company_key"] for e in v2[:2]], ["ck2", "ck3"])       # 0.55 stays (hysteresis)
        self.assertTrue(v2[1]["backfill"])

    def test_demote_general_only(self):
        spec = [("general_only", 0.9), ("specific", 0.9), ("general_only", 0.9, "profile"), ("specific", 0.8)]
        rows, fmap = rows_and_labels(spec, "scope")
        sv = self.sieve("general_only", sv=TECH, family="scope")
        v2, _u, _ex, _n = screen.pin_and_rank(rows, [], sv, 4, facets=fmap, out=(out := {}))
        self.assertEqual(out["excluded_by_scope"], [])
        self.assertEqual([e["company_key"] for e in v2], ["ck2", "ck3", "ck4", "ck1"])   # profile row not demoted
        self.assertTrue(v2[-1]["scope_demoted"])

    def test_exemptions(self):
        spec = [("hardware", 0.9), ("hardware", 0.9), ("hardware", 0.9), ("supplier", 0.9)]
        rows, fmap = rows_and_labels(spec)
        sv = self.sieve()
        sv["examples"] = [{"security_id": "X:0001", "company_key": "ck1", "want": "partial", "source": "card",
                           "pin": True, "via": "pin"}]
        agent = {"ck2": {"evidence_sha": "sha2", "in_group": False, "held_sid": "s1", "held_kind": "role.hardware",
                         "v": "yes", "state": "applied"},
                 "ck3": {"evidence_sha": "stale", "in_group": False, "held_sid": "s1", "held_kind": "role.hardware"}}
        v2, _u, _ex, _n = screen.pin_and_rank(rows, [], sv, 4, facets=fmap, agent=agent, out=(out := {}))
        self.assertEqual([e["company_key"] for e in out["excluded_by_scope"]], ["ck3"])     # a stale sha exempts nothing
        by = {e["company_key"]: e for e in v2}
        self.assertEqual(by["ck1"]["user_verdict"], "partial")                        # a human pin wins
        self.assertEqual(by["ck2"]["scope_exempt_agent"], "s1")

    def test_agent_no_with_the_chip_is_removed_too_and_keep_disables_agent_nos(self):
        spec = [("supplier", 0.9), ("unclear", 0.5), ("supplier", 0.9)]
        rows, fmap = rows_and_labels(spec)
        agent = {"ck2": {"evidence_sha": "sha2", "v": "no", "chip": "k", "state": "escalated"}}
        _v2, _u, _ex, notes = screen.pin_and_rank(rows, [], self.sieve(), 3, facets=fmap, agent=agent,
                                                  out=(out := {}))
        self.assertEqual([e["company_key"] for e in out["excluded_by_scope"]], ["ck2"])
        self.assertTrue(any("你的 AI 也判为这一类" in n for n in notes))
        agent = {"ck2": {"evidence_sha": "sha2", "v": "no", "chip": "k", "state": "applied"}}
        v2, _u, _ex, _n = screen.pin_and_rank(rows, [], self.sieve(answer="yes"), 3, facets=fmap, agent=agent,
                                              out=(out := {}))
        self.assertEqual(out["excluded_by_agent"], [])
        self.assertEqual({e["company_key"]: e.get("agent_state") for e in v2}["ck2"], "not_applied")

    def test_unchecked_rows(self):
        rows, fmap = rows_and_labels([("supplier", 0.9), ("supplier", 0.9)])
        del fmap["ck2"]
        v2, _u, _ex, _n = screen.pin_and_rank(rows, [], self.sieve(), 2, facets=fmap)
        self.assertEqual([bool(e.get("scope_unchecked")) for e in v2], [False, True])
        v2, _u, _ex, _n = screen.pin_and_rank(rows, [], SEA, 2, facets=fmap)          # nothing enforced: no badge
        self.assertEqual([bool(e.get("scope_unchecked")) for e in v2], [False, False])


class FineTuneGuard(unittest.TestCase):
    def test_rules_that_contradict_scope_answers_are_refused(self):
        fsha = scope.facets_sha(TECH)
        sv = scope.record(TECH, [{"sid": "s1", "family": "scope", "value": "general_only", "answer": "no",
                                  "source": "human", "facets_sha": fsha},
                                 {"sid": "s2", "family": "role", "value": "hardware", "answer": "yes",
                                  "source": "human", "facets_sha": fsha}])
        self.assertTrue(calib.scope_conflict(sv, "scope_broad"))
        self.assertTrue(calib.scope_conflict(sv, "hardware_to_operators"))
        self.assertFalse(calib.scope_conflict(sv, "buyer_not_supplier"))
        deck = {"deck_id": "deck-x-1", "cards": [{"n": 1, "type": "scope", "security_id": "X:1", "name": "A"},
                                                  {"n": 2, "type": "boundary_in", "security_id": "X:2", "name": "B"}]}
        answers = [calib.Answer(1, "yes", "partial", "b"), calib.Answer(2, "no", None, "k")]
        cands, dropped = calib.candidate_rules(sv, deck, answers)
        self.assertEqual(cands, [])
        self.assertEqual({d["id"] for d in dropped}, {"scope_broad", "hardware_to_operators"})
        self.assertTrue(all(d["why_zh"] == calib.SCOPE_CONFLICT_ZH for d in dropped))
        adopted = calib.adopt_rules(sv, {"adopted": [{"id": "hardware_to_operators"}], "rejected": []})
        self.assertEqual(adopted["rules"], [])
        self.assertEqual(adopted["rejected_rules"][0]["why_zh"], calib.SCOPE_CONFLICT_ZH)


if __name__ == "__main__":
    unittest.main()
