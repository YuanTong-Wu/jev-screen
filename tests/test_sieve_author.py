"""Tests for the sieve author fields (jevscreen.sieve_author, calib hooks, screen) and the sieve commands."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, cli, screen, sieve_author, store  # noqa: E402
from test_screen import FakeKeywords, StoreCase, make_factory  # noqa: E402
from why_seed import seed_why  # noqa: E402

IDEA = "humanoid robots"
ZH_IDEA = "人形机器人"


def add_quillon(cfg, home: Path) -> None:
    """A company whose profile never says robot (L1 rejects it) but whose annual report does (L2 explicit)."""
    (home / "docs").mkdir(exist_ok=True)
    doc = home / "docs" / "quil.txt"
    doc.write_text("Item 1. Business\n\nQuillon Systems develops humanoid robots for hospital logistics and sells "
                   "them to regional hospital groups under service contracts.\n\nWe employ about 300 people in two "
                   "offices and outsource the manufacturing of our mechanical parts.\n", encoding="utf-8")
    with store.session(cfg) as con:
        snap = con.execute("SELECT snapshot_id FROM snapshots LIMIT 1").fetchone()[0]
        con.execute("INSERT INTO securities (security_id, exchange, symbol, name, isin, country, tv_type, tv_subtype, "
                    "is_primary, company_key, active) VALUES ('NASDAQ:QUIL', 'NASDAQ', 'QUIL', 'Quillon Systems Inc.', "
                    "'US0000000301', 'United States', 'stock', 'common', true, 'isin:US0000000301', true), "
                    "('NASDAQ:ZEPH', 'NASDAQ', 'ZEPH', 'Zephyrine Inc.', 'US0000000302', 'United States', 'stock', "
                    "'common', true, 'isin:US0000000302', true)")
        con.execute("INSERT INTO market_daily (security_id, as_of, market_cap_usd, avg_volume_10d, snapshot_id) VALUES "
                    "('NASDAQ:QUIL', DATE '2026-09-26', 6e8, 1e6, ?), ('NASDAQ:ZEPH', DATE '2026-09-26', 2e10, 1e6, ?)",
                    [snap, snap])
        con.execute("INSERT INTO descriptions (security_id, source_id, company_key, text, snapshot_id) VALUES "
                    "('NASDAQ:QUIL', 'tradingview_profile', 'isin:US0000000301', 'Quillon sells hospital logistics "
                    "software.', ?), ('NASDAQ:ZEPH', 'tradingview_profile', 'isin:US0000000302', 'Zephyrine makes "
                    "kitchen appliances.', ?)", [snap, snap])
        con.execute("INSERT INTO documents (doc_id, security_id, company_key, source_id, cik, form, section, "
                    "filing_date, url, text_path, snapshot_id) VALUES ('sec_filing_text:301:q:item1', 'NASDAQ:QUIL', "
                    "'isin:US0000000301', 'sec_filing_text', '301', '10-K', 'item1', DATE '2026-02-01', "
                    "'https://www.sec.gov/quil.htm', ?, ?)", [str(doc), snap])


class AuthorCase(StoreCase):
    def setUp(self):
        super().setUp()
        seed_why(self.cfg, self.home)
        add_quillon(self.cfg, self.home)

    def main(self, argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), \
                mock.patch.object(screen, "_default_factory", make_factory(self.log)), \
                mock.patch.object(screen, "_default_keywords", self.kw), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def jmain(self, argv, stdin=None):
        code, out, err = self.main(argv + ["--json"], stdin)
        return code, json.loads(out) if out.strip() else None, err

    def draft(self, obj) -> str:
        p = self.home / "draft.json"
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def sieve(self, idea=IDEA):
        return calib.load_sieve(calib.sieve_path(self.cfg, idea))


class TestDraftsAndMerge(unittest.TestCase):
    def test_refused_fields(self):
        codes = {e["code"] for e in sieve_author.validate_draft(
            {"idea": IDEA, "include": ["X"], "examples": [], "rules": ["homonym"], "pins": [], "shiny": 1})}
        self.assertEqual(codes, {"pins_refused", "rules_refused", "unknown_field"})
        errs = sieve_author.validate_draft({"include": ["X"]})
        self.assertEqual(errs[0]["text_zh"], "钉选只能在你本人同意后加：jevscreen sieve pin …")
        self.assertEqual(sieve_author.validate_draft({"should_pass": [{"company": "A", "resolved": {}}]})[0]["code"],
                         "tool_field")
        self.assertTrue(sieve_author.validate_draft({"idea_en": "x" * 301}))
        self.assertEqual(sieve_author.validate_draft({"idea": IDEA, "idea_en": "Humanoid robots",
                                                      "seed_terms": {"zh": ["人形机器人"]},
                                                      "should_pass": [{"company": "NYSE:ROBO"}], "notes": "n"}), [])
        self.assertTrue(sieve_author.validate_draft({"should_pass": [{"company": "A"}],
                                                     "should_fail": [{"company": "a"}]}))    # both lists

    def test_merge_patch_and_diff(self):
        sv = {**calib.new_sieve(IDEA), "facets": {"category": "robots", "target": "warehouses"},
              "should_pass": [{"company": "ROBO", "resolved": {"security_id": "NYSE:ROBO", "company_key": "k1"}}]}
        sv2, diff = sieve_author.merge_author(sv, {"facets": {"mechanism": "picking", "target": None},
                                                    "should_pass": [{"company": "ROBO"}, {"company": "ROB2"}]})
        self.assertEqual(sv2["facets"], {"category": "robots", "mechanism": "picking"})
        self.assertEqual(sv2["should_pass"][0]["resolved"]["security_id"], "NYSE:ROBO")     # kept
        self.assertNotIn("resolved", sv2["should_pass"][1])
        self.assertEqual({d["field"] for d in diff}, {"facets", "should_pass"})
        self.assertEqual(next(d for d in diff if d["field"] == "should_pass")["added"], ["ROB2"])
        self.assertEqual(sv["facets"]["target"], "warehouses")                              # the input untouched

    def test_legacy_migration(self):
        sv = {**calib.new_sieve(IDEA), "examples": [
            {"security_id": "NYSE:ROBO", "company_key": "k1", "want": "explicit", "source": "sieve"},
            {"security_id": "NYSE:X", "want": "no", "source": "sieve"},
            {"security_id": "NYSE:Y", "want": "explicit", "source": "card", "pin": True}]}
        sv2, diff = sieve_author.merge_author(sv, {})
        self.assertEqual([e["resolved"]["method"] for e in sv2["should_pass"]], ["legacy"])
        self.assertEqual(sv2["should_fail"][0]["company"], "NYSE:X")
        self.assertEqual([e["security_id"] for e in sv2["examples"]], ["NYSE:Y"])
        self.assertEqual(diff[0], {"field": "examples", "migrated": 2})

    def test_idea_en_checks(self):
        sv = {**calib.new_sieve(ZH_IDEA), "should_pass": [
            {"company": "RoboCorp", "resolved": {"security_id": "NYSE:ROBO", "name": "RoboCorp Inc."}}]}
        errs = sieve_author.idea_en_errors("Humanoid robot makers such as RoboCorp", sv)
        self.assertEqual([e["word"] for e in errs], ["RoboCorp Inc."])
        self.assertEqual([e["word"] for e in sieve_author.idea_en_errors("Robots like ROBO for warehouses", sv)],
                         ["ROBO"])
        self.assertEqual(sieve_author.idea_en_errors("Humanoid robots for 2026 warehouses", sv), [])
        big = [("Zephyrine Inc.", "ZEPH"), ("Harmonic Inc.", "HLIT"), ("Block, Inc.", "XYZ")]
        w = sieve_author.idea_en_warnings("Zephyrine-grade humanoid robots with harmonic drives", sv, big)
        self.assertEqual([x["word"] for x in w], ["Zephyrine"])
        self.assertIn("也是一家公司的名字", w[0]["text_zh"])
        self.assertEqual(sieve_author.idea_en_warnings("Robots with ZEPH-level torque", sv, big)[0]["word"], "ZEPH")
        self.assertEqual(sieve_author.idea_en_warnings("robots zeph torque", sv, big), [])     # lower-case ticker
        self.assertEqual(sieve_author.idea_en_warnings("Zephyrine robots", {**sv, "seed_terms": {
            "en": ["zephyrine gearbox"]}}, big), [])                                          # the sieve's own term

    def test_author_keywords_conditions(self):
        sv = {**calib.new_sieve(ZH_IDEA), "idea_en": "Humanoid robot makers", "seed_terms": {"en": ["humanoid"]}}
        a = sieve_author.author_keywords(sv, ZH_IDEA)
        self.assertEqual((a["idea_en"], a["source"]), ("Humanoid robot makers", "sieve"))
        a = sieve_author.author_keywords(sv, "人形机器人产业链")                # another idea's sieve (--sieve PATH)
        self.assertIsNone(a["idea_en"])
        self.assertEqual(a["seed_terms"], {"en": ["humanoid"]})
        a = sieve_author.author_keywords({**sv, "idea": IDEA}, IDEA)            # an English idea
        self.assertIsNone(a["idea_en"])
        a = sieve_author.author_keywords(sv, ZH_IDEA, frozen=(True, "Humanoid robots"))
        self.assertEqual((a["idea_en"], a["source"], a["frozen"]), ("Humanoid robots", "frozen", True))  # kept
        self.assertIn("idea_en 已冻结", a["warnings"][0])
        a = sieve_author.author_keywords(sv, ZH_IDEA, frozen=(True, None))      # the paid run used the idea itself
        self.assertEqual((a["idea_en"], a["source"], a["frozen"]), (None, "frozen", True))
        # the human's yes approves exactly one idea_en text
        a = sieve_author.author_keywords({**sv, "idea_en_reprice": "Humanoid robot makers"}, ZH_IDEA,
                                         frozen=(True, "Humanoid robots"))
        self.assertEqual((a["idea_en"], a["source"]), ("Humanoid robot makers", "sieve"))
        for stale in ("Makers of robots", True):
            a = sieve_author.author_keywords({**sv, "idea_en_reprice": stale}, ZH_IDEA,
                                             frozen=(True, "Humanoid robots"))
            self.assertEqual((a["idea_en"], a["source"]), ("Humanoid robots", "frozen"))

    def test_idea_en_ticker_words_are_not_company_names(self):
        def sv_of(*ids, pin=None):
            s = {**calib.new_sieve(ZH_IDEA), "should_pass": [
                {"company": i.rpartition(":")[2], "resolved": {"security_id": i, "company_key": f"k:{i}",
                                                               "name": n}} for i, n in ids]}
            if pin:
                s["examples"] = [{"security_id": pin[0], "company_key": f"k:{pin[0]}", "name": pin[1],
                                  "want": "explicit", "source": "card", "pin": True}]
            return s
        sv = sv_of(("NYSE:A", "Agilent Technologies Inc."), ("NASDAQ:ON", "ON Semiconductor Corp"),
                   ("NYSE:IT", "Gartner Inc."), ("NYSE:NOW", "ServiceNow Inc."), ("NYSE:ALL", "Allstate Corp"))
        for text in ("Companies that sell a lab instrument platform based on AI", "Software that runs on edge devices",
                     "Firms that make it easy to insure all cars now", "Makers of A mass spectrometer",
                     "IT services that run ON premises NOW", "All-in-one ALL terrain robots"):
            self.assertEqual(sieve_author.idea_en_errors(text, sv), [], text)
        self.assertEqual(sieve_author.idea_en_errors("Makers of a mass spectrometer",
                                                     sv_of(pin=("NYSE:A", "Agilent Technologies Inc."))), [])
        # names are still errors, and so is a real all-caps ticker of 3+ letters
        self.assertEqual([e["word"] for e in sieve_author.idea_en_errors("Tools like Agilent Technologies", sv)],
                         ["Agilent Technologies Inc."])
        sv2 = sv_of(("NYSE:ROBO", "RoboCorp Inc."), ("NYSE:GE", "GE Aerospace"))
        self.assertEqual([e["word"] for e in sieve_author.idea_en_errors("Robots like ROBO", sv2)], ["ROBO"])
        self.assertEqual([e["word"] for e in sieve_author.idea_en_errors("Engines like GE makes", sv2)], ["GE"])
        self.assertEqual(sieve_author.idea_en_errors("robo advisers", sv2), [])


class TestLoadSaveAndScreen(AuthorCase):
    def write(self, sv, idea=IDEA):
        path = calib.sieve_path(self.cfg, idea)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sv, ensure_ascii=False), encoding="utf-8")
        return path

    def test_normaliser_round_trip(self):
        path = self.write({**calib.new_sieve(IDEA), "version": 1, "should_pass": [
            {"company": "QUIL", "want": "explicit",
             "resolved": {"security_id": "NASDAQ:QUIL", "company_key": "isin:US0000000301", "name": "Quillon"}}],
            "should_fail": [{"company": "BANK", "resolved": {"security_id": "NASDAQ:BANK",
                                                             "company_key": "isin:US0000000004"}}]})
        sv = calib.load_sieve(path)
        au = [e for e in sv["examples"] if e.get("_author")]
        self.assertEqual([(e["security_id"], e["want"], e["source"]) for e in au],
                         [("NASDAQ:QUIL", "explicit", "sieve"), ("NASDAQ:BANK", "no", "sieve")])
        self.assertEqual(len(calib.forced_examples(sv)), 2)
        self.assertEqual(calib.named_keys(sv) >= {"NASDAQ:QUIL", "isin:US0000000004"}, True)
        saved = calib.save_sieve(path, sv)
        self.assertNotIn("_author", path.read_text(encoding="utf-8"))
        self.assertEqual(len([e for e in saved["examples"] if e.get("_author")]), 2)
        # a card answer for the same company wins over the check (no second example)
        sv2 = {**calib.load_sieve(path), "examples": [{"security_id": "NASDAQ:QUIL", "company_key":
                                                       "isin:US0000000301", "want": "no", "source": "card",
                                                       "pin": True}]}
        self.write(sv2)
        sv3 = calib.load_sieve(path)
        self.assertEqual([e["source"] for e in sv3["examples"] if e["security_id"] == "NASDAQ:QUIL"], ["card"])
        self.assertTrue(sieve_author.conflicts(sv3))

    def test_should_pass_behaves_like_legacy_check(self):
        base = self.run_screen(reads=1, out_dir=self.home / "o0")
        path = self.write({**calib.new_sieve(IDEA), "version": 1, "should_pass": [
            {"company": "QUIL", "resolved": {"security_id": "NASDAQ:QUIL", "company_key": "isin:US0000000301",
                                             "name": "Quillon"}}]})
        res = self.run_screen(reads=1, sieve=str(path), out_dir=self.home / "o1")
        self.assertNotIn("NASDAQ:QUIL", [r["security_id"] for r in res["rows"] + res["unverified"]])
        check = res["calibration"]["checks"][0]
        self.assertEqual((check["security_id"], check["l1_pass"], check["l2_label"], check["in_output"]),
                         ("NASDAQ:QUIL", False, "explicit", False))
        # the same as a legacy source-'sieve' example (the rule-trial pool and card blocking read both alike)
        legacy = {**calib.new_sieve(IDEA), "examples": [{"security_id": "NASDAQ:QUIL", "company_key":
                                                         "isin:US0000000301", "want": "explicit", "source": "sieve"}]}
        reads = {"isin:US0000000301": {"security_id": "NASDAQ:QUIL", "p_pos": 0.9}}
        ins = {"isin:US0000000301": {"text": "t"}}
        self.assertEqual(calib._trial_pool(calib.load_sieve(path), reads, ins)[0],
                         calib._trial_pool(legacy, reads, ins)[0])
        self.assertIsNotNone(base)

    def test_idea_en_seed_terms_and_freeze(self):
        kw = FakeKeywords()
        path = self.write({**calib.new_sieve(ZH_IDEA), "version": 1, "idea_en": "Humanoid robot makers",
                           "seed_terms": {"en": ["humanoid robot", "biped"], "zh": ["人形机器人"]}}, ZH_IDEA)
        res = self.run_screen(ZH_IDEA, reads=1, dry_run=True, keywords_fn=kw, sieve="auto")
        self.assertEqual(kw.calls, [])                                  # the local model is not called
        self.assertEqual(res["idea_en"], "Humanoid robot makers")
        self.assertEqual(res["keywords"]["status"], "sieve")
        self.assertEqual(res["terms_by_lang"]["en"], ["humanoid robot", "biped"])
        self.assertEqual(res["params"]["idea_en_source"], "sieve")
        self.assertIn("Humanoid robot makers (original: 人形机器人)", res["questions"]["l1"]["instructions"])
        # a paid run with the model's idea_en freezes it: the sieve's differing idea_en is then ignored
        raw = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({k: v for k, v in raw.items() if k != "idea_en"}), encoding="utf-8")
        paid = self.run_screen(ZH_IDEA, reads=1, keywords_fn=kw, out_dir=self.home / "p", sieve="auto")
        self.assertEqual(paid["idea_en"], "Humanoid robots")
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        res = self.run_screen(ZH_IDEA, reads=1, dry_run=True, keywords_fn=kw, out_dir=self.home / "d2", sieve="auto")
        self.assertEqual(res["idea_en"], "Humanoid robots")
        self.assertTrue(any("idea_en 已冻结" in w for w in res["warnings"]))
        path.write_text(json.dumps({**raw, "idea_en_reprice": "Humanoid robot makers"}, ensure_ascii=False),
                        encoding="utf-8")
        res = self.run_screen(ZH_IDEA, reads=1, dry_run=True, keywords_fn=kw, out_dir=self.home / "d3", sieve="auto")
        self.assertEqual(res["idea_en"], "Humanoid robot makers")
        self.assertNotEqual(res["params"]["l2_question_sha"], paid["params"]["l2_question_sha"])
        self.assertTrue(any("比上次贵" in w for w in res["warnings"]))

    def test_freeze_keeps_the_paid_idea_en(self):
        # the paid run used the sieve's idea_en; the AI later rewrites it without --reprice: the next run must ask
        # exactly the same questions (every cached L1 / L2 answer is reused), with or without the local model
        path = self.write({**calib.new_sieve(ZH_IDEA), "version": 1, "idea_en": "Humanoid robot makers"}, ZH_IDEA)
        broken = FakeKeywords(error=RuntimeError("no local model"))
        paid = self.run_screen(ZH_IDEA, reads=1, keywords_fn=broken, out_dir=self.home / "p", sieve="auto")
        self.assertEqual((paid["idea_en"], paid["keywords"]["status"]), ("Humanoid robot makers", "sieve"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**raw, "idea_en": "Makers of humanoid robots"}, ensure_ascii=False),
                        encoding="utf-8")
        for i, kw in enumerate((broken, FakeKeywords())):
            res = self.run_screen(ZH_IDEA, reads=1, dry_run=True, keywords_fn=kw, out_dir=self.home / f"d{i}",
                                  sieve="auto")
            self.assertEqual(kw.calls if kw is not broken else [], [])          # the model is not asked
            self.assertEqual((res["idea_en"], res["keywords"]["status"]), ("Humanoid robot makers", "frozen"))
            self.assertEqual(res["questions"]["l1"], paid["questions"]["l1"])
            self.assertEqual(res["params"]["l2_question_sha"], paid["params"]["l2_question_sha"])
            self.assertTrue(any("idea_en 已冻结" in w for w in res["warnings"]))
            self.assertFalse(any("比上次贵" in w for w in res["warnings"]))
        # a paid run that used the idea itself (no idea_en) stays on the idea itself
        self.run_screen(ZH_IDEA, reads=1, keywords_fn=broken, out_dir=self.home / "p2", sieve="none")
        res = self.run_screen(ZH_IDEA, reads=1, dry_run=True, out_dir=self.home / "d3", sieve="auto")
        self.assertEqual((res["idea_en"], res["keywords"]["status"]), (None, "frozen"))
        self.assertIn('investment idea "人形机器人"', res["questions"]["l1"]["instructions"])

    def test_idea_en_naming_a_listed_company_stops_the_screen(self):
        self.write({**calib.new_sieve(ZH_IDEA), "version": 1, "idea_en": "Humanoid robots like Quillon Systems",
                    "should_pass": [{"company": "QUIL", "resolved": {"security_id": "NASDAQ:QUIL", "company_key":
                                                                    "isin:US0000000301", "name": "Quillon Systems"}}]},
                   ZH_IDEA)
        with self.assertRaises(ValueError) as cm:
            self.run_screen(ZH_IDEA, reads=1, dry_run=True, sieve="auto")
        self.assertIn("Quillon", str(cm.exception))
        self.assertEqual(self.log, [])

    def test_stale_save(self):
        path = self.write({**calib.new_sieve(IDEA), "version": 1})
        a, b = calib.load_sieve(path), calib.load_sieve(path)
        calib.save_sieve(path, a)
        with self.assertRaises(calib.SieveStale):
            calib.save_sieve(path, b)


class TestCommands(AuthorCase):
    def test_set_new_add_remove_list_check(self):
        run = self.run_screen(reads=1)
        code, body, _ = self.jmain(["sieve", "set", "--run", run["run_id"], "--from", self.draft({
            "idea": IDEA, "should_pass": [{"company": "QUIL", "why": "hospital robots"}],
            "should_fail": [{"company": "NASDAQ:BANK"}], "notes": "draft 1"})])
        self.assertEqual(code, 0, body)
        self.assertTrue(body["saved"])
        self.assertEqual(body["version"], 1)
        sv = self.sieve()
        self.assertEqual(sv["should_pass"][0]["resolved"]["security_id"], "NASDAQ:QUIL")
        self.assertEqual(sv["should_pass"][0]["resolved"]["method"], "symbol")
        # human text
        code, out, _ = self.main(["sieve", "add", "should_pass", "NYSE:ROBO", "--run", run["run_id"]])
        self.assertEqual(code, 0)
        self.assertIn("已更新筛子（版本 2）：应该有（检查） +1（RoboCorp NYSE:ROBO）", out)
        # a fuzzy name is refused on write, with candidates
        code, body, _ = self.jmain(["sieve", "add", "should_pass", "RoboCorps", "--run", run["run_id"]])
        self.assertEqual((code, body["status"]), (1, "unresolved"))
        self.assertEqual(body["candidates"][0]["security_id"], "NYSE:ROBO")
        code, body, _ = self.jmain(["sieve", "remove", "should_pass", "NYSE:ROBO", "--run", run["run_id"]])
        self.assertEqual((code, body["version"]), (0, 3))
        code, body, _ = self.jmain(["sieve", "list"])
        self.assertEqual(body["sieves"][0]["counts"]["should_pass"], 1)
        self.assertEqual(body["sieves"][0]["last_run"]["run_id"], run["run_id"])
        code, body, _ = self.jmain(["sieve", "check", "--run", run["run_id"]])
        self.assertEqual(code, 0, body)
        self.assertEqual({r["company"]: r["state"] for r in body["resolved"]}, {"QUIL": "ok", "NASDAQ:BANK": "ok"})
        self.assertIn("criteria", body["question"])
        # a draft with pins is refused and nothing is written
        code, body, _ = self.jmain(["sieve", "set", "--run", run["run_id"], "--from", self.draft({"include": ["X"]})])
        self.assertEqual((code, body["status"]), (1, "invalid_draft"))
        self.assertEqual(self.sieve()["version"], 3)
        # new on an existing sieve merges (never refuses)
        code, body, _ = self.jmain(["sieve", "new", "--run", run["run_id"], "--from", self.draft({"notes": "n2"})])
        self.assertEqual((code, body["existed"], body["version"]), (0, True, 4))

    def test_new_from_draft_idea_and_stdin(self):
        code, body, _ = self.jmain(["sieve", "new", "--from", "-"], stdin=json.dumps({"idea": ZH_IDEA,
                                                                                         "idea_en": "Humanoid robots"}))
        self.assertEqual(code, 0, body)
        self.assertEqual(self.sieve(ZH_IDEA)["idea_en"], "Humanoid robots")
        code, body, _ = self.jmain(["sieve", "set", "--idea", ZH_IDEA, "--from", self.draft(
            {"idea": "别的想法", "notes": "x"})])
        self.assertEqual((code, body["status"]), (1, "idea_mismatch"))
        code, body, _ = self.jmain(["sieve", "check", "--idea", "从没写过的想法"])
        self.assertEqual((code, body["status"]), (1, "no_sieve"))
        code, _out, err = self.main(["sieve", "check", "missing.json"])
        self.assertEqual(code, 1)

    def test_freeze_and_reprice_messages(self):
        self.run_screen(ZH_IDEA, reads=1)                                 # paid: idea_en 'Humanoid robots'
        code, body, _ = self.jmain(["sieve", "set", "--idea", ZH_IDEA, "--from", self.draft(
            {"idea_en": "Humanoid robot makers"})])
        self.assertEqual(code, 0)
        self.assertTrue(body["idea_en"]["frozen"] and body["idea_en"]["differs"])
        self.assertIsNotNone(body["idea_en"]["reprice"])
        self.assertIn("idea_en_frozen", [w["code"] for w in body["warnings"]])
        code, body, _ = self.jmain(["sieve", "set", "--idea", ZH_IDEA, "--reprice", "--from", self.draft({})])
        self.assertEqual(code, 0)
        self.assertTrue(self.sieve(ZH_IDEA)["idea_en_reprice"])
        self.assertNotIn("idea_en_frozen", [w["code"] for w in body["warnings"]])

    def test_reprice_is_bound_to_the_approved_idea_en(self):
        self.run_screen(ZH_IDEA, reads=1, out_dir=self.home / "p1")          # paid: idea_en 'Humanoid robots'
        code, body, _ = self.jmain(["sieve", "set", "--idea", ZH_IDEA, "--reprice", "--from", self.draft(
            {"idea": ZH_IDEA, "idea_en": "Humanoid robot makers"})])
        self.assertEqual(code, 0, body)
        self.assertEqual(self.sieve(ZH_IDEA)["idea_en_reprice"], "Humanoid robot makers")
        run2 = self.run_screen(ZH_IDEA, reads=1, out_dir=self.home / "p2", sieve="auto")
        self.assertEqual(run2["idea_en"], "Humanoid robot makers")
        # a later rewrite without --reprice (no human yes) is frozen again, with the warning
        code, body, _ = self.jmain(["sieve", "set", "--idea", ZH_IDEA, "--from", self.draft(
            {"idea_en": "Makers of bipedal humanoid robots"})])
        self.assertEqual(code, 0, body)
        self.assertIn("idea_en_frozen", [w["code"] for w in body["warnings"]])
        self.assertEqual(body["idea_en"]["effective"], "Humanoid robot makers")
        code, body, _ = self.jmain(["sieve", "check", "--idea", ZH_IDEA])
        self.assertIn("Humanoid robot makers (original", body["question"]["instructions"])
        run3 = self.run_screen(ZH_IDEA, reads=1, dry_run=True, out_dir=self.home / "d3", sieve="auto")
        self.assertEqual(run3["idea_en"], "Humanoid robot makers")
        self.assertEqual(run3["params"]["l2_question_sha"], run2["params"]["l2_question_sha"])
        # --reprice again approves the new text only
        code, body, _ = self.jmain(["sieve", "set", "--idea", ZH_IDEA, "--reprice", "--from", self.draft({})])
        self.assertEqual(self.sieve(ZH_IDEA)["idea_en_reprice"], "Makers of bipedal humanoid robots")
        run4 = self.run_screen(ZH_IDEA, reads=1, dry_run=True, out_dir=self.home / "d4", sieve="auto")
        self.assertEqual(run4["idea_en"], "Makers of bipedal humanoid robots")

    def test_checks_are_not_numbered_as_answers(self):
        run = self.run_screen(reads=1, out_dir=self.home / "screens" / "a")
        code, _out, _err = self.main(["sieve", "add", "should_pass", "NYSE:ROBO", "--run", run["run_id"]])
        self.assertEqual(code, 0)
        code, out, _ = self.main(["sieve", "show", IDEA])
        self.assertEqual(code, 0)
        self.assertIn("回答：（无）", out)
        self.assertIn("RoboCorp", out)                          # listed as a check, removable with sieve remove
        self.assertIn("sieve remove", out)
        code, out, _ = self.main(["sieve", "show", IDEA, "--json"])
        body = json.loads(out)
        self.assertEqual(body["examples"], [])                  # the file itself: no in-memory check examples
        self.assertEqual(body["should_pass"][0]["resolved"]["security_id"], "NYSE:ROBO")
        code, out, err = self.main(["answer", "--undo", "1", "--run", run["run_id"], "--no-apply"])
        self.assertEqual(code, 1, out + err)
        raw = json.loads(calib.sieve_path(self.cfg, IDEA).read_text(encoding="utf-8"))
        self.assertEqual(raw.get("history") or [], [])
        with self.assertRaises(calib.AnswerError):
            calib.undo_example(self.sieve(), 1)
        # with a real answer as well, N counts answers only
        sv = self.sieve()
        sv["examples"] = sieve_author.strip_author(sv["examples"]) + [
            {"security_id": "NASDAQ:BANK", "company_key": "isin:US0000000004", "name": "BankCo", "want": "no",
             "source": "card", "pin": True}]
        calib.save_sieve(calib.sieve_path(self.cfg, IDEA), sv)
        sv2, removed = calib.undo_example(self.sieve(), 1)
        self.assertEqual(removed["security_id"], "NASDAQ:BANK")
        self.assertFalse(any(h.get("_author") for h in sv2["history"]))

    def test_pin_and_unpin(self):
        run = self.run_screen(reads=1)
        code, body, _ = self.jmain(["sieve", "pin", "NASDAQ:QUIL", "yes", "--run", run["run_id"]])
        self.assertEqual(code, 0, body)
        self.assertTrue(body["ask_human"])
        ex = [e for e in self.sieve()["examples"] if not e.get("_author")]
        self.assertEqual((ex[0]["via"], ex[0]["want"], ex[0]["pin"]), ("pin", "explicit", True))
        res = self.run_screen(reads=1, from_run=run["run_id"], sieve="auto", out_dir=self.home / "o2")
        row = next(r for r in res["rows"] if r["security_id"] == "NASDAQ:QUIL")
        self.assertEqual((row["user_verdict"], row["user_pin_via"]), ("explicit", "pin"))
        md = (self.home / "o2" / "report.md").read_text(encoding="utf-8")
        self.assertIn("你让 AI 钉选的（", md)
        code, body, _ = self.jmain(["sieve", "unpin", "NASDAQ:QUIL", "--run", run["run_id"]])
        self.assertEqual(code, 0, body)
        self.assertEqual([e for e in self.sieve()["examples"] if not e.get("_author")], [])
        code, body, _ = self.jmain(["sieve", "unpin", "NASDAQ:QUIL", "--run", run["run_id"]])
        self.assertEqual((code, body["status"]), (1, "not_pinned"))


if __name__ == "__main__":
    unittest.main()
