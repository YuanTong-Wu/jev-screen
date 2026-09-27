"""Fixes from novice simulation #2 (docs/dev/novice_verdict_2.md section 6), offline:

- P0-1: missing China profiles are filled by default in the background right after consent (free, polite, consent
  gated, overlapping the other free steps, joined before the first read), with an opt-out; the fill question stays
  only as a fallback and names missing companies relevant to the idea;
- P0-3: a company that enters the result after a fill / re-rank without your AI's check goes to your AI (the same
  judge flow) and is marked "未核对 / not yet checked" until it is checked;
- P1-5: unanswered chat questions stay asked across versions, a declined SEC contact is never pushed again, the
  removed section's title matches what happened, Chinese short names in removed lists and `why`, `why` has its plain
  sentence at the top level, and the budget question is listed from the very first JSON.

No network and no paid calls: the fakes of test_quickstart / test_screen / test_review_flow; every company is
invented.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import consent, page, quickstart as qs, review, review_cli, store, why  # noqa: E402
from test_quickstart import IDEA as QIDEA  # noqa: E402
from test_novice_flow import CN_ROWS, FlowCase, TestFill, add_cn  # noqa: E402
import test_review_flow as TRF  # noqa: E402

CJK_TEXT = "你的 AI 核对后移出的"


# ------------------------------------------------------------------------------------------------ P1-5

class TestBudgetInTheFirstJson(FlowCase):
    def test_the_budget_question_is_pending_before_the_english_sentence(self):
        out = self.front(self.ZH)
        self.assertEqual(out["status"], "needs_agent")
        self.assertEqual(self.ids(out), ["idea_en", "consent_gray_sources", "approve_budget", "key_jev"])
        b = out["pending"][2]
        self.assertEqual(b["waits_for"], "idea_en")
        self.assertIsNone(b["idea_en"])
        for lang in ("zh", "en"):
            self.assertNotIn("None", b[f"question_{lang}"])
            self.assertNotIn("''", b[f"question_{lang}"])
        self.assertIn("两件事，并设置一个 key", out["intro_zh"])     # one round: consent, budget, and the key
        out = self.front(self.ZH, idea_en="Suppliers of humanoid robot joints")
        b = next(i for i in out["pending"] if i["id"] == "approve_budget")
        self.assertIsNone(b.get("waits_for"))
        self.assertIn("'Suppliers of humanoid robot joints'", b["question_en"])


class TestAskNowAcrossVersions(TRF.RelayAndPage):
    def test_an_unanswered_question_stays_in_ask_now_after_a_new_version(self):
        e = self.judge_unsure_top()
        key = qs.idea_key(QIDEA)
        s = qs.status(self.cfg, key)
        self.assertEqual([i.get("cid") for i in s["ask_now"]], [e["cid"]])
        other = next(r for r in s["top"] if r["rank"] != e["rank"])
        code, _d = review_cli.decide(self.cfg, other["overrides"]["keep"], s["run_id"])   # a new version
        self.assertEqual(code, 0)
        s2 = qs.status(self.cfg, key)
        self.assertNotEqual(s2["run_id"], s["run_id"])
        self.assertEqual([i.get("cid") for i in s2["ask_now"]], [e["cid"]])       # still asked, not in later
        self.assertIn(e["question_en"], s2["text_en"])
        code, _d = review_cli.decide(self.cfg, f"{e['cid']}=no", s2["run_id"])     # answered: gone
        self.assertEqual(qs.status(self.cfg, key)["ask_now"], [])


US_GAPS = {"l2_profile_only": [{"security_id": "NASDAQ:ROBO"}, {"security_id": "NYSE:ROB2"}]}


class TestSecDeclined(unittest.TestCase):
    def test_no_sec_push_after_the_human_said_no(self):
        res = {"gaps": US_GAPS, "params": {}}
        asked = page._gap_lines(res, {}, {"has_sec_key": False})
        self.assertIn("keys set sec-email", asked[0]["text_zh"])
        self.assertTrue(any(s["command"] == "jevscreen keys set sec-email" for s in qs._next_steps(asked)))
        lines = page._gap_lines(res, {}, {"has_sec_key": False, "sec_declined": True})
        [g] = lines
        self.assertIsNone(g["command"])
        for lang in ("zh", "en"):
            self.assertNotIn("sec-email", g[f"text_{lang}"])
        self.assertFalse(any("sec-email" in str(s.get("command")) for s in qs._next_steps(lines)))
        self.assertEqual(len(qs._next_steps(lines)), 1)            # only "try another idea"

    def test_page_data_reads_the_recorded_no(self):
        import tempfile
        from jevscreen import config
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {}, clear=False):
            safe_env.apply()
            cfg = config.Config(home=Path(tmp)).ensure()
            res = {"gaps": US_GAPS, "params": {}, "rows": [], "run_id": "r1", "status": "ok"}
            data = page.page_data(cfg, res, None, lang="zh")
            self.assertIn("sec-email", json.dumps([g["command"] for g in data["gaps"]]))
            consent.record(cfg, "sec-email-ask", "no")
            data = page.page_data(cfg, res, None, lang="zh")
            self.assertNotIn("sec-email", json.dumps(data["gaps"], ensure_ascii=False))
            from jevscreen import pagestatus
            opt = {c["id"]: c for c in pagestatus._optional(cfg, "zh")}
            self.assertNotIn("可选", opt["opt_sec"]["text"])


class TestRemovedTitle(unittest.TestCase):
    AGENT_ROW = {"name": "Feilong Auto Components", "security_id": "SZSE:002536", "company_key": "k1",
                 "verdict_source": "agent", "agent_chip": "e", "agent_why_zh": "做汽车零部件",
                 "agent_why_en": "car parts"}

    def data(self, **res):
        base = {"run_id": "r1", "idea": "储能温控", "rows": [], "params": {}, "status": "ok"}
        return page.build_page_data({**base, **res}, None, lang="zh",
                                    local_names={"SZSE:002536": "飞龙股份"})

    def test_no_scope_question_means_removed_after_your_ais_check(self):
        data = self.data(excluded_by_agent=[self.AGENT_ROW])
        txt = page.render_text(data, "zh")
        self.assertIn("你的 AI 核对后移出的（1 家）", txt)
        self.assertNotIn("按你的范围回答", txt)
        self.assertIn("飞龙股份", txt)                                  # the Chinese short name, not the English one
        self.assertNotIn("Feilong", txt)
        en = page.build_page_data({"run_id": "r1", "idea": "x", "rows": [], "params": {}, "status": "ok",
                                   "excluded_by_agent": [self.AGENT_ROW]}, None, lang="en")
        self.assertIn("Removed after your AI's check (1)", page.render_text(en, "en"))
        html = page.render_page(data)
        self.assertIn("removed_title_agent", html)                       # the page script picks the same title

    def test_a_scope_answer_keeps_the_scope_title(self):
        row = {"name": "Pay Terminal", "security_id": "SGX:P13", "scope_value": "role.hardware",
               "scope_source": "answer", "scope_p": 0.9}
        data = self.data(excluded_by_scope=[row], excluded_by_agent=[self.AGENT_ROW])
        self.assertIn("按你的范围回答和你的 AI 的判断移出的（2 家）", page.render_text(data, "zh"))

    def test_the_removed_rows_get_their_chinese_names_from_the_store(self):
        seen = []

        def local(con, sids):
            seen.extend(sids)
            return {}
        res = {"run_id": "r1", "rows": [], "excluded_by_agent": [self.AGENT_ROW],
               "excluded_by_scope": [{"security_id": "SSE:600000", "name": "X"}]}
        import tempfile
        from jevscreen import config, store
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(home=Path(tmp)).ensure()
            with store.session(cfg):
                pass
            with mock.patch.object(page, "_local_names", local):
                page._store_lookups(cfg, res)
        self.assertIn("SZSE:002536", seen)
        self.assertIn("SSE:600000", seen)

    def test_the_chat_summary_uses_chinese_short_names(self):
        summ = review_cli.agent_summary({"excluded_by_agent": [self.AGENT_ROW]}, None, 1, None,
                                        names={"SZSE:002536": "飞龙股份"})
        self.assertIn("飞龙股份", summ["text_zh"])
        self.assertNotIn("Feilong", summ["text_zh"])
        self.assertIn("Feilong", summ["text_en"])
        self.assertEqual(summ["removed"][0]["name_zh"], "飞龙股份")


class TestWhyNamesAndPlain(TRF.FlowCase):
    def test_who_uses_the_chinese_short_name_and_plain_is_top_level(self):
        sid = self.base["rows"][0]["security_id"]
        with mock.patch.object(qs, "_local_names", lambda con, sids: {s: "支付一号" for s in sids if s == sid}):
            out = why.run(self.cfg, [sid], run_ref=self.base["run_id"])
        [r] = out["results"]
        self.assertTrue(r["who_zh"].startswith("支付一号"), r["who_zh"])
        self.assertNotIn("支付一号", r["who_en"])
        self.assertEqual(out["plain_zh"], r["plain_zh"])
        self.assertEqual(out["plain_en"], r["plain_en"])
        self.assertTrue(out["plain_zh"])
        self.assertEqual(out["who_zh"], r["who_zh"])



# ------------------------------------------------------------------------------------------------ P0-3

class TestUncheckedAfterARerank(TRF.FlowCase):
    """Your AI drops three of the top 10: the two companies that move up into the top 10 were never read by it."""

    @staticmethod
    def drop_top3(it):
        if it.get("held_sid"):
            return {"v": "yes", "level": "explicit", "quote_ids": [1], "why": "它自己做数字支付", "in_group": False,
                    "short": "数字支付"}
        if it.get("rank") in (1, 2, 3):
            return {"v": "no", "chip": "e", "quote_ids": [1], "why": "不是这一类"}
        return {"v": "yes", "level": "explicit", "quote_ids": [1], "why": "它自己做数字支付"}

    def judged(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        code, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, self.drop_top3)))
        self.assertEqual(code, 0)
        return deck, out

    def test_moved_up_companies_go_to_your_ai_and_are_marked_until_checked(self):
        deck_a, out = self.judged()
        read = {it["company_key"] for it in deck_a["items"]}
        out_dir, res = review_cli.run_files(self.cfg, out["run_id"])
        top10 = [r for r in res["rows"] if r["rank"] <= 10]
        new = [r["company_key"] for r in top10 if r["company_key"] not in read]
        self.assertEqual(len(new), 2, [r["name"] for r in top10])
        rv = review.load_review(out_dir)
        self.assertEqual(sorted(rv["unchecked"]), sorted(new))
        ar = rv["agent_review"]
        self.assertEqual((ar["state"], ar["part"]), ("pending", "F1"))           # blocking: the same judge flow
        self.assertTrue(ar["blocking"])
        self.assertIn("part_b", ar)                                               # part B is kept for later
        deck_f = json.loads(Path(ar["deck_path"]).read_text(encoding="utf-8"))
        self.assertEqual(sorted(it["company_key"] for it in deck_f["items"]), sorted(new))
        data = page.read_page_data(page.stable_path(self.cfg, TRF.IDEA))
        marked = {r["rank"] for r in data["rows"] if r.get("unchecked")}
        self.assertEqual(marked, {r["rank"] for r in top10 if r["company_key"] in new})
        row = next(r for r in data["rows"] if r.get("unchecked"))
        self.assertEqual(row["badges"][0], "unchecked")
        self.assertEqual(page.STRINGS["zh"]["badge_unchecked"], "未核对")
        self.assertEqual(page.STRINGS["en"]["badge_unchecked"], "not yet checked")
        self.assertIn("未核对", page.render_text(data, "zh"))
        self.assertTrue(any(t["unchecked"] for t in page.top_rows(data, 10)))
        # your AI checks them: the marks go, the review is done
        code, out2 = review_cli.judge(self.cfg, deck_f["deck_id"], file=str(self.answers_file(
            deck_f, lambda it: {"v": "yes", "level": "partial", "quote_ids": [1], "why": "它自己做数字支付"})))
        self.assertEqual(code, 0)
        out_dir2, _r = review_cli.run_files(self.cfg, out2["run_id"])
        self.assertEqual(review.load_review(out_dir2)["agent_review"]["state"], "done")
        data = page.read_page_data(page.stable_path(self.cfg, TRF.IDEA))
        self.assertFalse(any(r.get("unchecked") for r in data["rows"]))

    def test_a_human_drop_brings_an_unread_company_into_the_top_10(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        yes = lambda it: {**self.drop_top3({**it, "rank": 99})}            # noqa: E731 - yes to everything
        code, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, yes)))
        self.assertIsNone(out.get("review_pending"))                            # nothing new in the top 10
        _d, r1 = review_cli.run_files(self.cfg, out["run_id"])
        eleventh = next(r for r in r1["rows"] if r["rank"] == 11)
        self.assertIn(eleventh["company_key"], review_cli._deck_keys(out["part_b"]["deck_path"]))   # only offered
        first = next(r for r in r1["rows"] if r["rank"] == 1)
        code, d = review_cli.decide(self.cfg, f"drop={first['security_id']}", out["run_id"])
        self.assertEqual(code, 0)
        self.assertEqual(d["review_pending"]["part"], "F1")
        deck_f = json.loads(Path(d["review_pending"]["deck_path"]).read_text(encoding="utf-8"))
        self.assertEqual([it["company_key"] for it in deck_f["items"]], [eleventh["company_key"]])
        out_dir, _r = review_cli.run_files(self.cfg, d["run_id"])
        self.assertEqual(review.load_review(out_dir)["unchecked"], [eleventh["company_key"]])

    def test_the_chat_line_says_not_yet_checked(self):
        row = {"rank": 3, "name": "X", "ticker": "X", "country": "China", "one_line": "x", "verdict_words_zh": "相关",
               "verdict_words_en": "related", "evidence_kind": "profile", "unchecked": True}
        self.assertIn("未核对", qs.top_evidence(qs.STRINGS["zh"], row))
        self.assertIn("not yet checked", qs.top_evidence(qs.STRINGS["en"], row))



# ------------------------------------------------------------------------------------------------ P0-1

class DefaultFillCase(FlowCase):
    """The Chinese idea and the fake crawl of TestFill (its tests are not repeated here); the default fill runs
    in-process (the real one is a detached child)."""
    IDEA_ZH = TestFill.IDEA_ZH

    def setUp(self):
        super().setUp()
        self.crawled: list = []

        def fake_crawl(cfg, client, *, countries, min_mcap_usd, **kw):
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
        self.deps.crawl_descriptions = fake_crawl
        self.deps.crawl_client = lambda cfg: mock.Mock(requests_made=0)
        self.fill_calls: list = []
        orig = self.deps.crawl_descriptions
        self.at_crawl: list = []

        def crawl(cfg, client, **kw):
            job = qs.load_job(cfg, qs.idea_key(self.IDEA_ZH)) or {}
            self.at_crawl.append({s["id"] for s in job.get("steps") or [] if s.get("status") in ("ok", "skipped")})
            return orig(cfg, client, **kw)
        self.deps.crawl_descriptions = crawl

        def start(cfg, key, market, floor):
            self.fill_calls.append((key, market, floor))
            qs.fill_child(cfg, key, market, floor, deps=self.deps)
            return {"pid": None}
        self.deps.fill_start = start

    def first_run(self, idea=None, **front):
        idea = idea or self.IDEA_ZH
        self.consent_yes("zh")
        self.set_key()
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1, **front)
        orig_after = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS)
            return orig_after(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(idea))
        return qs.status(self.cfg, qs.idea_key(idea))

    def live_fill(self, ending: bool = False, finish_after: float | None = None) -> list:
        """fill_start that looks like the real child (`... --fill KEY ...` plus its state file) and keeps running;
        with `ending` it writes on SIGTERM the ending the real child writes (finished, exit 130, interrupted); with
        `finish_after` it ends by itself (finished, exit 0) after that many seconds."""
        import subprocess
        procs: list = []
        script = FILL_CHILD if ending else FILL_CHILD_OK.format(s=finish_after) if finish_after else \
            "import time; time.sleep(60)"

        def start(cfg, key, market, floor):
            path = str(qs.fill_state_path(cfg, key))
            qs.write_fill_state(cfg, key, {"state": "running", "pid": None, "market": market})
            proc = subprocess.Popen([sys.executable, "-c", script, path, "--fill", key])
            procs.append(proc)
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            qs.write_fill_state(cfg, key, {"state": "running", "pid": proc.pid, "market": market})
            return {"pid": proc.pid}
        self.deps.fill_start = start
        return procs

    def with_cn(self, rows=None):
        """Worker.after_universe patched: the invented Chinese companies arrive with the stock list."""
        orig = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS + list(rows or []))
            return orig(w)
        return mock.patch.object(qs.Worker, "after_universe", after)


# the real child turns SIGTERM into KeyboardInterrupt and writes its ending before it exits
FILL_CHILD = ("import json, os, signal, sys, time\n"
              "path = sys.argv[1]\n"
              "def ending(*_a):\n"
              "    st = json.load(open(path))\n"
              "    st.update(state='finished', exit=130, status='interrupted')\n"
              "    open(path + '.tmp', 'w').write(json.dumps(st))\n"
              "    os.replace(path + '.tmp', path)\n"
              "    sys.exit(0)\n"
              "signal.signal(signal.SIGTERM, ending)\n"
              "time.sleep(60)\n")
# a child that ends by itself after {s} seconds (nothing crawled: the store is unchanged)
FILL_CHILD_OK = ("import json, os, sys, time\n"
                 "path = sys.argv[1]\n"
                 "time.sleep({s})\n"
                 "st = json.load(open(path))\n"
                 "st.update(state='finished', exit=0, status='ok')\n"
                 "open(path + '.tmp', 'w').write(json.dumps(st))\n"
                 "os.replace(path + '.tmp', path)\n")


class TestDefaultFill(DefaultFillCase):
    def test_a_chinese_idea_fills_missing_china_profiles_before_the_first_read(self):
        out = self.first_run()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.fill_calls, [(qs.idea_key(self.IDEA_ZH), "CN", 1e9)])
        self.assertEqual(self.crawled, [(["CN"], 1e9)])
        [done_steps] = self.at_crawl
        self.assertIn("descriptions", done_steps)                 # after the free profile download ...
        self.assertNotIn("screen", done_steps)                    # ... and before the AI's first read
        first_l1 = [it.item_id for c in self.l1_classified() for it in c.classified[0][0]]
        self.assertIn(store.company_key(None, "SZSE:300901"), first_l1)      # read in the first pass, no re-rank
        self.assertIn("Frostline Thermal", [r["name"] for r in out["top"]])
        fd = self.job(self.IDEA_ZH)["fill_default"]
        self.assertEqual((fd["state"], fd["added"], fd["market"]), ("done", 2, "CN"))
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"] + out["ask_now"]))
        self.assertIn("2 家", out["text_zh"])
        self.assertIn("补", out["text_zh"])
        self.assertEqual((out["version"] or 1), 1)                 # one version: nothing re-ranked afterwards
        self.assertFalse(any(r.get("unchecked") for r in out["top"]))
        from jevscreen import pagestatus
        job = self.job(self.IDEA_ZH)
        job["state"] = "running"                                   # the progress part, as while it ran
        bars = {b["id"]: b for b in pagestatus.build(self.cfg, lang="zh", job=job)["progress"]["items"]}
        self.assertEqual((bars["fill_default"]["state"], bars["fill_default"]["text"]), ("ok", "补到 2 家"))

    def test_the_first_json_says_it_will_fill_and_how_to_opt_out(self):
        self.consent_yes("zh")
        out = self.front(self.IDEA_ZH, idea_en="Suppliers of joints for humanoid robots")
        fdv = out["fill_default"]
        self.assertEqual((fdv["market"], fdv["opt_out_with"]), ("CN", "--fill-descriptions no"))
        self.assertIn("不想补", out["intro_zh"])
        self.assertIn(fdv["text_zh"], out["intro_zh"])
        self.assertIn("Chinese company profiles", fdv["text_en"])
        self.assertIsNone(qs.front(self.cfg, "humanoid robots", spawn=lambda c, k: None).get("fill_default"))

    def test_opt_out_means_no_fill_and_no_question(self):
        out = self.first_run(fill_descriptions="no")
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.crawled, [])
        self.assertEqual(self.fill_calls, [])
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"] + out["ask_now"]))

    def test_a_failed_default_fill_falls_back_to_the_question(self):
        calls = []

        def broken(cfg, client, **kw):
            calls.append(kw)
            raise RuntimeError("connection reset")
        self.deps.crawl_descriptions = broken
        out = self.first_run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.job(self.IDEA_ZH)["fill_default"]["state"], "failed")
        item = next(i for i in out["pending"] if i["id"] == "fill_descriptions")
        self.assertIn(item, out["ask_now"])

    def test_the_real_start_never_runs_in_the_test_suite(self):
        got = qs._default_fill_start(self.cfg, "k", "CN", 1e9)
        self.assertEqual(got.get("refused"), "testing")


class TestDefaultFillEndings(DefaultFillCase):
    def test_a_block_is_told_and_not_asked_again(self):
        from jevscreen.http import Blocked

        def blocked(cfg, client, **kw):
            raise Blocked("https://www.tradingview.com/symbols/SZSE-300901/", 429, "http_429")
        self.deps.crawl_descriptions = blocked
        out = self.first_run()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        fd = self.job(self.IDEA_ZH)["fill_default"]
        self.assertEqual((fd["state"], fd["exit"]), ("blocked", 2))
        self.assertIn("24 小时内不再请求", out["text_zh"])
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"]))   # within the cooldown

    def test_a_child_that_vanished_is_not_waited_for(self):
        self.deps.fill_start = lambda cfg, key, market, floor: {"pid": 2 ** 22 + 12345}
        with mock.patch.object(qs, "_pid_alive", return_value=False):
            out = self.first_run()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.job(self.IDEA_ZH)["fill_default"]["state"], "failed")
        self.assertTrue(any(i["id"] == "fill_descriptions" for i in out["pending"]))    # the fallback

    def test_opting_out_while_it_runs_stops_it(self):
        import subprocess
        import sys as _sys
        procs = []

        def start(cfg, key, market, floor):     # looks like the real child: `... --fill KEY ...` + its state file
            proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)", "--fill", key])
            procs.append(proc)
            qs.write_fill_state(cfg, key, {"state": "running", "pid": proc.pid, "market": market})
            return {"pid": proc.pid}
        self.deps.fill_start = start
        idea = self.IDEA_ZH
        self.consent_yes("zh")
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        orig_after = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS)
            return orig_after(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(idea))                     # stops at the key; the fill goes on
        self.assertEqual(self.job(idea)["fill_default"]["state"], "running")
        out = self.front(idea, fill_descriptions="no")
        self.assertEqual(out["fill_default"]["state"], "opted_out")
        procs[0].wait(timeout=10)                           # stopped by the opt-out
        self.assertNotEqual(procs[0].returncode, 0)
        self.set_key()
        self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.job(idea)["fill_default"]["state"], "opted_out")
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"] + out["ask_now"]))
        self.assertEqual(self.crawled, [])

    def test_an_opt_out_never_signals_a_process_that_is_not_the_fill(self):
        import subprocess
        import sys as _sys
        other = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)"])   # a reused pid
        self.addCleanup(other.kill)
        self.deps.fill_start = lambda cfg, key, market, floor: {"pid": other.pid}
        idea = self.IDEA_ZH
        self.consent_yes("zh")
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        orig_after = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS)
            return orig_after(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(idea))
        self.assertEqual(self.job(idea)["fill_default"]["pid"], other.pid)
        self.front(idea, fill_descriptions="no")             # no state file says this pid is the fill
        qs.write_fill_state(self.cfg, qs.idea_key(idea), {"state": "running", "pid": other.pid})
        self.front(idea, fill_descriptions="no")             # the state file agrees, the command line does not
        self.assertIsNone(other.poll())                      # still alive: never signalled
        self.assertFalse(qs.stop_fill(self.cfg, qs.idea_key(idea), other.pid))

    def test_a_fill_never_starts_once_the_first_read_is_done(self):
        from jevscreen import store as _st
        seen = []

        def start(cfg, key, market, floor):
            job = qs.load_job(cfg, key) or {}
            seen.append(qs.step_done(job, "screen"))
            raise _st.StoreLocked("busy")                   # never decided: fill_default stays unset
        self.deps.fill_start = start
        out = self.first_run()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertTrue(seen)
        self.assertNotIn(True, seen)                          # never after the screen: nothing would wait for it

    def test_the_page_shows_a_fill_that_ended_while_the_worker_was_away(self):
        from jevscreen import pagestatus
        key = qs.idea_key(self.IDEA_ZH)
        job = {"idea_key": key, "state": "running", "fill_default": {"state": "running", "pid": 4242,
                                                                    "total": 900, "done": 0}}
        items = {b["id"]: b for b in pagestatus._progress(pagestatus._with_fill_file(self.cfg, job), "zh",
                                                          qs.now_utc())["items"]}
        self.assertEqual((items["fill_default"]["state"], items["fill_default"]["text"]), ("run", "进行中…"))
        qs.write_fill_state(self.cfg, key, {"state": "finished", "pid": 4242, "exit": 0})
        items = {b["id"]: b for b in pagestatus._progress(pagestatus._with_fill_file(self.cfg, job), "zh",
                                                          qs.now_utc())["items"]}
        self.assertEqual(items["fill_default"]["state"], "ok")
        prog = pagestatus._progress(pagestatus._with_fill_file(self.cfg, {**job, "fill_pref": "no"}), "zh",
                                    qs.now_utc()) or {"items": []}
        self.assertNotIn("fail", [b["state"] for b in prog["items"]])    # an opt-out is not a red failure

    def test_only_china_is_filled_by_default(self):
        self.assertEqual(qs.default_fill_market({"idea": "储能温控", "countries": None}), "CN")
        self.assertEqual(qs.default_fill_market({"idea": "储能温控", "countries": ["CN"]}), "CN")
        self.assertIsNone(qs.default_fill_market({"idea": "储能温控", "countries": ["JP"]}))
        self.assertEqual(qs.fill_market({"idea": "储能温控", "countries": ["JP"]}), "JP")   # the question stays

    def test_the_child_writes_its_ending(self):
        from jevscreen import consent as _c
        _c.record(self.cfg, "gray-sources", "yes", lang="zh")
        qs.fill_child(self.cfg, "k1", "CN", 1e9, deps=self.deps)
        st = qs.read_fill_state(self.cfg, "k1")
        self.assertEqual((st["state"], st["exit"], st["market"]), ("finished", 0, "CN"))
        self.assertEqual(self.crawled, [(["CN"], 1e9)])
        self.deps.crawl_descriptions = None                 # the real crawl is refused in the test suite
        qs.fill_child(self.cfg, "k2", "CN", 1e9, deps=self.deps)
        self.assertEqual(qs.read_fill_state(self.cfg, "k2")["status"], "testing_refused")


class TestRelevantExamples(DefaultFillCase):
    def test_the_question_names_missing_companies_that_match_the_idea(self):
        extra = [("SZSE:300905", "Hexa Robot Joint Technology", 1.2e9, None),
                 ("SSE:600906", "Megacity Construction Group", 90e9, None),
                 ("SZSE:300907", "Xinghe Precision", 1.1e9, None)]
        raw = self.home / "szse_stock.json"
        raw.write_text(json.dumps({"stockList": [{"code": "300907", "orgId": "gsqs300907", "zwjc": "星河关节"}]},
                                  ensure_ascii=False), encoding="utf-8")

        def broken(cfg, client, **kw):
            raise RuntimeError("connection reset")
        self.deps.crawl_descriptions = broken
        self.consent_yes("zh")
        self.set_key()
        idea = self.IDEA_ZH
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        orig = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS + extra)
            with store.session(self.cfg) as con:
                snap = store.record_snapshot(con, source_id="cninfo_annual_report", kind="stock_list", request=None,
                                             raw_path=str(raw), raw_sha256=None, raw_bytes=None, rows=1,
                                             duration_s=None)
                store.upsert_many(con, "identifiers", ("security_id", "id_type", "id_value", "method", "snapshot_id"),
                                  [("SZSE:300907", "cninfo_orgid", "gsqs300907", "code_exact", snap)])
            return orig(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        item = next(i for i in out["pending"] if i["id"] == "fill_descriptions")
        names = [b["security_id"] for b in item["biggest"]]
        self.assertEqual(names[:2], ["SZSE:300905", "SZSE:300907"])      # by the idea's words, not by size
        self.assertNotIn("SSE:600906", names)                             # the largest, but unrelated
        self.assertIn("星河关节", item["question_zh"])
        self.assertIn("Hexa Robot Joint", item["question_en"])
        self.assertEqual(item.get("names_basis"), "idea_words")


class TestEnglishIdeaMostlyAShares(DefaultFillCase):
    def test_a_shares_heavy_first_read_fills_and_reranks_before_the_first_result(self):
        cn = [("SZSE:300911", "Jadepeak Robotics", 3e9, "Jadepeak makes robot arms for factories."),
              ("SZSE:300912", "Silverlake Robot", 2.5e9, "Silverlake builds service robot platforms."),
              ("SSE:600913", "Redcliff Automation", 2.2e9, "Redcliff sells robot controllers."),
              ("SZSE:300914", "Bluefin Robot Parts", 2.1e9, "Bluefin makes robot grippers.")]
        self.consent_yes()
        self.set_key()
        self.front(QIDEA, approve_budget=1)
        orig = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS + cn)
            return orig(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(qs.idea_key(QIDEA))
        out = qs.status(self.cfg, qs.idea_key(QIDEA))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.crawled, [(["CN"], 1e9)])
        fd = self.job(QIDEA)["fill_default"]
        self.assertEqual((fd["state"], fd["when"]), ("done", "after_l1"))
        self.assertIn("Frostline Thermal", [r["name"] for r in out["top"]])
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"] + out["ask_now"]))
        self.assertEqual(len(self.spawned), 1)                  # one worker: the first result already has them
        job = self.job(QIDEA)                                   # a resumed worker reloads the re-ranked run
        self.assertEqual(qs.step_of(job, "screen")["run_id"], out["run_id"])

    def test_an_english_idea_with_few_a_shares_is_not_filled(self):
        self.done_flow()
        self.assertEqual(self.crawled, [])
        self.assertEqual(self.fill_calls, [])


class TestFillBecomesReviewed(DefaultFillCase):
    """The fallback fill after a result your AI checked: the companies it brings in go to your AI first."""
    review = "keep"

    def test_new_companies_after_the_fallback_fill_are_checked_before_relaying(self):
        orig = self.deps.crawl_descriptions
        n = []

        def first_fails(cfg, client, **kw):
            n.append(1)
            if len(n) == 1:
                raise RuntimeError("connection reset")
            return orig(cfg, client, **kw)
        self.deps.crawl_descriptions = first_fails
        out = self.first_run()
        self.assertEqual(out["status"], "needs_agent")
        deck = json.loads(Path(out["pending"][0]["deck_path"]).read_text(encoding="utf-8"))
        ans = {str(it["n"]): {"v": "yes", "level": "partial", "quote_ids": [1], "why": "做机器人关节"}
               for it in deck["items"]}
        f = self.home / "a.json"
        f.write_text(json.dumps({"format": review.ANSWERS_FORMAT, "deck_id": deck["deck_id"], "answers": ans}),
                     encoding="utf-8")
        review_cli.judge(self.cfg, deck["deck_id"], file=str(f))
        out = qs.status(self.cfg, qs.idea_key(self.IDEA_ZH))
        self.assertEqual(out["status"], "done")
        self.assertTrue(any(i["id"] == "fill_descriptions" for i in out["ask_now"]))
        self.front(self.IDEA_ZH, fill_descriptions="yes")
        self.work(qs.idea_key(self.IDEA_ZH))
        out = qs.status(self.cfg, qs.idea_key(self.IDEA_ZH))
        self.assertEqual((out["status"], out["exit_code"]), ("needs_agent", 11), out.get("text_en"))
        [item] = out["pending"]
        self.assertEqual((item["id"], item["part"]), ("agent_review", "F1"))
        self.assertIn("新进入名单", out["text_zh"])
        deck_f = json.loads(Path(item["deck_path"]).read_text(encoding="utf-8"))
        self.assertIn(store.company_key(None, "SZSE:300901"), [it["company_key"] for it in deck_f["items"]])
        frost = next(r for r in out["top"] if r["name"] == "Frostline Thermal")
        self.assertTrue(frost["unchecked"])
        data = page.read_page_data(Path(out["page"]))
        self.assertTrue(next(r for r in data["rows"] if r["name"] == "Frostline Thermal")["unchecked"])
        ans = {str(it["n"]): {"v": "yes", "level": "explicit", "quote_ids": [1], "why": "做机器人关节"}
               for it in deck_f["items"]}
        f.write_text(json.dumps({"format": review.ANSWERS_FORMAT, "deck_id": deck_f["deck_id"], "answers": ans}),
                     encoding="utf-8")
        code, _j = review_cli.judge(self.cfg, deck_f["deck_id"], file=str(f))
        self.assertEqual(code, 0)
        out = qs.status(self.cfg, qs.idea_key(self.IDEA_ZH))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertFalse(any(r["unchecked"] for r in out["top"]))


# ------------------------------------------------------------------------------------------------ fill review fixes

class TestScopeChangeDuringTheFillWait(DefaultFillCase):
    def test_a_new_floor_merged_while_the_first_read_waits_is_priced_before_any_paid_read(self):
        procs = self.live_fill()
        idea, key = self.IDEA_ZH, qs.idea_key(self.IDEA_ZH)
        self.consent_yes("zh")
        self.set_key()
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        dry = []
        orig_dry = qs.Worker.dry_run

        def dry_run(w, *, min_mcap, countries, budget):
            dry.append(min_mcap)
            est = orig_dry(w, min_mcap=min_mcap, countries=countries, budget=budget)
            return {**est, "est_reserved_usd": 4.0} if min_mcap < 1e9 else est     # the wider scope is over $1
        orig_merge = qs.Worker.merge_inbox
        sent = []

        def merge(w):
            if w.job.get("current_step") == "screen" and not sent \
                    and (w.job.get("fill_default") or {}).get("state") == "running":
                # the human asks for smaller companies too while the first read waits: the worker holds the lock
                sent.append(qs.front(self.cfg, idea, min_mcap=2e8, spawn=lambda c, k: None)["status"])
                orig_merge(w)
                qs.write_fill_state(self.cfg, key, {"state": "finished", "pid": procs[0].pid, "exit": 0,
                                                    "status": "ok"})        # then the fill ends
                return
            return orig_merge(w)
        with self.with_cn(), mock.patch.object(qs.Worker, "dry_run", dry_run), \
                mock.patch.object(qs.Worker, "merge_inbox", merge):
            self.work(key)
        self.assertEqual(sent, ["running"])
        self.assertIn(2e8, dry)                                               # the new floor is priced first ...
        self.assertFalse(any(not f["dry_run"] for f in self.calls.factory))   # ... and nothing was read for pay
        out = qs.status(self.cfg, key)
        item = next(i for i in out["pending"] if i["id"] == "approve_budget")
        self.assertEqual(item["kind"], "over")                               # the 'over' question with alternatives
        self.assertEqual(self.job(idea)["min_mcap_usd"], 2e8)


class TestAfterL1FillSettledElsewhere(DefaultFillCase):
    ROWS = [("SZSE:300911", "Jadepeak Robotics", 3e9, "Jadepeak makes robot arms for factories."),
            ("SZSE:300912", "Silverlake Robot", 2.5e9, "Silverlake builds service robot platforms."),
            ("SSE:600913", "Redcliff Automation", 2.2e9, "Redcliff sells robot controllers."),
            ("SZSE:300914", "Bluefin Robot Parts", 2.1e9, "Bluefin makes robot grippers.")]

    def broken_wait(self, exc):
        """The first after_l1 wait raises `exc` (a kill: the worker ends interrupted and is resumed; an error: the
        loop goes on); the fill itself ends anyway."""
        key = qs.idea_key(QIDEA)
        self.consent_yes()
        self.set_key()
        self.front(QIDEA, approve_budget=1)
        orig_join = qs.Worker.join_fill
        fired = []

        def join(w, wait=True):
            if wait and (w.job.get("fill_default") or {}).get("when") == "after_l1" and not fired:
                fired.append(type(exc).__name__)
                raise exc
            return orig_join(w, wait=wait)
        with self.with_cn(self.ROWS), mock.patch.object(qs.Worker, "join_fill", join):
            self.work(key)
            if self.job(QIDEA)["state"] != "done":
                self.work(key)                                  # the resumed worker
        self.assertEqual(fired, [type(exc).__name__])
        out = qs.status(self.cfg, key)
        self.assertEqual(out["status"], "done", out.get("text_en"))
        job = self.job(QIDEA)
        self.assertEqual((job["fill_default"]["state"], job["fill_default"]["when"]), ("done", "after_l1"))
        self.assertIn("Frostline Thermal", [r["name"] for r in out["top"]])     # ranked in before the first result
        self.assertEqual(qs.step_of(job, "screen")["run_id"], out["run_id"])
        return out

    def test_a_worker_killed_while_it_waits(self):
        self.broken_wait(KeyboardInterrupt())

    def test_an_error_while_it_waits(self):
        self.broken_wait(RuntimeError("boom"))


SMALL_CN = [("SZSE:301001", "Tinyhill Motors", 5e8, "Tinyhill makes small motors."),
            ("SZSE:301002", "Pebble Gears", 4e8, "Pebble Gears makes gears."),
            ("SZSE:301005", "Reedpond Actuators", 6e8, None), ("SZSE:301006", "Mossbank Joints", 4e8, None),
            ("SZSE:301007", "Fernvale Drives", 3e8, None)]


class TestScopeChangeAfterTheFillStarted(DefaultFillCase):
    def started_then(self, **change):
        """The fill starts at the $1B floor (the worker stops at the key), then the human changes the scope."""
        idea = self.IDEA_ZH
        self.consent_yes("zh")
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        with self.with_cn(SMALL_CN):
            self.work(qs.idea_key(idea))
        self.assertEqual(self.job(idea)["fill_default"]["state"], "running")
        self.front(idea, **change)
        self.set_key()
        self.work(qs.idea_key(idea))
        return qs.status(self.cfg, qs.idea_key(idea))

    def test_a_lower_floor_counts_the_fill_at_its_own_floor_and_offers_the_rest(self):
        out = self.started_then(min_mcap=2e8)
        self.assertEqual(out["status"], "done", out.get("text_en"))
        fd = self.job(self.IDEA_ZH)["fill_default"]
        self.assertEqual((fd["state"], fd["added"], fd["floor"]), ("done", 2, 1e9))   # not the 2 already described
        self.assertIn("补了 2 家", out["text_zh"])
        item = next(i for i in out["pending"] if i["id"] == "fill_descriptions")      # below $1B was never filled
        self.assertEqual(item["country"], "CN")

    def test_another_market_is_not_waited_for_priced_or_claimed(self):
        procs = self.live_fill()
        out = self.started_then(countries=["US"])
        self.assertEqual(out["status"], "done", out.get("text_en"))
        procs[0].wait(timeout=10)                                  # stopped, not waited for up to 20 minutes
        job = self.job(self.IDEA_ZH)
        self.assertEqual((job["fill_default"]["state"], job["fill_default"]["reason"]), ("skipped", "scope"))
        self.assertLess(job["fill_default"]["seconds"], 30)       # the child sleeps 60 s: it was not waited for
        self.assertFalse((job.get("estimate") or {}).get("fill_usd"))
        self.assertNotIn("中国公司的简介", out["text_zh"])
        self.assertIsNone(out["fill_default"])

    def test_a_higher_floor_is_not_told_that_every_filled_profile_was_read(self):
        # the 'over' alternative 'market cap >= $5B only' after the fill started at $1B: the fill's own count is
        # kept (the page bar), but the first read only covered the filled companies above $5B
        out = self.started_then(min_mcap=5e9)
        self.assertEqual(out["status"], "done", out.get("text_en"))
        fd = self.job(self.IDEA_ZH)["fill_default"]
        self.assertEqual((fd["state"], fd["added"], fd["floor"]), ("done", 2, 1e9))
        self.assertNotIn("补了 2 家", out["text_zh"])
        self.assertNotIn("2 missing Chinese company profiles", out["text_en"])
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"]))    # the fill covered $5B

    def test_the_country_name_still_covers_china(self):
        # 'China' (the TradingView name, also a region) is a --countries token the screen accepts: the fill's market
        # is still screened, so the fill is waited for, not stopped as out of scope
        self.live_fill(finish_after=3)
        out = self.started_then(countries=["China"])
        self.assertEqual(out["status"], "done", out.get("text_en"))
        fd = self.job(self.IDEA_ZH)["fill_default"]
        self.assertEqual((fd["state"], fd["reason"], fd["exit"]), ("done", None, 0))
        self.assertTrue(qs.fill_in_scope(self.job(self.IDEA_ZH)))
        for cs, inside in ((["CN"], True), (["china"], True), (["CN", "HK"], True), (["US"], False),
                           (["Hong Kong"], False), (["JP"], False)):
            self.assertEqual(qs.fill_in_scope({"countries": cs, "fill_default": {"market": "CN"}}), inside, cs)


class TestOptOutEnding(DefaultFillCase):
    def test_an_opt_out_stays_an_opt_out_when_the_child_wrote_its_ending_first(self):
        from jevscreen import pagestatus
        procs = self.live_fill(ending=True)
        idea, key = self.IDEA_ZH, qs.idea_key(self.IDEA_ZH)
        self.consent_yes("zh")
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        with self.with_cn():
            self.work(key)                                       # stops at the key; the fill goes on
        self.front(idea, fill_descriptions="no")
        procs[0].wait(timeout=10)
        self.assertEqual(qs.read_fill_state(self.cfg, key)["status"], "interrupted")   # the child's own ending
        self.set_key()
        self.work(key)
        out = qs.status(self.cfg, key)
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.job(idea)["fill_default"]["state"], "opted_out")
        self.assertEqual(out["fill_default"]["state"], "opted_out")
        job = {**self.job(idea), "state": "running"}             # the progress part, as while the screen ran
        bars = {b["id"]: b for b in pagestatus.build(self.cfg, lang="zh", job=job)["progress"]["items"]}
        self.assertEqual(bars["fill_default"]["state"], "wait")    # grey, not a red failure

    def test_an_opt_out_while_the_worker_holds_the_lock(self):
        # the front stops the child at once and leaves the flag in the inbox; the child ends before the waiting
        # worker has merged that flag: the ending is still the human's opt-out
        procs = self.live_fill(ending=True)
        idea, key = self.IDEA_ZH, qs.idea_key(self.IDEA_ZH)
        self.consent_yes("zh")
        self.set_key()
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1)
        orig_alive = qs.Worker._fill_alive
        sent = []

        def alive(w, pid):
            if not sent and w.job.get("current_step") == "screen":
                sent.append(qs.front(self.cfg, idea, fill_descriptions="no", spawn=lambda c, k: None)["status"])
                procs[0].wait(timeout=10)
                return False
            return orig_alive(w, pid)
        with self.with_cn(), mock.patch.object(qs.Worker, "_fill_alive", alive):
            self.work(key)
        self.assertEqual(sent, ["running"])
        self.assertEqual(qs.read_fill_state(self.cfg, key)["status"], "interrupted")
        self.assertEqual(self.job(idea)["fill_default"]["state"], "opted_out")
        self.assertEqual(qs.status(self.cfg, key)["fill_default"]["state"], "opted_out")


class TestNextStepsAfterAnOptOutOrABlock(DefaultFillCase):
    CRAWL = "jevscreen crawl-descriptions"

    def assert_no_crawl_step(self, out):
        self.assertFalse(any(str(n.get("command") or "").startswith(self.CRAWL) for n in out["next_steps"]))
        for lang in ("zh", "en"):
            self.assertNotIn(self.CRAWL, out[f"text_{lang}"])

    def test_no_crawl_offered_after_the_opt_out(self):
        out = self.first_run(fill_descriptions="no")
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertTrue(any(g["id"] == "no_description" for g in out["gaps"]))     # the gap is still stated
        self.assert_no_crawl_step(out)

    def test_no_crawl_offered_within_the_cooldown(self):
        from jevscreen.http import Blocked

        def blocked(cfg, client, **kw):
            raise Blocked("https://www.tradingview.com/symbols/SZSE-300901/", 429, "http_429")
        self.deps.crawl_descriptions = blocked
        out = self.first_run()
        self.assertEqual(self.job(self.IDEA_ZH)["fill_default"]["state"], "blocked")
        self.assert_no_crawl_step(out)


class TestYesAfterAnOptOut(DefaultFillCase):
    def test_a_yes_after_the_result_fills_and_reranks(self):
        out = self.first_run(fill_descriptions="no")
        self.assertEqual(self.crawled, [])
        self.assertNotIn("Frostline Thermal", [r["name"] for r in out["top"]])
        n = len(self.spawned)
        out = self.front(self.IDEA_ZH, fill_descriptions="yes")          # the human changed their mind
        self.assertEqual(out["status"], "running", out.get("text_en"))
        self.assertEqual(len(self.spawned), n + 1)
        self.work(qs.idea_key(self.IDEA_ZH))
        out = qs.status(self.cfg, qs.idea_key(self.IDEA_ZH))
        self.assertEqual(self.crawled, [(["CN"], 1e9)])
        self.assertEqual(self.job(self.IDEA_ZH)["fill"]["added"], 2)
        self.assertIn("Frostline Thermal", [r["name"] for r in out["top"]])

    def test_a_yes_before_the_first_read_brings_the_default_fill_back(self):
        idea = self.IDEA_ZH
        self.consent_yes("zh")
        self.front(idea, idea_en="Suppliers of joints for humanoid robots", approve_budget=1,
                   fill_descriptions="no")
        with self.with_cn():
            self.work(qs.idea_key(idea))                          # stops at the key; no fill (opted out)
        self.assertEqual(self.job(idea)["fill_default"]["reason"], "opted_out")
        self.front(idea, fill_descriptions="yes")
        self.set_key()
        self.work(qs.idea_key(idea))
        out = qs.status(self.cfg, qs.idea_key(idea))
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(self.crawled, [(["CN"], 1e9)])
        self.assertEqual(self.job(idea)["fill_default"]["state"], "done")
        self.assertIn("Frostline Thermal", [r["name"] for r in out["top"]])     # in the first read, no question
        self.assertFalse(any(i["id"] == "fill_descriptions" for i in out["pending"] + out["ask_now"]))


class TestFillDocs(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_the_docs_say_when_the_fill_question_comes(self):
        for doc in ("AGENTS.md", "docs/AGENT_API.md"):
            text = " ".join((self.ROOT / doc).read_text(encoding="utf-8").split())     # ignore line wrapping
            self.assertNotIn("default fill was blocked or failed", text, doc)
            self.assertNotIn("(blocked, failed, timed out, not possible)", text, doc)
            self.assertIn("not after a block", text, doc)
            self.assertIn("another single market", text, doc)
        api = " ".join((self.ROOT / "docs" / "AGENT_API.md").read_text(encoding="utf-8").split())
        self.assertIn("timeout | stopped | opted_out", api)                 # every state the JSON can carry


class TestPageGapAfterAnOptOut(DefaultFillCase):
    def test_the_page_gap_line_drops_the_crawl_command_after_an_opt_out(self):
        res = {"idea": self.IDEA_ZH, "params": {"min_mcap_usd": 1e9},
               "gaps": {"no_description": [{"security_id": "SZSE:300901"}, {"security_id": "SSE:600902"}]}}
        [g] = page._gap_lines(res, {}, {})
        self.assertTrue(g["command"].startswith("jevscreen crawl-descriptions"))
        self.assertIn("crawl-descriptions", g["text_zh"])
        [g] = page._gap_lines(res, {}, {"crawl_hidden": True})
        self.assertIsNone(g["command"])
        self.assertNotIn("crawl-descriptions", g["text_zh"] + g["text_en"])
        self.assertFalse(qs.crawl_hidden(self.cfg, self.IDEA_ZH))
        self.consent_yes("zh")
        self.front(self.IDEA_ZH, idea_en="Suppliers of joints for humanoid robots", fill_descriptions="no")
        self.assertTrue(qs.crawl_hidden(self.cfg, self.IDEA_ZH))


if __name__ == "__main__":
    unittest.main()
