"""Retrieval levers (plan step 3), both default OFF:

- `--lang-terms`: excerpt terms for every document language even when idea_en is given (the local model is asked
  for terms only; its idea_en never replaces the caller's); generic fragments are weak terms.
- `--second-search`: L1-core rows whose annual-report excerpts L2 found insufficient get one more L2 read over other
  passages of the filing; explicit / partial there raises the row to partial at most (its own layer, `l2_second`).

Offline: test_screen's seeded store and FakeJev (no network, no paid call); the keyword model is a fake."""
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
from jevscreen import calib, eval_levers, evalset, page, retrieval, screen, shortlist, store  # noqa: E402

REDUCER_KW = {"idea_en": "SOMETHING ELSE the model wrote", "model": "fake-qwen", "cached": True,
              "keywords": {"en": ["speed reducer"], "zh": ["谐波减速器", "核心零部件"], "ja": ["波動歯車装置", "減速機"],
                           "ko": ["감속기", "제품"]}}

ROBO_LONG = """Item 1. Business

Overview

RoboCorp designs and manufactures industrial automation equipment for factories in North America, Europe and Asia.
Our products are sold to automotive, electronics and logistics customers through direct sales teams.

Products

Our automation cells combine robot arms, conveyors and vision systems; our humanoid robots for warehouse picking
are sold in small numbers and serviced under multi-year contracts at two customer sites in the United States.

Competition

We compete with large industrial conglomerates and with specialised automation vendors on price and reliability.

Research

Our laboratory builds humanoid robot prototypes and we began selling humanoid robots to two warehouse customers this
year; the humanoid line is small but growing and is reported inside our automation segment.

Employees

As of year end we had approximately 4,000 full-time employees located in eleven countries around the world.
"""


