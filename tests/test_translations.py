"""Tests for the one-language page (jevscreen.l10n) and the agent translations (jevscreen.translations,
`jevscreen page --export-strings / --import-translations`).

No network, no paid calls, no model: the "agent" is this test writing Chinese / English strings into the exported
file. Invented companies only.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401

from jevscreen import l10n, page, store, translations as tr  # noqa: E402
from test_page import LOCAL, PageCase, _novice_result, deck3, dom_text  # noqa: E402
from test_screen import StoreCase  # noqa: E402


def zh_of(text: str) -> str:
    """A stand-in 'translation' into Chinese (the test is the agent)."""
    return "中文译文：" + "".join("甲乙丙丁戊己庚辛壬癸"[ord(c) % 10] for c in text if c.isalnum())[:30]


class TestWords(unittest.TestCase):
    def test_sources_forms_dates_money(self):
        self.assertEqual(l10n.source_form_words("cninfo_annual_report", "annual_report_summary", "zh"), "巨潮资讯 · 年报摘要")
        self.assertEqual(l10n.source_form_words("CNINFO", "annual_report_summary", "en"),
                         "CNINFO · annual report summary")
        self.assertEqual(l10n.source_form_words("SEC", "10-K", "zh"), "美国年报 10-K")
        self.assertEqual(l10n.source_form_words("sec_filing_text", "10-K", "en"), "SEC 10-K")
        self.assertEqual(l10n.source_form_words("EDINET", "有価証券報告書", "zh"), "日本有价证券报告书")
        self.assertEqual(l10n.source_form_words("EDINET", "有価証券報告書", "en"), "Japan annual securities report")
        self.assertEqual(l10n.source_form_words("profile", None, "zh"), "公司简介")
        self.assertEqual(l10n.source_form_words("profile", None, "en"), "company profile")
        self.assertEqual(l10n.source_form_words("bse", "annual_report", "en"), "BSE India annual report")
        self.assertEqual(l10n.source_form_words("MOPS", "股東會年報", "zh"), "台湾股东会年报")
        self.assertEqual(l10n.source_form_words("DART", "사업보고서", "en"), "Korea business report (annual report)")
        self.assertIsNone(l10n.source_form_words(None, "some_internal_id", "zh"))
        self.assertEqual(l10n.date_words("2026-04-01", "zh"), "2026年4月1日")
        self.assertEqual(l10n.date_words("2026-04-01T00:00:00", "en"), "1 Apr 2026")
        self.assertEqual((l10n.usd_words(1e9, "zh"), l10n.usd_words(2.1e9, "zh"), l10n.usd_words(3e7, "zh")),
                         ("10 亿美元", "21 亿美元", "3000 万美元"))
        self.assertEqual((l10n.usd_words(1e9, "en"), l10n.usd_words(2.14e9, "en"), l10n.usd_words(3e8, "en")),
                         ("$1B", "$2.1B", "$300M"))
        self.assertEqual(l10n.country_words("Russian Federation", "zh"), "俄罗斯")
        self.assertEqual(l10n.country_words("Russian Federation", "en"), "Russian Federation")

    def test_text_language_and_key(self):
        self.assertEqual([l10n.text_lang(t) for t in ("Qinglan Thermal Tech Co. Ltd.", "青澜科技", "台積電",
                                                         "公司主要产品为 IGBT 模块。", "トヨタ自動車", "株式会社日立",
                                                         "삼성전자", "3M", "")],
                         ["en", "zh", "zh", "zh", "ja", "ja", "ko", None, None])
        self.assertTrue(l10n.is_foreign("We make pumps.", "zh"))
        self.assertFalse(l10n.is_foreign("We make pumps.", "en"))
        self.assertTrue(l10n.is_foreign("ポンプを製造する。", "zh"))
        self.assertEqual(l10n.text_sha("We  make\npumps."), l10n.text_sha("We make pumps."))


class TestPageData(unittest.TestCase):
    def data(self, lang="zh", translations=None, result=None):
        return page.build_page_data(result or _novice_result(), deck3(False), lang=lang, local_names=LOCAL,
                                    descriptions={"k2": "Baifeng makes aluminium."}, translations=translations)

    def test_only_one_language_of_strings_is_embedded(self):
        d = self.data("zh")
        self.assertEqual(set(d["strings"]), {"zh"})
        self.assertEqual(set(self.data("en")["strings"]), {"en"})
        self.assertNotIn("toggle", page.STRINGS["zh"])

    def test_foreign_texts_are_exported_most_important_first(self):
        d = self.data("zh")
        items = tr.pending_items(d)
        texts = [i["text"] for i in items]
        # the top rows first: row 2's English profile line, row 3's English name (no official Chinese name) ...
        self.assertEqual(texts[:2], ["Baifeng makes aluminium.", "Heyuan Controls Co. Ltd."])
        # cards are not on the page (a CLI tool for the user's AI): their texts are not exported nor pending
        self.assertNotIn("We make things.", texts)
        self.assertIn("Unverio 0 Inc.", texts)                        # the unverified names come last
        self.assertEqual(texts[-1], "Unverio 4 Inc.")
        self.assertNotIn("青澜科技", texts)                           # Chinese already
        self.assertEqual(len(texts), len(set(texts)))                 # one item per distinct text
        for i in items:
            self.assertEqual(set(i), {"id", "kind", "text", "source_lang", "target_lang", "sha", "context",
                                      "translation"})
            self.assertEqual(i["sha"], l10n.text_sha(i["text"]))
            self.assertEqual(i["target_lang"], "zh")
            self.assertIn(i["kind"], tr.KINDS)
        self.assertEqual({i["kind"] for i in items}, {"name", "description"})     # the only English excerpt is a card's
        self.assertEqual(d["translation"]["pending"], len(items))

    def test_a_stored_card_translation_still_reaches_the_card(self):
        """Card texts are neither exported nor counted as pending, but a translation already in the store (the same
        text on a row, or an earlier page) still fills the card for the `cards` printout."""
        sha = l10n.text_sha("We make things.")
        d = self.data("zh", translations={sha: "我们制造东西。"})
        quotes = [c.get("quote") or {} for c in d["cards"]]
        self.assertIn("我们制造东西。", [q.get("text_tr") for q in quotes])
        self.assertEqual(d["translation"]["pending"], len(tr.pending_items(d)))

    def test_an_english_page_exports_the_chinese_excerpts_not_the_names(self):
        d = self.data("en")
        items = tr.pending_items(d)
        self.assertTrue(items)
        self.assertEqual({i["kind"] for i in items}, {"excerpt"})
        self.assertIn("公司为电化学储能系统提供液冷温控解决方案，产品用于储能电站。", [i["text"] for i in items])
        self.assertTrue(all(i["source_lang"] == "zh" and i["target_lang"] == "en" for i in items))

    def test_a_translation_is_shown_first_with_the_original_behind_a_toggle(self):
        base = self.data("zh")
        trmap = {i["sha"]: zh_of(i["text"]) for i in tr.pending_items(base)}
        d = self.data("zh", translations=trmap)
        self.assertEqual(d["translation"]["pending"], 0)
        r2, r3 = d["rows"][1], d["rows"][2]
        self.assertTrue(r2["one_line_x"])
        self.assertEqual(r2["one_line_tr"], zh_of("Baifeng makes aluminium."))
        self.assertEqual(r2["one_line"], "Baifeng makes aluminium.")        # the original stays the fact
        self.assertEqual(page.display_name(r3, "zh"), zh_of("Heyuan Controls Co. Ltd."))
        top = page.top_rows(d)
        self.assertEqual(top[1]["one_line"], zh_of("Baifeng makes aluminium."))
        self.assertEqual(top[1]["one_line_original"], "Baifeng makes aluminium.")
        self.assertTrue(top[2]["name_translated"])
        text = page.render_text(d)
        self.assertIn(f"做什么：{zh_of('Baifeng makes aluminium.')}（AI 翻译）", text)
        self.assertIn(f"#3 {zh_of('Heyuan Controls Co. Ltd.')}（Heyuan Controls Co. Ltd.）", text)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_page_script_tags_translations_and_untranslated_originals(self):
        base = self.data("zh")
        items = tr.pending_items(base)
        half = {i["sha"]: zh_of(i["text"]) for i in items if i["text"] != "Baifeng makes aluminium."}
        text = dom_text(self, page.render_page(self.data("zh", translations=half)))["text"]
        self.assertIn("AI 翻译", text)
        self.assertIn("看原文", text)
        self.assertIn(zh_of("Heyuan Controls Co. Ltd."), text)
        self.assertIn("原文（未翻译）", text)                         # the profile line without a translation
        self.assertIn("Baifeng makes aluminium.", text)
        self.assertNotIn("We make things.", text)                     # cards are not on the page
        # the hidden original is there for the toggle, labelled 原文
        full = dom_text(self, page.render_page(self.data("zh", translations={i["sha"]: zh_of(i["text"])
                                                                              for i in items})))["text"]
        self.assertIn("原文\nBaifeng makes aluminium.", full)


class TestImportChecks(unittest.TestCase):
    def item(self, text="We make pumps.", t="我们生产水泵。", **kw):
        return {"id": "e-1", "kind": "excerpt", "text": text, "sha": l10n.text_sha(text), "target_lang": "zh",
                "source_lang": "en", "translation": t, **kw}

    def test_checks(self):
        row, problem = tr.check_item(self.item())
        self.assertIsNone(problem)
        self.assertEqual((row["text"], row["target_lang"]), ("我们生产水泵。", "zh"))
        self.assertEqual(tr.check_item(self.item(t=None)), (None, None))            # left out: skipped
        self.assertIn("not in Simplified Chinese", tr.check_item(self.item(t="We make pumps, really."))[1])
        self.assertIn("does not match", tr.check_item(self.item(sha="0" * 64))[1])
        self.assertIn("markup", tr.check_item(self.item(t="<b>我们生产水泵</b>"))[1])
        self.assertIn("too long", tr.check_item(self.item(t="水" * 500))[1])
        self.assertIn("zh or en", tr.check_item(self.item(target_lang="fr"))[1])
        en = self.item(text="公司生产水泵。", t="The company makes pumps.", target_lang="en")
        self.assertIsNone(tr.check_item(en)[1])
        self.assertIn("same as the text", tr.check_item(self.item(text="水泵", t="水泵"))[1])

    def test_read_file_accepts_a_list_or_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.json"
            p.write_text(json.dumps([self.item()]), encoding="utf-8")
            self.assertEqual(len(tr.read_file(p)), 1)
            p.write_text(json.dumps({"items": [self.item()]}), encoding="utf-8")
            self.assertEqual(len(tr.read_file(p)), 1)
            p.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                tr.read_file(p)


class TestNames(StoreCase):
    def test_share_classes_are_dropped(self):
        for raw, want in (("Foo Industries Co., Ltd. Class B", "Foo Industries Co., Ltd."),
                          ("Quillon Group Holding Ltd - ADR", "Quillon Group Holding Ltd"),
                          ("Zorvan, Inc. Class A ADS", "Zorvan, Inc."), ("Quux Corp (Class A)", "Quux Corp"),
                          ("Classic Brands Inc.", "Classic Brands Inc."), ("Series Co", "Series Co")):
            self.assertEqual(page._clean_name(raw), want)

    def test_taiwan_short_names_from_the_stored_mops_basic_data(self):
        from jevscreen.sources import mops
        raw = self.home / "basic-9905.json"
        raw.write_text(json.dumps({"code": 200, "result": {
            "companyName": {"isHidden": False, "value": "福爾摩沙冷卻工業股份有限公司"},
            "companyAbbreviation": {"isHidden": False, "value": "福冷"},
            "mainBusiness": {"isHidden": False, "value": "冷卻液分配裝置。"}}}, ensure_ascii=False), encoding="utf-8")
        raw2 = self.home / "basic-9906.json"
        raw2.write_text(json.dumps({"code": 200, "result": {
            "companyName": {"isHidden": False, "value": "港景氣候股份有限公司"}}}, ensure_ascii=False), encoding="utf-8")
        manifest = self.home / "aux.json"
        manifest.write_text(json.dumps([{"co_id": "9905", "kind": "basic-9905", "raw_path": str(raw)},
                                        {"co_id": "9906", "kind": "basic-9906", "raw_path": str(raw2)}]),
                            encoding="utf-8")
        with store.session(self.cfg) as con:
            snap = store.record_snapshot(con, source_id=mops.BASIC_SOURCE_ID, kind="aux_batch", request=None,
                                         raw_path=str(manifest), raw_sha256=None, raw_bytes=None, rows=2,
                                         duration_s=0.0)
            for sid, code in (("TWSE:9905", "9905"), ("TPEX:9906", "9906")):
                con.execute("INSERT INTO identifiers VALUES (?, ?, ?, 'symbol', ?)", [sid, mops.ID_TYPE, code, snap])
        with store.session(self.cfg, read_only=True) as con:
            got = page._tw_names(con, ["TWSE:9905", "TPEX:9906", "NYSE:ROBO"])
        self.assertEqual(got, {"TWSE:9905": "福冷", "TPEX:9906": "港景氣候"})
        self.assertEqual(mops.basic_names({"result": {"companyAbbreviation": {"isHidden": True, "value": "x"}}}),
                         (None, None))


class TestStoreTable(StoreCase):
    def test_table_import_replace_and_lookup(self):
        with store.session(self.cfg) as con:
            it = TestImportChecks.item(TestImportChecks())
            s = tr.import_items(self.cfg, con, [it, {**it, "id": "bad", "translation": "no"}])
            self.assertEqual((s["imported"], len(s["rejected"])), (1, 1))
            tr.import_items(self.cfg, con, [{**it, "translation": "我们制造泵。"}])      # a correction replaces it
            got = tr.lookup(con, [it["sha"], "f" * 64], "zh")
            self.assertEqual(got, {it["sha"]: "我们制造泵。"})
            self.assertEqual(con.execute("SELECT provenance, kind, source_lang FROM translations").fetchall(),
                             [("agent translation", "excerpt", "en")])
            self.assertEqual(tr.lookup(con, [it["sha"]], "en"), {})


class TestPageCommand(PageCase):
    """`jevscreen page --export-strings / --import-translations` on a screened run (English idea, English page:
    the fixtures are English, so the test switches the page to Chinese to have texts to translate)."""

    def test_export_translate_import_and_reuse(self):
        rid, od, _html = self.screen_page()
        f = self.home / "tr.json"
        code, out, err = self.main(["page", rid, "--export-strings", str(f), "--lang", "zh", "--json"])
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual((info["status"], info["lang"], info["file"]), ("ok", "zh", str(f)))
        self.assertGreater(info["items"], 0)
        self.assertEqual(info["items"] + info["remaining_after"], info["pending_total"])
        self.assertIn("--import-translations", info["import_command"])
        self.assertIn("Simplified Chinese", info["instructions_en"])
        items = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(len(items), info["items"])
        # the page itself was not changed by the export (still English)
        self.assertEqual(json.loads(re.search(r'id="data">(.*?)</script>', (od / "page.html").read_text(),
                                              re.S).group(1))["lang"], "en")
        # a small batch
        code, out, _e = self.main(["page", rid, "--export-strings", str(self.home / "b.json"), "--lang", "zh",
                                   "--batch", "1", "--json"])
        self.assertEqual(json.loads(out)["items"], 1)
        # the agent translates all but one, and gets one wrong
        for i, it in enumerate(items):
            it["translation"] = zh_of(it["text"]) if i else None
        if len(items) > 2:
            items[1]["translation"] = "not Chinese at all"
        f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        code, out, err = self.main(["page", rid, "--import-translations", str(f), "--lang", "zh", "--json"])
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["action"], "import_translations")
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["status"], "partial" if len(items) > 2 else "ok")
        self.assertEqual(res["translation_pending"], 1 + (1 if len(items) > 2 else 0))
        self.assertTrue(res["translation"]["export_command"])
        html = (od / "page.html").read_text(encoding="utf-8")
        data = json.loads(re.search(r'id="data">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(data["lang"], "zh")
        self.assertIn(zh_of(items[-1]["text"]), html)
        # the original evidence text is untouched in the store
        with store.session(self.cfg, read_only=True) as con:
            n = con.execute("SELECT count(*) FROM translations WHERE target_lang = 'zh'").fetchone()[0]
            self.assertEqual(n, len(items) - 1 - (1 if len(items) > 2 else 0))
            descs = [t for (t,) in con.execute("SELECT text FROM descriptions").fetchall()]
        self.assertFalse(any(t.startswith("中文译文") for t in descs))
        # reuse: rebuilding the page (any later run showing the same text) finds the stored translations
        code, out, _e = self.main(["page", rid, "--lang", "zh", "--json"])
        self.assertEqual(json.loads(out)["translation_pending"], res["translation_pending"])
        # an unreadable file: exit 1, nothing changed
        (self.home / "bad.json").write_text("[1", encoding="utf-8")
        code, out, _e = self.main(["page", rid, "--import-translations", str(self.home / "bad.json"), "--json"])
        self.assertEqual((code, json.loads(out)["status"]), (1, "error"))


if __name__ == "__main__":
    unittest.main()
