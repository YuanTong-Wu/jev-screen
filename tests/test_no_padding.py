"""The confirmed list is never padded to 10 (owner decision 2026-09-29: "如果凑不出十家，那就凑不出呗").

- the numbered main list holds only the confirmed tier (shortlist.py: L1 core + L2 explicit, or judge tier A), 0-n
  rows; every other listed row is in a separate, secondary 待核对 / To confirm section (shortlist.confirm_order) and
  never fills the main list;
- your AI's review reads the to-confirm rows first; a to-confirm row it judged yes, level explicit, citing 1-3
  sentences moves into the main list marked 你的 AI 核对 / checked by your AI (partial stays, no moves it down,
  unsure stays); a main-list row it judged no leaves the main list; the human's pins still win;
- the wording is calm: a short main list is normal ("确认 3 家；另有 7 家待核对"), an empty one says so plainly and
  points at the to-confirm section;
- quickstart JSON: top = the main list only, plus a compact to_confirm; report.md / --text / results.csv carry the
  section; --no-shortlist keeps the old padded list; old results without the sections render as before;
- eval: the headline is the main list's strict P@min(10, n_main) with n_main and must-include recall in it, plus the
  full top-10 numbers as before.

Offline: test_screen's seeded store and fakes (no network, no paid call); every company is invented.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_review_flow as TRF  # noqa: E402
import test_screen as TS  # noqa: E402
from jevscreen import (evalfrom, evalset, page, quickstart as qs, report, review, review_cli, screen,  # noqa: E402
                       short_list, shortlist)
from test_page import dom_text  # noqa: E402

CJK = re.compile(r"[㐀-鿿]")
IDEA = "储能电站液冷温控系统的核心供应商"
IDEA_EN = "Main suppliers of liquid-cooling thermal management systems for battery energy storage power stations."


def _row(i, *, l1="core", l2="explicit", ev="annual_report", **kw):
    return {"rank": i, "security_id": f"NYSE:C{i:02d}", "company_key": f"k{i}", "name": f"Coolant {i} Inc.",
            "country": "United States", "market_cap_usd": 2e9 - i * 1e7, "l1_label": l1, "l1_p_core": 0.8,
            "l2_label": l2, "l2_evidence": ev, "l2_p_pos": 0.9, "evidence_sha": f"sha{i}",
            "evidence_excerpt": f"Coolant {i} supplies liquid-cooling units for battery energy storage stations.", **kw}


def sections_result(n_main=3, n_confirm=7, *, promoted=False, status="ok"):
    """A finished run with n_main confirmed rows and n_confirm to confirm (the ranks mixed, as the score orders them);
    promoted: the first to-confirm row carries your AI's yes, level explicit, citing sentence 1."""
    n = n_main + n_confirm
    mains = set(range(2, 2 * n_main + 1, 2)) if 2 * n_main <= n else set(range(1, n_main + 1))
    raw = [_row(i) if i in mains else _row(i, l2="partial") for i in range(1, n + 1)]
    if promoted:
        c = next(r for r in raw if r["l2_label"] == "partial")
        c.update(agent_verdict="yes", agent_level="explicit", agent_quote_ids=[1], agent_state="applied",
                 agent_why_zh="年报写明为储能电站供应液冷机组", agent_why_en="the filing says it supplies the units")
    rows, info = shortlist.apply(raw)
    unv = [{"security_id": f"NYSE:U{i}", "company_key": f"u{i}", "name": f"Unverio {i} Inc.",
            "country": "United States", "l2_status": "insufficient"} for i in range(3)]
    return {"run_id": "scr-20260929120000-abcdef", "idea": IDEA, "idea_en": IDEA_EN, "status": status,
            "params": {"min_mcap_usd": 1e9, "max_out": 40, "shortlist": True, "list": "main"},
            "funnel": {"universe": 900, "described": 800, "l1_pass": 30}, "rows": rows, "unverified": unv,
            "shortlist": info, "output_dir": None, "cost_usd": 0.21, "finished_at": "2026-09-29T12:00:00+00:00"}


def data_of(res, lang="zh"):
    return page.build_page_data(res, None, lang=lang, l2_pieces={})


# ------------------------------------------------------------------------------------------------ the tiers

