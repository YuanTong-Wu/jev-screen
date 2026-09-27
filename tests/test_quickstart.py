"""Tests for `jevscreen quickstart` (jevscreen.quickstart / quickstart_cli): the fast front command, the worker (run
in-process with fakes), the job file, the one decision round, money, idea_en, cooldowns, concurrency and secrets.

No network and no paid calls: the scanner, the FinanceDatabase download, the key check and the canary are fakes
(quickstart.Deps); Jev is test_screen.FakeJev. The store is a temp DuckDB with invented companies. One test starts
the real detached worker on a store where every step it reaches is free and offline.
"""
from __future__ import annotations

import bz2
import contextlib
import csv
import datetime as dt
import getpass
import io
import json
import re
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, cli, config, consent, guard, keys, quickstart as qs, review_cli, screen, store  # noqa: E402,E501
from jevscreen.http import Blocked  # noqa: E402
from jevscreen.sources import financedatabase_local as fd  # noqa: E402
from test_screen import FakeJev, FakeKeywords, seed  # noqa: E402

IDEA = "humanoid robots"
FAKE_KEY = "sk-or-v1-" + "fake" * 12
FAKE_UA = "Test Person tester@example.test"
CLEAR_ENV = ("OPENROUTER_API_KEY", "JEVSCREEN_OPENROUTER_KEY_FILE", "JEVSCREEN_SEC_USER_AGENT", "CI",
             "JEVSCREEN_PACK_REPO", "JEVSCREEN_EDINET_API_KEY", "JEVSCREEN_OPENDART_API_KEY", "TYPESAFE_API_KEY",
             "JEVSCREEN_TYPESAFE_KEY_FILE", "AI_GATEWAY_API_KEY", "JEVSCREEN_VERCEL_KEY_FILE", "JEVSCREEN_JEV_PROVIDER")


def fd_bz2(path: Path, rows: list[dict]) -> Path:
    cols = ["symbol", "name", "summary", "currency", "sector", "industry_group", "industry", "exchange", "mic",
            "market", "country", "state", "city", "zipcode", "website", "market_cap", "isin", "cusip", "figi",
            "composite_figi", "shareclass_figi", "delisted"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    path.write_bytes(bz2.compress(buf.getvalue().encode("utf-8")))
    return path


FD_ROWS = [
    {"symbol": "ROBO", "name": "RoboCorp", "exchange": "NYQ", "isin": "US0000000001", "delisted": "False",
     "summary": "RoboCorp designs and sells humanoid robots for warehouses and factories worldwide."},
    {"symbol": "6000.T", "name": "ServoJP", "exchange": "JPX", "isin": "JP0000000002", "delisted": "False",
     "summary": "ServoJP manufactures servo motors and drives for factory automation equipment."},
    {"symbol": "GEAR.L", "name": "GearCo", "exchange": "LSE", "isin": "GB0000000003", "delisted": "False",
     "summary": "GearCo makes gearbox units for trucks, buses and agricultural machines."},
    {"symbol": "BANK", "name": "BankCo", "exchange": "NMS", "isin": "US0000000004", "delisted": "False",
     "summary": "BankCo offers retail banking, mortgages and payment cards to households."},
    {"symbol": "ROB2", "name": "Robo Two", "exchange": "NMS", "isin": "US0000000007", "delisted": "False",
     "summary": "Robo Two provides motion control software and robot controllers for integrators."},
]


class FakeRefresh:
    """Stands in for tradingview_scanner.refresh_universe: seeds the invented universe (today's market date)."""

    def __init__(self, home: Path, *, descriptions: bool = True, block: bool = False):
        self.home, self.descriptions, self.block, self.calls = home, descriptions, block, 0

    def __call__(self, cfg, con, client):
        self.calls += 1
        if self.block:
            raise Blocked("https://scanner.tradingview.com/global/scan", 403, "http_403")
        seed(cfg, self.home)
        con.execute("UPDATE market_daily SET as_of = current_date")
        if not self.descriptions:
            con.execute("DELETE FROM descriptions")
        return {"status": "ok", "rows": 7}


class Calls:
    def __init__(self):
        self.download = self.canary = self.check = self.open = 0
        self.factory: list = []


class QuickCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = config.Config(home=self.home).ensure()
        env = mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home), "LANG": "en_US.UTF-8"})
        env.start()
        self.addCleanup(env.stop)
        for k in CLEAR_ENV:
            os.environ.pop(k, None)
        self.calls = Calls()
        self.log: list = []
        self.refresh = FakeRefresh(self.home, descriptions=False)
        self.bz2 = fd_bz2(self.home / "equities.bz2", FD_ROWS)
        self.spawned: list[str] = []
        self.deps = qs.Deps(jev_factory=self.factory, keywords_fn=FakeKeywords(),
                            scanner_client=lambda cfg: mock.Mock(requests_made=0), refresh_universe=self.refresh,
                            fd_client=lambda cfg: mock.Mock(requests_made=0), download_equities=self.download,
                            check_jev=self.check_jev, canary=self.canary, fetch_docs=self.fetch_docs,
                            open_page=self.open_page)
        self.canary_result = {"status": "ok", "cost_usd": 0.00005}
        self.download_result = None
        self.fetch_calls: list = []
        self.fetch_impl = None          # (cfg, result, kw) -> run_fetch-like dict; None = nothing arrived

    def tearDown(self):
        self._tmp.cleanup()

    # -- fakes
    def factory(self, cfg, *, run_id, layer, budget_usd, dry_run, **kw):
        self.calls.factory.append({"layer": layer, "budget_usd": budget_usd, "dry_run": dry_run, **kw})
        return FakeJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=self.log)

    def download(self, cfg, client):
        self.calls.download += 1
        return self.download_result or {"status": "ok", "path": str(self.bz2)}

    def check_jev(self, cfg):
        self.calls.check += 1
        return {"status": "ok"}

    def canary(self, cfg):
        self.calls.canary += 1
        return dict(self.canary_result)

    def open_page(self, path):
        self.calls.open += 1
        return True

    def fetch_docs(self, cfg, result, **kw):
        """Stands in for ondemand_cli.run_fetch (the one on-demand path; tested in test_ondemand)."""
        self.fetch_calls.append({"run_id": result["run_id"], **kw})
        if kw.get("on_progress"):
            kw["on_progress"](0, 2)
            kw["on_progress"](2, 2)
        if self.fetch_impl is not None:
            return self.fetch_impl(cfg, result, kw)
        return {"result": result, "update": {"skipped": "nothing_fetched"},
                "fetch": {"status": "ok", "fetched": {}, "seconds": 1.0, "update": {"skipped": "nothing_fetched"},
                          "questions": [], "next_command": None, "summary_zh": "没有新年报",
                          "summary_en": "nothing new"}}

    # -- helpers
    def consent_yes(self, lang="en"):
        consent.record(self.cfg, "gray-sources", "yes", lang=lang)

    def set_key(self):
        keys.write_key(self.cfg, "openrouter", FAKE_KEY)

    def front(self, idea=IDEA, **kw):
        kw.setdefault("spawn", lambda cfg, key: self.spawned.append(key))
        return qs.front(self.cfg, idea, **kw)

    # what the simulated agent does with the review deck of the first result (scope design §7): 'skip'
    # (review_cli.judge --skip: the system's list stands, the flow goes on as before) or 'keep' (left pending: the
    # review tests drive it themselves)
    review = "skip"

    def work(self, key=None):
        key = key or qs.idea_key(IDEA)
        code = qs.worker(self.cfg, key, self.deps)
        ar = ((qs.load_job(self.cfg, key) or {}).get("agent_review") or {})
        if self.review == "skip" and ar.get("state") == "pending":
            review_cli.judge(self.cfg, ar["deck_id"], skip=True)
        return code

    def job(self, idea=IDEA):
        return qs.load_job(self.cfg, qs.idea_key(idea))

    def ids(self, out):
        return [i["id"] for i in out["pending"]]

    def l1_classified(self):
        return [c for c in self.log if c.layer == "l1" and c.classified]

    def done_flow(self, idea=IDEA, **kw):
        self.consent_yes()
        self.set_key()
        out = self.front(idea, approve_budget=1, **kw)
        self.assertEqual(out["status"], "running", out)
        self.work(qs.idea_key(idea))
        return qs.status(self.cfg, qs.idea_key(idea))


