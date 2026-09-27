"""Tests for jevscreen.why: one synthetic store and a few FakeJev runs, one company per stop (invented issuers)."""
from __future__ import annotations

import hashlib
import json
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, screen, store, why  # noqa: E402
from test_screen import StoreCase  # noqa: E402
from why_seed import seed_why  # noqa: E402

IDEA = "humanoid robots"


def extra_lines(cfg) -> None:
    """A second robot maker below the floor, and a preferred-share line (never in the universe)."""
    with store.session(cfg) as con:
        snap = con.execute("SELECT snapshot_id FROM snapshots WHERE source_id = 'tradingview_scanner' LIMIT 1"
                           ).fetchone()[0]
        con.execute("INSERT INTO securities (security_id, exchange, symbol, name, isin, country, tv_type, tv_subtype, "
                    "is_primary, company_key, active) VALUES ('NYSE:SMAL', 'NYSE', 'SMAL', 'Smallbot Inc.', "
                    "'US0000000201', 'United States', 'stock', 'common', true, 'isin:US0000000201', true), "
                    "('NYSE:PFDX', 'NYSE', 'PFDX', 'Prefco Preferred', NULL, 'United States', 'preferred', NULL, "
                    "true, 'sec:NYSE:PFDX', true)")
        con.execute("INSERT INTO market_daily (security_id, as_of, market_cap_usd, avg_volume_10d, snapshot_id) VALUES "
                    "('NYSE:SMAL', DATE '2026-09-26', 1.5e8, 1e6, ?)", [snap])
        con.execute("INSERT INTO descriptions (security_id, source_id, company_key, text, snapshot_id) VALUES "
                    "('NYSE:SMAL', 'tradingview_profile', 'isin:US0000000201', 'Smallbot makes robot arms.', ?)",
                    [snap])


class WhyCase(StoreCase):
    def setUp(self):
        super().setUp()
        seed_why(self.cfg, self.home)
        extra_lines(self.cfg)
        path = calib.sieve_path(self.cfg, IDEA)
        path.parent.mkdir(parents=True, exist_ok=True)
        sv = {**calib.new_sieve(IDEA), "version": 1,
              "examples": [{"security_id": "NYSE:KPAC", "company_key": "isin:US0000000104", "name": "Kestrelpoint",
                            "want": "no", "chip": "c", "source": "card", "pin": True, "deck_id": "deck-x-1"}],
              "should_pass": [{"company": "TINY", "want": "explicit",
                               "resolved": {"security_id": "NYSE:TINY", "company_key": "isin:US0000000006",
                                            "name": "Tiny Robots", "method": "symbol", "resolved_at": "t"}}]}
        path.write_text(json.dumps(sv), encoding="utf-8")
        self.res = self.run_screen(reads=1, max_out=1, sieve="auto", out_dir=self.home / "screens" / "a")

    def explain(self, *targets, run=None, **kw):
        return why.run(self.cfg, list(targets), run_ref=run or self.res["run_id"], **kw)

    def one(self, target, **kw):
        out = self.explain(target, **kw)
        return out["results"][0]


