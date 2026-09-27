"""The ONE page per idea (owner decision 2026-09-27): prerequisites checklist, live progress, the scope-question slot
and the results on <home>/pages/<idea_key>.html, rewritten by the quickstart worker and reloading itself while work
runs. Synthetic jobs for every state (tests/onepage_states.py), fakes for the worker; no network, no real key."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)
from jevscreen import config, http, keys, page, pagestatus  # noqa: E402
import onepage_states as S  # noqa: E402
from test_page import DOM_SHIM, data_of, dom_text  # noqa: E402
from test_quickstart import CLEAR_ENV, IDEA, QuickCase, no_network  # noqa: E402

# Latin words a Chinese page may carry: names of products, sites and units (everything else is Chinese)
ZH_OK_WORDS = {"Python", "TradingView", "Yahoo", "FinanceDatabase", "Jev", "key", "AI", "TypeSafe", "OpenRouter",
               "Vercel", "Gateway", "SEC", "EDINET", "OpenDART", "MB", "credits", "duckdb", "uv", "MOPS", "B", "M"}


def live_texts(live: dict) -> list[str]:
    out = [live.get("phase_words"), live.get("ok_line")]
    for c in (live.get("checks") or []) + (live.get("optional") or []):
        out += [c.get("text"), c.get("fix")]
    out += [a.get("text") for a in live.get("alerts") or []]
    p = live.get("progress") or {}
    for it in p.get("items") or []:
        out += [it.get("label"), it.get("text"), it.get("eta")]
    out += [p.get("money"), p.get("updated")]
    return [t for t in out if t]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home), "LANG": "en_US.UTF-8"})
        env.start()
        self.addCleanup(env.stop)
        for k in CLEAR_ENV:
            os.environ.pop(k, None)
        self.cfg = config.Config(home=self.home).ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def live(self, state, lang="zh"):
        job = S.job_for(state, self.cfg, lang=lang)
        return job, pagestatus.build(self.cfg, lang=lang, job=job)


class TestStates(Case):
    def by_id(self, live):
        return {c["id"]: c for c in live["checks"]}

    def test_fresh_nothing_done(self):
        job, live = self.live("fresh")
        c = self.by_id(live)
        self.assertEqual([x["id"] for x in live["checks"]],
                         ["python", "consent", "key", "universe", "descriptions", "pack", "ready"])
        self.assertEqual((c["python"]["state"], c["consent"]["state"], c["key"]["state"]), ("ok", "need", "need"))
        self.assertIn("推荐先试 TypeSafe 官方；官方暂停注册时用 OpenRouter", c["key"]["fix"])
        self.assertEqual({c[k]["state"] for k in ("universe", "descriptions", "pack", "ready")}, {"wait"})
        self.assertEqual((live["phase"], live["all_ok"], live["refresh"]), ("wait_you", False, True))
        self.assertIsNone(live["progress"])                    # nothing started: the checklist says what is first
        self.assertEqual({o["state"] for o in live["optional"]}, {"opt"})

    def test_downloading_shows_mb_and_eta(self):
        job, live = self.live("downloading")
        c = self.by_id(live)
        self.assertEqual((c["universe"]["state"], c["descriptions"]["state"]), ("ok", "run"))
        self.assertIn("3,120", c["universe"]["text"])
        items = {i["id"]: i for i in live["progress"]["items"]}
        d = items["descriptions"]
        self.assertEqual((d["state"], d["pct"], d["text"]), ("run", 41, "6.3 / 15 MB"))
        self.assertTrue(d["eta"].startswith("还要"))
        self.assertEqual(items["universe"]["state"], "ok")
        self.assertEqual((items["l1"]["state"], items["l2"]["state"]), ("wait", "wait"))
        self.assertEqual((live["phase"], live["refresh"]), ("download", True))

    def test_waiting_for_the_key(self):
        job, live = self.live("waiting_key", "en")
        c = self.by_id(live)
        self.assertEqual(c["key"]["state"], "need")
        self.assertIn("try TypeSafe's official API first; OpenRouter when its sign-ups are paused", c["key"]["fix"])
        self.assertEqual({c[k]["state"] for k in ("universe", "descriptions", "pack")}, {"ok"})
        self.assertIn("2,154", c["descriptions"]["text"])
        self.assertEqual((live["phase"], live["refresh"]), ("wait_you", True))

    def test_key_rejected_is_a_red_cross_with_the_provider(self):
        job, live = self.live("key_rejected")
        k = self.by_id(live)["key"]
        self.assertEqual((k["state"], k["text"]), ("fail", "OpenRouter 不接受这个 key"))
        self.assertIn("重新创建", k["fix"])
        self.assertEqual((live["phase"], live["refresh"]), ("key", True))
        en = pagestatus.build(self.cfg, lang="en", job=job)
        self.assertEqual(self.by_id(en)["key"]["text"], "OpenRouter rejected the key")

    def test_blocked_is_red_and_stops_refreshing(self):
        job, live = self.live("blocked")
        u = self.by_id(live)["universe"]
        self.assertEqual(u["state"], "fail")
        self.assertIn("TradingView 暂时拒绝了请求", u["text"])
        self.assertEqual(len(live["alerts"]), 1)                # the cooldown of that block is not said twice
        self.assertIn("24 小时内不再请求它", live["alerts"][0]["text"])
        self.assertEqual((live["phase"], live["refresh"]), ("blocked", False))
        self.assertEqual({i["id"]: i["state"] for i in live["progress"]["items"]}["universe"], "fail")

    def test_a_cooldown_of_another_source_is_said_in_plain_words(self):
        from jevscreen import guard
        guard.mark_blocked(self.cfg, "sync-cninfo", url="https://www.cninfo.com.cn/x", status=403, reason="http_403")
        job, live = self.live("screening")
        self.assertEqual(len(live["alerts"]), 1)
        self.assertIn("巨潮资讯网在", live["alerts"][0]["text"])
        self.assertIn("不是你的操作问题", live["alerts"][0]["text"])
        en = pagestatus.build(self.cfg, lang="en", job=job)
        self.assertIn("CNINFO refused our requests", en["alerts"][0]["text"])

    def test_screening_shows_ai_reads_and_money(self):
        job, live = self.live("screening")
        c = self.by_id(live)
        self.assertEqual((c["key"]["state"], c["ready"]["state"]), ("ok", "run"))
        self.assertEqual(c["key"]["text"], "Jev key 已设置，并已验证可以付费（OpenRouter）")
        items = {i["id"]: i for i in live["progress"]["items"]}
        self.assertEqual((items["l1"]["state"], items["l1"]["text"]), ("ok", "2,154 / 2,154"))
        self.assertEqual((items["l2"]["state"], items["l2"]["pct"], items["l2"]["text"]), ("run", 37, "45 / 120"))
        self.assertIsNotNone(items["l2"]["eta"])
        self.assertEqual(live["progress"]["money"], "已花 $0.18（约 ¥1.32），你同意的上限 $1.00")
        en = pagestatus.build(self.cfg, lang="en", job=job)
        self.assertEqual(en["progress"]["money"], "Spent $0.18 of the $1.00 you approved")
        self.assertEqual((live["phase"], live["refresh"]), ("screen", True))

    def test_done_collapses_to_one_green_line(self):
        job, live = self.live("done")
        self.assertTrue(live["all_ok"])
        self.assertEqual(live["ok_line"], "准备就绪：安装、数据授权、Jev key（OpenRouter，已验证）、股票清单和公司简介都好了")
        self.assertEqual((live["phase"], live["refresh"], live["progress"]), ("done", False, None))

    def test_vercel_is_named_version_cannot_be_pinned(self):
        keys.write_key(self.cfg, "vercel", "vck_" + "fake" * 10)
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "vercel"}):
            zh = pagestatus.build(self.cfg, lang="zh", job=S.base_job())
            en = pagestatus.build(self.cfg, lang="en", job=S.base_job(lang="en"), )
        self.assertIn("Vercel AI Gateway（版本无法锁定）", self.by_id(zh)["key"]["text"])
        self.assertIn("Vercel AI Gateway (version cannot be pinned)", self.by_id(en)["key"]["text"])

    def test_one_language_per_page(self):
        """A zh page is fully Chinese (Latin only for names of products, sites and units), an en page has no CJK."""
        for state in S.STATES:
            for lang in ("zh", "en"):
                with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JEVSCREEN_HOME": tmp}):
                    cfg = config.Config(home=Path(tmp)).ensure()
                    job = S.job_for(state, cfg, lang=lang)
                    texts = live_texts(pagestatus.build(cfg, lang=lang, job=job))
                texts += list(page.ONE_PAGE_STRINGS[lang].values())
                for t in texts:
                    t = re.sub(r"\{\w+\}", "", t)
                    if lang == "en":
                        self.assertNotRegex(t, r"[㐀-鿿]", (state, t))
                    else:
                        words = set(re.findall(r"[A-Za-z][A-Za-z.]*", re.sub(r"https?://\S+", "", t)))
                        self.assertLessEqual(words, ZH_OK_WORDS | {"Gateway"}, (state, t))
                        # a text without Chinese is only numbers, units and names ('6.3 / 15 MB')
                        self.assertRegex(t, "[\u3400-\u9fff]|^[\\d\\s.,/%A-Za-z]+$", (state, t))

    def test_no_network_and_no_store(self):
        """The status block and the page write read files only: no socket, and no store file is created."""
        def boom(*a, **kw):
            raise AssertionError("network used")
        with mock.patch.object(socket.socket, "connect", boom), no_network():
            for state in S.STATES:
                job = S.job_for(state, self.cfg)
                pagestatus.build(self.cfg, lang="zh", job=job)
                self.assertIsNotNone(pagestatus.write(self.cfg, job))
        self.assertFalse(Path(self.cfg.db_path).exists())


class TestRenderedPage(Case):
    def page_html(self, state, lang="zh"):
        return S.write_state_page(self.cfg, state, lang=lang).read_text(encoding="utf-8")

    def test_refresh_while_running_and_gone_when_done(self):
        for state, want in (("fresh", True), ("downloading", True), ("waiting_key", True), ("key_rejected", True),
                            ("blocked", False), ("screening", True), ("done", False)):
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JEVSCREEN_HOME": tmp}):
                cfg = config.Config(home=Path(tmp)).ensure()
                html = S.write_state_page(cfg, state).read_text(encoding="utf-8")
            self.assertEqual('<meta http-equiv="refresh" content="3">' in html, want, state)
            self.assertIn("default-src 'none'", html)            # still self-contained

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_sections_in_order_and_no_card_ui(self):
        html = self.page_html("screening")
        text = dom_text(self, html)["text"]
        order = [text.index(f"\n{w}\n") for w in ("准备情况", "进度", "结果")]
        self.assertEqual(order, sorted(order))
        self.assertIn("AI 用年报/简介核对", text)
        self.assertIn("45 / 120", text)
        self.assertNotIn("帮 AI 把范围定准", text)                # the questions slot is hidden while empty
        self.assertIn("结果会在筛选完成后出现在这里", text)
        self.assertNotIn("jevscreen answer", html)
        done = dom_text(self, self.page_html("done"))["text"]
        self.assertIn("准备就绪：", done)
        self.assertIn("前 3 名一览", done)
        self.assertIn("每家的证据（3 家，点开看）", done)
        self.assertIn("年报摘录未提到（缺口）", done)             # the evidence and its labels in the rows
        self.assertIn("出处：巨潮资讯 · 年报 · 2026年4月1日", done)
        self.assertNotIn("进度", done)                            # finished: no progress block

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_green_line_is_collapsed_and_rows_are_closed(self):
        html = self.page_html("done")
        probe = ("const d=all.filter(n=>n.tagName==='details');"
                 "console.log(JSON.stringify(d.map(n=>[n.className,!!n.open])));")
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            h = Path(tmp) / "h.js"
            h.write_text(DOM_SHIM + f"\nconst BLOB={json.dumps(blob)};\n(function(){{{js}}})();\n{probe}",
                         encoding="utf-8")
            import subprocess
            r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn(["okline", False], got)
        self.assertEqual({o for c, o in got if c == "row"}, {False})

    def test_plain_text_carries_the_status(self):
        data = page.read_page_data(S.write_state_page(self.cfg, "key_rejected"))
        text = page.render_text(data)
        self.assertIn("[等你处理 key]", text)
        self.assertIn("✗ OpenRouter 不接受这个 key（重新创建一个 key", text)
        self.assertIn("结果会在筛选完成后出现在这里", text)
        html = page.render_page(data)
        self.assertIn("OpenRouter 不接受这个 key", re.search(r"<noscript>(.*?)</noscript>", html, re.S).group(1))
        done = page.render_text(page.read_page_data(S.write_state_page(self.cfg, "done")))
        self.assertIn("✓ 准备就绪：", done)
        self.assertIn("#1 青澜科技", done)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_scope_questions_build_the_line_to_paste(self):
        """Stage 2 fills data['questions']; the page shows answer buttons and builds the line for the AI."""
        data = page.read_page_data(S.write_state_page(self.cfg, "done", lang="en"))
        data["questions"] = {"template": 'jevscreen quickstart "robots" --scope "{answers}"',
                             "items": [{"id": "q1", "text": "Count makers of parts?",
                                        "options": [{"label": "Yes", "value": "1a"}, {"label": "No", "value": "1b"}]},
                                       {"id": "q2", "text": "Only listed in Asia?",
                                        "options": [{"label": "Yes", "value": "2a"}, {"label": "No", "value": "2b"}]}]}
        html = page.render_page(data)
        drive = ("function b(t,i){return all.filter(n=>n.tagName==='button'&&n._text===t)[i];}"
                 "const ta=all.filter(n=>n.tagName==='textarea')[0];const before=ta.value;"
                 "b('No',0).onclick();b('Yes',1).onclick();"
                 "const cp=all.filter(n=>n.tagName==='button'&&n._text==='Copy')[0];"
                 "console.log(JSON.stringify({before:before,line:ta.value,disabled:cp.disabled}));")
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            h = Path(tmp) / "h.js"
            h.write_text(DOM_SHIM + f"\nconst BLOB={json.dumps(blob)};\n(function(){{{js}}})();\n{drive}",
                         encoding="utf-8")
            import subprocess
            r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got, {"before": "", "line": 'jevscreen quickstart "robots" --scope "1b 2a"',
                               "disabled": False})
        self.assertIn("Help the AI get the scope right (optional)", dom_text(self, html)["text"])


    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_answers_and_opened_originals_survive_the_3s_reload(self):
        """Review: the page reloads every 3 s while work runs; the chosen scope answers, the paste line and an opened
        'show original' used to vanish on every reload (only the open rows were kept in sessionStorage)."""
        sp = S.write_state_page(self.cfg, "done", lang="en")
        job = S.job_for("done", self.cfg, lang="en")
        job.update(state="running", fill={"answer": "yes", "at": S.iso(S.T0)})   # results + refresh together
        pagestatus.write(self.cfg, job)
        data = page.read_page_data(sp)
        self.assertTrue(data["live"]["refresh"])
        data["rows"][0]["quote"].update(text="Original words.", text_x=True, text_tr="Translated words.")
        data["questions"] = {"template": 'jevscreen quickstart "x" --scope "{answers}"',
                             "items": [{"id": "q1", "text": "Count makers of parts?",
                                        "options": [{"label": "Yes", "value": "1a"}, {"label": "No", "value": "1b"}]}]}
        html = page.render_page(data)
        self.assertIn('<meta http-equiv="refresh" content="3">', html)
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
        probe = ("function btn(t){return all.filter(n=>n.tagName==='button'&&n._text===t);}"
                 "function state(){const ta=all.filter(n=>n.tagName==='textarea')[0];"
                 "const orig=all.filter(n=>n.className==='orig'&&n.style.display!==undefined)[0];"
                 "return {on:btn('Yes').concat(btn('No')).filter(n=>n.className==='on').map(n=>n._text),"
                 "line:ta.value,copy:btn('Copy')[0].disabled,orig:orig.style.display};}")
        harness = (DOM_SHIM + "const STORE={};global.window={location:{search:'',hash:''},scrollY:0,"
                   "sessionStorage:{getItem:k=>(k in STORE?STORE[k]:null),setItem:(k,v)=>{STORE[k]=String(v);}}};\n"
                   f"const BLOB={json.dumps(blob)};\nfunction run(){{{js}}}\n{probe}\n"
                   "run();btn('Yes')[0].onclick();btn('Show original')[0].onclick();const before=state();"
                   "all.length=0;app.children=[];all.push(app);run();"      # the 3 s reload: a fresh DOM, same storage
                   "console.log(JSON.stringify({before:before,after:state()}));")
        with tempfile.TemporaryDirectory() as tmp:
            h = Path(tmp) / "h.js"
            h.write_text(harness, encoding="utf-8")
            import subprocess
            r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        want = {"on": ["Yes"], "line": 'jevscreen quickstart "x" --scope "1a"', "copy": False, "orig": "block"}
        self.assertEqual(got["before"], want)
        self.assertEqual(got["after"], want)


    def test_a_dead_worker_is_shown_as_stopped(self):
        """Review: a killed worker (laptop sleep, crash) left 'running' in the job; the page kept a spinner and a
        growing ETA. A page built after the heartbeat went stale says the work stopped."""
        import datetime as dt
        old = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0) - dt.timedelta(minutes=10)
        job = S.job_for("screening", self.cfg, now=old)
        live = pagestatus.build(self.cfg, lang="zh", job=job)
        self.assertEqual((live["phase"], live["phase_words"]), ("failed", "后台任务中断了"))
        self.assertIn("后台任务中断了：重跑同一条命令", " ".join(a["text"] for a in live["alerts"]))
        self.assertNotIn("run", [it["state"] for it in (live["progress"] or {}).get("items") or []])
        self.assertEqual(job["state"], "running")                  # the job file itself is left to quickstart
        fresh = pagestatus.build(self.cfg, lang="zh", job=S.job_for("screening", self.cfg))
        self.assertEqual(fresh["phase"], "screen")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_page_notices_the_worker_died_after_it_was_written(self):
        """Nobody rewrites the page once the worker is dead: the page compares the last heartbeat with the clock."""
        import datetime as dt
        old = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0) - dt.timedelta(minutes=10)
        data = page.read_page_data(S.write_state_page(self.cfg, "screening"))
        data["live"] = pagestatus.build(self.cfg, lang="zh", job=S.job_for("screening", self.cfg, now=old), now=old)
        self.assertEqual(data["live"]["phase"], "screen")         # written while the worker was alive
        text = dom_text(self, page.render_page(data))["text"]
        self.assertIn("后台任务中断了：重跑同一条命令", text)
        self.assertNotIn("AI 用年报/简介核对", text.split("进度")[0])  # the pill no longer says it is screening
        alive = dom_text(self, self.page_html("screening"))["text"]
        self.assertNotIn("后台任务中断了", alive)


class TestWrite(Case):
    def test_write_keeps_the_results_and_replaces_only_the_status(self):
        sp = S.write_state_page(self.cfg, "done")
        before = page.read_page_data(sp)
        job = S.job_for("done", self.cfg)
        job.update(state="running", fill={"answer": "yes", "at": S.iso(S.T0)})   # the optional fill runs
        pagestatus.write(self.cfg, job)
        after = page.read_page_data(sp)
        self.assertEqual((after["run_id"], after["rows"]), (before["run_id"], before["rows"]))
        self.assertEqual(after["live"]["phase"], "fill")
        self.assertTrue(after["live"]["refresh"])
        self.assertIn('<meta http-equiv="refresh"', sp.read_text(encoding="utf-8"))

    def test_a_new_idea_gets_a_page_in_its_language(self):
        job = S.base_job("humanoid robots", "en")
        sp = pagestatus.write(self.cfg, job)
        data = page.read_page_data(sp)
        self.assertEqual((data["lang"], data["run_id"], data["rows"]), ("en", None, []))
        self.assertEqual(sp, page.stable_path(self.cfg, "humanoid robots"))

    def test_a_secret_is_never_written(self):
        keys.write_key(self.cfg, "openrouter", S.FAKE_OR_KEY)
        job = S.base_job("robots " + S.FAKE_OR_KEY, "en")
        said = []
        self.assertIsNone(pagestatus.write(self.cfg, job, warn=said.append))
        self.assertFalse(page.stable_path(self.cfg, job["idea"]).exists())
        self.assertTrue(said and "secret" in said[0])
        self.assertNotIn(S.FAKE_OR_KEY, said[0])


class TestNoBrowserInTests(unittest.TestCase):
    def test_the_suite_never_opens_a_real_browser(self):
        self.assertFalse(page.can_open_browser(platform="darwin"))
        self.assertTrue(page.can_open_browser(env={}, platform="darwin"))


class TestBodyBytes(unittest.TestCase):
    def test_the_callback_hears_every_chunk(self):
        class Reader:
            headers = {"Content-Length": "40000"}

            def __init__(self):
                self.left = 40000

            def read1(self, n):
                n = min(n, self.left, 16384)
                self.left -= n
                return b"x" * n

            def read(self, n):
                return self.read1(n)
        seen = []
        import time
        dl = http._Deadline("https://example.test/x", None, 60.0, time.monotonic())
        with http.on_body_bytes(lambda n, t: seen.append((n, t))):
            body = http._read_body(Reader(), 10**6, dl, 200)
        self.assertEqual(len(body), 40000)
        self.assertEqual(seen[-1], (40000, 40000))
        self.assertGreater(len(seen), 1)
        seen.clear()
        http._read_body(Reader(), 10**6, dl, 200)              # outside the block: nobody is told
        self.assertEqual(seen, [])


class TestWorkerWritesThePage(QuickCase):
    def test_front_writes_the_fresh_page_and_does_not_open_it(self):
        with no_network():
            out = self.front()
        self.assertEqual(out["status"], "needs_human")
        sp = page.stable_path(self.cfg, IDEA)
        data = page.read_page_data(sp)
        self.assertEqual((data["lang"], data["run_id"], data["live"]["phase"]), ("en", None, "wait_you"))
        self.assertIn('<meta http-equiv="refresh" content="3">', sp.read_text(encoding="utf-8"))
        self.assertEqual(self.calls.open, 0)

    def test_the_worker_opens_it_once_at_its_first_step_and_ends_without_refresh(self):
        seen = {}

        def fetch(cfg, result, kw):            # during the on-demand fetch the page shows it, reloading
            sp = page.stable_path(self.cfg, IDEA)
            seen["html"] = sp.read_text(encoding="utf-8")
            seen["opened_by_then"] = self.calls.open
            return {"result": result, "update": {"skipped": "nothing_fetched"},
                    "fetch": {"status": "ok", "fetched": {}, "seconds": 1.0, "update": {}, "questions": [],
                              "next_command": None, "summary_zh": "", "summary_en": ""}}
        self.fetch_impl = fetch
        out = self.done_flow()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        self.assertEqual(seen["opened_by_then"], 1)                  # opened at the first step, before the result
        self.assertIn('<meta http-equiv="refresh" content="3">', seen["html"])
        self.assertIn("Fetching annual reports", seen["html"])
        sp = page.stable_path(self.cfg, IDEA)
        html = sp.read_text(encoding="utf-8")
        self.assertNotIn('http-equiv="refresh"', html)
        data = data_of(html)
        self.assertEqual(data["run_id"], out["run_id"])
        self.assertTrue(data["live"]["all_ok"])
        self.assertEqual(data["live"]["phase"], "done")
        self.assertEqual(self.calls.open, 1)                         # never twice
        self.assertTrue(out["page_opened"])
        job = self.job()
        self.assertTrue(job["page_opened"])
        # a second run of the idea (a changed scope) does not open another tab
        self.front(new_run=True)
        self.work()
        self.assertEqual(self.calls.open, 1)

    def test_no_open_is_respected(self):
        out = self.done_flow(no_open=True)
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.calls.open, 0)
        self.assertTrue(page.stable_path(self.cfg, IDEA).exists())


if __name__ == "__main__":
    unittest.main()
