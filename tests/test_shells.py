"""Tests for jevscreen.shells and the shells filter in screen (synthetic store, invented issuers, FakeJev)."""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import report, screen, shells, store  # noqa: E402
import test_screen  # noqa: E402
from test_screen import StoreCase, humanoid_sieve  # noqa: E402
from why_seed import seed_why  # noqa: E402

FXAC, QMRG, BVRB, KPAC = "isin:US0000000101", "isin:US0000000102", "isin:US0000000103", "isin:US0000000104"
SHIXIN, JIAXIN = "isin:CNE000009901", "isin:CNE000009902"


class TestPure(unittest.TestCase):
    def test_st_marks(self):
        self.assertEqual(shells.st_mark("ST示信"), "st")
        self.assertEqual(shells.st_mark("*ST甲信"), "star_st")
        self.assertEqual(shells.st_mark("S*ST乙"), "star_st")
        self.assertEqual(shells.st_mark("ＳＴ示信"), "st")          # full width
        self.assertIsNone(shells.st_mark("乙信科技"))
        self.assertIsNone(shells.st_mark("ST"))
        self.assertEqual(shells.strip_st("*ST甲信"), "甲信")

    def test_spac_rules(self):
        u = [{"company_key": "a", "security_id": "X:A", "name": "Alderpine Acquisition Corp. II"},
             {"company_key": "b", "security_id": "X:B", "name": "Brightvane Robotics"},
             {"company_key": "c", "security_id": "X:C", "name": "Cobaltfen Holdings"},
             {"company_key": "d", "security_id": "X:D", "name": "Dunmere Acquisition Corp"}]
        descs = {"b": [("tradingview_profile", "Brightvane, formerly a blank check company, makes grippers.", True)],
                 "c": [("tradingview_profile", "Cobaltfen Holdings is a blank check company.", True)]}
        facts = {"X:A": ("Financial Conglomerates", 0.0), "X:C": (None, None),
                 "X:D": ("Financial Conglomerates", 2e6)}          # revenue >= $1M: an operating company
        hits = shells.classify(u, descs, facts, None)
        self.assertEqual((hits["a"]["drop"], hits["a"]["security_id"]), (["spac"], "X:A"))
        self.assertEqual(hits["a"]["ev"]["spac"], {"pat": "name_acq", "in": "name", "off": [10, 26]})
        self.assertNotIn("b", hits)
        self.assertEqual(hits["c"]["ev"]["spac"]["pat"], "text_blank_check")
        self.assertEqual(hits["d"]["drop"], [])
        self.assertEqual(hits["d"]["flags"], ["spac_like"])
        self.assertEqual(shells.effective_drops(hits, "drop", {"c"}), {"a"})
        self.assertEqual(shells.effective_drops(hits, "drop", {"X:A"}), {"c"})      # by security_id too
        self.assertEqual(shells.effective_drops(hits, "keep"), set())
        with self.assertRaises(ValueError):
            shells.effective_drops(hits, "strict")
        # deterministic whatever the order of the description rows
        descs2 = {k: list(reversed(v)) for k, v in descs.items()}
        self.assertEqual(shells.classify(list(reversed(u)), descs2, facts, None), hits)


class ShellsCase(StoreCase):
    def setUp(self):
        super().setUp()
        seed_why(self.cfg, self.home)


