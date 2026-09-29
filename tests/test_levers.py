"""Accuracy levers behind flags (experiment queue #3 / #6 and the high-confidence shortlist):

- `--l2-constraints`: the idea's explicit constraints (end market, geography, role) derived from idea_en / facets;
  a cheap Jev question over the SAME L2 text of the L2-explicit companies; an explicit row whose text does not state
  the constraint becomes partial. Default OFF; L1 and the L2 question stay untouched (cache-safe).
- `--shortlist`: rows tiered into high (L1 core + L2 explicit) and confirm, the high tier first.

Offline: test_screen's seeded store and FakeJev (no network, no paid call)."""
from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_screen as TS  # noqa: E402
from jevscreen import constraints, evalset, page, screen, shortlist  # noqa: E402

STORAGE = ("Listed suppliers of liquid-cooling units, cold plates, and thermal-management systems for grid-scale "
           "battery storage cabinets and stations")
SEA = ("Digital payment and e-wallet services for Southeast Asian consumers and merchants, including payment "
       "processing, wallet management, and merchant acquiring")
WATER = ("Public companies owning and operating regulated drinking-water or wastewater utilities serving customers "
         "in the United States")


def kinds(cons):
    return {c["kind"]: c["text"] for c in cons}


class Derive(unittest.TestCase):
    def test_end_market_and_role(self):
        k = kinds(constraints.derive(STORAGE))
        self.assertEqual(k["end_market"], "grid-scale battery storage cabinets and stations")
        self.assertIn("role", k)
        self.assertNotIn("geography", k)

    def test_geography_in_the_for_clause_and_generic_customers_dropped(self):
        k = kinds(constraints.derive(SEA))
        self.assertEqual(k.get("geography"), "Southeast Asia")
        self.assertNotIn("end_market", k)              # "consumers and merchants" says nothing specific

    def test_serving_customers_in_a_place(self):
        k = kinds(constraints.derive(WATER))
        self.assertEqual(k.get("geography"), "the United States")
        self.assertNotIn("end_market", k)
        self.assertIn("operates", k["role"])

    def test_listing_nationality_is_not_a_text_constraint(self):
        for idea in ("Japanese factory automation equipment makers",
                     "Korea-listed cosmetics ODM and OEM manufacturers serving third-party beauty brands",
                     "Taiwan-listed ODM and contract manufacturers designing and building AI servers for customers",
                     "Indian electronics manufacturing services (EMS) companies"):
            k = kinds(constraints.derive(idea))
            self.assertNotIn("geography", k, idea)
        k = kinds(constraints.derive("Korea-listed cosmetics ODM and OEM manufacturers serving third-party beauty "
                                     "brands"))
        self.assertEqual(k["end_market"], "third-party beauty brands")
        self.assertNotIn("end_market", kinds(constraints.derive(
            "Taiwan-listed ODM and contract manufacturers designing and building AI servers for customers")))

    def test_brackets_and_used_in(self):
        self.assertEqual(kinds(constraints.derive("Advanced packaging equipment for HBM (TC bonders, hybrid "
                                                  "bonding)"))["end_market"], "HBM")
        self.assertEqual(kinds(constraints.derive("Listed manufacturers of photoresist formulations used in "
                                                  "semiconductor wafer fabrication"))["end_market"],
                         "semiconductor wafer fabrication")

    def test_nothing_to_check(self):
        self.assertEqual(constraints.derive("Humanoid robots"), [])
        self.assertEqual(constraints.derive(None), [])
        self.assertEqual(constraints.derive(""), [])

    def test_facets_and_given_constraints_win(self):
        k = kinds(constraints.derive("Humanoid robots", facets={"category": "robots", "target": "Japan",
                                                                "type": "geography"}))
        self.assertEqual(k, {"geography": "Japan"})
        k = kinds(constraints.derive("Humanoid robots", facets={"category": "vision AI", "target": "retail stores",
                                                                "type": "technology"}))
        self.assertEqual(k, {"end_market": "retail stores"})
        given = [{"kind": "end_market", "text": "hospitals"}, "not a dict"]
        self.assertEqual(constraints.derive(STORAGE, given=given), [{"kind": "end_market", "text": "hospitals"}])

    def test_deterministic(self):
        self.assertEqual(constraints.derive(STORAGE), constraints.derive(STORAGE))

    def test_or_list_of_roles_keeps_every_role(self):
        k = kinds(constraints.derive("Public companies that make or sell their own branded clear dental aligners "
                                     "for orthodontic treatment"))
        self.assertIn("makes", k["role"])
        self.assertIn("supplies", k["role"])
        k = kinds(constraints.derive("Listed suppliers developing, making, or selling sulfide or oxide solid "
                                     "electrolyte materials for solid-state batteries"))
        self.assertIn("develops", k["role"])
        self.assertIn("supplies", k["role"])
        self.assertEqual(kinds(constraints.derive(STORAGE))["role"], "supplies or provides it")


