"""The confirmed list only holds rows whose shown evidence supports the call (owner review 2026-09-29).

A row the main rule would list (L1 core + L2 explicit) whose page would mark it 边缘 / Borderline - the excerpt it
shows does not mention the idea (page.shown_gap 'no_mention'), or the repeated reads were split ('edge') - goes to
the 待核对 / To confirm section with that reason. The tier logic decides it (shortlist.main_of / settle), so the page,
quickstart's top, report.md and the eval agree; a human yes pin still wins, and a cited sentence of your AI's explicit
yes that links the product to the target clears it (the page then says 明确 / Clearly fits).

Also: a confirmed row says 原文写明 / Stated (the section's note says once it is the AI's reading), and the rows of
the to-confirm section carry no 待核对 pill of their own.

Offline: invented companies, test_screen's seeded store and fakes (no network, no paid call).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_no_padding as NP  # noqa: E402
from jevscreen import evalset, page, page_hud, screen, shortlist  # noqa: E402
from test_page import dom_text  # noqa: E402

OFF_TOPIC = "The company makes industrial pumps and valves for water utilities."


def gap_result(n_main=3, n_confirm=4):
    """sections_result with the second main row's excerpt off the idea (its tier still 'high', as a run written
    before the rule has it)."""
    res = NP.sections_result(n_main, n_confirm)
    m = [r for r in res["rows"] if r["shortlist_tier"] == "high"][1]
    m["evidence_excerpt"] = OFF_TOPIC
    return res, m["company_key"]


class Tier(unittest.TestCase):
    HIGH = {"l1_label": "core", "l2_label": "explicit"}
    YES = {"agent_verdict": "yes", "agent_level": "explicit", "agent_quote_ids": [1], "agent_state": "applied"}

    def test_a_row_its_page_marks_borderline_is_to_confirm(self):
        self.assertEqual(shortlist.tier_of({**self.HIGH, "shown_gap": "no_mention"}), "confirm")
        self.assertEqual(shortlist.main_of({**self.HIGH, "shown_gap": "no_mention"}), ("confirm", None))
        self.assertEqual(shortlist.main_of({**self.HIGH, "shown_gap": "edge"}), ("confirm", None))
        self.assertEqual(shortlist.main_of(dict(self.HIGH)), ("high", None))

    def test_your_ais_yes_alone_does_not_undo_it_the_humans_yes_does(self):
        self.assertEqual(shortlist.main_of({**self.HIGH, **self.YES, "shown_gap": "no_mention"}), ("confirm", None))
        human = {**self.HIGH, "shown_gap": "no_mention", "verdict_source": "user", "user_verdict": "explicit"}
        self.assertEqual(shortlist.main_of(human), ("high", "user"))


class ShownGap(unittest.TestCase):
    def setUp(self):
        self.res = NP.sections_result(3, 1)
        self.check = page.gap_check(self.res, {})

    def row(self, **kw):
        return {**NP._row(9), **kw}

    def test_the_excerpt_shown_decides(self):
        self.assertIsNone(self.check(self.row()))
        self.assertEqual(self.check(self.row(evidence_excerpt=OFF_TOPIC)), "no_mention")
        self.assertEqual(self.check(self.row(l2_edge=True)), "edge")
        # the human answered this company: never a gap of the page's own
        self.assertIsNone(self.check(self.row(evidence_excerpt=OFF_TOPIC, verdict_source="user",
                                              user_verdict="explicit")))

    def test_a_later_piece_of_the_text_layer_2_read_that_names_the_idea_is_the_quote(self):
        l2 = {"k9": {"sha": "sha9", "text": OFF_TOPIC, "overview": None,
                     "pieces": [OFF_TOPIC, "It also supplies liquid-cooling units for battery energy storage."]}}
        check = page.gap_check(self.res, l2)
        self.assertIsNone(check(self.row(evidence_excerpt=OFF_TOPIC)))

    def test_it_is_the_same_call_the_page_makes(self):
        rows = [self.row(), self.row(evidence_excerpt=OFF_TOPIC), self.row(l2_edge=True)]
        for i, r in enumerate(rows, 1):
            r.update(rank=i, company_key=f"g{i}", security_id=f"NYSE:G{i}")
        res = {**self.res, "rows": rows, "shortlist": None}
        d = page.build_page_data(res, None, lang="en", l2_pieces={})
        self.assertEqual([r["verdict"] == "edge" for r in d["rows"]], [bool(self.check(r)) for r in rows])


class Settle(unittest.TestCase):
    def test_the_row_moves_to_confirm_and_settling_twice_changes_nothing(self):
        res, k = gap_result()
        ranks_before = {r["company_key"]: r["rank_before_shortlist"] for r in res["rows"]}
        shortlist.settle(res, {})
        row = next(r for r in res["rows"] if r["company_key"] == k)
        self.assertEqual((row["shortlist_tier"], row["shown_gap"]), ("confirm", "no_mention"))
        self.assertEqual((res["shortlist"]["high"], res["shortlist"]["confirm"], res["shortlist"]["gap"]), (2, 5, 1))
        self.assertEqual([r["rank"] for r in res["rows"]], list(range(1, 8)))
        self.assertEqual([r["shortlist_tier"] for r in res["rows"]], ["high"] * 2 + ["confirm"] * 5)
        # explicit rows first in the to-confirm section: the moved row leads it
        self.assertEqual(res["rows"][2]["company_key"], k)
        self.assertEqual({r["company_key"]: r["rank_before_shortlist"] for r in res["rows"]}, ranks_before)
        again = json.loads(json.dumps(res))
        shortlist.settle(again, {})
        self.assertEqual(again, res)

    def test_a_human_yes_pin_still_wins(self):
        res, k = gap_result()
        row = next(r for r in res["rows"] if r["company_key"] == k)
        row.update(verdict_source="user", user_verdict="explicit")
        shortlist.settle(res, {})
        self.assertEqual(next(r for r in res["rows"] if r["company_key"] == k)["shortlist_tier"], "high")
        self.assertEqual(res["shortlist"]["high"], 3)

    def test_a_single_padded_list_is_left_as_it_is(self):
        res, _k = gap_result()
        res.pop("shortlist")
        before = json.loads(json.dumps(res))
        self.assertEqual(shortlist.settle(res, {}), before)


class Agree(unittest.TestCase):
    """A run written before the rule (tier high stored, the excerpt off the idea): page, chat, eval say the same."""

    def test_page_and_chat(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                S = page.STRINGS[lang]
                res, k = gap_result()
                d = NP.data_of(res, lang)
                main, confirm = page.main_and_confirm(d)
                self.assertEqual((len(main), len(confirm)), (2, 5))
                self.assertFalse([r for r in main if r["verdict"] == "edge" or "no_mention" in r["badges"]])
                moved = next(r for r in confirm if r["security_id"] == f"NYSE:C{int(k[1:]):02d}")
                self.assertEqual(moved["verdict"], "edge")
                self.assertEqual(moved["badges"][0], "no_mention")
                self.assertNotIn(moved["ticker"], [r["ticker"] for r in page.top_rows(d)])
                self.assertIn(moved["security_id"], [r["security_id"] for r in page.to_confirm_brief(d)["rows"]])
                txt = page.render_text(d, lang)
                self.assertIn(S["split_line"].format(m=2, u=5), txt)
                main_part, confirm_part = txt.split(S["confirm_title"].format(n=5))
                self.assertNotIn(S["badge_no_mention"], main_part)
                gap = S["quote_gap"].split("（")[0].split(" (")[0]
                moved_line = next(ln for ln in confirm_part.splitlines() if moved["ticker"] in ln)
                self.assertIn(S["verdict_edge"], moved_line)
                self.assertIn(gap, moved_line)
                # borderline and the gap are each said once (never "边缘 边缘：…" / "Borderline Borderline: …")
                self.assertEqual(moved_line.count(S["verdict_edge"]), 1, moved_line)
                self.assertNotIn(S["badge_no_mention"], moved_line)
                self.assertNotIn(S["verdict_edge"], main_part.split(S["main_title"].format(n=2))[1])

    def test_eval_scores_the_same_sections(self):
        res, k = gap_result()
        sid = f"NYSE:C{int(k[1:]):02d}"
        data = {"id": "xx", "type": "other", "labels": [{"security_id": sid, "label": "right"}]}
        s = evalset.score(data, res)
        self.assertEqual((s["main"]["total"], s["main"]["to_confirm"], s["main"]["right"]), (2, 5, 0))
        bare = {k2: v for k2, v in res.items() if k2 != "shortlist"}
        bare["rows"] = [{x: v for x, v in r.items() if x not in ("shortlist_tier", "rank_before_shortlist")}
                        for r in res["rows"]]
        s2 = evalset.score(data, bare)
        self.assertEqual((s2["main"]["total"], s2["main"]["right"]), (2, 0))


class Why(unittest.TestCase):
    def test_the_to_confirm_reason_names_the_gap(self):
        from jevscreen import why
        zh, en = zip(*why._confirm_reasons({"l1_label": "core", "l2_label": "explicit", "shown_gap": "no_mention"}))
        self.assertIn("摘录没提到你的想法", "".join(zh))
        self.assertIn("does not mention your idea", " ".join(en))


class ScreenAndVersion(NP.ScreenDefault):
    """A screen writes the settled sections; a free new version of a run settles them too."""

    def test_a_version_of_a_run_whose_confirmed_row_shows_an_off_topic_excerpt(self):
        res = self.screen_()
        self.assertEqual([(r["security_id"], r["shortlist_tier"]) for r in res["rows"]],
                         [("NYSE:ROBO", "high"), ("NASDAQ:ROB2", "confirm")])
        self.assertTrue(all(not r.get("shown_gap") for r in res["rows"]))
        out = self.home / "out"
        # the saved texts of ROBO no longer name the idea (as if its filing talked about something else)
        saved = json.loads((out / "results.json").read_text(encoding="utf-8"))
        for r in saved["rows"]:
            if r["security_id"] == "NYSE:ROBO":
                r["evidence_excerpt"] = OFF_TOPIC
        (out / "results.json").write_text(json.dumps(saved), encoding="utf-8")
        lines = [json.loads(x) for x in (out / "l2_inputs.jsonl").read_text(encoding="utf-8").splitlines() if x]
        for ln in lines:
            if ln["security_id"] == "NYSE:ROBO":
                ln["text"] = OFF_TOPIC
                ln["excerpts"] = [{"kind": "business", "text": OFF_TOPIC}]
        (out / "l2_inputs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        ro = screen.rank_only_run(self.cfg, "humanoid robots", from_run=res["run_id"], sieve="none",
                                  out_dir=self.home / "ro")
        got = {r["security_id"]: (r["shortlist_tier"], r.get("shown_gap")) for r in ro["rows"]}
        self.assertEqual(got["NYSE:ROBO"], ("confirm", "no_mention"))
        self.assertEqual((ro["shortlist"]["high"], ro["shortlist"].get("gap")), (0, 1))
        md = (self.home / "ro" / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Main list (confirmed): 0", md)
        d = page.build_page_data(ro, None, lang="en")
        self.assertEqual(page.top_rows(d), [])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Wording(unittest.TestCase):
    def test_stated_per_row_and_the_ais_reading_said_once(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                S = page.STRINGS[lang]
                self.assertEqual(S["verdict_main"], "原文写明" if lang == "zh" else "Stated")
                reading = "「原文写明」是 AI 的判断" if lang == "zh" else "\"Stated\" is the AI's reading"
                self.assertIn(reading, S["main_note"])
                self.assertIn(reading, S["main_note_judge"])
                d = NP.data_of(NP.sections_result(), lang)
                dom = dom_text(self, page.render_page(d))
                self.assertEqual(dom["table_verdicts"], [S["verdict_main"]] * 3)
                # once per surface: main_note on the plain page, main_hint in the HUD panel (which hides main_note);
                # each hides the other's line (CSS)
                self.assertEqual(dom["text"].count(reading), 2)
                self.assertIn(reading, S["main_hint"])
                self.assertIn("p.mainhint{display:none}", page.CSS)
                self.assertRegex(page_hud.CSS, r"p\.mainnote[^{}]*\{display:none\}")
                self.assertNotIn("AI 判断）" if lang == "zh" else "(the AI's reading)", dom["text"])

    def test_no_to_confirm_pill_on_the_rows_of_the_section(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                S = page.STRINGS[lang]
                d = NP.data_of(NP.sections_result(3, 4), lang)
                text = dom_text(self, page.render_page(d))["text"]
                section = text.split(S["confirm_note"])[1]
                word = "待核对" if lang == "zh" else "to confirm"
                self.assertEqual(len(re.findall(re.escape(word), section)), 0, section[:400])
                self.assertIn("Coolant", section)

    def test_a_moved_row_says_borderline_once_on_the_page(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                S = page.STRINGS[lang]
                res, _k = gap_result()
                text = dom_text(self, page.render_page(NP.data_of(res, lang)))["text"]
                section = text.split(S["confirm_note"])[1]
                self.assertIn(S["badge_no_mention"], section)
                self.assertNotIn(S["verdict_edge"] + ":", text)
                self.assertNotIn(S["verdict_edge"] + "：", text)
                self.assertNotRegex(text, re.escape(S["verdict_edge"]) + r"\s+" + re.escape(S["verdict_edge"]))
                for key in ("badge_edge", "badge_no_mention"):
                    self.assertNotIn(S["verdict_edge"].lower(), S[key].lower(), key)


class AllConfirmed(unittest.TestCase):
    """The confirmed list is the product's answer: the page lists every confirmed row (the table under its header
    too, never sliced to 10); the chat relays at most 10 and says how many more the page lists."""

    def test_the_page_table_lists_every_confirmed_row(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                S = page.STRINGS[lang]
                d = NP.data_of(NP.sections_result(16, 4), lang)
                self.assertEqual(len(page.main_and_confirm(d)[0]), 16)
                dom = dom_text(self, page.render_page(d))
                self.assertIn(S["main_title"].format(n=16), dom["text"])
                self.assertEqual(len(dom["table_ranks"]), 16)
                txt = page.render_text(d, lang)
                self.assertEqual(len(re.findall(r"^#\d+ ", txt, re.M)), 16)

    def test_the_chat_relays_ten_and_names_the_rest(self):
        from jevscreen import quickstart as qs
        from jevscreen import short_list
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                d = NP.data_of(NP.sections_result(16, 4), lang)
                top = page.top_rows(d, 10)
                self.assertEqual((len(top), page.top_more(d, len(top))), (10, 6))
                res = {"run_id": d["run_id"], "summary": page.summary_of(d, None), "short_list": short_list.of(d),
                       "top": top, "top_more": page.top_more(d, len(top)), "page": None, "page_opened": True,
                       "cost_usd": 0.2, "seconds": 60, "next_steps": []}
                text = qs.human_text({"idea": NP.IDEA, "result": res}, "done", [], lang, None)
                self.assertIn(page.STRINGS[lang]["chat_top_more"].format(n=10, k=6, m=16), text)
                self.assertEqual(bool(NP.CJK.search(page.STRINGS[lang]["chat_top_more"])), lang == "zh")
                small = NP.data_of(NP.sections_result(3, 4), lang)
                self.assertEqual(page.top_more(small, len(page.top_rows(small, 10))), 0)
                res2 = {**res, "top": page.top_rows(small, 10), "top_more": 0}
                self.assertNotIn(page.STRINGS[lang]["chat_top_more"].split("{")[0],
                                 qs.human_text({"idea": NP.IDEA, "result": res2}, "done", [], lang, None))


if __name__ == "__main__":
    unittest.main()
