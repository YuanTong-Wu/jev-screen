"""The typed atomic judgement layer (`screen --judge atomic3|single10`, structural plan step 2; default OFF):
product / role / target questions (or one ten-class question) over the SAME L2 text of every L2-verified row, a
fixed rule into tiers A / B / C (C only on positive counter-evidence), cache-safe, budgeted; plus `eval run --judge`
and `eval run --from-run` (never a fresh L1).

Offline: test_screen's seeded store and FakeJev (no network, no paid call)."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_screen as TS  # noqa: E402
from jevscreen import atomic, constraints, evalfrom, evalset, page, review, screen, shortlist  # noqa: E402

STORAGE = ("Listed suppliers of liquid-cooling units, cold plates, and thermal-management systems for grid-scale "
           "battery storage cabinets and stations")
WAREHOUSE = "Humanoid robots for warehouse picking"


def A(label, p=0.9):
    return {"label": label, "p": p}


class Target(unittest.TestCase):
    def test_head_noun(self):
        self.assertEqual(atomic.head_noun("grid-scale battery storage cabinets and stations"),
                         "storage cabinets / stations")
        self.assertEqual(atomic.head_noun("semiconductor wafer fabrication"), "wafer fabrication")
        self.assertIsNone(atomic.head_noun("HBM"))              # adds nothing
        self.assertIsNone(atomic.head_noun(""))

    def test_target_of(self):
        self.assertEqual(atomic.target_of(constraints.derive(STORAGE)),
                         {"text": "grid-scale battery storage cabinets and stations",
                          "head": "storage cabinets / stations"})
        self.assertEqual(atomic.target_of([{"kind": "geography", "text": "Southeast Asia"}]),
                         {"text": "Southeast Asia"})
        self.assertIsNone(atomic.target_of(constraints.derive("Humanoid robots")))
        self.assertIsNone(atomic.target_of([{"kind": "role", "text": "makes or builds it"}]))


class Questions(unittest.TestCase):
    def test_atomic3_shape(self):
        qs = atomic.build_questions("储能液冷", STORAGE, "atomic3", constraints.derive(STORAGE))
        self.assertEqual(sorted(qs), ["product", "role", "target"])
        self.assertEqual(list(qs["product"].criteria), list(atomic.PRODUCT))
        self.assertEqual(list(qs["role"].criteria), list(atomic.ROLE))
        self.assertEqual(list(qs["target"].criteria), list(atomic.TARGET))
        self.assertIn("head noun: storage cabinets / stations", qs["target"].instructions)
        self.assertIn("grid-scale battery storage cabinets and stations", qs["target"].criteria["named"])
        self.assertIn("consolidated subsidiary counts as the company itself", qs["role"].instructions)
        self.assertEqual({q.key for q in qs.values()}, {"judge_product", "judge_role", "judge_target"})

    def test_no_target_no_target_question(self):
        qs = atomic.build_questions("humanoid robots", "Humanoid robots", "atomic3", [])
        self.assertEqual(sorted(qs), ["product", "role"])
        qs = atomic.build_questions("humanoid robots", "Humanoid robots", "single10", [])
        self.assertEqual(list(qs["single"].criteria), list(atomic.SINGLE_NO_TARGET))

    def test_single10_has_ten_classes(self):
        qs = atomic.build_questions("x", WAREHOUSE, "single10", constraints.derive(WAREHOUSE))
        self.assertEqual(list(qs), ["single"])
        self.assertEqual(len(qs["single"].criteria), 10)
        self.assertEqual(qs["single"].key, "judge_single")

    def test_sha_is_deterministic_and_arm_specific(self):
        c = constraints.derive(STORAGE)
        a = atomic.question_sha(atomic.build_questions("x", STORAGE, "atomic3", c))
        self.assertEqual(a, atomic.question_sha(atomic.build_questions("x", STORAGE, "atomic3", c)))
        self.assertNotEqual(a, atomic.question_sha(atomic.build_questions("x", STORAGE, "single10", c)))
        self.assertIsNone(atomic.question_sha({}))

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            atomic.build_questions("x", None, "atomic4", [])


class TierRule(unittest.TestCase):
    def t(self, l2, has_target=True, **fams):
        return atomic.tier_of(l2, {k: A(*v) if isinstance(v, tuple) else A(v) for k, v in fams.items()}, has_target)

    def test_a_needs_explicit_own_supplier_and_target(self):
        self.assertEqual(self.t("explicit", product="own", role="supplier", target="named")[0], "A")
        self.assertEqual(self.t("explicit", product="own", role="supplier", target="one_of_several")[0], "A")
        self.assertEqual(self.t("partial", product="own", role="supplier", target="named")[0], "B")
        self.assertEqual(self.t("explicit", product="own", role="unclear", target="named")[0], "B")
        self.assertEqual(self.t("explicit", product="none", role="supplier", target="named")[0], "B")
        # no target in the idea: the target never counts
        self.assertEqual(self.t("explicit", has_target=False, product="own", role="supplier")[0], "A")

    def test_not_stated_or_unclear_never_demotes(self):
        for fams in ({"product": "none", "role": "unclear", "target": "not_stated"},
                     {"product": "own", "role": "supplier", "target": "not_stated"}):
            tier, reasons, state = self.t("explicit", **fams)
            self.assertEqual((tier, reasons, state), ("B", [], "checked"), fams)

    def test_c_only_on_positive_counter_evidence_above_the_threshold(self):
        tier, reasons, _ = self.t("explicit", product="own", role="buyer_user", target="named")
        self.assertEqual((tier, reasons), ("C", ["role:buyer_user"]))
        tier, reasons, _ = self.t("partial", product="adjacent", role="supplier", target="other_only")
        self.assertEqual((tier, reasons), ("C", ["product:adjacent", "target:other_only"]))
        # below P_DEMOTE a counter answer is not enough
        self.assertEqual(self.t("explicit", product="own", role=("channel", 0.55), target="named")[0], "B")
        self.assertEqual(self.t("explicit", product=("component", 0.6), role="supplier", target="named")[0], "C")

    def test_unchecked_is_b(self):
        self.assertEqual(atomic.tier_of("explicit", None, True), ("B", [], "unchecked"))
        self.assertEqual(atomic.tier_of("explicit", {}, True), ("B", [], "unchecked"))

    def test_single10_maps_to_the_same_rule(self):
        self.assertEqual(atomic.tier_of("explicit", {"single": A("supplier_named")}, True)[0], "A")
        self.assertEqual(atomic.tier_of("explicit", {"single": A("supplier_target_unstated")}, True)[0], "B")
        self.assertEqual(atomic.tier_of("explicit", {"single": A("supplier_other_target")}, True),
                         ("C", ["target:other_only"], "checked"))
        self.assertEqual(atomic.tier_of("explicit", {"single": A("other_role")}, True)[1], ["role:other_link"])
        self.assertEqual(atomic.tier_of("explicit", {"single": A("unclear")}, True)[0], "B")
        self.assertEqual(atomic.tier_of("explicit", {"single": A("supplier")}, False)[0], "A")
        self.assertEqual(atomic.tier_of("explicit", {"single": A("buyer_user", 0.5)}, True)[0], "B")

    def test_row_fields_tag_the_inference(self):
        f = atomic.row_fields("explicit", {"role": A("buyer_user"), "product": A("own")}, False)
        self.assertEqual((f["judge_tier"], f["judge_reason"], f["judge_inferred"]), ("C", ["role:buyer_user"], True))
        self.assertEqual(f["judge_labels"], {"role": "buyer_user", "product": "own"})
        f = atomic.row_fields("explicit", {"role": A("supplier"), "product": A("own")}, False)
        self.assertNotIn("judge_inferred", f)

    def test_order_rank(self):
        self.assertEqual(atomic.order_rank({}), 0)                       # off: the old order
        self.assertEqual(atomic.order_rank({"user_verdict": "partial"}), 0)
        self.assertEqual(atomic.order_rank({"judge_tier": "C"}), 2)
        self.assertEqual(atomic.order_rank({"judge_tier": "C", "user_verdict": "explicit"}), 0)
        self.assertEqual(atomic.order_rank({"judge_tier": "C", "agent_verdict": "yes", "agent_state": "applied"}), 1)
        self.assertEqual(atomic.order_rank({"judge_tier": "C", "agent_verdict": "yes", "agent_state": "held"}), 2)

    def test_order_rank_pinned_in_row_without_a_tier(self):
        # layer on (LAYER_KEY): a human partial pin that brought a row in is B, not A; explicit is A; none is B
        on = {atomic.LAYER_KEY: True}
        self.assertEqual(atomic.order_rank({**on, "user_verdict": "partial"}), 1)
        self.assertEqual(atomic.order_rank({**on, "user_verdict": "explicit"}), 0)
        self.assertEqual(atomic.order_rank(on), 1)
        self.assertEqual(atomic.order_rank({"user_verdict": "partial"}), 0)      # layer off: the old order

    def test_is_lifted(self):
        self.assertFalse(atomic.is_lifted({"judge_tier": "C"}))
        self.assertTrue(atomic.is_lifted({"judge_tier": "C", "agent_verdict": "yes", "agent_state": "applied"}))
        self.assertTrue(atomic.is_lifted({"judge_tier": "C", "user_verdict": "partial"}))
        self.assertFalse(atomic.is_lifted({"judge_tier": "B", "user_verdict": "partial"}))

    def test_estimate_counts_the_re_reads_of_the_arm(self):
        self.assertAlmostEqual(atomic.band_factor(0), 1 + 2 * atomic.BAND_SHARE)
        self.assertAlmostEqual(atomic.band_factor(3), 1 + 3 * atomic.BAND_SHARE)    # N..N+2 replace read 0
        self.assertGreater(atomic.estimate_items(10, 3, 3), atomic.estimate_items(10, 3, 0))

    def test_aggregate_and_stored_answers(self):
        a = atomic.aggregate([{"status": "ok", "probs": {"own": 0.6, "adjacent": 0.4}},
                              {"status": "ok", "probs": {"own": 0.2, "adjacent": 0.8}},
                              {"status": "failed"}], atomic.PRODUCT)
        self.assertEqual((a["label"], a["n"], a["p"]), ("adjacent", 2, 0.6))
        self.assertIsNone(atomic.aggregate([{"status": "failed"}], atomic.PRODUCT))
        got = atomic.stored_answers([("k1", "judge_role", "buyer_user", {"buyer_user": 0.8}, "ok"),
                                     ("k1", "l2", "explicit", {}, "ok"),
                                     ("k2", "judge_product", "own", {}, "failed")])
        self.assertEqual(got, {"k1": {"role": {"label": "buyer_user", "p": 0.8}}})

    def test_review_keys(self):
        rows = [{"company_key": "a", "judge_tier": "C", "score": 1.0}, {"company_key": "b", "judge_tier": "A"},
                {"company_key": "c", "judge_tier": "C", "score": 3.0}]
        self.assertEqual(atomic.review_keys(rows), ["c", "a"])
        self.assertEqual(atomic.review_keys(rows, 1), ["c"])


# ------------------------------------------------------------------------------------------------ run_layer

class JevUnavailable(Exception):
    """Matched by name as jev.JevUnavailable (screen._is_error)."""


class LayerClient:
    """A judge client: every item answers `label` at probability p; $0.01 per item read; estimate_uncached prices
    each item at $0.01; `fail_on` (question key) raises JevUnavailable on that question's first read."""

    def __init__(self, budget, p=0.9, fail_on=None):
        self.budget, self.p, self.fail_on = budget, p, fail_on
        self.spent_usd, self.requests_sent, self.reads = 0.0, 0, []

    def estimate_uncached(self, items, question):
        return {"est_cost_usd": 0.01 * len(items)}

    def classify(self, items, question):
        if question.key == self.fail_on:
            raise JevUnavailable("down")
        self.reads.append((question.key, getattr(question, "read", 0), len(items)))
        out = []
        for it in items:
            self.spent_usd += 0.01
            lab = list(question.criteria)[0]
            other = list(question.criteria)[-1]
            out.append({"item_id": it.item_id, "label": lab, "probs": {lab: self.p, other: round(1 - self.p, 4)},
                        "request_id": "r", "status": "ok", "error": None, "cached": False})
        self.requests_sent += 1
        return out