class MainOf(unittest.TestCase):
    CONFIRM = {"l1_label": "core", "l2_label": "partial"}
    HIGH = {"l1_label": "core", "l2_label": "explicit"}
    YES = {"agent_verdict": "yes", "agent_level": "explicit", "agent_quote_ids": [2, 3], "agent_state": "applied"}

    def test_your_ais_explicit_yes_with_quotes_moves_a_to_confirm_row_into_the_main_list(self):
        self.assertEqual(shortlist.main_of({**self.CONFIRM, **self.YES}), ("high", "agent"))

    def test_partial_unsure_no_quotes_too_many_quotes_or_waiting_stay_to_confirm(self):
        for extra in ({"agent_level": "partial"}, {"agent_verdict": "unsure", "agent_level": None},
                      {"agent_quote_ids": []}, {"agent_quote_ids": [1, 2, 3, 4]}, {"agent_state": "escalated"},
                      {"agent_state": "held"}):
            with self.subTest(extra=extra):
                self.assertEqual(shortlist.main_of({**self.CONFIRM, **self.YES, **extra}), ("confirm", None))

    def test_your_ais_no_takes_a_row_off_the_main_list_unless_the_human_keeps_the_kind(self):
        for state in ("escalated", "held"):
            self.assertEqual(shortlist.main_of({**self.HIGH, "agent_verdict": "no", "agent_state": state}),
                             ("confirm", "agent_no"))
        self.assertEqual(shortlist.main_of({**self.HIGH, "agent_verdict": "no", "agent_state": "not_applied"}),
                         ("high", None))
        self.assertEqual(shortlist.main_of({**self.HIGH, "agent_verdict": "unsure", "agent_state": "applied"}),
                         ("high", None))

    def test_the_humans_yes_always_wins(self):
        self.assertEqual(shortlist.main_of({**self.CONFIRM, "user_verdict": "partial", "verdict_source": "user"}),
                         ("high", "user"))
        self.assertEqual(shortlist.main_of({**self.HIGH, "user_verdict": "explicit",
                                            "verdict_source": "evidence+user"}), ("high", None))

    def test_order_system_rows_then_your_ais_then_to_confirm_with_moved_down_last(self):
        rows = [dict(self.CONFIRM, rank=1, security_id="A", **self.YES),
                dict(self.HIGH, rank=2, security_id="B"),
                dict(self.HIGH, rank=3, security_id="C", agent_verdict="no", agent_state="escalated"),
                dict(self.CONFIRM, rank=4, security_id="D"),
                dict(self.HIGH, rank=5, security_id="E")]
        out, info = shortlist.apply(rows)
        self.assertEqual([r["security_id"] for r in out], ["B", "E", "A", "D", "C"])
        self.assertEqual([r["rank"] for r in out], [1, 2, 3, 4, 5])
        self.assertEqual([r.get("main_via") for r in out], [None, None, "agent", None, "agent_no"])
        self.assertEqual((info["high"], info["confirm"], info["agent"], info["agent_no"]), (3, 2, 1, 1))

    def test_on_by_default_off_only_for_the_padded_list(self):
        self.assertTrue(shortlist.on({}))
        self.assertTrue(shortlist.on({"shortlist": False}))          # a run made before the default changed
        self.assertTrue(shortlist.on({"list": "main"}))
        self.assertFalse(shortlist.on({"list": "padded", "shortlist": False}))


# ------------------------------------------------------------------------------------------------ screen

class ScreenDefault(TS.StoreCase):
    def screen_(self, **kw):
        kw.setdefault("sieve", "none")
        return self.run_screen(**kw)

    def test_a_screen_lists_the_main_list_and_the_to_confirm_section_by_default(self):
        res = self.screen_()
        self.assertEqual(res["params"]["list"], "main")
        self.assertTrue(res["params"]["shortlist"])
        self.assertEqual([(r["security_id"], r["shortlist_tier"]) for r in res["rows"]],
                         [("NYSE:ROBO", "high"), ("NASDAQ:ROB2", "confirm")])
        with open(self.home / "out" / "results.csv", encoding="utf-8") as f:
            rd = list(csv.DictReader(f))
        self.assertEqual([r["section"] for r in rd], ["main", "to_confirm"])
        md = (self.home / "out" / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Main list (confirmed): 1", md)
        self.assertIn("## To confirm (not in the confirmed list): 1", md)
        con = report.format_console(res)
        self.assertIn("main list (confirmed): 1; to confirm (not in the confirmed list): 1", con)

    def test_no_shortlist_keeps_the_old_padded_list(self):
        res = self.screen_(shortlist=False)
        self.assertEqual(res["params"]["list"], "padded")
        self.assertNotIn("shortlist", res)
        self.assertTrue(all("shortlist_tier" not in r for r in res["rows"]))
        with open(self.home / "out" / "results.csv", encoding="utf-8") as f:
            self.assertNotIn("section", next(csv.reader(f)))
        self.assertIn("## Ranked results", (self.home / "out" / "report.md").read_text(encoding="utf-8"))
        # a version of the padded run stays padded
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=res["run_id"], sieve="none",
                                  out_dir=self.home / "ro")
        self.assertNotIn("shortlist", ro)
        self.assertEqual(ro["params"]["list"], "padded")

    def test_a_run_made_before_the_default_changed_gets_it_in_its_next_version(self):
        res = self.screen_(shortlist=False)
        # as if written before 'list' existed: params without it, shortlist False
        with TS.store.session(self.cfg) as con:
            pj = json.loads(con.execute("SELECT params_json FROM screen_runs WHERE run_id = ?",
                                        [res["run_id"]]).fetchone()[0])
            pj.pop("list", None)
            con.execute("UPDATE screen_runs SET params_json = ? WHERE run_id = ?", [json.dumps(pj), res["run_id"]])
        saved = json.loads((self.home / "out" / "results.json").read_text(encoding="utf-8"))
        saved["params"].pop("list", None)
        (self.home / "out" / "results.json").write_text(json.dumps(saved), encoding="utf-8")
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=res["run_id"], sieve="none",
                                  out_dir=self.home / "ro")
        self.assertEqual(ro["params"]["list"], "main")
        self.assertEqual([r["shortlist_tier"] for r in ro["rows"]], ["high", "confirm"])
        fr = self.screen_(from_run=res["run_id"], out_dir=self.home / "fr")
        self.assertEqual(fr["params"]["list"], "main")
        self.assertEqual([r["shortlist_tier"] for r in fr["rows"]], ["high", "confirm"])

    def test_your_ais_calls_move_rows_between_the_sections_for_free(self):
        res = self.screen_()
        sha = {r["security_id"]: r["evidence_sha"] for r in res["rows"]}
        agent = {"NASDAQ:ROB2": {"company_key": next(r["company_key"] for r in res["rows"]
                                                     if r["security_id"] == "NASDAQ:ROB2"),
                                 "evidence_sha": sha["NASDAQ:ROB2"], "v": "yes", "level": "explicit",
                                 "quote_ids": [1], "state": "applied", "why_zh": "年报写明", "why_en": "the filing"},
                 "NYSE:ROBO": {"company_key": next(r["company_key"] for r in res["rows"]
                                                   if r["security_id"] == "NYSE:ROBO"),
                               "evidence_sha": sha["NYSE:ROBO"], "v": "no", "chip": "k", "quote_ids": [1],
                               "state": "escalated", "why_zh": "只是买方", "why_en": "a buyer"}}
        agent = {v["company_key"]: v for v in agent.values()}
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=res["run_id"], sieve="none",
                                  out_dir=self.home / "ro", agent=agent)
        self.assertEqual(ro["cost_usd"], 0.0)
        got = [(r["security_id"], r["shortlist_tier"], r.get("main_via")) for r in ro["rows"]]
        self.assertEqual(got, [("NASDAQ:ROB2", "high", "agent"), ("NYSE:ROBO", "confirm", "agent_no")])
        d = page.build_page_data(ro, None, lang="zh", l2_pieces={})
        txt = page.render_text(d, "zh")
        self.assertIn(page.STRINGS["zh"]["via_agent"], txt)
        self.assertEqual([r["name"] for r in page.top_rows(d)], [d["rows"][0]["name"]])
        self.assertTrue(page.top_rows(d)[0]["checked_by_agent"])

    def test_the_review_deck_reads_the_to_confirm_rows_first(self):
        res = self.screen_()
        pool, inputs, names = review_cli.pool_inputs(self.cfg, dict(res, output_dir=str(self.home / "out")))
        deck = review.build_deck(res, inputs, None, {}, part="A", pool=pool, names_zh=names)
        top = [(it["security_id"], it["section"]) for it in deck["items"] if it["group"] == "top"]
        self.assertEqual(top, [("NASDAQ:ROB2", "to_confirm"), ("NYSE:ROBO", "main")])
        self.assertIn("to_confirm", deck["instructions_en"])

    def test_review_order_of_a_single_list_is_the_rank(self):
        rows = [{"rank": i, "company_key": f"k{i}"} for i in range(1, 13)]
        self.assertEqual([r["rank"] for r in review.review_order(rows)], list(range(1, 11)))
        tiered = [dict(r, shortlist_tier="high" if r["rank"] <= 2 else "confirm") for r in rows]
        self.assertEqual([r["rank"] for r in review.review_order(tiered)], list(range(3, 13)) + [1, 2])


