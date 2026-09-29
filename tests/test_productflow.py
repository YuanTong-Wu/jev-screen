"""`eval run --product-flow` (structural plan step 1): the eval screens the way the product lists a company (facets
derived from the idea text, the facet layer, idea-wording defaults that only demote, the shortlist tiers, your AI's
review layer off) and reports the full list's strict / lenient P@10, the high-confidence tier's P@min(10, n_high),
an estimated P@10 (unlabelled rows credited at their evidence tier's measured precision) and must-include top-10
recall; plus the free tier-order fix (a profile-only row whose L2 read was explicit, capped at related, ranks before
the related rows).

Offline: test_screen's seeded store and FakeJev (no network, no paid call)."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_screen as TS  # noqa: E402
from jevscreen import calib, evalfrom, evalset, productflow as pf, review, scope, shortlist  # noqa: E402

HANDS = {"id": "dexterous-hands", "idea": "人形机器人灵巧手", "idea_en": "Dexterous hands for humanoid robots"}
EVTOL = {"id": "evtol", "idea": "eVTOL aircraft makers", "idea_en": "eVTOL aircraft makers"}
WATER = {"id": "us-water", "idea": "美国水务", "idea_en": "Public companies owning and operating regulated "
         "drinking-water or wastewater utilities serving customers in the United States"}
WAREHOUSE = "Humanoid robots for warehouse picking"


class Facets(unittest.TestCase):
    def test_facets_come_from_the_idea_text_only(self):
        self.assertEqual(pf.facets_of_idea(HANDS["idea_en"]),
                         {"category": "Dexterous hands", "target": "humanoid robots", "type": "technology"})
        self.assertEqual(pf.facets_of_idea(WATER["idea_en"]),
                         {"category": "regulated drinking-water or wastewater utilities", "target": "the United States",
                          "type": "geography"})
        self.assertIsNone(pf.facets_of_idea(EVTOL["idea_en"]))            # no target: generic role question only
        self.assertEqual(pf.category_of("Listed suppliers developing, making, or selling sulfide, oxide, or other "
                                        "solid electrolyte materials for all-solid-state batteries"),
                         "sulfide, oxide, or other solid electrolyte materials")
        self.assertEqual(pf.category_of("Korea-listed cosmetics ODM and OEM manufacturers serving third-party beauty "
                                        "brands"), "cosmetics ODM and OEM")

    def test_wording_defaults_only_demote(self):
        sv = pf.sieve_for(HANDS)
        self.assertEqual(calib.validate_sieve(sv), [])
        got = [(e["family"], e["value"], e["answer"], e["source"], e["effect"]) for e in sv["scope_answers"]]
        self.assertEqual(got, [("role", "target_only", "no", "idea_wording", "demote"),
                               ("role", "buyer", "no", "idea_wording", "demote")])
        self.assertEqual([f"{e['family']}.{e['value']}" for e in pf.sieve_for(EVTOL)["scope_answers"]],
                         ["role.buyer", "role.holding"])
        self.assertEqual(pf.sieve_for(HANDS), pf.sieve_for(HANDS))          # deterministic
        self.assertEqual(scope.enforced(sv, scope.facets_sha(sv))[0]["effect"], "demote")
        self.assertEqual(pf.summary(sv), {"facets": sv["facets"], "defaults": ["role.target_only", "role.buyer"],
                                          "effect": "demote"})

    def test_a_default_that_would_remove_only_demotes(self):
        sv = pf.sieve_for(EVTOL)
        rows = [{"company_key": "a", "security_id": "X:A", "evidence_sha": "s", "l2_evidence": "annual_report"},
                {"company_key": "b", "security_id": "X:B", "evidence_sha": "t", "l2_evidence": "annual_report"}]
        facets = {"a": {"role": {"label": "buyer", "p": 0.9, "evidence_sha": "s"}},
                  "b": {"role": {"label": "supplier", "p": 0.9, "evidence_sha": "t"}}}
        kept, excluded, _notes = scope.apply_scope(rows, facets, sv, None, fsha=scope.facets_sha(sv))
        self.assertEqual(excluded, [])
        self.assertEqual([(r["company_key"], r.get("scope_demoted")) for r in kept], [("a", True), ("b", None)])


def row(sid, rank, l1="core", l2="explicit", ev="annual_report", **kw):
    return {"security_id": sid, "rank": rank, "l1_label": l1, "l2_label": l2, "l2_evidence": ev, **kw}


class TierOrder(unittest.TestCase):
    def test_capped_profile_rows_rank_before_related(self):
        rows = [row("X:HIGH", 1),
                row("X:PART", 2, l2="partial"),
                row("X:CAP", 3, l2="partial", ev="profile", l2_label_before_cap="explicit"),
                row("X:ADJ", 4, l1="adjacent"),
                row("X:PPROF", 5, l2="partial", ev="profile", l2_p_explicit=0.2, l2_p_partial=0.6),
                row("X:CAPOLD", 6, l2="partial", ev="profile", l2_p_explicit=0.7, l2_p_partial=0.2),
                row("X:DEM", 7, l2="partial", ev="profile", l2_label_before_cap="explicit", scope_demoted=True)]
        out, info = shortlist.apply(rows)
        self.assertEqual([r["security_id"] for r in out],
                         ["X:HIGH", "X:ADJ", "X:CAP", "X:CAPOLD", "X:PART", "X:PPROF", "X:DEM"])
        self.assertEqual([r["rank"] for r in out], list(range(1, 8)))
        self.assertEqual(info["high"], 1)
        self.assertIn("capped", info["rule"])

    def test_judge_c_stays_last(self):
        rows = [row("X:A", 1, judge_tier="A"), row("X:C", 2, judge_tier="C"),
                row("X:CAP", 3, l2="partial", ev="profile", l2_label_before_cap="explicit", judge_tier="B"),
                row("X:B", 4, l2="partial", judge_tier="B")]
        out, _ = shortlist.apply(rows)
        self.assertEqual([r["security_id"] for r in out], ["X:A", "X:CAP", "X:B", "X:C"])

    def test_evidence_tier(self):
        self.assertEqual(evalset.evidence_tier(row("X:A", 1)), "explicit_core")
        self.assertEqual(evalset.evidence_tier(row("X:A", 1, l1="adjacent")), "explicit_adjacent")
        self.assertEqual(evalset.evidence_tier(row("X:A", 1, l2="partial", ev="profile",
                                                   l2_label_before_cap="explicit")), "profile_capped")
        self.assertEqual(evalset.evidence_tier(row("X:A", 1, l2="partial", ev="profile", l2_p_explicit=0.7,
                                                   l2_p_partial=0.2)), "profile_capped")
        self.assertEqual(evalset.evidence_tier(row("X:A", 1, l2="partial")), "partial_core")
        self.assertEqual(evalset.evidence_tier(row("X:A", 1, l2="partial", l1="adjacent")), "partial_adjacent")
        self.assertEqual(evalset.evidence_tier(row("X:A", 1, l2="partial", ev="profile", l2_p_explicit=0.1,
                                                   l2_p_partial=0.6)), "partial_core")


class CapJev(TS.FakeJev):
    """L2 reads ServoJP's profile (TSE:6000, no annual report in the seed) as explicit: capped to related."""

    def classify(self, items, question):
        out = super().classify(items, question)
        if question.key == "fit" or question.key.startswith("facet_"):
            return out
        by_id = {it.item_id: it for it in items}
        for x in out:
            it = by_id.get(x["item_id"])
            if x["status"] == "ok" and it is not None and it.meta.get("security_id") == "TSE:6000":
                x.update(label="explicit", probs={"explicit": 0.8, "partial": 0.1, "insufficient": 0.1})
        return out