@contextlib.contextmanager
def no_network():
    boom = AssertionError("network used")
    with mock.patch("urllib.request.urlopen", side_effect=boom), \
            mock.patch("jevscreen.http.Client.request", side_effect=boom), \
            mock.patch("socket.create_connection", side_effect=boom):
        yield


class TestFront(QuickCase):
    def test_no_consent_asks_one_round_before_any_network(self):
        t = time.monotonic()
        with no_network():
            out = self.front()
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual((out["status"], out["exit_code"]), ("needs_human", 10))
        self.assertEqual(self.ids(out), ["consent_gray_sources", "approve_budget", "key_jev"])
        self.assertIn("OpenRouter", out["before_you_start_en"])
        self.assertIn("AI", out["intro_en"])
        self.assertIn("约 $0.3", out["intro_zh"])
        c = out["pending"][0]
        self.assertEqual(c["statement_version"], consent.STATEMENT_VERSION)
        self.assertEqual(c["record_answer_commands"], ["jevscreen consent set gray-sources yes --lang en",
                                                       "jevscreen consent set gray-sources no --lang en"])
        b = out["pending"][1]
        self.assertIn("'humanoid robots'", b["question_en"])
        self.assertEqual(b["rerun_with"], "--approve-budget 1")
        k = out["pending"][2]
        self.assertIsNone(k["provider"])                                  # no key yet: which account?
        self.assertEqual([c["provider"] for c in k["choices"]], ["typesafe", "openrouter", "vercel"])
        self.assertEqual(k["choices"][1]["agent_try"], "jevscreen keys set openrouter --dialog")
        self.assertIn("keys set openrouter", k["choices"][1]["human_command"])
        for name in ("TypeSafe", "OpenRouter", "Vercel"):
            self.assertIn(name, k["text_en"])
            self.assertIn(name, out["before_you_start_en"])
        self.assertEqual(self.spawned, [])
        self.assertNotIn("--approve-budget", out["next_command"])
        self.assertNotIn("consent", out["next_command"])

    def test_non_english_idea_needs_the_agent_first(self):
        with no_network():
            out = self.front("人形机器人的减速器")
        self.assertEqual((out["status"], out["exit_code"]), ("needs_agent", 11))
        self.assertEqual(self.ids(out)[0], "idea_en")
        self.assertIn("consent_gray_sources", self.ids(out))            # the human items are listed as well
        self.assertEqual(out["lang"], "zh")
        self.assertIn("--idea-en", out["pending"][0]["instructions_en"])
        out = self.front("人形机器人的减速器", idea_en="Suppliers of speed reducers for humanoid robots")
        self.assertEqual(out["status"], "needs_human")
        self.assertIn("Suppliers of speed reducers", out["pending"][1]["question_zh"])
        self.assertEqual(self.job("人形机器人的减速器")["idea_en_source"], "agent")

    def test_declined_stops_without_a_next_command(self):
        consent.record(self.cfg, "gray-sources", "no")
        out = self.front()
        self.assertEqual((out["status"], out["exit_code"], out["next_command"]), ("declined", 12, None))
        self.assertIn("I agree to use these sources", out["text_en"])
        self.assertEqual(self.spawned, [])

    def test_consent_v1_is_asked_again(self):
        consent.record(self.cfg, "gray-sources", "yes")
        data = json.loads(consent.consent_path(self.cfg).read_text())
        data["topics"]["gray-sources"].pop("statement_version")
        consent.consent_path(self.cfg).write_text(json.dumps(data))
        self.assertIn("consent_gray_sources", self.ids(self.front()))

    def test_shell_quoting_of_every_printed_command(self):
        idea = "robots $HOME `id` !x 'q' \"d\""
        out = self.front(idea, idea_en="Robots for home use", approve_budget=1)
        self.assertEqual(shlex.split(out["next_command"])[2], idea)
        argv = shlex.split(out["next_command"])
        self.assertEqual(argv[argv.index("--idea-en") + 1], "Robots for home use")
        self.assertEqual(argv[argv.index("--approve-budget") + 1], "1")

    def test_idea_en_with_a_company_name_is_refused(self):
        self.consent_yes()
        store_seed(self.cfg, self.home)
        out = self.front("人形机器人", idea_en="Companies like RoboCorp that build humanoid robots")
        self.assertEqual(out["status"], "needs_agent")
        self.assertTrue(any("RoboCorp" in p for p in out["idea_en_problems"]), out["idea_en_problems"])


    def test_idea_en_with_a_company_name_is_refused_on_a_first_run(self):
        """Run 1: no universe when the agent writes idea_en; the worker checks it once the list is downloaded."""
        idea = "人形机器人"
        bad = "Companies like RoboCorp that build humanoid robots"
        out = self.front(idea, idea_en=bad)
        self.assertFalse(out.get("idea_en_problems"))            # nothing to check against yet
        self.consent_yes()
        self.set_key()
        self.front(idea, approve_budget=1)
        self.work(qs.idea_key(idea))
        job = self.job(idea)
        self.assertEqual((job["state"], job["waiting_on"], job["idea_en"]), ("waiting", "idea_en", None))
        self.assertFalse(any(not f["dry_run"] for f in self.calls.factory))    # nothing sent to Jev
        self.assertEqual(self.calls.canary, 0)
        out = qs.status(self.cfg, qs.idea_key(idea), spawn=lambda cfg, key: self.spawned.append(key))
        self.assertEqual((out["status"], out["exit_code"]), ("needs_agent", 11))
        self.assertTrue(any("RoboCorp" in p for p in out["idea_en_problems"]), out.get("idea_en_problems"))
        self.assertIn("RoboCorp", out["pending"][0]["instructions_en"])
        self.assertNotIn("--idea-en", out["next_command"])
        # the approval the human gave is held for a fix of the refused sentence (P0-2): the command keeps it
        self.assertIn("--approve-budget 1", out["next_command"])
        self.assertEqual(out["suggested_idea_en"], "Companies that build humanoid robots")
        self.assertIn("--approve-budget 1", out["rerun_command"])
        self.assertIn("'Companies that build humanoid robots'", out["rerun_command"])
        # a sentence that says something else is the reprice question, not a silent new approval
        out = self.front(idea, idea_en="Makers of humanoid robots", approve_budget=1)
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(self.ids(out), ["reprice_idea_en"])
        item = out["pending"][0]
        self.assertTrue(item["old_refused"])
        self.assertIsNone(item["decline_with"])
        self.assertIn("Makers of humanoid robots", item["question_en"])
        self.assertIn("RoboCorp", item["question_zh"])
        out = self.front(idea, idea_en="Makers of humanoid robots", approve_budget=1)    # after the human's yes
        self.assertEqual(out["status"], "running")
        self.assertTrue(qs.approval_valid(self.job(idea)))

    def test_screen_refuses_an_idea_en_that_names_a_company(self):
        store_seed(self.cfg, self.home)
        with self.assertRaisesRegex(ValueError, "RoboCorp"):
            screen.screen(self.cfg, "humanoid robots", idea_en="Companies like RoboCorp", jev_factory=self.factory,
                          out_dir=self.home / "x", keywords_fn=FakeKeywords())
        self.assertEqual(self.calls.factory, [])
        self.assertFalse((self.home / "x").exists())