class RunLayer(unittest.TestCase):
    ITEMS = [type("It", (), {"item_id": f"k{i}", "text": "t"})() for i in range(4)]
    QS = atomic.build_questions("humanoid robots", "Humanoid robots", "atomic3", [])   # product + role

    def run_(self, client, remaining, read_offset=0):
        notes: list[str] = []
        out, info = atomic.run_layer(lambda *a: client, self.ITEMS, self.QS, remaining, {}, notes,
                                     read_offset=read_offset)
        return out, info, notes

    def test_band_reads_and_the_estimate_per_arm(self):
        for off, extra in ((0, [1, 2]), (3, [3, 4, 5])):
            c = LayerClient(10.0, p=0.65)                  # every item in the band
            out, info, _n = self.run_(c, 10.0, off)
            self.assertEqual([r for k, r, _n2 in c.reads if k == "judge_product"], [0] + extra)
            self.assertAlmostEqual(info["estimate_usd"], 2 * 0.04 * atomic.band_factor(off), places=6)
            self.assertEqual(info["status"], "ok")
            # with an offset the fresh reads replace read 0: n counts them alone
            self.assertEqual(out["k0"]["product"]["n"], len(extra) + (0 if off else 1))

    def test_re_reads_that_no_longer_fit_are_skipped_not_partial(self):
        c = LayerClient(10.0, p=0.65)
        # the estimate (2 x $0.04 x 1.6 = $0.128) fits; read 0 of both families plus the first band pass do not
        # leave room for every re-read pass ($0.04 each)
        out, info, notes = self.run_(c, 0.13)
        self.assertEqual(info["status"], "ok")
        self.assertGreaterEqual(info["band_skipped"], 1)
        self.assertLessEqual(c.spent_usd, 0.13 + 1e-9)
        self.assertEqual(sorted(out["k0"]), ["product", "role"])          # both families decided
        self.assertTrue(any("复读跳过" in n and "re-reads skipped" in n for n in notes))

    def test_jev_stop_after_the_first_family_is_partial(self):
        c = LayerClient(10.0, fail_on="judge_role")
        out, info, notes = self.run_(c, 10.0)
        self.assertEqual(info["status"], "partial")
        self.assertEqual(sorted(out["k0"]), ["product"])
        self.assertTrue(any("没读完" in n for n in notes))
        # a row with the product answer only is never demoted for the missing role, and not A
        self.assertEqual(atomic.tier_of("explicit", out["k0"], False)[0], "B")