class TestShellsInScreen(ShellsCase):
    def test_drop_mode(self):
        res = self.run_screen(reads=1)
        self.assertEqual(res["funnel"]["shells_dropped"], 2)
        l1_ids = {it.item_id for it in self.log[0].classified[0][0]}
        self.assertFalse({FXAC, QMRG} & l1_ids)
        self.assertTrue({BVRB, KPAC, SHIXIN} <= l1_ids)            # formerly-a-blank-check and the guard kept
        self.assertEqual(sorted(g["security_id"] for g in res["gaps"]["shells_dropped"]), ["NASDAQ:FXAC", "NYSE:QMRG"])
        with open(self.home / "out" / "shells_dropped.csv", encoding="utf-8") as fh:
            got = {r["security_id"]: r for r in csv.DictReader(fh)}
        self.assertEqual(got["NYSE:QMRG"]["pattern"], "text_blank_check")
        self.assertEqual(got["NASDAQ:FXAC"]["pattern"], "name_acq")
        self.assertNotIn("blank check", (self.home / "out" / "shells_dropped.csv").read_text(encoding="utf-8"))
        rows = {r["security_id"]: r for r in res["rows"]}
        self.assertEqual(rows["SZSE:309901"]["flags"], ["st"])
        self.assertEqual(rows["NYSE:KPAC"]["flags"], ["spac_like"])
        self.assertNotIn("flags", rows["NYSE:ROBO"])                 # empty flags are omitted
        self.assertEqual(res["st_warning"]["security_ids"], ["SZSE:309901"])
        sh = res["shells"]
        self.assertEqual((sh["mode"], sh["dropped"], sh["st_gap"]), ("drop", 2, None))
        self.assertEqual(sh["flags"], {"spac_like": 1, "st": 1, "star_st": 1})
        self.assertEqual(res["params"]["shells"], "drop")
        self.assertEqual(res["params"]["st_list"]["stale"], False)
        md = (self.home / "out" / "report.md").read_text(encoding="utf-8")
        self.assertIn("排除壳公司：2 家", md)
        self.assertIn(shells.ST_WARNING_ZH, md)
        self.assertIn("ST风险警示", md)
        with open(self.home / "out" / "results.csv", encoding="utf-8") as fh:
            got = {r["security_id"]: r for r in csv.DictReader(fh)}
        self.assertEqual(got["SZSE:309901"]["flags"], "st")
        console = report.format_console(res)
        self.assertIn(shells.ST_WARNING_ZH, console)
        self.assertIn("shells 2", console)
        self.assertIn(report.WHY_HINT_ZH, console)

    def test_keep_mode_and_item_keys(self):
        drop = self.run_screen(reads=1, dry_run=True, out_dir=self.home / "d1")
        keep = self.run_screen(reads=1, dry_run=True, out_dir=self.home / "d2", shells="keep")
        self.assertNotIn("shells_dropped", keep["funnel"])
        self.assertEqual(keep["funnel"]["described"], drop["funnel"]["described"] + 2)
        self.assertEqual(keep["shells"]["kept_mode"], 2)
        self.assertNotIn("排除壳公司：", (self.home / "d2" / "report.md").read_text(encoding="utf-8"))
        # the L1 items of the companies both runs read are identical (same item keys, same cache)
        res_drop = self.run_screen(reads=1, out_dir=self.home / "o1")
        n = len(self.log)
        res_keep = self.run_screen(reads=1, out_dir=self.home / "o2", shells="keep")
        a = {it.item_id: it.text for it in self.log[n - 2].classified[0][0]}
        b = {it.item_id: it.text for it in self.log[n].classified[0][0]}
        self.assertEqual(set(b) - set(a), {FXAC, QMRG})
        self.assertEqual({k: b[k] for k in a}, a)
        self.assertEqual(res_drop["shells"]["dropped"], 2)
        self.assertEqual(res_keep["shells"]["dropped"], 0)

    def test_protected_companies_never_dropped(self):
        for ex in ({"security_id": "NASDAQ:FXAC", "want": "no", "source": "card", "pin": True},
                   {"company_key": FXAC, "want": "explicit", "source": "sieve", "pin": False},
                   {"security_id": "NASDAQ:FXAC", "want": "unsure", "source": "card", "pin": False}):
            with self.subTest(ex=ex):
                res = self.run_screen(reads=1, dry_run=True, sieve=humanoid_sieve(examples=[ex]),
                                      out_dir=self.home / "p")
                self.assertEqual(res["funnel"]["shells_dropped"], 1)          # QMRG only
                self.assertEqual(res["shells"]["kept_protected"], 1)

    def test_st_list_missing_and_stale(self):
        with store.session(self.cfg) as con:
            con.execute("UPDATE snapshots SET fetched_at = ? WHERE kind = 'stock_list'", [dt.datetime(2026, 8, 1)])
        res = self.run_screen(reads=1, dry_run=True)
        self.assertTrue(res["params"]["st_list"]["stale"])
        self.assertIn("超过 14 天", res["shells"]["st_gap"]["zh"])
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM snapshots WHERE kind = 'stock_list'")
        res = self.run_screen(reads=1, dry_run=True, out_dir=self.home / "o3")
        self.assertIsNone(res["params"]["st_list"])
        self.assertIn("ST 名单缺失", res["shells"]["st_gap"]["zh"])
        self.assertIn("ST 名单缺失", (self.home / "o3" / "report.md").read_text(encoding="utf-8"))
        # a universe without A shares has nothing to mark: no gap
        res = self.run_screen(reads=1, dry_run=True, out_dir=self.home / "o4", countries=["US"])
        self.assertIsNone(res["shells"]["st_gap"])

    def test_from_run_base_without_shells_inherits_keep(self):
        res = self.run_screen(reads=1)
        with store.session(self.cfg) as con:
            pj = json.loads(con.execute("SELECT params_json FROM screen_runs WHERE run_id = ?",
                                        [res["run_id"]]).fetchone()[0])
            pj.pop("shells")
            con.execute("UPDATE screen_runs SET params_json = ? WHERE run_id = ?", [json.dumps(pj), res["run_id"]])
        res2 = self.run_screen(reads=1, from_run=res["run_id"], out_dir=self.home / "o2")
        self.assertEqual(res2["params"]["shells"], "keep")
        res3 = self.run_screen(reads=1, from_run=res["run_id"], out_dir=self.home / "o3", shells="drop")
        self.assertEqual(res3["params"]["shells"], "drop")

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            self.run_screen(shells="strict")


