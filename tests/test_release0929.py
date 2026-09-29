"""Release 0929: the novice #4 P0 / P1 fixes (docs: the novice verdict of 2026-09-29).

P0-1 the SEC question names the companies it helps (a thin-profile one first) and the page names the companies a thin
profile keeps out; P1-1 no budget question when only the reserve is over the cap; P1-2 the default fill stops once a
raised floor is covered; P1-3 the HUD says 'preparing' and what runs; P1-5 the results come first in the panel; P1-6
a FinanceDatabase text about another listed company is not imported; P1-7 the largest companies with no profile get
one before the first read. No network: every crawl is a fake.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_env  # noqa: E402,F401  (suite-wide network kill switch)

from jevscreen import fillgap, l10n, ondemand, page, page_hud, quickstart as qs, store  # noqa: E402
from jevscreen.sources import financedatabase_local as fd  # noqa: E402
from test_novice2_fixes import DefaultFillCase  # noqa: E402
from test_novice_flow import CN_ROWS, add_cn  # noqa: E402
from test_quickstart import IDEA, QuickCase  # noqa: E402


# ------------------------------------------------------------------------------------------------ P1-1 budget

class TestReserveIsNotAQuestion(QuickCase):
    def dry(self, est, reserved):
        return mock.patch.object(qs.Worker, "dry_run", lambda w, **kw: {
            "est_cost_usd": est, "est_reserved_usd": reserved, "est_seconds": 60, "companies": 5, "l1_usd": 0.2})

    def test_estimate_under_the_cap_runs_even_when_the_reserve_is_over(self):
        """Novice #4: estimate $0.257, reserve $0.305, approval $0.30: no second budget round."""
        self.consent_yes()
        self.set_key()
        with self.dry(0.257, 0.305):
            self.front(approve_budget=0.3)
            self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertFalse(any(i["id"] == "approve_budget" for i in out["pending"]))
        l1 = [f for f in self.calls.factory if f["layer"] == "l1" and not f["dry_run"]]
        self.assertLessEqual(l1[0]["budget_usd"], 0.3)                  # the cap stays hard

    def test_an_estimate_over_the_cap_asks_with_enough_digits(self):
        self.consent_yes()
        self.set_key()
        with self.dry(0.3049, 0.4):
            self.front(approve_budget=0.3)
            self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        b = next(i for i in out["pending"] if i["id"] == "approve_budget")
        self.assertEqual(b["kind"], "over")
        self.assertIn("$0.305", b["question_zh"])                        # never '$0.30 超过 $0.30'
        self.assertIn("$0.30", b["question_zh"])
        self.assertNotIn("预留", b["question_zh"])


# ------------------------------------------------------------------------------------------------ P1-2 fill floor

class TestFillFloorCovered(QuickCase):
    def test_floor_covered_only_when_the_floor_rose_and_nothing_above_it_is_missing(self):
        add_cn(self.cfg, CN_ROWS)             # 300901 $5.4B has no profile; 300904 $3.0B has one
        self.assertFalse(fillgap.floor_covered(self.cfg, "CN", 1e9, 1e9))        # the floor did not rise
        self.assertFalse(fillgap.floor_covered(self.cfg, "CN", 1e9, 5e9))        # 300901 still missing
        self.assertTrue(fillgap.floor_covered(self.cfg, "CN", 1e9, 6e9))         # nothing at $6B is missing
        self.assertFalse(fillgap.floor_covered(self.cfg, "CN", None, 6e9))


class TestFillStopsAtARaisedFloor(DefaultFillCase):
    def test_a_raised_floor_that_is_covered_ends_the_wait(self):
        procs = self.live_fill(ending=True)
        idea, key = self.IDEA_ZH, qs.idea_key(self.IDEA_ZH)
        self.consent_yes("zh")
        self.set_key()
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        orig_merge = qs.Worker.merge_inbox
        sent = []

        def merge(w):
            if w.job.get("current_step") == "screen" and not sent \
                    and (w.job.get("fill_default") or {}).get("state") == "running":
                sent.append(qs.front(self.cfg, idea, min_mcap=6e9, spawn=lambda c, k: None)["status"])
            return orig_merge(w)
        with self.with_cn(), mock.patch.object(qs.Worker, "merge_inbox", merge), \
                mock.patch.object(qs, "FILL_JOIN_MIN_S", 600):
            self.work(key)
        self.assertEqual(sent, ["running"])
        fdf = self.job(idea)["fill_default"]
        self.assertEqual((fdf["state"], fdf["reason"]), ("done", "floor"), fdf)
        self.assertLess(fdf["seconds"], 120)                    # not the 10-minute cap
        self.assertTrue(all(p.poll() is not None for p in procs))


# ------------------------------------------------------------------------------------------------ P1-3 / P1-5 HUD