# ------------------------------------------------------------------------------------------------ the screen

class JudgeJev(TS.FakeJev):
    """FakeJev that answers the judge questions: product 'adjacent' for a text about controllers, else 'own'; role
    'supplier'; target 'named' when the text has 'warehouse', else 'not_stated'; single10 accordingly."""

    def classify(self, items, question):
        if not question.key.startswith("judge_"):
            return super().classify(items, question)
        assert not self.dry_run
        self.classified.append((list(items), question))
        out = []
        for i in range(0, len(items), self.pack_size):
            pack = items[i:i + self.pack_size]
            if self._spent + self.COST > self.budget_usd + 1e-12:
                out += [{"item_id": it.item_id, "label": None, "probs": {}, "request_id": None,
                         "status": "skipped_budget", "error": "budget", "cached": False} for it in pack]
                continue
            self._spent += self.COST
            self._sent += 1
            for it in pack:
                lab = self.answer(question, it.text.lower())
                out.append({"item_id": it.item_id, "label": lab, "probs": {lab: 0.9},
                            "request_id": f"{self.layer}-req-{self._sent}", "status": "ok", "error": None,
                            "cached": False})
        return out

    @staticmethod
    def answer(question, t):
        fam = atomic.KEY_FAMILY[question.key]
        if fam == "product":
            return "adjacent" if "controller" in t else "own"
        if fam == "role":
            return "supplier"
        if fam == "target":
            return "named" if "warehouse" in t else "not_stated"
        if "controller" in t:
            return "adjacent"
        if "supplier_named" in question.criteria:
            return "supplier_named" if "warehouse" in t else "supplier_target_unstated"
        return "supplier"


