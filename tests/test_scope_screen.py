"""The facet layer inside screen and the free rank_only re-rank (scope design §3, §5.5): what is read, the
band re-reads, budget skips with a note, carry-forward, partial runs, dry-run estimate, result/DB shape, and the parity
of rank_only with a full from_run. Offline: test_screen's seeded store and FakeJev."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_screen as TS  # noqa: E402
from jevscreen import calib, review, scope, screen, store  # noqa: E402

UPSTREAM_ROB2 = [("motion control software", "role", "upstream", 0.9)]


def sieve_with(cfg, idea, entries=(), **extra):
    path = calib.sieve_path(cfg, idea)
    sv = {**calib.new_sieve(idea), **extra}
    if entries:
        fsha = scope.facets_sha(sv)
        sv = scope.record(sv, [{**e, "facets_sha": e.get("facets_sha", fsha)} for e in entries])
    return calib.save_sieve(path, sv), path


class CachingJev(TS.FakeJev):
    """FakeJev with an answer cache shared by every client of a test: a cached item is free (estimate_uncached)."""
    CACHE: dict = {}

    def _key(self, q, it):
        return (q.key, q.instructions, json.dumps(q.criteria, sort_keys=True), getattr(q, "read", 0), it.text)

    def estimate_uncached(self, items, question):
        miss = [it for it in items if self._key(question, it) not in self.CACHE]
        return self.estimate(miss, question) if miss else {"requests": 0, "items": 0, "est_cost_usd": 0.0}

    def classify(self, items, question):
        miss = [it for it in items if self._key(question, it) not in self.CACHE]
        got = {r["item_id"]: r for r in (super().classify(miss, question) if miss else [])}
        out = []
        for it in items:
            k = self._key(question, it)
            if it.item_id in got and got[it.item_id]["status"] == "ok":
                self.CACHE[k] = got[it.item_id]
            r = got.get(it.item_id) or {**self.CACHE[k], "cached": True}
            out.append({**r, "item_id": it.item_id})
        return out


class FacetLayer(TS.StoreCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(TS, "FACET_KEYWORDS", list(UPSTREAM_ROB2))
        p.start()
        self.addCleanup(p.stop)

    def test_reads_only_verified_companies_and_records_labels(self):
        res = self.run_screen(min_mcap_usd=0, facet_scan=True, sieve="none")
        self.assertEqual(sorted(res["facets"]), sorted(r["company_key"] for r in res["rows"]))
        self.assertEqual(res["facets"]["isin:US0000000007"]["role"]["label"], "upstream")
        self.assertEqual(res["facets"]["isin:US0000000001"]["role"]["label"], "supplier")
        f = res["layers"]["facet"]
        self.assertEqual((f["items"], f["families"], f["status"]), (3, ["role"], "ok"))
        self.assertGreater(res["cost_usd"], res["layers"]["l1"]["cost_usd"] + res["layers"]["l2"]["cost_usd"])
        self.assertTrue(res["params"]["facet_scan"])
        self.assertIsNotNone(res["params"]["facet_question_sha"])
        layers = {r[0] for r in self.query("SELECT DISTINCT layer FROM screen_results WHERE run_id = ?",
                                           [res["run_id"]])}
        self.assertEqual(layers, {"l1", "l2", "facet_role"})
        with store.session(self.cfg, read_only=True) as con:
            pool = calib.load_pool(con, res["run_id"], res["params"])
        self.assertTrue(all(p["l2_label"] in ("explicit", "partial", "insufficient", None) for p in pool))
        self.assertEqual(sorted(p["company_key"] for p in pool),
                         sorted(r[0] for r in self.query("SELECT company_key FROM screen_results WHERE run_id = ? "
                                                         "AND layer = 'l2'", [res["run_id"]])))

    def test_off_by_default_and_auto(self):
        res = self.run_screen(min_mcap_usd=0)
        self.assertNotIn("facets", res)
        self.assertNotIn("facet", res["layers"])
        self.assertFalse(res["params"]["facet_scan"])
        idea = "humanoid robots"
        sieve_with(self.cfg, idea, [{"sid": "s1", "family": "role", "value": "upstream", "answer": "no",
                                     "source": "human"}])
        res = self.run_screen(min_mcap_usd=0, sieve="auto")            # an enforced answer switches it on
        self.assertTrue(res["params"]["facet_scan"])
        self.assertEqual([r["security_id"] for r in res["excluded_by_scope"]], ["NASDAQ:ROB2"])
        self.assertEqual(res["excluded_by_scope"][0]["verdict_source"], "scope")
        self.assertNotIn("NASDAQ:ROB2", [r["security_id"] for r in res["rows"]])
        self.assertEqual(res["scope"]["removed"], 1)

    def test_band_reads_average(self):
        with mock.patch.object(TS, "FACET_KEYWORDS", [("motion control software", "role", "upstream", 0.6)]):
            res = self.run_screen(min_mcap_usd=0, facet_scan=True, sieve="none")
        lab = res["facets"]["isin:US0000000007"]["role"]
        self.assertEqual((lab["label"], lab["n"]), ("upstream", 3))       # read 0 in the band: 2 more reads
        self.assertEqual(res["facets"]["isin:US0000000001"]["role"]["n"], 1)   # 0.85: out of the band
        self.assertEqual(res["layers"]["facet"]["band_items"], 1)

    def test_budget_skip_keeps_the_run_ok_with_a_note(self):
        res = self.run_screen(min_mcap_usd=0, facet_scan=True, sieve="none", budget_usd=0.025)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["layers"]["facet"]["skipped"], "budget")
        self.assertTrue(any("范围检查跳过：预算不够" in n for n in res["notes"]))
        self.assertEqual(res["facets"], {})

    def test_missing_labels_with_an_enforced_answer_is_partial(self):
        sieve_with(self.cfg, "humanoid robots", [{"sid": "s1", "family": "role", "value": "upstream", "answer": "no",
                                                  "source": "human"}])
        res = self.run_screen(min_mcap_usd=0, budget_usd=0.025, sieve="auto")
        self.assertEqual(res["status"], "partial")
        self.assertTrue(any("范围回答有 3 家没能检查" in n for n in res["notes"]))
        self.assertTrue(all(r.get("scope_unchecked") for r in res["rows"]))

    def test_a_family_whose_wording_names_a_company_is_skipped(self):
        sieve_with(self.cfg, "humanoid robots", facets={"category": "RoboCorp arms", "target": "warehouses"},
                   facets_zh=None)
        res = self.run_screen(min_mcap_usd=0, facet_scan=True, sieve="auto")
        self.assertEqual(res["layers"].get("facet"), None)
        self.assertTrue(any("范围检查跳过「role」" in n for n in res["notes"]))

    def test_dry_run_estimate(self):
        res = self.run_screen(min_mcap_usd=0, facet_scan=True, sieve="none", dry_run=True)
        n = res["layers"]["l2"]["inputs"]
        self.assertEqual(res["layers"]["facet"]["estimate"]["est_cost_usd"], scope.estimate_items(n, 1))
        self.assertAlmostEqual(scope.estimate_items(10_000, 2), 0.024)

    def test_carry_forward_after_a_scope_answer_under_a_tiny_update_budget(self):
        """Critique 2.4: an update pass on $0.001 must not silently drop the scope answers."""
        CachingJev.CACHE = {}

        def factory(cfg, *, run_id, layer, budget_usd, dry_run):
            return CachingJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=self.log)
        first = self.run_screen(min_mcap_usd=0, facet_scan=True, jev_factory=factory)
        sieve_with(self.cfg, "humanoid robots", [{"sid": "s1", "family": "role", "value": "upstream", "answer": "no",
                                                  "source": "human"}])
        with mock.patch.object(TS, "FACET_KEYWORDS", []):          # the facet cache would answer; force a miss
            CachingJev.CACHE = {k: v for k, v in CachingJev.CACHE.items() if not k[0].startswith("facet_")}
            upd = self.run_screen(from_run=first["run_id"], budget_usd=0.001, sieve="auto",
                                  out_dir=self.home / "upd", jev_factory=factory)
        self.assertTrue(upd["params"]["facet_scan"])
        self.assertEqual(upd["layers"]["facet"]["skipped"], "budget")
        self.assertEqual(upd["layers"]["facet"]["carried"], 3)
        self.assertEqual([r["security_id"] for r in upd["excluded_by_scope"]], ["NASDAQ:ROB2"])

    def test_cached_rerun_is_free_and_not_skipped(self):
        CachingJev.CACHE = {}
        fac = TS.make_factory(self.log)

        def factory(cfg, *, run_id, layer, budget_usd, dry_run):
            return CachingJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=self.log)
        first = self.run_screen(min_mcap_usd=0, facet_scan=True, sieve="none", jev_factory=factory)
        again = self.run_screen(from_run=first["run_id"], facet_scan=True, sieve="none", jev_factory=factory,
                                budget_usd=0.0001, out_dir=self.home / "again")
        self.assertEqual(again["layers"]["facet"]["status"], "ok")
        self.assertEqual(again["layers"]["facet"]["cost_usd"], 0.0)
        self.assertEqual(again["facets"]["isin:US0000000007"]["role"]["label"], "upstream")
        del fac


class RankOnly(TS.StoreCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(TS, "FACET_KEYWORDS", list(UPSTREAM_ROB2))
        p.start()
        self.addCleanup(p.stop)
        self.idea = "humanoid robots"
        self.base = self.run_screen(min_mcap_usd=0, facet_scan=True, max_out=2)

    def test_parity_with_a_full_from_run(self):
        sieve_with(self.cfg, self.idea, [{"sid": "s1", "family": "role", "value": "upstream", "answer": "no",
                                          "source": "human"}])
        ro = screen.screen(self.cfg, self.idea, from_run=self.base["run_id"], rank_only=True, change_kind="scope")
        full = self.run_screen(from_run=self.base["run_id"], sieve="auto", out_dir=self.home / "full")
        pick = lambda r: [(x["security_id"], x["rank"], x["verdict_source"], bool(x.get("backfill")))  # noqa: E731
                          for x in r["rows"]]
        self.assertEqual(pick(ro), pick(full))
        self.assertEqual([x["security_id"] for x in ro["excluded_by_scope"]],
                         [x["security_id"] for x in full["excluded_by_scope"]])
        self.assertEqual(ro["cost_usd"], 0.0)
        self.assertEqual((ro["params"]["rank_only"], ro["params"]["change_kind"], ro["params"]["from_run"]),
                         (True, "scope", self.base["run_id"]))
        out = Path(ro["output_dir"])
        for name in ("results.json", "results.csv", "report.md", "l2_inputs.jsonl", "funnel.jsonl.gz"):
            self.assertTrue((out / name).exists(), name)
        row = self.query("SELECT status, cost_usd, params_json FROM screen_runs WHERE run_id = ?", [ro["run_id"]])[0]
        self.assertEqual((row[0], row[1]), ("ok", 0.0))
        self.assertEqual(json.loads(row[2])["change_kind"], "scope")
        n_base = self.query("SELECT count(*) FROM screen_results WHERE run_id = ?", [self.base["run_id"]])[0][0]
        n_new = self.query("SELECT count(*) FROM screen_results WHERE run_id = ?", [ro["run_id"]])[0][0]
        self.assertEqual(n_base, n_new)
        self.assertEqual(screen.read_ledger(out)[0]["run_id"], ro["run_id"])
        # a row that moved up from below the cut carries its evidence
        tiny = next(x for x in ro["rows"] if x["security_id"] == "NYSE:TINY")
        self.assertTrue(tiny["backfill"])
        self.assertTrue(tiny["evidence_excerpt"])

    def test_agent_layer_parity(self):
        doc = review.new_agent(self.idea)
        sha = next(r["evidence_sha"] for r in self.base["rows"] if r["security_id"] == "NYSE:ROBO")
        doc = review.put_verdicts(doc, [{"company_key": "isin:US0000000001", "security_id": "NYSE:ROBO",
                                         "evidence_sha": sha, "v": "no", "chip": "h", "state": "applied",
                                         "why_zh": "是买方", "why_en": "a buyer"}])
        review.save_agent(self.cfg, self.idea, doc)
        ro = screen.screen(self.cfg, self.idea, from_run=self.base["run_id"], rank_only=True, change_kind="agent")
        full = self.run_screen(from_run=self.base["run_id"], sieve="auto", out_dir=self.home / "full")
        self.assertEqual([x["security_id"] for x in ro["rows"]], [x["security_id"] for x in full["rows"]])
        self.assertEqual([x["security_id"] for x in ro["excluded_by_agent"]], ["NYSE:ROBO"])
        self.assertEqual(ro["excluded_by_agent"][0]["verdict_source"], "agent")
        self.assertEqual([x["security_id"] for x in full["excluded_by_agent"]], ["NYSE:ROBO"])

    def test_guards(self):
        sieve_with(self.cfg, self.idea, rules=["mention_only"])
        with self.assertRaises(screen.RankOnlyUnsafe) as cm:
            screen.screen(self.cfg, self.idea, from_run=self.base["run_id"], rank_only=True)
        self.assertEqual(cm.exception.reason, "l2_question_sha")
        path = calib.sieve_path(self.cfg, self.idea)
        sv = calib.load_sieve(path)
        sv["rules"] = []
        sv["facets"] = {"category": "humanoid robots", "target": "warehouses"}
        calib.save_sieve(path, sv)
        with self.assertRaises(screen.RankOnlyUnsafe) as cm:
            screen.screen(self.cfg, self.idea, from_run=self.base["run_id"], rank_only=True)
        self.assertIn(cm.exception.reason, ("l2_question_sha", "facets_sha"))
        with self.assertRaises(ValueError):
            screen.screen(self.cfg, self.idea, rank_only=True)

    def test_store_busy(self):
        with mock.patch.object(screen, "RANK_ONLY_WRITE_WAIT_S", 0.1), \
                mock.patch.object(store, "session", side_effect=store.StoreLocked("busy")):
            with self.assertRaises(store.StoreLocked):
                screen.screen(self.cfg, self.idea, from_run=self.base["run_id"], rank_only=True)


if __name__ == "__main__":
    unittest.main()
