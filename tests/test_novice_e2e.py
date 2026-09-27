"""The novice path end to end (the 2026-09 novice simulation): a Chinese idea whose English sentence starts with the
ordinary word "Core" while a company "Core Inc" is in the names, the key recorded after the worker started, one card
round with `jevscreen answer`, then `quickstart --status` and `page --text`. Checks: no false idea_en refusal, the
budget is not asked again, the status and the one stable page show the newest run, and the page text carries no
template placeholders and no internal labels.

No network and no paid calls: the fakes of test_quickstart / test_screen; every company is invented.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401

from jevscreen import calib, cli, keys, page, quickstart as qs, screen, store  # noqa: E402
from test_novice_flow import CN_ROWS, CORE_EN, ZH_IDEA, FlowCase, add_cn  # noqa: E402
from test_novice_review import add_us  # noqa: E402
from test_quickstart import FAKE_KEY  # noqa: E402
from test_screen import FakeKeywords  # noqa: E402

LOG: list[str] = []        # a readable transcript of the path (printed by the scripted run, unused by the suite)


def say(*a):
    LOG.append(" ".join(str(x) for x in a))


RAW = re.compile(r"\b(adjacent|p_core|p̄|not_run|annual_report|profile_only|excerpt_mentions_idea|verdict_from_user|"
                 r"l2_status|security_id|evidence_kind|user_only|budget_exhausted|core)\b")
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


class TestNoviceEndToEnd(FlowCase):
    def run_cli(self, *argv):
        with mock.patch.object(screen, "_default_factory", self.factory), \
                mock.patch.object(screen, "_default_keywords", FakeKeywords()), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_novice_path(self):
        idea = ZH_IDEA
        key = qs.idea_key(idea)
        self.consent_yes("zh")
        # as in test_novice_review: a seed term that makes the fake reads borderline, so the run has cards
        calib.save_sieve(calib.sieve_path(self.cfg, idea),
                         {**calib.new_sieve(idea), "target_terms": {"en": ["android"]}})
        qs.write_names_cache(self.cfg, ["Core Inc", "Frostline Thermal"], ["CORE"])   # the word is a real name
        # 1) front: idea_en starts with "Core", approve $1, no key yet
        out = self.front(idea, idea_en=CORE_EN, approve_budget=1)
        say("1 front:", out["status"], out["exit_code"], "pending=", self.ids(out), "state=", out.get("state"),
            "poll=", bool(out.get("poll_command")), "idea_en_problems=", out.get("idea_en_problems"))
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(self.ids(out), ["key_openrouter"])
        self.assertFalse(out.get("idea_en_problems"))
        self.assertTrue(out.get("poll_command"))
        # 2) worker; the key file is recorded mid-download; Core Inc and the Chinese companies arrive with the list
        keyfile = self.home / "my-key.txt"
        keyfile.write_text(FAKE_KEY + "\n")
        os.chmod(keyfile, 0o600)
        orig_refresh = self.refresh.__call__

        def refresh_then_key(cfg, con, client):
            r = orig_refresh(cfg, con, client)
            keys.record_key_file(self.cfg, "openrouter", keyfile)
            return r
        self.deps.refresh_universe = refresh_then_key
        orig_after = qs.Worker.after_universe

        def after(w):
            add_cn(self.cfg, CN_ROWS)
            add_us(self.cfg, [("NASDAQ:CORE", "Core Inc")])
            return orig_after(w)
        with mock.patch.object(qs.Worker, "after_universe", after):
            self.work(key)
        v1 = qs.status(self.cfg, key, spawn=lambda cfg, k: self.spawned.append(k))
        say("2 status after worker:", v1["status"], v1["exit_code"], "run=", v1.get("run_id"),
            "idea_en=", self.job(idea).get("idea_en"), "idea_en_problems=", v1.get("idea_en_problems"),
            "pending=", self.ids(v1), "canary=", self.calls.canary,
            "top=", [r["name"] for r in v1.get("top") or []])
        self.assertEqual(v1["status"], "done", v1.get("text_en"))
        self.assertEqual(self.job(idea).get("idea_en"), CORE_EN)
        self.assertFalse(v1.get("idea_en_problems"))
        self.assertNotIn("reprice_idea_en", self.ids(v1))
        self.assertNotIn("approve_budget", self.ids(v1))
        self.assertEqual(self.calls.canary, 1)
        self.assertNotIn("idea_en refused", " ".join(v1.get("notes") or []))
        # the agent re-runs the front command (as a novice's agent does): the budget is not asked again
        again = self.front(idea, idea_en=CORE_EN)
        say("2b front again:", again["status"], "pending=", self.ids(again))
        self.assertNotIn("approve_budget", self.ids(again))
        self.assertNotIn("reprice_idea_en", self.ids(again))
        # 3) one card round
        with store.session(self.cfg, read_only=True) as con:
            od = con.execute("SELECT output_dir FROM screen_runs WHERE run_id = ?", [v1["run_id"]]).fetchone()[0]
        deck = calib.load_deck(Path(od))
        self.assertTrue(deck and deck["cards"], "no cards")
        c1 = deck["cards"][0]
        say("3 cards:", len(deck["cards"]), "card1=", c1.get("name"), c1.get("security_id"))
        code, text, err = self.run_cli("answer", f"{c1['n']}不要c", "--apply-budget", "1")
        say("3 answer exit:", code)
        for ln in text.strip().splitlines():
            say("   |", ln)
        if err.strip():
            say("   stderr:", err.strip()[:300])
        self.assertIn(code, (0, 7), text + err)
        for internal in ("user_not_supplier", "L1 沿用", "候选规则："):
            self.assertNotIn(internal, text)
        # 4) --status after the answer: the newest run, the one stable page, the answered company not in top
        with store.session(self.cfg, read_only=True) as con:
            newest = con.execute("SELECT run_id FROM screen_runs WHERE trim(idea) = ? AND status IN ('ok','partial') "
                                 "ORDER BY started_at DESC LIMIT 1", [idea]).fetchone()[0]
        code, stext, _e = self.run_cli("quickstart", "--status", idea, "--json")
        v3 = json.loads(stext)
        say("4 status after answer: exit", code, v3["status"], "run=", v3["run_id"], "newest=", newest,
            "version=", v3.get("version"), "change_zh=", v3.get("change_zh"), "page=",
            Path(v3["page"]).name, "top=", [r["name"] for r in v3.get("top") or []])
        self.assertEqual(v3["run_id"], newest)
        self.assertNotEqual(v3["run_id"], v1["run_id"])
        self.assertEqual(Path(v3["page"]), page.stable_path(self.cfg, idea))
        self.assertNotIn(c1["security_id"], [r.get("security_id") for r in v3.get("top") or []])
        self.assertNotIn(c1.get("name"), [r["name"] for r in v3.get("top") or []])
        self.assertEqual(qs.read_page_data(page.stable_path(self.cfg, idea))["run_id"], newest)
        # 5) page --text (zh and en), JSON and plain
        for lang in ("zh", "en"):
            code, ptext, _e = self.run_cli("page", "--text", "--lang", lang, "--json")
            info = json.loads(ptext)
            txt = info["text"]
            say(f"5 page --text {lang}: exit", code, "run=", info["run_id"], "page=", Path(info["page"]).name,
                "lines=", len(txt.splitlines()))
            for ln in txt.splitlines()[:14]:
                say("   |", ln)
            self.assertEqual(info["run_id"], newest)
            self.assertEqual(PLACEHOLDER.findall(txt), [], txt)
            self.assertEqual(RAW.findall(txt), [], txt)
        html = page.stable_path(self.cfg, idea).read_text(encoding="utf-8")
        nos = re.search(r"<noscript>(.*?)</noscript>", html, re.S)
        self.assertTrue(nos)
        self.assertEqual(PLACEHOLDER.findall(nos.group(1)), [])
        say("5b stable page:", page.stable_path(self.cfg, idea).name, "bytes=", len(html), "noscript ok")



if __name__ == "__main__":
    unittest.main()