class ScreenCase(TS.StoreCase):
    def factory(self, cls=JudgeJev):
        log = self.log

        def f(cfg, *, run_id, layer, budget_usd, dry_run):
            return cls(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
        return f

    def screen_(self, idea_en=WAREHOUSE, **kw):
        kw.setdefault("jev_factory", self.factory())
        kw.setdefault("min_mcap_usd", 0)
        kw.setdefault("sieve", "none")
        return self.run_screen(idea_en=idea_en, **kw)

    def judge_calls(self):
        return [(items, q) for c in self.log for items, q in c.classified if q.key.startswith("judge_")]

    @staticmethod
    def row(res, sid):
        return next(r for r in res["rows"] if r["security_id"] == sid)


class JudgeScreen(ScreenCase):
    def test_off_by_default(self):
        res = self.screen_()
        self.assertIsNone(res["params"]["judge"])
        self.assertNotIn("judge", res)
        self.assertNotIn("judge", res["layers"])
        self.assertEqual(self.judge_calls(), [])
        self.assertTrue(all("judge_tier" not in r for r in res["rows"]))
        with open(self.home / "out" / "results.csv", encoding="utf-8") as f:
            self.assertNotIn("judge_tier", next(csv.reader(f)))

    def test_atomic3_tiers_order_the_list_and_l1_l2_stay_the_same(self):
        off = self.screen_(out_dir=self.home / "off")
        self.log.clear()
        res = self.screen_(judge="atomic3")
        self.assertEqual(res["params"]["judge"], "atomic3")
        self.assertEqual(res["params"]["l2_question_sha"], off["params"]["l2_question_sha"])
        self.assertEqual(res["questions"], off["questions"])
        robo, rob2 = self.row(res, "NYSE:ROBO"), self.row(res, "NASDAQ:ROB2")
        self.assertEqual((robo["judge_tier"], robo["judge_state"]), ("A", "checked"))
        self.assertEqual(robo["judge_labels"], {"product": "own", "role": "supplier", "target": "named"})
        self.assertEqual((rob2["judge_tier"], rob2["judge_reason"], rob2["judge_inferred"]),
                         ("C", ["product:adjacent"], True))
        # C is ranked after every A / B row, never removed; the L2 label is kept as read
        self.assertEqual(res["rows"][-1]["security_id"], "NASDAQ:ROB2")
        self.assertEqual(rob2["l2_label"], "partial")
        self.assertEqual({r["security_id"] for r in res["rows"]}, {r["security_id"] for r in off["rows"]})
        tiers = [r["judge_tier"] for r in res["rows"]]
        self.assertEqual(tiers, sorted(tiers))
        # one question per family over every L2-verified row
        calls = self.judge_calls()
        self.assertEqual(sorted(q.key for _i, q in calls), ["judge_product", "judge_role", "judge_target"])
        self.assertTrue(all(len(items) == len(res["rows"]) for items, _q in calls))
        info = res["judge"]
        self.assertEqual((info["mode"], info["A"], info["C"], info["unchecked"]), ("atomic3", 1, 1, 0))
        self.assertEqual(info["review"], [rob2["company_key"]])
        self.assertTrue(info["has_target"])
        self.assertGreater(res["layers"]["judge"]["cost_usd"], 0)
        self.assertAlmostEqual(res["cost_usd"], res["layers"]["l1"]["cost_usd"] + res["layers"]["l2"]["cost_usd"]
                               + res["layers"]["judge"]["cost_usd"], places=6)
        self.assertEqual(res["status"], "ok")
        with open(self.home / "out" / "results.csv", encoding="utf-8") as f:
            self.assertEqual([r["judge_tier"] for r in csv.DictReader(f)], tiers)

    def test_single10_is_one_question(self):
        res = self.screen_(judge="single10")
        self.assertEqual([q.key for _i, q in self.judge_calls()], ["judge_single"])
        self.assertEqual(self.row(res, "NYSE:ROBO")["judge_tier"], "A")
        self.assertEqual(self.row(res, "NASDAQ:ROB2")["judge_tier"], "C")

    def test_no_target_in_the_idea_asks_two_questions(self):
        res = self.screen_(idea_en="Humanoid robots", judge="atomic3")
        self.assertEqual(sorted(q.key for _i, q in self.judge_calls()), ["judge_product", "judge_role"])
        self.assertFalse(res["judge"]["has_target"])
        self.assertEqual(self.row(res, "NYSE:ROBO")["judge_tier"], "A")

    def test_budget_skip_keeps_the_old_order_with_a_note(self):
        off = self.screen_(out_dir=self.home / "off")
        res = self.screen_(judge="atomic3", budget_usd=0.025)
        self.assertEqual(res["layers"]["judge"]["skipped"], "budget")
        self.assertFalse(res["judge"]["applied"])
        self.assertTrue(all("judge_tier" not in r for r in res["rows"]))
        self.assertEqual([r["security_id"] for r in res["rows"]], [r["security_id"] for r in off["rows"]])
        self.assertEqual(res["status"], "partial")
        self.assertTrue(any("逐项判断跳过" in n and "not enough budget" in n for n in res["notes"]))

    def test_unknown_arm_is_refused_before_anything_is_sent(self):
        with self.assertRaises(ValueError):
            self.screen_(judge="atomic4")
        self.assertEqual(self.log, [])

    def test_dry_run_estimates_without_classifying(self):
        res = self.screen_(judge="atomic3", dry_run=True)
        self.assertIn("estimate", res["layers"]["judge"])
        self.assertEqual(self.judge_calls(), [])

    def test_from_run_inherits_the_arm_and_reads_no_l1(self):
        first = self.screen_(judge="atomic3", out_dir=self.home / "a")
        self.log.clear()
        again = self.screen_(from_run=first["run_id"], out_dir=self.home / "b")
        self.assertEqual(again["params"]["judge"], "atomic3")
        self.assertEqual(self.row(again, "NASDAQ:ROB2")["judge_tier"], "C")
        self.assertFalse(any(c.layer == "l1" for c in self.log))
        # and a from-run of a baseline run can turn the lever on (the A/B)
        base = self.screen_(out_dir=self.home / "c")
        self.log.clear()
        ab = self.screen_(from_run=base["run_id"], judge="single10", out_dir=self.home / "d",
                          jev_factory=evalfrom.no_l1_factory(self.cfg, self.factory()))
        self.assertEqual(ab["layers"]["l1"]["from_run"], base["run_id"])
        self.assertEqual(self.row(ab, "NASDAQ:ROB2")["judge_tier"], "C")
        off = self.screen_(from_run=first["run_id"], judge=None, out_dir=self.home / "e")
        self.assertTrue(all("judge_tier" not in r for r in off["rows"]))

    def test_answers_are_stored_and_the_pool_applies_them(self):
        res = self.screen_(judge="atomic3", max_out=1)
        got = self.query("SELECT security_id, layer, label FROM screen_results WHERE run_id = ? AND layer LIKE "
                         "'judge_%' ORDER BY security_id, layer", [res["run_id"]])
        self.assertIn(("NASDAQ:ROB2", "judge_product", "adjacent"), got)
        from jevscreen import calib, store
        with store.session(self.cfg, read_only=True) as con:
            pool = {p["security_id"]: p for p in calib.load_pool(con, res["run_id"], res["params"])}
            raw = {p["security_id"]: p for p in calib.load_pool(con, res["run_id"], {})}
        self.assertEqual((pool["NASDAQ:ROB2"]["judge_tier"], pool["NASDAQ:ROB2"]["judge_reason"]),
                         ("C", ["product:adjacent"]))
        self.assertEqual(pool["NYSE:ROBO"]["judge_tier"], "A")
        self.assertNotIn("judge_tier", raw["NASDAQ:ROB2"])

    def test_rank_only_keeps_a_judged_base_free_and_equal(self):
        first = self.screen_(judge="atomic3", out_dir=self.home / "a")
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=first["run_id"], sieve="none",
                                  out_dir=self.home / "ro")
        self.assertEqual(ro["cost_usd"], 0.0)

        def shape(r):
            return [(x["security_id"], x["rank"], x.get("judge_tier")) for x in r["rows"]]
        self.assertEqual(shape(ro), shape(first))
        ro2 = screen.rank_only_run(self.cfg, "humanoid robots", from_run=ro["run_id"], sieve="none",
                                   out_dir=self.home / "ro2")
        self.assertEqual(shape(ro2), shape(first))

    def test_rank_only_refuses_changed_judge_questions(self):
        first = self.screen_(judge="atomic3")
        with mock.patch.object(constraints, "derive", return_value=[{"kind": "end_market", "text": "farms"}]):
            with self.assertRaises(screen.RankOnlyUnsafe) as cm:
                screen.rank_only_run(self.cfg, "humanoid robots", from_run=first["run_id"], sieve="none",
                                     out_dir=self.home / "ro")
        self.assertIn("judge_question_sha", str(cm.exception.reason))

    def test_review_version_is_free(self):
        from jevscreen import review_cli
        first = self.screen_(judge="atomic3", out_dir=self.home / "a")
        res, notes = review_cli.new_version(self.cfg, "humanoid robots", first["run_id"], "decide")
        self.assertEqual(res["cost_usd"], 0.0)
        self.assertEqual([(r["security_id"], r["rank"], r.get("judge_tier")) for r in res["rows"]],
                         [(r["security_id"], r["rank"], r.get("judge_tier")) for r in first["rows"]])

    def test_demoted_rows_go_to_your_ai_review(self):
        from jevscreen import calib
        from jevscreen import store
        res = self.screen_(judge="atomic3", max_out=1)       # ROB2 (C) is below the cut
        self.assertNotIn("NASDAQ:ROB2", [r["security_id"] for r in res["rows"]])
        inputs = calib.load_inputs(self.home / "out")
        with store.session(self.cfg, read_only=True) as con:
            pool = calib.load_pool(con, res["run_id"], res["params"])
        with mock.patch.object(review, "DECK_A_EDGE", 0):   # not taken as a below-cut row first
            deck = review.build_deck(res, inputs, None, {}, part="A", pool=pool)
        [it] = [it for it in deck["items"] if it["group"] == "judge_demoted"]
        self.assertEqual(it["security_id"], "NASDAQ:ROB2")
        self.assertEqual(it["system"]["judge"]["tier"], "C")
        self.assertEqual(it["system"]["judge"]["reason"], ["product:adjacent"])

    def test_your_ai_yes_lifts_a_c_row_to_b(self):
        e = {"judge_tier": "C", "score": 5.0, "market_cap_usd": 1, "security_id": "X"}
        b = {"judge_tier": "B", "score": 1.0, "market_cap_usd": 1, "security_id": "Y"}
        self.assertEqual([x["security_id"] for x in sorted([e, b], key=screen._order)], ["Y", "X"])
        e.update(agent_verdict="yes", agent_state="applied")
        self.assertEqual([x["security_id"] for x in sorted([e, b], key=screen._order)], ["X", "Y"])

    def test_rows_beyond_the_cap_are_not_read_not_partial(self):
        with mock.patch.object(atomic, "READ_MAX", 1):
            res = self.screen_(judge="atomic3")
            from jevscreen import calib, store
            with store.session(self.cfg, read_only=True) as con:
                pool = calib.load_pool(con, res["run_id"], res["params"])
        states = sorted(r["judge_state"] for r in res["rows"])
        n = len(res["rows"]) - 1
        self.assertEqual(states, ["checked"] + [atomic.NOT_READ] * n)
        self.assertEqual((res["judge"]["unchecked"], res["judge"][atomic.NOT_READ]), (0, n))
        self.assertEqual(res["status"], "ok")
        self.assertIn(atomic.NOT_READ, [p.get("judge_state") for p in pool])
        row = next(r for r in res["rows"] if r["judge_state"] == atomic.NOT_READ)
        self.assertEqual(page._badges(row, None), ["judge_not_read"])

    def test_eval_can_name_a_lever_off_under_from_run(self):
        base = self.screen_(judge="atomic3", l2_constraints=True, shortlist=True, out_dir=self.home / "a")
        wrap = evalfrom.no_l1_factory(self.cfg, self.factory())
        # nothing named: eval run passes every lever off under --from-run (never inherited from the base run)
        same = self.screen_(from_run=base["run_id"], out_dir=self.home / "b", jev_factory=wrap,
                            **evalfrom.lever_kwargs(argparse.Namespace(), from_run=True))
        self.assertEqual([same["params"][k] for k in ("judge", "l2_constraints", "shortlist")], [None, False, False])
        # screen's own inheritance is unchanged: a lever not given is the base run's
        inh = self.screen_(from_run=base["run_id"], out_dir=self.home / "b2", jev_factory=wrap)
        self.assertEqual([inh["params"][k] for k in ("judge", "l2_constraints", "shortlist")], ["atomic3", True, True])
        # naming them off works too: eval run --judge none --no-l2-constraints --no-shortlist
        from jevscreen import cli
        a = cli.build_parser().parse_args(["eval", "run", "--budget-each", "0.1", "--budget-total", "0.1",
                                           "--judge", "none", "--no-l2-constraints", "--no-shortlist",
                                           "--from-run", "x=r"])
        off = self.screen_(from_run=base["run_id"], out_dir=self.home / "c", jev_factory=wrap,
                           **evalfrom.lever_kwargs(a, from_run=True))
        self.assertEqual([off["params"][k] for k in ("judge", "l2_constraints", "shortlist")], [None, False, False])
        self.assertTrue(all("judge_tier" not in r and "shortlist_tier" not in r for r in off["rows"]))

    def test_shortlist_with_judge_says_it_is_an_inference(self):
        res = self.screen_(judge="atomic3", shortlist=True)
        self.assertEqual(res["shortlist"]["rule"], shortlist.RULE_JUDGE)
        self.assertEqual(res["shortlist"]["by"], "judge")
        for lang, note in (("zh", "main_note_judge"), ("en", "main_note_judge")):
            d = page.build_page_data(res, None, lang=lang, l2_pieces={})
            self.assertTrue(d["shortlist"]["judge"])
            text = page.render_text(d)
            self.assertIn(page.STRINGS[lang][note], text)
            self.assertNotIn(page.STRINGS[lang]["main_note"], text)
        self.assertTrue(page.STRINGS["zh"]["main_note_judge"].startswith(page.STRINGS["zh"]["tag_inference"] + "："))
        self.assertTrue(page.STRINGS["en"]["main_note_judge"].startswith("Inferred:"))
        self.assertRegex(page.STRINGS["zh"]["main_note_judge"], r"[一-鿿]")
        self.assertNotRegex(page.STRINGS["en"]["main_note_judge"], r"[一-鿿]")
        # without --judge the old rule and note stay
        plain = self.screen_(shortlist=True, out_dir=self.home / "p")
        self.assertEqual(plain["shortlist"]["rule"], shortlist.RULE)
        self.assertNotIn("judge", page.build_page_data(plain, None, lang="en", l2_pieces={})["shortlist"])

    def test_shortlist_high_is_tier_a(self):
        res = self.screen_(judge="atomic3", shortlist=True)
        self.assertEqual(res["shortlist"]["high"], 1)
        self.assertEqual(self.row(res, "NYSE:ROBO")["shortlist_tier"], "high")
        self.assertEqual(shortlist.tier_of({"judge_tier": "B", "l1_label": "core", "l2_label": "explicit"}),
                         "confirm")