class SectionsFlow(TRF.FlowCase):
    """The review flow (judge) over the main list and the to-confirm section: 20 listed payment companies, none
    confirmed by the system alone (L1 did not call them core)."""
    SHORTLIST = True

    def test_your_ais_review_reads_the_to_confirm_rows_first_and_moves_the_explicit_ones_in(self):
        self.assertEqual({r["shortlist_tier"] for r in self.base["rows"]}, {"confirm"})
        rv = review_cli.prepare(self.cfg, self.base, lang="zh")
        deck = json.loads(Path(rv["agent_review"]["deck_path"]).read_text(encoding="utf-8"))
        tops = [it for it in deck["items"] if it["group"] == "top"]
        # every listed row is read before the relay (deck v2), not only the first 10: the held rows of the scope
        # question and the rest in rank order
        self.assertEqual([it["rank"] for it in tops], sorted(it["rank"] for it in tops))
        self.assertEqual(sorted(it["rank"] for it in deck["items"] if it["group"] in ("top", "held")),
                         list(range(1, 21)))
        self.assertEqual({it["section"] for it in tops}, {"to_confirm"})
        code, out = review_cli.judge(self.cfg, deck["deck_id"], file=str(self.answers_file(deck, self.yes_all)))
        self.assertEqual(code, 0)
        self.assertTrue(out["main_changed"])                 # part B: tell the human only when this is true
        self.assertIn("放进确认名单的 12 家", out["text_zh"])
        _d, r1 = review_cli.run_files(self.cfg, out["run_id"])
        self.assertEqual(r1["cost_usd"], 0.0)
        promoted = [r for r in r1["rows"] if r.get("main_via") == "agent"]
        self.assertEqual({r["company_key"] for r in promoted}, {it["company_key"] for it in tops})
        self.assertEqual([r["rank"] for r in promoted], list(range(1, 13)))     # the numbered main list
        self.assertTrue(all(r["shortlist_tier"] == "confirm" for r in r1["rows"] if r not in promoted))
        # part A held every listed row: none is left for part B
        in_a = {it["company_key"] for it in deck["items"]}
        self.assertEqual({r["company_key"] for r in self.base["rows"]} - in_a, set())
        data = page.read_page_data(page.stable_path(self.cfg, TRF.IDEA))
        self.assertEqual(data["shortlist"]["agent"], 12)
        self.assertIn(page.STRINGS["zh"]["via_agent"], page.render_text(data, "zh"))


# ------------------------------------------------------------------------------------------------ the page