class Pure(unittest.TestCase):
    def test_generic_terms(self):
        for t, lang in (("核心零部件", "zh"), ("设备供应商", "zh"), ("製品", "ja"), ("제품", "ko"), ("AI", "zh"),
                        ("OEM", "en")):
            self.assertTrue(retrieval.is_generic(t, lang), t)
        for t, lang in (("人形机器人", "zh"), ("谐波减速器", "zh"), ("減速機", "ja"), ("감속기", "ko"), ("HBM", "zh"),
                        ("robot", "en")):
            self.assertFalse(retrieval.is_generic(t, lang), t)

    def test_a_leftover_character_is_a_content_word(self):
        # review finding: stripping generic words out of a compound left one Han character (光, 器, 铀, 云), which
        # carries the meaning; only a term made wholly of generic words (and particles) is generic
        for t in ("光模块", "服务器", "热管理系统", "铀", "铀生产", "云服务", "应用材料", "光伏组件"):
            self.assertFalse(retrieval.is_generic(t, "zh"), t)
        for t in ("核心零部件", "设备供应商", "上市公司", "相关产品", "核心供应商", "组件", "的产品"):
            self.assertTrue(retrieval.is_generic(t, "zh"), t)
        self.assertTrue(retrieval.is_generic("主要製品", "ja"))
        self.assertFalse(retrieval.is_generic("光製品", "ja"))

    def test_model_and_seed_terms_split_generic_ones_too(self):
        # review finding: status 'generated' (no idea_en: the normal `jevscreen screen` path) and seed terms kept
        # 核心零部件 / 製品 / 제품 as full terms; the user's own flags stay as given
        fake = TS.FakeKeywords(REDUCER_KW)
        info = screen.resolve_keywords(None, "人形机器人谐波减速器核心零部件供应商", keywords=None, keywords_by_lang=None,
                                       translate=True, keywords_fn=fake, idea_en=None, all_langs=True)
        self.assertEqual(info["status"], "generated")
        self.assertEqual(info["terms"]["zh"], ["谐波减速器"])
        self.assertEqual((info["weak"]["zh"], info["weak"]["ko"]), (["核心零部件"], ["제품"]))
        off = screen.resolve_keywords(None, "人形机器人谐波减速器核心零部件供应商", keywords=None, keywords_by_lang=None,
                                      translate=True, keywords_fn=fake, idea_en=None, all_langs=False)
        self.assertIn("核心零部件", off["terms"]["zh"])           # the lever off: unchanged
        seeds = {"seed_terms": {"zh": ["谐波减速器", "核心零部件"]}}
        s = screen.resolve_keywords(None, "人形机器人", keywords=None, keywords_by_lang={"ja": ["製品"]},
                                    translate=True, keywords_fn=fake, idea_en="Humanoid robots", author=seeds,
                                    all_langs=True)
        self.assertEqual((s["terms"]["zh"], s["weak"]["zh"]), (["谐波减速器"], ["核心零部件"]))
        self.assertEqual(s["terms"]["ja"], ["製品"])                # the user's flag is kept as given
        self.assertNotIn("ja", s["weak"])

    def test_latin_anchors(self):
        got = retrieval.latin_anchors("Advanced packaging equipment for HBM, SiC wafers, GLP-1, eVTOL, AI agents, OEM "
                                      "and Reducers (RV)")
        self.assertEqual(got, ["HBM", "SiC", "GLP-1", "eVTOL", "RV"])

    def test_every_language_fills_the_fallback_languages(self):
        calls = []

        def kw(cfg, idea):
            calls.append(idea)
            return json.loads(json.dumps(REDUCER_KW))
        info = {"status": "agent", "idea_en": "Reducers (harmonic, RV) for humanoid robot joints"}
        base = {"en": ["humanoid robot joints", "Reducers"], "zh": ["人形机器人关节减速器", "核心零部件"], "ja": [],
                "ko": []}
        terms, weak, summ = retrieval.every_language(None, "人形机器人核心零部件", info, base,
                                                     user={"en": [], "zh": [], "ja": [], "ko": []}, seeds={},
                                                     keywords_fn=kw)
        self.assertEqual(calls, ["人形机器人核心零部件"])
        self.assertEqual(terms["en"], base["en"])                 # en keeps the words of idea_en
        self.assertIn("谐波减速器", terms["zh"])
        self.assertIn("人形机器人关节减速器", terms["zh"])
        self.assertNotIn("核心零部件", terms["zh"])
        self.assertIn("核心零部件", weak["zh"])                    # a generic fragment is weak, not dropped
        self.assertEqual(terms["ja"], ["波動歯車装置", "減速機", "RV"])
        self.assertEqual(terms["ko"], ["감속기", "RV"])
        self.assertEqual(weak["ko"], ["제품"])
        self.assertEqual(info["idea_en"], "Reducers (harmonic, RV) for humanoid robot joints")   # never replaced
        self.assertEqual(summ["status"], "model")

    def test_user_flags_are_kept_and_a_model_failure_keeps_the_anchors(self):
        def broken(cfg, idea):
            raise TS.KeywordsUnavailable("no model")
        base = {"en": ["x"], "zh": ["人形机器人"], "ja": ["ロボット"], "ko": []}
        terms, weak, summ = retrieval.every_language(None, "HBM 设备", {"status": "agent", "idea_en": "HBM tools"},
                                                     base, user={"ja": ["ロボット"]}, seeds={}, keywords_fn=broken)
        self.assertEqual(terms["ja"], ["ロボット"])
        self.assertEqual(terms["ko"], ["HBM"])
        self.assertEqual(summ["status"], "unavailable")
        self.assertIn("no model", summ["error"])

    def test_ja_borrows_han_terms_as_weak_without_model_terms(self):
        out = {"keywords": {"zh": ["减速器"], "en": [], "ja": [], "ko": []}}
        terms, weak, _ = retrieval.every_language(None, "x", {"status": "agent", "idea_en": "x"},
                                                  {"en": ["x"], "zh": [], "ja": [], "ko": []}, user={}, seeds={},
                                                  keywords_fn=lambda cfg, idea: out)
        self.assertEqual(terms["ja"], [])
        self.assertEqual(weak["ja"], ["减速器"])

    def test_paragraphs_skip_what_was_shown(self):
        text = "\n\n".join([
            "Our humanoid robots are sold to warehouses; this paragraph was the first keyword excerpt, it is long.",
            "Unrelated paragraph about employees and offices and the board, long enough to be a paragraph here.",
            "A second passage: the humanoid robot line has two customers and a pilot plant, long enough to count."])
        shown = [{"kind": "keywords", "text": text.split("\n\n")[0]}]
        got = retrieval.paragraphs(text, terms=["humanoid robot"], weak=[], lang="en", shown=shown)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("A second passage"))
        self.assertEqual(retrieval.paragraphs(text, terms=["gearbox"], weak=[], lang="en", shown=shown), [])
        # weak terms alone need four distinct hits (WEAK_TERM_WEIGHT 0.25)
        self.assertEqual(retrieval.paragraphs(text, terms=[], weak=["humanoid"], lang="en", shown=shown), [])

    def test_decide(self):
        d = retrieval.decide
        self.assertEqual(d("insufficient", {"label": "explicit"}), ("partial", "raised"))
        self.assertEqual(d("insufficient", {"label": "partial"}), ("partial", "raised"))
        self.assertEqual(d("insufficient", {"label": "insufficient"}), ("insufficient", "kept"))
        self.assertEqual(d("insufficient", None), ("insufficient", None))
        self.assertEqual(d("partial", {"label": "explicit"}), ("partial", None))

    def test_pieces_are_weak_with_the_lever(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "zh.txt"
            p.write_text("公司主要从事精密传动装置的研发、设计、生产和销售，产品广泛应用于各类高端制造领域，客户覆盖国内外主要厂商。\n\n"
                         "公司的产品也可以用于机器人，本段只提到机器人这一个词，没有别的相关内容，写得足够长以便成为一个段落。\n\n"
                         "报告期内公司继续加强内部管理与人才队伍建设，持续完善治理结构，提升经营效率和风险控制水平。", encoding="utf-8")
            c = {"company_key": "k", "security_id": "SSE:1", "desc": {"plain": "", "text": "", "tier": None}}
            doc = {"text_path": str(p), "source_id": "cninfo_annual_report", "form": "annual_report"}
            terms = {"zh": ["人形机器人关节减速器", "谐波减速器"]}
            off = screen._l2_input(c, doc, terms)
            none_named = screen._l2_input(c, doc, terms, pieces_weak=True)
            alone = screen._l2_input(c, doc, {"zh": ["人形机器人关节减速器"]}, pieces_weak=True)
            p.write_text(p.read_text(encoding="utf-8") + "\n\n公司另有一条谐波减速器产线，本段写的是谐波减速器的产能和客户，"
                         "足够长以便成为一个独立的段落，与前面的段落分开。", encoding="utf-8")
            named = screen._l2_input(c, doc, terms, pieces_weak=True)
        self.assertTrue(off["keyword_hit"])            # two pieces (机器 器人) count as full terms today
        # review minor: a filing that names none of the full terms keeps its pieces full (no keyword excerpt lost)
        self.assertEqual((none_named["keyword_hit"], none_named["evidence_sha"]), (True, off["evidence_sha"]))
        self.assertTrue(alone["keyword_hit"])          # a compound with no other term keeps its pieces full
        kw = next(e for e in named["excerpts"] if e["kind"] == "keywords")
        self.assertIn("谐波减速器", kw["text"])          # a filing naming a full term: the pieces only break ties


class SecondJev(TS.FakeJev):
    """FakeJev whose L2 finds every first excerpt insufficient, and a second-search text (its tag) explicit when it
    mentions humanoid robots."""

    def classify(self, items, question):
        if question.key != screen.L2_KEY:
            return super().classify(items, question)
        out = super().classify(items, question)
        for it, r in zip(items, out):
            if r.get("status") != "ok":
                continue
            second = "second search" in it.text
            if second and "humanoid robot" in it.text.lower():
                r.update(label="explicit", probs={"explicit": 0.7, "partial": 0.2, "insufficient": 0.1})
            elif not second and "NYSE:ROBO" == (it.meta or {}).get("security_id"):
                r.update(label="insufficient", probs={"explicit": 0.05, "partial": 0.1, "insufficient": 0.85})
        return out


class ScreenCase(TS.StoreCase):
    def setUp(self):
        super().setUp()
        (self.home / "docs" / "robo.txt").write_text(ROBO_LONG, encoding="utf-8")
        self.kw = TS.FakeKeywords(REDUCER_KW)

    def factory(self):
        log = self.log

        def f(cfg, *, run_id, layer, budget_usd, dry_run):
            return SecondJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
        return f

    def screen_(self, idea_en="Humanoid robots", **kw):
        kw.setdefault("jev_factory", self.factory())
        kw.setdefault("min_mcap_usd", 0)
        kw.setdefault("sieve", "none")
        kw.setdefault("l1_rescue", 0)
        return self.run_screen(idea_en=idea_en, **kw)

    def second_calls(self):
        return [items for c in self.log if c.layer == retrieval.KEY for items, _q in c.classified]


class SecondSearchScreen(ScreenCase):
    def test_off_by_default(self):
        res = self.screen_()
        self.assertFalse(res["params"]["second_search"])
        self.assertFalse(res["params"]["lang_terms"])
        self.assertNotIn("second_search", res)
        self.assertNotIn("retrieval_terms_sha", res["params"])
        self.assertNotIn("weak", res["keywords"])
        self.assertEqual(self.second_calls(), [])
        self.assertEqual(self.kw.calls, [])            # idea_en given: the local model is not asked
        self.assertIn("NYSE:ROBO", [r["security_id"] for r in res["unverified"]])

    def test_raises_an_insufficient_core_row_to_partial(self):
        off = self.screen_(out_dir=self.home / "off")
        res = self.screen_(second_search=True)
        self.assertEqual(res["questions"], off["questions"])          # L1 / L2 questions unchanged
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual((robo["l2_label"], robo["l2_status"], robo["l2_second_search"],
                          robo["l2_label_before_second"]), ("partial", "partial", "raised", "insufficient"))
        self.assertIn("humanoid", robo["evidence_excerpt"].lower())   # the passage it was raised on
        items = [it for batch in self.second_calls() for it in batch]
        self.assertEqual([it.meta["security_id"] for it in items], ["NYSE:ROBO"])
        self.assertIn("second search", items[0].text)
        self.assertNotIn("As of year end", items[0].text)
        info = res["second_search"]
        self.assertEqual((info["candidates"], info["items"], info["raised"]), (1, 1, 1))
        self.assertAlmostEqual(res["cost_usd"], res["layers"]["l1"]["cost_usd"] + res["layers"]["l2"]["cost_usd"]
                               + res["layers"][retrieval.KEY]["cost_usd"], places=6)
        lines = [json.loads(x) for x in (self.home / "out" / "l2_inputs.jsonl").read_text().splitlines()]
        robo_line = next(x for x in lines if x["security_id"] == "NYSE:ROBO")
        # review finding: a raised row's evidence is the second passage everywhere (cards, the user's AI, why),
        # not the first text L2 found insufficient; the first text stays beside it
        self.assertIn("second search", robo_line["text"])
        self.assertEqual(robo_line["evidence_sha"], robo["evidence_sha"])
        self.assertEqual(robo_line["l2_second_search"], "raised")
        self.assertNotIn("second search", robo_line["first_search"]["text"])
        self.assertNotEqual(robo_line["first_search"]["evidence_sha"], robo["evidence_sha"])
        ins = calib.load_inputs(self.home / "out")
        quote = calib.card_quote(ins[robo["company_key"]], robo)
        self.assertIn("humanoid", quote["text"].lower())
        got = self.query("SELECT security_id, label FROM screen_results WHERE run_id = ? AND layer = ?",
                         [res["run_id"], retrieval.KEY])
        self.assertEqual(got, [("NYSE:ROBO", "explicit")])            # stored as read; applied as partial

    def test_never_high_confidence(self):
        res = self.screen_(second_search=True, shortlist=True)
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual(robo["shortlist_tier"], "confirm")

    def test_pool_and_rank_only_apply_the_layer(self):
        first = self.screen_(second_search=True, out_dir=self.home / "a")
        with store.session(self.cfg, read_only=True) as con:
            pool = {p["security_id"]: p for p in calib.load_pool(con, first["run_id"], first["params"])}
            raw = {p["security_id"]: p for p in calib.load_pool(con, first["run_id"], {})}
        self.assertEqual((pool["NYSE:ROBO"]["l2_label"], pool["NYSE:ROBO"]["l2_second_search"]),
                         ("partial", "raised"))
        self.assertEqual(raw["NYSE:ROBO"]["l2_label"], "insufficient")
        # review minor: the screen's own row carries the probabilities a reload gives (never explicit)
        robo = next(r for r in first["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual((robo["l2_p_explicit"], robo["l2_p_partial"]),
                         (pool["NYSE:ROBO"]["l2_p_explicit"], round(pool["NYSE:ROBO"]["l2_p_partial"], 4)))
        self.assertEqual(robo["l2_p_explicit"], 0.0)
        out = self.home / "a"
        header = (out / "results.csv").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("l2_second_search", header)
        self.assertIn("l2_label_before_second", header)
        md = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("推断：二次检索", md)
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=first["run_id"], sieve="none",
                                  out_dir=self.home / "ro")
        self.assertEqual(ro["cost_usd"], 0.0)

        def shape(r):
            return [(x["security_id"], x["rank"], x["l2_label"], x.get("l2_second_search")) for x in r["rows"]]
        self.assertEqual(shape(ro), shape(first))

    def test_budget_skip_keeps_rows_unverified(self):
        full = self.screen_(second_search=True, out_dir=self.home / "a")
        need = full["layers"]["l1"]["cost_usd"] + full["layers"]["l2"]["cost_usd"]
        self.log.clear()
        res = self.screen_(second_search=True, budget_usd=need + 0.001, out_dir=self.home / "b")
        self.assertEqual(res["layers"][retrieval.KEY]["skipped"], "budget")
        self.assertEqual(res["status"], "partial")         # review minor: a lever that never ran is not an ok run
        self.assertIn("NYSE:ROBO", [r["security_id"] for r in res["unverified"]])
        self.assertTrue(any("二次检索" in n and "second search skipped" in n for n in res["notes"]))

    def test_from_run_inherits_and_no_flag_turns_it_off(self):
        first = self.screen_(second_search=True, out_dir=self.home / "a")
        again = self.screen_(from_run=first["run_id"], out_dir=self.home / "b")
        self.assertTrue(again["params"]["second_search"])
        self.assertEqual(again["layers"]["l1"]["cost_usd"], 0.0)       # L1 never read again
        off = self.screen_(from_run=first["run_id"], second_search=False, out_dir=self.home / "c")
        self.assertFalse(off["params"]["second_search"])
        self.assertNotIn("second_search", off)

    def test_dry_run_estimates(self):
        res = self.screen_(second_search=True, dry_run=True)
        self.assertIn("estimate", res["layers"][retrieval.KEY])
        self.assertEqual(self.second_calls(), [])


class LangTermsScreen(ScreenCase):
    def test_every_language_gets_terms_and_the_questions_stay(self):
        off = self.screen_(out_dir=self.home / "off")
        res = self.screen_(lang_terms=True)
        self.assertEqual(self.kw.calls, ["humanoid robots"])          # the model is asked for terms only
        self.assertEqual(res["questions"], off["questions"])
        self.assertEqual(res["idea_en"], "Humanoid robots")
        by = res["terms_by_lang"]
        self.assertEqual(by["en"], off["terms_by_lang"]["en"])
        self.assertIn("減速機", by["ja"])
        self.assertIn("감속기", by["ko"])
        self.assertNotIn("核心零部件", by["zh"])
        self.assertIn("核心零部件", res["keywords"]["weak"]["zh"])
        self.assertTrue(res["params"]["lang_terms"])
        self.assertTrue(res["params"]["retrieval_terms_sha"])

    def test_a_missing_model_is_said_and_the_run_is_partial(self):
        # review finding: the fallback (Latin anchors only) was silent and the run stayed ok
        self.kw = TS.FakeKeywords(error=TS.KeywordsUnavailable("No module named 'torch'"))
        ok = self.screen_(out_dir=self.home / "off")
        self.assertEqual(ok["status"], "ok")
        for kw in ({"lang_terms": True}, {"second_search": True}):
            res = self.screen_(out_dir=self.home / next(iter(kw)), **kw)
            self.assertEqual(res["keywords"]["lang_terms"]["status"], "unavailable")
            self.assertEqual(res["status"], "partial", kw)
            w = next(x for x in res["warnings"] if "keyword model" in x)
            zh, en = w.split(" / ", 1)
            self.assertNotRegex(zh.replace("jevscreen keywords", ""), r"[A-Za-z]")
            self.assertNotRegex(en, r"[一-鿿]")
            self.assertIn("torch", en)
        dry = self.screen_(out_dir=self.home / "dry", lang_terms=True, dry_run=True)
        self.assertTrue(any("keyword model" in x for x in dry["warnings"]))

    def test_eval_levers(self):
        from jevscreen import cli
        p = cli.build_parser()
        for flag, dest in (("lang-terms", "lang_terms"), ("second-search", "second_search")):
            self.assertIsNone(getattr(p.parse_args(["screen", "idea"]), dest))
            self.assertIs(getattr(p.parse_args(["screen", "idea", f"--{flag}"]), dest), True)
            self.assertIs(getattr(p.parse_args(["screen", "idea", f"--no-{flag}"]), dest), False)
        a = p.parse_args(["eval", "run", "--budget-each", "0.1", "--budget-total", "0.1", "--lang-terms",
                          "--second-search", "--from-runs", "xx=scr-1"])
        self.assertTrue(a.lang_terms and a.second_search)
        self.assertEqual(eval_levers.parse_from_runs(a.from_run), {"xx": "scr-1"})


class EvalFromRuns(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        d = self.tmp / "set"
        d.mkdir()
        for iid in ("xx", "yy"):
            (d / f"{iid}.json").write_text(json.dumps({
                "format": evalset.FORMAT, "id": iid, "idea": f"Humanoid robots {iid}", "type": "other",
                "labels": [{"security_id": "NYSE:A", "name": "A", "label": "right", "source_url":
                            "https://www.sec.gov/a.htm", "note": "n", "by": "ai", "reviewed": False,
                            "checked": "filing_read"}]}), encoding="utf-8")
        self.set = d

    def args(self, **kw):
        base = dict(set=self.set, json=True, budget_each=0.1, budget_total=0.2, ideas=None, reads=None,
                    read_offset=0, l2_constraints=False, shortlist=False, lang_terms=True, second_search=True,
                    from_runs=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def run_(self, args, fake, keywords=None, runs=None):
        from jevscreen import eval_cli
        cfg = mock.Mock(home=self.tmp)
        kw = keywords or (lambda cfg, idea: REDUCER_KW)
        stored = runs if runs is not None else {r: f"Humanoid robots {i}" for r, i in (
            ("scr-x", "xx"), ("scr-y", "yy"), ("scr-y2", "yy"))}
        from jevscreen import evalfrom
        with mock.patch.object(eval_cli, "_store_keys", return_value=(None, None)), \
                mock.patch.object(eval_cli, "_emit"), mock.patch.object(eval_levers, "_keywords", kw), \
                mock.patch.object(evalfrom, "_load_bases", lambda cfg, ids: {
                    r: {"idea": stored[r], "params": {}} if r in stored else "from_run: unknown screen run"
                    for r in ids}):
            return eval_cli._run(args, cfg, screen_fn=fake)

    def last_scores(self):
        got = sorted((self.tmp / "evals").glob("*/scores.json"))
        return json.loads(got[-1].read_text(encoding="utf-8"))["scores"]

    def test_keyword_model_missing_stops_before_anything_is_sent(self):
        # review finding: without torch the lever silently fell back to Latin anchors and the A/B paid for that
        seen = []

        def broken(cfg, idea):
            raise TS.KeywordsUnavailable("No module named 'torch'")
        with self.assertRaises(evalset.EvalError) as cm:
            self.run_(self.args(), lambda cfg, idea, **kw: seen.append(kw) or {}, keywords=broken)
        self.assertIn("xx", str(cm.exception))
        self.assertIn("torch", str(cm.exception))
        self.assertEqual(seen, [])
        with self.assertRaises(evalset.EvalError):     # --second-search alone needs the terms too
            self.run_(self.args(lang_terms=False), lambda cfg, idea, **kw: seen.append(kw) or {}, keywords=broken)
        self.assertEqual(seen, [])

    def test_control_arm_never_inherits_a_lever(self):
        seen = []

        def fake(cfg, idea, **kw):
            seen.append(kw)
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.0,
                    "layers": {"l1": {"from_run": kw.get("from_run"), "cost_usd": 0.0}}}
        self.run_(self.args(lang_terms=False, second_search=False, from_runs="xx=scr-x,yy=scr-y"), fake,
                  keywords=lambda cfg, idea: 1 / 0)             # no lever: the model is not asked
        self.assertEqual([(k["l2_constraints"], k["shortlist"], k["lang_terms"], k["second_search"]) for k in seen],
                         [(False, False, False, False)] * 2)
        seen.clear()
        self.run_(self.args(), fake)                             # no base run: nothing to inherit, as before
        self.assertNotIn("l2_constraints", seen[0])

    def test_unknown_base_run_stops_before_anything_is_sent(self):
        seen = []
        with self.assertRaises(evalset.EvalError) as cm:
            self.run_(self.args(from_runs="xx=scr-x,yy=scr-x"), lambda cfg, idea, **kw: seen.append(kw) or {})
        self.assertIn("yy=scr-x", str(cm.exception))            # a run of another idea
        with self.assertRaises(evalset.EvalError):
            self.run_(self.args(from_runs="xx=scr-x,yy=nope"), lambda cfg, idea, **kw: seen.append(kw) or {})
        self.assertEqual(seen, [])

    def test_passes_levers_and_bases(self):
        prior = self.tmp / "prior"
        prior.mkdir()
        (prior / "scores.json").write_text(json.dumps({"scores": [
            {"id": "xx", "run_id": "scr-x", "status": "ok"}, {"id": "yy", "run_id": "scr-y", "status": "partial"},
            {"id": "zz", "run_id": None, "status": "not_run"}]}), encoding="utf-8")
        seen = []

        def fake(cfg, idea, **kw):
            seen.append(kw)
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.0,
                    "layers": {"l1": {"from_run": kw.get("from_run"), "cost_usd": 0.0}}}
        self.run_(self.args(from_runs=f"{prior},yy=scr-y2"), fake)
        self.assertEqual([(k["from_run"], k["lang_terms"], k["second_search"]) for k in seen],
                         [("scr-x", True, True), ("scr-y2", True, True)])

    def test_missing_base_stops_before_anything_is_sent(self):
        seen = []

        def fake(cfg, idea, **kw):
            seen.append(kw)
            return {}
        with self.assertRaises(evalset.EvalError) as cm:
            self.run_(self.args(from_runs="xx=scr-x"), fake)
        self.assertIn("yy", str(cm.exception))
        self.assertEqual(seen, [])

    def test_an_l1_reread_stops_the_run(self):
        seen = []

        def fake(cfg, idea, **kw):
            seen.append(kw)
            return {"run_id": "r", "status": "ok", "rows": [], "cost_usd": 0.01,
                    "layers": {"l1": {"from_run": kw.get("from_run"), "cost_usd": 0.01}}}
        with self.assertRaises(evalset.EvalError):
            self.run_(self.args(from_runs="xx=scr-x,yy=scr-y"), fake)
        self.assertEqual(len(seen), 1)
        got = {s["id"]: s for s in self.last_scores()}
        self.assertEqual((got["xx"]["status"], got["xx"]["run_id"], got["xx"]["cost_usd"]), ("stopped", "r", 0.01))
        self.assertEqual(got["yy"]["status"], "not_run")

    def test_levers_named_in_scores(self):
        s = evalset.score({"id": "xx", "type": "other", "labels": []},
                          {"rows": [], "params": {"lang_terms": True, "second_search": True}})
        self.assertEqual(s["levers"], ["lang-terms", "second-search"])


class Page(unittest.TestCase):
    def test_badge_is_an_inference_in_each_language_and_never_explicit(self):
        self.assertEqual(page._badges({"l2_second_search": "raised", "l2_edge": True}, None)[0], "second_search")
        self.assertNotIn("second_search", page._badges({"l2_second_search": "kept"}, None))
        zh, en = page.STRINGS["zh"]["badge_second_search"], page.STRINGS["en"]["badge_second_search"]
        self.assertRegex(zh, r"^推断")
        self.assertNotRegex(zh, r"[A-Za-z]")
        self.assertTrue(en.startswith("Inference"))
        self.assertNotRegex(en, r"[一-鿿]")
        rows = [{"rank": 1, "security_id": "NYSE:ROBO", "name": "RoboCorp", "l1_label": "core", "l2_label": "partial",
                 "l2_evidence": "annual_report", "l2_second_search": "raised",
                 "l2_label_before_second": "insufficient", "evidence_excerpt": "humanoid robots for warehouses"}]
        res = {"run_id": "scr-x", "idea": "humanoid robots", "idea_en": "Humanoid robots", "status": "ok",
               "rows": rows, "params": {"min_mcap_usd": 1e9}, "output_dir": None}
        d = page.build_page_data(res, None, lang="en", l2_pieces={})
        self.assertIn("second_search", d["rows"][0]["badges"])
        self.assertNotEqual(d["rows"][0]["verdict"], "explicit")
        self.assertEqual(shortlist.tier_of(rows[0]), "confirm")


if __name__ == "__main__":
    unittest.main()
