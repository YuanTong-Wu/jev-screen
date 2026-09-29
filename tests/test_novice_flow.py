"""The novice-run fixes of the quickstart flow (docs/AGENT_API.md "quickstart"): the English sentence check (no false
company names, checked in the front), the approval kept across a one-word fix, the one stable page that always shows
the newest run, the optional profile fill with an incremental L1, the poll command while the worker runs, a key
recorded after the worker started, cumulative time / cost, the second-round questions only when relevant, and the
faster universe write.

No network and no paid calls: the fakes of test_quickstart / test_screen; every company is invented.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import duckdb  # noqa: E402

from jevscreen import calib, cli, config, keys, page, quickstart as qs, screen, store  # noqa: E402
from jevscreen.sources import tradingview_scanner as tv  # noqa: E402
from test_quickstart import FAKE_KEY, IDEA, QuickCase  # noqa: E402
from test_screen import SEC_COLS, FakeKeywords, StoreCase, make_factory  # noqa: E402

NAMES = ["Core Inc", "Main Street Capital Corp", "Global Power Ltd", "Quillon Robotics Inc", "Applied Materials Inc",
         "RoboCorp"]
TICKERS = ["CORE", "MAIN", "BESS", "QLRB"]
CORE_EN = "Core suppliers of liquid-cooling thermal management systems for battery energy storage power stations."
ZH_IDEA = "储能电站液冷温控系统的核心供应商"


class TestIdeaEnCheck(unittest.TestCase):
    def test_core_suppliers_is_not_a_company_name(self):
        self.assertEqual(calib.idea_en_problems(CORE_EN, ZH_IDEA, NAMES, TICKERS), [])
        self.assertEqual(calib.idea_en_problems("Main makers of Global power grid storage", "x", NAMES, TICKERS), [])
        self.assertEqual(calib.idea_en_problems("Makers of BESS cooling units", "x", NAMES, TICKERS), [])

    def test_real_names_still_count_case_sensitive(self):
        probs = calib.idea_en_problems("Peers of Quillon Robotics in humanoid robots", "人形机器人", NAMES, TICKERS)
        self.assertTrue(probs and "Quillon Robotics" in probs[0], probs)
        self.assertEqual(calib.idea_en_problems("peers of quillon robotics", "x", NAMES, TICKERS), [])
        self.assertTrue(calib.idea_en_problems("Suppliers like RoboCorp", "x", NAMES, TICKERS))
        self.assertTrue(calib.idea_en_problems("Chip tools such as Applied Materials sells", "x", NAMES, TICKERS))
        self.assertTrue(calib.idea_en_problems("Companies like QLRB", "x", NAMES, TICKERS))

    def test_suggestion_and_minor_fix(self):
        self.assertEqual(calib.suggest_idea_en("Humanoid robot makers such as RoboCorp and Quillon Robotics",
                                               ["RoboCorp", "Quillon Robotics"]), "Humanoid robot makers")
        self.assertEqual(calib.suggest_idea_en("Makers of humanoid robots (e.g. RoboCorp)", ["RoboCorp"]),
                         "Makers of humanoid robots")
        self.assertIsNone(calib.suggest_idea_en("RoboCorp peers", ["RoboCorp"]))
        self.assertTrue(calib.idea_en_minor_fix("Core suppliers of X", "Main suppliers of X", ["Core"]))
        self.assertFalse(calib.idea_en_minor_fix("Core suppliers of X", "Suppliers of X", ["Core"]))  # a drop
        self.assertTrue(calib.idea_en_minor_fix("Core suppliers of X", "core suppliers of X", ["Core"]))
        self.assertFalse(calib.idea_en_minor_fix("Core suppliers of X", "Main vendors of X", ["Core"]))
        self.assertFalse(calib.idea_en_minor_fix("Core suppliers of X", "Two big suppliers of X", ["Core"]))


class FlowCase(QuickCase):
    ZH = "人形机器人供应商"

    def names_cache(self, names=("RoboCorp", "Quillon Robotics Inc"), tickers=("ROBO",)):
        qs.write_names_cache(self.cfg, list(names), list(tickers))


class TestFrontCheck(FlowCase):
    def test_front_checks_against_the_names_cache_before_any_download(self):
        self.names_cache()
        out = self.front(self.ZH, idea_en="Peers of Quillon Robotics for humanoid robots")
        self.assertEqual((out["status"], out["exit_code"]), ("needs_agent", 11))
        self.assertIn("Quillon Robotics", " ".join(out["idea_en_problems"]))
        self.assertEqual(out["suggested_idea_en"], None)       # too little left: the agent writes a new one
        out = self.front(self.ZH, idea_en="Humanoid robot suppliers such as RoboCorp")
        self.assertEqual(out["suggested_idea_en"], "Humanoid robot suppliers")
        self.assertIn("--idea-en 'Humanoid robot suppliers'", out["rerun_command"])
        self.assertEqual(self.spawned, [])
        out = self.front(self.ZH, idea_en="Core suppliers of humanoid robot joints")
        self.assertEqual(out["status"], "needs_human")
        self.assertFalse(out.get("idea_en_problems"))
        self.assertNotIn("idea_en refused", " ".join(out["notes"]))

    def test_worker_checks_right_after_the_universe_and_asks_again_for_another_company(self):
        idea = self.ZH
        bad = "RoboCorp humanoid robot suppliers"
        self.consent_yes()
        self.set_key()
        out = self.front(idea, idea_en=bad, approve_budget=1)       # first run: no names anywhere yet
        self.assertEqual(out["status"], "running")
        seen = {}
        orig = qs.Worker.step_descriptions

        def spy(w):
            seen["at_descriptions"] = (w.job.get("idea_en"), list(w.job.get("idea_en_problems") or []))
            return orig(w)
        with mock.patch.object(qs.Worker, "step_descriptions", spy):
            self.work(qs.idea_key(idea))
        self.assertEqual(seen["at_descriptions"][0], None)          # refused before the next download step
        self.assertTrue(qs.names_cache_path(self.cfg).exists())
        self.assertTrue(qs.step_done(self.job(idea), "descriptions"))    # the free downloads went on
        out = qs.status(self.cfg, qs.idea_key(idea), spawn=lambda cfg, key: self.spawned.append(key))
        self.assertEqual(out["status"], "needs_agent")
        self.assertIn("--approve-budget 1", out["next_command"])
        self.assertEqual(out["refused_idea_en"], bad)
        self.assertFalse(any(not f["dry_run"] for f in self.calls.factory))
        # RoboCorp is a real company name: dropping it changes what the human approved, so they are asked again
        # (a recased ordinary word keeps the approval: test_novice_review.TestHeldApproval)
        out = self.front(idea, idea_en="Leading humanoid robot suppliers", approve_budget=1)
        self.assertEqual(out["status"], "needs_human", out.get("pending"))
        self.assertEqual(self.ids(out), ["reprice_idea_en"])
        self.assertIn("RoboCorp", out["pending"][0]["question_zh"])
        self.assertFalse(qs.approval_valid(self.job(idea)))
        out = self.front(idea, idea_en="Leading humanoid robot suppliers", approve_budget=1)   # the human's yes
        self.assertEqual(out["status"], "running", out.get("pending"))
        self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertNotIn("意思没变", out["text_zh"])
        self.assertFalse(out.get("idea_en_problems"))


class TestPollAndKey(FlowCase):
    def test_poll_command_while_the_worker_runs_even_with_open_items(self):
        self.consent_yes()
        out = self.front(approve_budget=1)                   # no key yet: the free downloads start anyway
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(self.ids(out), ["key_jev"])
        self.assertEqual(out["state"], "running")
        self.assertTrue(out["poll_command"].startswith("jevscreen quickstart --status --key"))
        self.assertIn("免费下载已经在后台开始", out["text_zh"])
        self.assertTrue(all("--from-file" in c["agent_try_file"] for c in out["pending"][0]["choices"]))

    def test_a_key_recorded_after_the_worker_started_is_used(self):
        self.consent_yes()
        keyfile = self.home / "my-key.txt"
        keyfile.write_text(FAKE_KEY + "\n")
        os.chmod(keyfile, 0o600)
        self.front(approve_budget=1)
        orig = self.refresh.__call__

        def refresh_then_key(cfg, con, client):
            r = orig(cfg, con, client)
            keys.record_key_file(self.cfg, "openrouter", keyfile)     # the human sets the key mid-download
            return r
        self.deps.refresh_universe = refresh_then_key
        self.assertFalse(qs.key_state(self.cfg)["configured"])
        self.work()
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.calls.canary, 1)

    def test_keys_set_from_file_records_the_location_only(self):
        keyfile = self.home / "k.txt"
        keyfile.write_text(FAKE_KEY + "\n")
        os.chmod(keyfile, 0o600)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = cli.main(["keys", "set", "openrouter", "--from-file", str(keyfile)])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(str(keyfile), text)
        self.assertEqual(json.loads(text)["source"], "recorded-file")
        self.assertEqual(self.cfg.openrouter_key(), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, (self.home / config.OPENROUTER_KEY_LOCATION).read_text())
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            cli.main(["keys", "check", "openrouter"])
        row = json.loads(buf.getvalue())["keys"][0]
        self.assertEqual((row["configured"], row["shape_ok"], row["path"]), (True, True, None))
        keys.write_key(self.cfg, "openrouter", "sk-or-v1-" + "other" * 10)     # a typed key replaces it
        self.assertFalse((self.home / config.OPENROUTER_KEY_LOCATION).exists())
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = cli.main(["keys", "set", "openrouter", "--from-file", str(self.home / "missing")])
        self.assertEqual(code, 1)


class TestStableNewest(FlowCase):
    def run_id_in(self, path):
        return qs.read_page_data(path)["run_id"]

    def test_status_and_page_follow_the_newest_run_on_one_page(self):
        from jevscreen import quickstart_cli
        a = self.done_flow()
        stable = page.stable_path(self.cfg, IDEA)
        self.assertEqual(Path(a["page"]), stable)
        newer = screen.screen(self.cfg, IDEA, from_run=a["run_id"], jev_factory=self.factory,
                              out_dir=self.home / "answer-run", keywords_fn=FakeKeywords(), sieve="auto")
        with contextlib.redirect_stderr(io.StringIO()):
            quickstart_cli.write_after_cards(self.cfg, newer, None)       # what `jevscreen answer` does
        out = qs.status(self.cfg, qs.idea_key(IDEA))
        self.assertEqual((out["run_id"], Path(out["page"])), (newer["run_id"], stable))
        self.assertEqual(out["top"], page.top_rows(qs.read_page_data(stable), 10))
        self.assertEqual(out["version"], 2)
        self.assertEqual(out["change_en"], "screened again")
        with contextlib.redirect_stdout(io.StringIO()) as buf, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["page", newer["run_id"], "--json"])
        info = json.loads(buf.getvalue())
        self.assertEqual(Path(info["page"]), stable)

    def test_fetch_questions_name_the_newest_run_and_only_when_relevant(self):
        def upd(cfg, result, kw):
            qs_ = [{"id": "sec_email", "ask_human": True, "human_question_zh": "要设置 SEC 邮箱吗？",
                    "human_question_en": "Set up the SEC e-mail?", "then_command": f"jevscreen fetch-docs {result['run_id']}"},
                   {"id": "mops_annual", "ask_human": True, "human_question_zh": "要开启 MOPS 吗？",
                    "human_question_en": "Turn MOPS on?", "then_command": f"jevscreen fetch-docs {result['run_id']}"}]
            fetch = {"status": "ok", "fetched": {"cninfo": 3}, "seconds": 2.0, "questions": qs_,
                     "update": {"run_id": result["run_id"] + "-u", "status": "ok", "cost_usd": 0.001}}
            new = dict(result, run_id=result["run_id"] + "-u", supersedes=result["run_id"], cost_usd=0.001,
                       layers={**result["layers"], "fetch": fetch},
                       params={**result["params"], "from_run": result["run_id"], "update_budget_usd": 0.001})
            return {"result": new, "update": fetch["update"], "fetch": fetch}
        self.fetch_impl = upd
        out = self.done_flow()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        ids = [q["id"] for q in out["fetch"]["questions"]]
        self.assertEqual(ids, ["sec_email"])                  # a US company is in the top 10; no Taiwan one
        q = out["fetch"]["questions"][0]
        self.assertEqual(q["then_command"], f"jevscreen fetch-docs {out['run_id']}")
        self.assertTrue(q["optional"])
        self.assertIn("可选", q["human_question_zh"])
        self.assertEqual(out["version"], 2)
        self.assertEqual(out["change_zh"], "补抓 3 家年报后重排")
        self.assertIn("补抓 3 家年报后重排", out["text_zh"])
        data = qs.read_page_data(out["page"])
        self.assertEqual(data["change"]["zh"], "补抓 3 家年报后重排")

    def test_time_and_cost_are_cumulative_for_the_idea(self):
        out = self.done_flow()
        job = self.job()
        self.assertGreater(job["worker_seconds"], 0)
        with store.session(self.cfg, read_only=True) as con:
            ledger = con.execute("SELECT coalesce(sum(cost_usd), 0) FROM jev_requests").fetchone()[0]
        self.assertAlmostEqual(out["total_cost_usd"], round(float(ledger) + job["approval"]["canary_usd"], 6))
        self.assertIn("从你同意起共用时", out["text_zh"])
        self.assertIn(f"共花费 ${qs._usd(out['total_cost_usd'])}", out["text_zh"])
        data = qs.read_page_data(out["page"])
        self.assertTrue(data["totals"])
        self.assertEqual(data["cost_usd"], out["total_cost_usd"])


def add_cn(cfg, rows):
    """Invented Chinese companies (no profile unless given one), today's market date."""
    today = dt.date.today()
    with store.session(cfg) as con:
        snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="t", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
        store.upsert_many(con, "securities", SEC_COLS, [
            (sid, sid.split(":")[0], sid.split(":")[1], name, None, "China", "stock", "common", True,
             store.company_key(None, sid), snap, True) for sid, name, _m, _d in rows])
        store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "avg_volume_10d",
                                                "snapshot_id"), [(sid, today, m, 1e6, snap) for sid, _n, m, _d in rows])
        described = [(sid, "tradingview_profile", store.company_key(None, sid), d, snap) for sid, _n, _m, d in rows
                     if d]
        if described:
            store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"),
                              described)


