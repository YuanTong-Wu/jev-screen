"""Tests for jevscreen.evalset and `jevscreen eval` (the open evaluation set). No network, no paid call."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import config, eval_cli, evalset  # noqa: E402

SEC = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/x10k.htm"


def label(sid, lab, must=False, url=SEC, by="ai"):
    return {"security_id": sid, "name": sid, "label": lab, "must_include": must, "source_url": url,
            "note": "own words", "by": by, "reviewed": False}


def idea(**kw):
    d = {"format": evalset.FORMAT, "id": "demo-idea", "idea": "demo idea", "idea_en": "Demo idea",
         "type": "product_category", "markets": ["US"], "min_mcap_usd": 1e9, "countries": None,
         "labels": [label("NYSE:A", "right", True), label("NYSE:B", "edge"), label("NYSE:C", "wrong"),
                    label("NYSE:D", "right", True)]}
    d.update(kw)
    return d


def result(ids, **kw):
    return {"run_id": "scr-1", "status": "ok", "cost_usd": 0.2, "timing": {"total_s": 60.0},
            "rows": [{"rank": i, "security_id": s} for i, s in enumerate(ids, 1)], **kw}


class TestCheck(unittest.TestCase):
    def test_a_good_file(self):
        self.assertEqual(evalset.check(idea()), [])

    def test_gray_and_missing_sources_are_refused(self):
        bad = idea(labels=[label("NYSE:A", "right", url="https://www.tradingview.com/symbols/NYSE-A/"),
                           label("NYSE:B", "edge", url=None), label("NYSE:C", "edge", True),
                           label("X", "maybe")])
        probs = " | ".join(evalset.check(bad))
        for needle in ("gray-private", "source_url is missing", "must_include only on a 'right' label",
                       "EXCHANGE:SYMBOL", "right, edge or wrong"):
            self.assertIn(needle, probs)

    def test_source_kinds(self):
        self.assertEqual(evalset.source_kind("http://www.cninfo.com.cn/new/disclosure/detail?x=1"), "official")
        self.assertEqual(evalset.source_kind("https://disclosure2.edinet-fsa.go.jp/x"), "official")
        self.assertEqual(evalset.source_kind("https://finance.yahoo.com/quote/A"), "gray")
        self.assertEqual(evalset.source_kind("https://ir.example.com/annual"), "company")
        self.assertEqual(evalset.source_kind(""), "missing")
        self.assertEqual(evalset.source_kind("http://file.finance.sina.com.cn/x.PDF"), "third_party")
        self.assertIn("mirror", " ".join(evalset.check(idea(labels=[label("NYSE:A", "right",
                                                                           url="https://xueqiu.com/S/A")]))))
        self.assertIn("checked must be", " ".join(evalset.check(idea(labels=[{**label("NYSE:A", "right"),
                                                                              "checked": "vibes"}]))))
        self.assertEqual(evalset.check(idea(labels=[{**label("NYSE:A", "right"),
                                                      "checked": "official_ir_read"}])), [])

    def test_reviewed_must_be_a_boolean(self):
        for value in ("false", "yes", 1, None):
            with self.subTest(value=value):
                problems = evalset.check(idea(labels=[{**label("NYSE:A", "right"), "reviewed": value}]))
                self.assertIn("reviewed must be a boolean", " ".join(problems))

    def test_a_label_that_is_not_an_object_is_reported_not_a_crash(self):
        probs = evalset.check(idea(labels=["NYSE:A", 5, label("NYSE:B", "edge")]))
        self.assertEqual(probs, ["label 1 (?): not an object", "label 2 (?): not an object"])

    def test_duplicates_are_found_after_normalising_and_spaces_are_refused(self):
        probs = " | ".join(evalset.check(idea(labels=[label("NYSE:A", "right", True), label("nyse:a", "wrong")])))
        self.assertIn("listed twice", probs)
        probs = " | ".join(evalset.check(idea(labels=[label("KOSDAQ:058610", "right"), label("KRX:058610", "edge")])))
        self.assertIn("listed twice", probs)                     # KOSDAQ lines are KRX lines in the store
        probs = " | ".join(evalset.check(idea(labels=[label("NYSE:A ", "right", True)])))
        self.assertIn("spaces", probs)
        self.assertIn("EXCHANGE:SYMBOL", " ".join(evalset.check(idea(labels=[label("NYSE:", "right")]))))

    def test_a_non_english_idea_needs_its_fixed_english_sentence(self):
        self.assertIn("idea_en", " ".join(evalset.check(idea(idea="人形机器人关节减速器", idea_en=None))))
        self.assertIn("idea_en", " ".join(evalset.check(idea(idea="人形机器人关节减速器", idea_en="  "))))
        self.assertEqual(evalset.check(idea(idea="人形机器人关节减速器", idea_en="Humanoid robot joint reducers")), [])
        self.assertEqual(evalset.check(idea(idea="Uranium miners", idea_en=None)), [])   # English: used as is

    def test_screen_filters_are_checked(self):
        probs = " | ".join(evalset.check(idea(min_mcap_usd="1B", countries="CN")))
        self.assertIn("min_mcap_usd", probs)
        self.assertIn("countries", probs)
        self.assertEqual(evalset.check(idea(min_mcap_usd=None, countries=["CN", "Hong Kong"])), [])

    def test_markets_must_be_codes_the_screen_filter_knows(self):
        # markets is the screen's filter when countries is null (eval run), so a typo would empty the universe
        self.assertIn("markets", " | ".join(evalset.check(idea(markets="IN"))))
        self.assertIn("XX", " | ".join(evalset.check(idea(markets=["IN", "XX"]))))
        self.assertEqual(evalset.check(idea(markets=["CA", "AU", "US", "KZ", "HK"])), [])   # uranium's markets
        self.assertEqual(evalset.check(idea(markets=None)), [])

    def test_screen_scope_is_countries_else_markets(self):
        self.assertEqual(evalset.screen_scope(idea(markets=["IN"], countries=None)), ["IN"])
        self.assertEqual(evalset.screen_scope(idea(markets=["US", "CN"], countries=["CN"])), ["CN"])
        self.assertIsNone(evalset.screen_scope(idea(markets=None, countries=None)))
        self.assertIsNone(evalset.screen_scope(idea(markets=[], countries=[])))

    def test_two_files_with_the_same_id_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.json", "b.json"):
                (Path(tmp) / name).write_text(json.dumps(idea()), encoding="utf-8")
            with self.assertRaises(evalset.EvalError) as cm:
                evalset.load_dir(tmp)
            self.assertIn("demo-idea", str(cm.exception))
            (Path(tmp) / "b.json").unlink()
            self.assertEqual(len(evalset.load_dir(tmp, ["demo-idea", "demo-idea"])), 1)


class TestScore(unittest.TestCase):
    def test_precision_counts_only_labelled_companies(self):
        s = evalset.score(idea(), result(["NYSE:A", "NYSE:B", "NYSE:X", "NYSE:C"]))
        a = s["at"]["10"]
        self.assertEqual((a["right"], a["edge"], a["wrong"], a["unlabelled"]), (1, 1, 1, 1))
        self.assertAlmostEqual(a["precision"], 1 / 3, places=4)
        self.assertAlmostEqual(a["precision_lenient"], 2 / 3, places=4)
        self.assertEqual((a["coverage"], a["unlabelled_ids"]), (0.75, ["NYSE:X"]))
        mi = s["must_include"]
        self.assertEqual((mi["n"], mi["found"], mi["missing"], mi["recall"]), (2, 1, ["NYSE:D"], 0.5))
        self.assertEqual((s["cost_usd"], s["seconds"], s["unreviewed_labels"]), (0.2, 60.0, 4))

    def test_top10_cut_and_unranked_rows(self):
        ids = [f"NYSE:Z{i}" for i in range(10)] + ["NYSE:A"]
        s = evalset.score(idea(), result(ids))
        self.assertEqual(s["must_include"]["found"], 1)
        self.assertEqual(s["must_include"]["found_top10"], 0)
        self.assertIsNone(s["at"]["10"]["precision"])            # nothing labelled in the top 10
        s = evalset.score(idea(), {"rows": [{"rank": None, "security_id": "NYSE:A"}]})
        self.assertEqual(s["shown"], 0)

    def test_aggregate_and_report(self):
        s1 = evalset.score(idea(), result(["NYSE:A", "NYSE:D"]))
        s2 = evalset.score(idea(id="geo-idea", type="geography"), result(["NYSE:C"]))
        agg = evalset.aggregate([s1, s2])
        self.assertEqual(agg["all"]["ideas"], 2)
        self.assertAlmostEqual(agg["all"]["p@10"], 0.5)
        self.assertEqual(set(agg["by_type"]), {"product_category", "geography"})
        self.assertAlmostEqual(agg["all"]["cost_usd"], 0.4)
        md = evalset.report_md([s1, s2], agg)
        self.assertIn("| geo-idea |", md)
        self.assertIn("P@10 50%", md)

    def test_exchange_aliases_and_the_other_line_of_a_dual_listing_match(self):
        data = idea(labels=[label("KOSDAQ:058610", "right", True), label("HKEX:0700", "right", True),
                            label("TSX:CCO", "right", True), label("NYSE:C", "wrong")])
        res = result([])
        res["rows"] = [{"rank": 1, "security_id": "KRX:058610", "company_key": "isin:KR1"},
                       {"rank": 2, "security_id": "HKEX:700", "company_key": "isin:KYG1"},
                       {"rank": 3, "security_id": "NYSE:CCJ", "company_key": "isin:CA1"},
                       {"rank": 4, "security_id": "NYSE:C", "company_key": "isin:US1"}]
        s = evalset.score(data, res)                       # no store: the dual listing is not known
        self.assertEqual((s["at"]["10"]["right"], s["at"]["10"]["unlabelled_ids"]), (2, ["NYSE:CCJ"]))
        s = evalset.score(data, res, keys={"TSX:CCO": "isin:CA1", "KRX:058610": "isin:KR1"})
        a = s["at"]["10"]
        self.assertEqual((a["right"], a["wrong"], a["unlabelled"]), (3, 1, 0))
        self.assertEqual((s["must_include"]["found"], s["must_include"]["missing"]), (3, []))
        self.assertEqual(s["matched_by_company"], ["TSX:CCO"])
        self.assertEqual(s["not_in_store"], ["HKEX:700", "NYSE:C"])

    def test_runs_that_did_not_finish_are_left_out_of_the_means_and_the_report_says_so(self):
        ok = evalset.score(idea(), result(["NYSE:A", "NYSE:D"]))
        cut = evalset.score(idea(id="cut-idea"), result([], status="budget_exhausted", run_id="scr-2"))
        busy = evalset.unscored(idea(id="busy-idea"), "jev_busy", run_id="scr-3", cost_usd=0.0)
        agg = evalset.aggregate([ok, cut, busy])
        self.assertEqual((agg["all"]["ideas"], agg["all"]["must_recall"]), (1, 1.0))
        self.assertEqual(agg["by_type"]["product_category"]["must_recall"], 1.0)
        self.assertEqual([x["id"] for x in agg["left_out"]], ["cut-idea", "busy-idea"])
        self.assertAlmostEqual(agg["cost_usd_total"], 0.4)
        md = evalset.report_md([ok, cut, busy], agg)
        self.assertIn("1 ideas scored", md)
        self.assertIn("left out of the means", md)
        self.assertIn("budget_exhausted", md)
        self.assertIn("jev_busy", md)
        self.assertIn("| status |", md)

    def test_the_report_says_when_labels_were_matched_by_security_id_only(self):
        s = evalset.score(idea(), result(["NYSE:A"]))              # the store could not be read
        self.assertIn("matched by security_id only", evalset.report_md([s], evalset.aggregate([s])))
        s = evalset.score(idea(), result(["NYSE:A"]), keys={"NYSE:A": "isin:US1"})
        self.assertNotIn("matched by security_id only", evalset.report_md([s], evalset.aggregate([s])))

    def test_the_report_counts_unreviewed_and_search_summary_labels(self):
        labs = [label("NYSE:A", "right", True), {**label("NYSE:B", "edge"), "checked": "search_summary"},
                {**label("NYSE:C", "wrong", by="human"), "reviewed": True}]
        s = evalset.score(idea(labels=labs), result(["NYSE:A"]))
        self.assertEqual((s["labels"], s["unreviewed_labels"], s["search_summary_labels"]), (3, 2, 1))
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("2 of 3 labels are unreviewed", md)
        self.assertIn("1 checked only against a search summary", md)
        self.assertIn("| 2/3 (1 search summary) |", md)

    def test_official_ir_read_is_distinct_from_a_filing_and_from_a_search_summary(self):
        labs = [{**label("NYSE:A", "right"), "checked": "filing_read", "reviewed": True},
                {**label("NYSE:B", "edge"), "checked": "official_ir_read", "reviewed": True},
                {**label("NYSE:C", "wrong"), "checked": "search_summary"}]
        s = evalset.score(idea(labels=labs), result(["NYSE:A", "NYSE:B", "NYSE:C"]))
        self.assertEqual((s["official_ir_labels"], s["search_summary_labels"]), (1, 1))
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("1 checked against official IR material", md)
        self.assertIn("1 official IR", md)

    def test_reviewed_search_summary_still_has_an_overall_source_warning(self):
        lab = {**label("NYSE:A", "right"), "reviewed": True, "checked": "search_summary"}
        s = evalset.score(idea(labels=[lab]), result(["NYSE:A"]))
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("Every label is reviewed", md)
        self.assertIn("1 checked only against a search summary", md)


class TestLabelPolicy(unittest.TestCase):
    """Owner label policy 2026-09-28 (docs/EVAL.md): diversified flag, owner-reviewed marker, evidence location,
    unresolved search summaries, strict (headline) and lenient P@k, P@min(k, n) with n shown."""

    def test_diversified_must_be_a_boolean_on_a_right_or_edge_label(self):
        self.assertEqual(evalset.check(idea(labels=[{**label("NYSE:A", "right"), "diversified": True},
                                                    {**label("NYSE:B", "edge"), "diversified": False}])), [])
        probs = " | ".join(evalset.check(idea(labels=[{**label("NYSE:A", "right"), "diversified": "yes"}])))
        self.assertIn("diversified must be a boolean", probs)
        probs = " | ".join(evalset.check(idea(labels=[{**label("NYSE:C", "wrong"), "diversified": True}])))
        self.assertIn("diversified only on a right or edge label", probs)

    def test_by_accepts_ai_adjudicated_but_not_free_text(self):
        self.assertEqual(evalset.check(idea(labels=[{**label("NYSE:A", "edge"), "by": "ai-adjudicated"}])), [])
        probs = " | ".join(evalset.check(idea(labels=[{**label("NYSE:A", "edge"), "by": "robot"}])))
        self.assertIn("by must be ai, ai-adjudicated, human", probs)

    def test_owner_review_marker_needs_reviewed_true(self):
        ok = {**label("NYSE:A", "right"), "reviewed": True, "reviewed_by": "owner"}
        self.assertEqual(evalset.check(idea(labels=[ok])), [])
        probs = " | ".join(evalset.check(idea(labels=[{**ok, "reviewed": False}])))
        self.assertIn("reviewed_by needs reviewed: true", probs)
        probs = " | ".join(evalset.check(idea(labels=[{**ok, "reviewed_by": "a friend"}])))
        self.assertIn("reviewed_by must be owner", probs)

    def test_evidence_location_is_short_text(self):
        self.assertEqual(evalset.check(idea(labels=[{**label("NYSE:A", "right"),
                                                     "evidence_at": "2025 10-K Item 1, p. 7"}])), [])
        probs = " | ".join(evalset.check(idea(labels=[{**label("NYSE:A", "right"), "evidence_at": "x" * 201}])))
        self.assertIn("evidence_at", probs)
        probs = " | ".join(evalset.check(idea(labels=[{**label("NYSE:A", "right"), "evidence_at": 7}])))
        self.assertIn("evidence_at", probs)

    def test_unresolved_only_marks_a_search_summary_label(self):
        ok = {**label("NYSE:B", "edge"), "checked": "search_summary", "unresolved": True}
        self.assertEqual(evalset.check(idea(labels=[ok])), [])
        probs = " | ".join(evalset.check(idea(labels=[{**ok, "checked": "filing_read"}])))
        self.assertIn("unresolved only on a search_summary label", probs)
        probs = " | ".join(evalset.check(idea(labels=[{**ok, "unresolved": "maybe"}])))
        self.assertIn("unresolved must be a boolean", probs)

    def test_fewer_rows_than_k_score_over_the_rows_shown_and_say_n(self):
        s = evalset.score(idea(), result(["NYSE:A", "NYSE:B", "NYSE:C"]))
        a = s["at"]["10"]
        self.assertEqual((a["n"], a["right"], a["edge"], a["wrong"]), (3, 1, 1, 1))
        self.assertAlmostEqual(a["precision"], 1 / 3, places=4)            # strict: right only
        self.assertAlmostEqual(a["precision_lenient"], 2 / 3, places=4)    # lenient: right + edge
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("strict P@10 33%", md)
        self.assertIn("lenient 67%", md)
        self.assertIn("P@min(10, n)", md)
        self.assertIn("| 3 | 33% (1/3; 1/1/1/0) | 67% |", md)    # n, strict (r / labelled; r/e/w/?), lenient

    def test_diversified_rights_and_owner_reviews_are_counted_and_shown(self):
        labs = [{**label("NYSE:A", "right", True), "diversified": True, "reviewed": True, "reviewed_by": "owner"},
                {**label("NYSE:B", "edge"), "diversified": True},
                {**label("NYSE:D", "right", True)},
                {**label("NYSE:E", "edge"), "checked": "search_summary", "unresolved": True}]
        s = evalset.score(idea(labels=labs), result(["NYSE:A", "NYSE:B", "NYSE:D"]))
        self.assertEqual(s["at"]["10"]["diversified_right"], 1)
        self.assertEqual((s["owner_reviewed_labels"], s["unresolved_labels"], s["diversified_labels"]), (1, 1, 2))
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("1 reviewed by the owner", md)
        self.assertIn("1 unresolved", md)
        self.assertIn("diversified", md)

    def test_by_type_table_reports_strict_and_lenient(self):
        s = evalset.score(idea(), result(["NYSE:A", "NYSE:B"]))
        md = evalset.report_md([s], evalset.aggregate([s]))
        self.assertIn("| type | ideas | strict P@10 | lenient P@10 | strict P@40 | lenient P@40 | must-include recall |",
                      md)
        self.assertIn("| product_category | 1 | 50% | 100% | 50% | 100% | 50% |", md)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.set = self.home / "ideas"
        self.set.mkdir()
        (self.set / "demo-idea.json").write_text(json.dumps(idea()), encoding="utf-8")
        self.cfg = config.Config(home=self.home / "home")

    def call(self, fn, **kw):
        ns = argparse.Namespace(set=self.set, json=True, **kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fn(ns, self.cfg) if fn is not eval_cli._run else fn(ns, self.cfg, **self.extra)
        return code, json.loads(buf.getvalue())

    def test_check(self):
        code, out = self.call(eval_cli.cmd_eval, eval_command="check")
        self.assertEqual((code, out["status"], out["files"]), (0, "ok", 1))
        (self.set / "bad.json").write_text(json.dumps(idea(id="Bad Id")), encoding="utf-8")
        code, out = self.call(eval_cli.cmd_eval, eval_command="check")
        self.assertEqual(code, 1)
        self.assertIn("bad.json", out["problems"])

    def run_eval(self, screen_fn, **kw):
        self.extra = {"screen_fn": screen_fn}
        args = {"eval_command": "run", "budget_each": 0.5, "budget_total": 1.0, "ideas": None, "reads": None,
                "read_offset": 0, **kw}
        return self.call(eval_cli._run, **args)

    def add_idea(self, iid):
        (self.set / f"{iid}.json").write_text(json.dumps(idea(id=iid)), encoding="utf-8")

    def test_run_screens_every_idea_within_its_budget_and_scores_it(self):
        calls = []

        def fake_screen(cfg, text, **kw):
            calls.append((text, kw))
            return result(["NYSE:A", "NYSE:C"], run_id="scr-fake")
        code, out = self.run_eval(fake_screen)
        self.assertEqual((code, out["status"]), (0, "ok"))
        [(text, kw)] = calls
        self.assertEqual((text, kw["budget_usd"], kw["sieve"], kw["idea_en"], kw["min_mcap_usd"], kw["translate"]),
                         ("demo idea", 0.5, "none", "Demo idea", 1e9, False))
        self.assertAlmostEqual(out["aggregate"]["all"]["p@10"], 0.5)
        self.assertTrue((Path(out["out_dir"]) / "report.md").exists())
        self.assertTrue((Path(out["out_dir"]) / "scores.json").exists())

    def test_run_progress_marks_low_coverage_precision_as_provisional(self):
        args = argparse.Namespace(set=self.set, json=False, budget_each=0.5, budget_total=0.5,
                                  ideas="demo-idea", reads=None, read_offset=0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = eval_cli._run(args, self.cfg, screen_fn=lambda cfg, text, **kw: result(["NYSE:A", "NYSE:X"]))
        self.assertEqual(code, 0)
        self.assertIn("provisional main list strict P@min(10, n_main)", out.getvalue())
        self.assertIn("full list strict P@10", out.getvalue())
        self.assertIn("coverage 1/2", out.getvalue())
        self.assertIn("lenient", out.getvalue())
        reviewed = {**label("NYSE:A", "right"), "reviewed": True, "checked": "filing_read"}
        (self.set / "demo-idea.json").write_text(json.dumps(idea(labels=[reviewed])), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(eval_cli._run(args, self.cfg, screen_fn=lambda cfg, text, **kw: result(["NYSE:A"])), 0)
        self.assertIn("full list strict P@10 1.0, lenient 1.0 (n 1, coverage 1/1", out.getvalue())
        self.assertNotIn("provisional", out.getvalue())
        unresolved = {**label("NYSE:B", "edge"), "reviewed": True, "checked": "search_summary", "unresolved": True}
        (self.set / "demo-idea.json").write_text(json.dumps(idea(labels=[reviewed, unresolved])), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(eval_cli._run(args, self.cfg, screen_fn=lambda cfg, text, **kw: result(["NYSE:A"])), 0)
        self.assertIn("provisional main list", out.getvalue())

    def test_run_needs_a_positive_finite_budget_and_a_valid_offset(self):
        from unittest import mock
        for kw in ({"budget_each": 0.0}, {"budget_each": float("nan")}, {"budget_each": float("inf")},
                   {"budget_total": float("nan")}, {"budget_each": 0.6, "budget_total": 0.5}, {"read_offset": -1},
                   {"reads": 0}):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                args = {"eval_command": "run", "budget_each": 0.5, "budget_total": 1.0, "ideas": None, "reads": None,
                        "read_offset": 0, **kw}
                # cmd_eval screens with the real screen(): refused before it, not by its own parameter check
                with mock.patch("jevscreen.screen.screen", side_effect=AssertionError("must not screen")):
                    code, out = self.call(eval_cli.cmd_eval, **args)
                self.assertEqual((code, out["command"], out["status"]), (1, "eval", "error"))
                self.assertFalse((self.home / "home" / "evals").exists())

    def test_the_parser_requires_the_total_budget(self):
        from jevscreen import cli
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.build_parser().parse_args(["eval", "run", "--budget-each", "0.5"])
        a = cli.build_parser().parse_args(["eval", "run", "--budget-each", "0.5", "--budget-total", "2"])
        self.assertEqual((a.budget_each, a.budget_total), (0.5, 2.0))

    def test_run_stops_before_an_idea_whose_budget_could_pass_the_total(self):
        for iid in ("idea-b", "idea-c"):
            self.add_idea(iid)
        calls = []

        def spends_all(cfg, text, **kw):
            calls.append(kw["out_dir"].name)
            return result(["NYSE:A"], cost_usd=kw["budget_usd"])
        code, out = self.run_eval(spends_all)
        self.assertEqual((code, out["status"], calls), (5, "budget_exhausted", ["demo-idea", "idea-b"]))
        self.assertEqual([(s["id"], s["status"]) for s in out["scores"]],
                         [("demo-idea", "ok"), ("idea-b", "ok"), ("idea-c", "not_run")])
        self.assertEqual(out["budget"], {"each": 0.5, "total": 1.0, "spent_usd": 1.0})
        self.assertEqual(out["aggregate"]["all"]["ideas"], 2)
        calls.clear()

        def spends_little(cfg, text, **kw):
            calls.append(kw["out_dir"].name)
            return result(["NYSE:A"], cost_usd=0.2)
        code, out = self.run_eval(spends_little)
        self.assertEqual((code, len(calls)), (0, 3))           # 0.2 + 0.2 + 0.5 <= 1.0: the third still fits

    def test_a_busy_jev_stops_the_run_and_is_not_scored(self):
        self.add_idea("idea-b")
        calls = []

        def busy(cfg, text, **kw):
            calls.append(kw["out_dir"].name)
            return result(["NYSE:A"], status="jev_busy", cost_usd=0.0, run_id="scr-busy")
        code, out = self.run_eval(busy)
        self.assertEqual((code, out["status"], calls), (4, "jev_busy", ["demo-idea"]))
        self.assertEqual([(s["id"], s["status"]) for s in out["scores"]],
                         [("demo-idea", "jev_busy"), ("idea-b", "not_run")])
        self.assertNotIn("at", out["scores"][0])
        self.assertEqual((out["aggregate"]["all"]["ideas"], out["aggregate"]["all"]["must_recall"]), (0, None))
        self.assertIn("jev_busy", (Path(out["out_dir"]) / "report.md").read_text(encoding="utf-8"))

    def test_a_screen_that_raises_is_recorded_and_the_others_are_still_scored(self):
        self.add_idea("aa-idea")

        def raises_first(cfg, text, **kw):
            if kw["out_dir"].name == "aa-idea":
                raise ValueError("idea_en 'X' names a company the idea does not; write it without them")
            return result(["NYSE:A"])
        code, out = self.run_eval(raises_first)
        self.assertEqual((code, out["status"]), (1, "error"))
        first, second = out["scores"]
        self.assertEqual((first["id"], first["status"]), ("aa-idea", "error"))
        self.assertIn("names a company", first["error"])
        self.assertEqual((second["status"], out["aggregate"]["all"]["ideas"]), ("ok", 1))
        saved = json.loads((Path(out["out_dir"]) / "scores.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["scores"]), 2)

    def test_ctrl_c_during_a_screen_keeps_the_report_and_counts_that_idea_as_paid(self):
        self.add_idea("idea-b")

        def interrupted(cfg, text, **kw):
            if kw["out_dir"].name == "idea-b":
                raise KeyboardInterrupt
            return result(["NYSE:A"], cost_usd=0.2)
        code, out = self.run_eval(interrupted)
        self.assertEqual((code, out["status"]), (130, "interrupted"))
        self.assertEqual([(s["id"], s["status"]) for s in out["scores"]],
                         [("demo-idea", "ok"), ("idea-b", "interrupted")])
        self.assertAlmostEqual(out["budget"]["spent_usd"], 0.7)      # its spend is unknown: its whole budget
        self.assertIn("idea-b: interrupted", (Path(out["out_dir"]) / "report.md").read_text(encoding="utf-8"))

    def test_an_escaped_jev_or_store_error_stops_with_its_exit_code(self):
        from jevscreen import store

        class JevUnavailable(RuntimeError):
            pass

        class JevBusy(JevUnavailable):
            pass
        self.add_idea("idea-b")
        for exc, code, status in ((JevBusy("another process is using Jev"), 4, "jev_busy"),
                                  (JevUnavailable("401"), 6, "ai_unavailable"),
                                  (store.StoreLocked("lock"), 3, "locked")):
            with self.subTest(status=status):
                def boom(cfg, text, exc=exc, **kw):
                    raise exc
                got, out = self.run_eval(boom)
                self.assertEqual((got, out["status"]), (code, status))
                self.assertEqual([s["status"] for s in out["scores"]][1], "not_run")
                self.assertTrue((Path(out["out_dir"]) / "scores.json").exists())

    def test_two_runs_in_the_same_second_get_two_folders_and_ids_are_screened_once(self):
        import datetime as dt
        from unittest import mock
        calls = []

        def fake(cfg, text, **kw):
            calls.append(kw["out_dir"])
            return result(["NYSE:A"])
        with mock.patch("jevscreen.store.now_utc", return_value=dt.datetime(2026, 9, 27, 12, 0, 0)):
            _, a = self.run_eval(fake, ideas="demo-idea,demo-idea")
            _, b = self.run_eval(fake)
        self.assertNotEqual(a["out_dir"], b["out_dir"])
        self.assertEqual((len(calls), a["aggregate"]["all"]["ideas"]), (2, 1))
        self.assertTrue((Path(a["out_dir"]) / "scores.json").exists())

    def test_check_reports_an_id_used_by_two_files(self):
        (self.set / "copy.json").write_text(json.dumps(idea()), encoding="utf-8")
        code, out = self.call(eval_cli.cmd_eval, eval_command="check")
        self.assertEqual(code, 1)
        self.assertIn("id demo-idea is also the id of copy.json", " ".join(out["problems"]["demo-idea.json"]))

    def test_labels_match_the_other_line_of_a_dual_listing_through_the_store(self):
        from jevscreen import store
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO securities (security_id, company_key) VALUES ('TSX:CCO', 'isin:CA1'), "
                        "('NYSE:CCJ', 'isin:CA1'), ('NYSE:A', 'isin:US1')")
        (self.set / "demo-idea.json").write_text(json.dumps(idea(labels=[label("TSX:CCO", "right", True),
                                                                          label("NYSE:A", "right", True)])),
                                                 encoding="utf-8")

        def fake(cfg, text, **kw):
            res = result([])
            res["rows"] = [{"rank": 1, "security_id": "NYSE:CCJ", "company_key": "isin:CA1"}]
            return res
        code, out = self.run_eval(fake)
        [s] = out["scores"]
        self.assertEqual((s["must_include"]["found"], s["must_include"]["missing"]), (1, ["NYSE:A"]))
        self.assertEqual(s["matched_by_company"], ["TSX:CCO"])
        run_dir = self.home / "run"                                  # eval score: the same match
        run_dir.mkdir()
        (run_dir / "results.json").write_text(json.dumps(fake(None, None)), encoding="utf-8")
        code, out = self.call(eval_cli.cmd_eval, eval_command="score", run=str(run_dir), idea="demo-idea")
        self.assertEqual((code, out["score"]["must_include"]["found"], out["score"]["not_in_store"]), (0, 1, []))

    def test_run_passes_the_ideas_markets_as_the_screen_filter(self):
        # 2026-09-28: runs before this fix passed countries only (null for most ideas: L1 read ~10k companies
        # worldwide; india-ems's L2 read mostly Chinese filings)
        (self.set / "demo-idea.json").write_text(json.dumps(idea(markets=["IN"], countries=None)), encoding="utf-8")
        (self.set / "b-idea.json").write_text(json.dumps(idea(id="b-idea", markets=["US", "CN"], countries=["CN"])),
                                              encoding="utf-8")
        (self.set / "c-idea.json").write_text(json.dumps(idea(id="c-idea", markets=None)), encoding="utf-8")
        calls = {}

        def fake(cfg, text, **kw):
            calls[kw["out_dir"].name] = kw["countries"]
            return result(["NYSE:A"], params={"countries": kw["countries"]})
        code, out = self.run_eval(fake, budget_total=2.0)
        self.assertEqual(code, 0)
        self.assertEqual(calls, {"demo-idea": ["IN"], "b-idea": ["CN"], "c-idea": None})
        by = {s["id"]: s for s in out["scores"]}
        self.assertEqual(by["demo-idea"]["screen_countries"], ["IN"])
        self.assertIsNone(by["c-idea"]["screen_countries"])
        md = (Path(out["out_dir"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("IN", md.split("## By idea\n", 1)[1])

    def test_right_labels_outside_the_screen_filter_are_reported(self):
        # a must-include the filter can never show must be visible, not silently scored as missing
        from jevscreen import store
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO securities (security_id, company_key, country, exchange) VALUES "
                        "('NSE:DIXON', 'isin:IN1', 'India', 'NSE'), ('SZSE:300454', 'isin:CN1', 'China', 'SZSE'), "
                        "('HKEX:6699', 'isin:CN2', 'China', 'HKEX'), ('NYSE:X', 'isin:US9', 'United States', 'NYSE')")
        (self.set / "demo-idea.json").write_text(json.dumps(idea(markets=["IN", "HK"], labels=[
            label("NSE:DIXON", "right", True), label("SZSE:300454", "right", True), label("HKEX:6699", "right"),
            label("NYSE:X", "wrong")])), encoding="utf-8")
        code, out = self.run_eval(lambda cfg, text, **kw: result(["NSE:DIXON"], params={"countries": kw["countries"]}))
        [s] = out["scores"]
        # matched as the screen matches --countries: a country code is the country of incorporation, so a
        # Hong Kong-listed company incorporated in China is outside "HK" (it needs CN); wrong labels do not matter
        self.assertEqual(s["outside_scope"], ["SZSE:300454", "HKEX:6699"])
        md = (Path(out["out_dir"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("SZSE:300454", md)
        self.assertIn("outside", md)

    def test_the_repository_set_is_well_formed(self):
        d = eval_cli.default_set_dir()
        if not d.exists():
            self.skipTest("no evals/ideas in this checkout")
        for f in sorted(d.glob("*.json")):
            with self.subTest(f=f.name):
                self.assertEqual(evalset.check(json.loads(f.read_text(encoding="utf-8"))), [])


if __name__ == "__main__":
    unittest.main()