class TestStops(WhyCase):
    def test_one_company_per_stop(self):
        want = {"NYSE:ROBO": "in_output", "NASDAQ:BANK": "l1_rejected", "LSE:GEAR": "l1_rejected",
                "TSE:6000": "l2_unverified", "NASDAQ:ROB2": "ranked_below_cut", "NYSE:SMAL": "below_min_mcap",
                "NYSE:NODS": "no_description", "NASDAQ:FXAC": "shell", "NYSE:KPAC": "excluded_by_user",
                "NYSE:TINY": "forced_extra", "NYSE:PFDX": "not_in_universe", "Nonexistent Widgets": "not_found"}
        out = self.explain(*want)
        self.assertEqual(out["status"], "partly_resolved")
        got = {r["target"]: r["stop"] for r in out["results"]}
        self.assertEqual(got, want)
        for r in out["results"]:
            self.assertIn(r["stop"], why.STAGE_IDS)
            self.assertTrue(r["plain_zh"] and r["plain_en"], r["target"])

    def test_facts_and_changes(self):
        bank = self.one("NASDAQ:BANK")
        self.assertEqual(bank["plain_zh"], "模型只读了它的简介，认为和你的想法关系不大，所以第一步就没通过。")
        l1 = next(s for s in bank["stages"] if s["id"] == "l1")
        self.assertEqual(l1["data"]["label"], "unrelated")
        self.assertIn("简介里没出现：humanoid robots、humanoid、robots", [f["zh"] for f in bank["facts"]])
        self.assertEqual([c["code"] for c in bank["changes"]], ["add_should_pass", "pin_yes"])
        add = bank["changes"][0]
        self.assertEqual(add["steps"][0]["argv"], ["jevscreen", "sieve", "add", "should_pass", "NASDAQ:BANK",
                                                   "--run", self.res["run_id"]])
        self.assertFalse(add["steps"][0]["ask_human"])
        self.assertTrue(add["steps"][1]["ask_human"])        # a paid from-run screen
        self.assertIn("不会进名单", add["outcome_zh"])
        gear = self.one("LSE:GEAR")
        self.assertIn("差一点", next(s for s in gear["stages"] if s["id"] == "l1")["text_zh"])
        small = self.one("NYSE:SMAL")
        codes = [c["code"] for c in small["changes"]]
        self.assertEqual(codes, ["add_should_pass", "lower_min_mcap"])
        low = small["changes"][1]
        self.assertEqual(low["steps"][0]["argv"][:4], ["jevscreen", "screen", "--idea-of", self.res["run_id"]])
        self.assertIn("140000000", low["steps"][0]["argv"])            # floor(0.98 x 1.5e8) to 2 digits
        self.assertIn("--dry-run", low["steps"][0]["argv"])
        self.assertFalse(low["steps"][0]["ask_human"])
        self.assertTrue(low["steps"][1]["ask_human"])
        shell = self.one("Fernhollow Acquisition")
        self.assertEqual(shell["stop"], "shell")
        self.assertIn("Acquisition Corp", shell["facts"][0]["en"])
        self.assertEqual([c["code"] for c in shell["changes"]], ["add_should_pass", "shells_keep"])
        cut = self.one("NASDAQ:ROB2")
        self.assertIn("名单只显示前 1", cut["plain_zh"])
        self.assertRegex(cut["plain_zh"], r"排第 \d")
        excl = self.one("NYSE:KPAC")
        self.assertEqual(excl["changes"][0]["steps"][0]["argv"][:4], ["jevscreen", "answer", "--undo", "1"])
        self.assertTrue(excl["changes"][0]["ask_human"])
        servo = self.one("TSE:6000")
        self.assertIn("没有年报原文，只看了简介。", [g["zh"] for g in servo["gaps"]])
        self.assertIn("gap_key", [c["code"] for c in servo["changes"]])         # EDINET needs a key: gap only
        shixin = self.one("示信")
        self.assertEqual((shixin["stop"], shixin["flags"]), ("ranked_below_cut", ["st"]))
        self.assertIn("交易所风险警示", shixin["facts"][0]["zh"])
        with mock.patch.dict("os.environ", {"JEVSCREEN_EDINET_API_KEY": "test" + "0" * 28}):
            servo = self.one("TSE:6000")
        sync = next(c for c in servo["changes"] if c["code"] == "sync_one")
        self.assertEqual(sync["steps"][0]["argv"], ["jevscreen", "sync-edinet", "--codes", "6000"])
        self.assertTrue(sync["steps"][0]["ask_human"])       # networked

    def test_commands_are_safe(self):
        out = self.explain("NASDAQ:BANK", "NYSE:SMAL", "NASDAQ:FXAC", "TSE:6000", "NYSE:KPAC", "NASDAQ:ROB2",
                           "示信")
        steps = [s for r in out["results"] for c in r["changes"] for s in c["steps"]]
        self.assertTrue(steps)
        for s in steps:
            self.assertEqual(shlex.split(s["command"]), s["argv"])
            self.assertNotIn(IDEA, s["command"])
            self.assertNotIn("humanoid", s["command"])
            if (s["argv"][1] == "screen" and "--dry-run" not in s["argv"]) or s["argv"][1].startswith("sync-") \
                    or s["argv"][1:3] == ["sieve", "pin"]:
                self.assertTrue(s["ask_human"], s["command"])
            if s["cost_usd"] > 0:
                self.assertTrue(s["ask_human"])

    def test_read_only_and_free(self):
        db = self.cfg.db_path
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        n = len(self.log)
        self.explain("NYSE:ROBO", "NASDAQ:BANK", "NASDAQ:ROB2", checks=True)
        self.assertEqual(len(self.log), n)                     # no Jev client was even built
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)

    def test_checks_and_pin_source(self):
        out = self.explain(checks=True)
        self.assertEqual({r["match"]["security_id"] for r in out["results"]}, {"NYSE:KPAC", "NYSE:TINY"})

    def test_render_text_golden(self):
        bank = self.one("NASDAQ:BANK")
        zh = why.render_text(bank, "zh").splitlines()
        self.assertEqual(zh[0], "BankCo（NASDAQ:BANK） · 你的想法「humanoid robots」· 2026" + zh[0].split("· 2026")[1])
        self.assertEqual(zh[1], "一句话：模型只读了它的简介，认为和你的想法关系不大，所以第一步就没通过。")
        self.assertTrue(any("✗ 模型判为「无关」（无关 90% / 相关 4% / 核心 1%）" in x for x in zh))
        self.assertIn("               只有判为「核心」，或判为「相关」且 核心+相关 ≥ 60% 才通过", zh)
        self.assertIn("[推断] 以上百分比是模型的判断，模型不给理由。", zh)
        self.assertIn("怎样会改变：", zh)
        en = why.render_text(bank, "en").splitlines()
        self.assertEqual(en[1], "One line: The model read only its profile and judged it unrelated, so it stopped at "
                                "step 1.")
        self.assertTrue(any("(under 1 cent, ~1 min)" in x for x in en))