CN_ROWS = [("SZSE:300901", "Frostline Thermal", 5.4e9, None), ("SZSE:300902", "Coolbay Tech", 2.7e9, None),
           ("SSE:600903", "Hanbridge Pumps", 2.0e9, None), ("SZSE:300904", "Lakestone Robot Joints", 3.0e9,
                                                            "Lakestone makes robot joints for humanoid robots.")]


class TestFill(FlowCase):
    IDEA_ZH = "人形机器人关节供应商"

    def setUp(self):
        super().setUp()
        self.crawled: list = []

        def crawl(cfg, client, *, countries, min_mcap_usd, **kw):
            self.crawled.append((countries, min_mcap_usd))
            with store.session(cfg) as con:
                snap = store.record_snapshot(con, source_id="tradingview_profile", kind="t", request=None,
                                             raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
                                             duration_s=None)
                store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text",
                                                        "snapshot_id"),
                                  [("SZSE:300901", "tradingview_profile", store.company_key(None, "SZSE:300901"),
                                    "Frostline Thermal makes servo robot joints and liquid cooling plates.", snap),
                                   ("SZSE:300902", "tradingview_profile", store.company_key(None, "SZSE:300902"),
                                    "Coolbay Tech sells office furniture.", snap)])
            return {"status": "ok", "ok": 2}
        self.deps.crawl_descriptions = crawl
        self.deps.crawl_client = lambda cfg: mock.Mock(requests_made=0)

    def first_result(self, idea):
        """The first result of a Chinese idea; the invented Chinese companies arrive with the stock list."""
        self.consent_yes("zh")
        self.set_key()
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        orig_after = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS)
            return orig_after(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(idea))
        return qs.status(self.cfg, qs.idea_key(idea))

    def test_the_optional_fill_question_and_the_incremental_rerank(self):
        idea = self.IDEA_ZH
        out = self.first_result(idea)
        self.assertEqual((out["status"], out["exit_code"]), ("done", 0), out.get("text_en"))
        item = next(i for i in out["pending"] if i["id"] == "fill_descriptions")
        self.assertTrue(item["optional"])
        self.assertEqual((item["country"], item["missing"]), ("CN", 3))
        self.assertIn("Frostline Thermal", item["question_zh"])         # the largest missing company is named
        self.assertIn("约 1 分钟", item["question_zh"])
        self.assertIn(item["question_zh"], out["text_zh"])
        self.assertEqual(item["rerun_with"], "--fill-descriptions yes")
        base_run = out["run_id"]
        l1_before = len(self.l1_classified())
        out = self.front(idea, fill_descriptions="yes")
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["run_id"], base_run)                        # the first result stays meanwhile
        self.assertTrue(out["poll_command"])
        self.work(qs.idea_key(idea))
        self.assertEqual(self.crawled, [(["CN"], 1e9)])
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertNotEqual(out["run_id"], base_run)
        new_l1 = [it.item_id for c in self.l1_classified()[l1_before:] for it in c.classified[0][0]]
        self.assertEqual(sorted(new_l1), sorted([store.company_key(None, "SZSE:300901"),
                                                 store.company_key(None, "SZSE:300902")]))
        self.assertEqual(out["change_zh"], "补了 2 家公司简介后重排")
        self.assertIn("Frostline Thermal", [r["name"] for r in out["top"] + out["to_confirm"]["rows"]])
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"]))
        self.assertEqual(qs.read_page_data(page.stable_path(self.cfg, idea))["run_id"], out["run_id"])

    def test_no_means_never_asked_again(self):
        idea = self.IDEA_ZH
        out = self.first_result(idea)
        self.assertTrue(any(i["id"] == "fill_descriptions" for i in out["pending"]))
        out = self.front(idea, fill_descriptions="no")
        self.assertEqual(out["status"], "done")
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"]))
        self.assertEqual(self.spawned, [qs.idea_key(idea)])      # only the first run's worker
        self.assertEqual(self.crawled, [])

    def test_a_blocked_fill_keeps_the_result(self):
        from jevscreen.http import Blocked
        idea = self.IDEA_ZH
        base = self.first_result(idea)

        def blocked(cfg, client, **kw):
            raise Blocked("https://www.tradingview.com/symbols/SZSE-300901/", 429, "http_429")
        self.deps.crawl_descriptions = blocked
        self.front(idea, fill_descriptions="yes")
        self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual((out["status"], out["run_id"]), ("done", base["run_id"]))
        self.assertIn("24 小时内不再请求", out["text_zh"])
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"]))
        again = self.front(idea)
        self.assertEqual(again["status"], "done")
        self.assertEqual(self.spawned.count(qs.idea_key(idea)), 2)       # nothing started a third time

    def test_an_english_idea_gets_no_fill_question(self):
        out = self.done_flow()
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"]))