def store_seed(cfg, home):
    seed(cfg, home)
    with store.session(cfg) as con:
        con.execute("UPDATE market_daily SET as_of = current_date")


class TestWorker(QuickCase):
    def test_free_steps_run_while_the_key_is_missing(self):
        self.consent_yes()
        out = self.front()
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(self.ids(out), ["approve_budget", "key_jev"])
        self.assertEqual(self.spawned, [qs.idea_key(IDEA)])
        self.work()
        self.assertEqual((self.refresh.calls, self.calls.download), (1, 1))
        job = self.job()
        self.assertEqual((job["state"], job["waiting_on"]), ("waiting", "key_jev"))
        self.assertEqual([s["id"] for s in job["steps"]], ["check", "universe", "descriptions", "pack", "idea_en"])
        with store.session(self.cfg, read_only=True) as con:
            kinds = [r[0] for r in con.execute("SELECT DISTINCT n.kind FROM descriptions d JOIN snapshots n "
                                               "USING (snapshot_id)").fetchall()]
            cmds = {r[0] for r in con.execute("SELECT command FROM runs").fetchall()}
        self.assertEqual(kinds, ["fd_summaries_bz2"])
        self.assertTrue({"fetch-fd", "import-fd"} <= cmds, cmds)
        self.assertEqual(self.calls.canary, 0)
        # nothing to do until the human acts: no second worker
        out = self.front()
        self.assertEqual((out["status"], len(self.spawned)), ("needs_human", 1))

    def test_full_run_then_reuse(self):
        out = self.done_flow()
        self.assertEqual((out["status"], out["exit_code"]), ("done", 0), out.get("text_en"))
        self.assertTrue(Path(out["page"]).exists())
        self.assertTrue(out["page_opened"])
        self.assertEqual([r["name"] for r in out["top"]], ["RoboCorp", "Robo Two"])
        self.assertEqual(out["summary"]["listed"], 2)
        self.assertEqual(len(out["next_steps"]), 2)        # cards are not a human step (scope design §11)
        self.assertNotIn("borderline companies", " ".join(n["text_en"] for n in out["next_steps"]))
        self.assertIn("RoboCorp", out["text_en"])
        self.assertEqual(self.calls.canary, 1)
        self.assertLess(out["spent_usd"], 1.0)
        self.assertIsNone(out["poll_command"])
        runs = len(self.l1_classified())
        # the same idea again: the finished result, $0, no new run, nothing reopened
        again = self.front()
        self.assertEqual((again["status"], again["run_id"]), ("done", out["run_id"]))
        self.assertEqual((len(self.spawned), len(self.l1_classified()), self.calls.open), (1, runs, 1))
        self.assertEqual(self.job()["steps"][-1]["id"], "finish")
        t = {s["id"] for s in self.job()["steps"]}
        self.assertEqual(t, set(qs.STEPS))

    def test_canary_402_stops_before_any_screen_spend(self):
        self.canary_result = {"status": 402, "cost_usd": 0.0, "error": "HTTP 402: no credit"}
        out = self.done_flow()
        self.assertEqual((out["status"], out["exit_code"]), ("ai_unavailable", 6))
        self.assertIn("no credit", out["text_en"])
        self.assertEqual(self.l1_classified(), [])
        self.assertFalse(any(not f["dry_run"] for f in self.calls.factory))
        # the human topped up and said "done": the front reruns the canary
        self.canary_result = {"status": "ok", "cost_usd": 0.00005}
        out = self.front(approve_budget=1)
        self.assertEqual(out["status"], "running")
        self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA))["status"], "done")

    def test_401_waits_for_a_new_key(self):
        self.canary_result = {"status": 401, "cost_usd": 0.0}
        out = self.done_flow()
        self.assertEqual(out["status"], "ai_unavailable")
        self.assertEqual(self.ids(out), ["key_jev"])
        self.assertTrue(out["pending"][0]["rejected"])
        n = len(self.spawned)
        self.assertEqual(self.front()["status"], "ai_unavailable")          # same key: nothing restarts
        self.assertEqual(len(self.spawned), n)
        time.sleep(0.01)
        keys.write_key(self.cfg, "openrouter", FAKE_KEY[:-4] + "zzzz")
        self.assertEqual(self.front()["status"], "running")


class TestOnDemandFetch(QuickCase):
    """The fetch step: quickstart calls the one on-demand path (ondemand_cli.run_fetch, faked here) after the
    screen, with its time budget and progress, and never fails the job over it."""

    def test_the_fetch_step_runs_after_the_screen_with_time_budget_and_progress(self):
        seen = []
        orig = qs.Worker.progress

        def spy(w, phase, zh, en, done=None, total=None, force=False, **kw):
            seen.append(en)
            return orig(w, phase, zh, en, done, total, force, **kw)
        with mock.patch.object(qs.Worker, "progress", spy):
            out = self.done_flow()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(len(self.fetch_calls), 1)
        call = self.fetch_calls[0]
        self.assertEqual(call["run_id"], out["run_id"])
        self.assertEqual(call["time_s"], qs.FETCH_DOCS_S)
        self.assertGreater(call["update_budget"], 0)
        self.assertLessEqual(call["update_budget"], 0.05)
        self.assertIn("[4/5] Fetching annual reports 2/2 (at most 150 s)…", seen)
        steps = [x["id"] for x in self.job()["steps"]]
        self.assertLess(steps.index("screen"), steps.index("fetch"))
        self.assertLess(steps.index("fetch"), steps.index("finish"))
        self.assertEqual(out["fetch"]["status"], "ok")

    def test_an_update_pass_counts_against_the_approval(self):
        def upd(cfg, result, kw):
            new = dict(result, run_id=result["run_id"] + "-u", supersedes=result["run_id"], cost_usd=0.002)
            return {"result": new, "update": {"run_id": new["run_id"], "status": "ok", "cost_usd": 0.002,
                                              "written": True},
                    "fetch": {"status": "ok", "fetched": {"sec": 1}, "seconds": 3.0,
                              "update": {"run_id": new["run_id"], "status": "ok", "cost_usd": 0.002}}}
        self.fetch_impl = upd
        out = self.done_flow()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        base = self.fetch_calls[0]["run_id"]
        costs = self.job()["approval"]["costs"]
        self.assertEqual(costs[base + "-u"], 0.002)
        self.assertAlmostEqual(out["cost_usd"], round(costs[base] + 0.002, 6))
        self.assertEqual(out["fetch"]["fetched"], {"sec": 1})

    def test_a_failing_fetch_is_a_note_not_a_failed_job(self):
        def boom(cfg, result, kw):
            raise RuntimeError("adapter exploded")
        self.fetch_impl = boom
        out = self.done_flow()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertTrue(any("adapter exploded" in n for n in out["notes"]), out["notes"])
        self.assertEqual(qs.step_of(self.job(), "fetch")["status"], "skipped")