class TestSuggestedCommandsRun(WhyCase):
    """Every screen command `why` suggests must run as given (a dry run here) and reproduce the explained run."""

    def run_cli(self, argv):
        import contextlib
        import io
        import os
        from jevscreen import cli
        from test_screen import make_factory
        got, real = [], screen.screen

        def spy(*a, **kw):
            got.append(real(*a, **kw))
            return got[-1]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), \
                mock.patch.object(screen, "_default_factory", make_factory(self.log)), \
                mock.patch.object(screen, "_default_keywords", self.kw), mock.patch.object(screen, "screen", spy), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, (got[-1] if got else None), err.getvalue()

    def screen_steps(self, run_id, targets):
        out = why.run(self.cfg, targets, run_ref=run_id)
        return [(r["target"], c["code"], s) for r in out["results"] for c in r["changes"] for s in c["steps"]
                if s["argv"][1] == "screen"]

    def test_fresh_screens_keep_every_option_of_the_run(self):
        base = self.run_screen("人形机器人", reads=1, translate=False, sieve="none", countries=["United States"],
                               shells="keep", out_dir=self.home / "screens" / "nt")
        steps = self.screen_steps(base["run_id"], ["NYSE:SMAL"])
        low = [s for _t, code, s in steps if code == "lower_min_mcap"]
        self.assertEqual(len(low), 2)
        self.assertIn("--dry-run", low[0]["argv"])
        for flag in ("--no-translate", "--shells", "--reads"):
            self.assertIn(flag, low[1]["argv"])
        self.assertEqual(low[1]["argv"][low[1]["argv"].index("--sieve") + 1], "none")
        code, res, err = self.run_cli(low[0]["argv"][1:])
        self.assertEqual(code, 0, err)
        self.assertEqual(res["questions"]["l1"], base["questions"]["l1"])       # the same L1 question: cache hits
        self.assertEqual(res["params"]["l2_question_sha"], base["params"]["l2_question_sha"])
        self.assertEqual((res["params"]["translate"], res["params"]["shells"], res["params"]["sieve_path"]),
                         (False, "keep", None))

    def test_a_caller_supplied_idea_en_is_passed_again(self):
        """A quickstart / --idea-en run: a fresh screen gets the same English sentence (--idea-en), so it asks the
        same questions; the local model is not asked."""
        base = self.run_screen("人形机器人", reads=1, sieve="none", idea_en="Makers of humanoid robots",
                               out_dir=self.home / "screens" / "agent")
        self.assertEqual(base["params"]["idea_en_source"], "agent")
        steps = self.screen_steps(base["run_id"], ["NYSE:SMAL"])
        low = [s for _t, code, s in steps if code == "lower_min_mcap"]
        self.assertTrue(low)
        argv = low[0]["argv"]
        self.assertEqual(argv[argv.index("--idea-en") + 1], "Makers of humanoid robots")
        calls = len(self.kw.calls)
        code, res, err = self.run_cli(argv[1:])
        self.assertEqual(code, 0, err)
        self.assertEqual(res["questions"]["l1"], base["questions"]["l1"])
        self.assertEqual(len(self.kw.calls), calls)
        self.assertEqual(why.next_idea_en(None, "人形机器人", {"idea": "人形机器人", "idea_en": "Other text"},
                                          base["params"], base["idea_en"]), "Makers of humanoid robots")

    def test_every_screen_step_runs_after_a_paid_and_a_dry_run(self):
        dry = self.run_screen(reads=1, dry_run=True, sieve="auto", out_dir=self.home / "screens" / "dry")
        targets = ["NYSE:SMAL", "NASDAQ:FXAC", "NASDAQ:BANK", "TSE:6000", "NASDAQ:ROB2", "NYSE:ROBO"]
        for run_id in (self.res["run_id"], dry["run_id"]):
            steps = self.screen_steps(run_id, targets)
            self.assertTrue(steps)
            for target, code, s in steps:
                argv = s["argv"][1:] + ([] if "--dry-run" in s["argv"] else ["--dry-run"])
                rc, res, err = self.run_cli(argv)
                self.assertEqual(rc, 0, f"{run_id} {target} {code}: {s['command']}: {err}")
                self.assertEqual(res["idea"], IDEA)
                if run_id == dry["run_id"]:
                    self.assertNotIn("--from-run", s["argv"])        # a dry run has no L1 answers to reuse
                    self.assertIn("--dry-run", s["argv"])
        # sieve check on a dry run hands back a command that runs
        from jevscreen import cli  # noqa: F401
        import contextlib
        import io
        import os
        o = io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), contextlib.redirect_stdout(o):
            from jevscreen import cli as c
            c.main(["sieve", "check", "--run", dry["run_id"], "--json"])
        nxt = json.loads(o.getvalue())["next_command"]["argv"]
        rc, res, err = self.run_cli(nxt[1:])
        self.assertEqual((rc, res["idea"]), (0, IDEA), err)

    def test_idea_of_disagreeing_with_the_idea_is_an_error(self):
        rc, _res, err = self.run_cli(["screen", "solid state batteries", "--idea-of", self.res["run_id"],
                                      "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("another idea", err)
        rc, res, err = self.run_cli(["screen", IDEA, "--idea-of", self.res["run_id"], "--dry-run", "--reads", "1"])
        self.assertEqual(rc, 0, err)


class TestPricesAfterSieveEdits(WhyCase):
    def edit(self, **fields):
        path = calib.sieve_path(self.cfg, IDEA)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.update(fields)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def test_from_run_priced_as_a_full_l2_reread_after_a_seed_terms_edit(self):
        full = self.res["layers"]["l2"]["cost_usd"]
        self.assertGreater(full, 0)
        add = self.one("NYSE:SMAL")["changes"][0]
        self.assertEqual(add["code"], "add_should_pass")
        _l1u, l2u = why.units(self.res)
        self.assertAlmostEqual(add["cost_usd"], l2u, places=6)           # nothing changed: one company
        self.edit(seed_terms={"en": ["bipedal robot", "actuator"]})
        add = self.one("NYSE:SMAL")["changes"][0]
        self.assertGreaterEqual(add["cost_usd"], full)
        self.assertIn("第二步", add["text_zh"])
        self.assertIn("重读", add["text_zh"])
        step = add["steps"][1]
        self.assertGreaterEqual(float(step["argv"][step["argv"].index("--budget") + 1]), full)
        pin = self.one("NASDAQ:BANK")["changes"][1]
        self.assertGreaterEqual(pin["cost_usd"], full)

    def test_sieve_set_warns_about_the_l2_reread(self):
        import contextlib
        import io
        import os
        from jevscreen import cli
        draft = self.home / "d.json"
        draft.write_text(json.dumps({"seed_terms": {"en": ["bipedal robot"]}}), encoding="utf-8")
        o = io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), contextlib.redirect_stdout(o):
            code = cli.main(["sieve", "set", "--run", self.res["run_id"], "--from", str(draft), "--json"])
        body = json.loads(o.getvalue())
        self.assertEqual(code, 0, body)
        w = next(w for w in body["warnings"] if w["code"] == "l2_reprice")
        self.assertIn("第二步", w["text_zh"])
        self.assertGreater(w["l2_usd"], 0)
        o = io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), contextlib.redirect_stdout(o):
            cli.main(["sieve", "check", "--run", self.res["run_id"], "--json"])
        self.assertIn("l2_reprice", [w["code"] for w in json.loads(o.getvalue())["warnings"]])

    def test_pin_priced_by_whether_step2_already_read_it(self):
        bank = self.one("NASDAQ:BANK")                        # l1_rejected, never read by step 2
        pin = next(c for c in bank["changes"] if c["code"] == "pin_yes")
        self.assertGreater(pin["cost_usd"], 0)
        self.assertNotIn("免费", why.render_text(bank, "zh").split("只有你本人同意")[1].splitlines()[0])
        servo = self.one("TSE:6000")                          # l2_unverified: already read
        pin = next(c for c in servo["changes"] if c["code"] == "pin_yes")
        self.assertEqual(pin["cost_usd"], 0)