class TestIncrementalL1(StoreCase):
    def test_l1_new_reads_only_the_newly_described(self):
        base = self.run_screen(reads=1)
        with store.session(self.cfg) as con:
            snap = store.record_snapshot(con, source_id="tradingview_profile", kind="t", request=None, raw_path=None,
                                         raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
            store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"),
                              [("NYSE:NODS", "tradingview_profile", "isin:US0000000005",
                                "NoDesc builds humanoid robot hands.", snap)])
        self.log.clear()
        res = self.run_screen(from_run=base["run_id"], l1_new=True, out_dir=self.home / "o2")
        l1 = [c for c in self.log if c.layer == "l1"]
        sent = [it.item_id for c in l1 for items, _q in c.classified for it in items]
        self.assertEqual(sent, ["isin:US0000000005"])
        self.assertEqual(res["layers"]["l1"]["new"], 1)
        self.assertTrue(res["params"]["l1_new"])
        self.assertIn("NYSE:NODS", [r["security_id"] for r in res["rows"]])
        self.assertEqual(page.change_of(res)["kind"], "fill")
        again = self.run_screen(from_run=base["run_id"], out_dir=self.home / "o3")      # without l1_new: $0 L1
        self.assertNotIn("NYSE:NODS", [r["security_id"] for r in again["rows"]])
        with self.assertRaisesRegex(ValueError, "l1_new needs from_run"):
            self.run_screen(l1_new=True, out_dir=self.home / "o4")