class TestMoney(QuickCase):
    def dry(self, est, reserved):
        return mock.patch.object(qs.Worker, "dry_run", lambda w, **kw: {
            "est_cost_usd": est, "est_reserved_usd": reserved, "est_seconds": 60, "companies": 5, "l1_usd": 0.2})

    def test_screen_budget_is_the_remaining_approval(self):
        self.consent_yes()
        self.set_key()
        with self.dry(1.4, 1.45):
            self.front(approve_budget=1.5)
            self.work()
        l1 = [f for f in self.calls.factory if f["layer"] == "l1" and not f["dry_run"]]
        self.assertAlmostEqual(l1[0]["budget_usd"], 1.5 - 0.00005, places=6)
        st = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual(st["status"], "done")
        self.assertNotIn("skipped_budget", json.dumps(self.job()))

    def scoped_dry(self):
        """The whole universe needs $2.00 reserved; only US needs $0.50; a $5B floor still $1.50."""
        def dry(w, **kw):
            r = 0.5 if kw.get("countries") == ["US"] else 1.5 if (kw.get("min_mcap") or 0) >= 5e9 else 2.0
            return {"est_cost_usd": r * 0.8, "est_reserved_usd": r, "est_seconds": 60, "companies": 5,
                    "l1_usd": 0.2}
        return mock.patch.object(qs.Worker, "dry_run", dry)

    def test_an_alternative_alone_reestimates_and_over_scopes_are_not_offered(self):
        """Picking a narrower scope (its `flags` only, without --approve-budget) re-estimates under the approval
        instead of asking a '$0.00 reservation' question; a scope that still does not fit is never offered."""
        self.consent_yes()
        self.set_key()
        with self.scoped_dry(), mock.patch.object(qs, "COUNTRY_WORDS", {"US": ["humanoid"]}):
            self.front(approve_budget=1)
            self.work()
            out = qs.status(self.cfg, qs.idea_key(IDEA))
            b = out["pending"][0]
            self.assertEqual(b["kind"], "over")
            self.assertEqual([a["flags"] for a in b["alternatives"]], ["--countries US"])
            n = len(self.spawned)
            out = self.front(countries=["US"])
            self.assertEqual(out["status"], "running", out["text_en"])
            self.assertEqual(len(self.spawned), n + 1)
            self.assertNotIn("$0.00", json.dumps(out))
            self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA))["status"], "done")

    def test_over_the_approval_offers_narrower_scopes(self):
        self.consent_yes()
        self.set_key()
        with self.scoped_dry(), mock.patch.object(qs, "COUNTRY_WORDS", {"US": ["humanoid"]}):
            self.front(approve_budget=1)
            self.work()
            out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual((out["status"], self.ids(out)), ("needs_human", ["approve_budget"]))
        b = out["pending"][0]
        self.assertEqual(b["kind"], "over")
        self.assertTrue(b["alternatives"])
        self.assertIn("--approve-budget 2.", b["rerun_with"])
        self.assertIn("$2.00", b["question_en"])
        self.assertFalse((self.home / "screens").exists() and any((self.home / "screens").iterdir()))
        with self.scoped_dry():
            self.assertEqual(self.front(approve_budget=2.2)["status"], "running")
            self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA))["status"], "done")

    def test_a_new_run_gets_only_the_remainder(self):
        out = self.done_flow()
        spent = out["spent_usd"]
        self.assertGreater(spent, 0.01)
        out = self.front(approve_budget=1, new_run=True)
        self.assertEqual(out["status"], "running")
        self.work()
        l1 = [f for f in self.calls.factory if f["layer"] == "l1" and not f["dry_run"]]
        self.assertAlmostEqual(l1[-1]["budget_usd"], round(1 - spent, 6), places=5)
        self.assertEqual(len(self.job()["approval"]["out_dirs"]), 2)

    def test_budget_exhausted_asks_a_top_up(self):
        self.consent_yes()
        self.set_key()

        def small_packs(cfg, *, run_id, layer, budget_usd, dry_run, **kw):
            self.calls.factory.append({"layer": layer, "budget_usd": budget_usd, "dry_run": dry_run, **kw})
            return FakeJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=self.log,
                           pack_size=2)
        self.deps.jev_factory = small_packs
        with self.dry(0.01, 0.01):
            self.front(approve_budget=0.025)
            self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual((out["status"], out["exit_code"]), ("budget_exhausted", 5))
        self.assertTrue(Path(out["page"]).exists())                 # the partial page is written
        self.assertIn("ran out", out["text_en"])
        # the top-up question is a real pending item, and a larger total continues the run (review R2)
        self.assertEqual(self.ids(out), ["approve_budget"])
        b = out["pending"][0]
        self.assertEqual(b["kind"], "topup")
        total = float(b["rerun_with"].split()[-1])
        self.assertGreater(total, 0.025)
        self.assertIn(f"${total:.2f}", b["question_en"])
        self.assertTrue(out["next_command"])
        n, first_run = len(self.spawned), out["run_id"]
        again = self.front()                                         # no new approval: the same result, no run
        self.assertEqual((again["status"], again["run_id"], len(self.spawned)), ("budget_exhausted", first_run, n))
        self.deps.jev_factory = self.factory
        out = self.front(approve_budget=1.0)
        self.assertEqual((out["status"], len(self.spawned)), ("running", n + 1))
        self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual(out["status"], "done", out["text_en"])
        self.assertNotEqual(out["run_id"], first_run)
        self.assertEqual(len(self.job()["approval"]["out_dirs"]), 2)

    def test_the_reprice_question_can_be_declined(self):
        """No to a new English sentence: rerunning with the pinned sentence (next_command) clears the question and
        the job goes on with the old one; the question names the budget cap and rerun_with carries it."""
        self.consent_yes()
        self.front("人形机器人", idea_en="Humanoid robots", approve_budget=1)
        self.work(qs.idea_key("人形机器人"))                         # pins it (waits for the key)
        out = self.front("人形机器人", idea_en="Makers of humanoid robots")
        self.set_key()
        out = qs.status(self.cfg, qs.idea_key("人形机器人"))
        self.assertEqual(self.ids(out), ["reprice_idea_en"])
        r = out["pending"][0]
        self.assertIn("$1", r["question_en"])
        self.assertIn("$1", r["question_zh"])
        self.assertEqual(r["rerun_with"], "--idea-en 'Makers of humanoid robots' --approve-budget 1")
        self.assertEqual(r["decline_with"], "--idea-en 'Humanoid robots'")
        n = len(self.spawned)
        self.assertEqual(self.ids(self.front("人形机器人")), ["reprice_idea_en"])     # no decision yet
        out = self.front("人形机器人", idea_en="Humanoid robots", approve_budget=1)  # next_command: a no
        self.assertEqual((out["status"], self.ids(out)), ("running", []))
        self.assertEqual(len(self.spawned), n + 1)
        job = self.job("人形机器人")
        self.assertEqual((job["idea_en"], job["reprice"]), ("Humanoid robots", None))
        self.assertEqual(job["approval"]["idea_en_sha"], qs.sha12("Humanoid robots"))

    def test_a_new_idea_en_after_the_result_is_asked_not_stored_silently(self):
        idea = "人形机器人"
        self.done_flow(idea, idea_en="Humanoid robots")
        out = self.front(idea, idea_en="Makers of humanoid robots")
        self.assertEqual((out["status"], self.ids(out)), ("needs_human", ["reprice_idea_en"]))
        self.assertIsNotNone(out["run_id"])                          # the finished result is still shown
        self.assertIn("Makers of humanoid robots", out["text_en"])
        self.assertNotIn("download the stock list", out["text_en"])  # no first-run intro for a finished job
        # a no: the old sentence again, then a new run with it works
        out = self.front(idea, idea_en="Humanoid robots", approve_budget=1, new_run=True)
        self.assertEqual(out["status"], "running", out["text_en"])
        self.assertIsNone(self.job(idea)["reprice"])

    def test_the_intro_names_only_what_is_still_needed(self):
        idea = "人形机器人的减速器"
        out = self.front(idea)
        self.assertEqual(out["status"], "needs_agent")
        out = self.front(idea, idea_en="Humanoid robot reducers")
        self.assertEqual(self.ids(out), ["consent_gray_sources", "approve_budget", "key_jev"])
        self.assertIn("两件事", out["intro_zh"])
        self.assertIn("Two decisions and one key", out["intro_en"])
        self.consent_yes()
        out = self.front(idea, idea_en="Humanoid robot reducers", approve_budget=1)
        self.assertEqual(self.ids(out), ["key_jev"])
        for k in ("intro_zh", "text_zh"):
            self.assertNotIn("两件事", out[k])
            self.assertIn("key", out[k])
        for k in ("intro_en", "text_en"):
            self.assertNotIn("decision", out[k])
            self.assertIn("One key is still needed", out[k])
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertNotIn("两件事", out["text_zh"])

    def test_a_new_idea_en_voids_the_approval(self):
        self.consent_yes()
        self.front("人形机器人", idea_en="Humanoid robots", approve_budget=1)
        self.assertIsNotNone(self.job("人形机器人")["approval"])
        self.work(qs.idea_key("人形机器人"))                         # pins it (waits for the key)
        out = self.front("人形机器人", idea_en="Humanoid robot makers")
        self.assertIn("reprice_idea_en", self.ids(out))
        self.assertEqual(self.job("人形机器人")["idea_en"], "Humanoid robots")
        out = self.front("人形机器人", idea_en="Humanoid robot makers", approve_budget=1)
        job = self.job("人形机器人")
        self.assertEqual((job["idea_en"], job["reprice"]), ("Humanoid robot makers", None))
        self.assertEqual(job["approval"]["idea_en_sha"], qs.sha12("Humanoid robot makers"))
        self.assertNotIn("reprice_idea_en", self.ids(out))

    def test_uncertain_items_small_are_resent_large_are_asked(self):
        out = self.done_flow()
        run_id = out["run_id"]
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO jev_items (item_key, request_id, run_id, layer, position, status, created_at) "
                        "VALUES ('k1', 'r1', ?, 'l1', 0, 'uncertain', current_timestamp)", [run_id])
        self.front(approve_budget=1, new_run=True)
        self.work()
        l1 = [f for f in self.calls.factory if f["layer"] == "l1" and not f["dry_run"]]
        self.assertTrue(l1[-1].get("retry_uncertain"))
        with mock.patch.object(qs, "UNCERTAIN_ITEM_USD", 0.05):
            self.front(approve_budget=1, new_run=True)
            self.work()
            out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual(out["pending"][0]["kind"], "uncertain")