class TestTextsForNovices(WhyCase):
    def ctx(self):
        return why.load_ctx(self.cfg, why.pick_run(why.list_runs(self.cfg), self.res["run_id"]))

    def test_sync_commands_and_gaps(self):
        import types
        from jevscreen import cli
        ctx = self.ctx()
        for sid in ("TWSE:2330", "TPEX:6223"):
            ch = why._sync_change(ctx, types.SimpleNamespace(security_id=sid, company_key="k"), 0.001)[0]
            argv = ch["steps"][0]["argv"]
            self.assertEqual(argv, ["jevscreen", "sync-mops", "--mode", "annual", "--codes", sid.split(":")[1]])
            self.assertEqual(cli.build_parser().parse_args(argv[1:]).mode, "annual")
        nse = why._sync_change(ctx, types.SimpleNamespace(security_id="NSE:INFY", company_key="k"), 0.001)[0]
        self.assertEqual(nse["code"], "gap_no_sync")
        self.assertNotIn("美股", nse["text_zh"])
        self.assertNotIn("sync-sec", nse["text_en"])
        us = why._sync_change(ctx, types.SimpleNamespace(security_id="NASDAQ:ZZZ", company_key="k"), 0.001)[0]
        self.assertIn("sync-sec", us["text_en"])
        exp = {"target": "x", "run": {"run_id": "r", "idea": "i"}, "stages": [], "changes": [nse]}
        text = why.render_text(exp, "zh")
        self.assertNotIn("免费", text)
        self.assertNotIn("(free", why.render_text(exp, "en"))

    def test_shells_texts(self):
        kpac = self.one("NYSE:KPAC")                          # spac_like: a name hit kept by the industry guard
        stage = next(s for s in kpac["stages"] if s["id"] == "shells")
        self.assertNotIn("没有命中", stage["text_zh"])
        self.assertIn("像", stage["text_zh"])
        self.assertTrue(any("没有排除" in f["zh"] for f in kpac["facts"]))
        fx = self.one("NASDAQ:FXAC")
        text = why.render_text(fx, "zh") + why.render_text(fx, "en")
        self.assertNotIn("name_acq", text)
        self.assertIn("Acquisition Corp", text)
        self.assertEqual(fx["stages"][1]["data"]["evidence"]["spac"]["pat"], "name_acq")   # the id stays in data

    def test_console_and_report_lines_have_english(self):
        from jevscreen import report
        res = {**self.res, "st_warning": {"security_ids": ["SZSE:309901"], "text_zh": "注意：ST", "text_en": "Note: ST"}}
        self.assertIn("Note: ST", report.format_console(res))
        lines = report._shells_lines({"shells": {"kept_protected": 2, "kept_mode": 3, "st_gap": {
            "zh": "没有 ST 名单", "en": "no ST list"}}})
        for ln in lines:
            if ln:
                self.assertIn(" / ", ln, ln)