class JudgePage(unittest.TestCase):
    def test_badges_are_labelled_inferences_and_pure_per_language(self):
        b = page._badges({"judge_tier": "C", "judge_reason": ["role:buyer_user", "target:other_only"]}, None)
        self.assertEqual(b, ["judge_c_role", "judge_c_target"])
        self.assertEqual(page._badges({"judge_tier": "B", "judge_state": "unchecked"}, None), ["judge_unchecked"])
        self.assertEqual(page._badges({"judge_tier": "A"}, None), [])
        self.assertEqual(page._badges({"judge_tier": "B", "judge_state": "not_read"}, None), ["judge_not_read"])
        for key in ("badge_judge_c_product", "badge_judge_c_role", "badge_judge_c_target", "badge_judge_unchecked",
                    "badge_judge_not_read"):
            self.assertRegex(page.STRINGS["zh"][key], r"[一-鿿]", key)
            self.assertNotRegex(page.STRINGS["en"][key], r"[一-鿿]", key)
        for key in ("badge_judge_c_product", "badge_judge_c_role", "badge_judge_c_target"):
            self.assertTrue(page.STRINGS["zh"][key].startswith(page.STRINGS["zh"]["tag_inference"] + "："), key)
            self.assertTrue(page.STRINGS["en"][key].startswith("Inferred:"), key)


    def test_a_lifted_c_row_drops_the_moved_down_badge(self):
        c = {"judge_tier": "C", "judge_reason": ["role:buyer_user"]}
        self.assertEqual(page._badges({**c, "agent_verdict": "yes", "agent_state": "applied"}, None), [])
        self.assertEqual(page._badges({**c, "user_verdict": "explicit"}, None), [])
        self.assertEqual(page._badges({**c, "agent_verdict": "yes", "agent_state": "held"}, None), ["judge_c_role"])


