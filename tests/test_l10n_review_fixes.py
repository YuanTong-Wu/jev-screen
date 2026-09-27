"""Review fixes of the one-language page and the agent translations (feat/full-l10n):

- the advertised import command keeps the page's language (a page rebuilt with --lang in the other language);
- a name the agent keeps as it is (no usual Chinese name) or leaves "keep original" is resolved, so the
  translation rounds end;
- a translated excerpt is never tagged as the filing fact;
- an agent-translated name always carries the original name (top table, lists, the chat text);
- a busy store never exports already translated texts nor overwrites a translated page with an untranslated one;
- market caps round before choosing the unit;
- `cards` prints the stored translations, `answer` errors speak the chosen language.

No network, no paid calls (fakes); invented companies only.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401

from jevscreen import cli, l10n, page, quickstart as qs, screen, store, translations as tr  # noqa: E402
from test_cli_calib import IDEA  # noqa: E402
from test_page import LOCAL, PageCase, _novice_result, deck3, dom_text  # noqa: E402
from test_screen import make_factory  # noqa: E402
from test_translations import zh_of  # noqa: E402


def data_of(html: str) -> dict:
    return json.loads(re.search(r'id="data">(.*?)</script>', html, re.S).group(1))


class Case(PageCase):
    def raw_main(self, argv):
        """cli.main without CliCase.main's automatic --lang zh for answer / cards."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), \
                mock.patch.object(screen, "_default_factory", make_factory(self.log)), \
                mock.patch.object(screen, "_default_keywords", self.kw), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def run_cmd(self, command: str):
        argv = shlex.split(command)
        self.assertEqual(argv[0], "jevscreen")
        return self.main(argv[1:])

    def translate_file(self, path: str, fn=zh_of) -> list:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
        for it in items:
            it["translation"] = fn(it)
        Path(path).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return items

    def translate_all_zh(self, rid: str) -> dict:
        """The agent's loop with the exact advertised commands, until nothing is pending (zh page of the English
        fixture idea)."""
        code, out, err = self.main(["page", rid, "--lang", "zh", "--json"])
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        for _round in range(5):
            if not info["translation_pending"]:
                break
            block = info["translation"]
            code, out, err = self.run_cmd(block["export_command"])
            self.assertEqual(code, 0, err)
            self.translate_file(json.loads(out)["file"], lambda it: zh_of(it["text"]))
            code, out, err = self.run_cmd(block["import_command"])
            self.assertEqual(code, 0, err)
            info = json.loads(out)
        self.assertEqual(info["translation_pending"], 0)
        return info