class TestBulkUpsert(unittest.TestCase):
    def test_bulk_path_writes_exactly_what_values_would(self):
        cols = ["security_id", "as_of", "metric", "period", "value_usd", "value_local", "currency_local",
                "fiscal_period_end", "snapshot_id"]
        rows = [[f"XEX:T{i:05d}", dt.date(2026, 9, 26), "total_revenue", "ttm" if i % 2 else "fy",
                 None if i % 7 == 0 else i * 1.5e6, 1e20 if i == 3 else -0.25 * i, "CNY" if i % 3 else None,
                 dt.date(2025, 12, 31) if i % 2 == 0 else None, "snap 'quoted' \"x\"\n"] for i in range(2500)]
        out = {}
        for mode, n in (("values", 10 ** 9), ("bulk", 2000)):
            con = duckdb.connect(":memory:")
            store.init(con)
            with mock.patch.object(tv, "BULK_MIN_ROWS", n):
                tv._upsert(con, "fundamentals_current", cols, rows)
                rows2 = [list(r) for r in rows[:10]]
                rows2[0][4] = 42.0
                tv._upsert(con, "fundamentals_current", cols, rows2 + rows[10:])     # replace, not duplicate
            out[mode] = con.execute("SELECT * FROM fundamentals_current ORDER BY security_id, metric, period"
                                    ).fetchall()
            con.close()
        self.assertEqual(len(out["bulk"]), 2500)
        self.assertEqual(out["bulk"], out["values"])


if __name__ == "__main__":
    unittest.main()