class EvalJudge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "set"
        self.d.mkdir()
        for iid in ("xx", "yy"):
            (self.d / f"{iid}.json").write_text(json.dumps({
                "format": evalset.FORMAT, "id": iid, "idea": "Humanoid robots", "type": "other",
                "labels": [{"security_id": "NYSE:A", "name": "A", "label": "right", "source_url":
                            "https://www.sec.gov/a.htm", "note": "n", "by": "ai", "reviewed": False,
                            "checked": "filing_read"}]}), encoding="utf-8")

    def run_(self, fake, **kw):
        from jevscreen import eval_cli
        args = argparse.Namespace(set=self.d, json=True, budget_each=0.1, budget_total=0.2, ideas=None, reads=None,
                                  read_offset=0, **kw)
        cfg = mock.Mock(home=Path(self.tmp.name))
        bases = mock.patch.object(evalfrom, "_load_bases", lambda cfg, ids: {
            r: {"idea": "Humanoid robots", "params": {}} for r in ids})
        with mock.patch.object(eval_cli, "_store_keys", return_value=(None, None)), \
                mock.patch.object(eval_cli, "_emit"), bases:
            return eval_cli._run(args, cfg, screen_fn=fake)

    def test_eval_run_passes_the_arm_and_the_base_runs(self):
        seen = {}

        def fake(cfg, idea, **kw):
            seen[kw["out_dir"].name] = kw
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.0, "params": {"judge": kw.get("judge")},
                    "layers": {"l1": {"from_run": kw.get("from_run"), "cost_usd": 0.0}}}
        with self.assertRaises(evalset.EvalError) as cm:       # no base run for yy: nothing is sent at all
            self.run_(fake, judge="single10", from_run="xx=scr-base-1")
        self.assertIn("no base run for yy", str(cm.exception))
        self.assertEqual(seen, {})
        code = self.run_(fake, judge="single10", from_run="xx=scr-base-1,yy=scr-base-2")
        self.assertEqual(code, 0)
        self.assertEqual(seen["xx"]["judge"], "single10")
        self.assertEqual(seen["xx"]["from_run"], "scr-base-1")
        # the wrapped factory refuses L1 before anything is sent
        with self.assertRaises(evalfrom.L1WouldRerun):
            seen["xx"]["jev_factory"](None, run_id="r", layer="l1", budget_usd=0.1, dry_run=False)
        self.assertEqual(evalset.levers_of({"params": {"judge": "single10"}, "judge": {"applied": True}}),
                         ["judge-single10"])

    def test_off_by_default(self):
        seen = []

        def fake(cfg, idea, **kw):
            seen.append(kw)
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.0}
        self.run_(fake)
        self.assertTrue(all("judge" not in kw and "from_run" not in kw for kw in seen))

    def test_eval_passes_named_levers_on_or_off_only(self):
        seen = {}

        def fake(cfg, idea, **kw):
            seen[kw["out_dir"].name] = kw
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.0,
                    "layers": {"l1": {"from_run": kw.get("from_run"), "cost_usd": 0.0}}}
        self.run_(fake, judge="none", l2_constraints=False, shortlist=False, from_run="xx=b1,yy=b2")
        self.assertEqual([seen["xx"][k] for k in ("judge", "l2_constraints", "shortlist")], [None, False, False])
        seen.clear()
        self.run_(fake, from_run="xx=b1,yy=b2")      # not named: off, explicitly (never the base run's)
        self.assertEqual([seen["xx"][k] for k in ("judge", "l2_constraints", "shortlist")], [None, False, False])
        seen.clear()
        self.run_(fake)                               # no base run: nothing named is left out (off by default)
        self.assertTrue(all(k not in seen["xx"] for k in ("judge", "l2_constraints")))
        self.assertIs(seen["xx"]["shortlist"], False)  # on in screen by default: the eval's bare list says off

    def test_a_skipped_layer_is_not_counted_as_the_arm(self):
        r = {"params": {"judge": "atomic3"}, "judge": {"applied": False}}
        self.assertEqual(evalset.levers_of(r), ["judge-atomic3 skipped"])
        s = evalset.score({"id": "xx", "type": "other", "labels": []}, {"rows": [], **r})
        self.assertNotIn("judge-atomic3 (", evalset.report_md([s], evalset.aggregate([s])))

    def test_no_l1_factory_resolves_the_provider_once(self):
        made = mock.Mock(return_value=lambda cfg_, **kw: kw["layer"])
        with mock.patch.object(screen, "_run_factory", made):
            f = evalfrom.no_l1_factory(object())
            self.assertEqual([f(None, layer=x) for x in ("l2", "judge", "facet")], ["l2", "judge", "facet"])
        self.assertEqual(made.call_count, 1)

    def test_base_runs_from_an_eval_folder_or_pairs(self):
        folder = Path(self.tmp.name) / "ev"
        folder.mkdir()
        (folder / "scores.json").write_text(json.dumps({"scores": [
            {"id": "xx", "run_id": "scr-1", "status": "ok"}, {"id": "yy", "run_id": "scr-2", "status": "partial"},
            {"id": "zz", "run_id": None, "status": "not_run"}]}), encoding="utf-8")
        self.assertEqual(evalfrom.base_runs(str(folder)), {"xx": "scr-1", "yy": "scr-2"})
        self.assertEqual(evalfrom.base_runs("a=scr-1, b=scr-2"), {"a": "scr-1", "b": "scr-2"})
        self.assertEqual(evalfrom.base_runs(None), {})
        for bad in ("a", "a=", str(Path(self.tmp.name) / "missing.json")):
            with self.assertRaises(ValueError):
                evalfrom.base_runs(bad)

    def test_levers_line_names_the_arm(self):
        s = evalset.score({"id": "xx", "type": "other", "labels": []},
                          {"rows": [], "params": {"judge": "atomic3"}, "judge": {"applied": True}})
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("judge-atomic3 (1 of 1 ideas)", md)

    def test_cli_flags(self):
        from jevscreen import cli
        p = cli.build_parser()
        self.assertIsNone(p.parse_args(["screen", "idea"]).judge)
        self.assertEqual(p.parse_args(["screen", "idea", "--judge", "atomic3"]).judge, "atomic3")
        a = p.parse_args(["eval", "run", "--budget-each", "0.1", "--budget-total", "0.1", "--judge", "single10",
                          "--from-run", "x=scr-1"])
        self.assertEqual((a.judge, a.from_run), ("single10", "x=scr-1"))
        seen = {}

        def fake(cfg, idea, **kw):
            seen.update(kw)
            raise ValueError("stop here")
        for argv, want in ((["screen", "idea"], screen.UNSET), (["screen", "idea", "--judge", "none"], None),
                           (["screen", "idea", "--judge", "single10"], "single10")):
            with mock.patch.object(screen, "screen", fake), mock.patch("sys.stderr"):
                cli.cmd_screen(p.parse_args(argv), mock.Mock())
            self.assertIs(seen["judge"], want) if want is screen.UNSET or want is None else \
                self.assertEqual(seen["judge"], want)


if __name__ == "__main__":
    unittest.main()