class TestDocsMatchTheCode(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_networked_steps_named_in_the_docs_are_the_run_networked_ones(self):
        """The quickstart docstring and DATA_RULES name exactly the commands quickstart runs through
        ops.run_networked; annual reports come only from the fetch step (ondemand children), and README says
        quickstart downloads equities.bz2."""
        src = (self.ROOT / "src" / "jevscreen" / "quickstart.py").read_text(encoding="utf-8")
        code = set(re.findall(r'ops\.run_networked\(\s*"([a-z-]+)"', src))
        self.assertEqual(code, {"refresh-universe", "fetch-fd", "import-fd", "crawl-descriptions"})
        for name, text in (("quickstart.py", qs.__doc__ or ""),
                           ("DATA_RULES.md", (self.ROOT / "docs" / "DATA_RULES.md").read_text(encoding="utf-8"))):
            flat = " ".join(text.split())
            m = re.search(r"under the standard command names \(([^)]*)\)", flat)
            self.assertIsNotNone(m, name)
            named = set(re.findall(r"[a-z]+(?:-[a-z]+)+", m.group(1)))
            self.assertEqual(named, code, name)
        readme = " ".join((self.ROOT / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("equities.bz2", readme)
        self.assertIn("jsDelivr", readme)


class TestIdeaEnAndDrift(QuickCase):
    def test_pinned_idea_en_and_from_run_inherits_it(self):
        idea = "人形机器人"
        out = self.done_flow(idea, idea_en="Humanoid robots")
        self.assertEqual(out["status"], "done")
        res = json.loads((Path(self.job(idea)["result"]["output_dir"]) / "results.json").read_text())
        self.assertEqual((res["idea_en"], res["keywords"]["status"]), ("Humanoid robots", "agent"))
        n_l1 = len(self.l1_classified())
        again = screen.screen(self.cfg, idea, from_run=res["run_id"], jev_factory=self.factory,
                              out_dir=self.home / "apply", keywords_fn=FakeKeywords())
        self.assertEqual((again["idea_en"], again["keywords"]["status"]), ("Humanoid robots", "agent"))
        self.assertEqual(len(self.l1_classified()), n_l1)                 # L1 $0: loaded from the base run
        with self.assertRaisesRegex(ValueError, "different idea_en"):
            screen.screen(self.cfg, idea, from_run=res["run_id"], idea_en="Robot arms", jev_factory=self.factory,
                          out_dir=self.home / "apply2")
        # the same idea_en again: nothing new is read (the Jev cache answers it), and the job is reused
        self.assertEqual(self.front(idea, idea_en="Humanoid robots")["status"], "done")


    def set_sieve_idea_en(self, idea, text):
        path = calib.sieve_path(self.cfg, idea)
        sv = calib.load_sieve(path) or calib.new_sieve(idea)
        sv["idea_en"] = text
        calib.save_sieve(path, sv)

    def test_a_sieve_idea_en_added_after_the_result_is_asked_not_ignored(self):
        """spec 6.2: --idea-en > sieve.idea_en > the job's pin (review: the pin used to win silently)."""
        idea = "人形机器人"
        self.done_flow(idea, idea_en="Humanoid robots")
        new = "Makers of joints and reducers for humanoid robots"
        self.set_sieve_idea_en(idea, new)
        n = len(self.spawned)
        out = self.front(idea)
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(self.ids(out), ["reprice_idea_en"])
        r = out["pending"][0]
        self.assertEqual((r["old_idea_en"], r["new_idea_en"]), ("Humanoid robots", new))
        self.assertEqual(len(self.spawned), n)                          # nothing screens with the old English
        out = self.front(idea, idea_en=new, approve_budget=1)
        self.assertEqual(out["status"], "running")
        self.work(qs.idea_key(idea))
        job = self.job(idea)
        self.assertEqual(job["state"], "done")
        res = json.loads((Path(job["result"]["output_dir"]) / "results.json").read_text())
        self.assertEqual(res["idea_en"], new)

    def test_a_sieve_idea_en_replaces_an_unapproved_pin_at_once(self):
        self.front(IDEA)                                                # pinned verbatim, nothing approved
        self.assertEqual(self.job()["idea_en_source"], "verbatim")
        self.set_sieve_idea_en(IDEA, "Companies that build humanoid robots")
        out = self.front(IDEA)
        self.assertEqual((out["idea_en"], out["idea_en_source"]), ("Companies that build humanoid robots", "sieve"))
        self.assertIn("Companies that build humanoid robots", out["pending"][1]["question_en"])


class TestCooldownAndBackoff(QuickCase):
    def test_a_scanner_block_stops_and_the_next_front_makes_no_request(self):
        self.consent_yes()
        self.refresh.block = True
        self.front()
        self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual((out["status"], out["exit_code"]), ("blocked", 2))
        self.assertTrue(out["retry_after"])
        self.assertIn("24 hours", out["text_en"])
        self.assertTrue((self.home / "cooldown" / "refresh-universe.json").exists())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli._cooldown_refusal("refresh-universe", self.cfg, False), 2)   # the plain command too
        n = len(self.spawned)
        out = self.front()
        self.assertEqual((out["status"], len(self.spawned), self.refresh.calls), ("blocked", n, 1))
        self.assertNotIn("--after-block", json.dumps(out))

    def test_a_download_failure_is_not_retried_within_15_min(self):
        self.consent_yes()
        self.download_result = {"status": "error", "errors": ["raw.githubusercontent.com: URLError"]}
        self.front()
        self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual((out["status"], out["exit_code"]), ("failed", 1))
        self.assertIn("--fd-file", out["text_en"])
        n = len(self.spawned)
        self.assertEqual((self.front()["status"], len(self.spawned)), ("failed", n))
        out = self.front(retry=True)
        self.assertEqual(len(self.spawned), n + 1)
        self.download_result = None
        self.work()
        self.assertEqual(self.calls.download, 2)
        # --fd-file imports a manual download without any request
        out = self.front(fd_file=str(self.bz2))
        self.work()
        self.assertEqual(self.calls.download, 2)


    def test_a_blocked_profile_download_reports_the_real_cooldown_and_fd_file_still_works(self):
        """A block of both mirrors: kind 'blocked' with the 24 h retry_after (not '15 minutes, --retry'), both
        download URLs in the text, and --fd-file continues without any request (review R6)."""
        self.consent_yes()

        def blocked(cfg, client):
            self.calls.download += 1
            raise Blocked(fd.FD_MIRRORS[1], 403, "http_403")
        self.deps.download_equities = blocked
        self.front()
        self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual((out["status"], out["exit_code"]), ("blocked", 2))
        ra = qs.parse_iso(out["retry_after"])
        self.assertGreater(ra - qs.now_utc(), dt.timedelta(hours=23))
        self.assertNotIn("--retry", out["next_command"] or "")
        self.assertNotIn("15 min", out["text_en"])
        for u in fd.FD_MIRRORS:
            self.assertIn(u, out["text_en"])
            self.assertIn(u, out["text_zh"])
        self.assertLess(out["text_zh"].index(fd.FD_MIRRORS[1]), out["text_zh"].index(fd.FD_MIRRORS[0]))
        n = len(self.spawned)
        self.assertEqual((self.front(retry=True)["status"], len(self.spawned)), ("blocked", n))  # no request
        out = self.front(fd_file=str(self.bz2))
        self.assertEqual(len(self.spawned), n + 1)                     # the free steps run again at once
        self.assertEqual(self.ids(out), ["approve_budget", "key_jev"])
        self.work()
        self.assertEqual(self.calls.download, 1)
        self.assertTrue(qs.step_done(self.job(), "descriptions"))


class TestConcurrency(QuickCase):
    def test_two_fronts_one_worker_and_a_stale_heartbeat_respawns(self):
        self.consent_yes()
        self.set_key()
        self.front(approve_budget=1)
        out = self.front(approve_budget=1)
        self.assertEqual((out["status"], len(self.spawned)), ("running", 1))
        job = self.job()
        job["worker"]["heartbeat_at"] = qs.iso(qs.now_utc() - dt.timedelta(seconds=120))
        qs.save_job(self.cfg, job)
        self.assertEqual(qs.status(self.cfg, job["idea_key"])["status"], "failed")     # interrupted: rerun
        out = self.front()
        self.assertEqual((out["status"], len(self.spawned)), ("running", 2))
        self.assertTrue(any("without finishing" in n for n in self.job()["notes"]))

    def test_flags_reach_a_running_worker_through_the_inbox(self):
        self.consent_yes()
        self.set_key()
        self.front()
        with guard.budget_lock(self.cfg, qs.LOCK):                 # the worker holds the lock
            job = self.job()
            job["state"] = "running"
            qs.save_job(self.cfg, job)
            out = self.front(approve_budget=1)
            self.assertEqual(out["status"], "running")
            self.assertTrue(qs.inbox_path(self.cfg, qs.idea_key(IDEA)).exists())
            self.assertIsNone(self.job()["approval"])              # only the lock holder writes the job
        self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA))["status"], "done")

    def test_sigterm_mid_screen_is_journaled_interrupted(self):
        self.consent_yes()
        self.set_key()
        self.front(approve_budget=1)

        def boom(self_, items, question):
            raise cli.Terminated("SIGTERM")
        with mock.patch.object(FakeJev, "classify", boom):
            self.work()
        self.assertEqual(self.job()["state"], "interrupted")
        with store.session(self.cfg, read_only=True) as con:
            st = con.execute("SELECT status FROM screen_runs").fetchall()
        self.assertEqual(st, [("interrupted",)])
        out = self.front()
        self.assertEqual(out["status"], "running")                 # the next front resumes
        self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA))["status"], "done")


