"""The owner rule outside the page: `answer`, `cards` and `why` speak the chosen language (--lang, default the idea's
quickstart language), and a finished quickstart hands the agent the translation step (translation_pending plus
the exact commands) that fills the page with its translations.

No network, no paid calls (FakeJev / RuleJev); invented companies only.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401

from jevscreen import calib, page, quickstart as qs, quickstart_cli  # noqa: E402
from test_cli_calib import IDEA, CliCase, rule_factory  # noqa: E402
from test_translations import zh_of  # noqa: E402

HAN = re.compile(r"[一-鿿]")


class TestEnglishOutputs(CliCase):
    def test_answer_and_cards_in_english(self):
        rid, _od = self.screen_with_deck(factory=rule_factory(self.log))
        code, out, _e = self.main(["cards", rid, "--lang", "en"])
        self.assertEqual(code, 0)
        self.assertIn("[1] RoboCorp", out)
        self.assertIn("Answer (example): jevscreen answer", out)
        self.assertIn("SEC 10-K", out)
        self.assertEqual(HAN.findall(out), [], out)
        code, out, err = self.main(["answer", "1c", "--lang", "en"], rule_factory(self.log))
        self.assertEqual(code, 0, out + err)
        new_id, _p, _nod = self.last_run()
        for want in ("Recorded 1 answer", "Takes effect now (free)", "out 1: RoboCorp #1 (you: no",
                     "Your answers suggest 1 rule", "Estimate about $", "Rule trials: 1 adopted",
                     f"new result {new_id}", "The result page is updated (the same page: refresh it): file://"):
            self.assertIn(want, out)
        self.assertEqual(HAN.findall(out), [], out)
        code, _out, err = self.main(["answer", "9c", "--lang", "en"])
        self.assertIn("There is no card 9", err)

    def test_the_default_language_is_the_ideas_quickstart_language(self):
        self.assertEqual(quickstart_cli.page_lang(self.cfg, "储能液冷"), "zh")
        job = {"format": qs.FORMAT, "idea": IDEA, "idea_key": qs.idea_key(IDEA), "lang": "zh"}
        qs.save_job(self.cfg, job)
        self.assertEqual(quickstart_cli.page_lang(self.cfg, IDEA), "zh")
        job["lang"] = "en"
        qs.save_job(self.cfg, job)
        self.assertEqual(quickstart_cli.page_lang(self.cfg, IDEA), "en")

    def test_why_in_english_names_no_internal_label(self):
        rid, _od = self.screen_with_deck()
        code, out, err = self.main(["why", "ROBO", "--run", rid, "--lang", "en"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("step 2", out)
        for raw in (" explicit", " partial", "annual_report", " l1 ", " l2 ", "said explicit"):
            self.assertNotIn(raw, out)
        self.assertEqual(HAN.findall(out), [], out)


class TestQuickstartTranslationStep(CliCase):
    """A finished run's quickstart JSON carries translation_pending and the two commands; after the agent's import
    the same status reports 0 pending and the top rows carry the translations."""

    def test_status_hands_over_the_translation_step(self):
        rid, od = self.screen_with_deck()
        result = json.loads((od / "results.json").read_text(encoding="utf-8"))
        # the human chose Chinese for this idea: its page (and every output) is Chinese
        job = {"format": qs.FORMAT, "idea": IDEA, "idea_key": qs.idea_key(IDEA), "lang": "zh", "state": "done", "created_at":
               result.get("started_at"), "result": {"run_id": rid, "status": "ok", "started_at":
                                                     result.get("started_at")}}
        qs.save_job(self.cfg, job)
        code, out, err = self.main(["page", rid, "--json"])
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual(info["lang"], "zh")
        job["result"]["page"] = info["page"]
        qs.save_job(self.cfg, job)
        st = qs.status(self.cfg, job["idea_key"], spawn=lambda *a: None)
        n = st["translation_pending"]
        self.assertGreater(n, 0)
        tr = st["translation"]
        self.assertEqual((tr["lang"], tr["pending"], tr["batch_max"]), ("zh", n, 60))
        self.assertIn(f"jevscreen page {rid} --export-strings", tr["export_command"])
        self.assertIn(f"jevscreen page {rid} --import-translations", tr["import_command"])
        self.assertTrue(any(r["one_line_needs_translation"] for r in st["top"]))
        # the agent: export, translate, import (the exact commands of the JSON), until nothing is pending
        for _round in range(5):
            argv = tr["export_command"].split()[1:]
            code, out, err = self.main(argv)
            self.assertEqual(code, 0, err)
            items = json.loads(Path(json.loads(out)["file"]).read_text(encoding="utf-8"))
            for it in items:
                it["translation"] = zh_of(it["text"])
            Path(json.loads(out)["file"]).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            code, out, err = self.main(tr["import_command"].split()[1:])
            self.assertEqual(code, 0, err)
            if json.loads(out)["translation_pending"] == 0:
                break
        st = qs.status(self.cfg, job["idea_key"], spawn=lambda *a: None)
        self.assertEqual(st["translation_pending"], 0)
        top = st["top"][0]
        self.assertTrue(top["one_line_translated"])
        self.assertEqual(top["one_line"], zh_of(top["one_line_original"]))
        self.assertTrue(top["name_translated"])
        self.assertEqual(top["name"], zh_of("RoboCorp"))
        html = Path(st["page"]).read_text(encoding="utf-8")
        data = json.loads(re.search(r'id="data">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(data["translation"]["pending"], 0)
        text = page.render_text(data)
        self.assertIn("（AI 翻译）", text)
        self.assertNotIn("RoboCorp develops", text)                   # every profile line is translated


if __name__ == "__main__":
    unittest.main()
