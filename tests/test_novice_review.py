"""Review fixes of the novice-run changes (docs/AGENT_API.md "quickstart"): the English sentence check never takes a
capital letter at the start of a sentence for a company name and lets a Chinese idea's own company through, a fix of
a refused sentence keeps the approval only when it truly changes nothing, the suggested rewrite never breaks the
sentence, a yes to the profile fill is never lost, the fill question names relevant companies and every fill ending
is told, an answer on an older page applies to the newest version, a key file must hold only the key, and small
view fixes (refusals on every path, the names cache not taken for a job).

No network and no paid calls: the fakes of test_quickstart / test_screen; every company is invented, except the
well-known names of the alias table the check itself carries (calib.IDEA_EN_ALIASES).
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

from jevscreen import calib, cli, guard, jev, keys, page, quickstart as qs, screen, store  # noqa: E402
from test_novice_flow import CN_ROWS, FlowCase, TestFill, add_cn  # noqa: E402
from test_quickstart import FAKE_KEY  # noqa: E402
from test_screen import SEC_COLS, FakeKeywords  # noqa: E402


def listed_names(out):
    """Every listed company of a quickstart status: the main list (top) and the to-confirm section."""
    return [r["name"] for r in out["top"] + ((out.get("to_confirm") or {}).get("rows") or [])]

# single-word names that are ordinary English words (invented issuers of a real shape), and a few that are not
WORD_NAMES = ["Immersion Corporation", "Lithium Ltd", "Harmonic Inc", "Titanium Group Holdings", "Stem Inc",
              "Coherent Corp", "Warehouse Co., Ltd.", "Phosphate Inc", "Voltara Inc", "ZENTRIX Ltd",
              "Quillon Robotics Inc"]
WORD_TICKERS = ["HRMN", "VLTR"]


def problems(text, idea="人形机器人", names=WORD_NAMES, tickers=WORD_TICKERS):
    return calib.idea_en_problems(text, idea, names, tickers)


class TestSentenceStart(unittest.TestCase):
    def test_an_ordinary_word_opening_a_sentence_is_not_a_company_name(self):
        for s, idea in (("Immersion cooling suppliers for data centers", "浸没式液冷供应商"),
                        ("Lithium battery recyclers", "锂电池回收"),
                        ("Harmonic reducer makers for humanoid robots", "人形机器人谐波减速器"),
                        ("Titanium alloy suppliers for aerospace", "航空钛合金"),
                        ("Stem cell therapy developers", "干细胞治疗"),
                        ("Coherent optics module makers", "相干光模块"),
                        ("Warehouse automation robot makers", "仓储自动化"),
                        ("Phosphate fertilizer producers", "磷肥"),
                        ("Data center cooling. Immersion cooling suppliers", "液冷"),
                        ("Suppliers Of Harmonic Reducers For Humanoid Robots", "谐波减速器")):      # Title Case
            self.assertEqual(problems(s, idea), [], s)

    def test_capital_letters_that_are_evidence_still_count(self):
        self.assertIn("Harmonic", " ".join(problems("Suppliers of Harmonic reducers for humanoid robots")))
        self.assertIn("Voltara", " ".join(problems("Voltara battery suppliers")))       # not an English word
        self.assertIn("ZENTRIX", " ".join(problems("ZENTRIX supply chain makers")))      # capitals throughout
        self.assertIn("Quillon Robotics", " ".join(problems("Quillon Robotics peers in humanoid robots")))
        self.assertTrue(calib.ordinary_word("suppliers") and calib.ordinary_word("Immersion"))
        self.assertFalse(calib.ordinary_word("Voltara"))


class TestChineseIdeaNamesTheCompany(unittest.TestCase):
    def test_a_faithful_translation_of_the_ideas_own_company_passes(self):
        cases = [("Suppliers for Tesla's Optimus humanoid robot", "特斯拉人形机器人Optimus的供应商", ["Tesla, Inc."], []),
                 ("Equipment suppliers to CATL", "宁德时代的设备供应商", [], ["CATL"]),
                 ("Apple supply chain companies", "苹果产业链", ["Apple Inc."], []),
                 ("Liquid cooling suppliers in the NVIDIA GB200 supply chain", "英伟达GB200液冷供应链",
                  ["NVIDIA Corporation"], []),
                 ("Suppliers to Toyota for solid-state batteries", "トヨタの全固体電池サプライヤー", ["Toyota Motor Corp"], [])]
        for s, idea, names, tickers in cases:
            self.assertEqual(calib.idea_en_problems(s, idea, names, tickers), [], s)

    def test_a_company_the_idea_does_not_name_still_counts(self):
        probs = calib.idea_en_problems("Humanoid robot suppliers for Tesla", "人形机器人供应商", ["Tesla, Inc."], [])
        self.assertIn("Tesla", " ".join(probs))
        probs = calib.idea_en_problems("Suppliers for Tesla and NVIDIA", "特斯拉供应商",
                                       ["Tesla, Inc.", "NVIDIA Corporation"], [])
        self.assertIn("NVIDIA", " ".join(probs))
        self.assertNotIn("Tesla", " ".join(probs))


class TestSuggestionAndMinorFix(unittest.TestCase):
    def test_suggestion_never_breaks_or_changes_the_phrase(self):
        self.assertIsNone(calib.suggest_idea_en("Suppliers for Voltara's humanoid robot", ["Voltara"]))
        self.assertIsNone(calib.suggest_idea_en("Voltara supply chain companies", ["Voltara"]))
        self.assertIsNone(calib.suggest_idea_en("RoboCorp humanoid robot suppliers", ["RoboCorp"]))
        self.assertIsNone(calib.suggest_idea_en("Suppliers to Voltara for batteries", ["Voltara"]))
        self.assertEqual(calib.suggest_idea_en("Liquid cooling makers, such as Envicool and Vertico",
                                               ["Envicool", "Vertico"]), "Liquid cooling makers")
        self.assertEqual(calib.suggest_idea_en("Companies like RoboCorp that build humanoid robots", ["RoboCorp"]),
                         "Companies that build humanoid robots")

    def test_only_a_recase_or_a_synonym_of_an_ordinary_word_keeps_the_meaning(self):
        fix = calib.idea_en_minor_fix
        self.assertTrue(fix("Suppliers of Harmonic reducers", "Suppliers of harmonic reducers", ["Harmonic"]))
        self.assertTrue(fix("Core suppliers of cooling", "Main suppliers of cooling", ["Core"]))
        self.assertFalse(fix("Core suppliers of cooling", "Not suppliers of cooling", ["Core"]))
        self.assertFalse(fix("Core suppliers of cooling", "Suppliers of cooling", ["Core"]))
        self.assertFalse(fix("Suppliers to Voltara", "Suppliers to Quillon", ["Voltara"]))
        self.assertFalse(fix("Voltara supply chain companies", "Supply chain companies", ["Voltara"]))
        self.assertFalse(fix("RoboCorp humanoid robot suppliers", "Leading humanoid robot suppliers", ["RoboCorp"]))
        self.assertFalse(fix("Makers, such as Voltara and RoboCorp", "Makers", ["Voltara", "RoboCorp"]))


def add_us(cfg, rows):
    """Invented US names (no market row: they are only names for the check)."""
    with store.session(cfg) as con:
        snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="t", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
        store.upsert_many(con, "securities", SEC_COLS, [
            (sid, sid.split(":")[0], sid.split(":")[1], name, None, "United States", "stock", "common", True,
             store.company_key(None, sid), snap, True) for sid, name in rows])


class TestHeldApproval(FlowCase):
    def refused_first_run(self, idea, bad):
        self.consent_yes()
        self.set_key()
        out = self.front(idea, idea_en=bad, approve_budget=1)
        self.assertEqual(out["status"], "running")
        orig = qs.Worker.after_universe

        def after(w):
            add_us(self.cfg, [("NASDAQ:HRMN", "Harmonic Inc")])
            return orig(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea), spawn=lambda cfg, key: self.spawned.append(key))
        self.assertEqual(out["status"], "needs_agent", out.get("text_en"))
        return out

    def paid(self):
        return sum(1 for f in self.calls.factory if not f["dry_run"])

    def test_a_different_company_is_the_reprice_question(self):
        idea = self.ZH
        self.refused_first_run(idea, "RoboCorp humanoid robot suppliers")
        for other in ("Leading humanoid robot suppliers", "Humanoid robot suppliers"):
            out = self.front(idea, idea_en=other, approve_budget=1)
            self.assertEqual(out["status"], "needs_human", out.get("pending"))
            self.assertEqual(self.ids(out), ["reprice_idea_en"])
            self.assertTrue(out["pending"][0]["old_refused"])
            self.assertFalse(qs.approval_valid(self.job(idea)))
            self.assertFalse(self.job(idea).get("idea_en_changes"))
        self.assertEqual(self.paid(), 0)

    def test_a_possessive_gets_no_broken_suggestion(self):
        idea = self.ZH
        out = self.refused_first_run(idea, "Suppliers for RoboCorp's humanoid robots")
        self.assertIsNone(out["suggested_idea_en"])
        self.assertIn("<sentence>", out["rerun_command"])

    def test_a_recased_ordinary_word_keeps_the_approval(self):
        idea = self.ZH
        self.refused_first_run(idea, "Suppliers of Harmonic reducers for humanoid robots")
        out = self.front(idea, idea_en="Suppliers of harmonic reducers for humanoid robots", approve_budget=1)
        self.assertEqual(out["status"], "running", out.get("pending"))
        job = self.job(idea)
        self.assertTrue(qs.approval_valid(job))
        self.assertEqual(job["idea_en_changes"][0]["kind"], "false_positive_fix")
        self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertIn("意思没变", out["text_zh"])


class TestViews(FlowCase):
    def test_a_refused_idea_en_is_reported_on_a_reused_result(self):
        self.done_flow(self.ZH, idea_en="Humanoid robot joint suppliers")
        self.names_cache()
        out = self.front(self.ZH, idea_en="Humanoid robot makers such as RoboCorp")
        self.assertEqual(out["status"], "done")
        self.assertIn("RoboCorp", " ".join(out.get("idea_en_problems") or []))
        self.assertEqual(out["suggested_idea_en"], "Humanoid robot makers")
        self.assertIn("--idea-en 'Humanoid robot makers'", out["rerun_command"])

    def test_the_names_cache_is_not_taken_for_a_job(self):
        self.done_flow(self.ZH, idea_en="Humanoid robot joint suppliers")
        self.names_cache()
        os.utime(qs.names_cache_path(self.cfg), None)
        out = qs.status(self.cfg)
        self.assertIsNotNone(out)
        self.assertEqual(out["idea_key"], qs.idea_key(self.ZH))


class TestFillReview(TestFill):
    def test_a_yes_while_another_job_holds_the_lock_is_kept_and_started_later(self):
        idea = self.IDEA_ZH
        self.first_result(idea)
        key = qs.idea_key(idea)
        with guard.budget_lock(self.cfg, qs.LOCK):
            out = self.front(idea, fill_descriptions="yes")
        self.assertEqual(out["status"], "store_busy")
        self.assertTrue(out["poll_command"])
        self.assertIn("补简介", out["text_zh"])
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"]))
        n = len(self.spawned)
        out = qs.status(self.cfg, key, spawn=lambda cfg, k: self.spawned.append(k))
        self.assertEqual(self.spawned[n:], [key])
        self.assertEqual(out["state"], "running")
        self.assertEqual(qs.read_inbox(self.cfg, key), {})
        self.work(key)
        self.assertEqual(self.crawled, [(["CN"], 1e9)])

    def test_the_question_names_related_companies_not_banks(self):
        idea = self.IDEA_ZH
        extra = [("SSE:601999", "Grandriver Bank Co., Ltd. Class A", 400e9, None),
                 ("SSE:688999", "Novachip Memory Corporation Class A", 300e9, None)]
        industry = {"SZSE:300904": ("Producer Manufacturing", "Industrial Machinery"),
                    "SZSE:300901": ("Producer Manufacturing", "Industrial Machinery"),
                    "SSE:600903": ("Producer Manufacturing", "Industrial Machinery"),
                    "SSE:601999": ("Finance", "Major Banks"),
                    "SSE:688999": ("Electronic Technology", "Semiconductors")}
        raw = self.home / "szse_stock.json"
        raw.write_text(json.dumps({"stockList": [{"code": "300901", "orgId": "gsqs300901", "zwjc": "霜线热能"}]},
                                  ensure_ascii=False), encoding="utf-8")
        self.consent_yes("zh")
        self.set_key()
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        orig = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS + extra)
            with store.session(self.cfg) as con:
                for sid, (sec, ind) in industry.items():
                    con.execute("UPDATE securities SET sector = ?, industry = ? WHERE security_id = ?", [sec, ind, sid])
                snap = store.record_snapshot(con, source_id="cninfo_annual_report", kind="stock_list", request=None,
                                             raw_path=str(raw), raw_sha256=None, raw_bytes=None, rows=1,
                                             duration_s=None)
                store.upsert_many(con, "identifiers", ("security_id", "id_type", "id_value", "method", "snapshot_id"),
                                  [("SZSE:300901", "cninfo_orgid", "gsqs300901", "code_exact", snap)])
            return orig(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        item = next(i for i in out["pending"] if i["id"] == "fill_descriptions")
        self.assertEqual(item["missing"], 5)
        q = item["question_zh"]
        self.assertIn("霜线热能", q)
        self.assertIn("Hanbridge Pumps", q)
        self.assertNotIn("Grandriver", q)
        self.assertNotIn("Novachip", q)
        self.assertNotIn("Class A", q)
        self.assertNotIn("Grandriver", item["question_en"])
        self.assertIn("Frostline Thermal", item["question_en"])

    def test_every_fill_ending_is_told(self):
        idea = self.IDEA_ZH
        base = self.first_result(idea)
        self.front(idea, fill_descriptions="yes")
        with mock.patch.object(qs.Worker, "remaining", lambda w: 1e-7):
            self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual((out["status"], out["run_id"]), ("done", base["run_id"]))
        self.assertIn("补到 2 家公司的简介", out["text_zh"])
        self.assertIn("结果不变", out["text_zh"])
        self.assertIn("profiles for 2 companies", out["text_en"])

    def test_a_failed_rerank_is_told(self):
        idea = self.IDEA_ZH
        self.first_result(idea)
        self.front(idea, fill_descriptions="yes")
        with mock.patch.object(screen, "screen", side_effect=RuntimeError("synthetic")):
            self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertIn("补到 2 家公司的简介", out["text_zh"])
        self.assertIn("结果不变", out["text_zh"])

    def test_a_fill_followed_by_an_update_pass_names_both(self):
        idea = self.IDEA_ZH
        self.first_result(idea)

        def upd(cfg, result, kw):
            if not (result.get("params") or {}).get("l1_new"):
                return None
            new = screen.screen(cfg, idea, from_run=result["run_id"], supersedes=result["run_id"],
                                jev_factory=self.factory, keywords_fn=FakeKeywords(), sieve="auto",
                                out_dir=self.home / "upd", budget_usd=1.0,
                                fetch_info={"status": "ok", "fetched": {"cninfo": 1}})
            fetch = {"status": "ok", "fetched": {"cninfo": 1}, "seconds": 1.0, "questions": [],
                     "update": {"run_id": new["run_id"], "status": "ok", "cost_usd": 0.0}}
            return {"result": new, "update": fetch["update"], "fetch": fetch}
        self.fetch_impl = lambda cfg, result, kw: upd(cfg, result, kw) or {
            "result": result, "update": {"skipped": "nothing_fetched"},
            "fetch": {"status": "ok", "fetched": {}, "seconds": 1.0, "update": {"skipped": "nothing_fetched"},
                      "questions": [], "next_command": None, "summary_zh": "没有新年报", "summary_en": "nothing new"}}
        self.front(idea, fill_descriptions="yes")
        self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertIn("补了 2 家公司简介", out["change_zh"])
        self.assertIn("补抓 1 家年报", out["change_zh"])

    def test_an_answer_on_the_older_page_applies_to_the_newest_version(self):
        idea = self.IDEA_ZH
        calib.save_sieve(calib.sieve_path(self.cfg, idea),
                         {**calib.new_sieve(idea), "target_terms": {"en": ["android"]}})
        v1 = self.first_result(idea)
        self.front(idea, fill_descriptions="yes")
        self.work(qs.idea_key(idea))
        v2 = qs.status(self.cfg, qs.idea_key(idea))
        self.assertIn("Frostline Thermal", listed_names(v2))
        with store.session(self.cfg, read_only=True) as con:
            od = con.execute("SELECT output_dir FROM screen_runs WHERE run_id = ?", [v1["run_id"]]).fetchone()[0]
        deck = calib.load_deck(Path(od))
        self.assertTrue(deck["cards"])
        with mock.patch.object(screen, "_default_factory", self.factory), \
                mock.patch.object(screen, "_default_keywords", FakeKeywords()), \
                contextlib.redirect_stdout(io.StringIO()) as buf, contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["answer", f"{deck['cards'][0]['n']}?", "--deck", deck["deck_id"],
                             "--apply-budget", "1"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("最新", buf.getvalue())
        v3 = qs.status(self.cfg, qs.idea_key(idea))
        self.assertNotIn(v3["run_id"], (v1["run_id"], v2["run_id"]))
        self.assertIn("Frostline Thermal", listed_names(v3))
        with store.session(self.cfg, read_only=True) as con:
            params = json.loads(con.execute("SELECT params_json FROM screen_runs WHERE run_id = ?",
                                            [v3["run_id"]]).fetchone()[0])
        self.assertEqual(params["from_run"], v2["run_id"])


class TestKeyFile(FlowCase):
    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_file_with_more_than_the_key_is_refused(self):
        for name, text in (("my.env", f"OPENROUTER_API_KEY={FAKE_KEY}\n"), ("notes.txt", f"my key {FAKE_KEY}\n")):
            p = self.home / name
            p.write_text(text)
            os.chmod(p, 0o600)
            code, out, err = self.run_cli("keys", "set", "openrouter", "--from-file", str(p))
            self.assertEqual(code, 1, out)
            self.assertIn("sk-or-", err + out)
            self.assertNotIn(FAKE_KEY, err + out)
            self.assertNotIn(str(p), err + out)
            self.assertIsNone(self.cfg.openrouter_key_location())

    def test_the_recorded_path_is_never_printed(self):
        p = self.home / "secret-place.txt"
        p.write_text(FAKE_KEY + "\n")
        os.chmod(p, 0o644)
        keys.record_key_file(self.cfg, "openrouter", p)
        code, out, err = self.run_cli("doctor", "--json")
        self.assertNotIn(str(p), out + err)
        self.assertNotIn("secret-place", out + err)
        self.assertIn("recorded key file", out)
        p.write_text("my key " + FAKE_KEY + "\n")        # changed after it was recorded
        t = jev.UrllibTransport(self.cfg)
        with self.assertRaises(jev.JevUnavailable) as cm:
            t.prepare()
        self.assertNotIn("secret-place", str(cm.exception))
        self.assertNotIn(FAKE_KEY, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