class TestHudSaysWhatRuns(unittest.TestCase):
    def data(self, lang, state="run", active=None, items=()):
        return {"lang": lang, "live": {"sand": {"state": state, "active": active}, "phase_words": "AI 正在筛选",
                                       "progress": {"items": list(items)}, "hud": {}}}

    def test_a_running_job_without_an_active_sieve_is_preparing_not_waiting(self):
        fill = {"id": "fill_default", "label": "补缺简介的公司（TradingView，免费）", "state": "run",
                "text": "412 / 1,111"}
        inst = page_hud.instrument(self.data("zh", items=[fill]))
        self.assertEqual(inst["stage"], "准备 · 0 / 3")
        self.assertIn("412 / 1,111", inst["doing"])
        self.assertEqual(page_hud.instrument(self.data("en"))["stage"], "Prep · 0 / 3")
        self.assertEqual(page_hud.instrument(self.data("zh", state="wait"))["stage"], "等待")
        self.assertEqual(page_hud.instrument(self.data("zh", state="wait"))["doing"], "")
        self.assertEqual(page_hud.instrument(self.data("zh", active=2))["stage"], "2 / 3")

    def test_the_doing_line_is_in_the_markup(self):
        fill = {"label": "补缺简介的公司", "state": "run", "text": "3 / 9"}
        html = page_hud.markup(self.data("zh", items=[fill]))
        self.assertIn('id="hud-doing"', html)
        self.assertIn("补缺简介的公司 · 3 / 9", html)
        self.assertNotIn('id="hud-doing"', page_hud.markup(self.data("zh", state="done")))

    def test_the_results_come_first_in_the_panel(self):
        css = page_hud.CSS
        self.assertIn(page_hud.P + ">section.results{order:-2}", css)
        self.assertIn(page_hud.P + "{display:flex;flex-direction:column}", css)
        self.assertIn(page_hud.P + ">.hud-ph{order:-3}", css)


# ------------------------------------------------------------------------------------------------ P1-6 wrong text

class TestAnotherCompanysText(unittest.TestCase):
    IES = ("IES Holdings, Inc. designs and installs integrated electrical and technology systems for data centers "
           "and industrial facilities in the United States.")

    def secs(self):
        return [{"security_id": "NYSE:CF", "name": "CF Industries Holdings, Inc.", "isin": None,
                 "company_key": "k:cf"},
                {"security_id": "NASDAQ:IESC", "name": "IES Holdings, Inc.", "isin": None, "company_key": "k:ies"},
                {"security_id": "NYSE:LH", "name": "Labcorp Holdings Inc.", "isin": None, "company_key": "k:lh"}]

    def row(self, lid, ticker, ex, name, summary):
        return {"listing_id": lid, "ticker": ticker, "exchange": ex, "name": name, "id_isin": None,
                "summary": summary}

    def test_a_text_about_another_listed_company_is_not_imported(self):
        rows = [self.row("CF", "CF", "NYQ", "CF Industries Holdings, Inc.", self.IES),
                self.row("LH", "LH", "NYQ", "Labcorp Holdings Inc.",
                         "Laboratory Corporation of America Holdings provides laboratory services worldwide.")]
        rep: dict = {}
        got = {m.security_id for m in fd.match(rows, self.secs(), report=rep)}
        self.assertNotIn("NYSE:CF", got)
        self.assertIn("NYSE:LH", got)                     # a renamed company's old text is kept
        self.assertIn("summary_subject", {r["reason"] for r in rep["name_rejections"]})

    def test_subject_words(self):
        self.assertEqual(fd.summary_subject(self.IES), "IES Holdings")
        self.assertIsNone(fd.summary_subject("The Company operates stores."))
        self.assertFalse(fd.subject_mismatch("Generac Holdings Inc. designs generators.", "Generac Holdings Inc."))
        self.assertTrue(fd.subject_mismatch(self.IES, "CF Industries Holdings, Inc."))


# ------------------------------------------------------------------------------------------------ P1-7 top fill