class Screen(TS.StoreCase):
    def factory(self, cls=TS.FakeJev):
        log = self.log

        def f(cfg, *, run_id, layer, budget_usd, dry_run):
            return cls(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
        return f

    def screen_(self, **kw):
        kw.setdefault("jev_factory", self.factory())
        kw.setdefault("min_mcap_usd", 0)
        kw.setdefault("sieve", "none")
        kw.setdefault("idea_en", WAREHOUSE)
        kw.setdefault("out_dir", self.home / f"o{len(self.log)}")
        return self.run_screen(**kw)

    def test_capped_row_says_so_and_ranks_before_related_in_the_tiers(self):
        res = self.screen_(jev_factory=self.factory(CapJev), shortlist=True)
        cap = next(r for r in res["rows"] if r["security_id"] == "TSE:6000")
        self.assertEqual((cap["l2_label"], cap["l2_label_cap"], cap["l2_label_before_cap"]),
                         ("partial", "仅简介", "explicit"))
        order = [r["security_id"] for r in res["rows"]]
        self.assertLess(order.index("TSE:6000"), order.index("NASDAQ:ROB2"))    # ROB2: related, annual report
        plain = self.screen_(shortlist=True)
        self.assertTrue(all("l2_label_before_cap" not in r for r in plain["rows"]))

    def test_product_flow_screen_demotes_and_never_removes(self):
        base = self.screen_()
        data = {"id": "x", "idea": "humanoid robots", "idea_en": WAREHOUSE}
        extra = [("licences", "role", "buyer", 0.9)]             # ROB2's text: the facet layer reads a buyer
        with mock.patch.object(TS, "FACET_KEYWORDS", TS.FACET_KEYWORDS + extra):
            res = self.screen_(**pf.screen_kwargs(data))
        self.assertEqual(sorted(r["security_id"] for r in res["rows"]),
                         sorted(r["security_id"] for r in base["rows"]))                 # nothing removed
        self.assertEqual(res.get("excluded_by_scope") or [], [])
        sha = {r["security_id"]: r["evidence_sha"] for r in base["rows"]}
        self.assertEqual({r["security_id"]: r["evidence_sha"] for r in res["rows"]}, sha)   # the same L2 text
        self.assertEqual(res["params"]["l2_question_sha"], base["params"]["l2_question_sha"])  # the same L2 question
        rob2 = next(r for r in res["rows"] if r["security_id"] == "NASDAQ:ROB2")
        self.assertTrue(rob2["scope_demoted"])
        self.assertEqual(res["rows"][-1]["security_id"], "NASDAQ:ROB2")                  # moved to the end
        self.assertTrue(res["params"]["facet_scan"])
        self.assertTrue(any(q.key == "facet_role" for c in self.log for _i, q in c.classified))
        self.assertTrue(all(r.get("shortlist_tier") for r in res["rows"]))
        self.assertFalse((calib.sieve_path(self.cfg, "humanoid robots")).exists())       # nothing written

    def test_product_flow_from_a_baseline_run_reads_no_l1(self):
        base = self.screen_()
        data = {"id": "x", "idea": "humanoid robots", "idea_en": WAREHOUSE}
        res = self.screen_(from_run=base["run_id"], jev_factory=evalfrom.no_l1_factory(self.cfg, self.factory()),
                           **pf.screen_kwargs(data))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["layers"]["l1"]["from_run"], base["run_id"])
        self.assertEqual(float(res["layers"]["l1"].get("cost_usd") or 0), 0.0)
        self.assertTrue(res["params"]["facet_scan"] and res["params"]["shortlist"])

    def test_agent_layer_off_ignores_a_review_file(self):
        base = self.screen_()
        sha = next(r["evidence_sha"] for r in base["rows"] if r["security_id"] == "NYSE:ROBO")
        doc = review.put_verdicts(review.new_agent("humanoid robots"), [{
            "company_key": "isin:US0000000001", "security_id": "NYSE:ROBO", "evidence_sha": sha, "v": "no",
            "chip": "h", "state": "applied", "why_zh": "是买方", "why_en": "a buyer"}])
        review.save_agent(self.cfg, "humanoid robots", doc)
        data = {"id": "x", "idea": "humanoid robots", "idea_en": WAREHOUSE}
        on = self.screen_(**{**pf.screen_kwargs(data), "agent_layer": True})
        self.assertIn("NYSE:ROBO", [r["security_id"] for r in on.get("excluded_by_agent") or []])
        off = self.screen_(**pf.screen_kwargs(data))
        self.assertEqual(off.get("excluded_by_agent") or [], [])
        self.assertIn("NYSE:ROBO", [r["security_id"] for r in off["rows"]])