class Page(unittest.TestCase):
    def test_three_confirmed_and_a_separate_to_confirm_section(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                S = page.STRINGS[lang]
                d = data_of(sections_result(), lang)
                self.assertEqual((d["shortlist"]["high"], d["shortlist"]["confirm"]), (3, 7))
                txt = page.render_text(d, lang)
                self.assertIn(S["split_line"].format(m=3, u=7), txt)
                self.assertIn(S["main_title"].format(n=3), txt)
                self.assertIn(S["confirm_title"].format(n=7), txt)
                self.assertNotIn("不到 10 家" if lang == "zh" else "fewer than 10", txt)   # no alarm
                main_part, confirm_part = txt.split(S["confirm_title"].format(n=7))
                self.assertEqual(len(re.findall(r"^#\d+ ", main_part, re.M)), 3)
                self.assertEqual(len(re.findall(r"^#\d+ ", confirm_part, re.M)), 0)   # never numbered as the list
                if lang == "en":
                    self.assertIsNone(CJK.search(txt), txt)
                if shutil.which("node"):
                    dom = dom_text(self, page.render_page(d))
                    self.assertEqual(dom["table_ranks"], ["#1", "#2", "#3"])
                    self.assertIn(S["confirm_title"].format(n=7), dom["text"])
                    self.assertIn(S["split_line"].format(m=3, u=7), dom["text"])
                    if lang == "en":        # the idea as the human wrote it is shown under its own label
                        self.assertIsNone(CJK.search(dom["text"].replace(IDEA, "")))

    def test_an_empty_main_list_says_so_plainly_and_points_at_the_section(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                S = page.STRINGS[lang]
                d = data_of(sections_result(0, 6), lang)
                txt = page.render_text(d, lang)
                self.assertIn(S["main_empty"].format(u=6), txt)
                self.assertIn(S["confirm_title"].format(n=6), txt)
                self.assertEqual(page.top_rows(d), [])
                if shutil.which("node"):
                    dom = dom_text(self, page.render_page(d))
                    self.assertEqual(dom["trs"], 0)
                    self.assertIn(S["main_empty"].format(u=6), dom["text"])
        d = data_of(sections_result(0, 0))
        self.assertIn(page.STRINGS["zh"]["main_empty0"], page.render_text(d, "zh"))

    def test_a_row_your_ai_confirmed_is_in_the_main_list_marked(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                d = data_of(sections_result(2, 5, promoted=True), lang)
                self.assertEqual((d["shortlist"]["high"], d["shortlist"]["agent"]), (3, 1))
                third = page.top_rows(d)[2]
                self.assertTrue(third["checked_by_agent"])
                self.assertIn(page.STRINGS[lang]["via_agent"], page.render_text(d, lang))
                if shutil.which("node"):
                    dom = dom_text(self, page.render_page(d))
                    self.assertEqual(len(dom["table_ranks"]), 3)
                    self.assertIn(page.STRINGS[lang]["via_agent"], dom["table_verdicts"][-1])

    def test_an_unfinished_run_says_the_list_may_change(self):
        d = data_of(sections_result(status="partial"), "en")
        self.assertIn(page.STRINGS["en"]["split_open"].split("{")[0], page.render_text(d, "en"))

    def test_an_old_result_without_the_sections_renders_as_before(self):
        res = sections_result()
        res.pop("shortlist")
        for r in res["rows"]:
            r.pop("shortlist_tier")
        d = data_of(res, "zh")
        self.assertIsNone(d["shortlist"])
        txt = page.render_text(d, "zh")
        self.assertIn(page.STRINGS["zh"]["list_title"].format(n=10), txt)
        self.assertNotIn(page.STRINGS["zh"]["confirm_title"].split("（")[0], txt)
        self.assertEqual(len(page.top_rows(d)), 10)
        self.assertIsNone(page.to_confirm_brief(d))

    def test_strings_are_in_one_language(self):
        for key in ("funnel_split", "split_line", "split_line0", "split_open", "main_title", "main_rows_title",
                    "main_note", "main_note_judge", "main_empty", "main_empty0", "main_empty_open", "confirm_title",
                    "confirm_note", "via_agent", "via_agent_no", "funnel_rescued_split",
                    "unverified_title_split", "short_why_unchecked_split", "verdict_main"):
            self.assertRegex(page.STRINGS["zh"][key], CJK, key)
            self.assertIsNone(CJK.search(page.STRINGS["en"][key]), key)
        for lang in ("zh", "en"):
            for key in ("main_short", "main_short_confirm", "main_none0", "main_none_open", "main_open",
                        "next_confirm"):
                self.assertEqual(bool(CJK.search(short_list.TEXT[lang][key])), lang == "zh", (lang, key))
            for key in ("done_split", "done_split0", "mark_agent", "done_src", "done_more", "done_more0"):
                self.assertEqual(bool(CJK.search(qs.STRINGS[lang][key])), lang == "zh", (lang, key))


# ------------------------------------------------------------------------------------------------ chat / JSON

def _job(d):
    res = {"run_id": d["run_id"], "summary": page.summary_of(d, None), "short_list": short_list.of(d),
           "top": page.top_rows(d), "to_confirm": page.to_confirm_brief(d), "page": None, "page_opened": True,
           "cost_usd": 0.2, "seconds": 60, "next_steps": short_list.steps(short_list.of(d), d["run_id"])}
    return {"idea": IDEA, "result": res}


class Chat(unittest.TestCase):
    def test_top_is_the_main_list_only_with_a_compact_to_confirm(self):
        d = data_of(sections_result(), "zh")
        self.assertEqual(len(page.top_rows(d)), 3)
        tc = page.to_confirm_brief(d)
        self.assertEqual(tc["n"], 7)
        self.assertEqual(len(tc["rows"]), 7)
        self.assertEqual(set(tc["rows"][0]), {"name", "name_en", "ticker", "country", "verdict_words_zh",
                                              "verdict_words_en", "evidence_kind", "agent", "moved_by_agent",
                                              "unchecked", "security_id"})
        self.assertEqual(len(page.top_rows(d, section="all")), 10)
        s = page.summary_of(d, None)
        self.assertEqual((s["listed"], s["main"], s["to_confirm"]), (10, 3, 7))

    def test_a_short_main_list_is_said_calmly(self):
        d = data_of(sections_result(), "zh")
        zh = qs.human_text(_job(d), "done", [], "zh", None)
        self.assertIn("确认 3 家", zh)
        self.assertIn("另有 7 家待核对", zh)
        self.assertIn(short_list.TEXT["zh"]["main_short"], zh)
        self.assertNotIn("不到 10 家", zh)
        self.assertEqual(len(re.findall(r"^\d+\. ", zh, re.M)), 3)     # only the main list is numbered
        en = qs.human_text(_job(data_of(sections_result(), "en")), "done", [], "en", None)
        self.assertIn("3 confirmed", en)
        self.assertIn("7 more to confirm", en)
        self.assertNotIn("fewer than 10", en)
        self.assertIsNone(CJK.search(en), en)

    def test_an_empty_main_list_points_at_the_to_confirm_section(self):
        d = data_of(sections_result(0, 6), "zh")
        zh = qs.human_text(_job(d), "done", [], "zh", None)
        self.assertEqual(zh.count("这次没有公司能从原文确认"), 1, zh)      # said once, in the done line
        self.assertIn("待核对的 6 家", zh)
        self.assertIn("「待核对」", zh)
        self.assertEqual(re.findall(r"^\d+\. ", zh, re.M), [])
        [step] = short_list.steps(short_list.of(d), d["run_id"])
        self.assertEqual(step["cost_usd"], 0.0)
        self.assertTrue(step["command"].startswith("jevscreen why C0"))

    def test_ten_confirmed_needs_no_extra_line(self):
        d = data_of(sections_result(10, 2), "zh")
        self.assertIsNone(short_list.text({"short_list": short_list.of(d)}, "zh"))
        self.assertIn("确认 10 家", qs.human_text(_job(d), "done", [], "zh", None))

    def test_a_row_your_ai_confirmed_says_so_in_chat(self):
        d = data_of(sections_result(2, 5, promoted=True), "zh")
        self.assertIn(qs.STRINGS["zh"]["mark_agent"], qs.human_text(_job(d), "done", [], "zh", None))


# ------------------------------------------------------------------------------------------------ eval

class Eval(unittest.TestCase):
    DATA = {"id": "xx", "type": "other", "labels": [
        {"security_id": "NYSE:A", "label": "right", "must_include": True},
        {"security_id": "NYSE:B", "label": "wrong"},
        {"security_id": "NYSE:C", "label": "right", "must_include": True},
        {"security_id": "NYSE:D", "label": "wrong"}]}
    ROWS = [{"rank": 1, "security_id": "NYSE:B", "l1_label": "core", "l2_label": "partial"},
            {"rank": 2, "security_id": "NYSE:A", "l1_label": "core", "l2_label": "explicit"},
            {"rank": 3, "security_id": "NYSE:C", "l1_label": "adjacent", "l2_label": "explicit"},
            {"rank": 4, "security_id": "NYSE:D", "l1_label": "core", "l2_label": "explicit"}]

    def test_the_main_list_is_scored_for_every_run_and_the_full_top_10_as_before(self):
        s = evalset.score(self.DATA, {"rows": [dict(r) for r in self.ROWS], "status": "ok"})   # bare: no tiers
        m = s["main"]
        self.assertEqual((m["n"], m["right"], m["wrong"], m["precision"]), (2, 1, 1, 0.5))
        self.assertEqual((m["must_found"], m["must_n"], m["must_recall"]), (1, 2, 0.5))
        self.assertEqual((m["total"], m["to_confirm"]), (2, 2))
        self.assertEqual(s["at"]["10"]["precision"], 0.5)                          # 2 right / 4 labelled
        agg = evalset.aggregate([s])["all"]
        self.assertEqual((agg["p_main"], agg["n_main"], agg["ideas_main_empty"], agg["must_found_main"]),
                         (0.5, 2, 0, 1))
        md = evalset.report_md([s], evalset.aggregate([s]))
        head = md.split("\n")[2]
        self.assertTrue(head.startswith("Main list"), head)
        self.assertIn("strict P@min(10, n_main) pooled over the main-list rows 50%", head)
        self.assertIn("mean over ideas 50%", head)
        self.assertIn("n_main 2 rows", head)
        self.assertIn("must-include in the main list 1/2", head)
        self.assertIn("Full list: strict P@10 50%", md)

    def test_eval_run_screens_the_bare_single_list_unless_asked(self):
        import argparse
        self.assertIs(evalfrom.lever_kwargs(argparse.Namespace(shortlist=None))["shortlist"], False)
        self.assertIs(evalfrom.lever_kwargs(argparse.Namespace(shortlist=True))["shortlist"], True)


# ------------------------------------------------------------------------------------------------ review fixes

class ScopeAndCounts(unittest.TestCase):
    HIGH = {"l1_label": "core", "l2_label": "explicit"}
    YES = {"agent_verdict": "yes", "agent_level": "explicit", "agent_quote_ids": [1], "agent_state": "applied"}

    def test_a_row_a_scope_answer_moved_down_is_to_confirm_whatever_your_ai_says(self):
        self.assertEqual(shortlist.main_of({**self.HIGH, "scope_demoted": True}), ("confirm", None))
        self.assertEqual(shortlist.main_of({"l1_label": "core", "l2_label": "partial", "scope_demoted": True,
                                            **self.YES}), ("confirm", None))
        self.assertEqual(shortlist.main_of({**self.HIGH, "scope_demoted": True, "judge_tier": "A"}),
                         ("confirm", None))
        # the human's yes still wins
        self.assertEqual(shortlist.main_of({**self.HIGH, "scope_demoted": True, "user_verdict": "explicit",
                                            "verdict_source": "evidence+user"}), ("high", None))
        out, _info = shortlist.apply([dict(self.HIGH, rank=1, security_id="A"),
                                      dict(self.HIGH, rank=2, security_id="C", scope_demoted=True),
                                      dict(l1_label="core", l2_label="partial", rank=3, security_id="B")])
        self.assertEqual([(r["security_id"], r["shortlist_tier"]) for r in out],
                         [("A", "high"), ("B", "confirm"), ("C", "confirm")])     # moved down: last

    def test_the_counts_include_a_user_pin_listed_below_the_cut(self):
        rows = [dict(self.HIGH, rank=i, security_id=f"H{i}") for i in (1, 2, 3)] + [
            dict(l1_label="core", l2_label="partial", rank=4, security_id="R"),
            dict(l1_label="adjacent", l2_label="partial", rank=41, security_id="P", below_cut=True,
                 user_verdict="partial", verdict_source="user")]
        out, info = shortlist.apply(rows)
        main, confirm = shortlist.split(out)
        self.assertEqual((info["high"], info["confirm"], info["user"]), (4, 1, 1))
        self.assertEqual((len(main), len(confirm)), (info["high"], info["confirm"]))


class WhyToConfirm(unittest.TestCase):
    def why(self, res, sid):
        import types
        from jevscreen import why
        row = next(r for r in res["rows"] if r["security_id"] == sid)
        out = {"stages": [], "facts": [], "inferences": [], "gaps": [], "changes": []}
        return why._in_output(types.SimpleNamespace(result=res), out, row)

    def test_a_to_confirm_row_is_not_said_to_be_in_the_list_and_gets_its_reasons(self):
        res = sections_result()
        _main, confirm = shortlist.split(res["rows"])
        out = self.why(res, confirm[0]["security_id"])
        self.assertEqual(out["stop"], "to_confirm")
        self.assertTrue(out["plain_zh"].startswith("它在「待核对」部分，不算入选："), out["plain_zh"])
        self.assertIn("原文只算相关", out["plain_zh"])
        self.assertNotIn("#", out["plain_en"])
        self.assertIn("not in the confirmed list", out["plain_en"])
        self.assertFalse(out["stages"][-1]["ok"])
        self.assertIsNone(CJK.search(out["plain_en"]))

    def test_a_main_row_says_its_place_in_the_confirmed_list_and_your_ais_check(self):
        res = sections_result(2, 5, promoted=True)
        main, _c = shortlist.split(res["rows"])
        out = self.why(res, main[0]["security_id"])
        self.assertEqual((out["stop"], out["plain_zh"]), ("in_output", "它在确认名单里，排第 1"))
        out = self.why(res, main[2]["security_id"])
        self.assertIn("你的 AI 核对", out["plain_zh"])
        self.assertIn("checked by your AI", out["plain_en"])

    def test_a_single_padded_list_answers_as_before(self):
        res = sections_result()
        res.pop("shortlist")
        out = self.why(res, res["rows"][4]["security_id"])
        self.assertEqual(out["plain_zh"], "它在名单里，排第 5")


class Escalations(unittest.TestCase):
    V = {"company_key": "k7", "security_id": "NYSE:C07", "name": "Coolant 7 Inc.", "name_zh": "冷却七",
         "v": "unsure", "unsure_kind": "meaning", "quote_ids": [], "escalation": "E1"}

    def item(self, v, section, rank=7):
        return review.escalation_item({**v, "ctx": {"rank": rank, "section": section}}, "c1",
                                      text="Coolant 7 makes pumps. It also sells coolers.", lang_ev="en", sieve=None)

    def test_a_to_confirm_row_names_the_section_not_a_rank_and_waits_on_the_page(self):
        it = self.item(self.V, "to_confirm")
        self.assertIn("（待核对）", it["question_zh"])
        self.assertIn("要不要放进确认名单？", it["question_zh"])
        self.assertNotIn("第 7 名", it["question_zh"])
        self.assertIn("(to confirm)", it["question_en"])
        self.assertNotIn("#7", it["question_en"])
        self.assertFalse(it["in_relayed_top"])

    def test_a_main_row_keeps_its_number_and_your_ais_no_says_it_was_moved(self):
        it = self.item(self.V, "main", rank=2)
        self.assertIn("第 2 名", it["question_zh"])
        self.assertTrue(it["in_relayed_top"])
        no = {**self.V, "v": "no", "chip": "k", "quote_ids": [1], "escalation": "E2", "unsure_kind": None}
        it = self.item(no, "main", rank=2)
        self.assertIn("已先移到待核对", it["question_zh"])
        self.assertIn("Put it back in the confirmed list?", it["question_en"])
        self.assertTrue(it["in_relayed_top"])
        # a single padded list: as before
        self.assertTrue(self.item(self.V, None)["in_relayed_top"])

    def test_only_rows_of_the_confirmed_list_are_relayed(self):
        res = sections_result()
        main, confirm = shortlist.split(res["rows"])
        self.assertTrue(all(review_cli.relayed(r, True) for r in main))
        self.assertFalse(any(review_cli.relayed(r, True) for r in confirm))
        self.assertTrue(review_cli.relayed({"rank": 7}, False))
        self.assertEqual(review_cli._main_keys(res), [r["company_key"] for r in main])

    def test_part_a_keeps_room_for_the_main_list(self):
        def row(i, tier):
            return {"rank": i, "company_key": f"k{i}", "security_id": f"NYSE:C{i:02d}", "name": f"Co {i}",
                    "country": "United States", "market_cap_usd": 3e9 - i * 1e7, "l1_label": "core",
                    "l1_p_core": 0.8, "l2_label": "explicit" if tier == "high" else "partial",
                    "l2_evidence": "annual_report", "l2_p_pos": 0.9, "evidence_sha": f"sha{i}", "shortlist_tier": tier}
        rows = [row(i, "high") for i in range(1, 5)] + [row(i, "confirm") for i in range(5, 27)]
        res = {"run_id": "scr-x", "idea": IDEA, "idea_en": IDEA_EN, "params": {"max_out": 40}, "rows": rows,
               "unverified": []}
        inputs = {r["company_key"]: {"text": "It makes liquid-cooling units. It sells them to storage plants.",
                                     "evidence_sha": r["evidence_sha"], "evidence": "annual_report"} for r in rows}
        held = [{"sid": "s1", "family": "geo", "value": "outside_only", "kind": "geo.outside_only",
                 "v": [f"k{i}" for i in range(15, 27)]}]
        with mock.patch.object(review, "DECK_A_MAX", 25):          # a cap smaller than the listed rows
            deck = review.build_deck(res, inputs, None, {}, part="A", held=held)
        self.assertEqual(len(deck["items"]), 25)
        tops = [it["rank"] for it in deck["items"] if it["group"] == "top"]
        self.assertEqual(tops[-4:], [1, 2, 3, 4])                  # every main row is read
        self.assertEqual(tops[:-4], list(range(5, 14)))           # the to-confirm rows fill the rest, first


class SectionMoves(unittest.TestCase):
    def test_the_diff_says_which_rows_changed_section_and_who_moved_them(self):
        from jevscreen import calib
        before = sections_result(2, 5)
        after = copy_result(before)
        c = next(r for r in after["rows"] if r["shortlist_tier"] == "confirm")
        c.update(agent_verdict="yes", agent_level="explicit", agent_quote_ids=[1], agent_state="applied")
        m = next(r for r in after["rows"] if r["shortlist_tier"] == "high")
        m.update(agent_verdict="no", agent_state="escalated")
        after["rows"], after["shortlist"] = shortlist.apply(sorted(after["rows"], key=lambda r: r["rank"]))
        zh = calib.render_diff_zh(before, after)
        self.assertIn("进入确认名单 1：", zh)
        self.assertIn("你的 AI 核对", zh)
        self.assertIn("移到待核对 1：", zh)
        en = calib._diff_en(before, after, "t")
        self.assertIn("into the confirmed list 1: ", en)
        self.assertIn("moved to confirm 1: ", en)
        self.assertIsNone(CJK.search(en.replace("t\n", "")), en)

    def test_your_ais_summary_names_the_rows_it_moved(self):
        summ = {"read": 7, "annual": 7, "profile": 0, "removed_total": 0, "removed": [], "names_zh": "",
                "names_en": "", "moved_in": [{"security_id": "X", "name_zh": "冷却七", "name_en": "Coolant 7"}]}
        zh = review_cli.summary_text(summ, "zh")
        self.assertIn("没有移出公司", zh)
        self.assertNotIn("名单不用改", zh)
        self.assertIn("放进确认名单的 1 家：冷却七", zh)
        en = review_cli.summary_text(summ, "en")
        self.assertIn("moved into the confirmed list: 1 (Coolant 7)", en)


def copy_result(res):
    import copy
    return copy.deepcopy(res)


class Wording(unittest.TestCase):
    def test_the_rescued_line_and_the_fold_do_not_call_to_confirm_rows_listed(self):
        for lang in ("zh", "en"):
            S = page.STRINGS[lang]
            res = sections_result(0, 6)
            for r in res["rows"][:2]:
                r["l1_rescued"] = True          # first read missed, the annual report mentions it
            d = data_of(res, lang)
            txt = page.render_text(d, lang)
            self.assertIn(S["funnel_rescued_split"].format(n=2), txt)
            self.assertNotIn(S["funnel_rescued"].format(n=2), txt)
            self.assertIn(S["unverified_title_split"].format(n=d["unverified_total"]), txt)
            if shutil.which("node"):
                dom = dom_text(self, page.render_page(d))["text"]
                self.assertIn(S["funnel_rescued_split"].format(n=2), dom)
                self.assertIn(S["unverified_title_split"].format(n=d["unverified_total"]), dom)
                self.assertNotIn(S["unverified_title"].format(n=d["unverified_total"]), dom)

    def test_a_main_row_says_stated_not_related(self):
        for lang in ("zh", "en"):
            S = page.STRINGS[lang]
            d = data_of(sections_result(), lang)
            top = page.top_rows(d)
            self.assertEqual({r[f"verdict_words_{lang}"] for r in top}, {S["verdict_main"]})
            self.assertEqual({r[f"verdict_words_{lang}"] for r in page.to_confirm_brief(d)["rows"]},
                             {S["verdict_partial"]})
            self.assertTrue(S["main_note"].startswith("推断：" if lang == "zh" else "Inferred:"))
            if shutil.which("node"):
                dom = dom_text(self, page.render_page(d))
                self.assertTrue(all(v.startswith(S["verdict_main"]) for v in dom["table_verdicts"]), dom)

    def test_a_row_your_ai_moved_out_shows_one_badge(self):
        res = sections_result(3, 3)
        m = next(r for r in res["rows"] if r["shortlist_tier"] == "high")
        m.update(agent_verdict="no", agent_state="escalated", agent_why_zh="只是买方", agent_why_en="a buyer")
        res["rows"], res["shortlist"] = shortlist.apply(sorted(res["rows"], key=lambda r: r["rank"]))
        if shutil.which("node"):
            for lang in ("zh", "en"):
                S = page.STRINGS[lang]
                dom = dom_text(self, page.render_page(data_of(res, lang)))["text"]
                self.assertIn(S["via_agent_no"], dom)
                self.assertNotIn(S["agent_tag"] + ("：" if lang == "zh" else ": ") + S["agent_no"] + "\n", dom)

    def test_an_unfinished_empty_run_does_not_advise_rewording(self):
        for lang in ("zh", "en"):
            S = page.STRINGS[lang]
            d = data_of(sections_result(0, 0, status="budget_exhausted"), lang)
            txt = page.render_text(d, lang)
            self.assertIn(S["main_empty_open"], txt)
            self.assertNotIn(S["main_empty0"], txt)
            if shutil.which("node"):
                dom = dom_text(self, page.render_page(d))["text"]
                self.assertIn(S["main_empty_open"], dom)
                self.assertNotIn(S["main_empty0"], dom)

    def test_the_chat_says_each_thing_once(self):
        zh = qs.human_text(_job(data_of(sections_result(), "zh")), "done", [], "zh", None)
        self.assertEqual(zh.count("不算入选"), 1, zh)
        self.assertEqual(zh.count("（免费"), 1, zh)                      # the free why step, once
        self.assertNotIn("只有简介 0", zh)                               # no profile-only row: no breakdown
        en = qs.human_text(_job(data_of(sections_result(), "en")), "done", [], "en", None)
        self.assertIn("7 more to confirm (not in the confirmed list)", en)
        self.assertNotIn("not on the list", en)
        self.assertNotIn("0 by profile only", en)
        zh0 = qs.human_text(_job(data_of(sections_result(0, 0), "zh")), "done", [], "zh", None)
        self.assertIn("也没有待核对的公司", zh0)
        self.assertNotIn("另有 0 家", zh0)
        self.assertEqual(zh0.count("这次没有公司能从原文确认"), 1, zh0)

    def test_the_to_confirm_reason_names_the_first_read(self):
        self.assertIn("初读只算相关业务", page.STRINGS["zh"]["confirm_note"])
        self.assertIn("first read did not judge it their central business", page.STRINGS["en"]["confirm_note"])
        self.assertIn("初读只算相关业务", short_list.TEXT["zh"]["main_short_confirm"])
        self.assertIn("not in the confirmed list", page.STRINGS["en"]["confirm_title"])


class PooledHeadline(unittest.TestCase):
    def test_the_headline_is_pooled_over_the_main_rows_with_the_mean_over_ideas_beside_it(self):
        a = {"id": "a", "type": "other", "labels": [{"security_id": "NYSE:A", "label": "right"}]}
        b = {"id": "b", "type": "other", "labels": [{"security_id": "NYSE:B", "label": "right"},
                                                    {"security_id": "NYSE:C", "label": "wrong"},
                                                    {"security_id": "NYSE:D", "label": "wrong"}]}
        hi = {"l1_label": "core", "l2_label": "explicit"}
        sa = evalset.score(a, {"rows": [dict(hi, rank=1, security_id="NYSE:A")], "status": "ok"})
        sb = evalset.score(b, {"rows": [dict(hi, rank=i, security_id=s) for i, s in
                                        enumerate(("NYSE:B", "NYSE:C", "NYSE:D"), 1)], "status": "ok"})
        agg = evalset.aggregate([sa, sb])["all"]
        self.assertEqual((agg["p_main"], agg["n_main_labelled"]), (0.5, 4))          # 2 right / 4 labelled
        self.assertAlmostEqual(agg["p_main_mean"], (1.0 + 1 / 3) / 2, places=3)
        head = evalset.report_md([sa, sb], evalset.aggregate([sa, sb])).split("\n")[2]
        self.assertIn("pooled over the main-list rows 50%", head)
        self.assertIn("mean over ideas 67%", head)


if __name__ == "__main__":
    unittest.main()
