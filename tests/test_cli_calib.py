"""Tests for the calibration CLI (screen --reads / --from-run / --sieve / --cards / --rank, cards, answer, sieve
check / show) and the report's 校准 section.

No network and no paid calls: every Jev client is a fake patched in as screen._default_factory; the store is a temp
DuckDB seeded by test_screen.seed.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, cli, report, screen  # noqa: E402
from test_screen import (FakeJev, JevUnavailable, StoreCase, band_factory, humanoid_sieve,  # noqa: E402
                         make_factory, seed_official)

IDEA = "humanoid robots"
ROBO, ROB2 = "isin:US0000000001", "isin:US0000000007"
RULE_MARK = "A company that only builds"          # calib.RULES user_not_supplier (generic text)


class RuleJev(FakeJev):
    """FakeJev whose L2 answer for RoboCorp turns 'insufficient' when the question carries the user_not_supplier
    rule (the trial and the apply screen see the same effect)."""

    def classify(self, items, question):
        out = super().classify(items, question)
        if question.key != "fit" and RULE_MARK in question.criteria.get("insufficient", ""):
            for x in out:
                if x["item_id"] == ROBO and x["status"] == "ok":
                    x.update(label="insufficient", probs={"explicit": 0.1, "partial": 0.1, "contradicted": 0.1,
                                                          "insufficient": 0.7})
        return out


def rule_factory(log):
    def factory(cfg, *, run_id, layer, budget_usd, dry_run, **kw):
        return RuleJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
    return factory


class CliCase(StoreCase):
    def main(self, argv, factory=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), \
                mock.patch.object(screen, "_default_factory", factory or make_factory(self.log)), \
                mock.patch.object(screen, "_default_keywords", self.kw), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write_sieve(self, **kw) -> Path:
        path = calib.sieve_path(self.cfg, IDEA)
        calib.save_sieve(path, {**calib.new_sieve(IDEA), **kw})
        return path

    def last_run(self) -> tuple[str, dict, Path]:
        rid, pj, od = self.query("SELECT run_id, params_json, output_dir FROM screen_runs ORDER BY started_at DESC "
                                 "LIMIT 1")[0]
        return rid, json.loads(pj), Path(od)

    def screen_with_deck(self, *extra, factory=None) -> tuple[str, Path]:
        """A screen whose deck is one scope card for RoboCorp (target term 'android' is not in its filing)."""
        self.write_sieve(target_terms={"en": ["android"]})
        code, out, err = self.main(["screen", IDEA, "--reads", "1", *extra], factory)
        self.assertEqual(code, 0, err)
        rid, _p, od = self.last_run()
        self.assertTrue((od / "cards.json").exists())
        return rid, od


class TestScreenFlags(CliCase):
    def test_parse_defaults_and_given(self):
        p = cli.build_parser()
        d = p.parse_args(["screen", "x"])
        self.assertEqual((d.reads, d.read_offset, d.cards, d.sieve, d.from_run, d.rank, d.given),
                         (None, 0, 8, "auto", None, None, frozenset()))
        a = p.parse_args(["screen", "x", "--reads", "1", "--read-offset", "3", "--cards", "0", "--sieve", "none",
                          "--from-run", "scr-1", "--rank", "ev", "--max-out", "40", "--no-translate"])
        self.assertEqual((a.reads, a.read_offset, a.cards, a.sieve, a.from_run, a.rank, a.no_translate),
                         (1, 3, 0, "none", "scr-1", "ev", True))
        self.assertEqual(a.given, {"reads", "rank", "max_out", "no_translate"})    # a default value given counts
        for bad in (["--rank", "best"], ["--reads", "0"], ["--read-offset", "-1"], ["--cards", "-1"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=bad):
                p.parse_args(["screen", "x", *bad])
        # the parser defaults are screen's defaults (options not given are passed as UNSET)
        self.assertEqual({"min_mcap_usd": d.min_mcap, "min_avg_volume": d.min_volume, "countries": d.countries,
                          "max_out": d.max_out, "budget_usd": d.budget, "l2_max": d.l2_max, "keywords": d.keywords,
                          "translate": not d.no_translate},
                         {k: screen.SCREEN_DEFAULTS[k] for k in ("min_mcap_usd", "min_avg_volume", "countries",
                                                                 "max_out", "budget_usd", "l2_max", "keywords",
                                                                 "translate")})
        self.assertEqual((cli.SCREEN_READS_DEFAULT, cli.RANK_CHOICES, cli.CARDS_DEFAULT),
                         (screen.L2_READS_DEFAULT, screen.RANKS, calib.CARDS_MAX))
        c = p.parse_args(["cards"])
        self.assertEqual((c.target, c.max, c.out, c.json), ("latest", 8, None, False))
        an = p.parse_args(["answer", "1要a 2不要c"])
        self.assertEqual((an.text, an.run, an.deck, an.file, an.no_apply, an.apply_budget, an.undo, an.json),
                         ("1要a 2不要c", "latest", None, None, False, 0.05, None, False))
        self.assertEqual(p.parse_args(["sieve", "check", "x"]).sieve_command, "check")
        self.assertTrue(p.parse_args(["sieve", "show", "x", "--json"]).json)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.parse_args(["sieve"])

    def test_from_run_inherits_what_is_not_given(self):
        code, _, err = self.main(["screen", IDEA, "--max-out", "1", "--reads", "1", "--no-translate", "--keywords",
                                  "humanoid robot", "--cards", "0"])
        self.assertEqual(code, 0, err)
        base, bp, _ = self.last_run()
        self.log.clear()
        code, out, err = self.main(["screen", IDEA, "--from-run", base, "--cards", "0"],
                                   band_factory(self.log, no_l1=True))
        self.assertEqual(code, 0, err)
        self.assertEqual([c.layer for c in self.log], ["l2"])                   # L1 never read
        self.assertIn(f"L1 loaded from run {base} ($0)", out)
        _, p, _ = self.last_run()
        self.assertEqual((p["max_out"], p["reads"], p["translate"], p["keywords"], p["from_run"], p["budget_usd"]),
                         (1, 1, False, ["humanoid robot"], base, bp["budget_usd"]))
        code, _, err = self.main(["screen", IDEA, "--from-run", base, "--max-out", "2", "--reads", "3", "--budget",
                                  "3", "--cards", "0"], band_factory(self.log, no_l1=True))
        self.assertEqual(code, 0, err)
        _, p, _ = self.last_run()
        self.assertEqual((p["max_out"], p["reads"], p["translate"], p["budget_usd"]), (2, 3, False, 3.0))
        code, _, err = self.main(["screen", IDEA, "--from-run", "scr-nope"])
        self.assertEqual(code, 1)
        self.assertIn("unknown screen run", err)
        code, _, err = self.main(["screen", IDEA, "--sieve", str(self.home / "missing.json")])
        self.assertEqual(code, 1)
        self.assertIn("sieve not found", err)

    def test_screen_writes_and_prints_the_deck(self):
        rid, od = self.screen_with_deck()
        code, out, _ = self.main(["screen", IDEA, "--reads", "1"])
        self.assertIn("已加载校准：0 条回答，0 条规则", out)
        self.assertIn("校准卡 · humanoid robots（1 张，约 1 分钟）", out)
        self.assertIn("[1] RoboCorp · 现排第1 — 只有大类、没提想法里的东西：这一类算不算？", out)
        self.assertIn(f"jevscreen answer \"1要a\" --run {self.last_run()[0]}", out)   # 1 card: only card 1 exists
        deck = calib.load_deck(od)
        self.assertEqual(len(calib.parse_answers("1要a", deck)), 1)
        two = {**deck, "cards": deck["cards"] + [{**deck["cards"][0], "n": 2}]}
        self.assertIn("jevscreen answer \"1要a 2不要c\"", cli._deck_text(two, od / "cards.md", rid))
        self.assertEqual((deck["deck_id"], [c["security_id"] for c in deck["cards"]]),
                         (f"deck-{rid}-1", ["NYSE:ROBO"]))
        self.assertEqual((od / "cards.md").read_text(encoding="utf-8"), calib.render_cards_md(deck))
        code, out, _ = self.main(["screen", IDEA, "--reads", "1", "--cards", "0"])
        self.assertEqual(code, 0)
        self.assertNotIn("校准卡", out)
        self.assertFalse((self.last_run()[2] / "cards.json").exists())
        code, out, _ = self.main(["screen", IDEA, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn("校准卡", out)
        # no sieve at all: an empty deck says so
        calib.sieve_path(self.cfg, IDEA).unlink()
        code, out, _ = self.main(["screen", IDEA, "--reads", "1"])
        self.assertIn(calib.EMPTY_DECK_ZH, out)
        self.assertNotIn("已加载校准", out)


class TestCardsCommand(CliCase):
    def test_latest_run_id_out_dir_and_rebuilt_inputs(self):
        rid, od = self.screen_with_deck()
        code, out, _ = self.main(["cards"])
        self.assertEqual(code, 0)
        self.assertIn("[1] RoboCorp", out)
        code, out, _ = self.main(["cards", rid, "--json"])
        deck = json.loads(out)
        self.assertEqual((code, deck["deck_id"], deck["format"]), (0, f"deck-{rid}-1", "jevscreen.cards/1"))
        dest = self.home / "elsewhere"
        code, out, _ = self.main(["cards", str(od), "--out", str(dest), "--max", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((dest / "cards.json").read_text())["cards"], [])
        self.assertIn(calib.EMPTY_DECK_ZH, out)
        # a run made before l2_inputs.jsonl: the inputs are rebuilt from the documents (the same text here)
        (od / "l2_inputs.jsonl").unlink()
        code, out, _ = self.main(["cards", rid])
        self.assertEqual(code, 0)
        self.assertIn("早于 l2_inputs.jsonl", out)
        rebuilt = calib.load_deck(od)
        self.assertEqual([c["quote"] for c in rebuilt["cards"]], [c["quote"] for c in deck["cards"]])
        self.assertEqual([c["evidence_sha"] for c in rebuilt["cards"]], [c["evidence_sha"] for c in deck["cards"]])

    def test_unknown_run(self):
        code, _, err = self.main(["cards", "scr-nope"])
        self.assertEqual(code, 1)
        self.assertIn("没有这个筛选运行", err)
        code, _, err = self.main(["cards"])
        self.assertEqual(code, 1)
        self.assertIn("没有找到可用的筛选结果", err)


class TestAnswerCommand(CliCase):
    def test_no_apply_errors_undo_and_show(self):
        rid, od = self.screen_with_deck()
        for text, msg in (("2要a", "没有第 2 张卡"), ("1要c", "c 是「不要」的理由"), ("", "没有回答")):
            code, _, err = self.main(["answer", text])
            self.assertEqual(code, 1, text)
            self.assertIn(msg, err)
        code, out, err = self.main(["answer", "1不要c", "--no-apply"])
        self.assertEqual(code, 0, err)
        self.assertIn("已记录 1 条回答", out)
        self.assertIn("移出 1：RoboCorp #1（你：不要）", out)
        self.assertIn(f"--from-run {rid}", out)
        sv = calib.load_sieve(calib.sieve_path(self.cfg, IDEA))
        self.assertEqual([(e["security_id"], e["want"], e["chip"], e["pin"], e["deck_id"]) for e in sv["examples"]],
                         [("NYSE:ROBO", "no", "c", True, f"deck-{rid}-1")])
        self.assertEqual(json.loads((od / "answers.json").read_text())["answers"]["1"]["v"], "no")
        self.assertEqual(self.query("SELECT count(*) FROM screen_runs")[0][0], 1)        # nothing screened
        code, out, _ = self.main(["sieve", "show", IDEA])
        self.assertEqual(code, 0)
        self.assertIn("1. RoboCorp NYSE:ROBO · 不要c · 钉选", out)                 # the answer grammar
        # the answered deck is replaced by the next one; answering the old deck id is refused
        self.main(["cards", rid])
        code, _, err = self.main(["answer", "1要a", "--deck", f"deck-{rid}-1"])
        self.assertEqual(code, 1)
        self.assertIn(f"已换成 deck-{rid}-2", err)
        code, out, err = self.main(["answer", "--undo", "1"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"已撤销第 1 条回答：RoboCorp（{calib.answer_words_zh('no', 'c')}）", out)
        self.assertNotIn("（no）", out)
        self.assertNotIn("sieves", out)                     # the sieve path only with --verbose
        self.assertEqual(calib.load_sieve(calib.sieve_path(self.cfg, IDEA))["examples"], [])
        code, _, err = self.main(["answer", "--undo", "3"])
        self.assertEqual(code, 1)
        self.assertIn("没有第 3 条回答", err)

    def test_file_answers_and_json(self):
        self.screen_with_deck()
        f = self.home / "a.json"
        f.write_text(json.dumps({"answers": {"1": {"v": "yes", "chip": "b"}}}), encoding="utf-8")
        code, out, err = self.main(["answer", "--file", str(f), "--no-apply", "--json"])
        self.assertEqual(code, 0, err)
        s = json.loads(out)
        self.assertEqual((s["status"], s["answers"][0]["level"], s["stage_a"]["rows"][0]),
                         ("recorded", "partial", "NYSE:ROBO"))
        self.assertIn("已记录 1 条回答", s["text"])

    def test_apply_adopts_the_rule_and_screens_again_without_l1(self):
        rid, od = self.screen_with_deck(factory=rule_factory(self.log))
        base_l1 = self.query("SELECT count(*) FROM screen_results WHERE run_id = ? AND layer = 'l1'", [rid])[0][0]
        self.log.clear()
        code, out, err = self.main(["answer", "1不要c", "--verbose"], rule_factory(self.log))
        self.assertEqual(code, 0, out + err)
        self.assertIn("候选规则：user_not_supplier", out)
        self.assertIn("预计：规则试验约 $0.0200", out)
        self.assertIn("· 采用", out)
        self.assertNotIn("fit", {q.key for c in self.log for _, q in c.classified})     # no L1 question at all
        sv = calib.load_sieve(calib.sieve_path(self.cfg, IDEA))
        self.assertEqual([r["id"] for r in sv["rules"]], ["user_not_supplier"])
        self.assertEqual((sv["rules"][0]["trial"]["agree_before"], sv["rules"][0]["trial"]["agree_after"]), (0, 1))
        new_id, p, new_od = self.last_run()
        self.assertEqual((p["from_run"], p["reads"], p["sieve_path"]),
                         (rid, 3, str(calib.sieve_path(self.cfg, IDEA))))
        self.assertIn(f"重新筛选 {new_id}", out)
        self.assertIn("移出 1：RoboCorp #1（你：不要）", out)
        res = json.loads((new_od / "results.json").read_text())
        self.assertIn(RULE_MARK, res["questions"]["l2"]["criteria"]["insufficient"])
        self.assertEqual([r["security_id"] for r in res["excluded_by_user"]], ["NYSE:ROBO"])
        self.assertEqual(self.query("SELECT count(*) FROM screen_results WHERE run_id = ? AND layer = 'l1'",
                                    [new_id])[0][0], base_l1)
        self.assertTrue((new_od / "cards.json").exists())
        md = (new_od / "report.md").read_text()
        self.assertIn("## 校准", md)
        self.assertIn("采用的规则：user_not_supplier", md)
        self.assertIn("### 你排除的：1", md)

    def test_default_answer_output_is_plain_and_names_the_new_result(self):
        """The printout a novice sees (no --verbose): plain lines, the new run id and the updated page."""
        rid, od = self.screen_with_deck(factory=rule_factory(self.log))
        code, out, err = self.main(["answer", "1不要c"], rule_factory(self.log))
        self.assertEqual(code, 0, out + err)
        new_id, _p, new_od = self.last_run()
        self.assertIn("从你的回答里总结出 1 条规则", out)
        self.assertIn("预计约 $", out)
        self.assertIn("规则试验：采用 1 条（一致度是用你的回答算的", out)
        self.assertIn(f"重新筛选完成（规则试验和重新筛选共花费 $", out)
        self.assertIn(f"新结果编号 {new_id}", out)
        # the idea's one stable page (the newest run), the same file the first screen opened
        self.assertIn("结果页已更新（同一个页面，刷新即可）：file://", out)
        from jevscreen import page
        self.assertIn(page.stable_path(self.cfg, IDEA).resolve().as_uri(), out)
        for internal in ("user_not_supplier", "L1 沿用", "候选规则："):
            self.assertNotIn(internal, out)

    def test_a_rescreen_that_ran_out_of_budget_does_not_say_done(self):
        class Pricey(RuleJev):            # estimates $0.01 a request, the paid re-screen L2 costs $0.05
            def classify(self, items, question):
                if self.layer == "l2" and not self.dry_run:
                    self.COST = 0.05
                return super().classify(items, question)
        log = self.log

        def factory(cfg, *, run_id, layer, budget_usd, dry_run, **kw):
            return Pricey(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
        self.screen_with_deck(factory=rule_factory(self.log))
        code, out, err = self.main(["answer", "1不要c"], factory)
        self.assertEqual(code, 0, out + err)
        self.assertIn("重新筛选只完成了一部分", out)
        self.assertNotIn("重新筛选完成（", out)

    def test_jev_down_in_the_rescreen_says_so(self):
        self.screen_with_deck(factory=rule_factory(self.log))
        log = self.log

        def factory(cfg, *, run_id, layer, budget_usd, dry_run, **kw):
            if layer == "l2" and not dry_run:
                raise JevUnavailable("down")
            return RuleJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
        code, out, err = self.main(["answer", "1不要c"], factory)
        self.assertEqual(code, 6, out + err)
        self.assertIn("重新筛选没有完成：AI 服务（Jev）不可用", out)
        self.assertNotIn("重新筛选完成（", out)
        self.assertIn("error: answer:", err)

    def test_every_rule_rejected_exits_7(self):
        rid, _ = self.screen_with_deck()
        code, out, err = self.main(["answer", "1不要c"])                  # the fake ignores rules: no gain
        self.assertEqual(code, 7, out + err)
        self.assertIn("和你的回答一致度没有提高（0→0），未采用", out)
        sv = calib.load_sieve(calib.sieve_path(self.cfg, IDEA))
        self.assertEqual((sv["rules"], [r["id"] for r in sv["rejected_rules"]]), ([], ["user_not_supplier"]))
        new_id, p, _ = self.last_run()
        self.assertEqual(p["from_run"], rid)                              # pins still applied by a new screen

    def test_over_budget_exits_5_and_keeps_stage_a(self):
        self.screen_with_deck()
        n_runs = self.query("SELECT count(*) FROM screen_runs")[0][0]
        self.log.clear()
        code, out, err = self.main(["answer", "1不要c", "--apply-budget", "0.01"])
        self.assertEqual(code, 5, out + err)
        self.assertIn("超过上限：回答已保存", out)
        self.assertIn("--apply-budget 0.06", out)
        self.assertEqual([c for c in self.log if not c.dry_run], [])       # no paid client at all
        self.assertEqual(self.query("SELECT count(*) FROM screen_runs")[0][0], n_runs)
        self.assertEqual(len(calib.load_sieve(calib.sieve_path(self.cfg, IDEA))["examples"]), 1)

    def test_jev_unavailable_exits_6_and_keeps_stage_a(self):
        self.screen_with_deck()

        def factory(cfg, *, run_id, layer, budget_usd, dry_run, **kw):
            if not dry_run:
                raise JevUnavailable("401")
            return FakeJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=self.log)
        code, out, err = self.main(["answer", "1不要c"], factory)
        self.assertEqual(code, 6, out + err)
        self.assertIn("回答和关键词已保存", err)
        self.assertEqual(len(calib.load_sieve(calib.sieve_path(self.cfg, IDEA))["examples"]), 1)

    def test_stale_sieve_is_refused(self):
        self.screen_with_deck()
        real = calib.load_sieve

        def stale(path):          # another writer saved in between: the version on disk moved on
            sv = real(path)
            if sv is not None:
                Path(path).write_text(json.dumps({**sv, "version": sv["version"] + 1}), encoding="utf-8")
            return sv
        with mock.patch.object(calib, "load_sieve", stale):
            code, _, err = self.main(["answer", "1不要c", "--no-apply"])
        self.assertEqual(code, 1)
        self.assertIn("请重新读取", err)


class TestSieveCommands(CliCase):
    def test_check_passes_and_prints_the_question(self):
        path = self.write_sieve(examples=[{"security_id": "NYSE:ROBO", "want": "explicit", "source": "sieve",
                                           "pin": False}], rules=[{"id": "mention_only", "version": 1}])
        code, out, err = self.main(["sieve", "check", IDEA])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"校准文件：{path}", out)
        self.assertIn("公司：1/1 条示例对得上", out)
        self.assertIn("L2 问题（Jev 收到的）：", out)
        self.assertIn("A single mention", out)
        self.assertIn("结果：通过", out)
        self.assertEqual(self.main(["sieve", "check", str(path)])[0], 0)

    def test_check_problems(self):
        facets = {"category": "robot arms like RoboCorp", "target": "warehouses", "mechanism": "picks"}
        self.write_sieve(facets=facets, examples=[{"security_id": "NASDAQ:ROBO", "want": "explicit",
                                                   "source": "sieve", "pin": False}])
        code, out, _ = self.main(["sieve", "check", IDEA])
        self.assertEqual(code, 1)
        self.assertIn("第 1 条示例 NASDAQ:ROBO 找不到公司；相近：", out)
        self.assertIn("NYSE:ROBO（RoboCorp）", out)
        self.assertIn("公司：0/1 条示例对得上", out)
        self.assertIn("含公司名 RoboCorp", out)
        path = calib.sieve_path(self.cfg, IDEA)
        path.write_text(json.dumps({"format": "jevscreen.sieve/1", "rules": ["nope"]}), encoding="utf-8")
        code, out, _ = self.main(["sieve", "check", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("结构：不通过", out)
        code, _, err = self.main(["sieve", "check", "another idea"])
        self.assertEqual(code, 1)
        self.assertIn("没有校准文件", err)
        code, _, err = self.main(["sieve", "show", "another idea"])
        self.assertEqual(code, 1)

    def test_check_flags_noisy_keywords(self):
        seed_official(self.cfg, self.home)       # one EDINET filing: ヒューマノイド is in 100% of them
        self.write_sieve(keywords={"ja": {"add": ["ヒューマノイド", "SAML"], "weak": [], "log": []}})
        code, out, err = self.main(["sieve", "check", IDEA])
        self.assertEqual(code, 0, out + err)
        self.assertIn("日文关键词（EDINET 1 份年报", out)
        self.assertIn("ヒューマノイド  df 1（100.0%）lift -  应降权", out)
        self.assertIn("SAML  df 0（0.0%）lift -  年报里没出现", out)

    def test_show_json(self):
        path = self.write_sieve(facets_zh={"category": "机器人", "target": "仓库"})
        code, out, _ = self.main(["sieve", "show", IDEA, "--json"])
        self.assertEqual((code, json.loads(out)["version"]), (0, 1))
        code, out, _ = self.main(["sieve", "show", str(path)])
        self.assertIn("回答：（无）", out)


class TestKeywordProposals(StoreCase):
    def test_noisy_seeds_preview_and_rebuilt_inputs(self):
        from jevscreen import store
        seed_official(self.cfg, self.home)
        res = self.run_screen(keywords_zh=["人形机器人"], keywords_ja=["ヒューマノイド"], reads=1)
        inputs = calib.load_inputs(self.home / "out")
        with store.session(self.cfg, read_only=True) as con:
            pool = calib.load_pool(con, res["run_id"], res["params"])
            rebuilt = calib.rebuild_inputs(con, res, list(inputs))
            props = {p["lang"]: p for p in calib.propose_keywords(self.cfg, con, res, None, inputs, pool)}
            sv = humanoid_sieve(examples=[{"security_id": "SZSE:300999", "want": "explicit", "chip": "a",
                                           "source": "card", "pin": True}])
            guarded = {p["lang"]: p for p in calib.propose_keywords(self.cfg, con, res, sv, inputs, pool)}
        self.assertEqual({k: v["text"] for k, v in rebuilt.items()}, {k: v["text"] for k, v in inputs.items()})
        self.assertEqual(sorted(props), ["ja", "ko", "zh"])
        zh = props["zh"]
        # 人形机器人 is in 2 of the 3 CNINFO filings: weak (down-weighted), and the preview shows who loses a hit
        self.assertEqual((zh["weak"], zh["add"], zh["adopted"]), (["人形机器人"], [], True))
        self.assertEqual(zh["noisy"]["stats"]["人形机器人"], {"df": 2, "share": 0.6667})
        self.assertEqual([(c["security_id"], c["hit_before"], c["hit_after"]) for c in zh["preview"]["changes"]],
                         [("SZSE:300999", True, False)])
        self.assertEqual(zh["log"], [{"term": "人形机器人", "action": "weak", "source": "noisy", "df": 2,
                                      "share": 0.6667}])
        # a company the user answered yes must not lose its keyword paragraph: the change is not adopted
        self.assertFalse(guarded["zh"]["adopted"])
        self.assertIn("China Robo", guarded["zh"]["why_zh"])
        self.assertTrue((self.home / "calib" / "df-cninfo_annual_report.json").exists())     # cached per source


class TestReportCalibration(StoreCase):
    def test_section_labels_and_console(self):
        sieve = humanoid_sieve(examples=[
            {"security_id": "LSE:GEAR", "want": "explicit", "chip": "a", "source": "card", "pin": True},
            {"security_id": "NYSE:ROBO", "want": "no", "chip": "c", "source": "card", "pin": True},
            {"security_id": "NASDAQ:BANK", "want": "explicit", "source": "sieve", "pin": False}],
            rejected_rules=[{"id": "laundry_list", "why_zh": "这条规则会误伤 X，未采用"}])
        res = self.run_screen(sieve=sieve, reads=1)
        md = (self.home / "out" / "report.md").read_text()
        for text in ("## 校准", "已加载校准：2 条回答，0 条规则", "没采用：laundry_list — 这条规则会误伤 X，未采用",
                     "### 用户判断（你答过的公司）", "| GearCo | 要a（直接） | insufficient | 用户判断（年报未写明） |",
                     "### 你排除的：1", "| NYSE:ROBO | RoboCorp | c 只是做/用这项技术，不卖想法里的东西 | explicit |",
                     "should_pass BankCo 未通过（L1 没过；应通过：多读 2 次，仍低于「相关」（仅简介，最多算相关））", "L1 漏掉了 GearCo (LSE:GEAR)",
                     "insufficient · 用户判断（年报未写明）"):
            self.assertIn(text, md)
        con = report.format_console(res)
        self.assertIn("已加载校准：2 条回答，0 条规则", con)
        self.assertIn("你排除的：1 家", con)
        self.assertIn("insufficient 用户判断（年报未写明）", con)

    def test_no_section_without_calibration(self):
        res = self.run_screen(reads=1)
        self.assertEqual(report._calibration_section(res), [])
        self.assertNotIn("校准", (self.home / "out" / "report.md").read_text())
        res = self.run_screen(jev_factory=band_factory(self.log), out_dir=self.home / "band")    # band reads
        md = (self.home / "band" / "report.md").read_text()
        self.assertIn("### 多读几次（稳定性）", md)
        self.assertIn("边缘（平均 0.40–0.60，再读可能翻）：1 家", md)
        self.assertIn("读取次数（列表中）：", md)
        self.assertIn("多读：边界 2 家各多读 2 次", report.format_console(res))


class TestReviewFixes(StoreCase):
    def test_a_yes_below_the_cut_is_listed(self):
        sieve = humanoid_sieve(examples=[
            {"security_id": "LSE:GEAR", "want": "explicit", "chip": "a", "source": "card", "pin": True},
            {"security_id": "TSE:6000", "want": "partial", "chip": "b", "source": "card", "pin": True}])
        res = self.run_screen(sieve=sieve, reads=1, max_out=1)
        got = [(r["security_id"], r["rank"], r.get("user_verdict"), r.get("below_cut")) for r in res["rows"]]
        self.assertEqual(got[0][:3], ("NYSE:ROBO", 1, None))
        self.assertEqual(got[1:], [("LSE:GEAR", 3, "explicit", True), ("TSE:6000", 4, "partial", True)])
        md = (self.home / "out" / "report.md").read_text()
        self.assertIn("### 用户判断（你答过的公司）", md)
        self.assertIn("| 3 | LSE:GEAR | GearCo | 要a（直接） | insufficient | 用户判断（年报未写明） · "
                      f"{report.BELOW_CUT_ZH} |", md)
        self.assertIn(report.BELOW_CUT_ZH, report.format_console(res))

    def test_console_says_which_band_reads_were_skipped(self):
        res = self.run_screen(jev_factory=band_factory(self.log), budget_usd=0.021)
        band = res["layers"]["l2"]["band"]
        self.assertEqual((band["ok"], band["skipped"]), (0, 4))
        con = report.format_console(res)
        self.assertIn("多读：边界 2 家，计划多读 4 次：成功 0，跳过 4", con)
        self.assertNotIn("各多读 2 次", con)
        # the band companies are unverified here; a listed row that was read once says so in the console table too
        res["rows"][0]["l2_read_note"] = screen.READ_ONCE_NOTE
        sid = res["rows"][0]["security_id"]
        row = next(line for line in report.format_console(res).splitlines() if sid in line)
        self.assertIn(screen.READ_ONCE_NOTE, row)

    def test_screen_refuses_a_rule_text_with_a_company_name(self):
        cases = [(dict(facets={"category": "robot arms like RoboCorp", "target": "warehouses"},
                       rules=["user_not_supplier"]), "含公司名 RoboCorp"),
                 (dict(rules=[{"id": "homonym", "terms": ["Robo Two"]}]), "含公司名 Robo Two"),
                 (dict(rules=[{"id": "homonym", "terms": ["ROBO"]}]), "含股票代码 ROBO")]
        for kw, msg in cases:
            with self.subTest(msg=msg), self.assertRaises(ValueError) as cm:
                self.run_screen(sieve=humanoid_sieve(**kw), reads=1)
            self.assertIn(msg, str(cm.exception))
            self.assertIn("没有发送任何请求", str(cm.exception))
        self.assertEqual(self.log, [])                                      # no Jev client was even created
        self.assertEqual(self.query("SELECT count(*) FROM screen_runs")[0][0], 0)
        # the run's own keyword term is not a ticker: the homonym rule may carry it
        res = self.run_screen(sieve=humanoid_sieve(rules=[{"id": "homonym", "terms": ["ROBO"]}]), reads=1,
                              keywords=["humanoid robots", "ROBO"])
        self.assertIn("Terms such as 'ROBO'", res["questions"]["l2"]["criteria"]["insufficient"])


if __name__ == "__main__":
    unittest.main()