def labels(*pairs, must=()):
    return [{"security_id": sid, "name": sid, "label": lab, "source_url": "https://www.sec.gov/a.htm", "note": "n",
             "by": "ai", "reviewed": False, "checked": "filing_read", **({"must_include": True} if sid in must else {})}
            for sid, lab in pairs]


class Estimate(unittest.TestCase):
    def idea(self, iid="a", labs=(), must=()):
        return {"id": iid, "type": "other", "idea": "x", "labels": labels(*labs, must=must)}

    def test_estimated_p_at_10_credits_unlabelled_rows_at_their_tier(self):
        rows = [row("X:1", 1), row("X:2", 2), row("X:3", 3), row("X:4", 4, l2="partial"),
                row("X:5", 5, l2="partial"), row("X:U1", 6), row("X:U2", 7, l2="partial")]
        data = self.idea(labs=[("X:1", "right"), ("X:2", "right"), ("X:3", "wrong"), ("X:4", "right"),
                               ("X:5", "edge")])
        s = evalset.score(data, {"rows": rows, "status": "ok"})
        self.assertEqual(s["tiers"]["explicit_core"], {"right": 2, "edge": 0, "wrong": 1, "unlabelled": 1})
        self.assertEqual(s["tiers"]["partial_core"], {"right": 1, "edge": 1, "wrong": 0, "unlabelled": 1})
        self.assertEqual(s["at"]["10"]["unlabelled_tiers"], {"explicit_core": 1, "partial_core": 1})
        agg = evalset.aggregate([s])
        tp = agg["tier_precision"]
        self.assertEqual((tp["explicit_core"]["n"], tp["explicit_core"]["precision"]), (3, round(2 / 3, 4)))
        self.assertEqual(tp["partial_core"]["precision"], 0.5)
        self.assertEqual(tp["partial_core"]["precision_lenient"], 1.0)
        # (3 right + 2/3 + 1/2) / 7 rows shown
        self.assertAlmostEqual(agg["all"]["p_est@10"], round((3 + 2 / 3 + 0.5) / 7, 4), places=4)
        self.assertAlmostEqual(agg["all"]["p_est_lenient@10"], round((4 + 2 / 3 + 1.0) / 7, 4), places=4)
        md = evalset.report_md([s], agg)
        self.assertIn("estimated P@10", md)
        self.assertIn("| explicit_core |", md)

    def test_a_tier_without_labels_is_credited_at_the_overall_precision(self):
        rows = [row("X:1", 1), row("X:2", 2, l1="adjacent", l2="partial")]
        s = evalset.score(self.idea(labs=[("X:1", "right")]), {"rows": rows, "status": "ok"})
        agg = evalset.aggregate([s])
        self.assertEqual(agg["tier_precision"]["partial_adjacent"]["n"], 0)
        self.assertEqual(agg["all"]["p_est@10"], 1.0)                       # overall labelled precision: 1/1

    def test_high_tier_and_must_include_top10_in_the_aggregate(self):
        rows, _ = shortlist.apply([row("X:1", 1), row("X:2", 2, l2="partial"), row("X:3", 3)])
        data = self.idea(labs=[("X:1", "right"), ("X:2", "right"), ("X:3", "wrong")], must=("X:2",))
        s = evalset.score(data, {"rows": rows, "status": "ok", "shortlist": {"high": 2}})
        self.assertEqual((s["shortlist"]["n"], s["shortlist"]["precision"]), (2, 0.5))
        agg = evalset.aggregate([s])
        self.assertEqual(agg["all"]["p_high"], 0.5)
        self.assertEqual(agg["all"]["n_high"], 2)
        self.assertEqual(agg["all"]["must_recall_top10"], 1.0)
        md = evalset.report_md([s], agg)
        self.assertIn("high-confidence tier strict P@min(10, n) 50%", md)


class EvalRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "set"
        self.d.mkdir()
        for data in (HANDS, EVTOL):
            (self.d / f"{data['id']}.json").write_text(json.dumps({
                "format": evalset.FORMAT, **data, "type": "other",
                "labels": labels(("NYSE:A", "right"))}), encoding="utf-8")

    def run_(self, fake, **kw):
        from jevscreen import eval_cli
        kw.setdefault("ideas", None)
        args = argparse.Namespace(set=self.d, json=True, budget_each=0.1, budget_total=0.2, reads=None,
                                  read_offset=0, **kw)
        cfg = mock.Mock(home=Path(self.tmp.name))
        bases = mock.patch.object(evalfrom, "_load_bases", lambda cfg, ids: {
            r: {"idea": {"b1": HANDS["idea"], "b2": EVTOL["idea"]}[r], "params": {}} for r in ids})
        with mock.patch.object(eval_cli, "_store_keys", return_value=(None, None)), \
                mock.patch.object(eval_cli, "_emit"), bases:
            return eval_cli._run(args, cfg, screen_fn=fake)

    def fake(self, seen):
        def f(cfg, idea, **kw):
            seen[kw["out_dir"].name] = kw
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.0, "params": {},
                    "layers": {"l1": {"from_run": kw.get("from_run"), "cost_usd": 0.0}}}
        return f

    def test_product_flow_passes_the_product_path(self):
        seen: dict = {}
        self.assertEqual(self.run_(self.fake(seen), product_flow=True), 0)
        kw = seen["dexterous-hands"]
        self.assertEqual(kw["sieve"]["facets"]["target"], "humanoid robots")
        self.assertEqual((kw["facet_scan"], kw["shortlist"], kw["agent_layer"]), (True, True, False))
        self.assertEqual(seen["evtol"]["sieve"]["facets"], None)
        scores = json.loads(next((Path(self.tmp.name) / "evals").glob("*/scores.json")).read_text("utf-8"))
        s = {x["id"]: x for x in scores["scores"]}["dexterous-hands"]
        self.assertIn("product-flow", s["levers"])
        self.assertEqual(s["product_flow"]["defaults"], ["role.target_only", "role.buyer"])

    def test_product_flow_wins_over_a_control_arms_levers(self):
        seen: dict = {}
        self.run_(self.fake(seen), product_flow=True, shortlist=False, from_run="dexterous-hands=b1,evtol=b2")
        self.assertEqual(seen["evtol"]["shortlist"], True)
        self.assertEqual(seen["evtol"]["from_run"], "b2")

    def test_a_map_file_may_hold_more_ideas_than_selected(self):
        m = Path(self.tmp.name) / "bases.json"
        m.write_text(json.dumps({"dexterous-hands": "b1", "evtol": "b2", "not-selected": "b9"}), encoding="utf-8")
        seen: dict = {}
        self.assertEqual(self.run_(self.fake(seen), product_flow=True, ideas="evtol", from_run=str(m)), 0)
        self.assertEqual(list(seen), ["evtol"])
        with self.assertRaises(evalset.EvalError) as cm:                # an explicit pair must be selected
            self.run_(self.fake(seen), ideas="evtol", from_run="evtol=b2,dexterous-hands=b1")
        self.assertIn("not a selected idea: dexterous-hands", str(cm.exception))

    def test_off_by_default(self):
        seen: dict = {}
        self.run_(self.fake(seen))
        self.assertTrue(all("facet_scan" not in kw and kw["sieve"] == "none" for kw in seen.values()))

    def test_cli_flag(self):
        from jevscreen import cli
        p = cli.build_parser()
        a = p.parse_args(["eval", "run", "--budget-each", "0.1", "--budget-total", "0.1", "--product-flow"])
        self.assertTrue(a.product_flow)
        self.assertFalse(p.parse_args(["eval", "run", "--budget-each", "0.1", "--budget-total", "0.1"]).product_flow)


if __name__ == "__main__":
    unittest.main()