class TestFailureTexts(QuickCase):
    """The human reads a sentence in their language, never a raw English error inside a Chinese one (review R8)."""

    def failed(self, reason, error, **kw):
        job = qs.new_job("人形机器人", lang="zh", min_mcap=1e9, countries=None)
        job.update(state="failed", idea_en="Humanoid robots",
                   failure={"kind": "failed", "reason": reason, "error": error, **kw})
        return qs.response(self.cfg, job, [])

    def test_known_failures_have_sentences(self):
        for reason, error in (("duckdb", "duckdb is not installed"),
                              ("consent", "consent for gray-private sources is missing"),
                              ("canary", "the test request failed (unavailable)"),
                              ("results", "results not readable: locked"),
                              ("screen_args", "unknown country token 'XX'")):
            out = self.failed(reason, error)
            self.assertNotIn(error, out["text_zh"], reason)
            self.assertNotIn(error, out["text_en"], reason)
            self.assertEqual(out["error"], error)                    # the agent still gets the detail

    def test_an_unknown_failure_is_framed_for_the_agent(self):
        out = self.failed(None, "RuntimeError: boom")
        self.assertFalse(out["text_zh"].startswith("出错了："))
        self.assertIn("重跑", out["text_zh"])

    def test_the_worker_records_the_reason(self):
        self.consent_yes()
        self.set_key()
        self.canary_result = {"status": "unavailable", "cost_usd": 0.0}
        out = self.done_flow()
        self.assertEqual(self.job()["failure"]["reason"], "canary")
        self.assertNotIn("the test request failed", out["text_zh"])