class TestTopGapFill(QuickCase):
    def worker(self, **job):
        w = mock.Mock()
        w.cfg, w.job, w.mu = self.cfg, {"min_mcap_usd": 1e9, "countries": None, **job}, mock.MagicMock()
        w.d = qs.Deps()
        return w

    def test_the_largest_missing_profiles_are_crawled_once(self):
        self.consent_yes()
        calls = []
        w = self.worker()
        w.d.crawl_client = lambda cfg: mock.Mock(requests_made=0)
        w.d.top_crawl = lambda cfg, client, **kw: calls.append(kw) or {"status": "ok", "ok": 3}
        fillgap.top_fill_once(w)
        fillgap.top_fill_once(w)                                  # once per job
        self.assertEqual(calls, [{"countries": None, "min_mcap_usd": 1e9, "limit": fillgap.TOP_FILL_N}])
        self.assertEqual(w.job["top_fill"]["status"], "ok")

    def test_never_after_the_opt_out_and_refused_in_tests_without_a_fake(self):
        w = self.worker(fill_pref="no")
        w.d.top_crawl = mock.Mock()
        fillgap.top_fill_once(w)
        w.d.top_crawl.assert_not_called()
        self.assertNotIn("top_fill", w.job)
        w = self.worker(countries=["KR"])
        fillgap.top_fill_once(w)
        self.assertEqual(w.job["top_fill"]["status"], "testing_refused")

    def test_the_worker_runs_it_before_the_first_read(self):
        order = []
        self.deps.crawl_client = lambda cfg: mock.Mock(requests_made=0)
        self.deps.top_crawl = lambda cfg, client, **kw: order.append("top") or {"status": "ok", "ok": 0}
        orig = qs.Worker.step_screen

        def screen(w):
            order.append("screen")
            return orig(w)
        with mock.patch.object(qs.Worker, "step_screen", screen):
            out = self.done_flow()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(order[:2], ["top", "screen"])
        self.assertEqual(order.count("top"), 1)


# ------------------------------------------------------------------------------------------------ P0-1 thin + SEC

class TestThinProfilesAreNamed(unittest.TestCase):
    def plan(self, entries):
        return ondemand.Plan(run_id="scr-1", entries=entries, readiness={"sec_email_ask": None})

    def entry(self, sid, name, mcap, reason, rescued):
        return {"company_key": sid, "security_id": sid, "name": name, "country": "United States",
                "market_cap_usd": mcap, "reason": reason, "rescued": rescued}

    def entries(self):
        return {e["company_key"]: e for e in (
            self.entry("NYSE:GEV", "GE Vernova Inc.", 2.5e11, "no_key_sec", True),
            self.entry("NYSE:GNRC", "Generac Holdings Inc.", 9e9, "no_key_sec", False),
            self.entry("NYSE:CAT", "Caterpillar Inc.", 1.8e11, "no_key_sec", False))}

    def test_the_sec_question_names_the_thin_company_first(self):
        pl = self.plan(self.entries())
        [q] = ondemand.questions_for(pl, {"no_key_sec": 3})
        self.assertIn("GE Vernova", q["human_question_zh"])
        self.assertIn("简介太薄", q["human_question_zh"])
        self.assertTrue(q["human_question_zh"].endswith("要设置吗？"))
        self.assertIn("GE Vernova, Caterpillar, Generac", q["human_question_en"])
        self.assertEqual(q["thin"], ["NYSE:GEV"])

    def test_a_thin_company_alone_still_asks(self):
        e = self.entries()
        pl = self.plan({"NYSE:GEV": e["NYSE:GEV"]})
        [q] = ondemand.questions_for(pl, {"no_key_sec": 1})           # the rescued one is subtracted, still asked
        self.assertEqual(q["id"], "sec_email")
        pl.readiness["sec_email_ask"] = "no"
        self.assertEqual(ondemand.questions_for(pl, {"no_key_sec": 1}), [])   # never after a recorded no

    def test_thin_waiting_and_the_page_line(self):
        pl = self.plan(self.entries())
        oc = {"NYSE:GEV": {"code": "no_key_sec"}, "NYSE:GNRC": {"code": "no_key_sec"}}
        tw = ondemand.thin_waiting(pl, oc)
        self.assertEqual([t["security_id"] for t in tw], ["NYSE:GEV"])
        lines = page._gap_lines({"gaps": {}}, {}, {"ondemand": {"thin_waiting": tw}, "sec_declined": True})
        self.assertEqual(lines[0]["id"], "thin_waiting")
        self.assertIn("GE Vernova", lines[0]["text_zh"])
        self.assertIn("你选择了不提供", lines[0]["text_zh"])
        self.assertIn("too thin", lines[0]["text_en"])
        fetched = ondemand.thin_waiting(pl, {"NYSE:GEV": {"code": "fetched"}})
        self.assertEqual(fetched, [])

    def test_the_question_stays_for_a_thin_us_company_outside_the_top(self):
        q = {"id": "sec_email", "human_question_zh": "问？", "human_question_en": "Q?", "thin": ["NYSE:GEV"]}
        got = qs._retarget_fetch({"questions": [q]}, "scr-2", [{"country": "China"}])
        self.assertEqual([x["id"] for x in got["questions"]], ["sec_email"])
        q2 = {**q, "thin": []}
        got = qs._retarget_fetch({"questions": [q2]}, "scr-2", [{"country": "China"}])
        self.assertEqual(got["questions"], [])


class TestZhScopeWords(unittest.TestCase):
    def test_iso_codes_read_as_chinese(self):
        self.assertEqual(l10n.iso2_words_zh(["CN", "HK"]), "中国大陆、香港")


if __name__ == "__main__":
    unittest.main()