class TestForcedExtras(ShellsCase):
    def test_below_floor_check_is_read_and_reported_not_ranked(self):
        sv = humanoid_sieve(examples=[
            {"security_id": "NYSE:TINY", "want": "explicit", "source": "sieve", "pin": False},
            {"security_id": "NYSE:TINY", "want": "explicit", "source": "card", "pin": True}])
        res = self.run_screen(reads=1, sieve=sv)
        l2_ids = {it.item_id for it in self.log[1].classified[0][0]}
        self.assertIn("isin:US0000000006", l2_ids)
        self.assertNotIn("isin:US0000000006", {it.item_id for it in self.log[0].classified[0][0]})   # no L1 call
        self.assertNotIn("NYSE:TINY", [r["security_id"] for r in res["rows"] + res["unverified"]])
        ex = res["calibration"]["extras"]
        self.assertEqual([(x["security_id"], x["stage"], x["l2_label"]) for x in ex],
                         [("NYSE:TINY", "below_min_mcap", "partial")])
        self.assertTrue(any("在这次筛选条件以外" in n for n in res["notes"]))
        md = (self.home / "out" / "report.md").read_text(encoding="utf-8")
        self.assertIn("你关心的公司", md)
        self.assertIn("市值低于门槛", md)

    def test_dry_run_counts_extras(self):
        sv = humanoid_sieve(examples=[{"security_id": "NYSE:TINY", "want": "explicit", "source": "sieve"}])
        base = self.run_screen(reads=1, dry_run=True, out_dir=self.home / "a")
        res = self.run_screen(reads=1, dry_run=True, sieve=sv, out_dir=self.home / "b")
        self.assertEqual(res["layers"]["l2"]["inputs"], base["layers"]["l2"]["inputs"] + 1)


class TestShellsCli(ShellsCase):
    main = test_screen.TestCli.main

    def test_flag_and_idea_from_run(self):
        code, out = self.main(["screen", "humanoid robots", "--shells", "keep", "--dry-run", "--reads", "1"])
        self.assertEqual(code, 0)
        self.assertNotIn("shells 2", out)
        code, out = self.main(["screen", "humanoid robots", "--reads", "1", "--fetch-docs", "off"])
        self.assertEqual(code, 0)
        self.assertIn("shells 2", out)
        run_id = self.query("SELECT run_id FROM screen_runs")[0][0]
        code, out = self.main(["screen", "--from-run", run_id, "--dry-run"])      # idea taken from the run
        self.assertEqual(code, 0)
        self.assertIn("L1 loaded from run", out)
        code, out = self.main(["screen", "--idea-of", run_id, "--dry-run", "--min-mcap", "5e7"])
        self.assertEqual(code, 0)
        self.assertNotIn("L1 loaded from run", out)
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code, _ = self.main(["screen", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("give the idea", err.getvalue())


if __name__ == "__main__":
    unittest.main()