class TestStablePage(QuickCase):
    """<home>/pages/<idea_key>.html always holds the newest run of the idea; a reused result points at a page of
    its own run (review R7)."""

    def plain_screen(self, idea=IDEA, **kw):
        from jevscreen import quickstart_cli
        res = screen.screen(self.cfg, idea, max_out=1, reads=1, translate=False, jev_factory=self.factory,
                            keywords_fn=FakeKeywords(), **kw)
        with contextlib.redirect_stderr(io.StringIO()):
            quickstart_cli.write_after_cards(self.cfg, res, None)      # what `jevscreen screen` does
        return res

    def run_id_in(self, path):
        return json.loads(Path(path).read_text().split('id="data">', 1)[1].split("</script>", 1)[0])["run_id"]

    def test_a_reused_result_never_shows_another_runs_page(self):
        a = self.done_flow()
        b = self.plain_screen()
        stable = Path(a["page"])
        self.assertEqual(self.run_id_in(stable), b["run_id"])          # the newest run of the idea
        again = self.front()
        # one stable page per idea, always the newest run (P0-4): quickstart shows what that page shows
        self.assertEqual((again["status"], again["run_id"]), ("done", b["run_id"]))
        self.assertEqual(Path(again["page"]), stable)
        self.assertEqual(self.run_id_in(again["page"]), b["run_id"])
        self.assertEqual(Path(again["page_uri"][len("file://"):]).resolve(), Path(again["page"]).resolve())

    def test_an_older_run_never_replaces_the_newest_page(self):
        a = self.done_flow()
        time.sleep(1.1)                                  # started_at has second resolution
        b = self.plain_screen()
        stable = Path(a["page"])
        for argv in (["page", a["run_id"], "--json"], ["cards", a["run_id"]]):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                cli.main(argv)
            self.assertEqual(self.run_id_in(stable), b["run_id"], argv)
            if argv[0] == "page":
                self.assertIsNone(json.loads(out.getvalue())["stable_page"])
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["page", b["run_id"], "--json"])
        self.assertEqual(json.loads(out.getvalue())["stable_page"], str(stable))

    def test_pages_rebuilt_outside_quickstart_keep_the_jobs_language(self):
        idea = "人形机器人"
        out = self.done_flow(idea, idea_en="Humanoid robots", lang="en")
        head = lambda p: Path(p).read_text()[:120]                  # noqa: E731
        self.assertIn('<html lang="en">', head(out["page"]))
        b = self.plain_screen(idea, idea_en="Humanoid robots")
        self.assertIn('<html lang="en">', head(Path(b["output_dir"]) / "page.html"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["page", b["run_id"], "--json"])
        self.assertIn('<html lang="en">', head(Path(b["output_dir"]) / "page.html"))


WORKER_CHILD = r"""
import sys, time
from pathlib import Path
from unittest import mock
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from jevscreen import config, quickstart as qs
from test_quickstart import FakeRefresh
from test_screen import FakeJev, FakeKeywords
home, key, bz = Path(sys.argv[3]), sys.argv[4], sys.argv[5]
cfg = config.Config(home=home).ensure()

class SlowJev(FakeJev):
    def classify(self, items, question):
        print("screening", flush=True)
        time.sleep(60)                         # the SIGTERM arrives here
        return super().classify(items, question)

deps = qs.Deps(jev_factory=lambda cfg, *, run_id, layer, budget_usd, dry_run, **kw: SlowJev(
                   cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run),
               keywords_fn=FakeKeywords(), scanner_client=lambda c: mock.Mock(requests_made=0),
               refresh_universe=FakeRefresh(home, descriptions=False), fd_client=lambda c: mock.Mock(requests_made=0),
               download_equities=lambda c, cl: {"status": "ok", "path": bz}, check_jev=lambda c: {"status": "ok"},
               canary=lambda c: {"status": "ok", "cost_usd": 0.00005},
               fetch_docs=lambda c, res, **kw: {"result": res, "update": None, "fetch": None},
               open_page=lambda p: True)
sys.exit(qs.worker(cfg, key, deps))
"""


class TestRealSignal(QuickCase):
    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_a_real_sigterm_mid_screen_is_journaled_interrupted_and_resumes(self):
        """The worker process gets a real SIGTERM while Jev is answering: the job and the screen run are both
        'interrupted', and the next front resumes to done."""
        import signal
        import subprocess
        self.consent_yes()
        self.set_key()
        self.front(approve_budget=1)
        tests = str(Path(__file__).resolve().parent)
        src = str(Path(__file__).resolve().parents[1] / "src")
        p = subprocess.Popen([sys.executable, "-c", WORKER_CHILD, tests, src, str(self.home), qs.idea_key(IDEA),
                              str(self.bz2)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "JEVSCREEN_HOME": str(self.home)})
        try:
            line = p.stdout.readline()
            self.assertEqual(line.strip(), "screening", p.stderr.read() if not line else line)
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(self.job()["state"], "interrupted")
        with store.session(self.cfg, read_only=True) as con:
            st = con.execute("SELECT status FROM screen_runs").fetchall()
        self.assertEqual(st, [("interrupted",)])
        out = self.front()
        self.assertEqual(out["status"], "running")
        self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA))["status"], "done")


class TestSafety(QuickCase):
    def test_never_prompts(self):
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), \
                mock.patch.object(getpass, "getpass", side_effect=AssertionError("prompted")):
            out = self.done_flow()
        self.assertEqual(out["status"], "done")

    def test_no_secret_anywhere(self):
        os.environ["JEVSCREEN_SEC_USER_AGENT"] = FAKE_UA
        err, outb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(outb):
            out = self.done_flow()
            with mock.patch.object(qs, "spawn_worker", lambda cfg, key: None):
                cli.main(["quickstart", IDEA, "--json"])
                cli.main(["quickstart", "--status", "--key", qs.idea_key(IDEA)])
        texts = [err.getvalue(), outb.getvalue(), json.dumps(out)]
        job = self.job()
        texts.append(qs.job_path(self.cfg, job["idea_key"]).read_text())
        od = Path(job["result"]["output_dir"])
        texts += [(od / n).read_text() for n in ("results.json", "page.html")]
        with store.session(self.cfg, read_only=True) as con:
            texts += [str(r) for r in con.execute("SELECT note FROM runs").fetchall()]
        for t in texts:
            for secret in (FAKE_KEY, FAKE_UA, "tester@example.test"):
                self.assertNotIn(secret, t)

    def test_the_job_file_refuses_a_secret(self):
        self.set_key()
        with self.assertRaises(qs.JobSecret):
            self.front(f"robots {FAKE_KEY}")

    def test_dry_runs_leave_nothing_under_screens(self):
        self.consent_yes()
        self.set_key()
        self.front(approve_budget=1)
        with mock.patch.object(qs.Worker, "step_screen", lambda w: w.wait("approve_budget", "topup")):
            self.work()
        self.assertTrue(qs.step_done(self.job(), "estimate"))
        self.assertFalse((self.home / "screens").exists() and any((self.home / "screens").iterdir()))