class Question(unittest.TestCase):
    def test_shape(self):
        cons = constraints.derive(STORAGE)
        q = constraints.build_question("储能液冷", STORAGE, cons)
        self.assertEqual(q.key, constraints.KEY)
        self.assertEqual(list(q.criteria), ["met", "product_only", "unclear"])
        self.assertIn("grid-scale battery storage cabinets and stations", q.criteria["met"])
        self.assertIn(STORAGE, q.instructions)
        self.assertEqual(constraints.question_sha(q), constraints.question_sha(
            constraints.build_question("储能液冷", STORAGE, cons)))

    def test_decide(self):
        d = constraints.decide
        self.assertEqual(d("explicit", {"label": "met", "p": 0.8}), ("explicit", "met"))
        self.assertEqual(d("explicit", {"label": "product_only", "p": 0.8}), ("partial", "missing"))
        self.assertEqual(d("explicit", {"label": "unclear", "p": 0.6}), ("partial", "unclear"))
        self.assertEqual(d("explicit", None), ("explicit", "unchecked"))
        self.assertEqual(d("partial", {"label": "product_only", "p": 0.9}), ("partial", None))
        self.assertEqual(d(None, None), (None, None))

    def test_aggregate_means(self):
        reads = [{"status": "ok", "probs": {"met": 0.6, "product_only": 0.4}},
                 {"status": "ok", "probs": {"met": 0.3, "product_only": 0.7}},
                 {"status": "failed", "probs": {}}]
        a = constraints.aggregate(reads)
        self.assertEqual((a["label"], a["n"]), ("product_only", 2))
        self.assertIsNone(constraints.aggregate([{"status": "failed"}]))


class ConstraintJev(TS.FakeJev):
    """FakeJev that answers the constraint question: met when a word of MET_WORDS is in both the question's 'met'
    criterion (the idea's constraint) and the text."""
    MET_WORDS = ("warehouse",)

    def classify(self, items, question):
        if question.key != constraints.KEY:
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
                met = any(w in it.text.lower() and w in question.criteria["met"].lower() for w in self.MET_WORDS)
                lab = "met" if met else "product_only"
                out.append({"item_id": it.item_id, "label": lab, "probs": {lab: 0.85, "unclear": 0.15},
                            "request_id": f"{self.layer}-req-{self._sent}", "status": "ok", "error": None,
                            "cached": False})
        return out


class ScreenCase(TS.StoreCase):
    def factory(self):
        log = self.log

        def f(cfg, *, run_id, layer, budget_usd, dry_run):
            return ConstraintJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
        return f

    def screen_(self, idea_en="Humanoid robots for hospitals", **kw):
        kw.setdefault("jev_factory", self.factory())
        kw.setdefault("min_mcap_usd", 0)
        kw.setdefault("sieve", "none")
        return self.run_screen(idea_en=idea_en, **kw)

    def constraint_calls(self):
        return [(items, q) for c in self.log for items, q in c.classified if q.key == constraints.KEY]


