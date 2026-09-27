"""The scope questions and your AI's review end to end (scope design §4-§9), offline: a synthetic universe
of payment companies, a fake Jev that labels POS-terminal makers 'hardware', the review flow (prepare -> judge ->
decide), the quickstart gating (needs_agent, ask_now, timeout, relay), the one page's scope-question slot and `why`.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_screen as TS  # noqa: E402
from jevscreen import calib, cli, config, page, quickstart as qs, review, review_cli, scope, screen, store  # noqa: E402
from test_quickstart import IDEA as QIDEA, QuickCase  # noqa: E402

IDEA = "Digital payments in Southeast Asia"
D = dt.date(2026, 9, 26)
CJK = re.compile(r"[㐀-鿿]")
SUPPLIERS = 12
HARDWARE = 8


def seed_payments(cfg) -> None:
    """20 payment companies: 12 run payment services, 8 make POS terminals sold to payment operators."""
    cols = ("security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
            "company_key", "last_seen_snapshot", "active")
    with store.session(cfg) as con:
        snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="t", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
        prof = store.record_snapshot(con, source_id="tradingview_profile", kind="t", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
        secs, mkt, descs = [], [], []
        for i in range(1, SUPPLIERS + HARDWARE + 1):
            hw = i > SUPPLIERS
            sid = f"SGX:P{i:02d}"
            isin = f"SG00000000{i:02d}"
            name = f"{'Terminal' if hw else 'Pay'} Maker {i}" if hw else f"Pay Service {i}"
            secs.append((sid, "SGX", f"P{i:02d}", name, isin, "Singapore", "stock", "common", True,
                         store.company_key(isin, sid), snap, True))
            mkt.append((sid, D, 5e9 - i * 1e8, 1e6, snap))
            text = (f"{name} makes POS terminal hardware for digital payments operators in Southeast Asia."
                    if hw else f"{name} runs digital payments and wallets for merchants in Southeast Asia.")
            descs.append((sid, "tradingview_profile", store.company_key(isin, sid), text, prof))
        store.upsert_many(con, "securities", cols, secs)
        store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "avg_volume_10d",
                                                "snapshot_id"), mkt)
        store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"),
                          descs)


class PayJev(TS.FakeJev):
    """L1 core / L2 explicit for every payment text; the facet 'role' of a POS-terminal maker is hardware (0.9)."""

    def classify(self, items, question):
        if question.key in ("fit", "evidence"):
            self.classified.append((list(items), question))
            out = []
            for i in range(0, len(items), self.pack_size):
                pack = items[i:i + self.pack_size]
                self._spent += self.COST
                self._sent += 1
                for it in pack:
                    if question.key == "fit":
                        lab, pr = "core", {"core": 0.9, "adjacent": 0.05, "unrelated": 0.03, "insufficient": 0.02}
                    else:
                        lab, pr = "explicit", {"explicit": 0.85, "partial": 0.1, "contradicted": 0.0,
                                               "insufficient": 0.05}
                    out.append({"item_id": it.item_id, "label": lab, "probs": pr, "request_id": f"r{self._sent}",
                                "status": "ok", "error": None, "cached": False})
            return out
        return super().classify(items, question)


class FlowCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        safe_env.apply()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.cfg = config.Config(home=self.home)
        seed_payments(self.cfg)
        p = mock.patch.object(TS, "FACET_KEYWORDS", [("pos terminal", "role", "hardware", 0.9)])
        p.start()
        self.addCleanup(p.stop)
        sv = {**calib.new_sieve(IDEA), "facets": {"category": "digital payments", "target": "Southeast Asia"},
              "facets_zh": {"category": "数字支付", "target": "东南亚"}}
        calib.save_sieve(calib.sieve_path(self.cfg, IDEA), sv)
        self.base = screen.screen(self.cfg, IDEA, jev_factory=self.factory, keywords_fn=TS.FakeKeywords(),
                                  sieve="auto", facet_scan=True, max_out=20, min_mcap_usd=0,
                                  out_dir=self.home / "base", translate=False)

    def factory(self, cfg, *, run_id, layer, budget_usd, dry_run):
        return PayJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run)

    def answers_file(self, deck, fn) -> Path:
        ans = {str(it["n"]): a for it in deck["items"] if (a := fn(it)) is not None}
        p = self.home / f"answers_{deck['part']}.json"
        p.write_text(json.dumps({"format": review.ANSWERS_FORMAT, "deck_id": deck["deck_id"], "answers": ans,
                                 "agent": "test-agent"}), encoding="utf-8")
        return p

    @staticmethod
    def yes_all(it):
        a = {"v": "yes", "level": "explicit", "quote_ids": [1], "why": "它自己做数字支付"}
        if it.get("held_sid"):
            a.update(v="no", chip="k", why="卖终端给支付运营方", in_group=True, short="支付终端")
            a.pop("level")
        return a


class ReviewFlow(FlowCase):
    def test_prepare_judge_decide(self):
        self.assertEqual(self.base["status"], "ok")
        self.assertEqual(len(self.base["rows"]), 20)
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        self.assertEqual(rv["agent_review"]["state"], "pending")
        [prov] = rv["provisional"]
        self.assertEqual((prov["kind"], len(prov["side_v"])), ("role.hardware", 8))
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        self.assertLessEqual(len(deck["items"]), review.DECK_A_MAX)
        self.assertEqual(sum(1 for it in deck["items"] if it["held_sid"] == prov["sid"]), 8)
        # the page of a pending review hides the questions
        self.assertEqual(review_cli.page_questions(self.cfg, self.base, "zh"), {"items": []})
        code, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, self.yes_all)))
        self.assertEqual(code, 0)
        self.assertTrue(out["applied"])
        v1 = out["run_id"]
        self.assertNotEqual(v1, self.base["run_id"])
        [q] = out["questions"]
        self.assertEqual(q["kind"], "role.hardware")
        self.assertIn("支付终端", q["question_zh"])                               # the agent's descriptor
        self.assertTrue(q["question_zh"].startswith("AI 读摘录后认为"))
        self.assertIsNone(CJK.search(q["question_en"]))
        self.assertIsNotNone(out["part_b"])
        _d, r1 = review_cli.run_files(self.cfg, v1)
        self.assertEqual(len(r1["rows"]), 20)                                   # held answers are not applied
        self.assertEqual(r1["params"]["change_kind"], "agent")
        agent = review.agent_verdicts(self.cfg, IDEA)
        self.assertEqual({v["state"] for v in agent.values() if v.get("held_kind")}, {"held"})
        # the page's slot: the question with 要 / 不要 / 不确定 building the decide line
        pq = review_cli.page_questions(self.cfg, r1, "zh")
        self.assertEqual([o["label"] for o in pq["items"][0]["options"]], ["要", "不要", "不确定"])
        self.assertEqual(pq["items"][0]["options"][1]["value"], f"{q['sid']}=no")
        self.assertEqual(pq["template"], f'jevscreen decide "{{answers}}" --run {v1} --via page')
        data = page.read_page_data(page.stable_path(self.cfg, IDEA))
        self.assertEqual(data["questions"]["items"][0]["id"], q["sid"])
        # the human says 不要: the 8 terminal makers leave, the diff says why, free
        code, out2 = review_cli.decide(self.cfg, f"{q['sid']}=no", v1, via="page")
        self.assertEqual(code, 0)
        _d, r2 = review_cli.run_files(self.cfg, out2["run_id"])
        self.assertEqual(len(r2["excluded_by_scope"]), 8)
        self.assertEqual(len(r2["rows"]), 12)
        self.assertEqual(r2["cost_usd"], 0.0)
        self.assertIn("按你的范围回答", out2["diff_zh"])
        self.assertIn("your scope answer", out2["diff_en"])
        sv = calib.load_sieve(calib.sieve_path(self.cfg, IDEA))
        [e] = sv["scope_answers"]
        self.assertEqual((e["answer"], e["relayed_via"], e["raw_tokens"]), ("no", "page", f"{q['sid']}=no"))
        self.assertEqual(sv["rules"], [])                                        # never a rule
        # keep= brings one back (a human pin wins), clear= undoes it
        target = r2["excluded_by_scope"][0]["security_id"]
        code, out3 = review_cli.decide(self.cfg, f"keep={target}", out2["run_id"])
        _d, r3 = review_cli.run_files(self.cfg, out3["run_id"])
        self.assertIn(target, [r["security_id"] for r in r3["rows"]])
        self.assertEqual(next(r for r in r3["rows"] if r["security_id"] == target)["user_pin_via"], "override_agent")
        code, out4 = review_cli.decide(self.cfg, f"clear={target}", out3["run_id"])
        _d, r4 = review_cli.run_files(self.cfg, out4["run_id"])
        self.assertNotIn(target, [r["security_id"] for r in r4["rows"]])
        # the undo of the scope answer: s1=yes brings the kind back
        code, out5 = review_cli.decide(self.cfg, f"{q['sid']}=yes", out4["run_id"])
        _d, r5 = review_cli.run_files(self.cfg, out5["run_id"])
        self.assertEqual(len(r5["rows"]), 20)
        hw = [r for r in r5["rows"] if r["name"].startswith("Terminal")]
        self.assertEqual({r.get("agent_state") for r in hw}, {"not_applied"})  # 要: matching agent no's not applied

    def test_skip_and_omitted_questions(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="en")
        code, out = review_cli.judge(self.cfg, rv["agent_review"]["deck_id"], skip=True)
        self.assertEqual(code, 0)
        self.assertEqual(out["run_id"], self.base["run_id"])                     # the system's list stands
        [q] = out["questions"]
        # a decide that omits the open question records it as skipped: never asked again
        code, out2 = review_cli.decide(self.cfg, f"keep={self.base['rows'][0]['security_id']}", self.base["run_id"])
        sv = calib.load_sieve(calib.sieve_path(self.cfg, IDEA))
        self.assertEqual([(e["sid"], e["answer"]) for e in sv["scope_answers"]], [(q["sid"], "skipped")])
        _d, r2 = review_cli.run_files(self.cfg, out2["run_id"])
        self.assertEqual(scope.find_splits(r2["rows"], r2["facets"], sv, top_n=20), [])

    def test_an_old_tab_never_turns_an_answer_into_a_skip(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        _c, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, self.yes_all)))
        [q] = out["questions"]
        review_cli.decide(self.cfg, f"{q['sid']}=no", out["run_id"])
        # the old page (the judged version) is still open in a tab: a keep= from it must not skip s1
        review_cli.decide(self.cfg, f"keep={self.base['rows'][0]['security_id']}", out["run_id"], via="page")
        sv = calib.load_sieve(calib.sieve_path(self.cfg, IDEA))
        self.assertEqual([(e["sid"], e["answer"]) for e in sv["scope_answers"]], [(q["sid"], "no")])

    def test_cli_json(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="en")
        with contextlib.redirect_stdout(io.StringIO()) as so, mock.patch.object(config, "load", return_value=self.cfg):
            code = cli.main(["judge", "--deck", rv["agent_review"]["deck_id"], "--skip", "--json"])
        got = json.loads(so.getvalue())
        self.assertEqual((code, got["status"], got["exit_code"], got["applied"]), (0, "ok", 0, True))
        sid = got["questions"][0]["sid"]
        with contextlib.redirect_stdout(io.StringIO()) as so, mock.patch.object(config, "load", return_value=self.cfg):
            code = cli.main(["decide", f"{sid}=?", "--run", self.base["run_id"], "--via", "chat", "--json"])
        got = json.loads(so.getvalue())
        self.assertEqual((code, got["status"]), (0, "ok"))
        self.assertTrue(got["diff_en"].startswith("Updated with your answers"))
        with contextlib.redirect_stdout(io.StringIO()) as so, contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(config, "load", return_value=self.cfg):
            code = cli.main(["judge", "--deck", "adeck-nope", "--skip", "--json"])
        self.assertEqual(code, 1)

    def test_no_paid_fallback_without_an_approval(self):
        path = calib.sieve_path(self.cfg, IDEA)
        sv = calib.load_sieve(path)
        sv["rules"] = ["mention_only"]                      # the L2 question changed: rank_only cannot apply it
        calib.save_sieve(path, sv)
        with self.assertRaises(review.AgentError) as cm:
            review_cli.decide(self.cfg, f"keep={self.base['rows'][0]['security_id']}", self.base["run_id"])
        self.assertIn("ask the human first", cm.exception.text_en)
        self.assertIn("--from-run", cm.exception.text_en)
        self.assertNotIn("rank_only", cm.exception.text_zh)                     # the zh text is Chinese only
        self.assertNotIn("screened again", cm.exception.text_zh)
        self.assertEqual(review_cli.paid_text({"paid_fallback_usd": 0.031}, "zh"),
                         review_cli.TEXT["zh"]["paid"].format(c="$0.03"))
        self.assertEqual(review_cli.paid_text({"cost_usd": 0.0}, "en"), "")
        pins = calib.pins(calib.load_sieve(path))
        self.assertIn(self.base["rows"][0]["security_id"], pins)              # the answer itself is saved

    def test_bad_tokens_and_files(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        with self.assertRaises(review.AgentError):
            review_cli.parse_tokens("s1=maybe")
        with self.assertRaises(review.AgentError):
            review_cli.decide(self.cfg, "c9=yes", self.base["run_id"])
        with self.assertRaises(review.AgentError):
            review_cli.decide(self.cfg, "keep=NOPE:1", self.base["run_id"])
        bad = self.home / "bad.json"
        bad.write_text(json.dumps({"deck_id": "adeck-other-A", "answers": {}}), encoding="utf-8")
        with self.assertRaises(review.AgentError):
            review_cli.judge(self.cfg, rv["agent_review"]["deck_id"], file=str(bad))
        # the CLI: exit 1 with a zh / en message, no traceback
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as so, \
                mock.patch.object(config, "load", return_value=self.cfg):
            code = cli.main(["decide", "s1=bogus", "--run", self.base["run_id"], "--json"])
        self.assertEqual(code, 1)
        self.assertIn("error_zh", json.loads(so.getvalue()))

    def test_why_explains_scope_and_agent_removals(self):
        from jevscreen import why
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        first_top = next(it for it in deck["items"] if it["group"] == "top")

        def fn(it):
            if it["n"] == first_top["n"]:
                return {"v": "no", "chip": "e", "quote_ids": [1], "why": "只是转售"}
            return self.yes_all(it)
        _c, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, fn)))
        [q] = out["questions"]
        _c, out2 = review_cli.decide(self.cfg, f"{q['sid']}=no", out["run_id"])
        runs = why.list_runs(self.cfg)
        ref = why.pick_run(runs, out2["run_id"])
        with store.session(self.cfg, read_only=True) as con:
            ctx = why.load_ctx(self.cfg, ref, con)
            hw = ctx.result["excluded_by_scope"][0]
            ex = why.explain(ctx, why.resolve_target(ctx, hw["security_id"]))
            self.assertEqual(ex["stop"], "scope_removed")
            self.assertIn("按你的范围回答", ex["plain_zh"])
            self.assertIn("推断", ex["plain_zh"])
            self.assertIn("your scope answer", ex["plain_en"])
            ag = ctx.result["excluded_by_agent"][0]
            ex = why.explain(ctx, why.resolve_target(ctx, ag["security_id"]))
            self.assertEqual(ex["stop"], "agent_removed")
            self.assertIn("你的 AI 判断不要：只是转售", ex["plain_zh"])
            self.assertTrue(any("decide" in (s.get("command") or "") for c in ex["changes"] for s in c["steps"]))


class Concurrency(FlowCase):
    def judged(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        _c, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, self.yes_all)))
        return out

    def test_decide_during_a_fill_is_queued_and_the_fill_reapplies(self):
        from jevscreen import guard
        out = self.judged()
        [q] = out["questions"]
        job = qs.new_job(IDEA, lang="zh", min_mcap=0, countries=None)
        job.update(state="running", worker={"heartbeat_at": qs.iso(qs.now_utc())})
        qs.save_job(self.cfg, job)
        lk = guard.budget_lock(self.cfg, qs.LOCK)
        lk.__enter__()
        try:
            code, got = review_cli.decide(self.cfg, f"{q['sid']}=no", out["run_id"])
        finally:
            lk.__exit__(None, None, None)
        self.assertEqual((code, got["queued"], got["applied"]), (0, True, False))
        self.assertIn("补简介还在进行", got["text_zh"])
        sv = calib.load_sieve(calib.sieve_path(self.cfg, IDEA))
        self.assertEqual(sv["scope_answers"][0]["answer"], "no")                 # saved even while queued
        self.assertTrue(qs.read_inbox(self.cfg, qs.idea_key(IDEA)).get("reapply"))
        # the fill's end: its result is re-ranked with the answers (rank_only, change_kind reapply)
        _d, v1 = review_cli.run_files(self.cfg, out["run_id"])
        w = qs.Worker(self.cfg, qs.load_job(self.cfg, qs.idea_key(IDEA)), qs.Deps())
        w.result = v1
        w.reapply_if_needed((sv["version"], review.version_of(self.cfg, IDEA)))
        self.assertEqual(w.result["params"]["change_kind"], "reapply")
        self.assertEqual(len(w.result["excluded_by_scope"]), 8)
        self.assertEqual(qs.read_inbox(self.cfg, qs.idea_key(IDEA)), {})

    def test_a_stale_sieve_is_read_again_once(self):
        out = self.judged()
        [q] = out["questions"]
        real = calib.save_sieve
        calls = []

        def flaky(path, sv, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise calib.SieveStale("changed meanwhile")
            return real(path, sv, **kw)
        with mock.patch.object(calib, "save_sieve", side_effect=flaky):
            code, got = review_cli.decide(self.cfg, f"{q['sid']}=no", out["run_id"])
        self.assertEqual((code, got["applied"], len(calls)), (0, True, 2))

    def test_page_tokens_parse(self):
        out = self.judged()
        _d, r1 = review_cli.run_files(self.cfg, out["run_id"])
        for lang in ("zh", "en"):
            pq = review_cli.page_questions(self.cfg, r1, lang)
            line = " ".join(o["value"] for it in pq["items"] for o in it["options"][:1])
            self.assertTrue(review_cli.parse_tokens(line))
            text = " ".join([it["text"] + it["note"] for it in pq["items"]] + [o["label"] for it in pq["items"]
                                                                                for o in it["options"]])
            if lang == "en":
                self.assertIsNone(CJK.search(text), text)
            else:
                self.assertNotIn("digital payments", text)
                self.assertNotIn("Keep", text)


class FacetsInput(unittest.TestCase):
    def test_a_place_target_is_never_a_company_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(home=Path(tmp))
            job = {"idea": "东南亚的数字支付", "facets_input": {
                "en": {"category": "digital payments", "target": "Southeast Asia"},
                "zh": {"category": "数字支付", "target": "东南亚"},
                "implied_no": [{"key": "geo.outside_only", "because": "东南亚"}]}}
            notes = qs.apply_facets(cfg, job, names=(["Asia", "Asia Pacific Wire & Cable"], ["ASIA"]))
            self.assertEqual(notes, [])
            sv = calib.load_sieve(calib.sieve_path(cfg, "东南亚的数字支付"))
            self.assertEqual(sv["facets"]["target"], "Southeast Asia")
            self.assertEqual([(e["family"], e["value"], e["because"]) for e in sv["scope_answers"]],
                             [("geo", "outside_only", "东南亚")])
            self.assertTrue(job["facets_applied"])


class QuickstartGating(QuickCase):
    review = "keep"

    def test_needs_agent_then_judge_then_relay(self):
        out = self.done_flow()
        self.assertEqual((out["status"], out["exit_code"]), ("needs_agent", 11))
        self.assertEqual([i["id"] for i in out["pending"]], ["agent_review"])
        self.assertEqual(out["next_command"], out["pending"][0]["record_command"])
        self.assertIn("agent_review", out["next_action_en"])
        self.assertEqual(out["text_zh"] if out["lang"] == "zh" else out["text_en"],
                         qs.STRINGS[out["lang"]]["review_wait"])                 # the list is not relayed yet
        self.assertNotIn("RoboCorp", out["text_en"])
        live = page.read_page_data(Path(out["page"]))["live"]
        self.assertTrue(live["refresh"])
        self.assertEqual(live["phase"], "review")
        self.assertIn("checking the excerpts", " ".join(a["text"] for a in live["alerts"]))
        deck_id = out["pending"][0]["deck_id"]
        # the same idea again while the review waits: no rerun, still needs the agent
        again = self.front()
        self.assertEqual(again["status"], "needs_agent")
        _c, j = review_cli.judge(self.cfg, deck_id, skip=True)
        self.assertTrue(j["applied"])
        done = qs.status(self.cfg, qs.idea_key(QIDEA))
        self.assertEqual((done["status"], done["exit_code"]), ("done", 0))
        self.assertIn("RoboCorp", done["text_en"])
        self.assertEqual(done["agent_review"]["state"], "skipped")
        self.assertEqual(done["ask_now"], [])
        self.assertFalse(self.job()["review"].get("relayed"))                   # nothing asked: no chat round used
        self.assertEqual(qs.front(self.cfg, QIDEA, spawn=lambda c, k: None)["status"], "done")   # $0, reused
        # the facet layer's cost is part of the approval's spend
        res = json.loads((Path(self.job()["result"]["output_dir"]) / "results.json").read_text(encoding="utf-8"))
        self.assertGreater(res["layers"]["facet"]["cost_usd"], 0)
        self.assertAlmostEqual(done["spent_usd"], sum(self.job()["approval"]["costs"].values())
                               + self.job()["approval"].get("canary_usd", 0.0), places=6)

    def test_timeout_finalizes_without_the_agent(self):
        self.done_flow()
        job = self.job()
        job["agent_review"]["created_at"] = qs.iso(qs.now_utc() - dt.timedelta(seconds=review.AGENT_REVIEW_TIMEOUT_S
                                                                                + 5))
        qs.save_job(self.cfg, job)
        out = qs.status(self.cfg, qs.idea_key(QIDEA))
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["agent_review"]["state"], "timed_out")
        self.assertIn(qs.STRINGS["en"]["review_timeout"], out["notes"])

    def test_precedence_and_ask_now(self):
        self.done_flow()
        job = self.job()
        job["review"] = {"run_id": job["result"]["run_id"], "answered": {}, "relayed": False,
                         "questions": [{"sid": f"s{i}", "question_zh": "问", "question_en": "Q", "effect_zh": "",
                                        "effect_en": "", "tokens": {"yes": f"s{i}=yes"}} for i in (1, 2, 3)],
                         "escalations": [{"cid": "c1", "reason": "E2", "in_relayed_top": True,
                                          "question_zh": "问", "question_en": "Q c1"},
                                         {"cid": "c2", "reason": "E1", "in_relayed_top": False,
                                          "question_zh": "问", "question_en": "Q c2"}]}
        job["agent_review"]["state"] = "done"
        qs.save_job(self.cfg, job)
        out = qs.response(self.cfg, self.job())
        self.assertEqual([i.get("sid") or i.get("cid") for i in out["ask_now"]], ["s1", "s2", "c1"])
        self.assertEqual([i.get("cid") for i in out["later"]], ["c2"])
        self.assertIn("jevscreen decide", out["ask_now"][0]["record_command"])
        self.assertIn("1. ", out["text_en"])
        # the top-up and reprice questions come before the review
        job = self.job()
        job["agent_review"]["state"] = "pending"
        job["reprice"] = {"old": "x", "new": "y"}
        self.assertEqual([i["id"] for i in qs.pending(self.cfg, job)], ["reprice_idea_en"])
        # after the first relay new items go to later only
        job = self.job()
        job["review"]["relayed"] = True
        out = qs.response(self.cfg, job)
        self.assertEqual(out["ask_now"], [])
        self.assertEqual([i.get("sid") or i.get("cid") for i in out["later"]], ["s1", "s2", "c1", "c2"])

    def test_facets_flag(self):
        self.consent_yes()
        self.set_key()
        f = {"en": {"category": "humanoid robots", "target": "warehouses", "type": "technology"},
             "zh": {"category": "人形机器人", "target": "仓库"},
             "implied_no": [{"key": "role.buyer", "because": "humanoid"}, {"key": "role.hardware", "because": "moon"}]}
        out = self.front(approve_budget=1, facets=json.dumps(f))
        self.assertEqual(out["agent_optional"], [])
        self.work()
        sv = calib.load_sieve(calib.sieve_path(self.cfg, QIDEA))
        self.assertEqual(sv["facets"]["category"], "humanoid robots")
        self.assertEqual(sv["facets_zh"]["target"], "仓库")
        self.assertEqual([(e["value"], e["source"]) for e in sv["scope_answers"]], [("buyer", "idea_wording")])
        self.assertTrue(any("words of the idea" in n for n in self.job()["facets_notes"]))
        late = self.front(facets=json.dumps(f))                 # after the screen started: a note, never blocking
        self.assertIn("apply next time", " ".join(late.get("facets_notes") or []))

    def test_a_company_name_in_facets_is_dropped_not_blocking(self):
        self.consent_yes()
        self.set_key()
        f = {"en": {"category": "RoboCorp arms", "target": "warehouses"}, "zh": {"category": "机器人", "target": "仓库"}}
        self.front(approve_budget=1, facets=f)
        self.work()
        sv = calib.load_sieve(calib.sieve_path(self.cfg, QIDEA))
        self.assertIsNone(sv.get("facets"))
        self.assertTrue(any("names RoboCorp" in n for n in self.job()["facets_notes"]))
        self.assertEqual(self.job()["state"], "done")

    def test_english_idea_offers_the_optional_facets_item(self):
        self.consent_yes()
        out = self.front()
        self.assertEqual([x["id"] for x in out["agent_optional"]], ["facets"])
        self.assertNotIn("facets", [i["id"] for i in out["pending"]])
        self.assertFalse(out["agent_optional"][0]["blocking"])


class SummaryCounts(FlowCase):
    def test_page_and_chat_count_the_same_checks(self):
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        code, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, self.yes_all)))
        n = len(deck["items"])
        self.assertIn(f"{n} 家", out["text_zh"])
        _d, r1 = review_cli.run_files(self.cfg, out["run_id"])
        self.assertEqual(page.change_of(r1)["n"], n)                            # held answers count too

    def test_removed_entries_carry_the_reason_words_only(self):
        rows = [{"name": "Robo Two", "security_id": "X:R2", "verdict_source": "agent", "agent_chip": "e"}]
        summ = review_cli.agent_summary({"excluded_by_agent": rows}, None, 1, None)
        [r] = summ["removed"]
        self.assertEqual(r["chip_words_zh"], review.chip_words(None)["e"]["zh"])
        self.assertEqual(r["chip_words_en"], review.chip_words(None)["e"]["en"])


class EscalationTexts(FlowCase):
    def judged(self, lang, fn):
        rv = review_cli.prepare(self.cfg, self.base, lang=lang)
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        tops = sorted([it for it in deck["items"] if not it.get("held_sid") and it.get("rank")],
                      key=lambda it: it["rank"])
        code, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(
            deck, lambda it: fn(it, tops))))
        self.assertEqual(code, 0)
        return tops, out

    def test_every_escalation_quotes_something(self):
        def fn(it, tops):
            if it is tops[0]:
                return {"v": "unsure", "unsure_kind": "meaning", "why": "意思不清楚"}
            if it is tops[1]:
                return {"v": "yes", "level": "explicit", "quote_ids": [99], "why": "它自己做数字支付"}
            return None
        tops, out = self.judged("zh", fn)
        escs = {e["rank"]: e for e in out["escalated"]}
        self.assertEqual({tops[0]["rank"], tops[1]["rank"]} - set(escs), set())
        for e in escs.values():
            self.assertNotIn("「」", e["question_zh"])
            self.assertNotIn('""', e["question_en"])
            self.assertIn("digital payments", e["question_zh"])
        self.assertIn("意思不清楚", escs[tops[0]["rank"]]["question_zh"])
        self.assertIn("它自己做数字支付", escs[tops[1]["rank"]]["question_zh"])

    def test_an_english_page_never_shows_chinese_from_the_ai(self):
        def fn(it, tops):
            if it.get("held_sid"):
                return {"v": "no", "chip": "k", "quote_ids": [1], "why": "卖终端", "in_group": True, "short": "支付终端"}
            if it is tops[0]:
                return {"v": "unsure", "unsure_kind": "meaning", "why": "意思不清楚"}
            return None
        _tops, out = self.judged("en", fn)
        for e in out["escalated"]:
            self.assertIsNone(CJK.search(e["question_en"]), e["question_en"])
        for q in out["questions"]:
            self.assertIsNone(CJK.search(q["question_en"]), q["question_en"])


class RelayAndPage(QuickCase):
    """Escalations reach a human: asked in chat until answered or a new version (not lost to a second status), and
    on the page (the question slot and the row's 'your AI's call')."""
    review = "keep"

    def judge_unsure_top(self, lang="zh"):
        out = self.done_flow(lang=lang)
        deck_id = out["pending"][0]["deck_id"]
        deck = json.loads(Path(self.job()["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        tops = sorted([it for it in deck["items"] if it.get("rank") is not None], key=lambda it: it["rank"])
        why = "意思不清楚" if lang == "zh" else "unclear"
        p = self.home / "ans.json"
        p.write_text(json.dumps({"format": review.ANSWERS_FORMAT, "deck_id": deck_id, "answers": {
            str(tops[0]["n"]): {"v": "unsure", "unsure_kind": "meaning", "why": why}}}), encoding="utf-8")
        code, j = review_cli.judge(self.cfg, deck_id, file=str(p))
        self.assertEqual(code, 0)
        [e] = [e for e in j["escalated"] if e["rank"] == tops[0]["rank"]]
        return e

    def test_a_second_status_asks_the_same_questions_again(self):
        e = self.judge_unsure_top()
        key = qs.idea_key(QIDEA)
        for _ in range(2):
            s = qs.status(self.cfg, key)
            self.assertEqual([i.get("cid") for i in s["ask_now"]], [e["cid"]])
            self.assertIn(e["question_zh"], s["text_zh"])
            self.assertNotIn("「」", s["text_zh"])
        # the human answered: a new version, nothing asked again
        code, _d = review_cli.decide(self.cfg, f"{e['cid']}=yes", s["run_id"])
        self.assertEqual(code, 0)
        s = qs.status(self.cfg, key)
        self.assertEqual(s["ask_now"], [])

    def test_escalations_and_the_ais_call_are_on_the_page(self):
        e = self.judge_unsure_top()
        s = qs.status(self.cfg, qs.idea_key(QIDEA))
        data = page.read_page_data(Path(s["page"]))
        [it] = [x for x in data["questions"]["items"] if x["id"] == e["cid"]]
        self.assertEqual([o["value"] for o in it["options"]], [f"{e['cid']}=yes", f"{e['cid']}=no", f"{e['cid']}=?"])
        self.assertEqual([o["label"] for o in it["options"]], ["要", "不要", "不确定"])
        self.assertEqual(it["text"], e["question_zh"])
        self.assertIn("jevscreen decide", data["questions"]["template"])
        row = next(r for r in data["rows"] if r["rank"] == e["rank"])
        self.assertEqual((row["agent"]["v"], row["agent"]["state"]), ("unsure", "escalated"))
        html = Path(s["page"]).read_text(encoding="utf-8")
        self.assertIn("agent_tag", html)                                         # the page script renders it
        if shutil.which("node"):
            import test_page
            dom = test_page.dom_text(self, html)["text"]
            self.assertIn(e["question_zh"], dom)                                 # in the question box
            self.assertIn(page.STRINGS["zh"]["agent_tag"], dom)                  # on the row
            self.assertIn(page.STRINGS["zh"]["agent_wait"], dom)
        txt = page.render_text(data, "zh")
        self.assertIn(page.STRINGS["zh"]["agent_tag"], txt)
        self.assertIn(e["question_zh"], txt)

    def test_the_questions_are_labelled_apart_from_the_list(self):
        e = self.judge_unsure_top()
        s = qs.status(self.cfg, qs.idea_key(QIDEA))
        lines = s["text_zh"].splitlines()
        q = next(ln for ln in lines if e["question_zh"] in ln)
        self.assertFalse(q.startswith("1. "), q)
        self.assertTrue(q.startswith("问题 1"), q)
        qen = next(ln for ln in s["text_en"].splitlines() if e["question_en"] in ln)
        self.assertTrue(qen.startswith("Q1"), qen)

    def test_a_timed_out_review_never_counts_as_relayed(self):
        self.done_flow(lang="zh")
        job = self.job()
        deck_id = job["agent_review"]["deck_id"]
        job["agent_review"]["created_at"] = qs.iso(qs.now_utc() - dt.timedelta(
            seconds=review.AGENT_REVIEW_TIMEOUT_S + 5))
        qs.save_job(self.cfg, job)
        # before any status: the page no longer says the AI is still checking, and stops reloading
        from jevscreen import pagestatus
        live = pagestatus.build(self.cfg, lang="zh", job=self.job())
        self.assertFalse(live["refresh"])
        self.assertNotIn(pagestatus.T["zh"]["review_banner"], " ".join(a["text"] for a in live["alerts"]))
        self.assertIn(pagestatus.T["zh"]["review_over"], " ".join(a["text"] for a in live["alerts"]))
        s = qs.status(self.cfg, qs.idea_key(QIDEA))
        self.assertEqual(s["agent_review"]["state"], "timed_out")
        self.assertIn(qs.STRINGS["zh"]["review_timeout"], s["text_zh"])         # the human is told why
        self.assertEqual(s["ask_now"], [])
        self.assertFalse(self.job()["review"].get("relayed"))
        # a late judge: its question is still asked in chat
        deck = json.loads(Path(job["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        top = min((it for it in deck["items"] if it.get("rank") is not None), key=lambda it: it["rank"])
        p = self.home / "late.json"
        p.write_text(json.dumps({"format": review.ANSWERS_FORMAT, "deck_id": deck_id, "answers": {
            str(top["n"]): {"v": "unsure", "unsure_kind": "meaning", "why": "意思不清楚"}}}), encoding="utf-8")
        review_cli.judge(self.cfg, deck_id, file=str(p))
        s = qs.status(self.cfg, qs.idea_key(QIDEA))
        self.assertTrue(s["ask_now"])
        self.assertIn("意思不清楚", s["text_zh"])


if __name__ == "__main__":
    unittest.main()


class Docs(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_agents_md_and_reference(self):
        agents = (self.ROOT / "AGENTS.md").read_text(encoding="utf-8")
        row = next(ln for ln in agents.splitlines() if ln.startswith("| `needs_agent`"))
        self.assertIn("agent_review", row)
        for w in ("jevscreen judge", "jevscreen decide", "quote_ids", "Never answer the human's scope questions",
                  "ask_now", "in_group", "skip_command", "--facets"):
            self.assertIn(w, agents, w)
        self.assertIn("at most 2 questions about\nthe idea's boundary", agents)
        ref = (self.ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
        for w in ("jevscreen judge --deck", "jevscreen decide", "--scope-scan"):
            self.assertIn(w, ref)
        api = (self.ROOT / "docs" / "AGENT_API.md").read_text(encoding="utf-8")
        for w in ("jevscreen.agent_deck/1", "jevscreen.agent_answers/1", "ask_now", "agent_optional", "E1", "E4"):
            self.assertIn(w, api)
        self.assertIn(qs.STRINGS["zh"]["before"], agents)