class TestCli(QuickCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(qs, "spawn_worker", lambda cfg, key: self.spawned.append(key)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_commands_are_registered(self):
        from jevscreen import quickstart_cli
        self.assertEqual(set(quickstart_cli.COMMANDS), {"quickstart", "page"})
        self.assertLessEqual(set(quickstart_cli.COMMANDS), set(cli.COMMANDS))
        a = cli.build_parser().parse_args(["quickstart", IDEA, "--approve-budget", "1", "--countries", "CN,HK"])
        self.assertEqual((a.approve_budget, a.countries, a.lang), (1.0, ["CN", "HK"], "auto"))

    def test_json_exit_codes_and_usage_error(self):
        code, out, _ = self.run_cli(["quickstart", IDEA, "--json"])
        data = json.loads(out)
        self.assertEqual((code, data["exit_code"], data["status"]), (10, 10, "needs_human"))
        code, out, err = self.run_cli(["quickstart", IDEA, "--bogus"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")                                     # a typo, not a block: no JSON
        code, out, _ = self.run_cli(["quickstart", "--status", "--key", qs.idea_key(IDEA), "--json"])
        self.assertEqual((code, json.loads(out)["status"]), (10, "needs_human"))
        code, out, _ = self.run_cli(["quickstart", "--status", "--key", "nope", "--json"])
        self.assertEqual(code, 1)
        code, out, _ = self.run_cli(["quickstart", IDEA])
        self.assertIn("OpenRouter", out)

    def test_status_wait_returns_on_change(self):
        self.consent_yes()
        self.set_key()
        self.front(approve_budget=1)
        t = time.monotonic()
        calls = []

        def sleep(s):
            calls.append(s)
            if len(calls) == 2:
                self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA), wait_s=30, sleep=sleep)
        self.assertEqual(out["status"], "done")
        self.assertEqual(len(calls), 2)
        self.assertLess(time.monotonic() - t, 30)


class TestRealDetachedWorker(QuickCase):
    def test_spawned_worker_runs_the_offline_steps_and_waits_for_the_key(self):
        """The real subprocess: universe fresh, profiles present, no key -> it reaches 'waiting' with no request."""
        store_seed(self.cfg, self.home)
        self.consent_yes()
        out = qs.front(self.cfg, IDEA)                           # real spawn_worker
        self.assertEqual(out["status"], "needs_human")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and (self.job() or {}).get("state") != "waiting":
            time.sleep(0.3)
        job = self.job()
        self.assertEqual((job["state"], job["waiting_on"]), ("waiting", "key_jev"),
                         qs.log_path(self.cfg, job["idea_key"]).read_text() if qs.log_path(
                             self.cfg, job["idea_key"]).exists() else job)
        self.assertEqual({s["id"]: s["status"] for s in job["steps"]},
                         {"check": "ok", "universe": "skipped", "descriptions": "skipped", "pack": "skipped",
                          "idea_en": "ok"})
        self.assertIsInstance(job["worker"]["pid"], int)


class HeldStore:
    """Another process holds the DuckDB write lock (a screen writing its rows, an FD import...)."""

    def __init__(self, cfg):
        self.cfg = cfg

    def __enter__(self):
        import subprocess
        self.p = subprocess.Popen([sys.executable, "-c", "import duckdb, sys, time; c = duckdb.connect(sys.argv[1]); "
                                   "print('locked', flush=True); time.sleep(120)", str(self.cfg.db_path)],
                                  stdout=subprocess.PIPE, text=True)
        self.p.stdout.readline()
        return self

    def __exit__(self, *exc):
        self.p.kill()
        self.p.wait()
        self.p.stdout.close()


class TestResume(QuickCase):
    """status() restarts a job that waits on nothing (review R1): polling alone reaches the result."""

    def spawn(self, cfg, key):
        self.spawned.append(key)

    def test_key_set_after_the_worker_waited_then_polling_reaches_done(self):
        self.consent_yes()
        out = self.front(approve_budget=1)
        self.assertEqual(self.ids(out), ["key_jev"])
        self.work()
        self.assertEqual((self.job()["state"], self.job()["waiting_on"]), ("waiting", "key_jev"))
        self.set_key()
        n = len(self.spawned)
        st = qs.status(self.cfg, qs.idea_key(IDEA), spawn=self.spawn)
        self.assertEqual((st["status"], st["exit_code"], len(self.spawned)), ("running", 0, n + 1))
        self.assertTrue(st["poll_command"])
        self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA), spawn=self.spawn)["status"], "done")

    def test_status_wait_after_the_key_dialog_reaches_done(self):
        self.consent_yes()
        self.front(approve_budget=1)
        self.work()
        self.set_key()                                    # keys set openrouter --dialog succeeded
        calls = []

        def sleep(s):
            calls.append(s)
            if self.job()["state"] == "running":
                self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA), wait_s=30, sleep=sleep, spawn=self.spawn)
        self.assertEqual((out["status"], len(self.spawned), len(calls)), ("done", 2, 1))

    def test_flags_left_in_the_inbox_after_the_worker_exit_are_merged_by_status(self):
        self.consent_yes()
        self.set_key()
        self.front()
        self.work()                                       # waits on the budget
        self.assertEqual(self.job()["waiting_on"], "approve_budget")
        qs.write_inbox(self.cfg, qs.idea_key(IDEA), {"approve_budget": 1.0})
        n = len(self.spawned)
        st = qs.status(self.cfg, qs.idea_key(IDEA), spawn=self.spawn)
        self.assertEqual((st["status"], len(self.spawned)), ("running", n + 1))
        self.work()
        self.assertEqual(qs.status(self.cfg, qs.idea_key(IDEA))["status"], "done")

    def test_status_never_restarts_a_job_that_waits_on_the_human(self):
        self.consent_yes()
        self.front()
        self.work()
        n = len(self.spawned)
        st = qs.status(self.cfg, qs.idea_key(IDEA), spawn=self.spawn)
        self.assertEqual((st["status"], len(self.spawned)), ("needs_human", n))


class TestLockedStore(QuickCase):
    """front / status never wait on the DuckDB lock (review R3); the worker never guesses the money."""

    def test_front_and_status_stay_fast_while_another_process_writes(self):
        out = self.done_flow()
        self.assertEqual(out["status"], "done")
        job = self.job()
        job["state"] = "running"
        job["worker"]["heartbeat_at"] = qs.iso(qs.now_utc())
        job["approval"]["ledger_usd"] = 0.3               # the worker's last ledger reading (a killed run)
        qs.save_job(self.cfg, job)
        with HeldStore(self.cfg):
            t = time.monotonic()
            st = qs.status(self.cfg, qs.idea_key(IDEA))
            self.assertLess(time.monotonic() - t, 5.0)
            self.assertAlmostEqual(st["spent_usd"], 0.3 + job["approval"]["canary_usd"], places=6)
            t = time.monotonic()
            fr = self.front()
            self.assertLess(time.monotonic() - t, 5.0)
            self.assertEqual(fr["status"], "running")
            t = time.monotonic()
            qs.status(self.cfg, qs.idea_key(IDEA), wait_s=2)
            self.assertLess(time.monotonic() - t, 4.0)       # the wait budget holds with a locked store

    def test_the_worker_never_uses_the_recorded_costs_for_the_screen_budget(self):
        self.done_flow()
        job = self.job()
        w = qs.Worker(self.cfg, job, self.deps)
        with mock.patch.object(qs, "WORKER_DB_WAIT_S", 0.5), HeldStore(self.cfg):
            with self.assertRaises(store.StoreLocked):
                qs.spent_usd(self.cfg, job, strict=True, wait_s=0.5)
            self.assertEqual(w.step_screen(), "stop")
        self.assertEqual(self.job()["failure"]["kind"], "busy")


class TestQueued(QuickCase):
    """A front for a second idea while another idea's worker runs keeps the answers (review R5)."""

    def test_second_idea_is_queued_with_its_flags(self):
        self.consent_yes()
        self.set_key()
        other = "solid state batteries"
        with guard.budget_lock(self.cfg, qs.LOCK):
            out = self.front(other, approve_budget=1)
            self.assertEqual((out["status"], out["exit_code"]), ("store_busy", 3))
            argv = shlex.split(out["next_command"])
            self.assertEqual(argv[argv.index("--approve-budget") + 1], "1")
            self.assertTrue(out["poll_command"])
            self.assertNotIn("I will continue", out["text_en"])
            self.assertNotIn("我会继续", out["text_zh"])
            self.assertIsNotNone(self.job(other))
            st = qs.status(self.cfg, qs.idea_key(other), spawn=lambda cfg, key: self.spawned.append(key))
            self.assertEqual((st["status"], st["exit_code"]), ("store_busy", 3))
            self.assertEqual(self.spawned, [])
        st = qs.status(self.cfg, qs.idea_key(other), spawn=lambda cfg, key: self.spawned.append(key))
        self.assertEqual((st["status"], self.spawned), ("running", [qs.idea_key(other)]))
        self.assertEqual(self.job(other)["approval"]["usd"], 1.0)      # the approval was kept, not asked again


if __name__ == "__main__":
    unittest.main()