class ConstraintScreen(ScreenCase):
    def test_off_by_default(self):
        res = self.screen_()
        self.assertFalse(res["params"]["l2_constraints"])
        self.assertNotIn("constraints", res)
        self.assertNotIn("constraint", res["layers"])
        self.assertEqual(self.constraint_calls(), [])
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual(robo["l2_label"], "explicit")
        self.assertNotIn("l2_constraint", robo)

    def test_missing_constraint_demotes_explicit_to_partial(self):
        off = self.screen_(out_dir=self.home / "off")
        self.log.clear()
        res = self.screen_(l2_constraints=True)
        self.assertTrue(res["params"]["l2_constraints"])
        # cache-safe: the L1 and L2 questions are the same as without the lever
        self.assertEqual(res["params"]["l2_question_sha"], off["params"]["l2_question_sha"])
        self.assertEqual(res["questions"]["l1"], off["questions"]["l1"])
        self.assertEqual(res["questions"]["l2"], off["questions"]["l2"])
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual((robo["l2_label"], robo["l2_status"], robo["l2_label_before_constraint"],
                          robo["l2_constraint"]), ("partial", "partial", "explicit", "missing"))
        # only the L2-explicit companies are asked
        calls = self.constraint_calls()
        self.assertEqual(sum(len(items) for items, _ in calls), 1)
        info = res["constraints"]
        self.assertEqual(info["items"], [{"kind": "end_market", "text": "hospitals"}])
        self.assertIn("it is for hospitals", info["question"]["criteria"]["met"])
        self.assertEqual((info["checked"], info["demoted"]), (1, 1))
        self.assertEqual(res["layers"]["constraint"]["status"], "ok")
        self.assertGreater(res["layers"]["constraint"]["cost_usd"], 0)
        self.assertAlmostEqual(res["cost_usd"], res["layers"]["l1"]["cost_usd"] + res["layers"]["l2"]["cost_usd"]
                               + res["layers"]["constraint"]["cost_usd"], places=6)
        self.assertEqual(res["status"], "ok")

    def test_met_constraint_keeps_explicit(self):
        res = self.screen_(idea_en="Humanoid robots for warehouse picking", l2_constraints=True)
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual((robo["l2_label"], robo["l2_constraint"]), ("explicit", "met"))
        self.assertNotIn("l2_label_before_constraint", robo)
        self.assertEqual(res["constraints"]["demoted"], 0)

    def test_budget_skip_keeps_explicit_unchecked_with_a_note(self):
        res = self.screen_(l2_constraints=True, budget_usd=0.025)
        self.assertEqual(res["layers"]["constraint"]["skipped"], "budget")
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual((robo["l2_label"], robo["l2_constraint"]), ("explicit", "unchecked"))
        self.assertEqual(res["status"], "partial")
        self.assertTrue(any("限定条件" in n and "not enough budget" in n for n in res["notes"]))

    def test_no_constraint_in_the_idea_skips_the_layer(self):
        res = self.screen_(idea_en="Humanoid robots", l2_constraints=True)
        self.assertEqual(res["layers"]["constraint"]["skipped"], "no_constraints")
        self.assertEqual(self.constraint_calls(), [])
        self.assertEqual(res["status"], "ok")
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual(robo["l2_label"], "explicit")

    def test_dry_run_estimates_without_classifying(self):
        res = self.screen_(l2_constraints=True, dry_run=True)
        self.assertIn("estimate", res["layers"]["constraint"])
        self.assertEqual(self.constraint_calls(), [])

    def test_from_run_inherits_the_flag(self):
        first = self.screen_(l2_constraints=True, out_dir=self.home / "a")
        again = self.screen_(from_run=first["run_id"], out_dir=self.home / "b")
        self.assertTrue(again["params"]["l2_constraints"])
        robo = next(r for r in again["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual(robo["l2_constraint"], "missing")

    def test_constraint_answers_are_stored_and_the_pool_applies_them(self):
        res = self.screen_(l2_constraints=True, max_out=1)
        got = self.query("SELECT security_id, label, status FROM screen_results WHERE run_id = ? AND layer = ?",
                         [res["run_id"], constraints.KEY])
        self.assertEqual(got, [("NYSE:ROBO", "product_only", "ok")])
        from jevscreen import calib, store
        with store.session(self.cfg, read_only=True) as con:
            pool = {p["security_id"]: p for p in calib.load_pool(con, res["run_id"], res["params"])}
        robo = pool["NYSE:ROBO"]
        self.assertEqual((robo["l2_label"], robo["l2_status"], robo["l2_constraint"],
                          robo["l2_label_before_constraint"]), ("partial", "partial", "missing", "explicit"))
        # the L2 answer itself is stored as read: without the lever the pool shows it explicit
        with store.session(self.cfg, read_only=True) as con:
            raw = {p["security_id"]: p for p in calib.load_pool(con, res["run_id"], {})}
        self.assertEqual(raw["NYSE:ROBO"]["l2_label"], "explicit")
        self.assertNotIn("l2_constraint", raw["NYSE:ROBO"])

    def test_rank_only_keeps_a_constrained_base_free_and_equal(self):
        first = self.screen_(l2_constraints=True, out_dir=self.home / "a")
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=first["run_id"], sieve="none",
                                  out_dir=self.home / "ro")
        self.assertEqual(ro["cost_usd"], 0.0)
        self.assertTrue(ro["params"]["rank_only"] and ro["params"]["l2_constraints"])

        def shape(r):
            return [(x["security_id"], x["rank"], x["l2_label"], x.get("l2_constraint"),
                     x.get("l2_label_before_constraint")) for x in r["rows"]]
        self.assertEqual(shape(ro), shape(first))
        # the copied answers (the constraint layer too) serve a version of the version
        ro2 = screen.rank_only_run(self.cfg, "humanoid robots", from_run=ro["run_id"], sieve="none",
                                   out_dir=self.home / "ro2")
        self.assertEqual(shape(ro2), shape(first))

    def test_rank_only_refuses_a_changed_constraint_question(self):
        first = self.screen_(l2_constraints=True)
        with mock.patch.object(constraints, "derive", return_value=[{"kind": "end_market", "text": "farms"}]):
            with self.assertRaises(screen.RankOnlyUnsafe) as cm:
                screen.rank_only_run(self.cfg, "humanoid robots", from_run=first["run_id"], sieve="none",
                                     out_dir=self.home / "ro")
        self.assertIn("constraint_question_sha", str(cm.exception.reason))

    def test_review_version_of_a_lever_run_is_free(self):
        from jevscreen import review_cli
        for kw in ({"l2_constraints": True}, {"shortlist": True}, {"l2_constraints": True, "shortlist": True}):
            first = self.screen_(out_dir=self.home / "a", **kw)
            res, notes = review_cli.new_version(self.cfg, "humanoid robots", first["run_id"], "decide")
            self.assertTrue(res["params"]["rank_only"], kw)
            self.assertEqual(res["cost_usd"], 0.0, kw)
            self.assertEqual(notes, [], kw)
            self.assertEqual([(r["security_id"], r["rank"], r.get("shortlist_tier")) for r in res["rows"]],
                             [(r["security_id"], r["rank"], r.get("shortlist_tier")) for r in first["rows"]], kw)
            self.assertEqual(res.get("shortlist"), first.get("shortlist"), kw)

    def test_rank_ev_demotion_moves_the_score(self):
        off = self.screen_(rank="ev", out_dir=self.home / "off")
        on = self.screen_(rank="ev", l2_constraints=True, out_dir=self.home / "on")
        r_off = next(r for r in off["rows"] if r["security_id"] == "NYSE:ROBO")
        r_on = next(r for r in on["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual(r_on["l2_constraint"], "missing")
        self.assertLess(r_on["score"], r_off["score"])
        want = screen.score_ev(0.0, r_on["l2_p_explicit"] + r_on["l2_p_partial"], r_on["l1_p_core"],
                               r_on["market_cap_usd"], r_on["l2_evidence"])
        self.assertAlmostEqual(r_on["score"], round(want, 4), places=3)
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=on["run_id"], sieve="none",
                                  out_dir=self.home / "ro")
        self.assertEqual([(r["security_id"], r["score"]) for r in ro["rows"]],
                         [(r["security_id"], r["score"]) for r in on["rows"]])

    def test_read_offset_redraws_the_band_reads(self):
        class BandJev(ConstraintJev):
            def classify(self, items, question):
                out = super().classify(items, question)
                if question.key == constraints.KEY:
                    for r in out:
                        if r["status"] == "ok":
                            r.update(label="met", probs={"met": 0.5, "product_only": 0.5})
                return out
        log = self.log

        def f(cfg, *, run_id, layer, budget_usd, dry_run):
            return BandJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
        res = self.screen_(l2_constraints=True, jev_factory=f, out_dir=self.home / "a")
        self.assertEqual(sorted(q.read for _i, q in self.constraint_calls()), [0, 1, 2])
        self.log.clear()
        res = self.screen_(l2_constraints=True, jev_factory=f, read_offset=3, out_dir=self.home / "b")
        self.assertEqual(sorted(q.read for _i, q in self.constraint_calls()), [0, 3, 4, 5])
        self.assertEqual(res["layers"]["constraint"]["read_offset"], 3)
        # the fresh reads replace read 0 in the mean (as the L2 band does)
        lab = next(iter(res["constraints"]["labels"].values()))
        self.assertEqual(lab["n"], 3)


class ShortlistPure(unittest.TestCase):
    ROWS = [{"rank": 1, "security_id": "A", "l1_label": "adjacent", "l2_label": "explicit"},
            {"rank": 2, "security_id": "B", "l1_label": "core", "l2_label": "partial"},
            {"rank": 3, "security_id": "C", "l1_label": "core", "l2_label": "explicit"},
            {"rank": 4, "security_id": "D", "l1_label": "core", "l2_label": "explicit"},
            {"rank": 9, "security_id": "E", "l1_label": "core", "l2_label": "explicit", "below_cut": True}]

    def test_high_tier_first_and_renumbered(self):
        rows, info = shortlist.apply([dict(r) for r in self.ROWS])
        self.assertEqual([r["security_id"] for r in rows], ["C", "D", "A", "B", "E"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3, 4, 9])
        self.assertEqual([r["rank_before_shortlist"] for r in rows[:4]], [3, 4, 1, 2])
        self.assertEqual([r["shortlist_tier"] for r in rows], ["high", "high", "confirm", "confirm", "high"])
        self.assertEqual((info["high"], info["confirm"]), (3, 2))       # over every row, E below the cut too

    def test_tier_rule(self):
        self.assertEqual(shortlist.tier_of({"l1_label": "core", "l2_label": "explicit"}), "high")
        # the constraint check did not reach it (budget, Jev unavailable, beyond READ_MAX): not high confidence
        self.assertEqual(shortlist.tier_of({"l1_label": "core", "l2_label": "explicit",
                                            "l2_constraint": "unchecked"}), "confirm")
        self.assertEqual(shortlist.tier_of({"l1_label": "core", "l2_label": "explicit", "l2_constraint": "met"}),
                         "high")
        self.assertEqual(shortlist.tier_of({"l1_label": "core", "l2_label": "partial"}), "confirm")
        self.assertEqual(shortlist.tier_of({"l1_label": None, "l2_label": "explicit"}), "confirm")

    def test_view_of_a_saved_result_leaves_it_untouched(self):
        res = {"rows": [dict(r) for r in self.ROWS]}
        view = shortlist.view(res)
        self.assertEqual([r["security_id"] for r in view["rows"]], ["C", "D", "A", "B", "E"])
        self.assertEqual(res["rows"][0]["security_id"], "A")
        self.assertEqual(view["shortlist"]["high"], 3)


class ShortlistScreen(ScreenCase):
    def test_on_by_default_off_with_no_shortlist(self):
        res = self.screen_()
        self.assertTrue(res["params"]["shortlist"])          # the owner decision of 2026-09-29: never padded
        self.assertIn("shortlist", res)
        res = self.screen_(shortlist=False)
        self.assertFalse(res["params"]["shortlist"])
        self.assertNotIn("shortlist", res)
        self.assertTrue(all("shortlist_tier" not in r for r in res["rows"]))
        with open(self.home / "out" / "results.csv", encoding="utf-8") as f:
            self.assertNotIn("shortlist_tier", next(csv.reader(f)))

    def test_tiers_in_results_json_and_csv(self):
        res = self.screen_(shortlist=True)
        tiers = [r["shortlist_tier"] for r in res["rows"]]
        self.assertEqual(tiers, sorted(tiers, key=lambda t: t != "high"))
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual((robo["shortlist_tier"], robo["rank"]), ("high", 1))
        self.assertEqual(res["shortlist"]["high"], sum(1 for t in tiers if t == "high"))
        saved = json.loads((self.home / "out" / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["shortlist"], res["shortlist"])
        with open(self.home / "out" / "results.csv", encoding="utf-8") as f:
            rd = list(csv.DictReader(f))
        self.assertEqual([r["shortlist_tier"] for r in rd], tiers)

    def test_with_constraints_a_demoted_row_is_to_confirm(self):
        res = self.screen_(shortlist=True, l2_constraints=True)
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual(robo["shortlist_tier"], "confirm")
        self.assertEqual(res["shortlist"]["high"], 0)


def page_rows():
    return [{"rank": 1, "security_id": "NYSE:A", "name": "Alpha", "l1_label": "core", "l2_label": "explicit",
             "l2_evidence": "annual_report", "shortlist_tier": "high", "rank_before_shortlist": 2},
            {"rank": 2, "security_id": "NYSE:B", "name": "Beta", "l1_label": "adjacent", "l2_label": "partial",
             "l2_evidence": "annual_report", "shortlist_tier": "confirm", "rank_before_shortlist": 1,
             "l2_constraint": "missing", "l2_label_before_constraint": "explicit"}]


class ShortlistPage(unittest.TestCase):
    def data(self, lang, shortlisted=True):
        rows = page_rows() if shortlisted else [{k: v for k, v in r.items() if k not in (
            "shortlist_tier", "rank_before_shortlist")} for r in page_rows()]
        res = {"run_id": "scr-x", "idea": "储能液冷", "idea_en": STORAGE, "status": "ok", "rows": rows,
               "params": {"min_mcap_usd": 1e9}, "output_dir": None}
        if shortlisted:
            res["shortlist"] = {"high": 1, "confirm": 1}
        return page.build_page_data(res, None, lang=lang, l2_pieces={})

    def test_rows_carry_the_tier_and_the_page_counts(self):
        d = self.data("zh")
        self.assertEqual([r["tier"] for r in d["rows"]], ["high", "confirm"])
        self.assertEqual(d["shortlist"], {"high": 1, "confirm": 1, "agent": 0})
        self.assertIsNone(self.data("zh", False).get("shortlist"))
        self.assertTrue(all(r.get("tier") is None for r in self.data("zh", False)["rows"]))

    def test_constraint_badge_and_no_explicit_upgrade(self):
        d = self.data("zh")
        beta = d["rows"][1]
        self.assertIn("constraint_missing", beta["badges"])
        self.assertNotEqual(beta["verdict"], "explicit")

    def test_unchecked_badge(self):
        self.assertIn("constraint_unchecked", page._badges({"l2_constraint": "unchecked"}, None))
        self.assertNotIn("constraint_unchecked", page._badges({"l2_constraint": "met"}, None))
        self.assertRegex(page.STRINGS["zh"]["badge_constraint_unchecked"], r"[一-鿿]")
        self.assertNotRegex(page.STRINGS["en"]["badge_constraint_unchecked"], r"[一-鿿]")

    def test_text_is_pure_per_language(self):
        zh = page.render_text(self.data("zh"))
        self.assertIn(page.STRINGS["zh"]["main_title"].format(n=1), zh)
        self.assertIn(page.STRINGS["zh"]["confirm_title"].format(n=1), zh)
        en = page.render_text(self.data("en"))
        self.assertIn(page.STRINGS["en"]["main_title"].format(n=1), en)
        for key in ("main_title", "confirm_title", "main_rows_title", "main_note", "badge_constraint_missing",
                    "badge_constraint_unclear", "confirm_note"):
            self.assertRegex(page.STRINGS["zh"][key], r"[一-鿿]", key)
            self.assertNotRegex(page.STRINGS["en"][key], r"[一-鿿]", key)

    def test_html_script_groups_by_tier(self):
        html = page.render_page(self.data("en"))
        self.assertIn("confirm_title", html)
        self.assertIn('"shortlist"', html)


class EvalShortlist(unittest.TestCase):
    DATA = {"id": "xx", "type": "other", "labels": [
        {"security_id": "NYSE:A", "label": "right"}, {"security_id": "NYSE:B", "label": "wrong"},
        {"security_id": "NYSE:C", "label": "edge"}]}

    def test_score_reports_the_high_tier(self):
        rows = [{"rank": 1, "security_id": "NYSE:B", "l1_label": "core", "l2_label": "partial"},
                {"rank": 2, "security_id": "NYSE:A", "l1_label": "core", "l2_label": "explicit"},
                {"rank": 3, "security_id": "NYSE:C", "l1_label": "core", "l2_label": "explicit"}]
        s = evalset.score(self.DATA, {"rows": rows})
        self.assertNotIn("shortlist", s)
        s = evalset.score(self.DATA, shortlist.view({"rows": rows}))
        self.assertEqual(s["at"]["10"]["precision"], round(1 / 3, 4))   # the order does not change P@10
        hi = s["shortlist"]
        self.assertEqual((hi["n"], hi["right"], hi["edge"], hi["wrong"]), (2, 1, 1, 0))
        self.assertEqual((hi["precision"], hi["precision_lenient"]), (0.5, 1.0))
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("## Main list (the confirmed tier", md)
        self.assertIn("| xx | 2 (2) | 50% | 100% | 1/1/0/0 |", md)

    def test_score_reports_high_tier_must_include_and_levers(self):
        data = {"id": "xx", "type": "other", "labels": [
            {"security_id": "NYSE:A", "label": "right", "must_include": True},
            {"security_id": "NYSE:D", "label": "right", "must_include": True},
            {"security_id": "NYSE:B", "label": "wrong"}]}
        rows = [{"rank": 1, "security_id": "NYSE:B", "l1_label": "core", "l2_label": "partial"},
                {"rank": 2, "security_id": "NYSE:A", "l1_label": "core", "l2_label": "explicit"},
                {"rank": 3, "security_id": "NYSE:D", "l1_label": "adjacent", "l2_label": "explicit"}]
        s = evalset.score(data, shortlist.view({"rows": rows, "params": {"l2_constraints": True}}))
        hi = s["shortlist"]
        self.assertEqual((hi["must_found"], hi["must_n"], hi["must_recall"]), (1, 2, 0.5))
        self.assertEqual(s["levers"], ["l2-constraints", "shortlist"])
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("| xx | 1 (1) | 100% | 100% | 1/0/0/0 | 1/2 |", md)
        self.assertIn("Levers on: l2-constraints (1 of 1 ideas), shortlist (1 of 1 ideas)", md)
        base = evalset.score(data, {"rows": rows})
        self.assertEqual(base["levers"], [])
        self.assertNotIn("Levers on", evalset.report_md([base], evalset.aggregate([base])))

    def test_cli_off_switches(self):
        from jevscreen import cli
        p = cli.build_parser()
        for flag, dest in (("shortlist", "shortlist"), ("l2-constraints", "l2_constraints")):
            self.assertIsNone(getattr(p.parse_args(["screen", "idea"]), dest))
            self.assertIs(getattr(p.parse_args(["screen", "idea", f"--{flag}"]), dest), True)
            self.assertIs(getattr(p.parse_args(["screen", "idea", f"--no-{flag}"]), dest), False)
        seen = {}

        def fake(cfg, idea, **kw):
            seen.update(kw)
            raise ValueError("stop here")
        args = p.parse_args(["screen", "idea", "--no-shortlist", "--l2-constraints"])
        with mock.patch.object(screen, "screen", fake), mock.patch("sys.stderr"):
            self.assertEqual(cli.cmd_screen(args, mock.Mock()), 1)
        self.assertIs(seen["shortlist"], False)
        self.assertIs(seen["l2_constraints"], True)
        args = p.parse_args(["screen", "idea"])
        with mock.patch.object(screen, "screen", fake), mock.patch("sys.stderr"):
            cli.cmd_screen(args, mock.Mock())
        self.assertIs(seen["shortlist"], screen.UNSET)
        self.assertIs(seen["l2_constraints"], screen.UNSET)

    def test_cli_flags_exist(self):
        from jevscreen import cli
        p = cli.build_parser()
        a = p.parse_args(["screen", "idea", "--l2-constraints", "--shortlist"])
        self.assertTrue(a.l2_constraints and a.shortlist)
        a = p.parse_args(["eval", "run", "--budget-each", "0.1", "--budget-total", "0.1", "--l2-constraints",
                          "--shortlist"])
        self.assertTrue(a.l2_constraints and a.shortlist)
        a = p.parse_args(["eval", "score", "RUN", "--idea", "x", "--shortlist"])
        self.assertTrue(a.shortlist)

    def test_eval_run_passes_the_levers(self):
        from jevscreen import eval_cli
        seen = []

        def fake(cfg, idea, **kw):
            seen.append(kw)
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.0}
        import argparse
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "set"
            d.mkdir()
            (d / "xx.json").write_text(json.dumps({
                "format": evalset.FORMAT, "id": "xx", "idea": "Humanoid robots", "type": "other",
                "labels": [{"security_id": "NYSE:A", "name": "A", "label": "right", "source_url":
                            "https://www.sec.gov/a.htm", "note": "n", "by": "ai", "reviewed": False,
                            "checked": "filing_read"}]}), encoding="utf-8")
            args = argparse.Namespace(set=d, json=True, budget_each=0.1, budget_total=0.1, ideas=None, reads=None,
                                      read_offset=0, l2_constraints=True, shortlist=True)
            cfg = mock.Mock(home=Path(tmp))
            with mock.patch.object(eval_cli, "_store_keys", return_value=(None, None)), \
                    mock.patch.object(eval_cli, "_emit"):
                eval_cli._run(args, cfg, screen_fn=fake)
        self.assertTrue(seen[0]["l2_constraints"])
        self.assertTrue(seen[0]["shortlist"])


if __name__ == "__main__":
    unittest.main()