class TestLicenceOfRunFiles(WhyCase):
    def test_report_and_docs_mark_the_run_files_personal(self):
        md = (self.home / "screens" / "a" / "report.md").read_text(encoding="utf-8")
        lic = md.split("## Licence", 1)[1]
        for name in ("funnel.jsonl.gz", "shells_dropped.csv", "l2_inputs.jsonl"):
            self.assertIn(name, lic)
        self.assertIn("do not share", lic)
        docs = Path(__file__).resolve().parents[1] / "docs"
        for doc in ("WHY.md", "SHELLS.md"):
            self.assertIn("Personal use only", (docs / doc).read_text(encoding="utf-8"), doc)


class TestPinnedWithoutProfile(StoreCase):
    def test_pinned_no_profile_company_is_ranked_and_why_says_so(self):
        import datetime as dt
        from test_screen import humanoid_sieve
        (self.home / "docs").mkdir(exist_ok=True)
        doc = self.home / "docs" / "nods.txt"
        doc.write_text("NoDesc Inc. designs humanoid robot hands for factory assembly lines. " * 20, encoding="utf-8")
        with store.session(self.cfg) as con:
            snap = con.execute("SELECT snapshot_id FROM snapshots WHERE source_id='sec_filing_text' LIMIT 1"
                               ).fetchone()[0]
            store.upsert_many(con, "documents", ("doc_id", "security_id", "company_key", "source_id", "cik", "form",
                                                 "section", "filing_date", "url", "text_path", "snapshot_id"), [
                ("sec_filing_text:555:c:item1", "NYSE:NODS", "isin:US0000000005", "sec_filing_text", "555", "10-K",
                 "item1", dt.date(2026, 2, 1), "https://www.sec.gov/nods.htm", str(doc), snap)])
        path = calib.sieve_path(self.cfg, IDEA)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(humanoid_sieve(examples=[
            {"security_id": "NYSE:NODS", "company_key": "isin:US0000000005", "name": "NoDesc", "want": "explicit",
             "chip": "a", "source": "card", "pin": True}])), encoding="utf-8")
        res = self.run_screen(reads=1, sieve="auto", out_dir=self.home / "screens" / "a")
        self.assertIn("NYSE:NODS", [r["security_id"] for r in res["rows"]])
        exp = why.run(self.cfg, ["NYSE:NODS"], run_ref=res["run_id"])["results"][0]
        self.assertEqual(exp["stop"], "in_output")
        self.assertNotIn("不进名单", exp["plain_zh"])
        self.assertTrue(any("没有公司简介" in f["zh"] for f in exp["facts"]))
        md = (self.home / "screens" / "a" / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("你关心的公司（在这次筛选条件以外）", md)


class TestRunsAndFiles(WhyCase):
    def test_other_runs(self):
        r = self.run_screen(reads=1, l2_max=1, out_dir=self.home / "screens" / "b")
        self.assertEqual(self.one("TSE:6000", run=r["run_id"])["stop"], "l2_not_sent")
        d = self.run_screen(reads=1, dry_run=True, out_dir=self.home / "screens" / "c")
        exp = self.one("NYSE:ROBO", run=d["run_id"])
        self.assertEqual(exp["stop"], "dry_run")
        self.assertTrue(exp["run"]["dry_run"])
        self.assertIn("这是试算", why.render_text(exp, "zh"))
        self.assertEqual(self.one("NYSE:ROBO", run=str(self.home / "screens" / "c"))["stop"], "dry_run")
        b = self.run_screen(reads=1, budget_usd=0.0, out_dir=self.home / "screens" / "d")
        self.assertEqual(self.one("NYSE:ROBO", run=b["run_id"])["stop"], "l1_not_sent")

    def test_files_only_when_locked(self):
        with mock.patch.object(store, "session", side_effect=store.StoreLocked("busy")):
            out = self.explain("NASDAQ:BANK", "NYSE:SMAL")
        self.assertEqual(out["status"], "partial_files_only")
        self.assertTrue(out["files_only"])
        self.assertEqual([r["stop"] for r in out["results"]], ["l1_rejected", "below_min_mcap"])
        self.assertIn("数据库被占用", why.render_text(out["results"][0], "zh"))

    def test_choose_run_and_latest(self):
        self.run_screen("servo motors", reads=1, out_dir=self.home / "screens" / "s")
        with self.assertRaises(why.WhyError) as cm:
            why.run(self.cfg, ["NYSE:ROBO"])
        self.assertEqual(cm.exception.status, "choose_run")
        self.assertEqual(len(cm.exception.extra["runs"]), 2)
        out = why.run(self.cfg, ["NYSE:ROBO"], idea=IDEA)
        self.assertEqual(out["run"]["run_id"], self.res["run_id"])
        with self.assertRaises(why.WhyError) as cm:
            why.run(self.cfg, ["NYSE:ROBO"], run_ref="scr-nope")
        self.assertEqual(cm.exception.status, "unknown_run")

    def test_no_runs(self):
        with self.assertRaises(why.WhyError) as cm:
            why.run(self.cfg, ["NYSE:ROBO"], idea="an idea never screened")
        self.assertEqual(cm.exception.status, "no_runs")


class TestPure(unittest.TestCase):
    def test_l1_pass_goldens(self):
        ok = {"status": "ok"}
        self.assertFalse(screen.l1_passes({**ok, "label": "unrelated", "probs": {"core": 0.3, "adjacent": 0.35}}))
        self.assertFalse(screen.l1_passes({**ok, "label": "adjacent", "probs": {"core": 0.3, "adjacent": 0.25}}))
        self.assertTrue(screen.l1_passes({**ok, "label": "core", "probs": {"core": 0.2}}))

    def test_units_and_costs(self):
        res = {"layers": {"l1": {"cost_usd": 0.01}, "l2": {"cost_usd": 0.01, "items": 5}}, "funnel": {"l1_sent": 5}}
        self.assertEqual(why.units(res), (0.002, 0.002))
        self.assertEqual(why.units({}), (why.L1_UNIT_FALLBACK, why.L2_UNIT_FALLBACK))
        self.assertEqual(why.suggest_floor(1.5e8), 140000000)
        self.assertEqual(why.suggest_floor(2.43e9), 2300000000)
        self.assertEqual(why.cost_text(0), ("免费", "free"))
        self.assertEqual(why.cost_text(0.004), ("不到 1 分钱", "under 1 cent"))
        self.assertEqual(why.cost_text(0.35), ("约 $0.35", "about $0.35"))
        self.assertEqual(why.lang_of("人形机器人"), "zh")
        self.assertEqual(why.lang_of("humanoid robots"), "en")


if __name__ == "__main__":
    unittest.main()