class TestImportKeepsTheLanguage(Case):
    def test_the_advertised_commands_keep_a_page_in_the_other_language(self):
        rid, od, _html = self.screen_page()                         # English idea: its default page is English
        code, out, err = self.main(["page", rid, "--lang", "zh", "--json"])
        self.assertEqual(code, 0, err)
        block = json.loads(out)["translation"]
        self.assertEqual(block["lang"], "zh")
        self.assertIn("--lang zh", block["import_command"])
        code, out, err = self.run_cmd(block["export_command"])
        self.assertEqual(code, 0, err)
        exp = json.loads(out)
        self.assertIn("--lang zh", exp["import_command"])
        self.translate_file(exp["file"], lambda it: zh_of(it["text"]))
        code, out, err = self.run_cmd(exp["import_command"])
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["lang"], "zh")
        self.assertEqual(data_of((od / "page.html").read_text(encoding="utf-8"))["lang"], "zh")

    def test_an_import_without_lang_rebuilds_in_the_items_language(self):
        rid, od, _html = self.screen_page()
        f = self.home / "t.json"
        code, out, err = self.main(["page", rid, "--export-strings", str(f), "--lang", "zh", "--json"])
        self.assertEqual(code, 0, err)
        self.translate_file(str(f), lambda it: zh_of(it["text"]))
        code, out, err = self.main(["page", rid, "--import-translations", str(f), "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["lang"], "zh")
        self.assertEqual(data_of((od / "page.html").read_text(encoding="utf-8"))["lang"], "zh")


class TestKeepOriginal(unittest.TestCase):
    def item(self, text, t, kind="name", **kw):
        return {"id": "n-1", "kind": kind, "text": text, "sha": l10n.text_sha(text), "target_lang": "zh",
                "source_lang": "en", "translation": t, **kw}

    def test_a_latin_name_is_kept_or_shortened(self):
        row, problem = tr.check_item(self.item("Zscaler, Inc.", "Zscaler"))
        self.assertIsNone(problem)
        self.assertEqual(row["text"], "Zscaler")
        row, problem = tr.check_item(self.item("Zscaler, Inc.", "Zscaler, Inc."))
        self.assertIsNone(problem)                                   # kept as it is: resolved
        self.assertEqual(row["text"], "Zscaler, Inc.")
        self.assertIsNone(tr.check_item(self.item("SK hynix Inc.", "SK海力士"))[1])

    def test_keep_original_for_a_text_that_has_no_translation(self):
        text = "ASML EUV NXE:3800E."
        self.assertIn("same as the text", tr.check_item(self.item(text, text, kind="excerpt"))[1])
        row, problem = tr.check_item(self.item(text, None, kind="excerpt", keep_original=True))
        self.assertIsNone(problem)
        self.assertEqual(row["text"], text)
        # a description in the wrong language is still refused (only names may stay Latin)
        self.assertIn("not in Simplified Chinese", tr.check_item(self.item("We make pumps.", "Pumps we make.",
                                                                           kind="description"))[1])

    def test_kept_texts_end_the_rounds(self):
        def build(trmap):
            return page.build_page_data(_novice_result(), deck3(False), lang="zh", local_names=LOCAL,
                                        descriptions={"k2": "Baifeng makes aluminium."}, translations=trmap)
        stored: dict[str, str] = {}
        for _round in range(6):
            items = tr.pending_items(build(stored))
            if not items:
                break
            for it in items:     # the agent: keeps every Latin company name, translates the other texts
                it["translation"] = it["text"] if it["kind"] == "name" else zh_of(it["text"])
                row, problem = tr.check_item(it)
                self.assertIsNone(problem, it)
                stored[row["sha"]] = row["text"]
        d = build(stored)
        self.assertEqual(d["translation"]["pending"], 0)
        self.assertEqual(tr.pending_items(d), [])
        r3 = d["rows"][2]
        self.assertIsNone(r3.get("name_tr"))                        # kept: the original name, no translation
        self.assertEqual(page.display_name(r3, "zh"), "Heyuan Controls Co. Ltd.")
        self.assertIn("keep_original", tr.INSTRUCTIONS_EN)


class TestFactIsTheOriginal(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_a_translated_excerpt_is_not_tagged_as_the_fact(self):
        base = page.build_page_data(_novice_result(), deck3(False), lang="en", local_names=LOCAL)
        trmap = {i["sha"]: f"TRANSLATED {n}" for n, i in enumerate(tr.pending_items(base))}
        d = page.build_page_data(_novice_result(), deck3(False), lang="en", local_names=LOCAL, translations=trmap)
        text = dom_text(self, page.render_page(d))["text"]
        self.assertIn("TRANSLATED", text)
        self.assertNotIn("Fact\nAI translation", text)
        self.assertIn("AI translation (not the filing text)\nTRANSLATED", text)
        self.assertIn("Original\n公司为", text)                       # the verbatim original stays the fact


class TestTranslatedNamesShowTheOriginal(unittest.TestCase):
    def data(self):
        res = _novice_result()
        res["excluded_by_user"] = [{"security_id": "NYSE:EX1", "company_key": "x1", "name": "Exclusa Corp",
                                    "user_note": "no"}]
        descs = {"k2": "Baifeng makes aluminium."}
        base = page.build_page_data(res, deck3(False), lang="zh", local_names=LOCAL, descriptions=descs)
        trmap = {i["sha"]: zh_of(i["text"]) for i in tr.pending_items(base)}
        return page.build_page_data(res, deck3(False), lang="zh", local_names=LOCAL, descriptions=descs,
                                    translations=trmap)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_top_table_and_lists_carry_the_original_name(self):
        text = dom_text(self, page.render_page(self.data()))["text"]
        heyuan = zh_of("Heyuan Controls Co. Ltd.")
        top = text.split("#3", 1)[1][:200]
        self.assertIn(heyuan, top)
        self.assertIn("Heyuan Controls Co. Ltd.", top)            # the original, under the translated name
        self.assertIn(f"{zh_of('Unverio 0 Inc.')}（Unverio 0 Inc.）", text)
        self.assertIn(f"{zh_of('Exclusa Corp')}（Exclusa Corp）", text)

    def test_chat_text_marks_translated_names_and_lines(self):
        d = self.data()
        job = {"idea": "储能液冷", "result": {"run_id": "scr-x", "summary": {"listed": 3}, "page": "/tmp/p.html",
                                              "top": page.top_rows(d)}}
        text = qs.human_text(job, "done", [], "zh", None)
        self.assertIn(f"{zh_of('Heyuan Controls Co. Ltd.')}（Heyuan Controls Co. Ltd.，", text)
        self.assertIn(f"{zh_of('Baifeng makes aluminium.')}（AI 翻译）", text)


class TestBusyStore(Case):
    @contextlib.contextmanager
    def lookups_locked(self):
        real = store.session

        def session(cfg, *, read_only=False, wait_s=None, **kw):
            if read_only and wait_s == page.LOOKUP_WAIT_S:
                raise store.StoreLocked("Could not set lock on file (test)")
            return real(cfg, read_only=read_only, wait_s=wait_s, **kw)
        with mock.patch.object(store, "session", session):
            yield

    def test_export_under_a_lock_says_busy_instead_of_exporting_translated_texts(self):
        rid, _od, _html = self.screen_page()
        self.translate_all_zh(rid)
        f = self.home / "busy.json"
        with self.lookups_locked():
            code, out, err = self.main(["page", rid, "--export-strings", str(f), "--lang", "zh", "--json"])
        self.assertEqual(code, 3, out + err)
        self.assertEqual(json.loads(out)["status"], "store_busy")
        self.assertFalse(f.exists())

    def test_a_rebuild_under_a_lock_keeps_the_translated_page(self):
        rid, od, _html = self.screen_page()
        self.translate_all_zh(rid)
        before = (od / "page.html").read_text(encoding="utf-8")
        self.assertEqual(data_of(before)["translation"]["pending"], 0)
        with self.lookups_locked():
            code, out, err = self.main(["page", rid, "--lang", "zh", "--json"])
        self.assertEqual(code, 3, out + err)
        self.assertEqual(json.loads(out)["status"], "store_busy")
        self.assertEqual((od / "page.html").read_text(encoding="utf-8"), before)

    def test_a_new_page_under_a_lock_carries_the_translations_of_the_previous_page(self):
        rid, od, _html = self.screen_page()
        self.translate_all_zh(rid)
        prev = data_of((od / "page.html").read_text(encoding="utf-8"))
        result = json.loads((od / "results.json").read_text(encoding="utf-8"))
        new_dir = self.home / "newrun"
        new_dir.mkdir()
        with self.lookups_locked():
            path, data = page.write_page(self.cfg, new_dir, {**result, "output_dir": str(new_dir)}, None, lang="zh",
                                         stable=False, warn=lambda m: None)
        self.assertIsNotNone(path)
        robo, before = data["rows"][0], prev["rows"][0]
        self.assertTrue(before["name_tr"] and before["quote"]["text_tr"])
        self.assertEqual(robo.get("name_tr"), before["name_tr"])
        self.assertEqual(robo["quote"].get("text_tr"), before["quote"]["text_tr"])


class TestMoney(unittest.TestCase):
    def test_rounding_before_the_unit(self):
        self.assertEqual(l10n.usd_words(999.6e6, "en"), "$1B")
        self.assertEqual(l10n.usd_words(999.4e6, "en"), "$999M")
        self.assertEqual(l10n.usd_words(99.996e6, "zh"), "1 亿美元")
        self.assertEqual(l10n.usd_words(99.4e6, "zh"), "9940 万美元")
        self.assertEqual(l10n.usd_words(9.96e9, "en"), "$10B")


class TestCardsAndAnswerLanguage(Case):
    def test_cards_print_the_stored_translations(self):
        rid, _od, _html = self.screen_page()
        job = {"format": qs.FORMAT, "idea": IDEA, "idea_key": qs.idea_key(IDEA), "lang": "zh"}
        qs.save_job(self.cfg, job)
        self.translate_all_zh(rid)
        code, out, err = self.main(["cards", rid])
        self.assertEqual(code, 0, err)
        what = re.search(r"做什么：(.*)", out).group(1)
        self.assertIn("AI 翻译", what)
        self.assertTrue(what.startswith("中文译文"), what)
        code, out, err = self.main(["cards", rid, "--lang", "en"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("中文译文", out)                            # an English print keeps the English texts

    def test_answer_errors_in_the_ideas_language(self):
        self.screen_page()                                          # an English idea: English output by default
        code, _out, err = self.raw_main(["answer", "1a", "--deck", "missing"])
        self.assertEqual(code, 1)
        self.assertNotRegex(err, r"[一-鿿]")
        self.assertIn("deck", err)
        code, _out, err = self.raw_main(["answer", "1a", "--run", "scr-nope", "--lang", "en"])
        self.assertEqual(code, 1)
        self.assertNotRegex(err, r"[一-鿿]")
        self.assertIn("scr-nope", err)
        code, _out, err = self.raw_main(["answer", "1a", "--run", "scr-nope", "--lang", "zh"])
        self.assertIn("没有这个筛选运行", err)


if __name__ == "__main__":
    unittest.main()
